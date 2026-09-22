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
    few = dp.learn_weights(snaps[:9])
    assert few["broad_buying"]["sessions"] == 3                  # 9 - 1 - 5
    many = dp.learn_weights(snaps)
    assert many["broad_buying"]["sessions"] == 34
    assert few["broad_buying"]["weight"] < many["broad_buying"]["weight"] <= 2.0
    assert 0.25 <= many["inst_foreign"]["weight"] <= 2.0
    changed = snaps[:9] + snaps_from([(d, {t: dict(r, close=1) for t, r in rows.items()})
                                      for d, rows in days[9:]])
    assert dp.learn_weights(changed[:9]) == few       # later data never used
    print("  ok learning")


def test_stalker_flag_is_shown_but_not_a_check():
    # Retired: a year of full broker data showed no edge for the top-3
    # retail-sell flag. It stays in the NeoBDM lists line as information.
    days = panel(6, start="2026-09-07")
    last = days[-1][1]
    last["AAAA"] = row(cs=5, nr=0.1, f=0.1, m=-0.1)            # broad + inst = 2 checks
    for t in ("BBBB", "CCCC", "DDDD"):
        last[t] = row(cs=0, nr=-0.1, f=-0.1, m=-0.1)
    capture = days[-1][0]
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days, stalker={capture: ["AAAA"]}, status={capture: "HITS"})
        status, text = dp.run_morning(utc_at_myt(capture), lambda t: True, preview=True, **env)
    assert "stalker" not in dp.TAG_ORDER and "stalker" not in dp.learn_weights(snaps_from(days))
    assert status == "preview" and "No strong setup today" in text, text
    assert "Stalker: AAAA" in text, text
    print("  ok stalker is information only")


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
    drift = {"AAAA": 0.01, "BBBB": 0.0, "CCCC": 0.0, "DDDD": 0.0}
    days = panel(10, start="2026-09-01", drift=drift)          # Tue 01 .. Mon 14
    yes_at = utc_at_myt(days[2][0], 11).isoformat()             # yes on the Thu 03 snapshot
    follows = {"last_update_id": 1, "events": [
        {"update_id": 1, "at": yes_at, "action": "yes", "ticker": "AAAA", "days": 3,
         "reason": "bandar accumulating"}]}
    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days[:4], follows=follows)
        for n in (4, 5, 6, 7):                                   # four mornings in a row
            env["neobdm_db"] = os.path.join(tmp, f"n{n}.db")
            make_db(env["neobdm_db"], days[:n])
            status, text = dp.run_morning(utc_at_myt(days[n - 1][0]), lambda t: True, **env)
            assert status == "sent", status
            texts.append(text)
        conn = sqlite3.connect(env["picks_db"])
        rows = conn.execute("SELECT source, ticker, days, end_snapshot, ret, reason, why, "
                            "bandar_share FROM pick_results WHERE source = 'you'").fetchall()
        conn.close()
    assert "day 1 of 3" in texts[0] and "Your reason: bandar accumulating" in texts[0]
    assert "day 2 of 3" in texts[1]
    assert "✅ AAAA (you, 3 days): +3.0%" in texts[2] and "bandar kept buying" in texts[2], texts[2]
    assert "Your reason: bandar accumulating" in texts[2] and "Bandar bought 3/3 days" in texts[2]
    assert "AAAA (you" not in texts[3]                          # shown once, then gone
    assert len(rows) == 1 and rows[0][:4] == ("you", "AAAA", 3, days[5][0])
    assert abs(rows[0][4] - (1.01 ** 3 - 1)) < 1e-9 and rows[0][5:] == (
        "bandar accumulating", "bandar kept buying", 1.0)
    print("  ok your timed pick")


def test_machine_picks_are_tracked_and_finish_with_why():
    drift = {"AAAA": -0.02, "BBBB": 0.0, "CCCC": 0.0, "DDDD": 0.0}
    days = panel(12, start="2026-09-01", drift=drift)
    for i, (_, rows) in enumerate(days):
        for t in ("BBBB", "CCCC", "DDDD"):
            rows[t]["clean_score"] = 0                           # only AAAA passes 3 checks
        if i >= 3:
            rows["AAAA"]["m_dn_0"] = -0.1                        # bandar sells after the pick
    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days[:3])
        for n in range(3, 10):
            env["neobdm_db"] = os.path.join(tmp, f"n{n}.db")
            make_db(env["neobdm_db"], days[:n])
            status, text = dp.run_morning(utc_at_myt(days[n - 1][0]), lambda t: True, **env)
            texts.append(text)
        conn = sqlite3.connect(env["picks_db"])
        rows = conn.execute("SELECT ticker, started, ret, why FROM pick_results "
                            "WHERE source = 'machine' ORDER BY started").fetchall()
        conn.close()
    assert "1. AAAA" in texts[0]                                 # picked from the 3rd snapshot
    assert "Machine picks running: AAAA" in texts[1] and "(day 1/5)" in texts[1]
    done = next(t for t in texts if "AAAA (machine, 5 days)" in t)
    assert "❌ AAAA (machine, 5 days)" in done and "bandar turned seller" in done, done
    assert "Picked for:" in done
    assert rows[0][0] == "AAAA" and rows[0][1] == days[2][0] and rows[0][3] == "bandar turned seller"
    print("  ok machine picks tracked")


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
    drift = {"AAAA": 0.005, "BBBB": 0.02, "CCCC": 0.02, "DDDD": 0.02}
    days = panel(10, start="2026-09-07", drift=drift)
    del days[3]                                              # Thu 10 capture missing
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "n.db")
        make_db(path, days)
        conn = sqlite3.connect(path)
        snaps = dp.load_snapshots(conn)
        conn.close()
    lines, result = dp.finish_pick(snaps, "AAAA", 1, 5, "machine")
    assert lines[0].startswith("⚪ AAAA") and "no market comparison" in lines[0], lines
    assert result["market_ret"] is None
    print("  ok gap gives no verdict")


