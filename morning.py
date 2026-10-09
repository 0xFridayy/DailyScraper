"""Scheduled scrape followed by certified picks or a nonfinancial status report.

Unsupported analytics produce an operational report through the scraper sender.
Held raw price reports never replace unavailable analytics. Delivery is guarded;
only supported picks construct the credential-based daily_picks sender.
"""

from datetime import datetime, timezone
import json

from price_contract import CONTRACT_VERSION, UnsupportedPriceContract

import neobdm_scraper as scraper


def retry_stale_warning(send, text):
    """One more attempt at the stale warning, through the picks sender. True
    only if it was delivered. Never substitutes the raw report; a second
    failure is only printed."""
    try:
        ok = bool(send(text))
    except Exception as e:
        print(f"stale warning retry failed: {type(e).__name__}")
        return False
    print(f"stale warning retry: {'sent' if ok else 'failed'}")
    return ok


def main():
    real_send = scraper.send_telegram
    held = []

    def safe_send(text):
        try:
            real_send(text)
        except Exception as e:
            print(f"operational send failed: {type(e).__name__}")

    def hold(message):
        if str(message).startswith("NeoBDM error"):
            safe_send(message)
        else:
            held.append(message)

    def report_operational(status, reason, refusal=None):
        # Capturing a report proves neither freshness nor certified source basis.
        # Do not inspect a database or reproduce price values to fill that gap.
        scrape_status = "REPORT_CAPTURED" if held else "NO_REPORT_CAPTURED"
        report = {"analytics": status, "scrape": scrape_status,
                  "data_health": "UNVERIFIED", "reason": reason}
        text = ("Morning operational status\n"
                f"Scrape: {scrape_status}; data health: UNVERIFIED.\n"
                f"Analytics: {status}.\n{reason}\n"
                "Picks and financial returns withheld.")
        payload = {"operational_report": report}
        if refusal is not None:
            payload["daily_picks"] = refusal
        print(json.dumps(payload, sort_keys=True))
        safe_send(text)

    scraper.send_telegram = hold
    try:
        scraper.run_all_jobs()
    finally:
        scraper.send_telegram = real_send

    try:
        import daily_picks
        sender = None

        def send(text):
            nonlocal sender
            if sender is None:
                sender = daily_picks.telegram_sender_from_env()
            return sender(text)

        run_utc = datetime.now(timezone.utc)
        status, text = daily_picks.run_morning(run_utc, send=send)
        print(f"daily picks: {status}")
        if status == "send_failed":
            report_operational("DELIVERY_FAILED", "Picks delivery failed; financial report withheld.")
        elif status == "stale_send_failed" and text:
            if retry_stale_warning(send, text):
                # A recording error does not change whether the warning was sent.
                try:
                    daily_picks.record_stale_warning(run_utc, text, datetime.now(timezone.utc))
                except Exception as e:
                    print(f"stale warning record failed: {type(e).__name__}")
    except UnsupportedPriceContract as exc:
        result = exc.as_dict()
        if (exc.consumer != "daily_picks.run_morning" or exc.status != "UNSUPPORTED"
                or exc.contract_version != CONTRACT_VERSION):
            result["status"] = "CONTRACT_ERROR"
        report_operational(result["status"],
            "Certified source basis, holding windows and output identity are required.", result)
    except Exception as e:
        report_operational("ANALYTICS_FAILED", f"Picks step failed ({type(e).__name__}).")
    return 0


if __name__ == "__main__":
    main()
