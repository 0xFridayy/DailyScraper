"""Offline evidence acceptance/adversarial tests. Run python test_inventory_evidence.py.

No live collection. Pure kernel cases need only the standard library. The
targeted reader regression uses the repository's existing synthetic FakeVendor.
"""

import copy
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from decimal import localcontext, DefaultContext
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import inventory_evidence as ie
import targeted_actor_db as tdb

SCOPE = ie.Scope("A", "REGULAR", "TARGETED_SELECTOR_UNION", ie.MEASUREMENT_CONTRACT, "reported-lots-v1")
EARLY = "2026-10-02T10:00:00Z"
LATE = "2026-10-03T10:00:00Z"


def axis_n(n, start="2026-09-01"):
    d = date.fromisoformat(start)
    while len(ie.idx_session_axis(start, d.isoformat()).sessions) < n:
        d += timedelta(days=1)
    return ie.idx_session_axis(start, d.isoformat())


def capture(name="capture-a", accepted=EARLY, returned=("ES", "CC"), response=EARLY):
    return ie.Capture("neobdm:/api/inventory", name, response, accepted,
                      hashlib.sha256(name.encode()).hexdigest(),
                      request_parameters=(("symbol", "SINI"), ("investor_type", "A")),
                      requested_selectors=("TOP_5_NB_LOT_C20",), returned_brokers=returned)


def row(d, net=10, broker="ES", scope=SCOPE, cap=None, revision=1, **changes):
    values = dict(buy_lots=max(net, 0), sell_lots=max(-net, 0), net_lots=net,
                  buy_value_rp=max(net, 0) * 100_000, sell_value_rp=max(-net, 0) * 100_000,
                  net_value_rp=net * 100_000)
    values.update(changes)
    return ie.Observation("SINI", broker, d, revision, scope, cap or capture(), **values)


def build(rows, axis=None, codes=("ES",), windows=(5,), **kwargs):
    axis = axis or axis_n(5)
    kwargs.setdefault("compatibility_scopes", (SCOPE,))
    return ie.build_inventory_evidence(rows, ticker="SINI", broker_codes=codes, axis=axis,
                                      windows=windows, availability_cutoff=kwargs.pop("availability_cutoff", LATE),
                                      **kwargs)


def series(doc, broker="ES"):
    return doc["brokers"][broker]["series"]


def metric(doc, name, n=5, broker="ES"):
    return doc["brokers"][broker]["measurements"][str(n)][name]


def market_rows(axis, volume=100_000, unit="shares", **changes):
    return [ie.MarketObservation("SINI", d, capture("market"), 1000 + i * 10,
                                 volume, unit, SCOPE.basis_version, SCOPE.market_scope, **changes)
            for i, d in enumerate(axis.sessions)]


def keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child)


FORBIDDEN = {"actual_cost_basis", "actual_holding_duration", "actual_position_age",
             "actual_fraction_distributed", "actual_holder_pnl", "actual_holdings",
             "full_market_concentration", "full_market_rank", "market_concentration",
             "beneficial_owner", "controller", "insider", "driver", "proxy", "smart_money",
             "actor_identity", "owner", "person", "healthy_rotation"}


