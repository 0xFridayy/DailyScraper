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
from contextlib import redirect_stdout
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
    fri = {"AAAA": row(1000), "BBBB": row(2000)}
    sat_copy = {k: dict(v) for k, v in fri.items()}
    mon = {"AAAA": row(1010), "BBBB": row(1990)}
    snaps = []
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "n.db")
        make_db(path, [("2026-09-11", fri), ("2026-09-12", sat_copy),
                       ("2026-09-13", sat_copy), ("2026-09-15", mon)])
        conn = sqlite3.connect(path)
        snaps = dp.load_snapshots(conn)
        conn.close()
    assert [s.date for s in snaps] == ["2026-09-11", "2026-09-15"], snaps
    print("  ok copies dropped")


def test_session_label_is_previous_weekday():
    assert dp.session_label("2026-09-18") == "Thu 17 Sep"
    assert dp.session_label("2026-09-12") == "Fri 11 Sep"   # Saturday capture
    assert dp.session_label("2026-09-14") == "Fri 11 Sep"   # Monday capture
    print("  ok session labels")


# ── tags, filters and ranking ──────────────────────────────────────────────

def test_tags_never_read_later_snapshots():
    days = panel(10)
    snaps = snaps_from(days)
    before = dp.tag_snapshot(snaps, 5, {"AAAA"})
    later = [(d, {t: dict(r, close=r["close"] * 3, m_dn_0=-1, tval=999) for t, r in rows.items()})
             for d, rows in days[6:]]
    after = dp.tag_snapshot(snaps_from(days[:6] + later), 5, {"AAAA"})
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
    last["CCCC"] = row(cs=1, nr=0.1, f=0.1, m=0.1)            # inst + bandar_3days
    last["DDDD"] = row(cs=0, nr=-0.1, f=-0.1, m=-0.1)         # none
    tagged = dp.tag_snapshot(snaps_from(days), 5, {"CCCC"})   # CCCC also stalker
    picks = dp.rank_picks(tagged, {})
    assert [t for t, _ in picks] == ["CCCC", "BBBB"], picks
    picks = dp.rank_picks(tagged, {"stalker": 0.25, "bandar_3days": 0.25, "inst_foreign": 1,
                                   "broad_buying": 2})
    assert [t for t, _ in picks] == ["BBBB", "CCCC"], picks
    print("  ok ranking")


def test_zero_clean_score_is_a_real_tiebreak_value():
    days = panel(4)
    last = days[-1][1]
    last["AAAA"] = row(cs=0, nr=0.1, f=0.1)
    last["BBBB"] = row(cs=None, nr=0.1, f=0.1)
    tagged = dp.tag_snapshot(snaps_from(days), 3)
    ranked = dp.rank_picks(tagged, {}, n=10)
    names = [t for t, _ in ranked]
    assert names.index("AAAA") < names.index("BBBB"), names
    print("  ok zero is not missing")


# ── outcomes and learning ──────────────────────────────────────────────────

def test_outcome_waits_for_exit_and_enters_next_session():
    drift = {"AAAA": 0.02, "BBBB": 0.0, "CCCC": 0.0, "DDDD": -0.02}
    days = panel(12, drift=drift)
    snaps = snaps_from(days)
    assert dp.forward_excess(snaps[:7], 1, "AAAA") is None     # exit (index 7) not yet known
    ret, excess = dp.forward_excess(snaps[:8], 1, "AAAA")
    assert abs(ret - (1.02 ** 5 - 1)) < 1e-9                    # entry index 2, exit index 7
    avg = ((1.02 ** 5 - 1) + 0 + 0 + (0.98 ** 5 - 1)) / 4
    assert abs(excess - (ret - avg)) < 1e-9
    print("  ok outcome timing")


def test_learning_is_slow_capped_and_uses_only_matured_sessions():
    drift = {"AAAA": 0.03, "BBBB": 0.0, "CCCC": 0.0, "DDDD": 0.0}
    days = panel(40, drift=drift)
    for _, rows in days:
        rows["AAAA"]["clean_score"] = 5          # only AAAA gets broad_buying
        for t in "BCD":
            rows[t * 4]["clean_score"] = 0
            rows[t * 4]["nr_dn_0"] = -0.1
    snaps = snaps_from(days)
    few = dp.learn_weights(snaps[:9], {}, set())
    assert few["broad_buying"]["sessions"] == 3                  # 9 - 1 - 5
    many = dp.learn_weights(snaps, {}, set())
    assert many["broad_buying"]["sessions"] == 34
    assert few["broad_buying"]["weight"] < many["broad_buying"]["weight"] <= 2.0
    assert 0.25 <= many["inst_foreign"]["weight"] <= 2.0
    changed = snaps[:9] + snaps_from([(d, {t: dict(r, close=1) for t, r in rows.items()})
                                      for d, rows in days[9:]])
    assert dp.learn_weights(changed[:9], {}, set()) == few       # later data never used
    print("  ok learning")


