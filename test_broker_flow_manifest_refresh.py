"""broker_flow_manifest_refresh: the audited-anchor refresh of the broker_flow
evidence manifest (HANDOFF Lampiran U).

Synthetic fixtures reuse test_broker_flow_canonical's database, parquet and
generator-built manifest, pinned as the audited anchor for the refresh, the
reader and PR #74 alike. A "current" database is a copy of it after a nightly
backfill top-up (values rewritten on 07-01 and 07-03, one row added on 07-02)
and one more PR #72 capture (2026-10-01, holding the 2026-09-30 session with
its own PERSISTED scan run). The real-data tests read neobdm.db from git at the
pinned commits, never the nightly-changing working copy; they skip only when
that git history is absent, which means they did not run."""

import copy
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

import broker_flow_canonical as bfc
import broker_flow_manifest_refresh as bmr
import broker_flow_regime as bfr
import test_broker_flow_canonical as tc

HERE = os.path.dirname(os.path.abspath(__file__))
COMMITTED = os.path.join(HERE, bfr.MANIFEST_PATH)
LIVE, BACKFILL = bfr.LIVE, bfr.BACKFILL
L05, L06, L30 = ("2026-07-05", LIVE), ("2026-07-06", LIVE), ("2026-09-30", LIVE)
INF, MIX, NEW = ("2026-09-01", LIVE), ("2026-08-12", LIVE), ("2026-10-01", LIVE)

TOP_UP = (
    "UPDATE broker_flow SET netval = netval + 0.5 WHERE date = '2026-07-01' AND bval IS NULL",
    "UPDATE broker_flow SET netval = 7.0 WHERE date = '2026-07-03' AND ticker = 'AAAA' "
    "AND broker_code = 'AK'",
    "INSERT INTO broker_flow VALUES ('2026-07-02','FFFF','ZP',NULL,NULL,1.0,NULL,NULL)",
)
NIGHTLY = (
    "INSERT INTO broker_flow VALUES ('2026-10-01','AAAA','AK',3.0,1.0,2.0,100.0,100.0)",
    "INSERT INTO broker_flow VALUES ('2026-10-01','AAAA','ZP',1.0,3.0,-2.0,100.0,100.0)",
)
RUN = "2026-10-01T00:30:00+00:00"


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def by_key(m):
    return {(r["broker_flow_date"], r["regime"]): r for r in m["records"]}


def group_hash(db, d, regime):
    con = bfr.connect_readonly(db)
    try:
        return bfr._group_hash(con, d, regime)
    finally:
        con.close()


def add_scan(db, rows):
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO broker_flow_scan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


class Refresh(tc.Synth):
    """test_broker_flow_canonical's fixture; its manifest is the audited
    anchor of the refresh too."""

    def pin(self, path):
        super().pin(path)
        self.mp.setattr(bmr, "AUDITED_ANCHOR_SHA256", bfc.AUDITED_MANIFEST_SHA256)

    def current(self, name="current.db", *sql, top_up=True, nightly=True, scan=True):
        db = self.db_copy(name, *((TOP_UP if top_up else ()) + (NIGHTLY if nightly else ()) + sql))
        if nightly and scan:
            add_scan(db, tc.scan_run("2026-10-01", "2026-09-30", {"AK": 1, "ZP": 1}, RUN))
        return db

    def refresh(self, db, name="refreshed.json", **kw):
        out = str(self.dir / name)
        m, cf = bmr.refresh(db, out, **kw)
        return out, m, cf


@pytest.fixture
def rf(tmp_path, monkeypatch):
    return Refresh(tmp_path, monkeypatch)


def test_the_current_fixture_differs_from_the_anchor_as_designed(rf):
    db = rf.current()
    anchor = by_key(rf.manifest)
    changed = {k for k, r in anchor.items() if group_hash(db, *k) != (r["row_count"], r["rows_sha256"])}
    assert changed == {("2026-07-01", BACKFILL), ("2026-07-02", BACKFILL), ("2026-07-03", BACKFILL)}
    assert group_hash(db, *NEW)[0] == 2


# --------------------------------------------------------------------------
# Anchor: content, never path or label
# --------------------------------------------------------------------------

def test_the_trust_root_is_the_audited_content():
    assert bmr.AUDITED_ANCHOR_SHA256 == bfc.AUDITED_MANIFEST_SHA256 == \
        "146b3efd7969ed22b0858ed5eac54f227199319d8bf9bcac7b6d211515788025"
    assert os.path.samefile(bmr.COMMITTED_ANCHOR, COMMITTED)


def test_the_exact_audited_anchor_is_accepted_wherever_it_lies(rf):
    db = rf.current()
    _, m, cf = rf.refresh(db)
    assert m["refresh"]["contract"] == bmr.REFRESH_CONTRACT == "audited-anchor-refresh-v1"
    assert m["refresh"]["anchor"]["manifest_sha256"] == bmr.AUDITED_ANCHOR_SHA256
    assert m["input"]["snapshot"] == cf.snapshot == bmr.REFRESH_CONTRACT and not cf.audited
    # trusted by content: a CRLF copy somewhere else is the same anchor
    elsewhere = rf.dir / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "copy.json").write_bytes(open(rf.mpath, "rb").read().replace(b"\n", b"\r\n"))
    _, again, _ = rf.refresh(db, "via_copy.json", anchor_path=str(elsewhere / "copy.json"))
    assert bfr.dumps(again) == bfr.dumps(m)


@pytest.mark.parametrize("edit", [
    lambda recs, m: recs[tc.BF01].update(notes=["edited"]),
    lambda recs, m: recs[INF].update(inferred_session_date="2026-08-28"),
    lambda recs, m: m["records"].remove(recs[L06]),
    lambda recs, m: m["input"].update(source_commit="0" * 40),
], ids=["notes", "inferred", "record_removed", "source_commit"])
def test_an_altered_anchor_is_refused(rf, edit):
    altered = rf.edited(edit, "altered.json")
    out = rf.dir / "out.json"
    with pytest.raises(bmr.RefreshRefused, match="not the audited anchor"):
        bmr.refresh(rf.current(), str(out), anchor_path=altered)
    assert not out.exists()


def test_the_anchor_file_itself_altered_is_refused(rf):
    m = copy.deepcopy(rf.manifest)
    m["records"][0]["notes"] = ["edited"]
    bfr.write_manifest(m, rf.mpath)
    with pytest.raises(bmr.RefreshRefused, match="not the audited anchor"):
        rf.refresh(rf.current())


@pytest.mark.parametrize("label", [bfr.AUDITED_SNAPSHOT["name"], bmr.REFRESH_CONTRACT, None])
def test_a_spoofed_audited_label_is_refused(rf, label):
    spoof = copy.deepcopy(rf.manifest)
    spoof["input"]["snapshot"] = label
    spoof["input"]["source_commit"] = bfr.AUDITED_SNAPSHOT["source_commit"]
    path = rf.write(spoof, "spoof.json")
    with pytest.raises(bmr.RefreshRefused, match="grants nothing"):
        bmr.refresh(rf.current(), str(rf.dir / "out.json"), anchor_path=path)


