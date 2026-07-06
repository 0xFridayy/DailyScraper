"""
Broker-flow vs price-only feature ablation, across forward-return horizons.

Tests the core question SYSTEM.md's Layer 1 exists to answer: does the
broker-flow data add anything over plain price/volume, or is any apparent
signal actually just price momentum? Compares three feature sets (full,
broker-only, price-only) at three forward-return horizons (1d/3d/5d)
against a naive "always predict zero return" baseline — a real model should
beat naive MAE; if it doesn't, there's no usable signal at that
configuration.

2026-07-07 result (57 days, 45 tickers): every variant at every horizon was
beaten by the naive baseline. See walk_forward_backtest.py's docstring and
SYSTEM.md's Backtest Results Log for the numbers and the caveats (most of
this data is netval-only reconstructed, not live-scraped). Rerun this as
more live data accumulates from 2026-07-05 onward.
"""

import sqlite3
import numpy as np
import pandas as pd
from xgboost import XGBRegressor
from walk_forward_backtest import (
    _broker_day_aggregates, _broker_correlation_1d, XGB_PARAMS, sharpe_stats, DB_PATH,
)

BROKER_FEATURES = ["broker_concentration", "net_flow_total", "n_brokers",
                    "net_buy_ratio", "retail_presence_pct", "broker_correlation_1d"]
PRICE_FEATURES = ["momentum_1d", "volume_ratio"]
FULL_FEATURES = BROKER_FEATURES + PRICE_FEATURES


def build_multi_horizon_panel(conn, horizons=(1, 3, 5)):
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    px = pd.read_sql("SELECT date, ticker, close, volume FROM price_history", conn)

    agg = _broker_day_aggregates(bf)
    corr = _broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")

    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["prev_close"] = px.groupby("ticker")["close"].shift(1)
    px["momentum_1d"] = (px["close"] - px["prev_close"]) / px["prev_close"]
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    for h in horizons:
        px[f"target_{h}d"] = px.groupby("ticker")["close"].shift(-h) / px["close"] - 1

    target_cols = [f"target_{h}d" for h in horizons]
    return agg.merge(px[["ticker", "date", "momentum_1d", "volume_ratio"] + target_cols],
                      on=["ticker", "date"], how="inner")


def walk_forward(panel, features, target_col, train_min=30, test_window=6):
    p = panel.dropna(subset=features + [target_col]).sort_values("date").reset_index(drop=True)
    dates = sorted(p["date"].unique())
    cycles, train_end = [], train_min
    while train_end + test_window <= len(dates):
        cycles.append((dates[:train_end], dates[train_end:train_end + test_window]))
        train_end += test_window

    all_preds, all_actuals, maes = [], [], []
    for train_dates, test_dates in cycles:
        train_df = p[p["date"].isin(train_dates)]
        test_df = p[p["date"].isin(test_dates)]
        split = int(len(train_df) * 0.8)
        fit_df, eval_df = train_df.iloc[:split], train_df.iloc[split:]
        model = XGBRegressor(**XGB_PARAMS)
        model.fit(fit_df[features], fit_df[target_col],
                  eval_set=[(eval_df[features], eval_df[target_col])], verbose=False)
        test_pred = model.predict(test_df[features])
        maes.append(np.abs(test_pred - test_df[target_col]).mean())
        all_preds.append(test_pred)
        all_actuals.append(test_df[target_col].values)

    pooled = sharpe_stats(pd.Series(np.concatenate(all_preds)), pd.Series(np.concatenate(all_actuals)))
    naive_mae = p[target_col].abs().mean()
    return dict(pooled=pooled, mean_test_mae=np.mean(maes), naive_mae=naive_mae, n=len(p))


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_multi_horizon_panel(conn)
    conn.close()

    print(f"Panel: {len(panel)} rows, {panel['date'].nunique()} dates\n")

    print("=== Feature-set ablation (1-day forward return) ===")
    for name, feats in [("FULL (broker+price)", FULL_FEATURES),
                         ("PRICE-ONLY", PRICE_FEATURES),
                         ("BROKER-ONLY", BROKER_FEATURES)]:
        r = walk_forward(panel, feats, "target_1d")
        beat_naive = "beats naive" if r["mean_test_mae"] < r["naive_mae"] else "WORSE than naive"
        print(f"{name}: test_mae={r['mean_test_mae']:.4f} naive_mae={r['naive_mae']:.4f} ({beat_naive})")
        if r["pooled"]["n_trades"]:
            print(f"  sharpe={r['pooled']['sharpe']:.2f} n_trades={r['pooled']['n_trades']} "
                  f"hit_rate={r['pooled']['hit_rate']:.2%}")
        else:
            print("  no trades triggered")

    print("\n=== Horizon sweep (full feature set) ===")
    for h in (1, 3, 5):
        r = walk_forward(panel, FULL_FEATURES, f"target_{h}d")
        beat_naive = "beats naive" if r["mean_test_mae"] < r["naive_mae"] else "WORSE than naive"
        print(f"{h}d: test_mae={r['mean_test_mae']:.4f} naive_mae={r['naive_mae']:.4f} ({beat_naive})", end=" ")
        if r["pooled"]["n_trades"]:
            print(f"| sharpe={r['pooled']['sharpe']:.2f} n_trades={r['pooled']['n_trades']} "
                  f"hit_rate={r['pooled']['hit_rate']:.2%}")
        else:
            print("| no trades triggered")
