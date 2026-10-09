"""Tests for broker_learning_run.py, the orchestrator (BROKER_LEARNING.md §1, §7).

    py test_broker_learning_run.py

Offline only: every run here is `--no-fetch` over a temporary legacy-layout
cache built from inventory_raw/ (skipped when that cache is absent), with a
temporary DB and history folder, so nothing touches broker_learning.db, the
network or Telegram. These pin the orchestrator's gates and filters, which
the per-module tests cannot see: what counts as a failed ticker, when a
weekly run refuses to write, which books enter broker_profitability, and
which daily invocations may write the committed live ledger.
"""

import gzip
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import redirect_stderr

import broker_learning_run as run

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "inventory_raw")
FIELDS = ("blot", "bval", "slot", "sval", "nlot", "nval")
SKIPPED = []


def _skip(name, why):
    SKIPPED.append(f"{name}: {why}")
    print(f"  skip {name}: {why}")


def _legacy(ticker):
    path = os.path.join(RAW, f"{ticker}.json.gz")
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _truncate(data, cut):
    """The payload without its last `cut` sessions: a ticker that stopped trading."""
    k = len(data["date"]) - cut
    out = dict(data, date=data["date"][:k], ohlc=data["ohlc"][:k])
    for f in FIELDS:
        out[f] = {b: s[:k] for b, s in data[f].items()}
    return out


def _write(folder, ticker, data=None, raw=None):
    path = os.path.join(folder, f"{ticker}.json.gz")
    if raw is not None:
        with open(path, "wb") as fh:
            fh.write(raw)
        return
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)


def _weekly(raw_dir, tmp):
    """The financial CLI refuses before cache access, dispatch or persistence."""
    from price_contract import CONTRACT_VERSION, UnsupportedPriceContract
    from unittest.mock import patch
    db = os.path.join(tmp, "bl.db")
    history = os.path.join(tmp, "hist")
    argv = ["weekly", "--no-fetch", "--legacy-cache", "--raw-dir", raw_dir,
            "--db", db, "--history-dir", history]
    before = sorted(os.listdir(raw_dir))
    with patch.object(run, "parse_args", side_effect=AssertionError("refusal must precede dispatch")), \
            patch("builtins.open", side_effect=AssertionError("refusal must precede file IO")):
        try:
            run.main(argv)
        except UnsupportedPriceContract as exc:
            assert exc.consumer == "broker_learning_run.main"
            assert exc.status == "UNSUPPORTED" and exc.contract_version == CONTRACT_VERSION
        else:
            raise AssertionError("uncertified weekly output or zero/stale fallback returned")
    assert sorted(os.listdir(raw_dir)) == before
    assert not os.path.exists(db) and not os.path.exists(history)
    assert sorted(os.listdir(tmp)) == ["raw"]


def test_daily_no_fetch_needs_dry_run_or_db():
    """Review finding: `daily --no-fetch` alone wrote cached, weeks-old
    sessions into the committed insert-only live ledger as "prospective"."""
    with redirect_stderr(io.StringIO()) as err:
        try:
            run.parse_args(["daily", "--no-fetch"])
            raise AssertionError("daily --no-fetch without --dry-run/--db was accepted")
        except SystemExit as e:
            assert e.code == 2
    assert "committed live ledger" in err.getvalue()
    assert run.parse_args(["daily", "--no-fetch", "--dry-run"]).no_fetch
    assert run.parse_args(["daily", "--no-fetch", "--db", "x.db"]).db == "x.db"
    assert not run.parse_args(["daily"]).no_fetch             # the CI path is unchanged
    assert run.parse_args(["weekly", "--no-fetch"]).no_fetch  # weekly is retrospective


def test_weekly_counts_unreadable_cache_files_as_failed():
    """Review finding: without --tickers, corrupt cache files were skipped
    and never counted, so 2 unreadable of 4 ran as ('ok', 2, 0)."""
    sini, raja = _legacy("SINI"), _legacy("RAJA")
    if sini is None or raja is None:
        return _skip("unreadable cache", "inventory_raw/ cache not present")
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw")
        os.makedirs(raw)
        _write(raw, "SINI", sini)
        _write(raw, "RAJA", raja)
        _write(raw, "AAAA", raw=b"not gzip at all")
        _write(raw, "BBBB", raw=gzip.compress(b"{truncated json"))
        _weekly(raw, tmp)
        unreadable = {}
        cached = dict(run.bc.iter_cached(None, run.bc.MODE_MARKET, raw_dir=raw,
                                         legacy=True, unreadable=unreadable))
        assert sorted(cached) == ["RAJA", "SINI"]
        assert sorted(unreadable) == ["AAAA", "BBBB"]
        assert all(v.startswith("unreadable cache") for v in unreadable.values())
        assert run.should_fail_run(len(unreadable), len(cached) + len(unreadable))


def test_weekly_fails_when_mostly_empty():
    """Review finding: zero-session answers had no ceiling. 1 real ticker and
    3 empty ones is a broken fetch, not a quiet week: no weekly table."""
    sini = _legacy("SINI")
    if sini is None:
        return _skip("mostly empty", "inventory_raw/ cache not present")
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw")
        os.makedirs(raw)
        _write(raw, "SINI", sini)
        for t in ("DEDA", "DEDB", "DEDC"):
            _write(raw, t, {"date": [], "ohlc": []})
        _weekly(raw, tmp)
        empty, nonempty = {}, []
        cached = list(run.bc.iter_cached(None, run.bc.MODE_MARKET, raw_dir=raw, legacy=True))
        for ticker, payload in cached:
            if not run._empty_payload(ticker, payload, empty):
                nonempty.append(ticker)
        assert nonempty == ["SINI"]
        assert sorted(empty) == ["DEDA", "DEDB", "DEDC"]
        assert len(empty) == 3 and run.bc.too_many_empty(len(empty), len(cached))


def test_profitability_leaves_out_books_that_end_before_as_of():
    """Review finding: a ticker whose data stopped months before as_of was
    marked at that old close inside a "mark-to-market" P/L. Only books whose
    data reaches as_of count; the note says how many were left out. The same
    run's note carries §4.5's net trade stats for the buy rules."""
    sini, bren = _legacy("SINI"), _legacy("BREN")
    if sini is None or bren is None:
        return _skip("stale books", "inventory_raw/ cache not present")
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw")
        os.makedirs(raw)
        _write(raw, "SINI", sini)
        _write(raw, "BREN", _truncate(bren, 10))     # eligible at its own last row
        _weekly(raw, tmp)
        # Test the supported date filter with opaque book identities, not P/L.
        acc = run._Weekly()
        fresh, stale = object(), object()
        acc.books = {"SINI": (sini["date"][-1], fresh),
                     "BREN": (_truncate(bren, 10)["date"][-1], stale)}
        as_of = max(last for last, _ in acc.books.values())
        assert acc.fresh_books(as_of) == {"SINI": fresh}
        assert len(acc.books) - len(acc.fresh_books(as_of)) == 1
        assert acc.fresh_books("2099-01-01") == {}, "no stale book fallback"


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    print(f"broker learning run: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"\nAll {len(ALL)} tests passed."
          + (f" ({len(SKIPPED)} skipped: {'; '.join(SKIPPED)})" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
