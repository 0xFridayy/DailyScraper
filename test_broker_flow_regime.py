"""broker_flow_regime: the read-only date evidence manifest for broker_flow.

Synthetic fixtures pin the classification rules. The real neobdm.db and the
committed manifest are checked when present; the gitignored
broker_daily.parquet is used only when BROKER_DAILY_PARQUET (or a local
broker_daily.parquet) supplies it, and its hash must equal the one the
committed manifest recorded: an explicitly supplied wrong file FAILS.

The audited-snapshot baseline database is neobdm.db at
AUDITED_SNAPSHOT["source_commit"], read from git history, so the snapshot
tests do not depend on the nightly-changing working copy."""

import copy
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


def group_state(db, d, regime=bfr.LIVE):
    con = bfr.connect_readonly(db)
    try:
        return bfr._group_hash(con, d, regime)
    finally:
        con.close()


@pytest.fixture
def pin(monkeypatch):
    """Point the audited 2026-08-12 rerun pin at a synthetic fixture's rows;
    the real pin describes the real rows, which no fixture reproduces."""
    def _pin(db):
        n, h = group_state(db, "2026-08-12")
        ev = copy.deepcopy(bfr.AUDITED_MIXED)
        ev["2026-08-12"]["rerun"].update(live_rows=n, rows_sha256=h)
        monkeypatch.setattr(bfr, "AUDITED_MIXED", ev)
        return db
    return _pin


@pytest.fixture
def fixture(tmp_path, pin):
    return pin(make_fixture(tmp_path)), make_parquet(tmp_path)


def snapshot_of(db, pq, name="synthetic"):
    """A snapshot contract describing exactly this fixture."""
    con = bfr.connect_readonly(db)
    rows, digest = bfr.broker_flow_fingerprint(con)
    groups = bfr._groups(con)
    scan_rows, scan_digest = bfr._scan_fingerprint(bfr._scan_rows(con))
    con.close()
    by_regime = {}
    for _, is_bf, n in groups:
        c = by_regime.setdefault(bfr.BACKFILL if is_bf else bfr.LIVE, {"dates": 0, "rows": 0})
        c["dates"] += 1
        c["rows"] += n
    return {"name": name, "source_commit": "0" * 40,
            "broker_flow": {"rows": rows, "records": len(groups), "ordered_sha256": digest,
                            "by_regime": by_regime},
            "broker_flow_scan": {"rows": scan_rows, "ordered_sha256": scan_digest},
            "broker_daily_sha256": bfr.file_sha256(pq)}


def edited_copy(src, dst, *sql):
    shutil.copy(src, dst)
    con = sqlite3.connect(dst)
    for s in sql:
        con.execute(s)
    con.commit()
    con.close()
    return str(dst)


FUTURE_ROW = "INSERT INTO broker_flow VALUES ('2026-10-01','AAAA','AK',1.0,1.0,0.0,1.0,1.0)"


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


def test_scan_that_disagrees_with_the_rows_raises_instead_of_classifying(tmp_path, pin):
    db = pin(make_fixture(tmp_path, scan_tracked=(3, 3)))
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


def test_a_scan_table_is_optional(tmp_path, pin):
    db = pin(make_fixture(tmp_path, with_scan=False))
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


def parquet_choice(explicit, local, expected_sha):
    """("use", path) | ("skip", why) | ("fail", why). An explicitly supplied
    parquet that is missing or not the recorded file is an error, never a skip;
    only the implicit local fallback may be skipped."""
    if explicit:
        if not os.path.isfile(explicit):
            return "fail", f"BROKER_DAILY_PARQUET={explicit} does not exist"
        if bfr.file_sha256(explicit) != expected_sha:
            return "fail", f"BROKER_DAILY_PARQUET={explicit} is not sha256 {expected_sha}"
        return "use", explicit
    if not os.path.isfile(local):
        return "skip", "broker_daily.parquet not supplied (set BROKER_DAILY_PARQUET)"
    if bfr.file_sha256(local) != expected_sha:
        return "skip", "local broker_daily.parquet is not the file the manifest recorded"
    return "use", local


def real_parquet(manifest):
    verdict, value = parquet_choice(os.environ.get("BROKER_DAILY_PARQUET"),
                                    os.path.join(HERE, "broker_daily.parquet"),
                                    manifest["input"]["content_match_evidence"]["sha256"])
    if verdict == "fail":
        pytest.fail(value)
    if verdict == "skip":
        pytest.skip(value)
    return value


