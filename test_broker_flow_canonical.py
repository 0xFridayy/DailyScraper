"""broker_flow_canonical: the read-only canonical broker_flow reader.

Synthetic fixtures are classified by the real PR #74 generator
(broker_flow_regime.build_manifest), so the reader is tested against the
manifest shape it will actually meet; the fixture's own manifest stands in for
the audited one (the anchor) the same way the PR #74 tests stand in for
AUDITED_MIXED. The real-data tests read the committed audited manifest with
neobdm.db exactly as the audit saw it (git AUDITED_SNAPSHOT["source_commit"]),
never the nightly-changing working copy; they skip only when that git history
is absent (a shallow clone), which means they did not run."""

import ast
import copy
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
from datetime import date

import pytest

import broker_flow_canonical as bfc
import broker_flow_regime as bfr

HERE = os.path.dirname(os.path.abspath(__file__))
COMMITTED = os.path.join(HERE, bfr.MANIFEST_PATH)
ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def digest(manifest):
    return hashlib.sha256(bfr.dumps(manifest).encode("ascii")).hexdigest()


# --------------------------------------------------------------------------
# Synthetic fixture
# --------------------------------------------------------------------------
#
#   acquisition  regime    class (by the generator)   canonical session
#   2026-07-01   BACKFILL  SOURCE_DATED_BACKFILL      2026-07-01
#   2026-07-02   BACKFILL  SOURCE_DATED_BACKFILL      2026-07-02  } no shared key, but two
#   2026-07-04   LIVE      CONTENT_MATCHED            2026-07-02  } capture classes -> quarantined
#   2026-07-03   BACKFILL  SOURCE_DATED_BACKFILL      2026-07-03  } shared keys measured
#   2026-07-05   LIVE      CONTENT_MATCHED            2026-07-03  } differently -> quarantined
#   2026-07-06   LIVE      CONTENT_MATCHED            2026-07-06  (same day; FFFF/ZP absent)
#   2026-07-08   LIVE      CONTENT_MATCHED            2026-07-07  } identical copies; 07-08
#   2026-07-09   LIVE      CONTENT_MATCHED            2026-07-07  } lacks AAAA/ZP, BBBB/ZP
#   2026-07-15   LIVE      CONTENT_MATCHED            2026-07-14  } AAAA/AK bavg differs
#   2026-07-16   LIVE      CONTENT_MATCHED            2026-07-14  } -> quarantined
#   2026-08-12   LIVE      MIXED                      NULL
#   2026-09-01   LIVE      INFERRED_ONLY              NULL
#   2026-09-30   LIVE      SCAN_VERIFIED              2026-09-29  (CCCC/AK observed 0.0)

TICKERS = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"]
BROKERS = ["AK", "ZP"]
KEYS = [(t, b) for t in TICKERS for b in BROKERS]
ABSENT_0706 = ("FFFF", "ZP")                     # never observed in session 07-06
SUBSET_0708 = {("AAAA", "ZP"), ("BBBB", "ZP")}   # only the 07-09 copy has these
CONFLICT_KEY = ("AAAA", "AK")                    # bavg differs between 07-15 and 07-16
ZERO_KEY = ("CCCC", "AK")                        # FULL_CALLBACK row with bval = sval = 0


def session_values(seed):
    return {k: ((i * 7 + seed * 13) % 50 * 1e8 + 3e8, (i * 11 + seed * 5) % 40 * 1e8 + 1e8)
            for i, k in enumerate(KEYS)}


SESSIONS = {"2026-07-01": session_values(0), "2026-07-02": session_values(1),
            "2026-07-03": session_values(2), "2026-07-06": session_values(3),
            "2026-07-07": session_values(4), "2026-07-14": session_values(6),
            "2026-08-11": session_values(7), "2026-08-12": session_values(8),
            "2026-08-24": session_values(9)}


def as_live(values, keys=None):
    return {k: (round(b / 1e9, 1), round(s / 1e9, 1)) for k, (b, s) in values.items()
            if keys is None or k in keys}


SCAN_COLUMNS = """(scrape_date TEXT NOT NULL,
    broker_code TEXT NOT NULL, run_started_utc TEXT NOT NULL, status TEXT NOT NULL,
    snapshot TEXT NOT NULL, session_date TEXT, akum_rows_returned INTEGER,
    dist_rows_returned INTEGER, tracked_rows INTEGER, method TEXT NOT NULL,
    started_utc TEXT, completed_utc TEXT, detail TEXT,
    expected_session_date TEXT, calendar_version TEXT,
    PRIMARY KEY (scrape_date, broker_code, run_started_utc))"""
CALENDAR = "idx-2026-2027.v1"


def scan_run(scrape_date, session, tracked, run):
    return [(scrape_date, code, run, "OK", "PERSISTED", session, 10, 10, n,
             "dash_callback_v1", run, run, "", session, CALENDAR)
            for code, n in sorted(tracked.items())]


def make_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE broker_flow (date TEXT NOT NULL, ticker TEXT NOT NULL,
        broker_code TEXT NOT NULL, bval REAL, sval REAL, netval REAL, bavg REAL, savg REAL,
        PRIMARY KEY (date, ticker, broker_code))""")
    rows = []
    for d, scale in (("2026-07-01", 0.1), ("2026-07-02", 0.25), ("2026-07-03", -0.5)):
        rows += [(d, t, b, None, None, scale * (i + 1), None, None)
                 for i, (t, b) in enumerate(KEYS[:4])]
    live = {
        "2026-07-04": as_live(SESSIONS["2026-07-02"], KEYS[6:]),
        "2026-07-05": as_live(SESSIONS["2026-07-03"], KEYS[2:6]),
        "2026-07-06": as_live(SESSIONS["2026-07-06"], set(KEYS) - {ABSENT_0706}),
        "2026-07-08": as_live(SESSIONS["2026-07-07"], set(KEYS) - SUBSET_0708),
        "2026-07-09": as_live(SESSIONS["2026-07-07"]),
        "2026-07-15": as_live(SESSIONS["2026-07-14"]),
        "2026-07-16": as_live(SESSIONS["2026-07-14"]),
        "2026-08-12": {**as_live(SESSIONS["2026-08-11"]),
                       **{k: v for k, v in as_live(SESSIONS["2026-08-12"]).items()
                          if k[1] == "AK"}},
        "2026-09-01": {k: (v[0] + 0.7, v[1])
                       for k, v in as_live(SESSIONS["2026-08-24"]).items()},
        "2026-09-30": {k: (0.0, 0.0) if k == ZERO_KEY else (1.0, 2.0) for k in KEYS[:5]},
    }
    for d, vals in live.items():
        for (t, b), (bv, sv) in sorted(vals.items()):
            bavg = 101.0 if (d, (t, b)) == ("2026-07-16", CONFLICT_KEY) else 100.0
            rows.append((d, t, b, bv, sv, round(bv - sv, 1), bavg, 100.0))
    con.executemany("INSERT INTO broker_flow VALUES (?,?,?,?,?,?,?,?)", rows)
    con.execute("CREATE TABLE broker_flow_scan " + SCAN_COLUMNS)
    con.executemany("INSERT INTO broker_flow_scan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    scan_run("2026-09-30", "2026-09-29", {"AK": 3, "ZP": 2},
                             "2026-09-30T01:52:17+00:00") +
                    [("2026-09-30", "AK", "2026-09-30T00:10:00+00:00", "SOURCE_FAILURE",
                      "REJECTED", None, None, None, None, "dash_callback_v1", None, None,
                      "http 500", "2026-09-29", CALENDAR)])
    con.commit()
    con.close()
    return str(path)


def make_parquet(path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    cols = {c: [] for c in ("date", "ticker", "broker", "nlot", "nval", "blot", "bval",
                            "slot", "sval")}
    for s, vals in sorted(SESSIONS.items()):
        for (t, b), (bv, sv) in vals.items():
            for c, v in zip(cols, (s, t, b, 0, bv - sv, 0, bv, 0, sv)):
                cols[c].append(v)
    pq.write_table(pa.table(cols), path)
    return str(path)


class Synth:
    """The fixture database, its parquet, and its generator-built manifest,
    which is pinned as the audited anchor for these tests."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path
        self.mp = monkeypatch
        self.db = make_db(tmp_path / "neobdm.db")
        self.pin_mixed(self.db)
        self.pq = make_parquet(tmp_path / "broker_daily.parquet")
        self.manifest = bfr.build_manifest(self.db, self.pq)
        self.mpath = self.write(self.manifest, "manifest.json")
        self.pin(self.mpath)

    def pin_mixed(self, db):
        """Point the audited 2026-08-12 rerun pin at the fixture's rows (the
        real pin describes real rows no fixture reproduces). The generator and
        the reader both read bfr.AUDITED_MIXED."""
        con = bfr.connect_readonly(db)
        n, h = bfr._group_hash(con, "2026-08-12", bfr.LIVE)
        con.close()
        ev = copy.deepcopy(bfr.AUDITED_MIXED)
        ev["2026-08-12"]["rerun"].update(live_rows=n, rows_sha256=h)
        self.mp.setattr(bfr, "AUDITED_MIXED", ev)

    def pin(self, path):
        """Make the manifest at path the audited anchor."""
        with open(path, encoding="ascii") as f:
            self.mp.setattr(bfc, "AUDITED_MANIFEST_SHA256", digest(json.load(f)))
        self.mp.setattr(bfc, "AUDITED_MANIFEST_PATH", path)

    def write(self, manifest, name):
        path = str(self.dir / name)
        bfr.write_manifest(manifest, path)
        return path

    def load(self, manifest_path=None, db=None):
        return bfc.load_canonical_broker_flow(db or self.db, manifest_path or self.mpath)

    def inspect(self, manifest_path=None, db=None):
        return bfc.inspect_broker_flow_evidence(db or self.db, manifest_path or self.mpath)

    def edited(self, edit, name="edited.json"):
        """The manifest after edit(records_by_key, manifest), with summary and
        input totals recomputed so only the rule under test can object. Not
        the anchor: its digest differs."""
        m = copy.deepcopy(self.manifest)
        edit({(r["broker_flow_date"], r["regime"]): r for r in m["records"]}, m)
        try:
            m["summary"] = bfr._summary(m["records"])
            m["input"]["broker_flow"]["by_regime"] = bfr._regime_counts(m["records"])
            m["input"]["broker_flow"]["rows"] = sum(r["row_count"] for r in m["records"])
        except (KeyError, TypeError):
            pass    # an unknown class/field: the record rule itself must object
        return self.write(m, name)

    def db_copy(self, name, *sql):
        dst = str(self.dir / name)
        shutil.copy(self.db, dst)
        con = sqlite3.connect(dst)
        for s in sql:
            con.execute(s)
        con.commit()
        con.close()
        return dst


