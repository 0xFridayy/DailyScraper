"""walk_forward_backtest.build_panel() on the canonical broker flow (HANDOFF
Lampiran V): the reference consumer migration.

build_panel() reads broker flow only through
broker_flow_canonical.load_canonical_broker_flow() under an explicit manifest,
keys features by canonical_session_date, and refuses rather than falls back.

Also the orchestrator contract this migration introduced (check_ml_health
CLI, short point-in-time warnings). Runs in the ml-health workflow.

Synthetic fixtures: the PR #75 fixture (test_broker_flow_canonical.Synth,
classified by the real PR #74 generator and pinned as the anchor) with price
tables added to the same file, and small backfill-only databases whose
manifests the generator builds unpinned. The real-data tests read neobdm.db
from git at the pinned 2026-10-01 blob and refresh a manifest for it with
broker_flow_manifest_refresh; they skip only when that git object is absent
(a shallow clone), which means they did not run."""

import ast
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import broker_flow_canonical as bfc
import broker_flow_manifest_refresh as bmr
import broker_flow_regime as bfr
import check_ml_health as chk
import walk_forward_backtest as wfb
from price_audit import clean_panel
from test_broker_flow_canonical import ABSENT_0706, KEYS, SUBSET_0708, ZERO_KEY, Synth, digest, sha

HERE = os.path.dirname(os.path.abspath(__file__))
WFB_SOURCE = os.path.join(HERE, "walk_forward_backtest.py")
BROKER_FEATURES = ["broker_concentration", "net_flow_total", "n_brokers", "net_buy_ratio",
                   "retail_presence_pct"]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def weekdays(start, end):
    d, out = date.fromisoformat(start), []
    while d <= date.fromisoformat(end):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def add_prices(db, tickers, sessions, quarantine=()):
    """Smooth, limit-band-safe OHLCV for every (ticker, session), plus an
    explicit price_quarantine table (so load_clean never runs its detectors)."""
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE price_history (date TEXT NOT NULL, ticker TEXT NOT NULL, open REAL, "
                "high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (date, ticker))")
    con.execute("CREATE TABLE price_quarantine (date TEXT, ticker TEXT)")
    rows = []
    for k, t in enumerate(tickers):
        base = 1000.0 + 250 * k
        for i, d in enumerate(sessions):
            close = round(base * (1 + 0.02 * math.sin(i * 0.9 + k)), 2)
            rows.append((d, t, round(close * 0.998, 2), round(close * 1.01, 2),
                         round(close * 0.99, 2), close, 1e6 * (1 + 0.1 * ((i + k) % 4))))
    con.executemany("INSERT INTO price_history VALUES (?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO price_quarantine VALUES (?,?)", list(quarantine))
    con.commit()
    con.close()


SYNTH_SESSIONS = weekdays("2026-06-15", "2026-10-09")
TICKERS = sorted({t for t, _ in KEYS})


@pytest.fixture
def synth(tmp_path, monkeypatch):
    s = Synth(tmp_path, monkeypatch)
    add_prices(s.db, TICKERS, SYNTH_SESSIONS)
    return s


def panel_of(db, manifest, broker_db=None):
    conn = wfb.connect_price_db(db)
    try:
        return wfb.build_panel(conn, broker_flow_db_path=broker_db or db,
                               broker_flow_manifest_path=manifest)
    finally:
        conn.close()


def backfill_db(directory, flows, sessions, tickers, name="neobdm.db", quarantine=(),
                unique=True):
    """A backfill-shaped database (bval NULL, dated <= BACKFILL_END: every date
    is SOURCE_DATED_BACKFILL) with prices, and the manifest the real generator
    builds for it, unpinned. flows: {(date, ticker, broker): netval or None}."""
    db = os.path.join(directory, name)
    con = sqlite3.connect(db)
    pk = ", PRIMARY KEY (date, ticker, broker_code)" if unique else ""
    con.execute("CREATE TABLE broker_flow (date TEXT NOT NULL, ticker TEXT NOT NULL, "
                "broker_code TEXT NOT NULL, bval REAL, sval REAL, netval REAL, bavg REAL, "
                f"savg REAL{pk})")
    con.executemany("INSERT INTO broker_flow VALUES (?,?,?,NULL,NULL,?,NULL,NULL)",
                    [(d, t, b, v) for (d, t, b), v in sorted(flows.items())])
    con.commit()
    con.close()
    add_prices(db, tickers, sessions, quarantine)
    manifest = os.path.join(directory, name + ".manifest.json")
    bfr.write_manifest(bfr.build_manifest(db), manifest)
    return db, manifest


BF_SESSIONS = weekdays("2026-03-02", "2026-05-29")
BF_BROKERS = ["AK", "BK", "CC", "XL", "ZP"]


def bf_flows(sessions, tickers=("AAA", "BBB"), brokers=BF_BROKERS, skip=()):
    """Deterministic backfill netvals; `skip` holds (date, ticker, broker)
    triples, (date, ticker) pairs or bare dates that have no rows at all."""
    out = {}
    for i, d in enumerate(sessions):
        for k, t in enumerate(tickers):
            for j, b in enumerate(brokers):
                if d in skip or (d, t) in skip or (d, t, b) in skip:
                    continue
                out[(d, t, b)] = round(((i * 7 + j * 13 + k * 5) % 23 - 11) * 0.37, 4)
    return out


# --------------------------------------------------------------------------
# The canonical input contract (adapter)
# --------------------------------------------------------------------------

def test_frame_maps_canonical_session_date_to_date_one_row_per_canonical_row(synth):
    cf = synth.load()
    frame = wfb.canonical_broker_flow_frame(cf)
    assert list(frame.columns) == ["date", "ticker", "broker_code", "netval"]
    assert len(frame) == len(cf.rows) == 32
    want = [(r.canonical_session_date, r.ticker, r.broker_code, r.netval) for r in cf.rows]
    got = list(frame.itertuples(index=False, name=None))
    assert got == want
    assert sorted(frame["date"].unique()) == ["2026-07-01", "2026-07-06", "2026-07-07",
                                              "2026-09-29"] == sorted(cf.sessions)


