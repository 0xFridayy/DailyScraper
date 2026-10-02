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
                            axis, windows=(2,))
                rows = series(doc)
                self.assertIsNone(rows[1]["cumulative_observable_lots"])
                self.assertEqual(rows[1]["segment_cumulative_net_lots"], 10)
                self.assertIn(field.upper(), rows[1]["break_reason"])
                self.assertEqual(metric(doc, "net_flow_slope", 2)["null_reason"], "INCOMPATIBLE_SEGMENTS")
        doc = build([row(axis.sessions[0]), row(axis.sessions[1], scope=replace(SCOPE, source_measurement_contract="BILLIONS"))], axis)
        self.assertEqual(series(doc)[1]["coverage"], ie.INVALID)

    def test_explicit_followup_and_request_mismatches_cannot_be_labeled_selector_union(self):
        axis = axis_n(1)
        explicit = replace(capture(), requested_selectors=(), requested_brokers=("ES",))
        mislabeled = build([row(axis.sessions[0], cap=explicit)], axis)
        self.assertEqual(series(mislabeled)[0]["coverage"], ie.INVALID)
        self.assertEqual(series(mislabeled)[0]["null_reason"], "CAPTURE_SCOPE_MISMATCH")
        correct = build([row(axis.sessions[0], cap=explicit, scope=replace(SCOPE, capture_scope="EXPLICIT_FOLLOWUP"))], axis)
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
        self.assertIsNone(earlier["known_at"])
        self.assertEqual(earlier["provenance"], [])
        later = build(rows, axis)
        self.assertEqual(later["known_at"], ie.utc_text(LATE))
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
        earlier = build(rows + [conflict, correction], axis, observation_revision=2, availability_cutoff=EARLY)
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
        doc = build([row(axis.sessions[0], scope=replace(SCOPE, market_scope="UNKNOWN"))], axis)
        self.assertEqual(series(doc)[0]["coverage"], ie.INVALID)
        self.assertIsNone(series(doc)[0]["cumulative_observable_lots"])
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
        self.assertEqual(rotation["concurrent_positive_brokers"], ["CC"])
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
        self.assertEqual(latest["evidence"]["known_at"], ie.utc_text(EARLY))
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
        with patch.object(tdb, "utc_now", side_effect=RuntimeError("interrupted after body commit")):
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
                          for d in axis.sessions], axis)
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
            result = tp.run(tp.FakeVendor({"SINI": market}), ["SINI"], self.tmp.name, db="source.db")
        self.assertEqual(result["status"], "ok")
        source = result["db_path"]
        basis = tao._basis_reference()
        scope = replace(SCOPE, basis_version=basis["canonical_json_sha256"])
        args = dict(anchor=dates[0], cutoff=dates[-1], availability_cutoff=LATE, broker_codes=("ES", "ZZ"),
                    scope=scope, basis_reference_known_at=EARLY, windows=(5,))
        with closing(tao.open_readonly(source)) as conn:
            original = tao.observation_json(tao.observe(conn, "SINI"))
            missing = tao.observe_inventory_evidence(conn, "SINI", **args)
            self.assertTrue(all(r["coverage"] == ie.UNOBSERVED for r in series(missing)))
        writer = tdb.connect(source)
        try:
            with patch.object(tdb, "utc_now", return_value=LATE):
                accepted = tdb.accept_inventory_snapshot(writer, "SINI", dates[-1])
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
            future_basis = tao.observe_inventory_evidence(conn, "SINI", **dict(args, basis_reference_known_at="2026-10-04T10:00:00Z"))
            self.assertEqual(future_basis["provenance"], [])
            with self.assertRaises(ValueError):
                tao.observe_inventory_evidence(conn, "SINI", **dict(args, scope=replace(scope, capture_scope="EXPLICIT_FOLLOWUP")))
            conflict_path = os.path.join(self.tmp.name, "basis.json")
            Path(conflict_path).write_text(json.dumps({"regimes": [{"ticker": "SINI", "regime_first_date": dates[2],
                                                                   "regime_last_date": dates[2]}]}))
            with patch.object(tao, "BASIS_FILE", conflict_path):
                conflict_scope = replace(scope, basis_version=tao._basis_reference()["canonical_json_sha256"])
                quarantined = tao.observe_inventory_evidence(conn, "SINI", **dict(args, scope=conflict_scope))
                self.assertEqual(series(quarantined)[2]["coverage"], ie.QUARANTINED)
                self.assertEqual(series(quarantined)[2]["null_reason"], "KNOWN_BASIS_CONFLICT")
                self.assertIsNone(series(quarantined)[-1]["cumulative_observable_lots"])
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


if __name__ == "__main__":
    unittest.main()
