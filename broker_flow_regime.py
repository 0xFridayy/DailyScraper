"""Read-only evidence manifest for the historical broker_flow table.

broker_flow.date is NOT one convention (HANDOFF Appendix R, Appendix S):

  BACKFILL  bval IS NULL      date = the /api/inventory session date
                              (backfill_inventory.insert_inventory stores
                              data.date as-is, days <= BACKFILL_END only)
  LIVE      bval IS NOT NULL  date = MYT scrape date. Before PR #72 there was
                              no window gate, so a row can hold the previous
                              session, the same day's session, or a mixture.

This module never changes a row. It reads neobdm.db immutable/read-only and
writes one JSON document that says, for every (broker_flow.date, regime),
what session that date is PROVEN to hold, and on what evidence:

  date_class              evidence level  canonical_session_date
  SCAN_VERIFIED           PROVEN          broker_flow_scan.session_date
  SOURCE_DATED_BACKFILL   PROVEN          broker_flow.date
  CONTENT_MATCHED         PROVEN          the one session in broker_daily.parquet
                                          every row agrees with
  INFERRED_ONLY           INFERRED        NULL (inferred_session_date is a guess)
  MIXED                   AMBIGUOUS       NULL (rows from two sessions)

capture_class: FULL_CALLBACK (PR #72 dash_callback_v1), DOM_TOP15 (the legacy
rendered 15-row table), SELECTOR_UNION_BACKFILL (TOP_5_NB/NS selector union).

CONTENT_MATCHED needs broker_daily.parquet, which is gitignored. Without it
the generator FAILS CLOSED: those dates become INFERRED_ONLY and the manifest
says the evidence was unavailable. It never falls back to the calendar rule.
Each record carries rows_sha256 of the exact rows it classified, so a committed
manifest can be checked against the database without the parquet (verify).

The committed manifest is ONE snapshot: AUDITED_SNAPSHOT (2026-09-30, master
1aeca53). The default build checks that the database and the parquet are
exactly that snapshot before classifying anything, and writes nothing if they
are not. A later database is a new snapshot and needs --new-snapshot with a
different --out; it can never replace the audited manifest.

Usage:
  py -3 broker_flow_regime.py build  --db neobdm.db --broker-daily PATH
  py -3 broker_flow_regime.py build  --new-snapshot --db DB --broker-daily PATH \
        --out OTHER.json
  py -3 broker_flow_regime.py verify --db neobdm.db \
        --manifest evidence/broker_flow_date_evidence.json
"""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import idx_calendar

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = "broker_flow_date_evidence_v1"
GENERATOR_VERSION = 1
MANIFEST_PATH = os.path.join("evidence", "broker_flow_date_evidence.json")

BACKFILL, LIVE = "BACKFILL", "LIVE"

SCAN_VERIFIED = "SCAN_VERIFIED"
SOURCE_DATED_BACKFILL = "SOURCE_DATED_BACKFILL"
CONTENT_MATCHED = "CONTENT_MATCHED"
INFERRED_ONLY = "INFERRED_ONLY"
MIXED = "MIXED"
DATE_CLASSES = (SCAN_VERIFIED, SOURCE_DATED_BACKFILL, CONTENT_MATCHED, INFERRED_ONLY, MIXED)
EVIDENCE_LEVEL = {SCAN_VERIFIED: "PROVEN", SOURCE_DATED_BACKFILL: "PROVEN",
                  CONTENT_MATCHED: "PROVEN", INFERRED_ONLY: "INFERRED", MIXED: "AMBIGUOUS"}

FULL_CALLBACK = "FULL_CALLBACK"
DOM_TOP15 = "DOM_TOP15"
SELECTOR_UNION_BACKFILL = "SELECTOR_UNION_BACKFILL"
CAPTURE_CLASSES = (FULL_CALLBACK, DOM_TOP15, SELECTOR_UNION_BACKFILL)

# Content match: live bval/sval are billions of Rupiah rounded to 0.1, the
# parquet holds full Rupiah. A row agrees with a session when both sides are
# within half a display unit. A date is CONTENT_MATCHED only when EXACTLY ONE
# candidate session agrees on EVERY row (a row with no parquet counterpart
# never agrees: absent is not zero).
RP_PER_UNIT = 1e9
TOLERANCE = 0.05 + 1e-6
CANDIDATE_WINDOW_DAYS = 14
TOP_CANDIDATES = 3

