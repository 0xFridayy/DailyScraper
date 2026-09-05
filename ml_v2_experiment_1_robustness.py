"""Robustness audit for ML V2 Experiment #1 feature set C versus B.

The accepted deterministic predictions are reproduced with the unchanged panel,
feature construction, XGBoost parameters, seed, and split list. Paired scoring
uses trading date as the independent unit. Broker concentration ablations refit
C after removing one broker-code group at a time; nothing is tuned.
"""

import argparse
import os
import re
import sqlite3

import numpy as np
import pandas as pd

from signal_metrics import spearman_ic
from ml_v2_experiment_1 import (
    DB_PATH, build_experiment_panel, make_walk_forward_splits,
    prediction_digest, run_experiment, run_feature_set, split_digest,
    summarize_predictions,
)


#: Experiment #1 (LEGACY, close(T) -> close(T+1) contract). FROZEN PROVENANCE:
#: never re-pin this in place. That contract is invalid as a tradable-return
#: claim — the entire drift sat in the pre-entry close->open gap the signal
#: could not reach — but it remains a real, reproducible RANKING result under a
#: stated contract, and HEAD must never imply the original experiment used
#: next-open execution.
LEGACY_CLOSE_CONTRACT_DIGEST = "147d734749c71e2d"

#: Experiment #1E (EXECUTABLE, open(T+1) -> open(T+2) contract). A separate
#: contract version with its own identity, not an overwrite of the above.
#: Pinned 2026-09-05 after determinism was proven two ways: (a) two fully
#: independent OS-process invocations of `py ml_v2_experiment_1.py --embargo 0`
#: produced byte-identical stdout (panel shape, split digest, full result
#: table, and this digest), and (b) an in-process harness rebuilt the panel,
#: splits, and predictions from two independent sqlite connections and asserted
#: exact pandas .equals() on the panel, the fold list, every feature set's
#: prediction rows, and the summary table. See
#: ML_V2_EXPERIMENT_1E_RESULTS.md for the accepted (negative/weak) finding —
#: this pin records reproducibility, not a claim that the result is strong.
EXECUTABLE_V1_PREDICTION_DIGEST = "4dac8d153b33f7aa"

#: What the current run is checked against. Points at the executable contract
#: once #1E is pinned; until then there is nothing to enforce, and the legacy
#: digest is deliberately NOT reused as a stand-in for it.
ACCEPTED_PREDICTION_DIGEST = EXECUTABLE_V1_PREDICTION_DIGEST
BOOTSTRAP_SEED = 1701
N_BOOTSTRAP = 20_000
WEEKLY_BLOCK_DAYS = 5
PAIRED_METRICS = ("daily_ic_delta", "top_hit_delta", "return_edge_delta")
HEADLINE_METRICS = (
    "pooled_ic", "mean_daily_ic", "median_daily_ic", "positive_ic_days",
    "hit_edge", "return_edge",
)


def paired_date_differences(pred_b, pred_c):
    """Score C-B within each date, preserving cross-sectional selection."""
    b = pred_b.rename(columns={"prediction": "prediction_b"})
    c = pred_c.rename(columns={"prediction": "prediction_c"})
    keys = ["ticker", "date", "target", "cycle"]
    joined = b[keys + ["prediction_b"]].merge(
        c[keys + ["prediction_c"]], on=keys, how="inner", validate="one_to_one",
    )
    if len(joined) != len(b) or len(joined) != len(c):
        raise AssertionError("B and C do not contain identical OOS rows")

    rows = []
    for date, day in joined.groupby("date", sort=True):
        n_top = max(1, int(np.ceil(len(day) * 0.10)))
        top_b = day.nlargest(n_top, "prediction_b")
        top_c = day.nlargest(n_top, "prediction_c")
        base_mean = day["target"].mean()
        rows.append({
            "date": date,
            "n_tickers": len(day),
            "daily_ic_b": spearman_ic(day["prediction_b"], day["target"]),
            "daily_ic_c": spearman_ic(day["prediction_c"], day["target"]),
            "top_hit_b": float((top_b["target"] > 0).mean()),
            "top_hit_c": float((top_c["target"] > 0).mean()),
            "return_edge_b": float(top_b["target"].mean() - base_mean),
            "return_edge_c": float(top_c["target"].mean() - base_mean),
        })
    out = pd.DataFrame(rows)
    out["daily_ic_delta"] = out["daily_ic_c"] - out["daily_ic_b"]
    out["top_hit_delta"] = out["top_hit_c"] - out["top_hit_b"]
    out["return_edge_delta"] = out["return_edge_c"] - out["return_edge_b"]
    return out


