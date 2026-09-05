"""
Lightweight regression tests for walk_forward_backtest.py and kelly_sizing.py.

No test framework dependency (assert-based, run directly) — this repo has
none yet and these checks are simple enough not to need one. Focus is
specifically on leakage and formula correctness, since a silent bug here
(e.g. a "lagged" feature that actually sees today's data) would quietly
invalidate every backtest number in SYSTEM.md's log without ever raising an
exception. Run after touching build_panel(), signal_quality(), or
kelly_fraction() — before trusting a new backtest run's results.
"""

import pandas as pd
import numpy as np
import sqlite3

from walk_forward_backtest import (
    _broker_day_aggregates, _broker_correlation_1d, _price_features_and_target,
    build_panel, signal_quality, FEATURES,
)
from signal_metrics import spearman_ic, signal_stats, trade_stats
from strategy_variants import get_walk_forward_predictions, simulate_trade, _index_price_history
from kelly_sizing import kelly_fraction, kelly_from_trades
from price_audit import (add_forward_returns, add_lagged_returns,
                         series_signature, ticker_from_title, should_fail_run,
                         detect, bagholders_from_payload, bagholders_from_payloads,
                         inventory_date_blocks, date_offset_holds,
                         authoritative_duplicate_deletions, cmd_quarantine,
                         OHLCV)
from ml_v2_experiment_1 import (
    EXPERIMENT_XGB_PARAMS, FLOW_WINDOWS, PRICE_FEATURES,
    _historical_net_lots, build_broker_identity_features,
    feature_sets_for_columns, make_walk_forward_splits,
)
from ml_v2_experiment_1_robustness import (
    _bootstrap_statistics, paired_date_differences,
)
from pattern_type_backtest import date_balanced_hit_edge, trade_level_stats
from foreign_flow_signal_backtest import (
    date_balanced_hit_edge as foreign_date_balanced_hit_edge,
    trade_stats as foreign_trade_stats,
)
from regime_gated_momentum import select_threshold


def test_broker_day_aggregates_basic():
    bf = pd.DataFrame([
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "X", "netval": 10.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Y", "netval": -5.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Z", "netval": 2.0},
    ])
    agg = _broker_day_aggregates(bf)
    row = agg.iloc[0]
    assert row["n_brokers"] == 3
    assert abs(row["net_flow_total"] - 7.0) < 1e-9
    # top-3 of 3 brokers = all of them -> concentration should be exactly 1.0
    assert abs(row["broker_concentration"] - 1.0) < 1e-9
    # 2 of 3 brokers net positive
    assert abs(row["net_buy_ratio"] - (2 / 3)) < 1e-9
    print("test_broker_day_aggregates_basic passed")


def test_broker_correlation_first_day_is_nan():
    # a ticker's first day has no prior day to correlate against -> must be NaN,
    # never silently 0 or 1 (which would be a real, easy-to-miss leakage bug)
    bf = pd.DataFrame([
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "X", "netval": 10.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Y", "netval": -5.0},
        {"date": "2026-01-01", "ticker": "AAA", "broker_code": "Z", "netval": 3.0},
        {"date": "2026-01-02", "ticker": "AAA", "broker_code": "X", "netval": 8.0},
        {"date": "2026-01-02", "ticker": "AAA", "broker_code": "Y", "netval": -4.0},
        {"date": "2026-01-02", "ticker": "AAA", "broker_code": "Z", "netval": 1.0},
    ])
    corr = _broker_correlation_1d(bf)
    day1 = corr[corr["date"] == "2026-01-01"]
    day2 = corr[corr["date"] == "2026-01-02"]
    assert len(day1) == 0, "first day should produce no correlation row at all"
    assert len(day2) == 1
    # X,Y,Z moved 10->8, -5->-4, 3->1: same signs, strongly correlated -> should be near +1
    assert corr.iloc[0]["broker_correlation_1d"] > 0.9
    print("test_broker_correlation_first_day_is_nan passed")


def test_price_features_no_leakage():
    """Features as of close(T); target under the EXECUTABLE contract.

    This test used to assert that entry at close(T) was CORRECT. It was not:
    a decision taken at EOD(T) consumes that same close and the post-session
    broker summary, so it cannot transact at that close. The target is now
    open(T+1) -> open(T+2). Features are unchanged and still as-of close(T).
    """
    px = pd.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, 11)],
        "ticker": ["AAA"] * 10,
        "open":  [100, 102, 98, 106, 97, 111, 91, 121, 81, 131],
        "high":  [101, 103, 100, 107, 99, 112, 92, 122, 82, 132],
        "low":   [99, 100, 97, 104, 96, 109, 89, 119, 79, 129],
        "close": [100, 101, 99, 105, 98, 110, 90, 120, 80, 130],
        "volume": [1000] * 10,
    })
    out = _price_features_and_target(px)
    row = out[out["date"] == "2026-01-02"].iloc[0]

    # momentum_1d on day d still uses ONLY past+current close, both known as of
    # day d's close.
    assert abs(row["momentum_1d"] - (101 - 100) / 100) < 1e-9

    # target for a decision on day 2 must be open(day3) -> open(day4): the
    # earliest anchor the decision could actually reach.
    assert abs(row["target"] - (106 / 98 - 1)) < 1e-9, (
        "target must be open(T+1)->open(T+2); if this equals a close-based "
        "return the unexecutable contract has come back"
    )
    # and it must NOT equal the old close(T)->close(T+1) value
    assert abs(row["target"] - ((99 - 101) / 101)) > 1e-6

    # the close-anchored value survives only as a labelled diagnostic
    assert abs(row["target_cc"] - ((99 - 101) / 101)) < 1e-9

    # volume_ratio must use a trailing mean that EXCLUDES today.
    defined = out.dropna(subset=["volume_ratio"])
    assert defined["date"].min() == "2026-01-06", (
        "volume_ratio should first be defined on day 6, using days 1-5 as the "
        "trailing window (shift(1) before rolling(5) excludes day 6's own "
        "volume) — if this drifts to day 5 or earlier, today's own volume is "
        "leaking into its own trailing average"
    )
    print("test_price_features_no_leakage passed")


def _oa_frame(rows):
    """Small OHLC frame for open-anchored label tests."""
    return pd.DataFrame(rows)


def test_open_anchored_labels_match_hand_computed_values():
    import price_audit as pa
    dates = [f"2026-01-{d:02d}" for d in range(1, 6)]
    px = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 5,
        "open":  [100.0, 102.0, 104.0, 106.0, 108.0],
        "high":  [110.0, 112.0, 114.0, 116.0, 118.0],
        "low":   [95.0, 97.0, 99.0, 101.0, 103.0],
        "close": [101.0, 103.0, 105.0, 107.0, 109.0],
        "volume": [1000] * 5,
    })
    out = pa.add_forward_returns(px, dates, horizons=(1,), open_anchored=True)
    r0 = out.iloc[0]
    # decision day 1: enter open(day2)=102, exit open(day3)=104
    assert abs(r0["fwd_oo_1"] - (104 / 102 - 1)) < 1e-12
    # open(day2)=102 -> close(day2)=103
    assert abs(r0["fwd_oc_1"] - (103 / 102 - 1)) < 1e-12
    # close(day1)=101 -> open(day2)=102
    assert abs(r0["gap_1"] - (102 / 101 - 1)) < 1e-12
    # unchanged close-anchored label
    assert abs(r0["fwd_1"] - (103 / 101 - 1)) < 1e-12
    print("test_open_anchored_labels_match_hand_computed_values passed")


def test_multiplicative_composition_not_additive():
    """1 + fwd_1 == (1+gap_1)*(1+fwd_oc_1). The ADDITIVE form is wrong and is
    asserted wrong here so the distinction is pinned by test, not comment."""
    import price_audit as pa
    dates = [f"2026-01-{d:02d}" for d in range(1, 6)]
    px = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 5,
        "open":  [100.0, 108.0, 104.0, 106.0, 108.0],
        "high":  [115.0, 118.0, 114.0, 116.0, 118.0],
        "low":   [95.0, 97.0, 99.0, 101.0, 103.0],
        "close": [101.0, 103.0, 105.0, 107.0, 109.0],
        "volume": [1000] * 5,
    })
    out = pa.add_forward_returns(px, dates, horizons=(1,), open_anchored=True)
    d = out.dropna(subset=["fwd_1", "gap_1", "fwd_oc_1"])
    assert len(d) > 0
    lhs = 1 + d["fwd_1"]
    rhs = (1 + d["gap_1"]) * (1 + d["fwd_oc_1"])
    assert (lhs - rhs).abs().max() < 1e-12, "multiplicative identity must hold"
    additive = d["gap_1"] + d["fwd_oc_1"]
    assert (additive - d["fwd_1"]).abs().max() > 1e-9, (
        "the additive form must NOT hold — if it does the fixture is degenerate "
        "and the test proves nothing"
    )
    print("test_multiplicative_composition_not_additive passed")


def test_invalid_open_anchor_yields_nan_even_when_close_passes():
    """The FAST 2025-10-14 case: prev_close 580, open 870 (+50%), close 720.

    Close-to-close is +24.1%, inside the 25% band, so _step_valid PASSES — yet
    an entry anchored on 870 is fabricated. Every target anchored on that open
    must be NaN, while the ROW itself is kept (never deleted).
    """
    import price_audit as pa
    dates = [f"2026-01-{d:02d}" for d in range(1, 5)]
    px = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 4,
        "open":  [575.0, 870.0, 725.0, 730.0],   # day2 open is +50% on prev close
        "high":  [585.0, 880.0, 735.0, 740.0],
        "low":   [570.0, 715.0, 715.0, 720.0],
        "close": [580.0, 720.0, 730.0, 735.0],   # 580 -> 720 = +24.1%, in band
        "volume": [1000] * 4,
    })
    out = pa.add_forward_returns(px, dates, horizons=(1,), open_anchored=True)
    day1 = out.iloc[0]
    assert pd.notna(day1["fwd_1"]), "close-anchored label passes the band, as measured"
    assert pd.isna(day1["fwd_oo_1"]), "entry anchored on an out-of-band open must be NaN"
    assert pd.isna(day1["fwd_oc_1"])
    assert pd.isna(day1["gap_1"])
    assert len(out) == 4, "rows are kept, never deleted"
    print("test_invalid_open_anchor_yields_nan_even_when_close_passes passed")


