"""Offline immutable BandarmoloNY done_detail capture and verification.

No transport or credentials. Raw objects keep the exact supplied bytes. Capture
bodies commit before a separate durable acceptance transaction. Each observation
is append-only, including repeat confirmations and ambiguous 404 observations.
"""

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

from bandarmolony_trade_contract import (
    CAPTURE_STATES, DATASET, CaptureEnvelope, TradeContractError,
    broker_totals, canonical_json, normalize_parquet,
    sanitize_source_path, sha256_bytes, tape_summary, utc_text,
)

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "private_data" / "bandarmolony" / "trade_capture.db"
SIDECARS = ("-journal", "-wal", "-shm")
PRODUCT = "BANDARMOLONY_TRADE_CAPTURE_V1"
STORE_VERSION = "1"


def utc_now():
    return utc_text(datetime.now(timezone.utc))


def _git(args, cwd):
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise TradeContractError("private output guard cannot run git") from None


def check_private_output(path, sidecars=()):
    """Reuse the targeted-panel guard without importing its collection modules.

    Every output and sidecar must be ignored inside any Git worktree. Also
    reject tracked descendants, which check-ignore alone cannot protect when
    a directory has been force-added. Outside a worktree, local output is safe
    from normal git-add. Git inspection failures fail closed.
    """
    real = Path(path).resolve()
    anchor = real.parent
    while not anchor.is_dir() and anchor != anchor.parent:
        anchor = anchor.parent
    top = _git(["rev-parse", "--show-toplevel"], anchor)
    if top.returncode:
        if "not a git repository" in top.stderr.lower():
            return real
        raise TradeContractError("cannot establish private output destination")
    repo = Path(top.stdout.strip())
    for candidate in (real, *(Path(str(real) + suffix) for suffix in sidecars)):
        tracked = _git(["ls-files", "--", str(candidate)], repo)
        if tracked.returncode or tracked.stdout.strip():
            raise TradeContractError("tracked output destination refused")
        ignored = _git(["check-ignore", "-q", str(candidate)], repo)
        if ignored.returncode != 0:
            raise TradeContractError("public or unignored output destination refused")
    return real


