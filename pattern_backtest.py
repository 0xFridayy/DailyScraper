"""
Pattern backtest v2 — bug-fixed + regime-conditional.

Fixes vs v1 (which was INVALID):
  - v1 sliced exit windows positionally across a date+ticker-sorted frame, so a
    trade's "next 5 bars" were OTHER TICKERS' rows from the same day. All v1
    numbers were artifacts.
  - v1's +1% intraday target assumed perfect limit fills at the high; removed.

Rules (defined ex-ante, no tuning after seeing results):
  - Signals: long-side patterns from patterns_detected.csv
    (breakout_up, break_up, bounce_up, up_reversal), deduped to one trade per
    (ticker, signal date), strongest pattern kept.
  - Entry: next trading day's close after the signal date.
  - Exit: close 5 trading days after entry. No target/stop.
  - Costs: 0.15% entry + 0.25% exit.
  - Regime metric: 20-day rolling sum of AGGREGATE foreign daily flow
    (sum of category 'f' across the universe, from txchart_history.db).
    risk_on if that sum > 0 as of the signal date, else risk_off.
    Foreign flow is a one-sided classification, legitimately nonzero —
    unlike total broker netval, which sums to zero by accounting identity.

Reports performance unconditionally AND split by regime, plus an always-long
baseline per regime (to separate the filter's value from market beta), plus a
regime split of the saved XGBoost walk-forward predictions.
"""
import json
import os
import sqlite3

import numpy as np
import pandas as pd

from signal_metrics import trade_stats

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "txchart_history.db")
PATTERN_FILE = os.path.join(HERE, "patterns_detected.csv")
XGB_PREDS = {3: os.path.join(HERE, "backtest_out", "preds_h3.csv"),
             5: os.path.join(HERE, "backtest_out", "preds_h5.csv")}

COST = 0.0015 + 0.0025      # round trip
HOLD = 5                     # trading days
REGIME_WINDOW = 20           # rolling sum window for aggregate foreign flow
LONG_TYPES = {"breakout_up", "break_up", "bounce_up", "up_reversal"}


def load_prices():
    con = sqlite3.connect(DB)
    px = pd.read_sql("SELECT date, ticker, close FROM ohlcv", con)
    fl = pd.read_sql(
        "SELECT date, ticker, daily_flow FROM category_flow WHERE category='f'", con)
    con.close()
    return px, fl


def build_regime(foreign):
    """date -> 'risk_on'/'risk_off' from 20d rolling sum of aggregate foreign flow."""
    agg = (foreign.groupby("date")["daily_flow"].sum()
           .sort_index())
    roll = agg.rolling(REGIME_WINDOW, min_periods=REGIME_WINDOW).sum()
    regime = pd.Series(np.where(roll > 0, "risk_on", "risk_off"), index=roll.index)
    regime[roll.isna()] = None
    return regime, agg, roll


def simulate_trades(px, patterns):
    """Entry next close, exit close HOLD days later — per ticker, positionally safe."""
    trades = []
    for ticker, g in px.groupby("ticker"):
        g = g.sort_values("date").reset_index(drop=True)
        pos = {d: i for i, d in enumerate(g["date"])}
        pats = patterns[patterns["ticker"] == ticker]
        for _, p in pats.iterrows():
            i = pos.get(p["date"])
            if i is None or i + 1 + HOLD >= len(g):
                continue
            entry = g.loc[i + 1, "close"]
            exit_ = g.loc[i + 1 + HOLD, "close"]
            if not entry or entry <= 0:
                continue
            trades.append({
                "signal_date": p["date"], "ticker": ticker,
                "pattern": p["pattern"], "strength": p["strength"],
                "entry_date": g.loc[i + 1, "date"],
                "exit_date": g.loc[i + 1 + HOLD, "date"],
                "entry": entry, "exit": exit_,
                "net_ret": exit_ / entry - 1 - COST,
            })
    return pd.DataFrame(trades)


def stats(rets, base_rate=None):
    """Cost-adjusted trade summary via signal_metrics.trade_stats.

    No annualisation — observations are per-trade, not daily, and concurrent
    cross-ticker trades are not independent (signal_metrics.py measures
    +0.275 mean pairwise daily correlation on this panel). Ret_per_risk is
    mean/std, deliberately unscaled; it ranks variants but is NOT a Sharpe
    ratio. See HANDOFF.md TEMUAN 2 for why the sqrt(252) / sqrt(252/N)
    rescaling was removed rather than replaced.
    """
    ts = trade_stats(rets, base_rate=base_rate)
    return dict(
        n=ts["n_trades"],
        hit_rate=round(float(ts["hit_rate"]), 3) if ts["n_trades"] else None,
        avg_net=round(float(ts["mean_ret"]), 5) if ts["n_trades"] else None,
        median_net=round(float(ts["median_ret"]), 5) if ts["n_trades"] else None,
        ret_per_risk=round(float(ts["ret_per_risk"]), 4) if ts["n_trades"] else None,
        base_rate=ts["base_rate"],
        hit_edge=(round(float(ts["hit_edge"]), 3) if ts["n_trades"]
                  and ts["base_rate"] is not None else None),
    )


