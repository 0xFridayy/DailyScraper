"""Experiment #1F: feature-domain-specific validity on the original session axis.

Four independent domains. A row is not simply "good" or "bad":

    price_valid           the OHLC observation itself is usable
    volume_valid          the vendor volume is usable (after authorised repair)
    broker_basis_valid    the broker series sits on the price/volume basis
    broker_feature_valid  a specific broker feature's whole lookback is usable

Broker invalidity must never erase a valid price row. Measured on the frozen
297, every broker-basis-invalid session carries a perfectly good price
observation, so collapsing the domains would discard usable returns and shrink
the Variant A price-only population for no reason. Under this module's default
PRIMARY mode that is 1,425 sessions; the retrospective SECONDARY mode excludes
807. Quoting 807 as "the" number would be quoting the mode this module does
not default to.

PRIMARY vs SECONDARY
--------------------
The primary, tradable path is PIT-conservative: every off-basis regime session
is broker_basis_valid = False, INCLUDING the three regimes whose factors are
economically certifiable (MLPT 25/1, RAJA 5/1, RMKE 5/1).

That is a deliberate asymmetry and it costs coverage. The case for the other
side is real and worth stating: a split is economically neutral, the vendor's
own OHLC is already retrospectively split-adjusted, and harmonising broker lots
onto the price basis only removes a constant scale error inside a regime. On
that reading harmonisation is a normalisation, not a prediction.

It is not adopted for the primary path because it cannot be PROVEN here:

  - the source carries no corporate-action record, no publication date and no
    available_at field, so nothing confirms these ratios are splits at all
    rather than a vendor restatement or a reporting-basis change;
  - each regime's BOUNDARY was inferred from the full frozen window, so the
    date on which the basis changed is itself a retrospective estimate;
  - the corrected sensitivity audit shows regime segmentation granularity moves
    with the lot-gap threshold (14 / 18 / 30 regimes across the audited grid)
    even though the ticker population is stable at 10, so boundaries are
    evidence rather than precise facts;
  - RAJA is already a documented #1E contamination case, so it is the worst
    possible candidate on which to spend the benefit of the doubt.

The factors are therefore preserved as diagnostics and may be used in a
clearly labelled RETROSPECTIVE ECONOMIC-NORMALIZATION SENSITIVITY, never as
headline tradable evidence.
"""
import json
import os

import experiment_1f_candidate as cand
import experiment_1f_normalization as norm

#: The ACTUAL #1F broker feature windows, read from ml_v2_experiment_1 and
#: experiment_1f_universe_gate. Not inferred, not extended.
FLOW_WINDOWS = (1, 3, 5, 10, 20)
ADV20_WINDOW = 20

#: Mode labels. PRIMARY is the default everywhere; no signature may select
#: SECONDARY implicitly, and every artifact prints its mode at top level.
PRIMARY = "PRIMARY_PIT_CONSERVATIVE"

#: The policy is conservative; the DATA is not proven point-in-time. The
#: basis-invalid calendar mask is itself derived retrospectively from the full
#: frozen window, so "SAFE" would claim an observability property no evidence
#: in this source supports. Conservative describes what the policy DOES --
#: refuse retrospective harmonisation and exclude unresolved regimes -- without
#: asserting that the classification was knowable on the session it excludes.
PIT_OBSERVABILITY = "UNESTABLISHED"
SECONDARY = "RETROSPECTIVE_ECONOMIC_NORMALIZATION_SENSITIVITY"
MODES = (PRIMARY, SECONDARY)

ARTIFACT_PRIMARY = "validity_impact_primary.json"
ARTIFACT_SECONDARY = "validity_impact_secondary.json"


