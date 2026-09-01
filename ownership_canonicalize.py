"""Run the deterministic entity canonicalization backfill.

    py -3 ownership_canonicalize.py [--db neobdm_ownership.db] [--dry-run]

Idempotent: rerunning with unchanged inputs writes no new rows and never
changes an existing entity_id. Populates no ML feature, maps no owner to a
broker, and leaves investor_name_raw byte-identical everywhere.
"""
import argparse
import datetime as dt
import sqlite3

import ownership_entity as oe
from ownership_schema import create_schema


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="neobdm_ownership.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and report, then roll back")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(args.db)
    create_schema(conn)

    if args.dry_run:
        conn.execute("BEGIN")
    result = oe.backfill(conn, now)
    summary = oe.summarize(conn)
    cross = oe.cross_ticker_entities(conn)

    print(f"raw names          : {summary['raw_names']}")
    print(f"canonical entities : {summary['canonical_entities']}")
    print("aliases by rule    :")
    for rule in ("R0", "R1", "R2", "R3", "R4"):
        print(f"    {rule}: {summary['by_rule'][rule]}")
    print(f"unresolved (review): {summary['review']}")
    print(f"cross-ticker ents  : {len(cross)}")
    for entity_id, name, n in cross:
        print(f"    {entity_id}  {n} tickers  {name}")

    if args.dry_run:
        conn.rollback()
        print("\n[dry-run] rolled back, nothing written")
    return result


if __name__ == "__main__":
    main()
