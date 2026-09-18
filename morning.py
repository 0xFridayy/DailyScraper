"""Morning entry point for daily-scrape.yml: run the unchanged scrape, then
send ONE message with picks and follow-ups instead of the raw NeoBDM list.

neobdm_scraper.run_all_jobs() is not edited. Its send_telegram is swapped for
a holder while it runs: "NeoBDM error" messages still go out immediately,
the raw daily report is held. If the picks step fails for any reason, the
held raw report is sent instead, so a morning message is never lost.

Always exits 0 like the scraper did, so the workflow's commit step still
saves the day's data. Roll back by pointing the workflow at
`python neobdm_scraper.py --now` again.
"""

import traceback
from datetime import datetime, timezone

import neobdm_scraper as scraper
import daily_picks


def main():
    real_send = scraper.send_telegram
    held = []

    def hold(message):
        if str(message).startswith("NeoBDM error"):
            real_send(message)
        else:
            held.append(message)

    scraper.send_telegram = hold
    try:
        scraper.run_all_jobs()
    finally:
        scraper.send_telegram = real_send

    try:
        status, _ = daily_picks.run_morning(datetime.now(timezone.utc),
                                            send=daily_picks.telegram_sender_from_env())
        print(f"daily picks: {status}")
        if status == "send_failed" and held:
            real_send(held[-1])
    except Exception as e:
        print(f"daily picks failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        if held:
            real_send(f"{held[-1]}\n\n⚠️ Picks step failed today ({type(e).__name__}); "
                      "this is the raw NeoBDM list instead.")
    return 0


if __name__ == "__main__":
    main()