def test_stalker_down_days_are_skipped_in_learning():
    days = panel(12)
    snaps = snaps_from(days)
    flagged = {d: {"AAAA", "BBBB"} for d, _ in days}
    all_days = dp.learn_weights(snaps, flagged, set())
    some_down = dp.learn_weights(snaps, flagged, {days[0][0], days[1][0]})
    assert some_down["stalker"]["sessions"] == all_days["stalker"]["sessions"] - 2
    print("  ok unknown source is not 'no'")


def test_corporate_action_hint_and_limits():
    assert dp.corporate_action_hint(2710, 534) == "looks like a 1:5 stock split"
    assert dp.corporate_action_hint(100, 200) == "looks like a 2:1 reverse split"
    assert dp.corporate_action_hint(1000, 850) is None             # a normal ARB day
    assert dp.corporate_action_hint(1000, 1260) is None            # ARA 25% + slack
    assert dp.corporate_action_hint(1000, 1400) == "moved past the normal daily limit"
    assert dp.corporate_action_hint(None, 500) is None
    try:
        import price_audit
    except Exception as e:
        print(f"  SKIP band cross-check ({type(e).__name__})")
    else:
        for p in (50, 199, 1000, 5000, 5001, 20000):
            assert dp.limit_up(p) == price_audit.ara_bound(p), p
    assert dp.limit_up(200) == 0.35                      # IDX: Rp50-200 is the 35% tier
    assert dp.corporate_action_hint(200, 270) is None    # a legal ARA from Rp200
    print("  ok corporate actions")


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
    days = panel(12, start="2026-08-31")
    for i, (_, rows) in enumerate(days):
        rows["AAAA"] = row(close=1000 + 10 * i, m=0.1, f=-0.1, tval=10.0,
                           top='["BK", "RF", "CC", "AK", "ZP"]')
    days[-1][1]["AAAA"] = row(close=1200, m=0.1, f=-0.1, tval=40.0, pct5=0.15,
                              top='["BK", "XL", "YP", "AK", "ZP"]')
    since = utc_at_myt(days[4][0], 11).isoformat()
    lines = dp.follow_lines(snaps_from(days), "AAAA", since)
    text = "\n".join(lines)
    assert "since yes" in lines[0] and "+15.4%" in lines[0], lines[0]   # 1040 -> 1200
    assert "Bandar bought on 3 of the last 3 days" in text
    assert "Foreign bought on 0 of the last 3 days" in text
    assert "BK still in today's top 5" in text
    assert "Trading spike" in text
    assert "Ran up 15% in 5 days" in text
    print("  ok follow-up lines")


def test_follow_of_unknown_stock_says_so():
    days = panel(4)
    lines = dp.follow_lines(snaps_from(days), "ZZZZ", utc_at_myt(days[0][0]).isoformat())
    assert lines == ["ZZZZ: no NeoBDM data today (not in the liquid list)."], lines
    print("  ok unknown follow")


# ── telegram inbox ─────────────────────────────────────────────────────────

def test_parse_command_variants():
    assert inbox.parse_command("yes bbca") == ("yes", ["BBCA"], None)
    assert inbox.parse_command("/YES@my_bot BBCA, bbri") == ("yes", ["BBCA", "BBRI"], None)
    assert inbox.parse_command("Stop BBCA") == ("stop", ["BBCA"], None)
    assert inbox.parse_command("list") == ("list", [], None)
    assert inbox.parse_command("yessir BBCA") is None
    assert inbox.parse_command("hello") is None
    assert inbox.parse_command(None) is None
    assert inbox.parse_command("yes BBCAX") == ("yes", [], None)
    print("  ok command parsing")


