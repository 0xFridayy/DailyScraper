"""Synthetic offline trade-capture tests. Run python test_bandarmolony_trade_capture.py.

Fixtures contain invented executions only. No source account, credentials,
network collection, paid Parquet, OHLC, or actor data is used.
"""

from contextlib import closing
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
import stat
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

import bandarmolony_trade_contract as contract
import bandarmolony_trade_capture as capture


RECENT_TYPES = {
    "TRX_CODE": pa.int64(), "TRX_SESS": pa.int32(), "TRX_TYPE": pa.string(),
    "BRK_COD1": pa.string(), "INV_TYP1": pa.string(), "BRK_COD2": pa.string(),
    "INV_TYP2": pa.string(), "STK_CODE": pa.string(), "STK_VOLM": pa.int64(),
    "STK_PRIC": pa.int64(), "TRX_DATE": pa.date32(), "TRX_ORD1": pa.int64(),
    "TRX_ORD2": pa.int64(), "TRX_TIME": pa.int32(), "HAKA_HAKI": pa.string(),
    "VALUE": pa.decimal128(28, 2),
}
CANONICAL_FIELDS = {
    "ticker", "trade_date", "trx_code", "session", "board", "buyer_broker",
    "buyer_investor_type", "seller_broker", "seller_investor_type", "shares",
    "price_idr", "value_rp", "buy_order_no", "sell_order_no", "trade_time",
    "vendor_haka_haki", "source_capture_id", "source_schema_version",
}


def envelope(name="capture-a", **changes):
    """Give each numbered observation independent synthetic request timestamps."""
    values = dict(
        capture_id=name, ticker="DEWA", trade_date="2026-10-01",
        requested_at="2026-10-01T10:00:00.123456Z",
        response_at="2026-10-01T10:00:01.654321Z",
        last_modified="2026-10-01T01:59:59.111111Z",
        x_ms_creation_time="2026-10-01T01:58:00.222222Z",
        x_ms_request_id="12345678-1234-4234-8234-123456789abc",
        source_path_without_query_or_token="https://example.invalid/trade/DEWA.parquet",
    )
    values.update(changes)
    return contract.CaptureEnvelope(**values)


def observation(name, minute, **changes):
    stamp = datetime(2026, 10, 1, 10, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return envelope(name, requested_at=stamp.isoformat(),
                    response_at=(stamp + timedelta(seconds=1)).isoformat(), **changes)


def source_row(**changes):
    values = dict(
        TRX_CODE=1001, TRX_SESS=1, TRX_TYPE="NG", BRK_COD1="AB", INV_TYP1="D",
        BRK_COD2="CD", INV_TYP2="F", STK_CODE="DEWA", STK_VOLM=125,
        STK_PRIC=1270, TRX_DATE=date(2026, 10, 1), TRX_ORD1=10,
        TRX_ORD2=20, TRX_TIME=90101, HAKA_HAKI="HAKA",
    )
    values.update(changes)
    if "VALUE" not in changes:
        value = values["STK_VOLM"] * values["STK_PRIC"]
        values["VALUE"] = Decimal(f"{value // 100}.{value % 100:02d}")
    return values


def sample_rows():
    return [
        source_row(),
        source_row(TRX_CODE=1002, TRX_TYPE="RG", BRK_COD2="EF", STK_VOLM=300,
                   STK_PRIC=1280, TRX_ORD2=21, TRX_TIME=90102, HAKA_HAKI="HAKI"),
        source_row(TRX_CODE=1003, TRX_TYPE="RG", BRK_COD1="CD", BRK_COD2="AB",
                   STK_VOLM=75, STK_PRIC=1290, TRX_ORD1=11, TRX_ORD2=22,
                   TRX_TIME=160001, HAKA_HAKI="HAKA"),
    ]


def write_parquet(path, rows=None, *, legacy=False, field_types=None, drop=(),
                  extra=None, compression="zstd", use_dictionary=True,
                  row_group_size=None, metadata=None, required=False):
    """Write a tiny realistic compressed fixture, with explicit physical types."""
    rows = [dict(row) for row in (sample_rows() if rows is None else rows)]
    types = dict(RECENT_TYPES)
    if legacy:
        for name in ("STK_CODE", "TRX_DATE"):
            types.pop(name)
        types["TRX_TYPE"] = pa.int32()
        for row in rows:
            row["TRX_TYPE"] = 0
    types.update(field_types or {})
    for name in drop:
        types.pop(name, None)
    for name, (typ, value) in (extra or {}).items():
        types[name] = typ
        for row in rows:
            row[name] = value
    schema = pa.schema([pa.field(name, typ, nullable=not required)
                        for name, typ in types.items()], metadata=metadata)
    table = pa.Table.from_pylist(rows, schema=schema)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression=compression, use_dictionary=use_dictionary,
                   row_group_size=row_group_size)
    return path


def remove_readonly(path):
    """Windows applies a read-only attribute to chmod(0400) raw fixtures."""
    path = Path(path)
    path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.unlink()


def make_tree_writable(root):
    """Run after connections close and before TemporaryDirectory removes raw files."""
    root = Path(root)
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IWRITE | stat.S_IREAD)