# Quarantined by the date-regime audit (2026-09-30). Proof is in committed git
# history: the first write and the same-date rerun are both commits of
# neobdm.db. INSERT OR REPLACE kept every row the rerun did not return, so the
# `identical` rows are either untouched first-write rows or rewritten with the
# same values; nothing tells which. Recomputed by rerun_diff() in the tests.
# rerun.rows_sha256 pins the rows the rerun left (== _group_hash of the rerun
# blob): a date whose current rows differ is NOT the audited mixture and the
# build refuses it instead of calling it MIXED.
AUDITED_MIXED = {
    "2026-08-12": {
        "first_write": {"commit": "6793d52abe39c0888c32726b59f0952e957d4cb6",
                        "neobdm_db_blob": "e362f31e0d2e85ebc8801a4f5d852fa863aa68a4",
                        "live_rows": 212},
        "rerun": {"commit": "f441ec1146c0c93dcfd1e11cc14d5fb3ca475b9c",
                  "neobdm_db_blob": "c0000cc0d4e39d564c4f5136bf593e23cc46742b",
                  "live_rows": 322,
                  "rows_sha256": "3d9482f2e2f928a01a2d054655ba3dce5041c916a518724ee1f684a25b72f1e6"},
        "rerun_diff": {"identical": 110, "changed": 102, "new": 110, "removed": 0},
    },
    "2026-08-27": {
        "first_write": {"commit": "8b454b0297994209c0e190b400ed3932afcbad15",
                        "neobdm_db_blob": "8da082a0bc238d2b382f03f343f939bd08a994c8",
                        "live_rows": 198},
        "rerun": {"commit": "da4d96bf7b8d4f56a66a67f2cd7fa28d554b0462",
                  "neobdm_db_blob": "356c126d8b380e9d248e677226e6dcdb5b80f4e5",
                  "live_rows": 279,
                  "rows_sha256": "9e05d901556a262e1a90a2a3285f835835d1c10c420b6f4c9c548875d71a3b5a"},
        "rerun_diff": {"identical": 92, "changed": 106, "new": 81, "removed": 0},
    },
}

# The committed manifest is the 2026-09-30 audited snapshot and nothing else.
# The default build checks every fact here BEFORE classifying or writing
# anything; a later database or a different parquet raises SnapshotMismatch.
# A new snapshot is a deliberate `build --new-snapshot --out OTHER_PATH`.
AUDITED_SNAPSHOT = {
    "name": "audited-2026-09-30",
    "source_commit": "1aeca5313819e4843ce5cab210d0030dd8f58a78",
    "broker_flow": {
        "rows": 232_493,
        "records": 305,
        "ordered_sha256": "9c433af09daa3ed6780f90583cfa5e268d33265f4b8ddde6678777389b46d353",
        "by_regime": {"BACKFILL": {"dates": 218, "rows": 212_839},
                      "LIVE": {"dates": 87, "rows": 19_654}},
    },
    # SCAN_VERIFIED and its canonical session come from these rows, so they
    # are pinned too (_scan_fingerprint: every row, NULL columns omitted).
    "broker_flow_scan": {
        "rows": 29,
        "ordered_sha256": "ff59ea3a7bcb8014aca70a3d077da68d55ea98fd709720d6897cb47c51f104d7",
    },
    "broker_daily_sha256": "c8d1948f00d99ba96fe17376292f32a9cda2be36e2eb5ce303e680427f05cc32",
    # inferred_session_date and SCAN_VERIFIED validation both use the calendar
    "idx_calendar_version": "idx-2026-2027.v1",
}

# PERSISTED scan runs written before PR #73 added expected_session_date and
# calendar_version. Their NULL metadata is accepted for exactly these runs;
# any other run with NULL metadata is contradictory (every PR #73 write fills
# both). Never inferred from NULLs or timestamps.
AUDITED_LEGACY_SCAN_RUNS = frozenset({("2026-09-30", "2026-09-30T01:52:17.810039+00:00")})

BACKFILL_EVIDENCE_REF = ("backfill_inventory.insert_inventory: broker_flow.date = "
                         "/api/inventory data.date, days <= BACKFILL_END (2026-07-04), "
                         "brokers=TOP_5_NB_LOT_C20+TOP_5_NS_LOT_C20")

COLUMNS = ("date", "ticker", "broker_code", "bval", "sval", "netval", "bavg", "savg")


class ProvenanceContradiction(ValueError):
    """broker_flow and its own provenance disagree; nothing is classified."""


class SnapshotMismatch(ValueError):
    """The inputs are not the audited snapshot the build was asked for."""


def regime_of(bval):
    """bval is NULL only on backfill rows; the live path always writes it."""
    return BACKFILL if bval is None else LIVE


# --------------------------------------------------------------------------
# Read-only access and hashing
# --------------------------------------------------------------------------

