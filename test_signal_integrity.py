"""SIG1 synthetic regression fixtures. No production data or network writes.

Run: python -m pytest test_signal_integrity.py -q
Run history metadata below was verified with read-only GitHub logs. Price bars
and panels created by these fixtures are synthetic, including the ENRG model.
"""

from datetime import date, timedelta
import json
import sqlite3

import pytest

import check_signal_integrity as csi
import neobdm_source_contract as nsc
from idx_price_rules_sig1 import assess_limit, price_band
from price_audit import detect, load
from signal_integrity_health import open_readonly, require_current_prices, table_freshness
from sig1_coverage_contract import UniverseContract
from idx_calendar import is_idx_session, latest_idx_session_before

SYNTHETIC_UNIVERSE = UniverseContract("sig1-original-synthetic-16.v1", "fixed members in fixture()",
                                    tuple(f"T{k:03d}" for k in range(16)))


def check_fixture(conn, *args, **kwargs):
    return csi.check(conn, *args, universe=SYNTHETIC_UNIVERSE, **kwargs)


def add_preceding_prices(conn):
    latest = conn.execute("SELECT MAX(date) FROM price_history").fetchone()[0]
    previous = latest_idx_session_before(date.fromisoformat(latest)).isoformat()
    for k in range(16):
        add_price(conn, previous, f"T{k:03d}", close=100 + k, volume=1000 + k)


# Date, run ID, newest panel/price/broker dates, heuristic count, absent close.
# Broker newest values are checked by staleness log lines where present; None
# means no exact date was reported and the synthetic fixture uses capture day.
RUNS = [
    ("2026-10-06", 37433866422, "2026-10-05", "2026-10-05", None, 1, None),
    ("2026-10-07", 37588944551, "2026-10-07", "2026-10-06", "2026-10-04", 1, None),
    ("2026-10-08", 37746710982, "2026-10-08", "2026-10-07", "2026-10-04", 2, None),
    ("2026-10-09", 37902160262, "2026-10-09", "2026-10-08", None, 2, None),
    ("2026-10-10", 38035402394, "2026-10-10", "2026-10-09", None, 1, "GOTO"),
    ("2026-10-11", 38122569885, "2026-10-11", "2026-10-09", None, 1, "GOTO"),
    ("2026-10-12", 38220642765, "2026-10-12", "2026-10-09", None, 1, "GOTO"),
    ("2026-10-13", 38369043806, "2026-10-12", "2026-10-09", None, 1, "GOTO"),
    ("2026-10-14", 38523619405, "2026-10-14", "2026-10-09", None, 1, "BNBR"),
]


def add_price(conn, day, ticker="AAAA", close=100, volume=1000, **extra):
    bar = dict(open=close, high=close + 1, low=close - 1, close=close, volume=volume)
    bar.update(extra)
    conn.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?,?,?,?,?)",
                 (day, ticker, *(bar[c] for c in csi.OHLCV)))