def test_open_outside_high_low_or_nonpositive_is_invalid():
    import price_audit as pa
    dates = [f"2026-01-{d:02d}" for d in range(1, 4)]
    for label, opens in [("open>high", [100.0, 130.0, 104.0]),
                          ("open<=0", [100.0, 0.0, 104.0])]:
        px = _oa_frame({
            "date": dates, "ticker": ["AAA"] * 3,
            "open": opens,
            "high": [110.0, 112.0, 114.0],
            "low": [95.0, 97.0, 99.0],
            "close": [101.0, 103.0, 105.0],
            "volume": [1000] * 3,
        })
        out = pa.add_forward_returns(px, dates, horizons=(1,), open_anchored=True)
        assert pd.isna(out.iloc[0]["fwd_oc_1"]), f"{label} must invalidate the anchor"
        assert pd.isna(out.iloc[0]["gap_1"]), f"{label} must invalidate the anchor"
    print("test_open_outside_high_low_or_nonpositive_is_invalid passed")


def test_corrupt_close_t_plus_1_invalidates_fwd_oc_1():
    """PR #36 review: a corrupt close(T+1) must invalidate fwd_oc_1.

    The old h==1 branch hardcoded its close-step mask to True unconditionally
    (no window at all was checked), so a fabricated close(T+1) used directly
    as the fwd_oc_1 exit price passed straight through. close(T+1)=180 is
    +80% on close(T)=100 -- outside the ARA band -- so `_step_valid` at T is
    False and must now propagate into fwd_oc_1.
    """
    import price_audit as pa
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    px = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 3,
        "open":  [100.0, 101.0, 182.0],
        "high":  [101.0, 182.0, 186.0],
        "low":   [99.0, 100.0, 181.0],
        "close": [100.0, 180.0, 183.0],  # close(T+1)=180 is +80% vs close(T)=100
        "volume": [1000] * 3,
    })
    out = pa.add_forward_returns(px, dates, horizons=(1,), open_anchored=True)
    assert pd.isna(out.iloc[0]["fwd_oc_1"]), (
        "fwd_oc_1 must be invalidated by a corrupt close(T+1) exit price"
    )
    print("test_corrupt_close_t_plus_1_invalidates_fwd_oc_1 passed")


def test_corrupt_close_t_plus_1_invalidates_fwd_oo_1_even_when_both_opens_pass():
    """PR #36 review: a corrupt close(T+1) must invalidate fwd_oo_1 even when
    BOTH individual opens pass their own local previous-close check.

    open(T+1)=101 is fine against close(T)=100 (+1%). open(T+2)=182 is ALSO
    individually fine against close(T+1)=180 (+1.1%) -- but 180 itself is
    +80% off close(T)=100, which neither open-vs-immediate-prior-close check
    can see. The old code checked close(T+1)->close(T+2) (180->183, which
    looks locally sane) instead of close(T)->close(T+1) (100->180, which does
    not), so this corruption slipped through undetected.
    """
    import price_audit as pa
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    px = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 3,
        "open":  [100.0, 101.0, 182.0],
        "high":  [101.0, 182.0, 186.0],
        "low":   [99.0, 100.0, 181.0],
        "close": [100.0, 180.0, 183.0],
        "volume": [1000] * 3,
    })
    open_valid = pa._open_anchor_valid(px, px.groupby("ticker"))
    assert bool(open_valid.iloc[1]), "open(T+1) must pass its own local check"
    assert bool(open_valid.iloc[2]), "open(T+2) must ALSO pass its own local check (vs the corrupt close(T+1))"

    out = pa.add_forward_returns(px, dates, horizons=(1,), open_anchored=True)
    assert pd.isna(out.iloc[0]["fwd_oo_1"]), (
        "fwd_oo_1 must be invalidated by corrupt close(T+1) even though both "
        "individual opens independently pass"
    )
    print("test_corrupt_close_t_plus_1_invalidates_fwd_oo_1_even_when_both_opens_pass passed")


def test_corrupt_close_after_oo_exit_does_not_invalidate_fwd_oo_1():
    """PR #36 review: a corrupt close(T+2) — which occurs AFTER fwd_oo_1's
    open(T+2) exit and is never the reference for any open in this window —
    must NOT invalidate fwd_oo_1. Exit validity depends on open(T+2) against
    close(T+1), never on close(T+2) itself."""
    import price_audit as pa
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    px = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 3,
        "open":  [100.0, 101.0, 106.0],
        "high":  [101.0, 107.0, 999.0],
        "low":   [99.0, 100.0, 106.0],
        "close": [100.0, 105.0, 999.0],  # close(T+2)=999 is wildly corrupt
        "volume": [1000] * 3,
    })
    out = pa.add_forward_returns(px, dates, horizons=(1,), open_anchored=True)
    row0 = out.iloc[0]
    assert pd.notna(row0["fwd_oo_1"]), (
        "a corrupt close strictly after the open(T+1+h) exit must not "
        "invalidate fwd_oo_1"
    )
    expected = 106.0 / 101.0 - 1
    assert abs(row0["fwd_oo_1"] - expected) < 1e-9
    print("test_corrupt_close_after_oo_exit_does_not_invalidate_fwd_oo_1 passed")


def test_close_window_validity_fix_holds_for_h_greater_than_1():
    """PR #36 review requirement 4: the same two properties hold at h=2 —
    corrupt close(T+1) invalidates fwd_oo_2/fwd_oc_2, but corrupt close(T+3)
    (the exit session's own close, strictly after open(T+3)'s exit) does not.
    """
    import price_audit as pa
    dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]

    corrupt_entry_leg = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 4,
        "open":  [100.0, 101.0, 182.0, 186.0],
        "high":  [101.0, 182.0, 187.0, 191.0],
        "low":   [99.0, 100.0, 181.0, 184.0],
        "close": [100.0, 180.0, 185.0, 188.0],  # close(T+1)=180 is +80% vs close(T)=100
        "volume": [1000] * 4,
    })
    out_a = pa.add_forward_returns(corrupt_entry_leg, dates, horizons=(2,), open_anchored=True)
    row_a = out_a.iloc[0]
    assert pd.isna(row_a["fwd_oo_2"]), "corrupt close(T+1) must invalidate fwd_oo_2"
    assert pd.isna(row_a["fwd_oc_2"]), "corrupt close(T+1) must invalidate fwd_oc_2"

    corrupt_after_exit = _oa_frame({
        "date": dates, "ticker": ["AAA"] * 4,
        "open":  [100.0, 101.0, 106.0, 111.0],
        "high":  [101.0, 107.0, 112.0, 999.0],
        "low":   [99.0, 100.0, 105.0, 111.0],
        "close": [100.0, 105.0, 110.0, 999.0],  # close(T+3)=999: the exit session's own close
        "volume": [1000] * 4,
    })
    out_b = pa.add_forward_returns(corrupt_after_exit, dates, horizons=(2,), open_anchored=True)
    row_b = out_b.iloc[0]
    assert pd.notna(row_b["fwd_oo_2"]), (
        "corrupt close(T+3), the exit session's own close, must not "
        "invalidate fwd_oo_2 (exit validity depends on open(T+3) vs "
        "close(T+2), never on close(T+3) itself)"
    )
    expected = 111.0 / 101.0 - 1
    assert abs(row_b["fwd_oo_2"] - expected) < 1e-9
    print("test_close_window_validity_fix_holds_for_h_greater_than_1 passed")


def test_both_realization_purges_hold_on_timestamps():
    """decision at EOD(i); label enters OPEN(i+1) and realizes OPEN(i+1+h).

    Ordering is on TIMESTAMPS, not dates: OPEN(k) precedes EOD(k) within one
    session, which is exactly why purge=h (not h+1) is correct.
    """
    from walk_forward_backtest import make_walk_forward_splits
    dates = list(range(258))
    realized_at = lambda i, h: (i + 1 + h) + 0.0   # OPEN
    decision_at = lambda i: i + 0.5                # EOD

    for h in (1, 5, 10, 20):
        splits, report = make_walk_forward_splits(dates, horizon=h)
        assert splits, f"h={h} produced no scored folds"
        for sp in splits:
            train = list(sp["fit"]) + list(sp["eval"])
            assert max(realized_at(i, h) for i in train) < min(decision_at(k) for k in sp["test"]), \
                f"outer purge violated at h={h}"
            assert max(realized_at(i, h) for i in sp["fit"]) < min(decision_at(k) for k in sp["eval"]), \
                f"internal fit->eval purge violated at h={h}"
            assert not (set(sp["fit"]) & set(sp["eval"])), "fit/eval must be date-disjoint"
        assert report["n_folds_scored"] == len(splits)

    # the guard has teeth: with no purge at all the outer invariant fails
    violations = sum(
        1 for te in range(30, 258 - 6 + 1, 6)
        if not max(realized_at(i, 1) for i in range(te)) < min(decision_at(k) for k in range(te, te + 6))
    )
    assert violations > 0, "counter-check failed: unpurged folds should violate"
    print("test_both_realization_purges_hold_on_timestamps passed")


