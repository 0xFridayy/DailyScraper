"""Isolated SIG1 corrective witnesses; every source row is synthetic."""

from dataclasses import replace
from datetime import date
import hashlib
from pathlib import Path
import sqlite3

import pytest

import check_signal_integrity as csi
import signal_integrity_health as health
from sig1_coverage_contract import (CoverageException, EXPECTED_UNIVERSE, UniverseContract)
from ticker_registry import load_registry
from test_signal_integrity import add_price, fixture, categories


AS_OF = date(2026, 10, 14)
SYNTHETIC_UNIVERSE = UniverseContract("synthetic-16.v1", "test_sig1_corrective:fixed 16 members",
                                    tuple(f"T{k:03d}" for k in range(16)))


def current_fixture():
    conn = fixture()
    for day in ("2026-10-09", "2026-10-12"):
        for k in range(16):
            add_price(conn, day, f"T{k:03d}", close=100 + k, volume=1000 + k)
    return conn


def production_scope_fixture():
    conn = fixture()
    conn.execute("DELETE FROM price_history")
    for day in ("2026-10-09", "2026-10-12", "2026-10-13"):
        for k, ticker in enumerate(EXPECTED_UNIVERSE.tickers):
            add_price(conn, day, ticker, close=100 + k, volume=1000 + k)
    for k, ticker in enumerate(EXPECTED_UNIVERSE.tickers[:16]):
        conn.execute("UPDATE market_summary_daily SET ticker=?,symbol=? WHERE ticker=?",
                     (ticker, ticker, f"T{k:03d}"))
        conn.execute("UPDATE konglo_signal_watch SET ticker=? WHERE ticker=?", (ticker, f"T{k:03d}"))
    return conn


def save_fixture(conn, path):
    conn.commit()
    target = sqlite3.connect(path)
    conn.backup(target)
    target.close()


def test_high1_persistent_dropout_refused():
    conn = current_fixture()
    conn.execute("DELETE FROM price_history WHERE ticker='T015' AND date IN ('2026-10-12','2026-10-13')")
    assert conn.execute("SELECT date FROM price_history WHERE ticker='T015'").fetchall() == [("2026-10-09",)]
    with pytest.raises(RuntimeError, match="T015"):
        health.require_current_prices(conn, AS_OF, SYNTHETIC_UNIVERSE)


def test_high2_quarantined_latest_session_refused():
    conn = current_fixture()
    conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history WHERE date='2026-10-13'")
    assert conn.execute("SELECT MAX(date) FROM price_history").fetchone()[0] == "2026-10-13"
    with pytest.raises(RuntimeError, match="usable newest 2026-10-12"):
        health.require_current_prices(conn, AS_OF, SYNTHETIC_UNIVERSE)


def test_high3_partial_topup_full_health_and_cli_refuse(tmp_path, capsys):
    conn = production_scope_fixture()
    first = EXPECTED_UNIVERSE.tickers[0]
    conn.execute("DELETE FROM price_history WHERE date > '2026-10-09' AND ticker != ?", (first,))
    problems, _, stats = csi.check(conn, AS_OF)
    assert csi.exit_code(problems, stats) != 0
    assert {"SOURCE_STALE", "INCOMPLETE_COVERAGE"} <= categories(stats)
    assert len(stats["freshness"]["price_history"]["coverage"]["2026-10-13"]["missing"]) == len(EXPECTED_UNIVERSE.tickers) - 1
    path = tmp_path / "synthetic.db"
    save_fixture(conn, path)
    with pytest.raises(SystemExit) as stopped:
        csi.main(["--db", str(path), "--as-of", "2026-10-14"])
    assert stopped.value.code != 0
    assert "INCOMPLETE_COVERAGE" in capsys.readouterr().out


@pytest.mark.parametrize("day", ["2026-08-17", "2026-10-10"])
def test_medium1_historical_non_session_rejected(day):
    conn = current_fixture()
    add_price(conn, day, "ZZZZ", close=300, volume=3000)
    problems = []
    csi.check_value_sanity(conn, problems)
    assert len(problems) == 1 and f"non-session price date {day}" in problems[0]


