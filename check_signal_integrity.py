"""
Read-only integrity gate for independent producers and their signal consumers.

Daily Scrape writes capture-dated screener/broker tables and signal status.
Price History Top-up writes session-dated inventory prices independently.
The screener's pre-open capture is compared with the previous IDX session,
including weekends and exchange holidays. Recent overlap is reported explicitly.

Heuristic price-limit/series findings remain unresolved until verified. They
cannot establish new ticker-selection damage. Cross-ticker OHLCV collisions
and structural impossibilities still fail; a prior price snapshot establishes
whether a collision is new, including historical-date rewrites.

Run: python check_signal_integrity.py
     python check_signal_integrity.py --as-of 2026-10-14 --producer-states states.json
     python check_signal_integrity.py --price-baseline prior.db
     python check_signal_integrity.py --telegram
Exit: 1 = hard failure; 2 = degraded/acknowledged, still unhealthy; 0 = healthy.
"""

import json
import math
import argparse
import os
import sqlite3
import sys
from pathlib import Path
from datetime import date, datetime, timedelta
from statistics import median

import pandas as pd
import requests

import neobdm_source_contract as nsc
from price_audit import detect, load, OHLCV
from idx_calendar import IdxCalendarUnavailable, latest_idx_session_before
from idx_price_rules_sig1 import assess_limit
from signal_integrity_health import (PRODUCERS, jakarta_today, open_readonly, table_freshness,
                                     inspect_price_rows, price_collisions, report_admission, AdmissionRefused)
from sig1_coverage_contract import EXPECTED_UNIVERSE, NUMERIC_SOURCE_FIELDS

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")

WINDOW_DAYS = 10          # recent capture comparison; unresolved prices remain visible
MAX_STALE_SESSIONS = 2    # IDX closures do not count as missed sessions

# Cross-source agreement. Both paths quote the same exchange, so a real match is
# exact; the tolerance only absorbs float round-tripping.
EXPECTED_DATE_OFFSET = 1  # candidate 1 means the previous IDX session
CLOSE_TOLERANCE = 0.001   # 0.1%
MIN_AGREEMENT = 0.85      # below this, one of the two scrape paths has drifted
MIN_PAIRS_TO_JUDGE = 15   # under this the sample is too thin to fail a build on

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


def _recent_dates(conn, table, n):
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT date FROM {table} ORDER BY date DESC LIMIT ?", (n,))]


def _shift(datestr, days):
    return (datetime.strptime(datestr, "%Y-%m-%d").date()
            - timedelta(days=days)).strftime("%Y-%m-%d")


def _source_date(capture_date, offset):
    # The previous closing session is not necessarily the previous calendar
    # day. Weekend/holiday captures must not manufacture a timing defect.
    if offset == EXPECTED_DATE_OFFSET:
        return latest_idx_session_before(date.fromisoformat(capture_date)).isoformat()
    return _shift(capture_date, offset)


# ── individual checks ─────────────────────────

def _finding(stats, category, detail, severity="degraded", **evidence):
    stats.setdefault("findings", []).append(dict(category=category, severity=severity,
                                                detail=detail, **evidence))
    if severity == "degraded":
        stats["degraded"] = True