def connect_readonly(db_path):
    """immutable=1 takes no locks and never writes a journal or WAL."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _row_line(row):
    return json.dumps(list(row), separators=(",", ":"), ensure_ascii=True) + "\n"


def _sha256_rows(rows):
    h = hashlib.sha256()
    n = 0
    for row in rows:
        h.update(_row_line(row).encode("ascii"))
        n += 1
    return n, h.hexdigest()


def broker_flow_fingerprint(con):
    """(row count, sha256 over every row ordered by the primary key)."""
    return _sha256_rows(con.execute(
        f"SELECT {', '.join(COLUMNS)} FROM broker_flow ORDER BY date, ticker, broker_code"))


def _group_hash(con, d, regime):
    null = "IS NULL" if regime == BACKFILL else "IS NOT NULL"
    return _sha256_rows(con.execute(
        f"SELECT {', '.join(COLUMNS)} FROM broker_flow WHERE date = ? AND bval {null} "
        "ORDER BY ticker, broker_code", (d,)))


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_exists(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


# --------------------------------------------------------------------------
# Evidence readers
# --------------------------------------------------------------------------

def _scan_rows(con):
    if not _table_exists(con, "broker_flow_scan"):
        return []
    cols = [r[1] for r in con.execute("PRAGMA table_info(broker_flow_scan)")]
    return [dict(zip(cols, r)) for r in con.execute(
        "SELECT * FROM broker_flow_scan ORDER BY scrape_date, run_started_utc, broker_code")]


def _scan_fingerprint(scan):
    # NULL columns are left out so a later nullable ALTER (PR #73 added two)
    # does not change the hash of rows written before it.
    return _sha256_rows([sorted((k, v) for k, v in r.items() if v is not None) for r in scan])


def scan_evidence(scan, d, live_by_broker):
    """The PERSISTED run for scrape_date d, or None. live_by_broker:
    {broker_code: live broker_flow rows on d}.

    SCAN_VERIFIED needs provenance that proves itself; anything contradictory
    raises ProvenanceContradiction (never a silent downgrade):
      - one run, each broker_code once, every row status OK / dash_callback_v1;
      - one non-NULL session_date, equal to the latest IDX session strictly
        before d under the runtime calendar (so never same-day, future,
        non-session or stale; an unanswerable calendar fails too);
      - PR #73 metadata: every row has the same non-NULL expected_session_date,
        equal to session_date, and the same non-NULL calendar_version. All-NULL
        metadata only for a run in AUDITED_LEGACY_SCAN_RUNS.
        The recorded calendar_version is NOT required to equal the runtime one:
        the runtime calendar re-derives the session and must agree with the
        recorded expected_session_date, which is the substantive check. A
        version-label match would add nothing and would make every run recorded
        before a calendar bump (needed for 2028) unusable.
      - tracked_rows non-NULL and equal, per broker, to the live rows of that
        broker on d, and no live broker outside the run."""
    persisted = [r for r in scan if r["scrape_date"] == d and r["snapshot"] == "PERSISTED"]
    if not persisted:
        return None

    def contradiction(why):
        return ProvenanceContradiction(f"{d}: PERSISTED scan {why}")

    runs = sorted({r["run_started_utc"] for r in persisted}, key=str)
    if len(runs) != 1:
        raise contradiction(f"has {len(runs)} runs: {runs}")
    run = runs[0]
    counts = Counter(r["broker_code"] for r in persisted)
    dupes = sorted(c for c, k in counts.items() if k > 1)
    if dupes:
        raise contradiction(f"repeats broker codes {dupes}")
    bad = sorted(r["broker_code"] for r in persisted
                 if r["status"] != "OK" or r["method"] != "dash_callback_v1")
    if bad:
        raise contradiction(f"has rows that are not OK dash_callback_v1: {bad}")

    sessions = sorted({r["session_date"] for r in persisted}, key=str)
    if len(sessions) != 1 or sessions[0] is None:
        raise contradiction(f"does not name one source session: {sessions}")
    session = sessions[0]
    version = idx_calendar.CALENDAR_VERSION
    try:
        want = idx_calendar.latest_idx_session_before(date.fromisoformat(d)).isoformat()
    except Exception as e:     # uncovered date, lookback exhausted, bad date: unprovable
        raise contradiction(f"session {session} cannot be checked against {version}: {e}")
    if session != want:
        raise contradiction(f"source session {session} is not {want}, the latest IDX session "
                            f"before {d} per {version}")

    expected = {r.get("expected_session_date") for r in persisted}
    versions = {r.get("calendar_version") for r in persisted}
    if expected == {None} and versions == {None}:
        if (d, run) not in AUDITED_LEGACY_SCAN_RUNS:
            raise contradiction(f"run {run} has no expected_session_date/calendar_version and "
                                "is not an audited pre-PR #73 run")
    elif None in expected or None in versions:
        raise contradiction("has expected_session_date/calendar_version on some rows only")
    elif len(expected) != 1 or len(versions) != 1:
        raise contradiction(f"rows disagree: expected_session_date {sorted(expected)}, "
                            f"calendar_version {sorted(versions)}")
    elif expected != {session}:
        raise contradiction(f"expected_session_date {sorted(expected)[0]} != "
                            f"source session {session}")

    missing = sorted(r["broker_code"] for r in persisted if r["tracked_rows"] is None)
    if missing:
        raise contradiction(f"has NULL tracked_rows for {missing}")
    tracked = {r["broker_code"]: r["tracked_rows"] for r in persisted}
    total, live_rows = sum(tracked.values()), sum(live_by_broker.values())
    if total != live_rows:
        raise contradiction(f"tracked {total} rows but broker_flow holds {live_rows} live rows")
    wrong = sorted(f"{c}:{n}!={live_by_broker.get(c, 0)}" for c, n in tracked.items()
                   if n != live_by_broker.get(c, 0))
    unscanned = sorted(set(live_by_broker) - set(tracked))
    if wrong or unscanned:
        raise contradiction(f"does not match live rows per broker: tracked!=live {wrong}, "
                            f"live brokers outside the run {unscanned}")
    return {"run_started_utc": run, "session_date": session,
            "codes": len(persisted), "tracked_rows": total,
            "expected_session_date": sorted(expected, key=str),
            "calendar_version": sorted(versions, key=str),
            "sha256": _scan_fingerprint(persisted)[1]}


def load_broker_daily(path, live, window_days=CANDIDATE_WINDOW_DAYS):
    """{session: {(ticker, broker): (bval, sval)}} limited to what the live
    dates can need, plus file metadata. live: {date: {(ticker, broker): ...}}.
    pyarrow is imported only here."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    dates = sorted(live)
    tickers = sorted({t for rows in live.values() for t, _ in rows})
    brokers = sorted({b for rows in live.values() for _, b in rows})
    pf = pq.ParquetFile(path)
    meta = {"name": os.path.basename(path), "sha256": file_sha256(path),
            "bytes": os.path.getsize(path), "num_rows": pf.metadata.num_rows}
    by_session = {}
    if dates:
        lo = (date.fromisoformat(dates[0]) - timedelta(days=window_days)).isoformat()
        t = pq.read_table(path, columns=["date", "ticker", "broker", "bval", "sval"],
                          filters=[("date", ">=", lo), ("date", "<=", dates[-1])])
        t = t.filter(pc.and_(pc.is_in(t["ticker"], value_set=_pa_strings(tickers)),
                             pc.is_in(t["broker"], value_set=_pa_strings(brokers))))
        for s, tk, br, bv, sv in zip(*(t[c].to_pylist()
                                       for c in ("date", "ticker", "broker", "bval", "sval"))):
            by_session.setdefault(s, {})[(tk, br)] = (bv, sv)
    date_col = pq.read_table(path, columns=["date"])["date"]
    meta["date_min"] = pc.min(date_col).as_py()
    meta["date_max"] = pc.max(date_col).as_py()
    return by_session, meta


