"""broker_flow_regime: the read-only date evidence manifest for broker_flow.

Synthetic fixtures pin the classification rules. The real neobdm.db and the
committed manifest are checked when present; the gitignored
broker_daily.parquet is used only when BROKER_DAILY_PARQUET (or a local
broker_daily.parquet) supplies it, and its hash must equal the one the
committed manifest recorded."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

import broker_flow_regime as bfr

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_DB = os.path.join(HERE, "neobdm.db")
COMMITTED = os.path.join(HERE, bfr.MANIFEST_PATH)

# Audit baseline (read-only Broker Flow Historical Date-Regime Audit, 2026-09-30).
AUDIT = {"rows": 232_493, "dates": 305,
         "BACKFILL": {"rows": 212_839, "dates": 218},
         "LIVE": {"rows": 19_654, "dates": 87},
         bfr.SCAN_VERIFIED: {"rows": 965, "dates": 1},
         bfr.SOURCE_DATED_BACKFILL: {"rows": 212_839, "dates": 218},
         bfr.CONTENT_MATCHED: {"rows": 11_433, "dates": 49},
         bfr.INFERRED_ONLY: {"rows": 6_655, "dates": 35},
         bfr.MIXED: {"rows": 601, "dates": 2}}
SAME_DAY = ["2026-07-06", "2026-07-07", "2026-07-09", "2026-07-10", "2026-07-13"]


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# --------------------------------------------------------------------------
# Synthetic fixture
# --------------------------------------------------------------------------

TICKERS = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"]
BROKERS = ["AK", "ZP"]
KEYS = [(t, b) for t in TICKERS for b in BROKERS]


def session_values(seed):
    """Distinct full-Rupiah (bval, sval) per key for one parquet session."""
    return {k: ((i * 7 + seed * 13) % 50 * 1e8 + 3e8, (i * 11 + seed * 5) % 40 * 1e8 + 1e8)
            for i, k in enumerate(KEYS)}


SESSIONS = {"2026-07-02": session_values(1), "2026-07-03": session_values(2),
            "2026-07-06": session_values(3), "2026-07-07": session_values(4),
            "2026-07-13": session_values(5), "2026-07-14": session_values(5),   # identical
            "2026-08-11": session_values(6), "2026-08-12": session_values(7),
            "2026-08-24": session_values(8)}


def as_live(values):
    return {k: (round(b / 1e9, 1), round(s / 1e9, 1)) for k, (b, s) in values.items()}


def make_fixture(tmp_path, scan_tracked=None, with_scan=True):
    db = tmp_path / "neobdm.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE broker_flow (date TEXT NOT NULL, ticker TEXT NOT NULL,
        broker_code TEXT NOT NULL, bval REAL, sval REAL, netval REAL, bavg REAL, savg REAL,
        PRIMARY KEY (date, ticker, broker_code))""")
    rows = []
    for d in ("2026-07-02", "2026-07-03"):                       # backfill: bval NULL
        rows += [(d, t, b, None, None, 0.5, None, None) for t, b in KEYS[:4]]
    live = {
        "2026-07-06": as_live(SESSIONS["2026-07-06"]),          # same-day session
        "2026-07-08": as_live(SESSIONS["2026-07-07"]),          # previous session
        "2026-07-15": as_live(SESSIONS["2026-07-14"]),          # two identical sessions
        "2026-08-12": {**as_live(SESSIONS["2026-08-11"]),       # quarantined mixture
                       **{k: v for k, v in as_live(SESSIONS["2026-08-12"]).items()
                          if k[1] == "AK"}},
        "2026-09-01": {k: (v[0] + 0.7, v[1]) for k, v in as_live(SESSIONS["2026-08-24"]).items()},
        "2026-09-30": {k: (1.0, 2.0) for k in KEYS[:5]},
    }
    for d, vals in live.items():
        rows += [(d, t, b, bv, sv, bv - sv, 100.0, 100.0) for (t, b), (bv, sv) in vals.items()]
    con.executemany("INSERT INTO broker_flow VALUES (?,?,?,?,?,?,?,?)", rows)
    if with_scan:
        con.execute("""CREATE TABLE broker_flow_scan (scrape_date TEXT NOT NULL,
            broker_code TEXT NOT NULL, run_started_utc TEXT NOT NULL, status TEXT NOT NULL,
            snapshot TEXT NOT NULL, session_date TEXT, akum_rows_returned INTEGER,
            dist_rows_returned INTEGER, tracked_rows INTEGER, method TEXT NOT NULL,
            started_utc TEXT, completed_utc TEXT, detail TEXT,
            PRIMARY KEY (scrape_date, broker_code, run_started_utc))""")
        tracked = scan_tracked or (3, 2)
        run = "2026-09-30T01:52:17+00:00"
        con.executemany("INSERT INTO broker_flow_scan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("2026-09-30", "AK", run, "OK", "PERSISTED", "2026-09-29", 10, 10, tracked[0],
             "dash_callback_v1", run, run, ""),
            ("2026-09-30", "ZP", run, "OK", "PERSISTED", "2026-09-29", 10, 10, tracked[1],
             "dash_callback_v1", run, run, ""),
            ("2026-09-29", "AK", "2026-09-29T01:00:00+00:00", "SOURCE_FAILURE", "REJECTED",
             None, None, None, None, "dash_callback_v1", None, None, "http 500"),
            # an earlier run for the same scrape_date that was not persisted
            ("2026-09-30", "AK", "2026-09-30T00:10:00+00:00", "SOURCE_FAILURE", "REJECTED",
             None, None, None, None, "dash_callback_v1", None, None, "http 500"),
            ("2026-09-30", "ZP", "2026-09-30T00:10:00+00:00", "OK", "REJECTED",
             "2026-09-28", 10, 10, 4, "dash_callback_v1", None, None, "stale session"),
        ])
    con.commit()
    con.close()
    return str(db)


