"""Experiment #1F Phase 2: candidate normalisation evidence.

Emits PROPOSED repairs and OBSERVED basis regimes. It authorises nothing, and
it repairs nothing. Both outputs are candidate artifacts for human review.

What this corrects in the merged normalize_market_data.py
---------------------------------------------------------
1. Detection is not authorisation. The merged module writes every stage-A hit
   into a key literally called `authorised_repairs`, and the merged Gate A
   consumes that key as accepted truth. A detector firing is evidence that a
   repair may be warranted; a human reviewing the exact candidate key-set is
   what makes it authorised. This module emits `volume_repair_candidates.json`
   with PROPOSED status and never uses the word authorised for its own output.
   The merged production path is left untouched -- see the Gate-A compatibility
   memo rather than a silent contract change.

2. Regimes are segmented, not assumed to be a prefix. The merged classifier
   requires `prefix_block`: every off-basis session must precede every on-basis
   one. A ticker whose basis shifts in the middle of the window, or shifts back,
   is therefore structurally unclassifiable rather than measured. This module
   segments contiguous runs on the ORIGINAL session axis and supports zero, one
   or many regimes in prefix, middle or suffix position.

3. Exactness is proved, not approximated. The merged reconstruction test uses
   np.isclose(), whose default relative tolerance GROWS with magnitude: at a
   price of 8,575 and r=5 it accepts anything within 0.43 of an integer, so it
   can certify a value that is not integral at all. Every price and volume in
   the frozen 297 is an exact Python int, so this module decides integrality in
   exact Fraction arithmetic and never rounds a value before testing it.

4. The source domain fails closed. The merged daily_totals() counts a
   non-integral lot and carries on, and truncates any series longer than the
   date axis with `series[:n]`. For the strict 297 both are hard failures.

Layer discipline is unchanged: this reads the experiment-owned snapshot and
writes candidate JSON. It never touches the raw cache, either parquet, or
neobdm.db.
"""
import hashlib
import json
import math
import os
from fractions import Fraction

import numpy as np

import build_inventory_db as bidb
import experiment_1f_candidate as cand
from normalize_market_data import (
    BASIS_MIN_DEVIATION,
    BASIS_MIN_LOT_GAP,
    DUAL_ESTIMATOR_MAX_DISAGREEMENT,
    MIN_REGIME_DAYS,
    MODULUS,
    RATIO_CORRELATION_MIN,
    RECONSTRUCTION_MIN_RATE,
    SHARES_PER_LOT,
    TICK_ABOVE,
    TICK_BANDS,
    detect_volume_wraps,
)

RULE_VERSION_WRAP = "uint32_volume_wrap_stage_a/1"
RULE_VERSION_BASIS = "observed_basis_regime/2-segmented"

CANDIDATE_REPAIRS = "volume_repair_candidates.json"
CANDIDATE_BASIS = "observed_basis_factor_candidate.json"


# --------------------------------------------------------------------------
# fail-closed source domain
# --------------------------------------------------------------------------