def check_freshness(conn, problems, notes=None, stats=None, as_of=None, producer_states=None,
                    universe=EXPECTED_UNIVERSE, closed_session=None):
    notes = notes if notes is not None else []
    stats = stats if stats is not None else {}
    stats["freshness"] = {}
    for table in PRODUCERS:
        try:
            health = table_freshness(conn, table, as_of, producer_states, MAX_STALE_SESSIONS, universe,
                                     closed_session=closed_session)
        except IdxCalendarUnavailable as exc:
            detail = f"{table} freshness UNVERIFIED: {exc}"
            notes.append(detail)
            _finding(stats, "FRESHNESS_UNVERIFIED", detail, table=table)
            continue
        except (sqlite3.Error, ValueError, TypeError) as exc:
            detail = f"{table} STRUCTURAL_DEFECT: {exc}"
            problems.append(detail)
            _finding(stats, "STRUCTURAL_DEFECT", detail, "fail", table=table)
            continue
        stats["freshness"][table] = health
        lag_description = (f"{health['lag']} session(s) behind" if health['lag'] is not None else "session lag UNKNOWN/unavailable")
        detail = (f"{table} {health['status']}: writer {health['writer']}, workflow {health['workflow']} "
                  f"({health['producer_state']}); newest {health['latest']}, expected {health['expected']} "
                  f"by {health['date_basis']} date; source {health['freshness']} ({lag_description})")
        if health.get("structural_faults"):
            problems.extend(health["structural_faults"])
            for fault in health["structural_faults"]:
                _finding(stats, "STRUCTURAL_DEFECT", fault, "fail", table=table)
        if health.get("unsupported_dates") or health["freshness"] == "CALENDAR_UNSUPPORTED":
            detail += f"; historical calendar UNKNOWN/UNSUPPORTED: {health.get('unsupported_dates', [])[:5]}"
            notes.append(detail)
            _finding(stats, "CALENDAR_UNSUPPORTED", detail, table=table)
        if health.get("ahead_of_cutoff_dates"):
            ahead = f"{table} AHEAD_OF_CUTOFF: {health['ahead_of_cutoff_dates']}; required {health['expected']}"
            problems.append(ahead)
            _finding(stats, "AHEAD_OF_CUTOFF", ahead, "fail", table=table)
        if health.get("missing_coverage"):
            summary = {
                session: {"missing_count": len(missing), "sample": missing[:8],
                          "required": health["coverage"][session]["required"],
                          "present": health["coverage"][session]["present"]}
                for session, missing in health["missing_coverage"].items()
            }
            coverage_detail = (f"{table} INCOMPLETE_COVERAGE after quarantine under "
                               f"{health['universe_version']}: {summary}; "
                               "complete membership remains in JSON evidence")
            problems.append(coverage_detail)
            _finding(stats, "INCOMPLETE_COVERAGE", coverage_detail, "fail", table=table)
        if health["freshness"] in {"EMPTY", "INVALID_DATE", "UNAVAILABLE", "AHEAD_OF_CUTOFF"}:
            problems.append(detail)
            _finding(stats, health["freshness"], detail, "fail", table=table)
        if health["status"] == "PRODUCER_PAUSED":
            notes.append(detail + "; separate owner approval required for recovery")
            _finding(stats, "PRODUCER_PAUSED", detail, table=table)
        if health["freshness"] == "SOURCE_STALE":
            # The data remains unhealthy even when a paused producer explains it.
            problems.append(detail)
            _finding(stats, "SOURCE_STALE", detail, "fail", table=table)
        elif health["freshness"] == "LAGGING":
            notes.append(detail)
            _finding(stats, "SOURCE_LAGGING", detail, table=table)


