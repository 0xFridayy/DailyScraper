"""
Historical backfill for broker_flow.netval + price_history from NeoBDM's
inventory JSON API.

WHY THIS IS A REWRITE
---------------------
The old implementation drove the retired /inventory/ Dash page: a react-select
dropdown, a DateRangePicker walked back month by month, a #submit-button, and a
Plotly chart read out of the DOM. That page was removed in a 2026-07/08 patch
(it now shows a retirement notice), which is why runs #15-#17 failed at a
selector that had previously been proven — the element no longer existed. Four
days of "the chart is one request behind" debugging chased a race that had
actually become a missing page.

The replacement UI, /inventory-chart/, is backed by a clean JSON endpoint:

    GET /api/inventory
        ?symbol=ENRG
        &start_date=2025-08-23
        &end_date=2026-08-23
        &investor_type=A
        &brokers=TOP_5_NB_LOT_C20
        &brokers=TOP_5_NS_LOT_C20

    -> { success, data: {
             date:  ["2025-09-29", ...],            # trading days, parallel to ohlc
             blot/slot/nlot: { "AK": [...], ... },  # buy/sell/NET LOT per broker
             bval/sval/nval: { "AK": [...], ... },  # buy/sell/net VALUE in full Rp
             ohlc:  [ {date, open, high, low, close, volume, volume_sma20}, ... ]
         }, meta: { symbol, brokers, start_date, end_date, investor_type } }

Authentication is the same session-cookie model the Market Summary screener
already uses: login once with Playwright, then issue authenticated GETs through
the browser context's request API. A GET needs no CSRF token.

This kills the entire DOM-scraping bug class — no dropdown, no date picker, no
chart-render race, no stale-fingerprint guessing, no "one request behind".

netval IS STILL LOT-DERIVED, IN BILLIONS
----------------------------------------
The endpoint hands us `nlot` (per-day NET lot per broker, = blot - slot; verified
per-day, not cumulative) directly, so netval is:

    netval = nlot[day] * 100 * close[day] / 1e9        (billions of Rupiah)

This is the SAME formula and the SAME unit as the old Plotly backfill, and it is
deliberate on two counts:

  1. It obeys the standing rule: derive flow from LOT, never from a displayed Rp
     value. (The old Plotly Rp trace truncated to 2 decimals at its display tier
     and silently zeroed billion-Rupiah days — see walk_forward_backtest.py.) The
     JSON `nval` here is actually full-precision and would be usable, but see (2).

  2. It matches the unit of the rows already stored by the old backfill, so a
     recent-window re-fetch heals existing rows in place rather than mixing two
     conventions. Contamination older than the endpoint's rolling year is
     reconciled from the archived full-market parquet instead.
     bval/sval/bavg/savg are left NULL for the same reason — the live path
     (neobdm_scraper.save_broker_flow) stores those in a different, page-derived
     unit, and reconciling the two conventions is a separate task, not this one.
     The API now provides real bval/sval, so populating them later is a one-line
     change once that convention is settled.

Every request is recorded in the capture manifest (inventory_capture), under
the repository root's _capture_manifest/ because this keeps no cache file: the
exact query before it is sent, with the two selectors recorded as selectors,
then the outcome and the brokers they resolved to. cache_ref is always null.

Usage: py backfill_inventory.py TICKER1 TICKER2 ...
(no args = all of TRACKED_TICKERS from neobdm_scraper)
"""

import sys
import json
import math
import sqlite3
import time
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright
import inventory_capture as ic
from neobdm_scraper import login, API_BASE, BROKER_FLOW_CODES, TRACKED_TICKERS, DB_PATH
from price_audit import (series_signature, should_fail_run, inventory_window,
                         inventory_window_is_short, ara_bound, ARB_BOUND, TOL)

from idx_calendar import IdxCalendarUnavailable, latest_idx_session_before
from price_contract import RAW_ACTUAL, positive_real, actual_bar_reason, adjudicate_series
from price_contract_frame import default_registry, quarantine_withholds
from price_history_revision import revision_changes, same_value as _same_value