def _bootstrap_statistics(values, n_bootstrap=N_BOOTSTRAP, block_days=None, seed=BOOTSTRAP_SEED):
    values = np.asarray(pd.Series(values).dropna(), dtype=float)
    n = len(values)
    if n == 0:
        return {"n": 0, "mean_ci": (np.nan, np.nan), "median_ci": (np.nan, np.nan)}
    rng = np.random.default_rng(seed)
    if block_days is None:
        indices = rng.integers(0, n, size=(n_bootstrap, n))
    else:
        block_days = min(block_days, n)
        n_blocks = int(np.ceil(n / block_days))
        starts = rng.integers(0, n - block_days + 1, size=(n_bootstrap, n_blocks))
        offsets = np.arange(block_days)
        indices = (starts[..., None] + offsets).reshape(n_bootstrap, -1)[:, :n]
    samples = values[indices]
    means = samples.mean(axis=1)
    medians = np.median(samples, axis=1)
    return {
        "n": n,
        "mean_ci": tuple(np.percentile(means, [2.5, 97.5])),
        "median_ci": tuple(np.percentile(medians, [2.5, 97.5])),
    }


def paired_bootstrap_report(per_date):
    rows = []
    for index, metric in enumerate(PAIRED_METRICS):
        values = per_date[metric].dropna()
        iid = _bootstrap_statistics(values, seed=BOOTSTRAP_SEED + index)
        block = _bootstrap_statistics(
            values, block_days=WEEKLY_BLOCK_DAYS, seed=BOOTSTRAP_SEED + 100 + index,
        )
        rows.append({
            "metric": metric, "n_dates": len(values),
            "mean": values.mean(), "median": values.median(),
            "pct_c_beats_b": (values > 0).mean(), "pct_ties": (values == 0).mean(),
            "date_boot_mean_lo": iid["mean_ci"][0],
            "date_boot_mean_hi": iid["mean_ci"][1],
            "date_boot_median_lo": iid["median_ci"][0],
            "date_boot_median_hi": iid["median_ci"][1],
            "block_boot_mean_lo": block["mean_ci"][0],
            "block_boot_mean_hi": block["mean_ci"][1],
            "block_boot_median_lo": block["median_ci"][0],
            "block_boot_median_hi": block["median_ci"][1],
        })
    return pd.DataFrame(rows).set_index("metric")


def headline_delta(pred_b, pred_c):
    b = summarize_predictions(pred_b)
    c = summarize_predictions(pred_c)
    return {metric: c[metric] - b[metric] for metric in HEADLINE_METRICS}


def leave_one_ticker_out(pred_b, pred_c):
    full = headline_delta(pred_b, pred_c)
    rows = []
    for ticker in sorted(pred_b["ticker"].unique()):
        b = pred_b[pred_b["ticker"] != ticker]
        c = pred_c[pred_c["ticker"] != ticker]
        delta = headline_delta(b, c)
        rows.append({
            "removed_ticker": ticker,
            **delta,
            **{f"influence_{metric}": delta[metric] - full[metric]
               for metric in HEADLINE_METRICS},
        })
    return pd.DataFrame(rows), full


def _broker_groups(identity_features):
    groups = {}
    for feature in identity_features:
        match = re.fullmatch(r"broker_(.+)_flow_(1|3|5|10|20)d", feature)
        if match:
            groups.setdefault(match.group(1), []).append(feature)
    return {code: sorted(features) for code, features in sorted(groups.items())}


def broker_group_ablation(panel, feature_sets, splits, pred_b, full_delta):
    """Refit unchanged C while omitting each broker code's five flow windows."""
    c_features = feature_sets["broker_identity"]
    groups = _broker_groups(c_features)
    rows = []
    for index, (code, removed) in enumerate(groups.items(), 1):
        print(f"  broker ablation {index:02d}/{len(groups):02d}: {code}", flush=True)
        features = [feature for feature in c_features if feature not in removed]
        pred = run_feature_set(panel, features, splits)
        delta = headline_delta(pred_b, pred)
        rows.append({
            "removed_broker": code, "n_removed_features": len(removed),
            **delta,
            **{f"influence_{metric}": delta[metric] - full_delta[metric]
               for metric in HEADLINE_METRICS},
        })
    return pd.DataFrame(rows)


def ihsg_regime_audit(per_date, path):
    """Use existing local JCI only; positive/negative means trailing-20d return."""
    if not path or not os.path.exists(path):
        return None
    ihsg = pd.read_csv(path, usecols=["date", "close"])
    ihsg["date"] = pd.to_datetime(ihsg["date"])
    ihsg = ihsg.sort_values("date").drop_duplicates("date")
    ihsg["ihsg_return_20d"] = ihsg["close"] / ihsg["close"].shift(20) - 1
    ihsg["ihsg_regime"] = np.where(
        ihsg["ihsg_return_20d"] >= 0, "positive_20d", "negative_20d",
    )
    joined = per_date.copy()
    joined["date"] = pd.to_datetime(joined["date"])
    joined = joined.merge(
        ihsg[["date", "ihsg_return_20d", "ihsg_regime"]], on="date", how="inner",
    ).dropna(subset=["ihsg_return_20d"])
    rows = []
    for regime, group in joined.groupby("ihsg_regime", sort=True):
        row = {"regime": regime, "n_dates": group["date"].nunique()}
        for metric in PAIRED_METRICS:
            row[f"mean_{metric}"] = group[metric].mean()
            row[f"median_{metric}"] = group[metric].median()
            row[f"pct_c_beats_b_{metric}"] = (group[metric] > 0).mean()
        rows.append(row)
    return joined, pd.DataFrame(rows).set_index("regime")