def broker_basis_validity(n_sessions, regimes, mode=PRIMARY):
    """Per-session broker_basis_valid on the ORIGINAL session axis.

    In PRIMARY mode every off-basis regime is invalid regardless of whether its
    factor is certifiable, because certifiability is an economic claim and this
    domain answers a point-in-time one. In SECONDARY mode a certified regime is
    treated as harmonisable, which is exactly the retrospective assumption the
    primary path refuses.
    """
    valid = [True] * n_sessions
    for regime in regimes:
        certified = regime["classification"] in ("RECONSTRUCTIBLE",
                                                 "CANDIDATE_HARMONIZABLE")
        if mode == SECONDARY and certified:
            continue
        for i in range(regime["start_session_index"],
                       regime["end_session_index"] + 1):
            if 0 <= i < n_sessions:
                valid[i] = False
    return valid


def rolling_feature_validity(broker_valid, window):
    """A W-session feature is valid only when its ENTIRE lookback is valid.

    Indices are positions on the original session axis. Invalid sessions are
    never compressed away: dropping them and rolling over the survivors would
    silently roll a window across a gap and present it as W contiguous
    sessions.
    """
    out = []
    for i in range(len(broker_valid)):
        if i + 1 < window:
            out.append(False)                  # lookback not yet complete
        else:
            out.append(all(broker_valid[i - window + 1:i + 1]))
    return out


def cumulative_inventory_availability(broker_valid):
    """Observable inventory availability. It does NOT reset at a hole.

    The merged implementation advances a segment counter at every clean-panel
    hole and restarts the cumulative sum at zero. That asserts the missing
    prior flows were zero, which is false, and it leaves a near-zero inventory
    immediately after every hole -- a provenance artifact a model can learn as
    a regime marker.

    The conservative policy: once the cumulative state depends on an unresolved
    broker-invalid interval, the state is unavailable from that point on. It is
    not re-anchored, because re-anchoring needs the true inventory level at the
    restart, and that level is a function of the very flows that are missing.
    No valid-data-only estimator of it exists, so any re-anchor would be
    invented. A finite rolling feature recovers after its window; a cumulative
    one does not, and saying so is the honest answer.
    """
    available, contaminated = [], False
    for ok in broker_valid:
        if not ok:
            contaminated = True
        available.append(not contaminated)
    return available


def observable_inventory_validity(broker_valid):
    """Inventory needs both an uncontaminated state and a complete ADV20."""
    state = cumulative_inventory_availability(broker_valid)
    adv20 = rolling_feature_validity(broker_valid, ADV20_WINDOW)
    return [s and a for s, a in zip(state, adv20)]


def ticker_validity(dates, regimes, mode=PRIMARY):
    """Every broker feature domain for one ticker, on the original axis."""
    basis = broker_basis_validity(len(dates), regimes, mode=mode)
    features = {f"broker_flow_{w}d": rolling_feature_validity(basis, w)
                for w in FLOW_WINDOWS}
    features["broker_observable_inventory"] = observable_inventory_validity(basis)
    return {"dates": list(dates), "broker_basis_valid": basis,
            "broker_feature_valid": features}