def test_unsupported_historical_calendar_is_unknown_and_unhealthy():
    conn = current_fixture()
    add_price(conn, "2024-10-14", "ZZZZ", close=300, volume=3000)
    problems, _, stats = csi.check(conn, AS_OF, universe=SYNTHETIC_UNIVERSE)
    assert "CALENDAR_UNSUPPORTED" in categories(stats)
    assert not any("2024-10-14" in p and "non-session" in p for p in problems)
    assert csi.exit_code(problems, stats) != 0
    admitted = health.require_current_prices(conn, AS_OF, SYNTHETIC_UNIVERSE)
    assert admitted["historical_calendar_status"] == "UNKNOWN/UNSUPPORTED"
    assert admitted["freshness"] == "CURRENT"


def test_medium2_numeric_contract_without_type_baseline():
    conn = fixture()
    conn.execute("UPDATE market_summary_daily SET m_dn_0='text'")
    problems = []
    csi.check_schema_and_coverage(conn, problems, [], {})
    assert len(problems) == 1
    assert "numeric source contract: m_dn_0" in problems[0]


@pytest.mark.parametrize("change", ["close='broken'", "close=1e999", "low=200", "volume=-1"])
def test_report_admission_rejects_invalid_ohlcv(change):
    conn = current_fixture()
    conn.execute(f"UPDATE price_history SET {change} WHERE date='2026-10-13' AND ticker='T000'")
    with pytest.raises(RuntimeError, match="INVALID_DATA"):
        health.require_current_prices(conn, AS_OF, SYNTHETIC_UNIVERSE)


def test_numeric_looking_sqlite_text_is_not_a_numeric_price():
    conn = current_fixture()
    conn.execute("CREATE TABLE untyped AS SELECT * FROM price_history")
    conn.execute("DROP TABLE price_history")
    # A table with no affinity preserves the actual source storage class.
    conn.execute("CREATE TABLE price_history(date,ticker,open,high,low,close,volume)")
    conn.execute("INSERT INTO price_history SELECT * FROM untyped")
    conn.execute("UPDATE price_history SET close='100' WHERE date='2026-10-13' AND ticker='T000'")
    assert conn.execute("SELECT typeof(close) FROM price_history WHERE date='2026-10-13' AND ticker='T000'").fetchone()[0] == "text"
    with pytest.raises(RuntimeError, match="nonnumeric SQLite"):
        health.require_current_prices(conn, AS_OF, SYNTHETIC_UNIVERSE)


def test_missing_preceding_session_fails_closed():
    conn = current_fixture()
    conn.execute("DELETE FROM price_history WHERE date != '2026-10-13'")
    with pytest.raises(RuntimeError, match="2026-10-12"):
        health.require_current_prices(conn, AS_OF, SYNTHETIC_UNIVERSE)


def test_two_required_sessions_at_calendar_boundary_do_not_lookup_a_third():
    conn = current_fixture()
    conn.execute("DELETE FROM price_history WHERE date='2026-10-09'")
    conn.execute("UPDATE price_history SET date='2026-01-02' WHERE date='2026-10-12'")
    conn.execute("UPDATE price_history SET date='2026-01-05' WHERE date='2026-10-13'")
    assert health.require_current_prices(conn, date(2026, 1, 6), SYNTHETIC_UNIVERSE)["freshness"] == "CURRENT"


@pytest.mark.parametrize("bad_evidence", ["unverified", "unknown_status", "unknown_uri", "missing_hash", "unknown_kind", "unreviewed", "invalid_listing"])
def test_unknown_exception_evidence_does_not_waive_dropout(bad_evidence):
    conn = current_fixture()
    conn.execute("DELETE FROM price_history WHERE ticker='T015' AND date>'2026-10-09'")
    exception = CoverageException("T015", "suspension", date(2026, 10, 12), date(2026, 10, 13),
                                  "fixture://dated-evidence", "a" * 64, "synthetic reviewer", True)
    changes = {"unverified": dict(verified=False), "unknown_status": dict(verified="UNKNOWN"),
               "unknown_uri": dict(evidence_uri="UNKNOWN"), "missing_hash": dict(evidence_sha256=""),
               "unknown_kind": dict(kind="unknown"), "unreviewed": dict(reviewed_by=""),
               "invalid_listing": dict(kind="pre_listing", listing_date=date(2026, 10, 12))}
    universe = replace(SYNTHETIC_UNIVERSE, exceptions=(replace(exception, **changes[bad_evidence]),))
    with pytest.raises(RuntimeError, match="T015"):
        health.require_current_prices(conn, AS_OF, universe)