class ValidatedPriceValues(dict):
    """Normalized source prices with independent per-session dispositions."""

    def __init__(self):
        super().__init__()
        self.dispositions = []
        self.changed = set()


# Priming this page first sets the csrftoken/sessionid cookies for the inventory
# path. The data GET is authenticated by the sessionid cookie alone (no CSRF).
INVENTORY_CHART_URL = "https://neobdm.tech/inventory-chart/"
INVENTORY_API = f"{API_BASE}/inventory"

BACKFILL_END = "2026-07-04"  # never overwrite live-scraped broker_flow rows from 07-05 on

# Above this share of failed tickers the run exits non-zero. A few failures are
# normal (suspended name, no data); a wholesale failure must not look like
# success — see price_audit.should_fail_run for why that mattered.
MAX_FAILURE_RATE = 0.30
FAILURE_SNAPSHOT = "topup-failure.json"   # raw first-failure response, a CI artifact

# The endpoint serves a ROLLING one-year window, and answers a start_date before
# it with success and only the last ~20 sessions (see
# price_audit.inventory_window). 365 sat exactly on that edge; 360 keeps the same
# margin as harvest_inventory.LOOKBACK_DAYS. From 2026-08-31 to 09-09 the API
# also gave every request, this one included, only ~20 sessions; since 09-10 it
# has served the full year again (~239 sessions a night).
WINDOW_DAYS = 360
MIN_SESSIONS = 100    # a year is ~239 sessions; the short fallback is <= 20
INVESTOR_TYPE = "A"   # A = All (foreign + domestic); matches the site default

# The site's own selector grammar, and the pair /inventory-chart/ itself sends:
# the 5 largest net buyers + 5 largest net sellers by lot over the last 20
# candles. On 2026-09-03 both these AND 30/101 explicit broker codes came back
# with only 10 brokers and 20 sessions -- that was the 08-31..09-09 short-answer
# period, not a lasting limit; the 2026-09-24 harvest got a full year for all
# 101 explicit codes. History older than the rolling year still cannot be
# re-fetched here; that cleanup uses the harvested ohlc.parquet through
# price_audit.py reconcile-cross-dups.
INVENTORY_BROKERS = ["TOP_5_NB_LOT_C20", "TOP_5_NS_LOT_C20"]


class InventoryError(RuntimeError):
    """The inventory API did not return usable data for this ticker."""

    def __init__(self, msg, raw=None):
        super().__init__(msg)
        self.raw = raw


class HistoricalRevisionError(InventoryError):
    """A source response tried to revise stored non-NULL observations."""

    def __init__(self, evidence):
        self.evidence = evidence
        first = evidence["refusals"][0]
        super().__init__(f"{evidence['ticker']} {first['session']}: historical_revision refused; "
                         f"no verified correction authorization; fields={','.join(first['changes'])}; "
                         f"{evidence.get('admission_refusal', '')}")


def _refuse_revisions(ticker, representation, revisions, admission_refusal=None):
    if revisions:
        evidence = {"reason": "UNAUTHORIZED_HISTORICAL_REVISION", "ticker": ticker,
                    "input_representation": representation, "refusals": revisions}
        if admission_refusal:
            evidence["admission_refusal"] = admission_refusal
        print(json.dumps({"historical_revision_refusal": evidence}, sort_keys=True))
        raise HistoricalRevisionError(evidence)


def _json_or_none(resp):
    try:
        return json.loads(resp.text())
    except Exception:
        return None


def _date_window(window_days=WINDOW_DAYS):
    """Requested date bounds, inside the API's rolling year (see WINDOW_DAYS)."""
    return inventory_window(datetime.now(timezone.utc), window_days)