def make_parquet(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    cols = {c: [] for c in ("date", "ticker", "broker", "nlot", "nval", "blot", "bval",
                            "slot", "sval")}
    for s, vals in sorted(SESSIONS.items()):
        for (t, b), (bv, sv) in vals.items():
            for c, v in zip(cols, (s, t, b, 0, bv - sv, 0, bv, 0, sv)):
                cols[c].append(v)
    path = tmp_path / "broker_daily.parquet"
    pq.write_table(pa.table(cols), path)
    return str(path)


@pytest.fixture
def fixture(tmp_path):
    return make_fixture(tmp_path), make_parquet(tmp_path)


def by_key(m):
    return {(r["broker_flow_date"], r["regime"]): r for r in m["records"]}


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def test_regime_is_decided_by_bval_null_only():
    assert bfr.regime_of(None) == bfr.BACKFILL
    assert bfr.regime_of(0.0) == bfr.LIVE
    assert bfr.regime_of(12.3) == bfr.LIVE


def test_every_date_regime_pair_gets_exactly_one_record(fixture):
    db, pq = fixture
    m = bfr.build_manifest(db, pq)
    keys = [(r["broker_flow_date"], r["regime"]) for r in m["records"]]
    con = sqlite3.connect(db)
    expected = [(d, bfr.BACKFILL if bf else bfr.LIVE) for d, bf in con.execute(
        "SELECT DISTINCT date, bval IS NULL FROM broker_flow ORDER BY date, bval IS NULL")]
    con.close()
    total = sqlite3.connect(db).execute("SELECT COUNT(*) FROM broker_flow").fetchone()[0]
    assert keys == expected and len(set(keys)) == len(keys)
    assert sum(r["row_count"] for r in m["records"]) == m["input"]["broker_flow"]["rows"] == total


def test_backfill_is_source_dated_with_its_own_date_as_canonical(fixture):
    m = bfr.build_manifest(*fixture)
    for d in ("2026-07-02", "2026-07-03"):
        r = by_key(m)[(d, bfr.BACKFILL)]
        assert (r["date_class"], r["capture_class"], r["canonical_session_date"]) == \
            (bfr.SOURCE_DATED_BACKFILL, bfr.SELECTOR_UNION_BACKFILL, d)
        assert r["evidence_level"] == "PROVEN" and r["inferred_session_date"] is None


def test_scan_verified_takes_the_session_from_broker_flow_scan(fixture):
    r = by_key(bfr.build_manifest(*fixture))[("2026-09-30", bfr.LIVE)]
    assert (r["date_class"], r["capture_class"], r["canonical_session_date"]) == \
        (bfr.SCAN_VERIFIED, bfr.FULL_CALLBACK, "2026-09-29")
    assert r["scan"]["tracked_rows"] == r["row_count"] == 5
    assert r["evidence_hash"].startswith("sha256:")


def test_scan_that_disagrees_with_the_rows_raises_instead_of_classifying(tmp_path):
    db = make_fixture(tmp_path, scan_tracked=(3, 3))
    with pytest.raises(bfr.ProvenanceContradiction, match="tracked 6 rows"):
        bfr.build_manifest(db, None)


def test_content_match_needs_one_session_agreeing_on_every_row(fixture):
    m = bfr.build_manifest(*fixture)
    same_day = by_key(m)[("2026-07-06", bfr.LIVE)]
    prev = by_key(m)[("2026-07-08", bfr.LIVE)]
    assert (same_day["date_class"], same_day["canonical_session_date"]) == \
        (bfr.CONTENT_MATCHED, "2026-07-06")
    assert same_day["inferred_session_date"] == "2026-07-03"
    assert same_day["inferred_matches_canonical"] is False
    assert (prev["date_class"], prev["canonical_session_date"]) == \
        (bfr.CONTENT_MATCHED, "2026-07-07")
    cm = prev["content_match"]
    assert cm["matched_session"] == cm["best_correlation_session"] == "2026-07-07"
    assert cm["top_candidates"][0]["agreeing_rows"] == prev["row_count"] == 12
    assert cm["correlation"] > cm["runner_up_correlation"]
    assert prev["evidence_hash"] == "sha256:" + m["input"]["content_match_evidence"]["sha256"]
    assert m["summary"]["live_inference_disagrees_with_proof"] == ["2026-07-06"]


def test_two_sessions_agreeing_on_every_row_is_not_a_match(fixture):
    r = by_key(bfr.build_manifest(*fixture))[("2026-07-15", bfr.LIVE)]
    assert r["content_match"]["full_agreement_sessions"] == ["2026-07-13", "2026-07-14"]
    assert r["date_class"] == bfr.INFERRED_ONLY and r["canonical_session_date"] is None
    assert "ambiguous" in " ".join(r["notes"])


def test_a_row_without_a_parquet_counterpart_never_agrees():
    rows = {("AAAA", "AK"): (0.0, 0.0), ("BBBB", "AK"): (1.0, 2.0)}
    src = {"2026-07-06": {("BBBB", "AK"): (1e9, 2e9)}}
    cm = bfr.content_match("2026-07-06", rows, src)
    assert cm["matched_session"] is None
    assert cm["top_candidates"][0] == {"session": "2026-07-06", "joined_rows": 1,
                                       "agreeing_rows": 1, "correlation": None}


def test_agreement_is_half_a_display_unit_on_both_sides():
    src = {"2026-07-06": {("A", "AK"): (1.25e9, 3e9), ("B", "AK"): (2e9, 1e9),
                          ("C", "AK"): (4e9, 0.0)}}
    ok = {("A", "AK"): (1.2, 3.0), ("B", "AK"): (2.0, 1.0), ("C", "AK"): (4.0, 0.0)}
    assert bfr.content_match("2026-07-06", ok, src)["matched_session"] == "2026-07-06"
    off = {**ok, ("C", "AK"): (4.0, 0.1)}
    assert bfr.content_match("2026-07-06", off, src)["matched_session"] is None


def test_mixed_dates_are_quarantined_with_null_canonical_and_null_inference(fixture):
    m = bfr.build_manifest(*fixture)
    r = by_key(m)[("2026-08-12", bfr.LIVE)]
    assert (r["date_class"], r["canonical_session_date"], r["inferred_session_date"]) == \
        (bfr.MIXED, None, None)
    assert r["evidence_level"] == "AMBIGUOUS"
    assert r["git_rerun"] == bfr.AUDITED_MIXED["2026-08-12"]
    part = r["content_match"]["agreement_partition"]
    assert part["sessions"] == ["2026-08-11", "2026-08-12"] or \
        part["sessions"] == ["2026-08-12", "2026-08-11"]
    assert part["neither"] == 0 and part["only_first"] and part["only_second"]


def test_inferred_only_has_a_guess_but_no_canonical_session(fixture):
    r = by_key(bfr.build_manifest(*fixture))[("2026-09-01", bfr.LIVE)]
    assert (r["date_class"], r["canonical_session_date"], r["inferred_session_date"]) == \
        (bfr.INFERRED_ONLY, None, "2026-08-31")
    assert r["evidence_level"] == "INFERRED" and r["evidence_hash"] is None
    assert any("NOT verified" in n for n in r["notes"])


def test_without_the_parquet_nothing_is_content_matched(fixture, tmp_path):
    db, _ = fixture
    for arg in (None, str(tmp_path / "absent" / "broker_daily.parquet")):
        m = bfr.build_manifest(db, arg)
        ev = m["input"]["content_match_evidence"]
        assert ev["status"] == "UNAVAILABLE" and ev["sha256"] is None and ev["reason"]
        assert m["summary"]["by_date_class"][bfr.CONTENT_MATCHED] == {"dates": 0, "rows": 0}
        for d in ("2026-07-06", "2026-07-08"):
            r = by_key(m)[(d, bfr.LIVE)]
            assert (r["date_class"], r["canonical_session_date"], r["content_match"]) == \
                (bfr.INFERRED_ONLY, None, None)
            assert "content-match evidence unavailable" in r["notes"]
        # PROVEN classes that do not depend on the parquet are unchanged
        assert by_key(m)[("2026-09-30", bfr.LIVE)]["date_class"] == bfr.SCAN_VERIFIED
        assert by_key(m)[("2026-08-12", bfr.LIVE)]["date_class"] == bfr.MIXED


def test_generation_is_byte_deterministic(fixture, tmp_path):
    db, pq = fixture
    a, b = bfr.dumps(bfr.build_manifest(db, pq)), bfr.dumps(bfr.build_manifest(db, pq))
    assert a == b
    # nothing machine- or clock-specific: no absolute paths, no timestamps
    assert str(tmp_path) not in a and json.dumps(str(tmp_path))[1:-1] not in a
    assert "generated_at" not in a


def test_building_never_mutates_the_database(fixture):
    db, pq = fixture
    before_file, before = sha(db), bfr.broker_flow_fingerprint(sqlite3.connect(db))
    m = bfr.build_manifest(db, pq)
    bfr.verify_manifest(m, db)
    assert sha(db) == before_file
    assert bfr.broker_flow_fingerprint(sqlite3.connect(db)) == before
    assert (m["input"]["broker_flow"]["rows"], m["input"]["broker_flow"]["ordered_sha256"]) == before
    assert not [p for p in os.listdir(os.path.dirname(db)) if p.endswith(("-journal", "-wal"))]


def test_the_readonly_connection_refuses_writes(fixture):
    con = bfr.connect_readonly(fixture[0])
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM broker_flow")
    con.close()


def test_ordered_hash_changes_with_any_value(fixture, tmp_path):
    db = fixture[0]
    other = tmp_path / "copy.db"
    shutil.copy(db, other)
    con = sqlite3.connect(other)
    con.execute("UPDATE broker_flow SET netval = netval + 1e-9 WHERE date = '2026-07-02' "
                "AND ticker = 'AAAA' AND broker_code = 'AK'")
    con.commit()
    assert bfr.broker_flow_fingerprint(con)[1] != bfr.broker_flow_fingerprint(sqlite3.connect(db))[1]
    con.close()


def test_verify_reports_drift_per_record(fixture, tmp_path):
    db, pq = fixture
    m = bfr.build_manifest(db, pq)
    other = tmp_path / "drift.db"
    shutil.copy(db, other)
    con = sqlite3.connect(other)
    con.execute("UPDATE broker_flow SET bval = 9.9 WHERE date = '2026-07-06' AND ticker = 'AAAA'")
    con.execute("DELETE FROM broker_flow WHERE date = '2026-07-08' AND ticker = 'AAAA'")
    con.execute("INSERT INTO broker_flow VALUES ('2026-10-01','AAAA','AK',1,1,0,1,1)")
    con.commit()
    con.close()
    assert bfr.verify_manifest(m, db) == {"missing": [], "gone": [], "count_changed": [],
                                          "content_changed": []}
    assert bfr.verify_manifest(m, str(other)) == {
        "missing": [("2026-10-01", bfr.LIVE)], "gone": [],
        "count_changed": [("2026-07-08", bfr.LIVE)],
        "content_changed": [("2026-07-06", bfr.LIVE)]}


def test_a_scan_table_is_optional(tmp_path):
    db = make_fixture(tmp_path, with_scan=False)
    r = by_key(bfr.build_manifest(db, None))[("2026-09-30", bfr.LIVE)]
    assert r["date_class"] == bfr.INFERRED_ONLY and r["canonical_session_date"] is None


# --------------------------------------------------------------------------
# The committed manifest and the real database
# --------------------------------------------------------------------------

def committed():
    if not os.path.exists(COMMITTED):
        pytest.skip("committed manifest not present")
    with open(COMMITTED, encoding="ascii") as f:
        return json.load(f)


def real_db():
    if not os.path.exists(REAL_DB):
        pytest.skip("neobdm.db not present")
    return REAL_DB


def real_parquet(manifest):
    path = os.environ.get("BROKER_DAILY_PARQUET") or os.path.join(HERE, "broker_daily.parquet")
    if not os.path.isfile(path):
        pytest.skip("broker_daily.parquet not supplied (set BROKER_DAILY_PARQUET)")
    if bfr.file_sha256(path) != manifest["input"]["content_match_evidence"]["sha256"]:
        pytest.skip("supplied broker_daily.parquet is not the file the manifest recorded")
    return path


def test_committed_manifest_holds_the_audited_classification():
    m = committed()
    bf = m["input"]["broker_flow"]
    assert (bf["rows"], bf["dates"]) == (AUDIT["rows"], AUDIT["dates"])
    for regime in (bfr.BACKFILL, bfr.LIVE):
        assert bf["by_regime"][regime] == AUDIT[regime]
    for cls in bfr.DATE_CLASSES:
        assert m["summary"]["by_date_class"][cls] == AUDIT[cls], cls
    assert m["summary"]["unresolved"] == {"dates": 37, "rows": 7_256}
    assert m["summary"]["live_inference_disagrees_with_proof"] == SAME_DAY
    ev = m["input"]["content_match_evidence"]
    assert ev["status"] == "AVAILABLE" and len(ev["sha256"]) == 64
    assert (ev["date_min"], ev["date_max"]) == ("2025-08-22", "2026-08-21")


def test_committed_manifest_obeys_the_record_rules():
    m = committed()
    keys = [(r["broker_flow_date"], r["regime"]) for r in m["records"]]
    assert keys == sorted(keys, key=lambda k: (k[0], k[1] == bfr.BACKFILL))
    assert len(set(keys)) == len(keys)
    assert len(keys) == m["summary"]["records"] == AUDIT["dates"]
    for r in m["records"]:
        cls = r["date_class"]
        assert r["evidence_level"] == bfr.EVIDENCE_LEVEL[cls]
        assert (r["regime"] == bfr.BACKFILL) == (cls == bfr.SOURCE_DATED_BACKFILL)
        if cls in (bfr.INFERRED_ONLY, bfr.MIXED):
            assert r["canonical_session_date"] is None
        if cls == bfr.MIXED:
            assert r["inferred_session_date"] is None
            assert r["git_rerun"] == bfr.AUDITED_MIXED[r["broker_flow_date"]]
        if cls == bfr.SOURCE_DATED_BACKFILL:
            assert r["canonical_session_date"] == r["broker_flow_date"]
        if cls == bfr.CONTENT_MATCHED:
            cm = r["content_match"]
            assert cm["full_agreement_sessions"] == [r["canonical_session_date"]]
            assert cm["top_candidates"][0]["agreeing_rows"] == r["row_count"]
            assert cm["top_candidates"][0]["joined_rows"] == r["row_count"]
            assert cm["top_candidates"][1]["agreeing_rows"] < r["row_count"] / 10
            assert cm["correlation"] >= 0.99999 and cm["runner_up_correlation"] <= 0.91
            assert r["canonical_session_date"] <= r["broker_flow_date"]
    mixed = sorted(r["broker_flow_date"] for r in m["records"] if r["date_class"] == bfr.MIXED)
    assert mixed == ["2026-08-12", "2026-08-27"]
    s930 = by_key(m)[("2026-09-30", bfr.LIVE)]
    assert (s930["date_class"], s930["capture_class"], s930["row_count"],
            s930["canonical_session_date"]) == (bfr.SCAN_VERIFIED, bfr.FULL_CALLBACK, 965,
                                                "2026-09-29")
    same = {d: by_key(m)[(d, bfr.LIVE)]["canonical_session_date"] for d in SAME_DAY}
    assert same == {d: d for d in SAME_DAY}


def test_committed_manifest_still_describes_the_real_rows():
    """Records bind to their rows by rows_sha256. Live rows of a past date must
    never change; backfill values are healed by the nightly top-up, so only
    their row counts are pinned. New dates after the manifest are reported."""
    m, db = committed(), real_db()
    res = bfr.verify_manifest(m, db)
    assert res["gone"] == [] and res["count_changed"] == []
    assert [k for k in res["content_changed"] if k[1] == bfr.LIVE] == []
    last = max(r["broker_flow_date"] for r in m["records"])
    assert all(d > last for d, _ in res["missing"]), res["missing"]


def test_real_db_build_fails_closed_without_the_parquet_and_never_mutates():
    m, db = committed(), real_db()
    before_file = sha(db)
    before = bfr.broker_flow_fingerprint(bfr.connect_readonly(db))
    fresh = bfr.build_manifest(db, None, m["input"]["source_commit"])
    assert bfr.dumps(fresh) == bfr.dumps(bfr.build_manifest(db, None, m["input"]["source_commit"]))
    assert sha(db) == before_file
    assert bfr.broker_flow_fingerprint(bfr.connect_readonly(db)) == before
    assert (fresh["input"]["broker_flow"]["rows"],
            fresh["input"]["broker_flow"]["ordered_sha256"]) == before
    assert fresh["summary"]["by_date_class"][bfr.CONTENT_MATCHED] == {"dates": 0, "rows": 0}
    old, new = by_key(m), by_key(fresh)
    for key, r in old.items():
        if key not in new or new[key]["rows_sha256"] != r["rows_sha256"]:
            continue
        want = bfr.INFERRED_ONLY if r["date_class"] == bfr.CONTENT_MATCHED else r["date_class"]
        assert new[key]["date_class"] == want, key
    if before == (m["input"]["broker_flow"]["rows"], m["input"]["broker_flow"]["ordered_sha256"]):
        # the database is still exactly the audited baseline
        assert before[0] == AUDIT["rows"]
        assert fresh["summary"]["unresolved"]["rows"] == 7_256 + 11_433


def test_supplied_parquet_reproduces_the_committed_manifest():
    m, db = committed(), real_db()
    pq = real_parquet(m)
    fresh = bfr.build_manifest(db, pq, m["input"]["source_commit"])
    if fresh["input"]["broker_flow"]["ordered_sha256"] == m["input"]["broker_flow"]["ordered_sha256"]:
        with open(COMMITTED, encoding="ascii") as f:
            assert bfr.dumps(fresh) == f.read()
        return
    old, new = by_key(m), by_key(fresh)
    for key, r in old.items():
        if r["regime"] == bfr.LIVE and new.get(key, {}).get("rows_sha256") == r["rows_sha256"]:
            for field in ("date_class", "canonical_session_date", "content_match", "evidence_hash"):
                assert new[key][field] == r[field], (key, field)


def git(*args, **kw):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, **kw)