def strict_totals(data, ticker):
    """Exact per-session integer lot totals. No truncation, no tolerated junk.

    Lots are summed as Python ints so a uint32 wrap signature survives exactly;
    float summation of the same values destroys it.
    """
    dates = data.get("date") or []
    n = len(dates)
    if not n:
        raise cand.CandidateError(f"{ticker}: empty session axis")

    # ONE definition of strict-source completeness, shared with the ingest path.
    # These totals are zero-initialised exactly like the ingest matrices, so a
    # broker present in blot but absent from bval would contribute a fabricated
    # zero to the bval total -- and every basis factor and wrap candidate is
    # inferred from these totals. Two strict readers disagreeing about what a
    # valid source is would be worse than either rule alone, so the check comes
    # from build_inventory_db and runs BEFORE any repair or basis inference.
    partial = bidb.partially_present_brokers(data)
    if partial:
        raise cand.CandidateError(
            f"{ticker}: {len(partial)} broker(s) appear in some required fields "
            f"but not all: {partial[:5]}. {bidb.ZERO_FILL_REFUSAL}")

    totals = {"dates": list(dates),
              "blot": [0] * n, "slot": [0] * n, "nlot": [0] * n,
              "bval": [0.0] * n, "sval": [0.0] * n, "nval": [0.0] * n}

    for field in ("blot", "slot", "nlot", "bval", "sval", "nval"):
        src = data.get(field) or {}
        if not isinstance(src, dict):
            raise cand.CandidateError(f"{ticker}: {field} is not a mapping")
        lots = field.endswith("lot")
        for broker, series in src.items():
            if not isinstance(series, list):
                raise cand.CandidateError(f"{ticker}/{broker}/{field}: not a list")
            if len(series) != n:
                raise cand.CandidateError(
                    f"{ticker}/{broker}/{field}: length {len(series)} != {n} "
                    "sessions; padding or truncating would invent data")
            for i, value in enumerate(series):
                if isinstance(value, bool):
                    raise cand.CandidateError(f"{ticker}/{broker}/{field}[{i}]: bool")
                if lots:
                    if isinstance(value, int):
                        totals[field][i] += value
                    elif isinstance(value, float) and math.isfinite(value) \
                            and value.is_integer():
                        totals[field][i] += int(value)
                    else:
                        raise cand.CandidateError(
                            f"{ticker}/{broker}/{field}[{i}]: not an integral lot "
                            f"({value!r}); factor inference on a malformed source "
                            "domain is refused")
                else:
                    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                        raise cand.CandidateError(
                            f"{ticker}/{broker}/{field}[{i}]: not a finite value "
                            f"({value!r})")
                    totals[field][i] += float(value)

    ohlc = {row["date"]: row for row in (data.get("ohlc") or [])}
    for key in ("open", "high", "low", "close", "volume"):
        totals[key] = [ohlc[d].get(key) if d in ohlc else None for d in dates]
    return totals


# --------------------------------------------------------------------------
# stage A volume wrap: PROPOSED repairs
# --------------------------------------------------------------------------

def repair_candidates(ticker, totals, source_sha256):
    """Stage-A hits, recorded as PROPOSED and revalidated on both bases.

    Reuses the merged exact-integer detector unchanged -- its guard conjunction
    is sound and already covered by tests -- and adds the two-sided revalidation
    the candidate contract requires. The repaired session is explicitly INCLUDED
    in validation rather than skipped.
    """
    out = []
    for hit in detect_volume_wraps(ticker, totals):
        i = totals["dates"].index(hit["date"])
        raw = int(hit["raw_volume"])
        proposed = int(hit["normalized_volume"])
        buy_shares = SHARES_PER_LOT * totals["blot"][i]
        sell_shares = SHARES_PER_LOT * totals["slot"][i]
        out.append({
            "ticker": ticker,
            "date": hit["date"],
            "status": "PROPOSED",
            "authorised": False,
            "rule_version": RULE_VERSION_WRAP,
            "raw_volume": raw,
            "proposed_normalized_volume": proposed,
            "delta": proposed - raw,
            "wrap_count": hit["wrap_count"],
            "aggregate_buy_shares": buy_shares,
            "aggregate_sell_shares": sell_shares,
            "source_sha256": source_sha256,
            "evidence": dict(hit["evidence"]),
            "revalidation": {
                # RAW basis must reproduce the expected FAILURE signature...
                "raw_identity_holds": raw == buy_shares,
                "raw_shortfall": buy_shares - raw,
                "raw_shortfall_is_one_modulus": (buy_shares - raw) == MODULUS,
                # ...and the PROPOSED basis must reconcile exactly, on this date.
                "proposed_identity_buy_side": proposed == buy_shares,
                "proposed_identity_sell_side": proposed == sell_shares,
                "validated_on_repaired_date": True,
            },
        })
    return out


# --------------------------------------------------------------------------
# exact reconstruction evidence
# --------------------------------------------------------------------------

def tick_size_exact(price):
    """IDX fraksi harga by band, as an exact integer.

    NOTE: this table has no authoritative counterpart anywhere in the
    repository -- price_audit.ara_bound() bands prices at 200/5000 for
    auto-rejection percentages, which is a different rule with different edges.
    Equivalence for the historical sample therefore cannot be proven, so the
    grid rate below is SUPPORTING EVIDENCE and never a certificate.
    """
    for edge, size in TICK_BANDS:
        if price < edge:
            return Fraction(size).limit_denominator(1)
    return Fraction(TICK_ABOVE).limit_denominator(1)


