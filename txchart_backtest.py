"""
Roadmap #2 (first runnable pass) — XGBoost walk-forward on NeoBDM
transaction-chart history (category-level flows, txchart_history.db).

Implements the design agreed in walk_forward_backtest.py's stub:
  - expanding train window, fixed test slices
  - IC (Spearman) from the prediction, trade stats from a tradeable rule
    (long when pred > 0.5%) — no annualisation of per-trade returns
  - reports n_trades, hit_rate, ret_per_risk alongside IC

Additions over the stub design:
  - explicit IDX transaction costs charged inside each trade (entry 0.15%,
    exit 0.25% incl. sales tax) on top of the 0.5% signal threshold
  - embargo between train and test equal to the label horizon, so no train
    label window overlaps test dates (lookahead guard)
  - always-long baseline over the same test rows for context

Framing per the stub: with ~9 months of data this validates the PIPELINE.
A good IC / trade stats here means "the code works", not "the edge is real".

Data caveat: category flows (foreign/institution/smart/market-maker/...) are
NeoBDM's aggregation, NOT per-broker codes — broker_concentration (HHI) and
named smart-money brokers (IF/TP/AZ) are not derivable here. Those arrive as
broker_flow accumulates history.
"""
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "txchart_history.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_out")

# m/nr/s/f/i/z; retail 'r' is the exact mirror of 'nr' in this dataset — drop it.
CATS = ["m", "nr", "s", "f", "i", "z"]

THRESHOLD = 0.005          # long when predicted fwd return > 0.5%
COST_ENTRY = 0.0015        # IDX buy commission
COST_EXIT = 0.0025         # IDX sell commission incl. 0.1% sales tax
INITIAL_TRAIN_DATES = 90   # expanding window starts after this many trading dates
TEST_LEN = 10              # trading dates per test slice
SEED = 42

XGB_PARAMS = dict(          # v1 config from IDX Trading ML System.md
    max_depth=4, learning_rate=0.05, n_estimators=100, subsample=0.8,
    colsample_bytree=0.8, reg_lambda=1.0, random_state=SEED, n_jobs=-1,
)


def load_panel():
    con = sqlite3.connect(DB)
    px = pd.read_sql("SELECT * FROM ohlcv", con)
    flows = pd.read_sql(
        "SELECT date, ticker, category, daily_flow FROM category_flow", con)
    cross = pd.read_sql("SELECT date, ticker, value AS cross_index FROM cross_index", con)
    con.close()

    wide = flows.pivot_table(index=["date", "ticker"], columns="category",
                             values="daily_flow").reset_index()
    df = px.merge(wide, on=["date", "ticker"], how="left")
    df = df.merge(cross, on=["date", "ticker"], how="left")
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


def engineer(df, horizon):
    """Per-ticker features (trailing only) + forward-return label."""
    out = []
    for _, g in df.groupby("ticker", sort=False):
        g = g.sort_values("date").copy()
        c, v = g["close"], g["volume"]

        for cat in CATS:
            f = g[cat].astype(float)
            std20 = f.rolling(20, min_periods=15).std().replace(0, np.nan)
            g[f"{cat}_z1"] = f / std20
            g[f"{cat}_z5"] = f.rolling(5).sum() / (std20 * np.sqrt(5))
            g[f"{cat}_z10"] = f.rolling(10).sum() / (std20 * np.sqrt(10))

        g["cross_chg5"] = g["cross_index"] - g["cross_index"].shift(5)
        g["vol_ratio"] = v / v.rolling(20, min_periods=15).mean()
        r1 = c.pct_change()
        g["ret_1d"] = r1
        g["ret_5d"] = c.pct_change(5)
        g["ret_10d"] = c.pct_change(10)
        g["vol_10d"] = r1.rolling(10).std()
        lo, hi = c.rolling(20, min_periods=15).min(), c.rolling(20, min_periods=15).max()
        g["px_pos20"] = (c - lo) / (hi - lo).replace(0, np.nan)

        g["label"] = c.shift(-horizon) / c - 1.0
        # daily close-to-close returns for the holding window
        for k in range(1, horizon + 1):
            g[f"fwd_r{k}"] = c.shift(-k) / c.shift(-(k - 1)) - 1.0
        out.append(g)

    panel = pd.concat(out, ignore_index=True)
    feats = ([f"{cat}_z{w}" for cat in CATS for w in (1, 5, 10)]
             + ["cross_index", "cross_chg5", "vol_ratio",
                "ret_1d", "ret_5d", "ret_10d", "vol_10d", "px_pos20"])
    keep = panel.dropna(subset=feats + ["label"]).reset_index(drop=True)
    return keep, feats


