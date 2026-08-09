"""
Roadmap #5 (early look) - Double DQN, end-to-end entry AND exit in one policy.

Everything before this file decomposed the problem into two stages: an
XGBoost model scores next-day return (entry signal), then a separate,
hand-tuned rule decides the exit (fixed hold, TP/SL - see
strategy_variants.py). This file collapses that into one agent: at each day,
for each ticker, it picks a single action - "want to be long" or "want to be
flat" - and learns both when to get in AND when to get out from reward
signal alone, no hand-tuned exit rule.

Per the user's explicit call: proceed now on the ~220-254 days already
backfilled in neobdm.db rather than waiting for the 60-live-day bar
walk_forward_backtest.py's docstring describes. That bar exists for a real,
already-observed reason (that docstring: Sharpe went from clearly negative
at 19-57 days to +0.94 at 218 days - small samples here have already been
shown to flip sign), and DDQN is MORE sample-hungry and overfit-prone than
XGBoost, not less - ~46 tickers x ~220 days is a small dataset for RL. Read
every result from this file with that discount in mind, same as every other
script in this repo.

State (12-dim, z-scored on the training split only to avoid leakage):
  - The same 8 features walk_forward_backtest.FEATURES uses (broker_flow
    aggregates + momentum_1d + volume_ratio) - all already validated
    no-leakage in test_pipeline.py.
  - position (0/1): the agent's own current holding, known at decision time.
  - days_in_position: consecutive days held so far.
  - unrealized_return: cumulative return since entry (0 if flat).
  - at_ara / at_arb are NOT included - the agent can't see today's limit
    lock before acting on it (that would be lookahead); it only feels the
    consequence via the reward/forced-hold, same as a real trader would.

Action space: Discrete(2). 0 = "flat", 1 = "long". This one action does
double duty as entry (0->1) and exit (1->0) - there's no separate exit
action, which is the actual "end-to-end" part of the ask.

Reward, reusing validated pieces from elsewhere in this repo rather than
inventing new market mechanics:
  - Daily mark-to-market: reward_t = position_after_fill_{t-1} * daily_return_t,
    where daily_return_t is the SAME next-day target walk_forward_backtest.py's
    panel already computes (no separate entry-price bookkeeping needed - this
    telescopes to the correct total P&L over any holding length).
  - ARA/ARB fill realism, copied from ara_arb_simulation.py's already-verified
    thresholds (tiered ARA: <Rp200 +35%, Rp200-5000 +25%, >Rp5000 +20%; flat
    ARB -15%): an entry (0->1) requested on a day stuck at ARA doesn't fill;
    an exit (1->0) requested on a day stuck at ARB doesn't fill. Position
    just stays put that step, which has the same effect as ara_arb_simulation
    .py's "roll forward" logic without needing separate bookkeeping.
  - Transaction cost: NOT modeled anywhere else in this repo (strategy_variants
    .py flags this as a gap). Added here explicitly: 0.15% one-way (~0.3%
    round trip), a conservative placeholder for typical IDX retail brokerage
    fees - charged whenever the filled position changes.
  - Loss-aversion shaping (x1.5 on negative daily P&L only): this is the one
    piece not lifted from elsewhere in the repo - it's a standard, simple RL
    reward-shaping technique for encoding "cut losses early, let winners run"
    (the single most consistent piece of advice from actual trader practice -
    see PR discussion) without hardcoding a fixed stop-loss %, so the agent
    can learn its OWN adaptive exit threshold instead of a rule we picked.

Double DQN specifics: decouples action selection (online network) from
value estimation (target network) to reduce the Q-value overestimation bias
plain DQN is known for - standard technique, see R-DDQN / SRDDQN 2024
literature on RL for trading for context on why this matters here.

Same anti-data-snooping discipline as strategy_variants.py: chronological
70/30 search/holdout split by date, holdout never touched during training.

2026-08-09 result (45 tickers, 2025-08-04 to 2026-07-06, 40 epochs): clean
overfitting, exactly the failure mode flagged above before this was run.
Search-period (train) trade-level Sharpe 1.68 (n=1241, hit_rate 48.4%)
looks promising - then collapses to Sharpe -0.65 (n=569, hit_rate 36.9%)
on the untouched holdout. Daily-level Sharpe (2.63 search vs -0.43 holdout)
shows the same collapse; note the daily-level hit_rate numbers (20%/18%)
are NOT comparable to the trade-level ones - flat (no-position) days score
as a 0.0 return there, which counts as "not a win," dragging that stat
down artificially, so read the trades:{} block for hit rate, not daily:{}.
Not evidence the end-to-end approach is a dead end, but on this data size
it has not found anything close to a robust policy - it's the same
small-sample instability walk_forward_backtest.py's docstring documents
for XGBoost at 19-57 days, evidently worse here since DDQN needs more
data to generalize, not less. Rerun as live data accumulates (see
DailyScraper's price_history gap, currently blocking that from happening -
this needs fixing before rerunning this script is worth much).
"""

