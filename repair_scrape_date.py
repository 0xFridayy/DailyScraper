"""
Restore date-keyed rows that an off-window scrape overwrote.

WHY THIS EXISTS
---------------
market_summary_daily and konglo_signal_watch are keyed by the SCRAPE date, and
the whole pipeline reads that as meaning the PREVIOUS session (HANDOFF Appendix
E). That only holds when the scrape runs before IDX opens. Run it after the
close and the screener serves TODAY's close, which INSERT OR REPLACE then writes
over the morning's correct rows for the same date.

That happened on 2026-08-27: a manual workflow_dispatch at 21:09 WIB replaced
204 correctly-offset rows with 205 same-day ones, and check_signal_integrity's
cross-source agreement fell from 100% to 85% (Appendix Q). price_history was
untouched — it is keyed by real trading dates from the inventory API, not by
the scrape date, which is exactly why the two paths disagreed loudly enough to
notice.

neobdm_scraper._offset_safe() now refuses those writes off-window, so this
should not recur. This tool repairs damage already committed: the correct rows
are still in git, in the commit before the bad run.

USAGE
-----
    git show <good-commit>:neobdm.db > /tmp/good.db
    py repair_scrape_date.py 2026-08-27 /tmp/good.db          # report only
    py repair_scrape_date.py 2026-08-27 /tmp/good.db --apply  # write

Idempotent: re-running after --apply reports nothing left to change.
"""

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")

# Keyed by scrape date, so an off-window run corrupts them. broker_flow and
# price_history are deliberately NOT here: both carry real trading dates.
TABLES = ["market_summary_daily", "konglo_signal_watch"]
DATE_COL = {"market_summary_daily": "date", "konglo_signal_watch": "flag_date"}


def _rows(conn, table, date_str):
    col = DATE_COL[table]
    try:
        cur = conn.execute(f"SELECT * FROM {table} WHERE {col}=?", (date_str,))
    except sqlite3.OperationalError:
        return None, []
    return [d[0] for d in cur.description], cur.fetchall()


def repair(date_str, good_path, apply=False):
    if not os.path.exists(good_path):
        raise SystemExit(f"source DB not found: {good_path}")
    good = sqlite3.connect(good_path)
    live = sqlite3.connect(DB_PATH)
    changed = 0
    try:
        for table in TABLES:
            cols, want = _rows(good, table, date_str)
            _, have = _rows(live, table, date_str)
            if cols is None:
                print(f"{table}: not present in the source DB - skipped")
                continue
            print(f"{table}: live has {len(have)} row(s), source has {len(want)}")
            if not want:
                print(f"  source has nothing for {date_str} - refusing to blank the table")
                continue
            if have == want:
                print("  already matches - nothing to do")
                continue
            changed += len(want)
            if not apply:
                continue
            col = DATE_COL[table]
            live.execute(f"DELETE FROM {table} WHERE {col}=?", (date_str,))
            ph = ",".join("?" * len(cols))
            quoted = ",".join(f'"{c}"' for c in cols)
            live.executemany(f"INSERT INTO {table} ({quoted}) VALUES ({ph})", want)
            print(f"  restored {len(want)} row(s)")
        if apply and changed:
            live.commit()
    finally:
        good.close()
        live.close()

    if not changed:
        print("\nNothing to change.")
    elif apply:
        print(f"\nRestored {changed} row(s) for {date_str}.")
        print("Re-run: py check_signal_integrity.py   to confirm the paths agree again.")
    else:
        print(f"\n{changed} row(s) WOULD be restored. Re-run with --apply to write.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if len(args) != 2:
        raise SystemExit(__doc__)
    repair(args[0], args[1], apply="--apply" in sys.argv)