@pytest.mark.parametrize("d", sorted(bfr.AUDITED_MIXED))
def test_mixed_quarantine_is_rederived_from_git_history(d, tmp_path):
    ev = bfr.AUDITED_MIXED[d]
    paths = []
    for step in ("first_write", "rerun"):
        blob = ev[step]["neobdm_db_blob"]
        if git("rev-parse", f"{ev[step]['commit']}:neobdm.db").stdout.decode().strip() != blob:
            pytest.skip("git history for the quarantined rerun is not available")
        out = tmp_path / f"{step}.db"
        with open(out, "wb") as f:
            assert subprocess.run(["git", "cat-file", "blob", blob], cwd=HERE, stdout=f).returncode == 0
        paths.append(str(out))
    diff = bfr.rerun_diff(*paths, d)
    assert diff["before_rows"] == ev["first_write"]["live_rows"]
    assert diff["after_rows"] == ev["rerun"]["live_rows"]
    assert {k: diff[k] for k in ("identical", "changed", "new", "removed")} == ev["rerun_diff"]
    # the rerun rows are what the current database still holds for that date
    assert by_key(committed())[(d, bfr.LIVE)]["rows_sha256"] == \
        bfr._group_hash(bfr.connect_readonly(paths[1]), d, bfr.LIVE)[1]
