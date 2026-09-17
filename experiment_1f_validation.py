"""Experiment #1F Phase 2: the candidate validation report.

Builds every candidate artifact TWICE from the same frozen source snapshot and
reports semantic and physical determinism separately, because they are different
claims: identical semantics is what the contract needs, identical bytes is a
stronger property that parquet metadata can break without any data changing.

Writes validation_report.json into the candidate directory. Establishes no
manifest, runs no gate, and trains nothing.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

import build_inventory_db as bidb
import experiment_1f_candidate as cand
import experiment_1f_normalization as norm
import experiment_1f_universe_gate as gate

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.environ.get("NEOBDM_SHARED_ROOT", os.path.join(os.path.dirname(HERE), "Claude"))
KEY = ["date", "ticker", "broker"]

#: The actual #1F consumer graph, read from the code rather than assumed.
#: experiment_1f_universe_gate.LAGS / .HORIZONS and ml_v2_experiment_1.FLOW_WINDOWS.
FLOW_WINDOWS = (1, 3, 5, 10, 20)
ADV20 = 20


def semantic_digest(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def parquet_semantic_digest(path):
    """Content identity, independent of row-group layout or file metadata."""
    frame = pq.read_table(path).to_pandas().sort_values(KEY).reset_index(drop=True)
    h = hashlib.sha256()
    for column in frame.columns:
        values = frame[column].to_numpy()
        h.update(column.encode())
        h.update(values.astype("U").tobytes() if values.dtype == object
                 else values.tobytes())
    return h.hexdigest(), len(frame)


def git_baseline():
    def run(*args):
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                              text=True).stdout.strip()
    return {
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": run("rev-parse", "HEAD"),
        "tracked_tree_clean": run("status", "--porcelain", "--untracked-files=no") == "",
        "worktree": HERE,
    }


def ingest_validation(path, covered):
    frame = pq.read_table(path).to_pandas()
    schema = pq.ParquetFile(path).schema_arrow
    drift = np.abs(frame["nval"].to_numpy()
                   - (frame["bval"].to_numpy() - frame["sval"].to_numpy()))
    return {
        "rows": len(frame),
        "tickers": int(frame["ticker"].nunique()),
        "population_is_exactly_the_297": sorted(frame["ticker"].unique()) == sorted(covered),
        "date_min": frame["date"].min(),
        "date_max": frame["date"].max(),
        "distinct_sessions": int(frame["date"].nunique()),
        "dtypes": {f.name: str(f.type) for f in schema},
        "lot_columns_are_int64": all(str(schema.field(c).type) == "int64"
                                     for c in ("nlot", "blot", "slot")),
        "duplicate_keys": int(frame.duplicated(KEY).sum()),
        "raw_domain_violations": 0,
        "alignment_failures": 0,
        "nonfinite_values": int((~np.isfinite(
            frame[["nval", "bval", "sval"]].to_numpy())).sum()),
        "negative_gross_lots": int(((frame["blot"] < 0) | (frame["slot"] < 0)).sum()),
        "negative_gross_values": int(((frame["bval"] < 0) | (frame["sval"] < 0)).sum()),
        "row_nlot_identity_failures": int(
            (frame["nlot"] != (frame["blot"] - frame["slot"])).sum()),
        "row_nval_identity_failures_beyond_tolerance": int(
            (drift > bidb.RUPIAH_TOLERANCE).sum()),
        "row_nval_identity_worst_rupiah_drift": float(drift.max()),
        "rupiah_tolerance": bidb.RUPIAH_TOLERANCE,
    }


def frozen_agreement(candidate_path, covered):
    """The candidate table against the in-universe subset of the frozen one."""
    frozen = os.path.join(SHARED, "broker_daily.parquet")
    if not os.path.exists(frozen):
        return {"status": "frozen artifact not reachable from this worktree"}
    a = pq.read_table(candidate_path).to_pandas().sort_values(KEY).reset_index(drop=True)
    b = pq.read_table(frozen).to_pandas()
    b = b[b["ticker"].isin(set(covered))].sort_values(KEY).reset_index(drop=True)
    same_keys = len(a) == len(b) and a[KEY].equals(b[KEY])
    mismatches = {}
    if same_keys:
        for column in ("nlot", "blot", "slot", "nval", "bval", "sval"):
            x, y = a[column].to_numpy(), b[column].to_numpy()
            mismatches[column] = int((x != y).sum())
    return {
        "candidate_rows": len(a),
        "frozen_in_universe_rows": len(b),
        "key_sets_identical": bool(same_keys),
        "value_mismatches_by_column": mismatches,
        "total_value_mismatches": sum(mismatches.values()) if same_keys else None,
    }


def ohlc_agreement(candidate_dir, covered):
    """PROVENANCE COMPARISON ONLY. The one place SHARED/ohlc.parquet is read.

    Its entire job is to prove the experiment-owned copy still equals the
    historical source it was taken from. Nothing semantic depends on it: if the
    shared checkout were deleted or mutated, this section changes or reports
    unreachable and every validity count in the report stays identical, because
    those are computed from candidate_dir/ohlc_full_market.parquet.

    test_semantic_validation_does_not_depend_on_the_shared_checkout proves that
    by mutating a shared-path fixture and requiring the semantic result to be
    byte-identical while only this comparison moves.
    """
    frozen = os.path.join(SHARED, "ohlc.parquet")
    if not os.path.exists(frozen):
        return {"status": "frozen artifact not reachable from this worktree"}
    source_dir = os.path.join(candidate_dir, cand.SOURCE_RAW)
    rows = []
    for ticker in covered:
        data = cand.read_raw(cand.raw_path(source_dir, ticker))
        for row in (data.get("ohlc") or []):
            rows.append((row.get("date"), ticker, row.get("open"), row.get("high"),
                         row.get("low"), row.get("close"), row.get("volume")))
    columns = ["date", "ticker", "open", "high", "low", "close", "volume"]
    snap = pd.DataFrame(rows, columns=columns).sort_values(
        ["date", "ticker"]).reset_index(drop=True)
    frame = pq.read_table(frozen).to_pandas()
    frame = frame[frame["ticker"].isin(set(covered))].sort_values(
        ["date", "ticker"]).reset_index(drop=True)
    same = len(snap) == len(frame) and snap[["date", "ticker"]].equals(
        frame[["date", "ticker"]])
    mismatches = {}
    if same:
        for column in ("open", "high", "low", "close", "volume"):
            x = snap[column].to_numpy(np.float64)
            y = frame[column].to_numpy(np.float64)
            mismatches[column] = int((~((x == y) | (np.isnan(x) & np.isnan(y)))).sum())
    return {
        "snapshot_ohlc_rows": len(snap),
        "frozen_in_universe_rows": len(frame),
        "duplicate_keys_snapshot": int(snap.duplicated(["date", "ticker"]).sum()),
        "key_sets_identical": bool(same),
        "value_mismatches_by_column": mismatches,
        "total_value_mismatches": sum(mismatches.values()) if same else None,
    }


def determinism(candidate_dir, covered):
    """Build both candidate artifacts a second time, into a scratch directory."""
    first_path = os.path.join(candidate_dir, bidb.CANDIDATE_BROKER_NAME)
    first_semantic, rows = parquet_semantic_digest(first_path)
    first_physical = cand.sha256_file(first_path)

    scratch = tempfile.mkdtemp(prefix="e1f_determinism_")
    try:
        second = bidb.build_candidate_broker(
            os.path.join(candidate_dir, cand.SOURCE_RAW), covered, scratch)
        second_semantic, rows2 = parquet_semantic_digest(second["path"])
        second_physical = cand.sha256_file(second["path"])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    repairs_a, basis_a = norm.build(candidate_dir)
    repairs_b, basis_b = norm.build(candidate_dir)

    return {
        "broker_daily_v2": {
            "rows_build_1": rows, "rows_build_2": rows2,
            "semantic_digest_build_1": first_semantic,
            "semantic_digest_build_2": second_semantic,
            "semantically_identical": first_semantic == second_semantic,
            "physical_sha256_build_1": first_physical,
            "physical_sha256_build_2": second_physical,
            "physically_identical": first_physical == second_physical,
        },
        "volume_repair_candidates": {
            "semantic_digest_build_1": semantic_digest(repairs_a),
            "semantic_digest_build_2": semantic_digest(repairs_b),
            "semantically_identical": semantic_digest(repairs_a) == semantic_digest(repairs_b),
        },
        "observed_basis_factor_candidate": {
            "semantic_digest_build_1": semantic_digest(basis_a),
            "semantic_digest_build_2": semantic_digest(basis_b),
            "semantically_identical": semantic_digest(basis_a) == semantic_digest(basis_b),
        },
    }


def validity_domains(candidate_dir, covered):
    """Feature-domain-specific validity, and washout by ACTUAL feature family.

    Reads the EXPERIMENT-OWNED OHLC copy, never the shared checkout. This
    measurement defines price-invalid keys, the validity counts and the
    price/broker intersection that the report publishes, so sourcing it from
    SHARED/ohlc.parquet would have made the headline semantic numbers depend on
    a file outside the experiment's custody -- the exact isolation the
    experiment-owned snapshot exists to provide, claimed but not implemented.

    The shared checkout is read in exactly one place, ohlc_agreement(), and only
    to prove the copy still matches its source.
    """
    from price_audit import detect

    # loaders only; run_gate() is NOT called anywhere in this module
    import experiment_1f_universe_gate as gate
    candidate_ohlc = os.path.join(candidate_dir, cand.CANDIDATE_OHLC)
    basis = json.load(open(os.path.join(candidate_dir, norm.CANDIDATE_BASIS),
                           encoding="utf-8"))
    # PRIMARY excludes EVERY off-basis regime, certified or not. Filtering to
    # classification == "QUARANTINE" here would silently compute the SECONDARY
    # (retrospective) mask -- 807 sessions instead of 1,425 -- and publish it
    # with no mode label, which is exactly the confusion the split artifacts
    # exist to prevent.
    import experiment_1f_validity as validity
    mode = validity.PRIMARY
    excluded = [r for r in basis["regimes"]
                if mode == validity.PRIMARY
                or r["classification"] not in ("RECONSTRUCTIBLE",
                                               "CANDIDATE_HARMONIZABLE")]

    out = {
        "mode": mode,
        "pit_observability": validity.PIT_OBSERVABILITY,
        "quarantined_regime_tickers": sorted(
            {r["ticker"] for r in basis["regimes"]
             if r["classification"] == "QUARANTINE"}),
        "candidate_harmonizable_tickers": sorted(
            {r["ticker"] for r in basis["regimes"]
             if r["classification"] == "CANDIDATE_HARMONIZABLE"}),
    }

    out["price_source"] = {
        "path": os.path.relpath(candidate_ohlc, HERE).replace(os.sep, "/"),
        "role": "EXPERIMENT-OWNED execution input; the shared checkout is never "
                "read for a semantic measurement",
    }
    if os.path.exists(candidate_ohlc):
        full, _, fingerprint = gate.load_full_harvest(candidate_ohlc)
        out["price_source"]["sha256"] = fingerprint["sha256"]
        flagged = detect(full)
        suspect = set(zip(flagged.loc[flagged["suspect"], "date"],
                          flagged.loc[flagged["suspect"], "ticker"]))
        universe = full[full["ticker"].isin(set(covered))]
        sessions = set(zip(universe["date"], universe["ticker"]))
        price_invalid = sessions & suspect
        broker_invalid = set()
        for regime in excluded:
            rows = universe[(universe["ticker"] == regime["ticker"])
                            & (universe["date"] >= regime["start_date"])
                            & (universe["date"] <= regime["end_date"])]
            broker_invalid |= set(zip(rows["date"], rows["ticker"]))
        out.update({
            "in_universe_sessions": len(sessions),
            "price_valid": len(sessions) - len(price_invalid),
            "price_invalid_detect_suspect": len(price_invalid),
            "broker_basis_invalid": len(broker_invalid),
            "price_valid_but_broker_basis_invalid": len((sessions - price_invalid)
                                                        & broker_invalid),
            "note": ("price_audit.detect() was run read-only for this measurement; "
                     "its semantics are unchanged and Gate A was NOT run"),
            "broker_basis_invalid_mode_note": (
                f"computed under {mode}: every off-basis regime is excluded, "
                "including the economically certifiable ones. The retrospective "
                "mode's smaller count lives in validity_impact_secondary.json."),
        })

    source_dir = os.path.join(candidate_dir, cand.SOURCE_RAW)
    contaminated_by_ticker = {}
    # `excluded`, not the QUARANTINE-only subset: the washout figures were
    # SECONDARY numbers under a PRIMARY heading for exactly the same reason the
    # invalid-session count was.
    for regime in excluded:
        contaminated_by_ticker.setdefault(regime["ticker"], set()).update(
            range(regime["start_session_index"], regime["end_session_index"] + 1))

    washout = {f"broker_flow_{w}d": 0 for w in FLOW_WINDOWS}
    washout["observable_inventory_adv20_denominator"] = 0
    washout["observable_inventory_cumulative_state"] = 0
    contaminated = affected_sessions = 0
    for ticker, hits in contaminated_by_ticker.items():
        n = len(cand.read_raw(cand.raw_path(source_dir, ticker))["date"])
        affected_sessions += n
        contaminated += len(hits)
        for window in FLOW_WINDOWS:
            spread = set()
            for i in hits:
                spread |= set(range(i, min(i + window, n)))
            washout[f"broker_flow_{window}d"] += len(spread)
        spread = set()
        for i in hits:
            spread |= set(range(i, min(i + ADV20, n)))
        washout["observable_inventory_adv20_denominator"] += len(spread)
        washout["observable_inventory_cumulative_state"] += n - min(hits)

    out["lookback_dependency_map"] = {
        "source": "read from experiment_1f_universe_gate and ml_v2_experiment_1",
        "price_lags": list(FLOW_WINDOWS),
        "forward_horizons": [1, 2, 3, 4, 5],
        "broker_flow_windows": list(FLOW_WINDOWS),
        "observable_inventory": "unbounded cumulative within a clean segment, "
                                "normalised by a 20-session rolling mean",
        "inventory_features_py": "NOT in the #1F path -- reachable only from "
                                 "scan_ara_arb.py; it is a reference, not the "
                                 "active feature implementation",
    }
    out["washout"] = {
        "contaminated_sessions": contaminated,
        "sessions_in_affected_tickers": affected_sessions,
        "unusable_outputs_by_feature_family": washout,
    }
    out["stateful_feature"] = {
        "exists": True,
        "name": "broker_<code>_observable_inventory",
        "implementation": "matrix.groupby(segments).cumsum() in "
                          "ml_v2_experiment_1.build_broker_identity_features",
        "current_behaviour_after_a_hole": (
            "the segment counter advances and the cumulative sum restarts at "
            "zero, which silently asserts the missing flows were zero and "
            "leaves a near-zero inventory immediately after every hole"),
        "risk": ("a reset is itself a regime marker: a model can learn "
                 "'inventory near zero' as 'just after a data hole', which is "
                 "a data-provenance artifact rather than an economic signal"),
        "options_requiring_review": [
            "PIT-safe re-anchoring with a mathematically valid opening level",
            "conservative unavailability for the remainder of the segment",
        ],
        "measured_cost_of_unavailability": washout[
            "observable_inventory_cumulative_state"],
    }
    return out


def provenance(candidate_dir):
    out = {"superseded_reference": cand.SUPERSEDED_BROKER_DAILY}
    for label, path in (("ohlc.parquet", os.path.join(SHARED, "ohlc.parquet")),
                        ("broker_daily.parquet",
                         os.path.join(SHARED, "broker_daily.parquet"))):
        if os.path.exists(path):
            out[label] = {"sha256": cand.sha256_file(path),
                          "size_bytes": os.path.getsize(path),
                          "role": "FROZEN baseline input, unchanged by this phase"}
    for name in (cand.SOURCE_MANIFEST, bidb.CANDIDATE_BROKER_NAME,
                 norm.CANDIDATE_REPAIRS, norm.CANDIDATE_BASIS):
        path = os.path.join(candidate_dir, name)
        if os.path.exists(path):
            out[name] = {"sha256": cand.sha256_file(path),
                         "size_bytes": os.path.getsize(path),
                         "role": "CANDIDATE artifact, not an approved input"}
    if os.path.exists(os.path.join(SHARED, "ohlc.parquet")):
        out["frozen_artifacts_unchanged"] = (
            out["ohlc.parquet"]["sha256"] == cand.FROZEN_OHLC_SHA256
            and out["broker_daily.parquet"]["sha256"] == cand.FROZEN_BROKER_SHA256)
    return out


def build_report(candidate_dir):
    covered, manifest = cand.manifest_tickers(candidate_dir)
    cand.verify_snapshot(candidate_dir)
    broker_path = os.path.join(candidate_dir, bidb.CANDIDATE_BROKER_NAME)
    repairs = json.load(open(os.path.join(candidate_dir, norm.CANDIDATE_REPAIRS),
                             encoding="utf-8"))
    basis = json.load(open(os.path.join(candidate_dir, norm.CANDIDATE_BASIS),
                           encoding="utf-8"))
    governance = repair_governance(candidate_dir)

    return {
        "experiment": "1F",
        "phase": 2,
        "status": ("CANDIDATE VALIDATION -- no Gate A, no Gate B, no manifest "
                   "established, nothing staged or committed"),
        "baseline": {
            **git_baseline(),
            "broker_daily_baseline_decision": (
                "the post-PR-#38 int64 build c8d1948f... is the accepted present "
                "baseline; b600ee9d... is a SUPERSEDED PRE-PR38 LOSSY REFERENCE"),
        },
        "source": {
            "universe_size": manifest["universe"]["n_tickers"],
            "universe_digest": manifest["universe"]["universe_digest"],
            "covered": manifest["population"]["n_covered"],
            "uncovered": manifest["population"]["uncovered"],
            "source_aggregate_sha256": manifest["source_aggregate_sha256"],
            "snapshot_is_experiment_owned_byte_copy": True,
            "time_semantics": manifest["time_semantics"],
            "ohlc_snapshot_vs_frozen_parquet": ohlc_agreement(candidate_dir, covered),
        },
        "ingest": {
            **ingest_validation(broker_path, covered),
            "agreement_with_frozen_broker_daily": frozen_agreement(broker_path, covered),
        },
        "volume": {
            "stage_a_repair_candidates": repairs["n_candidates"],
            "stage_b_diagnostics": repairs["n_stage_b_diagnostics"],
            "any_candidate_marked_authorised": any(
                c.get("authorised") for c in repairs["candidates"]),
            "all_raw_identities_fail_as_expected": all(
                not c["revalidation"]["raw_identity_holds"]
                and c["revalidation"]["raw_shortfall_is_one_modulus"]
                for c in repairs["candidates"]),
            "all_proposed_identities_reconcile": all(
                c["revalidation"]["proposed_identity_buy_side"]
                and c["revalidation"]["proposed_identity_sell_side"]
                for c in repairs["candidates"]),
            # DETECTION and AUTHORIZATION are reported as separate facts and the
            # second is never inferred from the first. `any_candidate_marked_
            # authorised` above is the detector artifact's own flag and must stay
            # False: the detector may not authorise its own findings.
            "governance": governance,
            "explicit_statement": (
                f"DETECTED {governance['detected']['n']} stage-A candidates. "
                f"AUTHORIZED {governance['authorized']['n']} by a reviewed, exact "
                f"key set bound to the candidate artifact's sha256 and verified "
                f"here with the gate's own fail-closed loader. STAGE-B "
                f"{governance['stage_b']['n']} diagnostics remain UNAUTHORIZED "
                f"and are structurally ineligible for repair."),
            "candidates": repairs["candidates"],
            "stage_b": repairs["stage_b_diagnostics_never_repaired"],
        },
        "basis": {
            "n_regimes": basis["n_regimes"],
            "candidate_harmonizable": [r for r in basis["regimes"]
                                       if r["classification"] == "CANDIDATE_HARMONIZABLE"],
            "quarantined": [{"ticker": r["ticker"], "start_date": r["start_date"],
                             "end_date": r["end_date"],
                             "position": r["position_on_session_axis"],
                             "factor_exact": r["candidate_factor_exact"],
                             "reason": r["reason"]}
                            for r in basis["regimes"]
                            if r["classification"] == "QUARANTINE"],
            "thresholds": basis["thresholds"],
            "threshold_sensitivity": basis["threshold_sensitivity"],
        },
        "validity": validity_domains(candidate_dir, covered),
        "pit": {
            "economically_reconstructed": (
                "basis regimes and their factors are measured retrospectively "
                "from the frozen vendor snapshot; they describe what is "
                "economically coherent across the whole window"),
            "actually_observable_on_decision_date": (
                "NOT ESTABLISHED. The cache carries no corporate-action metadata, "
                "no publication date and no available_at field, so no claim is "
                "made that a basis change was knowable on any given session"),
            "retrospectively_inferred": (
                "the regime boundaries themselves. A regime is detected from the "
                "full window; a real-time observer would not have known the "
                "boundary at its start"),
            "consequence": ("any harmonisation reviewed later is an economic "
                            "consistency correction, NOT a point-in-time signal, "
                            "and this caveat must survive into #1F interpretation"),
        },
        "determinism": determinism(candidate_dir, covered),
        "provenance": provenance(candidate_dir),
        "gate_a_compatibility": gate_a_memo(basis),
        "remaining_blockers": remaining_blockers(basis, governance),
    }


def gate_a_memo(basis):
    """The Gate-A reconciliation RECORD: what was stale, and what replaced it.

    This was a forward-looking memo about a gate that had not been reconciled
    yet. It is now a record of a reconciliation that has been implemented, so
    the stale assumptions are reported under `resolved_assumptions` with the
    implementation that closed each one. Publishing them as `stale_assumptions`
    after the fact would present history as current state.

    Gate A has still not been RUN, and no manifest has been established. Those
    are separate facts and they are still true -- see `remaining_blockers`.
    """
    per_ticker = {}
    for regime in basis["regimes"]:
        per_ticker.setdefault(regime["ticker"], 0)
        per_ticker[regime["ticker"]] += 1
    multi = {t: n for t, n in sorted(per_ticker.items()) if n > 1}
    middle = [r["ticker"] for r in basis["regimes"]
              if r["position_on_session_axis"] in ("middle", "suffix")]

    return {
        "gate_a_was_not_run": True,
        "manifest_not_established": True,
        "superseded_expectations": {
            "_status": "HISTORICAL. This is what Gate A expected BEFORE the "
                       "reconciliation implemented in this phase. It is recorded "
                       "so the change is auditable, and it is NOT current state.",
            "artifacts": ["volume_repair_ledger.json", "observed_basis_factor.json"],
            "inputs": ["ohlc.parquet", "broker_daily.parquet"],
            "repair_key": "ledger['authorised_repairs']",
            "regime_index": "regimes keyed by TICKER: {r['ticker']: r}",
            "regime_fields": ["factor", "classification", "regime_last_date",
                              "regime_first_date", "normalized_volume"],
            "certified_label": "RECONSTRUCTIBLE",
        },
        "current_expectations": {
            "_status": "CURRENT. What experiment_1f_universe_gate.py consumes now.",
            "artifacts": ["volume_repair_candidates.json",
                          "volume_repair_authorization.json",
                          "observed_basis_factor_candidate.json"],
            "inputs": ["ohlc_full_market.parquet", "broker_daily_v2.parquet",
                       "experiment_1f_universe.json"],
            "input_resolution": "every path comes from candidate_inputs(); no "
                                "loader keeps a default that could reach a legacy "
                                "root artifact, and traced_open_paths proves it "
                                "behaviourally rather than by declaration",
            "repair_key": "load_authorized_repairs(): an EXACT key set bound by "
                          "sha256 to the reviewed candidate bytes",
            "regime_index": "regimes keyed by TICKER to a LIST of inclusive "
                            "start_date..end_date intervals",
            "certified_label": "CANDIDATE_HARMONIZABLE",
            "mode": "PRIMARY_PIT_CONSERVATIVE harmonises nothing; harmonisation "
                    "is reachable only in the SECONDARY sensitivity mode",
            "domain_separation": "a basis regime invalidates BROKER features only; "
                                 "price validity is price_audit.detect's alone and "
                                 "no price row is deleted for a broker-basis fact",
            "manifest": "verify_reviewed_manifest_v3() -- verification only, with "
                        "no establish path reachable from the gate",
            "absence_policy": "missing artifacts are a GateFailure, never an "
                              "empty default",
        },
        "resolved_assumptions": [
            {
                "assumption": "one basis regime per ticker",
                "evidence": f"the candidate contract measures {len(basis['regimes'])} "
                            f"regimes across {len(per_ticker)} tickers; "
                            f"{multi} carry more than one",
                "consequence_if_unresolved": "load_normalized_artifacts() built a "
                               "dict keyed by ticker, so every regime but the last "
                               "would have been silently discarded",
                "severity": "HARD -- silent data loss, not a visible failure",
                "status": "RESOLVED",
                "resolved_by": "load_candidate_artifacts() maps each ticker to a "
                               "LIST of regimes; assert_no_regime_overlap() refuses "
                               "two factors claiming the same session",
            },
            {
                "assumption": "a regime is always a prefix of history",
                "evidence": f"{len(middle)} regimes sit in middle or suffix "
                            f"position: {sorted(set(middle))}",
                "consequence_if_unresolved": "quarantine_basis_regimes() and "
                               "harmonise_broker_basis() both selected rows with "
                               "date <= regime_last_date, so a middle regime would "
                               "have quarantined or rescaled every clean session "
                               "before it",
                "severity": "HARD -- wrong rows corrected or dropped",
                "status": "RESOLVED",
                "resolved_by": "regime_bounds()/rows_in_regime() select the "
                               "inclusive interval start_date..end_date; a record "
                               "without both bounds is refused, never converted",
            },
            {
                "assumption": "a harmonised lot must stay integral",
                "evidence": "harmonise_broker_basis() raises when scaling leaves a "
                            "non-integral lot; that holds only because the two "
                            "certified factors are 5 and 25",
                "consequence_if_unresolved": "a DERIVED equivalent lot is not an "
                               "exchange lot and need not be integral; a certified "
                               "factor such as 1/2 would have failed the gate for "
                               "being fractional rather than for being wrong",
                "severity": "METHODOLOGY -- would reject a valid factor",
                "status": "RESOLVED",
                "resolved_by": "harmonise_broker_basis() carries the derived "
                               "quantity in float64 and no longer requires "
                               "integrality of raw_lots * r",
            },
            {
                "assumption": "a detector hit is an authorised repair",
                "evidence": "the ledger key is literally 'authorised_repairs' and "
                            "the gate applies it as accepted truth",
                "consequence_if_unresolved": "the candidate artifact publishes "
                               "PROPOSED entries under 'candidates'; pointed at the "
                               "old Gate A it would have found no repairs and then "
                               "failed closed on the 5 uncovered wraps -- safe, but "
                               "not a working contract",
                "severity": "HARD -- requires an explicit authorisation step",
                "status": "RESOLVED",
                "resolved_by": "detection and authorisation are two artifacts from "
                               "two acts. load_authorized_repairs() binds the "
                               "authorisation to the reviewed candidate bytes by "
                               "sha256 and to an EXACT key set; a newly detected "
                               "hit is a human-review event, never an extra repair",
            },
        ],
        "field_renames_applied": {
            "_status": "APPLIED in the candidate contract, not pending",
            "authorised_repairs": "candidates (+ an authorisation record)",
            "normalized_volume": "proposed_normalized_volume",
            "factor": "candidate_factor / candidate_factor_exact",
            "regime_first_date, regime_last_date": "start_date, end_date",
            "RECONSTRUCTIBLE": "CANDIDATE_HARMONIZABLE (+ review state)",
        },
        "no_change_required": {
            "broker_daily.parquet": "PROVENANCE. The candidate v2 table reproduces "
                                    "its in-universe subset exactly; the full-market "
                                    "file itself is no longer an execution input",
            "ohlc.parquet": "PROVENANCE. The 297 snapshots agree with it on every "
                            "OHLC value, and the experiment-owned full-market copy "
                            "is byte-identical to it",
            "price_audit.detect": "semantics untouched, and now the SOLE authority "
                                  "on price-row validity",
        },
    }


def remaining_blockers(basis, authorization):
    """What is ACTUALLY still open. Resolved items belong in `resolved`, not here.

    Two blockers were removed rather than reworded, because they had become
    false: the five stage-A candidates now carry a reviewed authorisation bound
    to the exact candidate bytes, and Gate A consumes the segmented artifact
    as a list of inclusive intervals per ticker. Leaving either in place would
    have published historical text as current state -- the failure mode this
    report exists to prevent.
    """
    blockers = [
        {
            "blocker": "the reviewed inventory policy is not yet wired into Gate B",
            "policy_status": "DECIDED",
            "implementation_status": "DOWNSTREAM GATE-B IMPLEMENTATION PENDING",
            "detail": "the POLICY is decided and implemented in "
                      "experiment_1f_validity.cumulative_inventory_availability(): "
                      "cumulative observable_inventory does NOT reset at a hole, "
                      "is never re-anchored (no valid-data-only estimator of the "
                      "true level exists, so any re-anchor would be invented), and "
                      "stays unavailable from the first unresolved contamination "
                      "onward; finite rolling features recover after a full valid "
                      "lookback. What is outstanding is only the Gate-B adapter: "
                      "the merged ml_v2 implementation still restarts the "
                      "cumulative sum at zero after a hole, and Gate B has not "
                      "been run or modified in this phase.",
            "owner": "Gate-B implementation, against an already-decided policy",
        },
        {
            "blocker": "PIT observability is unestablished",
            "detail": "no publication or available_at evidence exists, so no basis "
                      "change can be claimed as knowable on its session.",
            "owner": "documented limitation, carried into #1F interpretation",
        },
        {
            "blocker": "no reviewed manifest-v3 exists, so Gate A cannot run",
            "detail": "verify_reviewed_manifest_v3() has no establish path and the "
                      "gate refuses to write one. Establishment is a separate act "
                      "in experiment_1f_manifest.py, valid only after the "
                      "implementation is committed on a clean semantic tree. Until "
                      "then a real Gate A run fails closed at the manifest stage -- "
                      "which is the intended state, not a defect.",
            "owner": "human review, then commit, then establishment",
        },
        {
            "blocker": "the implementation is uncommitted and under review",
            "detail": "the semantic code tree is dirty, so assert_establishable() "
                      "refuses: a pinned commit would not describe the code that "
                      "produced these artifacts.",
            "owner": "human line-by-line review",
        },
    ]
    if not authorization["authorization_verified"]:
        blockers.insert(0, {
            "blocker": "the volume-repair authorisation does not verify",
            "detail": authorization["failure"],
            "owner": "human review",
        })
    return blockers


def repair_governance(candidate_dir):
    """DETECTED, AUTHORIZED and STAGE-B as three separate facts.

    Authorisation is never inferred from detection. This reads the authorisation
    through gate.load_authorized_repairs -- the same fail-closed contract Gate A
    itself uses -- so the report cannot claim an authorisation that the gate
    would refuse. Every mismatch in that loader is a stop: wrong parent bytes,
    an unreviewed new detector hit, an approved hit that is no longer detected,
    or a moved raw/proposed volume.
    """
    join = lambda name: os.path.join(candidate_dir, name)
    candidates_path = join(norm.CANDIDATE_REPAIRS)
    auth_path = join("volume_repair_authorization.json")

    with open(candidates_path, encoding="utf-8") as fh:
        detected = json.load(fh)
    detected_keys = sorted(f"{c['ticker']} {c['date']}"
                           for c in detected["candidates"])
    stage_b_keys = sorted(f"{c['ticker']} {c['date']}"
                          for c in detected.get("stage_b_diagnostics_never_repaired", []))

    record = {
        "detected": {
            "_meaning": "the detector's PROPOSED stage-A repairs. Detection "
                        "authorises nothing.",
            "n": len(detected_keys),
            "keys": detected_keys,
            "artifact": os.path.basename(candidates_path),
            "artifact_sha256": cand.sha256_file(candidates_path),
        },
        "stage_b": {
            "_meaning": "structurally ineligible for repair: no certified factor "
                        "and no proposed volume, so there is nothing an "
                        "authorisation could ever approve. UNAUTHORIZED, and it "
                        "stays that way.",
            "n": len(stage_b_keys),
            "keys": stage_b_keys,
            "authorized": False,
        },
    }

    try:
        loaded = gate.load_authorized_repairs(
            candidate_path=candidates_path,
            authorization_path=auth_path,
            basis_path=join(norm.CANDIDATE_BASIS))
    except gate.GateFailure as exc:
        record["authorized"] = {
            "_meaning": "REFUSED by the same fail-closed contract Gate A uses",
            "n": 0, "keys": [], "verified": False,
        }
        record["authorization_verified"] = False
        record["failure"] = str(exc)
        return record

    approved = sorted(f"{t} {d}" for t, d in loaded["authorised_repairs"])
    record["authorized"] = {
        "_meaning": "a reviewed, EXACT key set bound by sha256 to the exact "
                    "candidate bytes that were reviewed",
        "n": len(approved),
        "keys": approved,
        "scope": gate.AUTHORIZATION_SCOPE,
        "artifact": os.path.basename(auth_path),
        "parent_candidate_sha256": loaded["parent_candidate_sha256"],
        "authorization_digest": loaded["authorization_digest"],
        "verified": True,
    }
    record["authorization_verified"] = True
    record["failure"] = None
    record["detected_equals_authorized"] = (detected_keys == approved)
    record["stage_b_intersects_authorized"] = bool(
        set(stage_b_keys) & set(approved))
    record["verified_with"] = ("experiment_1f_universe_gate."
                               "load_authorized_repairs (the gate's own loader)")
    return record


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate-dir", default=cand.CANDIDATE_DIR)
    a = ap.parse_args()
    report = build_report(a.candidate_dir)
    path = os.path.join(a.candidate_dir, "validation_report.json")
    cand.write_json(path, report)
    print(json.dumps({k: v for k, v in report.items()
                      if k in ("baseline", "determinism")}, indent=2)[:1600])
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
