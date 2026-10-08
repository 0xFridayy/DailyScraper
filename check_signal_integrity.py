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

Cross-source offset scores are exploratory. Screener capture dates, matching
closes and a best-scoring date offset do not verify an exchange session. The
monitor reports UNKNOWN_SOURCE_SESSION until an explicit source adapter can
supply that evidence. Corporate-action references do not align source sessions.

Price monitoring also reports boundaries, unresolved references, withheld
labels and per-ticker freshness against the verified exchange calendar.

Run:  py check_signal_integrity.py            -> print status
      py check_signal_integrity.py --telegram -> also send it
Exit code is non-zero when something is wrong, so the workflow goes red too:
1 = failed, 2 = SOURCE_CONTRACT_BREAK_ACKNOWLEDGED (see check_schema_and_coverage).
"""

import json
import math
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from statistics import median

import pandas as pd
import requests

import neobdm_source_contract as nsc
from price_audit import detect, load

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")

WINDOW_DAYS = 10          # only recent scrapes; older rows are quarantine's job
MAX_STALE_WEEKDAYS = 2    # allows one public holiday before alerting

# Exploratory agreement only; no source-session certification.
CLOSE_TOLERANCE = 0.001   # 0.1%
MIN_PAIRS_TO_JUDGE = 15   # minimum sample for an exploratory agreement score

# Columns the daily signal is actually computed from, and that the screener is
# observed to populate. Deliberately NOT the full filter set: is_liquid /
# is_pinky / is_crossing are inputs to the server-side GORENGAN_FILTERS, not
# fields the API returns, and market_summary_daily has never carried `volume`
# either - all four are NULL on every captured date. Listing them here would
# fail the build on day one for a condition that has always held.
CRITICAL_FIELDS = list(nsc.ACTIVE_CRITICAL_FIELDS)
MIN_COVERAGE = 0.90

# Every column of the ACTIVE request contract is judged on the latest capture by
# itself -- no baseline -- so a field that stays empty keeps failing every day.
#
# Other columns are checked against their OWN history, so a column NeoBDM starts
# returning empty is caught without predicting which. The history is the FULL
# history, counted in healthy days: on 2026-09-15 the old 9-day AVERAGE put the
# first empty day of is_unusual_volume into the baseline (8/9 = 89% < 90%), the
# field was classed "never reliably populated", and the check went green while the
# field was still gone. A bad day can no longer remove a healthy day.
#
# A field intentionally dropped from the contract must be RETIRED in
# neobdm_source_contract.FIELD_LIFECYCLE; it is then reported explicitly instead
# of failing forever or being silently forgotten. Health is judged against the
# contract the capture ran under (its manifest, or the legacy contract when it has
# none), so a capture taken while the field was still requested stays a
# SOURCE_CONTRACT_BREAK_ACKNOWLEDGED, never a retroactive green.
MIN_BASELINE_COVERAGE = 0.90
MIN_BASELINE_DAYS = 2

# Response cardinality vs the median of recent captures (normal range ~195-225).
CARDINALITY_BASELINE_DAYS = 10
MIN_ROW_RATIO, MAX_ROW_RATIO = 0.60, 1.67
MIN_ROWS_FOR_COLLAPSE = 20   # constant-field collapse needs a real cross-section

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
    from zoneinfo import ZoneInfo
    from idx_calendar import is_idx_session, latest_idx_session_before, IdxCalendarUnavailable
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()
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
    for ticker, latest in conn.execute("SELECT ticker, MAX(date) FROM price_history GROUP BY ticker"):
        try:
            expected = latest_idx_session_before(today)
            day = date.fromisoformat(latest)
            if not is_idx_session(day):
                problems.append(f"price_history {ticker}: freshness UNRESOLVED, observation is not a verified session")
                continue
            missing = 0
            while day < expected:
                day += timedelta(days=1)
                missing += int(is_idx_session(day))
            if missing > MAX_STALE_WEEKDAYS:
                problems.append(f"price_history {ticker} STALE: newest {latest}; {missing} verified sessions missing")
        except (ValueError, TypeError, IdxCalendarUnavailable):
            problems.append(f"price_history {ticker}: freshness UNRESOLVED, calendar/session unsupported")


def check_new_contamination(conn, problems, notes, stats, *, registry=None, representation=None):
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

    has_q = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='price_quarantine'"
    ).fetchone()[0]

    # Quarantined rows must not serve as the baseline the NEXT row's daily move
    # is measured against, or old damage keeps manufacturing fresh alarms: on
    # 2026-08-27 this reported a clean, unique MDIA row for 08-24 as the scraper
    # "writing bad rows again", purely because the close before it was KIOS's
    # price, flagged and quarantined two weeks earlier. See price_audit.detect.
    from price_audit import adjudicate_quarantine
    px, known, _ = adjudicate_quarantine(conn, registry=registry, representation=representation)
    recent = px[px["date"].isin(window)]

    flagged = recent[recent["suspect"] & ~pd.Series(
        [(d, t) in known for d, t in zip(recent["date"], recent["ticker"])],
        index=recent.index)]

    stats["corporate_action_boundaries"] = int(recent["corporate_action_boundary"].sum())
    stats["unresolved_limit_references"] = int(recent["limit_reference_status"].eq("UNRESOLVED").sum())
    stats["pending_action_references"] = int(recent["corporate_action_status"].eq("PENDING_REFERENCE").sum())
    stats["price_contract"] = px.attrs.get("price_contract")
    if stats["corporate_action_boundaries"]:
        notes.append(f"{stats['corporate_action_boundaries']} corporate-action boundary session(s); crossing economic returns withheld")
    if stats["unresolved_limit_references"]:
        notes.append(f"{stats['unresolved_limit_references']} unresolved limit reference(s); not certified price acceptance")
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

    scores = {}
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

    stats["offset_scores"] = {k: (n, round(a, 3)) for k, (n, a) in scores.items()}
    stats["cross_source_session_status"] = "UNKNOWN"
    stats["cross_source"] = "UNKNOWN_SOURCE_SESSION"
    notes.append("Cross-source offsets are exploratory only. Screener capture dates do not verify exchange sessions; no corporate-action exception or canonical equality is inferred.")


def _coverage_by_date(conn, field):
    return {d: (n / total if total else 0.0) for d, n, total in conn.execute(
        f'SELECT date, COUNT("{field}"), COUNT(*) FROM market_summary_daily GROUP BY date')}


def _storage_classes(conn, field, dates):
    """SQLite storage classes of non-null values; integer and real are one class."""
    if not dates:
        return set()
    qs = ",".join("?" * len(dates))
    kinds = {r[0] for r in conn.execute(
        f'SELECT DISTINCT typeof("{field}") FROM market_summary_daily '
        f'WHERE date IN ({qs}) AND "{field}" IS NOT NULL', dates)}
    return {"numeric" if k in ("integer", "real") else k for k in kinds}


def _contract_at_capture(conn, capture_date):
    """(contract_version, requested_columns) the capture on `capture_date` ran under.
    Its manifest says so; a capture without one predates the repair and ran under
    the legacy contract. No manifest is ever inferred for such a capture."""
    if nsc.table_exists(conn, "ms_capture_manifest"):
        row = conn.execute(
            "SELECT contract_version, requested_columns FROM ms_capture_manifest WHERE capture_date=? "
            "ORDER BY capture_started_utc DESC LIMIT 1", (capture_date,)).fetchone()
        if row:
            return row[0], json.loads(row[1])
    return nsc.LEGACY_CONTRACT_VERSION, list(nsc.LEGACY_REQUEST_COLUMNS)


def check_schema_and_coverage(conn, problems, notes, stats, lifecycle=None):
    """A NeoBDM contract change shows up as a column vanishing, going all-null,
    changing type, collapsing to one value, or the panel changing size."""
    lc = nsc.FIELD_LIFECYCLE if lifecycle is None else lifecycle
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM market_summary_daily ORDER BY date DESC")]
    if not dates:
        problems.append("market_summary_daily is EMPTY")
        return
    latest, history = dates[0], dates[1:]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")]
    rows = conn.execute(
        "SELECT COUNT(*) FROM market_summary_daily WHERE date=?", (latest,)).fetchone()[0]
    stats["latest_ms"] = latest
    stats["ms_rows"] = rows
    if not rows:
        problems.append(f"market_summary_daily has no rows on {latest}")
        return

    retired = [f for f in lc if nsc.lifecycle_state(f, latest, lc) == nsc.RETIRED]
    # HEALTH AT CAPTURE is judged against the contract that capture ran under, not
    # the one in force today. Historically critical fields stay critical wherever
    # that contract still requested them.
    contract_version, requested = _contract_at_capture(conn, latest)
    stats["contract_at_capture"] = contract_version
    critical = CRITICAL_FIELDS + [f for f in nsc.HISTORICAL_CRITICAL_FIELDS
                                  if (f in requested or f not in retired) and f not in CRITICAL_FIELDS]
    active = list(dict.fromkeys(list(requested) + critical))

    coverage = {f: _coverage_by_date(conn, f) for f in cols if f not in ("date", "ticker")}

    # A field the then-active contract requested but did not get, and that the
    # lifecycle registry has since RETIRED, is a source-contract break the human
    # retirement EXPLAINS -- not a healthy capture, and not an unexplained failure.
    breaks = [f for f in active if f in retired and f in requested
              and coverage.get(f, {}).get(latest, 0.0) < MIN_COVERAGE]
    judged = [f for f in active if f not in breaks]

    missing = [f for f in judged if f not in cols]
    if missing:
        problems.append(f"active contract field(s) GONE from market_summary_daily: "
                        f"{', '.join(missing)} — the screener API changed shape")

    thin = [f"{f} {coverage[f].get(latest, 0.0):.0%}" for f in judged
            if f not in missing and coverage[f].get(latest, 0.0) < MIN_COVERAGE]
    if thin:
        problems.append(f"active contract field(s) mostly NULL/absent on {latest}: "
                        f"{', '.join(thin)} — the screener stopped delivering them (a field "
                        f"NeoBDM dropped on purpose must be RETIRED in the lifecycle registry)")

    for f in breaks:
        entry = lc[f]
        notes.append(f"{nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED}: the {latest} capture ran under "
                     f"{contract_version}, which requested {f}; it came back "
                     f"{coverage.get(f, {}).get(latest, 0.0):.0%} populated — a source-contract break at "
                     f"capture. The lifecycle registry RETIRED {f} from the {entry['effective_capture_date']} "
                     f"capture ({entry.get('acknowledged_in_contract') or 'not yet acknowledged'}); that "
                     f"explains the break, it does not make this capture healthy. Signal(s) "
                     f"{', '.join(entry.get('signals') or []) or 'none'}: source unavailable at capture, "
                     f"RETIRED_SOURCE under the current lifecycle")
    for f in retired:
        entry = lc[f]
        if f in breaks:
            continue
        if coverage.get(f, {}).get(latest, 0.0) > 0:
            problems.append(f"{f} is RETIRED since the {entry['effective_capture_date']} capture but "
                            f"is populated again on {latest} — the source contract changed back; "
                            f"review the lifecycle registry before trusting either reading")
        else:
            notes.append(f"{f} RETIRED by NeoBDM since the {entry['effective_capture_date']} capture "
                         f"(lifecycle registry): no longer requested, no replacement approved; "
                         f"signal(s) built on it ({', '.join(entry.get('signals') or []) or 'none'}) "
                         f"are RETIRED_SOURCE, not zero-signal days")
    stats["retired_fields"] = retired
    stats["contract_breaks"] = breaks
    stats["capture_health"] = nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED if breaks else nsc.HEALTHY

    # Every other column, against its full history in healthy days.
    regressed = []
    for f, by_date in coverage.items():
        if f in active or f in retired:
            continue
        healthy = sum(1 for d in history if by_date.get(d, 0.0) >= MIN_BASELINE_COVERAGE)
        if healthy >= MIN_BASELINE_DAYS and by_date.get(latest, 0.0) < MIN_COVERAGE:
            regressed.append(f"{f} healthy on {healthy} earlier day(s)→{by_date.get(latest, 0.0):.0%}")
    stats["cols_swept"] = len(cols)
    if regressed:
        problems.append(
            f"{len(regressed)} column(s) populated on earlier captures are empty on {latest}: "
            f"{', '.join(regressed[:6])}" + (" …" if len(regressed) > 6 else "")
            + " — the screener API changed what it returns")

    # Storage-class drift on active fields (e.g. a numeric column turning into text).
    drift = []
    for f in judged:
        if f in missing:
            continue
        before, now = _storage_classes(conn, f, history), _storage_classes(conn, f, [latest])
        if before and now - before:
            drift.append(f"{f} {sorted(before)}→{sorted(now)}")
    if drift:
        problems.append(f"type change on {latest}: {', '.join(drift)} — the field's meaning or "
                        f"encoding changed")

    # Constant-field collapse: a cross-section that suddenly carries one value.
    if rows >= MIN_ROWS_FOR_COLLAPSE:
        collapsed = []
        recent = history[:CARDINALITY_BASELINE_DAYS]
        for f in judged:
            if f in missing or f == nsc.IDENTITY_KEY:
                continue
            now = conn.execute(f'SELECT COUNT(DISTINCT "{f}") FROM market_summary_daily '
                               f'WHERE date=? AND "{f}" IS NOT NULL', (latest,)).fetchone()[0]
            if now != 1 or not recent:
                continue
            past = [conn.execute(f'SELECT COUNT(DISTINCT "{f}") FROM market_summary_daily '
                                 f'WHERE date=? AND "{f}" IS NOT NULL', (d,)).fetchone()[0] for d in recent]
            if median(past) > 1:
                collapsed.append(f)
        if collapsed:
            problems.append(f"constant-field collapse on {latest}: {', '.join(collapsed)} carry a single "
                            f"value across {rows} names")

    # Response cardinality.
    recent = history[:CARDINALITY_BASELINE_DAYS]
    if len(recent) >= MIN_BASELINE_DAYS:
        base = median(conn.execute("SELECT COUNT(*) FROM market_summary_daily WHERE date=?",
                                   (d,)).fetchone()[0] for d in recent)
        ratio = rows / base if base else 0.0
        stats["cardinality"] = f"{rows} vs median {base:g}"
        if ratio < MIN_ROW_RATIO or ratio > MAX_ROW_RATIO:
            problems.append(f"panel size changed on {latest}: {rows} rows vs median {base:g} of the "
                            f"previous {len(recent)} captures — filters, universe or pagination moved")


def check_capture_contract(conn, problems, notes, stats):
    """requested vs catalog vs stored config vs returned keys, from the capture
    manifest the scraper writes for every capture (neobdm_source_contract)."""
    if not nsc.table_exists(conn, "ms_capture_manifest"):
        notes.append("no capture manifest yet — captures before the source-contract repair carry "
                     "no raw provenance")
        return
    latest = conn.execute("SELECT MAX(date) FROM market_summary_daily").fetchone()[0]
    manifests = conn.execute(
        "SELECT capture_id, contract_status, contract_issues, requested_columns, catalog_columns_sha256, "
        "catalog_column_count, capture_started_utc FROM ms_capture_manifest WHERE capture_date=? "
        "ORDER BY capture_started_utc DESC", (latest,)).fetchall()
    if not manifests:
        any_manifest = conn.execute("SELECT COUNT(*) FROM ms_capture_manifest").fetchone()[0]
        (problems if any_manifest else notes).append(
            f"no capture manifest for {latest} — the raw-provenance path did not run for that capture")
        return
    capture_id, status, issues_json, requested_json, catalog_sha, catalog_n, started = manifests[0]
    stats["contract"] = f"{status} ({capture_id})"
    issues = json.loads(issues_json)
    if json.loads(requested_json) != list(nsc.ACTIVE_REQUEST_COLUMNS):
        problems.append(f"capture {capture_id} requested columns that differ from the active contract "
                        f"— the deployed scraper and neobdm_source_contract disagree")

    def summary(severity):
        return ", ".join(f"{i['code']}{'(' + i['field'] + ')' if i['field'] else ''}"
                         for i in issues if i["severity"] == severity)

    if status == nsc.CONTRACT_FAILED:
        problems.append(f"source contract FAILED for capture {capture_id}: {summary(nsc.FAIL)}")
    for i in issues:
        if i["code"] == nsc.RETIRED_FIELD_REAPPEARED:
            problems.append(f"{nsc.RETIRED_FIELD_REAPPEARED}: {i['field']} — {i['detail']} (capture "
                            f"{capture_id}). The active contract is unaffected; do not re-request or "
                            f"bridge the field until a human has verified what the reappeared name means")
    if summary(nsc.WARN):
        notes.append(f"source contract warnings for capture {capture_id}: {summary(nsc.WARN)} "
                     f"(recorded; additive keys do not invalidate the capture)")

    previous = conn.execute(
        "SELECT catalog_columns_sha256, catalog_column_count FROM ms_capture_manifest "
        "WHERE capture_started_utc < ? AND catalog_columns_sha256 IS NOT NULL "
        "ORDER BY capture_started_utc DESC LIMIT 1", (started,)).fetchone()
    if previous and catalog_sha and previous[0] != catalog_sha:
        notes.append(f"NeoBDM column catalog changed ({previous[1]}→{catalog_n} fields) — raw catalog "
                     f"responses are kept for both captures")


def check_signal_sources(conn, problems, notes, stats, lifecycle=None):
    """Zero hits is only a signal outcome when the source was actually healthy."""
    lc = nsc.FIELD_LIFECYCLE if lifecycle is None else lifecycle
    if not nsc.table_exists(conn, "signal_source_status"):
        return
    latest = conn.execute("SELECT MAX(flag_date) FROM signal_source_status").fetchone()[0]
    if not latest:
        return
    retired_signals = {s for f, e in lc.items()
                       if nsc.lifecycle_state(f, latest, lc) == nsc.RETIRED for s in e.get("signals") or []}
    has_at_capture = "status_at_capture" in {
        r[1] for r in conn.execute("PRAGMA table_info(signal_source_status)")}
    for source, status, detail, at_capture in conn.execute(
            "SELECT source, status, detail, " + ("status_at_capture" if has_at_capture else "NULL")
            + " FROM signal_source_status WHERE flag_date=? ORDER BY source", (latest,)):
        if at_capture and at_capture != status:
            notes.append(f"signal source {source} on {latest}: {at_capture} at capture, {status} under the "
                         f"current lifecycle")
        if status == nsc.SOURCE_UNAVAILABLE:
            problems.append(f"signal source {source} UNAVAILABLE on {latest}: {detail}")
        elif status == nsc.RETIRED_SOURCE and source not in retired_signals:
            problems.append(f"signal source {source} marked RETIRED_SOURCE on {latest} without a "
                            f"retired field in the lifecycle registry")
        elif status == nsc.EMPTY_UNVERIFIED:
            notes.append(f"signal source {source} returned nothing on {latest} and cannot tell "
                         f"'no hits' from a failed scrape")


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

LABEL_COLUMN = re.compile(r"(fwd(?:_oo|_oc)?|lag|max|mdd|gap)_(\d+)")


def _sessions_after(day, count):
    """The next `count` verified sessions after `day`; None if the calendar cannot say."""
    from idx_calendar import is_idx_session, IdxCalendarUnavailable
    out, current = [], date.fromisoformat(day)
    try:
        while len(out) < count:
            current += timedelta(days=1)
            if is_idx_session(current):
                out.append(current.isoformat())
    except IdxCalendarUnavailable:
        return None
    return out


def _sessions_before(day, count):
    from idx_calendar import latest_idx_session_before, IdxCalendarUnavailable
    out, current = [], date.fromisoformat(day)
    try:
        for _ in range(count):
            current = latest_idx_session_before(current)
            out.append(current.isoformat())
    except IdxCalendarUnavailable:
        return None
    return out[::-1]


def _independent_label(kind, h, day, observed, span):
    """(reason, expected) for one published label, from stored observations only.

    Sessions come from the verified calendar, so a missing ticker row is never
    bridged. An empty reason means the label is eligible and `expected` is the
    value the stored observations imply. No producer column is consulted.
    """
    if kind == "lag":
        path = _sessions_before(day, h)
        if path is None:
            return "UNSUPPORTED_CALENDAR", None
        start, steps = path[0], path[1:] + [day]
        if any(observed.get(s) is None for s in [start] + steps):
            return "MISSING_TICKER_SESSION", None
        if not observed[start]["close_anchor_admissible"]:
            return "UNTRUSTED_ANCHOR", None
        if not all(observed[s]["price_step_admissible"] for s in steps):
            return "INADMISSIBLE_PATH", None
        return span(start, "CLOSE", day, "CLOSE"), observed[day]["close"] / observed[start]["close"] - 1
    after = _sessions_after(day, h + 1 if kind == "fwd_oo" else h)
    if after is None:
        return "UNSUPPORTED_CALENDAR", None
    if any(observed.get(s) is None for s in [day] + after):
        return "MISSING_TICKER_SESSION", None
    anchor, steps, first, last = observed[day], after[:h], observed[after[0]], observed[after[-1]]
    if kind != "fwd_oo" and not anchor["close_anchor_admissible"]:
        return "UNTRUSTED_ANCHOR", None
    if not all(observed[s]["price_step_admissible"] for s in steps):
        return "INADMISSIBLE_PATH", None
    if kind in {"fwd_oc", "fwd_oo", "gap"} and not first["entry_open_admissible"]:
        return "INADMISSIBLE_ENTRY_OPEN", None
    if kind == "fwd_oo" and not last["entry_open_admissible"]:
        return "INADMISSIBLE_EXIT_OPEN", None
    if kind in {"fwd", "max", "mdd"}:
        values = {"fwd": last["close"], "max": max(observed[s]["high"] for s in steps),
                  "mdd": min(observed[s]["low"] for s in steps)}
        return span(day, "CLOSE", after[-1], "CLOSE"), values[kind] / anchor["close"] - 1
    if kind == "fwd_oc":
        return span(after[0], "OPEN", after[-1], "CLOSE"), last["close"] / first["open"] - 1
    if kind == "fwd_oo":
        return span(after[0], "OPEN", after[-1], "OPEN"), last["open"] / first["open"] - 1
    return span(day, "CLOSE", after[0], "OPEN"), first["open"] / anchor["close"] - 1


def _same_value(left, right):
    if left is None or right is None or pd.isna(left) or pd.isna(right):
        return (left is None or pd.isna(left)) and (right is None or pd.isna(right))
    return float(left) == float(right)


def check_price_contract(conn, problems, notes, stats, *, registry=None, representation=None):
    """Re-adjudicate every published label from stored observations.

    The producer frame supplies only which labels were published and their
    values. Admission, anchors, paths, extrema, spans and the values themselves
    are recomputed from price_history and price_quarantine through the shared
    adjudication core, so forged or stale reasons, flags, certificates and
    identities cannot vouch for invalid underlying data.
    """
    from price_audit import clean_panel
    from price_contract_frame import default_registry, span_result, adjudicate_observations, SOURCE_COLUMNS
    px = clean_panel(conn, horizons=(1, 2), lags=(1, 2), extremes=True,
                     open_anchored=True, registry=registry, representation=representation)
    reasons = [c for c in px if c.endswith("_reason") and c.startswith(("fwd_", "lag_", "gap_"))]
    withheld = sum(int(px[c].ne("").sum()) for c in reasons)
    stats["withheld_price_labels"] = withheld
    stats["price_contract"] = px.attrs.get("price_contract")
    registry = registry or default_registry()
    representation = representation or "UNKNOWN"       # never the producer's own claim
    market = "REGULAR"
    raw = pd.read_sql("SELECT ticker, date, open, high, low, close, volume FROM price_history", conn)
    quarantine = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_quarantine'").fetchone():
        columns = {r[1] for r in conn.execute("PRAGMA table_info(price_quarantine)")}
        quarantine = {(d, t): r for d, t, r in conn.execute(
            "SELECT date, ticker, " + ("reasons" if "reasons" in columns else "NULL") + " FROM price_quarantine")}
    observed = adjudicate_observations(raw, registry=registry, representation=representation,
                                       market=market, quarantine=quarantine)
    axis = sorted(set(raw.date))
    differing = [f"{t} {d}" for t, d, *values in px[list(SOURCE_COLUMNS)].itertuples(index=False, name=None)
                 if (t, d) not in observed or not all(
                     _same_value(v, observed[(t, d)][f]) for f, v in zip(SOURCE_COLUMNS[2:], values))]
    if differing:
        problems.append(f"published observation differs from stored price_history for {len(differing)} "
                        f"row(s) (e.g. {', '.join(differing[:3])}); its labels are not evidence")
    by_ticker = {}
    for (ticker, day), record in observed.items():
        by_ticker.setdefault(ticker, {})[day] = record
    for column in px:
        match = LABEL_COLUMN.fullmatch(column)
        if not match:
            continue
        kind, h = match.group(1), int(match.group(2))
        bad = []
        for ticker, day, value in zip(px.ticker, px.date, px[column]):
            if pd.isna(value):
                continue

            def span(start, start_phase, end, end_phase, ticker=ticker):
                result = span_result(ticker, start, end, registry=registry, representation=representation,
                                     market=market, session_axis=axis, start_phase=start_phase,
                                     end_phase=end_phase)
                return "" if result.status == "COMPARABLE" else result.reason

            reason, expected = _independent_label(kind, h, day, by_ticker.get(ticker, {}), span)
            if not reason and not math.isclose(float(value), expected, rel_tol=1e-9, abs_tol=1e-12):
                reason = "VALUE_MISMATCH"
            if reason:
                bad.append(f"{ticker} {day} {reason}")
        if bad:
            problems.append(f"{column}: published economic value on an independently ineligible span "
                            f"({len(bad)}: {', '.join(bad[:3])})")
    notes.append(f"{withheld} price label(s) withheld under the pinned price/reference/return contract")


def check(conn):
    problems, notes, stats = [], [], {}
    check_freshness(conn, problems)
    check_schema_and_coverage(conn, problems, notes, stats)
    check_capture_contract(conn, problems, notes, stats)
    check_new_contamination(conn, problems, notes, stats)
    check_price_contract(conn, problems, notes, stats)
    check_cross_source(conn, problems, notes, stats)
    check_signals_measurable(conn, problems, notes, stats)
    check_signal_sources(conn, problems, notes, stats)
    check_value_sanity(conn, problems)
    return problems, notes, stats


def format_report(problems, notes, stats):
    if problems:
        head = "🔴 SIGNAL INTEGRITY FAILED"
    elif stats.get("capture_health") == nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED:
        head = (f"🟠 {nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED} — the latest capture broke its then-active "
                f"contract ({', '.join(stats.get('contract_breaks') or [])}); the retirement explains it, "
                f"it is not healthy")
    elif stats.get("retired_fields"):
        head = (f"🟢 signal integrity OK — active contract valid; RETIRED source field(s): "
                f"{', '.join(stats['retired_fields'])}")
    else:
        head = "🟢 signal integrity OK"
    lines = [head]
    if stats.get("latest_ms"):
        lines.append(f"{stats['latest_ms']}: {stats.get('ms_rows', 0)} panel rows | "
                     f"{stats.get('signals', 0)} signals | "
                     f"{stats.get('cross_source', 'cross-source n/a')} | "
                     f"contract {stats.get('contract', 'no manifest')} | "
                     f"health at capture {stats.get('capture_health', 'n/a')} under "
                     f"{stats.get('contract_at_capture', 'n/a')}")
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
    # Quiet when healthy: Telegram only hears about problems (exit 1 or 2).
    if "--telegram" in sys.argv and exit_code(problems, stats) != 0:
        send_telegram(report)
    sys.exit(exit_code(problems, stats))


def exit_code(problems, stats):
    """1 = failed; 2 = source-contract break acknowledged (explained, still not
    healthy, so the run stays red); 0 = healthy."""
    if problems:
        return 1
    if stats.get("capture_health") == nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED:
        return 2
    return 0


if __name__ == "__main__":
    main()