def _pa_strings(values):
    import pyarrow as pa
    return pa.array(values, type=pa.string())


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return round(sxy / math.sqrt(sxx * syy), 6)


def _agrees(live_v, src_v):
    return all(abs(a - b / RP_PER_UNIT) <= TOLERANCE for a, b in zip(live_v, src_v))


def content_match(d, rows, by_session, window_days=CANDIDATE_WINDOW_DAYS):
    """Compare one live date's rows with every parquet session in
    [d - window_days, d]. rows: {(ticker, broker): (bval, sval)}."""
    lo = (date.fromisoformat(d) - timedelta(days=window_days)).isoformat()
    cands = []
    agree_sets = {}
    for s in sorted(x for x in by_session if lo <= x <= d):
        src = by_session[s]
        joined = [k for k in rows if k in src]
        agree = frozenset(k for k in joined if _agrees(rows[k], src[k]))
        xs = [v for k in joined for v in rows[k]]
        ys = [v / RP_PER_UNIT for k in joined for v in src[k]]
        agree_sets[s] = agree
        cands.append({"session": s, "joined_rows": len(joined),
                      "agreeing_rows": len(agree), "correlation": _pearson(xs, ys)})
    by_corr = sorted((c for c in cands if c["correlation"] is not None),
                     key=lambda c: (-c["correlation"], c["session"]))
    by_agree = sorted(cands, key=lambda c: (-c["agreeing_rows"],
                                            -(c["correlation"] or -2.0), c["session"]))
    full = [c["session"] for c in cands if c["agreeing_rows"] == len(rows) > 0]
    out = {
        "row_count": len(rows),
        "candidates_considered": len(cands),
        "window": [lo, d],
        "full_agreement_sessions": full,
        "matched_session": full[0] if len(full) == 1 else None,
        "correlation": by_corr[0]["correlation"] if by_corr else None,
        "best_correlation_session": by_corr[0]["session"] if by_corr else None,
        "runner_up_session": by_corr[1]["session"] if len(by_corr) > 1 else None,
        "runner_up_correlation": by_corr[1]["correlation"] if len(by_corr) > 1 else None,
        "top_candidates": by_agree[:TOP_CANDIDATES],
    }
    if out["matched_session"] is None and len(by_agree) >= 2:
        a, b = by_agree[0]["session"], by_agree[1]["session"]
        sa, sb = agree_sets[a], agree_sets[b]
        out["agreement_partition"] = {
            "sessions": [a, b], "only_first": len(sa - sb), "only_second": len(sb - sa),
            "both": len(sa & sb), "neither": len(set(rows) - sa - sb)}
    return out


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def _inferred_session(d):
    try:
        return idx_calendar.latest_idx_session_before(date.fromisoformat(d)).isoformat()
    except idx_calendar.IdxCalendarUnavailable:
        return None