def test_an_anchor_pinned_by_hash_must_still_be_a_valid_manifest(rf):
    bad = copy.deepcopy(rf.manifest)
    bad["summary"]["by_date_class"][bfr.CONTENT_MATCHED]["dates"] += 1
    rf.pin(rf.write(bad, "pinned_but_invalid.json"))
    with pytest.raises(bmr.RefreshRefused, match="fails its own checks"):
        rf.refresh(rf.current())


def test_a_refresh_is_never_anchored_on_a_refresh(rf):
    db = rf.current()
    out, _, _ = rf.refresh(db)
    with pytest.raises(bmr.RefreshRefused, match="not the audited anchor"):
        rf.refresh(db, "chained.json", anchor_path=out)


def test_the_reader_and_the_refresh_must_share_one_trust_root(rf, monkeypatch):
    monkeypatch.setattr(bmr, "AUDITED_ANCHOR_SHA256", "0" * 64)
    with pytest.raises(bmr.RefreshRefused, match="trust root"):
        rf.refresh(rf.current())


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def test_the_cli_publishes_and_reports(rf, capsys):
    db, out = rf.current(), str(rf.dir / "cli" / "refreshed.json")
    assert bmr.main(["--db", db, "--out", out, "--source-commit", "abc1234"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["out"], report["new_live"], report["backfill_changed"]) == \
        (os.path.abspath(out), {"2026-10-01": bfr.SCAN_VERIFIED}, 3)
    assert report["canonical"]["quarantined"] == ["2026-07-02", "2026-07-03", "2026-07-14"]
    with open(out, encoding="ascii") as f:
        assert json.load(f)["input"]["source_commit"] == "abc1234"


def test_an_explicit_output_path_is_required(rf, capsys):
    db = rf.current()
    with pytest.raises(SystemExit) as e:
        bmr.main(["--db", db])
    assert e.value.code == 2 and "--out" in capsys.readouterr().err
    for out in (None, "", "  "):
        with pytest.raises(bmr.RefreshRefused, match="explicit output path"):
            bmr.refresh(db, out)


def test_the_committed_audited_manifest_is_never_the_output(rf, monkeypatch, capsys):
    db, before = rf.current(), sha(COMMITTED)
    spellings = [COMMITTED, os.path.join(HERE, "evidence", ".", "..", bfr.MANIFEST_PATH)]
    if os.name == "nt":
        spellings += [COMMITTED.upper(), COMMITTED + "."]
    for out in spellings:
        with pytest.raises(bmr.RefreshRefused, match="committed audited manifest"):
            bmr.refresh(db, out)
    monkeypatch.chdir(HERE)
    assert bmr.main(["--db", db, "--out", bfr.MANIFEST_PATH]) == 2
    assert "REFUSED, nothing written" in capsys.readouterr().err
    assert sha(COMMITTED) == before


def test_the_audited_manifest_path_is_refused_even_when_the_file_is_absent(rf, monkeypatch):
    """A checkout without the committed manifest must not get one written by a
    refresh: the path itself is protected, not only an existing file."""
    absent = str(rf.dir / "checkout" / "evidence" / "broker_flow_date_evidence.json")
    monkeypatch.setattr(bmr, "COMMITTED_ANCHOR", absent)
    for out in (absent, str(rf.dir / "checkout" / "x" / ".." / "evidence" /
                            "broker_flow_date_evidence.json")):
        with pytest.raises(bmr.RefreshRefused, match="committed audited manifest"):
            bmr.refresh(rf.current(), out)
    assert not os.path.exists(absent)


def test_the_anchor_in_use_and_its_aliases_are_never_the_output(rf):
    db, before = rf.current(), sha(rf.mpath)
    aliases = [rf.mpath, str(rf.dir / "sub" / ".." / os.path.basename(rf.mpath))]
    if os.name == "nt":
        aliases += [rf.mpath.upper(), rf.mpath + "."]
    for kind, make in (("hard", os.link), ("sym", os.symlink)):
        try:
            make(rf.mpath, str(rf.dir / f"{kind}link.json"))
            aliases.append(str(rf.dir / f"{kind}link.json"))
        except (OSError, NotImplementedError):
            pass                                  # unprivileged Windows: no symlinks
    assert str(rf.dir / "hardlink.json") in aliases
    for out in aliases:
        with pytest.raises(bmr.RefreshRefused, match="audited anchor"):
            bmr.refresh(db, out)
    copied = str(rf.dir / "anchor_copy.json")
    shutil.copy(rf.mpath, copied)
    with pytest.raises(bmr.RefreshRefused, match="audited anchor"):
        bmr.refresh(db, copied, anchor_path=copied)
    assert sha(rf.mpath) == before == sha(copied)


def test_the_source_database_is_never_the_output(rf):
    db = rf.current()
    before = sha(db)
    with pytest.raises(bmr.RefreshRefused, match="source database"):
        bmr.refresh(db, db)
    assert sha(db) == before


def _refuse_reading(*args, **kwargs):
    raise bfc.ManifestMismatch("simulated refusal", {"scan": []})


@pytest.mark.parametrize("failure", ["wal", "changed_content_match", "reader_refuses",
                                     "contradictory_scan"])
def test_a_failed_refresh_leaves_an_existing_output_untouched(rf, monkeypatch, failure):
    db = rf.current(scan=failure != "contradictory_scan")
    if failure == "wal":
        open(db + "-wal", "wb").close()
        error = bfc.SourceStateError
    elif failure == "changed_content_match":
        db = rf.current("cm.db", "UPDATE broker_flow SET bavg = 100.5 WHERE date = '2026-07-06' "
                                 "AND ticker = 'AAAA' AND broker_code = 'AK'")
        error = bmr.RefreshRefused
    elif failure == "reader_refuses":
        monkeypatch.setattr(bfc, "load_canonical_broker_flow", _refuse_reading)
        error = bmr.RefreshRefused
    else:
        add_scan(db, tc.scan_run("2026-10-01", "2026-09-29", {"AK": 1, "ZP": 1}, RUN))
        error = bmr.RefreshRefused
    folder = rf.dir / "published"
    folder.mkdir()
    (folder / "refreshed.json").write_bytes(b"previous manifest\n")
    with pytest.raises(error):
        bmr.refresh(db, str(folder / "refreshed.json"))
    assert (folder / "refreshed.json").read_bytes() == b"previous manifest\n"
    assert os.listdir(folder) == ["refreshed.json"]          # no temp file left behind


def test_an_output_that_becomes_an_alias_during_validation_is_refused(rf, monkeypatch):
    out = str(rf.dir / "out.json")
    real = bmr.self_validate

    def link_then_validate(*args):
        os.link(rf.mpath, out)
        return real(*args)
    monkeypatch.setattr(bmr, "self_validate", link_then_validate)
    before = sha(rf.mpath)
    with pytest.raises(bmr.RefreshRefused, match="audited anchor"):
        bmr.refresh(rf.current(), out)
    assert sha(rf.mpath) == sha(out) == before
    assert not [n for n in os.listdir(rf.dir) if n.endswith(".tmp")]


def test_a_successful_refresh_replaces_the_output_atomically(rf):
    folder = rf.dir / "published"
    folder.mkdir()
    (folder / "refreshed.json").write_bytes(b"previous manifest\n")
    _, m, _ = rf.refresh(rf.current(), "published/refreshed.json")
    assert (folder / "refreshed.json").read_bytes() == bfr.dumps(m).encode("ascii")
    assert os.listdir(folder) == ["refreshed.json"]


def test_a_refresh_is_deterministic(rf):
    db = rf.current()
    a, m, _ = rf.refresh(db, "a.json")
    b, _, _ = rf.refresh(db, "b.json")
    moved = str(rf.dir / "moved" / "neobdm.db")
    os.makedirs(os.path.dirname(moved))
    shutil.copy(db, moved)
    c, _, _ = rf.refresh(moved, "c.json")
    assert open(a, "rb").read() == open(b, "rb").read() == open(c, "rb").read()
    assert m["refresh"]["current_db"] == {"sha256": sha(db), "bytes": os.path.getsize(db)}


# --------------------------------------------------------------------------
# SOURCE_DATED_BACKFILL: re-derived from the current rows
# --------------------------------------------------------------------------

def test_changed_backfill_rows_are_rederived_from_the_current_db(rf):
    db = rf.current()
    _, m, cf = rf.refresh(db)
    recs, anchor = by_key(m), by_key(rf.manifest)
    for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
        r, a = recs[(d, BACKFILL)], anchor[(d, BACKFILL)]
        assert (r["row_count"], r["rows_sha256"]) == group_hash(db, d, BACKFILL)
        assert r["rows_sha256"] != a["rows_sha256"]          # the audited hash is not carried
        assert dict(r, row_count=None, rows_sha256=None) == dict(a, row_count=None, rows_sha256=None)
        assert (r["date_class"], r["canonical_session_date"], r["evidence_ref"]) == \
            (bfr.SOURCE_DATED_BACKFILL, d, bfr.BACKFILL_EVIDENCE_REF)
    assert recs[("2026-07-02", BACKFILL)]["row_count"] == anchor[("2026-07-02", BACKFILL)]["row_count"] + 1
    assert m["refresh"]["inheritance"]["backfill"] == {
        "rederived": 3, "changed_since_anchor": ["2026-07-01", "2026-07-02", "2026-07-03"]}
    row = cf.get("2026-07-01", "AAAA", "AK")
    assert (row.netval,) == tc.raw(db, "2026-07-01", ("AAAA", "AK"))[2:3] != \
        tc.raw(rf.db, "2026-07-01", ("AAAA", "AK"))[2:3]


def test_a_rederived_backfill_record_must_match_the_anchor_beyond_its_rows(rf, monkeypatch):
    """Only row_count and rows_sha256 may move: any other drift of PR #74's
    backfill record (a generator change) refuses the refresh."""
    real = bfr._build

    def drifted(*args):
        m = real(*args)
        by_key(m)[("2026-07-02", BACKFILL)]["notes"] = ["generator drift"]
        return m
    monkeypatch.setattr(bfr, "_build", drifted)
    with pytest.raises(bmr.RefreshRefused, match=r"\('2026-07-02', 'BACKFILL'\): re-derived record "
                                                 "differs from the anchored one beyond its rows"):
        rf.refresh(rf.current())


def test_backfill_rows_removed_are_rederived_too(rf):
    db = rf.current("shrunk.db", "DELETE FROM broker_flow WHERE date = '2026-07-01' "
                                 "AND ticker = 'AAAA' AND broker_code = 'AK'")
    _, m, cf = rf.refresh(db)
    assert by_key(m)[("2026-07-01", BACKFILL)]["row_count"] == 3
    assert cf.get("2026-07-01", "AAAA", "AK") is None            # gone, not zero


def test_refreshed_backfill_hashes_bind_to_the_current_db(rf):
    db = rf.current()
    out, _, _ = rf.refresh(db)
    with pytest.raises(bfc.ManifestMismatch) as e:                 # not the anchor's database
        bfc.load_canonical_broker_flow(rf.db, out)
    assert e.value.diff == {"missing": [], "gone": [NEW],
                            "count_changed": [("2026-07-02", BACKFILL)],
                            "content_changed": [("2026-07-01", BACKFILL), ("2026-07-03", BACKFILL)]}
    later = str(rf.dir / "later.db")
    shutil.copy(db, later)
    con = sqlite3.connect(later)
    con.execute("UPDATE broker_flow SET netval = 9.0 WHERE date = '2026-07-02' AND ticker = 'AAAA' "
                "AND broker_code = 'AK'")
    con.commit()
    con.close()
    with pytest.raises(bfc.ManifestMismatch) as e:                 # nor a later top-up
        bfc.load_canonical_broker_flow(later, out)
    assert e.value.diff["content_changed"] == [("2026-07-02", BACKFILL)]


# --------------------------------------------------------------------------
# CONTENT_MATCHED: anchored records only, carried exactly
# --------------------------------------------------------------------------

def _content_matched(m):
    return {k: r for k, r in by_key(m).items() if r["date_class"] == bfr.CONTENT_MATCHED}


def test_unchanged_anchored_content_matches_are_carried_exactly(rf):
    _, m, _ = rf.refresh(rf.current())
    assert _content_matched(m) == _content_matched(rf.manifest)
    assert len(_content_matched(m)) == m["refresh"]["inheritance"]["anchored_live"][bfr.CONTENT_MATCHED] == 7
    assert m["input"]["content_match_evidence"] == rf.manifest["input"]["content_match_evidence"]


@pytest.mark.parametrize("sql", [
    "UPDATE broker_flow SET bavg = 100.5 WHERE date = '2026-07-06' AND ticker = 'AAAA' "
    "AND broker_code = 'AK'",
    "DELETE FROM broker_flow WHERE date = '2026-07-06' AND ticker = 'AAAA' AND broker_code = 'AK'",
    "INSERT INTO broker_flow VALUES ('2026-07-06','FFFF','ZP',1.0,1.0,0.0,100.0,100.0)",
    "UPDATE broker_flow SET netval = netval + 0.1 WHERE date = '2026-07-05' AND ticker = 'BBBB' "
    "AND broker_code = 'AK'",
], ids=["value", "row_deleted", "row_added", "conflict_side"])
def test_a_changed_anchored_content_match_fails_closed(rf, sql):
    with pytest.raises(bmr.RefreshRefused,
                       match=r"'LIVE'\) CONTENT_MATCHED: historical LIVE rows changed"):
        rf.refresh(rf.current("changed.db", sql))


@pytest.mark.parametrize("d", ["2026-07-06", "2026-07-05"])
def test_a_missing_anchored_content_match_fails_closed(rf, d):
    db = rf.current("gone.db", f"DELETE FROM broker_flow WHERE date = '{d}'")
    with pytest.raises(bmr.RefreshRefused,
                       match=rf"\('{d}', 'LIVE'\) CONTENT_MATCHED: anchored group is gone"):
        rf.refresh(db)


def test_no_new_content_match_is_invented(rf):
    """The 10-01 rows agree on every row with session 09-30 of a parquet that
    PR #74 would content-match, and no scan proves the date: the refresh still
    says INFERRED_ONLY. It has no parquet input at all."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    values = tc.session_values(10)
    rows = ", ".join(f"('2026-10-01','{t}','{b}',{bv},{sv},{round(bv - sv, 1)},100.0,100.0)"
                     for (t, b), (bv, sv) in sorted(tc.as_live(values).items()))
    db = rf.current("matchable.db", "INSERT INTO broker_flow VALUES " + rows, nightly=False)
    cols = {c: [] for c in ("date", "ticker", "broker", "bval", "sval")}
    for (t, b), (bv, sv) in sorted(values.items()):
        for c, v in zip(cols, ("2026-09-30", t, b, bv, sv)):
            cols[c].append(v)
    matchable = str(rf.dir / "broker_daily.parquet")
    pq.write_table(pa.table(cols), matchable)
    assert by_key(bfr.build_manifest(db, matchable))[NEW]["date_class"] == bfr.CONTENT_MATCHED
    _, m, cf = rf.refresh(db)
    assert (by_key(m)[NEW]["date_class"], by_key(m)[NEW]["canonical_session_date"]) == \
        (bfr.INFERRED_ONLY, None)
    assert _content_matched(m) == _content_matched(rf.manifest)
    assert "2026-09-30" not in cf.sessions
    for fn in (bmr.refresh, bmr.build_refreshed_manifest):
        assert not [p for p in inspect.signature(fn).parameters if "daily" in p or "parquet" in p]


def test_a_refresh_without_the_parquet_keeps_the_anchored_content_matches(rf):
    os.remove(rf.pq)
    db = rf.current()
    _, m, cf = rf.refresh(db)
    assert _content_matched(m) == _content_matched(rf.manifest)
    before = rf.load()
    for session in ("2026-07-06", "2026-07-07"):
        assert [r for r in cf.rows if r.canonical_session_date == session] == \
            [r for r in before.rows if r.canonical_session_date == session]
    # what PR #74 alone does without the parquet: every content match is lost
    plain = bfr.build_manifest(db, None)
    assert not _content_matched(plain)


# --------------------------------------------------------------------------
# SCAN_VERIFIED: re-proven from the current broker_flow_scan
# --------------------------------------------------------------------------

def test_a_new_scan_backed_date_is_scan_verified(rf):
    _, m, cf = rf.refresh(rf.current())
    r = by_key(m)[NEW]
    assert (r["date_class"], r["canonical_session_date"], r["capture_class"], r["evidence_level"],
            r["scan"]["run_started_utc"], r["scan"]["tracked_rows"]) == \
        (bfr.SCAN_VERIFIED, "2026-09-30", bfr.FULL_CALLBACK, "PROVEN", RUN, 2)
    assert m["refresh"]["inheritance"]["new_live"] == {"2026-10-01": bfr.SCAN_VERIFIED}
    assert cf.sessions["2026-09-30"] == bfr.FULL_CALLBACK
    assert {(x.ticker, x.broker_code, x.acquisition_date) for x in cf.rows
            if x.canonical_session_date == "2026-09-30"} == \
        {("AAAA", "AK", "2026-10-01"), ("AAAA", "ZP", "2026-10-01")}


@pytest.mark.parametrize("tracked, session, match", [
    ({"AK": 1, "ZP": 1}, "2026-09-29", "is not 2026-09-30"),     # stale source session
    ({"AK": 1, "ZP": 1}, "2026-10-01", "is not 2026-09-30"),     # same-day session
    ({"AK": 2, "ZP": 1}, "2026-09-30", "tracked 3 rows"),
    ({"AK": 2, "ZP": 0}, "2026-09-30", "per broker"),           # totals agree, brokers do not
    ({"AK": 2}, "2026-09-30", "live brokers outside the run"),
], ids=["stale", "same_day", "total", "per_broker", "unscanned_broker"])
def test_a_contradictory_new_scan_fails_closed(rf, tracked, session, match):
    db = rf.current("bad_scan.db", scan=False)
    add_scan(db, tc.scan_run("2026-10-01", session, tracked, RUN))
    with pytest.raises(bmr.RefreshRefused, match=match):
        rf.refresh(db)


def test_two_persisted_runs_for_one_new_date_fail_closed(rf):
    db = rf.current("two_runs.db")
    add_scan(db, tc.scan_run("2026-10-01", "2026-09-30", {"AK": 1, "ZP": 1},
                             "2026-10-01T02:00:00+00:00"))
    with pytest.raises(bmr.RefreshRefused, match="2 runs"):
        rf.refresh(db)


@pytest.mark.parametrize("rejected", [False, True], ids=["no_scan", "rejected_run_only"])
def test_a_new_date_without_a_valid_scan_stays_untrusted(rf, rejected):
    db = rf.current("no_scan.db", scan=False)
    if rejected:
        add_scan(db, [("2026-10-01", "AK", RUN, "SOURCE_FAILURE", "REJECTED", None, None, None, None,
                       "dash_callback_v1", None, None, "http 500", "2026-09-30", tc.CALENDAR)])
    _, m, cf = rf.refresh(db)
    r = by_key(m)[NEW]
    assert (r["date_class"], r["canonical_session_date"], r["evidence_level"]) == \
        (bfr.INFERRED_ONLY, None, "INFERRED")
    assert m["refresh"]["inheritance"]["new_live"] == {"2026-10-01": bfr.INFERRED_ONLY}
    assert {e.acquisition_date: e.date_class for e in cf.excluded}["2026-10-01"] == bfr.INFERRED_ONLY
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get("2026-09-30", "AAAA", "AK")
    assert e.value.status == bfc.NOT_COVERED


CONTRADICTS = "PR #74 evidence contradicts itself: 2026-09-30"


@pytest.mark.parametrize("sql, match", [
    ("DELETE FROM broker_flow_scan WHERE scrape_date = '2026-09-30' AND snapshot = 'PERSISTED'",
     r"'2026-09-30', 'LIVE'\) SCAN_VERIFIED: the current evidence classifies it INFERRED_ONLY"),
    ("DELETE FROM broker_flow_scan WHERE scrape_date = '2026-09-30' AND snapshot = 'PERSISTED' "
     "AND broker_code = 'ZP'", CONTRADICTS),
    ("UPDATE broker_flow_scan SET detail = 'edited' WHERE scrape_date = '2026-09-30' "
     "AND snapshot = 'PERSISTED' AND broker_code = 'AK'",
     r"SCAN_VERIFIED: the current scan run proves different \['evidence_hash', 'scan'\]"),
    ("UPDATE broker_flow_scan SET session_date = '2026-09-28', expected_session_date = "
     "'2026-09-28' WHERE scrape_date = '2026-09-30' AND snapshot = 'PERSISTED'", CONTRADICTS),
    ("UPDATE broker_flow_scan SET tracked_rows = 4 WHERE scrape_date = '2026-09-30' "
     "AND snapshot = 'PERSISTED' AND broker_code = 'AK'", CONTRADICTS),
], ids=["run_gone", "broker_gone", "fingerprint", "session", "tracked"])
def test_the_anchored_scan_verified_date_is_re_proven_not_inherited(rf, sql, match):
    with pytest.raises(bmr.RefreshRefused, match=match):
        rf.refresh(rf.current("scan_drift.db", sql))


def test_an_anchored_unproven_date_that_a_scan_now_proves_fails_closed(rf):
    db = rf.current("late_scan.db")
    add_scan(db, tc.scan_run("2026-09-01", "2026-08-31", {"AK": 6, "ZP": 6},
                             "2026-09-01T01:00:00+00:00"))
    with pytest.raises(bmr.RefreshRefused, match=r"'2026-09-01', 'LIVE'\) INFERRED_ONLY: the "
                                                 "current evidence classifies it SCAN_VERIFIED"):
        rf.refresh(db)


# --------------------------------------------------------------------------
# MIXED and quarantine
# --------------------------------------------------------------------------

def test_an_unchanged_anchored_mixed_date_stays_mixed(rf):
    _, m, cf = rf.refresh(rf.current())
    assert by_key(m)[MIX] == by_key(rf.manifest)[MIX]
    assert {e.acquisition_date: e.date_class for e in cf.excluded}["2026-08-12"] == bfr.MIXED
    assert not [r for r in cf.rows if "2026-08-12" in r.copy_acquisition_dates]


@pytest.mark.parametrize("sql", [
    "UPDATE broker_flow SET bval = bval + 0.1 WHERE date = '2026-08-12' AND ticker = 'AAAA' "
    "AND broker_code = 'AK'",
    "DELETE FROM broker_flow WHERE date = '2026-08-12' AND ticker = 'AAAA' AND broker_code = 'ZP'",
    "INSERT INTO broker_flow VALUES ('2026-08-12','GGGG','AK',1.0,1.0,0.0,100.0,100.0)",
])
def test_a_changed_anchored_mixed_date_fails_closed(rf, sql):
    with pytest.raises(bmr.RefreshRefused, match="2026-08-12"):
        rf.refresh(rf.current("mixed.db", sql))


def test_the_07_03_conflict_stays_quarantined_after_refresh(rf):
    db = rf.current()
    out, m, cf = rf.refresh(db)
    # the top-up moved the backfill side off its audited hash; the live side is the anchor's
    assert by_key(m)[("2026-07-03", BACKFILL)]["rows_sha256"] != \
        by_key(rf.manifest)[("2026-07-03", BACKFILL)]["rows_sha256"]
    assert by_key(m)[L05] == by_key(rf.manifest)[L05]
    q = {x.canonical_session_date: x for x in cf.quarantined}["2026-07-03"]
    assert [(d, regime) for d, regime, *_ in q.records] == [("2026-07-03", BACKFILL), L05]
    assert q.reasons == (bfc.CAPTURE_CLASSES_DIFFER, bfc.VALUES_CONFLICT)
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get("2026-07-03", "AAAA", "AK")
    assert e.value.status == bfc.QUARANTINED
    assert {r.status for r in bfc.inspect_broker_flow_evidence(db, out)
            if r.acquisition_date in ("2026-07-03", "2026-07-05")} == {bfc.QUARANTINED}
    assert {x.canonical_session_date for x in cf.quarantined} == \
        {x.canonical_session_date for x in rf.load().quarantined} == \
        {"2026-07-02", "2026-07-03", "2026-07-14"}


def test_weaker_evidence_cannot_clear_the_anchored_quarantine(rf):
    db = rf.current()
    # PR #74 without the parquet: 07-05 falls to INFERRED_ONLY while the 07-03
    # backfill side no longer has its audited hash. The reader keeps the session out.
    weaker = rf.write(bfr.build_manifest(db, None), "weaker.json")
    cf = bfc.load_canonical_broker_flow(db, weaker)
    assert by_key(json.load(open(weaker)))[L05]["date_class"] == bfr.INFERRED_ONLY
    assert "2026-07-03" not in cf.sessions
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get("2026-07-03", "BBBB", "AK")
    assert e.value.status == bfc.QUARANTINED
    # the refresh never publishes such a downgrade: a changed live side is refused
    with pytest.raises(bmr.RefreshRefused, match=r"\('2026-07-05', 'LIVE'\) CONTENT_MATCHED: "
                                                 "historical LIVE rows changed"):
        rf.refresh(rf.current("live_side.db", "UPDATE broker_flow SET savg = 99.0 WHERE "
                                              "date = '2026-07-05' AND ticker = 'CCCC'"))


def _downgrade_07_05(real):
    def reconcile(anchor, derived):
        records, inheritance = real(anchor, derived)
        return [dict(r, date_class=bfr.INFERRED_ONLY, evidence_level="INFERRED",
                     evidence_kind="idx_calendar_inference", canonical_session_date=None)
                if (r["broker_flow_date"], r["regime"]) == L05 else r for r in records], inheritance
    return reconcile


def test_self_validation_refuses_a_view_that_clears_an_anchored_quarantine(rf, monkeypatch):
    """Even with the reader's own anchoring disabled, a refresh whose records
    would expose the 07-03 backfill side is not published."""
    monkeypatch.setattr(bmr, "_reconcile", _downgrade_07_05(bmr._reconcile))
    monkeypatch.setattr(bfc, "_anchored_quarantine_records", lambda records, groups: {})
    out = rf.dir / "out.json"
    with pytest.raises(bmr.RefreshRefused, match=r"clears anchored quarantine \['2026-07-03'\]"):
        bmr.refresh(rf.current(), str(out))
    assert not out.exists()


def test_the_anchored_quarantine_is_computed_from_the_anchor(rf):
    db = rf.current()
    _, context = bmr.build_refreshed_manifest(db)
    assert bmr.anchored_quarantine(context["anchor"], context["groups"]) == \
        {"2026-07-02", "2026-07-03", "2026-07-14"}


# --------------------------------------------------------------------------
# Historical LIVE immutability; groups outside the contract
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", [L06, L30, INF, MIX], ids=["CONTENT_MATCHED", "SCAN_VERIFIED",
                                                           "INFERRED_ONLY", "MIXED"])
@pytest.mark.parametrize("change", ["update", "delete", "insert"])
def test_a_protected_historical_live_group_cannot_change(rf, key, change):
    """Every anchored LIVE class is immutable. The refresh names the change
    itself; a change PR #74's own proof sees first (a scan run's per-broker
    rows, the AUDITED_MIXED rerun state) is its contradiction."""
    d = key[0]
    sql = {"update": f"UPDATE broker_flow SET savg = savg + 1 WHERE date = '{d}' "
                     "AND ticker = 'AAAA' AND broker_code = 'AK'",
           "delete": f"DELETE FROM broker_flow WHERE date = '{d}' AND ticker = 'AAAA' "
                     "AND broker_code = 'AK'",
           "insert": f"INSERT INTO broker_flow VALUES ('{d}','GGGG','AK',1.0,1.0,0.0,100.0,100.0)"}
    cls = by_key(rf.manifest)[key]["date_class"]
    if key == MIX or (key == L30 and change != "update"):
        match = f"PR #74 evidence contradicts itself: {d}"
    else:
        match = rf"\('{d}', 'LIVE'\) {cls}: historical LIVE rows changed"
    out = rf.dir / "out.json"
    with pytest.raises(bmr.RefreshRefused, match=match):
        bmr.refresh(rf.current("historical.db", sql[change]), str(out))
    assert not out.exists()


@pytest.mark.parametrize("sql, match", [
    ("INSERT INTO broker_flow VALUES ('2026-08-01','AAAA','AK',1.0,1.0,0.0,100.0,100.0)",
     r"\('2026-08-01', 'LIVE'\): a LIVE group the anchor does not hold, inside its coverage"),
    ("INSERT INTO broker_flow VALUES ('2026-09-30','GGGG','AK',NULL,NULL,1.0,NULL,NULL)",
     r"\('2026-09-30', 'BACKFILL'\): a BACKFILL date the anchor"),
    ("INSERT INTO broker_flow VALUES ('2026-06-30','AAAA','AK',NULL,NULL,1.0,NULL,NULL)",
     r"\('2026-06-30', 'BACKFILL'\): a BACKFILL date the anchor"),
    ("DELETE FROM broker_flow WHERE date = '2026-07-01'",
     r"\('2026-07-01', 'BACKFILL'\) SOURCE_DATED_BACKFILL: anchored group is gone"),
    ("DELETE FROM broker_flow WHERE date = '2026-09-01'",
     r"\('2026-09-01', 'LIVE'\) INFERRED_ONLY: anchored group is gone"),
], ids=["live_inside_coverage", "backfill_after_end", "backfill_new_date", "backfill_gone",
        "inferred_gone"])
def test_groups_outside_the_contract_are_refused(rf, sql, match):
    with pytest.raises(bmr.RefreshRefused, match=match):
        rf.refresh(rf.current("outside.db", sql))


def test_new_live_groups_after_the_anchor_are_allowed(rf):
    db = rf.current("two_new.db", "INSERT INTO broker_flow VALUES "
                                  "('2026-10-02','AAAA','AK',2.0,1.0,1.0,100.0,100.0)")
    _, m, cf = rf.refresh(db)
    assert m["refresh"]["inheritance"]["new_live"] == {"2026-10-01": bfr.SCAN_VERIFIED,
                                                       "2026-10-02": bfr.INFERRED_ONLY}
    assert len(m["records"]) == len(rf.manifest["records"]) + 2


def test_every_violation_is_reported_together(rf):
    db = rf.current("many.db", "DELETE FROM broker_flow WHERE date = '2026-07-06'",
                    "UPDATE broker_flow SET savg = 1 WHERE date = '2026-07-15'",
                    "INSERT INTO broker_flow VALUES ('2026-06-30','AAAA','AK',NULL,NULL,1.0,NULL,NULL)")
    with pytest.raises(bmr.RefreshRefused, match="3 violation") as e:
        rf.refresh(db)
    assert all(d in str(e.value) for d in ("2026-07-06", "2026-07-15", "2026-06-30"))


# --------------------------------------------------------------------------
# Source quiescence and read-only safety
# --------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_a_database_that_is_not_quiescent_is_refused(rf, monkeypatch, suffix):
    db = rf.current()
    open(db + suffix, "wb").close()
    classified = []
    real = bfr._build
    monkeypatch.setattr(bfr, "_build", lambda *a: classified.append(a) or real(*a))
    out = rf.dir / "out.json"
    with pytest.raises(bfc.SourceStateError, match=suffix):
        bmr.refresh(db, str(out))
    assert not out.exists() and classified == []         # refused before anything is read


def test_a_hard_linked_source_is_refused(rf, monkeypatch):
    writer = rf.current("writer.db")
    alias = str(rf.dir / "alias.db")
    try:
        os.link(writer, alias)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"hard links unavailable: {e}")
    classified = []
    real = bfr._build
    monkeypatch.setattr(bfr, "_build", lambda *a: classified.append(a) or real(*a))
    with pytest.raises(bfc.SourceStateError, match="hard links"):
        bmr.refresh(alias, str(rf.dir / "out.json"))
    assert classified == []


def test_a_source_that_changes_while_it_is_read_is_refused(rf, monkeypatch):
    """Touched once, during the refresh's own read: the reader's later read
    would see a stable file, so the refresh's bracket must catch it."""
    db = rf.current()
    real = bfr._scan_rows
    touched = []

    def touch_then_read(con):
        if not touched:
            st = os.stat(db)
            os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
            touched.append(True)
        return real(con)
    monkeypatch.setattr(bfr, "_scan_rows", touch_then_read)
    out = rf.dir / "out.json"
    with pytest.raises(bfc.SourceStateError, match="changed while it was read"):
        bmr.refresh(db, str(out))
    assert not out.exists()


