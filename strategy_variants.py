"""
================================================================================
VOID — every Sharpe figure quoted below (0.94, 0.81, 5.36, 6.95, and the
"Sharpe > 1.5" bar) was produced by `mean/std * sqrt(252)` applied to per-trade,
cross-sectionally overlapping returns. Both halves of that are wrong and they
compound. The numbers are kept, not deleted, so nobody re-derives them and
believes them a second time. See signal_metrics.py for what replaced them.

Stage 3 (2026-08-23) first honest read, same 249-day panel:
  IC -0.025 | top-decile hit 43.5% vs base 42.3% (edge +1.2pp)
  threshold rule hit 42.4% (edge +0.1pp vs base)
i.e. no directional information, exactly as the base-rate finding predicted.
Return-based edges from this run are NOT usable yet: 82 contaminated rows drag
the panel mean from +0.32% to +14.38%, so mean-based figures stay meaningless
until build_panel() sources price_audit.clean_panel() (HANDOFF stage 1).
================================================================================

Strategy (not feature) variation: same trained model/entry-signal as
walk_forward_backtest.py, but tests different EXIT mechanics (entry
threshold, holding period, take-profit/stop-loss using actual daily
high/low) to see which combination gives the best Sharpe. Everything up to
this point only varied features while keeping the crude "1-day fixed exit,
0.5% threshold, no TP/SL" strategy constant - this tests the strategy
itself.

Includes a search/holdout split (search_dates = first 70% of predicted
dates, holdout = last 30%, never touched during the grid search) specifically
to guard against picking a variant that just curve-fit the historical
window - trying 11 variants and reporting only the best without an
out-of-sample check would be classic data-snooping bias.

2026-07-07 result: "Tighter entry >1.0%, 3d hold, TP=5%/SL=3%" won the search
period (Sharpe 5.36, n=1317) and held up on the untouched holdout (Sharpe
6.95, n=168, hit_rate 58.9%) - it did NOT collapse out-of-sample the way an
overfit result typically would. See run_strategy_search() for the full
ranked results in both periods.

IMPORTANT CAVEATS before reading too much into this:
  - NO transaction costs, spread, or slippage modeled. Sharpe 5-7 is far
    outside what's normal for a real strategy (2-3 is excellent for a
    professional fund) - at this trade frequency (hundreds of trades),
    IDX brokerage fees + spread could materially erode or eliminate the
    edge. Model this before taking it seriously.
  - IDX has daily price limit rules (ARA/ARB) not simulated here - a stock
    can gap past a TP/SL level or get stuck limit-up/limit-down, which
    would change these fills in practice.
  - The underlying entry signal is still the same model established
    elsewhere (walk_forward_backtest.py, feature_ablation.py) to be
    momentum/volume-driven, not broker-flow-driven. This result is about
    managing exits on THAT signal better (cutting losses early via SL,
    locking gains via TP) - it is not evidence the broker-accumulation
    thesis works.
  - One single 70/30 split, not multiple splits - directionally reassuring,
    not definitive. A more rigorous check would repeat this with several
    different split points.

Explicit user requirement: hold 1-7 trading days max (see MAX_HOLD_DAYS
below). Every VARIANTS entry already satisfies this (hold_days is 1, 3, or
5), asserted at import time so a future edit can't silently add a longer
hold without that assertion failing loudly.
"""

import sqlite3
import numpy as np
import pandas as pd
from xgboost import XGBRegressor
from walk_forward_backtest import (
    build_panel, FEATURES, XGB_PARAMS, DB_PATH, make_walk_forward_splits,
)
from price_audit import clean_panel
from signal_metrics import trade_stats

MAX_HOLD_DAYS = 7  # explicit user ceiling

VARIANTS = [
    ("Baseline: >0.5%, 1d hold, no TP/SL", 0.005, 1, None, None),
    ("Looser entry >0.3%, 1d hold, no TP/SL", 0.003, 1, None, None),
    ("Tighter entry >1.0%, 1d hold, no TP/SL", 0.010, 1, None, None),
    ("Tighter entry >2.0%, 1d hold, no TP/SL", 0.020, 1, None, None),
    ("Same entry, 3d hold, no TP/SL", 0.005, 3, None, None),
    ("Same entry, 5d hold, no TP/SL", 0.005, 5, None, None),
    ("Same entry, 3d hold, TP=3%/SL=3%", 0.005, 3, 0.03, 0.03),
    ("Same entry, 5d hold, TP=5%/SL=3%", 0.005, 5, 0.05, 0.03),
    ("Same entry, 5d hold, TP=3%/SL=5%", 0.005, 5, 0.03, 0.05),
    ("Same entry, 3d hold, TP=2%/SL=2% (tight)", 0.005, 3, 0.02, 0.02),
    ("Tighter entry >1.0%, 3d hold, TP=5%/SL=3%", 0.010, 3, 0.05, 0.03),
]
assert all(1 <= v[2] <= MAX_HOLD_DAYS for v in VARIANTS), "a variant's hold_days is outside the 1-7 day requirement"