def _live_rows(con, d):
    return {(t, b): (bv, sv) for t, b, bv, sv in con.execute(
        "SELECT ticker, broker_code, bval, sval FROM broker_flow "
        "WHERE date = ? AND bval IS NOT NULL", (d,))}


def build_manifest(db_path, broker_daily_path=None, source_commit=None, snapshot=None):
    """Classify every (broker_flow.date, regime). Opens db_path read-only.

    snapshot: a contract like AUDITED_SNAPSHOT. When given, the database and
    the parquet must be exactly that snapshot's, checked before anything is
    classified; otherwise SnapshotMismatch. None = a generic, unpinned build."""
    con = connect_readonly(db_path)
    try:
        return _build(con, broker_daily_path, source_commit, snapshot)
    finally:
        con.close()


def _groups(con):
    return con.execute(
        "SELECT date, bval IS NULL, COUNT(*) FROM broker_flow GROUP BY date, bval IS NULL "
        "ORDER BY date, bval IS NULL").fetchall()


def check_snapshot(snapshot, total, flow_sha, groups, scan_fp, broker_daily_path,
                   source_commit):
    """Every difference from the snapshot contract, as one SnapshotMismatch.
    scan_fp: (rows, sha256) of broker_flow_scan by _scan_fingerprint, the
    same value the manifest records as input.broker_flow_scan."""
    want = snapshot["broker_flow"]
    got_regime = {}
    for _, is_bf, n in groups:
        c = got_regime.setdefault(BACKFILL if is_bf else LIVE, {"dates": 0, "rows": 0})
        c["dates"] += 1
        c["rows"] += n
    diffs = []
    if total != want["rows"]:
        diffs.append(f"broker_flow rows {total} != {want['rows']}")
    if len(groups) != want["records"]:
        diffs.append(f"(date, regime) records {len(groups)} != {want['records']}")
    if got_regime != want["by_regime"]:
        diffs.append(f"by_regime {got_regime} != {want['by_regime']}")
    if flow_sha != want["ordered_sha256"]:
        diffs.append(f"ordered sha256 {flow_sha} != {want['ordered_sha256']}")
    want_scan = snapshot["broker_flow_scan"]
    if scan_fp[0] != want_scan["rows"]:
        diffs.append(f"broker_flow_scan rows {scan_fp[0]} != {want_scan['rows']}")
    if scan_fp[1] != want_scan["ordered_sha256"]:
        diffs.append(f"broker_flow_scan ordered sha256 {scan_fp[1]} != "
                     f"{want_scan['ordered_sha256']}")
    if idx_calendar.CALENDAR_VERSION != snapshot["idx_calendar_version"]:
        diffs.append(f"runtime idx_calendar {idx_calendar.CALENDAR_VERSION} != "
                     f"{snapshot['idx_calendar_version']}")
    if source_commit is not None and source_commit != snapshot["source_commit"]:
        diffs.append(f"source_commit {source_commit} != {snapshot['source_commit']}")
    if not (broker_daily_path and os.path.isfile(broker_daily_path)):
        diffs.append("broker_daily.parquet not supplied; the audited snapshot requires it")
    elif file_sha256(broker_daily_path) != snapshot["broker_daily_sha256"]:
        diffs.append(f"broker_daily.parquet sha256 != {snapshot['broker_daily_sha256']}")
    if diffs:
        raise SnapshotMismatch(f"not the {snapshot['name']} snapshot: " + "; ".join(diffs))


