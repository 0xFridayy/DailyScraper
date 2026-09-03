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
             date:  ["2026-08-04", ...],            # currently capped at 20 sessions
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
     conventions. Older contamination is reconciled from the archived full-
     market parquet; the live endpoint no longer serves enough history.
     bval/sval/bavg/savg are left NULL for the same reason — the live path
     (neobdm_scraper.save_broker_flow) stores those in a different, page-derived
     unit, and reconciling the two conventions is a separate task, not this one.
     The API now provides real bval/sval, so populating them later is a one-line
     change once that convention is settled.

Usage: py backfill_inventory.py TICKER1 TICKER2 ...
(no args = all of TRACKED_TICKERS from neobdm_scraper)
"""

import sys
import json
import sqlite3
import time
from datetime import date, timedelta
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright
from neobdm_scraper import login, API_BASE, BROKER_FLOW_CODES, TRACKED_TICKERS, DB_PATH
from price_audit import series_signature, should_fail_run

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

# Ask for the longest window the endpoint historically allowed. As of 2026-09-03
# the live service caps every tested request shape to 20 sessions despite these
# dates; this remains useful for nightly top-ups but cannot heal older history.
WINDOW_DAYS = 365
INVESTOR_TYPE = "A"   # A = All (foreign + domestic); matches the site default

# The site's own selector grammar. Live verification on 2026-09-03 established
# that both these selectors AND 30/101 repeated explicit broker codes now return
# only 10 brokers and 20 sessions. Keep the dashboard selectors here because they
# at least choose the current dominant buyers/sellers for top-ups. Historical
# cleanup uses the previously harvested authoritative ohlc.parquet through
# price_audit.py reconcile-cross-dups instead of pretending this endpoint can
# still re-fetch a year.
INVENTORY_BROKERS = ["TOP_5_NB_LOT_C20", "TOP_5_NS_LOT_C20"]


class InventoryError(RuntimeError):
    """The inventory API did not return usable data for this ticker."""

    def __init__(self, msg, raw=None):
        super().__init__(msg)
        self.raw = raw


def _json_or_none(resp):
    try:
        return json.loads(resp.text())
    except Exception:
        return None


def _date_window(window_days=WINDOW_DAYS):
    """Requested date bounds; the service may return a shorter capped window."""
    end = date.today()
    return (end - timedelta(days=window_days)).isoformat(), end.isoformat()


def fetch_inventory(req, ticker, start_date, end_date):
    """Authenticated GET of the inventory endpoint. Raises InventoryError with the
    raw response text attached, so the first failure can be snapshotted."""
    query = [("symbol", ticker), ("start_date", start_date),
             ("end_date", end_date), ("investor_type", INVESTOR_TYPE)]
    query += [("brokers", b) for b in INVENTORY_BROKERS]
    url = f"{INVENTORY_API}?{urlencode(query)}"

    resp = req.get(url, timeout=60000)
    raw = None
    try:
        raw = resp.text()
    except Exception:
        pass
    payload = _json_or_none(resp)
    if not payload or not payload.get("success"):
        msg = (payload or {}).get("message")
        raise InventoryError(
            f"{ticker}: inventory API status={resp.status} success="
            f"{(payload or {}).get('success')} message={msg!r}", raw=raw)
    return payload


def insert_inventory(conn, ticker, payload):
    """Store price_history (all days) + broker_flow (days <= BACKFILL_END).

    Returns (broker_rows, price_rows, returned_broker_codes, signature).
    """
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}

    # Hard gate: the API is symbol-keyed, but never store a payload whose meta
    # disagrees with what we asked for.
    shown = str(meta.get("symbol") or "").upper()
    if shown and shown != ticker.upper():
        raise InventoryError(
            f"API returned symbol {shown} for requested {ticker} — refusing to store")

    ohlc = data.get("ohlc") or []
    if not ohlc:
        return 0, 0, [], None

    price_rows = [
        (o["date"], ticker, o.get("open"), o.get("high"),
         o.get("low"), o.get("close"), o.get("volume"))
        for o in ohlc
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO price_history
           (date, ticker, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)""",
        price_rows,
    )

    close_by_date = {o["date"]: o.get("close") for o in ohlc}
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
        """INSERT OR REPLACE INTO broker_flow
           (date, ticker, broker_code, bval, sval, netval, bavg, savg)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        broker_rows,
    )

    sig = series_signature(
        {"x": [o["date"] for o in ohlc], "close": [o.get("close") for o in ohlc]})
    return len(broker_rows), len(price_rows), returned, sig


def run_backfill(tickers):
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

    failed = []
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
            try:
                payload = fetch_inventory(req, ticker, start_date, end_date)
                broker_n, price_n, returned, signature = insert_inventory(
                    conn, ticker, payload)

                if price_n == 0:
                    print("  no inventory data")
                    continue

                # Last line of defence, needing no knowledge of the API: two
                # different stocks cannot produce byte-identical OHLCV.
                if signature is not None and signature == prev_signature:
                    raise InventoryError(
                        f"series identical to {prev_ticker} — stale response, not stored")

                conn.commit()
                prev_signature, prev_ticker = signature, ticker
                kept = [c for c in returned if c in BROKER_FLOW_CODES]
                dates = payload["data"].get("date") or []
                rng = f"{dates[0]} to {dates[-1]}" if dates else "n/a"
                print(f"  {broker_n} broker_flow rows, {price_n} price_history rows ({rng})")
                print(f"  brokers returned={returned} kept(in BROKER_FLOW_CODES)={kept}")
            except Exception as e:
                print(f"  FAILED: {e}")
                raw = getattr(e, "raw", None)
                if not failed and raw is not None:
                    # First failure only: one raw response is enough to diagnose,
                    # and 45 dumps would be noise. This replaces the old page
                    # screenshot — there is no page to snapshot now, the response
                    # body IS the diagnostic.
                    try:
                        with open(FAILURE_SNAPSHOT, "w", encoding="utf-8") as fh:
                            fh.write(raw)
                        print(f"  saved {FAILURE_SNAPSHOT} for diagnosis")
                    except Exception as snap_err:
                        print(f"  (could not save failure snapshot: {snap_err})")
                failed.append(ticker)
            time.sleep(1.5)   # pace against NeoBDM's ~50-request abuse budget

        browser.close()

    conn.close()
    print(f"\nFailed tickers: {failed}")

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