@pytest.fixture
def synth(tmp_path, monkeypatch):
    return Synth(tmp_path, monkeypatch)


def rec(synth, d, regime=bfr.LIVE):
    return {(r["broker_flow_date"], r["regime"]): r for r in synth.manifest["records"]}[(d, regime)]


def raw(db, d, key):
    con = bfr.connect_readonly(db)
    try:
        return con.execute("SELECT bval, sval, netval, bavg, savg FROM broker_flow "
                           "WHERE date = ? AND ticker = ? AND broker_code = ?",
                           (d, *key)).fetchone()
    finally:
        con.close()


def test_the_fixture_is_classified_as_designed(synth):
    want = {("2026-07-01", bfr.BACKFILL): (bfr.SOURCE_DATED_BACKFILL, "2026-07-01"),
            ("2026-07-02", bfr.BACKFILL): (bfr.SOURCE_DATED_BACKFILL, "2026-07-02"),
            ("2026-07-03", bfr.BACKFILL): (bfr.SOURCE_DATED_BACKFILL, "2026-07-03"),
            ("2026-07-04", bfr.LIVE): (bfr.CONTENT_MATCHED, "2026-07-02"),
            ("2026-07-05", bfr.LIVE): (bfr.CONTENT_MATCHED, "2026-07-03"),
            ("2026-07-06", bfr.LIVE): (bfr.CONTENT_MATCHED, "2026-07-06"),
            ("2026-07-08", bfr.LIVE): (bfr.CONTENT_MATCHED, "2026-07-07"),
            ("2026-07-09", bfr.LIVE): (bfr.CONTENT_MATCHED, "2026-07-07"),
            ("2026-07-15", bfr.LIVE): (bfr.CONTENT_MATCHED, "2026-07-14"),
            ("2026-07-16", bfr.LIVE): (bfr.CONTENT_MATCHED, "2026-07-14"),
            ("2026-08-12", bfr.LIVE): (bfr.MIXED, None),
            ("2026-09-01", bfr.LIVE): (bfr.INFERRED_ONLY, None),
            ("2026-09-30", bfr.LIVE): (bfr.SCAN_VERIFIED, "2026-09-29")}
    got = {(r["broker_flow_date"], r["regime"]): (r["date_class"], r["canonical_session_date"])
           for r in synth.manifest["records"]}
    assert got == want


# --------------------------------------------------------------------------
# Trust
# --------------------------------------------------------------------------

@pytest.mark.parametrize("acq, regime, cls, session", [
    ("2026-07-01", bfr.BACKFILL, bfr.SOURCE_DATED_BACKFILL, "2026-07-01"),
    ("2026-09-30", bfr.LIVE, bfr.SCAN_VERIFIED, "2026-09-29"),
    ("2026-07-06", bfr.LIVE, bfr.CONTENT_MATCHED, "2026-07-06"),
])
def test_each_proven_class_enters_the_trusted_view(synth, acq, regime, cls, session):
    rows = [r for r in synth.load().rows if r.acquisition_date == acq]
    assert rows, f"{cls} rows are missing from the trusted view"
    assert {(r.canonical_session_date, r.date_class, r.regime, r.evidence_level)
            for r in rows} == {(session, cls, regime, "PROVEN")}
    assert len(rows) == rec(synth, acq, regime)["row_count"]


@pytest.mark.parametrize("acq, cls", [("2026-09-01", bfr.INFERRED_ONLY),
                                      ("2026-08-12", bfr.MIXED)])
def test_unproven_classes_are_excluded_by_default_and_listed(synth, acq, cls):
    cf = synth.load()
    assert not [r for r in cf.rows if r.acquisition_date == acq or acq in r.copy_acquisition_dates]
    assert {r.date_class for r in cf.rows} <= set(bfc.TRUSTED_CLASSES)
    excluded = {e.acquisition_date: e for e in cf.excluded}
    assert set(excluded) == {"2026-08-12", "2026-09-01"}
    assert (excluded[acq].date_class, excluded[acq].row_count) == \
        (cls, rec(synth, acq)["row_count"])


def test_inspection_surfaces_excluded_rows_without_upgrading_them(synth):
    rows = synth.inspect()
    inferred = [r for r in rows if r.acquisition_date == "2026-09-01"]
    mixed = [r for r in rows if r.acquisition_date == "2026-08-12"]
    assert len(inferred) == rec(synth, "2026-09-01")["row_count"]
    assert len(mixed) == rec(synth, "2026-08-12")["row_count"]
    for r in inferred:
        assert (r.status, r.date_class, r.evidence_level, r.canonical_session_date,
                r.survivor_acquisition_date) == (bfc.EXCLUDED, bfr.INFERRED_ONLY, "INFERRED",
                                                 None, None)
        # the calendar guess is visible, labelled, and never canonical
        assert r.inferred_session_date == rec(synth, "2026-09-01")["inferred_session_date"]
    for r in mixed:
        assert (r.status, r.date_class, r.evidence_level, r.canonical_session_date,
                r.inferred_session_date) == (bfc.EXCLUDED, bfr.MIXED, "AMBIGUOUS", None, None)


