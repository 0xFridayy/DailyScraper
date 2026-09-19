"""Morning report: machine picks (strong setups only), your picks (with your
reason), and a result for every finished pick - did it beat the market, and
why - plus a slow learning loop. Built only from data the daily scrape
already stored in neobdm.db.

Standard library only (requests is imported lazily for sending), and it never
imports neobdm_scraper, so CI can import and test it.

Dates (same convention as the scraper): a market_summary_daily row dated D was
captured on the morning of D and holds the PREVIOUS trading session. Weekend
and holiday scrapes store exact copies of that session; load_snapshots() drops
them, so snapshot index k counts real trading sessions. A pick made from
snapshot k is bought at the close of snapshot k+1 (the session traded on the
day the message arrives) and scored HORIZON sessions later. Scoring snapshot k
never reads a snapshot newer than k.

Run by morning.py inside the daily-scrape job. Local preview (no send, no
writes):  py -3 daily_picks.py --preview
"""

import json
import os
import sqlite3
import sys
import time
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
NEOBDM_DB = os.path.join(HERE, "neobdm.db")
PICKS_DB = os.path.join(HERE, "daily_picks.db")
FOLLOWS_JSON = os.path.join(HERE, "telegram_follows.json")

# The scraper dates rows in Asia/Kuala_Lumpur time (UTC+8, no DST).
MYT = timezone(timedelta(hours=8))

N_PICKS = 3             # machine picks per day, at most
MIN_TAGS = 3            # a machine pick needs at least three of the five checks
MIN_VALUE_BN = 2.0      # skip names trading under Rp 2 bn a day
MAX_RUNUP = 0.25        # skip names already up 25%+ over 5 sessions
RUNUP_WARN = 0.12       # warn (don't skip) from +12%
HORIZON = 5             # ~1 week holding idea
MAX_FOLLOWS = 10        # protects message length and future NeoBDM requests
COPY_SHARE = 0.95       # a snapshot this identical to the last one is a copy
SPLIT_FACTORS = (2, 3, 4, 5, 10, 20, 25)

# Starting guesses, checked on ~17 sessions only (2026-08-16..09-10). The
# weekly learning step moves the weights; nothing here is proven yet.
TAG_ORDER = ("broad_buying", "inst_foreign", "bandar_3days", "stalker", "value_up")
TAG_LABEL = {
    "broad_buying": "Clean score +3 or more",
    "inst_foreign": "Local funds + foreigners buying",
    "bandar_3days": "Bandar bought 3 days in a row",
    "stalker": "Retail sold, big broker picked it up",
    "value_up": "Trading value jumped",
}

FIELDS = ("close", "high", "low", "m_dn_0", "nr_dn_0", "f_dn_0", "m_cn_5",
          "clean_score", "tval", "pct_5", "top_5_buyer")

# gap_before: a trading session between this snapshot and the previous one
# was never captured (e.g. the scrape ran after 10:00 MYT), so returns and
# streaks across it would silently span an extra session.
Snapshot = namedtuple("Snapshot", "date rows gap_before", defaults=(False,))


# ── Loading ────────────────────────────────────────────────────────────────

def _num(row, field):
    """A real number or None. Missing values are never turned into zero."""
    value = row.get(field) if row else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _is_copy(prev_rows, rows):
    common = [t for t in rows if t in prev_rows]
    if not common:
        return False
    same = sum(1 for t in common
               if all(prev_rows[t][f] == rows[t][f] for f in ("close", "m_dn_0", "tval")))
    return same / len(common) > COPY_SHARE


def _session_of(capture_date):
    """A capture on day D holds the previous weekday's session."""
    d = date.fromisoformat(capture_date) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _lost_session(prev_date, cur_date, captured):
    """True if a weekday session between two kept snapshots was never
    captured at all. A holiday still leaves a (copy) capture the next day, so
    it doesn't count; a missed or late scrape leaves no row."""
    s = _session_of(prev_date) + timedelta(days=1)
    end = _session_of(cur_date)
    while s < end:
        if s.weekday() < 5:
            first = last = s + timedelta(days=1)    # captures that would hold s
            while last.weekday() >= 5:
                last += timedelta(days=1)
            if not any((first + timedelta(days=i)).isoformat() in captured
                       for i in range((last - first).days + 1)):
                return True
        s += timedelta(days=1)
    return False


def load_snapshots(conn):
    """One Snapshot per real trading session, oldest first."""
    by_date = {}
    query = f"SELECT date, ticker, {', '.join(FIELDS)} FROM market_summary_daily"
    for day, ticker, *values in conn.execute(query):
        by_date.setdefault(day, {})[ticker] = dict(zip(FIELDS, values))
    snaps = []
    for day in sorted(by_date):
        if snaps and _is_copy(snaps[-1].rows, by_date[day]):
            continue
        gap = bool(snaps) and _lost_session(snaps[-1].date, day, by_date)
        snaps.append(Snapshot(day, by_date[day], gap))
    return snaps


def _gap_inside(snaps, a, b):
    return any(snaps[j].gap_before for j in range(max(a, 0) + 1, b + 1))


def latest_capture_date(conn):
    return conn.execute("SELECT max(date) FROM market_summary_daily").fetchone()[0]


