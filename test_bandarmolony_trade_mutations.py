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
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Callable
import unittest


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

    def valid(self, action):
        """A valid fixture rejected by a mutant is a behavioral assertion."""
        try:
            return action()
        except (self.contract.TradeContractError, sqlite3.IntegrityError) as exc:
            self.fail("valid synthetic input was rejected: " + type(exc).__name__)

    def tape(self, rows=None, **options):
        path = self.file(rows, **options)
        return self.valid(lambda: self.contract.normalize_parquet(path, self.envelope()))

    def ingest(self, rows=None, name="mutation-capture-a", **options):
        path = self.file(rows, name=name + ".parquet", **options)
        return self.valid(lambda: self.store.ingest(path, self.envelope(name)))

    def test_swap_buyer_seller(self):
        row = self.tape().rows[0]
        self.assertEqual((row["buyer_broker"], row["seller_broker"]), ("AA", "BB"))

    def test_shares_are_not_lots(self):
        self.assertEqual(self.tape().rows[0]["shares"], 137)

    def test_value_times_100(self):
        self.assertEqual(self.tape().rows[0]["value_rp"], 14111)

    def test_legacy_board_unknown(self):
        self.assertEqual(self.tape(legacy=True).rows[0]["board"], "UNKNOWN")

    def test_haka_is_vendor_only(self):
        row = self.tape().rows[0]
        self.assertIn("vendor_haka_haki", row)
        self.assertTrue({"aggressor", "initiator", "buyer_initiated", "seller_initiated"}
                        .isdisjoint(row))

    def test_previous_revision_cannot_be_overwritten(self):
        first = self.ingest()
        self.ingest([self.row(STK_PRIC=104, VALUE=Decimal("142.48"))],
                    name="mutation-capture-b")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "UPDATE trade_captures SET content_json='{}' WHERE capture_id=?",
                (first["capture_id"],))

    def test_first_capture_has_no_finality(self):
        self.assertEqual(self.ingest()["capture_state"], "FIRST_SEEN")

    def test_absence_has_no_zero_tape(self):
        result = self.valid(lambda: self.store.observe_absence(
            self.envelope(http_status=404)))
        self.assertEqual(result["capture_state"], "ABSENT_OBSERVED")
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
        with self.assertRaises(self.contract.TradeContractError):
            self.store.verify(first["capture_id"])

    def coherent_sql_tamper(self, capture_id, digest):
        # Re-sign the body after removing then restoring its immutable triggers.
        # This isolates content-hash validation from the independent body seal.
        conn = self.store.conn
        triggers = list(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
        for name, _sql in triggers:
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        conn.execute("UPDATE trade_captures SET normalized_content_sha256=? "
                     "WHERE capture_id=?", (digest, capture_id))
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

    def test_normalized_hash_tamper(self):
        first = self.ingest()
        self.coherent_sql_tamper(first["capture_id"], "0" * 64)
        with self.assertRaises(self.contract.TradeContractError):
            self.store.verify(first["capture_id"])

    def test_conflicting_duplicate_rejected(self):
        path = self.file([self.row(), self.row(BRK_COD1="CC")])
        with self.assertRaises(self.contract.TradeContractError):
            self.contract.normalize_parquet(path, self.envelope())

    def test_timestamps_excluded_from_hash(self):
        path = self.file()
        first = self.valid(lambda: self.contract.normalize_parquet(path, self.envelope()))
        second = self.valid(lambda: self.contract.normalize_parquet(path, self.envelope(
            "mutation-capture-b", requested_at="2026-10-02T02:00:00.123456Z",
            response_at="2026-10-02T02:00:01.654321Z")))
        self.assertEqual(first.normalized_content_sha256, second.normalized_content_sha256)

    def test_source_sas_query_stripped(self):
        source = "https://example.invalid/trades/DEWA.parquet?sig=dummy-mutation-secret&sv=1"
        env = self.envelope(source_path_without_query_or_token=source)
        expected = "https://example.invalid/trades/DEWA.parquet"
        self.assertEqual(env.source_path_without_query_or_token, expected)
        self.assertNotIn("dummy-mutation-secret", repr(env))
        first = self.valid(lambda: self.store.ingest(self.file(), env))
        self.assertNotIn("dummy-mutation-secret", json.dumps(self.store.inspect(first["capture_id"])))

    def test_tracked_output_refused(self):
        repository = self.root / "synthetic-git"
        repository.mkdir()
        (repository / ".gitignore").write_text("private/\n", encoding="utf-8")
        tracked = repository / "private" / "tracked.parquet"
        tracked.parent.mkdir()
        tracked.write_bytes(b"synthetic tracked sentinel, not parquet")
        subprocess.run(["git", "init", "-q", str(repository)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repository), "add", "-f", str(tracked)],
                       check=True, capture_output=True)
        with self.assertRaises(self.contract.TradeContractError):
            self.capture.check_private_output(tracked.parent)

    def test_ticker_date_mismatch_refused(self):
        for changes in ({"STK_CODE": "ZZZZ"}, {"TRX_DATE": date(2026, 9, 30)}):
            with self.subTest(field=next(iter(changes))):
                path = self.file([self.row(**changes)])
                with self.assertRaises(self.contract.TradeContractError):
                    self.contract.normalize_parquet(path, self.envelope())

    def test_unsupported_schema_refused(self):
        import pyarrow as pa

        path = self.file(extra={"UNSUPPORTED_COLUMN": (pa.int32(), 1)})
        with self.assertRaises(self.contract.TradeContractError):
            self.contract.normalize_parquet(path, self.envelope())

    def test_money_remains_exact_integer(self):
        shares = 2 ** 53 + 1
        with localcontext() as context:
            context.prec = 40
            source_value = Decimal(shares) / Decimal(100)
        row = self.tape([self.row(STK_VOLM=shares, STK_PRIC=1, VALUE=source_value)]).rows[0]
        self.assertIs(type(row["value_rp"]), int)
        self.assertEqual(row["value_rp"], shares)

    def test_immutable_acceptance_cannot_be_bypassed(self):
        first = self.ingest()
        # FK checks may be disabled by another SQLite client; the trigger must
        # still reject acceptance without a validated body.
        self.store.conn.commit()
        self.store.conn.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "INSERT INTO trade_acceptances(capture_id,durable_accepted_at,body_sha256) "
                "VALUES(?,?,?)", ("missing-body", "2026-10-02T03:00:00.000001Z", "0" * 64))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "UPDATE trade_acceptances SET body_sha256=? WHERE capture_id=?",
                ("0" * 64, first["capture_id"]))


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
    matches = [node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == function]
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

    return (
        Mutation("swap_buyer_seller_broker", contract, "test_swap_buyer_seller", swap_brokers),
        Mutation("treat_shares_as_lots", contract, "test_shares_are_not_lots",
                 in_function("normalize_parquet", 'shares = _integer(row["STK_VOLM"], "shares", 1)',
                             'shares = _integer(row["STK_VOLM"], "shares", 1) * 100')),
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
        Mutation("first_capture_final", capture, "test_first_capture_has_no_finality",
                 in_function("_capture_state", 'return "FIRST_SEEN"', 'return "FINAL"')),
        Mutation("absence_as_zero", capture, "test_absence_has_no_zero_tape",
                 in_function("_record", '"row_count": None if tape is None else len(tape.rows),',
                             '"row_count": 0 if tape is None else len(tape.rows),')),
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
                 in_function("sanitize_source_path", 'if not isinstance(value, str) or len(value) > 4096:',
                             'return value\n    if not isinstance(value, str) or len(value) > 4096:')),
        Mutation("allow_tracked_output_directory", capture, "test_tracked_output_refused",
                 in_function("check_private_output", 'if tracked.returncode or tracked.stdout.strip():',
                             'if tracked.returncode == 0 and tracked.stdout.strip():\n'
                             '            return real\n'
                             '        if tracked.returncode or tracked.stdout.strip():')),
        Mutation("accept_ticker_date_mismatch", contract, "test_ticker_date_mismatch_refused",
                 accept_mismatch),
        Mutation("accept_unsupported_schema", contract, "test_unsupported_schema_refused",
                 accept_unknown_schema),
        Mutation("floating_point_money", contract, "test_money_remains_exact_integer",
                 in_function("_source_value_rp", 'if type(value) is int:',
                             'return int(float(value) * 100)\n    if type(value) is int:')),
        Mutation("bypass_immutable_acceptance", capture, "test_immutable_acceptance_cannot_be_bypassed",
                 bypass_acceptance),
    )


