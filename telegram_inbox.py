"""Read the owner's Telegram replies and keep telegram_follows.json current.

Commands (case doesn't matter, a leading "/" and "@botname" are fine):
    yes BBCA 2w bandar accumulating before breakout
        your pick: tickers first, then how long (optional), then WHY
        (required for a new pick). How long counts trading days: 5d, 2w
        (=10), 1m (=21), also "10 hari", "2 minggu", "1 bulan", "seminggu",
        "sebulan". Without it: until you say stop. Sending yes again for a
        stock you hold only changes how long, counted from your first yes.
    stop BBCA             stop following
    list                  show your picks

Run every few hours by telegram-inbox.yml, which is the ONLY writer of
telegram_follows.json. Telegram keeps unread replies for 24 hours, so the
morning job alone could miss some. Needs only TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID and `requests`.

Don't also run `python neobdm_scraper.py` (scheduler mode) anywhere: its own
poller reads the same updates and would swallow these replies.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

from daily_picks import (FOLLOWS_JSON, HERE, MAX_FOLLOWS, active_follows,
                         ended_follows_from_file, load_follows, save_follows,
                         telegram_sender)

COMMAND = re.compile(r"^\s*/?(yes|stop|list)(?:@\w+)?(?:\s+(.*))?$", re.I | re.S)
TICKER = re.compile(r"([A-Za-z]{4})(?![\w.])[\s,]*")
DURATION = re.compile(r"(\d{1,3})\s*(days?|hari|hr|weeks?|wks?|minggu|mgg|months?|mo|bulan|bln"
                      r"|d|w|m)(?![\w.])", re.I)
WORD_DAYS = re.compile(r"(seminggu|sebulan)(?![\w.])", re.I)
REASON_LEAD = re.compile(r"^(?:because|karena|krn|alasan|reason)\b[\s:,-]*", re.I)
UNIT_DAYS = {"d": 1, "day": 1, "days": 1, "hari": 1, "hr": 1,
             "w": 5, "wk": 5, "wks": 5, "week": 5, "weeks": 5, "minggu": 5, "mgg": 5,
             "m": 21, "mo": 21, "month": 21, "months": 21, "bulan": 21, "bln": 21,
             "seminggu": 5, "sebulan": 21}
MAX_DAYS = 250   # about a year of trading days

PROBLEMS = {
    "duration": "⚠️ Couldn't read how long. Use 5d, 2w or 1m (max 250 trading days). "
                "Nothing changed.",
    "order": "⚠️ Put the tickers first, then one duration, then your reason, e.g. "
             "yes BBCA BBRI 2w banks rebounding. Nothing changed.",
    "no_ticker": "⚠️ I couldn't find a ticker. Try: yes BBCA 2w your reason",
}


def parse_command(text, known=None):
    """{'action', 'tickers', 'days', 'reason', 'problem'} or None.

    Format: yes TICKER [TICKER ...] [how long] [reason]. With `known`, a
    4-letter word after the first ticker that is not a real ticker starts the
    reason (so "yes BBCA high volume" keeps HIGH in the reason)."""
    match = COMMAND.match(text or "")
    if not match:
        return None
    rest = (match.group(2) or "").strip()
    tickers = []
    while (token := TICKER.match(rest)):
        word = token.group(1).upper()
        if tickers and known is not None and word not in known:
            break
        tickers.append(word)
        rest = rest[token.end():]
    days, problem = None, None
    found = DURATION.match(rest) or WORD_DAYS.match(rest)
    if found:
        if found.re is WORD_DAYS:
            n = UNIT_DAYS[found.group(1).lower()]
        else:
            n = int(found.group(1)) * UNIT_DAYS[found.group(2).lower()]
        rest = rest[found.end():]
        if 0 < n <= MAX_DAYS:
            days = n
        else:
            problem = "duration"
    elif re.match(r"\d", rest):
        problem = "duration"
    reason = REASON_LEAD.sub("", rest.strip(" ,:-")).strip()
    # "yes BBCA 5d BBRI 1m": a second ticker with its own duration is not a
    # reason. ("yes BBCA 2w bank rebound" is fine even though BANK is a ticker.)
    words = reason.split()
    if (not problem and known is not None and len(words) >= 2
            and words[0].upper() in known and DURATION.match(" ".join(words[1:]))):
        problem = "order"
    return {"action": match.group(1).lower(), "tickers": tickers, "days": days,
            "reason": reason, "problem": problem}


def how_long(days):
    return "until you say stop" if days is None else f"{days} trading days"


def apply_updates(state, updates, chat_id, known, ended=None):
    """Pure: apply Telegram updates to the pick state.
    Returns (new_state, reply_text or None). Other chats are ignored, and an
    update already applied (same update_id) is never applied twice. `ended` =
    your finished timed picks (from daily_picks.db; they free a slot)."""
    events = list(state.get("events", []))
    last = state.get("last_update_id", 0)
    done = {e.get("update_id") for e in events}
    active = active_follows(events, ended)
    added, changed, already, stopped, unknown, full, need_reason = [], [], [], [], [], [], []
    problems, listed = [], False

    for update in sorted(updates, key=lambda u: u["update_id"]):
        last = max(last, update["update_id"])
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            continue
        cmd = parse_command(msg.get("text"), known)
        if cmd is None or update["update_id"] in done:
            continue
        if cmd["action"] == "list":
            listed = True
            continue
        if cmd["problem"] or not cmd["tickers"]:
            text = PROBLEMS[cmd["problem"] or "no_ticker"]
            if text not in problems:
                problems.append(text)
            continue
        at = datetime.fromtimestamp(msg.get("date", 0), timezone.utc).isoformat()
        for ticker in cmd["tickers"]:
            event = {"update_id": update["update_id"], "at": at, "action": cmd["action"],
                     "ticker": ticker}
            if cmd["days"] is not None:
                event["days"] = cmd["days"]
            if cmd["reason"] and cmd["action"] == "yes":
                event["reason"] = cmd["reason"]
            if ticker not in known:
                unknown.append(ticker)
            elif cmd["action"] == "yes" and ticker in active:
                # Always kept: while the pick runs it only changes how long; if
                # the pick had in fact just finished, it starts a new one.
                events.append(event)
                if cmd["days"] is not None and cmd["days"] != active[ticker]["days"]:
                    active[ticker]["days"] = cmd["days"]
                    changed.append((ticker, cmd["days"]))
                else:
                    already.append(ticker)
            elif cmd["action"] == "yes":
                if not cmd["reason"]:
                    need_reason.append(ticker)
                    continue
                if len(active) >= MAX_FOLLOWS:
                    full.append(ticker)
                    continue
                events.append(event)
                active[ticker] = {"at": at, "days": cmd["days"], "reason": cmd["reason"]}
                added.append((ticker, cmd["days"], cmd["reason"]))
            elif cmd["action"] == "stop" and ticker in active:
                events.append(event)
                del active[ticker]
                stopped.append(ticker)

    lines = []
    for ticker, days, reason in added:
        lines.append(f"✅ Your pick: {ticker} ({how_long(days)}) - reason: {reason}")
    if changed:
        lines.append("⏱ Changed: " + ", ".join(f"{t} ({how_long(d)})" for t, d in changed)
                     + ", counted from your first yes.")
    if already:
        lines.append(f"ℹ️ Already your pick: {', '.join(already)}")
    if stopped:
        lines.append(f"🛑 Stopped: {', '.join(stopped)}")
    if need_reason:
        example = f"yes {need_reason[0]} 2w bandar accumulating before breakout"
        lines.append(f"✍️ Add why you pick {', '.join(need_reason)}, so we can learn if the "
                     f"reason works. Example: {example}")
    if unknown:
        lines.append(f"⚠️ Not an IDX ticker I know: {', '.join(unknown)}")
    if full:
        lines.append(f"⚠️ You already have {MAX_FOLLOWS} picks (the max). Stop one first: "
                     f"{', '.join(full)}")
    lines += problems
    if lines or listed:
        names = ", ".join(f"{t} ({how_long(f['days'])})" for t, f in active.items())
        lines.append(f"🙋 Your picks ({len(active)}/{MAX_FOLLOWS}): {names or 'none yet'}")
    return {"last_update_id": last, "events": events}, ("\n".join(lines) or None)


def main():
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    state = load_follows(FOLLOWS_JSON)
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                            params={"offset": state.get("last_update_id", 0) + 1, "timeout": 0,
                                    "allowed_updates": json.dumps(["message"])},
                            timeout=20)
    except Exception as e:  # the message may contain the URL with the token
        print(f"getUpdates failed: {type(e).__name__}")
        return 1
    if resp.status_code == 409:
        print("getUpdates conflict (409): a webhook or another poller uses this bot.")
        return 1
    body = resp.json() if resp.ok else {}
    if not body.get("ok"):
        print(f"getUpdates failed: HTTP {resp.status_code}")
        return 1

    with open(os.path.join(HERE, "all_tickers.json"), encoding="utf-8") as f:
        known = set(json.load(f))
    ended = ended_follows_from_file()
    new_state, reply = apply_updates(state, body.get("result", []), chat_id, known, ended)
    if new_state != state:
        save_follows(FOLLOWS_JSON, new_state)
    if reply:
        telegram_sender(token, chat_id)(reply)
    print(f"processed {len(body.get('result', []))} update(s); "
          f"picks {len(active_follows(new_state['events'], ended))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
