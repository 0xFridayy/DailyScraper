"""idx_calendar: the official IDX session calendar broker_flow's expected
source session comes from. The announcements in idx_calendar.SOURCES are the
authority; price_history is only a secondary consistency check."""

import os
import sqlite3
from datetime import date, datetime, timedelta

import pytest

import idx_calendar as cal

HERE = os.path.dirname(os.path.abspath(__file__))


def days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def test_coverage_is_whole_years_each_with_closures_and_sources():
    years = list(range(cal.COVERED_FROM.year, cal.COVERED_THROUGH.year + 1))
    assert (cal.COVERED_FROM, cal.COVERED_THROUGH) == (date(2026, 1, 1), date(2027, 12, 31))
    assert sorted(cal.IDX_CLOSED_WEEKDAYS) == sorted(cal.SOURCES) == years


@pytest.mark.parametrize("year", [2026, 2027])
def test_closures_are_weekdays_of_their_year_and_match_the_announced_session_count(year):
    closed = [date.fromisoformat(d) for d in cal.IDX_CLOSED_WEEKDAYS[year]]
    assert all(d.year == year and d.weekday() < 5 for d in closed)
    weekdays = sum(1 for d in days(date(year, 1, 1), date(year, 12, 31)) if d.weekday() < 5)
    assert weekdays - len(closed) == cal.SOURCES[year]["sessions"]
    assert sum(cal.is_idx_session(d) for d in days(date(year, 1, 1), date(year, 12, 31))) == \
        cal.SOURCES[year]["sessions"]


def test_the_remaining_2026_closures_are_the_announced_year_end_ones():
    assert sorted(d for d in cal.IDX_CLOSED_WEEKDAYS[2026] if d >= "2026-10-01") == \
        ["2026-12-24", "2026-12-25", "2026-12-31"]


def test_sources_name_the_verified_announcements():
    s26, s27 = cal.SOURCES[2026], cal.SOURCES[2027]
    assert (s26["idx"]["announcement"], s26["idx"]["date"]) == ("Peng-00171/BEI.POP/09-2025", "2025-09-23")
    assert (s26["ksei"]["announcement"], s26["ksei"]["date"]) == ("PENG-0002/DIR/KSEI/0126", "2026-01-08")
    assert s26["ksei"]["title"] and s26["ksei"]["url"].startswith("https://web.ksei.co.id/files/")
    assert [h["announcement"] for h in s26["ksei_history"]] == ["PENG-0005/DIR/KSEI/1025"]
    assert (s27["idx"]["announcement"], s27["idx"]["date"]) == ("Peng-00169/BEI.POP/09-2026", "2026-09-16")
    assert (s27["ksei"]["announcement"], s27["ksei"]["date"]) == ("PENG-0004/DIR/KSEI/0926", "2026-09-24")
    assert all(s["verified_at"] == cal.VERIFIED_AT == "2026-09-30" for s in cal.SOURCES.values())
    assert "Peng-00171/BEI.POP/09-2026" not in repr(cal.SOURCES)       # no unproven 2027 update


@pytest.mark.parametrize("d, session", [
    (date(2026, 9, 29), True), (date(2026, 9, 26), False), (date(2026, 9, 27), False),
    (date(2026, 8, 17), False), (date(2026, 3, 20), False), (date(2026, 12, 31), False),
    (date(2026, 12, 30), True), (date(2027, 2, 5), False), (date(2027, 12, 30), True),
])
def test_is_idx_session(d, session):
    assert cal.is_idx_session(d) is session


@pytest.mark.parametrize("d", [date(2025, 12, 31), date(2028, 1, 1), date(2027, 12, 31) + timedelta(days=400)])
def test_is_idx_session_refuses_uncovered_dates(d):
    with pytest.raises(cal.IdxCalendarUnavailable, match="is outside IDX calendar idx-2026-2027.v1"):
        cal.is_idx_session(d)