def exact_reconstruction(prices, factor):
    """Fraction-exact integrality and tick-grid rates for price * factor.

    Every price in the frozen 297 is an exact Python int, so `price * r` is an
    exact rational and its integrality is decidable with no tolerance at all.
    np.isclose() is deliberately not used: its default rtol of 1e-5 scales with
    magnitude and would certify a product 0.43 away from an integer at typical
    IDX price levels.
    """
    if not prices:
        return {"n": 0, "integral_rate": 0.0, "on_grid_rate": 0.0,
                "arithmetic": "exact_rational", "all_prices_integral": True}
    integral = on_grid = 0
    all_int = True
    for price in prices:
        if isinstance(price, float) and not price.is_integer():
            all_int = False
        product = Fraction(price) * factor
        if product.denominator == 1:
            integral += 1
            size = tick_size_exact(int(product))
            if (product / size).denominator == 1:
                on_grid += 1
    n = len(prices)
    return {
        "n": n,
        "integral_rate": integral / n,
        "on_grid_rate": on_grid / n,
        "arithmetic": "exact_rational",
        "all_prices_integral": all_int,
    }


# --------------------------------------------------------------------------
# contiguous regime segmentation
# --------------------------------------------------------------------------

def label_sessions(totals, volume):
    """Label every ORIGINAL session OFF / ON / UNKNOWN.

    UNKNOWN is a session we cannot measure (no volume, or no buy lots), not a
    session on basis 1. Keeping it distinct is what lets a regime span a
    quarantined or untraded session without either silently absorbing it or
    being split in two by it.
    """
    labels, ratios = [], {}
    for i in range(len(totals["dates"])):
        v = volume[i]
        blot = totals["blot"][i]
        if v is None or blot <= 0:
            labels.append("UNKNOWN")
            continue
        ratio = Fraction(int(v), SHARES_PER_LOT * blot)
        ratios[i] = ratio
        lot_gap = abs(int(v) - SHARES_PER_LOT * blot) / SHARES_PER_LOT
        off = (abs(float(ratio) - 1.0) > BASIS_MIN_DEVIATION
               and lot_gap > BASIS_MIN_LOT_GAP)
        labels.append("OFF" if off else "ON")
    return labels, ratios


def segment_regimes(labels):
    """Maximal runs of consecutive sessions containing at least one OFF.

    An ON session -- one measured to sit on basis 1 -- is a real boundary and
    closes a regime. An UNKNOWN session is not a boundary: it carries no
    evidence either way, so splitting on it would fabricate two regimes where
    the source supports one. Runs are indices into the ORIGINAL session axis,
    never into a compressed array, so a quarantined session keeps its position.
    """
    segments, start = [], None
    for i, label in enumerate(labels + ["ON"]):
        if label == "ON":
            if start is not None:
                run = list(range(start, i))
                while run and labels[run[-1]] == "UNKNOWN":
                    run.pop()
                if any(labels[j] == "OFF" for j in run):
                    segments.append(run)
                start = None
        elif start is None:
            start = i
    return segments