TABLE_SQL = {
    "trade_store_meta": """CREATE TABLE trade_store_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    "trade_captures": """CREATE TABLE trade_captures (
        capture_id TEXT PRIMARY KEY,
        dataset TEXT NOT NULL CHECK (dataset = 'BANDARMOLONY_DONE_DETAIL'),
        ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
        capture_revision INTEGER NOT NULL CHECK (capture_revision > 0),
        previous_capture_id TEXT REFERENCES trade_captures(capture_id),
        capture_state TEXT NOT NULL CHECK (capture_state IN
            ('FIRST_SEEN','REVISED','REPEAT_CONFIRMED','ABSENT_OBSERVED')),
        metadata_json TEXT NOT NULL,
        content_length INTEGER,
        raw_response_sha256 TEXT,
        normalized_content_sha256 TEXT,
        schema_fingerprint TEXT, schema_version TEXT,
        row_count INTEGER, source_row_count INTEGER,
        duplicate_counts_json TEXT,
        content_json TEXT,
        body_recorded_at TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        UNIQUE(ticker, trade_date, capture_revision),
        CHECK ((capture_state = 'ABSENT_OBSERVED' AND content_length IS NULL
            AND raw_response_sha256 IS NULL AND normalized_content_sha256 IS NULL
            AND schema_fingerprint IS NULL AND schema_version IS NULL
            AND row_count IS NULL AND source_row_count IS NULL
            AND duplicate_counts_json IS NULL AND content_json IS NULL)
          OR (capture_state != 'ABSENT_OBSERVED' AND content_length >= 0
            AND raw_response_sha256 IS NOT NULL AND normalized_content_sha256 IS NOT NULL
            AND schema_fingerprint IS NOT NULL AND schema_version IS NOT NULL
            AND row_count >= 0 AND source_row_count >= row_count
            AND duplicate_counts_json IS NOT NULL AND content_json IS NOT NULL)))""",
    "trade_acceptances": """CREATE TABLE trade_acceptances (
        capture_id TEXT PRIMARY KEY REFERENCES trade_captures(capture_id),
        body_sha256 TEXT NOT NULL,
        durable_accepted_at TEXT NOT NULL)""",
}
TRIGGER_SQL = {}
for _table, _keys in {
    "trade_store_meta": ("key",),
    "trade_captures": ("capture_id",),
    "trade_acceptances": ("capture_id",),
}.items():
    for _action in ("UPDATE", "DELETE"):
        _name = f"immutable_{_table}_{_action.lower()}"
        TRIGGER_SQL[_name] = (
            f"CREATE TRIGGER {_name} BEFORE {_action} ON {_table} "
            "BEGIN SELECT RAISE(ABORT, 'immutable trade capture'); END")
    _name = f"immutable_{_table}_insert"
    _same = " AND ".join(f"{key} = NEW.{key}" for key in _keys)
    if _table == "trade_captures":
        _same += " OR (ticker = NEW.ticker AND trade_date = NEW.trade_date AND capture_revision = NEW.capture_revision)"
    TRIGGER_SQL[_name] = (
        f"CREATE TRIGGER {_name} BEFORE INSERT ON {_table} "
        f"WHEN EXISTS (SELECT 1 FROM {_table} WHERE {_same}) "
        "BEGIN SELECT RAISE(ABORT, 'immutable trade capture'); END")
TRIGGER_SQL["trade_acceptance_requires_body"] = """CREATE TRIGGER trade_acceptance_requires_body
    BEFORE INSERT ON trade_acceptances
    WHEN NOT EXISTS (SELECT 1 FROM trade_captures WHERE capture_id = NEW.capture_id
        AND body_sha256 = NEW.body_sha256 AND body_recorded_at <= NEW.durable_accepted_at)
    BEGIN SELECT RAISE(ABORT, 'trade acceptance requires committed validated body'); END"""
TRIGGER_SQL["trade_capture_requires_parent"] = """CREATE TRIGGER trade_capture_requires_parent
    BEFORE INSERT ON trade_captures
    WHEN (NEW.capture_revision = 1 AND (NEW.previous_capture_id IS NOT NULL OR EXISTS
        (SELECT 1 FROM trade_captures WHERE ticker = NEW.ticker AND trade_date = NEW.trade_date)))
      OR (NEW.capture_revision > 1 AND NOT EXISTS
        (SELECT 1 FROM trade_captures c JOIN trade_acceptances a USING(capture_id)
         WHERE c.capture_id = NEW.previous_capture_id AND c.ticker = NEW.ticker
           AND c.trade_date = NEW.trade_date AND c.capture_revision = NEW.capture_revision - 1))
    BEGIN SELECT RAISE(ABORT, 'trade capture requires accepted parent'); END"""


def _sql_text(value):
    return " ".join(value.split())


def _body_digest(record):
    """Bind every persisted capture field, including provenance, to acceptance."""
    body = {key: value for key, value in dict(record).items() if key != "body_sha256"}
    return sha256_bytes(canonical_json(body).encode("utf-8"))


def _fsync_dir(path):
    if os.name == "nt":
        return  # Directory fsync is unavailable through Python on Windows.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_mkdir(path):
    """Persist each newly created directory entry before storing its children."""
    missing = []
    current = Path(path)
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise TradeContractError("output parent is not a directory") from None
        _fsync_dir(directory.parent)


class TradeCaptureStore:
    """Private, append-only local store. Readers never create or repair stores."""

    def __init__(self, db=DEFAULT_DB, raw_root=None, *, read_only=False):
        self.db = Path(db).resolve()
        self.raw_root = Path(raw_root).resolve() if raw_root is not None else self.db.parent / "trade_raw"
        self.read_only = read_only
        self._check_paths()
        if read_only:
            self.conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        else:
            _private_mkdir(self.db.parent)
            new_file = not self.db.exists()
            self.conn = sqlite3.connect(self.db)
            if new_file:
                os.chmod(self.db, 0o600)
            self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        try:
            tables = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not tables and not read_only:
                self.conn.execute("PRAGMA journal_mode = DELETE")
                self.conn.execute("PRAGMA synchronous = FULL")
                with self.conn:
                    for sql in TABLE_SQL.values():
                        self.conn.execute(sql)
                    self.conn.executemany("INSERT INTO trade_store_meta VALUES (?, ?)", self._meta().items())
                    for sql in TRIGGER_SQL.values():
                        self.conn.execute(sql)
                _fsync_dir(self.db.parent)
            self._verify_schema()
        except BaseException:
            self.conn.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        self.conn.close()

    def _meta(self):
        return {"product": PRODUCT, "schema_version": STORE_VERSION, "raw_root": str(self.raw_root)}

    def _check_paths(self):
        # Raw location is persisted in store identity. Refuse credential-shaped
        # output components before creating directories or database metadata.
        for path in (self.db, self.raw_root):
            if sanitize_source_path(str(path)) != str(path).replace("\\", "/"):
                raise TradeContractError("output paths cannot contain query or fragment credentials")
        check_private_output(self.db, SIDECARS)
        check_private_output(self.raw_root)
        check_private_output(self.raw_root / ".private-output-probe")
        if self.db == self.raw_root or self.raw_root in self.db.parents:
            raise TradeContractError("database cannot be inside the raw object store")

    def _verify_schema(self):
        actual = {(row["type"], row["name"]): _sql_text(row["sql"])
                  for row in self.conn.execute(
                      "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','trigger')")}
        expected = {("table", name): _sql_text(sql) for name, sql in TABLE_SQL.items()}
        expected.update({("trigger", name): _sql_text(sql) for name, sql in TRIGGER_SQL.items()})
        if actual != expected:
            raise TradeContractError("unsupported or tampered trade store schema")
        if dict(self.conn.execute("SELECT key, value FROM trade_store_meta")) != self._meta():
            raise TradeContractError("trade store identity or raw location mismatch")
        if self.conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TradeContractError("trade store has orphan records")

    def raw_path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise TradeContractError("invalid raw content identity")
        return self.raw_root / "sha256" / digest[:2] / (digest + ".parquet")

    def _preserve_raw(self, data, digest):
        destination = self.raw_path(digest)
        check_private_output(destination)
        _private_mkdir(destination.parent)
        # Never use rename/replace: an existing content identity cannot be overwritten.
        fd, tmp_name = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o400)
            try:
                os.link(tmp_name, destination)
            except FileExistsError:
                if destination.is_symlink() or destination.read_bytes() != data:
                    raise TradeContractError("existing raw object differs from supplied bytes")
            _fsync_dir(destination.parent)
        finally:
            os.unlink(tmp_name)
        return destination

    @contextmanager
    def _write_transaction(self):
        if self.read_only:
            raise TradeContractError("read-only trade store")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _get_record(self, capture_id):
        row = self.conn.execute("SELECT * FROM trade_captures WHERE capture_id = ?", (capture_id,)).fetchone()
        if row is None:
            raise TradeContractError("capture not found")
        return dict(row)

    def _acceptance(self, capture_id):
        row = self.conn.execute("SELECT * FROM trade_acceptances WHERE capture_id = ?", (capture_id,)).fetchone()
        return None if row is None else dict(row)

    def ingest(self, file, envelope):
        if not isinstance(envelope, CaptureEnvelope) or envelope.http_status != 200:
            raise TradeContractError("local ingest requires a successful capture envelope")
        if envelope.response_at > utc_now():
            raise TradeContractError("response timestamp cannot be in the future")
        self._check_paths()
        self._verify_schema()
        try:
            data = Path(file).read_bytes()
        except OSError:
            raise TradeContractError("cannot read local input file") from None
        digest = sha256_bytes(data)
        preserved = self._preserve_raw(data, digest)
        # Parse the exact preserved snapshot whose hash we checked. Reopening
        # the path in the parser could race a concurrent rewrite of that object.
        try:
            snapshot = preserved.read_bytes()
        except OSError:
            raise TradeContractError("preserved raw object is unreadable") from None
        if sha256_bytes(snapshot) != digest:
            raise TradeContractError("preserved raw snapshot sha256 mismatch")
        tape = normalize_parquet(snapshot, envelope)
        return self._record(envelope, digest=digest, content_length=len(data), tape=tape)

    def observe_absence(self, envelope):
        """Record a supplied 404 observation. This API makes no HTTP request."""
        if not isinstance(envelope, CaptureEnvelope) or envelope.http_status != 404:
            raise TradeContractError("absence observation requires HTTP 404 provenance")
        return self._record(envelope)

    def _record(self, envelope, *, digest=None, content_length=None, tape=None):
        self._check_paths()
        self._verify_schema()
        metadata_json = canonical_json(asdict(envelope))
        desired = {
            "metadata_json": metadata_json, "content_length": content_length,
            "raw_response_sha256": digest,
            "normalized_content_sha256": None if tape is None else tape.normalized_content_sha256,
            "schema_fingerprint": None if tape is None else tape.schema_fingerprint,
            "schema_version": None if tape is None else tape.schema_version,
            "row_count": None if tape is None else len(tape.rows),
            "source_row_count": None if tape is None else tape.source_row_count,
            "duplicate_counts_json": None if tape is None else canonical_json(tape.duplicate_counts),
            "content_json": None if tape is None else tape.content_json,
        }
        with self._write_transaction():
            existing = self.conn.execute("SELECT * FROM trade_captures WHERE capture_id = ?",
                                         (envelope.capture_id,)).fetchone()
            if existing is not None:
                existing = dict(existing)
                if any(existing[key] != value for key, value in desired.items()):
                    raise TradeContractError("capture identity conflicts with immutable body")
                self._verify_chain(envelope.capture_id, allow_pending=True)
            else:
                chain = self.conn.execute(
                    "SELECT * FROM trade_captures WHERE ticker=? AND trade_date=? ORDER BY capture_revision",
                    (envelope.ticker, envelope.trade_date)).fetchall()
                previous = dict(chain[-1]) if chain else None
                if previous:
                    self._verify_chain(previous["capture_id"])
                last_content = next((dict(row) for row in reversed(chain)
                                     if row["capture_state"] != "ABSENT_OBSERVED"), None)
                # The state depends on the new raw digest, not normalized content equality.
                state = self._capture_state(envelope, digest, previous, last_content)
                recorded = utc_now()
                if envelope.response_at > recorded:
                    raise TradeContractError("body recording cannot precede response")
                existing = dict(
                    capture_id=envelope.capture_id, dataset=DATASET,
                    ticker=envelope.ticker, trade_date=envelope.trade_date,
                    capture_revision=1 if previous is None else previous["capture_revision"] + 1,
                    previous_capture_id=None if previous is None else previous["capture_id"],
                    capture_state=state, **desired, body_recorded_at=recorded,
                )
                existing["body_sha256"] = _body_digest(existing)
                columns = tuple(existing)
                self.conn.execute(
                    f"INSERT INTO trade_captures ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    tuple(existing[column] for column in columns))
        # The body is now committed. A retry may accept ONLY this revalidated body.
        with self._write_transaction():
            self._verify_chain(envelope.capture_id, allow_pending=True)
            marker = self._acceptance(envelope.capture_id)
            if marker is None:
                accepted = utc_now()
                if accepted < existing["body_recorded_at"] or accepted < envelope.response_at:
                    raise TradeContractError("acceptance cannot precede validated body")
                self.conn.execute("INSERT INTO trade_acceptances VALUES (?, ?, ?)",
                                  (envelope.capture_id, existing["body_sha256"], accepted))
        return self.verify(envelope.capture_id)

    @staticmethod
    def _capture_state(envelope, digest, previous, last_content):
        if envelope.http_status == 404:
            return "ABSENT_OBSERVED"
        if last_content is None:
            return "FIRST_SEEN"
        if digest != last_content["raw_response_sha256"]:
            return "REVISED"
        prior = json.loads(previous["metadata_json"])
        if envelope.requested_at < prior["response_at"] or envelope.response_at <= prior["response_at"]:
            raise TradeContractError("repeat observation must independently follow the prior response")
        return "REPEAT_CONFIRMED"

    def _verify_record(self, record, *, allow_pending=False):
        if record["dataset"] != DATASET:
            raise TradeContractError("unsupported capture dataset identity")
        if record["body_sha256"] != _body_digest(record):
            raise TradeContractError("immutable capture body digest mismatch")
        try:
            stored = json.loads(record["metadata_json"])
            envelope = CaptureEnvelope(**stored)
        except (TypeError, ValueError):
            raise TradeContractError("invalid persisted capture envelope") from None
        if canonical_json(asdict(envelope)) != record["metadata_json"]:
            raise TradeContractError("persisted envelope is not canonical or sanitized")
        if any(record[key] != getattr(envelope, key) for key in ("capture_id", "ticker", "trade_date")):
            raise TradeContractError("capture body disagrees with request envelope")
        now = utc_now()
        if utc_text(record["body_recorded_at"]) != record["body_recorded_at"]:
            raise TradeContractError("body timestamp is not canonical")
        if not envelope.response_at <= record["body_recorded_at"] <= now:
            raise TradeContractError("invalid body recording timestamp")
        marker = self._acceptance(record["capture_id"])
        if marker is None:
            if not allow_pending:
                raise TradeContractError("capture has no durable acceptance")
        elif (marker["body_sha256"] != record["body_sha256"]
              or utc_text(marker["durable_accepted_at"]) != marker["durable_accepted_at"]
              or not record["body_recorded_at"] <= marker["durable_accepted_at"] <= now):
            raise TradeContractError("immutable acceptance disagrees with committed body")
        if envelope.http_status == 404:
            if record["capture_state"] != "ABSENT_OBSERVED" or any(record[key] is not None for key in (
                "raw_response_sha256", "normalized_content_sha256", "content_json", "row_count",
                "schema_version", "schema_fingerprint", "source_row_count", "duplicate_counts_json", "content_length")):
                raise TradeContractError("absence must not claim a zero-trade tape")
            return envelope, None
        raw = self.raw_path(record["raw_response_sha256"])
        check_private_output(raw)
        if raw.is_symlink():
            raise TradeContractError("raw object cannot be a symbolic link")
        try:
            data = raw.read_bytes()
        except OSError:
            raise TradeContractError("raw object is missing or unreadable") from None
        if sha256_bytes(data) != record["raw_response_sha256"]:
            raise TradeContractError("raw sha256 mismatch")
        if len(data) != record["content_length"]:
            raise TradeContractError("raw content length mismatch")
        tape = normalize_parquet(data, envelope)
        if tape.normalized_content_sha256 != record["normalized_content_sha256"]:
            raise TradeContractError("normalized sha256 mismatch")
        if sha256_bytes(record["content_json"].encode("utf-8")) != record["normalized_content_sha256"]:
            raise TradeContractError("canonical normalized hash mismatch")
        checks = {
            "content_json": tape.content_json,
            "schema_fingerprint": tape.schema_fingerprint,
            "schema_version": tape.schema_version,
            "row_count": len(tape.rows),
            "source_row_count": tape.source_row_count,
            "duplicate_counts_json": canonical_json(tape.duplicate_counts),
        }
        if any(record[key] != value for key, value in checks.items()):
            raise TradeContractError("normalized body, schema, or provenance mismatch")
        totals = broker_totals(tape.rows)
        if (sum(row["buy_shares"] for row in totals.values()) != sum(row["sell_shares"] for row in totals.values())
            or sum(row["buy_value_rp"] for row in totals.values()) != sum(row["sell_value_rp"] for row in totals.values())
            or sum(row["buy_value_rp"] for row in totals.values()) != sum(row["value_rp"] for row in tape.rows)):
            raise TradeContractError("broker-day self-reconciliation failed")
        return envelope, tape

    def _verify_chain(self, capture_id, *, allow_pending=False):
        target = self._get_record(capture_id)
        rows = self.conn.execute(
            "SELECT * FROM trade_captures WHERE ticker=? AND trade_date=? AND capture_revision<=? ORDER BY capture_revision",
            (target["ticker"], target["trade_date"], target["capture_revision"])).fetchall()
        if not rows or rows[-1]["capture_id"] != capture_id:
            raise TradeContractError("invalid immutable capture revision")
        previous, last_content = None, None
        for index, row in enumerate(rows, start=1):
            record = dict(row)
            if record["capture_revision"] != index or record["previous_capture_id"] != (None if previous is None else previous["capture_id"]):
                raise TradeContractError("broken immutable capture version chain")
            envelope, tape = self._verify_record(
                record, allow_pending=allow_pending and record["capture_id"] == capture_id)
            expected_state = self._capture_state(envelope, record["raw_response_sha256"], previous, last_content)
            if record["capture_state"] != expected_state or expected_state not in CAPTURE_STATES:
                raise TradeContractError("capture state disagrees with version history")
            if previous:
                prior_marker = self._acceptance(previous["capture_id"])
                if prior_marker is None or record["body_recorded_at"] < prior_marker["durable_accepted_at"]:
                    raise TradeContractError("capture body precedes parent acceptance")
            previous = record
            if tape is not None:
                last_content = record
        return target

    def _metadata(self, record):
        envelope = json.loads(record["metadata_json"])
        result = {key: value for key, value in record.items()
                  if key not in ("metadata_json", "content_json", "duplicate_counts_json", "body_sha256")}
        result.update(envelope)
        marker = self._acceptance(record["capture_id"])
        result["durable_accepted_at"] = None if marker is None else marker["durable_accepted_at"]
        return result

    def verify(self, capture_id):
        self._check_paths()
        self._verify_schema()
        record = self._verify_chain(capture_id)
        return self._metadata(record)

    def read_rows(self, capture_id):
        self.verify(capture_id)
        record = self._get_record(capture_id)
        if record["content_json"] is None:
            raise TradeContractError("absence observation has no trade tape")
        return [dict(row, source_capture_id=capture_id) for row in json.loads(record["content_json"])["rows"]]

    def inspect(self, capture_id):
        metadata = self.verify(capture_id)
        keys = ("capture_id", "ticker", "trade_date", "schema_version", "row_count", "source_row_count",
                "capture_state", "capture_revision", "previous_capture_id", "requested_at", "response_at", "durable_accepted_at")
        result = {key: metadata[key] for key in keys}
        for key in ("raw_response_sha256", "normalized_content_sha256"):
            result[key + "_prefix"] = None if metadata[key] is None else metadata[key][:12]
        if metadata["capture_state"] == "ABSENT_OBSERVED":
            result["tape"] = None
        else:
            record = self._get_record(capture_id)
            summary = tape_summary(json.loads(record["content_json"])["rows"])
            result.update({key: value for key, value in summary.items()
                           if key not in ("fills_per_buy_order", "fills_per_sell_order")})
        return result


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes arbitrary unknown arguments, possibly tokens.
        self.print_usage()
        self.exit(2, "invalid command arguments; use --help\n")


def main(argv=None):
    parser = _SafeArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_SafeArgumentParser)
    ingest = sub.add_parser("ingest", help="ingest a local Parquet file")
    for name in ("ticker", "trade-date", "file", "requested-at", "response-at"):
        ingest.add_argument("--" + name, required=True)
    for name in ("last-modified", "creation-time", "request-id", "source-path", "capture-id"):
        ingest.add_argument("--" + name)
    ingest.add_argument("--created-by", default="local-ingest-v1")
    for command in ("verify", "inspect"):
        child = sub.add_parser(command, help=command + " an accepted capture offline")
        child.add_argument("--capture-id", required=True)
    for child in sub.choices.values():
        child.add_argument("--db", default=str(DEFAULT_DB))
        child.add_argument("--raw-root")
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            fields = dict(ticker=args.ticker, trade_date=args.trade_date,
                          requested_at=args.requested_at, response_at=args.response_at,
                          last_modified=args.last_modified, x_ms_creation_time=args.creation_time,
                          x_ms_request_id=args.request_id,
                          source_path_without_query_or_token=args.source_path or "",
                          created_by=args.created_by)
            if args.capture_id is not None:
                fields["capture_id"] = args.capture_id
            envelope = CaptureEnvelope(**fields)
            with TradeCaptureStore(args.db, args.raw_root) as store:
                result = store.ingest(args.file, envelope)
        else:
            with TradeCaptureStore(args.db, args.raw_root, read_only=True) as store:
                result = getattr(store, args.command)(args.capture_id)
        print(canonical_json(result))
        return 0
    except (ValueError, OSError, sqlite3.Error, RuntimeError):
        print("trade capture failed validation or storage integrity checks", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