@pytest.mark.parametrize("bad", ["2026-09-29", datetime(2026, 9, 29, 7, 0), None, 20260929])
def test_only_calendar_dates_are_accepted(bad):
    for lookup in (cal.is_idx_session, cal.latest_idx_session_before):
        with pytest.raises(TypeError):
            lookup(bad)


@pytest.mark.parametrize("scrape_date, expected", [
    (date(2026, 9, 30), date(2026, 9, 29)),     # normal next day
    (date(2026, 10, 1), date(2026, 9, 30)),
    (date(2026, 9, 28), date(2026, 9, 25)),     # Monday <- Friday
    (date(2026, 9, 27), date(2026, 9, 25)),     # Sunday
    (date(2026, 8, 18), date(2026, 8, 14)),     # not 08-17, the holiday a previous-weekday rule picks
    (date(2026, 8, 17), date(2026, 8, 14)),     # on the holiday
    (date(2026, 3, 25), date(2026, 3, 17)),     # Nyepi + Lebaran 03-18..03-24
    (date(2026, 12, 28), date(2026, 12, 23)),   # Christmas 12-24, 12-25 + weekend
    (date(2027, 1, 4), date(2026, 12, 30)),     # 12-31 Libur Bursa, 01-01, weekend
    (date(2027, 1, 1), date(2026, 12, 30)),
    (date(2027, 3, 16), date(2027, 3, 5)),      # longest closure: 03-08..03-15, 11 days back
    (date(2028, 1, 1), date(2027, 12, 30)),     # the walk stays inside coverage
])
def test_latest_idx_session_before(scrape_date, expected):
    assert cal.latest_idx_session_before(scrape_date) == expected


def test_latest_session_is_strictly_before_and_skips_only_non_sessions():
    for scrape_date in days(date(2026, 1, 3), date(2028, 1, 1)):
        got = cal.latest_idx_session_before(scrape_date)
        assert got < scrape_date and cal.is_idx_session(got)
        assert not any(cal.is_idx_session(d) for d in days(got + timedelta(days=1), scrape_date - timedelta(days=1)))


@pytest.mark.parametrize("scrape_date, missing", [
    (date(2026, 1, 1), "2025-12-31"), (date(2026, 1, 2), "2025-12-31"),   # before COVERED_FROM
    (date(2028, 1, 2), "2028-01-01"), (date(2028, 1, 4), "2028-01-03"),   # after COVERED_THROUGH
])
def test_latest_session_fails_closed_outside_coverage(scrape_date, missing):
    with pytest.raises(cal.IdxCalendarUnavailable, match=f"^{missing} is outside IDX calendar"):
        cal.latest_idx_session_before(scrape_date)


def test_latest_session_refuses_to_walk_past_the_lookback_limit(monkeypatch):
    closed = {d.isoformat() for d in days(date(2026, 6, 1), date(2026, 6, 30))}
    monkeypatch.setitem(cal.IDX_CLOSED_WEEKDAYS, 2026, frozenset(closed))
    with pytest.raises(cal.IdxCalendarUnavailable, match="no IDX session in the 14 days before 2026-07-01"):
        cal.latest_idx_session_before(date(2026, 7, 1))


def test_price_history_sessions_agree_with_the_calendar_secondary_check():
    """SECONDARY consistency only: price_history bars must fall exactly on the
    calendar's sessions over the dates both cover. It never defines the calendar."""
    path = os.path.join(HERE, "neobdm.db")
    if not os.path.exists(path):
        pytest.skip("neobdm.db not present")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        observed = {r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM price_history WHERE date BETWEEN ? AND ?",
            (cal.COVERED_FROM.isoformat(), cal.COVERED_THROUGH.isoformat()))}
    except sqlite3.OperationalError:
        pytest.skip("neobdm.db has no price_history")
    finally:
        conn.close()
    if not observed:
        pytest.skip("price_history has no covered dates")
    last = date.fromisoformat(max(observed))
    sessions = {d.isoformat() for d in days(cal.COVERED_FROM, last) if cal.is_idx_session(d)}
    assert observed == sessions