def test_parse_durations_in_trading_days():
    cases = {
        "yes BBCA 5d": 5, "yes BBCA 2w": 10, "yes bbca 1m": 21, "yes BBCA 3 days": 3,
        "yes BBCA 10 hari": 10, "yes BBCA 2 minggu": 10, "yes BBCA 1 bulan": 21,
        "yes BBCA 1 week": 5, "yes BBCA 2 months": 42, "yes BBCA 1mo": 21,
        "yes BBCA": None, "yes BBCA 0d": None, "yes BBCA 99m": None,   # 0 and >1 year ignored
    }
    for text, days in cases.items():
        action, tickers, got = inbox.parse_command(text)
        assert (action, tickers, got) == ("yes", ["BBCA"], days), (text, tickers, got)
    # the unit words are never read as tickers, and one duration covers all tickers
    assert inbox.parse_command("yes BBCA BBRI 2 week") == ("yes", ["BBCA", "BBRI"], 10)
    assert inbox.parse_command("yes 10 hari TLKM") == ("yes", ["TLKM"], 10)
    print("  ok durations")


def _update(uid, text, chat=42, ts=1_758_000_000):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "date": ts, "text": text}}


def test_apply_updates_follow_stop_cap_and_idempotency():
    known = {"BBCA", "BBRI", "TLKM"} | {f"A{c}AZ" for c in "ABCDEFGHIJ"}
    state = {"last_update_id": 0, "events": []}
    state, reply = inbox.apply_updates(state, [
        _update(1, "yes BBCA BBRI XXXX"),
        _update(2, "yes TLKM", chat=999),          # someone else's chat
        _update(3, "stop BBRI"),
    ], "42", known)
    assert state["last_update_id"] == 3
    assert dp.active_follows(state["events"]) == {
        "BBCA": {"at": state["events"][0]["at"], "days": None}}
    assert "Following: BBCA (until you say stop), BBRI (until you say stop)" in reply
    assert "XXXX" in reply and "Stopped: BBRI" in reply
    again, reply2 = inbox.apply_updates(state, [_update(1, "yes BBCA BBRI XXXX")], "42", known)
    assert again["events"] == state["events"]     # replayed update not applied twice
    many = [_update(10 + i, f"yes A{c}AZ") for i, c in enumerate("ABCDEFGHIJ")]
    full, reply3 = inbox.apply_updates(state, many, "42", known)
    assert len(dp.active_follows(full["events"])) == dp.MAX_FOLLOWS
    assert "Already following 10" in reply3
    _, quiet = inbox.apply_updates(state, [_update(40, "good morning")], "42", known)
    assert quiet is None
    print("  ok inbox")


def test_inbox_durations_change_and_list():
    known = {"BBCA", "BBRI"}
    state, reply = inbox.apply_updates({"last_update_id": 0, "events": []},
                                       [_update(1, "yes BBCA 2w")], "42", known)
    assert "Following: BBCA (10 trading days)" in reply
    state, reply = inbox.apply_updates(state, [_update(2, "yes BBCA 1m"),
                                               _update(3, "yes BBRI"),
                                               _update(4, "yes BBRI")], "42", known)
    follows = dp.active_follows(state["events"])
    assert follows["BBCA"] == {"at": state["events"][0]["at"], "days": 21}  # same start
    assert "Changed: BBCA (21 trading days)" in reply and "Already following: BBRI" in reply
    assert "Following (2/10): BBCA (21 trading days), BBRI (until you say stop)" in reply
    print("  ok duration changes")


def test_finished_follow_frees_its_slot_and_can_restart():
    known = {f"A{c}AZ" for c in "ABCDEFGHIJ"} | {"BBCA"}
    ten = [_update(1 + i, f"yes A{c}AZ 5d") for i, c in enumerate("ABCDEFGHIJ")]
    state, _ = inbox.apply_updates({"last_update_id": 0, "events": []}, ten, "42", known)
    first = state["events"][0]
    ended = frozenset({(first["ticker"], first["at"])})              # AAAZ's 5 days are up
    state, reply = inbox.apply_updates(state, [_update(20, "yes BBCA")], "42", known, ended)
    assert "Following: BBCA" in reply and "(10/10)" in reply        # the ended one freed a slot
    state, _ = inbox.apply_updates(state, [_update(21, "stop BBCA"),
                                           _update(22, "yes AAAZ", ts=1_759_000_000)],
                                   "42", known, ended)
    again = dp.active_follows(state["events"], ended)["AAAZ"]
    assert again["at"] != first["at"] and again["days"] is None     # a fresh follow
    print("  ok finished follows free a slot")


