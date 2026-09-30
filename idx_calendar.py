"""IDX (Bursa Efek Indonesia) trading calendar: which dates are exchange sessions.

Pure and dependency-free so CI can test it without playwright. It answers one
question for broker_flow: which session must a capture dated `scrape_date` hold?

    expected_source_session(scrape_date) = latest_idx_session_before(scrape_date)

the latest official IDX session STRICTLY BEFORE the MYT scrape date (the live
broker_flow.date convention, HANDOFF Appendix R). It never depends on the clock
time of the capture, and it never guesses: a weekday is a session unless the
exchange announced it closed, and a date outside COVERED_FROM..COVERED_THROUGH
raises IdxCalendarUnavailable instead of falling back to "previous weekday"
(IDX holidays and cuti bersama make that wrong) or to price_history.

Closures are copied from the official announcements in SOURCES and must be
extended (with a new CALENDAR_VERSION) when IDX publishes the next year or
amends a published one. price_history's observed sessions are only a secondary
consistency check (test_idx_calendar.py), never the authority.
"""

from datetime import date, datetime, timedelta

CALENDAR_VERSION = "idx-2026-2027.v1"
COVERED_FROM = date(2026, 1, 1)
COVERED_THROUGH = date(2027, 12, 31)
VERIFIED_AT = "2026-09-30"

# Longest walk back a lookup may take. The longest covered closure (2027-03-08
# to 03-15 plus a weekend) needs 11 days; anything longer means the calendar is
# wrong, so the lookup refuses rather than reaching further back.
MAX_LOOKBACK_DAYS = 14

# Weekday exchange closures (libur bursa), exactly as announced. Weekends are
# never sessions and are not listed.
IDX_CLOSED_WEEKDAYS = {
    2026: frozenset({
        "2026-01-01",   # Tahun Baru 2026 Masehi
        "2026-01-16",   # Isra Mikraj Nabi Muhammad SAW
        "2026-02-16",   # Cuti Bersama Tahun Baru Imlek 2577 Kongzili
        "2026-02-17",   # Tahun Baru Imlek 2577 Kongzili
        "2026-03-18",   # Cuti Bersama Hari Suci Nyepi Tahun Baru Saka 1948
        "2026-03-19",   # Hari Suci Nyepi Tahun Baru Saka 1948
        "2026-03-20",   # Cuti Bersama Idul Fitri 1447 Hijriah
        "2026-03-23",   # Cuti Bersama Idul Fitri 1447 Hijriah
        "2026-03-24",   # Cuti Bersama Idul Fitri 1447 Hijriah
        "2026-04-03",   # Wafat Yesus Kristus
        "2026-05-01",   # Hari Buruh Internasional
        "2026-05-14",   # Kenaikan Yesus Kristus
        "2026-05-15",   # Cuti Bersama Kenaikan Yesus Kristus
        "2026-05-27",   # Idul Adha 1447 Hijriah
        "2026-05-28",   # Cuti Bersama Hari Raya Idul Adha 1447 Hijriah
        "2026-06-01",   # Hari Lahir Pancasila
        "2026-06-16",   # 1 Muharam Tahun Baru Islam 1448 Hijriah
        "2026-08-17",   # Proklamasi Kemerdekaan
        "2026-08-25",   # Maulid Nabi Muhammad SAW
        "2026-12-24",   # Cuti Bersama Kelahiran Yesus Kristus
        "2026-12-25",   # Kelahiran Yesus Kristus
        "2026-12-31",   # Libur Bursa
    }),
    2027: frozenset({
        "2027-01-01",   # Tahun Baru 2027 Masehi
        "2027-01-05",   # Isra Mikraj Nabi Muhammad S.A.W. 1448 Hijriah
        "2027-02-05",   # Cuti Bersama Tahun Baru Imlek 2578 Kongzili
        "2027-03-08",   # Hari Suci Nyepi (Tahun Baru Saka 1949)
        "2027-03-09",   # Cuti Bersama Idul Fitri 1448 Hijriah
        "2027-03-10",   # Idul Fitri 1448 Hijriah
        "2027-03-11",   # Idul Fitri 1448 Hijriah
        "2027-03-12",   # Cuti Bersama Idul Fitri 1448 Hijriah
        "2027-03-15",   # Cuti Bersama Idul Fitri 1448 Hijriah
        "2027-03-25",   # Cuti Bersama Wafat Yesus Kristus
        "2027-03-26",   # Wafat Yesus Kristus
        "2027-05-06",   # Kenaikan Yesus Kristus
        "2027-05-17",   # Idul Adha 1448 Hijriah
        "2027-05-18",   # Cuti Bersama Hari Raya Idul Adha 1448 Hijriah
        "2027-05-19",   # Cuti Bersama Hari Raya Waisak 2571 BE
        "2027-05-20",   # Hari Raya Waisak 2571 BE
        "2027-06-01",   # Hari Lahir Pancasila
        "2027-08-17",   # Proklamasi Kemerdekaan
        "2027-12-24",   # Cuti Bersama Kelahiran Yesus Kristus
        "2027-12-31",   # Libur Bursa
    }),
}

