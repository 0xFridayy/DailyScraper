"""Plain-script tests for arb_veto.write() (run by check_ml_health.py in CI,
or directly: py -3 test_arb_veto.py).

The contract: one as_of is one computation. After a successful write, the rows
for that as_of are exactly the list just scored -- nothing survives from an
earlier run for the same session -- and every other as_of is left as it was.
A write that fails leaves the previous list in place, whole.

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

def test_initial_run_writes_the_list():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        valid_until = av.write(AS_OF, veto_list(("AAAA", 0.9), ("BBBB", 0.8)), db)
        assert valid_until == VALID_UNTIL, valid_until
        got = listed(db)
        assert got == {"AAAA": (0.9, 1, VALID_UNTIL), "BBBB": (0.8, 2, VALID_UNTIL)}, got
        assert vetoed(db) == {"AAAA", "BBBB"}, vetoed(db)
    print("  ok first run writes exactly its list")


def test_same_as_of_rerun_drops_names_it_no_longer_flags():
    """What happened to 2026-09-23 in production: the Rp0.5bn run's BTEK and
    BAJA outlived the Rp2bn rerun that replaced it -- seven rows under a
    top-5, ranks 2 and 5 twice -- and went on vetoing through 2026-09-30."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        av.write(AS_OF, veto_list(("AAAA", 0.98), ("BBBB", 0.97), ("CCCC", 0.90)), db)
        av.write(AS_OF, veto_list(("BBBB", 0.97), ("DDDD", 0.94)), db)
        got = listed(db)
        assert set(got) == {"BBBB", "DDDD"}, got
        assert sorted(rank for _, rank, _ in got.values()) == [1, 2], got
        assert vetoed(db) == {"BBBB", "DDDD"}, vetoed(db)
    print("  ok same-as_of rerun drops the names it no longer flags")


def test_rerun_replaces_an_earlier_list_on_as_of_alone():
    """The earlier run's rows as production holds them: their own stamp, and
    a valid_until of their own (a run under other settings). The rerun must
    replace them on as_of alone and restamp every row it keeps."""
    stamp = "2026-09-24T12:04:57+00:00"
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(av.SCHEMA)
            conn.executemany("INSERT INTO arb_veto VALUES (?,?,?,?,?,?)", [
                (DAY, "AAAA", 0.98, 1, "2026-10-01", stamp),
                (DAY, "BBBB", 0.97, 2, "2026-10-01", stamp),
                (DAY, "CCCC", 0.90, 3, "2026-10-01", stamp)])
            conn.commit()
        av.write(AS_OF, veto_list(("BBBB", 0.97), ("DDDD", 0.94)), db)
        rows = [r for r in stored(db) if r[0] == DAY]
        assert [r[1:5] for r in rows] == [("BBBB", 0.97, 1, VALID_UNTIL),
                                          ("DDDD", 0.94, 2, VALID_UNTIL)], rows
        stamps = {r[5] for r in rows}
        assert len(stamps) == 1 and stamp not in stamps, rows
        assert vetoed(db, "2026-10-01") == set(), vetoed(db, "2026-10-01")
    print("  ok rerun replaces an earlier list whatever its stamp or valid_until")


def test_same_as_of_rerun_updates_changed_rows():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        av.write(AS_OF, veto_list(("AAAA", 0.90), ("BBBB", 0.80)), db)
        av.write(AS_OF, veto_list(("BBBB", 0.95), ("AAAA", 0.85)), db)
        got = listed(db)
        assert got == {"BBBB": (0.95, 1, VALID_UNTIL), "AAAA": (0.85, 2, VALID_UNTIL)}, got
    print("  ok same-as_of rerun updates p and rank of the names it keeps")


def test_unchanged_rerun_is_idempotent():
    top = veto_list(("AAAA", 0.9), ("BBBB", 0.8), ("CCCC", 0.7))
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        av.write(AS_OF, top, db)
        first = stored(db)
        av.write(AS_OF, top, db)
        again = stored(db)
        # recorded_utc says when the list was written, not what it says
        assert [r[:5] for r in again] == [r[:5] for r in first], (first, again)
        assert len({r[5] for r in again}) == 1, again      # one computation, one stamp
        assert vetoed(db) == {"AAAA", "BBBB", "CCCC"}
    print("  ok unchanged rerun leaves the same list")


