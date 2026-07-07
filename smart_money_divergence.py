"""
Tests the specific domain thesis already encoded in neobdm_scraper.py's
SMART_MONEY / RETAIL_BROKERS / BIG_PLAYER_ABSORBERS constants: the "TOD"
bullish signal described there is SMART_MONEY codes (IF/AZ/BB) net-buying a
stock while RETAIL_BROKERS (XL/XC/YP/PD) are net-selling it — smart money
absorbing retail distribution. Every other feature tried so far (SHAP,
feature_ablation.py) treats all brokers as one generic aggregate, which
could dilute this specific pattern if it's real. This isolates it directly.

2026-07-07 result: directionally consistent (mean forward return on
tod_signal=1 days is 2x higher than on tod_signal=0 days: 0.49% vs 0.24%,
n=1289 vs n=8167) but NOT statistically significant (Welch's t-test,
p=0.18 - nowhere near even an uncorrected 0.05, let alone corrected for how
many things were tried today). Walk-forward Sharpe on smart-money features
alone (0.56) or combined with everything else (0.88) doesn't beat the
plain full feature set (0.94) either. Not evidence for the thesis; also not
strong evidence against it given the sample size - rerun as more live data
accumulates, since directional consistency + growing significance as
sample size increases would be the actual signal to watch for (compare to
how the overall Sharpe went from clearly negative to +0.94 purely from
3.7x more data - this could do the same).
"""

import sqlite3
import numpy as np
import pandas as pd
from scipy import stats
from xgboost import XGBRegressor
from walk_forward_backtest import _broker_day_aggregates, _broker_correlation_1d, XGB_PARAMS, sharpe_stats, DB_PATH
from neobdm_scraper import SMART_MONEY, RETAIL_BROKERS, BIG_PLAYER_ABSORBERS

SMART_MONEY_SET = set(SMART_MONEY)
RETAIL_SET = set(RETAIL_BROKERS)
ABSORBER_SET = set(BIG_PLAYER_ABSORBERS)

SMART_FEATURES = ["smart_money_net", "retail_net", "absorber_net", "tod_divergence",
                   "tod_signal", "absorber_vs_retail"]


def _group_features(g):
    smart_net = g.loc[g["broker_code"].isin(SMART_MONEY_SET), "netval"].sum()
    retail_net = g.loc[g["broker_code"].isin(RETAIL_SET), "netval"].sum()
    absorber_net = g.loc[g["broker_code"].isin(ABSORBER_SET), "netval"].sum()
    return pd.Series({
        "smart_money_net": smart_net,
        "retail_net": retail_net,
        "absorber_net": absorber_net,
        "tod_divergence": smart_net - retail_net,
        "tod_signal": 1.0 if (smart_net > 0 and retail_net < 0) else 0.0,
        "absorber_vs_retail": absorber_net - retail_net,
    })


def build_panel_with_smart_money(conn):
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    px = pd.read_sql("SELECT date, ticker, close, volume FROM price_history", conn)

    agg = _broker_day_aggregates(bf)
    corr = _broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")
    special = bf.groupby(["ticker", "date"]).apply(_group_features, include_groups=False).reset_index()
    agg = agg.merge(special, on=["ticker", "date"], how="left")

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
    panel = build_panel_with_smart_money(conn)
    conn.close()

    print(f"Panel: {len(panel)} rows, {panel['date'].nunique()} dates")

    on = panel[panel["tod_signal"] == 1]["target"]
    off = panel[panel["tod_signal"] == 0]["target"]
    t, p_value = stats.ttest_ind(on, off, equal_var=False)
    print(f"\ntod_signal=1 (smart money buying while retail sells): {len(on)} of {len(panel)} "
          f"ticker-days ({len(on)/len(panel):.1%})")
    print(f"Mean forward return: on={on.mean():.4f} (n={len(on)}) vs off={off.mean():.4f} (n={len(off)})")
    print(f"Welch's t-test: t={t:.3f}, p={p_value:.4f} "
          f"({'SIGNIFICANT' if p_value < 0.05 else 'not significant'} at alpha=0.05)")

    base_features = ["broker_concentration", "net_flow_total", "n_brokers", "net_buy_ratio",
                      "retail_presence_pct", "broker_correlation_1d"]
    price_features = ["momentum_1d", "volume_ratio"]

    print()
    for name, feats in [("Smart-money features only", SMART_FEATURES),
                         ("Smart-money + price", SMART_FEATURES + price_features),
                         ("Everything (base + smart-money + price)",
                          base_features + SMART_FEATURES + price_features)]:
        r = walk_forward(panel, feats)
        beat = "beats naive" if r["mean_test_mae"] < r["naive_mae"] else "worse than naive"
        print(f"{name} (n={r['n']}): test_mae={r['mean_test_mae']:.4f} naive_mae={r['naive_mae']:.4f} ({beat})")
        if r["pooled"]["n_trades"]:
            print(f"  sharpe={r['pooled']['sharpe']:.2f} n_trades={r['pooled']['n_trades']} "
                  f"hit_rate={r['pooled']['hit_rate']:.2%}")
        else:
            print("  no trades triggered")
