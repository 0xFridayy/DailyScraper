"""The DDQN episode frame on the canonical broker flow (HANDOFF Lampiran W).

ddqn_episode_data.build_episode_frame() (re-exported by ddqn_entry_exit) reads
broker flow only through walk_forward_backtest.load_canonical_inputs(), the
snapshot contract build_panel() uses, under an explicit manifest; and no
(ticker, episode_id) group it returns steps across a session the ticker has no
row for. Plus the run_ml_reports wiring: one manifest, refreshed at most once,
handed to DDQN as to XGBoost, and the DDQN provenance reported.

Torch-free, so it runs in ml-health CI; the few tests that import
ddqn_entry_exit itself skip without torch and say so. Fixtures are
test_walk_forward_canonical's (the PR #75 Synth database with prices, small
backfill-only databases with generator-built manifests). The real-data tests
read neobdm.db from git at the PR #78 base (master 2026-10-02) and refresh a
manifest for it; they skip only when that git object is absent."""

import ast
import importlib.util
import inspect
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter

import numpy as np
import pandas as pd
import pytest

import broker_flow_canonical as bfc
import broker_flow_manifest_refresh as bmr
import broker_flow_regime as bfr
import ddqn_episode_data as ded
import walk_forward_backtest as wfb
from ara_arb_simulation import annotate_limits
from price_audit import clean_panel
from test_broker_flow_canonical import ABSENT_0706, KEYS, SUBSET_0708, ZERO_KEY, digest, sha
from test_walk_forward_canonical import (BF_SESSIONS, BROKER_FEATURES, backfill_db, bf_flows,  # noqa: F401
                                         image_sha256, panel_of, report_inputs, reports, synth,
                                         wal_canonical, weekdays)

HERE = os.path.dirname(os.path.abspath(__file__))
DDQN_SOURCE = os.path.join(HERE, "ddqn_entry_exit.py")
DATA_SOURCE = os.path.join(HERE, "ddqn_episode_data.py")
NO_TORCH = importlib.util.find_spec("torch") is None
needs_torch = pytest.mark.skipif(NO_TORCH, reason="ddqn_entry_exit imports torch, which ml-health "
                                                  "does not install")
CORR = "broker_correlation_1d"
FRAME_COLUMNS = ["ticker", "date", *BROKER_FEATURES, CORR, "momentum_1d", "volume_ratio",
                 "daily_return", "at_ara", "at_arb", "episode_id"]

# Backfill fixture calendar: 2026-04-03 (Good Friday) is an exchange holiday,
# so it has neither prices nor broker flow and is not on the session axis.
HOLIDAY = "2026-04-03"
SESSIONS = [d for d in BF_SESSIONS if d != HOLIDAY]
GAP = "2026-04-08"                   # prices for everyone, no broker rows at all
TICKER_GAP = ("2026-04-15", "BBB")   # BBB priced but has no broker rows; AAA observed


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def frame_of(db, manifest, broker_db=None):
    conn = wfb.connect_price_db(db)
    try:
        return ded.build_episode_frame(conn, broker_flow_db_path=broker_db or db,
                                       broker_flow_manifest_path=manifest)
    finally:
        conn.close()


