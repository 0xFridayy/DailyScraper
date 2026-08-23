"""
Post-scrape correctness gate: is the data we just captured actually RIGHT?

check_capture_health.py already answers "did rows arrive, and are the columns
populated". That is a different question from "are the values correct", and the
gap between the two is exactly where this project has already been burned: the
cross-ticker contamination in price_history passed every shape check for months
because the rows were present, the columns were full, and the numbers looked
like plausible prices. They were just the wrong stock's prices.

NeoBDM is a third-party site that changes without notice - daily-scrape.yml
already carries a note about market_summary/dashboard scrapes silently failing
after a suspected site update. So the checks here are deliberately built to
catch a scrape that SUCCEEDS but returns something different from what it
returned yesterday.

The strongest check available is cross-source agreement. market_summary_daily
(screener API) and price_history (inventory Plotly chart) are two independent
scrape paths that both carry close/high/low/volume for overlapping tickers. If
one path breaks or starts returning another ticker's series, the two disagree.
Nothing else in this repo compares them.

That comparison needs one correction first, discovered by building it:

  market_summary_daily.date is the SCRAPE date, not the data date.

The scrape runs 23:00 UTC = 07:00 Asia/Kuala_Lumpur, before the market opens,
so the freshest screener data is the PREVIOUS session's close. Aligning
market_summary_daily.date - 1 day against price_history.date matches 34 of 37
overlapping pairs exactly (median deviation 0.00%); aligning them directly
matches 4 of 36. All three residual mismatches are known-contaminated tickers
(COIN, ELTY), so the offset is the alignment and the leftovers are the real
defects. The `last_date` column, which presumably should carry the data date,
is NULL for every row.

Nothing currently joins the two tables, so this is not an active bug - it is a
landmine for HANDOFF.md stage 5, which puts daily-signal tickers into
TRACKED_TICKERS and would then join signals to price_history by date. The
offset is asserted below so that a change in NeoBDM's publish timing (or in
when the workflow runs) fails loudly instead of silently shifting every label
by one day.

Run:  py check_signal_integrity.py            -> print status
      py check_signal_integrity.py --telegram -> also send it
Exit code is non-zero when something is wrong, so the workflow goes red too.
"""

import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from price_audit import detect, load

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")

WINDOW_DAYS = 10          # only recent scrapes; older rows are quarantine's job
MAX_STALE_WEEKDAYS = 2    # allows one public holiday before alerting

# Cross-source agreement. Both paths quote the same exchange, so a real match is
# exact; the tolerance only absorbs float round-tripping.
EXPECTED_DATE_OFFSET = 1  # market_summary_daily.date - 1 == price_history.date
CLOSE_TOLERANCE = 0.001   # 0.1%
MIN_AGREEMENT = 0.85      # below this, one of the two scrape paths has drifted
MIN_PAIRS_TO_JUDGE = 15   # under this the sample is too thin to fail a build on

# Columns the daily signal is actually computed from, and that the screener is
# observed to populate. Deliberately NOT the full filter set: is_liquid /
# is_pinky / is_crossing are inputs to the server-side GORENGAN_FILTERS, not
# fields the API returns, and market_summary_daily has never carried `volume`
# either - all four are NULL on every captured date. Listing them here would
# fail the build on day one for a condition that has always held.
CRITICAL_FIELDS = ["close", "high", "low", "tval", "clean_score",
                   "m_dn_0", "nr_dn_0", "f_dn_0", "market_cap_t", "pct_5"]
MIN_COVERAGE = 0.90

# Everything else is checked against its OWN history instead of a hardcoded
# list, so a column that NeoBDM starts returning empty is caught without anyone
# having to predict which column it will be. Self-calibrating: a field that was
# always NULL stays ignored; a field that was full and goes empty fails.
MIN_BASELINE_COVERAGE = 0.90
MIN_BASELINE_DAYS = 2

# A signalled ticker with no captured close cannot be scored. Some churn is
# normal and structural: the screener panel applies liquidity filters, so a
# tracked name can drop out on any given day (TPIA on 2026-08-23, for instance,
# is in price_history but missed the panel). Measured over 115 signals it runs
# at 7%, and 0% on most days. Failing on one name would make this red most
# mornings for a condition nobody can act on; a threshold still catches the
# panel actually shrinking or the signal source drifting off-universe.
MAX_UNMEASURABLE_SIGNALS = 0.30


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


def _recent_dates(conn, table, n):
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT date FROM {table} ORDER BY date DESC LIMIT ?", (n,))]