def _build(con, broker_daily_path, source_commit, snapshot):
    total, flow_sha = broker_flow_fingerprint(con)
    groups = _groups(con)
    scan = _scan_rows(con)
    scan_fp = _scan_fingerprint(scan)
    if snapshot is not None:
        check_snapshot(snapshot, total, flow_sha, groups, scan_fp, broker_daily_path,
                       source_commit)
        source_commit = snapshot["source_commit"]
    live_dates = [d for d, is_bf, _ in groups if not is_bf]
    live = {d: _live_rows(con, d) for d in live_dates}

    if broker_daily_path and os.path.isfile(broker_daily_path):
        by_session, pq_meta = load_broker_daily(broker_daily_path, live)
        evidence = dict(pq_meta, status="AVAILABLE",
                        upstream="build_inventory_db.py <- inventory_raw/*.json.gz "
                                 "(/api/inventory, source-dated)")
    else:
        by_session = None
        evidence = {"status": "UNAVAILABLE", "name": "broker_daily.parquet", "sha256": None,
                    "reason": ("no --broker-daily path given" if not broker_daily_path else
                               f"{os.path.basename(broker_daily_path)} not found"),
                    "effect": "no date can be CONTENT_MATCHED; those dates stay INFERRED_ONLY"}

    records = []
    for d, is_bf, n in groups:
        regime = BACKFILL if is_bf else LIVE
        n_hashed, rows_sha = _group_hash(con, d, regime)
        assert n_hashed == n
        rec = {"broker_flow_date": d, "regime": regime, "row_count": n, "rows_sha256": rows_sha,
               "canonical_session_date": None, "inferred_session_date": None,
               "inferred_matches_canonical": None, "content_match": None,
               "capture_commit": None, "notes": []}
        if regime == BACKFILL:
            rec.update(date_class=SOURCE_DATED_BACKFILL, capture_class=SELECTOR_UNION_BACKFILL,
                       canonical_session_date=d, evidence_kind="generation_path",
                       evidence_ref=BACKFILL_EVIDENCE_REF, evidence_hash=None)
            records.append(_finish(rec))
            continue

        rec["inferred_session_date"] = _inferred_session(d)
        cm = content_match(d, live[d], by_session) if by_session is not None else None
        rec["content_match"] = cm
        sc = scan_evidence(scan, d, Counter(b for _, b in live[d]))

        if d in AUDITED_MIXED:
            if sc is not None:
                raise ProvenanceContradiction(f"{d}: quarantined MIXED date has a PERSISTED scan")
            mx = AUDITED_MIXED[d]
            if (n, rows_sha) != (mx["rerun"]["live_rows"], mx["rerun"]["rows_sha256"]):
                raise ProvenanceContradiction(
                    f"{d}: live rows ({n}, {rows_sha[:12]}) are not the audited rerun state "
                    f"({mx['rerun']['live_rows']}, {mx['rerun']['rows_sha256'][:12]})")
            rec.update(date_class=MIXED, capture_class=DOM_TOP15, evidence_kind="git_rerun_diff",
                       evidence_ref=f"neobdm.db@{mx['first_write']['commit'][:7]} -> "
                                    f"neobdm.db@{mx['rerun']['commit'][:7]}",
                       evidence_hash="git-blob:" + mx["rerun"]["neobdm_db_blob"],
                       capture_commit=mx["rerun"]["commit"], git_rerun=mx,
                       inferred_session_date=None)
            rec["notes"].append("same-date rerun kept rows it did not return (INSERT OR REPLACE); "
                                "rows from two sessions, no canonical session")
        elif sc is not None:
            rec.update(date_class=SCAN_VERIFIED, capture_class=FULL_CALLBACK,
                       canonical_session_date=sc["session_date"], evidence_kind="broker_flow_scan",
                       evidence_ref=f"broker_flow_scan scrape_date={d} "
                                    f"run_started_utc={sc['run_started_utc']} snapshot=PERSISTED",
                       evidence_hash="sha256:" + sc["sha256"], scan=sc)
        elif cm is not None and cm["matched_session"] is not None:
            rec.update(date_class=CONTENT_MATCHED, capture_class=DOM_TOP15,
                       canonical_session_date=cm["matched_session"],
                       evidence_kind="content_match", evidence_ref="broker_daily.parquet",
                       evidence_hash="sha256:" + evidence["sha256"])
        else:
            rec.update(date_class=INFERRED_ONLY, capture_class=DOM_TOP15,
                       evidence_kind="idx_calendar_inference",
                       evidence_ref=f"idx_calendar.latest_idx_session_before "
                                    f"({idx_calendar.CALENDAR_VERSION})",
                       evidence_hash=None)
            rec["notes"].append("inferred_session_date assumes a pre-open scrape; NOT verified")
            if cm is None:
                rec["notes"].append("content-match evidence unavailable")
            elif len(cm["full_agreement_sessions"]) > 1:
                rec["notes"].append("several sessions agree on every row; ambiguous")
            elif (evidence.get("date_max") and rec["inferred_session_date"]
                  and rec["inferred_session_date"] > evidence["date_max"]):
                rec["notes"].append("inferred session is after the content-match evidence "
                                    f"coverage ({evidence['date_max']})")
            else:
                rec["notes"].append("no candidate session agrees on every row")
        records.append(_finish(rec))

    return {
        "schema": SCHEMA,
        "generator": "broker_flow_regime.py",
        "generator_version": GENERATOR_VERSION,
        "principle": ("broker_flow.date is untouched historical/acquisition evidence; "
                      "canonical_session_date is metadata only and is NULL unless PROVEN"),
        "input": {
            "snapshot": snapshot["name"] if snapshot else None,
            "source_commit": source_commit,
            "broker_flow": {"rows": total, "ordered_sha256": flow_sha,
                            "order": "date, ticker, broker_code",
                            "dates": len({d for d, _, _ in groups}),
                            "by_regime": _regime_counts(records)},
            "broker_flow_scan": dict(zip(("rows", "ordered_sha256"), scan_fp)),
            "content_match_evidence": evidence,
            "idx_calendar_version": idx_calendar.CALENDAR_VERSION,
        },
        "rules": {
            "regime": "BACKFILL if bval IS NULL else LIVE",
            "order": ["BACKFILL -> SOURCE_DATED_BACKFILL", "LIVE in AUDITED_MIXED -> MIXED",
                      "LIVE with PERSISTED dash_callback_v1 scan -> SCAN_VERIFIED",
                      "LIVE with exactly one fully agreeing session -> CONTENT_MATCHED",
                      "otherwise -> INFERRED_ONLY"],
            "content_match": {"unit_rp": RP_PER_UNIT, "tolerance": TOLERANCE,
                              "candidate_window_days": CANDIDATE_WINDOW_DAYS,
                              "requires": "every live row has a parquet counterpart within "
                                          "tolerance on bval AND sval, for exactly one session"},
            "evidence_level": EVIDENCE_LEVEL,
        },
        "summary": _summary(records),
        "records": records,
    }


