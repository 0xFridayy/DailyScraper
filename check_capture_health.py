"""
Daily health check on the ML capture panel.

The point of this script is to fail LOUDLY and EARLY. The signal evaluation
needs weeks of accumulated panel data, and the expensive failure mode is not a
crash -- it's the capture quietly degrading (filters stop applying, columns come
back null, the screener silently reverts) and nobody noticing until the report
is due and two weeks of data turn out to be unusable.

So this checks the shape of the data, not just its presence, and reports what to
hand back to Claude when something is wrong.

Exit code is non-zero when unhealthy, so the workflow also goes red.

Run:  python check_capture_health.py            -> print status
      python check_capture_health.py --telegram -> also send it
"""

import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")

# The capture columns that must actually carry values. Anything here coming back
# mostly-null means the screener config regressed even though rows still land.
REQUIRED_FIELDS = ["close", "high", "low", "m_dn_0", "nr_dn_0", "f_dn_0",
                   "m_cn_5", "m_dn_3", "clean_score", "tval", "market_cap_t",
                   "pct_5"]
MIN_ROWS, MAX_ROWS = 80, 400   # sane band for the filtered universe (~200)
MIN_COVERAGE = 0.90            # per-field non-null fraction on the latest day
MAX_STALE_WEEKDAYS = 2         # allows one public holiday before alerting
TARGET_DATE = date(2026, 8, 30)
TARGET_OUTCOMES = 30           # what evaluate_signals.py needs to say anything


def _load_dotenv():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def weekdays_between(d1, d2):
    return sum(1 for i in range((d2 - d1).days)
               if (d1 + timedelta(days=i + 1)).weekday() < 5)


def check(conn):
    """Returns (problems, notes, stats). Empty problems == healthy."""
    problems, notes = [], []

    dates = [r[0] for r in conn.execute(
        "select distinct date from market_summary_daily order by date")]
    if not dates:
        return ["panel is EMPTY — no market_summary_daily rows at all"], [], {}

    latest = dates[-1]
    today = date.today()
    try:
        latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        return [f"unparseable panel date {latest!r}"], [], {}

    stale = weekdays_between(latest_d, today)
    if stale > MAX_STALE_WEEKDAYS:
        problems.append(
            f"panel is STALE — newest day is {latest} ({stale} weekdays ago). "
            f"The nightly scrape has probably been failing.")

    rows = conn.execute(
        "select count(*) from market_summary_daily where date=?", (latest,)
    ).fetchone()[0]
    if rows < MIN_ROWS:
        problems.append(f"only {rows} rows on {latest} (expected ~200). Filters "
                        f"may be over-restrictive, or the walk was cut short.")
    elif rows > MAX_ROWS:
        problems.append(f"{rows} rows on {latest} (expected ~200). The gorengan "
                        f"filters are probably not applying — check the screener "
                        f"PATCH response for success=False.")

    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")}
    for f in REQUIRED_FIELDS:
        if f not in cols:
            problems.append(f"column {f} missing from the table entirely")
            continue
        n = conn.execute(
            f'select count("{f}") from market_summary_daily where date=?',
            (latest,)).fetchone()[0]
        cov = n / rows if rows else 0
        if cov < MIN_COVERAGE:
            problems.append(f"{f} only {cov:.0%} populated on {latest} — the "
                            f"screener column set has regressed")

    # Warrants/rights leaking in means the universe reverted to ALL.
    junk = [r[0] for r in conn.execute(
        "select ticker from market_summary_daily where date=?", (latest,))
        if not (r[0].isalpha() and len(r[0]) <= 4)]
    if junk:
        problems.append(f"{len(junk)} non-equity codes on {latest} "
                        f"(e.g. {', '.join(junk[:3])}) — universe reverted to ALL")

    # Signals must still be landing, and be measurable against the panel.
    try:
        sig = [r[0] for r in conn.execute(
            "select ticker from konglo_signal_watch where flag_date=?", (latest,))]
    except sqlite3.OperationalError:
        sig = []
        problems.append("konglo_signal_watch table is missing")
    if not sig:
        problems.append(f"no signals logged for {latest} — record_konglo_signals "
                        f"is not firing")
    else:
        panel = {r[0] for r in conn.execute(
            "select ticker from market_summary_daily where date=?", (latest,))}
        gap = [t for t in sig if t not in panel]
        if gap:
            notes.append(f"{len(gap)} signalled name(s) outside the panel "
                         f"({', '.join(gap[:4])}) — not measurable")

    # Progress toward the first real evaluation.
    try:
        total_sig = conn.execute(
            "select count(*) from konglo_signal_watch where flag_date >= ?",
            (dates[0],)).fetchone()[0]
    except sqlite3.OperationalError:
        total_sig = 0
    per_day = total_sig / max(len(dates), 1)
    left = weekdays_between(today, TARGET_DATE) if today < TARGET_DATE else 0
    projected = int(total_sig + per_day * left)
    stats = {"latest": latest, "rows": rows, "days": len(dates),
             "signals": total_sig, "projected": projected, "left": left}
    if today < TARGET_DATE and projected < TARGET_OUTCOMES:
        notes.append(f"on track for only ~{projected} signals by {TARGET_DATE} "
                     f"(need {TARGET_OUTCOMES}+)")
    return problems, notes, stats


def format_report(problems, notes, stats):
    if not stats:
        head = "🔴 CAPTURE BROKEN"
    elif problems:
        head = "🔴 CAPTURE UNHEALTHY"
    else:
        head = "🟢 capture OK"

    lines = [head]
    if stats:
        lines.append(f"{stats['latest']}: {stats['rows']} rows | panel "
                     f"{stats['days']}d | {stats['signals']} signals "
                     f"(~{stats['projected']} by {TARGET_DATE})")
    for p in problems:
        lines.append(f"❌ {p}")
    for n in notes:
        lines.append(f"⚠️ {n}")
    if problems:
        lines += ["", "Tell Claude: \"the NeoBDM capture health check is failing "
                      "with the above — check the screener PATCH response and "
                      "the DAILY_SCRAPER config\"."]
    return "\n".join(lines)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — not sending")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": message,
                            "disable_web_page_preview": True}, timeout=15)
    print("sent to Telegram" if r.ok else f"telegram error {r.status_code}: {r.text}")


def main():
    _load_dotenv()
    conn = sqlite3.connect(DB_PATH)
    try:
        problems, notes, stats = check(conn)
    finally:
        conn.close()
    report = format_report(problems, notes, stats)
    print(report)
    if "--telegram" in sys.argv:
        send_telegram(report)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