def test_a_source_that_changes_before_validation_is_refused(rf, monkeypatch):
    db = rf.current()
    real = bmr._reconcile

    def write_then_reconcile(anchor, derived):
        con = sqlite3.connect(db)                  # broker_flow untouched, the file is not
        con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()
        return real(anchor, derived)
    monkeypatch.setattr(bmr, "_reconcile", write_then_reconcile)
    out = rf.dir / "out.json"
    with pytest.raises(bfc.SourceStateError, match="between the refresh and its validation"):
        bmr.refresh(db, str(out))
    assert not out.exists()


def test_the_database_is_never_written(rf, monkeypatch):
    db = rf.current()
    before, schema = sha(db), tc.schema_of(db)
    opened, statements = [], []
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append((str(database), kwargs.get("uri")))
        con = real_connect(database, *args, **kwargs)
        con.set_trace_callback(statements.append)
        return con
    with monkeypatch.context() as mp:
        mp.setattr(sqlite3, "connect", spy)
        rf.refresh(db)
    assert opened and all(uri and d.endswith("?mode=ro&immutable=1") for d, uri in opened)
    assert statements and all(s.lstrip().upper().startswith(("SELECT", "PRAGMA TABLE_INFO"))
                              for s in statements)
    assert sha(db) == before and tc.schema_of(db) == schema and tc.sidecars(db) == []