def describe_regime(ticker, totals, volume, labels, ratios, run):
    """Measure and classify one contiguous regime on its own evidence."""
    dates = totals["dates"]
    off = [i for i in run if labels[i] == "OFF"]
    values = [ratios[i] for i in off]
    exactly_constant = len(set(values)) == 1
    if exactly_constant:
        factor = values[0]
    else:
        factor = Fraction(float(np.median([float(v) for v in values]))
                          ).limit_denominator(100000)

    lots = np.array([totals["blot"][i] for i in off], dtype=np.float64)
    gap = np.array([abs(int(volume[i]) - SHARES_PER_LOT * totals["blot"][i])
                    / SHARES_PER_LOT for i in off], dtype=np.float64)
    correlation = (float(np.corrcoef(lots, gap)[0, 1])
                   if len(off) > 2 and np.std(lots) > 0 and np.std(gap) > 0
                   else float("nan"))

    e2 = []
    for i in off:
        close, blot = totals["close"][i], totals["blot"][i]
        if close and blot > 0:
            e2.append((totals["bval"][i] / (blot * SHARES_PER_LOT)) / float(close))
    estimator_free = float(np.median(e2)) if e2 else float("nan")
    if math.isnan(estimator_free) or float(factor) == 0:
        gap_rel, disagree = float("nan"), False
    else:
        gap_rel = abs(estimator_free / float(factor) - 1.0)
        disagree = gap_rel > DUAL_ESTIMATOR_MAX_DISAGREEMENT

    prices = [totals[k][i] for i in off for k in ("open", "high", "low", "close")
              if totals[k][i]]
    recon = exact_reconstruction(prices, factor)

    reasons = []
    if len(off) < MIN_REGIME_DAYS:
        reasons.append(f"only {len(off)} off-basis sessions (< {MIN_REGIME_DAYS})")
    if not exactly_constant:
        reasons.append(f"ratio not exactly constant ({len(set(values))} distinct)")
    if not math.isnan(correlation) and correlation < RATIO_CORRELATION_MIN:
        reasons.append(f"ratio-like correlation {correlation:.3f} < {RATIO_CORRELATION_MIN}")
    if recon["integral_rate"] < RECONSTRUCTION_MIN_RATE:
        reasons.append(f"exact integral reconstruction {recon['integral_rate']:.4f} "
                       f"< {RECONSTRUCTION_MIN_RATE}")
    if disagree:
        reasons.append(f"volume-free estimator disagrees by {gap_rel:.3f} "
                       f"> {DUAL_ESTIMATOR_MAX_DISAGREEMENT}")

    position = ("whole_window" if run[0] == 0 and run[-1] == len(dates) - 1
                else "prefix" if run[0] == 0
                else "suffix" if run[-1] == len(dates) - 1
                else "middle")

    return {
        "ticker": ticker,
        "rule_version": RULE_VERSION_BASIS,
        "start_date": dates[run[0]],
        "end_date": dates[run[-1]],
        "start_session_index": run[0],
        "end_session_index": run[-1],
        "session_count": len(run),
        "off_basis_sessions": len(off),
        "unknown_sessions_inside": sum(1 for i in run if labels[i] == "UNKNOWN"),
        "position_on_session_axis": position,
        "candidate_factor": float(factor),
        "candidate_factor_exact": {
            "numerator": factor.numerator,
            "denominator": factor.denominator,
            "note": "canonical rational; the float above is diagnostic only",
        },
        "classification": "CANDIDATE_HARMONIZABLE" if not reasons else "QUARANTINE",
        "reason": reasons or ["all evidence criteria met"],
        "evidence": {
            "exactly_constant": exactly_constant,
            "distinct_ratios": len(set(values)),
            "ratio_like_correlation": correlation,
            "estimator_volume_based": float(factor),
            "estimator_volume_free": estimator_free,
            "estimator_relative_gap": gap_rel,
            "estimators_disagree": disagree,
            "reconstruction": recon,
            "tick_grid_status": ("SUPPORTING EVIDENCE ONLY: the fraksi table has "
                                 "no authoritative counterpart in this repository, "
                                 "so it cannot certify a factor"),
        },
    }


