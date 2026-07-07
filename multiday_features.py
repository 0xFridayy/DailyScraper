"""
Tests whether framing broker features as MULTI-DAY patterns (rolling
averages, consecutive-buy-day streaks) rather than single-day snapshots
reveals signal the single-day features miss. Motivation: "hidden broker
accumulation" (the actual thesis) is inherently a sustained, multi-day
phenomenon - a single day's net_flow_total or broker_concentration snapshot
may not capture a quiet position being built over 1-2 weeks the way a
5/10-day trend would.

2026-07-07 result: no improvement. Broker-only rolling features score a
WORSE pooled Sharpe (0.49) than the single-day broker features they were
meant to improve on (0.68, from feature_ablation.py). Combining everything
(single-day + multi-day + price) doesn't beat price-only or the plain full
feature set either. SHAP on the combined set still has momentum_1d
dominating by ~3x; the best-performing multi-day feature
(net_flow_total_roll10) still ranks below several single-day features. This
is a second well-motivated feature-engineering hypothesis (after the plain
single-day features) that didn't pan out on this data - see
smart_money_divergence.py for the third (domain-specific TOD signal).
"""

import sqlite3
import numpy as np
import pandas as pd
from xgboost import XGBRegressor
from walk_forward_backtest import _broker_day_aggregates, _broker_correlation_1d, XGB_PARAMS, sharpe_stats, DB_PATH

MULTI_DAY_BROKER = ["net_flow_total_roll5", "net_flow_total_roll10",
                    "broker_concentration_roll5", "broker_concentration_roll10",
                    "buy_days_5", "buy_days_10"]
SINGLE_DAY_BROKER = ["broker_concentration", "net_flow_total", "n_brokers",
                     "net_buy_ratio", "retail_presence_pct", "broker_correlation_1d"]
PRICE = ["momentum_1d", "volume_ratio"]


def _add_rolling(df, col, windows=(5, 10)):
    for w in windows:
        # shift(1) before rolling: today's own value is never in its own trailing window
        df[f"{col}_roll{w}"] = df.groupby("ticker")[col].transform(
            lambda s: s.shift(1).rolling(w, min_periods=max(2, w // 2)).mean()
        )
    return df


def _buy_streak(s, w):
    is_buy = (s.shift(1) > 0).astype(float)
    return is_buy.rolling(w, min_periods=max(2, w // 2)).sum()


def build_panel_with_multiday(conn):
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    px = pd.read_sql("SELECT date, ticker, close, volume FROM price_history", conn)

    agg = _broker_day_aggregates(bf)
    corr = _broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")
    agg = agg.sort_values(["ticker", "date"]).reset_index(drop=True)

    agg = _add_rolling(agg, "net_flow_total")
    agg = _add_rolling(agg, "broker_concentration")
    agg["buy_days_5"] = agg.groupby("ticker")["net_flow_total"].transform(lambda s: _buy_streak(s, 5))
    agg["buy_days_10"] = agg.groupby("ticker")["net_flow_total"].transform(lambda s: _buy_streak(s, 10))

    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["prev_close"] = px.groupby("ticker")["close"].shift(1)
    px["momentum_1d"] = (px["close"] - px["prev_close"]) / px["prev_close"]
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    px["target"] = px.groupby("ticker")["close"].shift(-1) / px["close"] - 1

    panel = agg.merge(px[["ticker", "date", "momentum_1d", "volume_ratio", "target"]],
                       on=["ticker", "date"], how="inner")
    return panel.dropna(subset=["target"])


def walk_forward(panel, features, train_min=30, test_window=6):
    p = panel.dropna(subset=features + ["target"]).sort_values("date").reset_index(drop=True)
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
        model.fit(fit_df[features], fit_df["target"],
                  eval_set=[(eval_df[features], eval_df["target"])], verbose=False)
        test_pred = model.predict(test_df[features])
        maes.append(np.abs(test_pred - test_df["target"]).mean())
        all_preds.append(test_pred)
        all_actuals.append(test_df["target"].values)
    pooled = sharpe_stats(pd.Series(np.concatenate(all_preds)), pd.Series(np.concatenate(all_actuals)))
    return dict(pooled=pooled, mean_test_mae=np.mean(maes), naive_mae=p["target"].abs().mean(), n=len(p))


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel_with_multiday(conn)
    conn.close()

    print(f"Panel: {len(panel)} rows, {panel['date'].nunique()} dates\n")

    configs = [
        ("Single-day broker only (baseline)", SINGLE_DAY_BROKER),
        ("Multi-day broker only", MULTI_DAY_BROKER),
        ("Multi-day broker + price", MULTI_DAY_BROKER + PRICE),
        ("Single+multi-day broker + price (everything)", SINGLE_DAY_BROKER + MULTI_DAY_BROKER + PRICE),
    ]
    for name, feats in configs:
        r = walk_forward(panel, feats)
        beat = "beats naive" if r["mean_test_mae"] < r["naive_mae"] else "worse than naive"
        print(f"{name} (n={r['n']}): test_mae={r['mean_test_mae']:.4f} naive_mae={r['naive_mae']:.4f} ({beat})")
        if r["pooled"]["n_trades"]:
            print(f"  sharpe={r['pooled']['sharpe']:.2f} n_trades={r['pooled']['n_trades']} "
                  f"hit_rate={r['pooled']['hit_rate']:.2%}")
        else:
            print("  no trades triggered")
        print()