def _print_concentration_summary(table, id_column, full_delta):
    for metric in ("pooled_ic", "mean_daily_ic", "hit_edge", "return_edge"):
        influence = f"influence_{metric}"
        worst = table.loc[table[influence].idxmin()]
        largest = table.loc[table[influence].abs().idxmax()]
        print(
            f"  {metric}: full {full_delta[metric]:+.6f}; LOO range "
            f"[{table[metric].min():+.6f}, {table[metric].max():+.6f}]; "
            f"largest deterioration removing {worst[id_column]} "
            f"({worst[influence]:+.6f}); largest absolute influence "
            f"{largest[id_column]} ({largest[influence]:+.6f})"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jci", default=os.path.join(os.path.dirname(__file__), "jci_daily.csv"))
    parser.add_argument(
        "--embargo", type=int, default=0,
        help="same meaning as ml_v2_experiment_1.py --embargo; 0 is headline, "
             "5 is the robustness variant.",
    )
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        panel, feature_sets = build_experiment_panel(conn)
    splits, split_report = make_walk_forward_splits(
        panel, embargo=args.embargo, return_report=True,
    )
    print(
        f"Common panel {len(panel)} rows / {panel['date'].nunique()} dates | "
        f"{len(splits)} folds (embargo={args.embargo}) | split {split_digest(splits)}",
        flush=True,
    )
    print(
        f"Fold report: scored={split_report['n_folds_scored']} "
        f"skipped_too_thin={split_report['n_too_thin']} "
        f"infeasible={split_report['n_infeasible']} "
        f"nominal={split_report['n_folds_nominal']}",
        flush=True,
    )
    table, predictions = run_experiment(panel, feature_sets, splits)
    digest = prediction_digest(predictions)
    if ACCEPTED_PREDICTION_DIGEST is None:
        # Experiment #1E has not been pinned yet. Report the digest so it can be
        # reviewed and pinned deliberately; do NOT fall back to comparing against
        # LEGACY_CLOSE_CONTRACT_DIGEST, which belongs to a different contract and
        # cannot be reproduced under next-open execution by construction.
        print(
            f"Experiment #1E digest (executable contract, UNPINNED): {digest}\n"
            f"  legacy close-contract digest, frozen: {LEGACY_CLOSE_CONTRACT_DIGEST}",
            flush=True,
        )
    elif digest != ACCEPTED_PREDICTION_DIGEST:
        raise AssertionError(
            f"accepted predictions were not reproduced: {digest} != "
            f"{ACCEPTED_PREDICTION_DIGEST}"
        )
    else:
        print(f"Accepted prediction digest reproduced: {digest}", flush=True)

    pred_b = predictions["existing_broker_aggregate"]
    pred_c = predictions["broker_identity"]
    per_date = paired_date_differences(pred_b, pred_c)
    paired = paired_bootstrap_report(per_date)
    print("\nPAIRED PER-DATE ROBUSTNESS")
    print(paired.to_string(float_format=lambda value: f"{value:+.6f}"))

    ticker_loo, full_delta = leave_one_ticker_out(pred_b, pred_c)
    print("\nLEAVE-ONE-TICKER-OUT")
    _print_concentration_summary(ticker_loo, "removed_ticker", full_delta)
    print("  largest pooled-IC influences:")
    show = ticker_loo.reindex(ticker_loo["influence_pooled_ic"].abs().nlargest(8).index)
    print(show[["removed_ticker", "pooled_ic", "influence_pooled_ic",
                "mean_daily_ic", "hit_edge", "return_edge"]].to_string(
                    index=False, float_format=lambda value: f"{value:+.6f}"))

    print("\nLEAVE-ONE-BROKER-GROUP-OUT (refit, unchanged splits/params)", flush=True)
    broker_loo = broker_group_ablation(
        panel, feature_sets, splits, pred_b, full_delta,
    )
    _print_concentration_summary(broker_loo, "removed_broker", full_delta)
    print("  largest pooled-IC influences:")
    show = broker_loo.reindex(broker_loo["influence_pooled_ic"].abs().nlargest(10).index)
    print(show[["removed_broker", "pooled_ic", "influence_pooled_ic",
                "mean_daily_ic", "hit_edge", "return_edge"]].to_string(
                    index=False, float_format=lambda value: f"{value:+.6f}"))

    regime = ihsg_regime_audit(per_date, args.jci)
    print("\nIHSG REGIME AUDIT (existing local data only)")
    if regime is None:
        print("  skipped: no existing local IHSG context")
    else:
        matched, regime_table = regime
        print(
            f"  matched {matched['date'].nunique()}/{per_date['date'].nunique()} OOS dates, "
            f"{matched['date'].min().date()}..{matched['date'].max().date()}"
        )
        print(regime_table.to_string(float_format=lambda value: f"{value:+.6f}"))

    print("\nUNCHANGED FULL C-vs-B HEADLINE DELTA")
    print(pd.Series(full_delta).to_string(float_format=lambda value: f"{value:+.6f}"))


if __name__ == "__main__":
    main()
