import json
from pathlib import Path
from ownership_receipt import open_receipt, save_receipt, pending_batch
from ownership_auth import legacy_tickers
import pytest

def test_subset_never_shrinks_registry_and_failed_first_does_not_starve(tmp_path):
    receipt = open_receipt(tmp_path/'receipt.json', '2026-10-23', ['BMRI'])
    assert len(receipt['per_ticker']) == 1042
    receipt['per_ticker']['AADI']['status'] = 'FAILED'
    receipt['per_ticker']['IHSG'] = {'status': 'SOURCE_UNAVAILABLE'}
    assert pending_batch(receipt, ['AADI', 'AALI', 'IHSG'], 1) == ['AALI']
    save_receipt(tmp_path/'receipt.json', receipt)
    assert not receipt['complete'] and not receipt['eligible_scope_complete']
    assert receipt['counts']['NOT_ATTEMPTED'] == 1040

def test_wrong_identity_and_outside_registry_refused(tmp_path):
    with pytest.raises(ValueError):
        open_receipt(tmp_path/'a', '../run', ['BMRI'])
    with pytest.raises(ValueError):
        open_receipt(tmp_path/'a', 'run', ['WRONG'])
    receipt = open_receipt(tmp_path/'a', 'run', ['BMRI'])
    save_receipt(tmp_path/'a', receipt)
    with pytest.raises(ValueError):
        open_receipt(tmp_path/'a', 'other', ['BMRI'])

def test_empty_content_and_dryrun_cannot_certify_ownership(tmp_path):
    r = open_receipt(tmp_path/'a', 'run', ['BMRI'], dry_run=True)
    for item in r['per_ticker'].values():
        item.update(status='DRY_RUN', content_status='OBSERVED')
    save_receipt(tmp_path/'a', r)
    assert not r['complete'] and not r['eligible_scope_complete']
    assert r['history_certification'] == 'NOT_CERTIFIED'

def test_legacy_config_can_be_read_without_importing_credentials():
    import sys
    assert len(legacy_tickers()) == 45
    assert 'neobdm_scraper' not in sys.modules