def fixture(panel_date="2026-10-14", price_date="2026-10-13", broker_date=None):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE price_history (date TEXT, ticker TEXT, open REAL, high REAL, "
                 "low REAL, close REAL, volume REAL, PRIMARY KEY(date,ticker))")
    conn.execute("CREATE TABLE broker_flow (date TEXT, ticker TEXT, broker_code TEXT, netval REAL, bval REAL, sval REAL, bavg REAL, savg REAL)")
    conn.execute("INSERT INTO broker_flow VALUES (?, 'AAAA', 'AK', 1, 2, 1, 100, 100)", (broker_date or panel_date,))
    active = nsc.ACTIVE_REQUEST_COLUMNS
    conn.execute("CREATE TABLE market_summary_daily (date TEXT, ticker TEXT, "
                 + ",".join(f'"{c}"' for c in active) + ", is_unusual_volume)")
    for k in range(16):
        ticker = f"T{k:03d}"
        values = [ticker if c == "symbol" else '["AK"]' if c == "top_5_buyer" else
                  100 + k if c == "close" else 101 + k if c == "high" else 99 + k if c == "low" else k + 1
                  for c in active]
        conn.execute("INSERT INTO market_summary_daily VALUES (" + ",".join("?" * (len(values) + 3)) + ")",
                     [panel_date, ticker, *values, None])
        add_price(conn, price_date, ticker, close=100 + k, volume=1000 + k)
    conn.execute("CREATE TABLE konglo_signal_watch (flag_date TEXT, ticker TEXT, sources TEXT DEFAULT 'dashboard_Bandarmologi')")
    conn.executemany("INSERT INTO konglo_signal_watch (flag_date,ticker) VALUES (?,?)",
                     [(panel_date, f"T{k:03d}") for k in range(16)])
    # Only the manifest fields this monitor reads. Captures genuinely use v2.
    conn.execute("CREATE TABLE ms_capture_manifest (capture_id TEXT, capture_date TEXT, "
                 "contract_version TEXT, requested_columns TEXT, capture_started_utc TEXT, "
                 "contract_status TEXT, contract_issues TEXT, catalog_columns_sha256 TEXT, catalog_column_count INTEGER)")
    conn.execute("INSERT INTO ms_capture_manifest VALUES (?,?,?,?,?,?,?,?,?)",
                 ("synthetic", panel_date, nsc.CONTRACT_VERSION, json.dumps(active), panel_date + "T01:00:00Z",
                  nsc.CONTRACT_OK, "[]", "synthetic", 430))
    conn.execute("CREATE TABLE signal_source_status (flag_date TEXT, source TEXT, status TEXT, detail TEXT)")
    conn.execute("INSERT INTO signal_source_status VALUES (?,?,?,?)",
                 (panel_date, "top_akum_bandar", nsc.RETIRED_SOURCE, "synthetic retirement"))
    for source in ("dashboard_Bandarmologi", "dashboard_NonRetail", "dashboard_Foreign", "broker_stalker"):
        conn.execute("INSERT INTO signal_source_status VALUES (?,?,?,?)", (panel_date, source, nsc.HITS, "synthetic"))
    conn.execute("CREATE TABLE price_quarantine AS SELECT * FROM price_history WHERE 0")
    return conn


def categories(stats):
    return {f["category"] for f in stats.get("findings", [])}


@pytest.mark.parametrize("run", RUNS, ids=lambda r: str(r[1]))
def test_nine_run_failures_are_reclassified_and_remain_unhealthy(run):
    day, run_id, panel_day, price_day, broker_day, count, missing = run
    conn = fixture(panel_day, price_day, broker_day)
    # Model a corporate-action discontinuity; this is not an asserted source bar.
    add_price(conn, "2026-10-02", "ENRG", close=1440, volume=2000)
    add_price(conn, "2026-10-05", "ENRG", close=1030, volume=2001)
    if count == 2:
        add_price(conn, "2026-10-06", "VKTR", close=100, volume=3000)
        add_price(conn, "2026-10-07", "VKTR", close=150, volume=3001)
    if missing:
        conn.execute("UPDATE konglo_signal_watch SET ticker=? WHERE ticker='T000'", (missing,))
    before = conn.serialize()
    states = {"daily-scrape.yml": "active"}
    # GitHub shows top-up ran through Oct 10, then was disabled. Its historical
    # state during earlier checks is not guessed from today's disabled state.
    if day >= "2026-10-11":
        states["price-history-topup.yml"] = "disabled_manually"
    problems, notes, stats = check_fixture(conn, date.fromisoformat(day), states)
    assert stats["unresolved_suspects"] == count
    assert stats["new_contamination"] == 0
    assert "BACKLOG_SUSPECT" in categories(stats)
    assert "RETIRED_SOURCE" in categories(stats)
    assert csi.exit_code(problems, stats) != 0
    assert "ticker-selection defect is writing" not in csi.format_report(problems, notes, stats)
    if missing:
        assert stats["unmeasurable_signals"] == [missing]
        assert "UNMEASURABLE_SIGNALS" in categories(stats)
    if day == "2026-10-14":
        assert {"PRODUCER_PAUSED", "SOURCE_STALE"} <= categories(stats)
    assert conn.serialize() == before
    conn.close()