# Where each year's closures come from. The IDX announcement is the authority;
# the KSEI announcement is the official follow-up that restates its closures
# (idx.co.id is bot-protected, so the KSEI text is the copy that was read).
# `sessions` is the announced number of trading days, a cross-check on the list.
SOURCES = {
    2026: {
        "idx": {"announcement": "Peng-00171/BEI.POP/09-2025", "date": "2025-09-23",
                "title": "Kalender Libur Bursa Tahun 2026",
                "url": "https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/Exchange/"
                       "Peng-00171%20Libur%20Bursa%202026-No.%20Peng-00171BEI.POP09-2025.pdf"},
        "ksei": {"announcement": "PENG-0002/DIR/KSEI/0126", "date": "2026-01-08",
                 "title": None,   # adjustment of PENG-0005/DIR/KSEI/1025; follows the IDX announcement above
                 "url": None},
        "ksei_history": [
            {"announcement": "PENG-0005/DIR/KSEI/1025", "date": "2025-10-01",
             "title": "Pengumuman Hari Libur dan Cuti Bersama PT KSEI Tahun 2026",
             "url": "https://web.ksei.co.id/files/Pengumuman_Hari_Libur_dan_Cuti_Bersama_PT_KSEI_Tahun_2026.pdf"},
        ],
        "sessions": 239,
        "verified_at": VERIFIED_AT,
    },
    2027: {
        "idx": {"announcement": "Peng-00169/BEI.POP/09-2026", "date": "2026-09-16",
                "title": "Kalender Libur Bursa Tahun 2027", "url": None},
        "ksei": {"announcement": "PENG-0004/DIR/KSEI/0926", "date": "2026-09-24",
                 "title": "Pengumuman Hari Libur dan Cuti Bersama PT KSEI Tahun 2027",
                 "url": "https://web.ksei.co.id/files/Pengumuman_Hari_Libur_dan_Cuti_Bersama_PT_KSEI_Tahun_2027.pdf"},
        "ksei_history": [],
        "sessions": 241,
        "verified_at": VERIFIED_AT,
    },
}


class IdxCalendarUnavailable(Exception):
    """The calendar cannot establish whether a date is an IDX session."""


def _check_date(d):
    # A datetime is a date too, but its day depends on a timezone this module
    # cannot know; only a calendar date is accepted.
    if not isinstance(d, date) or isinstance(d, datetime):
        raise TypeError(f"{d!r} is not a calendar date")


def is_idx_session(d):
    """Is `d` an official IDX trading session? Raises IdxCalendarUnavailable
    outside COVERED_FROM..COVERED_THROUGH."""
    _check_date(d)
    if not COVERED_FROM <= d <= COVERED_THROUGH:
        raise IdxCalendarUnavailable(
            f"{d} is outside IDX calendar {CALENDAR_VERSION} ({COVERED_FROM} to {COVERED_THROUGH})")
    return d.weekday() < 5 and d.isoformat() not in IDX_CLOSED_WEEKDAYS[d.year]


def latest_idx_session_before(scrape_date):
    """The latest official IDX session strictly before `scrape_date`: the session
    a broker_flow capture dated `scrape_date` must hold. Raises
    IdxCalendarUnavailable if any date it has to examine is uncovered, or if no
    session lies within MAX_LOOKBACK_DAYS."""
    _check_date(scrape_date)
    d = scrape_date
    for _ in range(MAX_LOOKBACK_DAYS):
        d -= timedelta(days=1)
        if is_idx_session(d):
            return d
    raise IdxCalendarUnavailable(
        f"no IDX session in the {MAX_LOOKBACK_DAYS} days before {scrape_date} per {CALENDAR_VERSION}")