def check_new_contamination(conn, problems, notes, stats, price_baseline=None):
    """Separate heuristic suspects from proven collisions and proven new writes.

    Neither an advancing nor a frozen MAX(date) establishes write activity:
    inventory top-up can rewrite old dates. No heuristic-only finding can
    establish a new ticker-selection defect.
    """
    window = set(_recent_dates(conn, "price_history", WINDOW_DAYS))
    if not window:
        return

    has_q = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='price_quarantine'"
    ).fetchone()[0]
    known = set()
    if has_q:
        # A quarantine acknowledges a particular bar, not every future rewrite
        # of its key. A changed quarantined bar must be examined again.
        qcols = {r[1] for r in conn.execute("PRAGMA table_info(price_quarantine)")}
        if set(OHLCV) <= qcols:
            known = set(map(tuple, conn.execute(
                "SELECT p.date, p.ticker FROM price_history p JOIN price_quarantine q "
                "USING(date,ticker) WHERE " + " AND ".join(f"p.{c} IS q.{c}" for c in OHLCV))))
        else:
            notes.append("price_quarantine lacks OHLCV evidence; key-only entries cannot acknowledge current bars")

    # Quarantined rows must not serve as the baseline the NEXT row's daily move
    # is measured against, or old damage keeps manufacturing fresh alarms: on
    # 2026-08-27 this reported a clean, unique MDIA row for 08-24 as the scraper
    # "writing bad rows again", purely because the close before it was KIOS's
    # price, flagged and quarantined two weeks earlier. See price_audit.detect.
    raw = load(conn)
    # Structural validation reports malformed values separately. Coercion only
    # in this diagnostic frame lets unaffected ticker collisions still run.
    raw[OHLCV] = raw[OHLCV].apply(pd.to_numeric, errors="coerce")
    trusted = [(d, t) not in known for d, t in zip(raw["date"], raw["ticker"])]
    px = detect(raw, trusted=trusted)
    px["cross_ticker_dup"] &= px.groupby(["date"] + OHLCV, dropna=False)["ticker"].transform("nunique").gt(1)
    px["suspect"] = px[["limit_violation", "cross_ticker_dup", "series_break"]].any(axis=1)
    recent = px[px["date"].isin(window)]
    # Neither a rolling window nor producer recovery resolves an old suspect.
    # Keep backlog visible; old-date rewrites can introduce new collisions too.
    examined = px[px["suspect"]]

    flagged = examined[examined["cross_ticker_dup"] | (examined["suspect"] & ~pd.Series(
        [(d, t) in known for d, t in zip(examined["date"], examined["ticker"])],
        index=examined.index, dtype=bool))]

    stats["window_rows"] = len(recent)
    stats["unresolved_suspects"] = len(flagged)
    stats["recent_unresolved_suspects"] = int(flagged["date"].isin(window).sum())
    stats["new_contamination"] = 0
    if not len(flagged):
        return

    by_reason = {r: int(flagged[r].sum()) for r in
                 ("limit_violation", "cross_ticker_dup", "series_break")
                 if flagged[r].any()}
    # Event dates and table MAX(date) cannot establish insert/update time. A
    # supplied prior snapshot can prove a new collision, including an old-date
    # rewrite. Without it, a proven defect still fails but its newness is unknown.
    baseline_rows = None
    baseline_dups = set()
    if price_baseline is not None:
        baseline_rows = {(r.date, r.ticker): tuple(getattr(r, c) for c in OHLCV)
                         for r in price_baseline.itertuples()}
        before = detect(price_baseline)
        baseline_dups = set(zip(before.loc[before.cross_ticker_dup, "date"],
                                before.loc[before.cross_ticker_dup, "ticker"]))
    collision_evidence = {(c["date"], t): c for c in price_collisions(conn) for t in c["tickers"]}
    for row in flagged.itertuples():
        key = (row.date, row.ticker)
        fingerprint = tuple(getattr(row, c) for c in OHLCV)
        if row.cross_ticker_dup:
            evidence = collision_evidence[key]
            if evidence["classification"] == "REVIEW_REQUIRED":
                detail = f"REVIEW_REQUIRED: {row.date} {row.ticker} identical OHLCV; independent clone evidence unavailable"
                notes.append(detail)
                _finding(stats, "REVIEW_REQUIRED", detail, table="price_history", evidence=evidence)
                continue
            changed = baseline_rows is not None and (
                baseline_rows.get(key) != fingerprint or key not in baseline_dups)
            category = "NEW_CONTAMINATION" if changed else "CORRUPTION_PRESENT"
            detail = (f"{category}: {row.date} {row.ticker} identical OHLCV across tickers; "
                      + ("new/changed collision versus supplied price snapshot" if changed else
                         "insert/update time unverified; no claim of a new ticker-selection defect"))
            problems.append(detail)
            _finding(stats, category, detail, "fail", table="price_history", date=row.date, ticker=row.ticker, evidence=evidence)
            stats["new_contamination"] += int(changed)
        else:
            evidence = assess_limit(row) if row.limit_violation else {"classification": "UNKNOWN"}
            detail = (f"BACKLOG_SUSPECT: {row.date} {row.ticker}, "
                      f"limit_violation={row.limit_violation}, series_break={row.series_break}; "
                      f"classification {evidence['classification']}; "
                      f"{evidence.get('detail', 'corporate action, rule regime and source bar require verification')}. "
                      "Unresolved price finding; row date and table freshness do not prove a new write.")
            notes.append(detail)
            _finding(stats, "BACKLOG_SUSPECT", detail, table="price_history", date=row.date,
                     ticker=row.ticker, evidence=evidence)
    stats["suspect_reasons"] = by_reason


