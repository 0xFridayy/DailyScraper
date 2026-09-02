"""
Per-(pattern,type) backtest of patterns_detected.csv signals, with full rigor:
  1. No lookahead: signal is detected as of date d (already true of the CSV,
     since pattern_detector.py only ever looks at bar i and earlier via
     rolling windows); we enter at d+1's close and never touch data before d
     when deciding to trade.
  2. Genuine OOS split: fixed rule, no fitting -> split on SIGNAL DATE at the
     72nd percentile of the 168 unique pattern dates (2025-10-13..2026-07-06)
     = 2026-04-20, giving 120 "early" dates / 48 "holdout" dates (~28.6% by
     date count, inside the 25-30% band). This cutoff is chosen ONCE from the
     full date range, not per pattern-type, and is never touched again.
  3. IDX fill realism: annotate_limits() on txchart_history.db ohlcv close
     series per ticker. Entry day (signal_date + 1 trading day) at_ara ->
     exclude trade. Exit day (entry + HOLD trading days) at_arb -> roll
     forward to next non-at_arb day, capped at entry+20.
  4. Real transaction costs: apply_costs_to_returns / net_return_stats,
     "moderate" preset headline, ideal/conservative for context.
  5. Signal accuracy, not a sizing model: every net-of-cost trade hit rate is
     compared with an always-long universe over the SAME signal dates and
     holding period. hit_edge = signal hit rate - universe base rate.
  6. No cross-sectional pseudo-replication in the headline ranking: hit edge
     is also computed within each signal date, then averaged with one vote per
     date. The search-period mean daily hit edge ranks variants. Holdout never
     selects or reorders variants; it is evaluation only. Per-trade
     ret_per_risk is retained as unannualised context and is not a Sharpe.
  7. Fewer than 30 signal dates is flagged explicitly as low-N.

Universe: patterns_detected.csv only has real stock tickers (no COMPOSITE);
COMPOSITE is dropped from the ohlcv pull as a safety net anyway.

All (pattern,type) combos with n>=40 signals after dedupe are tested, for
both HOLD=3 and HOLD=5 trading days (both pre-specified in the task, not a
search - reported side by side, not cherry-picked).
"""
import os
import sqlite3

import numpy as np
import pandas as pd

from ara_arb_simulation import annotate_limits
from transaction_cost_model import (
    apply_costs_to_returns,
    net_return_stats,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "txchart_history.db")
PATTERN_FILE = os.path.join(HERE, "patterns_detected.csv")

MIN_SIGNALS = 40
HOLDS = (3, 5)
SPLIT_DATE = "2026-04-20"   # 72nd pct of 168 unique pattern dates; fixed once, never re-picked
ARB_ROLL_CAP = 20


def load_data():
    con = sqlite3.connect(DB)
    px = pd.read_sql(
        "SELECT date, ticker, close FROM ohlcv WHERE ticker != 'COMPOSITE' ORDER BY ticker, date",
        con,
    )
    con.close()
    patterns = pd.read_csv(PATTERN_FILE, dtype={"date": str})
    return px, patterns


def dedupe_patterns(patterns):
    """One signal per (ticker, date, pattern, type); strongest kept if dupes exist."""
    patterns = patterns.sort_values("strength", ascending=False)
    return patterns.drop_duplicates(subset=["ticker", "date", "pattern", "type"]).copy()


def prep_price_panels(px):
    px = annotate_limits(px[["date", "ticker", "close"]])
    by_ticker = {t: g.reset_index(drop=True) for t, g in px.groupby("ticker")}
    idx_by_ticker = {t: {d: i for i, d in enumerate(g["date"])} for t, g in by_ticker.items()}
    return by_ticker, idx_by_ticker


