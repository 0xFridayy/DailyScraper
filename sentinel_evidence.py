"""Read-only source evidence checks for Sentinel, independent of observed membership."""
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from idx_calendar import (IdxCalendarUnavailable, latest_idx_session_before, expected_closed_session,
                          CALENDAR_VERSION)
from ticker_registry import load_registry

OHLCV = ("open", "high", "low", "close", "volume")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_sessions(today, count=2, closed_session=None):
    current = expected_closed_session(today, closed_session)
    result = [current]
    for _ in range(count - 1):
        current = latest_idx_session_before(current)
        result.append(current)
    return result


def check_price_evidence(db_path, today, registry=None, closed_session=None):
    # closed_session: see idx_calendar.expected_closed_session; only the caller
    # knows whether this snapshot's producer stores a session the evening it closes.
    registry = registry or load_registry()
    item = dict(name="neobdm.db/price_history", severity="CRITICAL",
                status="UNKNOWN", asof=None, registry=registry.metadata(),
                clean_reporting_certification="NOT_CERTIFIED",
                history_completeness="NOT_CERTIFIED", calendar_version=CALENDAR_VERSION)
    path = Path(db_path)
    if not path.is_file():
        return dict(item, status="MISSING_DATABASE")
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        return dict(item, status="UNFROZEN_WAL_DATABASE")
    try:
        required = expected_sessions(today, closed_session=closed_session)
    except IdxCalendarUnavailable:
        return dict(item, status="CALENDAR_UNSUPPORTED")
    item["required_sessions"] = [d.isoformat() for d in required]
    before = file_sha256(path)
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            db.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "price_history" not in tables:
                return dict(item, status="MISSING_TABLE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(price_history)")}
            if "date" not in columns:
                return dict(item, status="MISSING_DATE_COLUMN")
            needed = {"date", "ticker", *OHLCV}
            if not needed <= columns:
                return dict(item, status="STRUCTURAL_DEFECT", missing_columns=sorted(needed - columns))
            latest = db.execute("SELECT MAX(date) FROM price_history").fetchone()[0]
            if not latest:
                return dict(item, status="EMPTY")
            try:
                last = date.fromisoformat(latest)
                if last.isoformat() != latest:
                    raise ValueError()
            except (ValueError, TypeError):
                return dict(item, status="INVALID_SOURCE_DATE")
            item["asof"] = latest
            if last > required[0]:
                return dict(item, status="FUTURE_SOURCE_DATE")
            quarantine = "price_quarantine" in tables
            if quarantine and not {"date", "ticker"} <= {
                    r[1] for r in db.execute("PRAGMA table_info(price_quarantine)")}:
                return dict(item, status="QUARANTINE_SCHEMA_INVALID")
            coverage, invalid, duplicates, all_missing = {}, [], [], set()
            for session in required:
                required_members = registry.required_for(session)
                raw = db.execute(
                    "SELECT ticker,open,high,low,close,volume FROM price_history WHERE date=?",
                    (session.isoformat(),)).fetchall()
                quarantined = {r[0] for r in db.execute(
                    "SELECT ticker FROM price_quarantine WHERE date=?", (session.isoformat(),))
                } if quarantine else set()
                accepted, seen = set(), set()
                for ticker, *values in raw:
                    if ticker in seen:
                        duplicates.append(dict(ticker=ticker, date=session.isoformat()))
                    seen.add(ticker)
                    if ticker not in required_members or ticker in quarantined:
                        continue
                    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                            or any(v <= 0 for v in values[:4]) or values[4] < 0
                            or values[1] < values[2]
                            or not values[2] <= values[0] <= values[1]
                            or not values[2] <= values[3] <= values[1]):
                        invalid.append(dict(ticker=ticker, date=session.isoformat()))
                    else:
                        accepted.add(ticker)
                missing = sorted(required_members - accepted)
                all_missing.update(missing)
                coverage[session.isoformat()] = dict(required=len(required_members),
                    raw_present=len(required_members & seen), accepted_present=len(accepted),
                    quarantined=len(required_members & quarantined), missing=missing)
            item.update(coverage=coverage, invalid_ohlcv=invalid, duplicate_keys=duplicates,
                        quarantine_applied=quarantine, snapshot_sha256=before,
                        missing_ticker_count=len(all_missing))
            if invalid or duplicates:
                return dict(item, status="INVALID_DATA")
            if all_missing:
                return dict(item, status="INCOMPLETE_COVERAGE")
            if not quarantine:
                return dict(item, status="QUARANTINE_NOT_CERTIFIED")
            # The real SIG1 gate admits the same immutable connection and full registry.
            try:
                from signal_integrity_health import report_admission
                from sig1_coverage_contract import UniverseContract
            except ImportError:
                return dict(item, status="REPORT_ADMISSION_NOT_INTEGRATED")
            universe = UniverseContract(registry.version, registry.source, registry.tickers)
            admission = report_admission(db, as_of=today, universe=universe, closed_session=closed_session)
            item["report_admission"] = {
                "admitted": admission.get("admitted", False),
                "rejection_reasons": admission.get("rejection_reasons", []),
                "snapshot_sha256": admission.get("snapshot_sha256")}
            if admission.get("snapshot_sha256") != before:
                return dict(item, status="DIFFERENT_REPORT_SNAPSHOT")
            if not admission.get("admitted"):
                return dict(item, status="REPORT_INPUT_REFUSED")
            item.update(status="CURRENT_REQUIRED_COVERAGE", severity="OK",
                        clean_reporting_certification="ADMITTED_BY_SIG1")
        finally:
            db.close()
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError, RuntimeError):
        return dict(item, status="SOURCE_EVIDENCE_UNAVAILABLE")
    if file_sha256(path) != before:
        return dict(item, status="SOURCE_CHANGED_DURING_CHECK", severity="CRITICAL",
                    clean_reporting_certification="NOT_CERTIFIED")
    return item