def legacy_raw_episode_frame(conn):
    """The pre-migration ddqn_entry_exit.build_episode_frame(), verbatim, kept
    here ONLY as the reference the old-vs-new comparisons run against."""
    px = clean_panel(conn, horizons=(), lags=(1, 5))
    bf = pd.read_sql("SELECT date, ticker, broker_code, netval FROM broker_flow", conn)
    bf = bf.merge(px[["date", "ticker"]], on=["date", "ticker"], how="inner")

    agg = wfb._broker_day_aggregates(bf)
    corr = wfb._broker_correlation_1d(bf)
    agg = agg.merge(corr, on=["ticker", "date"], how="left")

    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px["momentum_1d"] = px["lag_1"]
    px["vol_ma5"] = px.groupby("ticker")["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    px["vol_ma5"] = px["vol_ma5"].where(px["lag_5"].notna())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    px["daily_return"] = px["momentum_1d"]
    px = annotate_limits(px)

    all_dates = sorted(px["date"].unique())
    date_pos = {date: i for i, date in enumerate(all_dates)}
    px["_date_pos"] = px["date"].map(date_pos)
    date_gap = px.groupby("ticker")["_date_pos"].diff().ne(1)
    px["episode_id"] = (date_gap | px["lag_1"].isna()).groupby(px["ticker"]).cumsum()

    panel = agg.merge(
        px[["ticker", "date", "momentum_1d", "volume_ratio", "daily_return",
            "at_ara", "at_arb", "episode_id"]],
        on=["ticker", "date"], how="inner",
    )
    panel = panel.dropna(subset=["daily_return"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    return px, bf, panel


def legacy_of(db):
    conn = wfb.connect_price_db(db)
    try:
        return legacy_raw_episode_frame(conn)[2]
    finally:
        conn.close()


def sessions_of(db):
    """The clean price session axis: every date clean_panel returns for any ticker."""
    conn = wfb.connect_price_db(db)
    try:
        return sorted(clean_panel(conn, horizons=(), lags=(1, 5))["date"].unique())
    finally:
        conn.close()


def bridged_steps(panel, sessions):
    """(ticker, date) rows whose previous row in the same (ticker, episode_id)
    is not the previous session: a step TickerEnv would take across a hole."""
    pos = panel["date"].map({d: i for i, d in enumerate(sessions)})
    step = pos.groupby([panel["ticker"], panel["episode_id"]]).diff()
    hit = step.notna() & step.ne(1)
    return sorted(zip(panel.loc[hit, "ticker"], panel.loc[hit, "date"]))


def episode_of(panel, ticker, d):
    return panel.loc[(panel["ticker"] == ticker) & (panel["date"] == d), "episode_id"].item()


def keys_of(panel):
    return set(zip(panel["ticker"], panel["date"]))


@pytest.fixture
def holes(tmp_path):
    folder = tmp_path / "holes"          # its own folder: synth owns tmp_path/neobdm.db
    folder.mkdir()
    flows = bf_flows(SESSIONS, skip={GAP, TICKER_GAP})
    return backfill_db(str(folder), flows, SESSIONS, ["AAA", "BBB"])


@pytest.fixture
def q0703(synth):
    """The Synth database without its 2026-07-04 live acquisition: 2026-07-02 is
    then a backfill-only (canonical) session while 2026-07-03 stays quarantined,
    so the sessions either side of the quarantine are 07-02 and 07-06."""
    db = synth.db_copy("q0703.db", "DELETE FROM broker_flow WHERE date = '2026-07-04'")
    path = synth.write(bfr.build_manifest(db, synth.pq), "q0703.json")
    synth.pin(path)
    return db, path


# --------------------------------------------------------------------------
# The canonical input contract
# --------------------------------------------------------------------------

def test_the_frame_is_keyed_by_canonical_session_only(synth):
    cf = synth.load()
    panel = frame_of(synth.db, synth.mpath)
    assert list(panel.columns) == FRAME_COLUMNS
    assert sorted(panel["date"].unique()) == ["2026-07-01", "2026-07-06", "2026-07-07",
                                              "2026-09-29"] == sorted(cf.sessions)
    assert keys_of(panel) == {(r.ticker, r.canonical_session_date) for r in cf.rows}
    assert not panel.duplicated(["ticker", "date"]).any()
    # The raw table would have contributed acquisition dates (07-08/07-09 for
    # 07-07, 09-30 for 09-29), quarantined sessions and unproven evidence.
    old = legacy_of(synth.db)
    raw_only = {"2026-07-02", "2026-07-03", "2026-07-08", "2026-07-09", "2026-07-15",
                "2026-07-16", "2026-08-12", "2026-09-01", "2026-09-30"}
    assert raw_only <= set(old["date"]) and not raw_only & set(panel["date"])
    assert "2026-07-07" not in set(old["date"]) and "2026-07-07" in set(panel["date"])


@pytest.mark.parametrize("acq, cls", [("2026-09-01", bfr.INFERRED_ONLY), ("2026-08-12", bfr.MIXED)])
def test_unproven_evidence_never_reaches_the_frame(synth, acq, cls):
    cf = synth.load()
    assert {e.acquisition_date: e.date_class for e in cf.excluded}[acq] == cls
    rec = {(r["broker_flow_date"], r["regime"]): r for r in synth.manifest["records"]}[(acq, bfr.LIVE)]
    unproven = {acq, rec["inferred_session_date"], "2026-08-11"} - {None}
    assert not set(frame_of(synth.db, synth.mpath)["date"]) & unproven


@pytest.mark.parametrize("session", ["2026-07-02", "2026-07-03", "2026-07-14"])
def test_quarantined_sessions_contribute_nothing(synth, session):
    with pytest.raises(bfc.SessionNotCovered) as e:
        synth.load().get(session, "AAAA", "AK")
    assert e.value.status == bfc.QUARANTINED
    assert session not in set(frame_of(synth.db, synth.mpath)["date"])


def test_identical_acquisition_copies_are_not_summed(synth):
    cf = synth.load()
    panel = frame_of(synth.db, synth.mpath).set_index(["ticker", "date"])
    con = bfr.connect_readonly(synth.db)
    raw = {(t, b): v for t, b, v in con.execute(
        "SELECT ticker, broker_code, netval FROM broker_flow WHERE date = '2026-07-09'")}
    con.close()
    for t in sorted({t for t, _ in KEYS}):
        row = panel.loc[(t, "2026-07-07")]
        assert row["n_brokers"] == 2, t       # one row per broker, not one per copy
        assert row["net_flow_total"] == pytest.approx(raw[(t, "AK")] + raw[(t, "ZP")])
    for t, b in SUBSET_0708:                  # keys only the 07-09 copy holds are kept, once
        assert cf.get("2026-07-07", t, b).copy_acquisition_dates == ("2026-07-09",)


def test_absence_is_not_zero_and_observed_zero_is_not_absence(synth):
    panel = frame_of(synth.db, synth.mpath).set_index(["ticker", "date"])
    t, _ = ABSENT_0706
    assert panel.loc[(t, "2026-07-06"), "n_brokers"] == 1
    assert panel.loc[(t, "2026-07-07"), "n_brokers"] == 2
    z, _ = ZERO_KEY
    zero = panel.loc[(z, "2026-09-29")]
    assert zero["n_brokers"] == 1 and zero["net_flow_total"] == 0.0
    assert np.isnan(zero["broker_concentration"])     # 0 / 0 stays undefined, not filled


def test_null_netval_stays_nan_and_is_still_an_observed_broker(tmp_path):
    flows = bf_flows(SESSIONS)
    flows[("2026-04-01", "AAA", "CC")] = None
    db, man = backfill_db(str(tmp_path), flows, SESSIONS, ["AAA", "BBB"])
    row = frame_of(db, man).set_index(["ticker", "date"]).loc[("AAA", "2026-04-01")]
    observed = [v for (d, t, _), v in flows.items() if (d, t) == ("2026-04-01", "AAA") and v is not None]
    assert row["n_brokers"] == 5 and len(observed) == 4
    assert row["net_flow_total"] == pytest.approx(sum(observed))


def test_the_broker_aggregates_are_the_ones_build_panel_reads(synth, holes):
    for db, man in ((synth.db, synth.mpath), holes):
        m = frame_of(db, man).merge(panel_of(db, man), on=["ticker", "date"], suffixes=("", "_x"))
        assert len(m) > 0
        for f in BROKER_FEATURES:
            assert np.array_equal(m[f], m[f + "_x"], equal_nan=True), f


def test_without_broker_holes_the_frame_is_the_legacy_frame(tmp_path):
    # Backfill only, every session covered: the canonical view IS the raw table,
    # so every column - prices, ARA/ARB flags, episode ids, correlation - must be
    # exactly what the old code produced, including around a price quarantine.
    db, man = backfill_db(str(tmp_path), bf_flows(SESSIONS), SESSIONS, ["AAA", "BBB"],
                          quarantine=[("2026-04-22", "BBB")])
    new, old = frame_of(db, man), legacy_of(db)
    pd.testing.assert_frame_equal(new, old, check_exact=False, rtol=1e-12, atol=0)
    assert new.groupby("ticker")["episode_id"].nunique().to_dict() == {"AAA": 1, "BBB": 2}


def test_the_frame_is_deterministic(synth):
    a, b = frame_of(synth.db, synth.mpath), frame_of(synth.db, synth.mpath)
    pd.testing.assert_frame_equal(a, b)
    assert a.attrs == b.attrs


# --------------------------------------------------------------------------
# Episode continuity
# --------------------------------------------------------------------------

def test_weekends_and_exchange_holidays_are_not_holes(tmp_path):
    db, man = backfill_db(str(tmp_path), bf_flows(SESSIONS), SESSIONS, ["AAA", "BBB"])
    panel, sessions = frame_of(db, man), sessions_of(db)
    assert HOLIDAY not in sessions
    assert panel.groupby("ticker")["episode_id"].nunique().to_dict() == {"AAA": 1, "BBB": 1}
    for t in ("AAA", "BBB"):
        assert episode_of(panel, t, "2026-04-02") == episode_of(panel, t, "2026-04-06")  # Thu -> Mon
        assert episode_of(panel, t, "2026-03-13") == episode_of(panel, t, "2026-03-16")  # Fri -> Mon
    assert bridged_steps(panel, sessions) == []
    assert panel[CORR].notna().all()          # nor a hole for the one-session correlation


def test_a_withheld_session_splits_every_tickers_episode(holes):
    db, man = holes
    panel, sessions = frame_of(db, man), sessions_of(db)
    assert GAP in sessions and GAP not in set(panel["date"])
    for t in ("AAA", "BBB"):
        assert episode_of(panel, t, "2026-04-07") != episode_of(panel, t, "2026-04-09")
    assert bridged_steps(panel, sessions) == []
    # the pre-migration frame segmented before the broker join and stepped across it
    old = legacy_of(db)
    assert episode_of(old, "AAA", "2026-04-07") == episode_of(old, "AAA", "2026-04-09")
    assert {("AAA", "2026-04-09"), ("BBB", "2026-04-09")} <= set(bridged_steps(old, sessions))


def test_a_tickers_own_hole_splits_only_that_ticker(holes):
    db, man = holes
    d, t = TICKER_GAP
    panel, sessions = frame_of(db, man), sessions_of(db)
    assert (t, d) not in keys_of(panel) and ("AAA", d) in keys_of(panel)   # no row synthesized
    assert episode_of(panel, "BBB", "2026-04-14") != episode_of(panel, "BBB", "2026-04-16")
    assert episode_of(panel, "AAA", "2026-04-14") == episode_of(panel, "AAA", "2026-04-16")
    assert panel.groupby("ticker")["episode_id"].nunique().to_dict() == {"AAA": 2, "BBB": 3}
    assert bridged_steps(panel, sessions) == []
    old = legacy_of(db)
    assert episode_of(old, "BBB", "2026-04-14") == episode_of(old, "BBB", "2026-04-16")


def test_the_quarantined_0703_session_is_never_stepped_across(q0703):
    db, man = q0703
    cf = bfc.load_canonical_broker_flow(db, man)
    assert "2026-07-02" in cf.sessions and "2026-07-06" in cf.sessions
    assert [q.canonical_session_date for q in cf.quarantined] == ["2026-07-03", "2026-07-14"]
    panel, sessions = frame_of(db, man), sessions_of(db)
    assert "2026-07-03" in sessions and "2026-07-03" not in set(panel["date"])
    for t in ("AAAA", "BBBB"):
        assert episode_of(panel, t, "2026-07-01") == episode_of(panel, t, "2026-07-02")
        assert episode_of(panel, t, "2026-07-02") != episode_of(panel, t, "2026-07-06")
        assert episode_of(panel, t, "2026-07-06") == episode_of(panel, t, "2026-07-07")
    assert bridged_steps(panel, sessions) == []


def test_episode_ids_are_assigned_on_the_final_frame(holes):
    db, man = holes
    panel = frame_of(db, man)
    want = ded.session_episode_ids(panel, sessions_of(db))
    assert panel["episode_id"].tolist() == want.tolist()
    assert panel.groupby("ticker")["episode_id"].apply(lambda s: s.is_monotonic_increasing).all()


# --------------------------------------------------------------------------
# The one-session correlation
# --------------------------------------------------------------------------

def test_correlation_never_compares_across_a_hole(holes):
    db, man = holes
    panel = frame_of(db, man).set_index(["ticker", "date"])
    xgb = panel_of(db, man).set_index(["ticker", "date"])
    bf = wfb.canonical_broker_flow_frame(bfc.load_canonical_broker_flow(db, man))
    helper = wfb._broker_correlation_1d(bf).set_index(["ticker", "date"])
    after_gap, after_hole = "2026-04-09", "2026-04-16"
    for t in ("AAA", "BBB"):          # after the withheld session: as build_panel does
        assert np.isnan(panel.loc[(t, after_gap), CORR]) and np.isnan(xgb.loc[(t, after_gap), CORR])
        assert not np.isnan(helper.loc[(t, after_gap), CORR])
    # after BBB's own hole: NaN for BBB only. build_panel keeps the helper's
    # value there (PR #77); a sequential episode must not.
    assert np.isnan(panel.loc[("BBB", after_hole), CORR])
    assert xgb.loc[("BBB", after_hole), CORR] == pytest.approx(helper.loc[("BBB", after_hole), CORR])
    assert panel.loc[("AAA", after_hole), CORR] == pytest.approx(helper.loc[("AAA", after_hole), CORR])
    # everywhere else it is the helper's one-session value, unchanged
    rest = panel.drop(index=[("AAA", after_gap), ("BBB", after_gap), ("BBB", after_hole)])
    assert rest[CORR].notna().all()
    np.testing.assert_allclose(rest[CORR], helper.loc[rest.index, CORR], rtol=0, atol=0)


# --------------------------------------------------------------------------
# Explicit manifest, the shared snapshot contract, failure behaviour
# --------------------------------------------------------------------------

def test_the_manifest_is_required(synth):
    conn = wfb.connect_price_db(synth.db)
    try:
        with pytest.raises(TypeError):
            ded.build_episode_frame(conn)
        with pytest.raises(TypeError):
            ded.build_episode_frame(conn, broker_flow_db_path=synth.db)
        for missing in (None, "", "  "):
            with pytest.raises(wfb.BrokerFlowManifestRequired, match="broker_flow_manifest_refresh.py"):
                ded.build_episode_frame(conn, broker_flow_db_path=synth.db,
                                        broker_flow_manifest_path=missing)
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="broker_flow_db_path"):
            ded.build_episode_frame(conn, broker_flow_db_path=None,
                                    broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()


def test_the_frame_never_makes_or_refreshes_a_manifest(synth, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the DDQN frame must never build or refresh a manifest")
    for mod, name in ((bmr, "refresh"), (bmr, "build_refreshed_manifest"),
                      (bfr, "build_manifest"), (bfr, "write_manifest")):
        monkeypatch.setattr(mod, name, forbidden)
    before = sorted(os.listdir(synth.dir))
    assert len(frame_of(synth.db, synth.mpath)) > 0
    with pytest.raises(wfb.BrokerFlowManifestRequired):
        frame_of(synth.db, None)
    assert sorted(os.listdir(synth.dir)) == before


def test_failures_propagate_unchanged_and_never_fall_back(synth, tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        frame_of(synth.db, str(tmp_path / "nowhere.json"))
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="ascii")
    with pytest.raises(bfc.ManifestInvalid):
        frame_of(synth.db, str(garbage))
    changed = synth.db_copy("changed.db", "UPDATE broker_flow SET netval = netval + 1 "
                                          "WHERE date = '2026-07-06' AND ticker = 'AAAA'")
    with pytest.raises(bfc.ManifestMismatch):
        frame_of(changed, synth.mpath)
    open(synth.db + "-journal", "wb").close()
    try:
        with pytest.raises(bfc.SourceStateError, match="-journal"):
            frame_of(synth.db, synth.mpath)
    finally:
        os.remove(synth.db + "-journal")

    def refuse(*a, **k):
        raise bfc.ManifestMismatch("refused for the test", {"missing": [("2026-07-06", "LIVE")]})
    monkeypatch.setattr(bfc, "load_canonical_broker_flow", refuse)
    with pytest.raises(bfc.ManifestMismatch, match="refused for the test"):
        frame_of(synth.db, synth.mpath)


def test_prices_and_broker_flow_must_be_one_snapshot(synth, tmp_path):
    other = synth.db_copy("other_prices.db", "UPDATE price_history SET close = close * 1.01 "
                                             "WHERE date = '2026-07-06'")
    bfc.load_canonical_broker_flow(other, synth.mpath)            # the manifest is valid for it
    with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
        frame_of(synth.db, synth.mpath, broker_db=other)
    with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
        frame_of(other, synth.mpath, broker_db=synth.db)
    twin = str(tmp_path / "twin.db")
    shutil.copy(synth.db, twin)                                   # byte identity, not path identity
    pd.testing.assert_frame_equal(frame_of(twin, synth.mpath, broker_db=synth.db),
                                  frame_of(synth.db, synth.mpath))


def test_unverifiable_price_connections_are_refused(synth):
    mem = sqlite3.connect(":memory:")
    try:
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not file-backed"):
            ded.build_episode_frame(mem, broker_flow_db_path=synth.db,
                                    broker_flow_manifest_path=synth.mpath)
    finally:
        mem.close()
    conn = sqlite3.connect(synth.db)
    try:
        conn.execute("UPDATE price_history SET close = close * 1.01 WHERE date = '2026-07-06'")
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="open transaction"):
            ded.build_episode_frame(conn, broker_flow_db_path=synth.db,
                                    broker_flow_manifest_path=synth.mpath)
    finally:
        conn.rollback()
        conn.close()
    conn = wfb.connect_price_db(synth.db)
    try:
        conn.execute("CREATE TEMP VIEW Price_History AS SELECT * FROM main.price_history")
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="shadow"):
            ded.build_episode_frame(conn, broker_flow_db_path=synth.db,
                                    broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()


def test_a_wal_price_change_behind_identical_file_bytes_is_refused(synth, tmp_path):
    canonical = wal_canonical(synth)
    price = str(tmp_path / "price_wal.db")
    shutil.copy(canonical, price)
    baseline = frame_of(price, synth.mpath, broker_db=canonical)
    writer = sqlite3.connect(price)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE price_history SET close = close * 1.05 WHERE date = '2026-07-06'")
        writer.commit()
        assert sha(price) == sha(canonical), "the change lives only in the WAL"
        with pytest.raises(wfb.BrokerFlowSnapshotMismatch, match="not one database snapshot"):
            frame_of(price, synth.mpath, broker_db=canonical)
    finally:
        writer.close()
    assert len(baseline) > 0


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
            ded.build_episode_frame(conn, broker_flow_db_path=synth.db,
                                    broker_flow_manifest_path=synth.mpath)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_prices_keep_the_ddqn_clean_panel_parameters(synth, monkeypatch):
    calls, real_clean = [], wfb.clean_panel

    def spy(conn, *a, **k):
        calls.append((a, k))
        return real_clean(conn, *a, **k)
    monkeypatch.setattr(wfb, "clean_panel", spy)
    frame_of(synth.db, synth.mpath)
    assert calls == [((), {"horizons": (), "lags": (1, 5)})]


def test_both_consumers_read_through_the_one_shared_input_contract(synth, monkeypatch):
    seen, real_load = [], wfb.load_canonical_inputs

    def spy(conn, **k):
        seen.append(k)
        return real_load(conn, **k)
    monkeypatch.setattr(wfb, "load_canonical_inputs", spy)
    frame_of(synth.db, synth.mpath)
    panel_of(synth.db, synth.mpath)
    paths = dict(broker_flow_db_path=synth.db, broker_flow_manifest_path=synth.mpath)
    assert seen == [dict(paths, horizons=(), lags=(1, 5)),
                    dict(paths, horizons=(1,), lags=(1, 5), open_anchored=True)]


def test_no_broker_flow_sql_runs_on_the_price_connection(synth):
    conn = wfb.connect_price_db(synth.db)
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        ded.build_episode_frame(conn, broker_flow_db_path=synth.db,
                                broker_flow_manifest_path=synth.mpath)
    finally:
        conn.close()
    assert any("price_history" in s for s in seen), "the trace saw the price reads"
    assert not [s for s in seen if "broker_flow" in s.lower()]


def test_the_episode_frame_needs_no_torch_and_loads_no_refresh_tool(tmp_path):
    db, man = backfill_db(str(tmp_path), bf_flows(SESSIONS[:30]), SESSIONS[:30], ["AAA", "BBB"])
    code = ("import sys, ddqn_episode_data as d, walk_forward_backtest as w\n"
            "c = w.connect_price_db(sys.argv[1])\n"
            "p = d.build_episode_frame(c, broker_flow_db_path=sys.argv[1], "
            "broker_flow_manifest_path=sys.argv[2])\n"
            "print(len(p), 'torch' in sys.modules, 'broker_flow_manifest_refresh' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code, db, man], cwd=HERE, capture_output=True,
                         text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    n, torch_loaded, refresh_loaded = out.stdout.split()
    assert int(n) > 0 and (torch_loaded, refresh_loaded) == ("False", "False")


# --------------------------------------------------------------------------
# Provenance and the point-in-time caveat
# --------------------------------------------------------------------------

def test_provenance_is_build_panels_contract_and_not_a_feature(synth):
    panel = frame_of(synth.db, synth.mpath)
    prov = panel.attrs["broker_flow"]
    assert prov == panel_of(synth.db, synth.mpath).attrs["broker_flow"]
    assert prov["db_sha256"] == sha(synth.db) and prov["image_sha256"] == image_sha256(synth.db)
    assert prov["manifest_sha256"] == digest(synth.manifest)
    assert prov["quarantined_sessions"] == ["2026-07-02", "2026-07-03", "2026-07-14"]
    assert "NOT PROVEN" in prov["point_in_time"]
    assert not set(panel.columns) & {"db_sha256", "manifest_sha256", "acquisition_date"}
    assert set(wfb.FEATURES) <= set(panel.columns)


def test_no_ddqn_source_claims_more_than_session_alignment():
    for path in (DDQN_SOURCE, DATA_SOURCE):
        text = open(path, encoding="utf-8").read()
        assert "POINT-IN-TIME: NOT PROVEN" in text, path
        assert not re.search(r"(?i)\b(is|are)\s+leakage-free\b", text), path


# --------------------------------------------------------------------------
# CLI and the ddqn_entry_exit wiring
# --------------------------------------------------------------------------

def test_the_cli_refuses_without_a_manifest(capsys):
    with pytest.raises(SystemExit) as e:
        ded.parse_cli([])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--broker-flow-manifest is required" in err and "broker_flow_manifest_refresh.py" in err
    args = ded.parse_cli(["--broker-flow-manifest", "m.json", "--db", "x.db"])
    assert (args.broker_flow_manifest, args.db) == ("m.json", "x.db")


def _main_block(tree):
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            return node
    raise AssertionError("no __main__ block")


def test_ddqn_sources_read_no_raw_broker_flow_and_the_cli_passes_the_manifest_through():
    # Static, and only a supplement: ddqn_entry_exit needs torch, so in CI its
    # wiring cannot run; the behaviour itself is tested above through
    # ddqn_episode_data and, where torch exists, below.
    for path in (DDQN_SOURCE, DATA_SOURCE):
        source = open(path, encoding="utf-8").read()
        assert not re.search(r"(?i)\bfrom\s+broker_flow\b", source), path
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                assert name not in ("read_sql", "read_sql_query", "read_sql_table"), path
                if name in ("execute", "executemany"):
                    assert "broker_flow" not in ast.unparse(node), path
    tree = ast.parse(open(DDQN_SOURCE, encoding="utf-8").read())
    assert not [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_episode_frame"]
    assert any(isinstance(n, ast.ImportFrom) and n.module == "ddqn_episode_data"
               and {"build_episode_frame", "parse_cli"} <= {a.name for a in n.names} for n in tree.body)
    calls = [n for n in ast.walk(_main_block(tree)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "build_episode_frame"]
    assert len(calls) == 1
    assert {k.arg: ast.unparse(k.value) for k in calls[0].keywords} == {
        "broker_flow_db_path": "args.db", "broker_flow_manifest_path": "args.broker_flow_manifest"}


@needs_torch
def test_ddqn_entry_exit_reexports_the_frame_and_its_envs_never_bridge(holes):
    import ddqn_entry_exit as dee
    assert dee.build_episode_frame is ded.build_episode_frame
    db, man = holes
    panel = frame_of(db, man)
    pos = {d: i for i, d in enumerate(sessions_of(db))}
    envs = dee.make_envs(panel)
    for env in envs:
        assert np.all(np.diff([pos[d] for d in env.dates]) == 1), (env.ticker, env.dates)
    # BBB's 04-09..04-14 episode (4 sessions) is under make_envs' 10-row minimum
    assert {(env.ticker, env.dates[0], env.dates[-1]) for env in envs} == {
        ("AAA", "2026-03-03", "2026-04-07"), ("AAA", "2026-04-09", "2026-05-29"),
        ("BBB", "2026-03-03", "2026-04-07"), ("BBB", "2026-04-16", "2026-05-29")}


@needs_torch
def test_ddqn_cli_refuses_without_a_manifest(tmp_path):
    out = subprocess.run([sys.executable, DDQN_SOURCE, "--db", str(tmp_path / "x.db")], cwd=HERE,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 2
    assert "--broker-flow-manifest is required" in out.stderr
    assert "broker_flow_manifest_refresh.py" in out.stderr


# --------------------------------------------------------------------------
# run_ml_reports: one manifest for every model, DDQN provenance reported
# --------------------------------------------------------------------------

def _stub_training(rmr, monkeypatch, seen):
    """Everything after the frame in run_ddqn_report: torch's part, irrelevant
    to which broker flow the frame was built from."""
    trades = dict(n_trades=0, mean_ret=0.0, median_ret=0.0, hit_rate=0.0, ret_per_risk=np.nan)

    def split(panel):
        seen["panel"] = panel
        return panel.copy(), panel.copy()
    monkeypatch.setattr(rmr, "FEATURES", wfb.FEATURES)
    monkeypatch.setattr(rmr, "STATE_EXTRA", ["position", "days_in_position", "unrealized_return"])
    monkeypatch.setattr(rmr, "split_search_holdout", split)
    monkeypatch.setattr(rmr, "fit_normalizer", lambda df: (None, None))
    monkeypatch.setattr(rmr, "normalize_features", lambda df, mean, std: df[wfb.FEATURES])
    monkeypatch.setattr(rmr, "make_envs", lambda df: [])
    monkeypatch.setattr(rmr, "train_ddqn", lambda envs, state_dim, n_epochs: "net")
    monkeypatch.setattr(rmr, "evaluate_policy_with_trade_log", lambda net, envs: pd.DataFrame())
    monkeypatch.setattr(rmr, "evaluate_policy", lambda net, envs: dict(daily=trades, trades=trades))


def test_run_ddqn_report_builds_the_canonical_frame_under_the_given_manifest(reports, synth,
                                                                             monkeypatch):
    rmr = reports
    params = inspect.signature(rmr.run_ddqn_report).parameters
    assert list(params) == ["conn", "broker_flow_manifest_path", "db_path"]
    assert params["broker_flow_manifest_path"].default is inspect.Parameter.empty
    refreshed = []
    monkeypatch.setattr(bmr, "refresh", lambda *a, **k: refreshed.append(a))
    monkeypatch.setattr(rmr, "refresh_broker_flow_manifest", lambda *a, **k: refreshed.append(a))
    seen = {}
    _stub_training(rmr, monkeypatch, seen)
    conn = wfb.connect_price_db(synth.db)
    try:
        with pytest.raises(TypeError):
            rmr.run_ddqn_report(conn)
        result = rmr.run_ddqn_report(conn, synth.mpath, db_path=synth.db)
    finally:
        conn.close()
    assert refreshed == []
    pd.testing.assert_frame_equal(seen["panel"], frame_of(synth.db, synth.mpath))
    assert result["broker_flow"] == panel_of(synth.db, synth.mpath).attrs["broker_flow"]
    assert (result["n_dates"], result["date_min"], result["date_max"]) == (4, "2026-07-01", "2026-09-29")


@pytest.mark.parametrize("argv, given", [([], None),
                                         (["--broker-flow-manifest", "given.json"], "given.json"),
                                         (["--broker-flow-manifest=given.json"], "given.json"),
                                         (["--broker-flow-manifest="], "")])
def test_the_report_refreshes_at_most_once_and_every_model_gets_that_manifest(
        reports, synth, monkeypatch, argv, given):
    rmr = reports
    prov = panel_of(synth.db, synth.mpath).attrs["broker_flow"]
    xgb, strat, ddqn, konglo = report_inputs(prov)
    calls = []
    monkeypatch.setattr(rmr, "DB_PATH", synth.db)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def refresh(db_path):
        calls.append(("refresh", db_path))
        return "refreshed.json"

    def model(name, result):
        def run(conn, broker_flow_manifest_path, db_path=None):
            calls.append((name, broker_flow_manifest_path))
            return result
        return run
    monkeypatch.setattr(rmr, "refresh_broker_flow_manifest", refresh)
    monkeypatch.setattr(rmr, "run_xgboost_report", model("xgboost", xgb))
    monkeypatch.setattr(rmr, "run_strategy_variants_report", model("strategy", strat))
    monkeypatch.setattr(rmr, "run_ddqn_report", model("ddqn", dict(ddqn, broker_flow=dict(prov))))
    monkeypatch.setattr(rmr, "run_konglo_watch_report", lambda conn: konglo)
    monkeypatch.setattr(rmr, "send_telegram", lambda message: calls.append(("telegram", message)))
    rmr.main(argv)
    manifest = "refreshed.json" if given is None else given
    assert [c for c in calls if c[0] == "refresh"] == ([("refresh", synth.db)] if given is None else [])
    assert [c for c in calls if c[0] in ("xgboost", "strategy", "ddqn")] == \
        [("xgboost", manifest), ("strategy", manifest), ("ddqn", manifest)]
    (message,) = [c[1] for c in calls if c[0] == "telegram"]
    assert "DDQN broker flow: same canonical snapshot as XGBoost" in message


def test_the_report_states_which_snapshot_ddqn_read(reports, synth, tmp_path, monkeypatch):
    prov = frame_of(synth.db, synth.mpath).attrs["broker_flow"]
    xgb, strat, ddqn, konglo = report_inputs(panel_of(synth.db, synth.mpath).attrs["broker_flow"])
    same = dict(ddqn, broker_flow=prov)
    msg = reports.format_telegram_message(xgb, strat, same, konglo)
    assert "DDQN broker flow: same canonical snapshot as XGBoost" in msg
    assert wfb.PIT_WARNING in msg and "leakage-free" not in msg
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    reports.write_step_summary(xgb, strat, same, konglo)
    ddqn_part = summary.read_text(encoding="utf-8").split("## DDQN entry/exit", 1)[1]
    assert "same canonical snapshot as XGBoost" in ddqn_part
    for k in ("db_sha256", "image_sha256", "manifest_sha256"):
        assert prov[k] in ddqn_part
    assert "Session-aligned, not point-in-time proven." in ddqn_part
    for k in ("db_sha256", "image_sha256", "manifest_sha256"):
        other = dict(ddqn, broker_flow=dict(prov, **{k: "0" * 64}))
        assert "NOT the XGBoost snapshot" in reports.format_telegram_message(xgb, strat, other, konglo)
    assert "provenance missing" in reports.format_telegram_message(xgb, strat, ddqn, konglo)


# --------------------------------------------------------------------------
# Real data: neobdm.db at the PR #78 base (master 2026-10-02), refreshed manifest
# --------------------------------------------------------------------------

REAL_COMMIT = "a4c75116d400e5abba671a9e78bb017eb7d26b8f"     # master 2026-10-02, the PR #78 base
REAL_BLOB = "32565412750629c5bd6da419a5ef9d6fcf2b9b93"        # its neobdm.db
REAL_DB_SHA256 = "76eb841163d4fa6f1d519929c5a03f97c0d8d2235b370e2a5f7be9e1ee988df4"
# broker_flow_manifest_refresh of that database with source_commit REAL_COMMIT
# is deterministic: the content hash of the manifest it writes.
REAL_MANIFEST_SHA256 = "5a9f104d91fa5a054177b1c8e7b0e70b1b42478f126bb0539c6e14e1f62d1a3c"
PRICE_FIELDS = ["momentum_1d", "volume_ratio", "daily_return", "at_ara", "at_arb"]


def ddqn_split(panel, search_frac=0.7):
    """ddqn_entry_exit.split_search_holdout's date cut (that module needs torch)."""
    dates = sorted(panel["date"].unique())
    cut = int(len(dates) * search_frac)
    return [(min(ds), max(ds), len(ds)) for ds in (dates[:cut], dates[cut:])]


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    got = subprocess.run(["git", "cat-file", "-t", REAL_BLOB], cwd=HERE, capture_output=True)
    if got.returncode != 0 or got.stdout.strip() != b"blob":
        pytest.skip(f"git object {REAL_BLOB[:8]} (neobdm.db at {REAL_COMMIT[:7]}) is not available")
    folder = tmp_path_factory.mktemp("ddqn_real")
    db = str(folder / "neobdm.db")
    with open(db, "wb") as f:
        assert subprocess.run(["git", "cat-file", "blob", REAL_BLOB], cwd=HERE, stdout=f).returncode == 0
    assert sha(db) == REAL_DB_SHA256
    man = str(folder / "broker_flow_manifest.json")
    bmr.refresh(db, man, source_commit=REAL_COMMIT)
    conn = wfb.connect_price_db(db)
    try:
        new = ded.build_episode_frame(conn, broker_flow_db_path=db, broker_flow_manifest_path=man)
        xgb = wfb.build_panel(conn, broker_flow_db_path=db, broker_flow_manifest_path=man)
        px, raw_used, old = legacy_raw_episode_frame(conn)
        raw = conn.execute("SELECT count(*), count(DISTINCT date) FROM broker_flow").fetchone()
    finally:
        conn.close()
    cf = bfc.load_canonical_broker_flow(db, man)
    ev = bfc.inspect_broker_flow_evidence(db, man)
    assert sha(db) == REAL_DB_SHA256
    return dict(db=db, man=man, new=new, old=old, xgb=xgb, px=px, raw=raw, raw_used=raw_used,
                cf=cf, ev=ev, sessions=sorted(px["date"].unique()))


def test_real_provenance_pins_the_current_snapshot(real):
    prov = real["new"].attrs["broker_flow"]
    assert (prov["db_sha256"], prov["manifest_sha256"], prov["manifest_snapshot"],
            prov["source_commit"], prov["audited_manifest"]) == \
        (REAL_DB_SHA256, REAL_MANIFEST_SHA256, bmr.REFRESH_CONTRACT, REAL_COMMIT, False)
    assert prov["image_sha256"] == REAL_DB_SHA256
    assert prov["accounting"] == {"canonical": 222_308, "duplicate": 3_642, "quarantined": 1_252,
                                  "excluded": 7_256, "raw": 234_458}
    assert (prov["canonical_sessions"], prov["quarantined_sessions"], prov["excluded_records"]) == \
        (252, ["2026-07-03"], 37)
    assert prov == real["xgb"].attrs["broker_flow"], "XGBoost and DDQN: one snapshot, one manifest"


def test_real_inputs_before_and_after(real):
    cf, ev, px = real["cf"], real["ev"], real["px"]
    frame = wfb.canonical_broker_flow_frame(cf)
    used = frame.merge(px[["date", "ticker"]], on=["date", "ticker"], how="inner")
    assert real["raw"] == (234_458, 307)
    assert (len(real["raw_used"]), real["raw_used"]["date"].nunique()) == (221_685, 278)
    assert (len(frame), frame["date"].nunique()) == (222_308, 252)
    assert (len(used), used["date"].nunique()) == (216_378, 251)
    assert set(frame["date"]) - set(used["date"]) == {"2026-10-01"}   # no price for it yet
    by = Counter((e.status, e.date_class) for e in ev)
    assert (by[(bfc.EXCLUDED, bfr.INFERRED_ONLY)], by[(bfc.EXCLUDED, bfr.MIXED)]) == (6_655, 601)
    assert cf.duplicates_collapsed == 3_642
    assert not [d for d in real["sessions"] if pd.Timestamp(d).weekday() >= 5]


def test_real_frame_shape_and_search_holdout_split(real):
    old, new = real["old"], real["new"]
    assert (len(old), len(new)) == (10_887, 10_124)
    assert (old["date"].nunique(), new["date"].nunique()) == (278, 251)
    assert old["ticker"].nunique() == new["ticker"].nunique() == 45
    assert (new["date"].min(), new["date"].max()) == (old["date"].min(), old["date"].max()) == \
        ("2025-08-04", "2026-09-30")
    assert ddqn_split(old) == [("2025-08-04", "2026-05-26", 194), ("2026-05-29", "2026-09-30", 84)]
    assert ddqn_split(new) == [("2025-08-04", "2026-04-24", 175), ("2026-04-27", "2026-09-30", 76)]


def test_real_2026_07_03_is_quarantined_and_never_stepped_across(real):
    cf, old, new = real["cf"], real["old"], real["new"]
    (q,) = cf.quarantined
    assert (q.canonical_session_date, q.rows, q.conflicting_keys) == ("2026-07-03", 1_252, 168)
    assert "2026-07-03" in set(old["date"]) and "2026-07-03" not in set(new["date"])
    for frame, same in ((old, 25), (new, 0)):
        e2 = frame[frame["date"] == "2026-07-02"].set_index("ticker")["episode_id"]
        e6 = frame[frame["date"] == "2026-07-06"].set_index("ticker")["episode_id"]
        common = e2.index.intersection(e6.index)
        assert len(common) == 25 and int((e2[common] == e6[common]).sum()) == same
    assert new.loc[new["date"] == "2026-07-06", CORR].isna().all()


def test_real_same_day_july_sessions_stay_same_day(real):
    ev, old, new = real["ev"], real["old"], real["new"]
    same = sorted({e.acquisition_date for e in ev if e.regime == bfr.LIVE
                   and e.status in (bfc.CANONICAL, bfc.DUPLICATE)
                   and e.canonical_session_date == e.acquisition_date})
    assert same == ["2026-07-06", "2026-07-07", "2026-07-09", "2026-07-10", "2026-07-13"]
    m = old.merge(new, on=["ticker", "date"], suffixes=("_old", "_new"))
    m = m[m["date"].isin(same)]
    assert len(m) == 128
    for f in BROKER_FEATURES + PRICE_FIELDS:
        assert np.array_equal(m[f + "_old"], m[f + "_new"], equal_nan=True), f


def test_real_2026_08_21_union_is_one_row_per_key_never_summed(real):
    cf, new, old = real["cf"], real["new"], real["old"]
    rows = [r for r in cf.rows if r.canonical_session_date == "2026-08-21"]
    assert len(rows) == 211
    assert Counter(r.acquisition_date for r in rows) == {"2026-08-22": 202, "2026-08-23": 9}
    per_ticker = Counter(r.ticker for r in rows)
    got = new[new["date"] == "2026-08-21"].set_index("ticker")["n_brokers"]
    assert got.to_dict() == {t: float(n) for t, n in per_ticker.items()}
    # the old frame's "08-21" was the 08-21 ACQUISITION: 181 rows of session 08-20
    assert old.loc[old["date"] == "2026-08-21", "n_brokers"].sum() == 181


def test_real_old_vs_new_changes_are_all_explained(real):
    old, new, cf, ev = real["old"], real["new"], real["cf"], real["ev"]
    assert sorted(set(old["date"]) - set(new["date"])) == \
        ["2026-07-03", "2026-07-08", "2026-08-11", "2026-08-24"] + weekdays("2026-08-26", "2026-09-28")
    assert sorted(set(new["date"]) - set(old["date"])) == ["2026-08-10"]
    m = old.merge(new, on=["ticker", "date"], how="outer", suffixes=("_old", "_new"), indicator=True)
    assert dict(Counter(m["_merge"].astype(str))) == {"both": 10_021, "left_only": 866,
                                                      "right_only": 103}
    both = m[m["_merge"] == "both"]

    def changed(f):
        a, b = both[f + "_old"], both[f + "_new"]
        if f.startswith("at_"):
            return a.astype(bool) != b.astype(bool)
        return ~(np.isclose(a, b, rtol=0, atol=1e-12) | (a.isna() & b.isna()))

    for f in PRICE_FIELDS:
        assert not changed(f).any(), f"{f} must not change: the price path is untouched"
    agg = np.zeros(len(both), bool)
    for f in BROKER_FEATURES:
        agg |= changed(f).to_numpy()
    assert agg.sum() == 724
    corr_only = changed(CORR).to_numpy() & ~agg
    assert dict(Counter(both.loc[corr_only, "date"])) == {"2026-07-06": 14, "2026-07-09": 11}

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
    assert sorted(d for d in touched if not reasons(d)) == []
    assert Counter("+".join(sorted(reasons(d))) for d in set(both.loc[agg, "date"])) == \
        {"re-key": 20, "dedupe+re-key": 6, "exclusion+re-key": 2}
    assert {d: reasons(prev) for d, prev in (("2026-07-06", "2026-07-03"),
                                              ("2026-07-09", "2026-07-08"))} == \
        {"2026-07-06": {"quarantine"}, "2026-07-09": {"not covered"}}


def test_real_episodes_never_bridge_and_only_refine_the_price_segmentation(real):
    old, new, px, sessions = real["old"], real["new"], real["px"], real["sessions"]
    assert len(sessions) == 280
    old_bridges = bridged_steps(old, sessions)
    assert len(old_bridges) == 192 and bridged_steps(new, sessions) == []
    n_old = old.groupby(["ticker", "episode_id"]).ngroups
    n_new = new.groupby(["ticker", "episode_id"]).ngroups
    assert (n_old, n_new) == (95, 287) and n_new - n_old == len(old_bridges)
    # every new episode lies inside one old price-level episode: no price
    # boundary (gap, corporate action) was lost, only holes were added
    pxe = px.set_index(["ticker", "date"])["episode_id"]
    tagged = new.assign(px_episode=pxe.reindex(pd.MultiIndex.from_frame(new[["ticker", "date"]])).to_numpy())
    assert (tagged.groupby(["ticker", "episode_id"])["px_episode"].nunique() == 1).all()


def test_real_ddqn_reads_the_xgboost_broker_aggregates(real):
    new, xgb, cf = real["new"], real["xgb"], real["cf"]
    m = new.merge(xgb, on=["ticker", "date"], suffixes=("", "_x"))
    assert len(m) == 9_948
    for f in BROKER_FEATURES:
        assert np.array_equal(m[f], m[f + "_x"], equal_nan=True), f
    differ = m[~(np.isclose(m[CORR], m[CORR + "_x"], rtol=0, atol=0) | (m[CORR].isna() & m[CORR + "_x"].isna()))]
    # only DDQN's ticker-level rule: the previous session is covered, the ticker is absent from it
    prev = dict(zip(real["sessions"][1:], real["sessions"]))
    canon = {(r.ticker, r.canonical_session_date) for r in cf.rows}
    assert len(differ) == 2 and differ[CORR].isna().all()
    for t, d in zip(differ["ticker"], differ["date"]):
        assert prev[d] in cf.sessions and (t, prev[d]) not in canon
