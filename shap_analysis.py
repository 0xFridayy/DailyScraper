"""
Roadmap #3 — SHAP feature importance.

Fits on the full panel (not a held-out split — this is for interpretability,
"what does the model key on," not a performance claim) and reports mean
|SHAP value| per feature alongside XGBoost's built-in gain importance and
each feature's raw (model-free) correlation with the target, as a sanity
cross-check — a feature that ranks high in SHAP but has ~0 raw correlation
is a red flag for the model fitting noise, not a real relationship.

SYSTEM.md's Layer 1 success metric includes "broker_concentration in Top 3
feature importance." Rerun this whenever the panel changes (e.g. as more
live-scraped days accumulate from 2026-07-05 onward, since the 2025-08-01
inventory-chart backfill ceiling has now been reached — see
backfill_inventory.py) to check where it stands — see
walk_forward_backtest.py's docstring for the 2026-07-07 result on the full
218-day panel (rank 7 of 8, worse than an earlier smaller-sample run's 5 of
8 — momentum_1d dominates instead).
"""

import sqlite3
import numpy as np
import pandas as pd
import shap
from xgboost import XGBRegressor
from walk_forward_backtest import build_panel, FEATURES, DB_PATH


def run_shap_analysis(panel):
    X = panel[FEATURES].fillna(0)
    y = panel["target"]

    model = XGBRegressor(max_depth=4, learning_rate=0.05, n_estimators=100,
                          subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0)
    model.fit(X, y)

    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X)

    mean_abs_shap = pd.Series(np.abs(shap_values).mean(axis=0), index=FEATURES).sort_values(ascending=False)
    gain_imp = pd.Series(model.feature_importances_, index=FEATURES).sort_values(ascending=False)
    raw_corr = panel[FEATURES + ["target"]].corr()["target"].drop("target").sort_values(ascending=False, key=abs)
    bc_rank = list(mean_abs_shap.index).index("broker_concentration") + 1

    return dict(mean_abs_shap=mean_abs_shap, gain_importance=gain_imp,
                raw_correlation=raw_corr, broker_concentration_rank=bc_rank)


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build_panel(conn)
    conn.close()

    print(f"Panel: {len(panel)} rows, {panel['date'].nunique()} dates, {panel['ticker'].nunique()} tickers")

    result = run_shap_analysis(panel)
    print("\nMean |SHAP value| per feature (higher = more influence on predictions):")
    print(result["mean_abs_shap"].to_string())
    print("\nXGBoost built-in gain-based importance (for comparison):")
    print(result["gain_importance"].to_string())
    print("\nRaw correlation of each feature with target (model-free sanity check):")
    print(result["raw_correlation"].to_string())
    print(f"\nbroker_concentration rank by mean |SHAP|: {result['broker_concentration_rank']} "
          f"of {len(FEATURES)} (SYSTEM.md success bar: Top 3)")
