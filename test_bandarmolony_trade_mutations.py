"""Offline, rerunnable behavioral mutation gate for the immutable trade tape.

Run ``python test_bandarmolony_trade_mutations.py --run``. Every mutant gets
fresh source copies and a fresh Python process outside any Git repository.
The baseline must pass. Only assertion failures count as kills; syntax,
import, setup, and unexpected runtime errors make the gate fail as invalid.
All inputs are synthetic and all artifacts live in temporary directories.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Callable
import unittest
from unittest.mock import patch


class BehavioralAssertionFailure(AssertionError):
    """Only explicit assertions about the mutated guarantee count as kills."""


def make_tree_writable(root):
    """Clear fixture read-only attributes before native Windows temp cleanup."""
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def git(root, *arguments, check=True):
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    return subprocess.run(["git", "-C", str(root), *arguments], check=check,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=environment, timeout=30)


class MutationBehaviorTests(unittest.TestCase):
    """Focused synthetic assertions for the requested contract guarantees."""

    def setUp(self):
        import bandarmolony_trade_capture as capture
        import bandarmolony_trade_contract as contract
        import test_bandarmolony_trade_capture as fixtures

        self.capture = capture
        self.contract = contract
        self.fixtures = fixtures
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.callback(make_tree_writable, self.root)
        self.store = self.stack.enter_context(capture.TradeCaptureStore(
            self.root / "private" / "captures.db", self.root / "private" / "raw"
        ))

    def envelope(self, name="mutation-capture-a", **changes):
        return self.fixtures.envelope(name, **changes)

    def row(self, **changes):
        values = {
            "BRK_COD1": "AA", "BRK_COD2": "BB", "TRX_CODE": 12001,
            "STK_VOLM": 137, "STK_PRIC": 103, "VALUE": Decimal("141.11"),
            "TRX_TYPE": "NG", "TRX_ORD1": 1001, "TRX_ORD2": 1002,
        }
        values.update(changes)
        return self.fixtures.source_row(**values)

    def file(self, rows=None, name="synthetic.parquet", **options):
        path = self.root / name
        self.fixtures.write_parquet(path, rows=[self.row()] if rows is None else rows,
                                    **options)
        return path

    @contextmanager
    def behavior(self):
        """Mark the intended assertion, leaving arrangement failures invalid."""
        previous = self.failureException
        self.failureException = BehavioralAssertionFailure
        try:
            yield
        finally:
            self.failureException = previous

    def equal(self, actual, expected):
        with self.behavior():
            self.assertEqual(actual, expected)

    def rejects(self, action, error=None):
        with self.behavior(), self.assertRaises(error or self.contract.TradeContractError):
            action()

    def valid(self, action):
        # A rejected arranging fixture is an unexpected error, never a kill.
        return action()

    def tape(self, rows=None, **options):
        path = self.file(rows, **options)
        return self.valid(lambda: self.contract.normalize_parquet(path, self.envelope()))

    def ingest(self, rows=None, name="mutation-capture-a", **options):
        path = self.file(rows, name=name + ".parquet", **options)
        minute = 1 if name.endswith("-b") else 2 if name.endswith("-c") else 0
        env = self.envelope(name, requested_at=f"2026-10-01T10:{minute:02d}:00.123456Z",
                            response_at=f"2026-10-01T10:{minute:02d}:01.654321Z")
        return self.store.ingest(path, env)

    def test_swap_buyer_seller(self):
        row = self.tape().rows[0]
        self.equal((row["buyer_broker"], row["seller_broker"]), ("AA", "BB"))

    def test_shares_are_not_lots(self):
        self.equal(self.tape().rows[0]["shares"], 137)

    def test_value_times_100(self):
        self.equal(self.contract._source_value_rp(Decimal("141")), 14100)

    def test_legacy_board_unknown(self):
        self.equal(self.tape(legacy=True).rows[0]["board"], "UNKNOWN")

    def test_haka_is_vendor_only(self):
        row = self.tape().rows[0]
        with self.behavior():
            self.assertIn("vendor_haka_haki", row)
            self.assertTrue({"aggressor", "initiator", "buyer_initiated", "seller_initiated"}
                            .isdisjoint(row))

    def test_previous_revision_cannot_be_overwritten(self):
        first = self.ingest()
        self.ingest([self.row(STK_PRIC=104, VALUE=Decimal("142.48"))],
                    name="mutation-capture-b")
        with self.behavior(), self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "UPDATE trade_captures SET content_json='{}' WHERE capture_id=?",
                (first["capture_id"],))

    def test_first_capture_has_no_finality(self):
        self.equal(self.capture.TradeCaptureStore._content_relation(self.envelope(), "0" * 64, None),
                   ("CONTENT_FIRST_SEEN", 1))

    def test_absence_has_no_zero_tape(self):
        result = self.valid(lambda: self.store.observe_absence(
            self.envelope(http_status=404)))
        with self.behavior():
            self.assertEqual(result["observation_state"], "ABSENT_OBSERVED")
            self.assertIsNone(result["row_count"])
            self.assertIsNone(result["normalized_content_sha256"])

    def test_raw_hash_tamper(self):
        # Equal-length footer metadata changes preserve rows, schema, and size.
        import pyarrow.parquet as pq

        path = self.file()
        table = pq.read_table(path).replace_schema_metadata({b"mutation": b"raw-a"})
        pq.write_table(table, path, compression="zstd", use_dictionary=True)
        first = self.valid(lambda: self.store.ingest(path, self.envelope()))
        raw_root = self.root / "private" / "raw"
        raw = next(raw_root.rglob(first["raw_response_sha256"] + ".parquet"))
        table = table.replace_schema_metadata({b"mutation": b"raw-b"})
        replacement = self.root / "changed-footer.parquet"
        pq.write_table(table, replacement, compression="zstd", use_dictionary=True)
        self.assertEqual(raw.stat().st_size, replacement.stat().st_size)
        raw.chmod(0o600)
        raw.write_bytes(replacement.read_bytes())
        with self.behavior(), self.assertRaises(self.contract.TradeContractError):
            self.store.verify(first["capture_id"])

    def coherent_sql_tamper(self, capture_id, **changes):
        # Re-sign the body after removing then restoring its immutable triggers.
        # This isolates content-hash validation from the independent body seal.
        conn = self.store.conn
        triggers = list(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
        for name, _sql in triggers:
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        conn.execute("PRAGMA ignore_check_constraints=ON")
        assignments = ", ".join('"' + key + '"=?' for key in changes)
        conn.execute("UPDATE trade_captures SET " + assignments + " WHERE capture_id=?",
                     (*changes.values(), capture_id))
        cursor = conn.execute("SELECT * FROM trade_captures WHERE capture_id=?",
                              (capture_id,))
        record = dict(zip([col[0] for col in cursor.description], cursor.fetchone()))
        body_digest = self.capture._body_digest(record)
        conn.execute("UPDATE trade_captures SET body_sha256=? WHERE capture_id=?",
                     (body_digest, capture_id))
        conn.execute("UPDATE trade_acceptances SET body_sha256=? WHERE capture_id=?",
                     (body_digest, capture_id))
        for _name, sql in triggers:
            conn.execute(sql)
        conn.commit()
        conn.execute("PRAGMA ignore_check_constraints=OFF")

    def test_normalized_hash_tamper(self):
        first = self.ingest()
        self.coherent_sql_tamper(first["capture_id"], normalized_content_sha256="0" * 64)
        with self.behavior(), self.assertRaises(self.contract.TradeContractError):
            self.store.verify(first["capture_id"])

    def test_conflicting_duplicate_rejected(self):
        path = self.file([self.row(), self.row(BRK_COD1="CC")])
        with self.behavior(), self.assertRaises(self.contract.TradeContractError):
            self.contract.normalize_parquet(path, self.envelope())

    def test_timestamps_excluded_from_hash(self):
        path = self.file()
        first = self.valid(lambda: self.contract.normalize_parquet(path, self.envelope()))
        second = self.valid(lambda: self.contract.normalize_parquet(path, self.envelope(
            "mutation-capture-b", requested_at="2026-10-02T02:00:00.123456Z",
            response_at="2026-10-02T02:00:01.654321Z")))
        self.equal(first.normalized_content_sha256, second.normalized_content_sha256)

    def test_source_sas_query_stripped(self):
        source = "https://example.invalid/trades/DEWA.parquet?sig=dummy-mutation-secret&sv=1"
        env = self.envelope(source_path_without_query_or_token=source)
        expected = "https://example.invalid/trades/DEWA.parquet"
        with self.behavior():
            self.assertEqual(env.source_path_without_query_or_token, expected)
            self.assertNotIn("dummy-mutation-secret", repr(env))
        first = self.valid(lambda: self.store.ingest(self.file(), env))
        with self.behavior():
            self.assertNotIn("dummy-mutation-secret", json.dumps(self.store.inspect(first["capture_id"])))

    def test_tracked_output_refused(self):
        repository = self.root / "synthetic-git"
        repository.mkdir()
        (repository / ".gitignore").write_text("private/\n", encoding="utf-8")
        tracked = repository / "private" / "tracked.parquet"
        tracked.parent.mkdir()
        tracked.write_bytes(b"synthetic tracked sentinel, not parquet")
        git(repository, "init", "-q")
        git(repository, "add", "-f", str(tracked))
        with self.behavior(), self.assertRaises(self.contract.TradeContractError):
            self.capture.check_private_output(tracked.parent)

    def test_deleted_tracked_case_variant_refused(self):
        repository = self.root / "synthetic-case-git"
        repository.mkdir()
        # Ignore both spellings so only the tracked-path guard can refuse the
        # query. Remove the directory too: native Windows resolve() cannot
        # recover an existing on-disk spelling and hide the need for icase.
        (repository / ".gitignore").write_text(
            "private_data/\nPRIVATE_DATA/\n", encoding="utf-8")
        tracked = repository / "private_data" / "capture.db"
        tracked.parent.mkdir()
        tracked.write_bytes(b"synthetic tracked sentinel, not a database")

        git(repository, "init", "-q")
        git(repository, "config", "core.ignorecase", "false")
        git(repository, "add", "-f", "private_data/capture.db")
        tracked.unlink()
        tracked.parent.rmdir()
        requested = repository / "PRIVATE_DATA" / "CAPTURE.DB"
        # Arrangement failures are INVALID, outside the behavioral assertion.
        self.assertFalse(requested.exists())
        self.assertFalse(requested.parent.exists())
        relative = requested.resolve().relative_to(repository.resolve()).as_posix()
        self.assertEqual(relative, "PRIVATE_DATA/CAPTURE.DB")
        self.assertEqual(git(repository, "ls-files", "--", ":(literal)" + relative).stdout, "")
        self.assertEqual(git(repository, "ls-files", "--", ":(icase,literal)" + relative).stdout,
                         "private_data/capture.db\n")
        git(repository, "check-ignore", "-q", "--", relative)
        with self.behavior(), self.assertRaisesRegex(
                self.contract.TradeContractError, "^tracked output destination refused$"):
            self.capture.check_private_output(requested)

    def test_ticker_date_mismatch_refused(self):
        for changes in ({"STK_CODE": "ZZZZ"}, {"TRX_DATE": date(2026, 9, 30)}):
            with self.subTest(field=next(iter(changes))):
                path = self.file([self.row(**changes)])
                with self.behavior(), self.assertRaises(self.contract.TradeContractError):
                    self.contract.normalize_parquet(path, self.envelope())

    def test_unsupported_schema_refused(self):
        import pyarrow as pa

        path = self.file(extra={"UNSUPPORTED_COLUMN": (pa.int32(), 1)})
        with self.behavior(), self.assertRaises(self.contract.TradeContractError):
            self.contract.normalize_parquet(path, self.envelope())

    def test_money_remains_exact_integer(self):
        shares = 2 ** 53 + 1
        with localcontext() as context:
            context.prec = 40
            source_value = Decimal(shares) / Decimal(100)
        value = self.contract._source_value_rp(source_value)
        with self.behavior():
            self.assertIs(type(value), int)
            self.assertEqual(value, shares)

    def test_immutable_acceptance_cannot_be_bypassed(self):
        first = self.ingest()
        # FK checks may be disabled by another SQLite client; the trigger must
        # still reject acceptance without a validated body.
        self.store.conn.commit()
        self.store.conn.execute("PRAGMA foreign_keys=OFF")
        with self.behavior(), self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "INSERT INTO trade_acceptances(capture_id,durable_accepted_at,body_sha256) "
                "VALUES(?,?,?)", ("missing-body", "2026-10-02T03:00:00.000001Z", "0" * 64))
        with self.behavior(), self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "UPDATE trade_acceptances SET body_sha256=? WHERE capture_id=?",
                ("0" * 64, first["capture_id"]))

    def test_state_recomputed_from_raw_and_history(self):
        first = self.ingest()
        self.coherent_sql_tamper(first["capture_id"], observation_state="CONTENT_REPEAT")
        self.rejects(lambda: self.store.verify(first["capture_id"]))

    def test_duplicate_count_recomputed(self):
        first = self.ingest([self.row(), self.row()])
        self.coherent_sql_tamper(first["capture_id"], duplicate_counts_json="[]")
        self.rejects(lambda: self.store.verify(first["capture_id"]))

    def test_schema_fingerprint_recomputed(self):
        first = self.ingest()
        self.coherent_sql_tamper(first["capture_id"], schema_fingerprint="0" * 64)
        self.rejects(lambda: self.store.verify(first["capture_id"]))

    def test_trigger_body_verified(self):
        first = self.ingest()
        name = "immutable_trade_captures_update"
        self.store.conn.execute("DROP TRIGGER " + name)
        self.store.conn.execute("CREATE TRIGGER " + name +
                                " BEFORE UPDATE ON trade_captures BEGIN SELECT 1; END")
        self.store.conn.commit()
        self.rejects(lambda: self.store.verify(first["capture_id"]))

    def test_cli_read_only_purity(self):
        first = self.ingest()
        self.store.close()
        def files():
            return {str(path.relative_to(self.root)): path.read_bytes()
                    for path in self.root.rglob("*") if path.is_file()}

        before = files()
        args = ["inspect", "--db", str(self.root / "private" / "captures.db"),
                "--raw-root", str(self.root / "private" / "raw"),
                "--capture-id", first["capture_id"]]
        with redirect_stdout(io.StringIO()):
            result = self.capture.main(args)
        with self.behavior():
            self.assertEqual(result, 0)
            self.assertEqual(files(), before)
        connection = sqlite3.connect(self.root / "private" / "captures.db")
        try:
            connection.execute("PRAGMA journal_mode=WAL").fetchone()
        finally:
            connection.close()
        before = files()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = self.capture.main(args)
        with self.behavior():
            self.assertEqual(result, 1)
            self.assertEqual(files(), before)

    def test_zero_shares_and_price_refused(self):
        for field in ("STK_VOLM", "STK_PRIC"):
            path = self.file([self.row(**{field: 0, "VALUE": Decimal(0)})])
            self.rejects(lambda: self.contract.normalize_parquet(path, self.envelope()))

    def test_float_physical_type_refused(self):
        import pyarrow as pa

        # 137 shares × 100 Rp / 100 is exactly representable in FLOAT, so this
        # isolates schema refusal rather than conversion imprecision.
        path = self.file([self.row(STK_PRIC=100, VALUE=137.0)],
                         field_types={"VALUE": pa.float32()})
        self.rejects(lambda: self.contract.normalize_parquet(path, self.envelope()))

    def test_legacy_trx_type_exact_zero(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = self.file(legacy=True)
        table = pq.read_table(path)
        index = table.schema.get_field_index("TRX_TYPE")
        table = table.set_column(index, table.schema.field(index), pa.array([1], type=pa.int32()))
        pq.write_table(table, path)
        self.rejects(lambda: self.contract.normalize_parquet(path, self.envelope()))

    def test_source_fragment_stripped(self):
        env = self.envelope(source_path_without_query_or_token=
                            "https://example.invalid/trades/DEWA.parquet#private-fragment")
        self.equal(env.source_path_without_query_or_token,
                   "https://example.invalid/trades/DEWA.parquet")

    def test_content_length_verified(self):
        first = self.ingest()
        self.coherent_sql_tamper(first["capture_id"], content_length=first["content_length"] + 1)
        self.rejects(lambda: self.store.verify(first["capture_id"]))

    def test_raw_destination_verified(self):
        data = self.file().read_bytes()
        digest = self.contract.sha256_bytes(data)
        destination = self.store.raw_path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"different synthetic bytes at the same digest name")
        self.rejects(lambda: self.store._preserve_raw(data, digest))

    def test_parent_acceptance_order_verified(self):
        first = self.ingest()
        second = self.ingest(name="mutation-capture-b")
        earlier = datetime.fromisoformat(first["durable_accepted_at"].replace("Z", "+00:00"))
        earlier -= timedelta(microseconds=1)
        self.coherent_sql_tamper(second["capture_id"],
                                body_recorded_at=self.contract.utc_text(earlier))
        self.rejects(lambda: self.store.verify(second["capture_id"]))

    def test_absence_null_fields_verified(self):
        first = self.store.observe_absence(self.envelope(http_status=404))
        self.coherent_sql_tamper(first["capture_id"], content_length=1)
        self.rejects(lambda: self.store.verify(first["capture_id"]))

    def test_requires_parent_trigger(self):
        first = self.ingest()
        record = self.store._get_record(first["capture_id"])
        record.update(capture_id="orphan-child", observation_seq=2,
                      previous_observation_id="missing-parent")
        self.store.conn.commit()
        self.store.conn.execute("PRAGMA foreign_keys=OFF")
        columns = tuple(record)
        sql = ("INSERT INTO trade_captures (" + ",".join(columns) + ") VALUES (" +
               ",".join("?" for _ in columns) + ")")
        self.rejects(lambda: self.store.conn.execute(sql, tuple(record.values())),
                     sqlite3.IntegrityError)

    def test_meta_immutability_trigger(self):
        self.rejects(lambda: self.store.conn.execute(
            "UPDATE trade_store_meta SET value='mutated' WHERE key='schema_version'"),
                     sqlite3.IntegrityError)

    def test_source_chronology_refused(self):
        self.ingest()
        for status in (200, 404):
            env = self.envelope("chronology-" + str(status), http_status=status)
            action = (lambda: self.store.ingest(self.file(), env)) if status == 200 else (
                lambda: self.store.observe_absence(env))
            self.rejects(action)
            self.equal(self.store.conn.execute("SELECT COUNT(*) FROM trade_captures").fetchone()[0], 1)

    def test_source_chronology_verified(self):
        self.ingest()
        second = self.ingest(name="mutation-capture-b")
        record = self.store._get_record(second["capture_id"])
        metadata = json.loads(record["metadata_json"])
        metadata.update(requested_at="2026-10-01T10:00:00.123456Z",
                        response_at="2026-10-01T10:00:01.654321Z")
        self.coherent_sql_tamper(second["capture_id"],
                                metadata_json=self.contract.canonical_json(metadata))
        self.rejects(lambda: self.store.verify(second["capture_id"]))

    def test_raw_encoding_repeat_keeps_content_version(self):
        first = self.ingest()
        second = self.ingest(name="mutation-capture-b", compression="none",
                             use_dictionary=False)
        self.assertNotEqual(first["raw_response_sha256"], second["raw_response_sha256"])
        with self.behavior():
            self.assertEqual(second["observation_seq"], 2)
            self.assertEqual(second["observation_state"], "CONTENT_REPEAT")
            self.assertEqual(second["content_version"], first["content_version"])

    def test_source_schema_provenance_keeps_semantic_identity(self):
        first = self.ingest(legacy=True)
        second = self.ingest([self.row(TRX_TYPE="UNKNOWN")], name="mutation-capture-b")
        self.assertNotEqual(first["schema_version"], second["schema_version"])
        with self.behavior():
            self.assertEqual(second["normalized_content_sha256"], first["normalized_content_sha256"])
            self.assertEqual(second["observation_state"], "CONTENT_REPEAT")
            self.assertEqual(second["content_version"], first["content_version"])
            self.assertEqual(self.store.read_rows(first["capture_id"])[0]["source_schema_version"],
                             first["schema_version"])
            self.assertEqual(self.store.read_rows(second["capture_id"])[0]["source_schema_version"],
                             second["schema_version"])

    def test_zero_row_body_refused(self):
        for legacy in (False, True):
            path = self.file([], legacy=legacy)
            self.rejects(lambda: self.contract.normalize_parquet(path, self.envelope()))

    def test_rejected_body_never_published(self):
        for content in (b"<html>synthetic login</html>", b"not parquet"):
            path = self.root / "rejected.parquet"
            path.write_bytes(content)
            with self.assertRaises(self.contract.TradeContractError):
                self.store.ingest(path, self.envelope())
        with self.behavior():
            self.assertEqual(list((self.root / "private" / "raw").rglob("*.parquet")), [])
            self.assertEqual(list((self.root / "private" / "raw").rglob(".pending-*")), [])


@dataclass(frozen=True)
class Mutation:
    name: str
    module: str
    test: str
    transform: Callable[[str], str]


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("mutation anchor must match exactly once")
    return source.replace(old, new, 1)


def replace_in_function(source, function, old, new):
    tree = ast.parse(source)
    if "." in function:
        owner, function = function.split(".", 1)
        owners = [node for node in ast.walk(tree)
                  if isinstance(node, ast.ClassDef) and node.name == owner]
        if len(owners) != 1:
            raise ValueError("class anchor must match exactly once")
        candidates = owners[0].body
    else:
        candidates = ast.walk(tree)
    matches = [node for node in candidates
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function]
    if len(matches) != 1:
        raise ValueError("function anchor must match exactly once")
    node = matches[0]
    lines = source.splitlines(keepends=True)
    before = "".join(lines[:node.lineno - 1])
    function_source = "".join(lines[node.lineno - 1:node.end_lineno])
    after = "".join(lines[node.end_lineno:])
    return before + replace_once(function_source, old, new) + after


def mutations():
    """Explicit source patches; each changes one requested contract behavior."""
    contract = "bandarmolony_trade_contract.py"
    capture = "bandarmolony_trade_capture.py"

    def in_function(name, old, new):
        return lambda source: replace_in_function(source, name, old, new)

    def swap_brokers(source):
        source = replace_in_function(source, "normalize_parquet",
            '"buyer_broker": _code(row["BRK_COD1"], "buyer broker"),',
            '"buyer_broker": _code(row["BRK_COD2"], "buyer broker"),')
        return replace_in_function(source, "normalize_parquet",
            '"seller_broker": _code(row["BRK_COD2"], "seller broker"),',
            '"seller_broker": _code(row["BRK_COD1"], "seller broker"),')

    def skip_normalized_hash(source):
        source = replace_in_function(source, "_verify_record",
            'if tape.normalized_content_sha256 != record["normalized_content_sha256"]:',
            'if False:')
        return replace_in_function(source, "_verify_record",
            'if sha256_bytes(record["content_json"].encode("utf-8")) != record["normalized_content_sha256"]:',
            'if False:')

    def accept_mismatch(source):
        source = replace_in_function(source, "normalize_parquet",
            'if row["STK_CODE"] != envelope.ticker:', 'if False:')
        return replace_in_function(source, "normalize_parquet",
            'if _source_date(row["TRX_DATE"]) != envelope.trade_date:', 'if False:')

    def accept_unknown_schema(source):
        source = replace_in_function(source, "_validated_schema",
            'if columns == RECENT_COLUMNS:', 'if RECENT_COLUMNS <= columns:')
        return replace_in_function(source, "_validated_schema",
            'else:\n            supported = False', 'else:\n            supported = True')

    def permit_prior_update(source):
        # Remove only the persisted UPDATE guard. The modified implementation
        # still creates and verifies its own otherwise unchanged schema.
        anchor = 'TRIGGER_SQL["trade_acceptance_requires_body"] = '
        return replace_once(source, anchor,
            'TRIGGER_SQL.pop("immutable_trade_captures_update")\n' + anchor)

    def bypass_acceptance(source):
        anchor = 'TRIGGER_SQL["trade_capture_requires_parent"] = '
        return replace_once(source, anchor,
            'TRIGGER_SQL.pop("immutable_trade_acceptances_update")\n' + anchor)

    def drop_trigger(name):
        return lambda source: replace_once(source, "def _sql_text(value):",
            f'TRIGGER_SQL.pop("{name}")\n\ndef _sql_text(value):')

    def allow_zero(source):
        source = replace_in_function(source, "normalize_parquet",
            'shares = _integer(row["STK_VOLM"], "shares", 1)',
            'shares = _integer(row["STK_VOLM"], "shares", 0)')
        return replace_in_function(source, "normalize_parquet",
            'price_idr = _integer(row["STK_PRIC"], "price_idr", 1)',
            'price_idr = _integer(row["STK_PRIC"], "price_idr", 0)')

    return (
        Mutation("swap_buyer_seller_broker", contract, "test_swap_buyer_seller", swap_brokers),
        Mutation("treat_shares_as_lots", contract, "test_shares_are_not_lots",
                 in_function("normalize_parquet", '"shares": shares,', '"shares": shares * 100,')),
        Mutation("forget_value_times_100", contract, "test_value_times_100",
                 in_function("_source_value_rp", 'exponent = parts.exponent + 2',
                             'exponent = parts.exponent')),
        Mutation("infer_legacy_board_rg", contract, "test_legacy_board_unknown",
                 in_function("normalize_parquet", 'board = "UNKNOWN"', 'board = "RG"')),
        Mutation("haka_as_aggressor", contract, "test_haka_is_vendor_only",
                 in_function("normalize_parquet", '"vendor_haka_haki": _code',
                             '"aggressor": _code')),
        Mutation("overwrite_prior_revision", capture, "test_previous_revision_cannot_be_overwritten",
                 permit_prior_update),
        Mutation("first_capture_as_repeat", capture, "test_first_capture_has_no_finality",
                 in_function("_content_relation", 'return "CONTENT_FIRST_SEEN", 1',
                             'return "CONTENT_REPEAT", 1')),
        Mutation("skip_raw_hash_verification", capture, "test_raw_hash_tamper",
                 in_function("_verify_record", 'if sha256_bytes(data) != record["raw_response_sha256"]:',
                             'if False:')),
        Mutation("skip_normalized_hash_verification", capture, "test_normalized_hash_tamper",
                 skip_normalized_hash),
        Mutation("allow_conflicting_natural_key", contract, "test_conflicting_duplicate_rejected",
                 in_function("normalize_parquet",
                             'if trx_code in rows_by_key and rows_by_key[trx_code] != canonical:',
                             'if False:')),
        Mutation("timestamps_in_normalized_hash", contract, "test_timestamps_excluded_from_hash",
                 in_function("normalize_parquet", '"source_schema_version": schema_version,',
                             '"source_schema_version": schema_version,\n'
                             '                    "requested_at": envelope.requested_at,')),
        Mutation("retain_sas_query", contract, "test_source_sas_query_stripped",
                 in_function("sanitize_source_path",
                             'result = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))',
                             'result = urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))')),
        Mutation("allow_tracked_output_directory", capture, "test_tracked_output_refused",
                 in_function("check_private_output", 'if tracked.returncode or tracked.stdout.strip():',
                             'if tracked.returncode == 0 and tracked.stdout.strip():\n'
                             '                return real\n'
                             '            if tracked.returncode or tracked.stdout.strip():')),
        Mutation("remove_icase_tracked_path_protection", capture,
                 "test_deleted_tracked_case_variant_refused",
                 in_function("check_private_output", '":(icase,literal)" + relative',
                             '":(literal)" + relative')),
        Mutation("accept_ticker_date_mismatch", contract, "test_ticker_date_mismatch_refused",
                 accept_mismatch),
        Mutation("accept_unsupported_schema", contract, "test_unsupported_schema_refused",
                 accept_unknown_schema),
        Mutation("floating_point_money", contract, "test_money_remains_exact_integer",
                 in_function("_source_value_rp", 'if type(value) is int:',
                             'return int(float(value) * 100)\n    if type(value) is int:')),
        Mutation("bypass_immutable_acceptance", capture, "test_immutable_acceptance_cannot_be_bypassed",
                 bypass_acceptance),
        Mutation("skip_state_recomputation", capture, "test_state_recomputed_from_raw_and_history",
                 in_function("_verify_chain", 'record["observation_state"] != expected_state', 'False')),
        Mutation("skip_duplicate_count_verification", capture, "test_duplicate_count_recomputed",
                 in_function("_verify_record", '"duplicate_counts_json": canonical_json(tape.duplicate_counts),',
                             '"duplicate_counts_json": record["duplicate_counts_json"],')),
        Mutation("skip_schema_fingerprint_verification", capture, "test_schema_fingerprint_recomputed",
                 in_function("_verify_record", '"schema_fingerprint": tape.schema_fingerprint,',
                             '"schema_fingerprint": record["schema_fingerprint"],')),
        Mutation("verify_trigger_names_only", capture, "test_trigger_body_verified",
                 in_function("_verify_schema", 'if actual != expected:',
                             'if set(actual) != set(expected):')),
        Mutation("cli_writable_inspection", capture, "test_cli_read_only_purity",
                 in_function("main", 'read_only=args.command != "resume"', 'read_only=False')),
        Mutation("cli_read_wal_mode", capture, "test_cli_read_only_purity",
                 in_function("_require_checkpointed_database",
                             'if header[:16] == b"SQLite format 3\\x00" and b"\\x02" in header[18:20]:',
                             'if False:')),
        Mutation("allow_zero_shares_price", contract, "test_zero_shares_and_price_refused", allow_zero),
        Mutation("allow_float_physical_type", contract, "test_float_physical_type_refused",
                 in_function("_value_type", 'column.physical_type == "DOUBLE"',
                             'column.physical_type in {"DOUBLE", "FLOAT"}')),
        Mutation("allow_nonzero_legacy_type", contract, "test_legacy_trx_type_exact_zero",
                 in_function("normalize_parquet", 'row["TRX_TYPE"] != 0', 'False')),
        Mutation("retain_source_fragment", contract, "test_source_fragment_stripped",
                 in_function("sanitize_source_path",
                             'result = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))',
                             'result = urlunsplit((parsed.scheme.lower(), netloc, path, "", parsed.fragment))')),
        Mutation("skip_content_length_verification", capture, "test_content_length_verified",
                 in_function("_verify_record", 'if len(data) != record["content_length"]:', 'if False:')),
        Mutation("skip_raw_destination_verification", capture, "test_raw_destination_verified",
                 in_function("_preserve_raw", 'if not created and (destination.is_symlink() or destination.read_bytes() != data):',
                             'if False:')),
        Mutation("skip_parent_acceptance_order", capture, "test_parent_acceptance_order_verified",
                 in_function("_verify_chain", 'if prior_marker is None or record["body_recorded_at"] < prior_marker["durable_accepted_at"]:',
                             'if False:')),
        Mutation("skip_404_null_field_invariant", capture, "test_absence_null_fields_verified",
                 in_function("_verify_record", 'or any(record[key] is not None for key in (',
                             'or any(False for key in (')),
        Mutation("remove_requires_parent_trigger", capture, "test_requires_parent_trigger",
                 drop_trigger("trade_capture_requires_parent")),
        Mutation("remove_meta_immutability_trigger", capture, "test_meta_immutability_trigger",
                 drop_trigger("immutable_trade_store_meta_update")),
        Mutation("skip_source_chronology_write", capture, "test_source_chronology_refused",
                 in_function("_record", 'self._check_chronology(envelope, previous)', 'pass')),
        Mutation("skip_source_chronology_verify", capture, "test_source_chronology_verified",
                 in_function("_verify_chain", 'self._check_chronology(envelope, previous)', 'pass')),
        Mutation("raw_reencoding_advances_content_version", capture,
                 "test_raw_encoding_repeat_keeps_content_version",
                 in_function("_content_relation", 'if normalized_digest == last_content["normalized_content_sha256"]:',
                             'if False:')),
        Mutation("source_schema_provenance_changes_semantic_identity", contract,
                 "test_source_schema_provenance_keeps_semantic_identity",
                 in_function("normalized_document", '{"source_capture_id", "source_schema_version"}',
                             '{"source_capture_id"}')),
        Mutation("allow_zero_row_tape", contract, "test_zero_row_body_refused",
                 in_function("normalize_parquet", 'if source_row_count == 0:', 'if False:')),
        Mutation("publish_rejected_body", capture, "test_rejected_body_never_published",
                 in_function("ingest", 'digest = sha256_bytes(data)',
                             'digest = sha256_bytes(data)\n        self._preserve_raw(data, digest)')),
    )


class ClassifiedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.intended_failures = 0

    def addFailure(self, test, err):
        if issubclass(err[0], BehavioralAssertionFailure):
            self.intended_failures += 1
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None and issubclass(err[0], BehavioralAssertionFailure):
            self.intended_failures += 1
        super().addSubTest(test, subtest, err)


def suite_outcome(suite):
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0,
                                     resultclass=ClassifiedResult).run(suite)
    return {"tests": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "successful": result.wasSuccessful(), "intended_failures": result.intended_failures,
            "assertion_failures": [detail for _case, detail in result.failures],
            "unexpected_errors": [detail for _case, detail in result.errors]}


def classify(outcome):
    if (outcome["tests"] == 1 and outcome["failures"] > 0
            and outcome["failures"] == outcome.get("intended_failures", 0)
            and outcome["errors"] == 0 and not outcome.get("skipped", 0)):
        return "KILLED"
    if outcome["successful"] and outcome["tests"] == 1 and not outcome.get("skipped", 0):
        return "SURVIVED"
    return "INVALID"


def run_case(name):
    suite = (unittest.defaultTestLoader.loadTestsFromTestCase(MutationBehaviorTests)
             if name == "baseline" else unittest.TestSuite([MutationBehaviorTests(name)]))
    print(json.dumps(suite_outcome(suite), sort_keys=True))
    return 0


def child(directory, case):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(directory / Path(__file__).name), "--case", case],
        cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=90)
    if completed.returncode != 0:
        return {"successful": False, "failures": 0, "errors": 1,
                "unexpected_errors": [completed.stderr[-2000:]], "tests": 0}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"successful": False, "failures": 0, "errors": 1,
                "unexpected_errors": ["child returned invalid test-result JSON"], "tests": 0}


def copy_sources(destination, source=None):
    source = Path(__file__).resolve().parent if source is None else source
    for filename in ("bandarmolony_trade_contract.py", "bandarmolony_trade_capture.py",
                     "test_bandarmolony_trade_capture.py", Path(__file__).name):
        shutil.copy2(source / filename, destination / filename)


def run_gate():
    controls = suite_outcome(unittest.defaultTestLoader.loadTestsFromTestCase(MutationClassificationTests))
    if not controls["successful"]:
        print("CLASSIFICATION FAIL " + json.dumps(controls, sort_keys=True), flush=True)
        return False
    print(f"CLASSIFICATION PASS tests={controls['tests']}", flush=True)
    specs = mutations()
    if len({spec.name for spec in specs}) != len(specs):
        raise AssertionError("distinct mutation names are required")
    counts = {"total": len(specs), "killed": 0, "survived": 0, "invalid": 0}
    with tempfile.TemporaryDirectory(prefix="bandarmolony-mutations-") as temporary:
        root = Path(temporary)
        repository = git(root, "rev-parse", "--show-toplevel", check=False)
        if repository.returncode == 0:
            raise AssertionError("mutation copies must be outside Git repositories")
        baseline = root / "baseline"
        baseline.mkdir()
        copy_sources(baseline)
        result = child(baseline, "baseline")
        expected_tests = len(unittest.defaultTestLoader.getTestCaseNames(MutationBehaviorTests))
        if not result["successful"] or result["tests"] != expected_tests:
            print("BASELINE FAIL " + json.dumps(result, sort_keys=True), flush=True)
            return False
        print(f"BASELINE PASS tests={expected_tests}", flush=True)
        for spec in specs:
            isolated = root / spec.name
            isolated.mkdir()
            # Snapshot once: concurrent edits cannot change the baseline used
            # by later mutants within this invocation.
            copy_sources(isolated, source=baseline)
            path = isolated / spec.module
            try:
                original = path.read_text(encoding="utf-8")
                changed = spec.transform(original)
                if changed == original:
                    raise ValueError("mutation did not change source")
                compile(changed, str(path), "exec")
                path.write_text(changed, encoding="utf-8")
                outcome = child(isolated, spec.test)
            except (ValueError, SyntaxError, OSError, subprocess.TimeoutExpired) as exc:
                outcome = {"successful": False, "failures": 0, "errors": 1,
                           "unexpected_errors": [type(exc).__name__ + ": " + str(exc)],
                           "tests": 0}
            status = classify(outcome)
            if status == "KILLED":
                counts["killed"] += 1
            elif status == "SURVIVED":
                counts["survived"] += 1
            else:
                counts["invalid"] += 1
            print(status + " " + spec.name + " test=" + spec.test, flush=True)
            if status == "INVALID":
                print(json.dumps(outcome, sort_keys=True), flush=True)
        print("MUTATION RESULTS " + " ".join(f"{key}={value}" for key, value in counts.items()),
              flush=True)
    return counts["killed"] == counts["total"]


class MutationClassificationTests(unittest.TestCase):
    def test_deleted_case_git_helper_ignores_inherited_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            other = Path(temporary) / "unrelated"
            other.mkdir()
            git(other, "init", "-q")
            git(other, "config", "core.ignorecase", "true")
            (other / "sentinel.txt").write_bytes(b"unrelated repository sentinel")
            git(other, "add", "sentinel.txt")
            config = (other / ".git" / "config").read_bytes()
            index = (other / ".git" / "index").read_bytes()
            spoof_index = other / "spoof-index"
            overrides = {
                "GIT_DIR": str(other / ".git"),
                "GIT_WORK_TREE": str(other),
                "GIT_INDEX_FILE": str(spoof_index),
                "GIT_COMMON_DIR": str(other / ".git"),
                "GIT_CONFIG_COUNT": "invalid-spoof-value",
            }
            environments = [{key: value} for key, value in overrides.items()]
            environments.append(overrides)
            for environment in environments:
                with self.subTest(environment=environment), patch.dict(os.environ, environment):
                    outcome = suite_outcome(unittest.TestSuite([
                        MutationBehaviorTests("test_deleted_tracked_case_variant_refused")
                    ]))
                    self.assertTrue(outcome["successful"], json.dumps(outcome, sort_keys=True))
                    self.assertEqual((other / ".git" / "config").read_bytes(), config)
                    self.assertEqual((other / ".git" / "index").read_bytes(), index)
                    self.assertFalse(spoof_index.exists())

    def outcome(self, action, *, setup=False, intended=False):
        class Probe(unittest.TestCase):
            def setUp(probe):
                if setup:
                    action(probe)

            def runTest(probe):
                if intended:
                    probe.failureException = BehavioralAssertionFailure
                if not setup:
                    action(probe)

        return classify(suite_outcome(unittest.TestSuite([Probe()])))

    def test_setup_assertion_is_invalid(self):
        self.assertEqual(self.outcome(lambda probe: probe.fail("setup"), setup=True), "INVALID")

    def test_arrangement_assertion_is_invalid(self):
        self.assertEqual(self.outcome(lambda probe: probe.fail("arrange")), "INVALID")

    def test_runtime_error_is_invalid(self):
        self.assertEqual(self.outcome(lambda _probe: exec("raise RuntimeError('runtime')")), "INVALID")

    def test_import_error_is_invalid(self):
        self.assertEqual(self.outcome(lambda _probe: exec("raise ImportError('import')")), "INVALID")

    def test_syntax_error_is_invalid(self):
        self.assertEqual(self.outcome(lambda _probe: compile("if", "mutant", "exec")), "INVALID")

    def test_intended_assertion_is_killed(self):
        self.assertEqual(self.outcome(lambda probe: probe.fail("behavior"), intended=True), "KILLED")

    def test_passing_behavior_survives(self):
        self.assertEqual(self.outcome(lambda _probe: None), "SURVIVED")


class MutationGateTests(unittest.TestCase):
    def test_all_requested_mutants_are_killed(self):
        self.assertTrue(run_gate())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--run", action="store_true", help="run the complete mutation gate")
    parser.add_argument("--case", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case:
        raise SystemExit(run_case(args.case))
    if args.run:
        raise SystemExit(0 if run_gate() else 1)
    unittest.main(argv=[sys.argv[0]], defaultTest="MutationGateTests")