@pytest.mark.parametrize("state", ["active", "disabled_manually", "unknown"])
def test_current_table_does_not_turn_historical_suspect_into_new_contamination(state):
    conn = fixture()
    add_preceding_prices(conn)
    add_price(conn, "2026-10-01", "ENRG", close=1440)
    add_price(conn, "2026-10-05", "ENRG", close=1030)
    p, n, s = check_fixture(conn, date(2026, 10, 14), {"price-history-topup.yml": state})
    assert s["new_contamination"] == 0 and s["unresolved_suspects"] == 1
    assert csi.exit_code(p, s) == 2 and "DEGRADED" in csi.format_report(p, n, s)


def test_historical_heuristic_does_not_disappear_when_price_dates_advance():
    conn = fixture()
    add_preceding_prices(conn)
    add_price(conn, "2026-08-03", "AAAA", close=100, volume=222)
    add_price(conn, "2026-08-04", "AAAA", close=150, volume=223)
    for i in range(23):
        day = date(2026, 9, 21) + timedelta(days=i)
        if not is_idx_session(day):
            continue
        add_price(conn, day.isoformat(), "AAAA", close=150, volume=223 + i)
    p, _, s = check_fixture(conn, date(2026, 10, 14))
    assert s["unresolved_suspects"] == 1 and s["recent_unresolved_suspects"] == 0
    assert "BACKLOG_SUSPECT" in categories(s) and csi.exit_code(p, s) == 2


def test_series_break_alone_is_unknown_and_unresolved():
    conn = fixture()
    add_preceding_prices(conn)
    sessions = [date(2026, 9, 1) + timedelta(days=i) for i in range(39)
                if is_idx_session(date(2026, 9, 1) + timedelta(days=i))]
    for i, d in enumerate(sessions):
        add_price(conn, d.isoformat(), "AAAA", close=600 if i in (11, 12) else 100, volume=222 + i)
    p, _, s = check_fixture(conn, date(2026, 10, 14))
    findings = [f for f in s["findings"] if f["category"] == "BACKLOG_SUSPECT"]
    assert any("limit_violation=False, series_break=True" in f["detail"] for f in findings)
    assert all(f["evidence"]["classification"] == "UNKNOWN" for f in findings)
    assert csi.exit_code(p, s) == 2 and s["new_contamination"] == 0


def test_duplicate_keys_are_structural_not_cross_ticker_contamination():
    conn = fixture()
    conn.execute("CREATE TABLE duplicate_price AS SELECT * FROM price_history")
    conn.execute("DROP TABLE price_history")
    conn.execute("ALTER TABLE duplicate_price RENAME TO price_history")
    conn.execute("INSERT INTO price_history SELECT * FROM price_history WHERE ticker='T000'")
    p, _, s = csi.check(conn, date(2026, 10, 14))
    assert csi.exit_code(p, s) == 1 and "STRUCTURAL_DEFECT" in categories(s)
    assert "CORRUPTION_PRESENT" not in categories(s) and s["new_contamination"] == 0


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("with_baseline", [False, True])
def test_real_collisions_fail_when_paused_stale_or_missing_quarantine(paused, with_baseline):
    conn = fixture(price_date="2026-10-09")
    conn.execute("DROP TABLE price_quarantine")
    baseline = load(conn)
    # A new old-date collision cannot be hidden by the frozen table endpoint.
    add_price(conn, "2026-08-03", "AAAA", close=200, volume=222)
    add_price(conn, "2026-08-03", "BBBB", close=200, volume=222)
    conn.execute("INSERT INTO market_summary_daily(date,ticker,close) VALUES ('2026-08-04','BBBB',201)")
    states = {"price-history-topup.yml": "disabled_manually" if paused else "active"}
    p, _, s = csi.check(conn, date(2026, 10, 14), states, baseline if with_baseline else None)
    assert csi.exit_code(p, s) == 1
    assert ("NEW_CONTAMINATION" if with_baseline else "CORRUPTION_PRESENT") in categories(s)
    assert s["new_contamination"] == (2 if with_baseline else 0)