def test_thin_and_infeasible_folds_are_skipped_and_counted():
    from walk_forward_backtest import make_walk_forward_splits
    dates = list(range(258))
    _, r20 = make_walk_forward_splits(dates, horizon=20)
    assert r20["n_infeasible"] == 3, (
        "at h=20 on a 258-date panel three folds are arithmetically impossible "
        "(fit block <= 0 after both purges) and must be counted, not scored"
    )
    assert r20["n_folds_scored"] + r20["n_infeasible"] + r20["n_too_thin"] == r20["n_folds_nominal"]
    # every scored fold actually clears the stated minimums
    for h in (1, 5, 10, 20):
        splits, rep = make_walk_forward_splits(dates, horizon=h)
        for sp in splits:
            assert len(sp["fit"]) >= rep["min_fit_days"]
            assert len(sp["eval"]) >= rep["min_eval_days"]
    print("test_thin_and_infeasible_folds_are_skipped_and_counted passed")


def test_legacy_experiment1_digest_is_frozen():
    """The close-contract result is historical provenance, not a value to
    re-pin. #1E gets its own identity instead."""
    import ml_v2_experiment_1_robustness as rob
    assert rob.LEGACY_CLOSE_CONTRACT_DIGEST == "147d734749c71e2d", (
        "legacy Experiment #1 provenance must not be overwritten — the "
        "executable re-audit belongs under EXECUTABLE_V1_PREDICTION_DIGEST"
    )
    assert hasattr(rob, "EXECUTABLE_V1_PREDICTION_DIGEST")
    assert rob.EXECUTABLE_V1_PREDICTION_DIGEST != rob.LEGACY_CLOSE_CONTRACT_DIGEST, (
        "the executable contract cannot reproduce the close contract's digest "
        "by construction; reusing it would be a false provenance claim"
    )
    print("test_legacy_experiment1_digest_is_frozen passed")


def test_target_refuses_to_silently_use_the_close_contract():
    """No `open` and no fwd_oo_1 must RAISE, never quietly fall back.

    A silent fallback to close(T)->close(T+1) is precisely how an unexecutable
    target would creep back in unnoticed.
    """
    px = pd.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, 6)],
        "ticker": ["AAA"] * 5,
        "close": [100, 101, 99, 105, 98],
        "volume": [1000] * 5,
    })
    try:
        _price_features_and_target(px)
        assert False, "must refuse to fall back to the close-anchored target"
    except ValueError as e:
        assert "close" in str(e).lower()
    print("test_target_refuses_to_silently_use_the_close_contract passed")


def test_spearman_ic_direction():
    x = pd.Series([1.0, 2, 3, 4, 5])
    assert abs(spearman_ic(x, x * 3 + 1) - 1.0) < 1e-9, "monotonic up must be +1"
    assert abs(spearman_ic(x, -x) + 1.0) < 1e-9, "monotonic down must be -1"
    # rank-based, so one wild outlier must NOT dominate the way it would Pearson
    assert abs(spearman_ic(x, pd.Series([1.0, 2, 3, 4, 500])) - 1.0) < 1e-9
    assert np.isnan(spearman_ic(x, pd.Series([7.0] * 5))), "constant -> undefined, not 0"
    assert np.isnan(spearman_ic(pd.Series([1.0]), pd.Series([1.0]))), "too few points"
    print("test_spearman_ic_direction passed")


def test_signal_stats_detects_a_useless_signal():
    # THE case this repo missed for months: hit_rate 42.8% was reported as a
    # result while the universe base rate was also 42.8%. A hit rate without its
    # base rate is unreadable, so hit_edge must come out ~0 here even though the
    # raw hit rate looks like a number worth quoting.
    rng = np.random.default_rng(0)
    actual = pd.Series(rng.normal(0.004, 0.03, 4000))
    pred = pd.Series(rng.normal(0, 0.01, 4000))          # independent of actual
    s = signal_stats(pred, actual)
    assert abs(s["ic"]) < 0.05, f"independent series should have ~0 IC, got {s['ic']}"
    assert abs(s["hit_edge"]) < 0.06, f"no edge expected, got {s['hit_edge']}"
    assert abs(s["edge"]) < 0.01
    assert s["base_rate"] > 0.5, "sanity: this generator has a positive drift"
    print("test_signal_stats_detects_a_useless_signal passed")


def test_signal_stats_reports_a_negative_edge_as_negative():
    # A signal that is actively WRONG must not be flattered into looking flat.
    actual = pd.Series(np.linspace(-0.1, 0.1, 200))
    pred = pd.Series(np.linspace(0.1, -0.1, 200))        # perfectly inverted
    s = signal_stats(pred, actual)
    assert s["ic"] < -0.99
    assert s["edge"] < 0, "top decile of an inverted signal must underperform"
    assert s["hit_edge"] < 0
    print("test_signal_stats_reports_a_negative_edge_as_negative passed")


def test_signal_stats_ranks_cross_sectionally_each_day():
    # Day 1 is ranked perfectly, day 2 perfectly backwards. Pooling levels can
    # obscure that structure; daily IC must expose the +1/-1 cancellation and
    # top-decile must choose one name per date, not a global score cutoff.
    pred = pd.Series([1, 2, 3, 4, 10, 20, 30, 40], dtype=float)
    actual = pd.Series([1, 2, 3, 4, 4, 3, 2, 1], dtype=float)
    dates = pd.Series(["d1"] * 4 + ["d2"] * 4)
    s = signal_stats(pred, actual, groups=dates)
    assert abs(s["daily_ic"]) < 1e-12
    assert abs(s["daily_ic_median"]) < 1e-12
    assert abs(s["positive_ic_days"] - 0.5) < 1e-12
    assert s["n_daily_ic"] == 2
    assert s["n_top"] == 2, "top decile must select the best-ranked name each day"
    print("test_signal_stats_ranks_cross_sectionally_each_day passed")


def test_ml_v2_xgboost_seed_is_locked():
    assert isinstance(EXPERIMENT_XGB_PARAMS["random_state"], int)
    assert EXPERIMENT_XGB_PARAMS["n_jobs"] == 1
    assert EXPERIMENT_XGB_PARAMS["tree_method"] == "hist"
    print("test_ml_v2_xgboost_seed_is_locked passed")


def test_ml_v2_feature_sets_keep_the_same_price_controls():
    identity = ["broker_AK_flow_1d", "broker_BK_flow_1d"]
    inventory = ["broker_AK_observable_inventory", "broker_BK_observable_inventory"]
    sets = feature_sets_for_columns(identity, inventory)
    assert list(sets) == [
        "price_only", "existing_broker_aggregate", "broker_identity",
        "broker_identity + observable_inventory",
    ]
    for features in sets.values():
        assert set(PRICE_FEATURES).issubset(features)
    assert identity[0] in sets["broker_identity"], "broker code must survive in the feature name"
    assert not any("observable_inventory" in f for f in sets["broker_identity"])
    assert all(f in sets["broker_identity + observable_inventory"] for f in inventory)
    print("test_ml_v2_feature_sets_keep_the_same_price_controls passed")


def test_broker_identity_flows_and_observable_inventory_use_net_lots():
    dates = [f"2026-01-{day:02d}" for day in range(1, 22)]
    px = pd.DataFrame({
        "ticker": ["AAA"] * len(dates), "date": dates,
        "close": [100.0] * len(dates), "volume": [10_000.0] * len(dates),
        **{f"lag_{window}": [1.0] * len(dates) for window in FLOW_WINDOWS},
    })
    rows = []
    for date in dates:
        for code, lots in (("AK", 10.0), ("BK", -5.0)):
            rows.append({
                "ticker": "AAA", "date": date, "broker_code": code,
                "bval": np.nan, "sval": np.nan, "bavg": np.nan, "savg": np.nan,
                "netval": lots * 100.0 * 100.0 / 1e9, "close": 100.0,
            })
    bf = pd.DataFrame(rows)
    out, flow, inventory = build_broker_identity_features(px, bf)
    last = out.iloc[-1]
    assert len(flow) == 2 * len(FLOW_WINDOWS)
    assert len(inventory) == 2
    assert abs(last["broker_AK_flow_1d"] - 0.10) < 1e-12
    assert abs(last["broker_AK_flow_3d"] - 0.10) < 1e-12
    assert abs(last["broker_BK_flow_20d"] + 0.05) < 1e-12
    assert abs(last["broker_AK_observable_inventory"] - 2.10) < 1e-12
    assert abs(last["broker_BK_observable_inventory"] + 1.05) < 1e-12

    # A clean-panel gap restarts cumulative observable inventory rather than
    # assuming the missing interval carried zero flow.
    gapped = px.copy()
    gapped.loc[10, "lag_1"] = np.nan
    reset, _, _ = build_broker_identity_features(gapped, bf)
    assert abs(reset.iloc[-1]["broker_AK_observable_inventory"] - 1.10) < 1e-12

    live = pd.DataFrame([{
        "bval": 10 * 100 * 100 / 1e9, "sval": 2 * 100 * 100 / 1e9,
        "bavg": 100.0, "savg": 100.0, "netval": 0.0, "close": 100.0,
    }])
    assert abs(_historical_net_lots(live).iloc[0]["net_lots"] - 8.0) < 1e-12
    print("test_broker_identity_flows_and_observable_inventory_use_net_lots passed")


def test_ml_v2_walk_forward_splits_are_strictly_chronological():
    panel = pd.DataFrame({"date": [f"d{i:03d}" for i in range(50)]})
    splits, report = make_walk_forward_splits(
        panel, train_min=30, test_window=6, return_report=True
    )
    # Was 3 before the executable contract. The first candidate fold now has a
    # 22-date fit block after both realization purges and is correctly rejected
    # as too thin (MIN_FIT_DAYS=24) — skipped and COUNTED, never silently
    # scored. The chronology assertions below are the real point of this test.
    assert len(splits) == 2
    assert report["n_too_thin"] == 1
    assert report["n_folds_scored"] + report["n_too_thin"] + report["n_infeasible"] \
        == report["n_folds_nominal"]
    for split in splits:
        assert set(split["fit"]).isdisjoint(split["eval"])
        assert set(split["fit"]).isdisjoint(split["test"])
        assert set(split["eval"]).isdisjoint(split["test"])
        assert max(split["fit"]) < min(split["eval"]) < min(split["test"])
    print("test_ml_v2_walk_forward_splits_are_strictly_chronological passed")