def build_trades(pats, by_ticker, idx_by_ticker, hold):
    """Entry = next trading day's close after signal. Exit = HOLD trading days
    after entry, rolled forward past at_arb days (capped). Entry blocked (at_ara)
    trades excluded entirely."""
    trades = []
    for _, p in pats.iterrows():
        ticker = p["ticker"]
        g = by_ticker.get(ticker)
        idx_map = idx_by_ticker.get(ticker)
        if g is None or p["date"] not in idx_map:
            continue
        i_sig = idx_map[p["date"]]
        i_entry = i_sig + 1
        if i_entry >= len(g):
            continue
        if bool(g.loc[i_entry, "at_ara"]):
            continue  # can't reliably buy into a locked limit-up -> exclude
        i_exit_target = i_entry + hold
        if i_exit_target >= len(g):
            continue
        j = i_exit_target
        while j < len(g) and bool(g.loc[j, "at_arb"]) and j < i_entry + ARB_ROLL_CAP:
            j += 1
        if j >= len(g):
            j = len(g) - 1
        entry_price = g.loc[i_entry, "close"]
        exit_price = g.loc[j, "close"]
        if not entry_price or entry_price <= 0:
            continue
        trades.append({
            "ticker": ticker, "signal_date": p["date"], "strength": p["strength"],
            "entry_date": g.loc[i_entry, "date"], "exit_date": g.loc[j, "date"],
            "entry_idx": i_entry, "exit_idx": j,
            "gross_ret": exit_price / entry_price - 1,
        })
    return pd.DataFrame(trades)


def trade_level_stats(trades, universe_trades, preset="moderate"):
    """Cost-adjusted signal stats against the same-date always-long universe."""
    if trades.empty:
        return dict(n=0, hit_rate=np.nan, mean_net=np.nan, median_net=np.nan,
                    ret_per_risk=np.nan, base_rate=np.nan, hit_edge=np.nan)

    comparison = universe_trades[
        universe_trades["signal_date"].isin(trades["signal_date"].unique())
    ]
    if comparison.empty:
        base_rate = np.nan
    else:
        base_net = apply_costs_to_returns(comparison["gross_ret"].values, preset=preset)
        base_rate = float((base_net > 0).mean())

    s = net_return_stats(
        trades["gross_ret"].values,
        preset=preset,
        base_rate=None if np.isnan(base_rate) else base_rate,
    )
    return dict(n=int(s["n_trades"]), hit_rate=round(float(s["hit_rate"]), 4),
                mean_net=round(float(s["mean_ret"]), 5),
                median_net=round(float(s["median_ret"]), 5),
                ret_per_risk=round(float(s["ret_per_risk"]), 4),
                base_rate=round(float(base_rate), 4),
                hit_edge=round(float(s["hit_edge"]), 4))


def date_balanced_hit_edge(trades, universe_trades, preset="moderate"):
    """Average within-date hit edge, giving every signal date one vote.

    Pooled per-trade observations over-weight dates with many concurrent
    signals. This statistic first computes the signal and always-long hit rate
    on each signal date, subtracts them, and only then averages across dates.
    """
    if trades.empty:
        return dict(n_signal_days=0, daily_hit_edge=np.nan,
                    daily_hit_edge_median=np.nan, positive_edge_days=np.nan)

    signal = trades[["signal_date", "gross_ret"]].copy()
    signal["net_ret"] = apply_costs_to_returns(signal["gross_ret"].values, preset=preset)
    signal_daily = signal.assign(hit=signal["net_ret"] > 0).groupby("signal_date")["hit"].mean()

    comparison = universe_trades[
        universe_trades["signal_date"].isin(signal_daily.index)
    ][["signal_date", "gross_ret"]].copy()
    comparison["net_ret"] = apply_costs_to_returns(
        comparison["gross_ret"].values, preset=preset
    )
    universe_daily = comparison.assign(hit=comparison["net_ret"] > 0).groupby("signal_date")["hit"].mean()
    edge = (signal_daily - universe_daily).dropna()
    if edge.empty:
        return dict(n_signal_days=0, daily_hit_edge=np.nan,
                    daily_hit_edge_median=np.nan, positive_edge_days=np.nan)
    return dict(
        n_signal_days=int(len(edge)),
        daily_hit_edge=round(float(edge.mean()), 4),
        daily_hit_edge_median=round(float(edge.median()), 4),
        positive_edge_days=round(float((edge > 0).mean()), 4),
    )