def test_unchanged_collision_fails_but_is_not_claimed_new():
    conn = fixture()
    add_price(conn, "2026-10-13", "AAAA", close=200, volume=222)
    add_price(conn, "2026-10-13", "BBBB", close=200, volume=222)
    conn.execute("INSERT INTO market_summary_daily(date,ticker,close) VALUES ('2026-10-14','BBBB',201)")
    p, _, s = csi.check(conn, date(2026, 10, 14), price_baseline=load(conn))
    assert csi.exit_code(p, s) == 1 and s["new_contamination"] == 0
    assert "CORRUPTION_PRESENT" in categories(s)


def test_collision_is_new_when_unchanged_member_gains_a_new_clone():
    conn = fixture()
    add_price(conn, "2026-10-13", "AAAA", close=200, volume=222)
    baseline = load(conn)
    add_price(conn, "2026-10-13", "BBBB", close=200, volume=222)
    conn.execute("INSERT INTO market_summary_daily(date,ticker,close) VALUES ('2026-10-14','BBBB',201)")
    p, _, s = csi.check(conn, date(2026, 10, 14), price_baseline=baseline)
    assert csi.exit_code(p, s) == 1 and s["new_contamination"] == 2


def test_quarantine_does_not_hide_changed_bars_or_structural_defects():
    conn = fixture()
    conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history")
    conn.execute("UPDATE price_history SET open=100,high=101,low=99,close=100,volume=1000 WHERE ticker='T001'")
    p, _, s = csi.check(conn, date(2026, 10, 14))
    assert "CORRUPTION_PRESENT" in categories(s) and csi.exit_code(p, s) == 1
    conn.execute("UPDATE price_history SET low=200 WHERE ticker='T002'")
    p, _, s = csi.check(conn, date(2026, 10, 14))
    assert "STRUCTURAL_DEFECT" in categories(s) and csi.exit_code(p, s) == 1


@pytest.mark.parametrize("change", ["close=NULL", "close=0", "volume=-1", "high=1", "open=200", "close='broken'", "high=1e999"])
def test_structural_price_faults_still_fail(change):
    conn = fixture()
    # Structural faults outside the lookback still fail, even if quarantined.
    add_price(conn, "2026-01-02", "ZZZZ")
    conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history")
    conn.execute(f"UPDATE price_history SET {change} WHERE ticker='ZZZZ'")
    p, _, s = csi.check(conn, date(2026, 10, 14), {"price-history-topup.yml": "disabled_manually"})
    assert csi.exit_code(p, s) == 1 and "STRUCTURAL_DEFECT" in categories(s)


def test_missing_table_is_reported_and_other_checks_still_run():
    conn = fixture()
    conn.execute("DROP TABLE price_history")
    p, n, s = csi.check(conn, date(2026, 10, 14))
    assert csi.exit_code(p, s) == 1 and "STRUCTURAL_DEFECT" in categories(s)
    assert s["retired_fields"] == ["is_unusual_volume"]


def test_healthy_capture_and_legitimate_retirement_remain_healthy():
    conn = fixture()
    add_preceding_prices(conn)
    p, n, s = check_fixture(conn, date(2026, 10, 14))
    assert p == [] and csi.exit_code(p, s) == 0
    assert "RETIRED_SOURCE" in categories(s)
    assert "zero-hit" in csi.format_report(p, n, s)


def test_source_unavailable_and_excess_missing_closes_still_fail():
    conn = fixture()
    conn.execute("UPDATE signal_source_status SET status=?", (nsc.SOURCE_UNAVAILABLE,))
    conn.execute("UPDATE market_summary_daily SET close=NULL WHERE ticker < 'T008'")
    p, _, s = csi.check(conn, date(2026, 10, 14))
    assert csi.exit_code(p, s) == 1
    assert {"SOURCE_UNAVAILABLE", "UNMEASURABLE_SIGNALS", "SOURCE_CONTRACT_FAILURE"} <= categories(s)


