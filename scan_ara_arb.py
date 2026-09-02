"""Daily ARA / ARB scan over the whole IDX, driven by the inventory chart.

Pipeline (each step is its own script so any stage can be re-run alone):

    harvest_inventory.py     /api/inventory -> inventory_raw/*.json.gz   (1 call/ticker)
    build_inventory_db.py    cache -> ohlc.parquet + broker_daily.parquet
    inventory_features.py    parquet -> panel.parquet (features + ARA/ARB labels)
    scan_ara_arb.py          panel -> tomorrow's ranking            <- you are here

This replaces ara_arb_scan.py, which ran on the 43-ticker konglomerat watchlist
in neobdm.db and inherited that database's scraper contamination. The inventory
API gives the full universe, 101 broker codes, a real buy/sell split, and clean
OHLCV.

Usage:
    python scan_ara_arb.py --top 15
    python scan_ara_arb.py --refresh          # re-harvest first (slow, ~45 min)
"""
import argparse
import os
import subprocess
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from inventory_features import arb_price, ara_price  # noqa: E402
from test_inventory_signal import INV_PREFIX, PX_FEATS  # noqa: E402

MIN_TURN = 0.5      # Rp bn 20d average turnover -- below this a limit hit is a
                    # one-lot print, not something you can trade


def model():
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=250, learning_rate=0.06, min_samples_leaf=60,
        l2_regularization=1.0, class_weight="balanced", random_state=0)


def refresh():
    for step in ["harvest_inventory.py", "build_inventory_db.py", "inventory_features.py"]:
        print(f"--- {step}")
        subprocess.run([sys.executable, os.path.join(HERE, step)], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--min-turn", type=float, default=MIN_TURN)
    a = ap.parse_args()
    if a.refresh:
        refresh()

    d = pd.read_parquet(os.path.join(HERE, "panel.parquet"))
    d["date"] = pd.to_datetime(d["date"])
    liq = d[(d.turn20 >= a.min_turn) & (d.volume > 0)].copy()
    feats = ([f for f in liq.columns if f.startswith(INV_PREFIX)]
             + [f for f in PX_FEATS if f in liq.columns])
    X = liq[feats].replace([np.inf, -np.inf], np.nan)

    last = liq.date.max()
    train = liq[liq.date < last]
    today = liq[liq.date == last]
    print(f"universe {liq.ticker.nunique()} liquid tickers | last session "
          f"{str(last)[:10]} | train {len(train):,} rows | {len(feats)} features")

    for tgt, name, price_fn in [("y_ARB", "ARB", arb_price), ("y_ARA", "ARA", ara_price)]:
        clf = model().fit(X.loc[train.index], train[tgt].astype(int))
        t = today.assign(p=clf.predict_proba(X.loc[today.index])[:, 1])
        t = t.sort_values("p", ascending=False).head(a.top).copy()
        t["limit_px"] = price_fn(t.close.to_numpy(dtype=float))
        cols = ["ticker", "close", "limit_px", "r1", "vr20", "rv20",
                "top_code_20", "acc_share_20", "px_vs_cost_20", "retail_acc_20",
                f"{name.lower()}_c5", "p"]
        cols = [c for c in cols if c in t.columns]
        print(f"\n--- {name} ranking for the session after {str(last)[:10]} ---")
        print(t[cols].round(4).to_string(index=False))

    print("\nColumns: top_code_20 = broker with the largest 20-day net inventory build;")
    print("acc_share_20 = share of the last 20 days' traded volume that broker absorbed;")
    print("px_vs_cost_20 = close vs that broker's average accumulation price;")
    print("retail_acc_20 = retail basket (XL/XC/YP/PD) net, in days of average volume.")


if __name__ == "__main__":
    main()