def _shift(datestr, days):
    return (datetime.strptime(datestr, "%Y-%m-%d").date()
            - timedelta(days=days)).strftime("%Y-%m-%d")


# ── individual checks ─────────────────────────

def check_freshness(conn, problems):
    today = date.today()
    for table in ("market_summary_daily", "price_history", "broker_flow"):
        row = conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()[0]
        if not row:
            problems.append(f"{table} is EMPTY")
            continue
        try:
            latest = datetime.strptime(row, "%Y-%m-%d").date()
        except ValueError:
            problems.append(f"{table} has unparseable date {row!r}")
            continue
        stale = weekdays_between(latest, today)
        if stale > MAX_STALE_WEEKDAYS:
            problems.append(f"{table} STALE — newest {row} ({stale} weekdays ago)")


def check_new_contamination(conn, problems, notes, stats):
    """Unquarantined suspect rows in the recent window.

    "Unquarantined" is not the same as "newly written", and conflating the two
    made this alert blame the wrong thing. On 2026-08-23 it reported rows dated
    08-06 through 08-13 as the scraper "writing bad rows again" while
    price_history had in fact been frozen at 08-20 for two days because the
    topup was failing — the scraper had written nothing at all. An alert that
    misattributes the cause every morning is one people learn to skip.

    So the row age is interpreted against whether the scrape is actually
    advancing, and the absence of a quarantine baseline is called out as its own
    condition rather than silently inflating the count.
    """
    window = set(_recent_dates(conn, "price_history", WINDOW_DAYS))
    if not window:
        return

    px = detect(load(conn))
    recent = px[px["date"].isin(window)]

    has_q = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='price_quarantine'"
    ).fetchone()[0]
    known = set()
    if has_q:
        known = set(map(tuple, conn.execute(
            "SELECT date, ticker FROM price_quarantine").fetchall()))

    flagged = recent[recent["suspect"] & ~pd.Series(
        [(d, t) in known for d, t in zip(recent["date"], recent["ticker"])],
        index=recent.index)]

    stats["window_rows"] = len(recent)
    stats["fresh_suspects"] = len(flagged)
    if not len(flagged):
        return

    by_reason = {r: int(flagged[r].sum()) for r in
                 ("limit_violation", "cross_ticker_dup", "series_break")
                 if flagged[r].any()}
    sample = ", ".join(f"{r.date} {r.ticker}" for r in flagged.head(4).itertuples())

    # Is the scrape actually writing? If price_history has not advanced, nothing
    # in it can be a fresh write, whatever the quarantine table does or does not
    # contain.
    latest = conn.execute("SELECT MAX(date) FROM price_history").fetchone()[0]
    try:
        stale_days = weekdays_between(
            datetime.strptime(latest, "%Y-%m-%d").date(), date.today())
    except (TypeError, ValueError):
        stale_days = 0

    if not has_q:
        notes.append(
            f"{len(flagged)} suspect row(s) in the last {WINDOW_DAYS} scrape days "
            f"({by_reason}) — e.g. {sample}. No price_quarantine table exists, so "
            f"the whole backlog counts as unresolved. Run `py price_audit.py "
            f"quarantine` once to set the baseline; after that this only fires on "
            f"rows the scrape adds.")
    elif stale_days > MAX_STALE_WEEKDAYS:
        notes.append(
            f"{len(flagged)} unquarantined suspect row(s) ({by_reason}) — e.g. "
            f"{sample}. These are BACKLOG, not new damage: price_history has not "
            f"advanced past {latest} in {stale_days} weekdays, so the scraper has "
            f"written nothing. The staleness itself is the problem to chase.")
    else:
        problems.append(
            f"{len(flagged)} NEW contaminated price_history row(s) in the last "
            f"{WINDOW_DAYS} scrape days ({by_reason}) — e.g. {sample}. The scrape "
            f"IS advancing (newest {latest}), so the ticker-selection defect is "
            f"writing bad rows again.")