def test_suspended_day_has_no_price():
    # NeoBDM keeps a suspended stock in the list with tval 0 and yesterday's
    # price (seen live: PACK 1-16 Sep, SINI 8 Sep). That price can't be traded.
    drift = {"AAAA": 0.03, "BBBB": 0.0, "CCCC": 0.0, "DDDD": 0.0}
    days = panel(8, start="2026-09-07", drift=drift)
    frozen = days[5][1]["AAAA"]["close"]
    days[6][1]["AAAA"] = row(close=frozen, tval=0.0)
    snaps = snaps_from(days)
    lines, result = dp.finish_pick(snaps, "AAAA", 1, 5, "machine")
    assert lines[0].startswith("⚪ AAAA") and "no trading on the last day" in lines[0], lines
    assert result["ret"] is None
    rets = dp.session_returns(snaps, 1, 6)
    assert "AAAA" not in rets and set(rets) == {"BBBB", "CCCC", "DDDD"}, rets
    follow = dp.follow_lines(snaps[:7], "AAAA", utc_at_myt(days[2][0], 11).isoformat())
    assert follow[0].startswith("AAAA: no trading") and "%" not in follow[0], follow
    print("  ok suspended day is no price")


def test_stopped_pick_gets_its_result():
    drift = {"AAAA": 0.01, "BBBB": 0.0, "CCCC": 0.0, "DDDD": 0.0}
    days = panel(8, start="2026-09-01", drift=drift)
    follows = {"last_update_id": 2, "events": [
        {"update_id": 1, "at": utc_at_myt(days[1][0], 11).isoformat(), "action": "yes",
         "ticker": "AAAA", "days": 10, "reason": "breakout", "new": True},
        {"update_id": 2, "at": utc_at_myt(days[4][0], 11).isoformat(), "action": "stop",
         "ticker": "AAAA"}]}
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days[:6], follows=follows)
        status, text = dp.run_morning(utc_at_myt(days[5][0]), lambda t: True, **env)
        env["neobdm_db"] = os.path.join(tmp, "n7.db")
        make_db(env["neobdm_db"], days[:7])
        _, text2 = dp.run_morning(utc_at_myt(days[6][0]), lambda t: True, **env)
        conn = sqlite3.connect(env["picks_db"])
        rows = conn.execute("SELECT days, ret, reason FROM pick_results WHERE source='you'").fetchall()
        conn.close()
    assert "✅ AAAA (you, stopped after 3 days" in text and "Your reason: breakout" in text, text
    assert "AAAA (you" not in text2                              # once
    assert len(rows) == 1 and rows[0][0] == 3 and abs(rows[0][1] - (1.01 ** 3 - 1)) < 1e-9
    print("  ok stopped picks are scored")


def test_machine_does_not_repick_a_stock_it_holds():
    days = panel(9, start="2026-09-01")
    for _, rows in days:
        for t in ("BBBB", "CCCC", "DDDD"):
            rows[t]["clean_score"] = 0                           # only AAAA qualifies
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days[:3])
        texts = []
        for n in range(3, 10):
            env["neobdm_db"] = os.path.join(tmp, f"n{n}.db")
            make_db(env["neobdm_db"], days[:n])
            texts.append(dp.run_morning(utc_at_myt(days[n - 1][0]), lambda t: True, **env)[1])
        conn = sqlite3.connect(env["picks_db"])
        picked = conn.execute("SELECT snapshot_date FROM picks WHERE ticker='AAAA'").fetchall()
        conn.close()
    assert "1. AAAA" in texts[0] and "No strong setup today" in texts[1]
    assert [d for (d,) in picked] == [days[2][0], days[7][0]], picked    # again only after 5 days
    print("  ok no re-pick while holding")


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
    days = panel(12, start="2026-08-31")                  # ends Tue 2026-09-15
    sent = []
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days)
        today = days[-1][0]
        status, text = dp.run_morning(utc_at_myt(today), lambda t: sent.append(t) or True, **env)
        assert status == "sent", status
        assert len(sent) == 1 and "Morning report" in sent[0] and "Weekly scoreboard" in sent[0]
        status, _ = dp.run_morning(utc_at_myt(today, 11), lambda t: sent.append(t) or True, **env)
        assert status == "already_sent" and len(sent) == 1
        conn = sqlite3.connect(env["picks_db"])
        n_picks = conn.execute("SELECT count(*) FROM picks").fetchone()[0]
        kinds = sorted(k for (k,) in conn.execute("SELECT kind FROM sent_messages"))
        conn.close()
        assert n_picks == 3 and kinds == ["morning", "scoreboard"], (n_picks, kinds)
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
         "ticker": "BBBB", "reason": "cheap bank"}]}
    with tempfile.TemporaryDirectory() as tmp:
        env = _morning_env(tmp, days, follows=follows)
        status, text = dp.run_morning(utc_at_myt(days[-1][0]), lambda t: True, **env)
    assert status == "sent" and "🙋 Your picks" in text and "Your reason: cheap bank" in text, text
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
    assert status == "sent" and "Data: Fri 11 Sep close" in text, (status, text)
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