@pytest.fixture(scope="session")
def baseline_db(tmp_path_factory):
    """neobdm.db exactly as the audited snapshot saw it, from git history."""
    commit = bfr.AUDITED_SNAPSHOT["source_commit"]
    blob = git("rev-parse", f"{commit}:neobdm.db").stdout.decode().strip()
    if len(blob) != 40:
        pytest.skip(f"git history for {commit[:7]}:neobdm.db is not available")
    path = tmp_path_factory.mktemp("baseline") / "neobdm.db"
    with open(path, "wb") as f:
        assert subprocess.run(["git", "cat-file", "blob", blob], cwd=HERE, stdout=f).returncode == 0
    con = bfr.connect_readonly(path)
    assert bfr.broker_flow_fingerprint(con)[1] == \
        bfr.AUDITED_SNAPSHOT["broker_flow"]["ordered_sha256"]
    con.close()
    return str(path)


def test_committed_manifest_holds_the_audited_classification():
    m = committed()
    bf = m["input"]["broker_flow"]
    snap = bfr.AUDITED_SNAPSHOT
    assert (m["input"]["snapshot"], m["input"]["source_commit"]) == \
        (snap["name"], snap["source_commit"])
    assert bf["ordered_sha256"] == snap["broker_flow"]["ordered_sha256"]
    assert bf["by_regime"] == snap["broker_flow"]["by_regime"]
    assert m["input"]["broker_flow_scan"] == snap["broker_flow_scan"]
    assert m["input"]["content_match_evidence"]["sha256"] == snap["broker_daily_sha256"]
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


def test_exact_audited_baseline_reproduces_the_committed_manifest_byte_for_byte(baseline_db,
                                                                                tmp_path):
    m = committed()
    pq = real_parquet(m)
    with open(COMMITTED, encoding="ascii") as f:
        text = f.read()
    fresh = bfr.build_manifest(baseline_db, pq, snapshot=bfr.AUDITED_SNAPSHOT)
    assert bfr.dumps(fresh) == text
    out = tmp_path / "out.json"
    assert bfr.main(["build", "--db", baseline_db, "--broker-daily", pq, "--out", str(out)]) == 0
    assert out.read_text(encoding="ascii") == text


def test_audited_snapshot_refuses_the_baseline_without_its_parquet(baseline_db, tmp_path):
    with pytest.raises(bfr.SnapshotMismatch, match="not supplied"):
        bfr.build_manifest(baseline_db, None, snapshot=bfr.AUDITED_SNAPSHOT)
    wrong = make_parquet(tmp_path)
    with pytest.raises(bfr.SnapshotMismatch, match="broker_daily.parquet sha256"):
        bfr.build_manifest(baseline_db, wrong, snapshot=bfr.AUDITED_SNAPSHOT)
    with pytest.raises(bfr.SnapshotMismatch, match="source_commit"):
        bfr.build_manifest(baseline_db, None, "f" * 40, snapshot=bfr.AUDITED_SNAPSHOT)


@pytest.mark.parametrize("edit, reasons", [
    (FUTURE_ROW, ["broker_flow rows 232494 != 232493", "records 306 != 305",
                  "'LIVE': {'dates': 88, 'rows': 19655}", "ordered sha256"]),
    ("DELETE FROM broker_flow WHERE date = '2025-08-04'",
     ["records 304 != 305", "'BACKFILL': {'dates': 217", "ordered sha256"]),
    ("DELETE FROM broker_flow WHERE date = '2026-09-30'",
     ["broker_flow rows 231528 != 232493", "records 304 != 305",
      "'LIVE': {'dates': 86, 'rows': 18689}"]),
    ("UPDATE broker_flow SET netval = netval + 1e-9 WHERE rowid = (SELECT MIN(rowid) "
     "FROM broker_flow WHERE date = '2026-07-06')", ["ordered sha256"]),
])
def test_audited_snapshot_build_fails_on_any_departure_from_the_baseline(
        baseline_db, tmp_path, edit, reasons):
    db = edited_copy(baseline_db, tmp_path / "later.db", edit)
    with pytest.raises(bfr.SnapshotMismatch) as e:
        bfr.build_manifest(db, None, snapshot=bfr.AUDITED_SNAPSHOT)
    for reason in reasons:
        assert reason in str(e.value), (reason, str(e.value))
    # the generic engine still classifies it, as an explicit new snapshot
    if edit != FUTURE_ROW:
        return
    m = bfr.build_manifest(db, None)
    assert m["input"]["snapshot"] is None
    assert by_key(m)[("2026-10-01", bfr.LIVE)]["date_class"] == bfr.INFERRED_ONLY