# --------------------------------------------------------------------------
# Manifest validation: fail closed
# --------------------------------------------------------------------------

def test_a_db_group_without_a_manifest_record_fails_closed(synth):
    path = synth.edited(lambda recs, m: m["records"].remove(recs[("2026-07-06", bfr.LIVE)]))
    with pytest.raises(bfc.ManifestMismatch, match="2026-07-06") as e:
        synth.load(path)
    assert e.value.diff["missing"] == [("2026-07-06", bfr.LIVE)]


def test_a_new_post_snapshot_date_in_the_db_fails_closed(synth):
    db = synth.db_copy("newer.db", "INSERT INTO broker_flow VALUES "
                                   "('2026-10-01','AAAA','AK',1.0,1.0,0.0,1.0,1.0)")
    with pytest.raises(bfc.ManifestMismatch, match="2026-10-01") as e:
        synth.load(db=db)
    assert e.value.diff["missing"] == [("2026-10-01", bfr.LIVE)]


def test_a_manifest_record_whose_rows_are_gone_fails_closed(synth):
    db = synth.db_copy("gone.db", "DELETE FROM broker_flow WHERE date = '2026-07-06'")
    with pytest.raises(bfc.ManifestMismatch, match="2026-07-06") as e:
        synth.load(db=db)
    assert e.value.diff["gone"] == [("2026-07-06", bfr.LIVE)]


def test_every_difference_is_reported_together(synth):
    db = synth.db_copy("drifted.db",
                       "INSERT INTO broker_flow VALUES ('2026-10-01','AAAA','AK',1.0,1.0,0.0,1.0,1.0)",
                       "UPDATE broker_flow SET netval = 9.0 WHERE date = '2026-07-01' "
                       "AND ticker = 'AAAA' AND broker_code = 'AK'",
                       "INSERT INTO broker_flow VALUES ('2026-07-02','FFFF','ZP',NULL,NULL,1.0,NULL,NULL)")
    with pytest.raises(bfc.ManifestMismatch) as e:
        synth.load(db=db)
    assert e.value.diff == {"missing": [("2026-10-01", bfr.LIVE)], "gone": [],
                            "count_changed": [("2026-07-02", bfr.BACKFILL)],
                            "content_changed": [("2026-07-01", bfr.BACKFILL)]}


def test_duplicate_manifest_records_fail(synth):
    path = synth.edited(lambda recs, m: m["records"].append(
        copy.deepcopy(recs[("2026-07-06", bfr.LIVE)])))
    with pytest.raises(bfc.ManifestInvalid, match="duplicate"):
        synth.load(path)


def test_duplicate_json_keys_fail(synth):
    path = synth.dir / "dupkey.json"
    path.write_text('{"schema": "broker_flow_date_evidence_v1", "schema": "x"}', encoding="ascii")
    with pytest.raises(bfc.ManifestInvalid, match="duplicate"):
        synth.load(str(path))


def _set(key, **fields):
    def edit(recs, m):
        recs[key].update(fields)
    return edit


def _drop(key, field):
    def edit(recs, m):
        del recs[key][field]
    return edit


def _set_top(**fields):
    def edit(recs, m):
        m.update(fields)
    return edit


def _set_input(**fields):
    def edit(recs, m):
        m["input"].update(fields)
    return edit


def _set_cm(key, **fields):
    def edit(recs, m):
        recs[key]["content_match"].update(fields)
    return edit


L06, L08, L30, BF01 = (("2026-07-06", bfr.LIVE), ("2026-07-08", bfr.LIVE),
                       ("2026-09-30", bfr.LIVE), ("2026-07-01", bfr.BACKFILL))
INF, MIX = ("2026-09-01", bfr.LIVE), ("2026-08-12", bfr.LIVE)


@pytest.mark.parametrize("edit, why", [
    (_set_top(schema="broker_flow_date_evidence_v2"), "schema"),
    (_set_top(generator="something_else.py"), "generator"),
    (_set_top(generator_version=2), "generator_version"),
    (_set_top(records=[]), "records"),
    (_set(L06, date_class="VERIFIED_BY_HAND"), "date_class"),
    (_drop(L06, "rows_sha256"), "rows_sha256"),
    (_set(L06, regime="BOTH"), "regime"),
    (_set(L06, broker_flow_date="06/07/2026"), r"broker_flow_date '06/07/2026' is not an ISO date"),
    # malformed types fail as ManifestInvalid, never as a TypeError/AttributeError
    (_set(L06, date_class=["CONTENT_MATCHED"]), "date_class"),
    (_set(L06, date_class={}), "date_class"),
    (_set_input(content_match_evidence="AVAILABLE"), "evidence_hash"),
    (_set_input(content_match_evidence=[1]), "evidence_hash"),
    (_set_input(content_match_evidence={"status": "AVAILABLE", "sha256": 123}), "evidence_hash"),
    (_set(L06, rows_sha256="not-a-hash"), "rows_sha256"),
    (_set(L06, row_count=0), "row_count"),
    (_set(L06, row_count=True), "row_count"),
    (_set(L06, capture_class="FULL_UNIVERSE"), "capture_class"),
    # evidence level and date class must agree
    (_set(INF, evidence_level="PROVEN"), "evidence_level"),
    (_set(MIX, evidence_level="PROVEN"), "evidence_level"),
    # unproven classes never carry a canonical session
    (_set(INF, canonical_session_date="2026-08-31"), "canonical_session_date"),
    (_set(MIX, canonical_session_date="2026-08-11"), "canonical_session_date"),
    # proven classes always do, strictly ISO, consistent with their evidence
    (_set(L06, canonical_session_date=None), "canonical_session_date"),
    (_set(L06, canonical_session_date="2026-07-07"), "canonical_session_date"),
    (_set(L06, canonical_session_date="not-a-date"), "ISO canonical_session_date"),
    # date.fromisoformat accepts these on 3.11+; one session under two spellings
    # would be counted twice
    (_set(L06, canonical_session_date="20260706"), "ISO canonical_session_date"),
    (_set(L06, canonical_session_date="2026-W28-1"), "ISO canonical_session_date"),
    (_set_cm(L06, matched_session="2026-07-07"), "content_match"),
    (_set_cm(L06, full_agreement_sessions=["2026-07-06", "2026-07-07"]), "content_match"),
    (_set(L06, evidence_hash="sha256:" + "0" * 64), "evidence_hash"),
    (_set(BF01, canonical_session_date="2026-06-30"), "canonical_session_date"),
    (_set(BF01, regime=bfr.LIVE), "regime"),
    (_set(BF01, evidence_ref="somewhere else"), "evidence_ref"),
    (_set(L30, canonical_session_date="2026-09-28"), "canonical_session_date"),
    (_set(L30, date_class=bfr.CONTENT_MATCHED), "date_class"),
    (_set(L08, date_class=bfr.SCAN_VERIFIED, capture_class=bfr.FULL_CALLBACK,
          evidence_kind="broker_flow_scan"), "scan"),
])
def test_invalid_manifest_records_fail_closed(synth, edit, why):
    path = synth.edited(edit)
    with pytest.raises(bfc.ManifestInvalid, match=why):
        synth.load(path)


def test_a_canonical_session_after_the_acquisition_date_fails(synth):
    def edit(recs, m):
        r = recs[L06]
        r["canonical_session_date"] = "2026-07-07"
        r["content_match"].update(matched_session="2026-07-07",
                                  full_agreement_sessions=["2026-07-07"])
    with pytest.raises(bfc.ManifestInvalid, match="after"):
        synth.load(synth.edited(edit))


def test_a_date_with_no_window_fails_as_invalid_not_overflow(synth):
    def edit(recs, m):
        r = recs[L06]
        r.update(broker_flow_date="0001-01-05", canonical_session_date="0001-01-05")
        r["content_match"].update(matched_session="0001-01-05",
                                  full_agreement_sessions=["0001-01-05"])
    with pytest.raises(bfc.ManifestInvalid, match="window"):
        synth.load(synth.edited(edit))