def evaluate_combo(pats_combo, by_ticker, idx_by_ticker, universe_trades,
                   hold, split_date):
    trades = build_trades(pats_combo, by_ticker, idx_by_ticker, hold)
    if trades.empty:
        return None
    early = trades[trades["signal_date"] < split_date]
    hold_ = trades[trades["signal_date"] >= split_date]

    out = {"n_raw_signals": len(pats_combo), "hold_days": hold,
           "n_trades_total": len(trades)}

    for label, sub in (("search", early), ("holdout", hold_)):
        ts = trade_level_stats(sub, universe_trades, preset="moderate")
        ds = date_balanced_hit_edge(sub, universe_trades, preset="moderate")
        out[f"{label}_n"] = ts["n"]
        out[f"{label}_hit_rate"] = ts.get("hit_rate", np.nan)
        out[f"{label}_mean_net"] = ts.get("mean_net", np.nan)
        out[f"{label}_ret_per_risk"] = ts.get("ret_per_risk", np.nan)
        out[f"{label}_base_rate"] = ts.get("base_rate", np.nan)
        out[f"{label}_hit_edge"] = ts.get("hit_edge", np.nan)
        out[f"{label}_n_signal_days"] = ds["n_signal_days"]
        out[f"{label}_daily_hit_edge"] = ds["daily_hit_edge"]
        out[f"{label}_positive_edge_days"] = ds["positive_edge_days"]
        out[f"{label}_reliability"] = (
            "LOW_N" if ds["n_signal_days"] < 30 else "OK"
        )

        # Cost-sensitivity context; moderate remains the headline preset.
        for preset in ("ideal", "conservative"):
            ts_p = trade_level_stats(sub, universe_trades, preset=preset)
            ds_p = date_balanced_hit_edge(sub, universe_trades, preset=preset)
            out[f"{label}_hit_edge_{preset}"] = ts_p["hit_edge"]
            out[f"{label}_daily_hit_edge_{preset}"] = ds_p["daily_hit_edge"]

    return out


def main():
    px, patterns = load_data()
    patterns = dedupe_patterns(patterns)
    by_ticker, idx_by_ticker = prep_price_panels(px)

    counts = patterns.groupby(["pattern", "type"]).size().sort_values(ascending=False)
    print("Signal counts after dedupe:")
    print(counts)
    combos = [(pat, typ) for (pat, typ), n in counts.items() if n >= MIN_SIGNALS]
    print(f"\nCombos with n>={MIN_SIGNALS}: {combos}")
    print(f"Split date (fixed, 72nd pct of pattern dates): {SPLIT_DATE}")

    results = []
    universe_signals = px[["ticker", "date"]].copy()
    universe_signals["strength"] = 0.0
    universe_by_hold = {
        hold: build_trades(universe_signals, by_ticker, idx_by_ticker, hold)
        for hold in HOLDS
    }
    for pat, typ in combos:
        pats_combo = patterns[(patterns["pattern"] == pat) & (patterns["type"] == typ)]
        for hold in HOLDS:
            r = evaluate_combo(
                pats_combo, by_ticker, idx_by_ticker, universe_by_hold[hold],
                hold, SPLIT_DATE,
            )
            if r is None:
                continue
            r["pattern"] = pat
            r["type"] = typ
            results.append(r)

    res_df = pd.DataFrame(results)
    cols = ["pattern", "type", "hold_days", "n_raw_signals", "n_trades_total",
            "search_n", "search_n_signal_days", "search_daily_hit_edge",
            "search_hit_rate", "search_base_rate", "search_hit_edge",
            "search_ret_per_risk", "holdout_n", "holdout_n_signal_days",
            "holdout_daily_hit_edge", "holdout_hit_rate", "holdout_base_rate",
            "holdout_hit_edge", "holdout_ret_per_risk", "holdout_reliability",
            "holdout_daily_hit_edge_ideal", "holdout_daily_hit_edge_conservative"]
    res_df = res_df[cols]
    res_df = res_df.sort_values(
        ["search_daily_hit_edge", "search_hit_edge"], ascending=False
    )
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print("\n=== RANKED BY SEARCH-PERIOD DAILY HIT EDGE (moderate costs) ===")
    print(res_df.to_string(index=False))

    res_df.to_csv(os.path.join(HERE, "pattern_type_backtest_results.csv"), index=False)
    print("\nSaved pattern_type_backtest_results.csv")


if __name__ == "__main__":
    main()