def check_cross_source(conn, problems, notes, stats):
    """market_summary_daily (screener API) vs price_history (inventory chart).

    Two independent scrape paths for the same numbers. Also re-derives the date
    offset rather than trusting it, so a change in publish timing surfaces as
    its own message instead of masquerading as mass contamination.
    """
    ms = pd.read_sql(
        "SELECT date, ticker, close FROM market_summary_daily WHERE close IS NOT NULL", conn)
    ph = pd.read_sql(
        "SELECT date, ticker, close FROM price_history WHERE close IS NOT NULL", conn)
    if ms.empty or ph.empty:
        notes.append("cross-source check skipped — one of the tables is empty")
        return

    best, scores = None, {}
    for offset in (0, 1, 2):
        m = ms.copy()
        m["date"] = m["date"].map(lambda d: _shift(d, offset))
        j = m.merge(ph, on=["date", "ticker"], suffixes=("_ms", "_ph"))
        j = j[(j["close_ms"] > 0) & (j["close_ph"] > 0)]
        if len(j) < MIN_PAIRS_TO_JUDGE:
            scores[offset] = (len(j), 0.0)
            continue
        agree = ((j["close_ph"] / j["close_ms"] - 1).abs() <= CLOSE_TOLERANCE).mean()
        scores[offset] = (len(j), agree)
        if best is None or agree > scores[best][1]:
            best = offset

    stats["offset_scores"] = {k: (n, round(a, 3)) for k, (n, a) in scores.items()}
    if best is None:
        notes.append(f"cross-source check skipped — fewer than {MIN_PAIRS_TO_JUDGE} "
                     f"overlapping (date,ticker) pairs at any offset")
        return

    n_best, agree_best = scores[best]
    stats["cross_source"] = f"offset={best}d n={n_best} agree={agree_best:.0%}"

    if best != EXPECTED_DATE_OFFSET:
        problems.append(
            f"date alignment CHANGED — market_summary_daily now matches "
            f"price_history at offset {best}d, not the expected "
            f"{EXPECTED_DATE_OFFSET}d (agreement {agree_best:.0%} on {n_best} pairs). "
            f"NeoBDM's publish timing or the workflow schedule moved; every "
            f"date-joined label is shifted until EXPECTED_DATE_OFFSET is updated.")
        return

    if agree_best < MIN_AGREEMENT:
        m = ms.copy()
        m["date"] = m["date"].map(lambda d: _shift(d, best))
        j = m.merge(ph, on=["date", "ticker"], suffixes=("_ms", "_ph"))
        j = j[(j["close_ms"] > 0) & (j["close_ph"] > 0)].copy()
        j["dev"] = (j["close_ph"] / j["close_ms"] - 1).abs()
        worst = j.nlargest(4, "dev")
        sample = ", ".join(
            f"{r.date} {r.ticker} {r.close_ms:g}vs{r.close_ph:g}" for r in worst.itertuples())
        problems.append(
            f"the two scrape paths DISAGREE — only {agree_best:.0%} of {n_best} "
            f"overlapping closes match (need {MIN_AGREEMENT:.0%}). One of the "
            f"screener API or the inventory chart has drifted. Worst: {sample}")


def check_schema_and_coverage(conn, problems, notes, stats):
    """A NeoBDM API change shows up as a column vanishing or going all-null."""
    dates = _recent_dates(conn, "market_summary_daily", WINDOW_DAYS)
    if not dates:
        problems.append("market_summary_daily is EMPTY")
        return
    latest, baseline = dates[0], dates[1:]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")]
    rows = conn.execute(
        "SELECT COUNT(*) FROM market_summary_daily WHERE date=?", (latest,)).fetchone()[0]
    stats["latest_ms"] = latest
    stats["ms_rows"] = rows
    if not rows:
        problems.append(f"market_summary_daily has no rows on {latest}")
        return

    missing = [f for f in CRITICAL_FIELDS if f not in cols]
    if missing:
        problems.append(f"signal field(s) GONE from market_summary_daily: "
                        f"{', '.join(missing)} — the screener API changed shape")

    def coverage(field, on_date, total=None):
        n = conn.execute(f'SELECT COUNT("{field}") FROM market_summary_daily WHERE date=?',
                         (on_date,)).fetchone()[0]
        total = total or conn.execute(
            "SELECT COUNT(*) FROM market_summary_daily WHERE date=?", (on_date,)).fetchone()[0]
        return n / total if total else 0.0

    thin = [f"{f} {coverage(f, latest, rows):.0%}" for f in CRITICAL_FIELDS
            if f not in missing and coverage(f, latest, rows) < MIN_COVERAGE]
    if thin:
        problems.append(f"critical field(s) mostly NULL on {latest}: {', '.join(thin)} "
                        f"— the screener returned the column but stopped filling it")

    # Self-calibrating sweep over every other column.
    if len(baseline) < MIN_BASELINE_DAYS:
        notes.append(f"coverage-regression sweep skipped — only {len(baseline)} "
                     f"baseline day(s), need {MIN_BASELINE_DAYS}")
        return

    regressed = []
    for f in cols:
        if f in ("date", "ticker") or f in CRITICAL_FIELDS:
            continue
        base = sum(coverage(f, d) for d in baseline) / len(baseline)
        if base < MIN_BASELINE_COVERAGE:
            continue                      # never reliably populated — not our problem
        now = coverage(f, latest, rows)
        if now < MIN_COVERAGE:
            regressed.append(f"{f} {base:.0%}→{now:.0%}")

    stats["cols_swept"] = len(cols)
    if regressed:
        problems.append(
            f"{len(regressed)} column(s) were populated on the baseline days and "
            f"went empty on {latest}: {', '.join(regressed[:6])}"
            + (" …" if len(regressed) > 6 else "")
            + " — the screener API changed what it returns")


