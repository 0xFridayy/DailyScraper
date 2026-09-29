"""Morning entry point for daily-scrape.yml: run the scrape, then send ONE
message with picks and follow-ups instead of the raw NeoBDM list.

neobdm_scraper.run_all_jobs() runs as-is, but its send_telegram is swapped for
a holder while it runs: "NeoBDM error" messages still go out immediately, the
raw daily report is held. If the picks step fails or can't send its picks
("send_failed"), the held raw report is sent instead.

When the picks step finds no fresh data, its stale warning is the only
message. The held raw report is never sent then, because it would present a
late or failed scrape as if it were a fresh report: "stale" means the warning
went out; "stale_send_failed" means it did not, and only that warning text is
retried, once.

Nothing here may stop the workflow's commit step: picks code is imported only
after the scrape, every send is guarded, and the script always exits 0 (as
`neobdm_scraper.py --now` did). Errors print only their type, because request
errors can contain the bot URL. Roll back by pointing the workflow at
`python neobdm_scraper.py --now` again.
"""

from datetime import datetime, timezone

import neobdm_scraper as scraper


def retry_stale_warning(send, text):
    """One more attempt at the stale warning, through the picks sender. Never
    substitutes the raw report; a second failure is only printed."""
    try:
        ok = send(text)
    except Exception as e:
        print(f"stale warning retry failed: {type(e).__name__}")
        return
    print(f"stale warning retry: {'sent' if ok else 'failed'}")


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
        send = daily_picks.telegram_sender_from_env()
        status, text = daily_picks.run_morning(datetime.now(timezone.utc), send=send)
        print(f"daily picks: {status}")
        if status == "send_failed" and held:
            safe_send(held[-1])
        elif status == "stale_send_failed" and text:
            retry_stale_warning(send, text)
    except Exception as e:
        print(f"daily picks failed: {type(e).__name__}")
        if held:
            safe_send(f"{held[-1]}\n\n⚠️ Picks step failed today ({type(e).__name__}); "
                      "this is the raw NeoBDM list instead.")
    return 0


if __name__ == "__main__":
    main()