import random
import sqlite3
from collections import deque

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

from walk_forward_backtest import FEATURES, DB_PATH, _broker_day_aggregates, _broker_correlation_1d
from ara_arb_simulation import annotate_limits

TRANSACTION_COST = 0.0015   # one-way; ~0.3% round trip, conservative IDX retail placeholder
LOSS_AVERSION = 1.5         # multiplier on negative daily P&L only ("cut losses" shaping)
GAMMA = 0.97
STATE_EXTRA = ["position", "days_in_position", "unrealized_return"]


def build_episode_frame(conn):
    """Like walk_forward_backtest.build_panel(), but keeps daily_return and
    ARA/ARB flags per ticker/date instead of collapsing to an aggregated
    Sharpe - the DDQN environment needs to step through days in order."""
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    px = pd.read_sql("SELECT date, ticker, open, high, low, close, volume FROM price_history", conn)

    agg = _broker_day_aggregates(bf)
    corr = _broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")

    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["prev_close"] = px.groupby("ticker")["close"].shift(1)
    px["momentum_1d"] = (px["close"] - px["prev_close"]) / px["prev_close"]
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    px["daily_return"] = px["momentum_1d"]  # today's close-over-close return, same quantity walk_forward_backtest calls "target" one day earlier
    px = annotate_limits(px)  # adds at_ara / at_arb, using the same tiered/flat bounds as ara_arb_simulation.py

    panel = agg.merge(
        px[["ticker", "date", "momentum_1d", "volume_ratio", "daily_return", "at_ara", "at_arb"]],
        on=["ticker", "date"], how="inner",
    )
    panel = panel.dropna(subset=["daily_return"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    return panel


def split_search_holdout(panel, search_frac=0.7):
    dates = sorted(panel["date"].unique())
    split_idx = int(len(dates) * search_frac)
    search_dates, holdout_dates = set(dates[:split_idx]), set(dates[split_idx:])
    return panel[panel["date"].isin(search_dates)].copy(), panel[panel["date"].isin(holdout_dates)].copy()


def fit_normalizer(train_df):
    mean = train_df[FEATURES].mean()
    std = train_df[FEATURES].replace([np.inf, -np.inf], np.nan).std().replace(0, 1)
    return mean, std


def normalize_features(df, mean, std):
    z = (df[FEATURES] - mean) / std
    return z.clip(-5, 5).fillna(0.0)


class TickerEnv:
    """One episode = one ticker's chronological date sequence. Feature values
    are pre-normalized; only the sequential position/reward bookkeeping
    happens here."""

    def __init__(self, feats_z, daily_return, at_ara, at_arb):
        self.feats_z = feats_z          # (T, len(FEATURES)) already z-scored
        self.daily_return = daily_return  # (T,)
        self.at_ara = at_ara
        self.at_arb = at_arb
        self.T = len(daily_return)
        self.reset()

    def reset(self):
        self.t = 0
        self.position = 0
        self.days_in_position = 0
        self.unrealized_return = 0.0
        return self._state()

    def _state(self):
        extra = np.array([self.position, self.days_in_position, self.unrealized_return], dtype=np.float32)
        return np.concatenate([self.feats_z[self.t], extra]).astype(np.float32)

    def step(self, action):
        """Execute `action` (desired position) at close of day self.t,
        subject to ARA/ARB fill locks, then realize day t+1's return under
        the resulting position. Returns (next_state, reward, done)."""
        prev_position = self.position
        desired = int(action)

        filled_position = prev_position
        if desired == 1 and prev_position == 0 and not self.at_ara[self.t]:
            filled_position = 1
        elif desired == 0 and prev_position == 1 and not self.at_arb[self.t]:
            filled_position = 0
        # else: either no change requested, or the fill was locked out -> stays put

        cost = TRANSACTION_COST if filled_position != prev_position else 0.0

        self.t += 1
        done = self.t >= self.T - 1
        ret = float(self.daily_return[self.t]) if not done else 0.0
        pnl = filled_position * ret
        if pnl < 0:
            pnl *= LOSS_AVERSION
        reward = pnl - cost

        if filled_position == 1:
            self.days_in_position += 1
            self.unrealized_return = (1 + self.unrealized_return) * (1 + (ret if not done else 0.0)) - 1
        else:
            self.days_in_position = 0
            self.unrealized_return = 0.0
        self.position = filled_position

        return self._state(), reward, done, dict(realized_return=ret if filled_position else 0.0)


def make_envs(panel):
    envs = []
    for ticker, g in panel.groupby("ticker"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < 10:
            continue
        feats = g[FEATURES].to_numpy(dtype=np.float32)
        envs.append(TickerEnv(
            feats, g["daily_return"].to_numpy(dtype=np.float32),
            g["at_ara"].to_numpy(dtype=bool), g["at_arb"].to_numpy(dtype=bool),
        ))
    return envs


class QNet(nn.Module):
    def __init__(self, state_dim, n_actions=2):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, 64), nn.ReLU(),
            nn.Linear(64, 64), nn.ReLU(),
            nn.Linear(64, n_actions),
        )

    def forward(self, x):
        return self.net(x)


class ReplayBuffer:
    def __init__(self, capacity=50_000):
        self.buf = deque(maxlen=capacity)

    def push(self, *transition):
        self.buf.append(transition)

    def sample(self, batch_size):
        batch = random.sample(self.buf, batch_size)
        s, a, r, s2, d = map(np.array, zip(*batch))
        return (torch.tensor(s, dtype=torch.float32), torch.tensor(a, dtype=torch.int64),
                torch.tensor(r, dtype=torch.float32), torch.tensor(s2, dtype=torch.float32),
                torch.tensor(d, dtype=torch.float32))


def train_ddqn(train_envs, state_dim, n_epochs=40, batch_size=64, lr=1e-3,
               target_sync_every=200, seed=0):
    random.seed(seed)
    torch.manual_seed(seed)
    np.random.seed(seed)

    online = QNet(state_dim)
    target = QNet(state_dim)
    target.load_state_dict(online.state_dict())
    opt = torch.optim.Adam(online.parameters(), lr=lr)
    buf = ReplayBuffer()

    eps_start, eps_end = 1.0, 0.05
    step_count = 0
    for epoch in range(n_epochs):
        eps = eps_end + (eps_start - eps_end) * np.exp(-3.0 * epoch / n_epochs)
        for env in train_envs:
            s = env.reset()
            done = False
            while not done:
                if random.random() < eps:
                    a = random.randint(0, 1)
                else:
                    with torch.no_grad():
                        a = int(online(torch.tensor(s, dtype=torch.float32)).argmax().item())
                s2, r, done, _ = env.step(a)
                buf.push(s, a, r, s2, float(done))
                s = s2
                step_count += 1

                if len(buf.buf) >= batch_size:
                    bs, ba, br, bs2, bd = buf.sample(batch_size)
                    with torch.no_grad():
                        next_actions = online(bs2).argmax(dim=1)
                        next_q = target(bs2).gather(1, next_actions.unsqueeze(1)).squeeze(1)
                        y = br + GAMMA * next_q * (1 - bd)
                    q = online(bs).gather(1, ba.unsqueeze(1)).squeeze(1)
                    loss = nn.functional.smooth_l1_loss(q, y)
                    opt.zero_grad()
                    loss.backward()
                    opt.step()

                if step_count % target_sync_every == 0:
                    target.load_state_dict(online.state_dict())
    return online


def sharpe_from_returns(returns):
    returns = np.array(returns)
    n = len(returns)
    if n == 0:
        return dict(sharpe=np.nan, n_trades=0, hit_rate=np.nan)
    std = returns.std()
    sharpe = (returns.mean() / std * np.sqrt(252)) if std > 0 else np.nan
    return dict(sharpe=sharpe, n_trades=n, hit_rate=(returns > 0).mean())


def evaluate_policy(net, envs):
    """Greedy (epsilon=0) rollout. Reports per-day strategy returns (for
    Sharpe, comparable to sharpe_stats() elsewhere in this repo) and
    round-trip trade returns (entry to exit) separately."""
    daily_returns = []
    trade_returns = []
    n_entries = n_blocked_entries = n_blocked_exits = 0

    for env in envs:
        s = env.reset()
        done = False
        open_trade_return = None
        while not done:
            with torch.no_grad():
                a = int(net(torch.tensor(s, dtype=torch.float32)).argmax().item())
            prev_position = env.position
            desired = a
            if desired == 1 and prev_position == 0 and env.at_ara[env.t]:
                n_blocked_entries += 1
            if desired == 0 and prev_position == 1 and env.at_arb[env.t]:
                n_blocked_exits += 1

            s2, r, done, info = env.step(a)
            daily_returns.append(env.position * info["realized_return"] if env.position else 0.0)

            if prev_position == 0 and env.position == 1:
                n_entries += 1
                open_trade_return = 0.0
            if env.position == 1 and open_trade_return is not None:
                open_trade_return = (1 + open_trade_return) * (1 + info["realized_return"]) - 1
            if prev_position == 1 and env.position == 0 and open_trade_return is not None:
                trade_returns.append(open_trade_return)
                open_trade_return = None
            s = s2
        if open_trade_return is not None:
            trade_returns.append(open_trade_return)  # still open at episode end -> close it out for scoring

    return dict(
        daily=sharpe_from_returns(daily_returns),
        trades=sharpe_from_returns(trade_returns),
        n_entries=n_entries, n_blocked_entries=n_blocked_entries, n_blocked_exits=n_blocked_exits,
    )


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_episode_frame(conn)
    conn.close()

    print(f"Panel: {len(panel)} rows, {panel['ticker'].nunique()} tickers, "
          f"{panel['date'].min()} to {panel['date'].max()}")

    search_df, holdout_df = split_search_holdout(panel)
    mean, std = fit_normalizer(search_df)

    search_df = search_df.copy()
    holdout_df = holdout_df.copy()
    search_df[FEATURES] = normalize_features(search_df, mean, std)
    holdout_df[FEATURES] = normalize_features(holdout_df, mean, std)

    train_envs = make_envs(search_df)
    holdout_envs = make_envs(holdout_df)
    print(f"Train episodes: {len(train_envs)}  Holdout episodes: {len(holdout_envs)}")

    state_dim = len(FEATURES) + len(STATE_EXTRA)
    net = train_ddqn(train_envs, state_dim, n_epochs=40)

    search_result = evaluate_policy(net, train_envs)
    holdout_result = evaluate_policy(net, holdout_envs)

    print("\n=== SEARCH (train) period, greedy policy ===")
    print(f"  daily : sharpe={search_result['daily']['sharpe']:.2f} n={search_result['daily']['n_trades']} "
          f"hit_rate={search_result['daily']['hit_rate']:.2%}")
    print(f"  trades: sharpe={search_result['trades']['sharpe']:.2f} n={search_result['trades']['n_trades']} "
          f"hit_rate={search_result['trades']['hit_rate']:.2%}")
    print(f"  entries={search_result['n_entries']} blocked_entries(ARA)={search_result['n_blocked_entries']} "
          f"blocked_exits(ARB)={search_result['n_blocked_exits']}")

    print("\n=== HOLDOUT period, greedy policy (never touched during training) ===")
    print(f"  daily : sharpe={holdout_result['daily']['sharpe']:.2f} n={holdout_result['daily']['n_trades']} "
          f"hit_rate={holdout_result['daily']['hit_rate']:.2%}")
    print(f"  trades: sharpe={holdout_result['trades']['sharpe']:.2f} n={holdout_result['trades']['n_trades']} "
          f"hit_rate={holdout_result['trades']['hit_rate']:.2%}")
    print(f"  entries={holdout_result['n_entries']} blocked_entries(ARA)={holdout_result['n_blocked_entries']} "
          f"blocked_exits(ARB)={holdout_result['n_blocked_exits']}")