def fetch_inventory(req, ticker, start_date, end_date, captures):
    """Authenticated GET; returns (payload, Capture, original response text).
    Raises InventoryError with the raw response text attached, so the first
    failure can be snapshotted. The caller keeps the original text for content
    rejections after a successful fetch.

    The request goes into `captures` (inventory_capture.CaptureLog) before it
    is sent, and a failure found here is recorded before it is raised. What
    became of a returned payload is for the caller to record."""
    query = [("symbol", ticker), ("start_date", start_date),
             ("end_date", end_date), ("investor_type", INVESTOR_TYPE)]
    query += [("brokers", b) for b in INVENTORY_BROKERS]
    qs = urlencode(query)
    url = f"{INVENTORY_API}?{qs}"

    cap = captures.begin(qs)
    try:
        resp = req.get(url, timeout=60000)
        raw = None
        try:
            raw = resp.text()
        except Exception:
            pass
        payload = _json_or_none(resp)
        cap.response(resp.status, raw, payload, ic.raw_body(resp))
        if not payload or not payload.get("success"):
            msg = (payload or {}).get("message")
            err = InventoryError(
                f"{ticker}: inventory API status={resp.status} success="
                f"{(payload or {}).get('success')} message={msg!r}", raw=raw)
            cap.finish(ic.source_refusal(resp.status, payload), err)
            raise err
    except Exception as e:
        cap.finish(ic.ERROR, e)          # only if nothing above recorded it
        raise
    return payload, cap, raw


def _real_close(close, ticker, day):
    """Normalize supported numeric closes to SQLite REAL before validation."""
    if isinstance(close, bool) or not isinstance(close, (int, float)):
        raise InventoryError(f"{ticker} {day}: ambiguous close {close!r}")
    try:
        normalized = float(close)
    except (OverflowError, TypeError, ValueError) as e:
        raise InventoryError(f"{ticker} {day}: close cannot be represented as REAL") from e
    if not math.isfinite(normalized) or normalized <= 0:
        raise InventoryError(f"{ticker} {day}: ambiguous close {close!r}")
    return normalized


def _refusal(ticker, day, record, close):
    reason = record["anchor_trust_reason"]
    if reason == "LIMIT_VIOLATION":
        official = record["limit_admission_status"] == "OUT_OF_BAND"
        reference = record["limit_reference_price"] if official else record["consistency_reference_price"]
        kind = record["limit_reference_kind"] if official else "UNADJUDICATED_PREVIOUS_ACTUAL_CLOSE"
        return (f"{ticker} {day}: limit_violation, reference {reference:g} ({kind}), previous session "
                f"{record['previous_actual_session']}, close {close:g} ({close / reference - 1:+.2%}); "
                f"open/high/low/close all checked - refusing to store")
    if reason in {"CROSS_TICKER_DUPLICATE", "DUPLICATE_IDENTITY", "SERIES_BREAK"}:
        return f"{ticker} {day}: independent price defect ({reason}) - refusing to store"
    if reason == "ZERO_VOLUME":
        return f"{ticker} {day}: UNRESOLVED UNVERIFIED_TRADING_SESSION - refusing to store"
    if reason == "UNRESOLVED_EVENT_REFERENCE":
        return f"{ticker} {day}: UNRESOLVED {record['limit_unresolved_reason']} - refusing to store"
    return f"{ticker} {day}: UNRESOLVED {reason} - refusing to store"