def test_refused_audited_build_writes_nothing(baseline_db, tmp_path, monkeypatch):
    """Default CLI target is the committed manifest; point HERE at a copy so a
    broken guard could only damage the copy, then prove it is untouched."""
    home = tmp_path / "repo"
    (home / "evidence").mkdir(parents=True)
    target = home / bfr.MANIFEST_PATH
    shutil.copy(COMMITTED if os.path.exists(COMMITTED) else __file__, target)
    before = sha(target)
    monkeypatch.setattr(bfr, "HERE", str(home))
    db = edited_copy(baseline_db, tmp_path / "later.db", FUTURE_ROW)
    pq = make_parquet(tmp_path)
    assert bfr.main(["build", "--db", db, "--broker-daily", pq]) == 2
    assert sha(target) == before
    other = tmp_path / "elsewhere.json"
    other.write_text("keep", encoding="ascii")
    assert bfr.main(["build", "--db", db, "--broker-daily", pq, "--out", str(other)]) == 2
    assert other.read_text(encoding="ascii") == "keep"
    assert sorted(os.listdir(home / "evidence")) == [os.path.basename(bfr.MANIFEST_PATH)]
    assert not os.path.exists(str(other) + ".tmp")


def test_synthetic_snapshot_contract_passes_exactly_and_fails_on_drift(fixture, tmp_path):
    db, pq = fixture
    contract = snapshot_of(db, pq)
    pinned = bfr.build_manifest(db, pq, snapshot=contract)
    assert pinned["input"]["snapshot"] == "synthetic"
    assert bfr.dumps(pinned) == bfr.dumps(bfr.build_manifest(db, pq, snapshot=contract))
    for i, (edit, reason) in enumerate([
            (FUTURE_ROW, "records"),
            ("DELETE FROM broker_flow WHERE date = '2026-07-02'", "records"),
            ("UPDATE broker_flow SET netval = 0.25 WHERE date = '2026-07-03' "
             "AND ticker = 'AAAA' AND broker_code = 'AK'", "ordered sha256"),
            ("UPDATE broker_flow_scan SET session_date = '2026-09-28' "
             "WHERE scrape_date = '2026-09-30' AND snapshot = 'PERSISTED'",
             "broker_flow_scan ordered sha256"),
            ("DELETE FROM broker_flow_scan WHERE snapshot = 'REJECTED'",
             "broker_flow_scan rows")]):
        other = edited_copy(db, tmp_path / f"drift{i}.db", edit)
        with pytest.raises(bfr.SnapshotMismatch, match=reason):
            bfr.build_manifest(other, pq, snapshot=contract)