def test_timed_follow_shows_days_then_finishes_once_and_is_recorded():
    drift = {"AAAA": 0.01, "BBBB": 0.0, "CCCC": 0.0, "DDDD": 0.0}
    days = panel(10, start="2026-09-01", drift=drift)          # Tue 01 .. Mon 14
    yes_at = utc_at_myt(days[2][0], 11).isoformat()             # yes on the Thu 03 snapshot
    follows = {"last_update_id": 1, "events": [
        {"update_id": 1, "at": yes_at, "action": "yes", "ticker": "AAAA", "days": 3}]}
    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days[:4], follows=follows)
        for n in (4, 5, 6):                                      # three mornings in a row
            env["neobdm_db"] = os.path.join(tmp, f"n{n}.db")
            make_db(env["neobdm_db"], days[:n])
            status, text = dp.run_morning(utc_at_myt(days[n - 1][0]), lambda t: True, **env)
            assert status == "sent", status
            texts.append(text)
        conn = sqlite3.connect(env["picks_db"])
        rows = conn.execute("SELECT ticker, days, ended_snapshot, ret FROM follow_results").fetchall()
        conn.close()
    assert "day 1 of 3" in texts[0] and "day 2 of 3" in texts[1], texts[:2]
    assert "🏁 AAAA: your 3-day follow is done: +3.0% since yes" in texts[2], texts[2]
    assert rows == [("AAAA", 3, days[5][0], rows[0][3])] and abs(rows[0][3] - (1.01 ** 3 - 1)) < 1e-9
    with tempfile.TemporaryDirectory() as tmp:                   # next day: gone
        env2 = _morning_env(tmp, days[:7], follows=follows)
        conn = sqlite3.connect(env2["picks_db"])
        dp.ensure_schema(conn)
        conn.execute("INSERT INTO follow_results VALUES ('AAAA', ?, 3, ?, 0.03, 0.0, 'x')",
                     (yes_at, days[5][0]))
        conn.commit()
        conn.close()
        status, text = dp.run_morning(utc_at_myt(days[6][0]), lambda t: True, **env2)
    assert status == "sent" and "👀 Your stocks" not in text and "🏁" not in text, text
    print("  ok timed follow")


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

def _morning_env(tmp, days, follows=None, stalker=None):
    neo = os.path.join(tmp, "neobdm.db")
    make_db(neo, days, stalker=stalker)
    fol = os.path.join(tmp, "follows.json")
    dp.save_follows(fol, follows or {"last_update_id": 0, "events": []})
    return dict(neobdm_db=neo, picks_db=os.path.join(tmp, "picks.db"), follows_json=fol)


def test_morning_sends_once_per_session_and_records_picks():
    days = panel(12, start="2026-08-31")                  # ends Tue 2026-09-15
    sent = []
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days)
        today = days[-1][0]
        status, text = dp.run_morning(utc_at_myt(today), lambda t: sent.append(t) or True, **env)
        assert status == "sent", status
        assert len(sent) == 1 and "Morning picks" in sent[0] and "Weekly scoreboard" in sent[0]
        status, _ = dp.run_morning(utc_at_myt(today, 11), lambda t: sent.append(t) or True, **env)
        assert status == "already_sent" and len(sent) == 1
        conn = sqlite3.connect(env["picks_db"])
        n_picks = conn.execute("SELECT count(*) FROM picks").fetchone()[0]
        kinds = sorted(k for (k,) in conn.execute("SELECT kind FROM sent_messages"))
        conn.close()
        assert n_picks == 4 and kinds == ["morning", "scoreboard"], (n_picks, kinds)
    print("  ok once per session")


def test_morning_is_quiet_on_weekends_and_after_holidays():
    days = panel(8, start="2026-09-07")                   # Mon 09-07 .. Wed 09-16
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days)
        sent = []
        send = lambda t: sent.append(t) or True
        assert dp.run_morning(utc_at_myt("2026-09-12"), send, **env)[0] == "weekend"
        assert dp.run_morning(utc_at_myt(days[-1][0]), send, **env)[0] == "sent"
        # next morning's capture is a copy (the market was closed): stay quiet
        copy_days = days + [("2026-09-17", {t: dict(r) for t, r in days[-1][1].items()})]
        env2 = dict(env, neobdm_db=os.path.join(tmp, "neobdm2.db"))
        make_db(env2["neobdm_db"], copy_days)
        assert dp.run_morning(utc_at_myt("2026-09-17"), send, **env2)[0] == "already_sent"
        assert len(sent) == 1
    print("  ok quiet days")