def check_cross_source(conn, problems, notes, stats):
    """market_summary_daily (screener API) vs price_history (inventory chart).

    Two independent scrape paths for the same numbers. Also re-derives the date
    offset rather than trusting it, so a change in publish timing surfaces as
    its own message instead of masquerading as mass contamination.
    """
    ms = pd.read_sql(
        "SELECT date, ticker, close FROM market_summary_daily WHERE close IS NOT NULL", conn)
    # Compare usable rows, the same quarantine-filtered base as report readers.
    from price_audit import load_clean
    ph = load_clean(conn)[["date", "ticker", "close"]].copy()
    for panel in (ms, ph):
        panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
        panel.drop(panel.index[~panel.close.abs().lt(float("inf")) | panel.close.le(0)], inplace=True)
    if ms.empty or ph.empty:
        detail = "cross-source UNKNOWN: no usable comparison evidence"
        notes.append(detail)
        _finding(stats, "CROSS_SOURCE_UNVERIFIED", detail)
        return
    # Full-history agreement diluted recent mismatches and looked current
    # even after the independent price producer stopped. Judge recent captures
    # and expose the actual overlap endpoint alongside the agreement score.
    dates = _recent_dates(conn, "market_summary_daily", WINDOW_DAYS)
    ms = ms[ms["date"].isin(dates)]
    stats["cross_source_price_latest"] = ph["date"].max()
    stats["cross_source_capture_latest"] = ms["date"].max()
    latest_capture = ms[ms.date == ms.date.max()].copy()
    latest_capture["date"] = latest_capture.date.map(lambda d: _source_date(d, EXPECTED_DATE_OFFSET))
    current_overlap = latest_capture.merge(ph, on=["date", "ticker"])
    if len(current_overlap) < MIN_PAIRS_TO_JUDGE:
        detail = (f"cross-source UNKNOWN: latest capture has {len(current_overlap)} usable pairs, "
                  f"requires {MIN_PAIRS_TO_JUDGE}; historical overlap cannot certify the latest capture")
        notes.append(detail)
        _finding(stats, "CROSS_SOURCE_UNVERIFIED", detail)

    best, scores = None, {}
    # Prefer the expected alignment on equal scores. Constant closes cannot
    # establish that publish timing changed.
    for offset in (EXPECTED_DATE_OFFSET, 0, 2):
        m = ms.copy()
        m["date"] = m["date"].map(lambda d: _source_date(d, offset))
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
        detail = (f"cross-source UNKNOWN: fewer than {MIN_PAIRS_TO_JUDGE} "
                  "overlapping (date,ticker) pairs at any offset")
        notes.append(detail)
        _finding(stats, "CROSS_SOURCE_UNVERIFIED", detail)
        return

    n_best, agree_best = scores[best]
    alignment = "previous IDX session" if best == EXPECTED_DATE_OFFSET else f"offset={best}d"
    stats["cross_source"] = f"{alignment} n={n_best} agree={agree_best:.0%}"
    aligned = ms.copy()
    aligned["date"] = aligned["date"].map(lambda d: _source_date(d, best))
    overlap = aligned.merge(ph, on=["date", "ticker"], suffixes=("_ms", "_ph"))
    stats["cross_source_overlap_latest"] = overlap["date"].max()
    stats["cross_source"] += f" through {stats['cross_source_overlap_latest']} (recent captures only)"

    if best != EXPECTED_DATE_OFFSET and agree_best >= MIN_AGREEMENT:
        problems.append(
            f"date alignment CHANGED — market_summary_daily now matches "
            f"price_history at offset {best}d, not the expected "
            f"previous IDX session (agreement {agree_best:.0%} on {n_best} pairs). "
            f"Verify capture timing and the source session before changing any date-joined labels.")
        return

    if agree_best < MIN_AGREEMENT:
        m = ms.copy()
        m["date"] = m["date"].map(lambda d: _source_date(d, best))
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

    check_numeric_source_contract(conn, judged, cols, latest, problems)

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
        elif status == nsc.RETIRED_SOURCE:
            notes.append(f"signal source {source} RETIRED_SOURCE on {latest}: unavailable by lifecycle, "
                         "excluded from zero-hit interpretation")
            _finding(stats, "RETIRED_SOURCE", f"{source} on {latest}", "info", source=source)
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
    stats["unmeasurable_signals"] = gap
    detail = (f"{len(gap)} of {len(sig)} signalled ticker(s) on {latest} have no "
              f"captured close ({', '.join(gap[:4])}) — they can never be scored")
    if share > MAX_UNMEASURABLE_SIGNALS:
        problems.append(
            f"{detail}. That is {share:.0%}, past the {MAX_UNMEASURABLE_SIGNALS:.0%} "
            f"limit — the panel has shrunk or the signal source drifted off-universe.")
    else:
        notes.append(f"{detail} ({share:.0%}, normal panel churn)")
    _finding(stats, "UNMEASURABLE_SIGNALS", detail,
             "fail" if share > MAX_UNMEASURABLE_SIGNALS else "info", tickers=gap)