def test_ml_v2_robustness_pairs_predictions_within_date():
    rows_b, rows_c = [], []
    targets = [-0.04, -0.01, 0.01, 0.04]
    for date in ("d1", "d2"):
        for i, target in enumerate(targets):
            base = {"ticker": f"T{i}", "date": date, "target": target, "cycle": 1}
            rows_b.append({**base, "prediction": float(4 - i)})
            rows_c.append({**base, "prediction": float(i)})
    paired = paired_date_differences(pd.DataFrame(rows_b), pd.DataFrame(rows_c))
    assert len(paired) == 2
    assert (paired["top_hit_delta"] == 1.0).all()
    assert (paired["return_edge_delta"] > 0).all()
    assert (paired["daily_ic_delta"] > 1.9).all()
    print("test_ml_v2_robustness_pairs_predictions_within_date passed")


def test_ml_v2_bootstrap_is_date_level_and_deterministic():
    values = pd.Series([1.0, -1.0, 2.0, -2.0, 0.5, -0.5])
    first = _bootstrap_statistics(values, n_bootstrap=500, seed=99)
    second = _bootstrap_statistics(values, n_bootstrap=500, seed=99)
    weekly = _bootstrap_statistics(values, n_bootstrap=500, block_days=5, seed=99)
    assert first == second
    assert first["n"] == len(values) == weekly["n"]
    assert first["mean_ci"][0] <= values.mean() <= first["mean_ci"][1]
    print("test_ml_v2_bootstrap_is_date_level_and_deterministic passed")


def test_trade_stats_has_no_annualisation():
    # mean/std with NO sqrt(anything). If someone reintroduces a scale factor
    # this pins it down numerically.
    r = [0.02, -0.01, 0.03, -0.02, 0.01]
    s = trade_stats(r)
    arr = np.array(r)
    assert s["n_trades"] == 5
    assert abs(s["mean_ret"] - arr.mean()) < 1e-12
    assert abs(s["ret_per_risk"] - arr.mean() / pd.Series(r).std()) < 1e-12
    assert abs(s["hit_rate"] - 0.6) < 1e-12
    print("test_trade_stats_has_no_annualisation passed")


def test_trade_stats_edges():
    assert trade_stats([])["n_trades"] == 0
    # zero variance must give NaN, never inf - an inf would rank top of any sort
    assert np.isnan(trade_stats([0.01, 0.01, 0.01])["ret_per_risk"])
    # base_rate is optional, but when given the edge must be computed
    s = trade_stats([0.01, -0.01, 0.01, 0.01], base_rate=0.5)
    assert abs(s["hit_edge"] - 0.25) < 1e-12
    assert np.isnan(trade_stats([0.01, -0.01])["hit_edge"]), "no base rate -> no edge claim"
    print("test_trade_stats_edges passed")


def test_signal_quality_scores_every_row_not_just_triggered():
    # The old sharpe_stats() computed its statistic over TRIGGERED rows only, so
    # every day the rule sat out vanished from the denominator. The signal half
    # must score all rows; the trade half only the triggered ones.
    pred = pd.Series([0.01, 0.02, -0.01, 0.006, 0.001])
    actual = pd.Series([0.02, -0.01, 0.03, 0.01, -0.05])
    q = signal_quality(pred, actual)
    assert q["n"] == 5, "signal half must see every row"
    assert q["n_trades"] == 3, "threshold >0.005 triggers indices 0,1,3"
    assert abs(q["base_rate"] - 0.6) < 1e-12, "3 of 5 actuals are positive"
    assert abs(q["trade_hit"] - (2 / 3)) < 1e-12
    assert "sharpe" not in q, "Sharpe must be gone, not renamed"
    print("test_signal_quality_scores_every_row_not_just_triggered passed")


def test_pattern_type_stats_use_same_date_baseline_and_balance_dates():
    # d1 has two signals while d2 has one. The headline date-balanced edge must
    # give the two dates equal weight rather than letting d1 vote twice.
    universe = pd.DataFrame({
        "signal_date": ["d1"] * 4 + ["d2"] * 4,
        "gross_ret": [0.10, 0.08, -0.10, -0.08,
                      0.10, -0.08, -0.09, -0.10],
    })
    signals = pd.DataFrame({
        "signal_date": ["d1", "d1", "d2"],
        "gross_ret": [0.10, 0.08, -0.08],
    })

    pooled = trade_level_stats(signals, universe, preset="moderate")
    balanced = date_balanced_hit_edge(signals, universe, preset="moderate")

    # Pooled: signal 2/3 versus universe 3/8.
    assert abs(pooled["base_rate"] - 0.375) < 1e-12
    assert abs(pooled["hit_edge"] - round((2 / 3) - (3 / 8), 4)) < 1e-12
    # Per date: d1 edge 1 - 1/2 = +1/2; d2 edge 0 - 1/4 = -1/4.
    assert balanced["n_signal_days"] == 2
    assert abs(balanced["daily_hit_edge"] - 0.125) < 1e-12
    assert balanced["positive_edge_days"] == 0.5
    print("test_pattern_type_stats_use_same_date_baseline_and_balance_dates passed")


def test_foreign_flow_stats_use_same_date_baseline_and_balance_dates():
    universe = [
        {"entry_date": "d1", "gross_return": r}
        for r in (0.10, 0.08, -0.10, -0.08)
    ] + [
        {"entry_date": "d2", "gross_return": r}
        for r in (0.10, -0.08, -0.09, -0.10)
    ]
    signals = [
        {"entry_date": "d1", "gross_return": 0.10},
        {"entry_date": "d1", "gross_return": 0.08},
        {"entry_date": "d2", "gross_return": -0.08},
    ]

    pooled = foreign_trade_stats(signals, universe, preset="moderate")
    balanced = foreign_date_balanced_hit_edge(signals, universe, preset="moderate")

    assert abs(pooled["base_rate"] - 0.375) < 1e-12
    assert abs(pooled["hit_edge"] - ((2 / 3) - (3 / 8))) < 1e-12
    assert balanced["n_signal_days"] == 2
    assert abs(balanced["daily_hit_edge"] - 0.125) < 1e-12
    assert balanced["positive_edge_days"] == 0.5
    print("test_foreign_flow_stats_use_same_date_baseline_and_balance_dates passed")


def test_regime_threshold_selection_uses_daily_edge_and_minimum_dates():
    search = pd.DataFrame([
        # Best point estimate is deliberately too sparse to be eligible.
        {"thresh": 0.01, "daily_hit_edge": 0.20, "hit_edge": 0.25,
         "n_signal_days": 5},
        # Of the supported variants, daily edge wins even though pooled edge loses.
        {"thresh": 0.02, "daily_hit_edge": 0.04, "hit_edge": 0.01,
         "n_signal_days": 25},
        {"thresh": 0.03, "daily_hit_edge": 0.03, "hit_edge": 0.50,
         "n_signal_days": 30},
    ])
    winner, reliable = select_threshold(search, min_signal_days=20)
    assert winner["thresh"] == 0.02
    assert reliable is True

    low_n_winner, reliable = select_threshold(search.iloc[[0]], min_signal_days=20)
    assert low_n_winner["thresh"] == 0.01
    assert reliable is False
    print("test_regime_threshold_selection_uses_daily_edge_and_minimum_dates passed")


