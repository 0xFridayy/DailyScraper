"""Synthetic offline trade-capture tests. Run python test_bandarmolony_trade_capture.py.

Fixtures contain invented executions only. No source account, credentials,
network collection, paid Parquet, OHLC, or actor data is used.
"""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
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
        requested_at="2026-10-01T02:00:00.123456Z",
        response_at="2026-10-01T02:00:01.654321Z",
        last_modified="2026-10-01T01:59:59.111111Z",
        x_ms_creation_time="2026-10-01T01:58:00.222222Z",
        x_ms_request_id="12345678-1234-4234-8234-123456789abc",
        source_path_without_query_or_token="https://example.invalid/trade/DEWA.parquet",
    )
    values.update(changes)
    return contract.CaptureEnvelope(**values)


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
        self.db = self.root / "private" / "trade_capture.db"
        self.raw = self.root / "private" / "trade_raw"
        self.store = capture.TradeCaptureStore(self.db, raw_root=self.raw)
        self.addCleanup(self.store.close)
        self.sequence = 0

    def ingest(self, rows=None, *, env=None, **fixture_options):
        self.sequence += 1
        if env is None:
            stamp = datetime(2026, 10, 1, 2, tzinfo=timezone.utc) + timedelta(minutes=self.sequence)
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
        self.assertEqual(second["capture_state"], "REVISED")

    def test_capture_timestamps_do_not_change_hash(self):
        path = write_parquet(self.root / "times.parquet")
        first = contract.normalize_parquet(path, envelope("capture-a"))
        second = contract.normalize_parquet(path, envelope(
            "capture-b", requested_at="2026-10-02T02:00:00.000001Z",
            response_at="2026-10-02T02:00:01.000002Z"))
        self.assertEqual(first.normalized_content_sha256, second.normalized_content_sha256)
        self.assertEqual(first.content_json, second.content_json)
        self.assertNotIn("source_capture_id", first.content_json)
        self.assertNotIn("requested_at", first.content_json)

    def test_first_seen(self):
        result = self.ingest()
        self.assertEqual(result["capture_state"], "FIRST_SEEN")
        self.assertEqual(result["capture_revision"], 1)
        self.assertIsNone(result["previous_capture_id"])

    def test_revised_immutable_history(self):
        first = self.ingest([source_row()])
        second = self.ingest([source_row(STK_VOLM=126)])
        self.assertEqual(second["capture_state"], "REVISED")
        self.assertEqual(second["capture_revision"], 2)
        self.assertEqual(second["previous_capture_id"], first["capture_id"])
        self.assertNotEqual(first["raw_response_sha256"], second["raw_response_sha256"])
        self.assertEqual(self.rows(first)[0]["shares"], 125)
        self.assertEqual(self.rows(second)[0]["shares"], 126)
        self.assertEqual(self.store.inspect(first["capture_id"])["capture_state"], "FIRST_SEEN")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 2)

    def test_repeat_confirmation(self):
        first = self.ingest()
        repeat = self.ingest()
        self.assertEqual(repeat["capture_state"], "REPEAT_CONFIRMED")
        self.assertNotEqual(first["capture_id"], repeat["capture_id"])
        self.assertEqual(first["raw_response_sha256"], repeat["raw_response_sha256"])
        self.assertEqual(first["normalized_content_sha256"], repeat["normalized_content_sha256"])
        self.assertEqual(repeat["capture_revision"], 2)

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
        self.assertEqual(set(contract.CAPTURE_STATES),
                         {"FIRST_SEEN", "REVISED", "REPEAT_CONFIRMED", "ABSENT_OBSERVED"})
        for result in results:
            self.assertNotIn("FINAL", result["capture_state"])

    def test_absence_is_not_zero_trades(self):
        result = self.store.observe_absence(envelope(http_status=404))
        self.assertEqual(result["capture_state"], "ABSENT_OBSERVED")
        for key in ("row_count", "raw_response_sha256", "normalized_content_sha256", "content_length"):
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

    def test_broker_day_share_balance(self):
        rows = self.rows(self.ingest())
        totals = contract.broker_totals(rows)
        self.assertEqual(list(totals), sorted(totals))
        self.assertEqual(sum(v["buy_shares"] for v in totals.values()), 500)
        self.assertEqual(sum(v["sell_shares"] for v in totals.values()), 500)
        self.assertEqual(sum(v["net_shares"] for v in totals.values()), 0)
        self.assertEqual(totals["AB"]["buy_shares"], 425)
        self.assertEqual(totals["AB"]["sell_shares"], 75)

    def test_broker_day_value_balance(self):
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
        with sqlite3.connect(self.db) as independent:
            self.assertEqual(independent.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
            self.assertEqual(independent.execute("SELECT count(*) FROM trade_acceptances").fetchone()[0], 0)
        with self.assertRaises(contract.TradeContractError):
            self.store.verify(env.capture_id)
        accepted = self.store.ingest(path, env)
        self.assertEqual(accepted["capture_revision"], 1)
        self.assertEqual(accepted["capture_state"], "FIRST_SEEN")
        self.assertTrue(accepted["durable_accepted_at"])
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
        self.assertEqual(second["capture_revision"], 1)
        self.assertEqual(second["capture_state"], "FIRST_SEEN")

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

    def test_parser_passes_local_handles_for_string_and_uri_shaped_path(self):
        local = write_parquet(self.root / "local.parquet")
        uri_shaped = write_parquet(self.root / "s3:" / "synthetic-bucket" / "local.parquet")
        expected = contract.normalize_parquet(local, envelope())
        for path in (str(local), uri_shaped):
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
        raw_file = self.store.raw_path(contract.sha256_bytes(original_bytes))
        parsed = []
        parser = capture.normalize_parquet

        def swap_parse_restore(source, env):
            prior = raw_file.read_bytes()
            raw_file.chmod(0o600)
            raw_file.write_bytes(replacement.read_bytes())
            try:
                tape = parser(source, env)
                parsed.append((source, tape.rows[0]["shares"]))
                return tape
            finally:
                raw_file.write_bytes(prior)
                raw_file.chmod(0o400)

        with patch.object(capture, "normalize_parquet", side_effect=swap_parse_restore):
            result = self.store.ingest(original, envelope())
        self.assertEqual(result["raw_response_sha256"], contract.sha256_bytes(original_bytes))
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
        self.assertEqual(first["capture_state"], "FIRST_SEEN")
        self.assertEqual(first["capture_revision"], 2)
        self.assertEqual(first["previous_capture_id"], absent["capture_id"])

    def test_absence_has_own_immutable_provenance(self):
        first = self.ingest()
        absent = self.store.observe_absence(envelope(
            "capture-absent", http_status=404, requested_at="2026-10-01T02:01:30.333333Z",
            response_at="2026-10-01T02:01:31.444444Z"))
        repeat = self.ingest()
        self.assertEqual(absent["previous_capture_id"], first["capture_id"])
        self.assertEqual(repeat["previous_capture_id"], absent["capture_id"])
        self.assertEqual(repeat["capture_state"], "REPEAT_CONFIRMED")
        self.assertEqual(repeat["capture_revision"], 3)
        self.assertEqual(absent["response_at"], "2026-10-01T02:01:31.444444Z")

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
            "UPDATE trade_captures SET capture_state='REVISED' WHERE capture_id=?",
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
        rewrite_immutable_row(self.store.conn,
                              "UPDATE trade_captures SET previous_capture_id=NULL WHERE capture_id=?",
                              (revised["capture_id"],))
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
        next(self.raw.rglob("*.parquet")).unlink()
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
        env = envelope(requested_at="2026-10-01T09:00:00.123456+07:00",
                       response_at="2026-10-01T09:00:01.654321+07:00")
        self.assertEqual(env.requested_at, "2026-10-01T02:00:00.123456Z")
        self.assertEqual(env.response_at, "2026-10-01T02:00:01.654321Z")
        result = self.ingest(env=env)
        self.assertEqual(result["requested_at"], env.requested_at)
        self.assertEqual(result["response_at"], env.response_at)
        self.assertRegex(result["durable_accepted_at"], r"\.\d{6}Z$")

    def test_envelope_validation_and_immutability(self):
        for changes in (
            {"ticker": "dewa"}, {"ticker": "DEWA?sig=dummy"}, {"trade_date": "2026-02-30"},
            {"requested_at": "2026-10-01T02:00:00"},
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
        next(self.raw.rglob("*.parquet")).unlink()
        with self.assertRaises(contract.TradeContractError):
            self.store.inspect(result["capture_id"])

    def test_offline_cli_ingest_verify_inspect(self):
        path = write_parquet(self.root / "cli.parquet")
        cli_db = self.root / "cli-private" / "capture.db"
        command = [sys.executable, str(Path(capture.__file__)), "ingest", "--db", str(cli_db),
                   "--ticker", "DEWA", "--trade-date", "2026-10-01", "--file", str(path),
                   "--requested-at", "2026-10-01T02:00:00.123456Z",
                   "--response-at", "2026-10-01T02:00:01.654321Z", "--capture-id", "cli-capture"]
        ingested = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(ingested.stdout)["capture_state"], "FIRST_SEEN")
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
             "--requested-at", "2026-10-01T02:00:00Z", "--response-at", "2026-10-01T02:00:01Z",
             "--source-path", f"https://example.invalid/file?sig={dummy}"],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(dummy, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