@pytest.mark.parametrize("kind", ["suspension", "exclusion", "pre_listing"])
def test_verified_dated_exception_only_waives_its_interval(kind):
    conn = current_fixture()
    conn.execute("DELETE FROM price_history WHERE ticker='T015' AND date='2026-10-12'")
    exception = CoverageException("T015", kind, date(2026, 10, 12), date(2026, 10, 12),
                                  "fixture://dated-evidence", "a" * 64, "synthetic reviewer", True,
                                  date(2026, 10, 13) if kind == "pre_listing" else None)
    universe = replace(SYNTHETIC_UNIVERSE, exceptions=(exception,))
    assert health.require_current_prices(conn, AS_OF, universe)["freshness"] == "CURRENT"
    conn.execute("DELETE FROM price_history WHERE ticker='T015' AND date='2026-10-13'")
    with pytest.raises(RuntimeError, match="T015"):
        health.require_current_prices(conn, AS_OF, universe)


@pytest.mark.parametrize("overlap", [0, 14])
def test_insufficient_comparison_evidence_never_healthy(overlap):
    conn = current_fixture()
    conn.execute("DELETE FROM market_summary_daily WHERE ticker >= ?", (f"T{overlap:03d}",))
    problems, notes, stats = [], [], {}
    csi.check_cross_source(conn, problems, notes, stats)
    assert "CROSS_SOURCE_UNVERIFIED" in categories(stats)
    assert csi.exit_code(problems, stats) != 0


def schema_fixture():
    conn = fixture()
    for k in range(16, 30):
        conn.execute("INSERT INTO market_summary_daily SELECT date,?, ?,close+?,high+?,low+?,"
                     "m_dn_0+?,nr_dn_0+?,f_dn_0+?,m_cn_5+?,m_dn_3+?,top_5_buyer,"
                     "clean_score+?,tval+?,market_cap_t+?,pct_5+?,is_unusual_volume "
                     "FROM market_summary_daily WHERE ticker='T000'",
                     (f"T{k:03d}", f"T{k:03d}", *([k] * 12)))
    for day in ("2026-10-12", "2026-10-13"):
        conn.execute("INSERT INTO market_summary_daily SELECT ?,ticker,symbol,close,high,low,m_dn_0,"
                     "nr_dn_0,f_dn_0,m_cn_5,m_dn_3,top_5_buyer,clean_score,tval,market_cap_t,pct_5,"
                     "is_unusual_volume FROM market_summary_daily WHERE date='2026-10-14'", (day,))
    return conn


def schema_problems(conn):
    problems = []
    csi.check_schema_and_coverage(conn, problems, [], {})
    return problems


def test_type_drift_guard_has_an_isolated_witness():
    conn = schema_fixture()
    assert schema_problems(conn) == []
    conn.execute("UPDATE market_summary_daily SET top_5_buyer=CAST(substr(ticker,2) AS INTEGER) WHERE date='2026-10-14'")
    problems = schema_problems(conn)
    assert len(problems) == 1 and "type change" in problems[0] and "top_5_buyer" in problems[0]


def test_constant_field_collapse_guard_has_an_isolated_witness():
    conn = schema_fixture()
    assert schema_problems(conn) == []
    conn.execute("UPDATE market_summary_daily SET pct_5=7.0 WHERE date='2026-10-14'")
    problems = schema_problems(conn)
    assert len(problems) == 1 and "constant-field collapse" in problems[0] and "pct_5" in problems[0]


def test_missing_signal_close_threshold_has_an_isolated_witness():
    conn = fixture()
    conn.execute("UPDATE market_summary_daily SET close=NULL WHERE ticker<'T004'")
    problems = []
    csi.check_signals_measurable(conn, problems, [], {})
    assert problems == []  # 4/16 is within 30%.
    conn.execute("UPDATE market_summary_daily SET close=NULL WHERE ticker='T004'")
    csi.check_signals_measurable(conn, problems, [], {})
    assert len(problems) == 1 and "past the 30% limit" in problems[0]