# --------------------------------------------------------------------------
# The PR #75 reader on the refreshed manifest
# --------------------------------------------------------------------------

def test_the_reader_accepts_the_refreshed_manifest(rf):
    db = rf.current()
    out, m, cf = rf.refresh(db)
    again = bfc.load_canonical_broker_flow(db, out)
    assert again.rows == cf.rows and not again.audited
    assert again.manifest_sha256 == hashlib.sha256(bfr.dumps(m).encode("ascii")).hexdigest()
    assert sum(v for k, v in again.accounting.items() if k != "raw") == again.accounting["raw"]
    before = rf.load()
    assert set(again.sessions) == set(before.sessions) | {"2026-09-30"}

    def untouched(view):
        return [r for r in view.rows if r.canonical_session_date not in ("2026-07-01", "2026-09-30")]
    assert untouched(again) == untouched(before)


def test_inferred_only_and_mixed_stay_excluded(rf):
    _, _, cf = rf.refresh(rf.current())
    assert {e.acquisition_date: e.date_class for e in cf.excluded} == \
        {"2026-08-12": bfr.MIXED, "2026-09-01": bfr.INFERRED_ONLY}
    assert {r.date_class for r in cf.rows} <= set(bfc.TRUSTED_CLASSES)


def test_missing_is_still_not_zero(rf):
    _, _, cf = rf.refresh(rf.current())
    assert cf.get("2026-07-06", *tc.ABSENT_0706) is None
    zero = cf.get("2026-09-29", *tc.ZERO_KEY)
    assert (zero.bval, zero.sval, zero.netval) == (0.0, 0.0, 0.0)
    for session in ("2026-07-10", "2026-08-24", "2026-08-11"):
        with pytest.raises(bfc.SessionNotCovered) as e:
            cf.get(session, "AAAA", "AK")
        assert e.value.status == bfc.NOT_COVERED


