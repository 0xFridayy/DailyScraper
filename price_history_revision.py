"""Conservative financial revision policies and an offline pre-commit delta check.

The inventory writer has no verified correction authorization mechanism.
Every change to an existing non-NULL observation is therefore refused. Source
representation, daily price limits and corporate-action references cannot grant
correction authority. NULL fills still need the writer's price admission checks.
Broker NULL fills have no verified repair contract and are refused independently.
"""

import argparse
from contextlib import closing
import json
from hashlib import file_digest
from pathlib import Path
import sqlite3


OHLCV_FIELDS = ("open", "high", "low", "close", "volume")
BROKER_FIELDS = ("bval", "sval", "netval", "bavg", "savg")


def same_value(stored, proposed):
    """Compare the numeric REAL representation used by the price contract."""
    if stored is None or proposed is None:
        return stored is None and proposed is None
    try:
        return float(stored) == float(proposed)
    except (OverflowError, TypeError, ValueError):
        return False


def revision_changes(stored, proposed):
    """Return only financial evidence, never source metadata or authentication."""
    return {field: {"stored": stored[field], "proposed": proposed.get(field)}
            for field in OHLCV_FIELDS
            if stored[field] is not None and not same_value(stored[field], proposed.get(field))}


def broker_revision_changes(stored, proposed):
    """No broker correction or missing-field repair authority is implemented."""
    return {field: {"stored": stored[field], "proposed": proposed.get(field)}
            for field in BROKER_FIELDS
            if not same_value(stored[field], proposed.get(field))}


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _baseline_receipt(baseline):
    return Path(str(Path(baseline).resolve()) + ".json")


def _sha256(path):
    with Path(path).open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def _validate_schema(conn):
    """Malformed/missing tables cannot make even an empty baseline pass."""
    if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("invalid financial database")
    for table, keys, fields in (
        ("price_history", ("date", "ticker"), OHLCV_FIELDS),
        ("broker_flow", ("date", "ticker", "broker_code"), BROKER_FIELDS),
    ):
        columns = ",".join(keys + fields)
        conn.execute(f"SELECT {columns} FROM {table} LIMIT 0")
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        primary_key = tuple(r[1] for r in sorted(info, key=lambda r: r[5]) if r[5])
        if primary_key != keys:
            raise ValueError(f"invalid {table} observation key")
        null_key = " OR ".join(f"{key} IS NULL" for key in keys)
        if conn.execute(f"SELECT 1 FROM {table} WHERE {null_key} LIMIT 1").fetchone():
            raise ValueError(f"invalid {table} NULL observation key")


def snapshot_database(database, baseline):
    """Use SQLite backup so the baseline includes any committed WAL contents."""
    target = Path(baseline).resolve()
    if target == Path(database).resolve():
        raise ValueError("baseline must be separate from the candidate database")
    # Never replace a prior baseline: missing/invalid baselines must fail closed.
    with target.open("xb"):
        pass
    with closing(readonly(database)) as source, closing(sqlite3.connect(target)) as destination:
        _validate_schema(source)
        source.backup(destination)
    # Bind the immutable baseline to this candidate and its exact snapshot.
    # This detects accidental substitution/tampering, not malicious forgery of
    # both local files. The workflow must create them before invoking the writer.
    receipt = {"version": 1, "database": str(Path(database).resolve()), "sha256": _sha256(target)}
    with _baseline_receipt(target).open("x", encoding="utf-8") as output:
        json.dump(receipt, output, sort_keys=True)
    _baseline_receipt(target).chmod(0o444)
    target.chmod(0o444)


def verify_database_delta(baseline, database):
    """Reject financial revisions independently of writer and duplicate counts.

NULL fills are checked for admission by the writer. This gate does not grant
repair authority or certify new observations. It detects bypass of the writer's
revision barrier before CI commits its candidate database.
All broker changes, including NULL fills, require authority absent from this path.
"""
    if Path(baseline).resolve() == Path(database).resolve():
        raise ValueError("baseline must be separate from the candidate database")
    receipt = json.loads(_baseline_receipt(baseline).read_text(encoding="utf-8"))
    if (not isinstance(receipt, dict) or receipt.get("version") != 1
            or receipt.get("database") != str(Path(database).resolve())
            or receipt.get("sha256") != _sha256(baseline)):
        raise ValueError("wrong or altered historical baseline")
    with closing(readonly(baseline)) as before, closing(readonly(database)) as after:
        _validate_schema(before)
        _validate_schema(after)
        after.execute("ATTACH DATABASE ? AS baseline", (Path(baseline).resolve().as_uri() + "?mode=ro",))
        # Fail on malformed/missing schema even for an empty baseline.
        before.execute("SELECT date,ticker,open,high,low,close,volume FROM price_history LIMIT 0")
        after.execute("SELECT date,ticker,open,high,low,close,volume FROM price_history LIMIT 0")
        reasons = " OR ".join(f"(b.{f} IS NOT NULL AND b.{f} IS NOT a.{f})" for f in OHLCV_FIELDS)
        rows = after.execute(f"""
            SELECT b.date,b.ticker,b.open,b.high,b.low,b.close,b.volume,
                   a.date,a.open,a.high,a.low,a.close,a.volume
            FROM baseline.price_history b LEFT JOIN main.price_history a
              ON a.date=b.date AND a.ticker=b.ticker
            WHERE a.date IS NULL OR {reasons}
            ORDER BY b.ticker,b.date
        """)
        refusals = []
        for row in rows:
            stored = dict(zip(OHLCV_FIELDS, row[2:7]))
            proposed = dict(zip(OHLCV_FIELDS, row[8:13]))
            refusals.append({"session": row[0], "ticker": row[1],
                             "reason": "DELETED_OBSERVATION" if row[7] is None else "UNAUTHORIZED_HISTORICAL_REVISION",
                             "changes": revision_changes(stored, proposed)})
        # Independent SQL over the full broker key, including NULL transitions.
        # Do not condition this on prices, price admission or writer success.
        broker_reasons = " OR ".join(f"b.{field} IS NOT a.{field}" for field in BROKER_FIELDS)
        broker_rows = after.execute(f"""
            SELECT b.date,b.ticker,b.broker_code,b.bval,b.sval,b.netval,b.bavg,b.savg,
                   a.date,a.bval,a.sval,a.netval,a.bavg,a.savg
            FROM baseline.broker_flow b LEFT JOIN main.broker_flow a
              ON a.date=b.date AND a.ticker=b.ticker AND a.broker_code=b.broker_code
            WHERE a.date IS NULL OR {broker_reasons}
            ORDER BY b.ticker,b.date,b.broker_code
        """)
        for row in broker_rows:
            stored = dict(zip(BROKER_FIELDS, row[3:8]))
            proposed = dict(zip(BROKER_FIELDS, row[9:14]))
            changes = broker_revision_changes(stored, proposed)
            reason = ("DELETED_OBSERVATION" if row[8] is None else
                      "UNVERIFIED_BROKER_NULL_REPAIR" if all(
                          value["stored"] is None for value in changes.values()) else
                      "UNAUTHORIZED_HISTORICAL_REVISION")
            refusals.append({"table": "broker_flow", "session": row[0], "ticker": row[1],
                             "broker_code": row[2], "reason": reason, "changes": changes})
        return refusals


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=("snapshot", "verify"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            snapshot_database(args.database, args.baseline)
            return 0
        refusals = verify_database_delta(args.baseline, args.database)
        print(json.dumps({"historical_revision_gate": "FAIL" if refusals else "PASS",
                          "refusals": refusals}, sort_keys=True))
        return 1 if refusals else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"historical_revision_gate": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