def check_numeric_source_contract(conn, judged, cols, latest, problems):
    """Numeric field meanings apply even to the first observed capture."""
    for field in sorted(NUMERIC_SOURCE_FIELDS & set(judged) & set(cols)):
        values = conn.execute(f'SELECT "{field}", typeof("{field}") FROM market_summary_daily '
                              f'WHERE date=? AND "{field}" IS NOT NULL', (latest,)).fetchall()
        bad = sum(kind not in {"integer", "real"} or not math.isfinite(value)
                  for value, kind in values)
        if bad:
            problems.append(f"numeric source contract: {field} has {bad} nonnumeric/nonfinite value(s) on {latest}")


def check_value_sanity(conn, problems, notes=None, stats=None):
    """Structural impossibilities fail independently of age, pause or quarantine."""
    faults, unsupported = inspect_price_rows(conn)
    problems.extend(f for f in faults if f not in problems)
    if unsupported and notes is not None and stats is not None:
        detail = f"historical price calendar UNKNOWN/UNSUPPORTED: {unsupported[:5]}"
        notes.append(detail)
        _finding(stats, "CALENDAR_UNSUPPORTED", detail)


# ── reporting ─────────────────────────────────

def check_report_inputs(conn, problems, notes, stats, as_of, universe, closed_session=None):
    try:
        stats["report_admission"] = report_admission(conn, as_of, universe=universe,
                                                     closed_session=closed_session)
    except AdmissionRefused as exc:
        stats["report_admission"] = exc.evidence
        for reason in exc.evidence["rejection_reasons"]:
            detail = "ML required input refused: " + reason
            problems.append(detail)
            _finding(stats, "REPORT_INPUT_REFUSED", detail, "fail")


def check(conn, as_of=None, producer_states=None, price_baseline=None, universe=EXPECTED_UNIVERSE,
          closed_session=None):
    # closed_session: see idx_calendar.expected_closed_session. The CLI never
    # passes it; only a caller that owns an after-close price producer may.
    problems, notes, stats = [], [], {}
    stats["as_of"] = (as_of or jakarta_today()).isoformat()
    check_freshness(conn, problems, notes, stats, as_of, producer_states, universe, closed_session)
    checks = [
        (check_report_inputs, "REPORT_INPUT_REFUSED",
         (conn, problems, notes, stats, as_of, universe, closed_session)),
        (check_schema_and_coverage, "SOURCE_CONTRACT_FAILURE", (conn, problems, notes, stats)),
        (check_capture_contract, "SOURCE_CONTRACT_FAILURE", (conn, problems, notes, stats)),
        (check_value_sanity, "STRUCTURAL_DEFECT", (conn, problems, notes, stats)),
        (check_new_contamination, "PRICE_CHECK_FAILURE", (conn, problems, notes, stats, price_baseline)),
        (check_cross_source, "CROSS_SOURCE_DISAGREEMENT", (conn, problems, notes, stats)),
        (check_signals_measurable, "UNMEASURABLE_SIGNALS", (conn, problems, notes, stats)),
        (check_signal_sources, "SOURCE_UNAVAILABLE", (conn, problems, notes, stats)),
    ]
    for fn, category, args in checks:
        before = len(problems)
        try:
            fn(*args)
        except IdxCalendarUnavailable as exc:
            detail = f"{fn.__name__} UNVERIFIED: {exc}"
            notes.append(detail)
            _finding(stats, "FRESHNESS_UNVERIFIED", detail)
        except (sqlite3.Error, pd.errors.DatabaseError, pd.errors.DataError, ValueError, TypeError, KeyError) as exc:
            problems.append(f"STRUCTURAL_DEFECT: {fn.__name__} cannot inspect the data: {exc}")
            category = "STRUCTURAL_DEFECT"
        for detail in problems[before:]:
            if not any(f["detail"] == detail for f in stats.get("findings", [])):
                _finding(stats, category, detail, "fail")
    return problems, notes, stats