def test_duplicate_sessions_still_collapse_to_one_row_per_key(rf):
    _, _, cf = rf.refresh(rf.current())
    session = [r for r in cf.rows if r.canonical_session_date == "2026-07-07"]
    assert len(session) == len(tc.KEYS) == 12 and cf.duplicates_collapsed == 10
    kept, only_later = cf.get("2026-07-07", "CCCC", "AK"), cf.get("2026-07-07", "AAAA", "ZP")
    assert (kept.acquisition_date, kept.copy_acquisition_dates) == \
        ("2026-07-08", ("2026-07-08", "2026-07-09"))
    assert (only_later.acquisition_date, only_later.copy_acquisition_dates) == \
        ("2026-07-09", ("2026-07-09",))


def test_same_day_sessions_keep_their_proven_session(rf):
    _, _, cf = rf.refresh(rf.current())
    rows = [r for r in cf.rows if r.acquisition_date == "2026-07-06"]
    assert len(rows) == 11 and {r.canonical_session_date for r in rows} == {"2026-07-06"}


# --------------------------------------------------------------------------
# Real data: the pinned 2026-10-01 snapshot and the audited baseline
# --------------------------------------------------------------------------

CURRENT_COMMIT = "6f21a72f7c0c06d1684184a51f0587d2d9590d6f"      # master, 2026-10-01
CURRENT_BLOB = "c2f839adcb2f24c88a76c0bfccd933401f468488"         # its neobdm.db
CURRENT_SHA256 = "df3d94af6e8f7fc91dd7802de2d0694d6b86b5ac0aa34c9571d7be5cd6a16abf"