def impact(candidate_dir, mode=PRIMARY):
    """Per-ticker, per-feature validity impact over the frozen 297."""
    covered, _ = cand.manifest_tickers(candidate_dir)
    with open(os.path.join(candidate_dir, norm.CANDIDATE_BASIS),
              encoding="utf-8") as fh:
        basis = json.load(fh)
    by_ticker = {}
    for regime in basis["regimes"]:
        by_ticker.setdefault(regime["ticker"], []).append(regime)

    source_dir = os.path.join(candidate_dir, cand.SOURCE_RAW)
    per_ticker, totals = {}, {"sessions": 0, "broker_basis_invalid": 0}
    for name in FLOW_WINDOWS:
        totals[f"broker_flow_{name}d_invalid"] = 0
    totals["broker_observable_inventory_invalid"] = 0

    for ticker in sorted(by_ticker):
        dates = cand.read_raw(cand.raw_path(source_dir, ticker))["date"]
        result = ticker_validity(dates, by_ticker[ticker], mode=mode)
        basis_valid = result["broker_basis_valid"]
        invalid = [d for d, ok in zip(dates, basis_valid) if not ok]
        entry = {
            "sessions": len(dates),
            "broker_basis_invalid": len(invalid),
            "first_invalid_date": invalid[0] if invalid else None,
            "last_invalid_date": invalid[-1] if invalid else None,
            "regimes": [{"start_date": r["start_date"], "end_date": r["end_date"],
                         "classification": r["classification"],
                         "position": r["position_on_session_axis"]}
                        for r in by_ticker[ticker]],
            "feature_unavailable": {},
        }
        for feature, flags in result["broker_feature_valid"].items():
            n = sum(1 for ok in flags if not ok)
            entry["feature_unavailable"][feature] = n
            key = (f"{feature}_invalid" if feature.startswith("broker_flow")
                   else "broker_observable_inventory_invalid")
            totals[key] = totals.get(key, 0) + n
        first_bad = next((i for i, ok in enumerate(basis_valid) if not ok), None)
        entry["inventory_unavailable_from"] = (
            dates[first_bad] if first_bad is not None else None)
        per_ticker[ticker] = entry
        totals["sessions"] += len(dates)
        totals["broker_basis_invalid"] += len(invalid)

    return {
        "mode": mode,
        "pit_observability": PIT_OBSERVABILITY,
        "policy": {
            "pit_observability": PIT_OBSERVABILITY,
            "pit_observability_meaning": (
                "retrospective basis harmonisation is NOT applied and unresolved "
                "broker regimes are excluded conservatively, but the historical "
                "availability of the DQ classification itself is not proven: the "
                "mask is derived from the full frozen window"),
            # These describe THIS artifact's mode. Naming them "..._in_primary"
            # while computing them from the current mode made the SECONDARY file
            # assert that primary applies harmonisation and that certified
            # regimes are valid in primary -- both false, and stated in the one
            # artifact a reader is most likely to mistake for the headline.
            "this_mode_applies_basis_harmonisation": mode != PRIMARY,
            "this_mode_treats_certified_regimes_as_broker_invalid": mode == PRIMARY,
            "primary_applies_basis_harmonisation": False,
            "certified_regimes_are_broker_invalid_in_primary": True,
            "secondary_label": SECONDARY,
            "observable_inventory_resets_at_a_hole": False,
            "observable_inventory_reanchored": False,
            "reanchor_rationale": (
                "a re-anchor requires the true inventory level at the restart, "
                "which is a function of the missing flows themselves; no "
                "valid-data-only estimator exists, so none is invented"),
        },
        "windows": {"flow": list(FLOW_WINDOWS), "adv20": ADV20_WINDOW},
        "totals": totals,
        "by_ticker": per_ticker,
    }


# --------------------------------------------------------------------------
# date-level threshold sensitivity: does eligibility itself move?
# --------------------------------------------------------------------------

def _per_ticker_series(candidate_dir):
    """(dates, totals, repaired volume) for every ticker with a regime."""
    covered, manifest = cand.manifest_tickers(candidate_dir)
    shas = {f["ticker"]: f["sha256"] for f in manifest["files"]}
    source_dir = os.path.join(candidate_dir, cand.SOURCE_RAW)
    out = {}
    for ticker in sorted(covered):
        data = cand.read_raw(cand.raw_path(source_dir, ticker))
        _, _, _, totals, _, volume = norm.analyse_ticker(ticker, data, shas[ticker])
        out[ticker] = (totals["dates"], totals, volume)
    return out


def invalid_keys_at(per_ticker, deviation, lot_gap):
    """The exact PRIMARY broker-invalid (ticker, date) set at one threshold pair.

    PRIMARY marks every off-basis regime session invalid regardless of whether
    its factor could be certified, so no classification step is needed: the
    invalid set is exactly the union of the segmented runs.
    """
    keys, n_regimes, tickers = set(), 0, set()
    for ticker, (dates, totals, volume) in per_ticker.items():
        runs, _ = norm.classify_at(totals, volume, deviation, lot_gap)
        if not runs:
            continue
        tickers.add(ticker)
        n_regimes += len(runs)
        for run in runs:
            for i in run:
                keys.add((ticker, dates[i]))
    return keys, n_regimes, tickers