def get_walk_forward_predictions(panel, horizon=1, embargo=0):
    """Same expanding-window CV as walk_forward_backtest.py, but keeps
    ticker/date on every test-set prediction instead of just aggregating
    stats, so different exit mechanics can be simulated on the same
    entry signal."""
    # One shared split helper, not a fourth divergent copy. This file used to
    # re-implement walk_forward_backtest's expanding window and 0.8 positional
    # split independently, so a fix in one never reached the other.
    splits, _ = make_walk_forward_splits(
        sorted(panel["date"].unique()), horizon=horizon, embargo=embargo,
    )

    pred_rows = []
    for sp in splits:
        test_dates = sp["test"]
        fit_df = panel[panel["date"].isin(sp["fit"])]
        eval_df = panel[panel["date"].isin(sp["eval"])]
        test_df = panel[panel["date"].isin(test_dates)].copy()
        model = XGBRegressor(**XGB_PARAMS)
        model.fit(fit_df[FEATURES], fit_df["target"],
                  eval_set=[(eval_df[FEATURES], eval_df["target"])], verbose=False)
        test_df["pred"] = model.predict(test_df[FEATURES])
        # `target` rides along because score_all() needs it for the base rate
        # every hit rate is read against. Dropping it here is what made
        # run_ml_reports.py die with KeyError: 'target' every night from
        # 2026-08-23 (the commit that started reporting base rates) onward.
        pred_rows.append(test_df[["ticker", "date", "pred", "target"]])
    return pd.concat(pred_rows, ignore_index=True)


def _index_price_history(px):
    px_by_ticker = {t: g.sort_values("date").reset_index(drop=True) for t, g in px.groupby("ticker")}
    date_idx_by_ticker = {t: {d: i for i, d in enumerate(g["date"])} for t, g in px_by_ticker.items()}
    return px_by_ticker, date_idx_by_ticker


def simulate_trade(px_by_ticker, date_idx_by_ticker, ticker, entry_date, hold_days, tp_pct, sl_pct):
    """Decision at EOD(entry_date); enter at the NEXT session's OPEN.

    Entry used to be entry_date's close, which is not executable: the decision
    consumes that same close and the post-session broker summary. The anchor is
    now open(T+1). That is a PRICE ANCHOR, not a guaranteed fill — a locked
    ARA/ARB open or a thin name may not absorb the order at that price; see
    ara_arb_simulation.annotate_limits(). Fillability is a separate execution-
    quality layer and is deliberately not modelled here.

    hold_days counts SESSIONS FROM ENTRY, not sessions after entry:
    hold_days=1 means the position opens AND is timed-exited within session
    T+1 itself (TP/SL checked against T+1's own high/low, timed exit at T+1's
    close); hold_days=2 extends that through T+2, etc. An earlier version of
    this function incremented past the entry session before starting the
    hold loop, so hold_days=1 evaluated T+2 instead of T+1 — one session too
    long. TP/SL are checked from the entry session onward against each held
    session's own high/low; a day hitting both is treated as SL
    (conservative)."""
    g = px_by_ticker.get(ticker)
    idx_map = date_idx_by_ticker.get(ticker)
    if g is None or entry_date not in idx_map:
        return None
    i0 = idx_map[entry_date]
    if i0 + 1 >= len(g):
        return None
    # The decision(T)->entry(T+1) step is itself a transition that can bridge
    # a quarantine/suspension gap now that entry no longer coincides with the
    # decision session (open(i0+1) may not really be the very next trading
    # day if a row was dropped), so it needs the same guard every held
    # transition below gets.
    if "fwd_1" in g and pd.isna(g.loc[i0, "fwd_1"]):
        return None
    # i0 is the DECISION session; the position opens at i0+1's open.
    entry_price = g.loc[i0 + 1, "open"]
    if not (entry_price > 0):
        return None
    i0 = i0 + 1  # i0 is now the ENTRY session (T+1) itself.

    for k in range(hold_days):
        if i0 + k >= len(g):
            break
        if k > 0 and "fwd_1" in g and pd.isna(g.loc[i0 + k - 1, "fwd_1"]):
            return None  # quarantine/suspension gap: never bridge it as a hold day
        day = g.loc[i0 + k]
        if sl_pct is not None and day["low"] <= entry_price * (1 - sl_pct):
            return -sl_pct
        if tp_pct is not None and day["high"] >= entry_price * (1 + tp_pct):
            return tp_pct
        if k == hold_days - 1 or i0 + k == len(g) - 1:
            return (day["close"] - entry_price) / entry_price
    return None


