"""Offline witnesses: durable receipts gate swallowed collector failures."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
import daily_capture_receipt as receipt

for key in ("NEOBDM_USERNAME", "NEOBDM_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.setdefault(key, "test-placeholder")
import neobdm_scraper as scraper
import morning


def fixture_db(path, start, failed=()):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ms_capture_manifest (capture_started_utc TEXT, capture_finished_utc TEXT, capture_date TEXT, contract_status TEXT, persisted_to_market_summary_daily INTEGER, rows INTEGER, pages_fetched INTEGER)")
    conn.execute("INSERT INTO ms_capture_manifest VALUES (?,?,?,?,?,?,?)", (start.isoformat(), start.isoformat(), start.date().isoformat(), "OK", 1, 246, 13))
    scraper._ensure_broker_flow_tables(conn)
    capture = scraper.capture_broker_flow(None, [], scraper.BROKER_FLOW_CAPTURE_CODES)
    assert not failed or capture["reason"]
    if failed:
        conn.execute("INSERT INTO broker_flow (date,ticker,broker_code,bval,sval,netval,bavg,savg) VALUES (?,?,?,?,?,?,?,?)", (capture['scrape_date'], 'TEST', 'YU', 42, 0, 42, 1, 0))
        conn.commit()
    before = conn.execute("SELECT * FROM broker_flow").fetchall()
    assert scraper.persist_broker_flow_capture(conn, capture) is (not bool(failed))
    if failed:
        assert conn.execute("SELECT * FROM broker_flow").fetchall() == before
    conn.close()
    return capture


def mock_reader(failed):
    def read(page, code):
        if code in failed:
            raise ValueError("broker-akum-stalker.children is not a list TOPSECRET")
        session, _ = scraper.expected_source_session(datetime.now().date())
        return dict(session_date=session, akum=[], dist=[])
    return read


def run_receipt(tmp_path, failed=()):
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    db = tmp_path / 'capture.db'
    with patch.object(scraper, 'read_broker_flow_code', mock_reader(failed)):
        fixture_db(db, start, failed)
    result = receipt.build_receipt(db, start.isoformat(), scraper.BROKER_FLOW_CAPTURE_CODES, '39877919031', '1')
    return result, db, start


def test_actual_yu_zp_abort_is_not_green_and_snapshot_is_unchanged(tmp_path):
    result, db, start = run_receipt(tmp_path, ('YU', 'ZP'))
    assert result['sources']['market_summary']['complete']
    assert not result['complete']
    flow = result['sources']['broker_flow']
    assert flow['brokers']['YU']['source_status'] == 'SOURCE_FAILURE'
    assert flow['brokers']['ZP']['source_status'] == 'SOURCE_FAILURE'
    assert all(b['snapshot'] == 'REJECTED' for b in flow['brokers'].values())
    assert 'TOPSECRET' not in json.dumps(result)
    out = tmp_path / 'receipt.json'
    receipt.write_atomic(out, result)
    args = ['--output', str(out), '--not-before', start.isoformat(), '--run-id', '39877919031', '--run-attempt', '1', '--check']
    assert receipt.main(args) == 1
    # Legacy reporter still returns zero after the actual atomic capture rejected.
    fake_picks = types.SimpleNamespace(telegram_sender_from_env=lambda: None,
        run_morning=lambda *args, **kwargs: ('sent', 'ok'))
    morning_db = tmp_path / 'morning.db'
    def failed_save(page):
        fixture_db(morning_db, start, ('YU', 'ZP'))
        return None
    # Exercise the real run_all_jobs orchestration through morning.main.
    # Only external browser/network/report inputs are replaced.
    with patch.object(scraper, 'sync_playwright', MagicMock()), \
         patch.object(scraper, 'login', lambda page: None), \
         patch.object(scraper, 'scrape_market_summary', lambda *args: []), \
         patch.object(scraper, 'scrape_dashboard_presets', lambda *args: {}), \
         patch.object(scraper, 'scrape_broker_stalker', lambda *args: {}), \
         patch.object(scraper, 'save_daily_broker_flow', failed_save), \
         patch.object(scraper, 'read_broker_flow_code', mock_reader(('YU', 'ZP'))), \
         patch.object(scraper, '_offset_safe', lambda *args: False), \
         patch.object(scraper, 'format_combined_message', lambda *args: 'daily report'), \
         patch.dict(sys.modules, daily_picks=fake_picks):
        assert morning.main() == 0
    assert morning_db.exists()
    actual = receipt.build_receipt(morning_db, start.isoformat(), scraper.BROKER_FLOW_CAPTURE_CODES, 'run', '1')
    assert not actual['complete']


def test_healthy_capture_and_exact_run_identity(tmp_path):
    result, db, start = run_receipt(tmp_path)
    assert result['complete']
    assert receipt.check_receipt(result, start.isoformat(), '39877919031', '1')
    assert not receipt.check_receipt(result, start.isoformat(), '39877919032', '1')
    assert not receipt.check_receipt(result, start.isoformat(), '39877919031', '2')
    assert not receipt.check_receipt(result, (start+timedelta(seconds=1)).isoformat(), '39877919031', '1')
    stale = receipt.build_receipt(db, (datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(), scraper.BROKER_FLOW_CAPTURE_CODES, 'new', '1')
    assert not stale['complete']


def test_missing_database_does_not_create_database(tmp_path):
    path = tmp_path / 'absent.db'
    result = receipt.build_receipt(path, datetime.now(timezone.utc).isoformat(), ['YU'], '1', '1')
    assert not result['complete'] and not path.exists()


def test_workflow_gates_after_persistence_and_uploads_only_receipt():
    workflow = Path('.github/workflows/daily-scrape.yml').read_text()
    assert workflow.index('Commit updated neobdm.db') < workflow.index('Require complete current-run')
    assert 'path: artifacts/daily-capture-receipt.json' in workflow
    assert '--run-attempt "${{ github.run_attempt }}"' in workflow