def test_acquisition_date_is_never_the_feature_date(synth):
    cf = synth.load()
    frame = wfb.canonical_broker_flow_frame(cf)
    moved = [r for r in cf.rows if r.acquisition_date != r.canonical_session_date]
    assert {(r.acquisition_date, r.canonical_session_date) for r in moved} == \
        {("2026-07-08", "2026-07-07"), ("2026-07-09", "2026-07-07"), ("2026-09-30", "2026-09-29")}
    # no acquisition-only date ever becomes a feature date
    assert not set(frame["date"]) & {"2026-07-08", "2026-07-09", "2026-09-30", "2026-07-15",
                                     "2026-07-16", "2026-07-04", "2026-07-05"}
    panel = panel_of(synth.db, synth.mpath)
    assert "2026-09-30" not in set(panel["date"]) and "2026-09-29" in set(panel["date"])
    assert not set(panel["date"]) & {"2026-07-08", "2026-07-09"}


def test_identical_acquisition_copies_are_not_summed_and_the_union_is_kept_once(synth):
    cf = synth.load()
    frame = wfb.canonical_broker_flow_frame(cf)
    s0707 = frame[frame["date"] == "2026-07-07"]
    assert len(s0707) == len(KEYS) == 12
    assert not s0707.duplicated(["ticker", "broker_code"]).any()
    # keys held only by the 07-09 copy are kept, once
    for t, b in SUBSET_0708:
        got = s0707[(s0707["ticker"] == t) & (s0707["broker_code"] == b)]
        assert len(got) == 1 and cf.get("2026-07-07", t, b).copy_acquisition_dates == ("2026-07-09",)
    # every value is the single observed copy, never copy + copy
    con = bfr.connect_readonly(synth.db)
    raw = {(t, b): v for t, b, v in con.execute(
        "SELECT ticker, broker_code, netval FROM broker_flow WHERE date = '2026-07-09'")}
    con.close()
    assert {(t, b): v for t, b, v in s0707[["ticker", "broker_code", "netval"]]
            .itertuples(index=False)} == raw
    panel = panel_of(synth.db, synth.mpath).set_index(["ticker", "date"])
    aaaa = panel.loc[("AAAA", "2026-07-07")]
    assert aaaa["n_brokers"] == 2
    assert aaaa["net_flow_total"] == pytest.approx(raw[("AAAA", "AK")] + raw[("AAAA", "ZP")])


@pytest.mark.parametrize("acq, cls", [("2026-09-01", bfr.INFERRED_ONLY), ("2026-08-12", bfr.MIXED)])
def test_unproven_evidence_never_reaches_the_panel(synth, acq, cls):
    cf = synth.load()
    assert {e.acquisition_date: e.date_class for e in cf.excluded}[acq] == cls
    frame = wfb.canonical_broker_flow_frame(cf)
    panel = panel_of(synth.db, synth.mpath)
    rec = {(r["broker_flow_date"], r["regime"]): r for r in synth.manifest["records"]}[(acq, bfr.LIVE)]
    unproven = {acq, rec["inferred_session_date"], "2026-08-11"} - {None}
    assert not set(frame["date"]) & unproven
    assert not set(panel["date"]) & unproven


@pytest.mark.parametrize("session", ["2026-07-02", "2026-07-03", "2026-07-14"])
def test_quarantined_sessions_contribute_nothing(synth, session):
    cf = synth.load()
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get(session, "AAAA", "AK")
    assert e.value.status == bfc.QUARANTINED
    assert session not in set(wfb.canonical_broker_flow_frame(cf)["date"])
    assert session not in set(panel_of(synth.db, synth.mpath)["date"])


def test_the_session_after_a_withheld_session_has_no_one_day_correlation(tmp_path):
    # 2026-04-08 has no broker rows at all (a withheld session); BBB is also
    # absent on 2026-04-15 while AAA is observed (a ticker-level gap).
    gap, ticker_gap = "2026-04-08", ("2026-04-15", "BBB")
    db, man = backfill_db(str(tmp_path), bf_flows(BF_SESSIONS, skip={gap, ticker_gap}),
                          BF_SESSIONS, ["AAA", "BBB"])
    panel = panel_of(db, man).set_index(["ticker", "date"])
    after = "2026-04-09"
    assert np.isnan(panel.loc[("AAA", after), "broker_correlation_1d"])
    assert panel["broker_correlation_1d"].notna().sum() > 0.8 * len(panel)
    # without the withheld-session rule the helper would have compared 04-09 with 04-07
    bf = wfb.canonical_broker_flow_frame(bfc.load_canonical_broker_flow(db, man))
    raw_corr = wfb._broker_correlation_1d(bf).set_index(["ticker", "date"])
    assert not np.isnan(raw_corr.loc[("AAA", after), "broker_correlation_1d"])
    # a ticker missing from a covered session keeps the helper's own behaviour
    assert panel.loc[("BBB", "2026-04-16"), "broker_correlation_1d"] == pytest.approx(
        raw_corr.loc[("BBB", "2026-04-16"), "broker_correlation_1d"])


def test_same_day_live_acquisitions_stay_on_their_own_session(synth):
    cf = synth.load()
    rows = [r for r in cf.rows if r.acquisition_date == "2026-07-06"]
    assert len(rows) == 11 and {r.canonical_session_date for r in rows} == {"2026-07-06"}
    frame = wfb.canonical_broker_flow_frame(cf)
    assert len(frame[frame["date"] == "2026-07-06"]) == 11
    assert "2026-07-03" not in set(frame["date"])   # not shifted a session back


def test_absent_brokers_stay_absent(synth):
    frame = wfb.canonical_broker_flow_frame(synth.load())
    t, b = ABSENT_0706
    assert frame[(frame["date"] == "2026-07-06") & (frame["ticker"] == t) &
                 (frame["broker_code"] == b)].empty
    panel = panel_of(synth.db, synth.mpath).set_index(["ticker", "date"])
    assert panel.loc[(t, "2026-07-06"), "n_brokers"] == 1
    assert panel.loc[(t, "2026-07-07"), "n_brokers"] == 2


def test_observed_zero_stays_an_observed_row(synth):
    frame = wfb.canonical_broker_flow_frame(synth.load())
    t, b = ZERO_KEY
    row = frame[(frame["date"] == "2026-09-29") & (frame["ticker"] == t) & (frame["broker_code"] == b)]
    assert len(row) == 1 and row["netval"].iloc[0] == 0.0
    panel = panel_of(synth.db, synth.mpath).set_index(["ticker", "date"])
    assert panel.loc[(t, "2026-09-29"), "n_brokers"] == 1


