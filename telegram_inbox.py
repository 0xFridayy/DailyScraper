"""Read the owner's Telegram replies and keep telegram_follows.json current.

Commands (case doesn't matter, a leading "/" and "@botname" are fine):
    yes BBCA [BBRI ...]   start following
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
                         load_follows, save_follows, telegram_sender)

COMMAND = re.compile(r"^\s*/?(yes|stop|list)(?:@\w+)?(?:\s+(.*))?$", re.I | re.S)


def parse_command(text):
    """('yes'|'stop'|'list', [TICKERS]) or None for anything else."""
    match = COMMAND.match(text or "")
    if not match:
        return None
    tickers = [t.upper() for t in re.findall(r"\b[A-Za-z]{4}\b", match.group(2) or "")]
    return match.group(1).lower(), tickers


def apply_updates(state, updates, chat_id, known):
    """Pure: apply Telegram updates to the follow state.
    Returns (new_state, reply_text or None). Other chats are ignored, and an
    update already applied (same update_id) is never applied twice."""
    events = list(state.get("events", []))
    last = state.get("last_update_id", 0)
    done = {e.get("update_id") for e in events}
    active = active_follows(events)
    added, stopped, unknown, full, listed = [], [], [], [], False

    for update in sorted(updates, key=lambda u: u["update_id"]):
        last = max(last, update["update_id"])
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            continue
        command = parse_command(msg.get("text"))
        if command is None or update["update_id"] in done:
            continue
        action, tickers = command
        at = datetime.fromtimestamp(msg.get("date", 0), timezone.utc).isoformat()
        if action == "list":
            listed = True
            continue
        for ticker in tickers:
            if ticker not in known:
                unknown.append(ticker)
            elif action == "yes" and ticker not in active:
                if len(active) >= MAX_FOLLOWS:
                    full.append(ticker)
                    continue
                events.append({"update_id": update["update_id"], "at": at,
                               "action": "yes", "ticker": ticker})
                active[ticker] = at
                added.append(ticker)
            elif action == "stop" and ticker in active:
                events.append({"update_id": update["update_id"], "at": at,
                               "action": "stop", "ticker": ticker})
                del active[ticker]
                stopped.append(ticker)

    lines = []
    if added:
        lines.append(f"✅ Following: {', '.join(added)} - you'll see them in the next morning message.")
    if stopped:
        lines.append(f"🛑 Stopped: {', '.join(stopped)}")
    if unknown:
        lines.append(f"⚠️ Not an IDX ticker I know: {', '.join(unknown)}")
    if full:
        lines.append(f"⚠️ Already following {MAX_FOLLOWS} (the max). Stop one first: {', '.join(full)}")
    if lines or listed:
        names = ", ".join(active) if active else "nothing yet"
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
    new_state, reply = apply_updates(state, body.get("result", []), chat_id, known)
    if new_state != state:
        save_follows(FOLLOWS_JSON, new_state)
    if reply:
        telegram_sender(token, chat_id)(reply)
    print(f"processed {len(body.get('result', []))} update(s); "
          f"following {len(active_follows(new_state['events']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