def _finish(rec):
    rec["evidence_level"] = EVIDENCE_LEVEL[rec["date_class"]]
    if rec["canonical_session_date"] and rec["inferred_session_date"]:
        rec["inferred_matches_canonical"] = rec["canonical_session_date"] == rec["inferred_session_date"]
    if rec["date_class"] in (INFERRED_ONLY, MIXED):
        assert rec["canonical_session_date"] is None
    return rec


def _regime_counts(records):
    out = {}
    for r in records:
        c = out.setdefault(r["regime"], {"dates": 0, "rows": 0})
        c["dates"] += 1
        c["rows"] += r["row_count"]
    return out


def _summary(records):
    by_class = {c: {"dates": 0, "rows": 0} for c in DATE_CLASSES}
    by_capture = {c: {"dates": 0, "rows": 0} for c in CAPTURE_CLASSES}
    copies = {}
    for r in records:
        for bucket, key in ((by_class, r["date_class"]), (by_capture, r["capture_class"])):
            bucket[key]["dates"] += 1
            bucket[key]["rows"] += r["row_count"]
        if r["regime"] == LIVE and r["canonical_session_date"]:
            copies.setdefault(r["canonical_session_date"], []).append(r["broker_flow_date"])
    return {
        "records": len(records),
        "by_date_class": by_class,
        "by_capture_class": by_capture,
        "unresolved": {"dates": by_class[INFERRED_ONLY]["dates"] + by_class[MIXED]["dates"],
                       "rows": by_class[INFERRED_ONLY]["rows"] + by_class[MIXED]["rows"]},
        "live_sessions_held_by_several_dates": {s: ds for s, ds in sorted(copies.items())
                                                if len(ds) > 1},
        "live_inference_disagrees_with_proof": sorted(
            r["broker_flow_date"] for r in records if r["inferred_matches_canonical"] is False),
    }