def load_stalker(conn):
    """({date: tickers flagged by broker_stalker}, {dates the source was down}).
    A down source means "unknown", not "not flagged", so learning skips it."""
    flagged, down = {}, set()
    for day, ticker, sources in conn.execute(
            "SELECT flag_date, ticker, sources FROM konglo_signal_watch"):
        if "broker_stalker" in (sources or "").split(","):
            flagged.setdefault(day, set()).add(ticker)
    try:
        for day, status in conn.execute(
                "SELECT flag_date, status FROM signal_source_status "
                "WHERE source = 'broker_stalker'"):
            if status not in ("HITS", "NO_HITS"):
                down.add(day)
    except sqlite3.OperationalError:
        pass  # older DBs have no status table
    return flagged, down


def neobdm_lists(conn, capture_date):
    """Today's raw NeoBDM lists, grouped by source, for one compact line.
    A source whose status says it failed maps to None ("unavailable"), so a
    broken list never looks like an empty one."""
    lists = {}
    for ticker, sources in conn.execute(
            "SELECT ticker, sources FROM konglo_signal_watch WHERE flag_date = ? "
            "ORDER BY ticker", (capture_date,)):
        for source in (sources or "").split(","):
            if source:
                lists.setdefault(source, []).append(ticker)
    try:
        status = dict(conn.execute(
            "SELECT source, status FROM signal_source_status WHERE flag_date = ?",
            (capture_date,)).fetchall())
    except sqlite3.OperationalError:
        return lists  # older DBs have no status table
    for source, _ in LIST_NAMES:
        if status.get(source) not in ("HITS", "NO_HITS"):
            lists[source] = None   # failed, or tracking was skipped that morning
    return lists


# ── Price helpers ──────────────────────────────────────────────────────────

def limit_up(prev_close):
    """IDX auto-reject upper band: Rp50-200 35%, >200-5000 25%, >5000 20%.
    (price_audit.ara_bound puts exactly Rp200 in the 25% tier.)"""
    if prev_close <= 200:
        return 0.35
    if prev_close <= 5000:
        return 0.25
    return 0.20


def corporate_action_hint(prev_close, close):
    """Text when a one-session move is outside the normal daily limits, which
    usually means a split, rights/bonus issue or a data error; else None."""
    if not prev_close or not close:
        return None
    r = close / prev_close
    if 0.84 <= r <= 1 + limit_up(prev_close) + 0.01:
        return None
    for n in SPLIT_FACTORS:
        if abs(r * n - 1) < 0.03:
            return f"looks like a 1:{n} stock split"
        if abs(r / n - 1) < 0.03:
            return f"looks like a {n}:1 reverse split"
    return "moved past the normal daily limit"


def _close(snaps, j, ticker):
    return _num(snaps[j].rows.get(ticker), "close")


def day_move(snaps, k, ticker):
    if k < 1:
        return None
    prev, cur = _close(snaps, k - 1, ticker), _close(snaps, k, ticker)
    return cur / prev - 1 if prev and cur else None


def price_break(snaps, a, b, ticker):
    """First likely corporate action between snapshots a and b, comparing each
    available close with the previous available one (so a split on a day the
    name was missing from the list is still caught): (index, hint) or None."""
    prev = None
    for j in range(max(a, 0), b + 1):
        close = _close(snaps, j, ticker)
        if close is None:
            continue
        if prev is not None:
            hint = corporate_action_hint(prev, close)
            if hint:
                return j, hint
        prev = close
    return None


def recent_corporate_action(snaps, k, ticker, sessions=5):
    return price_break(snaps, k - sessions, k, ticker) is not None


def value_ratio(snaps, k, ticker, lookback=5, min_prior=3):
    """Value traded vs its own recent average. Tracks true volume closely
    (corr 0.99 on the 27 names with both), since volume isn't captured."""
    now = _num(snaps[k].rows.get(ticker), "tval")
    prior = [v for j in range(max(0, k - lookback), k)
             if (v := _num(snaps[j].rows.get(ticker), "tval")) is not None]
    if now is None or len(prior) < min_prior:
        return None
    avg = sum(prior) / len(prior)
    return now / avg if avg > 0 else None


def _buying_days(snaps, k, ticker, field, n=3):
    """(days with positive flow, days with data) over the last n sessions."""
    values = [_num(snaps[j].rows.get(ticker), field) for j in range(max(0, k - n + 1), k + 1)]
    values = [v for v in values if v is not None]
    return sum(v > 0 for v in values), len(values)


def _top_buyers(row):
    try:
        return list(json.loads(row.get("top_5_buyer") or "[]"))
    except (TypeError, ValueError):
        return []


# ── Scoring ────────────────────────────────────────────────────────────────

def tag_snapshot(snaps, k, stalker=frozenset()):
    """Buy signals for every name in snapshot k that passes the basic filters.
    Reads snapshots 0..k only."""
    tagged = {}
    for ticker, row in snaps[k].rows.items():
        tval, pct5 = _num(row, "tval"), _num(row, "pct_5")
        if tval is None or tval < MIN_VALUE_BN or pct5 is None or pct5 >= MAX_RUNUP:
            continue
        if not _num(row, "close"):
            continue
        if recent_corporate_action(snaps, k, ticker):
            continue
        tags = []
        score = _num(row, "clean_score")
        if score is not None and score >= 3:
            tags.append("broad_buying")
        nr, fr = _num(row, "nr_dn_0"), _num(row, "f_dn_0")
        if nr is not None and fr is not None and nr > 0 and fr > 0:
            tags.append("inst_foreign")
        if (k >= 2 and not _gap_inside(snaps, k - 2, k)
                and _buying_days(snaps, k, ticker, "m_dn_0") == (3, 3)):
            tags.append("bandar_3days")
        if ticker in stalker:
            tags.append("stalker")
        ratio = value_ratio(snaps, k, ticker)
        if ratio is not None and ratio >= 1.5:
            tags.append("value_up")
        tagged[ticker] = {"tags": tags, "value_ratio": ratio, "row": row}
    return tagged


