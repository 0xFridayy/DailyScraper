"""Tests for morning.py: which raw NeoBDM report, if any, goes out after the
picks step. No network, no Playwright session, no database.

    py -3 test_morning.py
"""

import os
import sys
import types
import zoneinfo

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

import morning  # noqa: E402
import neobdm_scraper as ns  # noqa: E402

RAW = "📈 NeoBDM Daily Signal\nraw report body"
STALE_WARNING = "⚠️ No fresh NeoBDM data this morning (the scrape ran late or failed), so no new picks today."


def run(status=None, raises=None, scrape_messages=(RAW,), picks_sent=None, picks_results=()):
    """Run morning.main() with the scrape and daily_picks faked. Returns every
    message that reached the real Telegram sender, in order. `picks_results`
    scripts the picks sender's successive outcomes (True/False, or an
    exception to raise); once exhausted it succeeds."""
    sent = []
    picks_sent = [] if picks_sent is None else picks_sent
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
    picks_sent = []
    sent = run(status="stale_send_failed", picks_sent=picks_sent, picks_results=[False, True])
    assert sent == [], sent                                  # never the held raw report
    assert picks_sent == [STALE_WARNING, STALE_WARNING], picks_sent


def test_stale_send_failed_never_sends_raw_even_when_the_retry_fails():
    for second in (False, RuntimeError("telegram down")):
        picks_sent = []
        sent = run(status="stale_send_failed", picks_sent=picks_sent, picks_results=[False, second])
        assert sent == [], (second, sent)
        assert picks_sent == [STALE_WARNING, STALE_WARNING], picks_sent


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