def git_db(commit, dst, blob=None):
    got = subprocess.run(["git", "rev-parse", f"{commit}:neobdm.db"], cwd=HERE,
                         capture_output=True).stdout.decode().strip()
    if len(got) != 40:
        pytest.skip(f"git history for {commit[:7]}:neobdm.db is not available")
    assert blob is None or got == blob
    with open(dst, "wb") as f:
        assert subprocess.run(["git", "cat-file", "blob", got], cwd=HERE, stdout=f).returncode == 0
    return str(dst)


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    folder = tmp_path_factory.mktemp("real_refresh")
    db = git_db(CURRENT_COMMIT, folder / "neobdm.db", CURRENT_BLOB)
    assert sha(db) == CURRENT_SHA256
    out = str(folder / "refreshed.json")
    anchor_before = sha(COMMITTED)
    m, cf = bmr.refresh(db, out, source_commit=CURRENT_COMMIT)
    assert sha(db) == CURRENT_SHA256 and sha(COMMITTED) == anchor_before
    assert tc.sidecars(db) == []
    with open(COMMITTED, encoding="ascii") as f:
        anchor = json.load(f)
    return db, out, m, cf, anchor


# Discovered by inspecting the pinned snapshot (HANDOFF Lampiran U): one new LIVE
# date, 2026-10-01, proven by its own PERSISTED run (29 brokers, 947 rows) to hold
# the 2026-09-30 session; 173 of 218 backfill dates topped up (2025-10-06 ..
# 2026-07-03, the API's rolling window up to BACKFILL_END); no historical LIVE
# group changed. 233,549 raw rows = 221,399 canonical + 3,642 identical copies +
# 1,252 quarantined (07-03) + 7,256 excluded.
REAL = {"sessions": 251, "rows": 221_399, "backfill_rows": 211_914, "live_rows": 9_485,
        "live_sessions": 34, "duplicates": 3_642, "quarantined_rows": 1_252,
        "excluded_records": 37, "excluded_rows": 7_256, "raw_rows": 233_549}


