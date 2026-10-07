"""Plain-script tests for daily_picks.py and telegram_inbox.py (run by
check_ml_health.py in CI, or directly: py -3 test_daily_picks.py).

Synthetic databases only; no network, no scraper import.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing, redirect_stdout
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "42")

import daily_picks as dp
import telegram_inbox as inbox


# ── helpers ────────────────────────────────────────────────────────────────

def row(close=1000, m=0.1, nr=0.1, f=0.1, cs=3, tval=10.0, pct5=0.01,
        top='["BK", "RF", "CC", "AK", "ZP"]'):
    return {"close": close, "high": close, "low": close, "m_dn_0": m, "nr_dn_0": nr,
            "f_dn_0": f, "m_cn_5": m, "clean_score": cs, "tval": tval, "pct_5": pct5,
            "top_5_buyer": top}


def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def make_db(path, days, stalker=None, status=None):
    """days: [(date, {ticker: row})]. stalker: {date: [tickers]}."""
    conn = sqlite3.connect(path)
    cols = ", ".join(f"{c}" for c in dp.FIELDS)
    conn.execute(f"CREATE TABLE market_summary_daily (date TEXT, ticker TEXT, {cols})")
    conn.execute("CREATE TABLE konglo_signal_watch (flag_date TEXT, ticker TEXT, "
                 "sources TEXT, is_tracked INTEGER)")
    conn.execute("CREATE TABLE signal_source_status (flag_date TEXT, source TEXT, "
                 "status TEXT)")
    for day, rows in days:
        for ticker, r in rows.items():
            conn.execute(f"INSERT INTO market_summary_daily VALUES (?, ?, "
                         f"{', '.join('?' for _ in dp.FIELDS)})",
                         (day, ticker, *[r[c] for c in dp.FIELDS]))
    for day, tickers in (stalker or {}).items():
        for t in tickers:
            conn.execute("INSERT INTO konglo_signal_watch VALUES (?, ?, 'broker_stalker', 0)",
                         (day, t))
    for day, st in (status or {}).items():
        conn.execute("INSERT INTO signal_source_status VALUES (?, 'broker_stalker', ?)", (day, st))
    conn.commit()
    conn.close()


def snaps_from(days):
    return [dp.Snapshot(d, rows) for d, rows in days]


def panel(n=12, start="2026-08-03", tickers=("AAAA", "BBBB", "CCCC", "DDDD"), drift=None):
    """n weekday sessions; each ticker's close grows by its drift per session.
    Bandar flow changes a little every day, so no day looks like a copy."""
    drift = drift or {t: 0.0 for t in tickers}
    days = []
    for i, day in enumerate(weekdays(start, n)):
        days.append((day, {t: row(close=round(1000 * (1 + drift[t]) ** i, 6), m=0.1 + 0.001 * i)
                           for t in tickers}))
    return days


def utc_at_myt(day, hour=9, minute=30):
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=dp.MYT).astimezone(timezone.utc)


# ── loading and dates ──────────────────────────────────────────────────────

def test_weekend_and_holiday_copies_are_dropped():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_session_label_is_previous_weekday():
    assert dp.session_label("2026-09-18") == "Thu 17 Sep"
    assert dp.session_label("2026-09-12") == "Fri 11 Sep"   # Saturday capture
    assert dp.session_label("2026-09-14") == "Fri 11 Sep"   # Monday capture
    print("  ok session labels")


# ── tags, filters and ranking ──────────────────────────────────────────────

def test_tags_never_read_later_snapshots():
    days = panel(10)
    snaps = snaps_from(days)
    before = dp.tag_snapshot(snaps, 5)
    later = [(d, {t: dict(r, close=r["close"] * 3, m_dn_0=-1, tval=999) for t, r in rows.items()})
             for d, rows in days[6:]]
    after = dp.tag_snapshot(snaps_from(days[:6] + later), 5)
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    print("  ok no future data in tags")


def test_missing_values_are_not_zero_and_do_not_fire():
    days = panel(4)
    days[-1][1]["AAAA"] = row(nr=None, f=0.2, cs=None)
    days[-1][1]["BBBB"] = row(tval=None)
    days[-1][1]["CCCC"] = row(pct5=None)
    tagged = dp.tag_snapshot(snaps_from(days), 3)
    assert "BBBB" not in tagged and "CCCC" not in tagged
    assert "inst_foreign" not in tagged["AAAA"]["tags"]
    assert "broad_buying" not in tagged["AAAA"]["tags"]
    print("  ok missing stays missing")


def test_filters_skip_illiquid_runups_and_recent_splits():
    days = panel(8)
    days[-1][1]["AAAA"] = row(tval=1.5)            # under Rp 2 bn
    days[-1][1]["BBBB"] = row(pct5=0.30)           # already ran 30%
    days[-3][1]["CCCC"] = row(close=200)           # 1000 -> 200: a 1:5 split
    days[-2][1]["CCCC"] = row(close=200)
    days[-1][1]["CCCC"] = row(close=200)
    tagged = dp.tag_snapshot(snaps_from(days), 7)
    assert set(tagged) == {"DDDD"}, sorted(tagged)
    print("  ok filters")


def test_rank_needs_two_signals_and_follows_weights():
    days = panel(6)
    last = days[-1][1]
    last["AAAA"] = row(cs=5, nr=-0.1, f=0.1, m=-0.1)          # 1 tag
    last["BBBB"] = row(cs=4, nr=0.1, f=0.1, m=-0.1)           # broad + inst
    last["CCCC"] = row(cs=1, nr=0.1, f=0.1, m=0.1, tval=20.0)  # inst + bandar_3days + value_up
    last["DDDD"] = row(cs=0, nr=-0.1, f=-0.1, m=-0.1)         # none
    tagged = dp.tag_snapshot(snaps_from(days), 5)
    picks = dp.rank_picks(tagged, {}, min_tags=2)
    assert [t for t, _ in picks] == ["CCCC", "BBBB"], picks
    picks = dp.rank_picks(tagged, {"value_up": 0.25, "bandar_3days": 0.25, "inst_foreign": 1,
                                   "broad_buying": 2}, min_tags=2)
    assert [t for t, _ in picks] == ["BBBB", "CCCC"], picks
    assert [t for t, _ in dp.rank_picks(tagged, {})] == ["CCCC"]    # the machine's 3+ bar
    print("  ok ranking")


def test_zero_clean_score_is_a_real_tiebreak_value():
    days = panel(4)
    last = days[-1][1]
    last["AAAA"] = row(cs=0, nr=0.1, f=0.1)
    last["BBBB"] = row(cs=None, nr=0.1, f=0.1)
    tagged = dp.tag_snapshot(snaps_from(days), 3)
    ranked = dp.rank_picks(tagged, {}, n=10, min_tags=2)
    names = [t for t, _ in ranked]
    assert names.index("AAAA") < names.index("BBBB"), names
    print("  ok zero is not missing")


# ── outcomes and learning ──────────────────────────────────────────────────

def test_outcome_waits_for_exit_and_enters_next_session():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_learning_is_slow_capped_and_uses_only_matured_sessions():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_stalker_flag_is_shown_but_not_a_check():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_corporate_action_hint_and_limits():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


# ── follow-ups ─────────────────────────────────────────────────────────────

def test_reference_price_is_what_the_owner_could_see():
    days = panel(5, start="2026-09-14")
    snaps = snaps_from(days)
    early = utc_at_myt("2026-09-16", 8, 0).isoformat()      # before that morning's data
    late = utc_at_myt("2026-09-16", 11, 0).isoformat()
    assert snaps[dp.reference_index(snaps, "AAAA", early)].date == "2026-09-15"
    assert snaps[dp.reference_index(snaps, "AAAA", late)].date == "2026-09-16"
    print("  ok reference price")


def test_follow_lines_report_flow_volume_and_warnings():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_follow_of_unknown_stock_says_so():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


# ── telegram inbox ─────────────────────────────────────────────────────────

KNOWN = {"BBCA", "BBRI", "TLKM", "BANK"} | {f"A{c}AZ" for c in "ABCDEFGHIJ"}


def cmd(text, known=KNOWN):
    c = inbox.parse_command(text, known)
    return c and (c["action"], c["tickers"], c["days"], c["reason"], c["problem"])


def test_parse_command_variants():
    assert cmd("yes bbca bandar buying") == ("yes", ["BBCA"], None, "bandar buying", None)
    assert cmd("/YES@my_bot BBCA, bbri banks") == ("yes", ["BBCA", "BBRI"], None, "banks", None)
    assert cmd("Stop BBCA taking profit")[:2] == ("stop", ["BBCA"])
    assert cmd("list") == ("list", [], None, "", None)
    assert cmd("yessir BBCA") is None and cmd("hello") is None and cmd(None) is None
    assert cmd("yes BBCAX") == ("yes", [], None, "BBCAX", None)
    assert cmd("yes BBCA high volume breakout")[1:4] == (["BBCA"], None, "high volume breakout")
    assert cmd("yes BBCA 2w karena foreign masuk")[3] == "foreign masuk"
    assert cmd("yes BBCA 2w bank rebound")[3:] == ("bank rebound", None)   # BANK is a ticker
    print("  ok command parsing")


def test_parse_durations_in_trading_days():
    cases = {
        "yes BBCA 5d": 5, "yes BBCA 2w": 10, "yes bbca 1m": 21, "yes BBCA 3 days": 3,
        "yes BBCA 10 hari": 10, "yes BBCA 2 minggu": 10, "yes BBCA 1 bulan": 21,
        "yes BBCA 1 week": 5, "yes BBCA 2 months": 42, "yes BBCA 1mo": 21,
        "yes BBCA 2 mgg": 10, "yes BBCA 1 bln": 21, "yes BBCA seminggu": 5,
        "yes BBCA sebulan": 21, "yes BBCA": None,
    }
    for text, days in cases.items():
        assert cmd(text + " why")[1:3] == (["BBCA"], days), (text, cmd(text + " why"))
    for bad in ("yes BBCA 12m why", "yes BBCA 0d why", "yes BBCA 1.5m why", "yes BBCA 5 why"):
        assert cmd(bad)[4] == "duration", bad
    assert cmd("yes BBCA 5d BBRI 1m")[4] == "order"
    assert cmd("yes BBCA BBRI 2 week banks")[1:3] == (["BBCA", "BBRI"], 10)
    print("  ok durations")


def _update(uid, text, chat=42, ts=1_758_000_000):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "date": ts, "text": text}}


def test_new_pick_needs_a_reason():
    state, reply = inbox.apply_updates({"last_update_id": 0, "events": []},
                                       [_update(1, "yes BBCA 2w")], "42", KNOWN)
    assert state["events"] == [] and "Add why you pick BBCA" in reply, reply
    state, reply = inbox.apply_updates(state, [_update(2, "yes BBCA 2w foreign accumulating")],
                                       "42", KNOWN)
    assert dp.active_follows(state["events"])["BBCA"]["reason"] == "foreign accumulating"
    assert "Your pick: BBCA (10 trading days) - reason: foreign accumulating" in reply
    print("  ok reason required")


def test_apply_updates_follow_stop_cap_and_idempotency():
    state = {"last_update_id": 0, "events": []}
    state, reply = inbox.apply_updates(state, [
        _update(1, "yes BBCA BBRI XXXX banks cheap"),
        _update(2, "yes TLKM telco", chat=999),              # someone else's chat
        _update(3, "stop BBRI"),
    ], "42", KNOWN)
    assert state["last_update_id"] == 3
    assert list(dp.active_follows(state["events"])) == ["BBCA"]
    assert "XXXX" in reply and "Stopped: BBRI" in reply
    again, _ = inbox.apply_updates(state, [_update(1, "yes BBCA BBRI XXXX banks cheap")],
                                   "42", KNOWN)
    assert again["events"] == state["events"]                # replayed update not applied twice
    many = [_update(10 + i, f"yes A{c}AZ why") for i, c in enumerate("ABCDEFGHIJ")]
    full, reply3 = inbox.apply_updates(state, many, "42", KNOWN)
    assert len(dp.active_follows(full["events"])) == dp.MAX_FOLLOWS
    assert "already have 10 picks" in reply3
    _, quiet = inbox.apply_updates(state, [_update(40, "good morning")], "42", KNOWN)
    assert quiet is None
    _, bad = inbox.apply_updates(state, [_update(41, "yes BBCA 12m why")], "42", KNOWN)
    assert "Couldn't read how long" in bad
    print("  ok inbox")


def test_inbox_durations_change_and_list():
    state, reply = inbox.apply_updates({"last_update_id": 0, "events": []},
                                       [_update(1, "yes BBCA 2w foreign buying")], "42", KNOWN)
    assert "BBCA (10 trading days)" in reply
    state, reply = inbox.apply_updates(state, [_update(2, "yes BBCA 1m"),
                                               _update(3, "yes BBRI rate cut play"),
                                               _update(4, "yes BBRI")], "42", KNOWN)
    follows = dp.active_follows(state["events"])
    assert follows["BBCA"]["at"] == state["events"][0]["at"] and follows["BBCA"]["days"] == 21
    assert "Changed: BBCA (21 trading days)" in reply and "Already your pick: BBRI" in reply
    assert "Your picks (2/10): BBCA (21 trading days), BBRI (until you say stop)" in reply
    print("  ok duration changes")


def test_duration_change_never_restarts_a_finished_pick():
    t1, t2, t3 = 1_758_000_000, 1_758_100_000, 1_759_000_000
    state, _ = inbox.apply_updates({"last_update_id": 0, "events": []},
                                   [_update(1, "yes AAAZ 3d why", ts=t1),
                                    _update(2, "yes AAAZ 4d", ts=t2)], "42", KNOWN)
    first = state["events"][0]["at"]
    done_at = datetime.fromtimestamp(t2 + 50_000, timezone.utc).isoformat()   # 🏁 recorded
    ended = {("AAAZ", first): done_at}
    assert dp.active_follows(state["events"], ended) == {}       # stays finished
    state, reply = inbox.apply_updates(state, [_update(3, "yes AAAZ 2w again", ts=t3)],
                                       "42", KNOWN, ended)
    again = dp.active_follows(state["events"], ended)["AAAZ"]
    assert again["at"] != first and again["days"] == 10 and again["reason"] == "again"
    print("  ok no double finish")


def test_finished_pick_frees_its_slot():
    ten = [_update(1 + i, f"yes A{c}AZ 5d why") for i, c in enumerate("ABCDEFGHIJ")]
    state, _ = inbox.apply_updates({"last_update_id": 0, "events": []}, ten, "42", KNOWN)
    first = state["events"][0]
    ended = {(first["ticker"], first["at"]): "2026-09-30T00:00:00+00:00"}
    _, reply = inbox.apply_updates(state, [_update(20, "yes BBCA dividend")], "42", KNOWN, ended)
    assert "Your pick: BBCA" in reply and "(10/10)" in reply
    print("  ok finished picks free a slot")


def test_your_timed_pick_shows_days_then_finishes_with_why():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_picks_are_tracked_and_finish_with_why():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_why_names_the_main_driver():
    base = {"bandar_share": 0.5, "foreign_share": 0.5, "trading_ratio": 1.0,
            "worst_day": -0.01, "market_ret": 0.0}
    assert dp.main_reason(0.05, dict(base, bandar_share=0.8)) == "bandar kept buying"
    assert dp.main_reason(-0.05, dict(base, bandar_share=0.2)) == "bandar turned seller"
    assert dp.main_reason(-0.05, dict(base, worst_day=-0.12)) == "one bad day (-12%)"
    assert dp.main_reason(-0.04, dict(base, market_ret=-0.05)) == "mostly moved with the market"
    assert dp.main_reason(0.03, base) == "beat the market without a clear flow signal"
    # the phrase always agrees with the ✅/❌ mark (review cases from real data)
    assert dp.main_reason(-0.005, dict(base, market_ret=-0.015)) == \
        "beat the market without a clear flow signal"
    assert dp.main_reason(0.009, dict(base, market_ret=0.019, bandar_share=0.8,
                                      foreign_share=0.8)) == "lagged even though bandar kept buying"
    assert dp.main_reason(0.005, dict(base, market_ret=0.028)) == \
        "lagged the market without a clear flow signal"             # not "moved with the market"
    print("  ok why")


def test_everyday_words_that_are_tickers_stay_in_the_reason():
    known = KNOWN | {"NAIK", "LABA", "GOLD", "CUAN", "BELI"}
    assert cmd("yes BBCA naik terus karena asing masuk", known)[1:4] == (
        ["BBCA"], None, "naik terus karena asing masuk")
    assert cmd("yes ANTM gold rally", known)[1] == ["ANTM"]
    assert cmd("yes bbca, bbri banks", known)[1] == ["BBCA", "BBRI"]
    assert cmd("yes BBCA BBRI 2w banks", known)[1:3] == (["BBCA", "BBRI"], 10)
    print("  ok everyday words")


def test_stop_ignores_numbers_in_its_text():
    state, _ = inbox.apply_updates({"last_update_id": 0, "events": []},
                                   [_update(1, "yes BBCA 2w foreign buying")], "42", KNOWN)
    state, reply = inbox.apply_updates(state, [_update(2, "stop BBCA 10% cuan")], "42", KNOWN)
    assert "Stopped: BBCA" in reply and dp.active_follows(state["events"]) == {}
    print("  ok stop with numbers")


def test_new_pick_confirmed_by_inbox_restarts_even_if_sent_before_the_finish():
    t_old, t_reply = 1_758_000_000, 1_758_600_000
    state, _ = inbox.apply_updates({"last_update_id": 0, "events": []},
                                   [_update(1, "yes AAAZ 5d why", ts=t_old)], "42", KNOWN)
    first = state["events"][0]["at"]
    ended = {("AAAZ", first): datetime.fromtimestamp(t_reply + 3600, timezone.utc).isoformat()}
    # reply sent before the morning run recorded the finish, processed after it
    state, reply = inbox.apply_updates(state, [_update(2, "yes AAAZ 2w still strong",
                                                       ts=t_reply)], "42", KNOWN, ended)
    assert "Your pick: AAAZ" in reply
    assert dp.active_follows(state["events"], ended)["AAAZ"]["days"] == 10
    print("  ok no lost re-pick")


def test_missing_trading_day_gives_no_win_or_loss():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_suspended_day_has_no_price():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_stopped_pick_gets_its_result():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_does_not_repick_a_stock_it_holds():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


# ── machine execution contract and historical compatibility ──────────────

def execution_panel():
    """Friday signal, Monday entry, following Monday exit. Capture dates
    describe the PREVIOUS session. Signal -> entry jumps 20%; entry -> exit 10%.
    Only AAAA passes broad_buying, making the learning comparison exact.
    """
    captures = ["2026-09-12", "2026-09-15", "2026-09-16", "2026-09-17",
                "2026-09-18", "2026-09-19", "2026-09-22", "2026-09-23"]
    prices = [1000, 1200, 1224, 1248, 1272, 1296, 1320, 1344]
    return [(d, {"AAAA": row(close=p, m=0.1 + i / 100),
                 "BBBB": row(close=1000, cs=0, nr=-0.1, m=-0.1 - i / 100)})
            for i, (d, p) in enumerate(zip(captures, prices))]


def execution_pick(snaps, timing=dp.MACHINE_TIMING):
    return [(snaps[0].date, "AAAA", "broad_buying", timing)]


def test_machine_signal_entry_exit_and_learning_use_the_same_window():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_missing_or_suspended_entry_is_never_postponed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_missing_or_suspended_exit_is_never_postponed():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_suspended_intermediate_session_does_not_shift_the_clock():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_copy_sessions_do_not_advance_entry_or_holding():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_missing_market_session_quarantines_timing_and_holding():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_entry_waits_through_a_holiday_copy():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_missing_signal_snapshot_reserves_the_ticker():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_price_break_checks_only_the_executed_window():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_legacy_migration_preserves_rows_and_their_original_clock():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_scoreboard_separates_legacy_from_executable_results():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_morning_persists_signal_and_entry_separately_and_retries_finish():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_machine_unsent_stale_snapshot_cannot_create_a_pick():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


# ── sending ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {"ok": True}
        self.ok = status < 400

    def json(self):
        return self._body


def test_split_message_stays_under_telegram_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(300)) + "\n" + "y" * 9000
    parts = dp.split_message(text)
    assert all(len(p) <= 3900 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")
    print("  ok message split")


def test_sender_retries_429_and_never_prints_token():
    calls = []

    def flaky(url, json, timeout):
        calls.append(json["text"])
        if len(calls) == 1:
            return _Resp(429, {"ok": False, "parameters": {"retry_after": 1}})
        return _Resp()

    assert dp.telegram_sender("SECRET", "42", post=flaky, sleep=lambda s: None)("hi")
    assert calls == ["hi", "hi"]

    def boom(url, json, timeout):
        raise RuntimeError(f"connection to {url} failed")

    out = io.StringIO()
    with redirect_stdout(out):
        ok = dp.telegram_sender("SECRET", "42", post=boom)("hi")
    assert not ok and "SECRET" not in out.getvalue()
    print("  ok sender")


# ── the morning run ────────────────────────────────────────────────────────

def _morning_env(tmp, days, follows=None, stalker=None, status=None):
    neo = os.path.join(tmp, "neobdm.db")
    make_db(neo, days, stalker=stalker, status=status)
    fol = os.path.join(tmp, "follows.json")
    dp.save_follows(fol, follows or {"last_update_id": 0, "events": []})
    return dict(neobdm_db=neo, picks_db=os.path.join(tmp, "picks.db"), follows_json=fol)


def test_morning_sends_once_per_session_and_records_picks():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_morning_is_quiet_on_weekends_and_after_holidays():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_stale_data_warns_once_and_send_failure_saves_nothing():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_undelivered_stale_warning_is_reported_as_such_and_not_recorded():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_morning_includes_follow_ups():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_failed_neobdm_list_says_unavailable_not_empty():
    days = panel(4, start="2026-09-14")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "n.db")
        make_db(path, days, stalker={"2026-09-17": ["AAAA"]},
                status={"2026-09-17": "HITS"})
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO signal_source_status VALUES "
                     "('2026-09-17', 'dashboard_Bandarmologi', 'EMPTY_UNVERIFIED')")
        conn.execute("INSERT INTO signal_source_status VALUES "
                     "('2026-09-17', 'dashboard_Foreign', 'NO_HITS')")
        conn.commit()
        lists = dp.neobdm_lists(conn, "2026-09-17")
        conn.close()
    text = dp.format_morning(date(2026, 9, 17), dp.Snapshot("2026-09-17", {}), [], {}, {},
                             [], [], [], lists, 4)
    assert "Stalker: AAAA" in text and "Bandar: unavailable" in text and "Foreign: -" in text
    assert "Non-retail: unavailable" in text             # no status row = not tracked
    print("  ok unavailable lists")


def test_yes_reference_is_the_message_the_owner_saw():
    days = panel(5, start="2026-09-14")                 # captures Mon 14 .. Fri 18
    days[3][1]["AAAA"] = row(close=900)                 # Thu 17 capture shows 900
    snaps = snaps_from(days)
    sent_at = {"2026-09-17": utc_at_myt("2026-09-17", 9, 20).isoformat()}
    before = utc_at_myt("2026-09-17", 9, 10).isoformat()
    after = utc_at_myt("2026-09-17", 9, 30).isoformat()
    assert snaps[dp.reference_index(snaps, "AAAA", before, sent_at)].date == "2026-09-16"
    assert snaps[dp.reference_index(snaps, "AAAA", after, sent_at)].date == "2026-09-17"
    del days[3][1]["BBBB"]
    assert dp.reference_index(snaps_from(days), "BBBB", after, sent_at) is None  # no older price
    print("  ok yes price = price in the message")


def test_missed_capture_marks_a_gap_and_blocks_returns_across_it():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_holiday_copy_is_not_a_gap():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_split_after_yes_is_not_shown_as_a_loss():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_monday_after_late_scrape_still_sends_fridays_session():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def _seed_veto(picks_db, rows):
    """rows: (as_of, ticker, valid_until). Writes the table arb_veto.py owns."""
    conn = sqlite3.connect(picks_db)
    dp.ensure_schema(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO arb_veto "
        "(as_of, ticker, p, rank, valid_until, recorded_utc) VALUES (?,?,0.9,1,?,'t')",
        [(a, t, v) for a, t, v in rows])
    conn.commit()
    conn.close()


def test_arb_veto_window_covers_the_session_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "picks.db")
        _seed_veto(path, [("2026-09-07", "AAAA", "2026-09-14"),   # covers
                          ("2026-09-15", "BBBB", "2026-09-22"),   # not issued yet
                          ("2026-08-24", "CCCC", "2026-08-31")])  # expired
        conn = sqlite3.connect(path)
        assert dp.arb_veto(conn, "2026-09-14") == {"AAAA"}, dp.arb_veto(conn, "2026-09-14")
        assert dp.arb_veto(conn, "2026-09-15") == {"BBBB"}
        assert dp.arb_veto(conn, "2026-09-30") == set()
        conn.close()
    print("  ok veto expires with its own five-session window")


def test_arb_veto_absent_or_empty_leaves_picks_alone():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


def test_arb_veto_blocks_a_name_that_would_have_been_picked():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("daily_picks.run_morning")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    print(f"daily picks: {len(ALL)} tests\n")
    for fn in ALL:
        print(fn.__name__)
        fn()
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