def test_input_totals_must_match_the_records(synth):
    m = copy.deepcopy(synth.manifest)
    m["input"]["broker_flow"]["rows"] += 1
    with pytest.raises(bfc.ManifestInvalid, match="input.broker_flow"):
        synth.load(synth.write(m, "bad_totals.json"))


def test_even_the_anchor_cannot_match_content_outside_the_window(synth):
    def edit(recs, m):
        r = recs[L06]
        r["canonical_session_date"] = "2026-06-01"
        r["content_match"].update(matched_session="2026-06-01",
                                  full_agreement_sessions=["2026-06-01"])
    path = synth.edited(edit, "far.json")
    synth.pin(path)
    with pytest.raises(bfc.ManifestInvalid, match="window"):
        synth.load(path)


def test_a_backfill_record_after_backfill_end_fails(synth):
    db = synth.db_copy("late_backfill.db", "INSERT INTO broker_flow VALUES "
                                           "('2026-07-07','AAAA','AK',NULL,NULL,1.0,NULL,NULL)")
    path = synth.write(bfr.build_manifest(db, synth.pq), "late_backfill.json")
    with pytest.raises(bfc.ManifestInvalid, match="BACKFILL_END"):
        synth.load(path, db=db)


def test_backfill_end_is_the_backfill_writers_own_constant():
    with open(os.path.join(HERE, "backfill_inventory.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    values = [n.value.value for n in tree.body if isinstance(n, ast.Assign)
              and [t.id for t in n.targets if isinstance(t, ast.Name)] == ["BACKFILL_END"]]
    assert values == [bfc.BACKFILL_END]


def test_a_manifest_that_is_not_json_fails(synth):
    path = synth.dir / "broken.json"
    path.write_text("{not json", encoding="ascii")
    with pytest.raises(bfc.ManifestInvalid):
        synth.load(str(path))


def test_a_missing_manifest_file_fails(synth):
    with pytest.raises(FileNotFoundError):
        synth.load(str(synth.dir / "nope.json"))


def test_a_summary_that_disagrees_with_the_records_fails(synth):
    m = copy.deepcopy(synth.manifest)
    m["summary"]["by_date_class"][bfr.CONTENT_MATCHED]["dates"] += 1
    with pytest.raises(bfc.ManifestInvalid, match="summary"):
        synth.load(synth.write(m, "bad_summary.json"))


# MIXED is decided by AUDITED_MIXED in both directions.

def test_mixed_outside_the_audited_quarantine_fails(synth, monkeypatch):
    ev = copy.deepcopy(bfr.AUDITED_MIXED)
    ev.pop("2026-08-12")
    monkeypatch.setattr(bfr, "AUDITED_MIXED", ev)
    with pytest.raises(bfc.ManifestInvalid, match="AUDITED_MIXED"):
        synth.load()


def test_mixed_must_be_the_audited_rerun_state(synth, monkeypatch):
    ev = copy.deepcopy(bfr.AUDITED_MIXED)
    ev["2026-08-12"]["rerun"]["rows_sha256"] = "0" * 64
    monkeypatch.setattr(bfr, "AUDITED_MIXED", ev)
    with pytest.raises(bfc.ManifestInvalid, match="AUDITED_MIXED"):
        synth.load()


@pytest.mark.parametrize("fields", [
    dict(date_class=bfr.INFERRED_ONLY, evidence_level="INFERRED",
         evidence_kind="idx_calendar_inference", inferred_session_date="2026-08-11"),
    dict(date_class=bfr.CONTENT_MATCHED, evidence_level="PROVEN", evidence_kind="content_match",
         canonical_session_date="2026-08-11", inferred_session_date="2026-08-11"),
])
def test_an_audited_mixed_date_cannot_be_relabelled(synth, fields):
    def edit(recs, m):
        r = recs[MIX]
        r.update(fields)
        r["content_match"].update(matched_session=r["canonical_session_date"],
                                  full_agreement_sessions=[r["canonical_session_date"]]
                                  if r["canonical_session_date"] else [])
        r["evidence_hash"] = "sha256:" + m["input"]["content_match_evidence"]["sha256"]
    with pytest.raises(bfc.ManifestInvalid, match="AUDITED_MIXED"):
        synth.load(synth.edited(edit))


# The audited manifest is recognised by its content, never by its label, and
# a manifest that is not it cannot assert what the reader cannot re-verify.

def _forge_content_match(key, session):
    def edit(recs, m):
        r = recs[key]
        r.update(date_class=bfr.CONTENT_MATCHED, evidence_level="PROVEN",
                 evidence_kind="content_match", capture_class=bfr.DOM_TOP15,
                 canonical_session_date=session, notes=[],
                 evidence_ref="broker_flow_regime.py content match",
                 evidence_hash="sha256:" + m["input"]["content_match_evidence"]["sha256"])
        r["content_match"].update(matched_session=session, full_agreement_sessions=[session])
    return edit


@pytest.mark.parametrize("key, session", [
    (INF, "2026-08-24"),          # INFERRED_ONLY upgraded to proven
    (L08, "2026-07-08"),          # a proven record moved to a session never captured
    (L06, "2026-06-30"),          # a same-day capture moved back
])
def test_a_manifest_other_than_the_audited_one_cannot_assert_a_content_match(synth, key, session):
    with pytest.raises(bfc.ManifestInvalid, match="CONTENT_MATCHED"):
        synth.load(synth.edited(_forge_content_match(key, session)))


@pytest.mark.parametrize("label", [bfr.AUDITED_SNAPSHOT["name"],
                                   bfr.AUDITED_SNAPSHOT["name"] + " "])
def test_the_audited_label_grants_nothing(synth, label):
    def edit(recs, m):
        _forge_content_match(INF, "2026-08-24")(recs, m)
        m["input"]["snapshot"] = label
    with pytest.raises(bfc.ManifestInvalid, match="audited|CONTENT_MATCHED"):
        synth.load(synth.edited(edit))


def test_a_manifest_labelled_audited_must_be_the_audited_content(synth):
    """Even with no forged claim: the audited label on other content is refused."""
    path = synth.edited(lambda recs, m: m["input"].update(snapshot=bfr.AUDITED_SNAPSHOT["name"]))
    with pytest.raises(bfc.ManifestInvalid, match="labelled"):
        synth.load(path)


def test_the_anchor_itself_must_be_intact(synth):
    with open(synth.mpath, "a", encoding="ascii") as f:
        f.write(" ")                                   # still valid JSON, same digest
    assert synth.load().audited
    derived = synth.edited(lambda recs, m: m["input"].update(snapshot="derived"), "derived.json")
    assert not synth.load(derived).audited
    m = copy.deepcopy(synth.manifest)
    m["records"][0]["notes"] = ["edited"]
    bfr.write_manifest(m, synth.mpath)                  # the anchor file itself altered
    with pytest.raises(bfc.ManifestInvalid, match="audited"):
        synth.load(derived)


def newer_db(synth):
    """The fixture after one more nightly PR #72 capture: 2026-10-01 holds the
    2026-09-30 session, proven by its own broker_flow_scan run."""
    db = synth.db_copy("newer.db",
                       "INSERT INTO broker_flow VALUES ('2026-10-01','AAAA','AK',3.0,1.0,2.0,100.0,100.0)",
                       "INSERT INTO broker_flow VALUES ('2026-10-01','AAAA','ZP',1.0,3.0,-2.0,100.0,100.0)")
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO broker_flow_scan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    scan_run("2026-10-01", "2026-09-30", {"AK": 1, "ZP": 1},
                             "2026-10-01T00:30:00+00:00"))
    con.commit()
    con.close()
    return db