def test_real_refresh_of_the_pinned_2026_10_01_db(real):
    db, out, m, cf, anchor = real
    assert m["refresh"]["anchor"] == {
        "manifest_sha256": bmr.AUDITED_ANCHOR_SHA256, "snapshot": "audited-2026-09-30",
        "source_commit": bfr.AUDITED_SNAPSHOT["source_commit"], "records": 305,
        "last_broker_flow_date": "2026-09-30"}
    assert m["refresh"]["current_db"] == {"sha256": CURRENT_SHA256, "bytes": os.path.getsize(db)}
    assert m["input"]["source_commit"] == CURRENT_COMMIT
    inh = m["refresh"]["inheritance"]
    assert inh["anchored_live"] == {bfr.CONTENT_MATCHED: 49, bfr.INFERRED_ONLY: 35, bfr.MIXED: 2,
                                    bfr.SCAN_VERIFIED: 1}
    assert inh["new_live"] == {"2026-10-01": bfr.SCAN_VERIFIED}
    changed = inh["backfill"]["changed_since_anchor"]
    assert (inh["backfill"]["rederived"], len(changed), changed[0], changed[-1]) == \
        (218, 173, "2025-10-06", "2026-07-03")
    assert (m["input"]["broker_flow"]["rows"], len(m["records"])) == (REAL["raw_rows"], 306)
    assert m["summary"]["by_date_class"] == {
        bfr.SCAN_VERIFIED: {"dates": 2, "rows": 1_912},
        bfr.SOURCE_DATED_BACKFILL: {"dates": 218, "rows": 212_948},
        bfr.CONTENT_MATCHED: {"dates": 49, "rows": 11_433},
        bfr.INFERRED_ONLY: {"dates": 35, "rows": 6_655},
        bfr.MIXED: {"dates": 2, "rows": 601}}
    new = by_key(m)[NEW]
    assert (new["date_class"], new["canonical_session_date"], new["row_count"],
            new["scan"]["run_started_utc"], new["scan"]["codes"], new["scan"]["calendar_version"]) == \
        (bfr.SCAN_VERIFIED, "2026-09-30", 947, "2026-10-01T01:51:07.838116+00:00", 29,
         ["idx-2026-2027.v1"])
    # every historical LIVE record is the anchor's, byte for byte
    anchored = by_key(anchor)
    assert all(by_key(m)[k] == r for k, r in anchored.items() if k[1] == LIVE)
    assert {k for k, r in by_key(m).items() if r["date_class"] == bfr.CONTENT_MATCHED} == \
        {k for k, r in anchored.items() if r["date_class"] == bfr.CONTENT_MATCHED}


