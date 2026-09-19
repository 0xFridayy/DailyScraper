"""Morning entry point for daily-scrape.yml: run the unchanged scrape, then
send ONE message with picks and follow-ups instead of the raw NeoBDM list.

neobdm_scraper.run_all_jobs() is not edited. Its send_telegram is swapped for
a holder while it runs: "NeoBDM error" messages still go out immediately,
the raw daily report is held. If the picks step fails, finds no fresh data,
or can't send, the held raw report is sent instead.

Nothing here may stop the workflow's commit step: picks code is imported only
after the scrape, every send is guarded, and the script always exits 0 (as
`neobdm_scraper.py --now` did). Errors print only their type, because request
errors can contain the bot URL. Roll back by pointing the workflow at
`python neobdm_scraper.py --now` again.
"""

from datetime import datetime, timezone

import neobdm_scraper as scraper


def main():
    real_send = scraper.send_telegram
    held = []

    def safe_send(text):
        try:
            real_send(text)
        except Exception as e:
            print(f"fallback send failed: {type(e).__name__}")

    def hold(message):
        if str(message).startswith("NeoBDM error"):
            safe_send(message)
        else:
            held.append(message)

    scraper.send_telegram = hold
    try:
        scraper.run_all_jobs()
    finally:
        scraper.send_telegram = real_send

    try:
        import daily_picks
        status, _ = daily_picks.run_morning(datetime.now(timezone.utc),
                                            send=daily_picks.telegram_sender_from_env())
        print(f"daily picks: {status}")
        if status in ("send_failed", "stale") and held:
            safe_send(held[-1])
    except Exception as e:
        print(f"daily picks failed: {type(e).__name__}")
        if held:
            safe_send(f"{held[-1]}\n\n⚠️ Picks step failed today ({type(e).__name__}); "
                      "this is the raw NeoBDM list instead.")
    return 0


if __name__ == "__main__":
    main()