def test_a_manifest_rebuilt_for_a_newer_db_is_read_with_its_claims_reverified(synth):
    db = newer_db(synth)
    path = synth.write(bfr.build_manifest(db, synth.pq), "rebuilt.json")
    before = synth.load()
    cf = synth.load(path, db=db)
    assert not cf.audited
    assert cf.sessions["2026-09-30"] == bfr.FULL_CALLBACK
    assert [r for r in cf.rows if r.canonical_session_date != "2026-09-30"] == list(before.rows)
    # built without the parquet, the content-matched dates fall to INFERRED_ONLY. The
    # only rows that appear are backfill sessions no longer contested by a proven live copy.
    weaker = synth.write(bfr.build_manifest(db, None), "no_parquet.json")
    thin = synth.load(weaker, db=db)
    assert {r.date_class for r in thin.rows} == {bfr.SOURCE_DATED_BACKFILL, bfr.SCAN_VERIFIED}
    extra = set(thin.rows) - set(cf.rows)
    assert {r.canonical_session_date for r in extra} == {"2026-07-02", "2026-07-03"}
    assert {r.date_class for r in extra} == {bfr.SOURCE_DATED_BACKFILL}


def test_a_date_proven_by_a_scan_run_must_be_labelled_scan_verified(synth):
    def downgrade(recs, m):
        r = recs[L30]
        r.update(date_class=bfr.INFERRED_ONLY, evidence_level="INFERRED",
                 capture_class=bfr.DOM_TOP15, evidence_kind="idx_calendar_inference",
                 canonical_session_date=None)
        del r["scan"]
    with pytest.raises(bfc.ManifestMismatch, match="scan"):
        synth.load(synth.edited(downgrade))


def test_trust_is_proven_or_nothing(synth):
    with pytest.raises(ValueError, match="inspect_broker_flow_evidence"):
        bfc.load_canonical_broker_flow(synth.db, synth.mpath, trust="inferred")
    assert bfc.load_canonical_broker_flow(synth.db, synth.mpath, trust="proven").rows


def test_a_scan_verified_record_must_name_the_runs_session_and_start(synth):
    def moved(recs, m):
        recs[L30]["canonical_session_date"] = "2026-09-28"
        recs[L30]["scan"]["session_date"] = "2026-09-28"
    with pytest.raises(bfc.ManifestMismatch, match="run proves"):
        synth.load(synth.edited(moved))

    def rerun(recs, m):
        recs[L30]["scan"]["run_started_utc"] = "2026-09-30T05:00:00+00:00"
    with pytest.raises(bfc.ManifestMismatch, match="run proves"):
        synth.load(synth.edited(rerun, "rerun.json"))


def test_a_rebuilt_content_match_with_a_new_hash_is_not_the_audited_record(synth):
    db = synth.db_copy("bavg.db", "UPDATE broker_flow SET bavg = 100.5 WHERE date = '2026-07-06' "
                                  "AND ticker = 'AAAA' AND broker_code = 'AK'")
    m = bfr.build_manifest(db, synth.pq)
    r = {(x["broker_flow_date"], x["regime"]): x for x in m["records"]}[L06]
    assert (r["date_class"], r["canonical_session_date"], r["row_count"]) == \
        (bfr.CONTENT_MATCHED, "2026-07-06", rec(synth, "2026-07-06")["row_count"])
    assert r["rows_sha256"] != rec(synth, "2026-07-06")["rows_sha256"]
    with pytest.raises(bfc.ManifestInvalid, match="CONTENT_MATCHED"):
        synth.load(synth.write(m, "bavg.json"), db=db)


def test_a_scan_verified_claim_without_a_scan_run_fails(synth):
    def edit(recs, m):
        recs[L08].update(date_class=bfr.SCAN_VERIFIED, capture_class=bfr.FULL_CALLBACK,
                         evidence_kind="broker_flow_scan",
                         scan={"session_date": "2026-07-07"},
                         evidence_hash="sha256:" + "0" * 64)
    with pytest.raises(bfc.ManifestMismatch, match="scan"):
        synth.load(synth.edited(edit))


# --------------------------------------------------------------------------
# Join / row integrity
# --------------------------------------------------------------------------

def test_every_trusted_row_joins_exactly_to_its_evidence_record(synth):
    recs = {(r["broker_flow_date"], r["regime"]): r for r in synth.manifest["records"]}
    for row in synth.load().rows:
        r = recs[(row.acquisition_date, row.regime)]
        assert (row.canonical_session_date, row.date_class, row.capture_class,
                row.evidence_level, row.rows_sha256) == \
            (r["canonical_session_date"], r["date_class"], r["capture_class"],
             r["evidence_level"], r["rows_sha256"])


def test_inspection_returns_every_raw_row_exactly_once(synth):
    con = sqlite3.connect(synth.db)
    want = sorted(con.execute("SELECT date, ticker, broker_code, bval, sval, netval, bavg, savg "
                              "FROM broker_flow").fetchall())
    con.close()
    got = sorted((r.acquisition_date, r.ticker, r.broker_code, r.bval, r.sval, r.netval,
                  r.bavg, r.savg) for r in synth.inspect())
    assert got == want


@pytest.mark.parametrize("sql, match", [
    ("DELETE FROM broker_flow WHERE date = '2026-07-06' AND ticker = 'AAAA' AND broker_code = 'AK'",
     "count_changed"),
    ("UPDATE broker_flow SET netval = netval + 0.1 WHERE date = '2026-07-06' AND ticker = 'AAAA' "
     "AND broker_code = 'AK'", "content_changed"),
    ("UPDATE broker_flow SET bavg = 99.0 WHERE date = '2026-09-01' AND ticker = 'AAAA' "
     "AND broker_code = 'AK'", "content_changed"),
    ("INSERT INTO broker_flow VALUES ('2026-07-01','FFFF','ZP',NULL,NULL,1.0,NULL,NULL)",
     "count_changed"),
    # backfill values are healed nightly: a DB whose backfill differs needs its own manifest
    ("UPDATE broker_flow SET netval = 9.0 WHERE date = '2026-07-01' AND ticker = 'AAAA' "
     "AND broker_code = 'AK'", "content_changed"),
])
def test_rows_that_differ_from_the_manifest_fail_closed(synth, sql, match):
    db = synth.db_copy("drifted.db", sql)
    with pytest.raises(bfc.ManifestMismatch, match=match):
        synth.load(db=db)


@pytest.mark.parametrize("sql", [
    "UPDATE broker_flow_scan SET session_date = '2026-09-28', expected_session_date = "
    "'2026-09-28' WHERE snapshot = 'PERSISTED'",
    "UPDATE broker_flow_scan SET tracked_rows = 4 WHERE snapshot = 'PERSISTED' AND broker_code = 'AK'",
    "DELETE FROM broker_flow_scan WHERE snapshot = 'PERSISTED' AND broker_code = 'ZP'",
    "UPDATE broker_flow_scan SET detail = 'edited' WHERE snapshot = 'PERSISTED' AND broker_code = 'AK'",
])
def test_scan_evidence_that_no_longer_proves_the_record_fails_closed(synth, sql):
    db = synth.db_copy("scan_drift.db", sql)
    with pytest.raises(bfc.ManifestMismatch, match="scan"):
        synth.load(db=db)


def test_acquisition_date_is_preserved_next_to_the_canonical_session(synth):
    cf = synth.load()
    same_day = cf.get("2026-07-06", "AAAA", "AK")
    prior = cf.get("2026-07-07", "CCCC", "AK")
    scan = cf.get("2026-09-29", "AAAA", "AK")
    assert (same_day.acquisition_date, same_day.canonical_session_date) == ("2026-07-06", "2026-07-06")
    assert (prior.acquisition_date, prior.canonical_session_date) == ("2026-07-08", "2026-07-07")
    assert (scan.acquisition_date, scan.canonical_session_date) == ("2026-09-30", "2026-09-29")
    for r in cf.rows:
        for v in (r.acquisition_date, r.canonical_session_date, *r.copy_acquisition_dates):
            assert type(v) is str and ISO.fullmatch(v)