def _sqrt_252_call_sites(source, filename):
    """Annualisation sites in one source string: any sqrt(...) whose argument
    contains the literal 252 ANYWHERE in its expression tree.

    Widened deliberately. The original matcher required the argument to be
    exactly Constant(252), so the "repaired" forms sqrt(252 / hold_days) and
    sqrt(n_per_year * 252) passed silently -- and those are the forms a
    well-meaning fix produces. HANDOFF.md TEMUAN 2 names TWO errors: the
    dimensional one (per-trade returns scaled as daily) and the independence
    one (concurrent cross-ticker trades counted as independent draws).
    Rescaling the constant addresses only the first while making the second
    invisible to this guard, which is worse than leaving it alone.

    Kept narrow on purpose:
      - only the argument of a call named `sqrt` is examined, so 252 appearing
        anywhere else in the file is not flagged;
      - the constant must be 252, so sqrt(5) / sqrt(10) z-score normalisations
        and sqrt(variance) are untouched;
      - AST-based, so prose about the defect in a docstring never registers as
        the defect.
    """
    import ast as _ast
    hits = []
    for node in _ast.walk(_ast.parse(source)):
        if not isinstance(node, _ast.Call) or len(node.args) != 1:
            continue
        fname = (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
        if fname != "sqrt":
            continue
        for sub in _ast.walk(node.args[0]):
            if (isinstance(sub, _ast.Constant) and not isinstance(sub.value, bool)
                    and sub.value == 252):
                hits.append(f"{filename}:{node.lineno}")
                break
    return hits

def test_sqrt_252_matcher_catches_repaired_forms():
    # The matcher itself, checked against the forms a naive fix produces.
    caught = [
        "s = r.mean() / r.std() * np.sqrt(252)",
        "s = r.mean() / r.std() * np.sqrt(252 / hold_days)",
        "s = r.mean() / r.std() * np.sqrt(hold_days / 252)",
        "s = r.mean() / r.std() * np.sqrt(n * 252)",
        "s = r.mean() / r.std() * math.sqrt(252.0)",
        "s = sqrt(252 * periods_per_day / overlap)",
    ]
    for src in caught:
        assert _sqrt_252_call_sites(src, "x.py"), f"matcher missed: {src}"
    ignored = [
        "z = f.rolling(5).sum() / (std20 * np.sqrt(5))",       # z-score normalisation
        "z = f.rolling(10).sum() / (std20 * np.sqrt(10))",
        "se = np.sqrt(on.var() / len(on) + off.var() / len(off))",
        "TRADING_DAYS = 252",                                   # bare constant, no sqrt
        "ann = (1 + r).prod() ** (252 / len(r)) - 1",           # CAGR, not a sqrt
        "d = np.sqrt(variance)",
    ]
    for src in ignored:
        assert not _sqrt_252_call_sites(src, "x.py"), f"matcher over-fired on: {src}"
    print("test_sqrt_252_matcher_catches_repaired_forms passed")

def test_no_sqrt_252_anywhere():
    # The defect this whole change removes. AST-based so prose about it in
    # docstrings does not count as a reoccurrence.
    import os as _os, subprocess as _subprocess
    here = _os.path.dirname(_os.path.abspath(__file__))
    listed = _subprocess.run(
        ["git", "ls-files", "--", "*.py"], cwd=here,
        capture_output=True, text=True, check=False,
    )
    files = listed.stdout.splitlines() if listed.returncode == 0 else [
        fn for fn in _os.listdir(here) if fn.endswith(".py")
    ]
    hits = []
    for fn in sorted(files):
        if fn == "check_ml_health.py":      # its own budget guard mentions it
            continue
        try:
            src = open(_os.path.join(here, fn), encoding="utf-8").read()
            hits += _sqrt_252_call_sites(src, fn)
        except (OSError, SyntaxError):
            continue
    assert not hits, f"sqrt(252) annualisation is back at {hits}"
    print("test_no_sqrt_252_anywhere passed")


def test_kelly_fraction_known_example():
    # textbook case: 60% win rate, 1:1 payoff -> full Kelly = 0.2
    f = kelly_fraction(win_rate=0.6, avg_win=1.0, avg_loss=1.0, fraction_cap=1.0)
    assert abs(f - 0.2) < 1e-9
    print("test_kelly_fraction_known_example passed")


def test_kelly_fraction_negative_edge_returns_zero():
    # losing edge (40% win rate, 1:1 payoff) must return exactly 0, not a
    # negative number — "bet negative" is meaningless for a long-only sizer
    f = kelly_fraction(win_rate=0.4, avg_win=1.0, avg_loss=1.0, fraction_cap=1.0)
    assert f == 0.0
    print("test_kelly_fraction_negative_edge_returns_zero passed")


def test_kelly_from_trades_matches_manual_calc():
    returns = [0.05, -0.02, 0.03, -0.01, 0.04, -0.02]
    f = kelly_from_trades(returns, fraction_cap=1.0)
    wins = [0.05, 0.03, 0.04]
    losses = [0.02, 0.01, 0.02]
    win_rate = 3 / 6
    payoff = (sum(wins) / 3) / (sum(losses) / 3)
    expected = win_rate - (1 - win_rate) / payoff
    assert abs(f - expected) < 1e-9
    print("test_kelly_from_trades_matches_manual_calc passed")


def test_forward_returns_never_bridge_a_removed_row():
    # The whole point of the gap guard. Quarantine removes d3, so d2's "next
    # row" is d4 - three days and a contamination-sized jump away. A plain
    # shift(-1) would report that as a real +354% next-day return and feed it
    # in as a training label.
    px = pd.DataFrame({
        "ticker": ["A"] * 4,
        "date": ["d1", "d2", "d4", "d5"],
        "close": [100.0, 110.0, 500.0, 505.0],
    })
    out = add_forward_returns(px, ["d1", "d2", "d3", "d4", "d5"], horizons=(1,))
    by_date = out.set_index("date")["fwd_1"]
    assert abs(by_date["d1"] - 0.10) < 1e-9, "contiguous row must still compute"
    assert np.isnan(by_date["d2"]), "d2->d4 spans the removed d3 and must be NaN"
    assert abs(by_date["d4"] - 0.01) < 1e-9, "contiguity resumes after the hole"
    assert np.isnan(by_date["d5"]), "last row has no next row"
    print("test_forward_returns_never_bridge_a_removed_row passed")


def test_lagged_returns_guarded_the_same_way():
    # Same hole, read backwards: d4's previous surviving row is d2, so a
    # momentum feature there would be fabricated too.
    px = pd.DataFrame({
        "ticker": ["A"] * 4,
        "date": ["d1", "d2", "d4", "d5"],
        "close": [100.0, 110.0, 500.0, 505.0],
    })
    out = add_lagged_returns(px, ["d1", "d2", "d3", "d4", "d5"], lags=(1,))
    by_date = out.set_index("date")["lag_1"]
    assert np.isnan(by_date["d1"]), "first row has no previous row"
    assert abs(by_date["d2"] - 0.10) < 1e-9
    assert np.isnan(by_date["d4"]), "d2->d4 spans the removed d3 and must be NaN"
    assert abs(by_date["d5"] - 0.01) < 1e-9
    print("test_lagged_returns_guarded_the_same_way passed")


def test_extreme_windows_share_the_contiguity_mask():
    # max_h / mdd_h roll over the same h rows the endpoint spans, so a window
    # that bridges a hole must be dropped, not just the endpoint return.
    px = pd.DataFrame({
        "ticker": ["A"] * 4,
        "date": ["d1", "d2", "d4", "d5"],
        "close": [100.0, 110.0, 500.0, 505.0],
        "high": [105.0, 115.0, 520.0, 515.0],
        "low": [95.0, 105.0, 480.0, 495.0],
    })
    out = add_forward_returns(px, ["d1", "d2", "d3", "d4", "d5"],
                              horizons=(1,), extremes=True)
    by_date = out.set_index("date")
    assert np.isnan(by_date.loc["d2", "max_1"]), "bridged window must be NaN"
    assert np.isnan(by_date.loc["d2", "mdd_1"]), "bridged window must be NaN"
    assert abs(by_date.loc["d1", "max_1"] - 0.15) < 1e-9   # high 115 vs close 100
    assert abs(by_date.loc["d1", "mdd_1"] - 0.05) < 1e-9   # low 105 vs close 100
    print("test_extreme_windows_share_the_contiguity_mask passed")


def test_one_day_targets_mask_corporate_actions_and_other_impossible_moves():
    px = pd.DataFrame({
        "ticker": ["A", "A", "A"],
        "date": ["d1", "d2", "d3"],
        "close": [2710.0, 534.0, 562.0],
        "high": [2770.0, 550.0, 566.0],
        "low": [2700.0, 532.0, 532.0],
    })
    out = add_forward_returns(px, ["d1", "d2", "d3"], horizons=(1,), extremes=True)
    by_date = out.set_index("date")
    assert np.isnan(by_date.loc["d1", "fwd_1"]), \
        "a stock split is not a tradeable -80% one-day model target"
    assert np.isnan(by_date.loc["d1", "max_1"])
    assert np.isnan(by_date.loc["d1", "mdd_1"])
    assert abs(by_date.loc["d2", "fwd_1"] - (562 / 534 - 1)) < 1e-12
    print("test_one_day_targets_mask_corporate_actions_and_other_impossible_moves passed")


def test_one_day_features_mask_corporate_actions_too():
    px = pd.DataFrame({
        "ticker": ["A", "A", "A"],
        "date": ["d1", "d2", "d3"],
        "close": [2710.0, 534.0, 562.0],
    })
    out = add_lagged_returns(px, ["d1", "d2", "d3"], lags=(1,))
    by_date = out.set_index("date")["lag_1"]
    assert np.isnan(by_date["d2"]), "the split must not become a -80% momentum/reward feature"
    assert abs(by_date["d3"] - (562 / 534 - 1)) < 1e-12
    print("test_one_day_features_mask_corporate_actions_too passed")


def test_multi_day_windows_cannot_cross_a_corporate_action():
    px = pd.DataFrame({
        "ticker": ["A"] * 6,
        "date": [f"d{i}" for i in range(1, 7)],
        "close": [2700.0, 2710.0, 534.0, 562.0, 570.0, 580.0],
        "high": [2710.0, 2770.0, 550.0, 566.0, 575.0, 585.0],
        "low": [2690.0, 2700.0, 532.0, 532.0, 565.0, 575.0],
    })
    dates = px["date"].tolist()
    fwd = add_forward_returns(px, dates, horizons=(3,), extremes=True).set_index("date")
    lag = add_lagged_returns(px, dates, lags=(3,)).set_index("date")
    assert np.isnan(fwd.loc["d1", "fwd_3"]), "3d target spans the d2->d3 split"
    assert np.isnan(fwd.loc["d1", "max_3"])
    assert np.isnan(lag.loc["d4", "lag_3"]), "3d momentum spans the d2->d3 split"
    assert not np.isnan(fwd.loc["d3", "fwd_3"]), "windows wholly after the split stay valid"
    print("test_multi_day_windows_cannot_cross_a_corporate_action passed")


def _contaminated_test_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE price_history (
        date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL
    )""")
    conn.execute("CREATE TABLE price_quarantine (date TEXT, ticker TEXT)")
    conn.execute("""CREATE TABLE broker_flow (
        date TEXT, ticker TEXT, broker_code TEXT, netval REAL
    )""")
    dates = [f"2026-01-{d:02d}" for d in range(1, 9)]
    closes = {"AAA": [100, 110, 1000, 500, 505, 510, 515, 520],
              "BBB": [200, 201, 202, 203, 204, 205, 206, 207]}
    for ticker, series in closes.items():
        for date, close in zip(dates, series):
            conn.execute("INSERT INTO price_history VALUES (?,?,?,?,?,?,?)",
                         (date, ticker, close, close, close, close, 1000))
            for code, netval in (("AK", 10.0), ("BK", -5.0), ("CC", 2.0)):
                conn.execute("INSERT INTO broker_flow VALUES (?,?,?,?)",
                             (date, ticker, code, netval))
    # Removing AAA d3 makes a naive shift join d2=110 to d4=500 (+354.5%).
    conn.execute("INSERT INTO price_quarantine VALUES (?,?)", (dates[2], "AAA"))
    conn.commit()
    return conn


def test_build_panel_cannot_recreate_impossible_target_returns():
    conn = _contaminated_test_db()
    panel = build_panel(conn)
    conn.close()
    aaa = panel[panel["ticker"] == "AAA"].set_index("date")
    assert "2026-01-02" not in aaa.index, \
        "the row before quarantine must lose its target, not bridge to the next clean bar"
    assert not ((panel["target"] > 0.35) | (panel["target"] < -0.15)).any(), \
        "an impossible ARA/ARB target reappeared in the production panel"
    print("test_build_panel_cannot_recreate_impossible_target_returns passed")


def test_strategy_simulator_refuses_to_hold_across_a_clean_panel_gap():
    px = pd.DataFrame({
        "ticker": ["AAA", "AAA", "AAA"],
        "date": ["d1", "d2", "d4"],
        "open": [100.0, 110.0, 500.0], "high": [100.0, 110.0, 500.0],
        "low": [100.0, 110.0, 500.0], "close": [100.0, 110.0, 500.0],
        "fwd_1": [0.10, np.nan, np.nan],
    })
    by_ticker, by_date = _index_price_history(px)
    ret = simulate_trade(by_ticker, by_date, "AAA", "d2", 1, None, None)
    assert ret is None, "a multi-day strategy must not treat d4 as the next bar after d2"
    print("test_strategy_simulator_refuses_to_hold_across_a_clean_panel_gap passed")


def _off_by_one_regression_fixture():
    """decision=d1(T), entry=d2(T+1), d3=T+2, d4=T+3.

    d2's high/low (120/90) breach a 10%-TP / 5%-SL threshold off entry_price
    105; d3's high/low (115/108) do NOT breach those same thresholds. That
    difference is deliberate: it is what makes a regression to the old
    off-by-one (which evaluated TP/SL against T+2 instead of T+1) fail loudly
    instead of silently passing for an unrelated reason.
    """
    px = pd.DataFrame({
        "ticker": ["AAA"] * 4,
        "date": ["d1", "d2", "d3", "d4"],
        "open":  [100.0, 105.0, 110.0, 130.0],
        "high":  [101.0, 120.0, 115.0, 140.0],
        "low":   [99.0, 90.0, 108.0, 125.0],
        "close": [100.0, 110.0, 130.0, 135.0],
        "fwd_1": [(110 - 100) / 100, (130 - 110) / 110, (135 - 130) / 130, np.nan],
    })
    return _index_price_history(px)


def test_hold_days_one_uses_entry_session_high_low_and_close():
    """Requirement 1: hold_days=1 with no TP/SL must exit at T+1's own close,
    computed off entry_price = open(T+1) = 105."""
    by_ticker, by_date = _off_by_one_regression_fixture()
    ret = simulate_trade(by_ticker, by_date, "AAA", "d1", 1, None, None)
    expected = (110.0 - 105.0) / 105.0  # close(T+1) vs open(T+1)
    assert ret is not None and abs(ret - expected) < 1e-9, (
        f"hold_days=1 must exit at T+1's close (expected {expected:+.6f}, got {ret}); "
        "the old off-by-one would evaluate T+2's close (130) instead"
    )
    print("test_hold_days_one_uses_entry_session_high_low_and_close passed")


def test_tp_and_sl_hit_on_entry_session_are_detected():
    """Requirement 2: a TP or SL breach on T+1 itself must fire immediately.

    T+1's high=120 clears a 10% TP off entry_price=105 (threshold 115.5); T+2's
    high=115 does NOT clear that same threshold. If the old off-by-one bug
    (checking T+2 instead of T+1) were reintroduced, this TP would silently
    fail to trigger and the trade would fall through to a timed exit instead.
    """
    by_ticker, by_date = _off_by_one_regression_fixture()
    tp_ret = simulate_trade(by_ticker, by_date, "AAA", "d1", 1, 0.10, None)
    assert tp_ret == 0.10, (
        f"TP must fire off T+1's high=120 (>= 115.5 threshold), got {tp_ret}"
    )

    # T+1's low=90 clears a 5% SL off entry_price=105 (threshold 99.75); T+2's
    # low=108 does NOT.
    sl_ret = simulate_trade(by_ticker, by_date, "AAA", "d1", 1, None, 0.05)
    assert sl_ret == -0.05, (
        f"SL must fire off T+1's low=90 (<= 99.75 threshold), got {sl_ret}"
    )
    print("test_tp_and_sl_hit_on_entry_session_are_detected passed")


def test_one_day_hold_never_touches_t_plus_2():
    """Requirement 3: for hold_days=1, T+2 (d3) must never be read at all.

    d3's close (130) and OHLC are deliberately far from d2's, so any
    contamination from reading d3 instead of d2 is impossible to miss.
    """
    by_ticker, by_date = _off_by_one_regression_fixture()
    ret = simulate_trade(by_ticker, by_date, "AAA", "d1", 1, None, None)
    t_plus_2_based = (130.0 - 105.0) / 105.0
    assert abs(ret - t_plus_2_based) > 1e-6, (
        "hold_days=1 result matches a T+2-close calculation — T+2 is being "
        "read for a one-day hold"
    )
    print("test_one_day_hold_never_touches_t_plus_2 passed")


def test_hold_days_two_expires_at_close_t_plus_2():
    """Requirement 4: hold_days=2 with no TP/SL must run through T+1 AND T+2,
    timed-exiting at T+2's close — not T+1's (one session too short) and not
    forced by running out of data (d4/T+3 exists precisely so the exit is a
    genuine k==hold_days-1 timed exit, not an end-of-panel fallback)."""
    by_ticker, by_date = _off_by_one_regression_fixture()
    ret = simulate_trade(by_ticker, by_date, "AAA", "d1", 2, None, None)
    expected = (130.0 - 105.0) / 105.0  # close(T+2) vs open(T+1)
    assert ret is not None and abs(ret - expected) < 1e-9, (
        f"hold_days=2 must exit at T+2's close (expected {expected:+.6f}, got {ret})"
    )
    print("test_hold_days_two_expires_at_close_t_plus_2 passed")


def test_all_tracked_model_price_consumers_use_clean_panel():
    # The low-level audit/check/scrape modules may inspect raw storage, but a
    # model, strategy, or report must not source its target/simulation from it.
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    consumers = [
        "walk_forward_backtest.py", "ddqn_entry_exit.py", "feature_ablation.py",
        "multiday_features.py", "smart_money_divergence.py", "strategy_variants.py",
        "ara_arb_simulation.py", "run_ml_reports.py", "horizon_scan.py",
    ]
    offenders = []
    for fn in consumers:
        source = open(_os.path.join(here, fn), encoding="utf-8").read()
        if "FROM price_history" in source:
            offenders.append(fn)
    assert not offenders, f"raw contaminated price reads remain in {offenders}"
    print("test_all_tracked_model_price_consumers_use_clean_panel passed")



def test_series_signature_distinguishes_two_stocks():
    # The scraper's last-line-of-defence guard. Two different stocks must never
    # produce the same signature; the same series must always produce the same
    # one, or the stale-chart check either never fires or fires constantly.
    cdia = {"x": ["2026-01-01", "2026-01-02"], "close": [700.0, 715.0]}
    coin = {"x": ["2026-01-01", "2026-01-02"], "close": [2560.0, 2600.0]}
    assert series_signature(cdia) == series_signature(dict(cdia))
    assert series_signature(cdia) != series_signature(coin)
    # A stale chart hands back the PREVIOUS ticker's series verbatim - which is
    # exactly the equality the guard trips on.
    assert series_signature(cdia) == series_signature({"x": cdia["x"], "close": cdia["close"]})
    print("test_series_signature_distinguishes_two_stocks passed")


def test_series_signature_none_when_empty():
    # No series means "nothing to compare", not "identical to the last one" -
    # returning a truthy constant here would abort every empty scrape as a
    # duplicate.
    assert series_signature(None) is None
    assert series_signature({}) is None
    assert series_signature({"x": [], "close": []}) is None
    print("test_series_signature_none_when_empty passed")


def test_ticker_from_title_only_asserts_when_it_can():
    # Present and authoritative.
    assert ticker_from_title("CDIA - Chandra Daya Investasi") == "CDIA"
    assert ticker_from_title("Inventory chart: COIN") == "COIN"
    # Absent must be None, NOT a guess: the title format is not guaranteed, and
    # treating "no code found" as a mismatch would abort every valid scrape.
    assert ticker_from_title("") is None
    assert ticker_from_title(None) is None
    assert ticker_from_title("Broker inventory") is None
    # Lowercase and wrong-length words are not codes.
    assert ticker_from_title("cdia daily chart") is None
    assert ticker_from_title("PT ABC") is None
    print("test_ticker_from_title_only_asserts_when_it_can passed")



def _price_frame(rows):
    """Minimal price_history-shaped frame for the detectors."""
    df = pd.DataFrame(rows, columns=["date", "ticker", "open", "high", "low",
                                     "close", "volume"])
    df.insert(0, "rid", range(1, len(df) + 1))
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


def _steady(ticker, start, days, price):
    # A flat, unremarkable series — no detector should ever flag these.
    return [(f"2026-08-{start + i:02d}", ticker, price, price, price, price, 1000)
            for i in range(days)]


def test_commit_gate_ignores_legitimate_volatility():
    # The gate the topup workflow runs counts cross_ticker_dup ONLY. A correct
    # scrape adds real IDX volatility — a +30% ARA day, a corporate action — that
    # trips limit_violation (and, on a big enough jump, series_break). Those must
    # NOT count against the gate, or every honest nightly scrape reddens it and
    # price_history stops advancing. This is the bug that froze it at 2026-08-21.
    base = _price_frame(_steady("AAA", 1, 6, 1000))
    d0 = detect(base)

    # Same series plus one legitimate +30% jump on a fresh day (prev close 1000 ->
    # ara bound 25%, so this is outside the band and trips limit_violation).
    after = _price_frame(_steady("AAA", 1, 6, 1000) + [("2026-08-07", "AAA",
                         1300, 1300, 1300, 1300, 1000)])
    d1 = detect(after)

    assert int(d1["limit_violation"].sum()) > int(d0["limit_violation"].sum()), \
        "a +30% day should register as a limit_violation"
    # ...but the metric the gate actually reads did not move.
    assert int(d1["cross_ticker_dup"].sum()) == int(d0["cross_ticker_dup"].sum()) == 0, \
        "legitimate volatility must not raise the cross-ticker-dup gate"
    print("test_commit_gate_ignores_legitimate_volatility passed")


def test_commit_gate_catches_a_recontaminated_scrape():
    # The regression the gate exists for: the scraper writes one ticker's OHLCV
    # under another's name. Two different real stocks cannot share byte-identical
    # open/high/low/close/volume, so this is exactly what cross_ticker_dup means,
    # and the gate must see it rise.
    clean = _price_frame(_steady("AAA", 1, 4, 1000) + _steady("BBB", 1, 4, 500))
    d0 = detect(clean)
    assert int(d0["cross_ticker_dup"].sum()) == 0

    # BBB's 08-04 row now carries AAA's exact OHLCV — the contamination signature.
    contaminated = _price_frame(
        _steady("AAA", 1, 4, 1000)
        + _steady("BBB", 1, 3, 500)
        + [("2026-08-04", "BBB", 1000, 1000, 1000, 1000, 1000)])
    d1 = detect(contaminated)
    assert int(d1["cross_ticker_dup"].sum()) > 0, \
        "identical OHLCV under two tickers on one date must raise cross_ticker_dup"
    print("test_commit_gate_catches_a_recontaminated_scrape passed")


def _inventory_payload(nlot, nval, dates=None):
    # Shape confirmed against a real /api/inventory response (HANDOFF Appendix N).
    data = {"nlot": nlot, "nval": nval}
    if dates is not None:
        data["date"] = dates
    return {"success": True, "data": data,
            "meta": {"symbol": "SINI"}}



def test_predictions_carry_the_columns_the_scorer_reads():
    # run_ml_reports.py died with KeyError: 'target' every night for a week
    # (2026-08-23 onward). score_all() started computing the base rate from
    # preds["target"], but get_walk_forward_predictions() returned only
    # ticker/date/pred. Nothing caught it: check_ml_health imports the module
    # and runs these tests, neither of which executed the report path.
    rng = np.random.default_rng(0)
    # 60 dates, not 40: under the executable contract a fold must clear TWO
    # realization purges plus MIN_FIT_DAYS, so a 40-date panel yields exactly
    # one candidate fold and it is (correctly) rejected as too thin. The test
    # is about which COLUMNS survive, so give it a panel long enough to
    # actually produce folds rather than loosening the split guard for it.
    dates = [f"2026-{m:02d}-{d:02d}" for m in (1, 2, 3) for d in range(1, 21)][:60]
    rows = []
    for d in dates:
        for t in ("AAA", "BBB", "CCC"):
            row = {"date": d, "ticker": t}
            for f in FEATURES:
                row[f] = float(rng.normal())
            row["target"] = float(rng.normal(0, 0.02))
            rows.append(row)
    panel = pd.DataFrame(rows)

    preds = get_walk_forward_predictions(panel)
    assert len(preds), "the synthetic panel should span enough dates to produce folds"
    for col in ("ticker", "date", "pred", "target"):
        assert col in preds.columns, f"score_all() reads {col!r}; it must survive"
    # The exact expression that used to raise.
    base_rate = float((preds["target"] > 0).mean())
    assert 0.0 <= base_rate <= 1.0
    print("test_predictions_carry_the_columns_the_scorer_reads passed")


def test_date_offset_only_holds_before_the_open():
    # The invariant every date join rests on (Appendix E) is "scrape date - 1 ==
    # data date", and it is true ONLY because the scheduled run beats the open.
    # The screener serves the last COMPLETED session, so after the open it hands
    # back today's close instead.
    from datetime import datetime as _dt
    sched = _dt(2026, 8, 27, 7, 0)            # 07:00, the cron's own slot
    assert date_offset_holds(sched)
    assert date_offset_holds(_dt(2026, 8, 27, 0, 30))
    assert date_offset_holds(_dt(2026, 8, 27, 9, 59))

    # 22:09 local == the 21:09 WIB manual dispatch that overwrote 2026-08-27's
    # correctly-offset rows with same-day closes and dropped cross-source
    # agreement from 100% to 85%.
    assert not date_offset_holds(_dt(2026, 8, 27, 22, 9))
    # IDX opens 09:00 WIB (UTC+7) == 10:00 in the UTC+8 scrape timezone.
    assert not date_offset_holds(_dt(2026, 8, 27, 10, 0)), "the open itself is already unsafe"
    assert not date_offset_holds(_dt(2026, 8, 27, 16, 30))
    print("test_date_offset_only_holds_before_the_open passed")


def test_quarantined_row_is_not_a_baseline_for_the_next_row():
    # The 2026-08-27 false alarm: MDIA's 08-13 close was KIOS's price (95), was
    # flagged and quarantined weeks earlier, and its real price is ~250. The next
    # clean row then read as +171% and got reported as the scraper "writing bad
    # rows again". A known-bad close must never be the baseline a good row is
    # judged against.
    rows = [(d, "AAA", 250, 250, 250, 250, 1000)
            for d in ("2026-08-01", "2026-08-02", "2026-08-03")]
    rows.append(("2026-08-04", "AAA", 95, 95, 95, 95, 1000))    # contaminated
    rows.append(("2026-08-05", "AAA", 252, 252, 252, 252, 1000))  # clean
    px = _price_frame(rows)

    naive = detect(px)
    bad = naive[naive["date"] == "2026-08-05"].iloc[0]
    assert bool(bad["limit_violation"]), "95 -> 252 must look like a violation untrusted"

    # Same data, with the contaminated row marked untrusted.
    trusted = [d != "2026-08-04" for d in px["date"]]
    guarded = detect(px, trusted=trusted)
    row = guarded[guarded["date"] == "2026-08-05"].iloc[0]
    assert not bool(row["limit_violation"]), \
        "a clean row after a quarantined one must not be flagged"
    # The baseline is DROPPED, not bridged to the last good close: a multi-day
    # move cannot be judged against a one-day ARA/ARB band either.
    assert pd.isna(row["prev_close"])
    print("test_quarantined_row_is_not_a_baseline_for_the_next_row passed")


def test_authoritative_panel_identifies_only_the_cloned_duplicate():
    px = pd.DataFrame([
        {"rid": 1, "date": "2026-01-02", "ticker": "AAA",
         "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1234},
        {"rid": 2, "date": "2026-01-02", "ticker": "BBB",
         "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1234},
    ])
    source = px.loc[px["ticker"] == "AAA", ["date", "ticker", *OHLCV]].copy()
    targets = authoritative_duplicate_deletions(px, source)
    assert targets[["date", "ticker"]].to_dict("records") == [
        {"date": "2026-01-02", "ticker": "BBB"}
    ]

    # If the source cannot uniquely identify the legitimate member, no partial
    # deletion is allowed.
    try:
        authoritative_duplicate_deletions(px, source.iloc[0:0])
    except RuntimeError as exc:
        assert "exactly one authoritative member" in str(exc)
    else:
        raise AssertionError("ambiguous collision group was not rejected")
    print("test_authoritative_panel_identifies_only_the_cloned_duplicate passed")


def test_quarantine_refresh_removes_healed_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE price_history (
        date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, PRIMARY KEY(date,ticker))""")
    duplicate = ("2026-01-02", 100, 110, 95, 105, 1234)
    conn.executemany(
        "INSERT INTO price_history VALUES (?,?,?,?,?,?,?)",
        [(duplicate[0], "AAA", *duplicate[1:]),
         (duplicate[0], "BBB", *duplicate[1:])],
    )
    cmd_quarantine(conn)
    assert conn.execute("SELECT COUNT(*) FROM price_quarantine").fetchone()[0] == 2

    conn.execute(
        "UPDATE price_history SET close=101, volume=4321 WHERE ticker='BBB'"
    )
    cmd_quarantine(conn)
    assert conn.execute("SELECT COUNT(*) FROM price_quarantine").fetchone()[0] == 0
    conn.close()
    print("test_quarantine_refresh_removes_healed_rows passed")


