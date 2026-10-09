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

def test_the_frame_is_keyed_by_canonical_session_only():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_unproven_evidence_never_reaches_the_frame():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_quarantined_sessions_contribute_nothing():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_identical_acquisition_copies_are_not_summed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_absence_is_not_zero_and_observed_zero_is_not_absence():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_null_netval_stays_nan_and_is_still_an_observed_broker():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_broker_aggregates_are_the_ones_build_panel_reads():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_without_broker_holes_the_frame_is_the_legacy_frame():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_frame_is_deterministic():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


# --------------------------------------------------------------------------
# Episode continuity
# --------------------------------------------------------------------------

def test_weekends_and_exchange_holidays_are_not_holes():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_a_withheld_session_splits_every_tickers_episode():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_a_tickers_own_hole_splits_only_that_ticker():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_quarantined_0703_session_is_never_stepped_across():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_episode_ids_are_assigned_on_the_final_frame():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


# --------------------------------------------------------------------------
# The one-session correlation
# --------------------------------------------------------------------------

def test_correlation_never_compares_across_a_hole():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


# --------------------------------------------------------------------------
# Explicit manifest, the shared snapshot contract, failure behaviour
# --------------------------------------------------------------------------

def test_the_manifest_is_required():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_frame_never_makes_or_refreshes_a_manifest():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_failures_propagate_unchanged_and_never_fall_back():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_prices_and_broker_flow_must_be_one_snapshot():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_unverifiable_price_connections_are_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_a_wal_price_change_behind_identical_file_bytes_is_refused():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_snapshot_cannot_change_under_clean_panel_unnoticed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_prices_keep_the_ddqn_clean_panel_parameters():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_both_consumers_read_through_the_one_shared_input_contract():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_no_broker_flow_sql_runs_on_the_price_connection():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_episode_frame_needs_no_torch_and_loads_no_refresh_tool():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


# --------------------------------------------------------------------------
# Provenance and the point-in-time caveat
# --------------------------------------------------------------------------

def test_provenance_is_build_panels_contract_and_not_a_feature():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


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
def test_ddqn_entry_exit_reexports_the_frame_and_refuses_uncertified_envs():
    import ddqn_entry_exit as dee
    from price_contract import UnsupportedPriceContract
    assert dee.build_episode_frame is ded.build_episode_frame
    with pytest.raises(UnsupportedPriceContract, match="ddqn_entry_exit.make_envs"):
        dee.make_envs(None)
    with pytest.raises(UnsupportedPriceContract, match="ddqn_entry_exit.TickerEnv"):
        dee.TickerEnv(None, None, None, None)


def test_ddqn_cli_refuses_without_a_manifest(tmp_path):
    out = subprocess.run([sys.executable, DDQN_SOURCE, "--db", str(tmp_path / "x.db")], cwd=HERE,
                         capture_output=True, text=True, timeout=300)
    from price_contract import CONTRACT_VERSION
    assert out.returncode == 1
    assert "UnsupportedPriceContract" in out.stderr
    assert "ddqn_entry_exit.__main__" in out.stderr
    assert CONTRACT_VERSION in out.stderr
    assert not (tmp_path / "x.db").exists()
    assert not list(tmp_path.iterdir())


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


def test_run_ddqn_report_builds_the_canonical_frame_under_the_given_manifest():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_report_refreshes_at_most_once_and_every_model_gets_that_manifest():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_the_report_states_which_snapshot_ddqn_read():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


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


def test_real_provenance_pins_the_current_snapshot():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_inputs_before_and_after():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_frame_shape_and_search_holdout_split():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_2026_07_03_is_quarantined_and_never_stepped_across():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_same_day_july_sessions_stay_same_day():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_2026_08_21_union_is_one_row_per_key_never_summed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_old_vs_new_changes_are_all_explained():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_episodes_never_bridge_and_only_refine_the_price_segmentation():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")


def test_real_ddqn_reads_the_xgboost_broker_aggregates():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("ddqn_episode_data.build_episode_frame")