def validate_inventory_prices(conn, ticker, ohlc, *, registry=None, representation="UNKNOWN", market="REGULAR"):
    """Refuse unauthorized revisions and invalid repairs before any writes.

    Judge the proposed series, including stored neighbours outside the response,
    with the same adjudication core as the audit (price_contract.adjudicate_series).
    A revised predecessor can invalidate an unchanged successor. A bar is
    compared with the close of its immediately preceding exchange session when
    that observation is admissible; otherwise it is stored only as an
    unadjudicated source capture that can start a restart window, never as a
    validated daily comparison. Unchanged historical observations keep their
    audit status without blocking every future top-up of the rolling year. No
    corporate-action exception is inferred.
    """
    registry = registry or default_registry()
    incoming = ValidatedPriceValues()
    for bar in ohlc:
        day, close = bar.get("date"), bar.get("close")
        try:
            canonical = date.fromisoformat(day).isoformat()
        except (TypeError, ValueError):
            canonical = None
        if canonical is None or canonical != day:
            raise InventoryError(f"{ticker}: ambiguous price date {day!r}")
        if day in incoming:
            raise InventoryError(f"{ticker}: duplicate date {day} in ohlc")
        incoming[day] = _real_close(close, ticker, day)
        reason = actual_bar_reason(bar)
        if reason:
            raise InventoryError(f"{ticker} {day}: OHLC domain: {reason}")

    fields = ("date", "open", "high", "low", "close", "volume")
    stored_bars = {row[0]: dict(zip(fields, row)) for row in conn.execute(
        "SELECT date, open, high, low, close, volume FROM price_history WHERE ticker=? ORDER BY date", (ticker,))}
    revisions = [{"session": bar["date"], "changes": changes}
                 for bar in ohlc if bar["date"] in stored_bars
                 if (changes := revision_changes(stored_bars[bar["date"]], bar))]
    proposed_bars = stored_bars | {bar["date"]: bar for bar in ohlc}
    changed = {bar["date"] for bar in ohlc if bar["date"] not in stored_bars
               or not all(_same_value(stored_bars[bar["date"]].get(field), bar.get(field))
                          for field in ("open", "high", "low", "close", "volume"))}
    # Independent defects come from the whole proposed snapshot, before a
    # quarantine overlay or this ticker's predecessor chain can grant trust.
    import pandas as pd
    from price_contract_frame import independent_price_defects, external_reasons
    snapshot = [dict(zip(("ticker",) + fields, row)) for row in conn.execute(
        "SELECT ticker,date,open,high,low,close,volume FROM price_history WHERE ticker<>?", (ticker,))]
    days = sorted(proposed_bars)
    snapshot.extend(dict(proposed_bars[day], ticker=ticker) for day in days)
    proposed_frame = pd.DataFrame(snapshot, columns=("ticker",) + fields)
    defects = independent_price_defects(proposed_frame, registry, market)
    mine = proposed_frame.index[proposed_frame.ticker.eq(ticker)]
    quarantined = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='price_quarantine' AND type='table'").fetchone():
        columns = {r[1] for r in conn.execute("PRAGMA table_info(price_quarantine)")}
        if "reasons" in columns:
            quarantined = dict(conn.execute("SELECT date,reasons FROM price_quarantine WHERE ticker=?", (ticker,)))
        else:
            quarantined = {r[0]: None for r in conn.execute("SELECT date FROM price_quarantine WHERE ticker=?", (ticker,))}
    # A quarantine record judges the stored values. A changed bar is new source
    # evidence and is judged afresh; an official reference can recover a sole
    # ordinary-limit quarantine of an unchanged event bar.
    trusted = []
    independent = external_reasons(defects.loc[mine], [True] * len(mine))
    for day, defect in zip(days, independent):
        trusted.append(day not in quarantined or day in changed or not quarantine_withholds(
            ticker, day, proposed_bars[day], quarantined[day], registry=registry, market=market,
            representation=representation, independent_defect=defect is not None))
    rows = [dict(proposed_bars[day], external_reason=reason, source_known=True, context_complete=bool(complete))
            for day, reason, complete in zip(days, external_reasons(defects.loc[mine], trusted),
                                             defects.loc[mine, "series_context_complete"])]
    records = dict(zip(days, adjudicate_series(ticker, rows, registry, market=market,
                                               representation=representation,
                                               source="proposed-price-history")))
    successors = set()
    for day in days:
        try:
            if day not in changed and latest_idx_session_before(date.fromisoformat(day)).isoformat() in changed:
                successors.add(day)
        except (IdxCalendarUnavailable, ValueError, TypeError):
            continue
    for day in days:
        record = records[day]
        if day not in changed and day not in successors:
            if day in incoming:
                incoming.dispositions.append({"session": day, "status": "PRESERVED_UNADJUDICATED"})
            continue
        if record["anchor_trust_status"] == "INADMISSIBLE":
            # A stored successor is re-judged only on its transition from the
            # revised predecessor; its other historical findings stay with the audit.
            if day in changed or record["anchor_trust_reason"] == "LIMIT_VIOLATION":
                refusal = _refusal(ticker, day, record, proposed_bars[day]["close"])
                _refuse_revisions(ticker, representation, revisions, refusal)
                raise InventoryError(refusal)
            continue
        if day not in incoming:
            continue
        if record["anchor_trust_status"] == "TRUSTED_EVENT_ANCHOR":
            status = "IN_BAND"
        elif record["limit_reference_status"] == "RESOLVED":
            status = "IN_BAND" if representation == RAW_ACTUAL else "ORDINARY_DIAGNOSTIC_ONLY"
        elif record["consistency_status"] == "IN_BAND":
            status = "RESTART_PENDING"
        else:
            status = "SOURCE_CAPTURE_UNADJUDICATED"
        incoming.dispositions.append({"session": day, "status": status,
                                      "anchor_trust_status": record["anchor_trust_status"],
                                      "unresolved_reason": record["limit_unresolved_reason"] or None,
                                      "reference_kind": record["limit_reference_kind"],
                                      "reference_price": record["limit_reference_price"],
                                      "consistency_reference_price": record["consistency_reference_price"],
                                      "previous_actual_session": record["previous_actual_session"],
                                      "previous_actual_close": record["previous_actual_close"],
                                      "event_id": record["corporate_action_event_id"],
                                      "registry_sha256": registry.content_sha256})

    # Admission answers whether a proposed bar is plausible. It never grants
    # permission to replace an observation. Keep the existing domain/transition
    # refusals, then independently refuse every non-NULL historical revision.
    _refuse_revisions(ticker, representation, revisions)
    incoming.changed = changed
    return incoming