def walk_forward(panel, feats, horizon):
    dates = np.array(sorted(panel["date"].unique()))
    di = {d: i for i, d in enumerate(dates)}
    panel = panel.assign(_di=panel["date"].map(di))

    preds, importances, cycles = [], {}, []
    te_start = INITIAL_TRAIN_DATES + horizon
    while te_start < len(dates):
        te_end = min(te_start + TEST_LEN, len(dates))
        train = panel[panel["_di"] < te_start - horizon]          # embargo
        test = panel[(panel["_di"] >= te_start) & (panel["_di"] < te_end)]
        if len(train) < 200 or test.empty:
            te_start = te_end
            continue
        model = XGBRegressor(**XGB_PARAMS)
        model.fit(train[feats], train["label"])
        p = test.copy()
        p["pred"] = model.predict(test[feats])
        preds.append(p)
        for f, imp in zip(feats, model.feature_importances_):
            importances[f] = importances.get(f, 0.0) + float(imp)
        cycles.append(dict(train_rows=len(train), test_rows=len(test),
                           test_dates=f"{dates[te_start]}..{dates[te_end-1]}"))
        te_start = te_end

    return pd.concat(preds, ignore_index=True), importances, cycles


def evaluate(preds, horizon, threshold):
    trades = preds[preds["pred"] > threshold].copy()
    net = trades["label"] - COST_ENTRY - COST_EXIT

    # portfolio daily returns: equal weight across trades open on each date
    daily = {}
    for _, t in trades.iterrows():
        for k in range(1, horizon + 1):
            r = t[f"fwd_r{k}"]
            if pd.isna(r):
                continue
            if k == 1:
                r -= COST_ENTRY
            if k == horizon:
                r -= COST_EXIT
            daily.setdefault((t["date"], k), []).append(r)
    # bucket by (signal date offset) — approximate calendar alignment by k-step
    port = pd.Series({key: np.mean(v) for key, v in daily.items()}).sort_index()

    from signal_metrics import trade_stats

    ic = preds[["pred", "label"]].corr(method="spearman").iloc[0, 1]
    base_net = preds["label"] - COST_ENTRY - COST_EXIT

    ts = trade_stats(net) if len(trades) else None
    base_ts = trade_stats(base_net) if len(base_net) else None

    res = dict(
        horizon=horizon,
        n_test_rows=int(len(preds)),
        ic_spearman=round(float(ic), 4),
        n_trades=int(len(trades)),
        hit_rate=round(float(ts["hit_rate"]), 4) if ts else None,
        avg_net_ret=round(float(ts["mean_ret"]), 5) if ts else None,
        med_net_ret=round(float(ts["median_ret"]), 5) if ts else None,
        ret_per_risk=round(float(ts["ret_per_risk"]), 4) if ts else None,
        baseline_always_long=dict(
            n=int(len(preds)),
            hit_rate=round(float(base_ts["hit_rate"]), 4) if base_ts else None,
            avg_net_ret=round(float(base_ts["mean_ret"]), 5) if base_ts else None,
            ret_per_risk=round(float(base_ts["ret_per_risk"]), 4) if base_ts else None,
        ),
    )
    return res, trades


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = load_panel()
    print(f"panel: {len(df)} rows, {df['ticker'].nunique()} tickers, "
          f"{df['date'].nunique()} dates {df['date'].min()}..{df['date'].max()}")

    all_results = {}
    for horizon in (3, 5):
        panel, feats = engineer(df, horizon)
        preds, importances, cycles = walk_forward(panel, feats, horizon)
        res, trades = evaluate(preds, horizon, THRESHOLD)
        res["cycles"] = cycles
        top = sorted(importances.items(), key=lambda kv: -kv[1])[:10]
        res["top_features"] = [(f, round(v, 4)) for f, v in top]
        all_results[f"fwd_{horizon}d"] = res

        preds_out = preds[["date", "ticker", "pred", "label"]].copy()
        preds_out.to_csv(os.path.join(OUT_DIR, f"preds_h{horizon}.csv"), index=False)
        trades[["date", "ticker", "pred", "label"]].to_csv(
            os.path.join(OUT_DIR, f"trades_h{horizon}.csv"), index=False)

        print(f"\n===== horizon {horizon}d =====")
        for k, v in res.items():
            if k not in ("cycles",):
                print(f"  {k}: {v}")

    with open(os.path.join(OUT_DIR, "results.json"), "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {OUT_DIR}")


if __name__ == "__main__":
    main()