def test_trusted_mask_leaves_real_contamination_detectable():
    # The mask must only relax the limit_violation BASELINE. cross_ticker_dup is
    # what actually proves the scraper regressed, and it must survive untouched —
    # otherwise this fix would blind the check it is meant to keep credible.
    rows = [("2026-08-01", "AAA", 100, 100, 100, 100, 500),
            ("2026-08-02", "AAA", 100, 100, 100, 100, 500),
            ("2026-08-01", "BBB", 100, 100, 100, 100, 500),
            ("2026-08-02", "BBB", 100, 100, 100, 100, 500)]
    px = _price_frame(rows)
    trusted = [False] * len(px)           # even with everything distrusted
    guarded = detect(px, trusted=trusted)
    assert int(guarded["cross_ticker_dup"].sum()) == 4, \
        "identical OHLCV across tickers must still be caught"
    assert bool(guarded["suspect"].any())
    # And the default path is unchanged for every existing caller.
    assert int(detect(px)["cross_ticker_dup"].sum()) == 4
    print("test_trusted_mask_leaves_real_contamination_detectable passed")


def test_bagholders_sum_per_day_lots_not_last_value():
    # nlot is PER-DAY net lot, not a cumulative series, so the position is its
    # sum. Reading the last element (what a cumulative series would need) would
    # report 10 lot for AK here instead of 300.
    payload = _inventory_payload(
        nlot={"AK": [100, 200, 10], "BK": [50, 25, 25]},
        nval={"AK": [1e6, 2e6, 1e5], "BK": [5e5, 2.5e5, 2.5e5]},
    )
    holders = bagholders_from_payload(payload, n=2)
    assert [h["code"] for h in holders] == ["AK", "BK"], "must rank by cumulative net lot"
    assert holders[0]["cum"] == 310
    assert holders[1]["cum"] == 100
    # avg = sum(nval) / (sum(nlot) * 100 shares) = 3.1e6 / 31000
    assert abs(holders[0]["avg"] - 100.0) < 1e-9
    print("test_bagholders_sum_per_day_lots_not_last_value passed")