def eval_variant(preds_subset, px_by_ticker, date_idx_by_ticker, thresh, hold_days, tp, sl,
                 base_rate=None):
    triggered = preds_subset[preds_subset["pred"] > thresh]
    trade_returns = [
        r for r in (
            simulate_trade(px_by_ticker, date_idx_by_ticker, row["ticker"], row["date"], hold_days, tp, sl)
            for _, row in triggered.iterrows()
        ) if r is not None
    ]
    return trade_stats(trade_returns, base_rate=base_rate)


def run_strategy_search(panel, px, search_frac=0.7):
    preds = get_walk_forward_predictions(panel)
    px_by_ticker, date_idx_by_ticker = _index_price_history(px)

    pred_dates = sorted(preds["date"].unique())
    split_idx = int(len(pred_dates) * search_frac)
    search_dates = set(pred_dates[:split_idx])
    holdout_dates = set(pred_dates[split_idx:])

    search_preds = preds[preds["date"].isin(search_dates)]
    holdout_preds = preds[preds["date"].isin(holdout_dates)]

    def score_all(preds_subset):
        # The base rate every hit rate below must be read against: how often the
        # next-day return of ANY row in this slice was positive. Without it a
        # hit rate is unreadable - see signal_metrics.py.
        base_rate = float((preds_subset["target"] > 0).mean()) if len(preds_subset) else None
        rows = []
        for label, thresh, hold_days, tp, sl in VARIANTS:
            stats = eval_variant(preds_subset, px_by_ticker, date_idx_by_ticker,
                                 thresh, hold_days, tp, sl, base_rate=base_rate)
            rows.append(dict(label=label, thresh=thresh, hold_days=hold_days, tp=tp, sl=sl, **stats))
        return pd.DataFrame(rows).sort_values("mean_ret", ascending=False)

    search_df = score_all(search_preds)
    holdout_df = score_all(holdout_preds)
    winner_label = search_df.iloc[0]["label"]
    winner_holdout = holdout_df[holdout_df["label"] == winner_label].iloc[0]

    return dict(
        search_dates=(min(search_dates), max(search_dates), len(search_dates)),
        holdout_dates=(min(holdout_dates), max(holdout_dates), len(holdout_dates)),
        search_results=search_df,
        holdout_results=holdout_df,
        winner_label=winner_label,
        winner_search_mean=search_df.iloc[0]["mean_ret"],
        winner_holdout_mean=winner_holdout["mean_ret"],
        winner_holdout_n=winner_holdout["n_trades"],
    )


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel(conn)
    px = clean_panel(conn, horizons=(1,), lags=(1,))
    conn.close()

    result = run_strategy_search(panel, px)
    print(f"Search period: {result['search_dates'][2]} dates "
          f"({result['search_dates'][0]} to {result['search_dates'][1]})")
    print(f"Holdout period: {result['holdout_dates'][2]} dates "
          f"({result['holdout_dates'][0]} to {result['holdout_dates'][1]})\n")

    print("=== SEARCH PERIOD ===")
    print(result["search_results"].to_string(index=False))
    print(f"\nWinner: {result['winner_label']} "
          f"(search mean return/trade {result['winner_search_mean']:+.2%})")

    print("\n=== HOLDOUT PERIOD (all variants shown for context; only the winner matters) ===")
    print(result["holdout_results"].to_string(index=False))
    print(f"\nWinner's holdout performance: mean return/trade "
          f"{result['winner_holdout_mean']:+.2%} (n={result['winner_holdout_n']})")
    print("\nNo Sharpe here by design - these are per-trade, cross-sectionally "
          "overlapping returns, which sqrt(252) cannot annualise. See signal_metrics.py.")
