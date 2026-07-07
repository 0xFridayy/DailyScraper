"""
Simulates IDX's Auto Rejection Atas/Bawah (ARA/ARB) daily price-limit rules
against the winning strategy from strategy_variants.py ("entry threshold
>2%, 1-day fixed exit, no TP/SL"), since a backtest that assumes every close
is fillable is unrealistic when the entry signal specifically targets
already-extreme-momentum days - exactly the days most likely to be sitting
at a limit with a one-sided order queue (locked ARA on the buy side means a
new buyer likely can't get filled; locked ARB on the sell side means an
existing holder likely can't get filled either).

Rule values were NOT taken from memory (regulatory details like this can go
stale) - they were empirically confirmed against every observed daily move
in price_history: ARA is tiered by previous close (<Rp200: +35%,
Rp200-5000: +25%, >Rp5000: +20%), ARB is a flat -15% regardless of price
tier. The max observed move in each tier lines up with this almost exactly
(e.g. exactly +25.00% / +20.00% / -15.00% at the boundaries), which is
strong evidence the underlying data already respects these limits - re-verify
if IDX changes the bands, since they have been revised before (this project
learned of at least one prior revision from asymmetric-symmetric to today's
tiered-ARA/flat-ARB structure).

Treatment of a stuck-limit trade:
  - Entry day at/near ARA (can't reliably buy into a locked limit-up with a
    one-sided order queue): EXCLUDE the trade entirely - no realistic fill,
    no position, doesn't enter the P&L at all.
  - Exit day at/near ARB (can't reliably sell into a locked limit-down):
    roll the exit FORWARD day by day until the first day that ISN'T stuck at
    ARB, and exit there instead - this is the realistic consequence of being
    stuck (you're still holding, and unsticking from a limit-down floor
    tends to continue down before it stabilizes, so this is not a
    favorable assumption for the strategy).

2026-07-07 result: see run_ara_arb_check() - a modest fraction of trades are
entry-blocked (excluded) and exit-delayed (rolled forward), and the impact on
the winning strategy's Sharpe is reported both including and excluding this
adjustment for direct comparison.
"""

import sqlite3
import numpy as np
import pandas as pd
from walk_forward_backtest import build_panel, DB_PATH
from strategy_variants import get_walk_forward_predictions

NEAR_LIMIT_TOLERANCE = 0.01  # within 1 percentage point of the theoretical limit counts as "stuck"


def ara_bound(prev_close):
    if prev_close < 200:
        return 0.35
    if prev_close <= 5000:
        return 0.25
    return 0.20


ARB_BOUND = -0.15


def annotate_limits(px):
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["prev_close"] = px.groupby("ticker")["close"].shift(1)
    px["pct_chg"] = (px["close"] - px["prev_close"]) / px["prev_close"]
    px["ara_bound"] = px["prev_close"].apply(ara_bound)
    px["at_ara"] = px["pct_chg"] >= (px["ara_bound"] - NEAR_LIMIT_TOLERANCE)
    px["at_arb"] = px["pct_chg"] <= (ARB_BOUND + NEAR_LIMIT_TOLERANCE)
    return px


def simulate_trade_with_limits(px_by_ticker, date_idx_by_ticker, ticker, entry_date):
    """1-day fixed exit, no TP/SL - but entry-blocked trades are excluded and
    ARB-stuck exits roll forward to the first unstuck day."""
    g = px_by_ticker.get(ticker)
    idx_map = date_idx_by_ticker.get(ticker)
    if g is None or entry_date not in idx_map:
        return None
    i0 = idx_map[entry_date]
    if g.loc[i0, "at_ara"]:
        return None  # entry blocked: can't reliably buy into a locked limit-up
    if i0 + 1 >= len(g):
        return None
    entry_price = g.loc[i0, "close"]

    j = i0 + 1
    while j < len(g) and g.loc[j, "at_arb"] and j < i0 + 20:  # cap the roll-forward search
        j += 1
    if j >= len(g):
        j = len(g) - 1
    exit_price = g.loc[j, "close"]
    return (exit_price - entry_price) / entry_price, (j - i0)


