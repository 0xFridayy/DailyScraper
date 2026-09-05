"""ML V2 Experiment #1: broker identity and observable inventory ablation.

This module deliberately runs one experiment only. It compares four feature
sets on the same clean rows and the same chronological walk-forward folds. It
does not contain strategy rules, DDQN, TP/SL, sizing, or ownership inference.

`observable_inventory` means cumulative net lots visible in this repository's
broker history. It is not beneficial ownership.
"""

import argparse
import hashlib
import json
import random
import sqlite3

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

from price_audit import clean_panel
from signal_metrics import signal_stats
from walk_forward_backtest import (
    DB_PATH, RANDOM_SEED, XGB_PARAMS, _broker_correlation_1d,
    _broker_day_aggregates, _price_features_and_target,
    make_walk_forward_splits as _shared_splits,
)


FLOW_WINDOWS = (1, 3, 5, 10, 20)
PRICE_FEATURES = ("momentum_1d", "volume_ratio")
AGGREGATE_FEATURES = (
    "broker_concentration", "net_flow_total", "n_brokers", "net_buy_ratio",
    "retail_presence_pct", "broker_correlation_1d",
)
EXPERIMENT_XGB_PARAMS = {
    **XGB_PARAMS,
    "random_state": RANDOM_SEED,
    "n_jobs": 1,
    "tree_method": "hist",
}


def seed_everything(seed=RANDOM_SEED):
    """Reset every RNG used by this pipeline before each paired model fit."""
    random.seed(seed)
    np.random.seed(seed)


def _historical_net_lots(bf):
    """Recover historical net lots without inventing an ownership quantity.

    Live rows expose buy/sell values and average prices, which recover buy and
    sell lots separately. Backfilled rows expose only net value; that value was
    originally built from net-lot change times close, so clean close recovers
    the observable net-lot change. The latter remains an approximation wherever
    close differs from the true execution VWAP.
    """
    out = bf.copy()
    live = (
        out[["bval", "sval", "bavg", "savg"]].notna().all(axis=1)
        & out["bavg"].gt(0) & out["savg"].gt(0)
    )
    buy_lots = out["bval"] * 1e9 / (out["bavg"] * 100.0)
    sell_lots = out["sval"] * 1e9 / (out["savg"] * 100.0)
    fallback = out["netval"] * 1e9 / (out["close"] * 100.0)
    out["net_lots"] = (buy_lots - sell_lots).where(live, fallback)
    return out


def build_broker_identity_features(px, bf, broker_codes=None):
    """Return code-preserving normalized flow and observable-inventory features.

    Flow windows include information available through the current close and
    are normalized by ticker traded lots over the same window. Inventory is the
    within-clean-segment cumulative net-lot history divided by trailing 20-day
    average traded lots. No future row enters either calculation.
    """
    px = px.sort_values(["ticker", "date"]).copy()
    bf = _historical_net_lots(bf)
    codes = tuple(sorted(broker_codes or bf["broker_code"].dropna().unique()))
    if not codes:
        return px[["ticker", "date"]].copy(), [], []

    daily = bf.pivot_table(
        index=["ticker", "date"], columns="broker_code", values="net_lots",
        aggfunc="sum",
    ).reindex(columns=codes)

    pieces = []
    flow_columns = [f"broker_{code}_flow_{window}d"
                    for window in FLOW_WINDOWS for code in codes]
    inventory_columns = [f"broker_{code}_observable_inventory" for code in codes]

    for ticker, ticker_px in px.groupby("ticker", sort=False):
        ticker_px = ticker_px.sort_values("date").copy()
        idx = pd.MultiIndex.from_arrays(
            [[ticker] * len(ticker_px), ticker_px["date"]],
            names=["ticker", "date"],
        )
        # Absence from the captured broker set is zero *observable* flow. It is
        # not a claim that the broker did not trade.
        matrix = daily.reindex(idx).fillna(0.0).reset_index(drop=True)
        volume_lots = ticker_px["volume"].reset_index(drop=True) / 100.0
        features = ticker_px[["ticker", "date"]].reset_index(drop=True)

        for window in FLOW_WINDOWS:
            net_lots = matrix.rolling(window, min_periods=window).sum()
            traded_lots = volume_lots.rolling(window, min_periods=window).sum()
            normalized = net_lots.div(traded_lots.replace(0, np.nan), axis=0)
            if window > 1 and f"lag_{window}" in ticker_px:
                valid = ticker_px[f"lag_{window}"].notna().reset_index(drop=True)
                normalized = normalized.where(valid, np.nan)
            normalized.columns = [f"broker_{code}_flow_{window}d" for code in codes]
            features = pd.concat([features, normalized], axis=1)

        # A clean-panel hole ends the observable history. Carrying inventory
        # through it would silently assume the missing/quarantined flows were 0.
        lag1 = ticker_px.get("lag_1", pd.Series(np.nan, index=ticker_px.index))
        segments = lag1.isna().reset_index(drop=True).cumsum()
        cumulative_lots = matrix.groupby(segments).cumsum()
        adv20_lots = volume_lots.rolling(20, min_periods=20).mean()
        inventory = cumulative_lots.div(adv20_lots.replace(0, np.nan), axis=0)
        inventory.columns = inventory_columns
        features = pd.concat([features, inventory], axis=1)
        pieces.append(features)

    return pd.concat(pieces, ignore_index=True), flow_columns, inventory_columns