def dumps(manifest):
    return json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


# --------------------------------------------------------------------------
# Verification of a committed manifest against a database
# --------------------------------------------------------------------------

def verify_manifest(manifest, db_path):
    """Compare each record with the rows it was computed over. Returns
    {"missing": [...], "gone": [...], "count_changed": [...], "content_changed": [...]}
    keyed by (date, regime). Read-only."""
    con = connect_readonly(db_path)
    try:
        groups = {(d, BACKFILL if is_bf else LIVE): n for d, is_bf, n in con.execute(
            "SELECT date, bval IS NULL, COUNT(*) FROM broker_flow GROUP BY date, bval IS NULL")}
        out = {"missing": [], "gone": [], "count_changed": [], "content_changed": []}
        seen = set()
        for r in manifest["records"]:
            key = (r["broker_flow_date"], r["regime"])
            seen.add(key)
            if key not in groups:
                out["gone"].append(key)
            elif groups[key] != r["row_count"]:
                out["count_changed"].append(key)
            elif _group_hash(con, *key)[1] != r["rows_sha256"]:
                out["content_changed"].append(key)
        out["missing"] = sorted(set(groups) - seen)
        return out
    finally:
        con.close()


def rerun_diff(db_before, db_after, d):
    """Live rows for date d in two versions of neobdm.db: how the second write
    relates to the first. Used to re-derive AUDITED_MIXED from git blobs."""
    def rows(path):
        con = connect_readonly(path)
        try:
            return {(t, b): v for t, b, *v in con.execute(
                "SELECT ticker, broker_code, bval, sval, netval, bavg, savg FROM broker_flow "
                "WHERE date = ? AND bval IS NOT NULL", (d,))}
        finally:
            con.close()
    a, b = rows(db_before), rows(db_after)
    return {"before_rows": len(a), "after_rows": len(b),
            "identical": sum(1 for k in b if k in a and a[k] == b[k]),
            "changed": sum(1 for k in b if k in a and a[k] != b[k]),
            "new": sum(1 for k in b if k not in a),
            "removed": sum(1 for k in a if k not in b)}


def _same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def write_manifest(manifest, out):
    """Write via a temp file + os.replace, so a failed write never leaves a
    half-written manifest in place."""
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write(dumps(manifest))
    os.replace(tmp, out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help=f"default: rebuild the {AUDITED_SNAPSHOT['name']} "
                                     "manifest; fails unless the inputs are exactly that snapshot")
    b.add_argument("--db", default="neobdm.db")
    b.add_argument("--broker-daily", default=None)
    b.add_argument("--source-commit", default=None)
    b.add_argument("--out", default=None, help=f"default {MANIFEST_PATH}")
    b.add_argument("--new-snapshot", action="store_true",
                   help="unpinned build of whatever the inputs are; needs --out, "
                        "and never the committed audited manifest")
    v = sub.add_parser("verify")
    v.add_argument("--db", default="neobdm.db")
    v.add_argument("--manifest", default=MANIFEST_PATH)
    args = ap.parse_args(argv)

    if args.cmd == "build":
        committed = os.path.join(HERE, MANIFEST_PATH)
        if args.new_snapshot:
            if not args.out or _same_path(args.out, committed):
                ap.error("--new-snapshot needs an explicit --out other than the committed "
                         f"{AUDITED_SNAPSHOT['name']} manifest ({MANIFEST_PATH})")
            snapshot = None
        else:
            snapshot = AUDITED_SNAPSHOT
        out = args.out or committed
        try:
            m = build_manifest(args.db, args.broker_daily, args.source_commit, snapshot)
        except (SnapshotMismatch, ProvenanceContradiction) as e:
            print(f"REFUSED, nothing written: {e}", file=sys.stderr)
            return 2
        write_manifest(m, out)
        print(json.dumps({"out": out, "snapshot": m["input"]["snapshot"],
                          "input": m["input"]["broker_flow"],
                          "content_match_evidence": m["input"]["content_match_evidence"]["status"],
                          "summary": m["summary"]["by_date_class"],
                          "unresolved": m["summary"]["unresolved"]}, indent=1))
        return 0
    with open(args.manifest, encoding="ascii") as f:
        m = json.load(f)
    res = verify_manifest(m, args.db)
    print(json.dumps(res, indent=1))
    live_changed = [k for k in res["content_changed"] if k[1] == LIVE]
    return 1 if (res["gone"] or res["count_changed"] or live_changed) else 0


if __name__ == "__main__":
    sys.exit(main())
