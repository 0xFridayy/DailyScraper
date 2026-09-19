"""Read the owner's Telegram replies and keep telegram_follows.json current.

Commands (case doesn't matter, a leading "/" and "@botname" are fine):
    yes BBCA [BBRI ...]   start following until you say stop
    yes BBCA 5d           follow for 5 trading days (also 2w, 1m,
                          "10 hari", "2 minggu", "1 bulan"); sending it again
                          for a followed stock only changes the duration
    stop BBCA             stop following
    list                  show what is followed

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
DURATION = re.compile(r"\b(\d{1,3})\s*(days?|hari|weeks?|wks?|minggu|months?|mo|bulan|d|w|m)\b",
                      re.I)
UNIT_DAYS = {"d": 1, "day": 1, "days": 1, "hari": 1,
             "w": 5, "wk": 5, "wks": 5, "week": 5, "weeks": 5, "minggu": 5,
             "m": 21, "mo": 21, "month": 21, "months": 21, "bulan": 21}
MAX_DAYS = 250   # about a year of trading days


def parse_command(text):
    """('yes'|'stop'|'list', [TICKERS], trading days or None), or None for
    anything else. Durations count trading days: 1w = 5, 1m = 21."""
    match = COMMAND.match(text or "")
    if not match:
        return None
    rest, days = match.group(2) or "", None
    found = DURATION.search(rest)
    if found:
        n = int(found.group(1)) * UNIT_DAYS[found.group(2).lower()]
        days = n if 0 < n <= MAX_DAYS else None
        rest = rest[:found.start()] + " " + rest[found.end():]
    tickers = [t.upper() for t in re.findall(r"\b[A-Za-z]{4}\b", rest)]
    return match.group(1).lower(), tickers, days


def how_long(days):
    return "until you say stop" if days is None else f"{days} trading days"


def apply_updates(state, updates, chat_id, known, ended=frozenset()):
    """Pure: apply Telegram updates to the follow state.
    Returns (new_state, reply_text or None). Other chats are ignored, and an
    update already applied (same update_id) is never applied twice. `ended`
    = timed follows that already finished (they don't count toward the max)."""
    events = list(state.get("events", []))
    last = state.get("last_update_id", 0)
    done = {e.get("update_id") for e in events}
    active = active_follows(events, ended)
    added, changed, already, stopped, unknown, full = [], [], [], [], [], []
    listed = False

    for update in sorted(updates, key=lambda u: u["update_id"]):
        last = max(last, update["update_id"])
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            continue
        command = parse_command(msg.get("text"))
        if command is None or update["update_id"] in done:
            continue
        action, tickers, days = command
        at = datetime.fromtimestamp(msg.get("date", 0), timezone.utc).isoformat()
        if action == "list":
            listed = True
            continue
        for ticker in tickers:
            event = {"update_id": update["update_id"], "at": at, "action": action,
                     "ticker": ticker}
            if ticker not in known:
                unknown.append(ticker)
            elif action == "yes" and ticker in active:
                if days is not None and days != active[ticker]["days"]:
                    events.append(dict(event, days=days))
                    active[ticker]["days"] = days
                    changed.append((ticker, days))
                else:
                    already.append(ticker)
            elif action == "yes":
                if len(active) >= MAX_FOLLOWS:
                    full.append(ticker)
                    continue
                events.append(dict(event, days=days) if days is not None else event)
                active[ticker] = {"at": at, "days": days}
                added.append((ticker, days))
            elif action == "stop" and ticker in active:
                events.append(event)
                del active[ticker]
                stopped.append(ticker)

    def named(pairs):
        return ", ".join(f"{t} ({how_long(d)})" for t, d in pairs)

    lines = []
    if added:
        lines.append(f"✅ Following: {named(added)} - you'll see them in the next morning message.")
    if changed:
        lines.append(f"⏱ Changed: {named(changed)}, counted from your first yes.")
    if already:
        lines.append(f"ℹ️ Already following: {', '.join(already)}")
    if stopped:
        lines.append(f"🛑 Stopped: {', '.join(stopped)}")
    if unknown:
        lines.append(f"⚠️ Not an IDX ticker I know: {', '.join(unknown)}")
    if full:
        lines.append(f"⚠️ Already following {MAX_FOLLOWS} (the max). Stop one first: {', '.join(full)}")
    if lines or listed:
        names = named((t, f["days"]) for t, f in active.items()) if active else "nothing yet"
        lines.append(f"👀 Following ({len(active)}/{MAX_FOLLOWS}): {names}")
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
          f"following {len(active_follows(new_state['events'], ended))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