def feature_sets_for_columns(identity_columns, inventory_columns):
    """The four fixed, deliberately non-tuned Experiment #1 definitions."""
    return {
        "price_only": list(PRICE_FEATURES),
        "existing_broker_aggregate": list(PRICE_FEATURES + AGGREGATE_FEATURES),
        "broker_identity": list(PRICE_FEATURES) + list(identity_columns),
        "broker_identity + observable_inventory": (
            list(PRICE_FEATURES) + list(identity_columns) + list(inventory_columns)
        ),
    }


def build_experiment_panel(conn):
    """Build one common clean panel and all four feature definitions.

    open_anchored=True is mandatory: without it px lacks `fwd_oo_1`, and
    _price_features_and_target() now raises ValueError rather than deriving
    anything from the raw `open` column it would otherwise still carry
    (load() always selects the full price_history row). An earlier version
    of that function had an unguarded raw-open fallback that silently
    computed an un-gap-guarded, un-ARA/ARB-validated open-to-open target
    instead of the properly validated fwd_oo_1 that
    price_audit.add_forward_returns builds; it was removed for exactly the
    silent-failure risk this docstring used to warn about.
    """
    px = clean_panel(conn, horizons=(1,), lags=(1, 3, 5, 10, 20), open_anchored=True)
    bf = pd.read_sql(
        "SELECT date, ticker, broker_code, bval, sval, netval, bavg, savg "
        "FROM broker_flow",
        conn,
    )
    bf = bf.merge(
        px[["date", "ticker", "close"]], on=["date", "ticker"], how="inner",
    )

    aggregate = _broker_day_aggregates(bf)
    correlation = _broker_correlation_1d(bf)
    aggregate = aggregate.merge(correlation, on=["ticker", "date"], how="left")
    price = _price_features_and_target(px)
    panel = aggregate.merge(price, on=["ticker", "date"], how="inner")
    panel.loc[panel["momentum_1d"].isna(), "broker_correlation_1d"] = np.nan

    identity, identity_columns, inventory_columns = build_broker_identity_features(px, bf)
    panel = panel.merge(identity, on=["ticker", "date"], how="left")
    panel = panel.dropna(subset=["target"] + list(PRICE_FEATURES))
    panel = panel.replace([np.inf, -np.inf], np.nan)
    panel = panel.sort_values(["date", "ticker"]).reset_index(drop=True)

    feature_sets = feature_sets_for_columns(identity_columns, inventory_columns)
    return panel, feature_sets


def make_walk_forward_splits(panel, train_min=30, test_window=6, eval_fraction=0.20,
                              horizon=1, embargo=0, return_report=False):
    """Precompute date-level fit/eval/test folds once for every feature set.

    Delegates to walk_forward_backtest.make_walk_forward_splits so there is ONE
    implementation of the split rules. That helper adds the two mandatory
    realization purges this file previously lacked: the outer train->test purge
    AND the internal fit->eval purge, without which labels from the tail of FIT
    realize inside EVAL and the fitted model has already seen outcomes from its
    own early-stopping validation period.
    """
    splits, report = _shared_splits(
        tuple(sorted(panel["date"].unique())),
        horizon=horizon, train_min=train_min, test_window=test_window,
        eval_fraction=eval_fraction, embargo=embargo,
    )
    return (splits, report) if return_report else splits