def check_signals_measurable(conn, problems, notes, stats):
    """A flagged ticker with no price captured can never be scored."""
    try:
        latest = conn.execute(
            "SELECT MAX(flag_date) FROM konglo_signal_watch").fetchone()[0]
    except sqlite3.OperationalError:
        problems.append("konglo_signal_watch table is missing")
        return
    if not latest:
        problems.append("konglo_signal_watch is EMPTY — signals are not being recorded")
        return

    sig = [r[0] for r in conn.execute(
        "SELECT ticker FROM konglo_signal_watch WHERE flag_date=?", (latest,))]
    priced = {r[0] for r in conn.execute(
        "SELECT ticker FROM market_summary_daily WHERE date=? AND close IS NOT NULL",
        (latest,))}
    stats["signals"] = len(sig)
    stats["latest_signal"] = latest

    if not sig:
        problems.append(f"no signals recorded for {latest} — record_konglo_signals "
                        f"is not firing")
        return
    gap = [t for t in sig if t not in priced]
    if not gap:
        return
    share = len(gap) / len(sig)
    detail = (f"{len(gap)} of {len(sig)} signalled ticker(s) on {latest} have no "
              f"captured close ({', '.join(gap[:4])}) — they can never be scored")
    if share > MAX_UNMEASURABLE_SIGNALS:
        problems.append(
            f"{detail}. That is {share:.0%}, past the {MAX_UNMEASURABLE_SIGNALS:.0%} "
            f"limit — the panel has shrunk or the signal source drifted off-universe.")
    else:
        notes.append(f"{detail} ({share:.0%}, normal panel churn)")


def check_value_sanity(conn, problems):
    """Cheap impossibilities, scoped to the window so history does not re-fire."""
    window = _recent_dates(conn, "price_history", WINDOW_DAYS)
    if not window:
        return
    qs = ",".join("?" * len(window))
    bad = conn.execute(
        f"SELECT COUNT(*) FROM price_history WHERE date IN ({qs}) AND "
        f"(close IS NULL OR close <= 0 OR volume < 0 OR high < low)", window).fetchone()[0]
    if bad:
        problems.append(f"{bad} price_history row(s) in the window are impossible "
                        f"(null/zero close, negative volume, or high < low)")


# ── reporting ─────────────────────────────────

def check(conn):
    problems, notes, stats = [], [], {}
    check_freshness(conn, problems)
    check_schema_and_coverage(conn, problems, notes, stats)
    check_new_contamination(conn, problems, notes, stats)
    check_cross_source(conn, problems, notes, stats)
    check_signals_measurable(conn, problems, notes, stats)
    check_value_sanity(conn, problems)
    return problems, notes, stats


def format_report(problems, notes, stats):
    head = "🔴 SIGNAL INTEGRITY FAILED" if problems else "🟢 signal integrity OK"
    lines = [head]
    if stats.get("latest_ms"):
        lines.append(f"{stats['latest_ms']}: {stats.get('ms_rows', 0)} panel rows | "
                     f"{stats.get('signals', 0)} signals | "
                     f"{stats.get('cross_source', 'cross-source n/a')}")
    if "fresh_suspects" in stats:
        lines.append(f"new contamination in last {WINDOW_DAYS}d: "
                     f"{stats['fresh_suspects']} of {stats.get('window_rows', 0)} rows")
    for p in problems:
        lines.append(f"❌ {p}")
    for n in notes:
        lines.append(f"⚠️ {n}")
    if problems:
        lines += ["", 'Tell Claude: "check_signal_integrity.py is failing with the '
                      'above — see HANDOFF.md stage 2 for the scraper defect."']
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