def test_null_netval_stays_nan_and_is_still_an_observed_broker(tmp_path):
    flows = bf_flows(BF_SESSIONS)
    flows[("2026-04-01", "AAA", "CC")] = None
    db, man = backfill_db(str(tmp_path), flows, BF_SESSIONS, ["AAA", "BBB"])
    cf = bfc.load_canonical_broker_flow(db, man)
    assert cf.get("2026-04-01", "AAA", "CC").netval is None
    frame = wfb.canonical_broker_flow_frame(cf)
    cell = frame[(frame["date"] == "2026-04-01") & (frame["ticker"] == "AAA") &
                 (frame["broker_code"] == "CC")]["netval"]
    assert len(cell) == 1 and np.isnan(cell.iloc[0]) and frame["netval"].dtype == np.float64
    panel = panel_of(db, man).set_index(["ticker", "date"])
    row = panel.loc[("AAA", "2026-04-01")]
    assert row["n_brokers"] == len(BF_BROKERS)
    observed = [v for (d, t, _), v in flows.items() if (d, t) == ("2026-04-01", "AAA") and v is not None]
    assert row["net_flow_total"] == pytest.approx(sum(observed))


def test_adapter_refuses_anything_but_a_canonical_view(synth):
    with pytest.raises(TypeError, match="CanonicalBrokerFlow"):
        wfb.canonical_broker_flow_frame(pd.DataFrame(columns=wfb.CANONICAL_FRAME_COLUMNS))


# --------------------------------------------------------------------------
# Explicit manifest, snapshot identity, failure behaviour
# --------------------------------------------------------------------------

def test_the_manifest_is_required(synth):
    conn = wfb.connect_price_db(synth.db)
    try:
        with pytest.raises(TypeError):
            wfb.build_panel(conn)
        for missing in (None, "", "  "):
            with pytest.raises(wfb.BrokerFlowManifestRequired, match="broker_flow_manifest_refresh.py"):
                wfb.build_panel(conn, broker_flow_db_path=synth.db, broker_flow_manifest_path=missing)
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="broker_flow_db_path"):
            wfb.build_panel(conn, broker_flow_db_path=None, broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()


def test_the_cli_refuses_without_a_manifest(capsys):
    with pytest.raises(SystemExit) as e:
        wfb.parse_cli([])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--broker-flow-manifest is required" in err and "broker_flow_manifest_refresh.py" in err
    args = wfb.parse_cli(["--broker-flow-manifest", "m.json", "--db", "x.db"])
    assert (args.broker_flow_manifest, args.db) == ("m.json", "x.db")


def test_failures_propagate_unchanged_and_never_fall_back(synth, tmp_path):
    missing = str(tmp_path / "nowhere.json")
    with pytest.raises(FileNotFoundError):
        panel_of(synth.db, missing)
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="ascii")
    with pytest.raises(bfc.ManifestInvalid):
        panel_of(synth.db, str(garbage))
    edited = synth.edited(lambda recs, m: recs[("2026-07-06", bfr.LIVE)].update(
        date_class=bfr.SCAN_VERIFIED))
    with pytest.raises(bfc.ManifestInvalid):
        panel_of(synth.db, edited)


def test_a_manifest_that_does_not_describe_the_database_is_refused(synth):
    db = synth.db_copy("changed.db", "UPDATE broker_flow SET netval = netval + 1 "
                                     "WHERE date = '2026-07-06' AND ticker = 'AAAA'")
    with pytest.raises(bfc.ManifestMismatch) as e:
        panel_of(db, synth.mpath)
    assert e.value.diff["content_changed"] == [("2026-07-06", bfr.LIVE)]


def test_invalid_scan_evidence_is_refused(synth):
    db = synth.db_copy("noscan.db", "DELETE FROM broker_flow_scan")
    with pytest.raises(bfc.ManifestMismatch, match="broker_flow_scan"):
        panel_of(db, synth.mpath)


def test_a_non_quiescent_source_is_refused(synth):
    open(synth.db + "-journal", "wb").close()
    try:
        with pytest.raises(bfc.SourceStateError, match="-journal"):
            panel_of(synth.db, synth.mpath)
    finally:
        os.remove(synth.db + "-journal")


def test_duplicate_raw_source_keys_are_refused(tmp_path):
    flows = bf_flows(BF_SESSIONS[:5])
    db, man = backfill_db(str(tmp_path), flows, BF_SESSIONS, ["AAA", "BBB"], unique=False)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO broker_flow SELECT * FROM broker_flow WHERE rowid = 1")
    con.commit()
    con.close()
    bfr.write_manifest(bfr.build_manifest(db), man)   # a manifest that describes the duplicate
    with pytest.raises(bfc.SourceStateError, match="duplicate raw keys"):
        panel_of(db, man)


def test_price_connection_and_broker_flow_must_be_one_snapshot(synth, tmp_path):
    # B: the same broker_flow (so the same manifest describes it), other prices
    other = synth.db_copy("other_prices.db", "UPDATE price_history SET close = close * 1.01 "
                                             "WHERE date = '2026-07-06'")
    bfc.load_canonical_broker_flow(other, synth.mpath)            # the manifest is valid for B
    with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
        panel_of(synth.db, synth.mpath, broker_db=other)          # prices A, broker flow B
    with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
        panel_of(other, synth.mpath, broker_db=synth.db)          # prices B, broker flow A
    # byte identity, not path identity: an exact copy is the same snapshot
    twin = str(tmp_path / "twin.db")
    shutil.copy(synth.db, twin)
    a = panel_of(twin, synth.mpath, broker_db=synth.db)
    pd.testing.assert_frame_equal(a, panel_of(synth.db, synth.mpath))


