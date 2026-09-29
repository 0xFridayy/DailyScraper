"""Tests for morning.py: which raw NeoBDM report, if any, goes out after the
picks step. No network, no Playwright session, no database.

    py -3 test_morning.py
"""

import os
import sqlite3
import sys
import tempfile
import types
import zoneinfo
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# First, before any stand-in exists: price_audit imports pandas, and pandas
# probes for pytz once, on import (see test_inventory_capture.py).
import price_audit  # noqa: E402,F401


def _refuse(what):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test tried to {what}")
    return refuse


def _stand_in_if_missing(name, **attrs):
    """A stand-in for a third-party module this machine lacks; wherever the
    real one is installed, the real one is used."""
    try:
        __import__(name)
        return
    except ImportError:
        pass
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sys.modules.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


_stand_in_if_missing("playwright.sync_api", sync_playwright=_refuse("start a browser"),
                     TimeoutError=type("TimeoutError", (Exception,), {}))
_stand_in_if_missing("schedule")
_stand_in_if_missing("pytz", timezone=zoneinfo.ZoneInfo)
for _secret in ("NEOBDM_USERNAME", "NEOBDM_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.setdefault(_secret, "test-placeholder")

import daily_picks as dp  # noqa: E402
import morning  # noqa: E402
import neobdm_scraper as ns  # noqa: E402

RAW = "📈 NeoBDM Daily Signal\nraw report body"
STALE_WARNING = "⚠️ No fresh NeoBDM data this morning (the scrape ran late or failed), so no new picks today."


def run(status=None, raises=None, scrape_messages=(RAW,), picks_sent=None, picks_results=(),
        recorded=None):
    """Run morning.main() with the scrape and daily_picks faked. Returns every
    message that reached the real Telegram sender, in order. `picks_results`
    scripts the picks sender's successive outcomes (True/False, or an
    exception to raise); once exhausted it succeeds. `recorded` receives the
    text of every record_stale_warning call."""
    sent = []
    picks_sent = [] if picks_sent is None else picks_sent
    recorded = [] if recorded is None else recorded
    outcomes = list(picks_results)

    def fake_run_all_jobs():
        for m in scrape_messages:
            ns.send_telegram(m)

    def fake_run_morning(now_utc, send):
        if raises is not None:
            raise raises
        if status in ("stale", "stale_send_failed"):
            send(STALE_WARNING)
            return status, STALE_WARNING
        return status, None

    def picks_send(text):
        picks_sent.append(text)
        outcome = outcomes.pop(0) if outcomes else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fake_picks = types.ModuleType("daily_picks")
    fake_picks.run_morning = fake_run_morning
    fake_picks.telegram_sender_from_env = lambda: picks_send
    fake_picks.record_stale_warning = lambda run_utc, text, sent_utc: recorded.append(text)
    saved = (sys.modules.get("daily_picks"), ns.run_all_jobs, ns.send_telegram)
    sys.modules["daily_picks"] = fake_picks
    ns.run_all_jobs, ns.send_telegram = fake_run_all_jobs, sent.append
    try:
        assert morning.main() == 0
        assert ns.send_telegram == sent.append          # the real sender is restored
    finally:
        if saved[0] is None:
            sys.modules.pop("daily_picks", None)
        else:
            sys.modules["daily_picks"] = saved[0]
        ns.run_all_jobs, ns.send_telegram = saved[1], saved[2]
    return sent


def test_stale_sends_only_the_stale_warning_never_the_held_raw_report():
    picks_sent = []
    sent = run(status="stale", picks_sent=picks_sent)
    assert sent == [], sent
    assert picks_sent == [STALE_WARNING], picks_sent


def test_stale_send_failed_retries_only_the_stale_warning():
    picks_sent, recorded = [], []
    sent = run(status="stale_send_failed", picks_sent=picks_sent, picks_results=[False, True],
               recorded=recorded)
    assert sent == [], sent                                  # never the held raw report
    assert picks_sent == [STALE_WARNING, STALE_WARNING], picks_sent
    assert recorded == [STALE_WARNING], recorded             # the delivered retry is recorded


def test_stale_send_failed_never_sends_raw_even_when_the_retry_fails():
    for second in (False, RuntimeError("telegram down")):
        picks_sent, recorded = [], []
        sent = run(status="stale_send_failed", picks_sent=picks_sent, picks_results=[False, second],
                   recorded=recorded)
        assert sent == [], (second, sent)
        assert picks_sent == [STALE_WARNING, STALE_WARNING], picks_sent
        assert recorded == [], (second, recorded)            # a failed retry records nothing


def test_a_failure_recording_the_retry_never_sends_raw():
    sent, picks_sent = [], []
    fake = types.ModuleType("daily_picks")
    fake.telegram_sender_from_env = lambda: (lambda t: picks_sent.append(t) or len(picks_sent) > 1)
    fake.run_morning = lambda now_utc, send: (send(STALE_WARNING), ("stale_send_failed", STALE_WARNING))[1]

    def broken_record(run_utc, text, sent_utc):
        raise sqlite3.OperationalError("database is locked")
    fake.record_stale_warning = broken_record
    prev = (sys.modules.get("daily_picks"), ns.run_all_jobs, ns.send_telegram)
    sys.modules["daily_picks"] = fake
    ns.run_all_jobs = lambda: ns.send_telegram(RAW)
    ns.send_telegram = sent.append
    try:
        assert morning.main() == 0
    finally:
        sys.modules["daily_picks"] = prev[0]
        ns.run_all_jobs, ns.send_telegram = prev[1], prev[2]
    assert sent == [], sent
    assert picks_sent == [STALE_WARNING, STALE_WARNING]


# ── the stale retry against the real daily_picks and a real picks database ──

RUN_MYT = datetime(2026, 9, 29, 7, 30, tzinfo=dp.MYT)          # a Tuesday, pre-open


def stale_env(tmp):
    """A NeoBDM db whose latest capture is days old, so today's run is stale."""
    neo = os.path.join(tmp, "neobdm.db")
    conn = sqlite3.connect(neo)
    conn.execute(f"CREATE TABLE market_summary_daily (date TEXT, ticker TEXT, {', '.join(dp.FIELDS)})")
    conn.execute("CREATE TABLE konglo_signal_watch (flag_date TEXT, ticker TEXT, sources TEXT, is_tracked INTEGER)")
    conn.execute("CREATE TABLE signal_source_status (flag_date TEXT, source TEXT, status TEXT)")
    conn.execute(f"INSERT INTO market_summary_daily VALUES ('2026-09-24', 'AAAA', "
                 f"{', '.join('1' for _ in dp.FIELDS)})")
    conn.commit()
    conn.close()
    follows = os.path.join(tmp, "follows.json")
    dp.save_follows(follows, {"last_update_id": 0, "events": []})
    return dict(neobdm_db=neo, picks_db=os.path.join(tmp, "picks.db"), follows_json=follows)


def stale_rows(env):
    if not os.path.exists(env["picks_db"]):
        return []
    conn = sqlite3.connect(env["picks_db"])
    try:
        return conn.execute("SELECT kind, key, sent_utc FROM sent_messages WHERE kind = 'stale'").fetchall()
    finally:
        conn.close()


def run_real(env, picks_results, clock_utc):
    """morning.main() against the real daily_picks bound to `env`, with the
    picks sender scripted and morning's clock fixed to `clock_utc` (a list:
    one instant per datetime.now() call, the last one repeating)."""
    sent, picks_sent = [], []
    outcomes, clock = list(picks_results), list(clock_utc)

    def picks_send(text):
        picks_sent.append(text)
        outcome = outcomes.pop(0) if outcomes else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (clock.pop(0) if len(clock) > 1 else clock[0]).astimezone(tz)

    bound = types.ModuleType("daily_picks")
    bound.telegram_sender_from_env = lambda: picks_send
    bound.run_morning = lambda now_utc, send: dp.run_morning(now_utc, send, **env)
    bound.record_stale_warning = lambda run_utc, text, sent_utc: dp.record_stale_warning(
        run_utc, text, sent_utc, picks_db=env["picks_db"])
    prev = (sys.modules.get("daily_picks"), ns.run_all_jobs, ns.send_telegram, morning.datetime)
    sys.modules["daily_picks"] = bound
    ns.run_all_jobs = lambda: ns.send_telegram(RAW)
    ns.send_telegram = sent.append
    morning.datetime = Clock
    try:
        assert morning.main() == 0
    finally:
        sys.modules["daily_picks"] = prev[0]
        ns.run_all_jobs, ns.send_telegram, morning.datetime = prev[1], prev[2], prev[3]
    return sent, picks_sent


def test_delivered_retry_is_recorded_once_and_the_next_run_stays_quiet():
    run_at = RUN_MYT.astimezone(timezone.utc)
    retry_at = run_at + timedelta(minutes=1)
    with tempfile.TemporaryDirectory() as tmp:
        env = stale_env(tmp)
        sent, picks_sent = run_real(env, [False, True], [run_at, retry_at])
        assert sent == [], sent                                          # no raw substitute
        assert len(picks_sent) == 2 and "No fresh NeoBDM data" in picks_sent[0]
        assert picks_sent[1] == picks_sent[0]
        assert stale_rows(env) == [("stale", "2026-09-29", retry_at.isoformat())]
        # the same local date again: already warned, nothing sent, still one row
        sent2, picks_sent2 = run_real(env, [], [run_at + timedelta(minutes=5)])
        assert sent2 == [] and picks_sent2 == [], (sent2, picks_sent2)
        status, _ = dp.run_morning(run_at + timedelta(minutes=6), lambda t: 1 / 0, **env)
        assert status == "stale_already_warned", status
        assert len(stale_rows(env)) == 1
        # recording is idempotent (INSERT OR IGNORE): the first delivery stays
        dp.record_stale_warning(run_at, picks_sent[0], retry_at + timedelta(hours=1), picks_db=env["picks_db"])
        assert stale_rows(env) == [("stale", "2026-09-29", retry_at.isoformat())]


def test_failed_or_raising_retry_records_nothing_and_never_sends_raw():
    run_at = RUN_MYT.astimezone(timezone.utc)
    for second in (False, RuntimeError("telegram down")):
        with tempfile.TemporaryDirectory() as tmp:
            env = stale_env(tmp)
            sent, picks_sent = run_real(env, [False, second], [run_at, run_at + timedelta(minutes=1)])
            assert sent == [], (second, sent)
            assert len(picks_sent) == 2, picks_sent
            assert stale_rows(env) == [], (second, stale_rows(env))
            # nothing recorded, so the next run warns again (and records it)
            sent2, picks_sent2 = run_real(env, [True], [run_at + timedelta(minutes=5)])
            assert sent2 == [] and len(picks_sent2) == 1
            assert [k for k, _d, _s in stale_rows(env)] == ["stale"]


def test_delivered_stale_warning_is_not_retried():
    picks_sent = []
    assert run(status="stale", picks_sent=picks_sent) == []
    assert picks_sent == [STALE_WARNING]


def test_send_failed_sends_the_held_raw_report():
    assert run(status="send_failed") == [RAW]


def test_picks_exception_sends_the_held_raw_report_with_the_failure_suffix():
    sent = run(raises=ValueError("boom"))
    assert len(sent) == 1, sent
    assert sent[0].startswith(RAW)
    assert "⚠️ Picks step failed today (ValueError)" in sent[0]
    assert "boom" not in sent[0]                     # only the type, never the text


def test_neobdm_error_passes_through_immediately():
    for status in ("sent", "stale", "stale_send_failed", "send_failed"):
        sent = run(status=status, scrape_messages=("NeoBDM error: RuntimeError: x",))
        assert sent == ["NeoBDM error: RuntimeError: x"], status


def _quiet(status):
    def test():
        assert run(status=status) == [], status
    test.__name__ = f"test_quiet_status_never_sends_the_held_raw_report[{status}]"
    return test


QUIET = [_quiet(s) for s in ("weekend", "already_sent", "stale_already_warned", "sent", "no_data")]


def test_no_held_report_means_nothing_to_fall_back_to():
    assert run(status="send_failed", scrape_messages=()) == []
    assert run(raises=ValueError("x"), scrape_messages=()) == []


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)] + QUIET


def main():
    print(f"morning: {len(ALL)} tests\n")
    failed = 0
    for fn in ALL:
        try:
            fn()
            print(f"  ok {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} of {len(ALL)} tests FAILED.")
        return 1
    print(f"\nAll {len(ALL)} tests OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