def test_recent_cross_source_mismatch_is_not_diluted_by_old_agreement():
    conn = fixture()
    # Twenty old captures agree exactly. Only the recent capture disagrees.
    for i in range(1, 21):
        capture = date(2026, 7, i + 1).isoformat()
        price = date(2026, 7, i).isoformat()
        for k in range(16):
            conn.execute("INSERT INTO market_summary_daily(date,ticker,close) VALUES (?,?,?)",
                         (capture, f"T{k:03d}", 100 + k))
            add_price(conn, price, f"T{k:03d}", close=100 + k, volume=1000 + k)
    # Fill the other nine recent captures with disagreement as well.
    for i in range(1, 10):
        capture = date(2026, 10, 14) - timedelta(days=i)
        for k in range(16):
            conn.execute("INSERT INTO market_summary_daily(date,ticker,close) VALUES (?,?,?)",
                         (capture.isoformat(), f"T{k:03d}", 200 + k))
            add_price(conn, (capture - timedelta(days=1)).isoformat(), f"T{k:03d}", close=100 + k,
                      volume=1000 + k)
    conn.execute("UPDATE market_summary_daily SET close=close+100 WHERE date='2026-10-14'")
    p, n, s = [], [], {}
    csi.check_cross_source(conn, p, n, s)
    assert p and s["cross_source_overlap_latest"] == "2026-10-13"


@pytest.mark.parametrize("as_of,expected", [(date(2026, 10, 12), "2026-10-09"),
                                           (date(2026, 8, 18), "2026-08-14")])
def test_weekends_and_idx_holidays_do_not_create_missing_sessions(as_of, expected):
    conn = fixture(price_date=expected)
    add_preceding_prices(conn)
    health = table_freshness(conn, "price_history", as_of, universe=SYNTHETIC_UNIVERSE)
    assert health["freshness"] == "CURRENT" and health["expected"] == expected


def test_a_same_day_price_session_is_due_only_when_the_caller_says_it_closed():
    as_of = date(2026, 10, 14)
    conn = fixture(price_date=as_of.isoformat())
    add_preceding_prices(conn)
    default = table_freshness(conn, "price_history", as_of, universe=SYNTHETIC_UNIVERSE)
    assert default["expected"] == "2026-10-13" and default["ahead_of_cutoff_dates"] == ["2026-10-14"]
    closed = table_freshness(conn, "price_history", as_of, universe=SYNTHETIC_UNIVERSE, closed_session=as_of)
    assert closed["freshness"] == "CURRENT" and closed["expected"] == "2026-10-14"
    assert not closed["ahead_of_cutoff_dates"]
    # Capture-dated producers keep their own convention.
    assert (table_freshness(conn, "broker_flow", as_of, universe=SYNTHETIC_UNIVERSE, closed_session=as_of)
            == table_freshness(conn, "broker_flow", as_of, universe=SYNTHETIC_UNIVERSE))
    assert require_current_prices(conn, as_of, SYNTHETIC_UNIVERSE, closed_session=as_of)["expected"] == "2026-10-14"
    ahead = lambda problems: [p for p in problems if "AHEAD_OF_CUTOFF" in p]
    assert ahead(check_fixture(conn, as_of)[0])
    assert not ahead(check_fixture(conn, as_of, closed_session=as_of)[0])


def test_paused_and_stale_are_separate_and_unknown_state_is_not_assumed_paused():
    conn = fixture(price_date="2026-10-09")
    health = table_freshness(conn, "price_history", date(2026, 10, 14),
                             {"price-history-topup.yml": "disabled_manually"}, universe=SYNTHETIC_UNIVERSE)
    assert health["status"] == "PRODUCER_PAUSED" and health["freshness"] == "SOURCE_STALE"
    assert table_freshness(conn, "price_history", date(2026, 10, 14), universe=SYNTHETIC_UNIVERSE)["status"] == "SOURCE_STALE"