def insert_inventory(conn, ticker, payload, *, registry=None, representation="UNKNOWN", market="REGULAR"):
    """Validate and write one ticker atomically, leaving commit to the caller.

    A savepoint protects callers that catch failures, including autocommit
    connections. An outer transaction prevents releasing the savepoint from
    committing before run_backfill records its persistence intent. Acquire the
    write lock before reading stored observations when we own the transaction.
    """
    owned_transaction = not conn.in_transaction
    if owned_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("SAVEPOINT inventory_ticker")
        result = _insert_inventory(conn, ticker, payload, registry=registry,
                                   representation=representation, market=market)
        conn.execute("RELEASE inventory_ticker")
    except BaseException:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK TO inventory_ticker")
                conn.execute("RELEASE inventory_ticker")
        except sqlite3.Error:
            # A lost savepoint cannot leave partial financial writes pending.
            conn.rollback()
        finally:
            if owned_transaction:
                conn.rollback()
        raise
    return result


def _insert_inventory(conn, ticker, payload, *, registry=None, representation="UNKNOWN", market="REGULAR"):
    """Store price_history (all days) + broker_flow (days <= BACKFILL_END).

    Returns (broker_rows, price_rows, returned_broker_codes, signature).
    """
    registry = registry or default_registry()
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}

    # Hard gate: the API is symbol-keyed, but never store a payload whose meta
    # disagrees with what we asked for.
    shown = str(meta.get("symbol") or "").upper()
    if not shown:
        raise InventoryError(f"API omitted symbol for requested {ticker} — refusing to store")
    if shown != ticker.upper():
        raise InventoryError(
            f"API returned symbol {shown} for requested {ticker} — refusing to store")

    ohlc = data.get("ohlc") or []
    if not ohlc:
        return 0, 0, [], None

    close_by_date = validate_inventory_prices(conn, ticker, ohlc, registry=registry,
                                              representation=representation, market=market)
    print(json.dumps({"ticker": ticker, "price_dispositions": close_by_date.dispositions,
                      "price_contract": registry.identity,
                      "input_representation": representation}, sort_keys=True))

    price_rows = [
        (o["date"], ticker, positive_real(o["open"]), positive_real(o["high"]),
         positive_real(o["low"]), close_by_date[o["date"]], float(o["volume"]))
        for o in ohlc if o["date"] in close_by_date.changed
    ]
    conn.executemany(
        """INSERT INTO price_history
           (date, ticker, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(date,ticker) DO UPDATE SET
             open=COALESCE(price_history.open,excluded.open),
             high=COALESCE(price_history.high,excluded.high),
             low=COALESCE(price_history.low,excluded.low),
             close=COALESCE(price_history.close,excluded.close),
             volume=COALESCE(price_history.volume,excluded.volume)""",
        price_rows,
    )

    dates = data.get("date") or []          # parallel to every nlot[...] series
    nlot = data.get("nlot") or {}
    returned = sorted(nlot.keys())

    broker_rows = []
    for code in returned:
        if code not in BROKER_FLOW_CODES:
            continue
        series = nlot.get(code) or []
        for i, d in enumerate(dates):
            if d > BACKFILL_END:            # protect live-scraped rows
                continue
            close = close_by_date.get(d)
            if close is None or i >= len(series) or series[i] is None:
                continue
            netval = (series[i] * 100 * close) / 1e9   # lot-derived, billions
            broker_rows.append((d, ticker, code, None, None, netval, None, None))

    conn.executemany(
        """INSERT INTO broker_flow
           (date, ticker, broker_code, bval, sval, netval, bavg, savg)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date,ticker,broker_code) DO UPDATE SET
             bval=excluded.bval, sval=excluded.sval, netval=excluded.netval,
             bavg=excluded.bavg, savg=excluded.savg
           WHERE broker_flow.bval IS NOT excluded.bval OR broker_flow.sval IS NOT excluded.sval
              OR broker_flow.netval IS NOT excluded.netval OR broker_flow.bavg IS NOT excluded.bavg
              OR broker_flow.savg IS NOT excluded.savg""",
        broker_rows,
    )

    sig = series_signature(
        {"x": [o["date"] for o in ohlc], "close": [close_by_date[o["date"]] for o in ohlc]})
    # Counts retain the public response coverage contract, including repeats.
    return len(broker_rows), len(ohlc), returned, sig