def test_fixed_real_universe_matches_the_checked_out_producer_without_importing_secrets():
    import ast
    source = (Path(__file__).resolve().parent / "neobdm_scraper.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    stock_owner = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "STOCK_OWNER" for t in node.targets))
    from ticker_registry import load_registry
    registry = load_registry()
    assert len(EXPECTED_UNIVERSE.tickers) > len(stock_owner)
    assert EXPECTED_UNIVERSE.tickers == registry.tickers
    assert EXPECTED_UNIVERSE.version == registry.version
    assert EXPECTED_UNIVERSE.source == registry.source
    assert EXPECTED_UNIVERSE.exceptions == ()


def test_complete_real_scope_fixture_is_admitted_and_not_written():
    conn = production_scope_fixture()
    before = conn.serialize()
    assert health.require_current_prices(conn, AS_OF)["freshness"] == "CURRENT"
    problems, _, stats = csi.check(conn, AS_OF)
    assert problems == [] and csi.exit_code(problems, stats) == 0
    assert conn.serialize() == before


@pytest.mark.parametrize("fault", ["dropout", "quarantine", "partial", "bad_price", "missing_prior"])
def test_orchestrator_refuses_before_every_report_side_effect(tmp_path, monkeypatch, fault):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(key, "synthetic")
    import run_ml_reports as rmr
    conn = production_scope_fixture()
    ticker = EXPECTED_UNIVERSE.tickers[-1]
    if fault == "dropout":
        conn.execute("DELETE FROM price_history WHERE ticker=? AND date>'2026-10-09'", (ticker,))
    elif fault == "quarantine":
        conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history WHERE date='2026-10-13'")
    elif fault == "partial":
        conn.execute("DELETE FROM price_history WHERE ticker != ? AND date>'2026-10-09'", (ticker,))
    elif fault == "bad_price":
        conn.execute("UPDATE price_history SET close='broken' WHERE ticker=? AND date='2026-10-13'", (ticker,))
    else:
        conn.execute("DELETE FROM price_history WHERE date<'2026-10-13'")
    path = tmp_path / "synthetic.db"
    save_fixture(conn, path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(rmr, "DB_PATH", str(path))
    monkeypatch.setattr(health, "jakarta_today", lambda: AS_OF)
    calls = []
    for name in ("refresh_broker_flow_manifest", "run_xgboost_report", "run_strategy_variants_report",
                 "run_ddqn_report", "run_konglo_watch_report", "send_telegram", "write_step_summary"):
        monkeypatch.setattr(rmr, name, lambda *a, **kw: calls.append("unexpected work"))
    with pytest.raises(RuntimeError, match="Current ML report refused"):
        rmr.main([])
    assert calls == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_reporting_snapshot_is_the_file_all_readers_consume(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic")
    import run_ml_reports as rmr
    path = tmp_path / "synthetic.db"
    save_fixture(production_scope_fixture(), path)
    with rmr.reporting_snapshot(path) as (conn, frozen_path):
        assert Path(frozen_path).read_bytes() == path.read_bytes()
        assert health.require_current_prices(conn, AS_OF)["freshness"] == "CURRENT"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM price_history")
    assert not Path(frozen_path).exists()


def test_orchestrator_consumes_the_admitted_frozen_file_even_if_source_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic")
    import run_ml_reports as rmr
    path = tmp_path / "synthetic.db"
    save_fixture(production_scope_fixture(), path)
    monkeypatch.setattr(rmr, "DB_PATH", str(path))
    monkeypatch.setattr(health, "jakarta_today", lambda: AS_OF)
    paths = []

    def refresh(db_path):
        assert Path(db_path) != path
        paths.append(db_path)
        # A producer can change after admission. Every consumer must retain
        # the image admitted earlier, and the source read lock is released.
        writer = sqlite3.connect(path, timeout=0)
        writer.execute("DELETE FROM price_history WHERE date='2026-10-13'")
        writer.commit()
        writer.close()
        return "synthetic-manifest"

    def report(conn, manifest, db_path):
        assert manifest == "synthetic-manifest"
        assert Path(conn.execute("PRAGMA database_list").fetchone()[2]) == Path(db_path)
        assert health.require_current_prices(conn, AS_OF)["freshness"] == "CURRENT"
        paths.append(db_path)
        return dict(broker_flow={})

    monkeypatch.setattr(rmr, "refresh_broker_flow_manifest", refresh)
    for name in ("run_xgboost_report", "run_strategy_variants_report", "run_ddqn_report"):
        monkeypatch.setattr(rmr, name, report)
    for name in ("run_konglo_watch_report", "send_telegram", "write_step_summary",
                 "format_broker_flow_provenance", "ddqn_snapshot_note", "format_telegram_message"):
        monkeypatch.setattr(rmr, name, lambda *a, **kw: "synthetic")
    rmr.main([])
    assert len(paths) == 4 and len(set(paths)) == 1
    assert not Path(paths[0]).exists()


def test_reporting_snapshot_refuses_an_uncheckpointed_wal_image(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic")
    import run_ml_reports as rmr
    path = tmp_path / "synthetic.db"
    save_fixture(production_scope_fixture(), path)
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("DELETE FROM price_history WHERE date='2026-10-13'")
        writer.commit()
        with pytest.raises(RuntimeError, match="not the source SQLite snapshot"):
            with rmr.reporting_snapshot(path):
                pytest.fail("an incomplete main-file copy was admitted")
    finally:
        writer.close()


# Final review regressions use the versioned full registry membership.
def test_n1_historical_calendar_does_not_hide_current_coverage():
    conn = production_scope_fixture()
    add_price(conn, "2024-01-02", "BBHI")
    conn.execute("DELETE FROM price_history WHERE date>'2026-10-09'")
    item = health.table_freshness(conn, "price_history", AS_OF, {"price-history-topup.yml": "disabled_manually"})
    assert item["freshness"] == "SOURCE_STALE"
    assert item["historical_calendar_status"] == "UNKNOWN/UNSUPPORTED"
    assert item["coverage"]["2026-10-13"]["present"] == 0
    assert item["coverage"]["2026-10-13"]["required"] == len(EXPECTED_UNIVERSE.tickers)
    assert item["usable_latest"] == "2026-10-09" and item["lag"] == 2
    assert set(item["session_lag_by_ticker"].values()) == {2}
    with pytest.raises(health.AdmissionRefused) as refused:
        health.require_current_prices(conn, AS_OF)
    assert refused.value.evidence["missing_coverage"]
    problems, notes, stats = csi.check(conn, AS_OF)
    assert csi.exit_code(problems, stats) == 1
    assert {"INCOMPLETE_COVERAGE", "SOURCE_STALE", "CALENDAR_UNSUPPORTED"} <= categories(stats)
    assert "None session" not in csi.format_report(problems, notes, stats)


def test_n1_current_admission_separates_historical_certification():
    conn = production_scope_fixture()
    add_price(conn, "2024-01-02", "BBHI")
    item = health.require_current_prices(conn, AS_OF)
    assert item["freshness"] == "CURRENT"
    assert item["unsupported_dates"] == ["2024-01-02"]
    assert item["historical_calendar_status"] == "UNKNOWN/UNSUPPORTED"
    assert item["source_certification"] == "UNVERIFIED" and item["spectra_ingestion"] == "BLOCKED"
    assert item["universe_sha256"] == load_registry().sha256


def test_n1_unsupported_ticker_lag_is_unknown_without_hiding_other_tickers():
    conn = production_scope_fixture()
    conn.execute("DELETE FROM price_history WHERE ticker='BBHI'")
    add_price(conn, "2024-01-02", "BBHI")
    item = health.table_freshness(conn, "price_history", AS_OF)
    assert item["session_lag_by_ticker"]["BBHI"] is None
    assert item["session_lag_by_ticker"]["BNBR"] == 0
    assert item["freshness"] == "INCOMPLETE_COVERAGE"


@pytest.mark.parametrize("case,status", [("empty", "EMPTY"), ("columns", "INVALID_DATA"), ("quarantined", "INCOMPLETE_COVERAGE")])
def test_m1_schema_empty_and_quarantine_are_separate_failures(case, status):
    from price_audit import load_clean
    conn = production_scope_fixture()
    if case == "empty":
        conn.execute("DELETE FROM price_history")
        assert set(load_clean(conn).columns) >= {"date", "ticker", *csi.OHLCV}
    elif case == "columns":
        conn.execute("ALTER TABLE price_history RENAME COLUMN close TO absent_close")
    else:
        conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history")
    with pytest.raises(health.AdmissionRefused) as refused:
        health.require_current_prices(conn, AS_OF)
    assert refused.value.evidence["freshness"] == status
    problems, notes, stats = csi.check(conn, AS_OF)
    assert csi.exit_code(problems, stats) == 1
    assert stats["freshness"]["price_history"]["freshness"] == status
    assert "None session" not in csi.format_report(problems, notes, stats)


def test_m1_empty_monitor_reaches_failure_notification(tmp_path, monkeypatch):
    conn = production_scope_fixture()
    conn.execute("DELETE FROM price_history")
    path = tmp_path / "empty.db"
    save_fixture(conn, path)
    sent = []
    monkeypatch.setattr(csi, "send_telegram", sent.append)
    monkeypatch.setattr(csi, "_load_dotenv", lambda: None)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit) as stopped:
        csi.main(["--db", str(path), "--as-of", str(AS_OF), "--telegram"])
    assert stopped.value.code == 1
    assert len(sent) == 1 and "EMPTY" in sent[0]


@pytest.mark.parametrize("session", ["2026-10-12", "2026-10-13"])
def test_m2_clone_with_stale_quarantine_shared_evidence(session):
    conn = production_scope_fixture()
    second = EXPECTED_UNIVERSE.tickers[1]
    conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history WHERE date='2026-10-09'")
    if session == "2026-10-12":
        conn.execute("INSERT INTO market_summary_daily SELECT '2026-10-13',ticker,symbol,close,high,low,m_dn_0,nr_dn_0,f_dn_0,m_cn_5,m_dn_3,top_5_buyer,clean_score,tval,market_cap_t,pct_5,is_unusual_volume FROM market_summary_daily")
    conn.execute("UPDATE price_history SET open=100,high=101,low=99,close=100,volume=1000 WHERE ticker=? AND date=?", (second, session,))
    with pytest.raises(health.AdmissionRefused) as refused:
        health.require_current_prices(conn, AS_OF)
    proof = refused.value.evidence["collisions"][0]
    assert proof["classification"] == "CORRUPTION_PRESENT"
    assert proof["conflicting_closes"] == {second: [101.0]}
    problems, _, stats = csi.check(conn, AS_OF)
    assert csi.exit_code(problems, stats) == 1
    assert any(f.get("evidence") == proof for f in stats["findings"] if f["category"] == "CORRUPTION_PRESENT")


def test_m2_equal_prices_with_distinct_volumes_are_admissible():
    conn = production_scope_fixture()
    conn.execute("UPDATE price_history SET open=100,high=101,low=99,close=100 WHERE ticker='BNBR'")
    assert health.require_current_prices(conn, AS_OF)["freshness"] == "CURRENT"


@pytest.mark.parametrize("volume", [0, 1000])
def test_m2_unproven_whole_bar_equality_requires_review(volume):
    conn = production_scope_fixture()
    conn.execute("DELETE FROM market_summary_daily WHERE ticker IN ('BBHI','BNBR')")
    conn.execute("UPDATE price_history SET open=100,high=101,low=99,close=100,volume=? WHERE ticker IN ('BBHI','BNBR') AND date='2026-10-13'", (volume,))
    with pytest.raises(health.AdmissionRefused) as refused:
        health.require_current_prices(conn, AS_OF)
    assert refused.value.evidence["freshness"] == "REVIEW_REQUIRED"
    problems, notes, stats = csi.check(conn, AS_OF)
    assert "REVIEW_REQUIRED" in categories(stats)
    assert "CORRUPTION_PRESENT" not in categories(stats)


@pytest.mark.parametrize("table,column", [("broker_flow", "date"), ("market_summary_daily", "date"), ("konglo_signal_watch", "flag_date"), ("signal_source_status", "flag_date")])
@pytest.mark.parametrize("fault", ["stale", "missing", "empty"])
def test_m3_each_required_source_fails_closed(table, column, fault):
    conn = production_scope_fixture()
    if fault == "missing":
        conn.execute(f"DROP TABLE {table}")
    elif fault == "empty":
        conn.execute(f"DELETE FROM {table}")
    else:
        conn.execute(f"UPDATE {table} SET {column}='2026-10-01'")
    assert health.require_current_prices(conn, AS_OF)["freshness"] == "CURRENT"
    with pytest.raises(health.AdmissionRefused) as refused:
        health.report_admission(conn, AS_OF)
    evidence = refused.value.evidence
    assert evidence["admitted"] is False
    assert any(r.startswith(table+":") for r in evidence["rejection_reasons"])
    item = evidence["datasets"][table]
    assert item["required"] and item["owner"] and item["expected"] == str(AS_OF)
    problems, _, stats = csi.check(conn, AS_OF)
    assert csi.exit_code(problems, stats) == 1
    assert stats["report_admission"]["rejection_reasons"] == evidence["rejection_reasons"]


@pytest.mark.parametrize("state", ["SOURCE_UNAVAILABLE", "EMPTY_UNVERIFIED", "UNKNOWN", "RETIRED_SOURCE"])
def test_m3_unavailable_konglo_family_is_never_a_zero_signal_day(state):
    conn = production_scope_fixture()
    conn.execute("UPDATE signal_source_status SET status=? WHERE source='dashboard_NonRetail'", (state,))
    with pytest.raises(health.AdmissionRefused) as refused:
        health.report_admission(conn, AS_OF)
    assert refused.value.evidence["datasets"]["signal_source_status"]["unavailable_sources"]["dashboard_NonRetail"] == state


def test_m3_component_requirements_are_explicit_and_snapshot_is_recorded():
    conn = production_scope_fixture()
    item = health.report_admission(conn, AS_OF)
    assert item["admitted"] and item["optional_exclusions"] == []
    assert item["snapshot_sha256"] == hashlib.sha256(conn.serialize()).hexdigest()
    assert set(item["datasets"]) == {"price_history", "broker_flow", "market_summary_daily", "konglo_signal_watch", "signal_source_status"}
    for table in ("market_summary_daily", "konglo_signal_watch", "signal_source_status"):
        conn.execute(f"DROP TABLE {table}")
    assert health.report_admission(conn, AS_OF, components=("xgboost",))["admitted"]


def test_m3_stale_inputs_stop_orchestrator_before_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic")
    import run_ml_reports as rmr
    conn = production_scope_fixture()
    conn.execute("UPDATE broker_flow SET date='2026-10-01'")
    path = tmp_path / "stale.db"
    save_fixture(conn, path)
    monkeypatch.setattr(rmr, "DB_PATH", str(path))
    monkeypatch.setattr(health, "jakarta_today", lambda: AS_OF)
    calls = []
    for name in ("refresh_broker_flow_manifest", "run_xgboost_report", "run_strategy_variants_report", "run_ddqn_report", "run_konglo_watch_report", "send_telegram", "write_step_summary"):
        monkeypatch.setattr(rmr, name, lambda *a, **kw: calls.append("unexpected work"))
    with pytest.raises(health.AdmissionRefused):
        rmr.main([])
    assert calls == []


def test_m4_health_runner_collects_sig1_with_pytest(monkeypatch):
    import check_ml_health as runner
    from types import SimpleNamespace
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="123 passed" if "pytest" in command else "", stderr="" if "pytest" in command else "Ran 1 test in 0.01s")
    monkeypatch.setattr(runner.subprocess, "run", run)
    problems, stats = [], {}
    runner.check_unit_tests(problems, stats)
    command = next(c for c in calls if "pytest" in c)
    assert command[1:3] == ["-m", "pytest"]
    assert "test_sig1_corrective.py" in command and "test_signal_integrity.py" in command
    assert stats["tests_passed"] >= 123 and problems == []


def test_readonly_wal_source_creates_no_sidecars(tmp_path, monkeypatch):
    import run_ml_reports as rmr
    path = tmp_path / "wal.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE witness(x)")
    writer.commit()
    writer.close()
    assert not Path(str(path)+"-wal").exists()
    with rmr.reporting_snapshot(path):
        assert not Path(str(path)+"-wal").exists()
        assert not Path(str(path)+"-shm").exists()


@pytest.mark.parametrize("case", ["empty", "columns", "quarantined"])
def test_m1_orchestrator_refuses_without_report_or_success_notification(tmp_path, monkeypatch, case, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic")
    import run_ml_reports as reports
    conn = production_scope_fixture()
    if case == "empty":
        conn.execute("DELETE FROM price_history")
    elif case == "columns":
        conn.execute("ALTER TABLE price_history RENAME COLUMN date TO absent_date")
    else:
        conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history")
    path = tmp_path / "refused.db"
    save_fixture(conn, path)
    monkeypatch.setattr(reports, "DB_PATH", str(path))
    monkeypatch.setattr(health, "jakarta_today", lambda: AS_OF)
    calls = []
    for name in ("refresh_broker_flow_manifest", "run_xgboost_report", "run_strategy_variants_report", "run_ddqn_report", "run_konglo_watch_report", "send_telegram", "write_step_summary"):
        monkeypatch.setattr(reports, name, lambda *a, **kw: calls.append("unexpected work"))
    with pytest.raises(health.AdmissionRefused) as refused:
        reports.main([])
    assert calls == []
    import json
    line = capsys.readouterr().out.strip().removeprefix("Current report input admission: ")
    assert json.loads(line) == json.loads(json.dumps(refused.value.evidence))


@pytest.mark.parametrize("empty_watch", [False, True])
def test_m3_verified_zero_hit_capture_can_leave_event_ledger_unchanged(empty_watch):
    conn = production_scope_fixture()
    conn.execute("UPDATE signal_source_status SET status='NO_HITS' WHERE source!='top_akum_bandar'")
    conn.execute("DELETE FROM konglo_signal_watch" if empty_watch else "UPDATE konglo_signal_watch SET flag_date='2026-10-13'")
    record = health.report_admission(conn, AS_OF)
    watch = record["datasets"]["konglo_signal_watch"]
    assert record["admitted"] and watch["observation_status"] == "VERIFIED_NO_HITS"
    assert watch["latest"] == (None if empty_watch else "2026-10-13")
    conn.execute("UPDATE signal_source_status SET flag_date='2026-10-13'")
    with pytest.raises(health.AdmissionRefused):
        health.report_admission(conn, AS_OF)



def test_report_import_does_not_require_torch():
    import os
    import subprocess
    import sys
    code = """import sys, importlib.abc
class NoTorch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'torch' or fullname.startswith('torch.'):
            raise ModuleNotFoundError('Torch deliberately absent in ML Health CI')
sys.meta_path.insert(0, NoTorch())
import run_ml_reports
assert 'torch' not in sys.modules
print('admission import is torch-free')
"""
    env = dict(os.environ, TELEGRAM_BOT_TOKEN="synthetic", TELEGRAM_CHAT_ID="synthetic")
    run = subprocess.run([sys.executable, "-c", code], env=env, cwd=Path(__file__).parent, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "admission import is torch-free" in run.stdout


def test_full_registry_missing_summary_preserves_complete_evidence():
    conn = production_scope_fixture()
    conn.execute("DELETE FROM price_history WHERE date > '2026-10-09'")
    problems, _, stats = csi.check(conn, AS_OF)
    detail = next(f["detail"] for f in stats["findings"] if f["category"] == "INCOMPLETE_COVERAGE")
    assert len(detail) < 1500
    assert len(stats["freshness"]["price_history"]["missing_coverage"]["2026-10-13"]) == len(EXPECTED_UNIVERSE.tickers)
    assert csi.exit_code(problems, stats) == 1


def test_unicode_notification_is_bounded_without_hiding_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    original = "SIGNAL INTEGRITY FAILED\n" + "\U0001f534" * 5000
    message = csi.notification_text(original)
    assert len(message.encode("utf-16-le")) <= 8192
    assert message.startswith("SIGNAL INTEGRITY FAILED")
    assert "https://github.com/example/project/actions/runs/123" in message


def test_cli_full_registry_json_out_preserves_refusal(tmp_path, monkeypatch):
    conn = production_scope_fixture()
    conn.execute("DELETE FROM price_history WHERE date > '2026-10-09'")
    source = tmp_path / "source.db"
    target = tmp_path / "integrity.json"
    save_fixture(conn, source)
    monkeypatch.setattr(csi, "_load_dotenv", lambda: None)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit) as stopped:
        csi.main(["--db", str(source), "--as-of", str(AS_OF), "--json-out", str(target)])
    evidence = __import__("json").loads(target.read_text(encoding="utf-8"))
    assert stopped.value.code == evidence["exit_code"] == 1
    missing = evidence["stats"]["freshness"]["price_history"]["missing_coverage"]["2026-10-13"]
    assert len(missing) == len(EXPECTED_UNIVERSE.tickers)