def stage_b_diagnostics(ticker, totals, volume, regime):
    """Stage B: a wrap hidden behind a basis factor. DIAGNOSTIC ONLY.

    When volume is reported on basis r, a wrap in the underlying as-traded share
    count is invisible to stage A: the stored value is r*(true - MODULUS), which
    is neither below MODULUS nor an exact modulus away from sum(blot)*100.

    BNBR is the counterexample that proves this must never authorise a repair.
    Its 2026-02-27 session reproduces as 1.191*(sum_blot*100 - 2**32), yet its
    factor is one this contract quarantines: 170 distinct ratios and a 0.0000
    exact integral reconstruction rate. Reconstructing a volume from a factor we
    cannot certify would manufacture data. A stage-B hit is therefore recorded
    as evidence REINFORCING quarantine, never as a repair.
    """
    factor = float(regime["candidate_factor"])
    hits = []
    for i in range(regime["start_session_index"], regime["end_session_index"] + 1):
        v = volume[i]
        buy_shares = SHARES_PER_LOT * totals["blot"][i]
        if v is None or buy_shares <= 0:
            continue
        v = float(v)
        if abs(v - factor * buy_shares) < 1.0:
            continue                          # ordinary session for this basis
        for k in (1, 2, 3):
            if buy_shares - k * MODULUS <= 0:
                break
            if abs(factor * (buy_shares - k * MODULUS) - v) < 1.0:
                hits.append({
                    "ticker": ticker,
                    "date": totals["dates"][i],
                    "status": "DIAGNOSTIC ONLY -- reinforces quarantine",
                    "authorised": False,
                    "proposed_repair": None,
                    "rule_version": "uint32_volume_wrap_stage_b_diagnostic/1",
                    "raw_volume": v,
                    "wrap_count": k,
                    "basis_factor": factor,
                    "regime_classification": regime["classification"],
                    "implied_as_traded_shares": buy_shares - k * MODULUS,
                    "why_not_repaired": (
                        "reconstruction requires the regime factor, which this "
                        "contract has not certified; repairing would manufacture "
                        "a volume the source never reported"),
                })
                break
    return hits


def analyse_ticker(ticker, data, source_sha256):
    """Proposed repairs and segmented regimes for one ticker."""
    totals = strict_totals(data, ticker)
    proposals = repair_candidates(ticker, totals, source_sha256)

    volume = list(totals["volume"])
    for hit in proposals:
        volume[totals["dates"].index(hit["date"])] = hit["proposed_normalized_volume"]

    labels, ratios = label_sessions(totals, volume)
    regimes = [describe_regime(ticker, totals, volume, labels, ratios, run)
               for run in segment_regimes(labels)]
    diagnostics = [hit for regime in regimes
                   for hit in stage_b_diagnostics(ticker, totals, volume, regime)]
    return proposals, regimes, diagnostics, totals, labels, volume


# --------------------------------------------------------------------------
# threshold sensitivity: a DQ policy must not be a snapshot-fitted alpha knob
# --------------------------------------------------------------------------

def classify_at(totals, volume, deviation, lot_gap):
    """Re-segment one ticker under alternative detection thresholds.

    Runs the PRODUCTION segmentation, unfiltered. An earlier version dropped
    runs shorter than MIN_REGIME_DAYS here, so at the nominal thresholds the
    audit reported 13 regimes where the classifier itself emits 18 -- it was
    measuring a different selection and calling it sensitivity. The production
    classifier emits every contiguous run and quarantines a short one with an
    explicit reason rather than hiding it, so the audit must do the same and
    report the sub-threshold class separately.
    """
    labels, ratios = [], {}
    for i in range(len(totals["dates"])):
        v, blot = volume[i], totals["blot"][i]
        if v is None or blot <= 0:
            labels.append("UNKNOWN")
            continue
        ratio = Fraction(int(v), SHARES_PER_LOT * blot)
        ratios[i] = ratio
        gap = abs(int(v) - SHARES_PER_LOT * blot) / SHARES_PER_LOT
        labels.append("OFF" if (abs(float(ratio) - 1.0) > deviation
                                and gap > lot_gap) else "ON")
    runs = segment_regimes(labels)
    off_days = [sum(1 for i in run if labels[i] == "OFF") for run in runs]
    return runs, off_days


def sensitivity(per_ticker, grid):
    """Segmentation stability across nearby thresholds.

    `regimes_total` counts what the classifier actually emits, so the nominal
    row reproduces the production artifact exactly before any perturbation.
    `regimes_meeting_min_days` is reported alongside it as a distinct quantity,
    not as a substitute for it.
    """
    out = []
    for deviation, lot_gap in grid:
        total = long_enough = 0
        flagged = []
        for ticker, (totals, volume) in per_ticker.items():
            runs, off_days = classify_at(totals, volume, deviation, lot_gap)
            if runs:
                flagged.append(ticker)
            total += len(runs)
            long_enough += sum(1 for n in off_days if n >= MIN_REGIME_DAYS)
        out.append({
            "basis_min_deviation": deviation,
            "basis_min_lot_gap": lot_gap,
            "tickers_with_regime": len(flagged),
            "regimes_total": total,
            "regimes_meeting_min_days": long_enough,
            "min_regime_days": MIN_REGIME_DAYS,
            "tickers": sorted(flagged),
        })
    return out