def test_the_canonical_session_comes_only_from_the_manifest(synth, monkeypatch):
    """An anchor that restates 07-06 as the 06-30 session: the reader follows
    it, not the raw date (07-06) and not the calendar (07-03), and consults the
    calendar only inside PR #74's re-proof of the scan run (scrape 09-30)."""
    def restate(recs, m):
        r = recs[L06]
        r["canonical_session_date"] = "2026-06-30"
        r["content_match"].update(matched_session="2026-06-30",
                                  full_agreement_sessions=["2026-06-30"])
    path = synth.edited(restate, "restated.json")
    synth.pin(path)
    asked = []
    real = bfr.idx_calendar.latest_idx_session_before
    monkeypatch.setattr(bfr.idx_calendar, "latest_idx_session_before",
                        lambda d: asked.append(d) or real(d))
    cf = synth.load(path)
    rows = [r for r in cf.rows if r.acquisition_date == "2026-07-06"]
    assert len(rows) == 11
    assert {r.canonical_session_date for r in rows} == {"2026-06-30"}
    assert "2026-07-06" not in cf.sessions
    assert set(asked) == {date(2026, 9, 30)}


# --------------------------------------------------------------------------
# Dedupe / session collapse
# --------------------------------------------------------------------------

def test_two_acquisition_dates_of_one_session_give_one_row_per_key(synth):
    cf = synth.load()
    session = [r for r in cf.rows if r.canonical_session_date == "2026-07-07"]
    assert len(session) == len(KEYS) == 12          # union, not 10 + 12
    assert len({(r.ticker, r.broker_code) for r in session}) == 12
    assert cf.duplicates_collapsed == 10


def test_the_survivor_is_the_earliest_acquisition_date_holding_the_key(synth):
    cf = synth.load()
    kept = cf.get("2026-07-07", "CCCC", "AK")
    only_later = cf.get("2026-07-07", "AAAA", "ZP")
    assert (kept.acquisition_date, kept.copy_acquisition_dates) == \
        ("2026-07-08", ("2026-07-08", "2026-07-09"))
    assert (only_later.acquisition_date, only_later.copy_acquisition_dates) == \
        ("2026-07-09", ("2026-07-09",))
    assert kept.rows_sha256 == rec(synth, "2026-07-08")["rows_sha256"]
    assert only_later.rows_sha256 == rec(synth, "2026-07-09")["rows_sha256"]


def test_dedupe_does_not_depend_on_manifest_record_order(synth):
    path = synth.edited(lambda recs, m: random.Random(7).shuffle(m["records"]), "shuffled.json")
    with open(path, encoding="ascii") as f:
        order = [r["broker_flow_date"] for r in json.load(f)["records"]]
    assert order != sorted(order)
    shuffled, plain = synth.load(path), synth.load()
    assert (shuffled.rows, shuffled.quarantined) == (plain.rows, plain.quarantined)


def test_raw_copies_are_kept_apart_from_the_canonical_view(synth):
    rows = {(r.acquisition_date, r.ticker, r.broker_code): r for r in synth.inspect()}
    assert rows[("2026-07-08", "CCCC", "AK")].status == bfc.CANONICAL
    dup = rows[("2026-07-09", "CCCC", "AK")]
    assert (dup.status, dup.survivor_acquisition_date) == (bfc.DUPLICATE, "2026-07-08")
    assert rows[("2026-07-09", "AAAA", "ZP")].status == bfc.CANONICAL
    canonical = {(r.acquisition_date, r.ticker, r.broker_code) for r in synth.load().rows}
    assert canonical == {k for k, r in rows.items() if r.status == bfc.CANONICAL}


def test_every_raw_row_is_accounted_for_once(synth):
    cf = synth.load()
    s = cf.summary()["accounting"]
    assert s == {"canonical": 32, "duplicate": 10, "quarantined": 4 + 6 + 4 + 4 + 12 + 12,
                 "excluded": 12 + 12, "raw": 32 + 10 + 42 + 24}
    assert sum(v for k, v in s.items() if k != "raw") == s["raw"] == len(synth.inspect())


def test_duplicate_copies_are_never_summed(synth):
    cf = synth.load()
    for key in KEYS:
        row = cf.get("2026-07-07", *key)
        assert (row.bval, row.sval, row.netval, row.bavg, row.savg) == raw(synth.db, "2026-07-09", key)


def test_conflicting_live_copies_quarantine_the_session(synth):
    cf = synth.load()
    assert "2026-07-14" not in cf.sessions
    q = {x.canonical_session_date: x for x in cf.quarantined}["2026-07-14"]
    assert [a for a, *_ in q.records] == ["2026-07-15", "2026-07-16"]
    assert (q.reasons, q.conflicting_keys, q.sample_keys, q.rows) == \
        ((bfc.VALUES_CONFLICT,), 1, (CONFLICT_KEY,), 24)
    statuses = {r.status for r in synth.inspect() if r.acquisition_date in ("2026-07-15", "2026-07-16")}
    assert statuses == {bfc.QUARANTINED}


@pytest.mark.parametrize("change", ["bval = bval + 0.04", "sval = sval + 0.04",
                                    "netval = netval + 0.04", "bavg = bavg + 1",
                                    "savg = savg + 1", "savg = NULL"])
def test_a_difference_in_any_value_quarantines_the_session(synth, change):
    """Every raw field takes part in the identity of a copy (0.04 stays inside
    the content-match tolerance, so the session is still proven)."""
    db = synth.db_copy("one_field.db", f"UPDATE broker_flow SET {change} WHERE date = "
                                       "'2026-07-09' AND ticker = 'CCCC' AND broker_code = 'AK'")
    path = synth.write(bfr.build_manifest(db, synth.pq), "one_field.json")
    synth.pin(path)
    cf = synth.load(path, db=db)
    q = {x.canonical_session_date: x for x in cf.quarantined}["2026-07-07"]
    assert (q.reasons, q.conflicting_keys, q.sample_keys) == \
        ((bfc.VALUES_CONFLICT,), 1, (("CCCC", "AK"),))
    with pytest.raises(bfc.SessionNotCovered):
        cf.get("2026-07-07", "CCCC", "AK")


def test_a_backfill_and_a_live_copy_measuring_shared_keys_differently_are_quarantined(synth):
    cf = synth.load()
    q = {x.canonical_session_date: x for x in cf.quarantined}["2026-07-03"]
    assert [(a, regime) for a, regime, *_ in q.records] == \
        [("2026-07-03", bfr.BACKFILL), ("2026-07-05", bfr.LIVE)]
    assert q.reasons == (bfc.CAPTURE_CLASSES_DIFFER, bfc.VALUES_CONFLICT)
    assert q.conflicting_keys == 2                     # KEYS[2:4] are in both


def test_capture_classes_are_never_merged_even_without_a_shared_key(synth):
    cf = synth.load()
    q = {x.canonical_session_date: x for x in cf.quarantined}["2026-07-02"]
    assert [(a, regime) for a, regime, *_ in q.records] == \
        [("2026-07-02", bfr.BACKFILL), ("2026-07-04", bfr.LIVE)]
    assert (q.reasons, q.conflicting_keys) == ((bfc.CAPTURE_CLASSES_DIFFER,), 0)
    assert set(cf.sessions) == {"2026-07-01", "2026-07-06", "2026-07-07", "2026-09-29"}


# --------------------------------------------------------------------------
# Missingness
# --------------------------------------------------------------------------

def test_a_broker_that_was_not_observed_stays_missing(synth):
    cf = synth.load()
    assert cf.get("2026-07-06", *ABSENT_0706) is None
    assert not [r for r in cf.rows if r.canonical_session_date == "2026-07-06"
                and (r.ticker, r.broker_code) == ABSENT_0706]


