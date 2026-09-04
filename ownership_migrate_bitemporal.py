"""One-shot, idempotent seed of the bitemporal layer (capture_run,
ownership_observation, observation_state, capture_payload) from the three
pre-remediation capture runs (2026-08-31, 2026-09-01, 2026-09-03).

Why this exists: those three runs predate the ingest fix and were written
under first-write-wins INSERT OR IGNORE, so any revision within them is
already unrecoverable. This script does not try to recover it. It seeds
whatever CAN be honestly known, tagged evidence_class='LEGACY_PARTIAL', and
leaves the strict 60-trading-day clock to start counting from zero on the
first run this script does not need to touch -- the first run made through
the remediated ingest_capture().

Two fidelity levels, chosen automatically per (run_id, ticker):

  REPLAYED   ownership_raw/<run_id>/<TICKER>.json exists on disk. Its raw pane
             text is fed straight back through ingest_capture() with the
             run's own original captured_at and evidence_class='LEGACY_PARTIAL'.
             This reuses the EXACT SAME field-construction code the live path
             uses, so there is no risk of the migration's payload shape
             drifting from what a future live capture of the same key would
             hash to. The eight legacy tables already hold these rows, so
             INSERT OR IGNORE no-ops on all of them (0 new legacy rows) --
             only the four new tables gain data. Currently true for
             2026-08-31 only; 45/45 tickers.

  BACKFILLED No raw file survives (2026-09-01, 2026-09-03: CI committed only
             the DB, per the .gitignore policy in force at the time). The
             legacy tables' own already-parsed columns are read back directly
             and turned into observations with the SAME payload field sets
             ingest_capture() uses per source_family/threshold. capture_run
             rows get status='legacy_reconstructed', pane_hash=NULL,
             row_count=NULL -- row_count is deliberately left unknown rather
             than set to "however many legacy rows happen to have this
             captured_at", because that count is itself a lower bound: any
             row that collided with an already-seen key from 2026-08-31 was
             silently dropped by the legacy ingest and is invisible even to
             this script.

Usage:
    python ownership_migrate_bitemporal.py --db neobdm_ownership.db          # report only
    python ownership_migrate_bitemporal.py --db neobdm_ownership.db --apply  # write + commit

Safe to re-run: capture_run is INSERT OR REPLACE keyed on (run_id, ticker,
pane), and record_observation() is idempotent by construction (unchanged
payload_hash -> no new row). Nothing in the eight pre-existing tables is ever
written, updated, or deleted by this script.
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ownership_ingest as ing  # noqa: E402
from ownership_schema import connect  # noqa: E402

RAW_DIR = os.path.join(HERE, "ownership_raw")
EVIDENCE_CLASS = "LEGACY_PARTIAL"

# (legacy_table, threshold_filter_or_None, source_family_column, key_cols,
#  payload_cols) -- payload_cols mirrors EXACTLY the field set ingest_capture()
# uses for that source, so a payload_hash computed here is comparable to one a
# future live capture of the same business_key would produce.
BACKFILL_SPECS = [
    dict(
        table="ownership_snapshot", threshold="1pct",
        source_family="stock_detail_kda1_current",
        key_cols=["ticker", lambda r: "1pct", "snapshot_date", "investor_name_raw"],
        business_key_kind="ownership_snapshot",
        payload_cols=["investor_category", "is_foreign", "ownership_pct_raw",
                       "ownership_pct", "scrip_lot", "scrip_pct", "scrip_raw",
                       "scripless_lot", "scripless_pct", "scripless_raw"],
    ),
    dict(
        table="ownership_snapshot", threshold="5pct",
        source_family="stock_detail_kda5_current",
        business_key_kind="ownership_snapshot",
        key_cols=["ticker", lambda r: "5pct", "snapshot_date", "investor_name_raw"],
        payload_cols=["is_foreign", "ownership_pct"],
    ),
    dict(
        table="ownership_change", threshold="1pct",
        source_family="stock_detail_pkda1_moves",
        business_key_kind="ownership_change",
        key_cols=["ticker", lambda r: "1pct", "change_date", "investor_name_raw", "row_ordinal"],
        payload_cols=["investor_category", "is_foreign", "resulting_ownership_pct_raw",
                       "resulting_ownership_pct", "scrip_lot_change",
                       "scripless_lot_change", "note"],
    ),
    dict(
        table="ownership_change", threshold="5pct",
        source_family="stock_detail_pkda5_moves",
        business_key_kind="ownership_change",
        key_cols=["ticker", lambda r: "5pct", "change_date", "investor_name_raw", "row_ordinal"],
        payload_cols=["is_foreign", "lot_change", "is_custodian_move", "custodian_or_code"],
    ),
    dict(
        table="custody_breakdown_snapshot", threshold=None,
        source_family="stock_detail_kda5_current",
        business_key_kind="custody_breakdown_snapshot",
        key_cols=["ticker", "snapshot_date", "investor_name_raw", "row_ordinal"],
        payload_cols=["investor_total_pct", "is_foreign", "custodian_label",
                       "custodian_pct_of_holder"],
    ),
    dict(
        table="balance_position_monthly", threshold=None,
        source_family="balance_position_chart",
        business_key_kind="balance_position_monthly",
        key_cols=["ticker", "period_date", "category"],
        payload_cols=["lots"],
    ),
    dict(
        table="balance_position_summary_monthly", threshold=None,
        source_family="balance_position_chart",
        business_key_kind="balance_position_summary_monthly",
        key_cols=["ticker", "period_date"],
        payload_cols=["pct_retail", "pct_institusi", "pct_foreign", "pct_scripless"],
    ),
    dict(
        table="float_holder_snapshot", threshold=None,
        source_family="float_holder_badge",
        business_key_kind="float_holder_snapshot",
        key_cols=["ticker", "snapshot_date"],
        payload_cols=["free_float_pct", "scripless_pct", "holder_count_raw"],
    ),
]

PANE_FOR_TABLE = {
    ("ownership_snapshot", "1pct"): "insider-current",
    ("ownership_snapshot", "5pct"): "insider5p-current",
    ("ownership_change", "1pct"): "insider-moves",
    ("ownership_change", "5pct"): "insider5p-moves",
    ("custody_breakdown_snapshot", None): "insider5p-current",
    ("balance_position_monthly", None): "balance_position",
    ("balance_position_summary_monthly", None): "balance_position",
    ("float_holder_snapshot", None): "badge",
}


def discover_run_ids(conn):
    """Every distinct captured_at DATE across all seven legacy tables."""
    tables = ["ownership_snapshot", "ownership_change", "custody_breakdown_snapshot",
              "balance_position_monthly", "balance_position_summary_monthly",
              "float_holder_snapshot"]
    run_ids = set()
    for t in tables:
        for (d,) in conn.execute(f"SELECT DISTINCT substr(captured_at,1,10) FROM {t}"):
            if d:
                run_ids.add(d)
    return sorted(run_ids)


def replayable_tickers(run_id):
    """Tickers for which ownership_raw/<run_id>/<TICKER>.json exists on disk."""
    d = os.path.join(RAW_DIR, run_id)
    if not os.path.isdir(d):
        return set()
    return {fn[:-5] for fn in os.listdir(d) if fn.endswith(".json")}


def replay_run(conn, run_id, ticker, stats):
    path = os.path.join(RAW_DIR, run_id, f"{ticker}.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    captured_at = raw["captured_at"]
    capture = raw["capture"]
    counts = ing.ingest_capture(
        conn, ticker, capture, captured_at,
        f"https://neobdm.tech/stock_detail/{ticker}/",
        run_id=run_id, evidence_class=EVIDENCE_CLASS,
    )
    stats["replayed_tickers"] += 1
    stats["new_observations"] += counts["ownership_observation"]
    stats["revisions_found"] += counts["ownership_revision"]
    return counts


def backfill_run(conn, run_id, ticker, stats):
    """Non-replayable path: read structured columns straight back out of the
    legacy tables and turn them into observations + a best-effort manifest."""
    panes_touched = {}
    captured_at_seen = None
    for spec in BACKFILL_SPECS:
        table = spec["table"]
        where = "ticker = ? AND substr(captured_at,1,10) = ?"
        params = [ticker, run_id]
        if spec["threshold"] is not None:
            where += " AND threshold = ?"
            params.append(spec["threshold"])
        cols = ["captured_at", "source_family"] + [
            c for c in spec["key_cols"] if isinstance(c, str)
        ] + spec["payload_cols"]
        # de-dup while preserving order (key_cols may overlap payload_cols in
        # theory; not currently, but keep the query honest either way)
        seen = set()
        cols = [c for c in cols if not (c in seen or seen.add(c))]
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} WHERE {where}", params,
        ).fetchall()
        if not rows:
            continue
        row_count = 0
        for r in rows:
            rec = dict(zip(cols, r))
            key_parts = [
                (rec[c] if isinstance(c, str) else c(rec)) for c in spec["key_cols"]
            ]
            key = ing.business_key(spec["business_key_kind"], ticker, *key_parts)
            payload = {c: rec[c] for c in spec["payload_cols"]}
            outcome = ing.record_observation(
                conn, run_id, ticker, spec["source_family"], key, payload,
                rec["captured_at"], EVIDENCE_CLASS,
            )
            if outcome != "unchanged":
                stats["new_observations"] += 1
                if outcome == "revised":
                    stats["revisions_found"] += 1
            row_count += 1
            captured_at_seen = rec["captured_at"]
        pane = PANE_FOR_TABLE[(table, spec["threshold"])]
        panes_touched[pane] = panes_touched.get(pane, 0) + row_count

    if not panes_touched:
        return
    captured_at = captured_at_seen or f"{run_id}T00:00:00Z"
    for pane, row_count in panes_touched.items():
        ing.record_capture_run(
            conn, run_id, ticker, pane, captured_at, "legacy_reconstructed",
            row_count=None,  # a lower bound at best -- see module docstring
            evidence_class=EVIDENCE_CLASS,
        )
    stats["backfilled_tickers"] += 1


def migrate(conn, apply_):
    run_ids = discover_run_ids(conn)
    print(f"Discovered {len(run_ids)} legacy run date(s): {run_ids}")

    stats = dict(replayed_tickers=0, backfilled_tickers=0,
                 new_observations=0, revisions_found=0)

    legacy_tables = ["ownership_snapshot", "ownership_change", "custody_breakdown_snapshot",
                      "balance_position_monthly", "balance_position_summary_monthly",
                      "float_holder_snapshot"]

    for run_id in run_ids:
        replayable = replayable_tickers(run_id)
        # Tickers present in this run at all -- must be the union across EVERY
        # legacy table, not just ownership_snapshot/ownership_change. A ticker
        # can have a fresh balance_position_monthly capture on a day its
        # KDA/PKDA panes happened to be empty or unchanged (7 such tickers
        # exist on 2026-09-01), and scoping to only two tables silently drops
        # that ticker's rows for the run entirely.
        all_tickers = set()
        for t in legacy_tables:
            all_tickers |= {r[0] for r in conn.execute(
                f"SELECT DISTINCT ticker FROM {t} WHERE substr(captured_at,1,10) = ?",
                (run_id,),
            )}
        print(f"\nrun_id={run_id}: {len(all_tickers)} ticker(s), "
              f"{len(replayable & all_tickers)} replayable from ownership_raw/")

        for ticker in sorted(all_tickers):
            if ticker in replayable:
                replay_run(conn, run_id, ticker, stats)
            else:
                backfill_run(conn, run_id, ticker, stats)

    print(f"\n{'APPLIED' if apply_ else 'DRY-RUN (no commit)'}")
    print(f"  tickers replayed (full fidelity)   : {stats['replayed_tickers']}")
    print(f"  tickers backfilled (partial)        : {stats['backfilled_tickers']}")
    print(f"  new observation rows written         : {stats['new_observations']}")
    print(f"  of which genuine revisions detected  : {stats['revisions_found']}")

    n_obs = conn.execute("SELECT COUNT(*) FROM ownership_observation").fetchone()[0]
    n_state = conn.execute("SELECT COUNT(*) FROM observation_state").fetchone()[0]
    n_runs = conn.execute("SELECT COUNT(*) FROM capture_run").fetchone()[0]
    n_frag = conn.execute("SELECT COUNT(*) FROM capture_payload").fetchone()[0]
    n_strict = conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM capture_run "
        "WHERE evidence_class='STRICT' AND status='ok'"
    ).fetchone()[0]
    print(f"  ownership_observation total rows     : {n_obs}")
    print(f"  observation_state total keys         : {n_state}")
    print(f"  capture_run total rows               : {n_runs}")
    print(f"  capture_payload (retained fragments) : {n_frag}")
    print(f"  strict-clock trading days so far     : {n_strict}  "
          f"(0 is correct immediately after this migration)")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "neobdm_ownership.db"))
    ap.add_argument("--apply", action="store_true",
                     help="commit the migration; default is a dry-run report only")
    a = ap.parse_args()

    conn = connect(a.db)  # create_schema() runs here; additive, no-op if already applied
    conn.execute("BEGIN")
    try:
        migrate(conn, a.apply)
    finally:
        if a.apply:
            conn.commit()
            print("\nCommitted.")
        else:
            conn.rollback()
            print("\nRolled back (dry-run). Pass --apply to write.")
        conn.close()


if __name__ == "__main__":
    main()