def test_bagholders_exclude_net_sellers():
    # The term means ACCUMULATOR. The retired implementation sorted by cumulative
    # net and took the top n unconditionally, so on a ticker everyone was dumping
    # it would print a net seller as the "bag holder".
    payload = _inventory_payload(
        nlot={"AK": [-500, -200], "BK": [-10, -5], "CC": [40, 60]},
        nval={"AK": [-5e6, -2e6], "BK": [-1e5, -5e4], "CC": [4e5, 6e5]},
    )
    holders = bagholders_from_payload(payload, n=2)
    assert [h["code"] for h in holders] == ["CC"], "only net accumulators qualify"
    # Everyone selling must yield an EMPTY list, not a least-bad seller.
    all_selling = _inventory_payload(nlot={"AK": [-5], "BK": [-1]},
                                     nval={"AK": [-5e4], "BK": [-1e4]})
    assert bagholders_from_payload(all_selling, n=2) == []
    print("test_bagholders_exclude_net_sellers passed")


def test_bagholders_survive_a_malformed_payload():
    # A retired endpoint / changed schema must degrade to "no holders", never
    # raise into the daily signal. None entries appear in real series.
    assert bagholders_from_payload(None) == []
    assert bagholders_from_payload({}) == []
    assert bagholders_from_payload({"success": True, "data": {}}) == []
    holed = _inventory_payload(nlot={"AK": [100, None, 50]}, nval={"AK": [1e6, None, 5e5]})
    assert bagholders_from_payload(holed)[0]["cum"] == 150
    # A broker present in nlot but absent from nval must not divide by nothing.
    no_val = _inventory_payload(nlot={"AK": [100]}, nval={})
    assert no_val and bagholders_from_payload(no_val)[0]["avg"] == 0
    print("test_bagholders_survive_a_malformed_payload passed")