def main():
    px, foreign = load_prices()
    regime, agg_f, roll_f = build_regime(foreign)

    n_on = int((regime == "risk_on").sum())
    n_off = int((regime == "risk_off").sum())
    print(f"Regime coverage: {n_on} risk-on days, {n_off} risk-off days "
          f"(window={REGIME_WINDOW}d, first defined {roll_f.dropna().index[0]})")

    patterns = pd.read_csv(PATTERN_FILE)
    patterns = patterns[patterns["type"].isin(LONG_TYPES)].copy()
    patterns = (patterns.sort_values("strength", ascending=False)
                .drop_duplicates(subset=["ticker", "date"]))
    print(f"Long-side pattern signals after dedupe: {len(patterns)}")

    trades = simulate_trades(px, patterns)
    trades["regime"] = trades["signal_date"].map(regime)
    trades = trades.dropna(subset=["regime"])

    # always-long baseline: every (ticker, date) with a 5d-forward close
    base_rows = []
    for ticker, g in px.groupby("ticker"):
        g = g.sort_values("date").reset_index(drop=True)
        fwd = g["close"].shift(-(1 + HOLD)) / g["close"].shift(-1) - 1 - COST
        base_rows.append(pd.DataFrame(
            {"date": g["date"], "net_ret": fwd}))
    base = pd.concat(base_rows).dropna()
    base["regime"] = base["date"].map(regime)
    base = base.dropna(subset=["regime"])

    universe_base_rate = float((base["net_ret"] > 0).mean()) if len(base) else None
    results = {"regime_days": {"risk_on": n_on, "risk_off": n_off},
               "universe_base_rate": round(universe_base_rate, 4) if universe_base_rate is not None else None}

    print("\n=== PATTERN TRADES (fixed 5d hold, costs 0.40%) ===")
    results["patterns_all"] = stats(trades["net_ret"], base_rate=universe_base_rate)
    print(f"  ALL:      {results['patterns_all']}")
    for reg in ("risk_on", "risk_off"):
        s = stats(trades.loc[trades["regime"] == reg, "net_ret"], base_rate=universe_base_rate)
        results[f"patterns_{reg}"] = s
        print(f"  {reg:9s} {s}")

    print("\n=== ALWAYS-LONG BASELINE (same horizon/costs) ===")
    for reg in ("risk_on", "risk_off"):
        s = stats(base.loc[base["regime"] == reg, "net_ret"], base_rate=universe_base_rate)
        results[f"baseline_{reg}"] = s
        print(f"  {reg:9s} {s}")

    # pattern-type breakdown, risk-on only
    print("\n=== PATTERN TYPE BREAKDOWN (risk-on only) ===")
    for pat, g in trades[trades["regime"] == "risk_on"].groupby("pattern"):
        print(f"  {pat:20s} {stats(g['net_ret'], base_rate=universe_base_rate)}")

    # XGBoost predictions re-scored by regime
    print("\n=== XGBOOST WALK-FORWARD PREDS BY REGIME ===")
    for h, path in XGB_PREDS.items():
        if not os.path.exists(path):
            continue
        preds = pd.read_csv(path)
        preds["regime"] = preds["date"].map(regime)
        preds = preds.dropna(subset=["regime"])
        for reg in ("risk_on", "risk_off"):
            sub = preds[preds["regime"] == reg]
            if len(sub) < 20:
                print(f"  h={h}d {reg}: too few rows ({len(sub)})")
                continue
            ic = sub[["pred", "label"]].corr(method="spearman").iloc[0, 1]
            sig = sub[sub["pred"] > 0.005]
            net = sig["label"] - COST
            s = stats(net, base_rate=universe_base_rate)
            print(f"  h={h}d {reg:9s} rows={len(sub)} IC={ic:+.3f} "
                  f"threshold-trades: {s}")
            results[f"xgb_h{h}_{reg}"] = {"rows": int(len(sub)),
                                          "ic": round(float(ic), 4), **s}

    trades.to_csv(os.path.join(HERE, "pattern_trades.csv"), index=False)
    with open(os.path.join(HERE, "backtest_out", "regime_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved pattern_trades.csv and backtest_out/regime_results.json")


if __name__ == "__main__":
    main()