def test_other_as_of_lists_are_left_alone():
    """A rerun rewrites its own session's list only: not the week before, not
    one long expired, not a later list already written, and not another
    list's row for a ticker it drops."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(av.SCHEMA)
            conn.executemany("INSERT INTO arb_veto VALUES (?,?,?,?,?,?)", [
                ("2026-09-02", "OOOO", 0.95, 1, "2026-09-09", "2026-09-06T21:00:00+00:00"),
                ("2026-09-16", "PPPP", 0.91, 1, "2026-09-23", "2026-09-20T21:00:00+00:00"),
                ("2026-09-16", "QQQQ", 0.88, 2, "2026-09-23", "2026-09-20T21:00:00+00:00"),
                ("2026-09-30", "ZZZZ", 0.93, 1, "2026-10-07", "2026-10-04T21:00:00+00:00")])
            conn.commit()
        history = stored(db, rowid=True)
        av.write(AS_OF, veto_list(("AAAA", 0.9), ("PPPP", 0.8)), db)
        av.write(AS_OF, veto_list(("CCCC", 0.7)), db)
        after = [r for r in stored(db, rowid=True) if r[1] != DAY]
        assert after == history, (history, after)
        assert set(listed(db)) == {"CCCC"}, listed(db)
        # 2026-09-23 is the 2026-09-16 list's last session and the rerun's first
        assert vetoed(db, "2026-09-23") == {"PPPP", "QQQQ", "CCCC"}, vetoed(db, "2026-09-23")
    print("  ok other as_of lists are untouched, row for row")


def test_failed_rerun_keeps_the_previous_list():
    """The NaN is on the second row, so by the time SQLite refuses it (NaN
    binds as NULL; p is NOT NULL) the rerun has already cleared the old list
    and inserted DDDD. None of that may be left behind."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        av.write(AS_OF, veto_list(("AAAA", 0.9), ("BBBB", 0.8), ("CCCC", 0.7)), db)
        before = stored(db, rowid=True)
        try:
            av.write(AS_OF, veto_list(("DDDD", 0.95), ("EEEE", math.nan)), db)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("a list with a NaN p was written")
        assert stored(db, rowid=True) == before, stored(db, rowid=True)
        assert vetoed(db) == {"AAAA", "BBBB", "CCCC"}, vetoed(db)
        # and the failure left nothing open: the next rerun still goes through
        av.write(AS_OF, veto_list(("DDDD", 0.95)), db)
        assert set(listed(db)) == {"DDDD"}, listed(db)
    print("  ok failed rerun leaves the previous list whole")


def test_rerun_failing_for_a_reason_outside_the_list_keeps_the_previous_one():
    """Atomicity must not hang on the data being bad. The list is valid; a
    trigger aborts the second insert the way a full disk or an I/O error
    would, after the delete and the first insert have run."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        av.write(AS_OF, veto_list(("AAAA", 0.9), ("BBBB", 0.8), ("CCCC", 0.7)), db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TRIGGER boom BEFORE INSERT ON arb_veto "
                         "WHEN NEW.ticker = 'EEEE' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            conn.commit()
        before = stored(db, rowid=True)
        try:
            av.write(AS_OF, veto_list(("DDDD", 0.95), ("EEEE", 0.9)), db)
        except sqlite3.DatabaseError as e:
            assert "injected" in str(e), e
        else:
            raise AssertionError("the injected failure did not reach write()")
        assert stored(db, rowid=True) == before, stored(db, rowid=True)
        assert vetoed(db) == {"AAAA", "BBBB", "CCCC"}, vetoed(db)
    print("  ok rerun failing mid-write for any reason leaves the previous list whole")


def test_list_that_cannot_be_stored_exactly_is_refused_whole():
    """(as_of, ticker) is the key, so one ticker twice cannot be stored as
    computed. Collapsing it would drop a rank without a word; refuse the
    whole list instead and keep the previous one."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        av.write(AS_OF, veto_list(("AAAA", 0.9), ("BBBB", 0.8)), db)
        before = stored(db, rowid=True)
        try:
            av.write(AS_OF, veto_list(("CCCC", 0.95), ("DDDD", 0.9), ("CCCC", 0.85)), db)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(f"a list naming CCCC twice was written: {stored(db)}")
        assert stored(db, rowid=True) == before, stored(db, rowid=True)
    print("  ok list with a repeated ticker is refused and the previous list kept")


def test_empty_rerun_clears_that_as_of_only():
    """--top-n 0 scores nothing, and the stored list says so. Only this
    as_of: an empty list is not a licence to touch another session's."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "picks.db")
        av.write(pd.Timestamp("2026-09-16"), veto_list(("PPPP", 0.9)), db)
        av.write(AS_OF, veto_list(("AAAA", 0.9)), db)
        av.write(AS_OF, veto_list(), db)
        assert listed(db) == {}, listed(db)
        assert set(listed(db, "2026-09-16")) == {"PPPP"}, listed(db, "2026-09-16")
    print("  ok empty rerun leaves that as_of empty and the rest alone")


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
