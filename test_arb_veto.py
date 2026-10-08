"""Plain-script tests for arb_veto.write() (run by check_ml_health.py in CI,
or directly: py -3 test_arb_veto.py).

Unversioned analytical lists must refuse before opening or mutating a database.
Existing session lists, probabilities, ranks and timestamps remain unchanged,
including malformed, duplicate and empty replacement attempts.

Synthetic databases only; no panel, no model fit.
"""

import math
import os
import sqlite3
import sys
import tempfile
from contextlib import closing

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "42")

import pandas as pd

import arb_veto as av
import daily_picks as dp
from price_contract import CONTRACT_VERSION, UnsupportedPriceContract

AS_OF = pd.Timestamp("2026-09-23")
DAY = "2026-09-23"
VALID_UNTIL = "2026-09-30"      # AS_OF + VALID_DAYS
SESSION = "2026-09-24"          # a session the AS_OF list covers


# ── helpers ────────────────────────────────────────────────────────────────

def veto_list(*names):
    """What score() hands write(): [(ticker, p), ...] likeliest first."""
    top = pd.DataFrame({"ticker": [t for t, _ in names], "p": [p for _, p in names],
                        "close": 1000.0, "rv20": 0.05})
    top["rank"] = top.index + 1
    return top


def stored(path, rowid=False):
    """Every row, recorded_utc included; with rowid, so a row deleted and
    re-inserted with the same values does not pass for untouched."""
    cols = ("rowid, " if rowid else "") + "as_of, ticker, p, rank, valid_until, recorded_utc"
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"SELECT {cols} FROM arb_veto "
                            "ORDER BY as_of, rank, ticker").fetchall()


def listed(path, day=DAY):
    """{ticker: (p, rank, valid_until)} for one as_of: the computation itself."""
    with closing(sqlite3.connect(path)) as conn:
        return {t: (p, r, v) for t, p, r, v in conn.execute(
            "SELECT ticker, p, rank, valid_until FROM arb_veto WHERE as_of = ?", (day,))}


def vetoed(path, session=SESSION):
    """What daily_picks actually blocks on that session."""
    with closing(sqlite3.connect(path)) as conn:
        return dp.arb_veto(conn, session)


# ── tests ──────────────────────────────────────────────────────────────────

def _seed(path, rows=None):
    rows = rows if rows is not None else [
        (DAY, "AAAA", .9, 1, VALID_UNTIL, "2026-09-24T12:04:57+00:00"),
        (DAY, "BBBB", .8, 2, VALID_UNTIL, "2026-09-24T12:04:57+00:00"),
    ]
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(av.SCHEMA)
        conn.executemany("INSERT INTO arb_veto VALUES (?,?,?,?,?,?)", rows)
        conn.commit()


def _refused_write(path, top, as_of=AS_OF):
    before = stored(path, rowid=True) if os.path.exists(path) else None
    before_bytes = open(path, "rb").read() if before is not None else None
    frame = top.copy(deep=True)
    try:
        av.write(as_of, top, path)
    except UnsupportedPriceContract as exc:
        status = exc.as_dict()
        assert status["consumer"] == "arb_veto.write" and "arb_veto.write" in status["reason"]
        assert status["status"] == "UNSUPPORTED" and status["contract_version"] == CONTRACT_VERSION
    else:
        raise AssertionError("an uncertified veto list was persisted")
    pd.testing.assert_frame_equal(top, frame)
    if before is None:
        assert not os.path.exists(path), "refusal must precede database creation"
    else:
        assert stored(path, rowid=True) == before
        with open(path, "rb") as fh:
            assert fh.read() == before_bytes, "refusal changed database bytes"


def test_initial_run_writes_the_list():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _refused_write(path, veto_list(("AAAA", .9), ("BBBB", .8)))
        assert os.listdir(tmp) == []


def test_same_as_of_rerun_drops_names_it_no_longer_flags():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path)
        _refused_write(path, veto_list(("BBBB", .97), ("DDDD", .94)))
        assert listed(path) == {"AAAA": (.9, 1, VALID_UNTIL), "BBBB": (.8, 2, VALID_UNTIL)}


def test_rerun_replaces_an_earlier_list_on_as_of_alone():
    stamp = "2026-09-24T12:04:57+00:00"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path, [(DAY, "AAAA", .98, 1, "2026-10-01", stamp),
                     (DAY, "BBBB", .97, 2, "2026-10-01", stamp),
                     (DAY, "CCCC", .90, 3, "2026-10-01", stamp)])
        _refused_write(path, veto_list(("BBBB", .97), ("DDDD", .94)))
        assert {row[5] for row in stored(path)} == {stamp}
        assert {row[4] for row in stored(path)} == {"2026-10-01"}


def test_same_as_of_rerun_updates_changed_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path)
        _refused_write(path, veto_list(("BBBB", .95), ("AAAA", .85)))
        assert listed(path) == {"AAAA": (.9, 1, VALID_UNTIL), "BBBB": (.8, 2, VALID_UNTIL)}


def test_unchanged_rerun_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path)
        top = veto_list(("AAAA", .9), ("BBBB", .8))
        first = stored(path, rowid=True)
        _refused_write(path, top)
        _refused_write(path, top)
        assert stored(path, rowid=True) == first


def test_other_as_of_lists_are_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path, [("2026-09-02", "OOOO", .95, 1, "2026-09-09", "old"),
                     ("2026-09-16", "PPPP", .91, 1, "2026-09-23", "prior"),
                     ("2026-09-16", "QQQQ", .88, 2, "2026-09-23", "prior"),
                     ("2026-09-30", "ZZZZ", .93, 1, "2026-10-07", "future")])
        history = stored(path, rowid=True)
        _refused_write(path, veto_list(("AAAA", .9), ("PPPP", .8)))
        _refused_write(path, veto_list(("CCCC", .7)))
        assert stored(path, rowid=True) == history and listed(path) == {}


def test_failed_rerun_keeps_the_previous_list():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path)
        _refused_write(path, veto_list(("DDDD", .95), ("EEEE", math.nan)))
        _refused_write(path, veto_list(("DDDD", .95)))
        assert set(listed(path)) == {"AAAA", "BBBB"}


def test_rerun_failing_for_a_reason_outside_the_list_keeps_the_previous_one():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TRIGGER boom BEFORE INSERT ON arb_veto "
                         "WHEN NEW.ticker = 'EEEE' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            conn.commit()
        _refused_write(path, veto_list(("DDDD", .95), ("EEEE", .9)))
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='boom'").fetchone()[0] == 1


def test_list_that_cannot_be_stored_exactly_is_refused_whole():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path)
        _refused_write(path, veto_list(("CCCC", .95), ("DDDD", .9), ("CCCC", .85)))
        assert set(listed(path)) == {"AAAA", "BBBB"}


def test_empty_rerun_clears_that_as_of_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed(path, [("2026-09-16", "PPPP", .9, 1, "2026-09-23", "prior"),
                     (DAY, "AAAA", .9, 1, VALID_UNTIL, "current")])
        _refused_write(path, veto_list())
        assert set(listed(path)) == {"AAAA"}
        assert set(listed(path, "2026-09-16")) == {"PPPP"}


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    print(f"arb veto: {len(ALL)} tests\n")
    for fn in ALL:
        print(fn.__name__)
        fn()
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