def rank_picks(tagged, weights, n=N_PICKS, min_tags=MIN_TAGS):
    """Top n names with at least min_tags signals, by summed tag weight."""
    ranked = []
    for ticker, cand in tagged.items():
        if len(cand["tags"]) < min_tags:
            continue
        row = cand["row"]
        score = sum(weights.get(t, 1.0) for t in cand["tags"])
        # Tie-break order only; a missing value just sorts last.
        clean = _num(row, "clean_score")
        flow = (_num(row, "nr_dn_0") or 0) + (_num(row, "f_dn_0") or 0)
        key = (-round(score, 9), -(clean if clean is not None else -99), -flow,
               -(_num(row, "tval") or 0), ticker)
        ranked.append((key, ticker, score))
    ranked.sort()
    return [(ticker, score) for _, ticker, score in ranked[:n]]


def reason_text(tag, row, ratio):
    if tag == "broad_buying":
        return f"Clean score {int(row['clean_score']):+d} (most money groups buying)"
    if tag == "value_up" and ratio is not None:
        return f"Trading value {ratio:.1f}x its normal"
    return TAG_LABEL[tag]


def pick_reason(cand, weights):
    """Every check the pick passed, strongest weight first."""
    tags = sorted(cand["tags"], key=lambda t: (-weights.get(t, 1.0), TAG_ORDER.index(t)))
    return " · ".join(reason_text(t, cand["row"], cand["value_ratio"]) for t in tags)


# ── Outcomes and learning ──────────────────────────────────────────────────

def session_returns(snaps, entry, exit_):
    """{ticker: close-to-close return} from snapshot entry to exit, skipping
    names with a likely corporate action inside the window. Empty when a
    session inside the window was never captured."""
    if _gap_inside(snaps, entry, exit_):
        return {}
    rets = {}
    for ticker, row in snaps[exit_].rows.items():
        start, end = _close(snaps, entry, ticker), _num(row, "close")
        if not start or not end:
            continue
        if price_break(snaps, entry, exit_, ticker):
            continue
        rets[ticker] = end / start - 1
    return rets


def forward_excess(snaps, k, ticker, h=HORIZON):
    """(return, return minus the average name) for a pick made from snapshot
    k, or None while the exit session hasn't happened or data is missing."""
    entry, exit_ = k + 1, k + 1 + h
    if exit_ >= len(snaps):
        return None
    rets = session_returns(snaps, entry, exit_)
    if ticker not in rets:
        return None
    return rets[ticker], rets[ticker] - sum(rets.values()) / len(rets)


def _mean(values):
    return sum(values) / len(values)


def learn_weights(snaps, stalker, down, h=HORIZON):
    """Per tag: the average excess return of tagged names in each session whose
    outcome has already happened (each name capped at +/-15%), shrunk toward
    neutral so a few lucky weeks can't swing it. Neighbouring 5-session
    windows share days, so only s/h of the s sessions count as independent:
        weight = clip(1 + 0.5 * n/(n+20) * avg_excess_pct, 0.25, 2.0), n = s/h
    Only uses snapshots inside `snaps`; pass exactly what was known then."""
    per_tag = {t: [] for t in TAG_ORDER}
    for k in range(len(snaps) - 1 - h):
        rets = session_returns(snaps, k + 1, k + 1 + h)
        if not rets:
            continue
        avg = _mean(rets.values())
        tagged = tag_snapshot(snaps, k, stalker.get(snaps[k].date, frozenset()))
        for tag in TAG_ORDER:
            if tag == "stalker" and snaps[k].date in down:
                continue
            xs = [max(-0.15, min(0.15, rets[t] - avg))
                  for t, cand in tagged.items() if tag in cand["tags"] and t in rets]
            if xs:
                per_tag[tag].append(_mean(xs))
    result = {}
    for tag, sessions in per_tag.items():
        s = len(sessions)
        avg_pct = 100 * _mean(sessions) if s else 0.0
        n = s / h
        weight = max(0.25, min(2.0, 1 + 0.5 * (n / (n + 20)) * avg_pct))
        result[tag] = {"weight": weight, "sessions": s, "avg_excess_pct": avg_pct}
    return result


# ── Your picks (follows) ───────────────────────────────────────────────────

def load_follows(path=FOLLOWS_JSON):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"last_update_id": 0, "events": []}