def run_backfill(tickers):
    run_registry = default_registry()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            date TEXT NOT NULL, ticker TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (date, ticker)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_flow (
            date TEXT NOT NULL, ticker TEXT NOT NULL, broker_code TEXT NOT NULL,
            bval REAL, sval REAL, netval REAL, bavg REAL, savg REAL,
            PRIMARY KEY (date, ticker, broker_code)
        )
    """)

    start_date, end_date = _date_window()
    print(f"Inventory window: {start_date} .. {end_date} "
          f"(brokers={INVENTORY_BROKERS}, investor_type={INVESTOR_TYPE})")

    print("price_contract=" + json.dumps(run_registry.identity, sort_keys=True))
    failed = []
    refused_revisions = []
    sessions = []   # sessions each stored ticker actually got back
    captures = ic.CaptureLog(ic.NO_CACHE_ROOT, "backfill_inventory", writes_cache=False,
                             broker_list_source="backfill_inventory.INVENTORY_BROKERS")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        page.set_default_timeout(60000)
        login(page)
        # Prime cookies for the inventory path; the data GETs go through this same
        # authenticated context, so no per-ticker page load is needed after this.
        page.goto(INVENTORY_CHART_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        req = page.context.request

        prev_signature = None
        prev_ticker = None

        for ticker in tickers:
            print(f"=== {ticker} ===")
            cap = None      # this ticker's capture, once fetch_inventory returns it
            response_raw = None
            try:
                payload, cap, response_raw = fetch_inventory(
                    req, ticker, start_date, end_date, captures)
                response_bars = payload["data"].get("ohlc") or []
                response_signature = series_signature({"x": [bar["date"] for bar in response_bars],
                                                       "close": [bar["close"] for bar in response_bars]})
                if response_signature is not None and response_signature == prev_signature:
                    raise InventoryError(f"series identical to {prev_ticker} — stale response, not stored")
                broker_n, price_n, returned, signature = insert_inventory(
                    conn, ticker, payload, registry=run_registry)

                if price_n == 0:
                    cap.finish(ic.EMPTY, "no ohlc rows: nothing stored")
                    print("  no inventory data")
                    continue

                # Last line of defence, needing no knowledge of the API: two
                # different stocks cannot produce byte-identical OHLCV.
                if signature is not None and signature == prev_signature:
                    raise InventoryError(
                        f"series identical to {prev_ticker} — stale response, not stored")

                dates = payload["data"].get("date") or []
                session_count = len(dates)
                kept = [c for c in returned if c in BROKER_FLOW_CODES]
                rng = f"{dates[0]} to {dates[-1]}" if dates else "n/a"
                # On record before the commit makes the rows durable: if the OK
                # line below never lands, the reader says PERSIST_UNCONFIRMED.
                cap.persisting("neobdm.db")
                conn.commit()
                prev_signature, prev_ticker = signature, ticker
                sessions.append(session_count)
                # The rows are committed and the clone guard must reflect them
                # before writing the separate audit result. Retry only that
                # result line; an audit failure must stop the run, not enter the
                # ticker-failure handler or permit a stale clone check.
                try:
                    cap.finish(ic.OK)
                except Exception:
                    try:
                        cap.finish(ic.OK)
                    except Exception as audit_error:
                        raise SystemExit(
                            f"{ticker}: capture manifest result failed after DB commit"
                        ) from audit_error
                print(f"  {broker_n} broker_flow rows, {price_n} price_history rows ({rng})")
                print(f"  brokers returned={returned} kept(in BROKER_FLOW_CODES)={kept}")
            except Exception as e:
                # insert_inventory rolls back its own failures. The outer
                # transaction also covers post-writer checks and the capture
                # persistence record. Earlier tickers are already committed.
                conn.rollback()
                # A failure fetch_inventory already recorded has no cap here.
                if cap is not None:
                    cap.finish(ic.REJECTED if isinstance(e, InventoryError) else ic.ERROR, e)
                print(f"  FAILED: {e}")
                raw = getattr(e, "raw", None)
                if isinstance(e, HistoricalRevisionError):
                    refused_revisions.append(ticker)
                    # Preserve only the financial diff, never an authenticated
                    # response's extra fields, cookies or headers.
                    raw = json.dumps(e.evidence, sort_keys=True)
                elif isinstance(e, InventoryError) and response_raw is not None:
                    raw = response_raw
                if not failed and raw is not None:
                    # First failure only: one diagnostic is enough to diagnose,
                    # and 45 dumps would be noise. This replaces the old page
                    # screenshot — there is no page to snapshot now, the response
                    # body IS the diagnostic.
                    try:
                        with open(FAILURE_SNAPSHOT, "w", encoding="utf-8", newline="") as fh:
                            fh.write(raw)
                        print(f"  saved {FAILURE_SNAPSHOT} for diagnosis")
                    except Exception as snap_err:
                        print(f"  (could not save failure snapshot: {snap_err})")
                failed.append(ticker)
            time.sleep(1.5)   # pace against NeoBDM's ~50-request abuse budget

        browser.close()

    conn.close()
    print(f"\nFailed tickers: {failed}")

    if refused_revisions:
        sys.exit(f"ABORT: unauthorized historical revisions refused for {refused_revisions}; "
                 "do not commit this run, regardless of the ticker failure rate")

    # A warning, not an exit: the short fallback still ends at the latest
    # session, so price_history WAS topped up. Failing here would have blocked
    # every top-up from 2026-08-31 to 09-09 and saved nothing.
    if inventory_window_is_short(sessions, MIN_SESSIONS):
        print(f"::warning::the longest inventory series was {max(sessions)} "
              f"sessions for {start_date}..{end_date}, under {MIN_SESSIONS}: the "
              f"API answered with its short fallback, not the year asked for. "
              f"price_history is current but nothing older was refreshed -- check "
              f"WINDOW_DAYS against the API's rolling window.")

    if should_fail_run(len(failed), len(tickers), MAX_FAILURE_RATE):
        rate = len(failed) / len(tickers) if tickers else 1.0
        sys.exit(
            f"ABORT: {len(failed)}/{len(tickers)} tickers failed ({rate:.0%}, "
            f"limit {MAX_FAILURE_RATE:.0%}). Not a partial outage — treat this as "
            f"the scrape being broken, and check {FAILURE_SNAPSHOT}."
        )


if __name__ == "__main__":
    targets = sys.argv[1:] or sorted(TRACKED_TICKERS)
    run_backfill(targets)
