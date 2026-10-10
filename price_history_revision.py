"""Conservative OHLCV revision policy and an offline pre-commit delta check.

The inventory writer has no verified correction authorization mechanism.
Every change to an existing non-NULL observation is therefore refused. Source
representation, daily price limits and corporate-action references cannot grant
correction authority. NULL fills still need the writer's price admission checks.
"""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


OHLCV_FIELDS = ("open", "high", "low", "close", "volume")


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


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def snapshot_database(database, baseline):
    """Use SQLite backup so the baseline includes any committed WAL contents."""
    target = Path(baseline).resolve()
    if target == Path(database).resolve():
        raise ValueError("baseline must be separate from the candidate database")
    # Never replace a prior baseline: missing/invalid baselines must fail closed.
    with target.open("xb"):
        pass
    with closing(readonly(database)) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    target.chmod(0o444)


def verify_database_delta(baseline, database):
    """Reject deletions and non-NULL revisions, independent of duplicate counts.

NULL fills are checked for admission by the writer. This gate does not grant
repair authority or certify new observations. It detects bypass of the writer's
revision barrier before CI commits its candidate database.
"""
    if Path(baseline).resolve() == Path(database).resolve():
        raise ValueError("baseline must be separate from the candidate database")
    with closing(readonly(baseline)) as before, closing(readonly(database)) as after:
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