def rewrite_immutable_row(conn, sql, parameters=(), *, reseal_capture_id=None):
    """Simulate an adversary, restoring trigger definitions before verification."""
    triggers = list(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"))
    conn.commit()
    for name, _ in triggers:
        conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    conn.execute(sql, parameters)
    if reseal_capture_id is not None:
        record = conn.execute("SELECT * FROM trade_captures WHERE capture_id=?",
                              (reseal_capture_id,)).fetchone()
        digest = capture._body_digest(record)
        conn.execute("UPDATE trade_captures SET body_sha256=? WHERE capture_id=?",
                     (digest, reseal_capture_id))
        conn.execute("UPDATE trade_acceptances SET body_sha256=? WHERE capture_id=?",
                     (digest, reseal_capture_id))
    for _, ddl in triggers:
        conn.execute(ddl)
    conn.commit()


class TradeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="trade-capture-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(make_tree_writable, self.root)
        self.db = self.root / "private" / "trade_capture.db"
        self.raw = self.root / "private" / "trade_raw"
        self.store = capture.TradeCaptureStore(self.db, raw_root=self.raw)
        self.addCleanup(self.store.close)
        self.sequence = 0

    def ingest(self, rows=None, *, env=None, **fixture_options):
        self.sequence += 1
        if env is None:
            stamp = datetime(2026, 10, 1, 10, tzinfo=timezone.utc) + timedelta(minutes=self.sequence)
            env = envelope(f"capture-{self.sequence}", requested_at=stamp.isoformat(),
                           response_at=(stamp + timedelta(seconds=1)).isoformat())
        path = write_parquet(self.root / f"input-{self.sequence}.parquet", rows,
                             **fixture_options)
        return self.store.ingest(path, env)

    def rows(self, result):
        return self.store.read_rows(result["capture_id"])

    def assert_rejected(self, rows=None, **options):
        with self.assertRaises(contract.TradeContractError):
            self.ingest(rows, **options)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)
        self.assertEqual(list(self.raw.rglob("*.parquet")), [])
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])

    def test_recent_valid_ingest(self):
        result = self.ingest()
        self.assertEqual(result["schema_version"], contract.RECENT_SCHEMA_VERSION)
        self.assertEqual(result["dataset"], "BANDARMOLONY_DONE_DETAIL")
        self.assertEqual(result["row_count"], 3)
        rows = self.rows(result)
        self.assertEqual(set(rows[0]), CANONICAL_FIELDS)
        self.assertEqual([r["trx_code"] for r in rows], [1001, 1002, 1003])
        self.assertTrue(all(r["source_capture_id"] == result["capture_id"] for r in rows))
        self.assertTrue(result["durable_accepted_at"])
        self.assertTrue(self.store.verify(result["capture_id"]))

    def test_legacy_valid_ingest(self):
        result = self.ingest(legacy=True, env=envelope(trade_date="2025-12-30"))
        self.assertEqual(result["schema_version"], "LEGACY_2025_14COL")
        self.assertTrue(all(r["ticker"] == "DEWA" and r["trade_date"] == "2025-12-30"
                            for r in self.rows(result)))

    def test_recent_ticker_mismatch(self):
        self.assert_rejected([source_row(STK_CODE="BBCA")])

    def test_recent_date_mismatch(self):
        self.assert_rejected([source_row(TRX_DATE=date(2026, 10, 2))])

    def test_unsupported_schema(self):
        self.assert_rejected(extra={"UNEXPECTED": (pa.int32(), 7)})

    def test_missing_required_column(self):
        self.assert_rejected(drop=("TRX_ORD1",))

    def test_wrong_physical_type(self):
        self.assert_rejected([source_row(STK_VOLM=125.0, VALUE=Decimal("1587.50"))],
                             field_types={"STK_VOLM": pa.float64()})

    def test_odd_share_ng_quantity(self):
        row = self.rows(self.ingest([source_row()]))[0]
        self.assertEqual(row["shares"], 125)
        self.assertEqual(row["board"], "NG")
        self.assertNotIn("lots", row)

    def test_exact_value_normalization(self):
        row = self.rows(self.ingest([source_row()]))[0]
        self.assertEqual(row["value_rp"], 158750)
        self.assertIs(type(row["value_rp"]), int)
        self.assertEqual(row["value_rp"], row["shares"] * row["price_idr"])

    def test_bad_value_mismatch(self):
        self.assert_rejected([source_row(VALUE=Decimal("1587.51"))])

    def test_buyer_seller_mapping(self):
        row = self.rows(self.ingest([source_row()]))[0]
        self.assertEqual((row["buyer_broker"], row["seller_broker"]), ("AB", "CD"))
        self.assertEqual((row["buyer_investor_type"], row["seller_investor_type"]), ("D", "F"))

    def test_same_broker_both_sides(self):
        row = self.rows(self.ingest([source_row(BRK_COD1="AB", BRK_COD2="AB")]))[0]
        totals = contract.broker_totals([row])["AB"]
        self.assertEqual(totals["buy_shares"], 125)
        self.assertEqual(totals["sell_shares"], 125)
        self.assertEqual(totals["net_shares"], 0)
        self.assertEqual(totals["net_value_rp"], 0)
        self.assertEqual((totals["trade_count_buy"], totals["trade_count_sell"]), (1, 1))

    def test_identical_duplicate_deduplicates_with_provenance(self):
        path = write_parquet(self.root / "duplicate.parquet", [source_row(), source_row()])
        normalized = contract.normalize_parquet(path, envelope())
        self.assertEqual(len(normalized.rows), 1)
        self.assertEqual(normalized.source_row_count, 2)
        self.assertEqual(dict(normalized.duplicate_counts)[1001], 2)
        result = self.store.ingest(path, envelope())
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["source_row_count"], 2)
        self.assertEqual(len(self.rows(result)), 1)
        self.assertEqual(contract.tape_summary(self.rows(result))["total_shares"], 125)

    def test_conflicting_duplicate_rejected(self):
        self.assert_rejected([source_row(), source_row(BRK_COD2="EF")])

    def test_one_order_multiple_executions(self):
        rows = self.rows(self.ingest())
        summary = contract.tape_summary(rows)
        self.assertEqual(summary["unique_buy_orders"], 2)
        self.assertEqual(summary["unique_sell_orders"], 3)
        self.assertEqual(summary["fills_per_buy_order"][10], 2)
        self.assertEqual(sum(summary["fills_per_buy_order"].values()), 3)

    def test_raw_hash_exact_bytes_and_deduplication(self):
        result = self.ingest()
        raw_bytes = (self.root / "input-1.parquet").read_bytes()
        self.assertEqual(result["raw_response_sha256"], hashlib.sha256(raw_bytes).hexdigest())
        stored = list(self.raw.rglob("*.parquet"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), raw_bytes)
        repeated = self.ingest()
        self.assertEqual(repeated["raw_response_sha256"], result["raw_response_sha256"])
        self.assertEqual(len(list(self.raw.rglob("*.parquet"))), 1)

    def test_normalized_hash_deterministic(self):
        result = self.ingest()
        rows = [{k: v for k, v in row.items() if k != "source_capture_id"}
                for row in self.rows(result)]
        expected = hashlib.sha256(contract.canonical_json(contract.normalized_document(rows)).encode()).hexdigest()
        self.assertEqual(result["normalized_content_sha256"], expected)
        self.assertEqual(contract.normalized_hash(rows), expected)

    def test_shuffled_rows_same_normalized_hash(self):
        first = self.ingest()
        second = self.ingest(list(reversed(sample_rows())))
        self.assertNotEqual(first["raw_response_sha256"], second["raw_response_sha256"])
        self.assertEqual(first["normalized_content_sha256"], second["normalized_content_sha256"])
        self.assertEqual(second["observation_state"], "CONTENT_REPEAT")
        self.assertEqual(second["content_version"], first["content_version"])

    def test_capture_timestamps_do_not_change_hash(self):
        path = write_parquet(self.root / "times.parquet")
        first = contract.normalize_parquet(path, envelope("capture-a"))
        second = contract.normalize_parquet(path, envelope(
            "capture-b", requested_at="2026-10-02T10:00:00.000001Z",
            response_at="2026-10-02T10:00:01.000002Z"))
        self.assertEqual(first.normalized_content_sha256, second.normalized_content_sha256)
        self.assertEqual(first.content_json, second.content_json)
        self.assertNotIn("source_capture_id", first.content_json)
        self.assertNotIn("requested_at", first.content_json)

    def test_first_seen(self):
        result = self.ingest()
        self.assertEqual(result["observation_state"], "CONTENT_FIRST_SEEN")
        self.assertEqual(result["observation_seq"], 1)
        self.assertIsNone(result["previous_observation_id"])
        self.assertIsNone(result["previous_content_capture_id"])
        self.assertEqual(result["content_version"], 1)

    def test_revised_immutable_history(self):
        first = self.ingest([source_row()])
        second = self.ingest([source_row(STK_VOLM=126)])
        self.assertEqual(second["observation_state"], "CONTENT_CHANGED")
        self.assertEqual(second["observation_seq"], 2)
        self.assertEqual(second["previous_observation_id"], first["capture_id"])
        self.assertEqual(second["previous_content_capture_id"], first["capture_id"])
        self.assertEqual(second["content_version"], 2)
        self.assertNotEqual(first["raw_response_sha256"], second["raw_response_sha256"])
        self.assertEqual(self.rows(first)[0]["shares"], 125)
        self.assertEqual(self.rows(second)[0]["shares"], 126)
        self.assertEqual(self.store.inspect(first["capture_id"])["observation_state"], "CONTENT_FIRST_SEEN")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 2)

    def test_repeat_confirmation(self):
        first = self.ingest()
        repeat = self.ingest()
        self.assertEqual(repeat["observation_state"], "CONTENT_REPEAT")
        self.assertNotEqual(first["capture_id"], repeat["capture_id"])
        self.assertEqual(first["raw_response_sha256"], repeat["raw_response_sha256"])
        self.assertEqual(first["normalized_content_sha256"], repeat["normalized_content_sha256"])
        self.assertEqual(repeat["observation_seq"], 2)
        self.assertEqual(repeat["content_version"], 1)
        self.assertEqual(repeat["previous_content_capture_id"], first["capture_id"])

    def test_old_version_remains_queryable_after_repeat(self):
        first = self.ingest([source_row()])
        changed = self.ingest([source_row(STK_VOLM=130)])
        repeated = self.ingest([source_row(STK_VOLM=130)])
        self.assertEqual(self.rows(first)[0]["shares"], 125)
        self.assertEqual(self.rows(changed)[0]["shares"], 130)
        self.assertEqual(self.rows(repeated)[0]["shares"], 130)
        for result in (first, changed, repeated):
            self.assertTrue(self.store.verify(result["capture_id"]))

    def test_no_finality_state(self):
        results = [self.ingest(), self.ingest(), self.ingest([source_row(STK_VOLM=126)])]
        self.assertEqual(set(contract.OBSERVATION_STATES),
                         {"CONTENT_FIRST_SEEN", "CONTENT_CHANGED", "CONTENT_REPEAT", "ABSENT_OBSERVED"})
        for result in results:
            self.assertNotIn("FINAL", result["observation_state"])

    def test_absence_is_not_zero_trades(self):
        result = self.store.observe_absence(envelope(http_status=404))
        self.assertEqual(result["observation_state"], "ABSENT_OBSERVED")
        for key in ("row_count", "raw_response_sha256", "normalized_content_sha256", "content_length",
                    "content_version"):
            self.assertIsNone(result[key])
        self.assertTrue(result["durable_accepted_at"])
        self.assertEqual(list(self.raw.rglob("*.parquet")), [])
        with self.assertRaises(contract.TradeContractError):
            self.store.read_rows(result["capture_id"])
        self.assertTrue(self.store.verify(result["capture_id"]))

    def test_private_output_guard(self):
        with tempfile.TemporaryDirectory(prefix="trade-git-guard-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            ignored = root / "private_data" / "capture.db"
            (root / ".gitignore").write_text("private_data/\n", encoding="utf-8")
            capture.check_private_output(ignored)
            with self.assertRaises(contract.TradeContractError):
                capture.check_private_output(root / "public" / "capture.db")

    def test_tracked_output_refused(self):
        with tempfile.TemporaryDirectory(prefix="trade-tracked-guard-") as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / ".gitignore").write_text("private_data/\n", encoding="utf-8")
            tracked = root / "private_data" / "trade_raw" / "existing.parquet"
            tracked.parent.mkdir(parents=True)
            tracked.write_bytes(b"synthetic tracked marker")
            subprocess.run(["git", "-C", str(root), "add", "-f", str(tracked)],
                           check=True, capture_output=True)
            with self.assertRaises(contract.TradeContractError):
                capture.check_private_output(tracked.parent)
            with self.assertRaises(contract.TradeContractError):
                capture.TradeCaptureStore(root / "private_data" / "capture.db", raw_root=tracked.parent)

    def test_source_path_strips_dummy_tokens_everywhere(self):
        dummy = "dummy-secret-never-persist"
        env = envelope(source_path_without_query_or_token=(
            "https://dummy-user:dummy-password@example.invalid/trade/DEWA.parquet"
            f"?sv=2026&sig={dummy}&session={dummy}&cookie={dummy}#{dummy}"))
        result = self.ingest(env=env)
        stored = self.store.conn.execute("SELECT metadata_json FROM trade_captures").fetchone()[0]
        surfaces = [repr(env), stored, json.dumps(result), json.dumps(self.store.inspect(env.capture_id)),
                    self.db.read_bytes().decode("latin1")]
        for text in surfaces:
            for forbidden in (dummy, "dummy-user", "dummy-password", "sig=", "session=", "cookie="):
                self.assertNotIn(forbidden, text)
        self.assertEqual(env.source_path_without_query_or_token,
                         "https://example.invalid/trade/DEWA.parquet")

    def test_auth_fields_cannot_be_persisted(self):
        names = {field.name for field in fields(contract.CaptureEnvelope)}
        self.assertTrue(names.isdisjoint({"sig", "token", "authorization", "cookie", "password", "username"}))
        for name in ("token", "sig", "authorization", "cookie", "password", "username"):
            with self.subTest(name=name), self.assertRaises(TypeError):
                envelope(**{name: "dummy-never-persist"})
        result = self.ingest()
        persisted = json.loads(self.store.conn.execute("SELECT metadata_json FROM trade_captures").fetchone()[0])
        self.assertTrue(set(persisted).isdisjoint({"token", "sig", "authorization", "cookie", "password", "username"}))
        self.assertTrue(result["raw_response_sha256"])

    def test_raw_tampering_detected(self):
        result = self.ingest(metadata={b"synthetic_marker": b"AAAA"})
        raw_file = next(self.raw.rglob("*.parquet"))
        changed = write_parquet(self.root / "changed.parquet", metadata={b"synthetic_marker": b"BBBB"})
        self.assertEqual(raw_file.stat().st_size, changed.stat().st_size)
        self.assertNotEqual(raw_file.read_bytes(), changed.read_bytes())
        raw_file.chmod(0o600)
        raw_file.write_bytes(changed.read_bytes())
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_normalized_hash_tampering_detected(self):
        result = self.ingest()
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_captures SET normalized_content_sha256=? WHERE capture_id=?",
                              ("0" * 64, result["capture_id"]), reseal_capture_id=result["capture_id"])
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_normalized_body_tampering_detected(self):
        result = self.ingest()
        document = json.loads(self.store.conn.execute("SELECT content_json FROM trade_captures").fetchone()[0])
        document["rows"][0]["buyer_broker"] = "ZZ"
        changed = contract.canonical_json(document)
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_captures SET content_json=? WHERE capture_id=?",
                              (changed, result["capture_id"]), reseal_capture_id=result["capture_id"])
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_acceptance_before_body_refused(self):
        self.store.conn.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute(
                "INSERT INTO trade_acceptances(capture_id,body_sha256,durable_accepted_at) VALUES (?,?,?)",
                ("orphan-capture", "0" * 64, "2026-10-03T01:00:00.123456+00:00"))
        self.store.conn.rollback()
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)

    def test_insert_or_replace_refused(self):
        result = self.ingest()
        for table in ("trade_captures", "trade_acceptances"):
            with self.subTest(table=table), self.assertRaises(sqlite3.DatabaseError):
                self.store.conn.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} WHERE capture_id=?",
                                        (result["capture_id"],))
            self.store.conn.rollback()
        self.assertEqual(self.rows(result)[0]["shares"], 125)

    def test_broker_day_share_accounting_invariant(self):
        rows = self.rows(self.ingest())
        totals = contract.broker_totals(rows)
        self.assertEqual(list(totals), sorted(totals))
        self.assertEqual(sum(v["buy_shares"] for v in totals.values()), 500)
        self.assertEqual(sum(v["sell_shares"] for v in totals.values()), 500)
        self.assertEqual(sum(v["net_shares"] for v in totals.values()), 0)
        self.assertEqual(totals["AB"]["buy_shares"], 425)
        self.assertEqual(totals["AB"]["sell_shares"], 75)

    def test_broker_day_value_accounting_invariant(self):
        rows = self.rows(self.ingest())
        totals = contract.broker_totals(rows)
        value = sum(row["value_rp"] for row in rows)
        self.assertEqual(value, 639500)
        self.assertEqual(sum(v["buy_value_rp"] for v in totals.values()), value)
        self.assertEqual(sum(v["sell_value_rp"] for v in totals.values()), value)
        self.assertEqual(sum(v["net_value_rp"] for v in totals.values()), 0)
        self.assertEqual(contract.tape_summary(rows)["total_value_rp"], value)

    def test_haka_haki_is_vendor_only(self):
        result = self.ingest()
        rows = self.rows(result)
        self.assertEqual(rows[0]["vendor_haka_haki"], "HAKA")
        self.assertEqual(rows[1]["vendor_haka_haki"], "HAKI")
        forbidden = {"aggressor", "initiator", "buyer_initiated", "seller_initiated"}
        for row in rows:
            self.assertTrue(set(row).isdisjoint(forbidden))
        self.assertTrue(set(self.store.inspect(result["capture_id"])).isdisjoint(forbidden))

    def test_legacy_board_unknown(self):
        rows = self.rows(self.ingest(legacy=True))
        self.assertTrue(all(row["board"] == "UNKNOWN" for row in rows))

    def test_recent_board_preserved(self):
        rows = self.rows(self.ingest())
        self.assertEqual([row["board"] for row in rows], ["NG", "RG", "RG"])

    def test_decimal_context_and_path_determinism(self):
        path = write_parquet(self.root / "decimal.parquet")
        hashes = []
        for precision in (4, 9, 28, 50):
            with localcontext() as ctx:
                ctx.prec = precision
                tape = contract.normalize_parquet(path, envelope(
                    source_path_without_query_or_token=r"C:\private\DEWA.parquet?sig=dummy"))
                hashes.append(tape.normalized_content_sha256)
        other = contract.normalize_parquet(path, envelope(
            source_path_without_query_or_token="/different/path/DEWA.parquet"))
        self.assertEqual(len(set(hashes + [other.normalized_content_sha256])), 1)

    def test_idempotent_ingest_retry(self):
        path = write_parquet(self.root / "retry.parquet")
        env = envelope()
        first = self.store.ingest(path, env)
        retry = self.store.ingest(path, env)
        self.assertEqual(first, retry)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 1)

    def test_crash_after_body_commit_can_retry_acceptance(self):
        path = write_parquet(self.root / "crash-retry.parquet")
        env = envelope()
        transaction = self.store._write_transaction
        calls = 0

        def fail_before_acceptance():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic interruption after body commit")
            return transaction()

        with patch.object(self.store, "_write_transaction", side_effect=fail_before_acceptance):
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                self.store.ingest(path, env)
        with closing(sqlite3.connect(self.db)) as independent:
            self.assertEqual(independent.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
            self.assertEqual(independent.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(env.capture_id)
        recorded = self.store.conn.execute("SELECT body_recorded_at FROM trade_captures").fetchone()[0]
        path.unlink()  # Resume needs the sealed body and immutable object, not the original input.
        accepted = self.store.resume(env.capture_id)
        self.assertEqual(accepted["observation_seq"], 1)
        self.assertEqual(accepted["observation_state"], "CONTENT_FIRST_SEEN")
        self.assertTrue(accepted["durable_accepted_at"])
        self.assertEqual(accepted["body_recorded_at"], recorded)
        self.assertGreaterEqual(accepted["durable_accepted_at"], recorded)
        self.assertEqual(self.store.resume(env.capture_id), accepted)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 1)

    def test_resealed_forged_normalized_body_not_adopted(self):
        path = write_parquet(self.root / "forged-retry.parquet")
        env = envelope()
        self.store.ingest(path, env)
        document = json.loads(self.store.conn.execute("SELECT content_json FROM trade_captures").fetchone()[0])
        document["rows"][0]["buyer_broker"] = "ZZ"
        forged = contract.canonical_json(document)
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_captures SET content_json=? WHERE capture_id=?",
                              (forged, env.capture_id), reseal_capture_id=env.capture_id)
        with self.assertRaises(contract.TradeContractError):
            self.store.ingest(path, env)
        self.assertEqual(self.store.conn.execute("SELECT content_json FROM trade_captures").fetchone()[0], forged)

    def test_same_trade_sequence_allowed_in_different_ticker_day(self):
        first = self.ingest([source_row()])
        second = self.ingest([source_row(STK_CODE="BBCA")],
                             env=envelope("capture-bbca", ticker="BBCA"))
        self.assertEqual(self.rows(first)[0]["trx_code"], self.rows(second)[0]["trx_code"])
        self.assertEqual(second["observation_seq"], 1)
        self.assertEqual(second["observation_state"], "CONTENT_FIRST_SEEN")

    def test_parser_batch_size_determinism(self):
        path = write_parquet(self.root / "batch-sizes.parquet", sample_rows() + [source_row()])
        tapes = [contract.normalize_parquet(path, envelope(), batch_size=n) for n in (1, 2, 65536)]
        self.assertEqual(len({tape.content_json for tape in tapes}), 1)
        self.assertEqual(len({tape.normalized_content_sha256 for tape in tapes}), 1)
        self.assertTrue(all(tape.duplicate_counts == tapes[0].duplicate_counts for tape in tapes))

    def test_parser_rejects_uri_strings_before_arrow(self):
        for scheme in ("https", "http", "s3", "gs", "abfs", "file"):
            with self.subTest(scheme=scheme):
                with patch.object(pq, "ParquetFile") as parser:
                    with self.assertRaises(contract.TradeContractError):
                        contract.normalize_parquet(f"{scheme}://example.invalid/DEWA.parquet", envelope())
                    parser.assert_not_called()

    def test_parser_passes_local_handles(self):
        local = write_parquet(self.root / "local.parquet")
        paths = [str(local), local]
        # A colon is legal in a POSIX directory, but an invalid Windows name.
        if os.name != "nt":
            paths.append(write_parquet(self.root / "s3:" / "synthetic-bucket" / "local.parquet"))
        expected = contract.normalize_parquet(local, envelope())
        for path in paths:
            with self.subTest(path=path), patch.object(pq, "ParquetFile", wraps=pq.ParquetFile) as parser:
                actual = contract.normalize_parquet(path, envelope())
                parser.assert_called_once()
                supplied = parser.call_args.args[0]
                self.assertTrue(callable(getattr(supplied, "read", None)))
                self.assertEqual(Path(supplied.name), Path(path))
                self.assertEqual(actual.normalized_content_sha256, expected.normalized_content_sha256)

    def test_parser_byte_snapshot_matches_local_file(self):
        path = write_parquet(self.root / "snapshot.parquet", sample_rows() + [source_row()])
        local = contract.normalize_parquet(path, envelope(), batch_size=1)
        snapshot = contract.normalize_parquet(path.read_bytes(), envelope(), batch_size=2)
        self.assertEqual(snapshot.rows, local.rows)
        self.assertEqual(snapshot.content_json, local.content_json)
        self.assertEqual(snapshot.normalized_content_sha256, local.normalized_content_sha256)
        self.assertEqual(snapshot.schema_fingerprint, local.schema_fingerprint)
        self.assertEqual(snapshot.duplicate_counts, local.duplicate_counts)
        self.assertEqual(snapshot.source_row_count, local.source_row_count)

    def test_ingest_hash_and_parser_use_same_raw_snapshot(self):
        original = write_parquet(self.root / "race-a.parquet", [source_row()])
        replacement = write_parquet(self.root / "race-b.parquet", [source_row(STK_VOLM=130)])
        original_bytes = original.read_bytes()
        parsed = []
        parser = capture.normalize_parquet

        def replace_input_after_snapshot(source, env):
            if not parsed:
                self.assertFalse(self.store.raw_path(contract.sha256_bytes(original_bytes)).exists())
            original.write_bytes(replacement.read_bytes())
            tape = parser(source, env)
            parsed.append((source, tape.rows[0]["shares"]))
            return tape

        with patch.object(capture, "normalize_parquet", side_effect=replace_input_after_snapshot):
            result = self.store.ingest(original, envelope())
        self.assertEqual(result["raw_response_sha256"], contract.sha256_bytes(original_bytes))
        self.assertEqual(self.store.raw_path(result["raw_response_sha256"]).read_bytes(), original_bytes)
        persisted = json.loads(self.store.conn.execute("SELECT content_json FROM trade_captures").fetchone()[0])
        self.assertEqual(persisted["rows"][0]["shares"], 125)
        self.assertTrue(parsed)
        self.assertTrue(all(source == original_bytes and shares == 125 for source, shares in parsed))
        self.assertEqual(self.rows(result)[0]["shares"], 125)

    def test_verify_hash_and_parser_use_same_raw_snapshot(self):
        result = self.ingest([source_row()])
        raw_file = self.store.raw_path(result["raw_response_sha256"])
        original_bytes = raw_file.read_bytes()
        replacement = write_parquet(self.root / "verify-race-b.parquet", [source_row(STK_VOLM=130)])
        parsed = []
        parser = capture.normalize_parquet

        def swap_parse_restore(source, env):
            raw_file.chmod(0o600)
            raw_file.write_bytes(replacement.read_bytes())
            try:
                tape = parser(source, env)
                parsed.append((source, tape.rows[0]["shares"]))
                return tape
            finally:
                raw_file.write_bytes(original_bytes)
                raw_file.chmod(0o400)

        with patch.object(capture, "normalize_parquet", side_effect=swap_parse_restore):
            verified = self.store.verify(result["capture_id"])
        self.assertEqual(verified["raw_response_sha256"], result["raw_response_sha256"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0], (original_bytes, 125))
        self.assertEqual(raw_file.read_bytes(), original_bytes)

    def test_credential_shaped_storage_paths_rejected_before_persistence(self):
        dummy = "dummy-secret-never-persist"
        for component in (f"sig={dummy}", f"raw?sig={dummy}", f"raw#session={dummy}",
                          f"raw#cookie={dummy}"):
            for destination in ("database", "raw-root"):
                with self.subTest(component=component, destination=destination):
                    path = self.root / component
                    db = path / "capture.db" if destination == "database" else self.root / "guarded.db"
                    raw = self.root / "safe-raw" if destination == "database" else path
                    with self.assertRaises(contract.TradeContractError) as rejected:
                        with capture.TradeCaptureStore(db, raw_root=raw):
                            pass
                    self.assertNotIn(dummy, str(rejected.exception))
                    self.assertFalse(db.exists())
                    self.assertFalse(path.exists())
        stored = dict(self.store.conn.execute("SELECT key, value FROM trade_store_meta"))
        self.assertNotIn(dummy, json.dumps(stored))
        self.assertNotIn(dummy, self.db.read_bytes().decode("latin1"))

    def test_future_absence_observation_refused(self):
        requested = datetime.now(timezone.utc) + timedelta(days=1)
        future = envelope("capture-future-absence", http_status=404,
                          requested_at=requested.isoformat(),
                          response_at=(requested + timedelta(seconds=1)).isoformat())
        with self.assertRaises(contract.TradeContractError):
            self.store.observe_absence(future)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)

    def test_same_capture_id_changed_body_refused(self):
        env = envelope()
        self.ingest([source_row()], env=env)
        with self.assertRaises(contract.TradeContractError):
            self.ingest([source_row(STK_VOLM=130)], env=env)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)

    def test_same_capture_id_changed_envelope_refused(self):
        env = envelope()
        self.ingest(env=env)
        with self.assertRaises(contract.TradeContractError):
            self.ingest(env=replace(env, created_by="different-ingest-v1"))

    def test_repeat_requires_independent_later_observation(self):
        env = envelope()
        self.ingest(env=env)
        with self.assertRaises(contract.TradeContractError):
            self.ingest(env=replace(env, capture_id="capture-repeat-same-time"))

    def test_first_success_after_absence(self):
        absent = self.store.observe_absence(envelope("capture-absent", http_status=404))
        first = self.ingest()
        self.assertEqual(first["observation_state"], "CONTENT_FIRST_SEEN")
        self.assertEqual(first["observation_seq"], 2)
        self.assertEqual(first["previous_observation_id"], absent["capture_id"])

    def test_absence_has_own_immutable_provenance(self):
        first = self.ingest()
        absent = self.store.observe_absence(envelope(
            "capture-absent", http_status=404, requested_at="2026-10-01T10:01:30.333333Z",
            response_at="2026-10-01T10:01:31.444444Z"))
        repeat = self.ingest()
        self.assertEqual(absent["previous_observation_id"], first["capture_id"])
        self.assertEqual(repeat["previous_observation_id"], absent["capture_id"])
        self.assertEqual(repeat["observation_state"], "CONTENT_REPEAT")
        self.assertEqual(repeat["observation_seq"], 3)
        self.assertEqual(absent["response_at"], "2026-10-01T10:01:31.444444Z")

    def test_http_status_contract(self):
        with self.assertRaises(contract.TradeContractError):
            self.store.observe_absence(envelope(http_status=200))
        with self.assertRaises(contract.TradeContractError):
            self.ingest(env=envelope(http_status=404))
        with self.assertRaises(contract.TradeContractError):
            envelope(http_status=500)

    def test_update_delete_and_duplicate_insert_refused(self):
        result = self.ingest()
        statements = [
            "UPDATE trade_captures SET observation_state='CONTENT_CHANGED' WHERE capture_id=?",
            "DELETE FROM trade_captures WHERE capture_id=?",
            "INSERT INTO trade_captures SELECT * FROM trade_captures WHERE capture_id=?",
            "UPDATE trade_acceptances SET durable_accepted_at='2026-10-03T00:00:00Z' WHERE capture_id=?",
            "DELETE FROM trade_acceptances WHERE capture_id=?",
            "INSERT INTO trade_acceptances SELECT * FROM trade_acceptances WHERE capture_id=?",
        ]
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.store.conn.execute(sql, (result["capture_id"],))
            self.store.conn.rollback()
        self.assertTrue(self.store.verify(result["capture_id"]))

    def test_missing_acceptance_detected(self):
        result = self.ingest()
        rewrite_immutable_row(self.store.conn, "DELETE FROM trade_acceptances WHERE capture_id=?",
                              (result["capture_id"],))
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_acceptance_timestamp_tampering_detected(self):
        result = self.ingest()
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_acceptances SET durable_accepted_at=? WHERE capture_id=?",
                              ("2026-09-01T00:00:00.000001Z", result["capture_id"]))
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_version_chain_tampering_detected(self):
        self.ingest()
        revised = self.ingest([source_row(STK_VOLM=126)])
        self.store.conn.execute("PRAGMA ignore_check_constraints=ON")
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_captures SET previous_observation_id=NULL WHERE capture_id=?",
                              (revised["capture_id"],))
        self.store.conn.execute("PRAGMA ignore_check_constraints=OFF")
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(revised["capture_id"])

    def test_corrupted_trigger_schema_refused(self):
        result = self.ingest()
        trigger = self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone()[0]
        self.store.conn.execute('DROP TRIGGER "' + trigger.replace('"', '""') + '"')
        self.store.conn.commit()
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_missing_raw_file_detected(self):
        result = self.ingest()
        remove_readonly(next(self.raw.rglob("*.parquet")))
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_forged_same_hash_raw_object_not_adopted(self):
        result = self.ingest()
        raw_file = next(self.raw.rglob("*.parquet"))
        raw_file.chmod(0o600)
        raw_file.write_bytes(b"forged same-hash destination")
        with self.assertRaises(contract.TradeContractError):
            self.ingest()
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
        self.assertEqual(raw_file.read_bytes(), b"forged same-hash destination")

    def test_parser_chunking_and_encoding_determinism(self):
        a = write_parquet(self.root / "chunks-a.parquet", row_group_size=1, use_dictionary=True)
        b = write_parquet(self.root / "chunks-b.parquet", row_group_size=20, use_dictionary=False)
        first = contract.normalize_parquet(a, envelope())
        second = contract.normalize_parquet(b, envelope())
        self.assertEqual(first.rows, second.rows)
        self.assertEqual(first.normalized_content_sha256, second.normalized_content_sha256)
        self.assertEqual(first.schema_fingerprint, second.schema_fingerprint)

    def test_schema_fingerprint_tracks_required_classification(self):
        a = write_parquet(self.root / "optional.parquet")
        b = write_parquet(self.root / "required.parquet", required=True)
        first = contract.normalize_parquet(a, envelope())
        second = contract.normalize_parquet(b, envelope())
        self.assertNotEqual(first.schema_fingerprint, second.schema_fingerprint)
        self.assertEqual(first.normalized_content_sha256, second.normalized_content_sha256)

    def test_schema_fingerprint_tracks_integer_width(self):
        a = write_parquet(self.root / "i64.parquet")
        b = write_parquet(self.root / "i32.parquet", field_types={"STK_VOLM": pa.int32()})
        first = contract.normalize_parquet(a, envelope())
        second = contract.normalize_parquet(b, envelope())
        self.assertNotEqual(first.schema_fingerprint, second.schema_fingerprint)
        self.assertEqual(first.normalized_content_sha256, second.normalized_content_sha256)

    def test_exact_money_above_float_precision(self):
        shares, price = 9007199254740993, 1271
        result = self.ingest([source_row(STK_VOLM=shares, STK_PRIC=price)])
        row = self.rows(result)[0]
        self.assertEqual(row["shares"], shares)
        self.assertEqual(row["value_rp"], shares * price)
        self.assertIs(type(row["value_rp"]), int)
        self.assertGreater(row["value_rp"], 2 ** 63)

    def test_supported_float_value_uses_decimal_roundtrip(self):
        row = self.rows(self.ingest([source_row(VALUE=1587.5)], field_types={"VALUE": pa.float64()}))[0]
        self.assertEqual(row["value_rp"], 158750)
        self.assertIs(type(row["value_rp"]), int)

    def test_float_value_with_fractional_rupiah_rejected(self):
        self.assert_rejected([source_row(VALUE=1587.500000000001)], field_types={"VALUE": pa.float64()})

    def test_null_required_value_rejected(self):
        for name in ("TRX_CODE", "BRK_COD1", "BRK_COD2", "VALUE", "TRX_TIME", "STK_CODE", "TRX_DATE"):
            with self.subTest(name=name):
                self.assert_rejected([source_row(**{name: None})])

    def test_bad_trade_time_rejected(self):
        self.assert_rejected([source_row(TRX_TIME=246001)])

    def test_negative_quantity_rejected(self):
        self.assert_rejected([source_row(STK_VOLM=-1, VALUE=Decimal("-12.70"))])

    def test_microseconds_and_timezone_canonicalization(self):
        env = envelope(requested_at="2026-10-01T17:00:00.123456+07:00",
                       response_at="2026-10-01T17:00:01.654321+07:00")
        self.assertEqual(env.requested_at, "2026-10-01T10:00:00.123456Z")
        self.assertEqual(env.response_at, "2026-10-01T10:00:01.654321Z")
        result = self.ingest(env=env)
        self.assertEqual(result["requested_at"], env.requested_at)
        self.assertEqual(result["response_at"], env.response_at)
        self.assertRegex(result["durable_accepted_at"], r"\.[0-9]{6}Z$")

    def test_envelope_validation_and_immutability(self):
        for changes in (
            {"ticker": "dewa"}, {"ticker": "DEWA?sig=dummy"}, {"trade_date": "2026-02-30"},
            {"requested_at": "2026-10-01T10:00:00"},
            {"response_at": "2026-10-01T01:00:00Z"},
            {"created_by": "Bearer dummy-never-persist"},
            {"x_ms_request_id": "session=dummy-never-persist"},
        ):
            with self.subTest(changes=changes), self.assertRaises(contract.TradeContractError):
                envelope(**changes)
        with self.assertRaises(FrozenInstanceError):
            envelope().ticker = "BBCA"

    def test_token_shaped_path_components_refused_without_echo(self):
        dummy = "Bearer dummy-never-echo"
        for path in (f"https://example.invalid/{dummy}/file.parquet",
                     "https://example.invalid/sig=dummy-never-echo/file.parquet",
                     "https://example.invalid/username=dummy-never-echo/file.parquet"):
            with self.subTest(path=path):
                with self.assertRaises(contract.TradeContractError) as error:
                    envelope(source_path_without_query_or_token=path)
                self.assertNotIn("dummy-never-echo", str(error.exception))

    def test_windows_and_posix_source_paths(self):
        self.assertEqual(contract.sanitize_source_path(r"C:\private\DEWA.parquet?sig=dummy#fragment"),
                         "C:/private/DEWA.parquet")
        self.assertEqual(contract.sanitize_source_path("/private/DEWA.parquet?sig=dummy"),
                         "/private/DEWA.parquet")

    def test_inspect_is_read_only_compact_and_verified(self):
        result = self.ingest()
        before = self.db.read_bytes()
        output = self.store.inspect(result["capture_id"])
        self.assertEqual(output["ticker"], "DEWA")
        self.assertEqual(output["row_count"], 3)
        self.assertNotIn("rows", output)
        self.assertEqual(output["total_shares"], 500)
        self.assertEqual(output["total_value_rp"], 639500)
        self.assertEqual(output["board_counts"], {"NG": 1, "RG": 2})
        self.assertEqual(output["time_range"], ["09:01:01", "16:00:01"])
        self.assertEqual(self.db.read_bytes(), before)
        remove_readonly(next(self.raw.rglob("*.parquet")))
        with self.assertRaises(contract.TradeContractError):
            self.store.inspect(result["capture_id"])

    def test_offline_cli_ingest_verify_inspect(self):
        path = write_parquet(self.root / "cli.parquet")
        cli_db = self.root / "cli-private" / "capture.db"
        command = [sys.executable, str(Path(capture.__file__)), "ingest", "--db", str(cli_db),
                   "--ticker", "DEWA", "--trade-date", "2026-10-01", "--file", str(path),
                   "--requested-at", "2026-10-01T10:00:00.123456Z",
                   "--response-at", "2026-10-01T10:00:01.654321Z", "--capture-id", "cli-capture"]
        ingested = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(ingested.stdout)["observation_state"], "CONTENT_FIRST_SEEN")
        for action in ("verify", "inspect"):
            result = subprocess.run([sys.executable, str(Path(capture.__file__)), action,
                                     "--db", str(cli_db), "--capture-id", "cli-capture"],
                                    capture_output=True, text=True, check=True)
            self.assertIsInstance(json.loads(result.stdout), dict)

    def test_cli_error_does_not_echo_sensitive_path(self):
        dummy = "dummy-cli-never-echo"
        result = subprocess.run(
            [sys.executable, str(Path(capture.__file__)), "ingest", "--db", str(self.root / "bad.db"),
             "--ticker", "DEWA", "--trade-date", "2026-10-01", "--file", str(self.root / dummy),
             "--requested-at", "2026-10-01T10:00:00Z", "--response-at", "2026-10-01T10:00:01Z",
             "--source-path", f"https://example.invalid/file?sig={dummy}"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(dummy, result.stdout + result.stderr)

    def test_required_observation_content_matrix(self):
        scenarios = [
            ("same-raw", [(200, 125, False), (200, 125, False)],
             ["CONTENT_FIRST_SEEN", "CONTENT_REPEAT"], [1, 1]),
            ("new-raw-same-content", [(200, 125, False), (200, 125, True)],
             ["CONTENT_FIRST_SEEN", "CONTENT_REPEAT"], [1, 1]),
            ("changed", [(200, 125, False), (200, 126, False)],
             ["CONTENT_FIRST_SEEN", "CONTENT_CHANGED"], [1, 2]),
            ("absent-twice", [(404, None, False), (404, None, False)],
             ["ABSENT_OBSERVED", "ABSENT_OBSERVED"], [None, None]),
            ("absent-then-content", [(404, None, False), (200, 125, False)],
             ["ABSENT_OBSERVED", "CONTENT_FIRST_SEEN"], [None, 1]),
            ("content-then-absent", [(200, 125, False), (404, None, False)],
             ["CONTENT_FIRST_SEEN", "ABSENT_OBSERVED"], [1, None]),
            ("changed-then-repeat", [(200, 125, False), (200, 126, False), (200, 126, False)],
             ["CONTENT_FIRST_SEEN", "CONTENT_CHANGED", "CONTENT_REPEAT"], [1, 2, 2]),
            ("absence-does-not-erase", [(200, 125, False), (404, None, False), (200, 125, True)],
             ["CONTENT_FIRST_SEEN", "ABSENT_OBSERVED", "CONTENT_REPEAT"], [1, None, 1]),
        ]
        for label, events, states, versions in scenarios:
            with self.subTest(scenario=label), capture.TradeCaptureStore(
                    self.root / label / "capture.db") as store:
                results = []
                last_content = None
                for index, (status, shares, reencoded) in enumerate(events, start=1):
                    env = observation(label + str(index), index, http_status=status)
                    if status == 404:
                        result = store.observe_absence(env)
                    else:
                        path = write_parquet(self.root / (label + str(index) + ".parquet"),
                                             [source_row(STK_VOLM=shares)],
                                             use_dictionary=not reencoded,
                                             compression="none" if reencoded else "zstd")
                        result = store.ingest(path, env)
                    self.assertEqual(result["observation_seq"], index)
                    self.assertEqual(result["observation_state"], states[index - 1])
                    self.assertEqual(result["content_version"], versions[index - 1])
                    self.assertEqual(result["previous_observation_id"],
                                     None if not results else results[-1]["capture_id"])
                    self.assertEqual(result["previous_content_capture_id"], last_content)
                    self.assertEqual(store.verify(env.capture_id), result)
                    if status == 200:
                        last_content = result["capture_id"]
                    results.append(result)
                if label == "same-raw":
                    self.assertEqual(results[0]["raw_response_sha256"], results[1]["raw_response_sha256"])
                if label == "new-raw-same-content":
                    self.assertNotEqual(results[0]["raw_response_sha256"], results[1]["raw_response_sha256"])
                    self.assertEqual(results[0]["normalized_content_sha256"], results[1]["normalized_content_sha256"])
                if label == "content-then-absent":
                    self.assertEqual(store.read_rows(results[0]["capture_id"])[0]["shares"], 125)

    def test_backdated_matrix_refused_for_content_and_absence(self):
        for first_status, second_status, shares, reencoded in (
                (200, 200, 125, False), (200, 200, 125, True), (200, 200, 126, False),
                (404, 404, None, False), (404, 200, 125, False), (200, 404, None, False)):
            label = f"late-{first_status}-{second_status}-{shares}-{reencoded}"
            with self.subTest(scenario=label), capture.TradeCaptureStore(
                    self.root / label / "capture.db") as store:
                first = observation(label + "a", 30, http_status=first_status)
                if first_status == 404:
                    store.observe_absence(first)
                else:
                    store.ingest(write_parquet(self.root / (label + "a.parquet"), [source_row()]), first)
                before_raw = set(store.raw_root.rglob("*.parquet"))
                second = observation(label + "b", 10, http_status=second_status)
                with self.assertRaises(contract.TradeContractError):
                    if second_status == 404:
                        store.observe_absence(second)
                    else:
                        store.ingest(write_parquet(self.root / (label + "b.parquet"),
                                     [source_row(STK_VOLM=shares)], use_dictionary=not reencoded), second)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 1)
                self.assertEqual(set(store.raw_root.rglob("*.parquet")), before_raw)
                self.assertEqual(list(store.raw_root.rglob(".pending-*")), [])

    def test_equal_response_or_overlapping_request_refused(self):
        for status in (200, 404):
            for kind in ("equal-response", "overlapping-request"):
                label = f"chronology-{status}-{kind}"
                with self.subTest(status=status, kind=kind), capture.TradeCaptureStore(
                        self.root / label / "capture.db") as store:
                    first = observation(label + "a", 10, http_status=status)
                    path = write_parquet(self.root / (label + ".parquet"))
                    if status == 404:
                        store.observe_absence(first)
                    else:
                        store.ingest(path, first)
                    second = replace(first, capture_id=label + "b",
                                     requested_at=(first.response_at if kind == "equal-response"
                                                   else "2026-10-01T10:10:00.500000Z"),
                                     response_at=(first.response_at if kind == "equal-response"
                                                  else "2026-10-01T10:10:03.000000Z"))
                    with self.assertRaises(contract.TradeContractError):
                        if status == 404:
                            store.observe_absence(second)
                        else:
                            store.ingest(path, second)

    def test_verify_rechecks_source_chronology(self):
        first = self.ingest([source_row()], env=observation("chronology-a", 30))
        second = self.ingest([source_row(STK_VOLM=126)], env=observation("chronology-b", 31))
        record = self.store.conn.execute("SELECT metadata_json FROM trade_captures WHERE capture_id=?",
                                         (second["capture_id"],)).fetchone()[0]
        changed = json.loads(record)
        changed.update(requested_at="2026-10-01T10:20:00.000000Z",
                       response_at="2026-10-01T10:20:01.000000Z")
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_captures SET metadata_json=? WHERE capture_id=?",
                              (contract.canonical_json(changed), second["capture_id"]),
                              reseal_capture_id=second["capture_id"])
        self.assertTrue(self.store.verify(first["capture_id"]))
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(second["capture_id"])

    def test_resealed_state_and_version_tampering_detected(self):
        first = self.ingest([source_row()])
        repeated = self.ingest([source_row()])
        for field, bad_value, good_value in (
                ("observation_state", "CONTENT_CHANGED", "CONTENT_REPEAT"),
                ("content_version", 2, 1),
                ("previous_content_capture_id", repeated["capture_id"], first["capture_id"])):
            with self.subTest(field=field):
                rewrite_immutable_row(self.store.conn,
                                      f"UPDATE trade_captures SET {field}=? WHERE capture_id=?",
                                      (bad_value, repeated["capture_id"]),
                                      reseal_capture_id=repeated["capture_id"])
                with self.assertRaises(contract.TradeContractError):
                    self.store.verify(repeated["capture_id"])
                rewrite_immutable_row(self.store.conn,
                                      f"UPDATE trade_captures SET {field}=? WHERE capture_id=?",
                                      (good_value, repeated["capture_id"]),
                                      reseal_capture_id=repeated["capture_id"])

    def test_resealed_duplicate_schema_and_length_tampering_detected(self):
        result = self.ingest([source_row(), source_row()])
        record = dict(self.store.conn.execute("SELECT * FROM trade_captures").fetchone())
        for field, bad_value in (("duplicate_counts_json", "[]"),
                                 ("schema_fingerprint", "0" * 64),
                                 ("source_row_count", 3),
                                 ("row_count", 2),
                                 ("content_length", result["content_length"] + 1)):
            with self.subTest(field=field):
                rewrite_immutable_row(self.store.conn,
                                      f"UPDATE trade_captures SET {field}=? WHERE capture_id=?",
                                      (bad_value, result["capture_id"]), reseal_capture_id=result["capture_id"])
                with self.assertRaises(contract.TradeContractError):
                    self.store.verify(result["capture_id"])
                rewrite_immutable_row(self.store.conn,
                                      f"UPDATE trade_captures SET {field}=? WHERE capture_id=?",
                                      (record[field], result["capture_id"]), reseal_capture_id=result["capture_id"])

    def test_raw_destination_metadata_tampering_detected(self):
        result = self.ingest()
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_store_meta SET value=? WHERE key='raw_root'",
                              (str(self.root / "different-raw-root"),))
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_parent_acceptance_ordering_rechecked(self):
        first = self.ingest([source_row()])
        second = self.ingest([source_row(STK_VOLM=126)])
        self.assertGreater(second["durable_accepted_at"], second["body_recorded_at"])
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_acceptances SET durable_accepted_at=? WHERE capture_id=?",
                              (second["durable_accepted_at"], first["capture_id"]))
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(second["capture_id"])

    def test_same_trigger_name_with_corrupt_body_refused(self):
        result = self.ingest()
        name = "immutable_trade_captures_update"
        self.store.conn.execute(f"DROP TRIGGER {name}")
        self.store.conn.execute(f"CREATE TRIGGER {name} BEFORE UPDATE ON trade_captures BEGIN SELECT 1; END")
        self.store.conn.commit()
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(result["capture_id"])

    def test_requires_parent_trigger_blocks_orphan_observation(self):
        result = self.ingest()
        record = dict(self.store.conn.execute("SELECT * FROM trade_captures").fetchone())
        record.update(capture_id="orphan-observation", observation_seq=2, previous_observation_id=None)
        columns = tuple(record)
        self.store.conn.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute(f"INSERT INTO trade_captures ({','.join(columns)}) VALUES "
                                    f"({','.join('?' for _ in columns)})", tuple(record.values()))
        self.store.conn.rollback()
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
        self.assertTrue(self.store.verify(result["capture_id"]))

    def test_meta_update_delete_and_replace_refused(self):
        for sql in ("UPDATE trade_store_meta SET value='forged' WHERE key='product'",
                    "DELETE FROM trade_store_meta WHERE key='product'",
                    "INSERT OR REPLACE INTO trade_store_meta VALUES ('product','forged')"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.store.conn.execute(sql)
            self.store.conn.rollback()

    def test_null_identity_keys_fail_sqlite_constraints(self):
        self.ingest()
        record = dict(self.store.conn.execute("SELECT * FROM trade_captures").fetchone())
        with closing(sqlite3.connect(":memory:")) as conn:
            for ddl in capture.TABLE_SQL.values():
                conn.execute(ddl)
            for field in ("capture_id", "ticker", "trade_date", "observation_seq"):
                candidate = dict(record, **{field: None})
                columns = tuple(candidate)
                with self.subTest(field=field), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(f"INSERT INTO trade_captures ({','.join(columns)}) VALUES "
                                 f"({','.join('?' for _ in columns)})", tuple(candidate.values()))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO trade_store_meta VALUES (NULL, 'forged')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO trade_acceptances VALUES (NULL, ?, ?)",
                             ("0" * 64, "2026-10-02T00:00:00.000000Z"))

    def test_absence_null_fields_enforced_in_sql_and_verify(self):
        absent = self.store.observe_absence(envelope(http_status=404))
        record = dict(self.store.conn.execute("SELECT * FROM trade_captures").fetchone())
        null_fields = ("content_version", "raw_response_sha256", "normalized_content_sha256",
                       "schema_fingerprint", "schema_version", "row_count", "source_row_count",
                       "duplicate_counts_json", "content_json", "content_length")
        with closing(sqlite3.connect(":memory:")) as conn:
            for ddl in capture.TABLE_SQL.values():
                conn.execute(ddl)
            for field in null_fields:
                self.assertIsNone(record[field])
                candidate = dict(record, **{field: 0 if field.endswith("count") or field in
                                            ("content_version", "content_length") else "forged"})
                columns = tuple(candidate)
                with self.subTest(field=field), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(f"INSERT INTO trade_captures ({','.join(columns)}) VALUES "
                                 f"({','.join('?' for _ in columns)})", tuple(candidate.values()))
        # Bypass CHECK constraints to ensure verifier independently enforces truth.
        self.store.conn.execute("PRAGMA ignore_check_constraints=ON")
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_captures SET content_length=0 WHERE capture_id=?",
                              (absent["capture_id"],), reseal_capture_id=absent["capture_id"])
        self.store.conn.execute("PRAGMA ignore_check_constraints=OFF")
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(absent["capture_id"])

    def test_zero_row_recent_and_legacy_refused(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assert_rejected([], legacy=legacy)

    def test_rejected_nonparquet_bytes_leave_no_published_object(self):
        for name, data in (("html", b"<html><title>Sign in</title></html>"),
                           ("malformed", b"PAR1not-a-parquet-bodyPAR1")):
            path = self.root / (name + ".parquet")
            path.write_bytes(data)
            with self.subTest(body=name), self.assertRaises(contract.TradeContractError):
                self.store.ingest(path, envelope(name))
            self.assertEqual(list(self.raw.rglob("*.parquet")), [])
            self.assertEqual(list(self.raw.rglob(".pending-*")), [])
            self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 0)
            self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)

    def test_raw_publish_windows_safe_order(self):
        data = b"invented offline lifecycle fixture"
        digest = contract.sha256_bytes(data)
        destination = self.store.raw_path(digest)
        events = []
        real_link, real_unlink, real_chmod = os.link, os.unlink, os.chmod

        def link(source, target, *args, **kwargs):
            self.assertTrue(Path(source).stat().st_mode & stat.S_IWRITE)
            events.append("link")
            return real_link(source, target, *args, **kwargs)

        def unlink(path, *args, **kwargs):
            if Path(path).name.startswith(".pending-"):
                self.assertTrue(Path(path).stat().st_mode & stat.S_IWRITE)
                self.assertTrue(destination.stat().st_mode & stat.S_IWRITE)
                events.append("unlink-temp")
            return real_unlink(path, *args, **kwargs)

        def chmod(path, mode, *args, **kwargs):
            if Path(path) == destination and not mode & stat.S_IWRITE:
                self.assertEqual(list(destination.parent.glob(".pending-*")), [])
                events.append("protect-destination")
            return real_chmod(path, mode, *args, **kwargs)

        with patch.object(capture.os, "link", side_effect=link), \
                patch.object(capture.os, "unlink", side_effect=unlink), \
                patch.object(capture.os, "chmod", side_effect=chmod):
            self.assertEqual(self.store._preserve_raw(data, digest), destination)
        self.assertEqual(events, ["link", "unlink-temp", "protect-destination"])
        self.assertEqual(destination.read_bytes(), data)
        self.assertFalse(destination.stat().st_mode & stat.S_IWRITE)
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])

    def test_raw_existing_conflict_cleanup_keeps_protection(self):
        data = b"invented immutable raw body"
        digest = contract.sha256_bytes(data)
        destination = self.store._preserve_raw(data, digest)
        self.assertEqual(self.store._preserve_raw(data, digest), destination)
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])
        destination.chmod(stat.S_IWRITE | stat.S_IREAD)
        destination.write_bytes(b"different bytes at the same digest path")
        destination.chmod(stat.S_IREAD)
        with self.assertRaisesRegex(contract.TradeContractError, "differs"):
            self.store._preserve_raw(data, digest)
        self.assertEqual(destination.read_bytes(), b"different bytes at the same digest path")
        self.assertFalse(destination.stat().st_mode & stat.S_IWRITE)
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])

    def test_raw_link_failure_leaves_no_orphan_or_pending_file(self):
        data = b"invented offline publication failure"
        with patch.object(capture.os, "link", side_effect=OSError("synthetic link failure")):
            with self.assertRaises(OSError):
                self.store._preserve_raw(data, contract.sha256_bytes(data))
        self.assertEqual(list(self.raw.rglob("*.parquet")), [])
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])

    def test_zero_shares_and_zero_price_refused(self):
        for values in ({"STK_VOLM": 0}, {"STK_PRIC": 0}):
            with self.subTest(values=values):
                self.assert_rejected([source_row(**values)])

    def test_value_float32_refused(self):
        self.assert_rejected([source_row(VALUE=1587.5)], field_types={"VALUE": pa.float32()})

    def test_legacy_trx_type_requires_exact_audited_zero(self):
        path = write_parquet(self.root / "legacy-nonzero.parquet", legacy=True)
        table = pq.read_table(path)
        index = table.schema.get_field_index("TRX_TYPE")
        table = table.set_column(index, table.schema.field(index), pa.array([1] * len(table), pa.int32()))
        pq.write_table(table, path)
        with self.assertRaises(contract.TradeContractError):
            self.store.ingest(path, envelope())
        self.assertEqual(list(self.raw.rglob("*.parquet")), [])

    def test_double_binary_imprecision_reconciles_exact_integer_money(self):
        for shares, price, value in ((29, 1, 0.29), (137, 103, 141.11)):
            with self.subTest(shares=shares, price=price):
                self.assertNotEqual(value * 100, shares * price)
                row = self.rows(self.ingest([source_row(STK_VOLM=shares, STK_PRIC=price, VALUE=value)],
                                            field_types={"VALUE": pa.float64()}))[0]
                self.assertEqual(row["value_rp"], shares * price)
                self.assertIs(type(row["value_rp"]), int)

    def test_double_nonfinite_and_ambiguous_large_money_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=str(value)):
                self.assert_rejected([source_row(VALUE=value)], field_types={"VALUE": pa.float64()})
        self.assert_rejected([source_row(STK_VOLM=2 ** 60, STK_PRIC=100, VALUE=float(2 ** 60))],
                             field_types={"VALUE": pa.float64()})

    def test_unicode_numeric_and_time_strings_refused(self):
        for changes, types in (({"TRX_TIME": "０９:０１:０１"}, {"TRX_TIME": pa.string()}),
                               ({"TRX_TIME": "٠٩٠١٠١"}, {"TRX_TIME": pa.string()}),
                               ({"STK_VOLM": "１２５"}, {"STK_VOLM": pa.string()})):
            with self.subTest(changes=changes):
                self.assert_rejected([source_row(**changes, VALUE=Decimal("1587.50"))], field_types=types)

    def test_time_lower_bounds_and_headers_apply_to_200_and_404(self):
        for status in (200, 404):
            for changes in (
                {"requested_at": "2026-09-30T16:58:00Z", "response_at": "2026-09-30T16:59:00Z",
                 "last_modified": None, "x_ms_creation_time": None},
                {"last_modified": "2026-10-01T10:00:02Z"},
                {"x_ms_creation_time": "2026-10-01T10:00:02Z", "last_modified": None},
                {"x_ms_creation_time": "2026-10-01T01:59:59.222222Z",
                 "last_modified": "2026-10-01T01:58:00.111111Z"},
                {"requested_at": "2026-10-01T10:01:00Z"},
            ):
                with self.subTest(status=status, changes=changes), self.assertRaises(contract.TradeContractError):
                    env = envelope(http_status=status, **changes)
                    if status == 404:
                        self.store.observe_absence(env)
                    else:
                        self.ingest(env=env)
        for headers in ({"last_modified": None}, {"x_ms_creation_time": None},
                        {"last_modified": None, "x_ms_creation_time": None}):
            self.assertIsInstance(envelope(**headers), contract.CaptureEnvelope)

    def test_response_cannot_precede_represented_trade(self):
        self.assert_rejected([source_row(TRX_TIME=160001)], env=envelope(
            requested_at="2026-10-01T09:00:00Z", response_at="2026-10-01T09:00:00.999999Z"))
        # Source has seconds only: capture during the represented second is plausible.
        result = self.ingest([source_row(TRX_TIME=160001)], env=envelope(
            "same-trade-second", requested_at="2026-10-01T09:00:01Z",
            response_at="2026-10-01T09:00:01.000001Z"))
        self.assertEqual(self.rows(result)[0]["trade_time"], "16:00:01")

    def test_source_sanitizer_matrix_device_paths_and_exception_context(self):
        secret = "dummy-secret-never-persist"
        for source in (f"https://example.invalid/trade;token={secret}/DEWA.parquet",
                       f"https://example.invalid/trade%3Btoken={secret}/DEWA.parquet",
                       rf"\\?\C:\private\DEWA.parquet?sig={secret}",
                       rf"\\.\C:\private\DEWA.parquet?sig={secret}",
                       f"https://[broken-host/{secret}/DEWA.parquet",
                       f"https://example.invalid:broken/{secret}/DEWA.parquet"):
            with self.subTest(source=source), self.assertRaises(contract.TradeContractError) as error:
                contract.sanitize_source_path(source)
            self.assertNotIn(secret, str(error.exception))
            self.assertIsNone(error.exception.__context__)

    def test_source_fragment_stripping_and_literal_local_hash(self):
        self.assertEqual(contract.sanitize_source_path("https://example.invalid/DEWA.parquet#fragment"),
                         "https://example.invalid/DEWA.parquet")
        self.assertEqual(contract.sanitize_source_path("/ordinary/capture/DEWA#draft.parquet"),
                         "/ordinary/capture/DEWA#draft.parquet")
        self.assertEqual(contract.sanitize_source_path(r"C:\ordinary\DEWA#draft.parquet"),
                         "C:/ordinary/DEWA#draft.parquet")

    def test_ordinary_storage_directory_names_allowed(self):
        for index, name in enumerate(("session", "token", "a" * 40), start=1):
            with self.subTest(name=name), capture.TradeCaptureStore(
                    self.root / name / "capture.db", raw_root=self.root / name / "raw") as store:
                self.assertTrue(store.db.is_file())
                path = write_parquet(self.root / name / "input.parquet")
                self.assertEqual(store.ingest(path, envelope("ordinary-" + str(index)))["row_count"], 3)

    def test_legacy_identical_raw_is_envelope_scoped(self):
        path = write_parquet(self.root / "legacy-scoped.parquet", legacy=True)
        first = self.store.ingest(path, envelope("legacy-dewa"))
        second = self.store.ingest(path, envelope("legacy-bbca", ticker="BBCA", trade_date="2026-09-30"))
        self.assertEqual(first["raw_response_sha256"], second["raw_response_sha256"])
        self.assertNotEqual(first["normalized_content_sha256"], second["normalized_content_sha256"])
        self.assertEqual(self.rows(first)[0]["ticker"], "DEWA")
        self.assertEqual(self.rows(second)[0]["ticker"], "BBCA")
        self.assertEqual(second["content_version"], 1)

    def test_duplicate_collapse_count_exposed_in_safe_metadata(self):
        result = self.ingest([source_row(), source_row(), source_row(TRX_CODE=1002)])
        for metadata in (result, self.store.verify(result["capture_id"]), self.store.inspect(result["capture_id"])):
            self.assertEqual(metadata["duplicate_collapse_count"], 1)
            self.assertEqual(metadata["row_count"], 2)
            self.assertEqual(metadata["source_row_count"], 3)

    def test_pending_body_blocks_successor_with_recovery_instruction(self):
        path = write_parquet(self.root / "pending.parquet")
        env = observation("pending-body", 1)
        transaction = self.store._write_transaction
        calls = 0

        def fail_acceptance():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic crash before acceptance")
            return transaction()

        with patch.object(self.store, "_write_transaction", side_effect=fail_acceptance):
            with self.assertRaises(RuntimeError):
                self.store.ingest(path, env)
        with self.assertRaises(contract.TradeContractError) as error:
            self.store.ingest(path, observation("pending-successor", 2))
        self.assertIn(env.capture_id, str(error.exception))
        self.assertIn("resume", str(error.exception).lower())
        self.store.resume(env.capture_id)
        self.assertEqual(self.store.ingest(path, observation("pending-successor", 2))["observation_seq"], 2)

    def test_pending_absence_resumes_without_envelope_or_input(self):
        env = observation("pending-absence", 1, http_status=404)
        transaction = self.store._write_transaction
        calls = 0

        def fail_acceptance():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic crash before absence acceptance")
            return transaction()

        with patch.object(self.store, "_write_transaction", side_effect=fail_acceptance):
            with self.assertRaises(RuntimeError):
                self.store.observe_absence(env)
        recorded = self.store.conn.execute("SELECT body_recorded_at FROM trade_captures").fetchone()[0]
        result = self.store.resume(env.capture_id)
        self.assertEqual(result["observation_state"], "ABSENT_OBSERVED")
        self.assertIsNone(result["content_version"])
        self.assertEqual(result["body_recorded_at"], recorded)
        self.assertTrue(result["durable_accepted_at"])
        self.assertEqual(self.store.resume(env.capture_id), result)

    def test_resume_revalidates_raw_and_refuses_unknown_capture(self):
        with self.assertRaises(contract.TradeContractError):
            self.store.resume("unknown-pending-body")
        result = self.ingest()
        rewrite_immutable_row(self.store.conn, "DELETE FROM trade_acceptances WHERE capture_id=?",
                              (result["capture_id"],))
        raw = self.store.raw_path(result["raw_response_sha256"])
        raw.chmod(stat.S_IWRITE | stat.S_IREAD)
        raw.write_bytes(b"corrupt pending raw object")
        with self.assertRaises(contract.TradeContractError):
            self.store.resume(result["capture_id"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)

    def test_cli_rejects_abbreviated_and_repeated_single_value_flags(self):
        for arguments in (("--capt", "capture-a"),
                          ("--capture-id", "capture-a", "--capture-id", "capture-a"),
                          ("--capture-id", "capture-a", "--db", str(self.db))):
            result = subprocess.run([sys.executable, str(Path(capture.__file__)), "verify", "--db",
                                     str(self.db), *arguments], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 2)

    def test_cli_read_only_purity_and_checkpointed_wal_mode(self):
        result = self.ingest()
        self.store.close()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        for action in ("verify", "inspect"):
            before = {str(path.relative_to(self.root)): path.read_bytes()
                      for path in self.root.rglob("*") if path.is_file()}
            run = subprocess.run([sys.executable, str(Path(capture.__file__)), action,
                                  "--db", str(self.db), "--raw-root", str(self.raw),
                                  "--capture-id", result["capture_id"]], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            self.assertNotEqual(run.returncode, 0)
            after = {str(path.relative_to(self.root)): path.read_bytes()
                     for path in self.root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(str(self.db) + suffix).exists())
        # A writer checkpoints and restores this store's DELETE journal model.
        with capture.TradeCaptureStore(self.db, raw_root=self.raw) as writer:
            self.assertEqual(writer.conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        for action in ("verify", "inspect"):
            before = {str(path.relative_to(self.root)): path.read_bytes()
                      for path in self.root.rglob("*") if path.is_file()}
            run = subprocess.run([sys.executable, str(Path(capture.__file__)), action,
                                  "--db", str(self.db), "--raw-root", str(self.raw),
                                  "--capture-id", result["capture_id"]], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["capture_id"], result["capture_id"])
            self.assertEqual({str(path.relative_to(self.root)): path.read_bytes()
                              for path in self.root.rglob("*") if path.is_file()}, before)

    def test_cli_resume_accepts_pending_body(self):
        result = self.ingest()
        rewrite_immutable_row(self.store.conn, "DELETE FROM trade_acceptances WHERE capture_id=?",
                              (result["capture_id"],))
        original_body = result["body_recorded_at"]
        self.store.close()
        command = [sys.executable, str(Path(capture.__file__)), "resume", "--db", str(self.db),
                   "--raw-root", str(self.raw), "--capture-id", result["capture_id"]]
        resumed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        metadata = json.loads(resumed.stdout)
        self.assertEqual(metadata["body_recorded_at"], original_body)
        self.assertTrue(metadata["durable_accepted_at"])
        retry = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout), metadata)

    def test_durable_default_location_outside_repository(self):
        self.assertNotEqual(capture.DEFAULT_DB.parent, Path(capture.__file__).resolve().parent / "private_data")
        self.assertFalse(capture.DEFAULT_DB.is_relative_to(Path(capture.__file__).resolve().parent))
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg"),
                                     "LOCALAPPDATA": str(self.root / "local-app-data")}):
            location = capture._user_data_directory()
            self.assertTrue(location.is_absolute())
            if sys.platform == "win32":
                self.assertEqual(location, self.root / "local-app-data" / "DailyScraper" / "bandarmolony")
            elif sys.platform != "darwin":
                self.assertEqual(location, self.root / "xdg" / "dailyscraper" / "bandarmolony")

    def test_publication_then_body_insert_failure_removes_orphan(self):
        path = write_parquet(self.root / "insert-failure.parquet")
        with patch.object(capture, "_body_digest", return_value=None):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.ingest(path, envelope())
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)
        self.assertEqual(list(self.raw.rglob("*.parquet")), [])
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])

    def test_read_only_reader_cannot_observe_uncommitted_body_or_acceptance(self):
        first = self.ingest()
        record = dict(self.store.conn.execute("SELECT * FROM trade_captures").fetchone())
        env = observation("uncommitted-writer", 2)
        record.update(capture_id=env.capture_id, observation_seq=2,
                      previous_observation_id=first["capture_id"],
                      previous_content_capture_id=first["capture_id"],
                      observation_state="CONTENT_REPEAT", metadata_json=contract.canonical_json(asdict(env)),
                      body_recorded_at=capture.utc_now())
        record["body_sha256"] = capture._body_digest(record)
        columns = tuple(record)
        with capture.TradeCaptureStore(self.db, raw_root=self.raw, read_only=True) as reader:
            self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                self.store.conn.execute(f"INSERT INTO trade_captures ({','.join(columns)}) VALUES "
                                        f"({','.join('?' for _ in columns)})", tuple(record.values()))
                self.store.conn.execute("INSERT INTO trade_acceptances VALUES (?, ?, ?)",
                                        (env.capture_id, record["body_sha256"], capture.utc_now()))
                before = {path.name: path.read_bytes() for path in self.db.parent.iterdir() if path.is_file()}
                self.assertEqual(reader.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
                self.assertEqual(reader.conn.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 1)
                with self.assertRaises(contract.TradeContractError):
                    reader.verify(env.capture_id)
                # A new operator reader also refuses the active recovery journal.
                with self.assertRaises(contract.TradeContractError):
                    with capture.TradeCaptureStore(self.db, raw_root=self.raw, read_only=True):
                        pass
                after = {path.name: path.read_bytes() for path in self.db.parent.iterdir() if path.is_file()}
                self.assertEqual(after, before)
            finally:
                self.store.conn.rollback()
            self.assertEqual(reader.verify(first["capture_id"])["observation_seq"], 1)
            with self.assertRaises(contract.TradeContractError):
                reader.verify(env.capture_id)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)

    def test_read_only_outstanding_wal_and_journal_refused(self):
        self.ingest()
        self.store.close()
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE synthetic_wal_probe (value TEXT)")
            writer.commit()
            self.assertGreater(Path(str(self.db) + "-wal").stat().st_size, 0)
            with self.assertRaises(contract.TradeContractError):
                with capture.TradeCaptureStore(self.db, raw_root=self.raw, read_only=True):
                    pass
        # Use a separate valid store for an unrecovered journal, without WAL probe schema.
        with capture.TradeCaptureStore(self.root / "journal" / "capture.db") as valid:
            journal_db, journal_raw = valid.db, valid.raw_root
        journal = Path(str(journal_db) + "-journal")
        journal.write_bytes(b"synthetic nonempty recovery journal")
        with self.assertRaises(contract.TradeContractError):
            with capture.TradeCaptureStore(journal_db, raw_root=journal_raw, read_only=True):
                pass
        self.assertEqual(journal.read_bytes(), b"synthetic nonempty recovery journal")

    def git(self, root, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True,
                              encoding="utf-8", errors="replace", env=environment, check=True)

    def git_repository(self, name, *, anchored=False):
        root = self.root / name
        root.mkdir()
        self.git(root, "init", "-q")
        (root / ".gitignore").write_text(("/" if anchored else "") + "private_data/\n",
                                          encoding="utf-8")
        return root

    def test_git_guard_case_variants_and_inherited_environment(self):
        root = self.git_repository("git-case")
        tracked = root / "private_data" / "capture.db"
        tracked.parent.mkdir()
        tracked.write_bytes(b"synthetic tracked marker")
        self.git(root, "add", "-f", "private_data/capture.db")
        other = self.git_repository("git-spoof")
        for variable, value in (("GIT_DIR", str(other / ".git")),
                                ("GIT_WORK_TREE", str(other)),
                                ("GIT_INDEX_FILE", str(other / "spoof-index")),
                                ("GIT_DIR", str(self.root / "nonexistent-git-dir")),
                                ("GIT_COMMON_DIR", str(other / ".git")),
                                ("GIT_CONFIG_COUNT", "invalid-spoof-value")):
            for path in (tracked, root / "PRIVATE_DATA" / "CAPTURE.DB"):
                with self.subTest(variable=variable, path=path), patch.dict(os.environ, {variable: value}):
                    with self.assertRaises(contract.TradeContractError):
                        capture.check_private_output(path)
        with self.assertRaises(contract.TradeContractError):
            capture.check_private_output(root / "PRIVATE_DATA")

    def test_git_guard_main_and_linked_worktree(self):
        root = self.git_repository("git-main")
        self.git(root, "add", ".gitignore")
        self.git(root, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "synthetic ignored-output fixture")
        linked = self.root / "git-linked"
        self.git(root, "worktree", "add", "--detach", str(linked), "HEAD")
        try:
            for checkout in (root, linked):
                with self.subTest(checkout=checkout):
                    candidate = checkout / "private_data" / "capture.db"
                    self.assertEqual(capture.check_private_output(candidate, capture.SIDECARS), candidate.resolve())
                    with self.assertRaises(contract.TradeContractError):
                        capture.check_private_output(checkout / "public" / "capture.db", capture.SIDECARS)
        finally:
            self.git(root, "worktree", "remove", "--force", str(linked))

    def test_git_guard_nested_and_outside_repository(self):
        outer = self.git_repository("git-outer", anchored=True)
        inner = outer / "nested"
        inner.mkdir()
        tracked = inner / "private_data" / "capture.db"
        tracked.parent.mkdir()
        tracked.write_bytes(b"synthetic marker tracked by outer checkout")
        self.git(outer, "add", "-f", str(tracked))
        self.git(inner, "init", "-q")
        (inner / ".gitignore").write_text("private_data/\n", encoding="utf-8")
        with self.assertRaises(contract.TradeContractError):
            capture.check_private_output(inner / "private_data" / "capture.db")
        outside = self.root / "outside" / "capture.db"
        self.assertEqual(capture.check_private_output(outside, capture.SIDECARS), outside.resolve())
        with patch.dict(os.environ, {"GIT_DIR": str(outer / ".git"), "GIT_WORK_TREE": str(outer)}):
            self.assertEqual(capture.check_private_output(outside), outside.resolve())

    def test_git_guard_tracked_sidecars_and_ignored_descendants(self):
        for suffix in capture.SIDECARS:
            root = self.git_repository("git-sidecar-" + suffix[1:])
            candidate = root / "private_data" / "capture.db"
            sidecar = Path(str(candidate) + suffix)
            sidecar.parent.mkdir()
            sidecar.write_bytes(b"synthetic tracked sidecar")
            self.git(root, "add", "-f", str(sidecar))
            with self.subTest(suffix=suffix), self.assertRaises(contract.TradeContractError):
                capture.check_private_output(candidate, capture.SIDECARS)
        root = self.git_repository("git-descendant")
        descendant = root / "private_data" / "raw" / "nested" / "synthetic.parquet"
        descendant.parent.mkdir(parents=True)
        descendant.write_bytes(b"synthetic tracked descendant")
        self.git(root, "add", "-f", str(descendant))
        with self.assertRaises(contract.TradeContractError):
            capture.check_private_output(root / "PRIVATE_DATA" / "RAW")

    def test_git_not_repository_error_cannot_hide_ancestor_context(self):
        root = self.git_repository("git-failed-discovery")
        failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal: not a git repository")
        with patch.object(capture, "_git", return_value=failed):
            with self.assertRaises(contract.TradeContractError):
                capture.check_private_output(root / "private_data" / "capture.db")

    def test_git_subprocess_environment_and_decoding_are_explicit(self):
        completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        with patch.dict(os.environ, {"GIT_DIR": "spoof", "git_index_file": "spoof"}), \
                patch.object(capture.subprocess, "run", return_value=completed) as run:
            self.assertIs(capture._git(["status", "--porcelain"], self.root), completed)
        self.assertEqual(run.call_args.kwargs["encoding"].lower(), "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")
        git_environment = {key: value for key, value in run.call_args.kwargs["env"].items()
                           if key.upper().startswith("GIT_")}
        self.assertEqual(git_environment, {})

    def test_resume_pending_conflicting_successor_refused(self):
        first = self.ingest()
        second = self.ingest([source_row(STK_VOLM=126)])
        # Already accepted bodies remain idempotent even when history continues.
        self.assertEqual(self.store.resume(first["capture_id"]), first)
        rewrite_immutable_row(self.store.conn, "DELETE FROM trade_acceptances WHERE capture_id=?",
                              (first["capture_id"],))
        before_bodies = list(self.store.conn.execute("SELECT * FROM trade_captures"))
        before_acceptances = list(self.store.conn.execute("SELECT * FROM trade_acceptances"))
        with self.assertRaisesRegex(contract.TradeContractError, "conflicting successor"):
            self.store.resume(first["capture_id"])
        self.assertEqual(list(self.store.conn.execute("SELECT * FROM trade_captures")), before_bodies)
        self.assertEqual(list(self.store.conn.execute("SELECT * FROM trade_acceptances")), before_acceptances)
        self.assertEqual(before_acceptances[0]["capture_id"], second["capture_id"])

    def test_source_schema_profile_change_is_semantic_repeat_in_both_directions(self):
        for first_legacy in (True, False):
            label = "legacy-first" if first_legacy else "recent-first"
            with self.subTest(direction=label), capture.TradeCaptureStore(
                    self.root / label / "capture.db") as store:
                results, tapes = [], []
                for index, legacy in enumerate((first_legacy, not first_legacy), start=1):
                    env = observation(label + str(index), index)
                    path = write_parquet(self.root / (label + str(index) + ".parquet"),
                                         [source_row(TRX_TYPE="UNKNOWN")], legacy=legacy)
                    expected_profile = (contract.LEGACY_SCHEMA_VERSION if legacy
                                        else contract.RECENT_SCHEMA_VERSION)
                    tape = contract.normalize_parquet(path, env)
                    self.assertEqual(tape.rows[0]["source_schema_version"], expected_profile)
                    self.assertNotIn("source_schema_version", json.loads(tape.content_json)["rows"][0])
                    result = store.ingest(path, env)
                    self.assertEqual(result["schema_version"], expected_profile)
                    self.assertEqual(result["content_version"], 1)
                    self.assertEqual(result["observation_seq"], index)
                    self.assertEqual(result["observation_state"],
                                     "CONTENT_FIRST_SEEN" if index == 1 else "CONTENT_REPEAT")
                    self.assertEqual(store.read_rows(result["capture_id"])[0]["source_schema_version"],
                                     expected_profile)
                    self.assertEqual(store.verify(result["capture_id"]), result)
                    results.append(result)
                    tapes.append(tape)
                self.assertNotEqual(results[0]["raw_response_sha256"], results[1]["raw_response_sha256"])
                self.assertNotEqual(results[0]["schema_version"], results[1]["schema_version"])
                self.assertNotEqual(results[0]["schema_fingerprint"], results[1]["schema_fingerprint"])
                self.assertEqual(tapes[0].content_json, tapes[1].content_json)
                self.assertEqual(results[0]["normalized_content_sha256"], results[1]["normalized_content_sha256"])
                self.assertEqual(results[1]["previous_content_capture_id"], results[0]["capture_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