def test_unverifiable_price_connections_are_refused(synth, tmp_path):
    mem = sqlite3.connect(":memory:")
    try:
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not file-backed"):
            wfb.build_panel(mem, broker_flow_db_path=synth.db, broker_flow_manifest_path=synth.mpath)
    finally:
        mem.close()
    conn = wfb.connect_price_db(synth.db)
    try:
        conn.execute("ATTACH DATABASE ':memory:' AS other")
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="attached"):
            wfb.build_panel(conn, broker_flow_db_path=synth.db, broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()
    # uncommitted writes are visible to the connection but not in the database image
    conn = sqlite3.connect(synth.db)
    try:
        conn.execute("UPDATE price_history SET close = close * 1.01 WHERE date = '2026-07-06'")
        assert conn.in_transaction
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="open transaction"):
            wfb.build_panel(conn, broker_flow_db_path=synth.db, broker_flow_manifest_path=synth.mpath)
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("ddl", [
    "CREATE TEMP TABLE price_history (x)",
    "CREATE TEMP TABLE PRICE_HISTORY AS SELECT * FROM main.price_history",
    "CREATE TEMP TABLE Price_History AS SELECT * FROM main.price_history",
    "CREATE TEMP TABLE PRICE_QUARANTINE (date TEXT, ticker TEXT)",
    "CREATE TEMP VIEW PRICE_HISTORY AS SELECT * FROM main.price_history",
    "CREATE TEMP VIEW Price_Quarantine AS SELECT * FROM main.price_quarantine",
])
def test_temp_objects_shadowing_the_price_tables_are_refused_in_any_case(synth, ddl):
    conn = wfb.connect_price_db(synth.db)
    try:
        conn.execute(ddl)
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="shadow"):
            wfb.build_panel(conn, broker_flow_db_path=synth.db, broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()


def test_unrelated_temp_objects_are_allowed(synth):
    conn = wfb.connect_price_db(synth.db)
    try:
        conn.execute("CREATE TEMP TABLE scratch_notes (x)")
        conn.execute("CREATE TEMP VIEW price_history_recent AS SELECT * FROM main.price_history")
        panel = wfb.build_panel(conn, broker_flow_db_path=synth.db,
                                broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()
    pd.testing.assert_frame_equal(panel, panel_of(synth.db, synth.mpath))


# --------------------------------------------------------------------------
# The price connection's actual SQLite snapshot
# --------------------------------------------------------------------------

def image_sha256(path):
    con = bfr.connect_readonly(path)
    try:
        return hashlib.sha256(con.serialize(name="main")).hexdigest()
    finally:
        con.close()


def wal_canonical(synth):
    """synth.db in WAL format with no sidecars left (a clean close checkpoints
    and removes them); the manifest still describes its unchanged rows. A price
    connection on this same file would create -wal/-shm beside it, which the
    canonical reader refuses as non-quiescent, so prices come from a copy."""
    con = sqlite3.connect(synth.db)
    assert con.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    con.close()
    assert not [s for s in ("-wal", "-shm") if os.path.exists(synth.db + s)]
    bfc.load_canonical_broker_flow(synth.db, synth.mpath)
    return synth.db


def test_the_ordinary_snapshot_passes_and_only_build_panels_transaction_is_ended(synth):
    conn = wfb.connect_price_db(synth.db)
    try:
        panel = wfb.build_panel(conn, broker_flow_db_path=synth.db,
                                broker_flow_manifest_path=synth.mpath)
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM price_history").fetchone()[0] > 0
    finally:
        conn.close()
    assert len(panel) > 0


def test_a_wal_price_change_behind_identical_main_file_bytes_is_refused(synth, tmp_path):
    baseline = panel_of(synth.db, synth.mpath)
    canonical = wal_canonical(synth)
    price = str(tmp_path / "price_wal.db")
    shutil.copy(canonical, price)
    # a WAL-mode twin is the same snapshot while its WAL is empty
    pd.testing.assert_frame_equal(panel_of(price, synth.mpath, broker_db=canonical), baseline)
    writer = sqlite3.connect(price)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE price_history SET close = close * 1.05 WHERE date = '2026-07-06'")
        writer.commit()
        assert sha(price) == sha(canonical), "the change lives only in the WAL"
        conn = wfb.connect_price_db(price)
        try:
            seen = conn.execute("SELECT close FROM price_history WHERE date = '2026-07-06' "
                                "ORDER BY ticker").fetchall()
            assert seen != image_rows(canonical), "the connection reads the WAL-updated prices"
            with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
                wfb.build_panel(conn, broker_flow_db_path=canonical,
                                broker_flow_manifest_path=synth.mpath)
            assert not conn.in_transaction
        finally:
            conn.close()
    finally:
        writer.close()


def image_rows(path):
    con = bfr.connect_readonly(path)
    try:
        return con.execute("SELECT close FROM price_history WHERE date = '2026-07-06' "
                           "ORDER BY ticker").fetchall()
    finally:
        con.close()


@pytest.mark.skipif(os.name == "nt", reason="an open file cannot be replaced on Windows")
def test_a_stale_open_connection_after_path_replacement_is_refused(synth, tmp_path):
    other = synth.db_copy("other.db", "UPDATE price_history SET close = close * 1.05 "
                                      "WHERE date = '2026-07-06'")
    conn = wfb.connect_price_db(other)
    try:
        assert conn.execute("SELECT count(*) FROM price_history").fetchone()[0] > 0
        staged = str(tmp_path / "staged.db")
        shutil.copy(synth.db, staged)
        os.replace(staged, other)                      # the pathname now names canonical bytes
        assert sha(other) == sha(synth.db)
        assert conn.execute("SELECT close FROM price_history WHERE date = '2026-07-06' "
                            "ORDER BY ticker").fetchall() != image_rows(other)
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
            wfb.build_panel(conn, broker_flow_db_path=other, broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()


def test_the_snapshot_cannot_change_under_clean_panel_unnoticed(synth, monkeypatch):
    real_clean = wfb.clean_panel

    def ends_the_snapshot(conn, *a, **k):
        conn.execute("ROLLBACK")
        w = sqlite3.connect(synth.db)
        w.execute("UPDATE price_history SET close = close * 1.05 WHERE date = '2026-07-06'")
        w.commit()
        w.close()
        return real_clean(conn, *a, **k)

    monkeypatch.setattr(wfb, "clean_panel", ends_the_snapshot)
    conn = wfb.connect_price_db(synth.db)
    try:
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="did not hold"):
            wfb.build_panel(conn, broker_flow_db_path=synth.db, broker_flow_manifest_path=synth.mpath)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_a_rollback_journal_writer_cannot_commit_during_the_read(synth, monkeypatch):
    real_clean = wfb.clean_panel
    blocked = []

    def concurrent_writer(conn, *a, **k):
        w = sqlite3.connect(synth.db, timeout=0)
        try:
            w.execute("UPDATE price_history SET close = close * 1.05 WHERE date = '2026-07-06'")
            w.commit()
        except sqlite3.OperationalError as e:
            blocked.append(str(e))
            w.rollback()
        finally:
            w.close()
        return real_clean(conn, *a, **k)

    baseline = panel_of(synth.db, synth.mpath)
    monkeypatch.setattr(wfb, "clean_panel", concurrent_writer)
    pd.testing.assert_frame_equal(panel_of(synth.db, synth.mpath), baseline)
    assert blocked and "locked" in blocked[0]


def test_a_wal_writer_during_the_read_is_invisible_to_it(synth, tmp_path, monkeypatch):
    baseline = panel_of(synth.db, synth.mpath)
    canonical = wal_canonical(synth)
    price = str(tmp_path / "price_wal.db")
    shutil.copy(canonical, price)
    real_clean = wfb.clean_panel
    writers = []

    def concurrent_commit(conn, *a, **k):
        w = sqlite3.connect(price)
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("UPDATE price_history SET close = close * 1.05 WHERE date = '2026-07-06'")
        w.commit()
        writers.append(w)
        return real_clean(conn, *a, **k)

    monkeypatch.setattr(wfb, "clean_panel", concurrent_commit)
    conn = wfb.connect_price_db(price)
    try:
        panel = wfb.build_panel(conn, broker_flow_db_path=canonical,
                                broker_flow_manifest_path=synth.mpath)
        pd.testing.assert_frame_equal(panel, baseline)
        assert panel.attrs["broker_flow"]["image_sha256"] == image_sha256(canonical)
        monkeypatch.setattr(wfb, "clean_panel", real_clean)
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
            wfb.build_panel(conn, broker_flow_db_path=canonical,
                            broker_flow_manifest_path=synth.mpath)   # the next read sees it
    finally:
        conn.close()
        for w in writers:
            w.close()


def test_no_usable_broker_rows_is_refused(tmp_path, synth, monkeypatch):
    # broker sessions that meet no clean price session
    flows = bf_flows(weekdays("2026-02-02", "2026-02-27"))
    db, man = backfill_db(str(tmp_path), flows, BF_SESSIONS, ["AAA", "BBB"], name="nocover.db")
    with pytest.raises(wfb.BrokerFlowUnavailable, match="no canonical broker-flow session"):
        panel_of(db, man)
    # a view whose only sessions are quarantined has no trusted rows at all
    real_load = bfc.load_canonical_broker_flow

    def quarantined_only(*a, **k):
        cf = real_load(*a, **k)
        return bfc.CanonicalBrokerFlow(
            rows=(), sessions={}, excluded=cf.excluded, quarantined=cf.quarantined,
            accounting=cf.accounting, db_path=cf.db_path, db_sha256=cf.db_sha256,
            manifest_path=cf.manifest_path, manifest_sha256=cf.manifest_sha256,
            audited=cf.audited, snapshot=cf.snapshot, source_commit=cf.source_commit)
    monkeypatch.setattr(bfc, "load_canonical_broker_flow", quarantined_only)
    with pytest.raises(wfb.BrokerFlowUnavailable, match="no trusted rows"):
        panel_of(synth.db, synth.mpath)


# --------------------------------------------------------------------------
# No raw fallback
# --------------------------------------------------------------------------

def test_build_panel_executes_no_broker_flow_sql_on_the_price_connection(synth):
    conn = wfb.connect_price_db(synth.db)
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        wfb.build_panel(conn, broker_flow_db_path=synth.db, broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()
    assert any("price_history" in s for s in seen), "the trace saw the price reads"
    assert not [s for s in seen if "broker_flow" in s.lower()]


def test_walk_forward_source_has_no_raw_broker_flow_read():
    source = open(WFB_SOURCE, encoding="utf-8").read()
    assert not re.search(r"(?i)\bfrom\s+broker_flow\b", source)
    tree = ast.parse(source)
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"read_sql", "read_sql_query", "read_sql_table"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("execute", "executemany"):
            text = ast.unparse(node)
            assert "broker_flow" not in text, text


def test_a_canonical_failure_is_never_answered_with_raw_rows(synth, monkeypatch):
    def refuse(*a, **k):
        raise bfc.ManifestMismatch("refused for the test", {"missing": [("2026-07-06", "LIVE")]})
    monkeypatch.setattr(bfc, "load_canonical_broker_flow", refuse)
    with pytest.raises(bfc.ManifestMismatch, match="refused for the test") as e:
        panel_of(synth.db, synth.mpath)
    assert e.value.diff == {"missing": [("2026-07-06", "LIVE")]}


def test_importing_the_module_does_not_load_the_canonical_reader():
    # experiment_1f_gate_b pins the repo-local modules its helpers load
    # (HELPER_FILES); build_panel imports the reader lazily to keep that closure.
    out = subprocess.run([sys.executable, "-c",
                          "import sys, walk_forward_backtest; "
                          "print(sorted(m for m in sys.modules if m.startswith('broker_flow')))"],
                         cwd=HERE, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# --------------------------------------------------------------------------
# Provenance, price intersection, determinism
# --------------------------------------------------------------------------

def test_provenance_is_exposed_and_is_not_a_feature(synth):
    panel = panel_of(synth.db, synth.mpath)
    prov = panel.attrs["broker_flow"]
    assert prov["db_sha256"] == sha(synth.db)
    assert prov["image_sha256"] == image_sha256(synth.db)
    assert prov["manifest_sha256"] == digest(synth.manifest)
    assert prov["manifest_path"] == os.path.abspath(synth.mpath)
    assert prov["manifest_snapshot"] == synth.manifest["input"].get("snapshot")
    assert prov["source_commit"] == synth.manifest["input"].get("source_commit")
    assert prov["quarantined_sessions"] == ["2026-07-02", "2026-07-03", "2026-07-14"]
    assert prov["accounting"] == synth.load().accounting
    assert "NOT PROVEN" in prov["point_in_time"]
    assert not set(panel.columns) & {"db_sha256", "manifest_sha256", "acquisition_date"}
    assert set(wfb.FEATURES) <= set(panel.columns)
    text = wfb.format_broker_flow_provenance(prov)
    assert prov["db_sha256"] in text and prov["manifest_sha256"] in text
    assert prov["image_sha256"] in text


def test_clean_price_intersection_is_unchanged(synth):
    con = sqlite3.connect(synth.db)
    con.execute("INSERT INTO price_quarantine VALUES ('2026-07-06', 'BBBB')")
    con.commit()
    con.close()
    panel = panel_of(synth.db, synth.mpath)
    conn = wfb.connect_price_db(synth.db)
    px = clean_panel(conn, horizons=(1,), lags=(1, 5), open_anchored=True)
    conn.close()
    frame = wfb.canonical_broker_flow_frame(synth.load())
    keys = set(zip(panel["ticker"], panel["date"]))
    clean = set(zip(px["ticker"], px["date"]))
    with_target = set(zip(px.loc[px["fwd_oo_1"].notna(), "ticker"], px.loc[px["fwd_oo_1"].notna(), "date"]))
    assert ("BBBB", "2026-07-06") not in keys
    assert keys == set(zip(frame["ticker"], frame["date"])) & clean & with_target
    # the next session must not bridge the quarantined price day
    nxt = panel[(panel["ticker"] == "BBBB") & (panel["date"] == "2026-07-07")]
    assert len(nxt) == 1 and np.isnan(nxt["momentum_1d"].iloc[0])
    assert np.isnan(nxt["broker_correlation_1d"].iloc[0])


def test_build_panel_is_deterministic(synth):
    a, b = panel_of(synth.db, synth.mpath), panel_of(synth.db, synth.mpath)
    pd.testing.assert_frame_equal(a, b)
    assert a.attrs == b.attrs


def test_cli_runs_end_to_end_and_prints_the_snapshot_hashes(tmp_path):
    sessions = weekdays("2026-01-05", "2026-06-30")
    db, man = backfill_db(str(tmp_path), bf_flows(sessions, tickers=("AAA", "BBB", "CCC")),
                          sessions, ["AAA", "BBB", "CCC"])
    out = subprocess.run([sys.executable, WFB_SOURCE, "--db", db, "--broker-flow-manifest", man],
                         cwd=HERE, capture_output=True, text=True, timeout=900)
    assert out.returncode == 0, out.stderr[-2000:]
    assert sha(db) in out.stdout
    assert digest(json.load(open(man, encoding="ascii"))) in out.stdout
    assert "point-in-time: NOT PROVEN" in out.stdout
    refused = subprocess.run([sys.executable, WFB_SOURCE, "--db", db], cwd=HERE,
                             capture_output=True, text=True, timeout=300)
    assert refused.returncode == 2 and "broker_flow_manifest_refresh.py" in refused.stderr


# --------------------------------------------------------------------------
# Real data: neobdm.db 2026-10-01 (PR #76 era), refreshed manifest
# --------------------------------------------------------------------------

REAL_COMMIT = "6f21a72f7c0c06d1684184a51f0587d2d9590d6f"     # master 2026-10-01, PR #76 era
REAL_BLOB = "c2f839adcb2f24c88a76c0bfccd933401f468488"        # its neobdm.db
REAL_DB_SHA256 = "df3d94af6e8f7fc91dd7802de2d0694d6b86b5ac0aa34c9571d7be5cd6a16abf"
# broker_flow_manifest_refresh of that database (source_commit REAL_COMMIT) is
# deterministic: this is the content hash of the manifest it writes.
REAL_MANIFEST_SHA256 = "0188cf97b8ee5a9aec638e8801e52fba7843429f3b5fc75a6412670a42d58660"


def legacy_raw_panel(conn):
    """The pre-migration build_panel(), verbatim, kept here ONLY as the
    reference the old-vs-new audit compares against."""
    px = clean_panel(conn, horizons=(1,), lags=(1, 5), open_anchored=True)
    raw = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    bf = raw.merge(px[["date", "ticker"]], on=["date", "ticker"], how="inner")
    agg = wfb._broker_day_aggregates(bf)
    corr = wfb._broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")
    pxf = wfb._price_features_and_target(px)
    panel = agg.merge(pxf, on=["ticker", "date"], how="inner")
    panel.loc[panel["momentum_1d"].isna(), "broker_correlation_1d"] = np.nan
    panel = panel.dropna(subset=["target"]).sort_values("date").reset_index(drop=True)
    return px, raw, bf, panel


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    got = subprocess.run(["git", "cat-file", "-t", REAL_BLOB], cwd=HERE, capture_output=True)
    if got.returncode != 0 or got.stdout.strip() != b"blob":
        pytest.skip(f"git object {REAL_BLOB[:8]} (neobdm.db at {REAL_COMMIT[:7]}) is not available")
    folder = tmp_path_factory.mktemp("wfb_real")
    db = str(folder / "neobdm.db")
    with open(db, "wb") as f:
        assert subprocess.run(["git", "cat-file", "blob", REAL_BLOB], cwd=HERE, stdout=f).returncode == 0
    assert sha(db) == REAL_DB_SHA256
    man = str(folder / "broker_flow_manifest.json")
    bmr.refresh(db, man, source_commit=REAL_COMMIT)
    conn = wfb.connect_price_db(db)
    try:
        new = wfb.build_panel(conn, broker_flow_db_path=db, broker_flow_manifest_path=man)
        px, raw, raw_used, old = legacy_raw_panel(conn)
    finally:
        conn.close()
    cf = bfc.load_canonical_broker_flow(db, man)
    ev = bfc.inspect_broker_flow_evidence(db, man)
    assert sha(db) == REAL_DB_SHA256
    return dict(db=db, man=man, new=new, old=old, px=px, raw=raw, raw_used=raw_used, cf=cf, ev=ev)


def test_real_provenance_pins_the_snapshot(real):
    prov = real["new"].attrs["broker_flow"]
    assert (prov["db_sha256"], prov["manifest_sha256"], prov["manifest_snapshot"],
            prov["source_commit"], prov["audited_manifest"]) == \
        (REAL_DB_SHA256, REAL_MANIFEST_SHA256, bmr.REFRESH_CONTRACT, REAL_COMMIT, False)
    # Measured, not assumed by build_panel: for this quiescent rollback-journal
    # file the serialized SQLite main image is byte-for-byte the file.
    assert prov["image_sha256"] == image_sha256(real["db"]) == REAL_DB_SHA256
    assert prov["accounting"] == {"canonical": 221_399, "duplicate": 3_642, "quarantined": 1_252,
                                  "excluded": 7_256, "raw": 233_549}
    assert (prov["canonical_sessions"], prov["quarantined_sessions"], prov["excluded_records"]) == \
        (251, ["2026-07-03"], 37)


def test_real_inputs_before_and_after(real):
    cf, ev = real["cf"], real["ev"]
    frame = wfb.canonical_broker_flow_frame(cf)
    used = frame.merge(real["px"][["date", "ticker"]], on=["date", "ticker"], how="inner")
    assert (len(real["raw"]), len(real["raw_used"])) == (233_549, 221_685)
    assert (len(frame), len(used)) == (221_399, 216_378)
    assert (real["raw"]["date"].nunique(), real["raw_used"]["date"].nunique()) == (306, 278)
    assert (frame["date"].nunique(), used["date"].nunique()) == (251, 251)
    by = Counter((e.status, e.date_class) for e in ev)
    assert by[(bfc.EXCLUDED, bfr.INFERRED_ONLY)] == 6_655
    assert by[(bfc.EXCLUDED, bfr.MIXED)] == 601
    assert sorted(x.acquisition_date for x in cf.excluded if x.date_class == bfr.MIXED) == \
        ["2026-08-12", "2026-08-27"]
    assert sum(1 for x in cf.excluded if x.date_class == bfr.INFERRED_ONLY) == 35
    assert cf.duplicates_collapsed == 3_642
    assert not frame.duplicated(["date", "ticker", "broker_code"]).any()


def test_real_2026_07_03_is_quarantined_and_absent_from_the_panel(real):
    cf, new, old = real["cf"], real["new"], real["old"]
    (q,) = cf.quarantined
    assert (q.canonical_session_date, q.rows, q.conflicting_keys) == ("2026-07-03", 1_252, 168)
    assert set(q.reasons) == {bfc.CAPTURE_CLASSES_DIFFER, bfc.VALUES_CONFLICT}
    with pytest.raises(bfc.SessionNotCovered) as e:
        cf.get("2026-07-03", "BBCA", "AK")
    assert e.value.status == bfc.QUARANTINED
    assert "2026-07-03" not in set(wfb.canonical_broker_flow_frame(cf)["date"])
    assert "2026-07-03" in set(old["date"]) and "2026-07-03" not in set(new["date"])
    # neither side substitutes for it, and the next session cannot reach across it
    assert new.loc[new["date"] == "2026-07-06", "broker_correlation_1d"].isna().all()


def test_real_same_day_july_acquisitions_stay_same_day(real):
    ev, old, new = real["ev"], real["old"], real["new"]
    same = sorted({e.acquisition_date for e in ev if e.regime == bfr.LIVE
                   and e.status in (bfc.CANONICAL, bfc.DUPLICATE)
                   and e.canonical_session_date == e.acquisition_date})
    assert same == ["2026-07-06", "2026-07-07", "2026-07-09", "2026-07-10", "2026-07-13"]
    key = ["ticker", "date"]
    m = old.merge(new, on=key, suffixes=("_old", "_new"))
    m = m[m["date"].isin(same)]
    assert len(m) == 129
    for f in BROKER_FEATURES:
        assert np.allclose(m[f + "_old"], m[f + "_new"], rtol=0, atol=1e-12, equal_nan=True), f


def test_real_2026_08_21_union_is_one_row_per_key_never_summed(real):
    cf, ev = real["cf"], real["ev"]
    frame = wfb.canonical_broker_flow_frame(cf)
    s = frame[frame["date"] == "2026-08-21"]
    assert len(s) == 211 and not s.duplicated(["ticker", "broker_code"]).any()
    rows = [r for r in cf.rows if r.canonical_session_date == "2026-08-21"]
    assert Counter(r.acquisition_date for r in rows) == {"2026-08-22": 202, "2026-08-23": 9}
    copies = Counter(e.acquisition_date for e in ev if e.canonical_session_date == "2026-08-21")
    assert copies == {"2026-08-22": 202, "2026-08-23": 211, "2026-08-24": 211}
    survivor = {(r.ticker, r.broker_code): r.netval for r in rows}
    assert dict(zip(zip(s["ticker"], s["broker_code"]), s["netval"])) == survivor


def test_real_old_vs_new_panel_changes_are_all_explained(real):
    old, new, cf, ev = real["old"], real["new"], real["cf"], real["ev"]
    assert (len(old), len(new)) == (10_756, 9_990)
    assert (old["date"].nunique(), new["date"].nunique()) == (276, 249)
    assert old["ticker"].nunique() == new["ticker"].nunique() == 45
    gone = sorted(set(old["date"]) - set(new["date"]))
    assert gone == ["2026-07-03", "2026-07-08", "2026-08-11", "2026-08-24"] + \
        [d for d in weekdays("2026-08-26", "2026-09-28")]
    assert sorted(set(new["date"]) - set(old["date"])) == ["2026-08-10"]

    key = ["ticker", "date"]
    m = old.merge(new, on=key, how="outer", suffixes=("_old", "_new"), indicator=True)
    assert dict(Counter(m["_merge"].astype(str))) == {"both": 9_899, "left_only": 857,
                                                      "right_only": 91}
    both = m[m["_merge"] == "both"]

    def changed(f):
        a, b = both[f + "_old"], both[f + "_new"]
        return ~(np.isclose(a, b, rtol=0, atol=1e-12) | (a.isna() & b.isna()))

    for f in ("momentum_1d", "volume_ratio", "target", "target_cc"):
        assert not changed(f).any(), f"{f} must not change: the price path is untouched"
    agg = np.zeros(len(both), bool)
    for f in BROKER_FEATURES:
        agg |= changed(f)
    assert agg.sum() == 642
    corr_only = changed("broker_correlation_1d") & ~agg
    assert dict(Counter(both.loc[corr_only, "date"])) == {"2026-07-06": 14, "2026-07-09": 11}

    # Every date whose rows changed, vanished or appeared is explained by the
    # canonical reader: session re-key, dedupe, exclusion, quarantine, or a
    # session with no trusted rows.
    quarantined = {q.canonical_session_date for q in cf.quarantined}
    excluded_raw = {e.acquisition_date for e in ev if e.status == bfc.EXCLUDED}
    sources = {}
    for e in ev:
        if e.status in (bfc.CANONICAL, bfc.DUPLICATE):
            sources.setdefault(e.canonical_session_date, set()).add(e.acquisition_date)

    def reasons(d):
        r = set()
        if d in quarantined:
            r.add("quarantine")
        if d in excluded_raw:
            r.add("exclusion")
        if sources.get(d, {d}) != {d}:
            r.add("re-key")
        if len(sources.get(d, ())) > 1:
            r.add("dedupe")
        if d not in cf.sessions and d not in quarantined:
            r.add("not covered")
        return r

    touched = set(both.loc[agg, "date"]) | (set(old["date"]) ^ set(new["date"]))
    unexplained = sorted(d for d in touched if not reasons(d))
    assert unexplained == []
    rekeyed = sorted(d for d in touched if "re-key" in reasons(d))
    assert (len(rekeyed), rekeyed[0], rekeyed[-1]) == (27, "2026-07-14", "2026-08-21")
    # corr-only changes: the previous session was withheld (07-03 quarantined,
    # 07-08 never captured), so the one-day feature is undefined there
    assert {d: reasons(prev) for d, prev in (("2026-07-06", "2026-07-03"),
                                              ("2026-07-09", "2026-07-08"))} == \
        {"2026-07-06": {"quarantine"}, "2026-07-09": {"not covered"}}


# --------------------------------------------------------------------------
# Orchestrators: check_ml_health CLI, short point-in-time warnings
# --------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--broker-flow-manifest", "m.json"], ["--broker-flow-manifest=m.json"]])
def test_health_cli_takes_the_manifest_in_both_forms(argv):
    assert chk.parse_args(argv).broker_flow_manifest == "m.json"
    args = chk.parse_args(argv + ["--quick", "--telegram"])
    assert (args.quick, args.telegram, args.broker_flow_manifest) == (True, True, "m.json")
    assert chk.parse_args([]).broker_flow_manifest is None


@pytest.mark.parametrize("argv", [["--bogus"], ["--broker", "m.json"], ["--broker-flow-manifest"],
                                  ["stray"]])
def test_health_cli_rejects_unknown_or_incomplete_options(argv, capsys):
    with pytest.raises(SystemExit) as e:
        chk.parse_args(argv)
    assert e.value.code == 2


@pytest.fixture
def health(synth, monkeypatch):
    """check_ml_health with its unrelated checks stubbed and DB_PATH on the
    synthetic database; refresh_broker_flow_manifest records its calls and
    stands in for the refresh tool with the fixture's own manifest."""
    for name in ("check_imports", "check_known_defects", "check_unit_tests", "check_model_runs",
                 "_load_dotenv"):
        monkeypatch.setattr(chk, name, lambda *a, **k: None)
    monkeypatch.setattr(chk, "DB_PATH", synth.db)
    refreshed = []

    def refresh(problems, stats, out=None):
        refreshed.append(out)
        stats["broker_flow_refreshed"] = True
        return synth.mpath
    monkeypatch.setattr(chk, "refresh_broker_flow_manifest", refresh)
    return refreshed


@pytest.mark.parametrize("form", ["space", "equals"])
def test_health_never_refreshes_over_an_explicit_bad_manifest(health, tmp_path, capsys, form):
    missing = str(tmp_path / "nonexistent.json")
    argv = ["--quick"] + (["--broker-flow-manifest", missing] if form == "space"
                          else [f"--broker-flow-manifest={missing}"])
    with pytest.raises(SystemExit) as e:
        chk.main(argv)
    assert e.value.code == 1
    assert health == [], "an explicit manifest must never be replaced by a refresh"
    assert "FileNotFoundError" in capsys.readouterr().out


def test_health_explicit_empty_manifest_is_not_omitted(health, capsys):
    with pytest.raises(SystemExit) as e:
        chk.main(["--quick", "--broker-flow-manifest="])
    assert e.value.code == 1 and health == []
    assert "BrokerFlowManifestRequired" in capsys.readouterr().out


def test_health_explicit_good_manifest_is_used_as_given(health, synth, capsys):
    with pytest.raises(SystemExit):
        chk.main(["--quick", f"--broker-flow-manifest={synth.mpath}"])
    out = capsys.readouterr().out
    assert health == [] and f"manifest {digest(synth.manifest)[:12]}" in out
    assert "refreshed this run" not in out


def test_health_omitted_manifest_refreshes_then_consumes(health, synth, capsys):
    with pytest.raises(SystemExit):
        chk.main(["--quick"])
    out = capsys.readouterr().out
    assert len(health) == 1
    assert f"manifest {digest(synth.manifest)[:12]}" in out and "refreshed this run" in out


def test_health_short_output_carries_the_point_in_time_warning(health, capsys):
    with pytest.raises(SystemExit):
        chk.main(["--quick"])
    out = capsys.readouterr().out
    assert wfb.PIT_WARNING in out and "point-in-time availability not proven" in out
    assert "leakage-free" not in out
    assert wfb.PIT_WARNING not in chk.format_report([], [], {})


@pytest.fixture
def reports(monkeypatch):
    """run_ml_reports imported fresh: it reads the Telegram secrets at import and
    imports ddqn_entry_exit (torch), neither needed for formatting."""
    import importlib
    import types
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", "test-placeholder"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", "test-placeholder"))
    try:
        import torch  # noqa: F401
    except ImportError:
        stub = types.ModuleType("ddqn_entry_exit")
        for name in ("build_episode_frame", "split_search_holdout", "fit_normalizer",
                     "normalize_features", "make_envs", "train_ddqn", "evaluate_policy",
                     "evaluate_policy_with_trade_log", "FEATURES", "STATE_EXTRA"):
            setattr(stub, name, None)
        monkeypatch.setitem(sys.modules, "ddqn_entry_exit", stub)
    monkeypatch.delitem(sys.modules, "run_ml_reports", raising=False)
    rmr = importlib.import_module("run_ml_reports")
    yield rmr
    sys.modules.pop("run_ml_reports", None)


def report_inputs(prov):
    trades = dict(n_trades=4, mean_ret=0.01, median_ret=0.005, hit_rate=0.5, base_rate=0.45,
                  hit_edge=0.05, ret_per_risk=0.3)
    pooled = dict(ic=0.01, daily_ic=0.02, daily_ic_median=0.01, top_hit=0.5, base_rate=0.45,
                  top_hit_edge=0.05, edge=0.001, n=100, n_trades=4, trade_mean=0.01,
                  trade_hit=0.5, trade_hit_edge=0.05)
    variants = pd.DataFrame([dict(label="v", mean_ret=0.01, median_ret=0.0, hit_rate=0.5,
                                  hit_edge=0.05, ret_per_risk=0.3, n_trades=4)])
    xgb = dict(n_dates=10, n_tickers=3, date_min="2026-01-01", date_max="2026-01-14",
               pooled=pooled, recent_trades=[], broker_flow=prov)
    strat = dict(winner_label="v", search_mean=0.01, holdout_mean=0.0, holdout_n=4,
                 search_results=variants, holdout_results=variants)
    ddqn = dict(n_dates=10, n_tickers=3, date_min="2026-01-01", date_max="2026-01-14",
                search=dict(trades=trades), holdout=dict(trades=trades), recent_holdout_trades=[])
    konglo = dict(signals=[], resolved=dict(n_trades=0), resolved_by_strategy={})
    return xgb, strat, ddqn, konglo


def test_telegram_report_carries_the_point_in_time_warning(reports, synth):
    prov = panel_of(synth.db, synth.mpath).attrs["broker_flow"]
    msg = reports.format_telegram_message(*report_inputs(prov))
    assert wfb.PIT_WARNING in msg and "leakage-free" not in msg


def test_github_summary_keeps_the_full_provenance_warning(reports, synth, tmp_path, monkeypatch):
    prov = panel_of(synth.db, synth.mpath).attrs["broker_flow"]
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    reports.write_step_summary(*report_inputs(prov))
    text = summary.read_text(encoding="utf-8")
    assert prov["db_sha256"] in text and prov["manifest_sha256"] in text
    assert "Session-aligned, not point-in-time proven." in text
