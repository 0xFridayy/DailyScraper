"""Producer ownership and session freshness. No database or workflow writes."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import hashlib
import json
import math

import pandas as pd

from idx_calendar import (CALENDAR_VERSION, IdxCalendarUnavailable, is_idx_session, latest_idx_session_before,
                          expected_closed_session)
from price_audit import load, load_clean, OHLCV
from sig1_coverage_contract import EXPECTED_UNIVERSE, REQUIRED_SESSION_COUNT


# Live tables use capture dates; inventory prices use exchange session dates.
PRODUCERS = {
    "market_summary_daily": {
        "workflow": "daily-scrape.yml", "writer": "neobdm_scraper.save_market_summary_daily",
        "date_basis": "capture", "downstream": "daily_picks, evaluate_signals",
    },
    "broker_flow": {
        "workflow": "daily-scrape.yml", "writer": "neobdm_scraper.persist_broker_flow_capture",
        "date_basis": "capture", "downstream": "canonical broker panels and ML",
        "historical_writer": "backfill_inventory.insert_inventory (through BACKFILL_END only)",
    },
    "price_history": {
        "workflow": "price-history-topup.yml", "writer": "backfill_inventory.insert_inventory",
        "date_basis": "session", "downstream": "clean_panel, ML targets, konglo watch",
        "manual_writers": ["price_audit.cmd_repair", "price_audit.cmd_reconcile_cross_dups (--apply)"],
    },
}


def jakarta_today():
    return datetime.now(timezone(timedelta(hours=7))).date()


def open_readonly(path):
    path = Path(path).absolute()
    if not path.is_file():
        raise FileNotFoundError(path)
    # immutable avoids creating WAL/SHM sidecars on a read-only source. A
    # nonempty WAL must be checkpointed by its owner or copied by a diagnostic.
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("Current ML report refused: frozen database is not the source SQLite snapshot: nonempty WAL")
    # Rollback-journal databases retain SQLite's read locks. WAL-mode main
    # files without pending WAL pages can be inspected without making sidecars.
    with path.open("rb") as source:
        wal_mode = source.read(20)[18:20] == bytes((2, 2))
    suffix = "?mode=ro&immutable=1" if wal_mode else "?mode=ro"
    connection = sqlite3.connect(path.as_uri() + suffix, uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def session_lag(latest, expected):
    return sum(is_idx_session(latest + timedelta(days=i + 1))
               for i in range(max(0, (expected - latest).days)))


def inspect_price_rows(conn, expected=None):
    """Validate storage classes, values and evidenced historical sessions.

    A numeric-looking TEXT/BLOB is not a numeric observation. Unsupported
    calendar periods stay UNKNOWN rather than being classified as weekdays.
    This reads raw rows too: quarantine cannot waive a structural impossibility.
    """
    columns = {r[1] for r in conn.execute("PRAGMA table_info(price_history)")}
    missing = {"date", "ticker", *OHLCV} - columns
    if missing:
        return [f"STRUCTURAL_DEFECT: price_history missing required columns {sorted(missing)}"], []
    px = load(conn)
    faults, unsupported = [], []
    bad_storage = conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE " + " OR ".join(
            f"typeof({c}) NOT IN ('integer','real')" for c in OHLCV)).fetchone()[0]
    numeric = px[OHLCV].apply(pd.to_numeric, errors="coerce")
    finite = numeric.notna() & numeric.abs().lt(float("inf"))
    bad_values = (~finite.all(axis=1)
                  | (numeric[["open", "high", "low", "close"]] <= 0).any(axis=1)
                  | (numeric.volume < 0) | (numeric.high < numeric.low)
                  | (numeric.open > numeric.high) | (numeric.open < numeric.low)
                  | (numeric.close > numeric.high) | (numeric.close < numeric.low))
    if bad_storage or bad_values.any():
        faults.append(f"STRUCTURAL_DEFECT: {bad_storage} nonnumeric SQLite OHLCV storage row(s), "
                      f"{int(bad_values.sum())} nonfinite/missing OHLCV, nonpositive prices, "
                      "negative volume or inconsistent OHLC ranges")
    if px.duplicated(["date", "ticker"]).any():
        faults.append("STRUCTURAL_DEFECT: duplicate price_history (date,ticker) keys")
    if px["ticker"].map(lambda t: not isinstance(t, str) or not t.strip()).any():
        faults.append("STRUCTURAL_DEFECT: missing ticker identities")
    for value in px["date"].drop_duplicates():
        try:
            session = date.fromisoformat(value)
            if session.isoformat() != value:
                raise ValueError("noncanonical date")
        except (TypeError, ValueError):
            faults.append(f"STRUCTURAL_DEFECT: invalid price date {value!r}")
            continue
        try:
            if not is_idx_session(session):
                faults.append(f"STRUCTURAL_DEFECT: non-session price date {value} per {CALENDAR_VERSION}")
        except IdxCalendarUnavailable:
            unsupported.append(value)
    return faults, unsupported


def price_collisions(conn, sessions=None):
    """Full-bar equality is a candidate, independent source conflict is proof.

    Screener captures refer to the preceding exchange session. A disagreement
    establishes corruption in the collision group, never which member to repair.
    No source evidence means REVIEW_REQUIRED, including zero-volume bars.
    Quarantine acknowledgements do not waive this test.
    """
    px = load(conn)
    if sessions is not None:
        px = px.loc[px["date"].isin({str(d) for d in sessions})]
    px = px.copy()
    px[OHLCV] = px[OHLCV].apply(pd.to_numeric, errors="coerce")
    candidates = px.loc[px.duplicated(["date"] + OHLCV, keep=False)
                        & px[OHLCV].notna().all(axis=1)]
    if candidates.empty:
        return []
    evidence = {}
    columns = {r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")}
    if {"date", "ticker", "close"} <= columns:
        for capture, ticker, close in conn.execute("SELECT date,ticker,close FROM market_summary_daily"):
            if not isinstance(close, (int, float)) or not math.isfinite(close) or close <= 0:
                continue
            try:
                session = latest_idx_session_before(date.fromisoformat(capture)).isoformat()
            except (IdxCalendarUnavailable, ValueError, TypeError):
                continue
            evidence.setdefault((session, ticker), set()).add(float(close))
    findings = []
    for key, group in candidates.groupby(["date"] + OHLCV):
        tickers = sorted(set(group["ticker"]))
        if len(tickers) < 2:
            continue
        conflicts = {t: sorted(evidence[(key[0], t)]) for t in tickers
                     if (key[0], t) in evidence and len(evidence[(key[0], t)]) == 1
                     and not math.isclose(next(iter(evidence[(key[0], t)])), key[4], rel_tol=0.001)}
        findings.append(dict(date=key[0], tickers=tickers,
                             classification="CORRUPTION_PRESENT" if conflicts else "REVIEW_REQUIRED",
                             evidence_source="market_summary_daily:previous_IDX_session",
                             conflicting_closes=conflicts, ohlcv=list(key[1:])))
    return findings


def price_snapshot_health(conn, expected, universe=EXPECTED_UNIVERSE, max_lag=2):
    """Current admission and retrospective calendar certification are separate."""
    faults, unsupported = inspect_price_rows(conn, expected)
    result = dict(universe_version=universe.version, universe_source=universe.source,
                  universe_sha256=hashlib.sha256(json.dumps(sorted(universe.tickers), separators=(",", ":")).encode()).hexdigest(),
                  calendar_version=CALENDAR_VERSION, structural_faults=faults,
                  historical_calendar_status="UNKNOWN/UNSUPPORTED" if unsupported else "SUPPORTED",
                  source_certification="UNVERIFIED", spectra_ingestion="BLOCKED",
                  unsupported_dates=unsupported, coverage={}, missing_coverage={},
                  usable_latest=None, lag=None, session_lag_by_ticker={}, collisions=[])
    if faults:
        return dict(result, freshness="INVALID_DATA")
    required_sessions = [expected]
    for _ in range(REQUIRED_SESSION_COUNT - 1):
        required_sessions.append(latest_idx_session_before(required_sessions[-1]))
    accepted = load_clean(conn)
    result["ahead_of_cutoff_dates"] = sorted({d for d in load(conn)["date"] if d > str(expected)})
    accepted = accepted.loc[accepted["date"].le(str(expected))]
    required_members = universe.required(expected)
    scoped = accepted.loc[accepted["ticker"].isin(required_members)]
    result["usable_latest"] = None if scoped.empty else scoped["date"].max()
    missing = {}
    for session in required_sessions:
        required = universe.required(session)
        present = set(accepted.loc[accepted.date == session.isoformat(), "ticker"])
        absent = sorted(required - present)
        result["coverage"][session.isoformat()] = dict(required=len(required),
                                                      present=len(required & present), missing=absent)
        if absent:
            missing[session.isoformat()] = absent
    latest_by_ticker = scoped.groupby("ticker")["date"].max().to_dict()
    for ticker in sorted(required_members):
        latest = latest_by_ticker.get(ticker)
        try:
            lag = session_lag(date.fromisoformat(latest), expected) if latest else None
        except IdxCalendarUnavailable:
            lag = None
        result["session_lag_by_ticker"][ticker] = lag
    result["lag"] = max((v for v in result["session_lag_by_ticker"].values() if v is not None), default=None)
    result["missing_coverage"] = missing
    result["collisions"] = price_collisions(conn, required_sessions)
    if missing:
        freshness = ("SOURCE_STALE" if result["lag"] is not None
                     and result["lag"] >= max(1, max_lag) else "INCOMPLETE_COVERAGE")
        if accepted.empty:
            freshness = "EMPTY" if not len(load(conn)) else "INCOMPLETE_COVERAGE"
        return dict(result, freshness=freshness)
    if result["collisions"]:
        status = "INVALID_DATA" if any(c["classification"] == "CORRUPTION_PRESENT" for c in result["collisions"]) else "REVIEW_REQUIRED"
        return dict(result, freshness=status)
    if result["ahead_of_cutoff_dates"]:
        return dict(result, freshness="AHEAD_OF_CUTOFF")
    return dict(result, freshness="CURRENT")


def table_freshness(conn, table, as_of=None, producer_states=None, max_lag=2,
                    universe=EXPECTED_UNIVERSE, closed_session=None):
    """A workflow's state explains availability; it never proves source freshness.

    closed_session applies only to session-dated tables; see
    idx_calendar.expected_closed_session. Capture-dated tables ignore it."""
    as_of = as_of or jakarta_today()
    producer = PRODUCERS[table]
    state = (producer_states or {}).get(producer["workflow"], "unknown")
    expected = (expected_closed_session(as_of, closed_session) if producer["date_basis"] == "session"
                else as_of)
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    latest = conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()[0] if "date" in columns else None
    result = dict(producer, table=table, producer_state=state, latest=latest, expected=str(expected))
    if table == "price_history":
        result.update(price_snapshot_health(conn, expected, universe, max_lag))
        return dict(result, status="PRODUCER_PAUSED" if state.startswith("disabled") else result["freshness"])
    required_columns = ({"date", "ticker", "broker_code", "netval", "bval", "sval", "bavg", "savg"}
                        if table == "broker_flow" else {"date", "ticker", "close", "high", "low"})
    if not required_columns <= columns:
        result["missing_columns"] = sorted(required_columns - columns)
        return dict(result, freshness="UNAVAILABLE", lag=None, status="UNAVAILABLE")
    if latest is None:
        return dict(result, freshness="EMPTY", lag=None, status="EMPTY")
    try:
        observed = date.fromisoformat(latest)
        if observed.isoformat() != latest:
            raise ValueError("noncanonical date")
    except (ValueError, TypeError):
        return dict(result, freshness="INVALID_DATE", lag=None, status="INVALID_DATE")
    if observed > expected:
        return dict(result, freshness="AHEAD_OF_CUTOFF", lag=None, status="AHEAD_OF_CUTOFF")
    try:
        lag = session_lag(observed, expected)
    except IdxCalendarUnavailable:
        return dict(result, freshness="CALENDAR_UNSUPPORTED", lag=None, status="CALENDAR_UNSUPPORTED")
    freshness = "SOURCE_STALE" if lag and lag >= max_lag else "LAGGING" if lag else "CURRENT"
    # Capture dates must be exact even on weekends; zero session lag is not freshness.
    if observed < expected and not lag:
        freshness = "SOURCE_STALE" if max_lag == 0 else "LAGGING"
    status = "PRODUCER_PAUSED" if state.startswith("disabled") else freshness
    return dict(result, freshness=freshness, lag=lag, status=status)


class AdmissionRefused(RuntimeError):
    """Stable JSON-compatible evidence attached to a refusal."""

    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def require_current_prices(conn, as_of=None, universe=EXPECTED_UNIVERSE, closed_session=None):
    """Admit only complete, numeric, valid, quarantine-filtered current prices."""
    try:
        health = table_freshness(conn, "price_history", as_of=as_of, max_lag=0, universe=universe,
                                 closed_session=closed_session)
    except (IdxCalendarUnavailable, sqlite3.Error, ValueError, TypeError) as exc:
        raise RuntimeError(f"Current ML report refused: admission evidence unavailable: {exc}") from exc
    if health["freshness"] != "CURRENT":
        description = "incomplete price_history" if health.get("missing_coverage") else f"price_history {health['freshness']}"
        raise AdmissionRefused(f"Current ML report refused: {description}; raw newest {health['latest']}, "
                           f"usable newest {health['usable_latest']}, required session {health['expected']} "
                           f"({health['freshness']}); missing usable closes/coverage "
                           f"{health.get('missing_coverage', {})}; structural faults "
                           f"{health['structural_faults']}; calendar UNKNOWN/UNSUPPORTED "
                           f"{health['unsupported_dates'][:5]}; collisions {health['collisions']}; ahead of cutoff {health.get('ahead_of_cutoff_dates', [])}.", health)
    return health


# Each enabled component declares its inputs. No optional dataset is consumed.
REPORT_COMPONENTS = {
    "xgboost": {"required": ("price_history", "broker_flow"), "optional": ()},
    "strategy_variants": {"required": ("price_history", "broker_flow"), "optional": ()},
    "ddqn": {"required": ("price_history", "broker_flow"), "optional": ()},
    "konglo": {"required": ("price_history", "market_summary_daily", "konglo_signal_watch", "signal_source_status"), "optional": ()},
}
KONGLO_SOURCES = ("top_akum_bandar", "dashboard_Bandarmologi", "dashboard_Foreign", "dashboard_NonRetail", "broker_stalker")


def report_admission(conn, as_of=None, components=None, universe=EXPECTED_UNIVERSE, closed_session=None):
    """Admit all enabled components against this same frozen connection."""
    import neobdm_source_contract as nsc
    as_of = as_of or jakarta_today()
    selected = tuple(REPORT_COMPONENTS) if components is None else tuple(components)
    datasets = sorted({d for c in selected for d in REPORT_COMPONENTS[c]["required"]})
    record = dict(schema="sig1-report-admission.v1", as_of=str(as_of),
                  snapshot_sha256=hashlib.sha256(conn.serialize()).hexdigest(),
                  components={c: REPORT_COMPONENTS[c] for c in selected}, datasets={},
                  optional_exclusions=[], admitted=False, rejection_reasons=[])
    for table in datasets:
        if table in PRODUCERS:
            try:
                item = table_freshness(conn, table, as_of, max_lag=0, universe=universe,
                                       closed_session=closed_session)
            except (IdxCalendarUnavailable, sqlite3.Error, ValueError, TypeError) as exc:
                item = dict(table=table, freshness="UNAVAILABLE", latest=None, expected=str(as_of), detail=str(exc), **PRODUCERS[table])
        else:
            item = dict(table=table, writer="neobdm_scraper.record_konglo_signals / neobdm_source_contract.record_signal_source_status",
                        expected=str(as_of), latest=None, freshness="UNAVAILABLE")
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                required_cols = {"flag_date", "ticker", "sources"} if table == "konglo_signal_watch" else {"flag_date", "source", "status"}
                if not required_cols <= cols:
                    raise ValueError(f"missing required columns: {sorted(required_cols - cols)}")
                item["latest"] = conn.execute(f"SELECT MAX(flag_date) FROM {table}").fetchone()[0]
                if table == "signal_source_status":
                    states = dict(conn.execute("SELECT source,status FROM signal_source_status WHERE flag_date=?", (str(as_of),)))
                    retired = {s for field, e in nsc.FIELD_LIFECYCLE.items() if nsc.lifecycle_state(field, str(as_of)) == nsc.RETIRED for s in e.get("signals") or []}
                    bad = {source: states.get(source, "UNAVAILABLE") for source in KONGLO_SOURCES
                           if states.get(source) not in {nsc.HITS, nsc.NO_HITS}
                           and not (states.get(source) == nsc.RETIRED_SOURCE and source in retired)}
                    if item["latest"] != str(as_of):
                        bad["capture_date"] = item["latest"]
                    item["source_states"] = states
                    item["freshness"] = "SOURCE_UNAVAILABLE" if bad else "CURRENT"
                    item["unavailable_sources"] = bad
                else:
                    item["freshness"] = "CURRENT" if item["latest"] == str(as_of) else "EMPTY" if item["latest"] is None else "SOURCE_STALE"
            except (sqlite3.Error, ValueError, TypeError) as exc:
                item["detail"] = str(exc)
        item["required"] = True
        item["owner"] = item["writer"]
        record["datasets"][table] = item
    # The watch is an event ledger. A complete, current NO_HITS capture proves
    # there need not be a new event row today; an absent table is still refused.
    watch = record["datasets"].get("konglo_signal_watch")
    sources = record["datasets"].get("signal_source_status", {})
    states = sources.get("source_states", {})
    if (watch and watch["freshness"] in {"EMPTY", "SOURCE_STALE"}
            and (watch["latest"] is None or watch["latest"] < str(as_of))
            and sources.get("freshness") == "CURRENT"
            and all(states.get(s) in {nsc.NO_HITS, nsc.RETIRED_SOURCE} for s in KONGLO_SOURCES)):
        watch.update(freshness="CURRENT", observation_status="VERIFIED_NO_HITS",
                     freshness_evidence="signal_source_status:" + str(as_of))
    for table, item in record["datasets"].items():
        if item["freshness"] != "CURRENT":
            record["rejection_reasons"].append(f"{table}:{item['freshness']}")
    record["admitted"] = not record["rejection_reasons"]
    if not record["admitted"]:
        raise AdmissionRefused("Current ML report refused: " + "; ".join(record["rejection_reasons"]), record)
    return record
