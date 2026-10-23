"""Durable, sanitized ownership coverage; denominator never follows responses."""
import json
from pathlib import Path
import re
from daily_capture_receipt import write_atomic
from ticker_registry import load_registry


def open_receipt(path, run_id, selected, dry_run=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', run_id) or run_id in ('.', '..'):
        raise ValueError('Invalid run identity')
    registry = load_registry()
    if len(selected) != len(set(selected)) or not set(selected) <= set(registry.tickers):
        raise ValueError('Selection must contain unique registry members')
    if Path(path).exists():
        receipt = json.loads(Path(path).read_text())
        if (receipt['run_id'] != run_id or receipt['registry']['sha256'] != registry.sha256 or
                set(receipt['per_ticker']) != set(registry.tickers) or receipt['dry_run'] != dry_run):
            raise ValueError('Receipt identity/registry mismatch')
        return receipt
    return dict(schema='spectra-ownership-coverage.v1', run_id=run_id,
        registry=registry.metadata(), dry_run=dry_run, complete=False,
        per_ticker={t: {'status': 'NOT_ATTEMPTED'} for t in registry.tickers})


def save_receipt(path, receipt, checkpoint=None):
    counts = {}
    for item in receipt['per_ticker'].values():
        counts[item['status']] = counts.get(item['status'], 0) + 1
    receipt['counts'] = counts
    receipt['complete'] = (not receipt['dry_run'] and
        counts.get('PERSISTED', 0) == receipt['registry']['count'])
    receipt['eligible_scope_complete'] = all(item['status'] == 'PERSISTED' and item.get('content_status') == 'OBSERVED'
        for ticker, item in receipt['per_ticker'].items() if ticker not in ('IHSG', 'ISSI'))
    receipt['history_certification'] = 'NOT_CERTIFIED'
    write_atomic(path, receipt)
    if checkpoint:
        checkpoint()


def pending_batch(receipt, selected, limit):
    if limit < 0:
        raise ValueError('Batch limit must not be negative')
    pending = [t for t in selected if receipt['per_ticker'][t]['status'] not in ('PERSISTED', 'SOURCE_UNAVAILABLE')]
    # Retry failed members after never-attempted members, preventing starvation.
    pending.sort(key=lambda t: receipt['per_ticker'][t]['status'] != 'NOT_ATTEMPTED')
    return pending[:limit] if limit else pending