class KernelTests(unittest.TestCase):
    def test_explicit_zero_and_omitted_broker_are_distinct(self):
        axis = axis_n(5)
        doc = build([row(d, 0) for d in axis.sessions], axis, codes=("ES", "ZZ"))
        self.assertEqual([r["coverage"] for r in series(doc)], [ie.OBSERVED_ZERO] * 5)
        self.assertEqual([r["raw"]["net_lots"] for r in series(doc)], [0] * 5)
        self.assertEqual([r["coverage"] for r in series(doc, "ZZ")], [ie.UNOBSERVED] * 5)
        self.assertTrue(all(r["raw"] is None and r["segment_id"] is None for r in series(doc, "ZZ")))
        self.assertIsNone(metric(doc, "net_flow_slope", broker="ZZ")["value"])

    def test_offsetting_zero_net_is_observed_activity(self):
        axis = axis_n(1)
        doc = build([row(axis.sessions[0], 0, buy_lots=5, sell_lots=5,
                         buy_value_rp=500_000, sell_value_rp=500_000)], axis, windows=(1,))
        self.assertEqual(series(doc)[0]["coverage"], ie.OBSERVED_NONZERO)
        self.assertEqual(metric(doc, "two_sided_broker_activity_lots", 1)["value"], 10)

    def test_hole_invalidates_original_anchor_and_local_opening_is_unknown(self):
        axis = axis_n(5)
        doc = build([row(d, n) for d, n in zip(axis.sessions, [100, 30, None, -20, 40]) if n is not None], axis)
        rows = series(doc)
        self.assertEqual([r["cumulative_observable_lots"] for r in rows], [100, 130, None, None, None])
        self.assertEqual([r["cumulative_observable_value"] for r in rows], [10_000_000, 13_000_000, None, None, None])
        self.assertEqual([r["segment_cumulative_net_lots"] for r in rows], [100, 130, None, -20, 20])
        self.assertEqual(rows[2]["coverage"], ie.UNOBSERVED)
        self.assertEqual(rows[2]["null_reason"], "MISSING_EXPECTED_SESSION")
        self.assertEqual(rows[3]["segment_start"], axis.sessions[3])
        self.assertNotEqual(rows[0]["segment_id"], rows[3]["segment_id"])
        self.assertTrue(all(r["opening_position_lots"] is None and r["left_censored"] for r in rows))

    def test_missing_broker_coverage_breaks_even_when_other_broker_is_observed(self):
        axis = axis_n(3)
        doc = build([row(axis.sessions[0]), row(axis.sessions[1], broker="CC"), row(axis.sessions[2])],
                    axis, windows=(3,))
        self.assertEqual(series(doc)[1]["null_reason"], "MISSING_BROKER_COVERAGE")
        self.assertIsNone(series(doc)[2]["cumulative_observable_lots"])

    def test_complete_post_hole_window_is_local_and_original_anchor_stays_null(self):
        axis = axis_n(8)
        doc = build([row(d) for i, d in enumerate(axis.sessions) if i != 2], axis)
        self.assertEqual(metric(doc, "net_flow_slope")["value"], 10)
        self.assertEqual(metric(doc, "net_flow_slope")["status"], "CALCULATED")
        self.assertEqual(series(doc)[-1]["segment_cumulative_net_lots"], 50)
        self.assertIsNone(series(doc)[-1]["cumulative_observable_lots"])
        self.assertIsNone(series(doc)[-1]["opening_position_lots"])

    def test_weekend_is_not_a_hole(self):
        axis = ie.idx_session_axis("2026-09-04", "2026-09-07")
        self.assertEqual(axis.sessions, ("2026-09-04", "2026-09-07"))
        doc = build([row(d, 10) for d in axis.sessions], axis, windows=(2,))
        self.assertEqual([r["cumulative_observable_lots"] for r in series(doc)], [10, 20])
        self.assertEqual(metric(doc, "net_flow_slope", 2)["value"], 10)

    def test_verified_holiday_is_not_a_hole(self):
        axis = ie.idx_session_axis("2026-08-14", "2026-08-18")
        self.assertEqual(axis.sessions, ("2026-08-14", "2026-08-18"))
        doc = build([row(d, 10) for d in axis.sessions], axis, windows=(2,))
        self.assertEqual(series(doc)[-1]["cumulative_observable_lots"], 20)

    def test_unsupported_calendar_preserves_raw_without_invented_continuity(self):
        axis = ie.idx_session_axis("2025-10-01", "2025-10-03")
        doc = build([row("2025-10-01"), row("2025-10-03")], axis, windows=(2,))
        self.assertEqual(doc["axis"]["status"], "UNSUPPORTED")
        self.assertEqual(doc["axis"]["sessions"], [])
        self.assertEqual([r["segment_cumulative_net_lots"] for r in series(doc)], [10, 10])
        self.assertTrue(all(r["cumulative_observable_lots"] is None for r in series(doc)))
        self.assertEqual(metric(doc, "net_flow_slope", 2)["null_reason"], "CALENDAR_UNSUPPORTED")

    def test_forged_verified_axis_and_non_session_row_are_refused(self):
        with self.assertRaises(ValueError):
            ie.SessionAxis("2026-09-01", "2026-09-03", ("2026-09-01", "2026-09-03"),
                           "VERIFIED", ie.idx_calendar.CALENDAR_VERSION)
        with self.assertRaises(ValueError):
            build([row("2026-09-05")], ie.idx_session_axis("2026-09-04", "2026-09-07"))

    def test_each_incompatible_scope_component_breaks_the_segment(self):
        axis = axis_n(2)
        for field, value in (("capture_scope", "EXPLICIT_FOLLOWUP"), ("basis_version", "basis-v2"),
                             ("investor_type", "foreign"), ("market_scope", "ALL_MARKETS")):
            with self.subTest(field=field):
                next_scope = replace(SCOPE, **{field: value})
                next_capture = capture("scope-change-" + field)
                if field == "capture_scope":
                    next_capture = replace(next_capture, requested_selectors=(), requested_brokers=("ES",))
                if field == "investor_type":
                    next_capture = replace(next_capture, request_parameters=(("symbol", "SINI"), ("investor_type", value)))
                doc = build([row(axis.sessions[0]), row(axis.sessions[1], scope=next_scope, cap=next_capture)],
                            axis, windows=(2,), compatibility_scopes=(SCOPE, next_scope))
                rows = series(doc)
                self.assertIsNone(rows[1]["cumulative_observable_lots"])
                self.assertEqual(rows[1]["segment_cumulative_net_lots"], 10)
                self.assertIn(field.upper(), rows[1]["break_reason"])
                self.assertEqual(metric(doc, "net_flow_slope", 2)["null_reason"], "INCOMPATIBLE_SEGMENTS")
        with self.assertRaisesRegex(ValueError, "unsupported declared compatibility scope"):
            build([], axis, compatibility_scopes=(replace(SCOPE, source_measurement_contract="BILLIONS"),))

    def test_explicit_followup_and_request_mismatches_cannot_be_labeled_selector_union(self):
        axis = axis_n(1)
        explicit = replace(capture(), requested_selectors=(), requested_brokers=("ES",))
        mislabeled = build([row(axis.sessions[0], cap=explicit)], axis)
        self.assertEqual(series(mislabeled)[0]["coverage"], ie.INVALID)
        self.assertEqual(series(mislabeled)[0]["null_reason"], "CAPTURE_SCOPE_MISMATCH")
        correct = build([row(axis.sessions[0], cap=explicit, scope=replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"))], axis,
                        compatibility_scopes=(replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"),))
        self.assertEqual(series(correct)[0]["coverage"], ie.OBSERVED_NONZERO)
        wrong_ticker = replace(capture(), request_parameters=(("symbol", "OTHR"), ("investor_type", "A")))
        wrong_investor = replace(capture(), request_parameters=(("symbol", "SINI"), ("investor_type", "F")))
        for cap in (wrong_ticker, wrong_investor):
            doc = build([row(axis.sessions[0], cap=cap)], axis)
            self.assertEqual(series(doc)[0]["null_reason"], "REQUEST_SCOPE_MISMATCH")

    def test_incomplete_window_withholds_every_measurement(self):
        axis = axis_n(5)
        doc = build([row(d) for d in axis.sessions if d != axis.sessions[2]], axis)
        for name, measurement in doc["brokers"]["ES"]["measurements"]["5"].items():
            with self.subTest(name=name):
                self.assertIsNone(measurement["value"])
                self.assertEqual(measurement["status"], "WITHHELD")
                self.assertEqual(measurement["null_reason"], "INCOMPLETE_COVERAGE")
                self.assertEqual(measurement["expected_sessions"], 5)
                self.assertEqual(measurement["observed_sessions"], 4)
                self.assertEqual(len(measurement["window"]["session_dates"]), 5)
                self.assertEqual(len(measurement["input_refs"]), 4)

    def test_complete_window_exact_slope_and_explicit_persistence(self):
        axis = axis_n(5)
        doc = build([row(d, n) for d, n in zip(axis.sessions, [100, -20, 0, 30, -10])], axis)
        slope = metric(doc, "net_flow_slope")
        self.assertEqual(slope["value"], 20)
        self.assertEqual(slope["unit"], "lots/session")
        self.assertEqual(slope["status"], "CALCULATED")
        self.assertIsNone(slope["null_reason"])
        self.assertEqual(slope["observed_sessions"], slope["expected_sessions"])
        persistence = metric(doc, "persistence")
        self.assertEqual((persistence["value"], persistence["numerator"], persistence["denominator"]), (0.4, 2, 5))
        self.assertEqual(metric(doc, "direction_efficiency")["value"], 0.625)
        self.assertEqual(metric(doc, "gross_buy_lots")["value"], 130)
        self.assertEqual(metric(doc, "gross_sell_lots")["value"], 30)
        self.assertEqual(metric(doc, "two_sided_broker_activity_lots")["value"], 160)

    def test_reversal_skips_zeros_within_a_complete_window(self):
        axis = axis_n(5)
        doc = build([row(d, n) for d, n in zip(axis.sessions, [10, 0, -5, 0, 3])], axis)
        self.assertEqual(metric(doc, "reversal")["value"], 2)
        self.assertEqual(metric(doc, "reversal")["zero_handling"], "SKIP_ZEROS_WITHIN_COMPLETE_WINDOW")

    def test_zero_denominator_is_null_not_nan(self):
        axis = axis_n(5)
        doc = build([row(d, 0) for d in axis.sessions], axis)
        for name in ("direction_efficiency", "implied_buy_price", "implied_sell_price"):
            self.assertIsNone(metric(doc, name)["value"])
            self.assertEqual(metric(doc, name)["null_reason"], "ZERO_DENOMINATOR")
        self.assertNotIn("NaN", ie.canonical_json(doc))
        self.assertEqual(metric(doc, "persistence")["value"], 0)

    def test_acceleration_requires_ten_sessions_and_uses_difference_of_means(self):
        axis = axis_n(10)
        rows = [row(d, n) for d, n in zip(axis.sessions, [2] * 5 + [8] * 5)]
        doc = build(rows, axis)
        acc = doc["brokers"]["ES"]["acceleration"]
        self.assertEqual((acc["value"], acc["preceding_sessions"], acc["recent_sessions"]), (6, 5, 5))
        self.assertEqual(acc["expected_sessions"], 10)
        self.assertIsNone(build(rows[1:], axis)["brokers"]["ES"]["acceleration"]["value"])
        self.assertEqual(build(rows[:5], axis_n(5))["brokers"]["ES"]["acceleration"]["null_reason"], "INSUFFICIENT_HISTORY")

    def test_adv_uses_twenty_verified_market_volumes_and_exact_units(self):
        axis = axis_n(21)
        rows = [row(d) for d in axis.sessions]
        doc = build(rows, axis, market_observations=market_rows(axis))
        adv = metric(doc, "flow_vs_adv")
        self.assertEqual(adv["adv20"]["value"], 1000)
        self.assertEqual(adv["value"], 0.05)
        self.assertEqual(adv["adv20"]["market_window"]["observed_sessions"], 20)
        # Selected broker activity can change massively without changing ADV.
        huge = build([row(d, 10000) for d in axis.sessions], axis, market_observations=market_rows(axis))
        self.assertEqual(metric(huge, "flow_vs_adv")["adv20"]["value"], 1000)
        lots = build(rows, axis, market_observations=market_rows(axis, 1000, "lots"))
        self.assertEqual(metric(lots, "flow_vs_adv")["value"], adv["value"])

    def test_adv_withholds_selected_broker_only_incomplete_or_invalid_market_data(self):
        axis = axis_n(21)
        rows = [row(d) for d in axis.sessions]
        markets = market_rows(axis)
        cases = [[], markets[2:], [replace(r, volume_unit="unknown") for r in markets],
                 [replace(r, volume=-1) for r in markets], [replace(r, valid=False) for r in markets],
                 [replace(r, basis_version="other") for r in markets],
                 [replace(r, market_scope="ALL") for r in markets],
                 [replace(r, measurement_contract="SELECTED_BROKER_TOTALS") for r in markets],
                 [replace(r, capture=capture("late-market", accepted="2026-10-04T10:00:00Z")) for r in markets]]
        for market in cases:
            with self.subTest(market=len(market)):
                self.assertIsNone(metric(build(rows, axis, market_observations=market), "flow_vs_adv")["value"])
        zero = build(rows, axis, market_observations=market_rows(axis, 0))
        self.assertEqual(metric(zero, "flow_vs_adv")["null_reason"], "ZERO_ADV")

    def test_implied_prices_are_ratio_of_sums_in_rupiah_per_share(self):
        axis = axis_n(2)
        doc = build([row(axis.sessions[0], 20, buy_lots=30, sell_lots=10, buy_value_rp=3_000_000,
                         sell_value_rp=1_200_000, net_value_rp=1_800_000),
                     row(axis.sessions[1], -10, sell_value_rp=1_400_000, net_value_rp=-1_400_000)],
                    axis, windows=(2,))
        self.assertEqual(metric(doc, "implied_buy_price", 2)["value"], 1000)
        self.assertEqual(metric(doc, "implied_sell_price", 2)["value"], 1300)
        self.assertEqual(metric(doc, "implied_buy_price", 2)["unit"], "rupiah/share")
        self.assertEqual(metric(doc, "implied_sell_price", 2)["shares_per_lot"], 100)

    def test_value_without_lots_withholds_price_even_when_other_sessions_have_lots(self):
        axis = axis_n(2)
        doc = build([row(axis.sessions[0]), row(axis.sessions[1], 0, buy_value_rp=500, net_value_rp=500)], axis, windows=(2,))
        self.assertEqual(metric(doc, "implied_buy_price", 2)["null_reason"], "VALUE_WITHOUT_REPORTED_LOTS")

    def test_price_flow_facts_use_same_window_and_prior_close(self):
        axis = axis_n(21)
        doc = build([row(d) for d in axis.sessions], axis, market_observations=market_rows(axis))
        divergence = metric(doc, "price_flow_divergence")
        self.assertAlmostEqual(divergence["value"]["price_return"], 1200 / 1150 - 1)
        self.assertEqual(divergence["value"]["net_flow_lots"], 50)
        self.assertEqual(divergence["value"]["net_flow_slope_lots_per_session"], 10)
        self.assertEqual(divergence["facts"]["price_return"]["window"], divergence["window"])
        self.assertEqual(divergence["facts"]["price_return"]["market_window"]["baseline_close_rp_per_share"], 1150)
        self.assertEqual(divergence["facts"]["price_return"]["market_window"]["end_close_rp_per_share"], 1200)
        missing = build([row(d) for d in axis.sessions], axis, market_observations=[])
        self.assertIsNone(metric(missing, "price_flow_divergence")["value"])
        self.assertEqual(metric(missing, "price_flow_divergence")["facts"]["net_flow"]["value"], 50)

    def test_late_historical_capture_does_not_acquire_earlier_known_at(self):
        axis = axis_n(5)
        rows = [row(d, cap=capture(accepted=LATE)) for d in axis.sessions]
        earlier = build(rows, axis, availability_cutoff=EARLY)
        self.assertTrue(all(r["coverage"] == ie.UNOBSERVED and r["raw"] is None for r in series(earlier)))
        self.assertIsNone(earlier["max_input_known_at"])
        self.assertEqual(earlier["provenance"], [])
        later = build(rows, axis)
        self.assertEqual(later["max_input_known_at"], ie.utc_text(LATE))
        self.assertEqual(later["provenance"][0]["response_at"], ie.utc_text(EARLY))

    def test_response_and_acceptance_order_and_timezone_are_checked(self):
        with self.assertRaises(ValueError):
            capture(response=LATE, accepted=EARLY)
        with self.assertRaises(ValueError):
            capture(accepted="2026-10-02T10:00:00")
        cap = capture(accepted="2026-10-02T17:00:00+07:00")
        self.assertEqual(cap.known_at, ie.utc_text(EARLY))

    def test_duplicate_capture_is_never_summed(self):
        axis = axis_n(5)
        rows = [row(d) for d in axis.sessions]
        repeated = rows + rows + [replace(r, capture=capture("capture-b")) for r in rows]
        doc = build(repeated, axis)
        self.assertEqual(series(doc)[-1]["cumulative_observable_lots"], 50)
        self.assertEqual(series(doc)[-1]["segment_cumulative_net_lots"], 50)
        self.assertEqual(metric(doc, "net_flow_slope")["value"], 10)
        self.assertEqual(len(series(doc)), 5)
        self.assertEqual(len(metric(doc, "net_flow_slope")["input_refs"]), 10)

    def test_conflicting_revisions_quarantine_then_later_revision_corrects(self):
        axis = axis_n(5)
        rows = [row(d) for d in axis.sessions]
        conflict = row(axis.sessions[2], 11, cap=capture("capture-b"))
        first = build(rows + [conflict], axis)
        encoded = ie.canonical_json(first)
        self.assertEqual(series(first)[2]["coverage"], ie.QUARANTINED)
        self.assertIsNone(series(first)[3]["cumulative_observable_lots"])
        correction = row(axis.sessions[2], 12, revision=2, cap=capture("correction", accepted=LATE))
        second = build(rows + [conflict, correction], axis, observation_revision=2, parent_revision=1)
        self.assertEqual(series(second)[-1]["cumulative_observable_lots"], 52)
        self.assertEqual(ie.canonical_json(first), encoded)
        earlier = build(rows + [conflict, correction], axis, observation_revision=2, parent_revision=1, availability_cutoff=EARLY)
        self.assertEqual(series(earlier)[2]["coverage"], ie.QUARANTINED)
        old_revision = build(rows + [conflict, correction], axis, observation_revision=1)
        self.assertEqual(series(old_revision)[2]["coverage"], ie.QUARANTINED)

    def test_later_hole_fill_creates_new_as_of_document(self):
        axis = axis_n(5)
        rows = [row(d) for i, d in enumerate(axis.sessions) if i != 2]
        correction = row(axis.sessions[2], revision=2, cap=capture("fill", accepted=LATE))
        prior = build(rows + [correction], axis, availability_cutoff=EARLY)
        latest = build(rows + [correction], axis, observation_revision=2, parent_revision=1)
        self.assertEqual(series(prior)[2]["coverage"], ie.UNOBSERVED)
        self.assertIsNone(series(prior)[-1]["cumulative_observable_lots"])
        self.assertEqual(series(latest)[-1]["cumulative_observable_lots"], 50)

    def test_invalid_null_numeric_and_quarantined_inputs_break_continuity(self):
        axis = axis_n(3)
        bad = [{"net_lots": None}, {"buy_lots": True}, {"sell_lots": 1.5}, {"net_lots": "10"},
               {"buy_value_rp": None}, {"net_value_rp": float("nan")}, {"buy_value_rp": float("inf")},
               {"buy_value_rp": "1000000"}, {"buy_lots": -1}, {"buy_value_rp": -1},
               {"net_lots": 11}, {"net_value_rp": 999_999}, {"buy_value_rp": 0}, {"buy_lots": 2**64}]
        for change in bad:
            with self.subTest(change=change):
                doc = build([row(axis.sessions[0]), row(axis.sessions[1], **change), row(axis.sessions[2])],
                            axis, windows=(3,))
                self.assertEqual(series(doc)[1]["coverage"], ie.INVALID)
                self.assertIsNone(series(doc)[2]["cumulative_observable_lots"])
                self.assertIsNone(metric(doc, "net_flow_slope", 3)["value"])
                ie.canonical_json(doc)
        quarantined = build([row(axis.sessions[0]), row(axis.sessions[1], coverage=ie.QUARANTINED,
                            null_reason="KNOWN_BASIS_CONFLICT"), row(axis.sessions[2])], axis)
        self.assertEqual(series(quarantined)[1]["coverage"], ie.QUARANTINED)
        suspended = build([row(axis.sessions[0], coverage=ie.UNOBSERVED, null_reason="SUSPENSION_UNCERTAIN")], axis)
        self.assertEqual(series(suspended)[0]["coverage"], ie.UNOBSERVED)
        self.assertIsNone(series(suspended)[0]["cumulative_observable_lots"])

    def test_capture_contract_excludes_secrets_and_conflicting_identity(self):
        with self.assertRaises(ValueError):
            replace(capture(), request_parameters=(("token", "secret"),))
        with self.assertRaises(ValueError):
            replace(capture(), request_parameters=(("symbol", "SINI"), ("symbol", "SINI")))
        axis = axis_n(1)
        doc = build([row(axis.sessions[0], cap=capture(returned=("CC",)))], axis)
        self.assertEqual(series(doc)[0]["coverage"], ie.INVALID)
        with self.assertRaises(ValueError):
            build([row(axis.sessions[0]), row(axis.sessions[0], cap=replace(capture(), content_sha256="0" * 64))], axis)

    def test_request_order_hash_kind_and_reference_availability_are_preserved(self):
        cap = replace(capture(), requested_brokers=("ES", "CC", "ES"),
                      requested_selectors=("TOP_5_NS_LOT_C20", "TOP_5_NB_LOT_C20"),
                      query_sha256="1" * 64, content_hash_kind="RESPONSE_TEXT_UTF8_SHA256")
        axis = axis_n(1)
        doc = build([row(axis.sessions[0], cap=cap)], axis)
        self.assertEqual(doc["provenance"][0]["requested_brokers"], ["ES", "CC", "ES"])
        self.assertEqual(doc["provenance"][0]["requested_selectors"], ["TOP_5_NS_LOT_C20", "TOP_5_NB_LOT_C20"])
        self.assertEqual(doc["provenance"][0]["content_hash_kind"], "RESPONSE_TEXT_UTF8_SHA256")
        self.assertEqual(series(doc)[0]["input_refs"][0]["source_id"], cap.source_id)
        with self.assertRaises(ValueError):
            replace(cap, content_hash_kind="UNKNOWN")
        with self.assertRaises(ValueError):
            build([], axis, reference_captures=(capture("future-reference", accepted="2026-10-04T10:00:00Z"),))

    def test_unknown_scope_and_suspension_never_establish_zero_or_continuity(self):
        axis = axis_n(1)
        with self.assertRaisesRegex(ValueError, "unsupported declared compatibility scope"):
            build([], axis, compatibility_scopes=(replace(SCOPE, market_scope="UNKNOWN"),))
        suspended = build([row(axis.sessions[0], coverage=ie.UNOBSERVED, null_reason="SUSPENSION_UNCERTAIN")], axis)
        self.assertIsNone(series(suspended)[0]["raw"])
        self.assertEqual(suspended["rotation_evidence"]["observed_subset_positive_broker_count"]["status"], "WITHHELD")

    def test_conflicting_market_captures_withhold_market_measurements(self):
        axis = axis_n(21)
        markets = market_rows(axis)
        conflict = replace(markets[-1], capture=capture("market-conflict"), volume=200_000, close=900)
        doc = build([row(d) for d in axis.sessions], axis, market_observations=markets + [conflict])
        self.assertIsNone(metric(doc, "flow_vs_adv")["value"])
        self.assertIsNone(metric(doc, "price_flow_divergence")["value"])
        self.assertEqual(metric(doc, "net_flow_slope")["value"], 10)

    def test_partial_coverage_and_identity_free_output_and_persistence_guard(self):
        import coverage_guard as cg
        doc = build([row(d) for d in axis_n(5).sessions])
        self.assertFalse(doc["contract"]["full_universe"])
        self.assertFalse(set(keys(doc)) & FORBIDDEN)
        ie.validate_document(doc)
        with self.assertRaises(cg.TargetedCoverageError):
            cg.refuse_targeted(doc, "Broker Learning")
        self.assertIsNotNone(cg.full_universe_reason(doc))
        for field in FORBIDDEN:
            modified = copy.deepcopy(doc)
            modified["brokers"]["ES"][field] = True
            with self.assertRaises(ValueError):
                ie.validate_document(modified)

    def test_sini_neutral_rotation_case_supports_future_questions_without_identity(self):
        axis = axis_n(15)
        rows = [row(d, n) for d, n in zip(axis.sessions, [20] * 10 + [-4] * 5)]
        rows += [row(d, n, broker="CC") for d, n in zip(axis.sessions, [0] * 10 + [4] * 5)]
        doc = build(rows, axis, codes=("ES", "CC"), market_observations=market_rows(axis))
        rotation = doc["rotation_evidence"]
        seller = rotation["broker_windows"]["ES"]
        self.assertEqual(seller["prior_window"]["net_flow"]["value"], 100)
        self.assertEqual(seller["recent_window"]["net_flow"]["value"], -20)
        self.assertEqual(seller["prior_window"]["implied_buy_price"]["value"], 1000)
        self.assertGreater(seller["recent_window"]["price_flow_divergence"]["facts"]["price_return"]["market_window"]["end_close_rp_per_share"],
                           seller["prior_window"]["implied_buy_price"]["value"])
        self.assertGreater(seller["recent_window"]["price_flow_divergence"]["facts"]["price_return"]["value"], 0)
        self.assertEqual(rotation["concurrent_positive_brokers"]["value"], ["CC"])
        self.assertEqual(rotation["observed_subset_positive_broker_count"]["value"], 1)
        self.assertEqual(rotation["broker_windows"]["CC"]["recent_window"]["persistence"]["value"], 1)
        self.assertEqual(seller["first_observed_activity"], axis.sessions[0])
        self.assertTrue(seller["left_censored"])
        self.assertFalse(set(keys(doc)) & FORBIDDEN)
        self.assertNotIn("Albert", ie.canonical_json(doc))

    def test_stable_order_no_nan_and_no_decimal_global_state_or_io(self):
        axis = axis_n(5)
        rows = [row(d, n) for d, n in zip(axis.sessions, [1, 2, 0, -1, 7])]
        expected = ie.canonical_json(build(rows, axis, codes=("ES", "CC"), windows=(5, 2)))
        with localcontext() as context:
            context.prec = 3
            with patch("builtins.open", side_effect=AssertionError("kernel attempted I/O")):
                actual = ie.canonical_json(build(list(reversed(rows)), axis, codes=("CC", "ES"), windows=(2, 5)))
        self.assertEqual(expected, actual)
        previous_emax = DefaultContext.Emax
        try:
            DefaultContext.Emax = 2
            self.assertEqual(expected, ie.canonical_json(build(rows, axis, codes=("ES", "CC"), windows=(5, 2))))
        finally:
            DefaultContext.Emax = previous_emax
        self.assertEqual(json.loads(expected)["requested_evidence_brokers"], ["CC", "ES"])
        self.assertEqual(ie.canonical_json(json.loads(expected)), expected)

    def test_rotation_list_is_withheld_with_incomplete_required_subset(self):
        axis = axis_n(5)
        for rows in ([], [row(d) for d in axis.sessions]):
            doc = build(rows, axis, codes=("ES", "CC"))
            measure = doc["rotation_evidence"]["concurrent_positive_brokers"]
            self.assertIsNone(measure["value"])
            self.assertEqual(measure["status"], "WITHHELD")
            self.assertEqual(measure["null_reason"], "INCOMPLETE_OBSERVED_SUBSET")
        rows = [row(d, broker=b) for b in ("ES", "CC") for d in axis.sessions]
        measure = build(rows, axis, codes=("ES", "CC"))["rotation_evidence"]["concurrent_positive_brokers"]
        self.assertEqual(measure["value"], ["CC", "ES"])
        self.assertEqual(measure["status"], "CALCULATED")

    def test_daily_session_finality_refuses_preopen_and_intraday(self):
        axis = axis_n(1)
        for response in ("2026-09-01T00:00:00Z", "2026-09-01T06:00:00Z", "2026-09-01T12:00:00Z"):
            doc = build([row(axis.start, cap=capture(response=response))], axis, windows=(1,))
            self.assertEqual(series(doc)[0]["coverage"], ie.INVALID)
            self.assertEqual(series(doc)[0]["null_reason"], "SESSION_NOT_FINAL_AT_CAPTURE")
            self.assertIsNone(metric(doc, "net_flow_slope", 1)["value"])
        doc = build([row(axis.start, cap=capture(response="2026-09-01T17:00:00Z"))], axis, windows=(1,))
        self.assertEqual(series(doc)[0]["coverage"], ie.OBSERVED_NONZERO)
        self.assertEqual(metric(doc, "net_flow_slope", 1)["value"], 10)
        markets = market_rows(axis_n(21))
        last = markets[-1]
        markets[-1] = replace(last, capture=capture("intraday-market", response=last.canonical_session_date + "T06:00:00Z"))
        doc = build([row(d) for d in axis_n(21).sessions], axis_n(21), market_observations=markets)
        self.assertIsNone(metric(doc, "flow_vs_adv")["value"])

    def test_broker_request_date_market_and_capture_scope_are_checked(self):
        axis = axis_n(1)
        for params, reason in (
            ((("symbol", "SINI"), ("investor_type", "A"), ("start_date", "2026-09-02")), "REQUEST_SESSION_OUT_OF_RANGE"),
            ((("symbol", "SINI"), ("investor_type", "A"), ("end_date", "2026-08-31")), "REQUEST_SESSION_OUT_OF_RANGE"),
            ((("symbol", "SINI"), ("investor_type", "A"), ("market", "NEGOTIATED")), "REQUEST_SCOPE_MISMATCH")):
            doc = build([row(axis.start, cap=replace(capture(), request_parameters=params))], axis)
            self.assertEqual(series(doc)[0]["coverage"], ie.INVALID)
            self.assertEqual(series(doc)[0]["null_reason"], reason)
        with self.assertRaises(ValueError):
            replace(SCOPE, capture_scope="FULL_UNIVERSE")
        with self.assertRaises(ValueError):
            replace(capture(), request_parameters=(("start_date", "bad-date"),))

    def test_market_provenance_must_match_ticker_request_market_and_range(self):
        axis = axis_n(21)
        rows = [row(d) for d in axis.sessions]
        for changes in (
            {"request_parameters": (("symbol", "BBCA"),)},
            {"request_parameters": (("symbol", "SINI"), ("market", "NEGOTIATED"))},
            {"request_parameters": (("symbol", "SINI"), ("end_date", "2026-08-31"))}):
            markets = [replace(m, capture=replace(m.capture, **changes)) for m in market_rows(axis)]
            doc = build(rows, axis, market_observations=markets)
            self.assertIsNone(metric(doc, "flow_vs_adv")["value"])
            self.assertIsNone(metric(doc, "price_flow_divergence")["value"])
        for changes in ({"market_scope": "NEGOTIATED"}, {"basis_version": "basis-v2"},
                        {"measurement_contract": "SELECTED_BROKER_TOTALS"}):
            markets = [replace(m, **changes) for m in market_rows(axis)]
            self.assertIsNone(metric(build(rows, axis, market_observations=markets), "flow_vs_adv")["value"])

    def test_conflicts_include_scope_and_explicit_coverage_state(self):
        axis = axis_n(1)
        first = row(axis.start)
        for conflict in (replace(first, scope=replace(SCOPE, basis_version="basis-v2"), capture=capture("scope-conflict")),
                         replace(first, coverage=ie.QUARANTINED, null_reason="KNOWN_BASIS_CONFLICT", capture=capture("state-conflict"))):
            doc = build([first, conflict], axis, compatibility_scopes=(SCOPE, conflict.scope))
            self.assertEqual(series(doc)[0]["coverage"], ie.QUARANTINED)
            self.assertEqual(series(doc)[0]["null_reason"], "CONFLICTING_REVISIONS")
            self.assertIsNone(series(doc)[0]["raw"])
            self.assertEqual(doc["observed_brokers"], [])

    def test_revision_numbers_cannot_skip_the_declared_parent(self):
        for revision, parent in ((3, 1), (5, 2), (2, 3)):
            with self.assertRaises(ValueError):
                build([], observation_revision=revision, parent_revision=parent)
        self.assertEqual(build([], observation_revision=2, parent_revision=1)["observation_revision"], 2)

    def test_explicit_followup_requires_actual_broker_request(self):
        axis = axis_n(1)
        cap = replace(capture(), requested_selectors=(), requested_brokers=("CC",))
        doc = build([row(axis.start, scope=replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"), cap=cap)], axis,
                    compatibility_scopes=(replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"),))
        self.assertEqual(series(doc)[0]["coverage"], ie.INVALID)
        self.assertEqual(series(doc)[0]["null_reason"], "CAPTURE_SCOPE_MISMATCH")

    def test_market_duplicate_provenance_is_complete_and_order_independent(self):
        axis = axis_n(21)
        markets = market_rows(axis)
        # Equal capture IDs from distinct sources are distinct input references.
        other = [replace(m, capture=replace(m.capture, source_id="ohlc:second", content_sha256="2" * 64)) for m in markets]
        rows = [row(d) for d in axis.sessions]
        first = build(rows, axis, market_observations=markets + other)
        shuffled = build(rows, axis, market_observations=list(reversed(other + markets)))
        self.assertEqual(ie.canonical_json(first), ie.canonical_json(shuffled))
        refs = metric(first, "flow_vs_adv")["adv20"]["input_refs"]
        market_refs = [r for r in refs if "measurement_contract" in r]
        self.assertEqual(len(market_refs), 40)
        self.assertEqual({r["source_id"] for r in market_refs}, {"neobdm:/api/inventory", "ohlc:second"})
        conflicting = replace(other[-1], volume=1)
        for order in (markets + [conflicting], [conflicting] + markets):
            self.assertIsNone(metric(build(rows, axis, market_observations=order), "flow_vs_adv")["value"])

    def test_observed_brokers_only_names_emitted_valid_requested_evidence(self):
        axis = axis_n(1)
        rows = [row(axis.start), row(axis.start, broker="CC"),
                row(axis.start, broker="ZZ", cap=capture("quarantined-zz", returned=("ZZ",)), coverage=ie.QUARANTINED)]
        doc = build(rows, axis, codes=("ES", "ZZ"))
        self.assertEqual(doc["observed_brokers"], ["ES"])
        self.assertNotIn("CC", doc["brokers"])

    def test_request_identity_is_canonical_and_covers_meaning_parameters(self):
        axis = axis_n(5)
        rows = [row(d) for d in axis.sessions]
        first = build(rows, axis, codes=("ES", "CC"), windows=(5, 2), compatibility_scopes=(SCOPE,))
        reordered = build(rows[::-1], axis, codes=("CC", "ES"), windows=(2, 5), compatibility_scopes=(SCOPE, SCOPE))
        self.assertEqual(first["request_contract_sha256"], reordered["request_contract_sha256"])
        correction = build(rows, axis, codes=("CC", "ES"), windows=(2, 5), compatibility_scopes=(SCOPE,),
                           observation_revision=2, parent_revision=1, availability_cutoff=EARLY)
        self.assertEqual(first["request_contract_sha256"], correction["request_contract_sha256"])
        for changes in ({"codes": ("CC",)}, {"windows": (2,)}, {"acceleration_half_window": 2},
                        {"compatibility_scopes": (SCOPE, replace(SCOPE, basis_version="basis-v2"))}):
            args = dict(codes=("CC", "ES"), windows=(2, 5), compatibility_scopes=(SCOPE,))
            args.update(changes)
            self.assertNotEqual(first["request_contract_sha256"], build(rows, axis, **args)["request_contract_sha256"])
        self.assertEqual(first["request_contract_sha256"], ie.content_hash(first["request_identity"]))

    def test_bad_parameters_are_refused(self):
        for window in (0, -1, True, 1.5):
            with self.subTest(window=window), self.assertRaises(ValueError):
                build([], windows=(window,))
        with self.assertRaises(ValueError):
            build([], observation_revision=1, parent_revision=1)
        with self.assertRaises(ValueError):
            ie.idx_session_axis("2026-09-05", "2026-09-01")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "panel.db")
        self.conn = tdb.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_immutable_revision_lineage_product_availability_and_idempotency(self):
        axis = axis_n(5)
        first = build([row(d) for i, d in enumerate(axis.sessions) if i != 2], axis,
                      availability_cutoff=EARLY)
        with patch.object(tdb, "utc_now", return_value=EARLY):
            self.assertEqual(tdb.record_inventory_evidence(self.conn, first), "inserted")
        before = self.conn.execute("SELECT observation_json FROM inventory_evidence_revisions").fetchone()[0]
        with patch.object(tdb, "utc_now", return_value=LATE):
            self.assertEqual(tdb.record_inventory_evidence(self.conn, first), "identical")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 1)
        self.assertIsNone(tdb.inventory_evidence_as_of(self.conn, "SINI", axis.start, axis.cutoff, "2026-10-02T09:00:00Z"))
        second = build([row(d) for d in axis.sessions], axis, observation_revision=2, parent_revision=1)
        with patch.object(tdb, "utc_now", return_value=LATE):
            tdb.record_inventory_evidence(self.conn, second)
        old = tdb.inventory_evidence_as_of(self.conn, "SINI", axis.start, axis.cutoff, EARLY)
        latest = tdb.inventory_evidence_as_of(self.conn, "SINI", axis.start, axis.cutoff, LATE)
        self.assertEqual(old["evidence"], first)
        self.assertIsNone(series(old["evidence"])[-1]["cumulative_observable_lots"])
        self.assertEqual(series(latest["evidence"])[-1]["cumulative_observable_lots"], 50)
        self.assertEqual(latest["known_at"], ie.utc_text(LATE))
        retrospective = tdb.latest_inventory_evidence(self.conn, "SINI", axis.start, axis.cutoff)
        self.assertEqual(retrospective["view"], "LATEST_RETROSPECTIVE")
        self.assertEqual(retrospective["evidence"], second)
        self.assertEqual(old["view"], "AS_OF")
        self.assertEqual(latest["evidence"]["max_input_known_at"], ie.utc_text(EARLY))
        self.assertEqual(self.conn.execute("SELECT observation_json FROM inventory_evidence_revisions WHERE observation_revision=1").fetchone()[0], before)
        changed = copy.deepcopy(first)
        changed["availability_cutoff"] = ie.utc_text(LATE)
        with self.assertRaisesRegex(ValueError, "conflict"):
            tdb.record_inventory_evidence(self.conn, changed)
        for sql in ("UPDATE inventory_evidence_revisions SET parent_revision=9", "DELETE FROM inventory_evidence_revisions",
                    "UPDATE inventory_evidence_acceptances SET durable_accepted_at='2000-01-01'"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql)
            self.conn.rollback()

    def test_unconfirmed_body_is_unavailable_and_retry_confirms_at_retry_time(self):
        axis = axis_n(5)
        doc = build([row(d) for d in axis.sessions], axis)
        with patch.object(tdb, "utc_now", side_effect=[LATE, RuntimeError("interrupted after body commit")]):
            with self.assertRaises(RuntimeError):
                tdb.record_inventory_evidence(self.conn, doc)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 1)
        self.assertIsNone(tdb.inventory_evidence_as_of(self.conn, "SINI", axis.start, axis.cutoff, LATE))
        with self.assertRaisesRegex(ValueError, "parent revision is not durably confirmed"):
            tdb.record_inventory_evidence(self.conn, build([row(d) for d in axis.sessions], axis,
                                                         observation_revision=2, parent_revision=1))
        with patch.object(tdb, "utc_now", return_value=LATE):
            self.assertEqual(tdb.record_inventory_evidence(self.conn, doc), "identical")
        self.assertIsNone(tdb.inventory_evidence_as_of(self.conn, "SINI", axis.start, axis.cutoff, EARLY))
        self.assertIsNotNone(tdb.inventory_evidence_as_of(self.conn, "SINI", axis.start, axis.cutoff, LATE))

    def test_invalid_lineage_and_extra_assertions_are_refused(self):
        axis = axis_n(5)
        rows = [row(d) for d in axis.sessions]
        with self.assertRaises(ValueError):
            tdb.record_inventory_evidence(self.conn, build(rows, axis, observation_revision=2, parent_revision=1))
        injected = build(rows, axis)
        injected["actual_cost_basis"] = 1000
        with self.assertRaises(ValueError):
            tdb.record_inventory_evidence(self.conn, injected)
        explicit = replace(capture(), requested_selectors=(), requested_brokers=("ES",))
        followup = build([row(d, cap=explicit, scope=replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"))
                          for d in axis.sessions], axis, compatibility_scopes=(replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"),))
        with self.assertRaisesRegex(ValueError, "selector-union evidence only"):
            tdb.record_inventory_evidence(self.conn, followup)
        self.assertEqual(tdb.panel_meta(self.conn), tdb.META)

    def test_additive_schema_upgrade_keeps_v1_identity_and_old_reader_behavior(self):
        for table in ("inventory_snapshot_acceptances", "inventory_evidence_acceptances", "inventory_evidence_revisions"):
            self.conn.execute("DROP TABLE " + table)
        self.conn.commit()
        original = tdb.panel_meta(self.conn)
        self.assertIsNone(tdb.inventory_evidence_as_of(self.conn, "SINI", "2026-09-01", "2026-09-07", LATE))
        self.assertIsNone(tdb.latest_inventory_evidence(self.conn, "SINI", "2026-09-01", "2026-09-07"))
        tdb.ensure_schema(self.conn)
        self.assertEqual(tdb.panel_meta(self.conn), original)
        self.assertIn("inventory_evidence_revisions", tdb._tables(self.conn))

    def test_targeted_v1_reader_and_additive_acceptance_integration(self):
        import test_targeted_actor_panel as tp
        import targeted_actor_observations as tao
        dates = list(axis_n(5).sessions)
        market = {"dates": dates, "ohlc": tp.ohlc_rows(dates, 1000),
                  "brokers": {"ES": tp.broker_series([10] * 5, [1_000_000] * 5)}}
        # A separate fixture store goes through the actual targeted collector.
        with patch.object(tdb, "utc_now", return_value=EARLY):
            result = tp.run(tp.FakeVendor({"SINI": market}), ["SINI"], self.tmp.name, db="source.db", clock=lambda: EARLY)
        self.assertEqual(result["status"], "ok")
        source = result["db_path"]
        with closing(tdb.connect(source)) as fixture_conn:
            fixture = tdb.snapshot(fixture_conn, "SINI", dates[-1])
            self.assertEqual(ie.utc_text(fixture["recorded_utc"]), ie.utc_text(EARLY))
            self.assertTrue(all(ie.utc_text(c["captured_at"]) == ie.utc_text(EARLY) for c in fixture["captures"]))
        basis = tao._basis_reference()
        scope = replace(SCOPE, basis_version=basis["canonical_json_sha256"])
        args = dict(anchor=dates[0], cutoff=dates[-1], availability_cutoff=LATE, broker_codes=("ES", "ZZ"),
                    scope=scope, windows=(5,))
        with closing(tao.open_readonly(source)) as conn:
            original = tao.observation_json(tao.observe(conn, "SINI"))
            missing = tao.observe_inventory_evidence(conn, "SINI", **args)
            self.assertTrue(all(r["coverage"] == ie.UNOBSERVED for r in series(missing)))
        writer = tdb.connect(source)
        try:
            with patch.object(tdb, "utc_now", return_value=LATE):
                accepted = tdb.accept_inventory_snapshot(writer, "SINI", dates[-1])
                tdb.accept_inventory_basis_reference(writer, tao.BASIS_SOURCE_ID, json.loads(Path(tao.BASIS_FILE).read_text()))
            with patch.object(tdb, "utc_now", return_value="2026-10-04T10:00:00Z"):
                self.assertEqual(tdb.accept_inventory_snapshot(writer, "SINI", dates[-1]), accepted)
        finally:
            writer.close()
        before = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        with closing(tao.open_readonly(source)) as conn:
            doc = tao.observe_inventory_evidence(conn, "SINI", **args)
            self.assertEqual(metric(doc, "net_flow_slope")["value"], 10)
            self.assertEqual(series(doc)[-1]["cumulative_observable_lots"], 50)
            self.assertTrue(all(r["coverage"] == ie.UNOBSERVED for r in series(doc, "ZZ")))
            self.assertEqual(tao.observation_json(tao.observe(conn, "SINI")), original)
            earlier = tao.observe_inventory_evidence(conn, "SINI", **dict(args, availability_cutoff=EARLY))
            self.assertIsNone(metric(earlier, "net_flow_slope")["value"])
            self.assertEqual(earlier["provenance"], [])
            with self.assertRaises(ValueError):
                tao.observe_inventory_evidence(conn, "SINI", **dict(args, basis_reference_known_at="2026-10-04T10:00:00Z"))
            with self.assertRaises(ValueError):
                tao.observe_inventory_evidence(conn, "SINI", **dict(args, scope=replace(scope, capture_scope="EXPLICIT_FOLLOWUP")))
            conflict_path = os.path.join(self.tmp.name, "basis.json")
            Path(conflict_path).write_text(json.dumps({"regimes": [{"ticker": "SINI", "regime_first_date": dates[2],
                                                                   "regime_last_date": dates[2]}]}))
            with patch.object(tao, "BASIS_FILE", conflict_path):
                conflict_scope = replace(scope, basis_version=tao._basis_reference()["canonical_json_sha256"])
                unavailable = tao.observe_inventory_evidence(conn, "SINI", **dict(args, scope=conflict_scope))
                self.assertTrue(all(r["coverage"] == ie.UNOBSERVED for r in series(unavailable)))
                self.assertEqual(tao.observe_inventory_evidence(conn, "SINI", **args), doc)
        self.assertEqual(hashlib.sha256(Path(source).read_bytes()).hexdigest(), before)
        self.assertFalse(any(os.path.exists(source + s) for s in tdb.SIDECARS))
        writer = tdb.connect(source)
        try:
            with patch.object(tdb, "utc_now", return_value=LATE):
                tdb.record_inventory_evidence(writer, doc)
        finally:
            writer.close()
        with closing(tao.open_readonly(source)) as conn:
            stored = tao.inventory_evidence_revision(conn, "SINI", dates[0], dates[-1])
            self.assertEqual(stored["view"], "LATEST_RETROSPECTIVE")
            self.assertEqual(stored["evidence"], doc)
            self.assertIsNone(tao.inventory_evidence_revision(conn, "SINI", dates[0], dates[-1], EARLY))
            self.assertEqual(tao.observation_json(tao.observe(conn, "SINI")), original)


class StorageFindingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = tdb.connect(os.path.join(self.tmp.name, "panel.db"))
        self.axis = axis_n(5)
        self.rows = [row(d) for d in self.axis.sessions]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def evidence(self, **changes):
        return build(self.rows, self.axis, availability_cutoff=changes.pop("availability_cutoff", EARLY),
                     compatibility_scopes=changes.pop("compatibility_scopes", (SCOPE,)), **changes)

    def store(self, doc, accepted=LATE):
        with patch.object(tdb, "utc_now", return_value=accepted):
            return tdb.record_inventory_evidence(self.conn, doc)

    def read(self, cutoff, digest=None):
        return tdb.inventory_evidence_as_of(self.conn, "SINI", self.axis.start, self.axis.cutoff,
                                          cutoff, request_contract_sha256=digest)

    def test_request_identity_refuses_changed_question_and_accepts_distinct_chain(self):
        first = self.evidence()
        self.store(first)
        for changes in ({"codes": ("CC",)}, {"windows": (2,)}, {"acceleration_half_window": 2},
                        {"compatibility_scopes": (SCOPE, replace(SCOPE, market_scope="OTHER"))}):
            with self.subTest(changes=changes):
                child = self.evidence(observation_revision=2, parent_revision=1, **changes)
                self.assertNotEqual(child["request_contract_sha256"], first["request_contract_sha256"])
                with self.assertRaisesRegex(ValueError, "immediately preceding revision"):
                    self.store(child)
        corrected = build([row(d, 11, revision=2) for d in self.axis.sessions], self.axis,
                          observation_revision=2, parent_revision=1, availability_cutoff=EARLY,
                          compatibility_scopes=(SCOPE,))
        self.assertEqual(corrected["request_contract_sha256"], first["request_contract_sha256"])
        self.store(corrected)
        independent = self.evidence(codes=("CC",))
        self.store(independent)
        with self.assertRaisesRegex(ValueError, "multiple evidence requests"):
            self.read(LATE)
        with self.assertRaisesRegex(ValueError, "multiple evidence requests"):
            tdb.latest_inventory_evidence(self.conn, "SINI", self.axis.start, self.axis.cutoff)
        selected = self.read(LATE, first["request_contract_sha256"])
        self.assertEqual(selected["evidence"], corrected)
        self.assertEqual(self.read(LATE, independent["request_contract_sha256"])["evidence"], independent)

    def test_durable_product_acceptance_is_distinct_from_early_inputs(self):
        doc = self.evidence()
        self.assertEqual(doc["max_input_known_at"], ie.utc_text(EARLY))
        self.assertNotIn("known_at", doc)
        self.store(doc, LATE)
        self.assertIsNone(self.read("2026-10-03T09:59:59Z"))
        envelope = self.read(LATE)
        self.assertEqual(envelope["known_at"], ie.utc_text(LATE))
        self.assertEqual(envelope["evidence"]["max_input_known_at"], ie.utc_text(EARLY))
        import coverage_guard
        for wrapped in (envelope, tdb.latest_inventory_evidence(self.conn, "SINI", self.axis.start, self.axis.cutoff)):
            with self.assertRaises(coverage_guard.TargetedCoverageError):
                coverage_guard.refuse_targeted(wrapped, "full-universe consumer")

    def test_product_acceptance_cannot_precede_input_or_parent(self):
        doc = self.evidence()
        with patch.object(tdb, "utc_now", side_effect=[LATE, "2026-10-02T09:59:59Z"]):
            with self.assertRaisesRegex(ValueError, "input's availability"):
                tdb.record_inventory_evidence(self.conn, doc)
        self.assertIsNone(self.read(LATE))
        self.store(doc, LATE)
        child = self.evidence(observation_revision=2, parent_revision=1)
        with patch.object(tdb, "utc_now", side_effect=["2026-10-04T10:00:00Z", "2026-10-03T09:59:59Z"]):
            with self.assertRaisesRegex(ValueError, "precedes parent revision"):
                tdb.record_inventory_evidence(self.conn, child)
        self.assertEqual(self.read(LATE)["evidence"], doc)

    def test_availability_cannot_move_backward_and_future_cutoff_cannot_occupy_revision(self):
        first = self.evidence(availability_cutoff=LATE)
        self.store(first)
        child = self.evidence(observation_revision=2, parent_revision=1)
        with self.assertRaisesRegex(ValueError, "availability cannot move backwards"):
            self.store(child)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 1)
        future = self.evidence(codes=("CC",), availability_cutoff="2026-10-04T10:00:00Z")
        with self.assertRaisesRegex(ValueError, "cutoff cannot be later"):
            self.store(future, LATE)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 1)
        self.store(self.evidence(codes=("CC",)))

    def test_unconfirmed_body_retry_becomes_visible_only_at_retry_time(self):
        doc = self.evidence()
        with patch.object(tdb, "utc_now", side_effect=[LATE, RuntimeError("after body commit")]):
            with self.assertRaises(RuntimeError):
                tdb.record_inventory_evidence(self.conn, doc)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 1)
        self.assertIsNone(self.read("2026-10-05T10:00:00Z"))
        retry = "2026-10-04T10:00:00Z"
        self.assertEqual(self.store(doc, retry), "identical")
        self.assertIsNone(self.read(LATE))
        self.assertIsNotNone(self.read(retry))
        self.store(doc, "2026-10-05T10:00:00Z")
        self.assertEqual(self.read(retry)["known_at"], ie.utc_text(retry))

    def test_revision_numbering_is_exactly_parent_plus_one(self):
        self.store(self.evidence())
        with self.assertRaises(ValueError):
            self.evidence(observation_revision=3, parent_revision=1)
        skipped = self.evidence(observation_revision=3, parent_revision=2)
        with self.assertRaisesRegex(ValueError, "immediately preceding revision"):
            self.store(skipped)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 1)

    def test_microsecond_durability_has_no_earlier_visibility(self):
        from datetime import datetime, timezone
        instant = datetime(2026, 10, 3, 10, 0, 0, 123456, tzinfo=timezone.utc)
        with patch.object(tdb, "datetime") as clock:
            clock.now.return_value = instant
            self.assertEqual(tdb.utc_now(), "2026-10-03T10:00:00.123456Z")
        self.store(self.evidence(), "2026-10-03T10:00:00.123456Z")
        self.assertIsNone(self.read("2026-10-03T10:00:00.123455Z"))
        self.assertIsNotNone(self.read("2026-10-03T10:00:00.123456Z"))

    def test_selector_union_refusal_inspects_capture_provenance_without_valid_rows(self):
        explicit = replace(capture("followup"), requested_selectors=(), requested_brokers=("CC",))
        doc = build([], self.axis, reference_captures=(explicit,), compatibility_scopes=(SCOPE,),
                    availability_cutoff=EARLY)
        self.assertTrue(all(r["scope"] is None for r in series(doc)))
        with self.assertRaisesRegex(ValueError, "selector-union evidence only"):
            self.store(doc)

    def test_raw_replace_update_delete_cannot_rewrite_immutable_keys(self):
        doc = self.evidence()
        self.store(doc)
        tdb.start_run(self.conn, "r", "v1", EARLY)
        columns = ("ticker,discovery_as_of,run_id,collection_mode,coverage_scope,selector_plan,"
                   "requested_start_date,requested_end_date,investor_type,first_session,last_session,"
                   "session_count,observed_broker_count,content_sha256,recorded_utc")
        with self.conn:
            self.conn.execute("INSERT INTO panel_snapshots (" + columns + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              ("SINI", self.axis.cutoff, "r", tdb.COLLECTION_MODE, tdb.COVERAGE_SCOPE, "v1",
                               self.axis.start, self.axis.cutoff, "A", self.axis.start, self.axis.cutoff,
                               5, 1, "f" * 64, EARLY))
        with patch.object(tdb, "utc_now", return_value=LATE):
            accepted = tdb.accept_inventory_snapshot(self.conn, "SINI", self.axis.cutoff)
            basis = tdb.accept_inventory_basis_reference(self.conn, "basis", {"regimes": []})
        for table in tdb._INVENTORY_KEYS:
            original = self.conn.execute("SELECT * FROM " + table).fetchall()
            info = self.conn.execute("PRAGMA table_info(" + table + ")").fetchall()
            columns = [r[1] for r in info]
            tampered = list(original[0])
            target = "durable_accepted_at" if "durable_accepted_at" in columns else "content_sha256"
            tampered[columns.index(target)] = "2000-01-01T00:00:00Z" if target == "durable_accepted_at" else "a" * 64
            # A body hash is not part of a revision key. For basis bodies, keep
            # the key and replace the content itself instead.
            if table == "inventory_basis_references":
                tampered = list(original[0])
                tampered[columns.index("content_json")] = "{}"
            for prefix in ("INSERT OR REPLACE INTO", "REPLACE INTO", "INSERT INTO"):
                with self.subTest(table=table, action=prefix), self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(prefix + " " + table + " VALUES (" + ",".join("?" for _ in columns) + ")", tampered)
                self.conn.rollback()
            for sql in ("UPDATE " + table + " SET " + columns[0] + "=" + columns[0], "DELETE FROM " + table):
                with self.subTest(table=table, action=sql), self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(sql)
                self.conn.rollback()
            self.assertEqual(self.conn.execute("SELECT * FROM " + table).fetchall(), original)
        with patch.object(tdb, "utc_now", return_value="2026-10-05T10:00:00Z"):
            self.assertEqual(tdb.accept_inventory_snapshot(self.conn, "SINI", self.axis.cutoff), accepted)
            self.assertEqual(tdb.accept_inventory_basis_reference(self.conn, "basis", {"regimes": []}), basis)
        self.assertEqual(self.store(doc), "identical")

    def test_basis_hash_and_postcommit_availability_are_coupled(self):
        first_content = {"regimes": []}
        with patch.object(tdb, "utc_now", return_value=EARLY):
            first = tdb.accept_inventory_basis_reference(self.conn, "basis", first_content)
        later_content = {"regimes": [{"ticker": "SINI"}]}
        with patch.object(tdb, "utc_now", return_value=LATE):
            later = tdb.accept_inventory_basis_reference(self.conn, "basis", later_content)
        self.assertNotEqual(first["content_sha256"], later["content_sha256"])
        self.assertEqual(first["content_sha256"], ie.content_hash(json.loads(first["content_json"])))
        self.assertIsNone(tdb.inventory_basis_reference_as_of(self.conn, "basis", later["content_sha256"], EARLY))
        self.assertEqual(tdb.inventory_basis_reference_as_of(self.conn, "basis", first["content_sha256"], EARLY), first)
        self.assertEqual(tdb.inventory_basis_reference_as_of(self.conn, "basis", later["content_sha256"], LATE), later)
        content = {"regimes": [{"ticker": "OTHER"}]}
        with patch.object(tdb, "utc_now", side_effect=[EARLY, RuntimeError("after basis body commit")]):
            with self.assertRaises(RuntimeError):
                tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        self.assertIsNone(tdb.inventory_basis_reference_as_of(self.conn, "basis", ie.content_hash(content), LATE))
        with patch.object(tdb, "utc_now", return_value="2026-10-04T10:00:00Z"):
            confirmed = tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        self.assertIsNone(tdb.inventory_basis_reference_as_of(self.conn, "basis", ie.content_hash(content), LATE))
        self.assertEqual(confirmed["durable_accepted_at"], ie.utc_text("2026-10-04T10:00:00Z"))

    def test_incompatible_unshipped_extension_fails_without_migrating_v1(self):
        self.conn.execute("DROP TABLE inventory_evidence_acceptances")
        self.conn.execute("DROP TABLE inventory_evidence_revisions")
        self.conn.execute("CREATE TABLE inventory_evidence_revisions (ticker TEXT, anchor TEXT, cutoff TEXT, "
                          "observation_revision INTEGER, PRIMARY KEY(ticker,anchor,cutoff,observation_revision))")
        self.conn.commit()
        before = tdb.panel_meta(self.conn)
        with self.assertRaisesRegex(tdb.NotTargetedPanelError, "incompatible unshipped"):
            tdb.ensure_schema(self.conn)
        self.assertEqual(tdb.panel_meta(self.conn), before)


class BasisAvailabilityTests(unittest.TestCase):
    """An accepted content hash, rather than today's file, determines as-of basis."""

    def setUp(self):
        import test_targeted_actor_panel as tp
        import targeted_actor_observations as tao
        self.tao = tao
        self.tmp = tempfile.TemporaryDirectory()
        self.axis = axis_n(5)
        dates = list(self.axis.sessions)
        market = {"dates": dates, "ohlc": tp.ohlc_rows(dates, 1000),
                  "brokers": {"ES": tp.broker_series([10] * 5, [1_000_000] * 5)}}
        with patch.object(tdb, "utc_now", return_value=EARLY):
            result = tp.run(tp.FakeVendor({"SINI": market}), ["SINI"], self.tmp.name, db="basis-source.db", clock=lambda: EARLY)
        self.path = result["db_path"]
        self.initial_content = {"regimes": []}
        writer = tdb.connect(self.path)
        try:
            with patch.object(tdb, "utc_now", return_value=EARLY):
                tdb.accept_inventory_snapshot(writer, "SINI", dates[-1])
                self.initial_basis = tdb.accept_inventory_basis_reference(
                    writer, tao.BASIS_SOURCE_ID, self.initial_content)
        finally:
            writer.close()
        self.scope = replace(SCOPE, basis_version=ie.content_hash(self.initial_content))
        self.args = dict(anchor=dates[0], cutoff=dates[-1], availability_cutoff=EARLY,
                         broker_codes=("ES",), scope=self.scope, windows=(5,))

    def tearDown(self):
        self.tmp.cleanup()

    def _observe(self, **changes):
        with closing(self.tao.open_readonly(self.path)) as conn:
            return self.tao.observe_inventory_evidence(conn, "SINI", **dict(self.args, **changes))

    def _accept_later_basis(self):
        content = {"regimes": [{"ticker": "SINI", "regime_first_date": self.axis.sessions[2],
                                "regime_last_date": self.axis.sessions[2]}]}
        writer = tdb.connect(self.path)
        try:
            with patch.object(tdb, "utc_now", return_value=LATE):
                accepted = tdb.accept_inventory_basis_reference(writer, self.tao.BASIS_SOURCE_ID, content)
        finally:
            writer.close()
        return content, accepted, replace(self.scope, basis_version=ie.content_hash(content))

    def test_later_basis_content_is_not_available_to_an_earlier_cutoff(self):
        before = self._observe()
        self.assertEqual(metric(before, "net_flow_slope")["value"], 10)
        content, accepted, later_scope = self._accept_later_basis()
        self.assertEqual(ie.canonical_json(self._observe()), ie.canonical_json(before))
        unavailable = self._observe(scope=later_scope, basis_reference_known_at=EARLY)
        self.assertTrue(all(r["coverage"] == ie.UNOBSERVED for r in series(unavailable)))
        self.assertEqual(unavailable["provenance"], [])
        after = self._observe(scope=later_scope, availability_cutoff=LATE)
        self.assertEqual(series(after)[2]["coverage"], ie.QUARANTINED)
        self.assertEqual(series(after)[2]["null_reason"], "KNOWN_BASIS_CONFLICT")
        self.assertIsNone(series(after)[-1]["cumulative_observable_lots"])
        self.assertEqual(after["max_input_known_at"], ie.utc_text(LATE))
        self.assertEqual(accepted["content_sha256"], ie.content_hash(content))

    def test_current_basis_file_cannot_change_historical_adapter_or_stored_result(self):
        before = self._observe()
        writer = tdb.connect(self.path)
        try:
            with patch.object(tdb, "utc_now", return_value=EARLY):
                tdb.record_inventory_evidence(writer, before)
        finally:
            writer.close()
        changed_file = os.path.join(self.tmp.name, "current-basis.json")
        Path(changed_file).write_text(json.dumps({"regimes": [
            {"ticker": "SINI", "regime_first_date": self.axis.sessions[0],
             "regime_last_date": self.axis.sessions[-1]}]}))
        source_before = Path(self.path).read_bytes()
        with patch.object(self.tao, "BASIS_FILE", changed_file):
            self.assertEqual(ie.canonical_json(self._observe()), ie.canonical_json(before))
            with closing(self.tao.open_readonly(self.path)) as conn:
                historical = self.tao.inventory_evidence_revision(
                    conn, "SINI", self.axis.start, self.axis.cutoff, EARLY)
                retrospective = self.tao.inventory_evidence_revision(
                    conn, "SINI", self.axis.start, self.axis.cutoff)
        self.assertEqual(historical["evidence"], before)
        self.assertEqual(retrospective["evidence"], before)
        self.assertEqual(retrospective["view"], "LATEST_RETROSPECTIVE")
        self.assertEqual(Path(self.path).read_bytes(), source_before)
        self.assertFalse(any(os.path.exists(self.path + s) for s in tdb.SIDECARS))

    def test_durable_basis_hash_and_time_are_coupled_and_attestation_cannot_backdate(self):
        content, accepted, later_scope = self._accept_later_basis()
        with closing(self.tao.open_readonly(self.path)) as conn:
            self.assertIsNone(tdb.inventory_basis_reference_as_of(
                conn, self.tao.BASIS_SOURCE_ID, ie.content_hash(content), EARLY))
            record = tdb.inventory_basis_reference_as_of(
                conn, self.tao.BASIS_SOURCE_ID, ie.content_hash(content), LATE)
        self.assertEqual(ie.content_hash(json.loads(record["content_json"])), record["content_sha256"])
        self.assertEqual(record["durable_accepted_at"], ie.utc_text(LATE))
        with self.assertRaisesRegex(ValueError, "stored durable basis acceptance"):
            self._observe(scope=later_scope, availability_cutoff=LATE, basis_reference_known_at=EARLY)
        after = self._observe(scope=later_scope, availability_cutoff=LATE, basis_reference_known_at=LATE)
        self.assertEqual(after["max_input_known_at"], ie.utc_text(LATE))

    def test_basis_content_hash_conflict_is_refused_by_adapter(self):
        writer = tdb.connect(self.path)
        try:
            record = tdb.inventory_basis_reference_as_of(writer, self.tao.BASIS_SOURCE_ID,
                                                        self.scope.basis_version, EARLY)
        finally:
            writer.close()
        record["content_json"] = ie.canonical_json({"regimes": [{"ticker": "SINI",
            "regime_first_date": self.axis.start, "regime_last_date": self.axis.cutoff}]})
        with patch.object(tdb, "inventory_basis_reference_as_of", return_value=record):
            with self.assertRaisesRegex(self.tao.BasisReferenceError, "immutable content hash"):
                self._observe()

    def test_accepted_basis_is_read_without_the_current_basis_file(self):
        with patch.object(self.tao, "_basis_reference", side_effect=AssertionError("current file must not be read")), \
                patch.object(self.tao, "BASIS_FILE", os.path.join(self.tmp.name, "missing.json")):
            doc = self._observe()
        self.assertEqual(metric(doc, "net_flow_slope")["value"], 10)

    def test_later_basis_can_be_read_as_explicit_latest_retrospective_request(self):
        original = self._observe()
        writer = tdb.connect(self.path)
        try:
            with patch.object(tdb, "utc_now", return_value=EARLY):
                tdb.record_inventory_evidence(writer, original)
        finally:
            writer.close()
        _, _, later_scope = self._accept_later_basis()
        later = self._observe(scope=later_scope, availability_cutoff=LATE)
        self.assertNotEqual(original["request_contract_sha256"], later["request_contract_sha256"])
        writer = tdb.connect(self.path)
        try:
            with patch.object(tdb, "utc_now", return_value=LATE):
                tdb.record_inventory_evidence(writer, later)
        finally:
            writer.close()
        with closing(self.tao.open_readonly(self.path)) as conn:
            earlier = self.tao.inventory_evidence_revision(
                conn, "SINI", self.axis.start, self.axis.cutoff, EARLY,
                request_contract_sha256=original["request_contract_sha256"])
            unavailable = self.tao.inventory_evidence_revision(
                conn, "SINI", self.axis.start, self.axis.cutoff, EARLY,
                request_contract_sha256=later["request_contract_sha256"])
            retrospective = self.tao.inventory_evidence_revision(
                conn, "SINI", self.axis.start, self.axis.cutoff,
                request_contract_sha256=later["request_contract_sha256"])
        self.assertEqual(earlier["evidence"], original)
        self.assertIsNone(unavailable)
        self.assertEqual(retrospective["view"], "LATEST_RETROSPECTIVE")
        self.assertEqual(retrospective["evidence"], later)
        self.assertEqual(series(retrospective["evidence"])[2]["coverage"], ie.QUARANTINED)

    def test_unconfirmed_basis_content_cannot_be_backdated_by_retry(self):
        content = {"regimes": [{"ticker": "SINI", "regime_first_date": self.axis.sessions[0],
                                "regime_last_date": self.axis.sessions[0]}]}
        digest = ie.content_hash(content)
        writer = tdb.connect(self.path)
        try:
            with patch.object(tdb, "utc_now", side_effect=[EARLY, RuntimeError("interrupted basis acceptance")]):
                with self.assertRaises(RuntimeError):
                    tdb.accept_inventory_basis_reference(writer, self.tao.BASIS_SOURCE_ID, content)
            self.assertIsNone(tdb.inventory_basis_reference_as_of(
                writer, self.tao.BASIS_SOURCE_ID, digest, LATE))
            with patch.object(tdb, "utc_now", return_value=LATE):
                record = tdb.accept_inventory_basis_reference(writer, self.tao.BASIS_SOURCE_ID, content)
            self.assertEqual(record["durable_accepted_at"], ie.utc_text(LATE))
            self.assertIsNone(tdb.inventory_basis_reference_as_of(
                writer, self.tao.BASIS_SOURCE_ID, digest, EARLY))
            self.assertIsNotNone(tdb.inventory_basis_reference_as_of(
                writer, self.tao.BASIS_SOURCE_ID, digest, LATE))
        finally:
            writer.close()


class ValidatorFindingsTests(unittest.TestCase):
    def setUp(self):
        self.axis = axis_n(20)
        self.doc = build([row(d) for d in self.axis.sessions], self.axis, windows=(5,20),
                         market_observations=market_rows(self.axis))

    def mutate(self, path, value):
        modified = copy.deepcopy(self.doc)
        target = modified
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return modified

    def test_current_builder_document_is_accepted(self):
        ie.validate_document(self.doc)
        ie.validate_document(build([], self.axis, codes=('ES','ZZ'), windows=(5,20)))

    def test_semantic_claims_cannot_be_strengthened(self):
        mutations = [
            (('rotation_evidence','concurrent_buying_means'), 'CONFIRMED_SELLER_TO_BUYER_TRANSFER'),
            (('rotation_evidence','context','coverage_scope'), 'FULL_UNIVERSE'),
            (('rotation_evidence','context','ticker'), 'BBCA'),
            (('rotation_evidence','context','declared_windows'), [1]),
            (('rotation_evidence','broker_windows','ES','left_censored'), False),
            (('brokers','ES','series',0,'left_censored'), False),
            (('brokers','ES','series',0,'opening_position_lots'), 0),
            (('contract','full_universe'), True),
            (('contract','shares_per_lot'), 1),
        ]
        for path, value in mutations:
            with self.subTest(path=path), self.assertRaises(ValueError):
                ie.validate_document(self.mutate(path,value))

    def test_state_and_reason_values_use_controlled_vocabulary(self):
        mutations = [
            (('brokers','ES','series',0,'coverage'), 'OWNER_CONFIRMED'),
            (('brokers','ES','series',0,'continuity_status'), 'HOLDINGS_KNOWN'),
            (('brokers','ES','series',0,'null_reason'), 'ES = Albert'),
            (('brokers','ES','series',0,'break_reason'), 'controller'),
            (('brokers','ES','measurements','5','net_flow','null_reason'), 'smart_money'),
        ]
        for path,value in mutations:
            with self.subTest(path=path), self.assertRaises(ValueError):
                ie.validate_document(self.mutate(path,value))

    def test_measurement_constants_and_status_value_pairs_are_pinned(self):
        mutations = [
            (('brokers','ES','measurements','5','net_flow','unit'), 'billions'),
            (('brokers','ES','measurements','5','net_flow','status'), 'OBSERVED'),
            (('brokers','ES','measurements','5','net_flow','value'), None),
            (('brokers','ES','measurements','5','reversal','zero_handling'), 'ZERO_IS_SELLING'),
            (('brokers','ES','measurements','5','implied_buy_price','shares_per_lot'), 1),
            (('brokers','ES','measurements','5','persistence','denominator'), 4),
            (('brokers','ES','acceleration','recent_sessions'), 10),
        ]
        for path,value in mutations:
            with self.subTest(path=path), self.assertRaises(ValueError):
                ie.validate_document(self.mutate(path,value))
        omitted = build([], self.axis)
        omitted['brokers']['ES']['measurements']['5']['net_flow']['value'] = 0
        with self.assertRaises(ValueError):
            ie.validate_document(omitted)

    def test_identity_hash_and_correspondence_are_enforced(self):
        modified = self.mutate(('request_identity','declared_windows'), [2])
        modified['request_contract_sha256'] = ie.content_hash(modified['request_identity'])
        with self.assertRaises(ValueError):
            ie.validate_document(modified)
        for path,value in [
            (('request_contract_sha256',), '0'*64),
            (('request_identity','acceleration_half_window'), True),
            (('request_identity','market_measurement_contract'), 'SELECTED_BROKER_TOTALS'),
            (('request_identity','compatibility_scopes'), []),
            (('observed_brokers',), ['CC','ES']),
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                ie.validate_document(self.mutate(path,value))

    def test_provenance_and_actual_input_availability_are_enforced(self):
        for path,value in [
            (('max_input_known_at',), '2000-01-01T00:00:00.000000Z'),
            (('provenance',0,'known_at'), '2000-01-01T00:00:00.000000Z'),
            (('provenance',0,'durable_accepted_at'), '2000-01-01T00:00:00.000000Z'),
            (('provenance',0,'content_hash_kind'), 'UNDECLARED_MEANING'),
            (('provenance',0,'request_parameters','actor_identity'), 'Albert'),
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                ie.validate_document(self.mutate(path,value))

    def test_concurrent_list_withheld_and_count_have_identical_semantics(self):
        missing = build([row(d) for d in self.axis.sessions], self.axis, codes=('ES','ZZ'))
        ie.validate_document(missing)
        self.assertIsNone(missing['rotation_evidence']['concurrent_positive_brokers']['value'])
        missing['rotation_evidence']['concurrent_positive_brokers']['value'] = []
        with self.assertRaises(ValueError):
            ie.validate_document(missing)

    def test_semantic_constants_and_counts_preserve_json_types(self):
        mutations = [
            (('contract','full_universe'), 0),
            (('contract','unobserved_is_zero'), 0),
            (('contract','shares_per_lot'), 100.0),
            (('brokers','ES','series',0,'evidence_revision'), True),
            (('rotation_evidence','context','observation_revision'), True),
            (('brokers','ES','measurements','5','net_flow','expected_sessions'), 5.0),
            (('brokers','ES','measurements','5','net_flow','window','sessions'), 5.0),
            (('brokers','ES','acceleration','recent_sessions'), 5.0),
            (('rotation_evidence','observed_subset_positive_broker_count','value'), True),
        ]
        for path,value in mutations:
            with self.subTest(path=path), self.assertRaises(ValueError):
                ie.validate_document(self.mutate(path,value))

    def test_storage_refuses_forged_observed_capture_session_and_request(self):
        axis = axis_n(1)
        valid = build([row(axis.start)], axis, windows=(1,))
        with tempfile.TemporaryDirectory() as tmp, closing(tdb.connect(os.path.join(tmp, "panel.db"))) as conn:
            for changed in (
                {"response_at": axis.start + "T00:00:00.000000Z"},
                {"request_parameters": {"symbol": "BBCA", "investor_type": "A"}},
                {"request_parameters": {"symbol": "SINI", "investor_type": "A", "market": "NEGOTIATED"}},
                {"request_parameters": {"symbol": "SINI", "investor_type": "A", "end_date": "2026-08-31"}},
                {"returned_brokers": ["CC"]}):
                forged = copy.deepcopy(valid)
                forged["provenance"][0].update(changed)
                with self.subTest(changed=changed), patch.object(tdb, "utc_now", return_value=LATE):
                    with self.assertRaisesRegex(ValueError, "source request/session/flow contract"):
                        tdb.record_inventory_evidence(conn, forged)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 0)
        malformed = copy.deepcopy(valid)
        malformed["brokers"]["ES"]["series"][0]["raw"]["buy_lots"] = 10.0
        with self.assertRaisesRegex(ValueError, "source request/session/flow contract"):
            ie.validate_document(malformed)
        with localcontext() as context:
            context.prec = 2
            ie.validate_document(valid)

    def test_storage_refuses_forged_market_capture_session_and_request(self):
        for changed in (
            {"response_at": self.axis.cutoff + "T06:00:00.000000Z"},
            {"request_parameters": {"symbol": "BBCA"}},
            {"request_parameters": {"symbol": "SINI", "market": "NEGOTIATED"}},
            {"request_parameters": {"symbol": "SINI", "end_date": "2026-08-31"}}):
            forged = copy.deepcopy(self.doc)
            market_capture = next(c for c in forged["provenance"] if c["capture_id"] == "market")
            market_capture.update(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "market reference violates"):
                ie.validate_document(forged)

    def test_nan_and_unknown_fields_cannot_be_persisted(self):
        with self.assertRaises(ValueError):
            ie.validate_document(self.mutate(('brokers','ES','series',0,'raw','buy_value_rp'),float('nan')))
        modified=copy.deepcopy(self.doc)
        modified['rotation_evidence']['context']['beneficial_owner']='Albert'
        with self.assertRaises(ValueError):
            ie.validate_document(modified)




class FinalKernelGuardsTests(unittest.TestCase):
    def test_compatibility_scopes_are_required_nonempty_and_supported(self):
        axis = axis_n(5)
        arguments = dict(ticker="SINI", broker_codes=("ES",), axis=axis, availability_cutoff=EARLY)
        with self.assertRaises((TypeError, ValueError)):
            ie.build_inventory_evidence([row(d) for d in axis.sessions], **arguments)
        for scopes in ((), None, ("scope",), (replace(SCOPE, market_scope="UNKNOWN"),),
                       (replace(SCOPE, source_measurement_contract="SELECTED_BROKER_TOTALS"),)):
            with self.subTest(scopes=scopes), self.assertRaises((TypeError, ValueError)):
                ie.build_inventory_evidence([], compatibility_scopes=scopes, **arguments)
        empty = build([], axis)
        empty["request_identity"]["compatibility_scopes"] = []
        empty["request_contract_sha256"] = ie.content_hash(empty["request_identity"])
        with self.assertRaisesRegex(ValueError, "nonempty declared"):
            ie.validate_document(empty)

    def test_invisible_future_and_higher_revision_scopes_do_not_change_historical_request(self):
        axis = axis_n(5)
        visible = [row(d) for d in axis.sessions]
        original = build(visible, axis, availability_cutoff=EARLY)
        other_scope = replace(SCOPE, basis_version="basis-v2")
        future = row(axis.cutoff, scope=other_scope, cap=capture("future-scope", accepted=LATE))
        higher = row(axis.cutoff, revision=2, scope=other_scope, cap=capture("higher-scope"))
        for hidden in ([future], [higher], [future, higher]):
            with self.subTest(hidden=hidden):
                historical = build(visible + hidden, axis, availability_cutoff=EARLY)
                self.assertEqual(ie.canonical_json(historical), ie.canonical_json(original))
                self.assertEqual(historical["request_identity"]["compatibility_scopes"], [ie.asdict(SCOPE)])
                self.assertEqual(historical["request_contract_sha256"], original["request_contract_sha256"])
        with self.assertRaisesRegex(ValueError, "row scope outside declared"):
            build(visible + [future], axis, availability_cutoff=LATE)
        with self.assertRaisesRegex(ValueError, "row scope outside declared"):
            build(visible + [higher], axis, observation_revision=2, parent_revision=1)
        reversed_inputs = build(visible[::-1] + [higher, future], axis, availability_cutoff=EARLY,
                                compatibility_scopes=(SCOPE, SCOPE))
        self.assertEqual(ie.canonical_json(reversed_inputs), ie.canonical_json(original))

    def test_root_revision_is_one_in_builder_and_validator(self):
        for revision in (2, 9):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "start at revision 1"):
                build([], observation_revision=revision)
        forged = build([])
        forged["observation_revision"] = 9
        forged["rotation_evidence"]["context"]["observation_revision"] = 9
        for value in forged["brokers"].values():
            for source_row in value["series"]:
                source_row["evidence_revision"] = 9
        with self.assertRaisesRegex(ValueError, "start at revision 1"):
            ie.validate_document(forged)

    def test_explicit_followup_identity_is_refused_even_without_rows_or_captures(self):
        doc = build([], compatibility_scopes=(replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"),))
        self.assertEqual(doc["provenance"], [])
        self.assertTrue(all(source_row["scope"] is None for source_row in series(doc)))
        with tempfile.TemporaryDirectory() as tmp, closing(tdb.connect(os.path.join(tmp, "panel.db"))) as conn:
            with self.assertRaisesRegex(ValueError, "selector-union evidence only"):
                tdb.record_inventory_evidence(conn, doc)

    def test_rotation_list_count_agreement_is_required(self):
        axis = axis_n(5)
        original = build([row(d) for d in axis.sessions], axis)
        forged = copy.deepcopy(original)
        forged["rotation_evidence"]["concurrent_positive_brokers"]["input_refs"] = []
        with self.assertRaisesRegex(ValueError, "share availability semantics"):
            ie.validate_document(forged)
        forged = copy.deepcopy(original)
        forged["rotation_evidence"]["concurrent_positive_brokers"]["value"] = []
        with self.assertRaisesRegex(ValueError, "differs from the observed subset"):
            ie.validate_document(forged)

    def test_session_axis_is_part_of_request_identity_and_is_validator_pinned(self):
        original = build([], axis_n(5))
        different_axis = build([], axis_n(6))
        self.assertIn("session_axis", original["request_identity"])
        self.assertEqual(original["request_identity"]["session_axis"], original["axis"])
        self.assertNotEqual(original["request_contract_sha256"], different_axis["request_contract_sha256"])
        forged = copy.deepcopy(original)
        del forged["request_identity"]["session_axis"]
        forged["request_contract_sha256"] = ie.content_hash(forged["request_identity"])
        with self.assertRaises(ValueError):
            ie.validate_document(forged)
        forged = copy.deepcopy(original)
        forged["request_identity"]["session_axis"] = different_axis["axis"]
        forged["request_contract_sha256"] = ie.content_hash(forged["request_identity"])
        with self.assertRaisesRegex(ValueError, "request identity differs"):
            ie.validate_document(forged)

    def test_market_capture_requires_exact_symbol(self):
        axis = axis_n(21)
        rows = [row(d) for d in axis.sessions]
        correct = build(rows, axis, market_observations=market_rows(axis))
        self.assertEqual(metric(correct, "flow_vs_adv")["adv20"]["value"], 1000)
        for params in ((), (("symbol", "BBCA"),)):
            markets = [replace(m, capture=replace(m.capture, request_parameters=params)) for m in market_rows(axis)]
            withheld = build(rows, axis, market_observations=markets)
            self.assertIsNone(metric(withheld, "flow_vs_adv")["value"])
            self.assertEqual(metric(withheld, "flow_vs_adv")["status"], "WITHHELD")
            forged = copy.deepcopy(correct)
            next(c for c in forged["provenance"] if c["capture_id"] == "market")["request_parameters"] = dict(params)
            with self.assertRaisesRegex(ValueError, "market reference violates"):
                ie.validate_document(forged)


class StorageHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hardening.db")
        self.conn = tdb.connect(self.path)
        self.axis = axis_n(5)
        self.doc = build([row(d) for d in self.axis.sessions], self.axis,
                         availability_cutoff=EARLY, compatibility_scopes=(SCOPE,))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def key(self, doc=None):
        doc = self.doc if doc is None else doc
        return (doc["ticker"], doc["axis"]["start"], doc["axis"]["cutoff"], doc["request_contract_sha256"])

    def raw(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = OFF")
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        return conn

    def insert_body(self, conn, doc=None, text=None, digest=None):
        doc = self.doc if doc is None else doc
        conn.execute("INSERT INTO inventory_evidence_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (*self.key(doc), doc["observation_revision"], doc["parent_revision"], ie.VERSION,
                      doc["availability_cutoff"], digest or ie.content_hash(doc),
                      ie.canonical_json(doc) if text is None else text))

    def store(self, doc=None, now=LATE):
        with patch.object(tdb, "utc_now", return_value=now):
            return tdb.record_inventory_evidence(self.conn, self.doc if doc is None else doc)

    def read(self, cutoff=LATE):
        return tdb.inventory_evidence_as_of(self.conn, "SINI", self.axis.start, self.axis.cutoff, cutoff)

    def test_same_digest_different_canonical_body_is_not_identical(self):
        forged = copy.deepcopy(self.doc)
        forged["availability_cutoff"] = ie.utc_text(LATE)
        with closing(self.raw()) as raw, raw:
            self.insert_body(raw, text=ie.canonical_json(forged), digest=ie.content_hash(self.doc))
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            self.store()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_acceptances").fetchone()[0], 0)

    def test_preseeded_sql_metadata_must_match_canonical_evidence_body(self):
        for metadata in ((None, ie.VERSION, "2035-01-01T00:00:00.000000Z"),
                         (9, ie.VERSION, self.doc["availability_cutoff"])):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "metadata.db")
                with closing(tdb.connect(path)) as writer:
                    with sqlite3.connect(path) as raw:
                        raw.execute("PRAGMA foreign_keys=OFF")
                        raw.execute("INSERT INTO inventory_evidence_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (*self.key(), 1, *metadata, ie.content_hash(self.doc), ie.canonical_json(self.doc)))
                        raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                            (*self.key(), 1, ie.utc_text(LATE)))
                    with patch.object(tdb, "utc_now", return_value=LATE), self.assertRaisesRegex(ValueError, "revision conflict"):
                        tdb.record_inventory_evidence(writer, self.doc)
                    with self.assertRaisesRegex(ValueError, "corrupt immutable evidence metadata"):
                        tdb.latest_inventory_evidence(writer, "SINI", self.axis.start, self.axis.cutoff)

    def test_forged_body_under_legitimate_digest_is_not_served(self):
        forged = copy.deepcopy(self.doc)
        forged["availability_cutoff"] = ie.utc_text(LATE)
        with closing(self.raw()) as raw, raw:
            self.insert_body(raw, text=ie.canonical_json(forged), digest=ie.content_hash(self.doc))
            raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                        (*self.key(), 1, ie.utc_text(LATE)))
        with self.assertRaisesRegex(ValueError, "corrupt immutable evidence body"):
            self.read()
        with self.assertRaisesRegex(ValueError, "corrupt immutable evidence body"):
            tdb.latest_inventory_evidence(self.conn, "SINI", self.axis.start, self.axis.cutoff)

    def test_raw_foreign_keys_off_cannot_insert_orphan_acceptances(self):
        statements = (
            ("inventory_evidence_acceptances", (*self.key(), 1, ie.utc_text(EARLY))),
            ("inventory_basis_acceptances", ("basis", "a" * 64, ie.utc_text(EARLY))),
            ("inventory_snapshot_acceptances", ("SINI", self.axis.cutoff, "a" * 64, ie.utc_text(EARLY), "1")),
        )
        with closing(self.raw()) as raw:
            for table, values in statements:
                with self.subTest(table=table), self.assertRaisesRegex(sqlite3.IntegrityError, "requires body"):
                    raw.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in values) + ")", values)
                raw.rollback()
                self.assertEqual(raw.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_existing_backdated_evidence_acceptance_is_not_adopted_or_served(self):
        before_input = "2026-10-02T09:59:59.999999Z"
        with closing(self.raw()) as raw, raw:
            self.insert_body(raw)
            raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                        (*self.key(), 1, before_input))
        with self.assertRaisesRegex(ValueError, "input's availability"):
            self.store()
        with self.assertRaisesRegex(ValueError, "input's availability"):
            self.read()

    def test_existing_acceptance_must_respect_request_cutoff(self):
        doc = copy.deepcopy(self.doc)
        doc["availability_cutoff"] = ie.utc_text(LATE)
        with closing(self.raw()) as raw, raw:
            self.insert_body(raw, doc)
            raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                        (*self.key(doc), 1, ie.utc_text(EARLY)))
        with self.assertRaisesRegex(ValueError, "cutoff cannot be later"):
            self.store(doc)

    def test_existing_child_acceptance_must_respect_parent_durability(self):
        self.store()
        child = build([row(d, revision=2) for d in self.axis.sessions], self.axis,
                      observation_revision=2, parent_revision=1, availability_cutoff=EARLY,
                      compatibility_scopes=(SCOPE,))
        with closing(self.raw()) as raw, raw:
            self.insert_body(raw, child)
            raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                        (*self.key(child), 2, ie.utc_text(EARLY)))
        with self.assertRaisesRegex(ValueError, "precedes parent revision"):
            self.store(child)
        with self.assertRaisesRegex(ValueError, "precedes parent revision"):
            self.read()

    def test_existing_acceptance_requires_canonical_timestamp(self):
        with closing(self.raw()) as raw, raw:
            self.insert_body(raw)
            raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                        (*self.key(), 1, "2026-10-03T10:00:00+00:00"))
        with self.assertRaisesRegex(ValueError, "canonical UTC"):
            self.store()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 1)

    def test_existing_future_evidence_acceptance_is_not_adopted(self):
        with closing(self.raw()) as raw, raw:
            self.insert_body(raw)
            raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                        (*self.key(), 1, ie.utc_text(LATE)))
        with self.assertRaisesRegex(ValueError, "cannot be in the future"):
            self.store(now=EARLY)
        self.assertIsNone(self.read(EARLY))

    def test_existing_future_basis_acceptance_is_not_adopted(self):
        content = {"regimes": []}
        digest = ie.content_hash(content)
        with closing(self.raw()) as raw, raw:
            raw.execute("INSERT INTO inventory_basis_references "
                        "(source_id,content_sha256,content_json,body_recorded_at,extension_version) VALUES (?,?,?,?,?)",
                        ("basis", digest, ie.canonical_json(content), ie.utc_text(EARLY), "1"))
            raw.execute("INSERT INTO inventory_basis_acceptances VALUES (?,?,?)",
                        ("basis", digest, ie.utc_text(LATE)))
        with patch.object(tdb, "utc_now", return_value=EARLY), self.assertRaisesRegex(ValueError, "cannot be in the future"):
            tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        self.assertIsNone(tdb.inventory_basis_reference_as_of(self.conn, "basis", digest, EARLY))

    def test_basis_preseeded_before_body_recording_is_not_adopted_or_served(self):
        content = {"regimes": []}
        digest = ie.content_hash(content)
        with closing(self.raw()) as raw, raw:
            raw.execute("INSERT INTO inventory_basis_references "
                        "(source_id,content_sha256,content_json,body_recorded_at,extension_version) VALUES (?,?,?,?,?)",
                        ("basis", digest, ie.canonical_json(content), ie.utc_text(LATE), "1"))
            raw.execute("INSERT INTO inventory_basis_acceptances VALUES (?,?,?)",
                        ("basis", digest, ie.utc_text(EARLY)))
        with patch.object(tdb, "utc_now", return_value=LATE), self.assertRaisesRegex(ValueError, "precedes body recording"):
            tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        with self.assertRaisesRegex(ValueError, "precedes body recording"):
            tdb.inventory_basis_reference_as_of(self.conn, "basis", digest, LATE)

    def test_forged_basis_content_with_legitimate_hash_is_not_adopted_or_served(self):
        content = {"regimes": []}
        digest = ie.content_hash(content)
        with closing(self.raw()) as raw, raw:
            raw.execute("INSERT INTO inventory_basis_references "
                        "(source_id,content_sha256,content_json,body_recorded_at,extension_version) VALUES (?,?,?,?,?)",
                        ("basis", digest, '{}', ie.utc_text(EARLY), "1"))
            raw.execute("INSERT INTO inventory_basis_acceptances VALUES (?,?,?)",
                        ("basis", digest, ie.utc_text(LATE)))
        with patch.object(tdb, "utc_now", return_value=LATE), self.assertRaisesRegex(ValueError, "reference conflict"):
            tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        with self.assertRaisesRegex(ValueError, "corrupt immutable basis"):
            tdb.inventory_basis_reference_as_of(self.conn, "basis", digest, LATE)

    def test_legacy_orphan_acceptances_are_refused_before_body_insert(self):
        content = {"regimes": []}
        digest = ie.content_hash(content)
        # Emulate pre-existing corruption from a database created before the
        # existence triggers. Schema upgrade reinstalls the insertion guards.
        with closing(self.raw()) as raw, raw:
            for table in ("inventory_evidence_acceptances", "inventory_basis_acceptances"):
                raw.execute("DROP TRIGGER " + table + "_requires_body")
            raw.execute("INSERT INTO inventory_evidence_acceptances VALUES (?,?,?,?,?,?)",
                        (*self.key(), 1, ie.utc_text(EARLY)))
            raw.execute("INSERT INTO inventory_basis_acceptances VALUES (?,?,?)",
                        ("basis", digest, ie.utc_text(EARLY)))
        tdb.ensure_schema(self.conn)
        with self.assertRaisesRegex(ValueError, "orphan evidence acceptance"):
            self.store()
        with patch.object(tdb, "utc_now", return_value=LATE), self.assertRaisesRegex(ValueError, "orphan basis acceptance"):
            tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_evidence_revisions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inventory_basis_references").fetchone()[0], 0)

    def test_legitimate_identical_retries_preserve_original_acceptance(self):
        self.assertEqual(self.store(), "inserted")
        self.assertEqual(self.store(now="2026-10-04T10:00:00Z"), "identical")
        self.assertEqual(self.read()["known_at"], ie.utc_text(LATE))
        content = {"regimes": []}
        with patch.object(tdb, "utc_now", return_value=LATE):
            first = tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        with patch.object(tdb, "utc_now", return_value="2026-10-04T10:00:00Z"):
            retry = tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        self.assertEqual(first, retry)
        self.assertEqual(tdb.inventory_basis_reference_as_of(self.conn, "basis", first["content_sha256"], LATE), first)

    def test_basis_availability_is_sampled_only_after_durable_body_commit(self):
        content = {"regimes": []}
        digest = ie.content_hash(content)
        samples = []

        def clock():
            with sqlite3.connect(self.path) as second_reader:
                body = second_reader.execute("SELECT body_recorded_at FROM inventory_basis_references "
                                             "WHERE source_id=? AND content_sha256=?", ("basis", digest)).fetchone()
                marker = second_reader.execute("SELECT 1 FROM inventory_basis_acceptances "
                                               "WHERE source_id=? AND content_sha256=?", ("basis", digest)).fetchone()
            samples.append(body)
            self.assertIsNone(marker)
            if len(samples) == 1:
                self.assertIsNone(body)
                return EARLY
            self.assertEqual(body, (ie.utc_text(EARLY),))
            self.assertFalse(self.conn.in_transaction)
            return LATE

        with patch.object(tdb, "utc_now", side_effect=clock):
            accepted = tdb.accept_inventory_basis_reference(self.conn, "basis", content)
        self.assertEqual(len(samples), 2)
        self.assertEqual(accepted["durable_accepted_at"], ie.utc_text(LATE))
        self.assertIsNone(tdb.inventory_basis_reference_as_of(self.conn, "basis", digest, "2026-10-03T09:59:59.999999Z"))
        self.assertIsNotNone(tdb.inventory_basis_reference_as_of(self.conn, "basis", digest, LATE))




