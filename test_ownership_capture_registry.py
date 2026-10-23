import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import ownership_capture as oc
from ownership_auth import SourcePaused

def test_auth_throttle_stops_before_dom_or_json():
    page = SimpleNamespace(goto=lambda *a, **k: SimpleNamespace(status=429))
    with pytest.raises(SourcePaused, match='429'):
        oc.capture_ticker(page, None, '', 'BMRI')
    req = SimpleNamespace(post=lambda *a, **k: SimpleNamespace(status=403))
    with pytest.raises(SourcePaused, match='403'):
        oc.fetch_balance_position_full(req, '', 'BMRI')

def test_single_capture_committed_receipt_and_resume_without_new_login(tmp_path, monkeypatch):
    raw = tmp_path / 'ownership_raw'
    monkeypatch.setattr(oc, 'ARCHIVE_ROOT', str(tmp_path))
    monkeypatch.setattr(oc, 'RAW_DIR', str(raw))
    monkeypatch.setattr(oc, 'login', lambda page: None)
    calls = []
    monkeypatch.setattr(oc, 'capture_ticker', lambda *a: calls.append(a[-1]) or dict(
        ticker=a[-1], panes={'insider-current': 'original pane'}, balance_position_traces=[]))
    monkeypatch.setattr(oc.ing, 'ingest_capture', lambda *a, **k: {'ownership_snapshot': 1})
    ctx = SimpleNamespace(new_page=lambda: object(), request=object(), cookies=lambda: [])
    browser = SimpleNamespace(new_context=lambda **k: ctx, close=lambda: None)
    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **k: browser))
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(oc, 'sync_playwright', Playwright)
    args = ['--tickers','BMRI','--limit','1','--run-id','run','--db',str(tmp_path/'test.db')]
    assert oc.main(args) == 0
    receipt = json.loads((tmp_path/'ownership_receipts/run.json').read_text())
    item = receipt['per_ticker']['BMRI']
    assert len(receipt['per_ticker']) == 1042 and item['status'] == 'PERSISTED'
    assert (tmp_path/item['artifact']['path']).is_file()
    assert not receipt['complete'] and not receipt['eligible_scope_complete']
    monkeypatch.setattr(oc, 'sync_playwright', lambda: (_ for _ in ()).throw(AssertionError('repeat login')))
    assert oc.main(args) == 0
    assert calls == ['BMRI']
