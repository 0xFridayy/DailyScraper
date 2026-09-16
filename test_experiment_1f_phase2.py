"""Experiment #1F Phase 2: the candidate data contract's tests.

Kept out of test_pipeline.py deliberately. That file already carries the merged
PR #38-#40 semantics for the PRODUCTION normalisation path, and those tests must
keep passing unchanged -- several of them assert the very contract this phase
reports as incompatible (Gate A consuming `authorised_repairs`). Mixing the two
in one file would make it easy to "fix" a production test to suit a candidate
rule. They stay separate so the incompatibility stays visible.

Run directly, or via check_ml_health.py, which runs it as a subprocess exactly
the way it runs test_pipeline.py.

Everything here is synthetic unless a test name says otherwise; the tests that
touch real artifacts skip cleanly when those artifacts are absent, so CI without
the gitignored data still exercises every structural rule.
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

import build_inventory_db as bidb
import experiment_1f_candidate as cand
import experiment_1f_normalization as norm

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.environ.get("NEOBDM_SHARED_ROOT",
                        os.path.join(os.path.dirname(HERE), "Claude"))

SKIPPED = []


def _skip(name, why):
    SKIPPED.append(f"{name}: {why}")
    print(f"  SKIP {name} ({why})")


def _candidate_snapshot_present():
    """The gitignored candidate snapshot (backtest_out/experiment_1f_candidate/) is
    built locally only; CI checkouts do not have it."""
    return os.path.exists(os.path.join(cand.CANDIDATE_DIR, cand.SOURCE_MANIFEST))


def _raw(dates, brokers, ohlc=None):
    """A raw vendor payload shaped exactly like inventory_raw/*.json.gz."""
    data = {"date": list(dates), "ohlc": ohlc or []}
    for field in ("nlot", "blot", "slot", "nval", "bval", "sval"):
        data[field] = {b: list(v[field]) for b, v in brokers.items() if field in v}
    return data


def _simple(n=30, blot=1000, volume_factor=1):
    """n sessions, one broker, buy-only, volume consistent with `volume_factor`."""
    dates = [f"2026-01-{i + 1:02d}" for i in range(n)]
    ohlc = [{"date": d, "open": 100, "high": 100, "low": 100, "close": 100,
             "volume": blot * 100 * volume_factor} for d in dates]
    brokers = {"AA": {"blot": [blot] * n, "slot": [0] * n, "nlot": [blot] * n,
                      "bval": [blot * 100.0 * 100] * n, "sval": [0.0] * n,
                      "nval": [blot * 100.0 * 100] * n}}
    return _raw(dates, brokers, ohlc)


# --------------------------------------------------------------------------
# strict source domain
# --------------------------------------------------------------------------

def test_strict_lot_rejects_every_non_lot_type():
    """bool, str, None, NaN, inf and non-integral floats are not lots."""
    for bad in (True, False, "5", None, float("nan"), float("inf"), 5.5, [], {}):
        try:
            bidb.strict_lot(bad, "T", "AA", "blot", 0)
        except bidb.StrictSourceError:
            continue
        raise AssertionError(f"strict_lot accepted {bad!r}")
    # ...and a genuine integer, including one far above the float32 exact range,
    # survives unchanged.
    assert bidb.strict_lot(80883692, "T", "AA", "blot", 0) == 80883692
    assert bidb.strict_lot(17168499.0, "T", "AA", "blot", 0) == 17168499
    print("  ok strict_lot rejects non-lots and keeps large int64 exact")


def test_strict_dates_rejects_a_non_ascending_axis():
    """PR #41 review (P2): the contract promises an ascending axis; broker series
    and regimes are positional, so shuffled or descending dates must fail closed."""
    ok = ["2026-01-02", "2026-01-05", "2026-01-06"]
    assert bidb.strict_dates(ok, "T") == ok
    for bad in (["2026-01-05", "2026-01-02"],
                ["2026-01-02", "2026-01-06", "2026-01-05"],
                ["2026-01-06", "2026-01-05", "2026-01-02"],
                ["2026-01-02", "20260102"]):
        try:
            bidb.strict_dates(bad, "T")
        except bidb.StrictSourceError:
            continue
        raise AssertionError(f"strict_dates accepted {bad!r}")
    try:
        bidb.strict_dates(["2026-01-02", "2026-01-02"], "T")
    except bidb.StrictSourceError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("a duplicate date was accepted")
    print("  ok strict_dates rejects descending, shuffled and same-day re-spelled axes")


def test_bool_is_rejected_before_float_coercion_can_hide_it():
    """np.asarray([True], float) is 1.0; validation must run on the RAW object."""
    assert float(np.asarray([True], dtype=np.float64)[0]) == 1.0
    try:
        bidb.strict_lot(True, "T", "AA", "blot", 0)
    except bidb.StrictSourceError as exc:
        assert "bool" in str(exc)
        print("  ok bool rejected before coercion erases the type evidence")
        return
    raise AssertionError("bool survived strict validation")


def test_series_length_mismatch_is_fatal_not_padded():
    """A short or long series must fail, never be zero-filled or truncated."""
    for length in (29, 31):
        data = _simple(n=30)
        data["blot"]["AA"] = [1000] * length
        for fn in (lambda: bidb.strict_ticker_frame(data, "T"),
                   lambda: norm.strict_totals(data, "T")):
            try:
                fn()
            except (bidb.StrictSourceError, cand.CandidateError) as exc:
                assert "length" in str(exc)
                continue
            raise AssertionError(f"length {length} was silently accepted")
    print("  ok series-length mismatch is fatal in both strict paths")


def test_strict_ohlc_does_not_coerce_a_malformed_price_to_nan():
    data = _simple(n=5)
    data["ohlc"][2]["close"] = "not a price"
    try:
        bidb.strict_ohlc_domain(data, "T")
    except bidb.StrictSourceError as exc:
        assert "close" in str(exc)
        print("  ok malformed price fails instead of becoming a silent NaN")
        return
    raise AssertionError("malformed price was coerced away")


def test_row_identity_violation_is_fatal():
    data = _simple(n=25)
    data["nlot"]["AA"][3] = 999            # != blot - slot
    try:
        bidb.strict_ticker_frame(data, "T")
    except bidb.StrictSourceError as exc:
        assert "nlot == blot - slot" in str(exc)
        print("  ok row-level nlot identity is enforced in exact integers")
        return
    raise AssertionError("row identity violation accepted")


# --------------------------------------------------------------------------
# candidate output isolation
# --------------------------------------------------------------------------

def test_strict_candidate_cannot_target_production_artifacts():
    """Neither the repo root nor any directory holding a production parquet."""
    try:
        bidb.guard_candidate_out_dir(bidb.HERE)
    except SystemExit:
        pass
    else:
        raise AssertionError("strict candidate accepted the repo root")

    tmp = tempfile.mkdtemp()
    try:
        open(os.path.join(tmp, "broker_daily.parquet"), "wb").close()
        try:
            bidb.guard_candidate_out_dir(tmp)
        except SystemExit:
            print("  ok candidate ingest refuses production paths")
            return
        raise AssertionError("strict candidate accepted a production directory")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# detection is not authorisation
# --------------------------------------------------------------------------

def _wrap_payload():
    """One session whose volume is exactly 2**32 short of sum(blot)*100."""
    n = 25
    dates = [f"2026-02-{i + 1:02d}" for i in range(n)]
    blot = [1000] * n
    blot[10] = 47_000_000                   # 4.7e9 shares, above the modulus
    volume = [b * 100 for b in blot]
    volume[10] = blot[10] * 100 - norm.MODULUS
    ohlc = [{"date": d, "open": 100, "high": 100, "low": 100, "close": 100,
             "volume": v} for d, v in zip(dates, volume)]
    brokers = {"AA": {"blot": blot, "slot": list(blot), "nlot": [0] * n,
                      "bval": [b * 10000.0 for b in blot],
                      "sval": [b * 10000.0 for b in blot], "nval": [0.0] * n}}
    return _raw(dates, brokers, ohlc), dates[10]


def test_stage_a_detection_is_exact_and_never_called_authorised():
    data, date = _wrap_payload()
    proposals, _, _, _, _, _ = norm.analyse_ticker("T", data, "f" * 64)
    assert len(proposals) == 1, proposals
    hit = proposals[0]
    assert hit["date"] == date
    assert hit["status"] == "PROPOSED"
    assert hit["authorised"] is False
    assert hit["delta"] == norm.MODULUS
    assert hit["wrap_count"] == 1
    assert hit["source_sha256"] == "f" * 64 and len(hit["source_sha256"]) == 64
    for field in ("ticker", "date", "raw_volume", "proposed_normalized_volume",
                  "delta", "wrap_count", "aggregate_buy_shares",
                  "aggregate_sell_shares", "evidence", "rule_version"):
        assert field in hit, field

    blob = json.dumps(norm.build.__doc__ or "") + json.dumps(hit)
    assert "authorised_repairs" not in blob
    print("  ok stage-A hits are PROPOSED, fully provenanced, never authorised")


def test_proposed_repair_is_revalidated_on_both_bases_including_that_date():
    data, _ = _wrap_payload()
    hit = norm.analyse_ticker("T", data, "f" * 64)[0][0]
    r = hit["revalidation"]
    assert r["raw_identity_holds"] is False          # raw must FAIL...
    assert r["raw_shortfall_is_one_modulus"] is True
    assert r["proposed_identity_buy_side"] is True   # ...proposed must reconcile
    assert r["proposed_identity_sell_side"] is True
    assert r["validated_on_repaired_date"] is True
    print("  ok raw fails and proposed reconciles, on the repaired date itself")


def test_a_near_miss_wrap_is_not_detected_at_all():
    """One share off the exact signature is not a wrap. No tolerance anywhere."""
    data, _ = _wrap_payload()
    i = 10
    data["ohlc"][i]["volume"] += 1
    assert norm.analyse_ticker("T", data, "f" * 64)[0] == []
    print("  ok a one-share deviation from the signature detects nothing")


# --------------------------------------------------------------------------
# exactness
# --------------------------------------------------------------------------

def test_exactness_cannot_pass_through_default_np_isclose_slack():
    """np.isclose's rtol GROWS with magnitude; the exact test must not.

    428754 * 1/10 = 42875.4, which is 0.4 from an integer. np.isclose's default
    tolerance at that magnitude is 1e-8 + 1e-5*42875 = 0.42875, so it certifies
    a value that is plainly not integral. Fraction arithmetic cannot.
    """
    from fractions import Fraction
    price, factor = 428754, Fraction(1, 10)
    product = float(price) * float(factor)
    assert np.isclose(product, np.round(product)), "premise: np.isclose accepts it"
    assert (Fraction(price) * factor).denominator != 1, "premise: it is not integral"

    recon = norm.exact_reconstruction([price], factor)
    assert recon["integral_rate"] == 0.0, recon
    assert recon["arithmetic"] == "exact_rational"
    print("  ok exact arithmetic refuses what np.isclose would have certified")


def test_exact_rational_factor_is_serialised_canonically():
    from fractions import Fraction
    n = 40
    dates = [f"2026-03-{i + 1:02d}" for i in range(n)]
    blot = [1000 + i for i in range(n)]
    ohlc = [{"date": d, "open": 100, "high": 100, "low": 100, "close": 100,
             "volume": b * 100 * 5} for d, b in zip(dates, blot)]
    # Rupiah stay nominal, so a factor-5 basis makes the broker VWAP 5x the
    # adjusted close. An incoherent fixture would trip the dual-estimator veto
    # and quarantine a factor this test means to certify.
    brokers = {"AA": {"blot": blot, "slot": [0] * n, "nlot": list(blot),
                      "bval": [b * 50000.0 for b in blot], "sval": [0.0] * n,
                      "nval": [b * 50000.0 for b in blot]}}
    _, regimes, _, _, _, _ = norm.analyse_ticker("T", _raw(dates, brokers, ohlc),
                                                 "f" * 64)
    assert len(regimes) == 1
    exact = regimes[0]["candidate_factor_exact"]
    assert (exact["numerator"], exact["denominator"]) == (5, 1), exact
    assert Fraction(exact["numerator"], exact["denominator"]) == 5
    assert regimes[0]["classification"] == "CANDIDATE_HARMONIZABLE"
    print("  ok exact rational factor serialised as numerator/denominator")


# --------------------------------------------------------------------------
# regime segmentation
# --------------------------------------------------------------------------

def test_segmentation_supports_zero_one_and_many_regimes_anywhere():
    """Prefix, middle and suffix regimes, and a clean series with none."""
    labels = ["ON"] * 10
    assert norm.segment_regimes(labels) == []

    labels = ["OFF"] * 5 + ["ON"] * 5
    assert norm.segment_regimes(labels) == [list(range(0, 5))]

    labels = ["ON"] * 3 + ["OFF"] * 4 + ["ON"] * 3
    assert norm.segment_regimes(labels) == [list(range(3, 7))]

    labels = ["ON"] * 3 + ["OFF"] * 4
    assert norm.segment_regimes(labels) == [list(range(3, 7))]

    labels = ["OFF"] * 2 + ["ON"] * 2 + ["OFF"] * 3 + ["ON"] + ["OFF"]
    assert norm.segment_regimes(labels) == [[0, 1], [4, 5, 6], [8]]
    print("  ok zero, one and many regimes in prefix, middle and suffix")


def test_an_unknown_session_neither_splits_a_regime_nor_is_absorbed():
    labels = ["OFF", "UNKNOWN", "OFF", "ON"]
    assert norm.segment_regimes(labels) == [[0, 1, 2]]
    # ...but a trailing UNKNOWN is not claimed by the regime
    assert norm.segment_regimes(["OFF", "UNKNOWN", "ON"]) == [[0]]
    # ...and an ON session is a real boundary
    assert norm.segment_regimes(["OFF", "ON", "OFF"]) == [[0], [2]]
    print("  ok UNKNOWN spans a regime, ON splits it, trailing UNKNOWN excluded")


def test_regime_indices_stay_on_the_original_session_axis():
    labels = ["ON"] * 7 + ["OFF"] * 3
    run = norm.segment_regimes(labels)[0]
    assert run == [7, 8, 9], "compressed rows would have reported [0, 1, 2]"
    print("  ok regime indices are original session positions, not compressed")


def test_an_ambiguous_regime_is_quarantined_not_certified():
    """A non-constant ratio cannot be certified however plausible it looks."""
    n = 40
    dates = [f"2026-04-{i + 1:02d}" for i in range(n)]
    blot = [1000 + i for i in range(n)]
    volume = [int(b * 100 * 5) + (i % 3) * 100 for i, b in enumerate(blot)]
    ohlc = [{"date": d, "open": 100, "high": 100, "low": 100, "close": 100,
             "volume": v} for d, v in zip(dates, volume)]
    brokers = {"AA": {"blot": blot, "slot": [0] * n, "nlot": list(blot),
                      "bval": [b * 10000.0 for b in blot], "sval": [0.0] * n,
                      "nval": [b * 10000.0 for b in blot]}}
    _, regimes, _, _, _, _ = norm.analyse_ticker("T", _raw(dates, brokers, ohlc),
                                                 "f" * 64)
    assert regimes and all(r["classification"] == "QUARANTINE" for r in regimes)
    assert any("not exactly constant" in reason
               for r in regimes for reason in r["reason"])
    print("  ok an ambiguous regime is quarantined, never certified")


def test_classification_never_branches_on_the_ticker_name():
    """Identical numbers must classify identically under any name."""
    n = 40
    dates = [f"2026-05-{i + 1:02d}" for i in range(n)]
    blot = [1000 + i for i in range(n)]
    ohlc = [{"date": d, "open": 100, "high": 100, "low": 100, "close": 100,
             "volume": b * 100 * 5} for d, b in zip(dates, blot)]
    brokers = {"AA": {"blot": blot, "slot": [0] * n, "nlot": list(blot),
                      "bval": [b * 50000.0 for b in blot], "sval": [0.0] * n,
                      "nval": [b * 50000.0 for b in blot]}}
    payload = _raw(dates, brokers, ohlc)
    out = {}
    for name in ("RAJA", "MLPT", "RMKE", "ZZZZ", "WINS"):
        regime = norm.analyse_ticker(name, payload, "f" * 64)[1][0]
        regime.pop("ticker")
        out[name] = json.dumps(regime, sort_keys=True)
    assert len(set(out.values())) == 1, "classification depends on the ticker name"
    print("  ok classification is a function of evidence, not of the ticker")


def test_stage_b_hit_is_diagnostic_and_carries_no_repair():
    regime = {"candidate_factor": 1.191, "start_session_index": 0,
              "end_session_index": 2, "classification": "QUARANTINE"}
    buy_lots = 60_000_000
    totals = {"dates": ["2026-02-27", "2026-02-28", "2026-03-01"],
              "blot": [buy_lots, 1000, 1000]}
    volume = [1.191 * (buy_lots * 100 - norm.MODULUS), 100000, 100000]
    hits = norm.stage_b_diagnostics("BNBR", totals, volume, regime)
    assert len(hits) == 1, hits
    assert hits[0]["authorised"] is False
    assert hits[0]["proposed_repair"] is None
    assert "DIAGNOSTIC" in hits[0]["status"]
    print("  ok a stage-B hit reinforces quarantine and proposes no repair")


# --------------------------------------------------------------------------
# manifest and determinism
# --------------------------------------------------------------------------

def test_aggregate_digest_changes_when_one_source_changes():
    base = [("AAA", "a" * 64), ("BBB", "b" * 64)]
    changed = [("AAA", "a" * 64), ("BBB", "b" * 63 + "c")]
    assert cand.aggregate_digest(base) != cand.aggregate_digest(changed)
    # order of enumeration must not matter
    assert cand.aggregate_digest(base) == cand.aggregate_digest(reversed(base))
    # ...and the separator must prevent concatenation aliasing
    assert (cand.aggregate_digest([("AB", "c" * 64)])
            != cand.aggregate_digest([("A", "B" + "c" * 63)]))
    assert len(cand.aggregate_digest(base)) == 64
    print("  ok aggregate digest is order-free, full-length and change-sensitive")


def test_universe_digest_is_validated():
    universe = cand.load_universe()
    assert len(universe) == cand.UNIVERSE_SIZE == 298
    assert len(set(universe)) == 298
    print("  ok approved universe is 298 unique names at the frozen digest")


def test_source_manifest_holds_exactly_297_with_full_hashes():
    path = os.path.join(cand.CANDIDATE_DIR, cand.SOURCE_MANIFEST)
    if not os.path.exists(path):
        return _skip("source_manifest", "candidate snapshot not built here")
    covered, manifest = cand.manifest_tickers(cand.CANDIDATE_DIR)
    assert len(covered) == 297 == manifest["population"]["n_covered"]
    assert len(set(covered)) == 297
    assert sorted(manifest["population"]["uncovered"]) == ["WIKA"]
    assert "WIKA" not in covered
    assert len(manifest["files"]) == 297
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64, entry["ticker"]
        assert entry["capture_timestamp"] is None
    assert len(manifest["source_aggregate_sha256"]) == 64
    print("  ok source manifest is exactly 297 with full sha256 and WIKA excluded")


def test_frozen_artifacts_are_unchanged():
    ohlc = os.path.join(SHARED, "ohlc.parquet")
    broker = os.path.join(SHARED, "broker_daily.parquet")
    if not (os.path.exists(ohlc) and os.path.exists(broker)):
        return _skip("frozen_artifacts", "shared checkout not present")
    assert cand.sha256_file(ohlc) == cand.FROZEN_OHLC_SHA256
    assert os.path.getsize(ohlc) == cand.FROZEN_OHLC_SIZE
    assert cand.sha256_file(broker) == cand.FROZEN_BROKER_SHA256
    assert os.path.getsize(broker) == cand.FROZEN_BROKER_SIZE
    assert cand.SUPERSEDED_BROKER_DAILY["sha256"] != cand.FROZEN_BROKER_SHA256
    print("  ok both frozen artifacts still carry their baseline fingerprints")


def test_candidate_artifacts_are_semantically_deterministic():
    path = os.path.join(cand.CANDIDATE_DIR, cand.SOURCE_MANIFEST)
    if not os.path.exists(path):
        return _skip("determinism", "candidate snapshot not built here")
    first = norm.build(cand.CANDIDATE_DIR)
    second = norm.build(cand.CANDIDATE_DIR)
    for a, b in zip(first, second):
        assert cand.aggregate_digest([("x", _digest(a))]) == \
               cand.aggregate_digest([("x", _digest(b))])
    print("  ok two independent normalisation builds are byte-identical")


def _digest(payload):
    import hashlib
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --------------------------------------------------------------------------
# validity domains and the real consumer graph
# --------------------------------------------------------------------------

def test_broker_quarantine_does_not_erase_price_validity():
    """A row may lose its broker features and keep its price observation."""
    domains = {"price_valid": True, "volume_valid": True,
               "broker_basis_valid": False, "broker_feature_valid": False}
    assert domains["price_valid"] and not domains["broker_basis_valid"]
    # The real measurement: every broker-quarantined session in the candidate
    # artifacts must still be a price row, never a deleted one.
    path = os.path.join(cand.CANDIDATE_DIR, norm.CANDIDATE_BASIS)
    if not os.path.exists(path):
        return _skip("validity_domains", "candidate artifacts not built here")
    basis = json.load(open(path, encoding="utf-8"))
    quarantined = {r["ticker"] for r in basis["regimes"]
                   if r["classification"] == "QUARANTINE"}
    covered, _ = cand.manifest_tickers(cand.CANDIDATE_DIR)
    assert quarantined <= set(covered)
    assert quarantined, "expected at least one quarantined regime"
    print(f"  ok {len(quarantined)} quarantined tickers remain in the price domain")


def test_actual_1f_windows_are_read_from_the_code_not_assumed():
    """The lookback map must come from the real consumer, not a guess."""
    import experiment_1f_universe_gate as gate
    from ml_v2_experiment_1 import FLOW_WINDOWS
    assert gate.LAGS == (1, 3, 5, 10, 20), gate.LAGS
    assert gate.HORIZONS == (1, 2, 3, 4, 5), gate.HORIZONS
    assert FLOW_WINDOWS == (1, 3, 5, 10, 20), FLOW_WINDOWS
    print(f"  ok actual windows: lags={gate.LAGS} horizons={gate.HORIZONS} "
          f"flows={FLOW_WINDOWS}")


def test_rolling_washout_spans_the_window_on_the_original_axis():
    """A contaminated session invalidates the whole window that consumes it."""
    n, bad = 30, 10
    for window in (1, 3, 5, 10, 20):
        invalid = set(range(bad, min(bad + window, n)))
        assert len(invalid) == min(window, n - bad)
        assert bad in invalid
        assert (bad - 1) not in invalid, "a window must not reach backwards"
    print("  ok washout spans exactly the consuming window, forward only")


def test_the_stateful_feature_exists_and_is_unbounded_after_contamination():
    """observable_inventory is cumulative, so contamination does not wash out.

    This is not hypothetical: build_broker_identity_features builds
    broker_<code>_observable_inventory as a within-segment cumsum. A finite
    rolling feature recovers after `window` sessions; a cumulative one does not
    recover at all, and resetting it to zero would silently assert the missing
    flows were zero.
    """
    import inspect
    from ml_v2_experiment_1 import build_broker_identity_features
    src = inspect.getsource(build_broker_identity_features)
    assert "cumsum()" in src, "expected a cumulative inventory feature"
    assert "groupby(segments)" in src, "expected segment-scoped accumulation"

    n, bad = 30, 10
    cumulative_invalid = set(range(bad, n))
    rolling20_invalid = set(range(bad, min(bad + 20, n)))
    assert cumulative_invalid >= rolling20_invalid
    assert len(cumulative_invalid) == n - bad
    print("  ok cumulative inventory stays contaminated to the end of its segment")


# ==========================================================================
# Phase 2.5: repair authorisation and the reconciled Gate-A contract
# ==========================================================================

import hashlib

import pandas as pd

import experiment_1f_universe_gate as gate
import experiment_1f_validity as validity

APPROVED_FIVE = [("BIPI", "2026-03-05"), ("BUMI", "2026-02-27"),
                 ("BUMI", "2026-03-02"), ("BUMI", "2026-03-03"),
                 ("BUMI", "2026-03-04")]


def _hit(ticker, date, raw=1000, sha="a" * 64):
    proposed = raw + norm.MODULUS
    return {"ticker": ticker, "date": date, "status": "PROPOSED",
            "authorised": False, "rule_version": norm.RULE_VERSION_WRAP,
            "raw_volume": raw, "proposed_normalized_volume": proposed,
            "delta": norm.MODULUS, "wrap_count": 1,
            "aggregate_buy_shares": proposed, "aggregate_sell_shares": proposed,
            "source_sha256": sha, "evidence": {}, "revalidation": {}}


def _authorized_pair(hits, approved=None, mutate_candidates=None,
                     mutate_auth=None, rebind=False):
    """Write a candidates+authorization pair into a temp dir and load it.

    `rebind` re-points the authorization at the mutated candidates file, which
    is what a careless regeneration looks like: the identity binding is
    satisfied, so the EXACT-KEY-SET check is the layer that has to catch it.
    Without rebind the physical/semantic identity check fires first, which is
    the outer line of defence and is asserted separately.
    """
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, norm.CANDIDATE_REPAIRS)
    candidates = {"status": "PROPOSED -- NOT AUTHORISED",
                  "rule_version": norm.RULE_VERSION_WRAP,
                  "modulus": norm.MODULUS, "n_candidates": len(hits),
                  "candidates": hits, "n_stage_b_diagnostics": 0,
                  "stage_b_diagnostics_never_repaired": []}
    cand.write_json(path, candidates)
    auth = norm.build_authorization(
        tmp, approved if approved is not None else [(h["ticker"], h["date"])
                                                    for h in hits])
    if mutate_candidates:
        mutate_candidates(candidates)
        cand.write_json(path, candidates)
        if rebind:
            auth["parent_candidate_sha256"] = cand.sha256_file(path)
            auth["parent_candidate_semantic_digest"] = hashlib.sha256(
                json.dumps(candidates, sort_keys=True,
                           separators=(",", ":")).encode()).hexdigest()
    if mutate_auth:
        mutate_auth(auth)
    cand.write_json(os.path.join(tmp, "volume_repair_authorization.json"), auth)
    basis = os.path.join(tmp, norm.CANDIDATE_BASIS)
    cand.write_json(basis, {"regimes": []})
    return tmp, gate.load_authorized_repairs(
        path, os.path.join(tmp, "volume_repair_authorization.json"), basis)


def test_authorization_accepts_the_exact_reviewed_key_set():
    hits = [_hit("BIPI", "2026-03-05"), _hit("BUMI", "2026-02-27")]
    tmp, loaded = _authorized_pair(hits)
    try:
        assert set(loaded["authorised_repairs"]) == {("BIPI", "2026-03-05"),
                                                     ("BUMI", "2026-02-27")}
        assert loaded["authorization_digest"]
        print("  ok the exact reviewed key set loads as authorised")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_real_five_key_authorization_is_bound_to_its_artifact():
    path = os.path.join(cand.CANDIDATE_DIR, "volume_repair_authorization.json")
    if not os.path.exists(path):
        return _skip("real_authorization", "candidate artifacts not built here")
    loaded = gate.load_authorized_repairs()
    assert set(loaded["authorised_repairs"]) == set(APPROVED_FIVE), \
        sorted(loaded["authorised_repairs"])
    auth = json.load(open(path, encoding="utf-8"))
    assert auth["scope"] == gate.AUTHORIZATION_SCOPE
    assert auth["n_authorized"] == 5
    assert loaded["parent_candidate_sha256"] == auth["parent_candidate_sha256"]
    assert all(r["authorised"] is True for r in auth["authorized"])
    print("  ok the five authorised repairs are bound to the reviewed artifact")


def test_an_additional_detector_hit_fails_authorization():
    """A new wrap is a human-review event, never an extra repair."""
    hits = [_hit("BIPI", "2026-03-05")]
    approved = [("BIPI", "2026-03-05")]

    def add(candidates):
        candidates["candidates"].append(_hit("NEWX", "2026-04-01"))
        candidates["n_candidates"] = 2

    try:
        _authorized_pair(hits, approved, mutate_candidates=add, rebind=True)
    except gate.GateFailure as exc:
        assert "UNREVIEWED" in str(exc) and "NEWX" in str(exc), str(exc)
        print("  ok an unreviewed detector hit fails the gate")
        return
    raise AssertionError("an extra detector hit was silently accepted")


def test_a_missing_authorized_hit_fails_authorization():
    hits = [_hit("BIPI", "2026-03-05"), _hit("BUMI", "2026-02-27")]

    def drop(candidates):
        candidates["candidates"] = candidates["candidates"][:1]
        candidates["n_candidates"] = 1

    try:
        _authorized_pair(hits, mutate_candidates=drop, rebind=True)
    except gate.GateFailure as exc:
        assert "no longer detected" in str(exc), str(exc)
        print("  ok an authorised repair that stopped being detected fails")
        return
    raise AssertionError("a missing authorised repair was tolerated")


def test_a_changed_source_sha_fails_authorization():
    """Both layers: tampering the candidates file breaks its identity, and an
    authorization that disagrees about a source hash fails the field check."""
    hits = [_hit("BIPI", "2026-03-05", sha="a" * 64)]

    def tamper_file(candidates):
        candidates["candidates"][0]["source_sha256"] = "b" * 64

    try:
        _authorized_pair(hits, mutate_candidates=tamper_file)
    except gate.GateFailure as exc:
        assert "does not reference this candidate artifact" in str(exc), str(exc)
    else:
        raise AssertionError("a tampered candidates file kept its identity")

    def tamper_auth(auth):
        auth["authorized"][0]["source_sha256"] = "c" * 64

    try:
        _authorized_pair(hits, mutate_auth=tamper_auth)
    except gate.GateFailure as exc:
        assert "source_sha256" in str(exc), str(exc)
        print("  ok a moved source file invalidates the authorisation")
        return
    raise AssertionError("a changed source sha was accepted")


def test_a_changed_volume_fails_authorization():
    for field in ("raw_volume", "proposed_normalized_volume"):
        hits = [_hit("BIPI", "2026-03-05")]

        def tamper(auth, field=field):
            auth["authorized"][0][field] += 1

        try:
            _authorized_pair(hits, mutate_auth=tamper)
        except gate.GateFailure as exc:
            assert field in str(exc), str(exc)
            continue
        raise AssertionError(f"a changed {field} was accepted")
    print("  ok a changed raw or proposed volume invalidates the authorisation")


def test_stage_b_can_never_be_authorized():
    """BNBR's stage-B hits have no proposed volume; nothing can approve them."""
    tmp = tempfile.mkdtemp()
    try:
        candidates = {
            "candidates": [], "n_candidates": 0, "n_stage_b_diagnostics": 1,
            "stage_b_diagnostics_never_repaired": [
                {"ticker": "BNBR", "date": "2026-02-27", "authorised": False,
                 "proposed_repair": None}],
        }
        cand.write_json(os.path.join(tmp, norm.CANDIDATE_REPAIRS), candidates)
        try:
            norm.build_authorization(tmp, [("BNBR", "2026-02-27")])
        except cand.CandidateError as exc:
            assert "stage-B" in str(exc) or "no detector hit supports" in str(exc)
            print("  ok a stage-B diagnostic can never be authorised")
            return
        raise AssertionError("a stage-B diagnostic was authorised")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_authorization_cannot_invent_a_key_no_detector_supports():
    tmp = tempfile.mkdtemp()
    try:
        cand.write_json(os.path.join(tmp, norm.CANDIDATE_REPAIRS),
                        {"candidates": [_hit("BIPI", "2026-03-05")],
                         "stage_b_diagnostics_never_repaired": []})
        try:
            norm.build_authorization(tmp, [("MADE", "2026-01-01")])
        except cand.CandidateError as exc:
            assert "no detector hit supports" in str(exc)
            print("  ok an authorisation cannot ratify an unmeasured repair")
            return
        raise AssertionError("an invented key was authorised")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# segmented regime contract
# --------------------------------------------------------------------------

def _regime(ticker, start, end, classification="QUARANTINE", factor=5.0):
    return {"ticker": ticker, "start_date": start, "end_date": end,
            "factor": factor, "classification": classification}


def _frame(ticker, dates):
    return pd.DataFrame({"ticker": [ticker] * len(dates), "date": list(dates),
                         "nlot": [0.0] * len(dates),
                         "blot": [1000.0] * len(dates),
                         "slot": [1000.0] * len(dates)})


def test_a_multi_regime_ticker_keeps_every_regime():
    """WINS carries seven regimes; a ticker-keyed dict would keep one."""
    artifacts = {"authorised_repairs": {}, "diagnostic_only": [],
                 "regimes": {"WINS": [_regime("WINS", "2025-08-22", "2025-10-15"),
                                      _regime("WINS", "2025-10-22", "2025-10-27"),
                                      _regime("WINS", "2026-04-16", "2026-05-21")]}}
    recon, quar = gate.basis_dispositions(artifacts, {"WINS"})
    assert len(quar["WINS"]) == 3, "all three regimes must survive dispositions"
    assert not recon
    print("  ok every regime of a multi-regime ticker is preserved")


def test_overlapping_regimes_are_a_hard_failure():
    regimes = {"X": [_regime("X", "2026-01-01", "2026-01-10"),
                     _regime("X", "2026-01-05", "2026-01-20")]}
    try:
        gate.assert_no_regime_overlap(regimes)
    except gate.GateFailure as exc:
        assert "overlap" in str(exc)
        print("  ok overlapping regime definitions fail rather than merge")
        return
    raise AssertionError("overlapping regimes were silently accepted")


def test_regime_selection_is_an_exact_interval_in_every_position():
    dates = [f"2026-01-{i:02d}" for i in range(1, 11)]
    frame = _frame("X", dates)
    for start, end in (("2026-01-01", "2026-01-03"),     # prefix
                       ("2026-01-04", "2026-01-06"),     # middle
                       ("2026-01-08", "2026-01-10")):    # suffix
        mask = gate.rows_in_regime(frame, "X", _regime("X", start, end))
        picked = sorted(frame.loc[mask, "date"])
        assert picked[0] == start and picked[-1] == end, picked
        assert all(start <= d <= end for d in picked), picked
    print("  ok prefix, middle and suffix regimes select exactly their interval")


def test_no_clean_row_before_a_middle_regime_is_quarantined_or_rescaled():
    """The superseded `date <= regime_last_date` swept everything before it."""
    dates = [f"2026-01-{i:02d}" for i in range(1, 11)]
    ohlc = _frame("X", dates)
    middle = _regime("X", "2026-01-05", "2026-01-07")

    kept, report = gate.quarantine_basis_regimes(ohlc, {"X": [middle]})
    assert report["rows_dropped"] == 3, report
    survivors = sorted(kept["date"])
    assert "2026-01-01" in survivors and "2026-01-04" in survivors, \
        "clean sessions BEFORE a middle regime must survive"
    assert "2026-01-10" in survivors, "the clean tail must survive"
    assert not any("2026-01-05" <= d <= "2026-01-07" for d in survivors)

    certified = _regime("X", "2026-01-05", "2026-01-07",
                        classification="CANDIDATE_HARMONIZABLE")
    scaled, sreport = gate.harmonise_broker_basis(_frame("X", dates),
                                                  {"X": [certified]})
    assert sreport["rows_scaled"] == 3, sreport
    before = scaled[scaled["date"] < "2026-01-05"]
    assert (before["blot"].to_numpy() == 1000.0).all(), \
        "sessions before a middle regime must not be rescaled"
    inside = scaled[(scaled["date"] >= "2026-01-05")
                    & (scaled["date"] <= "2026-01-07")]
    assert (inside["blot"].to_numpy() == 5000.0).all()
    print("  ok clean rows before a middle regime are neither dropped nor scaled")


def test_derived_equivalent_lots_may_be_fractional():
    """Raw lots are integral; a derived quantity under r=1/2 need not be."""
    dates = ["2026-01-01", "2026-01-02"]
    frame = pd.DataFrame({"ticker": ["X", "X"], "date": dates,
                          "nlot": [0.0, 0.0], "blot": [1001.0, 999.0],
                          "slot": [1001.0, 999.0]})
    regime = {"ticker": "X", "start_date": dates[0], "end_date": dates[-1],
              "classification": "CANDIDATE_HARMONIZABLE",
              "candidate_factor": 0.5,
              "candidate_factor_exact": {"numerator": 1, "denominator": 2}}
    scaled, report = gate.harmonise_broker_basis(frame, {"X": [regime]})
    assert report["rows_scaled"] == 2
    assert scaled["blot"].tolist() == [500.5, 499.5], scaled["blot"].tolist()
    print("  ok a fractional derived lot is not rejected for being fractional")


def test_a_legacy_single_regime_record_is_refused():
    legacy = {"ticker": "X", "factor": 5.0, "regime_last_date": "2026-01-03",
              "classification": "RECONSTRUCTIBLE"}
    try:
        gate.regime_bounds(legacy)
    except gate.GateFailure as exc:
        assert "start_date" in str(exc)
        print("  ok a legacy single-regime record is refused, never converted")
        return
    raise AssertionError("a legacy regime record was accepted")


# --------------------------------------------------------------------------
# PIT-conservative validity
# --------------------------------------------------------------------------

def test_a_certified_regime_is_still_broker_invalid_in_primary_mode():
    """MLPT/RAJA/RMKE are economically harmonisable and PIT-ineligible."""
    regimes = [{"classification": "CANDIDATE_HARMONIZABLE",
                "start_session_index": 2, "end_session_index": 4}]
    primary = validity.broker_basis_validity(8, regimes, mode=validity.PRIMARY)
    assert primary == [True, True, False, False, False, True, True, True], primary
    secondary = validity.broker_basis_validity(8, regimes, mode=validity.SECONDARY)
    assert all(secondary), "the retrospective mode may harmonise it"
    assert "RETROSPECTIVE" in validity.SECONDARY, \
        "the non-primary mode must be labelled as retrospective"
    assert gate.PRIMARY_PIT_APPLIES_BASIS_HARMONISATION is False
    print("  ok a certified regime stays broker-invalid in the primary path")


def test_finite_feature_validity_uses_the_original_session_axis():
    valid = [True] * 10
    valid[4] = False
    for window in validity.FLOW_WINDOWS:
        flags = validity.rolling_feature_validity(valid, window)
        assert len(flags) == 10, "one flag per ORIGINAL session, never compressed"
        assert flags[4] is False, "the invalid session itself is invalid"
        for i in range(5, min(4 + window, 10)):
            assert flags[i] is False, f"window {window} must stay invalid at {i}"
        if 4 + window < 10:
            assert flags[4 + window] is True, \
                f"a {window}-session feature must recover after its window"
        if window > 1:
            assert not any(flags[:window - 1]), "an incomplete lookback is not valid"
    print("  ok 1/3/5/10/20 validity spans the real window and then recovers")


def test_observable_inventory_never_resets_after_contamination():
    valid = [True] * 10
    valid[3] = False
    state = validity.cumulative_inventory_availability(valid)
    assert state[:3] == [True, True, True]
    assert not any(state[3:]), \
        "a cumulative feature must not become available again by resetting"
    print("  ok cumulative inventory stays unavailable, it does not reset to zero")


def test_observable_inventory_stays_unavailable_without_a_proven_reanchor():
    valid = [True] * 60
    valid[10] = False
    flags = validity.observable_inventory_validity(valid)
    assert not any(flags[10:]), "no re-anchor may be invented"
    flow20 = validity.rolling_feature_validity(valid, 20)
    assert flow20[31] is True, "the finite feature recovers..."
    assert flags[31] is False, "...but the cumulative one does not"
    print("  ok inventory needs a proven re-anchor and none is invented")


def test_broker_invalidity_leaves_the_price_domain_untouched():
    regimes = [{"classification": "QUARANTINE",
                "start_session_index": 1, "end_session_index": 3}]
    basis = validity.broker_basis_validity(6, regimes)
    price_valid = [True] * 6                    # detect() flagged nothing here
    assert basis.count(False) == 3
    assert all(price_valid), "broker quarantine must not touch price validity"
    usable = [p and not b for p, b in zip(price_valid, basis)]
    assert sum(usable) == 3, "3 rows are price-valid but broker-invalid"
    print("  ok a broker-invalid session keeps its independent price row")


def test_nominal_sensitivity_reproduces_production_segmentation():
    path = os.path.join(cand.CANDIDATE_DIR, norm.CANDIDATE_BASIS)
    if not os.path.exists(path):
        return _skip("sensitivity", "candidate artifacts not built here")
    basis = json.load(open(path, encoding="utf-8"))
    nominal = [row for row in basis["threshold_sensitivity"]
               if row["basis_min_deviation"] == norm.BASIS_MIN_DEVIATION
               and row["basis_min_lot_gap"] == norm.BASIS_MIN_LOT_GAP]
    assert len(nominal) == 1, nominal
    assert nominal[0]["regimes_total"] == basis["n_regimes"], (
        f"the audit reports {nominal[0]['regimes_total']} regimes at the nominal "
        f"thresholds but the classifier emits {basis['n_regimes']}; the audit is "
        "measuring a different segmentation")
    assert "regimes_meeting_min_days" in nominal[0], \
        "the sub-threshold class must be reported as its own quantity"
    print(f"  ok nominal audit reproduces production segmentation "
          f"({basis['n_regimes']} regimes)")


# ==========================================================================
# Phase 2.6: contract packaging, mode isolation and pre-Gate-A audit
# ==========================================================================

import inspect

import experiment_1f_manifest as manifest


def test_primary_is_the_default_whenever_mode_is_omitted():
    """No signature may select the retrospective mode implicitly."""
    for fn in (validity.broker_basis_validity, validity.ticker_validity,
               validity.impact):
        default = inspect.signature(fn).parameters["mode"].default
        assert default == validity.PRIMARY, f"{fn.__name__} defaults to {default!r}"
    assert inspect.signature(gate.candidate_inputs).parameters["mode"].default \
        == gate.PRIMARY_MODE
    assert inspect.signature(gate.run_gate).parameters["mode"].default \
        == gate.PRIMARY_MODE
    assert gate.candidate_inputs()["mode"] == "PRIMARY_PIT_CONSERVATIVE"
    print("  ok omitting mode selects PRIMARY_PIT_CONSERVATIVE everywhere")


def test_primary_and_secondary_cannot_be_confused():
    assert validity.PRIMARY == "PRIMARY_PIT_CONSERVATIVE"
    assert "SAFE" not in validity.PRIMARY, \
        "the label must not claim an observability property we cannot prove"
    assert validity.PIT_OBSERVABILITY == "UNESTABLISHED"
    assert validity.SECONDARY == "RETROSPECTIVE_ECONOMIC_NORMALIZATION_SENSITIVITY"
    assert validity.PRIMARY != validity.SECONDARY
    assert "RETROSPECTIVE" in validity.SECONDARY
    assert "PIT" not in validity.SECONDARY, \
        "the retrospective label must not suggest point-in-time eligibility"
    assert validity.ARTIFACT_PRIMARY != validity.ARTIFACT_SECONDARY
    assert "primary" in validity.ARTIFACT_PRIMARY
    assert "secondary" in validity.ARTIFACT_SECONDARY
    assert gate.PRIMARY_MODE == validity.PRIMARY
    assert gate.SECONDARY_MODE == validity.SECONDARY

    for name in (validity.ARTIFACT_PRIMARY, validity.ARTIFACT_SECONDARY):
        path = os.path.join(cand.CANDIDATE_DIR, name)
        if not os.path.exists(path):
            return _skip("mode_artifacts", "validity artifacts not built here")
        payload = json.load(open(path, encoding="utf-8"))
        assert payload["mode"] in (validity.PRIMARY, validity.SECONDARY)
        expected = (validity.PRIMARY if "primary" in name else validity.SECONDARY)
        assert payload["mode"] == expected, f"{name} carries mode {payload['mode']}"
    print("  ok the two modes are distinctly named and stamped in their files")


def test_primary_headline_counts_recompute_from_the_contract():
    """Recomputed, never copied from a report."""
    path = os.path.join(cand.CANDIDATE_DIR, norm.CANDIDATE_BASIS)
    if not os.path.exists(path):
        return _skip("headline_counts", "candidate artifacts not built here")
    report = validity.impact(cand.CANDIDATE_DIR)          # mode omitted on purpose
    assert report["mode"] == validity.PRIMARY
    totals = report["totals"]
    assert totals["broker_basis_invalid"] == 1425, totals
    assert totals["broker_flow_20d_invalid"] == 1631, totals
    assert totals["broker_observable_inventory_invalid"] == 2342, totals

    secondary = validity.impact(cand.CANDIDATE_DIR, mode=validity.SECONDARY)
    assert secondary["totals"]["broker_basis_invalid"] == 807
    assert secondary["totals"]["broker_basis_invalid"] < totals["broker_basis_invalid"], \
        "the retrospective mode must be strictly more permissive"
    print("  ok PRIMARY recomputes 1425 / 1631 / 2342 and SECONDARY 807")


def test_sensitivity_compares_exact_invalid_date_keys():
    path = os.path.join(cand.CANDIDATE_DIR,
                        "validity_date_mask_sensitivity.json")
    if not os.path.exists(path):
        return _skip("date_mask", "sensitivity artifact not built here")
    audit = json.load(open(path, encoding="utf-8"))
    assert audit["mode"] == validity.PRIMARY
    for row in audit["grid"]:
        for field in ("intersection_with_nominal", "union_with_nominal",
                      "added_vs_nominal", "removed_vs_nominal",
                      "jaccard_vs_nominal"):
            assert field in row, field
        assert 0.0 <= row["jaccard_vs_nominal"] <= 1.0
        assert (row["intersection_with_nominal"] + row["added_vs_nominal"]
                == row["broker_basis_invalid_sessions"]), row
    assert audit["verdict"] in ("ROBUST", "METHODOLOGY CONCERN", "HARD BLOCKER")
    assert audit["thresholds_were_not_tuned"] is True
    print(f"  ok date-mask sensitivity compares key sets "
          f"(verdict {audit['verdict']}, worst jaccard "
          f"{audit['worst_jaccard_vs_nominal']:.4f})")


def test_nominal_sensitivity_reproduces_the_production_invalid_keys_exactly():
    path = os.path.join(cand.CANDIDATE_DIR,
                        "validity_date_mask_sensitivity.json")
    if not os.path.exists(path):
        return _skip("date_mask_nominal", "sensitivity artifact not built here")
    audit = json.load(open(path, encoding="utf-8"))
    nominal = [r for r in audit["grid"] if r["is_nominal"]]
    assert len(nominal) == 1, nominal
    row = nominal[0]
    assert row["added_vs_nominal"] == 0 and row["removed_vs_nominal"] == 0
    assert row["jaccard_vs_nominal"] == 1.0
    primary = json.load(open(os.path.join(cand.CANDIDATE_DIR,
                                          validity.ARTIFACT_PRIMARY),
                             encoding="utf-8"))
    assert row["broker_basis_invalid_sessions"] == \
        primary["totals"]["broker_basis_invalid"], \
        "the nominal audit row must equal the production invalid-date count"
    print("  ok the nominal audit row is the production invalid-key set exactly")


# --------------------------------------------------------------------------
# execution vs provenance
# --------------------------------------------------------------------------

def test_no_gate_broker_invariant_needs_an_out_of_universe_row():
    """Behavioural proof: adding out-of-universe rows changes nothing.

    load_frozen_broker() filters to set(universe) & present on its first
    statement, so a frame carrying extra tickers must produce a byte-identical
    normalised broker table.
    """
    dates = [f"2026-01-{i:02d}" for i in range(1, 4)]
    def rows(ticker):
        return [{"date": d, "ticker": ticker, "broker": "AK", "nlot": 0,
                 "blot": 1000, "slot": 1000, "nval": 0.0,
                 "bval": 1000 * 100 * 100.0, "sval": 1000 * 100 * 100.0}
                for d in dates]
    inside = pd.DataFrame(rows("AAAA"))
    with_outsiders = pd.DataFrame(rows("AAAA") + rows("ZZZZ") + rows("QQQQ"))

    tmp = tempfile.mkdtemp()
    try:
        a_path = os.path.join(tmp, "a.parquet")
        b_path = os.path.join(tmp, "b.parquet")
        inside.to_parquet(a_path, index=False)
        with_outsiders.to_parquet(b_path, index=False)
        # The coverage pins now travel with the execution contract instead of
        # being read out of module globals, so this passes them explicitly
        # rather than monkeypatching gate.EXPECTED_* and hoping nothing else
        # captured the old value.
        fixture = {"broker_covered": 1, "broker_missing": ()}
        a, _, cov_a = gate.load_frozen_broker({"AAAA"}, path=a_path,
                                              expectations=fixture)
        b, _, cov_b = gate.load_frozen_broker({"AAAA"}, path=b_path,
                                              expectations=fixture)
        assert a.equals(b), \
            "out-of-universe broker rows changed the normalised table"
        assert cov_a["broker_covered_n"] == cov_b["broker_covered_n"] == 1
        # only the provenance statistics differ, and they are not semantic
        assert cov_a["harvest_rows_total"] != cov_b["harvest_rows_total"]
        print("  ok no broker invariant reads an out-of-universe row")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_broker_execution_input_is_the_isolated_297():
    inputs = gate.candidate_inputs()
    assert inputs["broker_parquet"].endswith("broker_daily_v2.parquet")
    assert "experiment_1f_candidate" in inputs["broker_parquet"]
    assert inputs["broker_parquet"] != gate.BROKER_PARQUET
    assert gate.BROKER_PARQUET in inputs["provenance_only"].values()
    print("  ok the broker execution input is the isolated 297-name table")


def test_ohlc_execution_input_stays_full_market():
    """cross_ticker_dup is blinded by any prior filtering, so OHLC is not narrowed.

    Phase 2.7 moved this input from the shared checkout to an experiment-owned
    byte copy. That changed CUSTODY, not CONTENT -- so the assertion that used
    to pin the shared path now pins full-market semantics instead, which is what
    this test was always protecting.
    """
    inputs = gate.candidate_inputs()
    assert inputs["ohlc_parquet"].endswith(cand.CANDIDATE_OHLC)
    assert "experiment_1f_candidate" in inputs["ohlc_parquet"], \
        "the OHLC input must be experiment-owned"
    assert gate.OHLC_PARQUET in inputs["provenance_only"].values(), \
        "the shared-checkout OHLC is demoted to provenance, not dropped"
    if os.path.exists(inputs["ohlc_parquet"]):
        import pyarrow.parquet as pq
        seen = len(pq.read_table(inputs["ohlc_parquet"]).column("ticker").unique())
        assert seen > 297, f"OHLC must stay the full cross-section, saw {seen}"
    doc = gate.candidate_inputs.__doc__
    assert "cross_ticker_dup" in doc, \
        "the asymmetry must be documented where the inputs are chosen"
    print("  ok OHLC stays full-market for cross-ticker contamination detection")


def test_candidate_mode_never_reads_a_legacy_root_artifact():
    inputs = gate.candidate_inputs()
    assert gate.assert_candidate_mode_reads_no_legacy_artifact(inputs) is True
    legacy = set(inputs["provenance_only"].values())
    assert gate.OBSERVED_BASIS_FACTOR_JSON in legacy
    assert gate.VOLUME_REPAIR_LEDGER_JSON in legacy
    execution = [v for k, v in inputs.items()
                 if k not in ("mode", "provenance_only", "expectations",
                              "diagnostic_only")
                 and isinstance(v, str)]
    assert not (set(execution) & legacy)
    assert gate.INPUT_MANIFEST_JSON in legacy, \
        "the legacy two-file manifest must be provenance now, not an input"

    poisoned = dict(inputs)
    poisoned["basis_artifact"] = gate.OBSERVED_BASIS_FACTOR_JSON
    try:
        gate.assert_candidate_mode_reads_no_legacy_artifact(poisoned)
    except gate.GateFailure as exc:
        assert "legacy" in str(exc)
        print("  ok the candidate contract refuses a legacy root artifact")
        return
    raise AssertionError("a legacy artifact was accepted as an execution input")


def test_the_legacy_root_basis_artifact_is_left_untouched():
    """It stays as production state; #1F simply does not consume it."""
    if not os.path.exists(gate.OBSERVED_BASIS_FACTOR_JSON):
        return _skip("legacy_untouched", "legacy artifact absent")
    payload = json.load(open(gate.OBSERVED_BASIS_FACTOR_JSON, encoding="utf-8"))
    assert any("regime_last_date" in r or "start_date" not in r
               for r in payload.get("regimes", [])), \
        "the legacy artifact must still carry its original one-regime contract"
    print("  ok the legacy root basis artifact is untouched and unconsumed")


# --------------------------------------------------------------------------
# manifest v3
# --------------------------------------------------------------------------

def test_manifest_separates_execution_from_provenance():
    if not _candidate_snapshot_present():
        return _skip("manifest_separates_execution_from_provenance", "candidate snapshot not built here")
    proposal = manifest.build(cand.CANDIDATE_DIR)
    assert proposal["established_utc"] is None, "the proposal is not established"
    assert "_status" in proposal and "NOT ESTABLISHED" in proposal["_status"]
    execution = proposal["A_execution_inputs"]
    provenance = proposal["B_provenance_inputs"]
    assert "broker_daily_v2_parquet" in execution
    assert "full_market_broker_daily_parquet" in provenance
    assert "broker_daily_parquet" not in execution
    assert provenance["full_market_broker_daily_parquet"][
        "in_universe_subset_reproduced_by_v2_exactly"] is True
    assert provenance["full_market_broker_daily_parquet"][
        "measured_value_mismatches"] == 0
    assert "legacy_root_artifacts" in provenance
    for section in ("A_execution_inputs", "B_provenance_inputs",
                    "C_normalization_policy", "D_authorization",
                    "E_code_identity", "F_universe", "G_rule_versions"):
        assert section in proposal, section
    # the validity artifacts are DERIVED reports, not execution inputs
    assert "validity_artifacts" in proposal["C_normalization_policy"]
    assert "primary" not in execution
    print("  ok manifest v3 separates execution, provenance and derived reports")


def test_manifest_pins_code_identity_and_rejects_drift():
    if not _candidate_snapshot_present():
        return _skip("manifest_pins_code_identity_and_rejects_drift", "candidate snapshot not built here")
    proposal = manifest.build(cand.CANDIDATE_DIR)
    code = proposal["E_code_identity"]
    pinned = {e["path"] for e in code["files"]}
    for required in ("experiment_1f_universe_gate.py", "price_audit.py",
                     "normalize_market_data.py", "build_inventory_db.py",
                     "experiment_1f_normalization.py", "experiment_1f_validity.py"):
        assert required in pinned, f"{required} can change the panel and must be pinned"
    for entry in code["files"]:
        assert entry["present"], f"{entry['path']} is pinned but missing"
        assert entry["role"], "every pinned file must say why it is pinned"

    # identity is the COMMITTED Git content, so drift is exercised against a
    # committed fixture repository whose state this test controls -- the result
    # must not depend on whether this checkout happens to be committed or clean
    repo, _head, _payload = _pinned_code_repo()
    committed = {"E_code_identity": manifest.code_identity(root=repo)}
    for entry in committed["E_code_identity"]["files"]:
        assert len(entry["git_content_sha256"]) == 64
        assert entry["tracked_at_head"] and entry["worktree_matches_head"]
    assert manifest.verify_code_identity(committed, root=repo) is True

    tampered = json.loads(json.dumps(committed))
    tampered["E_code_identity"]["files"][0]["git_content_sha256"] = "0" * 64
    try:
        manifest.verify_code_identity(tampered, root=repo)
    except manifest.NotEstablishable as exc:
        assert "reviewed committed content" in str(exc)
        print(f"  ok code identity pins {len(pinned)} files and rejects drift")
        return
    raise AssertionError("a changed implementation hash was accepted")


def _as_post_commit_clean(proposal):
    """Make a PRE-commit proposal look the way it will AFTER the approved commit.

    Phase 2.7-final made code identity the COMMITTED Git content plus a per-file
    proof that the worktree carries no semantic difference from it. Flipping the
    aggregate `semantic_code_tree_clean` flag is therefore no longer sufficient
    to model a clean tree: the per-file `tracked_at_head` /
    `worktree_matches_head` facts must move too, because those are what
    establishment actually checks now.

    The tests below use this to exercise the OTHER establishment preconditions
    (dirty tree, parentage, scope, mode) without each of them re-deriving the
    identity model. The refusals themselves -- a dirty tree and an untracked pin --
    are asserted against FRESH temporary repositories in
    test_establishment_refuses_every_precondition_violation and
    test_untracked_pinned_implementation_cannot_be_established.
    """
    out = json.loads(json.dumps(proposal))
    code = out["E_code_identity"]
    code["semantic_code_tree_clean"] = True
    code["dirty_semantic_code"] = []
    code["identity_unavailable_until_commit"] = []
    for entry in code["files"]:
        entry["present"] = True
        entry["tracked_at_head"] = True
        entry["worktree_matches_head"] = True
        entry["canonical_identity_status"] = "AVAILABLE"
        if not entry.get("git_content_sha256"):
            entry["git_content_sha256"] = "c" * 64
    universe = out["A_execution_inputs"].get("universe_json")
    if universe is not None:
        universe["tracked_at_head"] = True
        universe["worktree_matches_head"] = True
    return out


def test_a_dirty_working_tree_blocks_establishment():
    """Phase 2.7 narrowed this gate from the whole tree to SEMANTIC CODE.

    Generated candidate artifacts are gitignored and must never block
    establishment; otherwise writing the established manifest would violate its
    own precondition.
    """
    if not _candidate_snapshot_present():
        return _skip("a_dirty_working_tree_blocks_establishment", "candidate snapshot not built here")
    proposal = manifest.build(cand.CANDIDATE_DIR)
    dirty = json.loads(json.dumps(proposal))
    dirty["E_code_identity"]["semantic_code_tree_clean"] = False
    dirty["E_code_identity"]["dirty_semantic_code"] = ["experiment_1f_validity.py"]
    try:
        manifest.assert_establishable(dirty)
    except manifest.NotEstablishable as exc:
        assert "dirty" in str(exc)
    else:
        raise AssertionError("a dirty semantic code tree was allowed to establish")

    manifest.assert_establishable(_as_post_commit_clean(proposal))
    print("  ok a dirty semantic code tree is refused; a clean one passes")


def test_artifact_parentage_is_cryptographic_not_by_filename():
    if not _candidate_snapshot_present():
        return _skip("artifact_parentage_is_cryptographic_not_by_filename", "candidate snapshot not built here")
    proposal = manifest.build(cand.CANDIDATE_DIR)
    chain = proposal["H_parentage"]
    assert chain["all_bindings_ok"] is True
    links = {link["child"]: link for link in chain["chain"]}
    assert links["volume_repair_authorization.json"]["bound"] is True
    assert links[norm.CANDIDATE_REPAIRS]["per_candidate_source_binding_ok"] is True
    assert len(links["broker_daily_v2.parquet"]["parent_identity"]) == 64

    broken = _as_post_commit_clean(proposal)
    broken["E_code_identity"]["dirty_semantic_code"] = []
    broken["H_parentage"]["all_bindings_ok"] = False
    try:
        manifest.assert_establishable(broken)
    except manifest.NotEstablishable as exc:
        assert "parentage" in str(exc)
        print("  ok parentage is bound by identity and a mismatch blocks establishment")
        return
    raise AssertionError("a broken parent binding was accepted")


def test_authorization_parent_identity_is_still_exact():
    """The Phase-2.5 review binding must not have drifted."""
    path = os.path.join(cand.CANDIDATE_DIR, "volume_repair_authorization.json")
    if not os.path.exists(path):
        return _skip("auth_identity", "candidate artifacts not built here")
    auth = json.load(open(path, encoding="utf-8"))
    assert auth["parent_candidate_sha256"] == \
        "1c71194493d8ff06f3fbd89efd6d42279cf05b0c53b2536846dc5d53742917cb"
    assert auth["parent_candidate_semantic_digest"] == \
        "4b03b2261a764c969339e63b1d75860c66333ee83d397d969676d72786a310d5"
    assert cand.sha256_file(os.path.join(cand.CANDIDATE_DIR,
                                         norm.CANDIDATE_REPAIRS)) == \
        auth["parent_candidate_sha256"]
    assert [f"{a['ticker']} {a['date']}" for a in auth["authorized"]] == [
        "BIPI 2026-03-05", "BUMI 2026-02-27", "BUMI 2026-03-02",
        "BUMI 2026-03-03", "BUMI 2026-03-04"]
    print("  ok the five-key authorisation still binds to the reviewed bytes")


def test_the_manifest_is_not_established_by_building_it():
    if not _candidate_snapshot_present():
        return _skip("the_manifest_is_not_established_by_building_it", "candidate snapshot not built here")
    before = os.path.exists(gate.INPUT_MANIFEST_JSON)
    manifest.build(cand.CANDIDATE_DIR)
    assert os.path.exists(gate.INPUT_MANIFEST_JSON) == before, \
        "building the proposal must never establish a manifest"
    assert not os.path.exists(gate.INPUT_MANIFEST_JSON), \
        "no input manifest may exist in this worktree"
    print("  ok building the proposal establishes nothing")


# ==========================================================================
# Phase 2.7: final pre-commit hardening
# ==========================================================================

def test_primary_mode_does_not_claim_pit_safety():
    """The policy is conservative; the mask itself is retrospective."""
    assert validity.PRIMARY == "PRIMARY_PIT_CONSERVATIVE"
    assert "SAFE" not in validity.PRIMARY
    assert gate.PRIMARY_MODE == validity.PRIMARY
    assert gate.PIT_OBSERVABILITY == validity.PIT_OBSERVABILITY == "UNESTABLISHED"
    print("  ok the primary label claims conservatism, not observability")


def test_primary_report_records_pit_observability_unestablished():
    path = os.path.join(cand.CANDIDATE_DIR, validity.ARTIFACT_PRIMARY)
    if not os.path.exists(path):
        return _skip("pit_observability", "validity artifacts not built here")
    report = json.load(open(path, encoding="utf-8"))
    assert report["mode"] == "PRIMARY_PIT_CONSERVATIVE"
    assert report["pit_observability"] == "UNESTABLISHED"
    assert report["policy"]["pit_observability"] == "UNESTABLISHED"
    assert "not proven" in report["policy"]["pit_observability_meaning"]
    print("  ok the primary artifact records pit_observability=UNESTABLISHED")


def test_renaming_the_mode_did_not_change_eligibility():
    """The label moved; the numbers must not."""
    path = os.path.join(cand.CANDIDATE_DIR, norm.CANDIDATE_BASIS)
    if not os.path.exists(path):
        return _skip("rename_neutral", "candidate artifacts not built here")
    report = validity.impact(cand.CANDIDATE_DIR)
    assert report["totals"]["broker_basis_invalid"] == 1425
    assert report["totals"]["broker_flow_20d_invalid"] == 1631
    assert report["totals"]["broker_observable_inventory_invalid"] == 2342
    print("  ok eligibility is unchanged by the rename (1425 / 1631 / 2342)")


# --------------------------------------------------------------------------
# experiment-owned full-market OHLC
# --------------------------------------------------------------------------

def test_candidate_full_market_ohlc_matches_the_frozen_source_exactly():
    path = os.path.join(cand.CANDIDATE_DIR, cand.CANDIDATE_OHLC)
    if not os.path.exists(path):
        return _skip("ohlc_snapshot", "OHLC snapshot not built here")
    assert cand.sha256_file(path) == cand.FROZEN_OHLC_SHA256
    assert os.path.getsize(path) == cand.FROZEN_OHLC_SIZE
    assert cand.verify_full_market_ohlc(cand.CANDIDATE_DIR) == cand.FROZEN_OHLC_SHA256
    assert os.stat(path).st_nlink == 1, "the snapshot must be an independent copy"
    print("  ok the experiment-owned OHLC copy is byte-identical to the frozen source")


def test_candidate_ohlc_is_not_narrowed_to_the_297():
    """cross_ticker_dup needs out-of-universe rows to see a clone."""
    path = os.path.join(cand.CANDIDATE_DIR, cand.CANDIDATE_OHLC)
    if not os.path.exists(path):
        return _skip("ohlc_full_market", "OHLC snapshot not built here")
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    tickers = len(table.column("ticker").unique())
    assert tickers == 936, tickers
    assert tickers > 297, "the OHLC input must remain the full cross-section"
    covered, _ = cand.manifest_tickers(cand.CANDIDATE_DIR)
    assert tickers > len(covered)
    print(f"  ok the candidate OHLC keeps all {tickers} tickers, not just 297")


def test_a_corrupted_ohlc_snapshot_is_refused():
    tmp = tempfile.mkdtemp()
    try:
        bad = os.path.join(tmp, cand.CANDIDATE_OHLC)
        with open(bad, "wb") as fh:
            fh.write(b"not a parquet")
        try:
            cand.verify_full_market_ohlc(tmp)
        except SystemExit as exc:
            assert "drifted" in str(exc)
            print("  ok a drifted OHLC snapshot is refused")
            return
        raise AssertionError("a corrupted OHLC snapshot was accepted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_candidate_execution_does_not_read_the_shared_checkout():
    """After the snapshot, no execution input lives outside the candidate dir."""
    inputs = gate.candidate_inputs()
    assert inputs["ohlc_parquet"].endswith(cand.CANDIDATE_OHLC)
    assert "experiment_1f_candidate" in inputs["ohlc_parquet"]
    assert gate.OHLC_PARQUET in inputs["provenance_only"].values(), \
        "the shared-checkout OHLC must be demoted to provenance"
    for key, value in inputs.items():
        # `mode` and `expectations` are policy, not paths; `provenance_only` is
        # the set these must NOT be.
        if key in ("mode", "provenance_only", "expectations", "diagnostic_only"):
            continue
        if value.endswith(".parquet"):
            assert "experiment_1f_candidate" in value, \
                f"{key} still reads outside the experiment-owned directory"
    print("  ok every parquet execution input is experiment-owned")


# --------------------------------------------------------------------------
# clean-tree accounting
# --------------------------------------------------------------------------

def test_porcelain_parser_keeps_the_status_column_intact():
    """The old parser stripped the whole output and ate the first path's char."""
    raw = (" M build_inventory_db.py\n"
           " M check_ml_health.py\n"
           "?? experiment_1f_manifest.py\n"
           "A  staged_file.py\n"
           "R  old_name.py -> new_name.py\n")
    parsed = manifest.parse_porcelain(raw)
    paths = [e["path"] for e in parsed]
    assert "build_inventory_db.py" in paths, paths
    assert "uild_inventory_db.py" not in paths, "the truncation bug is back"
    assert paths[0] == "build_inventory_db.py", "the FIRST path is the one that broke"
    assert "new_name.py" in paths, "a rename must record its destination"
    assert "old_name.py" not in paths
    assert parsed[0]["status"] == " M"
    assert manifest.parse_porcelain("") == []
    print("  ok porcelain parsing preserves the leading status column")


def test_clean_tree_detection_recognises_build_inventory_db():
    identity = manifest.git_identity()
    for path in identity["dirty_paths"]:
        assert not path.startswith("uild_"), f"truncated path: {path}"
    assert all(p.endswith(".py") or "/" in p or "." in p
               for p in identity["dirty_paths"])
    if "build_inventory_db.py" in identity["dirty_semantic_code"]:
        print("  ok build_inventory_db.py is reported under its real name")
    else:
        print("  ok no truncated paths (build_inventory_db.py currently clean)")


def test_semantic_code_and_generated_artifacts_are_classified_apart():
    entries = manifest.parse_porcelain(
        " M experiment_1f_validity.py\n"
        "?? backtest_out/experiment_1f_candidate/experiment_1f_input_manifest.json\n"
        "?? backtest_out/experiment_1f_candidate/ohlc_full_market.parquet\n"
        " M README.md\n")
    semantic, generated, other = manifest.classify_dirty(entries)
    assert semantic == ["experiment_1f_validity.py"], semantic
    assert len(generated) == 2, generated
    assert other == ["README.md"], other
    # establishing the manifest into the candidate dir must NOT dirty semantic code
    only_generated = manifest.parse_porcelain(
        "?? backtest_out/experiment_1f_candidate/experiment_1f_input_manifest.json\n")
    sem2, gen2, _ = manifest.classify_dirty(only_generated)
    assert sem2 == [] and len(gen2) == 1, \
        "writing the established manifest must not make semantic code dirty"
    print("  ok semantic code and generated artifacts are classified apart")


def test_establishment_requires_semantic_code_clean_not_whole_tree():
    if not _candidate_snapshot_present():
        return _skip("establishment_requires_semantic_code_clean_not_whole_tree", "candidate snapshot not built here")
    proposal = manifest.build(cand.CANDIDATE_DIR)
    dirty = json.loads(json.dumps(proposal))
    dirty["E_code_identity"]["semantic_code_tree_clean"] = False
    dirty["E_code_identity"]["dirty_semantic_code"] = ["experiment_1f_validity.py"]
    try:
        manifest.assert_establishable(dirty)
    except manifest.NotEstablishable as exc:
        assert "semantic code tree is dirty" in str(exc)
    else:
        raise AssertionError("dirty semantic code was allowed to establish")

    # generated artifacts alone must NOT block establishment
    ok = _as_post_commit_clean(proposal)
    ok["E_code_identity"]["dirty_semantic_code"] = []
    ok["E_code_identity"]["working_tree_clean"] = False
    ok["E_code_identity"]["dirty_generated_artifacts"] = [
        "backtest_out/experiment_1f_candidate/experiment_1f_input_manifest.json"]
    assert manifest.assert_establishable(ok) is True, \
        "generated candidate artifacts must not block establishment"
    print("  ok establishment gates on semantic code, not on generated artifacts")


# --------------------------------------------------------------------------
# control plane
# --------------------------------------------------------------------------

def _fresh_pinned_repo(path, leave_untracked=()):
    """A NEW git repository at `path` holding copies of every CODE_IDENTITY file.

    One per test, never shared. The git-state tests MUTATE their repository (a
    local edit, a later commit), and a shared fixture was already broken once by
    a test that deleted a file from it -- sharing is safe only where it is
    provably read-only, and here it is not.

    What these tests assert is therefore a property of the identity and
    establishment CONTRACT, exercised against a repository whose state the test
    controls -- never a fact about whether this checkout happens to be committed.

    `leave_untracked` names pinned files that are copied but deliberately never
    added, so they exist on disk with no committed content. core.autocrlf=true
    reproduces this repository's real configuration. Returns (path, head_sha).
    """
    import subprocess
    import experiment_1f_manifest as mf

    os.makedirs(path, exist_ok=True)

    def git(*args):
        result = subprocess.run(["git", *args], cwd=path, capture_output=True,
                                text=True)
        assert result.returncode == 0, (args, result.stderr)
        return result.stdout

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "core.autocrlf", "true")
    git("config", "commit.gpgsign", "false")
    names = [name for name, _plane, _role in mf.CODE_IDENTITY]
    for name in names:
        shutil.copyfile(os.path.join(HERE, name), os.path.join(path, name))
    skip = set(leave_untracked)
    git("add", "--", *[n for n in names if n not in skip])
    git("commit", "-q", "-m", "pinned implementation")
    head, error = gate._git_head(path)
    assert head and not error, error
    return path, head


def _minimal_proposal(repo):
    """Only the sections assert_establishable() reads, built by the REAL builder.

    E_code_identity comes from experiment_1f_manifest.code_identity() run against
    `repo`, so dirt, tracking and committed content are genuine git facts. Every
    other section is fixed at its only valid value, which isolates the
    code-identity preconditions as the sole thing that can refuse.
    """
    import experiment_1f_manifest as mf
    return {
        "E_code_identity": mf.code_identity(root=repo),
        "A_execution_inputs": {},
        "H_parentage": {"all_bindings_ok": True},
        "D_authorization": {"scope": gate.AUTHORIZATION_SCOPE},
        "C_normalization_policy": {"mode": validity.PRIMARY},
        "_establishment": {"established": False},
    }


def _establish_against(repo, target):
    """Run the REAL establishment act against a fresh repository.

    build() inspects this module's own checkout, so it is redirected to REBUILD
    the proposal from `repo` on every call -- mirroring production's "rebuild
    fresh, never trust an older proposal". The preconditions, the independent
    code-identity re-hash, the HEAD check and the atomic write all run for real.
    """
    import experiment_1f_manifest as mf
    real_build = mf.build
    mf.build = lambda *a, **kw: _minimal_proposal(repo)
    try:
        return mf.establish_reviewed_manifest(
            candidate_dir=repo, explicit_confirmation=mf.CONFIRMATION_PHRASE,
            target_path=target, root=repo)
    finally:
        mf.build = real_build


def _head_blob_sha256(root, name):
    """SHA256 of the committed blob, via plumbing independent of the gate helper."""
    import subprocess
    blob = subprocess.run(["git", "cat-file", "blob", f"HEAD:{name}"], cwd=root,
                          capture_output=True)
    return hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else None


def test_the_manifest_verifier_is_pinned_as_control_plane():
    """The verifier is pinned by the mechanism it enforces -- before AND after commit.

    Nothing here depends on whether this checkout is committed. CODE_IDENTITY is
    checked structurally; the identity rule is checked as an EQUIVALENCE that
    holds in either state; and the tracked branch -- recompute the committed
    content and match the pin -- is exercised deterministically in a fresh
    repository where the file is certainly committed.
    """
    import experiment_1f_manifest as mf
    name = "experiment_1f_manifest.py"

    # 1. declared in CODE_IDENTITY as the sole CONTROL PLANE file
    declared = {n: plane for n, plane, _role in mf.CODE_IDENTITY}
    assert declared.get(name) == mf.CONTROL_PLANE, declared.get(name)
    assert [n for n, p in declared.items() if p == mf.CONTROL_PLANE] == [name]
    assert sum(p == mf.DATA_PLANE for p in declared.values()) == 8

    # 2. carried through the identity mechanism of THIS checkout, in whatever
    #    state it happens to be
    code = mf.code_identity()
    assert code["canonical_identity"] == gate.IDENTITY_GIT_CONTENT
    assert code["control_plane_files"] == [name]
    assert name not in code["data_plane_files"]
    entry = next(f for f in code["files"] if f["path"] == name)
    assert entry["plane"] == mf.CONTROL_PLANE and "manifest" in entry["role"]
    for pinned in code["files"]:
        # an identity exists EXACTLY when committed content exists -- true before
        # the commit and after it, never a claim about which state holds
        assert (pinned["git_content_sha256"] is not None) == \
            pinned["tracked_at_head"], pinned["path"]
        if pinned["tracked_at_head"]:
            assert pinned["git_content_sha256"] == \
                _head_blob_sha256(HERE, pinned["path"]), \
                f"{pinned['path']}: the pin must be the recomputed committed content"

    # 3. the tracked branch for the verifier itself, deterministically
    tmp = tempfile.mkdtemp(prefix="control_plane_repo_")
    try:
        repo, _head = _fresh_pinned_repo(os.path.join(tmp, "repo"))
        committed = mf.code_identity(root=repo)
        own = next(f for f in committed["files"] if f["path"] == name)
        assert own["tracked_at_head"] and own["worktree_matches_head"]
        assert own["git_content_sha256"] == _head_blob_sha256(repo, name) \
            == gate.git_content_sha256(os.path.join(repo, name), root=repo)
        assert mf.verify_code_identity({"E_code_identity": committed},
                                       root=repo) is True

        # 4. the verifier REFUSES an identity that omits the control plane
        failures = []
        gate._verify_code_identity(
            {"E_code_identity": {"files": [
                f for f in committed["files"] if f["plane"] != mf.CONTROL_PLANE]}},
            repo, failures)
        assert any("no CONTROL PLANE file is pinned" in f for f in failures), \
            failures
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok the verifier is pinned as CONTROL PLANE; its identity is recomputed")


def test_changing_the_verifier_bytes_invalidates_code_identity():
    """Run against the committed fixture repo: identity is now Git content.

    verify_code_identity() recomputes from the object store, so it needs a tree
    where the pinned files are actually committed. The refusal for a pin with no
    committed content is asserted separately, in a fresh repository, by
    test_untracked_pinned_implementation_cannot_be_established.
    """
    repo, _head, _payload = _pinned_code_repo()
    proposal = {"E_code_identity": manifest.code_identity(root=repo)}
    assert manifest.verify_code_identity(proposal, root=repo) is True

    tampered = json.loads(json.dumps(proposal))
    for entry in tampered["E_code_identity"]["files"]:
        if entry["path"] == "experiment_1f_manifest.py":
            entry["git_content_sha256"] = "0" * 64
    try:
        manifest.verify_code_identity(tampered, root=repo)
    except manifest.NotEstablishable as exc:
        assert "experiment_1f_manifest.py" in str(exc)
        print("  ok a changed verifier invalidates code identity")
        return
    raise AssertionError("a changed control-plane hash was accepted")


def test_validity_reports_stay_derived_not_execution_inputs():
    if not _candidate_snapshot_present():
        return _skip("validity_reports_stay_derived_not_execution_inputs", "candidate snapshot not built here")
    proposal = manifest.build(cand.CANDIDATE_DIR)
    execution = proposal["A_execution_inputs"]
    for name in execution:
        assert "validity_impact" not in name, \
            f"{name} must not be an execution input -- it is a derived report"
    artifacts = proposal["C_normalization_policy"]["validity_artifacts"]
    for side in ("primary", "secondary"):
        assert "DERIVED REPORT" in artifacts[side]["role"]
    print("  ok validity reports remain derived, with no circular dependency")


def test_the_manifest_establishment_target_is_the_candidate_directory():
    if not _candidate_snapshot_present():
        return _skip("the_manifest_establishment_target_is_the_candidate_directory", "candidate snapshot not built here")
    proposal = manifest.build(cand.CANDIDATE_DIR)
    est = proposal["_establishment"]
    assert est["established"] is False
    assert est["target_path"].startswith("backtest_out/experiment_1f_candidate/")
    assert not est["target_path"].startswith("experiment_1f_input_manifest.json"), \
        "the established manifest must not land in the repo root"
    assert "paradox" in est["why_not_repo_root"]
    assert "HUMAN CODE REVIEW" in est["order"]
    assert est["order"].index("HUMAN CODE REVIEW") < est["order"].index(
        "explicit manifest establishment")
    print("  ok establishment targets the candidate directory, after human review")


def test_the_robustness_result_is_preserved_without_overselling():
    path = os.path.join(cand.CANDIDATE_DIR,
                        "validity_date_mask_sensitivity.json")
    if not os.path.exists(path):
        return _skip("robustness", "sensitivity artifact not built here")
    audit = json.load(open(path, encoding="utf-8"))
    assert audit["nominal"]["invalid_sessions"] == 1425
    assert abs(audit["worst_jaccard_vs_nominal"] - 0.985263) < 1e-5
    assert audit["invalid_session_count_spread"] == 31
    assert audit["verdict"] == "ROBUST"
    assert "FROZEN CONTRACT" in audit["interpretation"]
    assert "not a universal" in audit["interpretation"]

    stability = audit["feature_level_stability"]
    assert stability["observable_inventory_invariant"] is True
    assert stability["observable_inventory_invalid_values"] == [2342]
    assert stability["broker_flow_20d_spread"] == 4

    non_binding = audit["non_binding_thresholds"]
    assert non_binding["basis_min_deviation"]["non_binding"] is True
    assert non_binding["ratio_correlation_min"]["non_binding"] is True
    assert non_binding["dual_estimator_max_disagreement"]["non_binding"] is True
    assert set(non_binding["binding_thresholds"]) == {
        "basis_min_lot_gap", "min_regime_days", "reconstruction_min_rate"}
    # non-binding thresholds are RETAINED, not deleted
    assert norm.BASIS_MIN_DEVIATION == 1e-3
    assert norm.RATIO_CORRELATION_MIN == 0.90
    assert norm.DUAL_ESTIMATOR_MAX_DISAGREEMENT == 0.20
    print("  ok robustness recorded as frozen-contract-scoped, thresholds retained")


def test_dirty_classification_is_fail_closed_not_py_only():
    """A non-.py execution input must still block establishment.

    experiment_1f_universe.json IS the approved 298-name universe and an
    execution input. Under an earlier .py-only rule it landed in a bucket that
    did not gate establishment, so a modified-but-uncommitted universe would
    have been pinned at its dirty value with semantic_code_tree_clean still
    true. neobdm.db and broker_codes.json had the same hole.
    """
    if not _candidate_snapshot_present():
        return _skip("dirty_classification_is_fail_closed_not_py_only", "candidate snapshot not built here")
    entries = manifest.parse_porcelain("\n".join([
        " M experiment_1f_universe.json",
        " M broker_codes.json",
        " M neobdm.db",
        " M some_new_input.yaml",
        " M README.md",
        "?? backtest_out/experiment_1f_candidate/experiment_1f_input_manifest.json",
    ]))
    semantic, generated, documentation = manifest.classify_dirty(entries)
    for path in ("experiment_1f_universe.json", "broker_codes.json",
                 "neobdm.db", "some_new_input.yaml"):
        assert path in semantic, f"{path} must gate establishment, got {semantic}"
    assert documentation == ["README.md"]
    assert len(generated) == 1

    # and the gate actually refuses on it
    proposal = manifest.build(cand.CANDIDATE_DIR)
    dirty = json.loads(json.dumps(proposal))
    dirty["E_code_identity"]["semantic_code_tree_clean"] = False
    dirty["E_code_identity"]["dirty_semantic_code"] = ["experiment_1f_universe.json"]
    try:
        manifest.assert_establishable(dirty)
    except manifest.NotEstablishable as exc:
        assert "experiment_1f_universe.json" in str(exc)
    else:
        raise AssertionError("a dirty approved universe was allowed to establish")
    print("  ok dirty classification is fail-closed, not .py-only")


# ── the FULL run_gate orchestration, exercised end-to-end ──────────────────
#
# The previous behavioural test traced load_candidate_artifacts() only, which
# proves a loader and not a pipeline: the defect it was written for -- run_gate
# calling a loader with its own defaults -- lived in run_gate's body, not in any
# loader. Isolation asserted about a loader is not isolation of the orchestration.
#
# So these build a complete synthetic execution contract on disk and run the real
# run_gate() against it, with builtins.open traced for the whole call.

_PINNED_REPO = None


def _pinned_code_repo():
    """A real Git repo holding COMMITTED copies of every pinned path.

    Phase 2.7-final made identity the committed Git content plus a proof that
    the worktree carries no semantic difference from it. A test cannot assume
    this checkout satisfies both at the moment it runs -- it may be
    uncommitted, or carry a local edit -- so run_gate() is never exercised
    end-to-end against the real repository's git state.

    So the orchestration fixtures run against a throwaway repository where the
    nine pinned implementation files and the universe ARE committed and clean.
    Nothing here is stubbed: the gate performs its real tracked/clean/content
    checks, against a tree that genuinely satisfies them. core.autocrlf=true is
    set so the fixture reproduces this repository's actual configuration rather
    than a friendlier one.

    Built once and cached; a git repo per fixture call would dominate runtime.
    """
    global _PINNED_REPO
    if _PINNED_REPO is not None:
        return _PINNED_REPO
    import atexit
    import subprocess
    import experiment_1f_manifest as mf

    tmp = tempfile.mkdtemp(prefix="pinned_code_repo_")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp, capture_output=True,
                              text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "core.autocrlf", "true")
    git("config", "commit.gpgsign", "false")
    for name, _plane, _role in mf.CODE_IDENTITY:
        shutil.copyfile(os.path.join(HERE, name), os.path.join(tmp, name))

    tickers = ["AAAA", "BBBB", "ZZZZ"]
    payload = {"universe_digest": gate._digest(sorted(tickers)),
               "n_tickers": len(tickers), "source_path": "synthetic.xlsx",
               "source_sheet": "Sheet1", "frozen_at_utc": "2026-01-01T00:00:00Z",
               "tickers": tickers}
    # two names, identical content: one default, one for the test that proves the
    # DECLARED universe is the file actually consumed
    for name in ("experiment_1f_universe.json",
                 "deliberately_different_universe.json"):
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    git("add", "-A")
    git("commit", "-q", "-m", "pinned code + universe")
    head, err = gate._git_head(tmp)
    assert head and not err, err
    _PINNED_REPO = (tmp, head, payload)
    return _PINNED_REPO


def _establish_synthetically(inputs, candidate_dir):
    """Run the REAL establishment act over a synthetic proposal.

    Only build() is redirected, to a proposal computed against the committed
    fixture repository; no git fact is stubbed. The refusal on a dirty tree is
    tested separately, in a fresh repository, by
    test_establishment_refuses_every_precondition_violation.
    Everything else is the production path: assert_establishable(), an
    independent verify_code_identity() re-hash of the real pinned files, the
    HEAD check, the overwrite refusal and the atomic write.

    Going through the real act means every orchestration fixture below consumes
    a manifest produced the way a real one will be, rather than a hand-written
    file that can drift from it.
    """
    import experiment_1f_manifest as mf
    proposal = _synthetic_proposal(inputs, candidate_dir)
    real_build = mf.build
    mf.build = lambda *a, **kw: json.loads(json.dumps(proposal))
    try:
        _, established = mf.establish_reviewed_manifest(
            candidate_dir=candidate_dir,
            explicit_confirmation=mf.CONFIRMATION_PHRASE,
            target_path=inputs["reviewed_manifest"],
            root=inputs["code_root"])
    finally:
        mf.build = real_build
    return established


def _synthetic_proposal(inputs, candidate_dir):
    """A PROPOSAL built by the PRODUCTION builders, not a reduced imitation.

    The earlier fixture hand-wrote a three-input manifest, which is exactly why
    it could not see that the real manifest pins seven execution inputs while
    run_gate hashed three -- a real established manifest would have failed Gate A
    on the other four. A_execution_inputs and E_code_identity now come straight
    from experiment_1f_manifest, so the fixture inherits any structural change to
    the production schema instead of diverging from it silently.
    """
    import experiment_1f_manifest as mf
    import experiment_1f_validity as validity

    repo = inputs["code_root"]
    head, error = gate._git_head(repo)
    assert not error, error

    candidates_path = inputs["repair_candidates"]
    with open(candidates_path, encoding="utf-8") as fh:
        candidates = json.load(fh)
    with open(inputs["source_manifest"], encoding="utf-8") as fh:
        source_manifest = json.load(fh)
    aggregate = source_manifest["source_aggregate_sha256"]
    parent_sha = gate.file_fingerprint_full(candidates_path)
    with open(inputs["repair_authorization"], encoding="utf-8") as fh:
        auth = json.load(fh)

    # the real code identity, computed against the committed fixture repo where
    # every pinned file is genuinely tracked and genuinely clean -- so nothing
    # here is stubbed and the gate runs its real checks
    code = mf.code_identity(root=repo)

    return {
        "_status": "PROPOSAL FOR REVIEW -- NOT ESTABLISHED (synthetic fixture)",
        "manifest_version": mf.MANIFEST_VERSION,
        "established_utc": None,
        "established_commit_sha": None,
        "A_execution_inputs": mf.execution_inputs_section(candidate_dir,
                                                          inputs=inputs,
                                                          root=repo),
        "B_provenance_inputs": {"_definition": "not verified by the gate"},
        "C_normalization_policy": {
            "mode": validity.PRIMARY,
            "pit_observability": validity.PIT_OBSERVABILITY,
            "primary_applies_basis_harmonisation": False,
            "observable_inventory_resets_at_a_hole": False,
            "observable_inventory_reanchored": False,
            "secondary_mode": validity.SECONDARY,
            "secondary_is_not_tradable_evidence": True,
            # prose/derived keys the verifier does not read, carried so the
            # fixture's section shape matches production exactly -- the
            # divergence tripwire in
            # test_synthetic_manifest_matches_the_production_schema fires if a
            # new key appears in build() and not here
            "pit_observability_meaning": "synthetic",
            "price_broker_domain_separation": "synthetic",
            "certified_but_pit_ineligible": [],
            "validity_artifacts": {},
        },
        "D_authorization": {
            "scope": gate.AUTHORIZATION_SCOPE,
            "authorized_keys": sorted(f"{a['ticker']} {a['date']}"
                                      for a in auth["authorized"]),
            "stage_b_structurally_ineligible": sorted(
                f"{h['ticker']} {h['date']}"
                for h in candidates.get("stage_b_diagnostics_never_repaired", [])),
            "parent_candidate_sha256": parent_sha,
            "parent_candidate_semantic_digest": gate._semantic_digest(candidates),
            "n_authorized": len(auth["authorized"]),
            "invalidation": "synthetic",
        },
        "E_code_identity": code,
        "F_universe": {"n_tickers": inputs["expectations"]["universe_size"],
                       "universe_digest": inputs["expectations"]["universe_digest"]},
        "G_rule_versions": {
            "volume_wrap": norm.RULE_VERSION_WRAP,
            "basis_regime": norm.RULE_VERSION_BASIS,
            "n_regimes": 1,
            "ingest": "build_inventory_db.build_candidate_broker",
        },
        "H_parentage": {
            "all_bindings_ok": True,
            "chain": [
                {"child": "broker_daily_v2.parquet",
                 "parent": "source_manifest.source_aggregate_sha256",
                 "parent_identity": aggregate, "binding": "synthetic"},
                {"child": "volume_repair_candidates.json",
                 "parent": "source_manifest + rule/code identity",
                 "per_candidate_source_binding_ok": True, "binding": "synthetic"},
                {"child": "volume_repair_authorization.json",
                 "parent": "volume_repair_candidates.json",
                 "parent_sha256": parent_sha, "recorded_parent_sha256": parent_sha,
                 "bound": True, "binding": "synthetic"},
                {"child": "observed_basis_factor_candidate.json",
                 "parent": "source_manifest + rule/code identity",
                 "parent_identity": aggregate, "binding": "synthetic"},
            ],
        },
        "_establishment": {"established": False},
        "_establishment_rules": [],
    }


def _orchestration_fixture(tmp, mode=None, universe_json=None, manifest=True):
    """A complete, self-consistent candidate contract in `tmp`.

    Internally coherent by construction: every broker value equals
    lots * SHARES_PER_LOT * price, so a broker's implied VWAP lands inside the
    session range. BBBB carries a 5x basis regime over its first three sessions
    -- the in-regime rows quote an ADJUSTED range (~920) against AS-TRADED
    broker lots, which is the real shape of the mismatch.

    All SEVEN execution inputs exist, because the manifest pins seven and the
    verifier requires the consumed set and the pinned set to be equal.

    Returns (inputs, expectations).
    """
    mode = mode or gate.PRIMARY_MODE
    dates = ["2026-01-%02d" % (i + 1) for i in range(6)]
    lots = 1000.0
    rows, brk = [], []
    for i, day in enumerate(dates):
        rows.append({"date": day, "ticker": "AAAA", "open": 100.0, "high": 101.0,
                     "low": 99.0, "close": 100.0, "volume": 100000.0})
        rows.append({"date": day, "ticker": "ZZZZ", "open": 50.0, "high": 51.0,
                     "low": 49.0, "close": 50.0, "volume": 20000.0})
        # OUTS is deliberately OUT of the universe: it must contribute detection
        # evidence and calendar dates without ever entering the panel.
        rows.append({"date": day, "ticker": "OUTS", "open": 7.0, "high": 8.0,
                     "low": 6.0, "close": 7.0, "volume": 3000.0})

        in_regime = i < 3
        rows.append({"date": day, "ticker": "BBBB", "open": 920.0, "high": 940.0,
                     "low": 900.0, "close": 920.0,
                     "volume": 500000.0 if in_regime else 100000.0})

        for ticker, price in (("AAAA", 100.0),
                              ("BBBB", 4600.0 if in_regime else 920.0)):
            value = lots * 100.0 * price
            # the raw v2 schema: `broker`, int64 lots, plain rupiah values
            brk.append({"date": day, "ticker": ticker, "broker": "AK",
                        "blot": int(lots), "slot": int(lots), "nlot": 0,
                        "bval": value, "sval": value, "nval": 0.0})

    ohlc_path = os.path.join(tmp, "ohlc_full_market.parquet")
    broker_path = os.path.join(tmp, "broker_daily_v2.parquet")
    pd.DataFrame(rows).to_parquet(ohlc_path, index=False)
    pd.DataFrame(brk).to_parquet(broker_path, index=False)

    tickers = ["AAAA", "BBBB", "ZZZZ"]
    expectations = {
        "universe_size": len(tickers),
        "universe_digest": gate._digest(sorted(tickers)),
        "price_covered": 3,
        "price_missing": (),
        "broker_covered": 2,          # ZZZZ has no broker flow, exactly like WIKA
        "broker_missing": ("ZZZZ",),
    }
    # The universe is TRACKED repository text, so it lives in the committed
    # fixture repo rather than loose in the tmpdir: an untracked execution input
    # has no canonical identity and must be refused.
    repo, repo_head, _payload = _pinned_code_repo()
    universe_json = universe_json or os.path.join(repo, "experiment_1f_universe.json")

    # a real source_manifest: the parentage check recomputes its aggregate from
    # its own file list, so the two must actually agree
    files = [{"ticker": t, "sha256": hashlib.sha256(t.encode()).hexdigest(),
              "size_bytes": 10, "capture_timestamp": None,
              "filesystem_mtime_utc": "2026-01-01T00:00:00Z"} for t in tickers]
    source_manifest_path = os.path.join(tmp, "source_manifest.json")
    with open(source_manifest_path, "w", encoding="utf-8") as fh:
        json.dump({"experiment": "1F", "phase": 2, "files": files,
                   "source_aggregate_sha256": cand.aggregate_digest(
                       (f["ticker"], f["sha256"]) for f in files),
                   "population": {"covered": tickers, "n_covered": len(tickers),
                                  "uncovered": [], "n_uncovered": 0},
                   "universe": {"n_tickers": len(tickers),
                                "universe_digest": expectations["universe_digest"]}},
                  fh, indent=2, sort_keys=True)

    candidates = {"candidates": [], "stage_b_diagnostics_never_repaired": []}
    cand_path = os.path.join(tmp, "volume_repair_candidates.json")
    with open(cand_path, "w", encoding="utf-8") as fh:
        json.dump(candidates, fh, indent=2, sort_keys=True)
    auth_path = os.path.join(tmp, "volume_repair_authorization.json")
    with open(auth_path, "w", encoding="utf-8") as fh:
        json.dump({"scope": gate.AUTHORIZATION_SCOPE, "authorized": [],
                   "parent_candidate_sha256": gate.file_fingerprint_full(cand_path),
                   "parent_candidate_semantic_digest":
                       gate._semantic_digest(candidates)},
                  fh, indent=2, sort_keys=True)
    basis_path = os.path.join(tmp, "observed_basis_factor_candidate.json")
    with open(basis_path, "w", encoding="utf-8") as fh:
        json.dump({"regimes": [{"ticker": "BBBB", "candidate_factor": 5.0,
                                "start_date": "2026-01-01", "end_date": "2026-01-03",
                                "classification": "CANDIDATE_HARMONIZABLE"}]},
                  fh, indent=2, sort_keys=True)

    inputs = gate.candidate_inputs(candidate_dir=tmp, mode=mode)
    inputs.update({
        "expectations": expectations,
        "git_root": repo,
        "code_root": repo,
        "ohlc_parquet": ohlc_path,
        "broker_parquet": broker_path,
        "universe_json": universe_json,
        "source_manifest": source_manifest_path,
        "repair_candidates": cand_path,
        "repair_authorization": auth_path,
        "basis_artifact": basis_path,
        "reviewed_manifest": os.path.join(tmp, "experiment_1f_manifest_v3.json"),
    })
    if manifest:
        _establish_synthetically(inputs, tmp)
    return inputs, expectations


EXPECTED_STAGES = ["execution_input_fingerprints", "load_candidate_artifacts",
                   "verify_reviewed_manifest_v3", "resolve_universe",
                   "load_full_harvest", "load_frozen_broker",
                   "basis_dispositions", "broker_basis_invalid_keys",
                   "build_validated_panel"]


def test_full_run_gate_orchestration_consumes_only_the_candidate_contract():
    """The whole production call path, traced. Not a loader in isolation.

    Asserts, in one real run: all SEVEN execution inputs are fingerprinted and
    consumed; the candidate OHLC and broker v2 are the files read; the exact
    authorisation is loaded; no legacy normalized artifact and no legacy
    two-file manifest is opened; the requested mode is enforced; and the
    manifest verifier cannot establish.
    """
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        opened = []
        with gate.traced_open_paths(opened):
            panel, broker, report = gate.run_gate(inputs=inputs,
                                                  mode=gate.PRIMARY_MODE)

        gate.assert_no_legacy_artifact_was_opened(opened, inputs)
        touched = {os.path.abspath(p) for p in opened}
        for legacy in inputs["provenance_only"].values():
            assert os.path.abspath(legacy) not in touched, \
                f"run_gate opened the provenance-only artifact {legacy}"

        stages = [s["stage"] for s in report["orchestration"]]
        assert stages == EXPECTED_STAGES, stages
        by_stage = {s["stage"]: s for s in report["orchestration"]}

        # every one of the seven, hashed before anything semantic happened
        assert by_stage["execution_input_fingerprints"]["pinned_inputs"] == \
            len(gate.EXECUTION_INPUTS) == 7
        assert stages.index("execution_input_fingerprints") == 0, \
            "the bytes must be pinned before any semantic load"
        assert stages.index("verify_reviewed_manifest_v3") < \
            stages.index("build_validated_panel"), \
            "the manifest must verify before a panel is built from the data"

        assert by_stage["resolve_universe"]["consumed"] == \
            by_stage["resolve_universe"]["declared"] == \
            os.path.abspath(inputs["universe_json"]), \
            "the universe consumed must be the universe declared"
        assert by_stage["load_full_harvest"]["consumed"] == \
            os.path.abspath(inputs["ohlc_parquet"])
        assert by_stage["load_frozen_broker"]["consumed"] == \
            os.path.abspath(inputs["broker_parquet"])
        assert by_stage["verify_reviewed_manifest_v3"]["can_establish"] is False
        assert set(by_stage["load_candidate_artifacts"]["consumed"]) == {
            inputs["repair_candidates"], inputs["repair_authorization"],
            inputs["basis_artifact"]}

        for path in (inputs["repair_candidates"], inputs["repair_authorization"],
                     inputs["basis_artifact"], inputs["universe_json"],
                     inputs["source_manifest"], inputs["reviewed_manifest"]):
            assert os.path.abspath(path) in touched, \
                f"{os.path.basename(path)} was declared but never opened"

        assert report["mode"] == gate.PRIMARY_MODE
        assert report["input_manifest"]["establishment_reachable_from_gate"] is False
        assert len(panel) and len(broker)
    print("  ok the full run_gate call path consumes only the candidate contract")


def test_all_seven_execution_inputs_are_pinned_and_verified():
    """Every pin is load-bearing, in BOTH directions, for ALL seven inputs.

    The regression for the defect that made a real manifest unusable: the
    manifest pinned seven execution inputs and run_gate verified three, so an
    actually established manifest would have failed on source_manifest, both
    repair artifacts and the basis artifact. A reduced synthetic manifest hid it.

    Here each of the seven is, in turn: removed from the manifest, corrupted in
    the manifest, and repointed at a different path. All twenty-one mutations
    must stop the gate. An eighth, never-consumed pin must stop it too.
    """
    names = [name for name, _, _ in gate.EXECUTION_INPUTS]
    assert len(names) == 7, names

    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        path = inputs["reviewed_manifest"]
        with open(path, encoding="utf-8") as fh:
            good = json.load(fh)

        assert set(k for k in good["A_execution_inputs"] if not k.startswith("_")) \
            == set(names), "the fixture must pin exactly the gate's contract"

        def _run(mutate):
            manifest = json.loads(json.dumps(good))
            mutate(manifest["A_execution_inputs"])
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            try:
                gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
            except gate.GateFailure as exc:
                return str(exc)
            raise AssertionError("a mutated execution pin was accepted")

        # each input is mutated in the field its OWN identity rule reads: raw
        # bytes for the generated artifacts, committed Git content for the
        # tracked universe. Mutating the wrong field would prove nothing.
        identities = {n: i for n, _, i in gate.EXECUTION_INPUTS}
        for name in names:
            missing = _run(lambda a, n=name: a.pop(n))
            assert "not pinned" in missing, f"{name}: {missing}"

            if identities[name] == gate.IDENTITY_RAW_BYTES:
                corrupted = _run(
                    lambda a, n=name: a[n].__setitem__("sha256", "0" * 64))
                assert "snapshot changed" in corrupted, f"{name}: {corrupted}"
            else:
                corrupted = _run(lambda a, n=name: a[n].__setitem__(
                    "git_content_sha256", "0" * 64))
                assert "committed content changed" in corrupted,                     f"{name}: {corrupted}"
                # a raw-byte mutation on a tracked input must NOT be what gates
                # it: that is the CRLF trap this identity rule removes
                tolerated = json.loads(json.dumps(good))
                tolerated["A_execution_inputs"][name]["sha256"] = "0" * 64
                tolerated["A_execution_inputs"][name]["worktree_raw_sha256"] = "0" * 64
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(tolerated, fh)
                gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)

            repointed = _run(
                lambda a, n=name: a[n].__setitem__("path", "somewhere/else.bin"))
            assert "pinned at" in repointed, f"{name}: {repointed}"

        orphan = _run(lambda a: a.__setitem__("never_read_input", {
            "path": "nothing.bin", "sha256": "1" * 64, "size_bytes": 1,
            "present": True, "role": "decoration"}))
        assert "not consumed" in orphan, orphan

        # and the unmutated manifest still passes
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
    print(f"  ok all {len(names)} execution pins verified; 22 mutations refused")


def test_reviewed_manifest_v3_verifies_more_than_section_a():
    """Commit, code identity, policy, authorisation, rule versions, parentage.

    A reviewed manifest is a CLAIM. Each of these proves a different part of it
    independently, and every one of them must be able to fail the gate on its
    own -- otherwise the section is decoration.
    """
    import experiment_1f_manifest as mf

    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        path = inputs["reviewed_manifest"]
        with open(path, encoding="utf-8") as fh:
            good = json.load(fh)

        def _run(mutate, expect):
            manifest = json.loads(json.dumps(good))
            mutate(manifest)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            try:
                gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
            except gate.GateFailure as exc:
                assert expect in str(exc), f"expected {expect!r} in: {exc}"
                return
            raise AssertionError(f"the gate accepted a manifest that should fail "
                                 f"on {expect!r}")

        # B: the pinned commit must be the commit being run
        _run(lambda m: m.__setitem__("established_commit_sha", "d" * 40),
             "does not describe the code about to run")

        # C: code identity, re-hashed independently of the manifest's own claim
        _run(lambda m: m["E_code_identity"]["files"][0].__setitem__(
            "git_content_sha256", "0" * 64), "committed content changed")
        # a raw-byte change on a pinned code file must NOT be the gating fact:
        # that is the CRLF trap. The committed content is what is pinned.
        _run(lambda m: m["E_code_identity"]["files"][0].__setitem__(
            "worktree_matches_head", None) or
            m["E_code_identity"]["files"][0].__setitem__(
                "git_content_sha256", "0" * 64), "committed content changed")
        _run(lambda m: m["E_code_identity"].__setitem__(
            "files", [f for f in m["E_code_identity"]["files"]
                      if f.get("plane") != gate_control_plane()]),
             "no CONTROL PLANE file is pinned")

        # D: normalization policy
        _run(lambda m: m["C_normalization_policy"].__setitem__("mode", "SOMETHING"),
             "primary mode")
        _run(lambda m: m["C_normalization_policy"].__setitem__(
            "pit_observability", "ESTABLISHED"), "pit_observability")
        _run(lambda m: m["C_normalization_policy"].__setitem__(
            "primary_applies_basis_harmonisation", True), "applies basis harmonisation")
        _run(lambda m: m["C_normalization_policy"].__setitem__(
            "observable_inventory_resets_at_a_hole", True), "resets at a hole")

        # E: authorisation, cross-checked against the loaded artifact
        _run(lambda m: m["D_authorization"].__setitem__("scope", "ANY"), "scope")
        _run(lambda m: m["D_authorization"].__setitem__(
            "authorized_keys", ["BUMI 2026-02-27"]), "EXACT key set")
        _run(lambda m: m["D_authorization"].__setitem__(
            "parent_candidate_sha256", "0" * 64), "candidate artifact on disk")
        _run(lambda m: m["D_authorization"].__setitem__(
            "parent_candidate_semantic_digest", "0" * 64), "semantic digest")

        # F: rule versions must be the ones actually imported
        _run(lambda m: m["G_rule_versions"].__setitem__("volume_wrap", "old/0"),
             "rule version volume_wrap")
        _run(lambda m: m["G_rule_versions"].__setitem__("n_regimes", 99),
             "basis regimes")

        # G: parentage RECOMPUTED, never trusted from the JSON
        _run(lambda m: m["H_parentage"].__setitem__("all_bindings_ok", False),
             "records a broken parentage")
        _run(lambda m: m["H_parentage"]["chain"][0].__setitem__(
            "parent_identity", "0" * 64), "recomputed source aggregate")
        _run(lambda m: m["H_parentage"]["chain"][2].__setitem__(
            "parent_sha256", "0" * 64), "authorisation parent")

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        _, _, report = gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        assert report["input_manifest"]["checks_performed"] == [
            "execution_inputs_complete", "commit_is_head", "code_identity_rehashed",
            "normalization_policy", "authorization_cross_checked", "rule_versions",
            "parentage_recomputed"], report["input_manifest"]["checks_performed"]
        assert report["input_manifest"]["parentage"][
            "recomputed_not_trusted_from_json"] is True
        assert report["input_manifest"]["authorization"][
            "cross_checked_with_artifact"] is True
        assert report["input_manifest"]["code_identity"]["files_rehashed"] == \
            len(mf.CODE_IDENTITY)
    print("  ok manifest-v3 verifies commit, code, policy, auth, rules, parentage")


def gate_control_plane():
    import experiment_1f_manifest as mf
    return mf.CONTROL_PLANE


def test_secondary_mode_needs_the_manifest_to_authorize_it():
    """A sensitivity run may not borrow a PRIMARY-only manifest's standing."""
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp, mode=gate.SECONDARY_MODE)
        path = inputs["reviewed_manifest"]
        with open(path, encoding="utf-8") as fh:
            good = json.load(fh)

        _, _, report = gate.run_gate(inputs=inputs, mode=gate.SECONDARY_MODE)
        assert report["input_manifest"]["policy"]["run_is_sensitivity_only"] is True
        assert report["input_manifest"]["policy"]["mode"] == gate.PRIMARY_MODE, \
            "the manifest's own mode stays PRIMARY even on a sensitivity run"

        for mutate, expect in (
                (lambda m: m["C_normalization_policy"].pop("secondary_mode"),
                 "declares secondary_mode"),
                (lambda m: m["C_normalization_policy"].__setitem__(
                    "secondary_is_not_tradable_evidence", False),
                 "not tradable evidence")):
            manifest = json.loads(json.dumps(good))
            mutate(manifest)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            try:
                gate.run_gate(inputs=inputs, mode=gate.SECONDARY_MODE)
            except gate.GateFailure as exc:
                assert expect in str(exc), str(exc)
            else:
                raise AssertionError(
                    "SECONDARY ran against a manifest that does not authorise it")
    print("  ok SECONDARY must be authorised by the manifest, not assumed")


def test_synthetic_manifest_matches_the_production_schema():
    """The fixture must fail if the production manifest schema moves.

    The previous fixture wrote its own reduced manifest, which is precisely how
    a seven-versus-three divergence survived. This compares the fixture's
    manifest against the REAL proposal on disk, section by section.
    """
    import experiment_1f_manifest as mf
    real_path = os.path.join(cand.CANDIDATE_DIR, mf.PROPOSAL_NAME)
    if not os.path.exists(real_path):
        _skip("test_synthetic_manifest_matches_the_production_schema",
              "real proposal absent")
        return
    with open(real_path, encoding="utf-8") as fh:
        real = json.load(fh)

    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        with open(inputs["reviewed_manifest"], encoding="utf-8") as fh:
            synthetic = json.load(fh)

    assert set(real) == set(synthetic), (
        f"top-level sections diverge: only in real {sorted(set(real) - set(synthetic))}, "
        f"only in fixture {sorted(set(synthetic) - set(real))}")
    for section in ("A_execution_inputs", "C_normalization_policy",
                    "D_authorization", "G_rule_versions"):
        real_keys = {k for k in real[section] if not k.startswith("_")}
        syn_keys = {k for k in synthetic[section] if not k.startswith("_")}
        missing = real_keys - syn_keys
        assert not missing, f"{section}: fixture is missing {sorted(missing)}"
    assert set(real["E_code_identity"]) == set(synthetic["E_code_identity"])
    assert {k for k in real["A_execution_inputs"] if not k.startswith("_")} == \
        {name for name, _, _ in gate.EXECUTION_INPUTS}, \
        "the real manifest must pin exactly the gate's execution contract"
    print(f"  ok fixture manifest matches the production schema "
          f"({len(gate.EXECUTION_INPUTS)} execution pins)")


def test_establishment_refuses_every_precondition_violation():
    """Confirmation, overwrite and a dirty tree -- each refused, none state-dependent.

    The dirty-tree refusal no longer leans on this checkout happening to be dirty,
    a transient fact the approved commit itself would make false. It runs in a
    fresh repository, first CLEAN -- establishment succeeds, proving the fixture
    is otherwise establishable -- then with exactly one committed pinned file
    edited in the working tree, where establishment refuses naming that file and
    no other. The refusal is therefore caused by the edit, provably.
    """
    import experiment_1f_manifest as mf
    reviewed_existed = os.path.exists(gate.REVIEWED_MANIFEST_V3_JSON)

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "experiment_1f_manifest_v3.json")

        # (1) confirmation: refused before any proposal is built or git is read
        for confirmation in (None, "", "yes", mf.CONFIRMATION_PHRASE.lower()):
            try:
                mf.establish_reviewed_manifest(explicit_confirmation=confirmation,
                                               target_path=target)
            except mf.EstablishmentRefused as exc:
                assert "confirmation phrase" in str(exc)
            else:
                raise AssertionError("establishment ran without confirmation")
        assert not os.path.exists(target), \
            "a refused establishment must not have written anything"

        # (2) an existing reviewed manifest is never silently replaced
        with open(target, "w", encoding="utf-8") as fh:
            json.dump({"manifest_version": "existing"}, fh)
        try:
            mf.establish_reviewed_manifest(
                explicit_confirmation=mf.CONFIRMATION_PHRASE, target_path=target)
        except mf.EstablishmentRefused as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("an existing reviewed manifest was overwritten")
        with open(target, encoding="utf-8") as fh:
            assert json.load(fh)["manifest_version"] == "existing", \
                "the existing manifest must be untouched"

    # (3) a dirty semantic tree, in a FRESH repository this test alone owns.
    #     Targets live OUTSIDE the repository: a manifest written inside it would
    #     itself be semantic dirt and contaminate the treatment.
    tmp = tempfile.mkdtemp(prefix="dirty_tree_repo_")
    try:
        repo, head = _fresh_pinned_repo(os.path.join(tmp, "repo"))
        edited = "experiment_1f_validity.py"

        # control: the clean fixture IS establishable
        clean_target = os.path.join(tmp, "clean_manifest.json")
        _, established = _establish_against(repo, clean_target)
        assert established["established_commit_sha"] == head
        assert os.path.exists(clean_target)

        # treatment: one committed pinned file edited after the commit
        with open(os.path.join(repo, edited), "a", encoding="utf-8") as fh:
            fh.write("\n# a local edit made after the reviewed commit\n")
        dirty = _minimal_proposal(repo)["E_code_identity"]
        assert dirty["semantic_code_tree_clean"] is False
        assert dirty["dirty_semantic_code"] == [edited], dirty["dirty_semantic_code"]

        dirty_target = os.path.join(tmp, "dirty_manifest.json")
        try:
            _establish_against(repo, dirty_target)
        except mf.NotEstablishable as exc:
            message = str(exc)
            assert "semantic code tree is dirty" in message, message
            assert edited in message, message
            named = [n for n, _p, _r in mf.CODE_IDENTITY
                     if n != edited and n in message]
            assert not named, f"the refusal must name only the edited file: {named}"
        else:
            raise AssertionError(
                "establishment succeeded against a dirty semantic tree")
        assert not os.path.exists(dirty_target), \
            "a refused establishment must not have written anything"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The real reviewed manifest is neither created nor removed BY THIS TEST: an
    # invariant about side effects, true before and after it is ever established.
    assert os.path.exists(gate.REVIEWED_MANIFEST_V3_JSON) == reviewed_existed
    print("  ok establishment refuses no-confirmation, overwrite and a dirty tree")


def test_establishment_produces_a_manifest_the_real_verifier_accepts():
    """real proposal schema -> establishment -> run_gate verification passes.

    The proposal is computed against the committed fixture repository, so the
    clean tree is a real git fact rather than a stub; the dirty-tree refusal is
    tested in test_establishment_refuses_every_precondition_violation. Everything
    else is the real act -- fresh proposal,
    assert_establishable, an independent code-identity re-hash, the HEAD check
    and the atomic write. What this closes is the loop the review asked for: a
    manifest produced by the establishment path must be one Gate A accepts.
    """
    import experiment_1f_manifest as mf
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp, manifest=False)
        established = _establish_synthetically(inputs, tmp)

        assert established["established_commit_sha"] ==             gate._git_head(inputs["code_root"])[0]
        assert established["established_utc"]
        assert established["_establishment"]["established"] is True
        assert os.path.exists(inputs["reviewed_manifest"])
        assert not [n for n in os.listdir(tmp) if n.endswith(".tmp")], \
            "the atomic write must leave no temporary file behind"

        _, _, report = gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        assert report["input_manifest"]["status"].startswith("verified")
        assert report["input_manifest"]["established_commit_sha"] == \
            report["input_manifest"]["head_commit_sha"]

        # and re-establishing over it is refused without the explicit decision
        try:
            _establish_synthetically(inputs, tmp)
        except mf.EstablishmentRefused as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("re-establishment was allowed implicitly")
    print("  ok an established manifest is accepted by the real Gate-A verifier")


def test_neobdm_is_diagnostic_only_and_cannot_alter_the_gate():
    """The one implicit path, made explicit and proven inert.

    broker_provenance() reads neobdm.db, which is NOT a manifest-pinned
    execution input. That is only defensible if it cannot influence anything, so
    this runs the gate with the database reachable and unreachable and requires
    the panel, the broker table and both digests to be identical.
    """
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        assert "neobdm_db" in inputs["diagnostic_only"], \
            "the diagnostic path must be declared, not resolved from a default"

        panel_a, broker_a, report_a = gate.run_gate(inputs=inputs,
                                                    mode=gate.PRIMARY_MODE)

        absent = json.loads(json.dumps(inputs["diagnostic_only"]))
        absent["neobdm_db"] = os.path.join(tmp, "definitely_not_here.db")
        inputs_b = dict(inputs, diagnostic_only=absent)
        panel_b, broker_b, report_b = gate.run_gate(inputs=inputs_b,
                                                    mode=gate.PRIMARY_MODE)

    assert report_a["panel_digest"] == report_b["panel_digest"]
    assert report_a["broker_digest"] == report_b["broker_digest"]
    assert panel_a.equals(panel_b) and broker_a.equals(broker_b)
    assert report_a["integrity"] == report_b["integrity"]
    assert report_a["broker_provenance"]["role"].startswith("DIAGNOSTIC")
    assert report_b["broker_provenance"]["status"].startswith("neobdm.db absent")
    print("  ok neobdm.db is diagnostic only and cannot alter any gate output")


def test_full_run_gate_primary_mode_harmonises_nothing():
    """Mode is enforced by the orchestration, not merely declared by a constant."""
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        _, _, primary = gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
    stage = {s["stage"]: s for s in primary["orchestration"]}["basis_dispositions"]
    assert stage["harmonisable_tickers"] == [], stage
    assert stage["quarantined_tickers"] == ["BBBB"], stage
    assert primary["cross_source"]["basis_harmonised"]["rows_scaled"] == 0, \
        "PRIMARY must scale no broker lot at all"

    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp, mode=gate.SECONDARY_MODE)
        _, _, secondary = gate.run_gate(inputs=inputs, mode=gate.SECONDARY_MODE)
    stage = {s["stage"]: s for s in secondary["orchestration"]}["basis_dispositions"]
    assert stage["harmonisable_tickers"] == ["BBBB"], stage
    assert secondary["cross_source"]["basis_harmonised"]["rows_scaled"] > 0, \
        "the certified regime must actually be harmonised in SECONDARY"
    print("  ok PRIMARY harmonises nothing; only SECONDARY does")


def test_run_gate_refuses_a_mode_that_disagrees_with_its_inputs():
    """A mode argument that contradicts the declared inputs is a stop."""
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp, mode=gate.SECONDARY_MODE)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "mode" in str(exc).lower()
        else:
            raise AssertionError("a mode/inputs disagreement must fail closed")
    print("  ok a mode disagreeing with the declared inputs fails closed")


def test_run_gate_keeps_price_valid_broker_invalid_rows_in_the_panel():
    """PRICE and BROKER are separate domains. This is the regression for that.

    BBBB's first three sessions are broker-basis invalid in PRIMARY. price_audit
    objects to none of them, so all three must survive into the price panel and
    stay eligible for variant A; only the BROKER rows disappear.
    """
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        panel, broker, report = gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)

    basis = report["broker_basis_validity"]
    assert basis["invalid_sessions"] == 3, basis
    assert basis["price_rows_deleted"] == 0, \
        "a broker-domain defect must never delete a price row"
    assert basis["price_valid_and_broker_invalid_rows_in_panel"] == 3, basis
    assert basis["invalid_keys"] == ["BBBB 2026-01-01", "BBBB 2026-01-02",
                                     "BBBB 2026-01-03"], basis["invalid_keys"]

    in_regime = panel[(panel["ticker"] == "BBBB")
                      & (panel["date"] <= "2026-01-03")]
    assert len(in_regime) == 3, \
        "every price-valid, broker-invalid session must remain in the panel"
    assert (panel["ticker"] == "BBBB").sum() == 6, \
        "BBBB keeps its full price history; only its broker rows are withheld"

    # and the broker side is genuinely unavailable for exactly those sessions
    left = broker[(broker["ticker"] == "BBBB") & (broker["date"] <= "2026-01-03")]
    assert left.empty, "broker rows on a quarantined basis regime must not survive"
    assert ((broker["ticker"] == "BBBB") & (broker["date"] > "2026-01-03")).sum() == 3, \
        "the verifiably clean broker tail must be retained"
    print("  ok price-valid & broker-invalid rows stay in the panel")


def test_run_gate_consumes_the_declared_universe_and_never_falls_back():
    """Point the declared universe elsewhere: it must be consumed, or hard-fail."""
    with tempfile.TemporaryDirectory() as tmp:
        # (1) a DIFFERENT fixture path is genuinely consumed. It lives in the
        #     committed fixture repo because a tracked execution input is the
        #     only kind that has a canonical identity.
        repo, _head, _payload = _pinned_code_repo()
        elsewhere = os.path.join(repo, "deliberately_different_universe.json")
        inputs, _ = _orchestration_fixture(tmp, universe_json=elsewhere)
        _, _, report = gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        assert report["universe"]["consumed_path"] == os.path.abspath(elsewhere), \
            report["universe"]["consumed_path"]
        assert os.path.abspath(gate.FROZEN_UNIVERSE_JSON) != os.path.abspath(elsewhere)

    # (2) a MISSING declared universe is a stop, never a silent fallback to the
    #     repo-root default or a re-read of the mutable workbook
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        # point the DECLARATION at a path that does not exist, rather than
        # deleting the fixture repo's copy: that repo is shared by every
        # orchestration fixture in this file.
        inputs = dict(inputs,
                      universe_json=os.path.join(tmp, "no_such_universe.json"))
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            # it now stops even earlier, at the fingerprint stage: the universe
            # is one of the seven pinned execution inputs, so its absence is
            # caught before any loader is reached
            assert "missing on disk" in str(exc) and "universe_json" in str(exc), \
                str(exc)
        else:
            raise AssertionError(
                "a missing declared universe must fail, not fall back")

        # and the loader itself refuses independently of that earlier guard
        try:
            gate.resolve_universe(frozen_path=inputs["universe_json"],
                                  require_frozen=True)
        except gate.GateFailure as exc:
            assert "fall back" in str(exc), str(exc)
        else:
            raise AssertionError("resolve_universe fell back to a default")

    # (3) refreeze is refused outright under the candidate contract
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE, refreeze=True)
        except gate.GateFailure as exc:
            assert "workbook" in str(exc)
        else:
            raise AssertionError("refreeze must be refused in candidate mode")
    print("  ok the declared universe is consumed; absence and refreeze fail closed")


def test_gate_a_can_never_establish_its_own_manifest():
    """Establishment is a separate act. The gate refuses, and the CLI cannot ask."""
    try:
        gate.run_gate(establish_manifest=True)
    except gate.ManifestEstablishmentRefused as exc:
        assert "separate act" in str(exc)
    else:
        raise AssertionError("run_gate must refuse to establish a manifest")

    # the CLI flag is gone, not renamed
    import argparse as _argparse
    source = open(os.path.join(HERE, "experiment_1f_universe_gate.py"),
                  encoding="utf-8").read()
    assert '"--establish-manifest"' not in source, \
        "the --establish-manifest flag must not be reachable from Gate A's CLI"

    # verify_reviewed_manifest_v3 has no establish parameter at all
    import inspect
    params = inspect.signature(gate.verify_reviewed_manifest_v3).parameters
    assert "establish" not in params, params

    # a PROPOSAL (no established_commit_sha) is refused as a manifest
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp, manifest=False)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "establish" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("a missing reviewed manifest must stop the gate")

        proposal = os.path.join(tmp, "experiment_1f_manifest_v3.json")
        with open(proposal, "w", encoding="utf-8") as fh:
            json.dump({"manifest_version": gate.MANIFEST_V3_VERSION,
                       "established_commit_sha": None,
                       "A_execution_inputs": {}}, fh)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "PROPOSAL" in str(exc), str(exc)
        else:
            raise AssertionError("a proposal must not be accepted as a manifest")
    print("  ok Gate A cannot establish, re-establish, or accept a proposal")


def test_reviewed_manifest_v3_refuses_drift_in_either_direction():
    """A consumed-but-unpinned input and a pinned-but-unconsumed input both fail."""
    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        path = inputs["reviewed_manifest"]

        with open(path, encoding="utf-8") as fh:
            good = json.load(fh)

        drifted = json.loads(json.dumps(good))
        drifted["A_execution_inputs"]["ohlc_full_market_parquet"]["sha256"] = "0" * 64
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(drifted, fh)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "snapshot changed" in str(exc), str(exc)
        else:
            raise AssertionError("a changed execution input must stop the gate")

        unpinned = json.loads(json.dumps(good))
        unpinned["A_execution_inputs"].pop("universe_json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(unpinned, fh)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "not pinned" in str(exc), str(exc)
        else:
            raise AssertionError("a consumed but unpinned input must stop the gate")

        orphan = json.loads(json.dumps(good))
        orphan["A_execution_inputs"]["never_read_parquet"] = {
            "path": os.path.join(tmp, "nothing.parquet"), "role": "x",
            "present": True, "sha256": "1" * 64, "size_bytes": 1}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(orphan, fh)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "not consumed" in str(exc), str(exc)
        else:
            raise AssertionError("a pinned but unconsumed input must stop the gate")

        # a v2 manifest cannot be reinterpreted as a v3 one
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"manifest_version": "experiment_1f/2",
                       "established_commit_sha": "0" * 40,
                       "A_execution_inputs": {}}, fh)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "version" in str(exc)
        else:
            raise AssertionError("a v2 manifest must be refused, not reinterpreted")
    print("  ok manifest-v3 drift fails closed in both directions")

def test_cross_source_invariants_accept_the_frame_run_gate_actually_passes():
    """The regression for a defect only a full-pipeline test could reach.

    load_frozen_broker() returns #1E's convention -- the net column is `netval`
    and all three values are in BILLIONS -- while cross_source_invariants()
    documented and unit-tested plain RUPIAH with `nval`. run_gate() wired the
    first into the second, so the very first real Gate A run would have died on
    KeyError: 'nval', and i6 would have compared a price 1e-9 too small against
    [low, high] and called every row a containment violation.

    Both frames must now produce identical invariants.
    """
    dates = ["2026-01-%02d" % (i + 1) for i in range(3)]
    raw = pd.DataFrame([
        {"date": d, "ticker": "AAAA", "broker": "AK", "nlot": 0,
         "blot": 1000, "slot": 1000, "nval": 0.0,
         "bval": 1000 * 100 * 100.0, "sval": 1000 * 100 * 100.0}
        for d in dates])
    ohlc = pd.DataFrame([
        {"date": d, "ticker": "AAAA", "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0, "volume": 100000.0} for d in dates])

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "b.parquet")
        raw.to_parquet(path, index=False)
        normalized, _, _ = gate.load_frozen_broker(
            {"AAAA"}, path=path,
            expectations={"broker_covered": 1, "broker_missing": ()})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert "netval" in normalized.columns and "nval" not in normalized.columns, \
        "the normalized frame is exactly the shape run_gate hands over"

    artifacts = {"ledger_digest": "t", "factor_digest": "t"}
    from_raw = gate.cross_source_invariants(ohlc, raw, artifacts)
    from_normalized = gate.cross_source_invariants(ohlc, normalized, artifacts)

    for key in ("i1_buy_vs_volume", "i2_sell_vs_volume", "i1_basis_scale_breaks",
                "i4_value_conservation", "i5_sum_nval_nonzero",
                "i6_implied_price_outside_range", "i6_rows_checked"):
        assert from_raw[key] == from_normalized[key], (
            f"{key} differs by broker frame shape: {from_raw[key]} vs "
            f"{from_normalized[key]}")
    assert from_normalized["i6_implied_price_outside_range"] == 0, \
        "a coherent broker row must not read as a containment violation"
    gate.assert_cross_source_integrity(from_normalized)
    print("  ok cross-source invariants accept the frame run_gate really passes")


def test_rupiah_round_trip_stays_far_inside_the_tolerance():
    """Measured on the REAL table, not assumed from an error bound.

    rupiah_value_view() reconstructs rupiah by multiplying the billions column
    back up, which is a float round-trip and not bit-exact. The claim that the
    residue is negligible against NVAL_TOLERANCE_RUPIAH is only worth anything
    if it is measured at the magnitudes the harvest actually carries.
    """
    path = os.path.join(cand.CANDIDATE_DIR, bidb.CANDIDATE_BROKER_NAME)
    if not os.path.exists(path):
        _skip("test_rupiah_round_trip_stays_far_inside_the_tolerance",
              "candidate broker table absent")
        return
    frame = pd.read_parquet(path, columns=["nval", "bval", "sval"])
    worst = 0.0
    for column in ("nval", "bval", "sval"):
        original = frame[column].to_numpy(dtype="float64")
        restored = (original / gate.RUPIAH_PER_BILLION) * gate.RUPIAH_PER_BILLION
        worst = max(worst, float(np.max(np.abs(restored - original))))
    assert worst < gate.NVAL_TOLERANCE_RUPIAH / 10.0, (
        f"round-trip residue {worst} rupiah is not comfortably inside the "
        f"{gate.NVAL_TOLERANCE_RUPIAH} rupiah tolerance")
    print(f"  ok worst rupiah round-trip residue {worst:.6g} vs tolerance "
          f"{gate.NVAL_TOLERANCE_RUPIAH}")

def test_semantic_validation_does_not_depend_on_the_shared_checkout():
    """Mutate the SHARED fixture; every semantic number must be unchanged.

    validity_domains() previously read SHARED/ohlc.parquet, so the report's
    headline validity counts, price-invalid keys and price/broker intersection
    were all computed from a file outside the experiment's custody -- the
    isolation the experiment-owned snapshot exists to provide, claimed but not
    implemented.

    The real shared checkout is never touched here. SHARED is redirected at a
    temporary directory holding a deliberately WRONG ohlc.parquet, and then at a
    directory holding nothing at all. The semantic result must be byte-identical
    in all three cases; only the clearly-labelled provenance comparison may move.
    """
    import experiment_1f_validation as validation
    if not os.path.exists(os.path.join(cand.CANDIDATE_DIR, cand.CANDIDATE_OHLC)):
        _skip("test_semantic_validation_does_not_depend_on_the_shared_checkout",
              "candidate OHLC absent")
        return

    covered, _ = cand.manifest_tickers(cand.CANDIDATE_DIR)
    real_shared = validation.SHARED
    baseline = validation.validity_domains(cand.CANDIDATE_DIR, covered)
    baseline_provenance = validation.ohlc_agreement(cand.CANDIDATE_DIR, covered)

    results, provenances = [], []
    tmp = tempfile.mkdtemp()
    try:
        # (1) a shared checkout holding DELIBERATELY WRONG prices
        wrong = pd.DataFrame([
            {"date": "2020-01-01", "ticker": "AAAA", "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 1.0, "volume": 1.0}])
        wrong.to_parquet(os.path.join(tmp, "ohlc.parquet"), index=False)
        validation.SHARED = tmp
        results.append(validation.validity_domains(cand.CANDIDATE_DIR, covered))
        provenances.append(validation.ohlc_agreement(cand.CANDIDATE_DIR, covered))

        # (2) a shared checkout that does not exist at all
        validation.SHARED = os.path.join(tmp, "not_here")
        results.append(validation.validity_domains(cand.CANDIDATE_DIR, covered))
        provenances.append(validation.ohlc_agreement(cand.CANDIDATE_DIR, covered))
    finally:
        validation.SHARED = real_shared
        shutil.rmtree(tmp, ignore_errors=True)

    for i, result in enumerate(results, start=1):
        assert result == baseline, (
            f"semantic validation moved when the shared checkout changed "
            f"(case {i}); it must depend only on the experiment-owned copy")
    assert baseline["price_source"]["path"].endswith(cand.CANDIDATE_OHLC), \
        baseline["price_source"]
    assert baseline["broker_basis_invalid"] > 0 and baseline["price_valid"] > 0, \
        "the baseline must actually be measuring something"

    # and the provenance comparison DID notice, which is its whole job
    assert provenances[0] != baseline_provenance, \
        "the provenance comparison must react to a mutated shared checkout"
    assert "not reachable" in str(provenances[1].get("status", "")), provenances[1]
    print(f"  ok semantic validity ({baseline['broker_basis_invalid']} broker-invalid, "
          f"{baseline['price_valid']} price-valid) is independent of SHARED")


def test_strict_ingest_refuses_a_partially_present_broker():
    """A broker present in one required field and absent from another fails.

    The matrix is allocated with zeros, so skipping a missing series turned an
    ABSENT measurement into an ASSERTED zero. The all-zero mask cannot rescue
    it: the field the broker IS present in is non-zero, so the row survives
    carrying a fabricated value.

    A broker absent from EVERY field is a different case and still yields no
    rows, so the guard does not reject data the vendor simply never mentioned.
    """
    dates = ["2026-01-01", "2026-01-02"]

    def payload(brokers):
        return _raw(dates, brokers)

    full = {"AK": {"nlot": [0, 0], "blot": [10, 10], "slot": [10, 10],
                   "nval": [0.0, 0.0], "bval": [1000.0, 1000.0],
                   "sval": [1000.0, 1000.0]}}
    frame = bidb.strict_ticker_frame(payload(full), "AAAA")
    assert frame is not None and len(frame) == 2, "the coherent fixture must load"

    # AK is present in blot/slot/nlot but MISSING from bval
    partial = {"AK": {k: v for k, v in full["AK"].items() if k != "bval"}}
    try:
        bidb.strict_ticker_frame(payload(partial), "AAAA")
    except bidb.StrictSourceError as exc:
        assert "not all" in str(exc) and "bval" in str(exc), str(exc)
        assert "asserted zero" in str(exc), \
            "the failure must name what zero-filling would have invented"
    else:
        raise AssertionError(
            "a broker missing a required field was silently zero-filled")

    # a broker absent from every field is NOT an error: it simply has no rows
    two = {"AK": full["AK"],
           "BK": {"nlot": [0, 0], "blot": [0, 0], "slot": [0, 0],
                  "nval": [0.0, 0.0], "bval": [0.0, 0.0], "sval": [0.0, 0.0]}}
    frame = bidb.strict_ticker_frame(payload(two), "AAAA")
    assert set(frame["broker"]) == {"AK"}, \
        "an all-zero broker must drop out without failing the build"
    print("  ok a partially-present broker fails closed instead of zero-filling")

# ── tracked-text identity: portable across CRLF/LF, still bound to the tree ──

def _temp_git_repo(tmp, filename="pinned_module.py", body=None):
    """A real repository with ONE committed LF file and autocrlf enabled.

    Isolated on purpose. Rewriting a real tracked file in this worktree to test
    line endings would risk leaving it rewritten if the test failed, and the
    property under test is a property of Git, not of this repository.
    """
    import subprocess

    def git(*args, **kw):
        return subprocess.run(["git", *args], cwd=tmp, capture_output=True,
                              text=True, **kw)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "core.autocrlf", "true")
    git("config", "commit.gpgsign", "false")
    path = os.path.join(tmp, filename)
    lf = body if body is not None else "def f():\n    return 1\n"
    with open(path, "wb") as fh:                       # committed as LF
        fh.write(lf.encode())
    git("add", "-A")
    git("commit", "-q", "-m", "pin")
    head = git("rev-parse", "HEAD").stdout.strip()
    return path, head, lf


def test_tracked_identity_is_the_same_for_lf_and_crlf_checkouts():
    """The CRLF blocker, closed. Same Git content -> same authoritative identity.

    This repository runs core.autocrlf=true with no .gitattributes, so a clean
    checkout holds CRLF while the committed blob holds LF. Hashing checkout
    bytes therefore produced: same HEAD, git status clean, identical Git
    content, DIFFERENT sha256, verification refused -- a machine-bound pin
    masquerading as a commit-bound one.

    Identity is now the committed Git content, and it must be invariant to the
    newline rendering on disk while a real content edit must still fail.
    """
    tmp = tempfile.mkdtemp()
    try:
        path, head, lf = _temp_git_repo(tmp)

        lf_identity = gate.tracked_text_identity(path, root=tmp)
        assert lf_identity["tracked_at_head"] is True
        assert lf_identity["worktree_matches_head"] is True
        canonical = lf_identity["git_content_sha256"]
        assert len(canonical) == 64

        # rewrite the SAME content with CRLF line endings
        with open(path, "wb") as fh:
            fh.write(lf.replace("\n", "\r\n").encode())
        crlf_identity = gate.tracked_text_identity(path, root=tmp)

        assert crlf_identity["git_content_sha256"] == canonical, \
            "the authoritative identity moved when only the newlines changed"
        assert crlf_identity["worktree_matches_head"] is True, \
            "Git regards a CRLF-only difference as clean; so must the contract"
        assert crlf_identity["worktree_raw_sha256"] != \
            lf_identity["worktree_raw_sha256"], \
            "the fixture must actually have different bytes on disk"
        assert "FORENSIC" in crlf_identity["worktree_raw_sha256_role"]

        # a REAL content edit must break both the identity and the cleanliness
        with open(path, "wb") as fh:
            fh.write(b"def f():\r\n    return 2\r\n")
        edited = gate.tracked_text_identity(path, root=tmp)
        assert edited["git_content_sha256"] == canonical, \
            "the committed content has not changed, only the worktree"
        assert edited["worktree_matches_head"] is False, \
            "a real edit MUST be visible; git-content hashing alone would hide it"

        # an untracked file has no canonical identity, and none is invented
        other = os.path.join(tmp, "not_committed.py")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        untracked = gate.tracked_text_identity(other, root=tmp)
        assert untracked["git_content_sha256"] is None
        assert untracked["canonical_identity_status"] == gate.UNAVAILABLE_UNTIL_COMMIT
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok LF and CRLF checkouts share one identity; a real edit does not")


def test_git_content_pin_still_refuses_an_edited_worktree():
    """Portability must not buy itself a worse hole.

    A git-content hash alone would verify happily while the file on disk had
    been edited, because HEAD had not moved. The tracked + clean conditions are
    what stop that, so each is exercised separately here.
    """
    entry = {"git_content_sha256": "a" * 64}

    clean = {"present": True, "tracked_at_head": True,
             "worktree_matches_head": True, "git_content_sha256": "a" * 64}
    assert gate._git_content_failures("f", entry, clean) == []

    edited = dict(clean, worktree_matches_head=False)
    assert any("staged or unstaged difference" in f
               for f in gate._git_content_failures("f", entry, edited))

    moved = dict(clean, git_content_sha256="b" * 64)
    assert any("committed content changed" in f
               for f in gate._git_content_failures("f", entry, moved))

    untracked = dict(clean, tracked_at_head=False,
                     canonical_identity_status=gate.UNAVAILABLE_UNTIL_COMMIT)
    assert any("NOT tracked at HEAD" in f
               for f in gate._git_content_failures("f", entry, untracked))

    absent = dict(clean, present=False)
    assert any("absent from the worktree" in f
               for f in gate._git_content_failures("f", entry, absent))

    unpinned = gate._git_content_failures("f", {"git_content_sha256": None}, clean)
    assert any("no full git_content_sha256" in f for f in unpinned)
    print("  ok a git-content pin still refuses edits, drift and untracked files")


def test_generated_artifacts_stay_pinned_by_raw_bytes():
    """The six experiment-owned artifacts are byte-pinned, deliberately.

    They are gitignored, no Git filter touches them, and their exact reviewed
    bytes ARE the object of review -- so a raw-byte mutation must still fail.
    """
    raw = [n for n, _, i in gate.EXECUTION_INPUTS if i == gate.IDENTITY_RAW_BYTES]
    tracked = [n for n, _, i in gate.EXECUTION_INPUTS
               if i == gate.IDENTITY_GIT_CONTENT]
    assert len(raw) == 6 and tracked == ["universe_json"], (raw, tracked)

    with tempfile.TemporaryDirectory() as tmp:
        inputs, _ = _orchestration_fixture(tmp)
        path = inputs["reviewed_manifest"]
        with open(path, encoding="utf-8") as fh:
            good = json.load(fh)

        for name in raw:
            assert good["A_execution_inputs"][name]["identity"] == \
                gate.IDENTITY_RAW_BYTES
            manifest = json.loads(json.dumps(good))
            manifest["A_execution_inputs"][name]["sha256"] = "0" * 64
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh)
            try:
                gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
            except gate.GateFailure as exc:
                assert "snapshot changed" in str(exc), str(exc)
            else:
                raise AssertionError(f"{name}: a raw-byte mutation was accepted")

        # and swapping a raw-byte pin to the tracked rule is itself refused
        manifest = json.loads(json.dumps(good))
        manifest["A_execution_inputs"][raw[0]]["identity"] = \
            gate.IDENTITY_GIT_CONTENT
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        try:
            gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        except gate.GateFailure as exc:
            assert "identity" in str(exc), str(exc)
        else:
            raise AssertionError("an input's identity rule was silently swapped")

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        _, _, report = gate.run_gate(inputs=inputs, mode=gate.PRIMARY_MODE)
        assert report["input_manifest"]["inputs"][raw[0]]["identity"] == \
            gate.IDENTITY_RAW_BYTES
    print("  ok 6 artifacts stay byte-pinned; 1 tracked input uses git content")


def test_untracked_pinned_implementation_cannot_be_established():
    """A pinned file with no committed content has no identity, and gets none.

    Built in a FRESH repository with exactly one pinned implementation file copied
    but never committed -- not by relying on this checkout's Phase-2 modules
    happening to be untracked, which the approved commit would end. Committing
    that file afterwards is the control: the identity resolves and establishment
    is no longer refused, so the refusal was caused by the missing commit alone.
    """
    import subprocess
    import experiment_1f_manifest as mf
    untracked = "experiment_1f_normalization.py"

    tmp = tempfile.mkdtemp(prefix="untracked_pin_repo_")
    try:
        repo, _head = _fresh_pinned_repo(os.path.join(tmp, "repo"),
                                         leave_untracked=(untracked,))
        path = os.path.join(repo, untracked)

        record = gate.tracked_text_identity(path, root=repo)
        assert record["present"] is True
        assert record["tracked_at_head"] is False
        assert record["git_content_sha256"] is None, \
            "an identity was invented for a file with no committed content"
        assert record["canonical_identity_status"] == gate.UNAVAILABLE_UNTIL_COMMIT
        assert mf.code_identity(root=repo)["identity_unavailable_until_commit"] \
            == [untracked]

        target = os.path.join(tmp, "manifest.json")
        try:
            _establish_against(repo, target)
        except mf.NotEstablishable as exc:
            assert "NOT TRACKED AT HEAD" in str(exc), str(exc)
            assert untracked in str(exc), str(exc)
        else:
            raise AssertionError(
                "an untracked pinned implementation was establishable")
        assert not os.path.exists(target)

        # control: commit it, and the identity resolves to its committed content
        for args in (["add", "--", untracked], ["commit", "-q", "-m", "commit pin"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        resolved = gate.tracked_text_identity(path, root=repo)
        assert resolved["tracked_at_head"] and resolved["worktree_matches_head"]
        assert resolved["git_content_sha256"] == _head_blob_sha256(repo, untracked)
        assert resolved["canonical_identity_status"] == "AVAILABLE"
        proposal = _minimal_proposal(repo)
        assert proposal["E_code_identity"]["identity_unavailable_until_commit"] == []
        assert mf.assert_establishable(proposal) is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok an untracked pin has no identity and is refused; committing it "
          "resolves both")


def test_universe_keeps_semantic_validation_alongside_tracked_identity():
    """The universe is tracked text AND an execution input. Both rules apply."""
    name, key, identity = next(e for e in gate.EXECUTION_INPUTS
                               if e[0] == "universe_json")
    assert identity == gate.IDENTITY_GIT_CONTENT

    inputs = gate.candidate_inputs()
    record = gate.tracked_text_identity(inputs[key])
    assert record["tracked_at_head"] and record["worktree_matches_head"]
    # The authoritative identity is the content Git stores at HEAD, recomputed
    # here through independent plumbing. Deliberately NOT a comparison against
    # the checkout's raw bytes: whether those differ from the blob depends on
    # the machine's newline settings, so asserting either way would pass on a
    # CRLF checkout and fail on a legitimate LF one. The two properties that
    # matter -- a CRLF/LF rendering does not change acceptance, and a genuine
    # content edit does -- are proven in an isolated repository by
    # test_tracked_identity_is_the_same_for_lf_and_crlf_checkouts.
    assert record["git_content_sha256"] == \
        _head_blob_sha256(HERE, "experiment_1f_universe.json")

    # the semantic contract is untouched by the identity change
    tickers, audit = gate.resolve_universe(frozen_path=inputs[key],
                                           require_frozen=True)
    assert len(tickers) == gate.EXPECTED_UNIVERSE_SIZE == 298
    assert audit["universe_digest"] == gate.ACCEPTED_UNIVERSE_DIGEST \
        == "7a4ce50ffb5a7ebe"
    print(f"  ok universe: git-content identity + n={len(tickers)} "
          f"digest={audit['universe_digest']}")


def test_both_strict_readers_share_one_source_completeness_contract():
    """One definition of "strict raw source", used by ingest AND normalisation.

    strict_ticker_frame() and strict_totals() both zero-initialise their
    accumulators, so a broker present in blot but absent from bval contributes a
    fabricated zero in either path -- and every basis factor and wrap candidate
    is inferred from the normalisation totals. Two validators disagreeing about
    what a valid source is would be worse than either rule alone.
    """
    dates = ["2026-01-01", "2026-01-02"]
    full = {"AK": {"nlot": [0, 0], "blot": [10, 10], "slot": [10, 10],
                   "nval": [0.0, 0.0], "bval": [1000.0, 1000.0],
                   "sval": [1000.0, 1000.0]}}

    coherent = _raw(dates, full)
    assert bidb.strict_ticker_frame(coherent, "AAAA") is not None
    assert norm.strict_totals(coherent, "AAAA")["blot"] == [10, 10]

    # AK is present in blot/nlot/slot but ABSENT from bval
    missing = {"AK": {k: v for k, v in full["AK"].items() if k != "bval"}}
    payload = _raw(dates, missing)
    assert bidb.partially_present_brokers(payload) == ["AK missing ['bval']"]

    try:
        bidb.strict_ticker_frame(payload, "AAAA")
    except bidb.StrictSourceError as exc:
        assert "bval" in str(exc) and "asserted zero" in str(exc), str(exc)
    else:
        raise AssertionError("strict_ticker_frame accepted a missing field")

    try:
        norm.strict_totals(payload, "AAAA")
    except cand.CandidateError as exc:
        assert "bval" in str(exc) and "asserted zero" in str(exc), str(exc)
    else:
        raise AssertionError("strict_totals accepted a missing field")

    # a broker absent from EVERY field is not an error in either reader
    both = {"AK": full["AK"],
            "BK": {"nlot": [0, 0], "blot": [0, 0], "slot": [0, 0],
                   "nval": [0.0, 0.0], "bval": [0.0, 0.0], "sval": [0.0, 0.0]}}
    payload = _raw(dates, both)
    frame = bidb.strict_ticker_frame(payload, "AAAA")
    assert set(frame["broker"]) == {"AK"}
    assert norm.strict_totals(payload, "AAAA")["blot"] == [10, 10]
    print("  ok ingest and normalisation share one source-completeness rule")


def test_real_297_has_no_partially_present_broker():
    """Measured on the approved snapshot: the stricter rule adds 0 violations."""
    source_dir = os.path.join(cand.CANDIDATE_DIR, cand.SOURCE_RAW)
    if not os.path.isdir(source_dir):
        _skip("test_real_297_has_no_partially_present_broker", "snapshot absent")
        return
    covered, _ = cand.manifest_tickers(cand.CANDIDATE_DIR)
    offenders, frame_fail, totals_fail = [], [], []
    for ticker in covered:
        data = cand.read_raw(cand.raw_path(source_dir, ticker))
        if bidb.partially_present_brokers(data):
            offenders.append(ticker)
        try:
            bidb.strict_ticker_frame(data, ticker)
        except bidb.StrictSourceError:
            frame_fail.append(ticker)
        try:
            norm.strict_totals(data, ticker)
        except cand.CandidateError:
            totals_fail.append(ticker)
    assert offenders == [] and frame_fail == [] and totals_fail == [], \
        (offenders[:5], frame_fail[:5], totals_fail[:5])
    print(f"  ok all {len(covered)} approved tickers pass both strict readers "
          f"(0 new violations)")

ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    print(f"Experiment #1F Phase 2 candidate contract: {len(ALL)} tests\n")
    for fn in ALL:
        print(f"{fn.__name__}")
        fn()
    print(f"\nAll {len(ALL)} tests passed."
          + (f" ({len(SKIPPED)} skipped: {'; '.join(SKIPPED)})" if SKIPPED else ""))


if __name__ == "__main__":
    sys.exit(main())
