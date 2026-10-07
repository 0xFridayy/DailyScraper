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


def test_acquisition_date_is_never_the_feature_date():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_identical_acquisition_copies_are_not_summed_and_the_union_is_kept_once():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_unproven_evidence_never_reaches_the_panel():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_quarantined_sessions_contribute_nothing():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_the_session_after_a_withheld_session_has_no_one_day_correlation():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_same_day_live_acquisitions_stay_on_their_own_session(synth):
    cf = synth.load()
    rows = [r for r in cf.rows if r.acquisition_date == "2026-07-06"]
    assert len(rows) == 11 and {r.canonical_session_date for r in rows} == {"2026-07-06"}
    frame = wfb.canonical_broker_flow_frame(cf)
    assert len(frame[frame["date"] == "2026-07-06"]) == 11
    assert "2026-07-03" not in set(frame["date"])   # not shifted a session back


def test_absent_brokers_stay_absent():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_observed_zero_stays_an_observed_row():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_null_netval_stays_nan_and_is_still_an_observed_broker():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_adapter_refuses_anything_but_a_canonical_view(synth):
    with pytest.raises(TypeError, match="CanonicalBrokerFlow"):
        wfb.canonical_broker_flow_frame(pd.DataFrame(columns=wfb.CANONICAL_FRAME_COLUMNS))


# --------------------------------------------------------------------------
# Explicit manifest, snapshot identity, failure behaviour
# --------------------------------------------------------------------------

def test_the_manifest_is_required():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_the_cli_refuses_without_a_manifest(capsys):
    with pytest.raises(SystemExit) as e:
        wfb.parse_cli([])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--broker-flow-manifest is required" in err and "broker_flow_manifest_refresh.py" in err
    args = wfb.parse_cli(["--broker-flow-manifest", "m.json", "--db", "x.db"])
    assert (args.broker_flow_manifest, args.db) == ("m.json", "x.db")


def test_failures_propagate_unchanged_and_never_fall_back():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_a_manifest_that_does_not_describe_the_database_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_invalid_scan_evidence_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_a_non_quiescent_source_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_duplicate_raw_source_keys_are_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_price_connection_and_broker_flow_must_be_one_snapshot():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_unverifiable_price_connections_are_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_temp_objects_shadowing_the_price_tables_are_refused_in_any_case():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_unrelated_temp_objects_are_allowed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


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


def test_the_ordinary_snapshot_passes_and_only_build_panels_transaction_is_ended():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_a_wal_price_change_behind_identical_main_file_bytes_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def image_rows(path):
    con = bfr.connect_readonly(path)
    try:
        return con.execute("SELECT close FROM price_history WHERE date = '2026-07-06' "
                           "ORDER BY ticker").fetchall()
    finally:
        con.close()


def test_a_stale_open_connection_after_path_replacement_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_the_snapshot_cannot_change_under_clean_panel_unnoticed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_a_rollback_journal_writer_cannot_commit_during_the_read():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_a_wal_writer_during_the_read_is_invisible_to_it():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_no_usable_broker_rows_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


# --------------------------------------------------------------------------
# No raw fallback
# --------------------------------------------------------------------------

def test_build_panel_executes_no_broker_flow_sql_on_the_price_connection():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


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


def test_a_canonical_failure_is_never_answered_with_raw_rows():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


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

def test_provenance_is_exposed_and_is_not_a_feature():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_clean_price_intersection_is_unchanged():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_build_panel_is_deterministic():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_cli_runs_end_to_end_and_prints_the_snapshot_hashes():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


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


def test_real_provenance_pins_the_snapshot():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_real_inputs_before_and_after():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_real_2026_07_03_is_quarantined_and_absent_from_the_panel():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_real_same_day_july_acquisitions_stay_same_day():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_real_2026_08_21_union_is_one_row_per_key_never_summed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_real_old_vs_new_panel_changes_are_all_explained():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


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


def test_health_never_refreshes_over_an_explicit_bad_manifest():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_health_explicit_empty_manifest_is_not_omitted():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_health_explicit_good_manifest_is_used_as_given():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_health_omitted_manifest_refreshes_then_consumes():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_health_short_output_carries_the_point_in_time_warning():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


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


def test_telegram_report_carries_the_point_in_time_warning():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")


def test_github_summary_keeps_the_full_provenance_warning():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("walk_forward_backtest.build_panel")