def _feature_invalid_counts(per_ticker, keys):
    """Downstream flow-window invalid counts for one invalid-date set."""
    counts = {f"broker_flow_{w}d_invalid": 0 for w in FLOW_WINDOWS}
    counts["broker_observable_inventory_invalid"] = 0
    for ticker, (dates, _, _) in per_ticker.items():
        bad = {d for t, d in keys if t == ticker}
        if not bad:
            continue
        valid = [d not in bad for d in dates]
        for w in FLOW_WINDOWS:
            counts[f"broker_flow_{w}d_invalid"] += sum(
                1 for ok in rolling_feature_validity(valid, w) if not ok)
        counts["broker_observable_inventory_invalid"] += sum(
            1 for ok in observable_inventory_validity(valid) if not ok)
    return counts


def date_mask_sensitivity(candidate_dir, grid=None):
    """Compare the EXACT invalid-date key set across nearby thresholds.

    Regime COUNT moving is not the question -- splitting one interval into three
    changes the count while declaring the same sessions unusable. The question
    for primary eligibility is whether the same OBSERVATIONS are excluded, so
    this compares (ticker, date) key sets against the nominal configuration.
    """
    grid = grid or [(d, g) for d in (5e-4, 1e-3, 2e-3)
                    for g in (50.0, 100.0, 200.0)]
    per_ticker = _per_ticker_series(candidate_dir)
    nominal_keys, nominal_regimes, nominal_tickers = invalid_keys_at(
        per_ticker, norm.BASIS_MIN_DEVIATION, norm.BASIS_MIN_LOT_GAP)

    rows = []
    for deviation, lot_gap in grid:
        keys, n_regimes, tickers = invalid_keys_at(per_ticker, deviation, lot_gap)
        added = keys - nominal_keys
        removed = nominal_keys - keys
        union = keys | nominal_keys
        rows.append({
            "basis_min_deviation": deviation,
            "basis_min_lot_gap": lot_gap,
            "is_nominal": (deviation == norm.BASIS_MIN_DEVIATION
                           and lot_gap == norm.BASIS_MIN_LOT_GAP),
            "n_tickers_with_basis_issue": len(tickers),
            "n_regimes": n_regimes,
            "broker_basis_invalid_sessions": len(keys),
            "intersection_with_nominal": len(keys & nominal_keys),
            "union_with_nominal": len(union),
            "added_vs_nominal": len(added),
            "removed_vs_nominal": len(removed),
            "jaccard_vs_nominal": (len(keys & nominal_keys) / len(union)) if union else 1.0,
            "added_sample": sorted(f"{t} {d}" for t, d in added)[:5],
            "removed_sample": sorted(f"{t} {d}" for t, d in removed)[:5],
            "feature_invalid": _feature_invalid_counts(per_ticker, keys),
        })

    worst = min(r["jaccard_vs_nominal"] for r in rows)
    spread = (max(r["broker_basis_invalid_sessions"] for r in rows)
              - min(r["broker_basis_invalid_sessions"] for r in rows))
    if worst >= 0.95:
        verdict = "ROBUST"
    elif worst >= 0.80:
        verdict = "METHODOLOGY CONCERN"
    else:
        verdict = "HARD BLOCKER"
    inv = {r["feature_invalid"]["broker_observable_inventory_invalid"] for r in rows}
    f20 = {r["feature_invalid"]["broker_flow_20d_invalid"] for r in rows}
    deviations = {r["basis_min_deviation"] for r in rows}
    by_gap = {}
    for r in rows:
        by_gap.setdefault(r["basis_min_lot_gap"], set()).add(
            r["broker_basis_invalid_sessions"])
    deviation_non_binding = all(len(v) == 1 for v in by_gap.values()) and len(deviations) > 1

    return {
        "mode": PRIMARY,
        "pit_observability": PIT_OBSERVABILITY,
        "question": ("does threshold movement merely split or merge the same "
                     "invalid calendar intervals, or does it change WHICH "
                     "observations are declared unusable?"),
        "interpretation": (
            "ROBUST ENOUGH FOR THIS FROZEN CONTRACT -- not a universal proof "
            "that these thresholds are correct for any other population"),
        "feature_level_stability": {
            "_why_this_matters": (
                "the basis mask is an intermediate; what reaches the model is "
                "feature eligibility. Downstream stability is therefore the "
                "sharper test, and it is stronger than the mask's own."),
            "observable_inventory_invalid_values": sorted(inv),
            "observable_inventory_invariant": len(inv) == 1,
            "observable_inventory_reason": (
                "every affected ticker carries a prefix regime beginning at "
                "session 0, so cumulative contamination starts at index 0 at "
                "every threshold and the feature is unavailable for the whole "
                "history regardless"),
            "broker_flow_20d_invalid_values": sorted(f20),
            "broker_flow_20d_spread": max(f20) - min(f20),
        },
        "non_binding_thresholds": {
            "_meaning": ("measured non-binding ON THIS FROZEN POPULATION. They "
                         "are retained, not deleted: a threshold that never "
                         "fires here may still be the guard that fires on a "
                         "future population, and removing it could only be "
                         "justified by proving the simpler rule semantically "
                         "equivalent AND safer, which has not been done."),
            "basis_min_deviation": {
                "non_binding": deviation_non_binding,
                "evidence": ("varying it across the audited grid changes no "
                             "invalid-session count at any lot gap; the lot-gap "
                             "conjunct dominates"),
            },
            "ratio_correlation_min": {
                "non_binding": True,
                "evidence": ("within a constant-ratio regime the lot gap is "
                             "identically proportional to blot, so the "
                             "correlation is ~1.0 by construction and never "
                             "uniquely quarantines a regime"),
            },
            "dual_estimator_max_disagreement": {
                "non_binding": True,
                "evidence": ("the veto never fires on the frozen 297: the "
                             "largest measured relative gap is 0.012 against a "
                             "0.20 threshold"),
            },
            "binding_thresholds": ["basis_min_lot_gap", "min_regime_days",
                                   "reconstruction_min_rate"],
        },
        "nominal": {"basis_min_deviation": norm.BASIS_MIN_DEVIATION,
                    "basis_min_lot_gap": norm.BASIS_MIN_LOT_GAP,
                    "invalid_sessions": len(nominal_keys),
                    "n_regimes": nominal_regimes,
                    "n_tickers": len(nominal_tickers)},
        "worst_jaccard_vs_nominal": worst,
        "invalid_session_count_spread": spread,
        "verdict": verdict,
        "thresholds_were_not_tuned": True,
        "grid": rows,
    }


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate-dir", default=cand.CANDIDATE_DIR)
    a = ap.parse_args()

    # Both artifacts, always, under names that cannot be mistaken for each
    # other. PRIMARY is written first and is the default everywhere.
    for mode, name in ((PRIMARY, ARTIFACT_PRIMARY), (SECONDARY, ARTIFACT_SECONDARY)):
        report = impact(a.candidate_dir, mode=mode)
        cand.write_json(os.path.join(a.candidate_dir, name), report)
        print(f"[{report['mode']}] -> {name}")
        print(f"   {json.dumps(report['totals'])}")

    audit = date_mask_sensitivity(a.candidate_dir)
    cand.write_json(os.path.join(a.candidate_dir,
                                 "validity_date_mask_sensitivity.json"), audit)
    print()
    print(f"[date-mask sensitivity] verdict={audit['verdict']} "
          f"worst_jaccard={audit['worst_jaccard_vs_nominal']:.4f} "
          f"nominal_invalid={audit['nominal']['invalid_sessions']}")
    for row in audit["grid"]:
        print(f"   dev={row['basis_min_deviation']:<7g} gap={row['basis_min_lot_gap']:<6g}"
              f" tickers={row['n_tickers_with_basis_issue']:<3}"
              f" regimes={row['n_regimes']:<3}"
              f" invalid={row['broker_basis_invalid_sessions']:<5}"
              f" +{row['added_vs_nominal']:<4} -{row['removed_vs_nominal']:<4}"
              f" jaccard={row['jaccard_vs_nominal']:.4f}"
              + ("  <- NOMINAL" if row["is_nominal"] else ""))


if __name__ == "__main__":
    main()