def format_report(problems, notes, stats):
    if problems:
        head = "🔴 SIGNAL INTEGRITY FAILED"
    elif stats.get("degraded"):
        head = "🟠 SIGNAL INTEGRITY DEGRADED — unresolved quality or producer availability; not healthy"
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
    if "unresolved_suspects" in stats:
        lines.append(f"unresolved suspects across history: {stats['unresolved_suspects']} "
                     f"({stats.get('recent_unresolved_suspects', 0)} recent) | "
                     f"proven new contamination: {stats.get('new_contamination', 0)} | "
                     f"recent price rows: {stats.get('window_rows', 0)}")
    for p in problems:
        lines.append(f"❌ {p}")
    for n in notes:
        lines.append(f"⚠️ {n}")
    if problems or stats.get("degraded"):
        lines += ["", "Recover the named producer or verify the named price/source defect. "
                      "A suspect price-limit move alone does not prove a ticker-selection defect."]
    return "\n".join(lines)


def notification_text(message):
    """Keep Telegram within its UTF-16 limit; retain full JSON/Actions evidence."""
    if len(message.encode("utf-16-le")) <= 7800:
        return message
    prefix = message.encode("utf-16-le")[:6800].decode("utf-16-le", errors="ignore")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    link = (f"https://github.com/{repository}/actions/runs/{run_id}"
            if repository and run_id else "Actions logs and --json-out evidence")
    return prefix + "\n... notification shortened; full evidence: " + link[:250]


def send_telegram(message):
    message = notification_text(message)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — not sending")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": message,
                            "disable_web_page_preview": True}, timeout=15)
    print("sent to Telegram" if r.ok else f"telegram error {r.status_code}: {r.text}")


def github_producer_states(repository, token=None):
    """Read workflow availability; no dispatch, enable, disable or reconfigure calls."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(f"https://api.github.com/repos/{repository}/actions/workflows",
                            headers=headers, params={"per_page": 100}, timeout=15)
    response.raise_for_status()
    return {w["path"].rsplit("/", 1)[-1]: w["state"] for w in response.json()["workflows"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    ap.add_argument("--price-baseline", help="prior price snapshot database, opened read-only")
    ap.add_argument("--producer-states", help="JSON mapping workflow filenames to observed GitHub states")
    ap.add_argument("--json-out", help="write complete machine-readable integrity evidence")
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args(argv)
    _load_dotenv()
    states, availability_note = {}, None
    if args.producer_states:
        with open(args.producer_states, encoding="utf-8") as fh:
            states = json.load(fh)
    elif os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY"):
        try:
            states = github_producer_states(os.environ["GITHUB_REPOSITORY"],
                                            os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            availability_note = "Producer state UNVERIFIED: read-only GitHub workflow lookup failed; freshness still enforced"
    baseline = None
    if args.price_baseline:
        prior = open_readonly(args.price_baseline)
        try:
            baseline = load(prior)
        finally:
            prior.close()
    conn = open_readonly(args.db)
    try:
        problems, notes, stats = check(conn, args.as_of, states, baseline)
    finally:
        conn.close()
    if availability_note:
        notes.append(availability_note)
        _finding(stats, "PRODUCER_STATE_UNVERIFIED", availability_note)
    if args.json_out:
        destination = Path(args.json_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps({
            "schema": "sig1-integrity-evidence.v1",
            "exit_code": exit_code(problems, stats),
            "problems": problems, "notes": notes, "stats": stats,
        }, indent=2, default=str), encoding="utf-8")
        temporary.replace(destination)
    report = format_report(problems, notes, stats)
    print(report)
    # Quiet when healthy: Telegram only hears about problems (exit 1 or 2).
    if args.telegram and exit_code(problems, stats) != 0:
        send_telegram(report)
    sys.exit(exit_code(problems, stats))


def exit_code(problems, stats):
    """1 = hard failure; 2 = degraded/acknowledged, still unhealthy; 0 = healthy."""
    if problems:
        return 1
    if stats.get("degraded") or stats.get("capture_health") == nsc.SOURCE_CONTRACT_BREAK_ACKNOWLEDGED:
        return 2
    return 0


if __name__ == "__main__":
    main()
