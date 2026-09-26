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
    """weekly --no-fetch over every file in raw_dir; (exit code, runs row, note)."""
    db = os.path.join(tmp, "bl.db")
    argv = ["weekly", "--no-fetch", "--legacy-cache", "--raw-dir", raw_dir, "--db", db,
            "--history-dir", os.path.join(tmp, "hist")]
    logging.disable(logging.CRITICAL)
    try:
        code = run.main(argv)
    finally:
        logging.disable(logging.NOTSET)
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT status, tickers_ok, tickers_fail, note FROM runs "
                           "WHERE kind = 'weekly'").fetchone()
        n_prof = conn.execute("SELECT COUNT(*) FROM broker_profitability").fetchone()[0]
    finally:
        conn.close()
    return code, row, json.loads(row[3]), n_prof


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
        code, row, note, _ = _weekly(raw, tmp)
    assert code == 1 and row[:3] == ("failed", 2, 2), row[:3]
    assert sorted(note["failed"]) == ["AAAA", "BBBB"], note
    assert all(v.startswith("unreadable cache") for v in note["failed"].values()), note


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
        code, row, note, n_prof = _weekly(raw, tmp)
    assert code == 1 and row[0] == "failed" and n_prof == 0, (code, row[:3])
    assert "over the empty limit" in note["reason"] and note["n_empty"] == 3, note


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
        code, row, note, n_prof = _weekly(raw, tmp)
        conn = sqlite3.connect(os.path.join(tmp, "bl.db"))
        top = conn.execute("SELECT MAX(n_tickers) FROM broker_profitability").fetchone()[0]
        conn.close()
    assert code == 0 and row[0] == "ok", row[:3]
    assert (note["n_books"], note["n_books_stale"]) == (1, 1), note
    assert n_prof > 0 and top == 1, "only SINI's book is in the table"
    net = note["net_trade_stats"]
    assert sorted(net) == ["R1", "R4", "R5", "R6"], "dir = +1 rules only"
    assert all("n_trades" in v and "base_rate" in v for v in net.values())


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