def split_digest(splits):
    payload = json.dumps(splits, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def run_feature_set(panel, features, splits):
    predictions = []
    for cycle, split in enumerate(splits, 1):
        fit = panel[panel["date"].isin(split["fit"])]
        evaluation = panel[panel["date"].isin(split["eval"])]
        test = panel[panel["date"].isin(split["test"])].copy()
        seed_everything()
        model = XGBRegressor(**EXPERIMENT_XGB_PARAMS)
        model.fit(
            fit[features], fit["target"],
            eval_set=[(evaluation[features], evaluation["target"])],
            verbose=False,
        )
        test["prediction"] = model.predict(test[features])
        test["cycle"] = cycle
        predictions.append(test[["ticker", "date", "target", "prediction", "cycle"]])
    if not predictions:
        raise ValueError("No walk-forward folds were produced")
    return pd.concat(predictions, ignore_index=True)


def summarize_predictions(predictions):
    s = signal_stats(
        predictions["prediction"], predictions["target"], groups=predictions["date"],
    )
    return {
        "n_predictions": int(s["n"]),
        "independent_dates": int(predictions["date"].nunique()),
        "pooled_ic": s["ic"],
        "mean_daily_ic": s["daily_ic"],
        "median_daily_ic": s["daily_ic_median"],
        "positive_ic_days": s["positive_ic_days"],
        "top_decile_hit_rate": s["hit_rate"],
        "base_hit_rate": s["base_rate"],
        "hit_edge": s["hit_edge"],
        "return_edge": s["edge"],
        "test_mae": float(np.abs(predictions["prediction"] - predictions["target"]).mean()),
    }


def run_experiment(panel, feature_sets, splits):
    results = []
    predictions = {}
    reference_keys = None
    for name, features in feature_sets.items():
        pred = run_feature_set(panel, features, splits)
        keys = pred[["ticker", "date", "target", "cycle"]].reset_index(drop=True)
        if reference_keys is None:
            reference_keys = keys
        elif not keys.equals(reference_keys):
            raise AssertionError(f"{name} did not score the exact same OOS rows")
        predictions[name] = pred
        results.append({"feature_set": name, "n_features": len(features),
                        **summarize_predictions(pred)})

    table = pd.DataFrame(results).set_index("feature_set")
    baseline = table.loc["price_only"]
    for metric in (
        "pooled_ic", "mean_daily_ic", "median_daily_ic", "positive_ic_days",
        "top_decile_hit_rate", "base_hit_rate", "hit_edge", "return_edge",
    ):
        table[f"delta_{metric}_vs_price_only"] = table[metric] - baseline[metric]
    return table, predictions


def prediction_digest(predictions):
    values = []
    for name, frame in predictions.items():
        values.append(name)
        values.extend(f"{v:.12g}" for v in frame["prediction"].to_numpy())
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:16]


def print_results(table):
    absolute = [
        "n_features", "n_predictions", "independent_dates", "pooled_ic",
        "mean_daily_ic", "median_daily_ic", "positive_ic_days",
        "top_decile_hit_rate", "base_hit_rate", "hit_edge", "return_edge",
        "test_mae",
    ]
    deltas = [column for column in table if column.startswith("delta_")]
    print("\nAbsolute out-of-sample results:")
    print(table[absolute].to_string(float_format=lambda value: f"{value:.6f}"))
    print("\nDelta versus price_only (A):")
    print(table[deltas].to_string(float_format=lambda value: f"{value:+.6f}"))
    print("\nIncremental comparisons requested for review:")
    for left, right, label in (
        ("broker_identity", "existing_broker_aggregate", "C - B"),
        ("broker_identity + observable_inventory", "broker_identity", "D - C"),
    ):
        delta = table.loc[left] - table.loc[right]
        print(
            f"  {label}: pooled IC {delta['pooled_ic']:+.4f}, "
            f"mean daily IC {delta['mean_daily_ic']:+.4f}, "
            f"median daily IC {delta['median_daily_ic']:+.4f}, "
            f"positive-IC days {delta['positive_ic_days']:+.2%}, "
            f"hit edge {delta['hit_edge']:+.2%}, return edge {delta['return_edge']:+.2%}"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--determinism-check", action="store_true",
        help="repeat the full experiment and require identical prediction hashes",
    )
    parser.add_argument(
        "--embargo", type=int, default=0,
        help="extra dates purged at the outer train->test boundary beyond the "
             "mandatory horizon purge (dependence/inference-optimism control, "
             "not leakage). 0 is the headline run; 5 is the robustness variant.",
    )
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        panel, feature_sets = build_experiment_panel(conn)
    splits, split_report = make_walk_forward_splits(
        panel, embargo=args.embargo, return_report=True,
    )
    print(
        f"Clean common panel: {len(panel)} rows, {panel['date'].nunique()} dates, "
        f"{panel['ticker'].nunique()} tickers | {len(splits)} identical folds "
        f"(embargo={args.embargo}) | split {split_digest(splits)} | seed {RANDOM_SEED}"
    )
    print(
        f"Fold report: scored={split_report['n_folds_scored']} "
        f"skipped_too_thin={split_report['n_too_thin']} "
        f"infeasible={split_report['n_infeasible']} "
        f"nominal={split_report['n_folds_nominal']} "
        f"min_fit_days={split_report['min_fit_days']} "
        f"min_eval_days={split_report['min_eval_days']}"
    )
    print("Feature counts: " + ", ".join(f"{name}={len(cols)}" for name, cols in feature_sets.items()))
    table, predictions = run_experiment(panel, feature_sets, splits)
    digest = prediction_digest(predictions)
    print_results(table)
    print(f"\nPrediction digest: {digest}")

    if args.determinism_check:
        repeated, repeated_predictions = run_experiment(panel, feature_sets, splits)
        repeated_digest = prediction_digest(repeated_predictions)
        if repeated_digest != digest or not repeated.equals(table):
            raise AssertionError(
                f"determinism check failed: {digest} != {repeated_digest}"
            )
        print(f"Determinism check: PASS ({repeated_digest})")


if __name__ == "__main__":
    main()