# --------------------------------------------------------------------------
# repair authorisation: a separate artifact, produced by a separate act
# --------------------------------------------------------------------------

def build_authorization(candidate_dir, approved_keys, reviewer_note=""):
    """Bind a human approval to one reviewed artifact and one exact key set.

    Deliberately does NOT touch volume_repair_candidates.json. Detection writes
    that file; review writes this one. Keeping them apart is what makes it
    impossible to authorise something by re-running a detector, and what lets
    the gate prove the reviewed bytes are the bytes on disk.
    """
    path = os.path.join(candidate_dir, CANDIDATE_REPAIRS)
    with open(path, encoding="utf-8") as fh:
        candidates = json.load(fh)
    detected = {(c["ticker"], c["date"]): c for c in candidates["candidates"]}

    approved = [tuple(k) for k in approved_keys]
    if len(set(approved)) != len(approved):
        raise cand.CandidateError("duplicate key in the approved set")
    unknown = sorted(set(approved) - set(detected))
    if unknown:
        raise cand.CandidateError(
            f"approved keys that no detector hit supports: {unknown}. An "
            "authorisation may only ratify a measured candidate, never invent one.")

    stage_b = {(h["ticker"], h["date"])
               for h in candidates.get("stage_b_diagnostics_never_repaired", [])}
    collision = sorted(set(approved) & stage_b)
    if collision:
        raise cand.CandidateError(
            f"stage-B diagnostics can never be authorised: {collision}")

    return {
        "artifact": "volume_repair_authorization",
        "status": "AUTHORIZED",
        "scope": "EXACT KEY SET ONLY",
        "rule_version": RULE_VERSION_WRAP,
        "parent_candidate_artifact": CANDIDATE_REPAIRS,
        "parent_candidate_sha256": cand.sha256_file(path),
        "parent_candidate_semantic_digest": hashlib.sha256(
            json.dumps(candidates, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "invalidation_rule": (
            "Authorisation covers exactly the key set below and nothing else. "
            "Any detector hit added, removed or changed -- including a change to "
            "a source file's sha256, a raw volume, a proposed volume or a delta "
            "-- invalidates this authorisation in full and requires fresh human "
            "review. An extra hit is not an extra repair; it is evidence the "
            "source moved beneath a review that no longer covers it."),
        "stage_b_note": (
            "Stage-B diagnostics are structurally ineligible: they carry no "
            "certified factor and no proposed volume, so there is nothing to "
            "authorise. BNBR 2026-02-27 and 2026-03-02 remain DIAGNOSTIC ONLY."),
        "reviewer_note": reviewer_note,
        "n_authorized": len(approved),
        "authorized": [
            {
                "ticker": t,
                "date": d,
                "authorised": True,
                "rule_version": detected[(t, d)]["rule_version"],
                "source_sha256": detected[(t, d)]["source_sha256"],
                "raw_volume": detected[(t, d)]["raw_volume"],
                "proposed_normalized_volume":
                    detected[(t, d)]["proposed_normalized_volume"],
                "delta": detected[(t, d)]["delta"],
                "aggregate_buy_shares": detected[(t, d)]["aggregate_buy_shares"],
                "aggregate_sell_shares": detected[(t, d)]["aggregate_sell_shares"],
            }
            for t, d in sorted(approved)
        ],
    }


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def build(candidate_dir):
    """Deterministic: output depends only on the snapshot and these rules."""
    covered, manifest = cand.manifest_tickers(candidate_dir)
    shas = {f["ticker"]: f["sha256"] for f in manifest["files"]}
    source_dir = os.path.join(candidate_dir, cand.SOURCE_RAW)

    proposals, regimes, diagnostics, per_ticker = [], [], [], {}
    for ticker in sorted(covered):
        data = cand.read_raw(cand.raw_path(source_dir, ticker))
        hits, found, diag, totals, _, volume = analyse_ticker(ticker, data, shas[ticker])
        proposals.extend(hits)
        regimes.extend(found)
        diagnostics.extend(diag)
        per_ticker[ticker] = (totals, volume)

    proposals.sort(key=lambda h: (h["ticker"], h["date"]))
    regimes.sort(key=lambda r: (r["ticker"], r["start_date"]))
    diagnostics.sort(key=lambda h: (h["ticker"], h["date"]))

    grid = [(d, g) for d in (5e-4, 1e-3, 2e-3) for g in (50.0, 100.0, 200.0)]
    repairs = {
        "status": "PROPOSED -- NOT AUTHORISED",
        "note": ("Every entry is a detector hit awaiting human review of the "
                 "exact candidate key-set. Detection is not authorisation: no "
                 "consumer may treat this file as accepted truth, and nothing "
                 "here has been applied to any artifact."),
        "rule_version": RULE_VERSION_WRAP,
        "modulus": MODULUS,
        "n_candidates": len(proposals),
        "candidates": proposals,
        "n_stage_b_diagnostics": len(diagnostics),
        "stage_b_diagnostics_never_repaired": diagnostics,
    }
    basis = {
        "status": "CANDIDATE -- evidence for review, no factor is applied",
        "rule_version": RULE_VERSION_BASIS,
        "note": ("Measured basis ratios per contiguous regime on the original "
                 "session axis. The source carries no corporate-action metadata, "
                 "so no event attribution is claimed."),
        "thresholds": {
            "basis_min_deviation": BASIS_MIN_DEVIATION,
            "basis_min_lot_gap": BASIS_MIN_LOT_GAP,
            "ratio_correlation_min": RATIO_CORRELATION_MIN,
            "min_regime_days": MIN_REGIME_DAYS,
            "reconstruction_min_rate": RECONSTRUCTION_MIN_RATE,
            "dual_estimator_max_disagreement": DUAL_ESTIMATOR_MAX_DISAGREEMENT,
            "selection_basis": ("measured source phenomena only; no ML outcome, "
                                "score or performance metric was consulted"),
        },
        "n_regimes": len(regimes),
        "regimes": regimes,
        "threshold_sensitivity": sensitivity(per_ticker, grid),
    }
    return repairs, basis


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate-dir", default=cand.CANDIDATE_DIR)
    a = ap.parse_args()

    repairs, basis = build(a.candidate_dir)
    cand.write_json(os.path.join(a.candidate_dir, CANDIDATE_REPAIRS), repairs)
    cand.write_json(os.path.join(a.candidate_dir, CANDIDATE_BASIS), basis)

    print(f"volume repair CANDIDATES (proposed, not authorised): "
          f"{repairs['n_candidates']}")
    for hit in repairs["candidates"]:
        r = hit["revalidation"]
        print(f"  {hit['ticker']:<5} {hit['date']}  {hit['raw_volume']:>13,}"
              f" -> {hit['proposed_normalized_volume']:>13,}  "
              f"raw_fails={not r['raw_identity_holds']} "
              f"proposed_reconciles={r['proposed_identity_buy_side']}")

    print(f"\nbasis regimes: {basis['n_regimes']}")
    for r in basis["regimes"]:
        e = r["evidence"]
        print(f"  {r['ticker']:<5} r={r['candidate_factor_exact']['numerator']}/"
              f"{r['candidate_factor_exact']['denominator']:<6} "
              f"{r['start_date']}..{r['end_date']} {r['off_basis_sessions']:>4}d "
              f"{r['position_on_session_axis']:<11} "
              f"integral={e['reconstruction']['integral_rate']:.4f} "
              f"grid={e['reconstruction']['on_grid_rate']:.4f}  {r['classification']}")

    print("\nthreshold sensitivity (tickers carrying >=1 regime):")
    for row in basis["threshold_sensitivity"]:
        print(f"  dev={row['basis_min_deviation']:<7g} gap={row['basis_min_lot_gap']:<6g}"
              f" tickers={row['tickers_with_regime']:<3} regimes={row['regimes_total']}")


if __name__ == "__main__":
    main()