def sharpe_from_returns(returns):
    returns = np.array(returns)
    n = len(returns)
    if n == 0:
        return dict(sharpe=np.nan, n_trades=0, hit_rate=np.nan)
    std = returns.std()
    sharpe = (returns.mean() / std * np.sqrt(252)) if std > 0 else np.nan
    return dict(sharpe=sharpe, n_trades=n, hit_rate=(returns > 0).mean())


def run_ara_arb_check(threshold=0.020):
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel(conn)
    px = pd.read_sql("SELECT date, ticker, open, high, low, close FROM price_history", conn)
    conn.close()

    px = annotate_limits(px)
    px_by_ticker = {t: g.reset_index(drop=True) for t, g in px.groupby("ticker")}
    date_idx_by_ticker = {t: {d: i for i, d in enumerate(g["date"])} for t, g in px_by_ticker.items()}

    preds = get_walk_forward_predictions(panel)
    triggered = preds[preds["pred"] > threshold]

    naive_returns = []       # ignoring ARA/ARB entirely (what strategy_variants.py reports)
    adjusted_returns = []    # with entry-block exclusion + ARB roll-forward
    n_entry_blocked = 0
    n_exit_delayed = 0
    hold_days_used = []

    for _, row in triggered.iterrows():
        g = px_by_ticker.get(row["ticker"])
        idx_map = date_idx_by_ticker.get(row["ticker"])
        if g is None or row["date"] not in idx_map:
            continue
        i0 = idx_map[row["date"]]
        if i0 + 1 >= len(g):
            continue
        naive_returns.append((g.loc[i0 + 1, "close"] - g.loc[i0, "close"]) / g.loc[i0, "close"])

        result = simulate_trade_with_limits(px_by_ticker, date_idx_by_ticker, row["ticker"], row["date"])
        if result is None:
            n_entry_blocked += 1
            continue
        ret, hold_days = result
        adjusted_returns.append(ret)
        hold_days_used.append(hold_days)
        if hold_days > 1:
            n_exit_delayed += 1

    naive_stats = sharpe_from_returns(naive_returns)
    adjusted_stats = sharpe_from_returns(adjusted_returns)

    return dict(
        threshold=threshold,
        n_signals=len(triggered),
        n_entry_blocked=n_entry_blocked,
        n_exit_delayed=n_exit_delayed,
        mean_hold_days=np.mean(hold_days_used) if hold_days_used else np.nan,
        naive=naive_stats,
        adjusted=adjusted_stats,
    )


if __name__ == "__main__":
    result = run_ara_arb_check(threshold=0.020)
    print(f"Winning strategy (>2% threshold): {result['n_signals']} raw signals")
    print(f"  Entry-blocked (stuck at ARA, excluded): {result['n_entry_blocked']} "
          f"({result['n_entry_blocked']/result['n_signals']:.1%})")
    print(f"  Exit-delayed (stuck at ARB, rolled forward): {result['n_exit_delayed']}")
    print(f"  Mean hold days once ARB roll-forward applied: {result['mean_hold_days']:.2f} (vs 1.0 assumed)")
    print()
    print(f"NAIVE (ignores ARA/ARB, assumes every close is fillable):")
    print(f"  sharpe={result['naive']['sharpe']:.2f} n_trades={result['naive']['n_trades']} "
          f"hit_rate={result['naive']['hit_rate']:.2%}")
    print(f"ARA/ARB-ADJUSTED (entry-blocked trades excluded, ARB-stuck exits rolled forward):")
    print(f"  sharpe={result['adjusted']['sharpe']:.2f} n_trades={result['adjusted']['n_trades']} "
          f"hit_rate={result['adjusted']['hit_rate']:.2%}")