def test_new_snapshot_mode_is_explicit_and_cannot_target_the_committed_manifest(
        fixture, tmp_path, monkeypatch):
    db, pq = fixture
    # sandbox the committed path so a broken guard can only hit a copy
    home = tmp_path / "repo"
    (home / "evidence").mkdir(parents=True)
    committed_path = str(home / bfr.MANIFEST_PATH)
    with open(committed_path, "w", encoding="ascii") as f:
        f.write("audited")
    monkeypatch.setattr(bfr, "HERE", str(home))
    for extra in ([], ["--out", committed_path],
                  ["--out", str(home / "evidence" / ".." / bfr.MANIFEST_PATH)]):
        with pytest.raises(SystemExit):
            bfr.main(["build", "--new-snapshot", "--db", db, "--broker-daily", pq, *extra])
    assert open(committed_path, encoding="ascii").read() == "audited"
    out = tmp_path / "new" / "snapshot.json"
    assert bfr.main(["build", "--new-snapshot", "--db", db, "--broker-daily", pq,
                     "--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="ascii"))
    assert written["input"]["snapshot"] is None and written["records"]


# broker_flow_scan is a classification input (SCAN_VERIFIED and its canonical
# session), so the audited snapshot pins it as well. Each edit below leaves
# broker_flow byte-identical.

SCAN_COLS = ("scrape_date, broker_code, run_started_utc, status, snapshot, session_date, "
             "akum_rows_returned, dist_rows_returned, tracked_rows, method, started_utc, "
             "completed_utc, detail")
SCAN_0930 = "scrape_date = '2026-09-30' AND snapshot = 'PERSISTED'"
SCAN_EDITS = {
    "session_date": f"UPDATE broker_flow_scan SET session_date = '2026-09-28' WHERE {SCAN_0930}",
    "snapshot": f"UPDATE broker_flow_scan SET snapshot = 'SUPERSEDED' "
                f"WHERE {SCAN_0930} AND broker_code = 'AI'",
    "status": f"UPDATE broker_flow_scan SET status = 'SOURCE_FAILURE' "
              f"WHERE {SCAN_0930} AND broker_code = 'AI'",
    "tracked_rows": f"UPDATE broker_flow_scan SET tracked_rows = tracked_rows + 1 "
                    f"WHERE {SCAN_0930} AND broker_code = 'AI'",
    "extra_row": f"INSERT INTO broker_flow_scan ({SCAN_COLS}) VALUES ('2026-09-29', 'AI', "
                 "'2026-09-29T01:00:00+00:00', 'SOURCE_FAILURE', 'REJECTED', NULL, NULL, NULL, "
                 "NULL, 'dash_callback_v1', NULL, NULL, 'http 500')",
}


def flow_fp(db):
    con = bfr.connect_readonly(db)
    try:
        return bfr.broker_flow_fingerprint(con)
    finally:
        con.close()


def scan_fp(db):
    con = bfr.connect_readonly(db)
    try:
        return bfr._scan_fingerprint(bfr._scan_rows(con))
    finally:
        con.close()


def optional_real_parquet():
    """The recorded parquet when supplied, else None; an explicitly supplied
    wrong one still fails."""
    verdict, value = parquet_choice(os.environ.get("BROKER_DAILY_PARQUET"),
                                    os.path.join(HERE, "broker_daily.parquet"),
                                    bfr.AUDITED_SNAPSHOT["broker_daily_sha256"])
    if verdict == "fail":
        pytest.fail(value)
    return value if verdict == "use" else None


def sandbox_committed(tmp_path, monkeypatch):
    """Point the CLI's default target at a copy so nothing here can touch the
    real committed manifest."""
    home = tmp_path / "repo"
    (home / "evidence").mkdir(parents=True)
    target = home / bfr.MANIFEST_PATH
    target.write_text("audited", encoding="ascii")
    monkeypatch.setattr(bfr, "HERE", str(home))
    return target


def test_exact_baseline_scan_is_the_pinned_scan(baseline_db):
    want = bfr.AUDITED_SNAPSHOT["broker_flow_scan"]
    assert scan_fp(baseline_db) == (want["rows"], want["ordered_sha256"]) == (
        29, "ff59ea3a7bcb8014aca70a3d077da68d55ea98fd709720d6897cb47c51f104d7")
    pq = optional_real_parquet()
    if pq is None:
        # the only objection to the exact baseline is the missing parquet
        with pytest.raises(bfr.SnapshotMismatch) as e:
            bfr.build_manifest(baseline_db, None, snapshot=bfr.AUDITED_SNAPSHOT)
        assert "broker_flow_scan" not in str(e.value) and "not supplied" in str(e.value)
    else:
        m = bfr.build_manifest(baseline_db, pq, snapshot=bfr.AUDITED_SNAPSHOT)
        assert m["input"]["broker_flow_scan"] == want


@pytest.mark.parametrize("edit, reasons", [
    ("session_date", ["broker_flow_scan ordered sha256"]),
    ("snapshot", ["broker_flow_scan ordered sha256"]),
    ("status", ["broker_flow_scan ordered sha256"]),
    ("tracked_rows", ["broker_flow_scan ordered sha256"]),
    ("extra_row", ["broker_flow_scan rows 30 != 29", "broker_flow_scan ordered sha256"]),
])
def test_audited_build_refuses_a_changed_scan_even_with_identical_broker_flow(
        baseline_db, tmp_path, monkeypatch, edit, reasons):
    db = edited_copy(baseline_db, tmp_path / "scan.db", SCAN_EDITS[edit])
    assert flow_fp(db) == flow_fp(baseline_db)
    assert scan_fp(db) != scan_fp(baseline_db)
    pq = optional_real_parquet()
    # SnapshotMismatch, not ProvenanceContradiction: refused before classifying
    with pytest.raises(bfr.SnapshotMismatch) as e:
        bfr.build_manifest(db, pq, snapshot=bfr.AUDITED_SNAPSHOT)
    msg = str(e.value)
    for reason in reasons:
        assert reason in msg, (reason, msg)
    assert "broker_flow rows" not in msg and "records" not in msg and "by_regime" not in msg
    if pq is not None:
        assert "parquet" not in msg
    target = sandbox_committed(tmp_path, monkeypatch)
    real_before = sha(COMMITTED) if os.path.exists(COMMITTED) else None
    args = ["build", "--db", db] + (["--broker-daily", pq] if pq else [])
    assert bfr.main(args) == 2
    assert target.read_text(encoding="ascii") == "audited"
    assert sorted(os.listdir(target.parent)) == [target.name]
    if real_before is not None:
        assert sha(COMMITTED) == real_before


def test_new_snapshot_mode_can_describe_a_different_scan_without_touching_the_audit(
        baseline_db, tmp_path, monkeypatch):
    db = edited_copy(baseline_db, tmp_path / "scan.db", SCAN_EDITS["session_date"])
    target = sandbox_committed(tmp_path, monkeypatch)
    real_before = sha(COMMITTED) if os.path.exists(COMMITTED) else None
    out = tmp_path / "other" / "scan_snapshot.json"
    assert bfr.main(["build", "--new-snapshot", "--db", db, "--out", str(out)]) == 0
    m = json.loads(out.read_text(encoding="ascii"))
    r = by_key(m)[("2026-09-30", bfr.LIVE)]
    assert (r["date_class"], r["canonical_session_date"]) == (bfr.SCAN_VERIFIED, "2026-09-28")
    assert m["input"]["snapshot"] is None
    assert m["input"]["broker_flow_scan"] != bfr.AUDITED_SNAPSHOT["broker_flow_scan"]
    assert m["input"]["broker_flow"]["ordered_sha256"] == \
        bfr.AUDITED_SNAPSHOT["broker_flow"]["ordered_sha256"]
    assert target.read_text(encoding="ascii") == "audited"
    if real_before is not None:
        assert sha(COMMITTED) == real_before


def test_explicitly_supplied_wrong_parquet_fails_but_a_missing_one_skips(tmp_path):
    good = tmp_path / "good.parquet"
    good.write_bytes(b"right bytes")
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"wrong bytes")
    want = bfr.file_sha256(good)
    absent = str(tmp_path / "absent.parquet")
    assert parquet_choice(str(good), absent, want) == ("use", str(good))
    assert parquet_choice(str(bad), absent, want)[0] == "fail"
    assert parquet_choice(absent, absent, want)[0] == "fail"
    assert parquet_choice(None, absent, want)[0] == "skip"
    assert parquet_choice("", str(bad), want)[0] == "skip"
    assert parquet_choice(None, str(good), want) == ("use", str(good))


def test_audited_mixed_date_with_other_rows_is_refused_not_called_mixed(fixture, tmp_path,
                                                                        monkeypatch):
    db, pq = fixture
    changed = edited_copy(db, tmp_path / "cleaned.db",
                          "DELETE FROM broker_flow WHERE date = '2026-08-12' AND broker_code = 'ZP'")
    with pytest.raises(bfr.ProvenanceContradiction, match="not the audited rerun state"):
        bfr.build_manifest(changed, pq)
    revalued = edited_copy(db, tmp_path / "revalued.db",
                           "UPDATE broker_flow SET bval = bval + 0.1 WHERE date = '2026-08-12' "
                           "AND ticker = 'AAAA' AND broker_code = 'AK'")
    with pytest.raises(bfr.ProvenanceContradiction, match="not the audited rerun state"):
        bfr.build_manifest(revalued, pq)
    # the real pin does not describe synthetic rows either
    monkeypatch.undo()
    with pytest.raises(bfr.ProvenanceContradiction, match="2026-08-12"):
        bfr.build_manifest(db, pq)


def test_real_mixed_dates_still_hold_the_pinned_rerun_rows():
    db = real_db()
    for d, ev in bfr.AUDITED_MIXED.items():
        assert group_state(db, d) == (ev["rerun"]["live_rows"], ev["rerun"]["rows_sha256"]), d


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
    # the pin is exactly the rows the rerun commit left, and the manifest holds them
    rerun_state = group_state(paths[1], d)
    assert rerun_state == (ev["rerun"]["live_rows"], ev["rerun"]["rows_sha256"])
    assert by_key(committed())[(d, bfr.LIVE)]["rows_sha256"] == rerun_state[1]