@pytest.mark.parametrize("session, status", [
    ("2026-07-03", bfc.QUARANTINED),     # conflicting copies
    ("2026-07-10", bfc.NOT_COVERED),     # never captured
    ("2026-08-24", bfc.NOT_COVERED),     # only an INFERRED_ONLY guess
    ("2026-08-11", bfc.NOT_COVERED),     # only inside MIXED 08-12
])
def test_an_uncovered_session_is_not_an_absent_broker(synth, session, status):
    cf = synth.load()
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get(session, "AAAA", "AK")
    assert (e.value.session, e.value.status) == (session, status)
    assert isinstance(e.value, LookupError)


def test_the_covered_sessions_carry_their_capture_class(synth):
    assert synth.load().sessions == {"2026-07-01": bfr.SELECTOR_UNION_BACKFILL,
                                     "2026-07-06": bfr.DOM_TOP15,
                                     "2026-07-07": bfr.DOM_TOP15,
                                     "2026-09-29": bfr.FULL_CALLBACK}


def test_no_synthetic_rows_are_created(synth):
    cf = synth.load()
    session = {(r.ticker, r.broker_code) for r in cf.rows if r.canonical_session_date == "2026-07-06"}
    assert session == set(KEYS) - {ABSENT_0706}
    assert len(cf.rows) == 4 + 11 + 12 + 5      # 07-01, 07-06, 07-07, 09-29


def test_an_observed_zero_is_not_absence(synth):
    zero = synth.load().get("2026-09-29", *ZERO_KEY)
    assert zero is not None and zero.capture_class == bfr.FULL_CALLBACK
    assert (zero.bval, zero.sval, zero.netval) == (0.0, 0.0, 0.0)


def test_null_fields_stay_null(synth):
    row = synth.load().get("2026-07-01", "AAAA", "AK")
    assert (row.bval, row.sval, row.bavg, row.savg) == (None, None, None, None)
    assert row.netval == 0.1
    assert row.capture_class == bfr.SELECTOR_UNION_BACKFILL


# --------------------------------------------------------------------------
# Read-only safety
# --------------------------------------------------------------------------

def sidecars(db):
    return [p for p in (db + "-wal", db + "-journal", db + "-shm") if os.path.exists(p)]


def schema_of(db):
    con = bfr.connect_readonly(db)
    try:
        return con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        con.close()


def test_reading_leaves_the_database_and_manifest_byte_identical(synth):
    db_before, m_before, schema_before = sha(synth.db), sha(synth.mpath), schema_of(synth.db)
    cf = synth.load()
    synth.inspect()
    assert sha(synth.db) == db_before == cf.db_sha256
    assert sha(synth.mpath) == m_before
    assert schema_of(synth.db) == schema_before
    assert sidecars(synth.db) == []


def test_the_reader_opens_sqlite_only_immutable_and_only_reads(synth, monkeypatch):
    opened, statements = [], []
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append((str(database), kwargs.get("uri")))
        con = real_connect(database, *args, **kwargs)
        con.set_trace_callback(statements.append)
        return con
    monkeypatch.setattr(sqlite3, "connect", spy)
    synth.load()
    synth.inspect()
    assert opened and all(uri and db.endswith("?mode=ro&immutable=1") for db, uri in opened)
    assert statements
    assert all(s.lstrip().upper().startswith(("SELECT", "PRAGMA TABLE_INFO")) for s in statements)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_a_database_that_is_not_quiescent_is_refused(synth, suffix):
    open(synth.db + suffix, "wb").close()
    with pytest.raises(bfc.SourceStateError, match=suffix):
        synth.load()