def run_case(name):
    suite = (unittest.defaultTestLoader.loadTestsFromTestCase(MutationBehaviorTests)
             if name == "baseline" else unittest.TestSuite([MutationBehaviorTests(name)]))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    payload = {"tests": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "successful": result.wasSuccessful(),
               "assertion_failures": [detail for _case, detail in result.failures],
               "unexpected_errors": [detail for _case, detail in result.errors]}
    print(json.dumps(payload, sort_keys=True))
    return 0


def child(directory, case):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(directory / Path(__file__).name), "--case", case],
        cwd=directory, env=env, capture_output=True, text=True, timeout=90)
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
    specs = mutations()
    if len(specs) != 18 or len({spec.name for spec in specs}) != 18:
        raise AssertionError("exactly 18 distinct requested mutants are required")
    counts = {"total": len(specs), "killed": 0, "survived": 0, "invalid": 0}
    with tempfile.TemporaryDirectory(prefix="bandarmolony-mutations-") as temporary:
        root = Path(temporary)
        repository = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, timeout=30)
        if repository.returncode == 0:
            raise AssertionError("mutation copies must be outside Git repositories")
        baseline = root / "baseline"
        baseline.mkdir()
        copy_sources(baseline)
        result = child(baseline, "baseline")
        if not result["successful"] or result["tests"] != 18:
            print("BASELINE FAIL " + json.dumps(result, sort_keys=True), flush=True)
            return False
        print("BASELINE PASS tests=18", flush=True)
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
            if outcome["tests"] == 1 and outcome["failures"] > 0 and outcome["errors"] == 0:
                status = "KILLED"
                counts["killed"] += 1
            elif outcome["successful"] and outcome["tests"] == 1:
                status = "SURVIVED"
                counts["survived"] += 1
            else:
                status = "INVALID"
                counts["invalid"] += 1
            print(status + " " + spec.name + " test=" + spec.test, flush=True)
            if status == "INVALID":
                print(json.dumps(outcome, sort_keys=True), flush=True)
        print("MUTATION RESULTS " + " ".join(f"{key}={value}" for key, value in counts.items()),
              flush=True)
    return counts["killed"] == counts["total"]


class MutationGateTests(unittest.TestCase):
    def test_all_requested_mutants_are_killed(self):
        self.assertTrue(run_gate())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run the complete mutation gate")
    parser.add_argument("--case", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case:
        raise SystemExit(run_case(args.case))
    if args.run:
        raise SystemExit(0 if run_gate() else 1)
    unittest.main(argv=[sys.argv[0]], defaultTest="MutationGateTests")