def check_receipt(path, registry, expected_date, artifact_root=None):
    """A declared successful upload is not proof until retrieved bytes are hashed."""
    item = dict(name=Path(path).name, severity="CRITICAL", status="NOT_CERTIFIED",
                expected_registry=registry.sha256, expected_date=str(expected_date))
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(item, status="MISSING_OR_INVALID_RECEIPT")
    if receipt.get("schema") != "spectra-collector-receipt.v1" or not isinstance(receipt.get("source"), str):
        return dict(item, status="INVALID_RECEIPT_SCHEMA")
    try:
        captured_at = datetime.fromisoformat(receipt["captured_at"].replace("Z", "+00:00"))
        if captured_at.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        return dict(item, status="INVALID_CAPTURE_TIMESTAMP")
    item.update(source=receipt.get("source"), acquisition_status=receipt.get("acquisition_status"),
                processing_status=receipt.get("processing_status"),
                coverage_scope=receipt.get("coverage_scope"),
                full_broker_universe=receipt.get("full_broker_universe"))
    if receipt.get("registry_sha256") != registry.sha256:
        return dict(item, status="REGISTRY_MISMATCH")
    if receipt.get("trade_date") != str(expected_date):
        return dict(item, status="STALE_RECEIPT")
    members = registry.required_for(expected_date)
    per_ticker = receipt.get("per_ticker")
    if not isinstance(per_ticker, dict):
        return dict(item, status="MISSING_TICKER_RECEIPTS")
    missing = sorted(t for t in members if not isinstance(per_ticker.get(t), dict)
                     or per_ticker[t].get("state") != "COMPLETE"
                     or per_ticker[t].get("last_session") != str(expected_date))
    item.update(required=len(members), complete=len(members) - len(missing), missing=missing)
    if missing:
        return dict(item, status="INCOMPLETE_COVERAGE")
    if receipt.get("acquisition_status") != "COMPLETE":
        return dict(item, status="ACQUISITION_NOT_COMPLETE")
    if receipt.get("processing_status") != "COMPLETE":
        return dict(item, status="PROCESSING_NOT_COMPLETE")
    if receipt.get("source_certification") not in {"VERIFIED", "VERIFIED_REQUESTED_SCOPE"}:
        return dict(item, status="SOURCE_NOT_CERTIFIED")
    persistence = receipt.get("persistence", {})
    if persistence.get("status") != "PERSISTED" or persistence.get("retrieval_status") != "VERIFIED":
        return dict(item, status="PRIVATE_PERSISTENCE_NOT_VERIFIED")
    artifacts = receipt.get("artifacts")
    if artifact_root is None or not isinstance(artifacts, list) or not artifacts:
        return dict(item, status="RETRIEVED_ARTIFACTS_NOT_AVAILABLE")
    root = Path(artifact_root).resolve()
    try:
        for artifact in artifacts:
            candidate = (root / artifact["path"]).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                return dict(item, status="ARTIFACT_PATH_UNAVAILABLE")
            if file_sha256(candidate) != artifact["sha256"]:
                return dict(item, status="ARTIFACT_HASH_MISMATCH")
    except (OSError, TypeError, KeyError):
        return dict(item, status="INVALID_ARTIFACT_EVIDENCE")
    return dict(item, status="VERIFIED_REQUESTED_SCOPE_ONLY", severity="OK",
                history_completeness=receipt.get("history_completeness", "NOT_CERTIFIED"))