def test_a_database_that_changes_while_it_is_read_is_refused(synth, monkeypatch):
    real = bfr._scan_rows

    def touch_then_read(con):
        st = os.stat(synth.db)
        os.utime(synth.db, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        return real(con)
    monkeypatch.setattr(bfr, "_scan_rows", touch_then_read)
    with pytest.raises(bfc.SourceStateError, match="changed"):
        synth.load()


def test_a_database_with_an_unknown_file_format_is_refused(synth):
    path = synth.db_copy("format.db")
    with open(path, "r+b") as f:
        f.seek(18)
        f.write(bytes([3, 1]))
    with pytest.raises(bfc.SourceStateError, match="format"):
        synth.load(db=path)


@pytest.mark.parametrize("damage", ["no_table", "corrupt"])
def test_an_unreadable_database_is_a_source_error(synth, damage):
    if damage == "no_table":
        path = synth.db_copy("no_table.db", "DROP TABLE broker_flow")
    else:
        path = synth.db_copy("corrupt.db")
        with open(path, "r+b") as f:
            f.seek(100)
            f.write(b"\xff" * 3000)
    with pytest.raises(bfc.SourceStateError, match="cannot be read"):
        synth.load(db=path)
    assert bfc.main(["--db", path, "--manifest", synth.mpath]) == 2


def test_path_objects_are_accepted(synth):
    import pathlib
    cf = bfc.load_canonical_broker_flow(pathlib.Path(synth.db), pathlib.Path(synth.mpath))
    assert cf.rows == synth.load().rows
    assert len(bfc.inspect_broker_flow_evidence(pathlib.Path(synth.db),
                                                pathlib.Path(synth.mpath))) == 108


@pytest.mark.skipif(os.name != "nt", reason="Windows strips a trailing dot from file names")
def test_another_spelling_of_the_file_cannot_skip_the_sidecar_check(synth):
    open(synth.db + "-journal", "wb").close()
    with pytest.raises(bfc.SourceStateError, match="-journal"):
        synth.load(db=synth.db + ".")


def test_a_file_that_is_not_sqlite_is_refused(synth):
    path = synth.dir / "not.db"
    path.write_bytes(b"x" * 200)
    with pytest.raises(bfc.SourceStateError, match="SQLite"):
        synth.load(db=str(path))


# --------------------------------------------------------------------------
# Real audited data: committed manifest + neobdm.db at the audited commit
# --------------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True)


@pytest.fixture(scope="session")
def baseline_db(tmp_path_factory):
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


@pytest.fixture(scope="session")
def audited(baseline_db):
    if not os.path.exists(COMMITTED):
        pytest.skip("committed manifest not present")
    with open(COMMITTED, encoding="ascii") as f:
        manifest = json.load(f)
    before = (sha(baseline_db), sha(COMMITTED))
    cf = bfc.load_canonical_broker_flow(baseline_db, COMMITTED)
    inspected = bfc.inspect_broker_flow_evidence(baseline_db, COMMITTED)
    assert (sha(baseline_db), sha(COMMITTED)) == before
    assert sidecars(baseline_db) == []
    return manifest, cf, inspected


# Hand-derived from the committed manifest and the audited rows (see HANDOFF,
# "Lampiran T"): PROVEN = 218 SOURCE_DATED_BACKFILL + 1 SCAN_VERIFIED + 49
# CONTENT_MATCHED records = 225,237 raw rows. Session 2026-07-03 is held by
# BACKFILL 07-03 (1,034 rows) and LIVE 07-05 (218 rows): two capture classes,
# and their 168 shared keys all differ -> quarantined. The rest collapse to 250
# sessions and 220,343 rows: 217 backfill sessions / 211,805 rows, 32
# CONTENT_MATCHED / 7,573 and 1 SCAN_VERIFIED / 965; 3,642 identical copies.
REAL = {"sessions": 250, "rows": 220_343, "backfill_rows": 211_805, "live_rows": 8_538,
        "live_sessions": 33, "excluded_records": 37, "excluded_rows": 7_256,
        "duplicates": 3_642, "quarantined_rows": 1_252, "raw_rows": 232_493}
SAME_DAY = ["2026-07-06", "2026-07-07", "2026-07-09", "2026-07-10", "2026-07-13"]
# Union of the identical copies per session (08-22 holds 202 of 08-21's 211).
COPIES = {"2026-07-07": 208, "2026-07-10": 214, "2026-07-13": 230, "2026-07-17": 223,
          "2026-07-24": 260, "2026-07-31": 245, "2026-08-07": 256, "2026-08-14": 217,
          "2026-08-21": 211}


def test_real_trusted_view_has_the_audited_counts(audited):
    manifest, cf, _ = audited
    assert len(cf.sessions) == REAL["sessions"]
    assert len(cf.rows) == REAL["rows"]
    live = [r for r in cf.rows if r.regime == bfr.LIVE]
    assert len(cf.rows) - len(live) == REAL["backfill_rows"]
    assert (len(live), len({r.canonical_session_date for r in live})) == \
        (REAL["live_rows"], REAL["live_sessions"])
    assert len({(r.canonical_session_date, r.ticker, r.broker_code) for r in cf.rows}) == len(cf.rows)
    assert {r.date_class for r in cf.rows} == set(bfc.TRUSTED_CLASSES)
    from collections import Counter
    assert Counter(cf.sessions.values()) == {bfr.SELECTOR_UNION_BACKFILL: 217,
                                             bfr.DOM_TOP15: 32, bfr.FULL_CALLBACK: 1}


def test_real_identity_is_the_audited_snapshot(audited, baseline_db):
    _, cf, _ = audited
    blob = git("cat-file", "blob", f"HEAD:{bfr.MANIFEST_PATH.replace(os.sep, '/')}").stdout
    assert cf.audited and cf.snapshot == bfr.AUDITED_SNAPSHOT["name"]
    assert cf.manifest_sha256 == hashlib.sha256(blob).hexdigest()     # line-ending independent
    assert cf.db_sha256 == sha(baseline_db)


def test_real_every_raw_row_is_accounted_for_once(audited):
    manifest, cf, inspected = audited
    by_class = manifest["summary"]["by_date_class"]
    assert len(inspected) == REAL["raw_rows"]
    for cls, c in by_class.items():
        assert len([r for r in inspected if r.date_class == cls]) == c["rows"]
    proven = sum(by_class[c]["rows"] for c in bfc.TRUSTED_CLASSES)
    assert len([r for r in inspected if r.status != bfc.EXCLUDED]) == proven == 225_237
    assert cf.summary()["accounting"] == {
        "canonical": REAL["rows"], "duplicate": REAL["duplicates"],
        "quarantined": REAL["quarantined_rows"], "excluded": REAL["excluded_rows"],
        "raw": REAL["raw_rows"]}


def test_real_exclusions_and_quarantine(audited):
    _, cf, inspected = audited
    assert (len(cf.excluded), sum(e.row_count for e in cf.excluded)) == \
        (REAL["excluded_records"], REAL["excluded_rows"])
    assert {e.date_class for e in cf.excluded} == {bfr.INFERRED_ONLY, bfr.MIXED}
    [q] = cf.quarantined
    assert (q.canonical_session_date, q.reasons, q.conflicting_keys, q.rows) == \
        ("2026-07-03", (bfc.CAPTURE_CLASSES_DIFFER, bfc.VALUES_CONFLICT), 168, 1_252)
    assert [(a, regime, cls) for a, regime, cls, *_ in q.records] == \
        [("2026-07-03", bfr.BACKFILL, bfr.SOURCE_DATED_BACKFILL),
         ("2026-07-05", bfr.LIVE, bfr.CONTENT_MATCHED)]
    assert len([r for r in inspected if r.status == bfc.QUARANTINED]) == 1_034 + 218
    with pytest.raises(bfc.SessionNotCovered):
        cf.get("2026-07-03", "BNBR", "AK")


def test_real_mixed_dates_never_reach_the_trusted_view(audited):
    _, cf, inspected = audited
    for d in ("2026-08-12", "2026-08-27"):
        assert not [r for r in cf.rows if r.acquisition_date == d or d in r.copy_acquisition_dates]
        assert {r.status for r in inspected if r.acquisition_date == d} == {bfc.EXCLUDED}
        assert {e.date_class for e in cf.excluded if e.acquisition_date == d} == {bfr.MIXED}
    # the 08-12 SESSION is proven through 08-13; only the MIXED acquisition is withheld
    assert {r.acquisition_date for r in cf.rows if r.canonical_session_date == "2026-08-12"} == \
        {"2026-08-13"}
    assert "2026-08-11" not in cf.sessions and "2026-08-27" not in cf.sessions


def test_real_inferred_only_dates_never_reach_the_trusted_view(audited):
    manifest, cf, _ = audited
    inferred = {r["broker_flow_date"] for r in manifest["records"]
                if r["date_class"] == bfr.INFERRED_ONLY}
    assert len(inferred) == 35
    assert not [r for r in cf.rows if r.acquisition_date in inferred
                or inferred & set(r.copy_acquisition_dates)]
    assert max(cf.sessions) == "2026-09-29"
    assert not [s for s in cf.sessions if "2026-08-21" < s < "2026-09-29"]


def test_real_same_day_dates_keep_their_proven_sessions(audited):
    _, cf, _ = audited
    for d in SAME_DAY:
        rows = [r for r in cf.rows if r.acquisition_date == d]
        assert rows and {r.canonical_session_date for r in rows} == {d}
    assert "2026-07-08" not in cf.sessions                   # never captured
    with pytest.raises(bfc.SessionNotCovered):
        cf.get("2026-07-08", "BBCA", "AK")


def test_real_observed_zero_rows_are_rows(audited):
    """A both-zero LIVE row with an average price is an observed value rounded
    below the 0.05bn display unit: returned as a row, never confused with
    absence."""
    _, cf, _ = audited
    zeros = [r for r in cf.rows if r.regime == bfr.LIVE and r.bval == 0 and r.sval == 0
             and (r.bavg or 0) > 0]
    assert zeros
    for r in zeros[:50]:
        assert cf.get(r.canonical_session_date, r.ticker, r.broker_code) is r


def test_real_duplicate_session_copies_are_counted_once(audited):
    manifest, cf, inspected = audited
    held = manifest["summary"]["live_sessions_held_by_several_dates"]
    assert set(held) == set(COPIES)     # LIVE-only: the 07-03 BACKFILL/LIVE pair is not listed
    for session, n in COPIES.items():
        rows = [r for r in cf.rows if r.canonical_session_date == session]
        assert len(rows) == n
        assert {r.acquisition_date for r in rows} <= set(held[session])
        assert {a for r in rows for a in r.copy_acquisition_dates} == set(held[session])
    by_key = {(r.acquisition_date, r.ticker, r.broker_code): r.status for r in inspected}
    assert by_key[("2026-08-23", "BNBR", "AO")] == bfc.CANONICAL      # absent from 08-22
    assert cf.get("2026-08-21", "BNBR", "AO").copy_acquisition_dates == ("2026-08-23", "2026-08-24")
    assert {r.acquisition_date for r in cf.rows if r.canonical_session_date == "2026-08-21"} == \
        {"2026-08-22", "2026-08-23"}


def test_real_audited_manifest_cannot_be_edited(audited, baseline_db, tmp_path):
    manifest = copy.deepcopy(audited[0])
    manifest["records"][0]["notes"] = ["edited by hand"]
    path = str(tmp_path / "edited_audited.json")
    bfr.write_manifest(manifest, path)
    with pytest.raises(bfc.ManifestInvalid, match="audited"):
        bfc.load_canonical_broker_flow(baseline_db, path)


def test_real_inferred_date_cannot_be_upgraded_by_an_unlabelled_manifest(audited, baseline_db,
                                                                         tmp_path):
    manifest = copy.deepcopy(audited[0])
    manifest["input"]["snapshot"] = None
    recs = {(r["broker_flow_date"], r["regime"]): r for r in manifest["records"]}
    _forge_content_match(("2026-08-25", bfr.LIVE), "2026-08-24")(recs, manifest)
    manifest["summary"] = bfr._summary(manifest["records"])
    path = str(tmp_path / "forged.json")
    bfr.write_manifest(manifest, path)
    with pytest.raises(bfc.ManifestInvalid, match="CONTENT_MATCHED"):
        bfc.load_canonical_broker_flow(baseline_db, path)