class SourceAcceptanceHardeningTests(unittest.TestCase):
    """Raw source markers cannot put a committed source before its recording."""

    def setUp(self):
        import test_targeted_actor_panel as tp
        import targeted_actor_observations as tao
        self.tao = tao
        self.tmp = tempfile.TemporaryDirectory()
        self.axis = axis_n(5)
        dates = list(self.axis.sessions)
        market = {"dates": dates, "ohlc": tp.ohlc_rows(dates, 1000),
                  "brokers": {"ES": tp.broker_series([10] * 5, [1_000_000] * 5)}}
        # Responses arrive EARLY, but the source is recorded LATE. Both clocks
        # are fixture inputs, independent of the current system time.
        with patch.object(tdb, "utc_now", return_value=LATE):
            result = tp.run(tp.FakeVendor({"SINI": market}), ["SINI"], self.tmp.name,
                            db="source-acceptance.db", clock=lambda: EARLY)
        self.path = result["db_path"]
        with closing(tdb.connect(self.path)) as conn:
            self.snapshot = tdb.snapshot(conn, "SINI", self.axis.cutoff)
            with patch.object(tdb, "utc_now", return_value=EARLY):
                tdb.accept_inventory_basis_reference(conn, tao.BASIS_SOURCE_ID, {"regimes": []})
        self.assertEqual(ie.utc_text(self.snapshot["recorded_utc"]), ie.utc_text(LATE))
        self.assertTrue(all(ie.utc_text(c["captured_at"]) == ie.utc_text(EARLY)
                            for c in self.snapshot["captures"]))
        self.scope = replace(SCOPE, basis_version=ie.content_hash({"regimes": []}))

    def tearDown(self):
        self.tmp.cleanup()

    def _preseed(self, accepted):
        with sqlite3.connect(self.path) as raw:
            raw.execute("PRAGMA foreign_keys = OFF")
            self.assertEqual(raw.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            raw.execute("INSERT INTO inventory_snapshot_acceptances VALUES (?,?,?,?,?)",
                        ("SINI", self.axis.cutoff, self.snapshot["content_sha256"], accepted, "1"))

    def _observe(self, cutoff):
        with closing(self.tao.open_readonly(self.path)) as reader:
            return self.tao.observe_inventory_evidence(
                reader, "SINI", anchor=self.axis.start, cutoff=self.axis.cutoff,
                availability_cutoff=cutoff, broker_codes=("ES",), scope=self.scope, windows=(5,))

    def test_preseeded_source_acceptance_before_recording_is_not_adopted_or_served(self):
        self._preseed(ie.utc_text(EARLY))
        with closing(tdb.connect(self.path)) as writer:
            with patch.object(tdb, "utc_now", return_value=LATE), self.assertRaises(ValueError):
                tdb.accept_inventory_snapshot(writer, "SINI", self.axis.cutoff)
        with self.assertRaises(ValueError):
            self._observe(EARLY)
        with self.assertRaises(ValueError):
            self._observe(LATE)

    def test_noncanonical_preseeded_source_acceptance_is_not_adopted_or_served(self):
        self._preseed("2026-10-03T10:00:00+00:00")
        with closing(tdb.connect(self.path)) as writer:
            with patch.object(tdb, "utc_now", return_value=LATE), self.assertRaises(ValueError):
                tdb.accept_inventory_snapshot(writer, "SINI", self.axis.cutoff)
        with self.assertRaises(ValueError):
            self._observe(LATE)

    def test_legitimate_source_acceptance_stays_invisible_before_recording_and_is_idempotent(self):
        with closing(tdb.connect(self.path)) as writer:
            with patch.object(tdb, "utc_now", return_value=LATE):
                accepted = tdb.accept_inventory_snapshot(writer, "SINI", self.axis.cutoff)
            with patch.object(tdb, "utc_now", return_value="2026-10-04T10:00:00Z"):
                self.assertEqual(tdb.accept_inventory_snapshot(writer, "SINI", self.axis.cutoff), accepted)
        self.assertEqual(accepted, ie.utc_text(LATE))
        earlier = self._observe("2026-10-03T09:59:59.999999Z")
        self.assertTrue(all(r["coverage"] == ie.UNOBSERVED for r in series(earlier)))
        visible = self._observe(LATE)
        self.assertEqual(metric(visible, "net_flow_slope")["value"], 10)
        self.assertEqual(visible["max_input_known_at"], ie.utc_text(LATE))


if __name__ == "__main__":
    unittest.main()