def test_stale_data_warns_once_and_send_failure_saves_nothing():
    days = panel(8, start="2026-09-07")
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days)
        status, _ = dp.run_morning(utc_at_myt(days[-1][0]), lambda t: False, **env)
        assert status == "send_failed"
        conn = sqlite3.connect(env["picks_db"])
        assert conn.execute("SELECT count(*) FROM picks").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM tag_weights").fetchone()[0] == 0
        conn.close()
        sent = []
        send = lambda t: sent.append(t) or True
        assert dp.run_morning(utc_at_myt(days[-1][0]), send, **env)[0] == "sent"
        later = utc_at_myt("2026-09-17")                  # no capture for this day
        assert dp.run_morning(later, send, **env)[0] == "stale"
        assert dp.run_morning(later, send, **env)[0] == "stale_already_warned"
        assert len(sent) == 2 and "No fresh NeoBDM data" in sent[1]
    print("  ok stale and failed sends")


def test_morning_includes_follow_ups():
    days = panel(10, start="2026-09-01")
    follows = {"last_update_id": 5, "events": [
        {"update_id": 5, "at": utc_at_myt(days[3][0], 11).isoformat(), "action": "yes",
         "ticker": "BBBB"}]}
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days, follows=follows)
        status, text = dp.run_morning(utc_at_myt(days[-1][0]), lambda t: True, **env)
    assert status == "sent" and "👀 Your stocks" in text and "BBBB" in text, text
    print("  ok follow-ups in the message")


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
                             [], lists, 4)
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
    days = panel(10, start="2026-09-07")                # Mon 07 .. Fri 18
    del days[2]                                          # Wed 09 capture missing
    snaps = snaps_from([])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "n.db")
        make_db(path, days)
        conn = sqlite3.connect(path)
        snaps = dp.load_snapshots(conn)
        conn.close()
    gaps = [s.date for s in snaps if s.gap_before]
    assert gaps == ["2026-09-10"], gaps
    assert dp.session_returns(snaps, 1, 3) == {}         # window crosses the lost session
    assert dp.session_returns(snaps, 2, 4) != {}
    print("  ok lost session detected")


def test_holiday_copy_is_not_a_gap():
    days = panel(4, start="2026-09-14")                 # captures Mon 14 .. Thu 17
    wed = days[2][1]
    days = days[:3] + [("2026-09-17", {t: dict(r) for t, r in wed.items()}),  # copy: Wed holiday
                       ("2026-09-18", days[3][1])]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "n.db")
        make_db(path, days)
        conn = sqlite3.connect(path)
        snaps = dp.load_snapshots(conn)
        conn.close()
    assert not any(s.gap_before for s in snaps), [(s.date, s.gap_before) for s in snaps]
    print("  ok holiday is not a gap")


def test_split_after_yes_is_not_shown_as_a_loss():
    days = panel(8, start="2026-09-07")
    for d, rows in days[4:]:
        rows["AAAA"] = row(close=200)                   # 1000 -> 200 (1:5) after the yes
    lines = dp.follow_lines(snaps_from(days), "AAAA", utc_at_myt(days[2][0], 11).isoformat())
    assert "not comparable" in lines[0] and "1:5" in lines[0] and "%" not in lines[0], lines[0]
    print("  ok split not shown as a loss")


def test_monday_after_late_scrape_still_sends_fridays_session():
    days = panel(5, start="2026-09-07")                 # Mon 07 .. Fri 11 captures
    days.append(("2026-09-12", {t: row(close=1010) for t in ("AAAA", "BBBB", "CCCC", "DDDD")}))
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days[:5])               # what existed on Friday
        sent = []
        send = lambda t: sent.append(t) or True
        assert dp.run_morning(utc_at_myt("2026-09-11"), send, **env)[0] == "sent"
        env["neobdm_db"] = os.path.join(tmp, "monday.db")
        make_db(env["neobdm_db"], days)                 # + Saturday's capture, no Monday rows
        status, text = dp.run_morning(utc_at_myt("2026-09-14", 10, 30), send, **env)
    assert status == "sent" and "From the Fri 11 Sep close" in text, (status, text)
    print("  ok Monday late scrape")


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