def save_follows(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def active_follows(events, ended=None):
    """Your current picks: {ticker: {"at", "days", "reason"}}.

    "at" is the first yes, "days" the trading days to hold (None = until
    stop), "reason" why you picked it. A later yes on a stock you already
    hold only changes its days. `ended` maps (ticker, at) of finished timed
    picks to when their result was recorded: a yes sent after that starts a
    fresh pick, a yes sent before it (a duration change) never does."""
    ended = ended or {}
    active = {}
    for event in events:
        ticker = event["ticker"]
        if event["action"] == "stop":
            active.pop(ticker, None)
            continue
        current = active.get(ticker)
        if current is not None:
            done_at = ended.get((ticker, current["at"]))
            if done_at is not None and datetime.fromisoformat(event["at"]) > datetime.fromisoformat(done_at):
                current = None
        if current is None:
            active[ticker] = {"at": event["at"], "days": event.get("days"),
                              "reason": event.get("reason", "")}
        elif "days" in event:
            current["days"] = event["days"]
    return {t: f for t, f in active.items() if (t, f["at"]) not in ended}


def ended_follows(conn):
    """{(ticker, first yes time): when the result was recorded} for your
    finished timed picks."""
    rows = conn.execute("SELECT ticker, started, recorded_utc FROM pick_results "
                        "WHERE source = 'you'").fetchall()
    return {(t, s): r for t, s, r in rows}


def ended_follows_from_file(path=None):
    """Same, read-only from daily_picks.db; empty if it doesn't exist yet."""
    path = path or PICKS_DB
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return ended_follows(conn)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def reference_index(snaps, ticker, since_utc, sent_at=None):
    """The snapshot the owner was looking at when saying yes: the latest one
    whose morning message went out before the reply. Without a sent record
    (weekends, first run) a snapshot counts once its day is over or it is
    past 10:00 MYT that day. None if that snapshot doesn't have the stock -
    never an older price."""
    since = datetime.fromisoformat(since_utc)
    local = since.astimezone(MYT)
    for j in range(len(snaps) - 1, -1, -1):
        sent = (sent_at or {}).get(snaps[j].date)
        if sent is not None:
            visible = datetime.fromisoformat(sent) <= since
        else:
            day = date.fromisoformat(snaps[j].date)
            visible = day < local.date() or (day == local.date() and local.hour >= 10)
        if visible:
            return j if ticker in snaps[j].rows else None
    return None


def follow_lines(snaps, ticker, since_utc, sent_at=None):
    k = len(snaps) - 1
    row = snaps[k].rows.get(ticker)
    if row is None or not _num(row, "close"):
        seen = next((j for j in range(k, -1, -1) if ticker in snaps[j].rows), None)
        tail = f" Last seen {session_label(snaps[seen].date)}." if seen is not None else ""
        return [f"{ticker}: no NeoBDM data today (not in the liquid list).{tail}"]

    lines = []
    ref = reference_index(snaps, ticker, since_utc, sent_at)
    close = _num(row, "close")
    broke = price_break(snaps, ref, k, ticker) if ref is not None else None
    if broke:
        lines.append(f"{ticker}  Rp {close:,.0f} - not comparable with your yes price: "
                     f"{broke[1]} around {session_label(snaps[broke[0]].date)}")
    elif ref is not None and ref < k and _close(snaps, ref, ticker) and close:
        start = _close(snaps, ref, ticker)
        rets = session_returns(snaps, ref, k)
        market = f", market {_mean(rets.values()) * 100:+.1f}%" if rets else ""
        lines.append(f"{ticker}  {(close / start - 1) * 100:+.1f}% since yes "
                     f"(Rp {start:,.0f} → {close:,.0f}{market})")
    elif ref == k:
        lines.append(f"{ticker}  Rp {close:,.0f} (you said yes at this price)")
    else:
        lines.append(f"{ticker}  Rp {close:,.0f} (no NeoBDM price from the day you said yes)")

    for label, field in (("Bandar", "m_dn_0"), ("Foreign", "f_dn_0")):
        buys, seen = _buying_days(snaps, k, ticker, field)
        if seen == 0:
            continue
        lines.append(f" • {label} bought on {buys} of the last {seen} days")

    if ref is not None:
        then = _top_buyers(snaps[ref].rows.get(ticker))[:2]
        now = _top_buyers(row)
        if then:
            kept = [b for b in then if b in now]
            status = f"{', '.join(kept)} still in today's top 5" if kept else "none in today's top 5"
            lines.append(f" • Top buyers when you said yes: {', '.join(then)} - {status}")

    ratio = value_ratio(snaps, k, ticker)
    if ratio is not None:
        if ratio >= 2:
            note = "spike"
        elif ratio <= 0.5:
            note = "drying up"
        else:
            note = "normal"
        lines.append(f" • Trading {note} ({ratio:.1f}x its usual value)")

    lines += [f" ⚠️ {w}" for w in warnings(snaps, k, ticker)]
    return lines


def _sessions_since(snaps, since_utc, ref):
    """Trading sessions that have closed since the yes."""
    if ref is not None:
        return len(snaps) - 1 - ref
    day = datetime.fromisoformat(since_utc).astimezone(MYT).date()
    return sum(1 for s in snaps if date.fromisoformat(s.date) > day)


def machine_view(snaps, j, ticker, stalker=None):
    """The machine's checks that a stock passed on snapshot j."""
    flagged = (stalker or {}).get(snaps[j].date, frozenset())
    return tag_snapshot(snaps, j, flagged).get(ticker, {}).get("tags", [])


def _labels(tags):
    return ", ".join(TAG_LABEL[t] for t in tags if t in TAG_LABEL) or "none of its checks"


def follow_block(snaps, ticker, follow, sent_at=None, stalker=None):
    """(message lines, result to record or None) for one of your picks. A
    timed pick whose days are up gets its final result; otherwise the normal
    follow-up lines, with 'day X of N' for timed picks."""
    since, days, reason = follow["at"], follow.get("days"), follow.get("reason") or ""
    ref = reference_index(snaps, ticker, since, sent_at)
    elapsed = _sessions_since(snaps, since, ref)
    if days is None or elapsed < days:
        lines = follow_lines(snaps, ticker, since, sent_at)
        if days is not None:
            lines[0] += f" · day {elapsed} of {days}"
        lines.insert(1, f" • Your reason: {reason or '(none given)'}")
        return lines, None

    tags = machine_view(snaps, ref, ticker, stalker) if ref is not None else []
    base = {"source": "you", "ticker": ticker, "started": since, "days": days,
            "reason": reason, "start_tags": ",".join(tags),
            "start_snapshot": snaps[ref].date if ref is not None else None}
    if ref is None:
        empty = {"end_snapshot": snaps[-1].date, "start_price": None, "end_price": None,
                 "ret": None, "market_ret": None, "why": None, "facts": None}
        return [f"⚪ {ticker} (you, {days} days): no NeoBDM price from the day you said yes."], \
            {**base, **empty}
    lines, result = finish_pick(snaps, ticker, ref, days, "you")
    lines[1:1] = [f"   Your reason: {reason or '(none given)'}",
                  f"   Machine's checks that day: {_labels(tags)}"]
    return lines, {**base, **result}


# ── Results and why ────────────────────────────────────────────────────────

def explain(snaps, ticker, start, end):
    """What happened while a pick was held (sessions start+1..end)."""
    window = range(start + 1, end + 1)
    rows = [snaps[j].rows.get(ticker) for j in window]
    facts = {}
    for name, field in (("bandar", "m_dn_0"), ("foreign", "f_dn_0")):
        values = [v for r in rows if (v := _num(r, field)) is not None]
        facts[name + "_buys"] = sum(v > 0 for v in values)
        facts[name + "_days"] = len(values)
        facts[name + "_share"] = facts[name + "_buys"] / len(values) if values else None
    held = [v for r in rows if (v := _num(r, "tval")) is not None]
    before = [v for j in range(max(0, start - 4), start + 1)
              if (v := _num(snaps[j].rows.get(ticker), "tval")) is not None]
    facts["trading_ratio"] = (_mean(held) / _mean(before)
                              if held and before and _mean(before) > 0 else None)
    moves = [m for j in window if (m := day_move(snaps, j, ticker)) is not None]
    facts["worst_day"] = min(moves) if moves else None
    rets = session_returns(snaps, start, end)
    facts["market_ret"] = _mean(rets.values()) if rets else None
    return facts


def main_reason(ret, facts):
    """The one plain reason that best fits what happened. These are
    patterns in the flow data, not proof of cause."""
    market = facts["market_ret"]
    excess = ret - (market or 0)
    if (market is not None and abs(market) >= 0.02 and market * ret > 0
            and abs(market) >= abs(excess)):
        return "mostly moved with the market"
    bandar, foreign = facts["bandar_share"], facts["foreign_share"]
    trading, worst = facts["trading_ratio"], facts["worst_day"]
    if excess > 0:
        if bandar is not None and bandar >= 0.6:
            return "bandar kept buying"
        if foreign is not None and foreign >= 0.6:
            return "foreign kept buying"
        if trading is not None and trading >= 1.5:
            return "buyers rushed in (trading jumped)"
        return "rose without a clear flow signal"
    if worst is not None and worst <= -0.07:
        return f"one bad day ({worst:.0%})"
    if bandar is not None and bandar <= 0.4:
        return "bandar turned seller"
    if foreign is not None and foreign <= 0.4:
        return "foreign sold"
    if trading is not None and trading <= 0.6:
        return "interest dried up (trading fell)"
    return "fell without a clear flow signal"


def fact_text(facts):
    parts = []
    for label, name in (("Bandar bought", "bandar"), ("Foreign bought", "foreign")):
        if facts[name + "_days"]:
            parts.append(f"{label} {facts[name + '_buys']}/{facts[name + '_days']} days")
    if facts["trading_ratio"] is not None:
        parts.append(f"trading {facts['trading_ratio']:.1f}x usual")
    if facts["worst_day"] is not None and facts["worst_day"] <= -0.05:
        parts.append(f"worst day {facts['worst_day']:.0%}")
    return " · ".join(parts)


def finish_pick(snaps, ticker, start, days, who):
    """Final result of a pick held `days` sessions from snapshot `start`,
    measured from the price shown when it was picked: (lines, result)."""
    end = min(start + days, len(snaps) - 1)
    result = {"end_snapshot": snaps[end].date, "start_price": None, "end_price": None,
              "ret": None, "market_ret": None, "why": None, "facts": None}
    late = "" if end == len(snaps) - 1 else f", ended {session_label(snaps[end].date)}"
    first, last = _close(snaps, start, ticker), _close(snaps, end, ticker)
    broke = price_break(snaps, start, end, ticker)
    if not first or not last or broke:
        why = broke[1] if broke else "no price at the end"
        return [f"⚪ {ticker} ({who}, {days} days{late}): not comparable - {why}"], result
    ret = last / first - 1
    facts = explain(snaps, ticker, start, end)
    why = main_reason(ret, facts)
    market = facts["market_ret"]
    excess = ret - (market or 0)
    mark = "✅" if excess > 0 else "❌"
    vs = (f", market {market * 100:+.1f}% → {'beat' if excess > 0 else 'lagged'} it by "
          f"{abs(excess) * 100:.1f}%" if market is not None else "")
    lines = [f"{mark} {ticker} ({who}, {days} days{late}): {ret * 100:+.1f}%{vs} - {why}",
             f"   {fact_text(facts)}"]
    result.update(start_price=first, end_price=last, ret=ret, market_ret=market,
                  why=why, facts=facts)
    return lines, result


def machine_progress(snaps, pick_rows, recorded):
    """(running line texts, [(lines, result)] finished) for machine picks.
    pick_rows: (snapshot_date, ticker, tags); recorded: {(source, ticker,
    started)} already in pick_results."""
    index = {s.date: i for i, s in enumerate(snaps)}
    k = len(snaps) - 1
    running, finished = [], []
    for snap_date, ticker, tags in pick_rows:
        j = index.get(snap_date)
        if j is None or ("machine", ticker, snap_date) in recorded:
            continue
        elapsed = k - j
        if elapsed >= HORIZON:
            lines, result = finish_pick(snaps, ticker, j, HORIZON, "machine")
            lines.insert(1, f"   Picked for: {_labels(tags.split(','))}")
            finished.append((lines, {"source": "machine", "ticker": ticker, "started": snap_date,
                                     "days": HORIZON, "reason": "", "start_tags": tags,
                                     "start_snapshot": snap_date, **result}))
        elif elapsed >= 1:
            first, now = _close(snaps, j, ticker), _close(snaps, k, ticker)
            if first and now and not price_break(snaps, j, k, ticker):
                running.append(f"{ticker} {(now / first - 1) * 100:+.1f}% (day {elapsed}/{HORIZON})")
            else:
                running.append(f"{ticker} n/a (day {elapsed}/{HORIZON})")
    return running, finished


def record_results(conn, results, now_utc):
    rows = []
    for r in results:
        f = r.get("facts") or {}
        rows.append((r["source"], r["ticker"], r["started"], r["days"], r["reason"],
                     r["start_tags"], r["start_snapshot"], r["end_snapshot"],
                     r["start_price"], r["end_price"], r["ret"], r["market_ret"],
                     f.get("bandar_share"), f.get("foreign_share"), f.get("trading_ratio"),
                     f.get("worst_day"), r["why"], now_utc.isoformat()))
    conn.executemany("INSERT OR IGNORE INTO pick_results VALUES "
                     "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def warnings(snaps, k, ticker):
    row = snaps[k].rows.get(ticker)
    out = []
    pct5 = _num(row, "pct_5")
    heavy = value_ratio(snaps, k, ticker, lookback=20, min_prior=5)
    if pct5 is not None and pct5 >= RUNUP_WARN and heavy is not None and heavy >= 2.5:
        out.append(f"Ran up {pct5:.0%} in 5 days on heavy trading. Sharp drops follow "
                   "this pattern about 2x more often than usual - don't chase the open.")
    move = day_move(snaps, k, ticker)
    broke = price_break(snaps, k - 5, k, ticker)
    if broke and broke[0] == k:
        out.append(f"{broke[1].capitalize()} - check IDX announcements; the numbers above may be off.")
    elif move is not None and move <= -0.14:
        out.append(f"Fell {move:.0%} yesterday (near the daily floor). About 1 in 5 do it "
                   "again the next day.")
    return out


# ── Picks database ─────────────────────────────────────────────────────────

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS picks (
        snapshot_date TEXT NOT NULL, ticker TEXT NOT NULL, rank INTEGER NOT NULL,
        score REAL NOT NULL, tags TEXT NOT NULL, reason TEXT NOT NULL, close REAL,
        recorded_utc TEXT NOT NULL, PRIMARY KEY (snapshot_date, ticker))""",
    """CREATE TABLE IF NOT EXISTS tag_weights (
        as_of TEXT NOT NULL, tag TEXT NOT NULL, weight REAL NOT NULL,
        sessions INTEGER NOT NULL, avg_excess_pct REAL NOT NULL,
        PRIMARY KEY (as_of, tag))""",
    """CREATE TABLE IF NOT EXISTS sent_messages (
        kind TEXT NOT NULL, key TEXT NOT NULL, sent_utc TEXT NOT NULL,
        text TEXT NOT NULL, PRIMARY KEY (kind, key))""",
    # One row per finished pick, machine or yours: result, what happened, why.
    """CREATE TABLE IF NOT EXISTS pick_results (
        source TEXT NOT NULL, ticker TEXT NOT NULL, started TEXT NOT NULL,
        days INTEGER NOT NULL, reason TEXT NOT NULL, start_tags TEXT NOT NULL,
        start_snapshot TEXT, end_snapshot TEXT NOT NULL, start_price REAL,
        end_price REAL, ret REAL, market_ret REAL, bandar_share REAL,
        foreign_share REAL, trading_ratio REAL, worst_day REAL, why TEXT,
        recorded_utc TEXT NOT NULL, PRIMARY KEY (source, ticker, started))""",
]


def ensure_schema(conn):
    for statement in SCHEMA:
        conn.execute(statement)
    conn.commit()


def current_weights(conn):
    rows = conn.execute("SELECT tag, weight FROM tag_weights WHERE as_of = "
                        "(SELECT max(as_of) FROM tag_weights)").fetchall()
    return {t: w for t, w in rows}


def already_sent(conn, kind, key):
    return conn.execute("SELECT 1 FROM sent_messages WHERE kind = ? AND key = ?",
                        (kind, key)).fetchone() is not None


def record_sent(conn, kind, key, text, now_utc):
    # Only the length is kept: the repo is public and the text holds follows.
    conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?, ?, ?, ?)",
                 (kind, key, now_utc.isoformat(), f"{len(text)} chars"))


# ── Messages ───────────────────────────────────────────────────────────────

def session_label(snapshot_date):
    """Snapshot D holds the previous weekday's session."""
    d = date.fromisoformat(snapshot_date) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return f"{d:%a} {d.day} {d:%b}"


LIST_NAMES = (("broker_stalker", "Stalker"), ("dashboard_Bandarmologi", "Bandar"),
              ("dashboard_NonRetail", "Non-retail"), ("dashboard_Foreign", "Foreign"))


def format_morning(today, snap, picks, tagged, weights, your_blocks, running,
                   finished_blocks, lists, n_sessions):
    lines = [f"📈 Morning report - {today:%a} {today.day} {today:%b}",
             f"Data: {session_label(snap.date)} close", "",
             f"🤖 Machine picks - strong setups only ({MIN_TAGS}+ checks), hold about 1 week"]
    if picks:
        for i, (ticker, _) in enumerate(picks, 1):
            cand = tagged[ticker]
            lines.append(f"{i}. {ticker}  Rp {_num(cand['row'], 'close'):,.0f}")
            lines.append(f"   {pick_reason(cand, weights)}")
        for ticker, _ in picks:
            pct5 = _num(tagged[ticker]["row"], "pct_5")
            if pct5 is not None and pct5 >= RUNUP_WARN:
                lines.append(f"⚠️ {ticker} is already up {pct5:.0%} in 5 days - don't chase.")
    else:
        lines.append(f"No strong setup today - nothing passed {MIN_TAGS}+ checks.")
    if running:
        lines += ["", "🤖 Machine picks running: " + " · ".join(running)]
    if your_blocks:
        lines += ["", "🙋 Your picks"]
        for block in your_blocks:
            lines += block
    if finished_blocks:
        lines += ["", "🏁 Finished (✅ beat the market, ❌ didn't)"]
        for block in finished_blocks:
            lines += block
    shown = [f"{name}: " + ("unavailable" if src in lists and lists[src] is None
                            else ", ".join(lists.get(src) or []) or "-")
             for src, name in LIST_NAMES]
    lines += ["", "📋 NeoBDM lists · " + " · ".join(shown),
              "", "Reply: yes TICKER 2w your reason · stop TICKER · list",
              f"Not proven yet - {n_sessions} trading days of data so far."]
    return "\n".join(lines)


WHY_BUCKETS = (
    ("Bandar kept buying while held", "bandar_share", lambda v: v >= 0.6),
    ("Bandar mostly sold while held", "bandar_share", lambda v: v <= 0.4),
    ("Foreign kept buying while held", "foreign_share", lambda v: v >= 0.6),
    ("Foreign mostly sold while held", "foreign_share", lambda v: v <= 0.4),
    ("Trading rose while held", "trading_ratio", lambda v: v >= 1.3),
    ("Trading dried up while held", "trading_ratio", lambda v: v <= 0.7),
)


def format_scoreboard(snaps, conn, learned, previous):
    lines = ["📊 Weekly scoreboard (won = beat the market)"]
    rows = [dict(zip(("source", "ret", "market_ret", "bandar_share", "foreign_share",
                      "trading_ratio", "start_tags"), r))
            for r in conn.execute("SELECT source, ret, market_ret, bandar_share, foreign_share, "
                                  "trading_ratio, start_tags FROM pick_results "
                                  "WHERE ret IS NOT NULL")]
    for r in rows:
        r["won"] = r["ret"] - (r["market_ret"] or 0) > 0
    for source, label in (("machine", "🤖 Machine"), ("you", "🙋 You")):
        mine = [r for r in rows if r["source"] == source]
        if mine:
            excess = [r["ret"] - (r["market_ret"] or 0) for r in mine]
            lines.append(f"{label}: {len(mine)} finished, {sum(r['won'] for r in mine)} won, "
                         f"{_mean(excess) * 100:+.1f}% vs market on average")
        else:
            lines.append(f"{label}: nothing finished yet")
    if rows:
        lines.append("What happened in winners vs losers (all finished picks):")
        for name, field, test in WHY_BUCKETS:
            hit = [r for r in rows if r[field] is not None and test(r[field])]
            if hit:
                lines.append(f" • {name}: {sum(r['won'] for r in hit)} of {len(hit)} won")
        for tag in TAG_ORDER:
            hit = [r for r in rows if tag in (r["start_tags"] or "").split(",")]
            if hit:
                lines.append(f" • Started with '{TAG_LABEL[tag]}': "
                             f"{sum(r['won'] for r in hit)} of {len(hit)} won")
    lines.append("Machine checks across all stocks, 1-week result (weight old → new):")
    for tag in TAG_ORDER:
        info = learned[tag]
        early = " - too early" if info["sessions"] < 60 else ""
        lines.append(f" • {TAG_LABEL[tag]}: {info['avg_excess_pct']:+.1f}% over "
                     f"{info['sessions']} days ({previous.get(tag, 1.0):.2f} → "
                     f"{info['weight']:.2f}){early}")
    lines.append(f"{len(rows)} finished picks so far - patterns need about 30 before they "
                 f"mean much. {len(snaps)} trading days of market data.")
    return "\n".join(lines)


def split_message(text, limit=3900):
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def telegram_sender(token, chat_id, post=None, sleep=time.sleep):
    """send(text) -> bool. Never prints the URL: it contains the bot token."""
    def send(text):
        if post is None:
            import requests
            do_post = requests.post
        else:
            do_post = post
        ok = True
        for part in split_message(text):
            for _ in range(3):
                try:
                    resp = do_post(f"https://api.telegram.org/bot{token}/sendMessage",
                                   json={"chat_id": chat_id, "text": part,
                                         "disable_web_page_preview": True}, timeout=15)
                except Exception as e:  # message may contain the URL - print type only
                    print(f"telegram send failed: {type(e).__name__}")
                    ok = False
                    break
                if resp.status_code == 429:
                    try:
                        wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
                    except ValueError:
                        wait = 5
                    sleep(min(wait, 30))
                    continue
                if not resp.ok:
                    print(f"telegram error {resp.status_code}")
                    ok = False
                break
            else:
                ok = False
        return ok
    return send


def telegram_sender_from_env():
    return telegram_sender(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])


# ── Morning run ────────────────────────────────────────────────────────────

def run_morning(now_utc, send, neobdm_db=NEOBDM_DB, picks_db=PICKS_DB,
                follows_json=FOLLOWS_JSON, preview=False):
    """Build and send the morning message. Returns (status, text).

    Sends nothing on weekends, and at most one message per trading session
    (so the morning after a holiday, which has no new data, stays quiet).
    preview=True ignores those checks and writes/sends nothing.
    """
    local = now_utc.astimezone(MYT)
    today = local.date()
    if local.weekday() >= 5 and not preview:
        return "weekend", None

    src = sqlite3.connect(f"file:{neobdm_db}?mode=ro", uri=True)
    try:
        snaps = load_snapshots(src)
        captured = latest_capture_date(src)
        stalker, down = load_stalker(src)
        lists = neobdm_lists(src, captured) if captured else {}
    finally:
        src.close()

    conn = sqlite3.connect(":memory:" if preview else picks_db)
    try:
        ensure_schema(conn)
        fresh_unsent = bool(snaps) and not already_sent(conn, "morning", snaps[-1].date)
        if captured != today.isoformat() and not preview and not fresh_unsent:
            if already_sent(conn, "stale", today.isoformat()):
                return "stale_already_warned", None
            text = ("⚠️ No fresh NeoBDM data this morning (the scrape ran late or "
                    "failed), so no new picks today.")
            if send(text):
                record_sent(conn, "stale", today.isoformat(), text, now_utc)
                conn.commit()
            return "stale", text
        if not snaps:
            return "no_data", None

        k = len(snaps) - 1
        snap = snaps[k]
        if already_sent(conn, "morning", snap.date) and not preview:
            return "already_sent", None

        week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
        weekly = preview or not already_sent(conn, "scoreboard", week)
        previous = current_weights(conn)
        learned = None
        if weekly:
            learned = learn_weights(snaps, stalker, down)
            conn.executemany("INSERT OR REPLACE INTO tag_weights VALUES (?, ?, ?, ?, ?)",
                             [(snap.date, t, v["weight"], v["sessions"], v["avg_excess_pct"])
                              for t, v in learned.items()])
        weights = current_weights(conn)

        tagged = tag_snapshot(snaps, k, stalker.get(snap.date, frozenset()))
        picks = rank_picks(tagged, weights)
        follows = active_follows(load_follows(follows_json).get("events", []),
                                 ended_follows(conn))
        sent_at = dict(conn.execute(
            "SELECT key, sent_utc FROM sent_messages WHERE kind = 'morning'").fetchall())
        recorded = set(conn.execute("SELECT source, ticker, started FROM pick_results").fetchall())
        your_blocks, finished_blocks, results = [], [], []
        for ticker, follow in list(follows.items())[:MAX_FOLLOWS]:
            lines, result = follow_block(snaps, ticker, follow, sent_at, stalker)
            if result:
                finished_blocks.append(lines)
                results.append(result)
            else:
                your_blocks.append(lines)
        pick_rows = conn.execute("SELECT snapshot_date, ticker, tags FROM picks "
                                 "ORDER BY snapshot_date, rank").fetchall()
        running, machine_done = machine_progress(snaps, pick_rows, recorded)
        for lines, result in machine_done:
            finished_blocks.append(lines)
            results.append(result)
        # Recorded before the scoreboard reads them; rolled back if the send fails.
        record_results(conn, results, now_utc)
        text = format_morning(today, snap, picks, tagged, weights, your_blocks, running,
                              finished_blocks, lists, len(snaps))
        if weekly:
            text += "\n\n" + format_scoreboard(snaps, conn, learned, previous)
        if preview:
            return "preview", text

        if not send(text):
            conn.rollback()
            return "send_failed", text
        conn.executemany(
            "INSERT OR IGNORE INTO picks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(snap.date, t, i, score, ",".join(tagged[t]["tags"]),
              pick_reason(tagged[t], weights), _num(tagged[t]["row"], "close"),
              now_utc.isoformat()) for i, (t, score) in enumerate(picks, 1)])
        record_sent(conn, "morning", snap.date, text, now_utc)
        if weekly:
            record_sent(conn, "scoreboard", week, text, now_utc)
        conn.commit()
        return "sent", text
    finally:
        conn.close()


if __name__ == "__main__":
    if "--preview" in sys.argv:
        status, message = run_morning(datetime.now(timezone.utc), send=None, preview=True)
        sys.stdout.reconfigure(encoding="utf-8")
        print(message or status)
    else:
        print("usage: py -3 daily_picks.py --preview   (morning.py runs the real thing)")