def test_observable_inventory_uses_three_exact_twenty_session_blocks():
    dates = [f"d{i:03d}" for i in range(1, 76)]
    discovery = _inventory_payload({}, {}, dates=dates)
    blocks = inventory_date_blocks(discovery, trading_days=60, block_days=20)
    assert blocks == [("d016", "d035"), ("d036", "d055"), ("d056", "d075")]
    print("test_observable_inventory_uses_three_exact_twenty_session_blocks passed")


def test_old_accumulator_remains_visible_across_sixty_session_inventory():
    # AK accumulated in the oldest block and then disappeared from the recent
    # top lists. A single recency-selected C20 request would miss it entirely.
    blocks = [
        _inventory_payload({"AK": [100] * 20}, {"AK": [1e6] * 20},
                           dates=[f"d{i:02d}" for i in range(1, 21)]),
        _inventory_payload({"BK": [20] * 20}, {"BK": [2e5] * 20},
                           dates=[f"d{i:02d}" for i in range(21, 41)]),
        _inventory_payload({"BK": [20] * 20}, {"BK": [2e5] * 20},
                           dates=[f"d{i:02d}" for i in range(41, 61)]),
    ]
    holders = bagholders_from_payloads(blocks, n=2)
    assert [h["code"] for h in holders] == ["AK", "BK"]
    assert holders[0]["cum"] == 2000
    assert holders[0]["observed_trading_days"] == 60
    print("test_old_accumulator_remains_visible_across_sixty_session_inventory passed")


def test_should_fail_run_tolerates_a_few_failures():
    # A handful of tickers legitimately fail (suspended, no chart). Reddening the
    # workflow for those would train everyone to ignore it.
    assert should_fail_run(0, 45) is False
    assert should_fail_run(5, 45) is False       # 11%
    assert should_fail_run(13, 45) is False      # 29%, just under the 30% limit
    print("test_should_fail_run_tolerates_a_few_failures passed")


def test_should_fail_run_catches_a_broken_scrape():
    # The case this exists for: every ticker failed, price_history is unchanged,
    # the contamination gate sees no growth and passes, and nothing is committed.
    # Without this the run would go GREEN having done nothing at all.
    assert should_fail_run(45, 45) is True
    assert should_fail_run(14, 45) is True       # 31%, just over
    assert should_fail_run(0, 0) is True         # nothing attempted is a failure
    print("test_should_fail_run_catches_a_broken_scrape passed")


if __name__ == "__main__":
    test_commit_gate_ignores_legitimate_volatility()
    test_commit_gate_catches_a_recontaminated_scrape()
    test_predictions_carry_the_columns_the_scorer_reads()
    test_date_offset_only_holds_before_the_open()
    test_quarantined_row_is_not_a_baseline_for_the_next_row()
    test_authoritative_panel_identifies_only_the_cloned_duplicate()
    test_quarantine_refresh_removes_healed_rows()
    test_trusted_mask_leaves_real_contamination_detectable()
    test_bagholders_sum_per_day_lots_not_last_value()
    test_bagholders_exclude_net_sellers()
    test_bagholders_survive_a_malformed_payload()
    test_observable_inventory_uses_three_exact_twenty_session_blocks()
    test_old_accumulator_remains_visible_across_sixty_session_inventory()
    test_should_fail_run_tolerates_a_few_failures()
    test_should_fail_run_catches_a_broken_scrape()
    test_series_signature_distinguishes_two_stocks()
    test_series_signature_none_when_empty()
    test_ticker_from_title_only_asserts_when_it_can()
    test_forward_returns_never_bridge_a_removed_row()
    test_lagged_returns_guarded_the_same_way()
    test_extreme_windows_share_the_contiguity_mask()
    test_one_day_targets_mask_corporate_actions_and_other_impossible_moves()
    test_one_day_features_mask_corporate_actions_too()
    test_multi_day_windows_cannot_cross_a_corporate_action()
    test_build_panel_cannot_recreate_impossible_target_returns()
    test_strategy_simulator_refuses_to_hold_across_a_clean_panel_gap()
    test_hold_days_one_uses_entry_session_high_low_and_close()
    test_tp_and_sl_hit_on_entry_session_are_detected()
    test_one_day_hold_never_touches_t_plus_2()
    test_hold_days_two_expires_at_close_t_plus_2()
    test_all_tracked_model_price_consumers_use_clean_panel()
    test_broker_day_aggregates_basic()
    test_broker_correlation_first_day_is_nan()
    test_price_features_no_leakage()
    # These 8 were defined but never wired into this runner -- found while
    # adding the PR #36 corrupt-close regression tests below and fixed
    # alongside them; test_pipeline.py's "all tests pass" never actually
    # exercised the open-anchor contract, the realization purges, the
    # thin/infeasible fold gate, or the frozen legacy digest until now.
    test_open_anchored_labels_match_hand_computed_values()
    test_multiplicative_composition_not_additive()
    test_invalid_open_anchor_yields_nan_even_when_close_passes()
    test_open_outside_high_low_or_nonpositive_is_invalid()
    test_corrupt_close_t_plus_1_invalidates_fwd_oc_1()
    test_corrupt_close_t_plus_1_invalidates_fwd_oo_1_even_when_both_opens_pass()
    test_corrupt_close_after_oo_exit_does_not_invalidate_fwd_oo_1()
    test_close_window_validity_fix_holds_for_h_greater_than_1()
    test_both_realization_purges_hold_on_timestamps()
    test_thin_and_infeasible_folds_are_skipped_and_counted()
    test_legacy_experiment1_digest_is_frozen()
    test_target_refuses_to_silently_use_the_close_contract()
    test_spearman_ic_direction()
    test_signal_stats_detects_a_useless_signal()
    test_signal_stats_reports_a_negative_edge_as_negative()
    test_signal_stats_ranks_cross_sectionally_each_day()
    test_ml_v2_xgboost_seed_is_locked()
    test_ml_v2_feature_sets_keep_the_same_price_controls()
    test_broker_identity_flows_and_observable_inventory_use_net_lots()
    test_ml_v2_walk_forward_splits_are_strictly_chronological()
    test_ml_v2_robustness_pairs_predictions_within_date()
    test_ml_v2_bootstrap_is_date_level_and_deterministic()
    test_trade_stats_has_no_annualisation()
    test_trade_stats_edges()
    test_signal_quality_scores_every_row_not_just_triggered()
    test_pattern_type_stats_use_same_date_baseline_and_balance_dates()
    test_foreign_flow_stats_use_same_date_baseline_and_balance_dates()
    test_regime_threshold_selection_uses_daily_edge_and_minimum_dates()
    test_sqrt_252_matcher_catches_repaired_forms()
    test_no_sqrt_252_anywhere()
    test_kelly_fraction_known_example()
    test_kelly_fraction_negative_edge_returns_zero()
    test_kelly_from_trades_matches_manual_calc()
    print("\nAll tests passed.")