def test_current_report_refuses_stale_and_partial_prices():
    conn = fixture(price_date="2026-10-09")
    with pytest.raises(RuntimeError, match="Current ML report refused"):
        require_current_prices(conn, date(2026, 10, 14), SYNTHETIC_UNIVERSE)
    add_price(conn, "2026-10-13", "T000")
    with pytest.raises(RuntimeError, match="incomplete price_history"):
        require_current_prices(conn, date(2026, 10, 14), SYNTHETIC_UNIVERSE)
    for k in range(16):
        add_price(conn, "2026-10-12", f"T{k:03d}", close=100+k, volume=1000+k)
    with pytest.raises(RuntimeError, match="missing usable closes"):
        require_current_prices(conn, date(2026, 10, 14), SYNTHETIC_UNIVERSE)
    for k in range(1, 16):
        add_price(conn, "2026-10-13", f"T{k:03d}", close=100+k, volume=1000+k)
    assert require_current_prices(conn, date(2026, 10, 14), SYNTHETIC_UNIVERSE)["latest"] == "2026-10-13"


def test_readonly_open_cannot_modify_a_fixture_or_create_a_missing_database(tmp_path):
    path = tmp_path / "synthetic.db"
    source = fixture()
    source.commit()
    dest = sqlite3.connect(path)
    source.backup(dest)
    dest.close()
    conn = open_readonly(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM price_history")
    conn.close()
    with pytest.raises(FileNotFoundError):
        open_readonly(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_enrg_boundary_is_evidence_not_a_ticker_exemption():
    conn = fixture()
    add_price(conn, "2026-10-02", "ENRG", close=1440)
    add_price(conn, "2026-10-05", "ENRG", close=1030)
    row = next(r for r in detect(load(conn)).itertuples() if r.ticker == "ENRG" and r.date == "2026-10-05")
    result = assess_limit(row)
    assert row.limit_violation
    assert result["classification"] == "CORPORATE_ACTION_BOUNDARY"
    assert result["theoretical_unrounded"] == pytest.approx(1063.3333333333333)
    assert result["bar_verified"] is False and result["reference_verified"] is False
    assert len(result["sources"]) == 4
    add_price(conn, "2026-10-05", "CLON", close=1030)
    conn.execute("INSERT INTO market_summary_daily(date,ticker,close) VALUES ('2026-10-06','CLON',1100)")
    p, _, s = csi.check(conn, date(2026, 10, 14))
    assert csi.exit_code(p, s) == 1 and "CORRUPTION_PRESENT" in categories(s)


@pytest.mark.parametrize("reference,board,band", [(200, "main", (170, 270)), (5000, "main", (4250, 6250)),
    (5001, "development", (4250.85, 6001.2)), (10, "main", (9, 11)), (1, "main", (1, 2)),
    (100, "watchlist", (90, 110))])
def test_effective_dated_rules_are_isolated_diagnostics(reference, board, band):
    assert price_band(reference, date(2026, 10, 5), board) == pytest.approx(band)
    assert price_band(reference, date(2026, 9, 27), board) is None
    assert price_band(reference, date(2026, 10, 16), board) is None
    assert price_band(reference, date(2026, 10, 5), "unknown") is None


def test_github_state_reader_only_uses_get(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"workflows": [{"path": ".github/workflows/price-history-topup.yml", "state": "disabled_manually"}]}

    calls = []
    monkeypatch.setattr(csi.requests, "get", lambda *a, **kw: calls.append((a, kw)) or Response())
    assert csi.github_producer_states("example/synthetic") == {"price-history-topup.yml": "disabled_manually"}
    assert len(calls) == 1 and calls[0][1]["timeout"] == 15


def test_daily_ml_orchestrator_refuses_before_refresh_calculation_or_send(monkeypatch, tmp_path):
    # Exercise the actual orchestrator, with its real freshness gate and a
    # synthetic truncated source. A failure must precede all expensive work.
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "synthetic")
    import run_ml_reports as rmr

    conn = fixture(price_date="2026-10-09")
    from test_sig1_corrective import save_fixture
    path = tmp_path / "synthetic.db"
    save_fixture(conn, path)
    calls = []
    monkeypatch.setattr(rmr, "DB_PATH", str(path))
    import signal_integrity_health as health
    monkeypatch.setattr(rmr, "report_admission", lambda c: health.report_admission(c, date(2026, 10, 14), universe=SYNTHETIC_UNIVERSE))
    for name in ("refresh_broker_flow_manifest", "run_xgboost_report", "run_strategy_variants_report",
                 "run_ddqn_report", "send_telegram"):
        monkeypatch.setattr(rmr, name, lambda *a, **kw: calls.append("unexpected work"))
    with pytest.raises(RuntimeError, match="Current ML report refused"):
        rmr.main([])
    assert calls == []


@pytest.mark.parametrize("bad_date", ["2026-10-99", "2026-01-99", "2026-10-15", "not-a-date"])
def test_invalid_or_future_dates_fail_without_claiming_contamination(bad_date):
    conn = fixture()
    add_price(conn, bad_date, "ZZZZ")
    p, _, s = csi.check(conn, date(2026, 10, 14))
    assert csi.exit_code(p, s) == 1
    assert ("AHEAD_OF_CUTOFF" if bad_date == "2026-10-15" else "STRUCTURAL_DEFECT") in categories(s)
    assert s.get("new_contamination", 0) == 0


def replay_baseline():
    """Run the actual supplied baseline and repaired monitor on nine synthetic states.

    Uses a Git blob as code input only. Neither monitor writes to the database.
    The printed rows can be regenerated with python test_signal_integrity.py.
    """
    import subprocess
    import types

    baseline = "f73f24bda4a66f9470ee348c306cb0bb0baf3c48"
    legacy = types.ModuleType("sig1_baseline")
    legacy.__file__ = csi.__file__
    source = subprocess.check_output(["git", "show", f"{baseline}:check_signal_integrity.py"]).decode("utf-8")
    exec(compile(source, "sig1_baseline.py", "exec"), legacy.__dict__)

    class Clock(date):
        current = date(2026, 10, 6)

        @classmethod
        def today(cls):
            return cls.current

    legacy.date = Clock
    for day, run_id, panel_day, price_day, broker_day, count, missing in RUNS:
        Clock.current = date.fromisoformat(day)
        conn = fixture(panel_day, price_day, broker_day)
        # Historical overlap keeps alignment from dominating the replay. This
        # isolates the observed recurrent heuristic and producer failures.
        for k in range(16):
            conn.execute("INSERT INTO market_summary_daily(date,ticker,close) VALUES ('2026-09-30',?,?)",
                         (f"T{k:03d}", 80 + k))
            add_price(conn, "2026-09-29", f"T{k:03d}", close=80 + k, volume=900 + k)
        add_price(conn, "2026-10-02", "ENRG", close=1440, volume=2000)
        add_price(conn, "2026-10-05", "ENRG", close=1030, volume=2001)
        if count == 2:
            add_price(conn, "2026-10-06", "VKTR", close=100, volume=3000)
            add_price(conn, "2026-10-07", "VKTR", close=150, volume=3001)
        if missing:
            conn.execute("UPDATE konglo_signal_watch SET ticker=? WHERE ticker='T000'", (missing,))
        states = {"price-history-topup.yml": "disabled_manually"} if day >= "2026-10-11" else {}
        p, n, s = legacy.check(conn)
        q, m, t = csi.check(conn, Clock.current, states)
        print(json.dumps(dict(day=day, run_id=run_id, before_exit=legacy.exit_code(p, s),
                              before_price="NEW_CONTAMINATION" if any("NEW contaminated" in x for x in p) else "BACKLOG",
                              before_suspects=s.get("fresh_suspects"), after_exit=csi.exit_code(q, t),
                              after_new_contamination=t.get("new_contamination"), after_categories=sorted(categories(t)))))
        conn.close()


if __name__ == "__main__":
    replay_baseline()
