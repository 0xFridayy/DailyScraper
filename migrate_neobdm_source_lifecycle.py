"""
One-off, idempotent, ADDITIVE migration for the 2026-09-14 NeoBDM source-contract
incident (is_unusual_volume retired).

What it does:
  * creates source_field_lifecycle / ms_raw_response / ms_capture_manifest /
    signal_source_status if absent
  * mirrors neobdm_source_contract.FIELD_LIFECYCLE (is_unusual_volume RETIRED,
    effective capture regime 2026-09-14, with the live-probe evidence)
  * annotates top_akum_bandar as RETIRED_SOURCE on flag dates from 2026-09-14
    (metadata only), keeping status_at_capture = SOURCE_UNAVAILABLE where the
    then-active contract requested the field and got none: the retirement explains
    those captures, it does not rewrite them as healthy
  * never creates a capture manifest or raw record for a historical capture

What it never does: change, delete or backfill a market_summary_daily or
konglo_signal_watch row. Both tables are fingerprinted before and after and the
migration rolls back if either fingerprint moves.

The scraper applies the same idempotent steps on every capture, so production
converges without committing a hand-migrated neobdm.db (which the daily cron
also rewrites -- two binary writers would conflict).

Run:  py migrate_neobdm_source_lifecycle.py [--db PATH]            dry run
      py migrate_neobdm_source_lifecycle.py [--db PATH] --apply    write
"""

import argparse
import hashlib
import os
import sqlite3
import sys

import neobdm_source_contract as nsc

HERE = os.path.dirname(os.path.abspath(__file__))
PROTECTED = ("market_summary_daily", "konglo_signal_watch")


def fingerprint(conn, table):
    if not nsc.table_exists(conn, table):
        return None
    h = hashlib.sha256()
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    h.update("|".join(cols).encode())
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):
        h.update(repr(row).encode("utf-8"))
    return h.hexdigest()


def unusual_volume_profile(conn):
    if not nsc.table_exists(conn, "market_summary_daily"):
        return {}
    return {d: (n, t, f, nulls) for d, n, t, f, nulls in conn.execute(
        "SELECT date, COUNT(*), SUM(is_unusual_volume = 1), SUM(is_unusual_volume = 0), "
        "SUM(is_unusual_volume IS NULL) FROM market_summary_daily GROUP BY date ORDER BY date")}


def migrate(conn, apply=False):
    before = {t: fingerprint(conn, t) for t in PROTECTED}
    profile_before = unusual_volume_profile(conn)
    conn.execute("SAVEPOINT lifecycle_migration")
    try:
        nsc.ensure_schema(conn, commit=False)
        added = nsc.annotate_retired_signal_days(conn, commit=False)
        lifecycle = conn.execute(
            "SELECT field, state, effective_capture_date FROM source_field_lifecycle ORDER BY field").fetchall()
        after = {t: fingerprint(conn, t) for t in PROTECTED}
        if after != before or unusual_volume_profile(conn) != profile_before:
            raise RuntimeError("protected table changed during migration — rolled back")
    except Exception:
        conn.execute("ROLLBACK TO lifecycle_migration")
        conn.execute("RELEASE lifecycle_migration")
        raise
    if apply:
        conn.execute("RELEASE lifecycle_migration")
        conn.commit()
    else:
        conn.execute("ROLLBACK TO lifecycle_migration")
        conn.execute("RELEASE lifecycle_migration")
    return {"applied": apply, "lifecycle": lifecycle, "retired_signal_days_added": added,
            "protected_fingerprints": before, "unusual_volume_by_date": profile_before}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", default=os.path.join(HERE, "neobdm.db"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        conn = sqlite3.connect(args.db, isolation_level=None)
    else:
        # Dry run never opens the file read-write: migrate an in-memory copy.
        source = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        conn = sqlite3.connect(":memory:", isolation_level=None)
        source.backup(conn)
        source.close()
    try:
        report = migrate(conn, apply=True)   # the dry run commits only into memory
        report["applied"] = args.apply
    finally:
        conn.close()
    print(f"{'APPLIED' if report['applied'] else 'DRY RUN (nothing written)'}: {args.db}")
    print("lifecycle:", report["lifecycle"])
    print("top_akum_bandar RETIRED_SOURCE annotations:", report["retired_signal_days_added"])
    print("protected fingerprints (unchanged):", report["protected_fingerprints"])
    recent = sorted(report["unusual_volume_by_date"].items())[-4:]
    print("is_unusual_volume per date (rows, true, false, null) — last 4, unchanged:", recent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
