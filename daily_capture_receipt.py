"""Sanitized availability evidence for the legacy morning job.

The report sender deliberately tolerates collection failures. CI checks durable
capture ledgers separately, after saving whatever the run collected. This file
never serializes provider payloads, exception details, credentials, or values.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

SCHEMA = "spectra-daily-capture-receipt.v1"


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("UTC boundary requires timezone")
    return result.astimezone(timezone.utc)


def current_rows(conn, table, column, start, end):
    # Names are internal constants, never supplied by the CLI.
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")
            if start <= instant(r[column]) <= end]


def build_receipt(db, not_before, expected_codes, run_id, run_attempt, now=None):
    start = instant(not_before)
    end = now or datetime.now(timezone.utc)
    codes = sorted(set(expected_codes))
    if not codes or any(not re.fullmatch(r"[A-Z]{2}", c) for c in codes):
        raise ValueError("invalid expected broker configuration")
    receipt = dict(schema=SCHEMA, run_id=str(run_id), run_attempt=str(run_attempt),
                   not_before_utc=start.isoformat(), generated_utc=end.isoformat(),
                   complete=False, scope="configured_daily_sources_and_broker_codes",
                   sources={})
    sources = receipt["sources"]
    sources["market_summary"] = dict(status="MISSING_CURRENT_CAPTURE", complete=False)
    sources["broker_flow"] = dict(status="MISSING_CURRENT_CAPTURE", complete=False,
        expected_codes=codes, failed_codes=codes, brokers={c: {"status": "MISSING"} for c in codes})
    if start > end:
        receipt["error"] = "INVALID_RUN_BOUNDARY"
        return receipt
    try:
        with sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            try:
                captures = current_rows(conn, "ms_capture_manifest", "capture_started_utc", start, end)
                if captures:
                    row = max(captures, key=lambda r: instant(r["capture_started_utc"]))
                    complete = (row["contract_status"] == "OK" and
                                row["persisted_to_market_summary_daily"] == 1 and row["rows"] > 0 and
                                start <= instant(row["capture_finished_utc"]) <= end)
                    sources["market_summary"] = dict(status="PERSISTED" if complete else "REJECTED",
                        complete=complete, capture_date=row["capture_date"], rows=row["rows"],
                        pages_fetched=row["pages_fetched"],
                        capture_started_utc=row["capture_started_utc"])
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                sources["market_summary"]["status"] = "INVALID_CAPTURE_LEDGER"
            try:
                scans = current_rows(conn, "broker_flow_scan", "run_started_utc", start, end)
                if scans:
                    latest = max(instant(r["run_started_utc"]) for r in scans)
                    scans = [r for r in scans if instant(r["run_started_utc"]) == latest]
                    actual = {r["broker_code"] for r in scans}
                    brokers = {}
                    for code in codes:
                        found = [r for r in scans if r["broker_code"] == code]
                        if len(found) != 1:
                            brokers[code] = dict(status="MISSING_OR_DUPLICATE", complete=False)
                            continue
                        r = found[0]
                        good = (r["status"] == "OK" and r["snapshot"] == "PERSISTED" and
                                bool(r["expected_session_date"]) and
                                r["session_date"] == r["expected_session_date"] and
                                r["session_date"] < r["scrape_date"] and
                                bool(r["completed_utc"]) and start <= instant(r["completed_utc"]) <= end)
                        brokers[code] = dict(status="PERSISTED" if good else "REJECTED", complete=good,
                            source_status="OK" if r["status"] == "OK" else "SOURCE_FAILURE",
                            snapshot="PERSISTED" if r["snapshot"] == "PERSISTED" else "REJECTED",
                            session_date=r["session_date"], expected_session_date=r["expected_session_date"])
                    sessions = {r["session_date"] for r in scans}
                    dates = {r["scrape_date"] for r in scans}
                    failed = [c for c in codes if not brokers[c].get("complete")]
                    complete = not failed and actual == set(codes) and len(sessions) == len(dates) == 1
                    sources["broker_flow"] = dict(status="PERSISTED" if complete else "REJECTED",
                        complete=complete, run_started_utc=latest.isoformat(),
                        capture_date=next(iter(dates)) if len(dates) == 1 else None,
                        expected_codes=codes,
                        failed_codes=failed, unexpected_code_count=len(actual - set(codes)), brokers=brokers)
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                sources["broker_flow"]["status"] = "INVALID_CAPTURE_LEDGER"
    except sqlite3.Error:
        receipt["error"] = "DATABASE_UNAVAILABLE"
    if (all(s["complete"] for s in sources.values()) and
            sources["market_summary"]["capture_date"] != sources["broker_flow"]["capture_date"]):
        sources["broker_flow"].update(status="CAPTURE_DATE_MISMATCH", complete=False)
    receipt["complete"] = all(s["complete"] for s in sources.values())
    return receipt


def write_atomic(path, receipt):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_receipt(receipt, not_before, run_id, run_attempt):
    try:
        return (receipt["schema"] == SCHEMA and receipt["complete"] is True and
                receipt["run_id"] == str(run_id) and receipt["run_attempt"] == str(run_attempt) and
                instant(receipt["not_before_utc"]) == instant(not_before) and
                instant(receipt["generated_utc"]) >= instant(not_before) and
                all(receipt["sources"][s]["complete"] is True for s in ("market_summary", "broker_flow")))
    except (KeyError, TypeError, ValueError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="neobdm.db")
    parser.add_argument("--output", default="artifacts/daily-capture-receipt.json")
    parser.add_argument("--not-before", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        try:
            receipt = json.loads(Path(args.output).read_text())
            valid = check_receipt(receipt, args.not_before, args.run_id, args.run_attempt)
        except (OSError, ValueError):
            valid = False
        print("Daily capture availability: " + ("COMPLETE" if valid else "INCOMPLETE"))
        return 0 if valid else 1
    # Existing collector config is the single authority for the requested codes.
    # Import only in capture mode; the receipt/check functions use stdlib only.
    from neobdm_scraper import BROKER_FLOW_CAPTURE_CODES
    receipt = build_receipt(args.db, args.not_before, BROKER_FLOW_CAPTURE_CODES,
                            args.run_id, args.run_attempt)
    write_atomic(args.output, receipt)
    print("Sanitized daily capture receipt written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