def test_real_canonical_reader_loads_the_refreshed_manifest(real):
    db, out, m, cf, _ = real
    again = bfc.load_canonical_broker_flow(db, out)
    assert again.rows == cf.rows and not again.audited and again.db_sha256 == CURRENT_SHA256
    assert (len(cf.sessions), len(cf.rows)) == (REAL["sessions"], REAL["rows"])
    from collections import Counter
    assert Counter(cf.sessions.values()) == {bfr.SELECTOR_UNION_BACKFILL: 217, bfr.DOM_TOP15: 32,
                                             bfr.FULL_CALLBACK: 2}
    live = [r for r in cf.rows if r.regime == LIVE]
    assert (len(cf.rows) - len(live), len(live), len({r.canonical_session_date for r in live})) == \
        (REAL["backfill_rows"], REAL["live_rows"], REAL["live_sessions"])
    assert cf.accounting == {"canonical": REAL["rows"], "duplicate": REAL["duplicates"],
                             "quarantined": REAL["quarantined_rows"],
                             "excluded": REAL["excluded_rows"], "raw": REAL["raw_rows"]}
    assert (len(cf.excluded), sum(e.row_count for e in cf.excluded)) == \
        (REAL["excluded_records"], REAL["excluded_rows"])
    assert {e.date_class for e in cf.excluded} == {bfr.INFERRED_ONLY, bfr.MIXED}
    assert (max(cf.sessions), cf.sessions["2026-09-30"], cf.sessions["2026-09-29"]) == \
        ("2026-09-30", bfr.FULL_CALLBACK, bfr.FULL_CALLBACK)
    assert {r.acquisition_date for r in cf.rows if r.canonical_session_date == "2026-09-30"} == \
        {"2026-10-01"}


def test_real_07_03_stays_quarantined(real):
    _, _, m, cf, anchor = real
    bf = (by_key(m)[("2026-07-03", BACKFILL)], by_key(anchor)[("2026-07-03", BACKFILL)])
    assert bf[0]["row_count"] == bf[1]["row_count"] == 1_034
    assert bf[0]["rows_sha256"] != bf[1]["rows_sha256"]          # topped up since the audit
    [q] = cf.quarantined
    assert (q.canonical_session_date, q.reasons, q.conflicting_keys, q.rows) == \
        ("2026-07-03", (bfc.CAPTURE_CLASSES_DIFFER, bfc.VALUES_CONFLICT), 168, 1_252)
    assert [(a, regime, cls) for a, regime, cls, *_ in q.records] == \
        [("2026-07-03", BACKFILL, bfr.SOURCE_DATED_BACKFILL), ("2026-07-05", LIVE, bfr.CONTENT_MATCHED)]
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get("2026-07-03", "BNBR", "AK")
    assert e.value.status == bfc.QUARANTINED


def test_real_reader_behaviour_is_unchanged_by_the_refresh(real):
    _, _, m, cf, _ = real
    for d in tc.SAME_DAY:
        rows = [r for r in cf.rows if r.acquisition_date == d]
        assert rows and {r.canonical_session_date for r in rows} == {d}
    held = m["summary"]["live_sessions_held_by_several_dates"]
    assert set(held) == set(tc.COPIES)
    for session, n in tc.COPIES.items():
        rows = [r for r in cf.rows if r.canonical_session_date == session]
        assert len(rows) == n
        assert {a for r in rows for a in r.copy_acquisition_dates} == set(held[session])
    assert cf.get("2026-08-21", "BNBR", "AO").copy_acquisition_dates == ("2026-08-23", "2026-08-24")
    assert {r.acquisition_date for r in cf.rows if r.canonical_session_date == "2026-08-21"} == \
        {"2026-08-22", "2026-08-23"}
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get("2026-07-08", "BBCA", "AK")
    assert e.value.status == bfc.NOT_COVERED
    zeros = [r for r in cf.rows if r.regime == LIVE and r.bval == 0 and r.sval == 0
             and (r.bavg or 0) > 0]
    assert zeros and all(cf.get(r.canonical_session_date, r.ticker, r.broker_code) is r
                         for r in zeros[:50])
    inferred = {r["broker_flow_date"] for r in m["records"] if r["date_class"] == bfr.INFERRED_ONLY}
    assert len(inferred) == 35 and not [r for r in cf.rows if inferred & set(r.copy_acquisition_dates)]
    assert not [s for s in cf.sessions if "2026-08-21" < s < "2026-09-29"]


def test_real_refresh_is_deterministic(real, tmp_path):
    db, out, _, _, _ = real
    again = str(tmp_path / "again.json")
    bmr.refresh(db, again, source_commit=CURRENT_COMMIT)
    assert open(again, "rb").read() == open(out, "rb").read()


def test_real_weaker_manifest_keeps_07_03_quarantined(real, tmp_path):
    """PR #74 without the parquet on the 2026-10-01 database: 07-05 is only
    INFERRED_ONLY and the 07-03 backfill hash is no longer the audited one, yet
    the session is not exposed from the backfill side."""
    db = real[0]
    path = str(tmp_path / "no_parquet.json")
    bfr.write_manifest(bfr.build_manifest(db, None), path)
    cf = bfc.load_canonical_broker_flow(db, path)
    [q] = cf.quarantined
    assert (q.canonical_session_date, q.rows) == ("2026-07-03", 1_252)
    assert "2026-07-03" not in cf.sessions


def test_real_refresh_of_the_audited_baseline_reproduces_the_anchor(tmp_path):
    db = git_db(bfr.AUDITED_SNAPSHOT["source_commit"], tmp_path / "neobdm.db")
    m, cf = bmr.refresh(db, str(tmp_path / "baseline.json"))
    with open(COMMITTED, encoding="ascii") as f:
        anchor = json.load(f)
    assert m["records"] == anchor["records"]
    assert m["refresh"]["inheritance"]["backfill"]["changed_since_anchor"] == []
    assert m["refresh"]["inheritance"]["new_live"] == {}
    assert cf.accounting == {"canonical": tc.REAL["rows"], "duplicate": tc.REAL["duplicates"],
                             "quarantined": tc.REAL["quarantined_rows"],
                             "excluded": tc.REAL["excluded_rows"], "raw": tc.REAL["raw_rows"]}
