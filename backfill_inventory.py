"""
Historical backfill for broker_flow.netval + price_history from the
/inventory/ page's per-broker cumulative Plotly chart.

The chart's DEFAULT view only shows ~3 months, which is easy to mistake for
the actual data limit (that mistake was made once already — see
walk_forward_backtest.py's docstring). It has a DateRangePicker (Start Date /
End Date fields) that goes back much further: as of 2026-07-07 the real
earliest available date was 2025-08-01 (everything before that is greyed out
"Not available" in the calendar). This script drives that picker to the
earliest available date instead of accepting the default range.

Whether that earliest-available date is a FIXED point (the site's actual
data start) or a ROLLING window (e.g. "always ~11 months back from today")
is not established — rerun find_earliest_available_date() standalone
periodically to check, rather than assuming 2025-08-01 stays valid forever.

Derives netval the same validated way as the original backfill: (cum_lot[d]
- cum_lot[d-1]) * 100 * close[d], NOT from the chart's own cumulative Rp
string (which truncates to 2 decimals at its display tier and silently
zeroes real multi-billion-Rupiah daily flows for large cumulative
positions — see walk_forward_backtest.py's docstring for the validation
that caught this).

Usage: py backfill_inventory.py TICKER1 TICKER2 ...
(no args = all of TRACKED_TICKERS from neobdm_scraper)
"""

import sys
import re
import sqlite3
import time

from playwright.sync_api import sync_playwright
from neobdm_scraper import login, NEOBDM_INVENTORY_URL, BROKER_FLOW_CODES, TRACKED_TICKERS, DB_PATH
from price_audit import series_signature, ticker_from_title

BACKFILL_END = "2026-07-04"  # never overwrite live-scraped rows from 07-05 onward

EXTRACT_JS = """() => {
    const el = document.querySelector('.js-plotly-plot');
    function unwrap(v) {
        if (v && v._inputArray) return Object.values(v._inputArray);
        return v;
    }
    if (!el || !el.data) return null;
    const lay = el.layout || {};
    const rawTitle = (lay.title && (lay.title.text !== undefined ? lay.title.text : lay.title)) || '';
    const out = {brokers: [], price: null, volume: null, title: String(rawTitle)};
    for (const t of el.data) {
        if (t.name === 'price') {
            out.price = {x: t.x, open: unwrap(t.open), high: unwrap(t.high), low: unwrap(t.low), close: unwrap(t.close)};
        } else if (t.name === 'volume') {
            out.volume = {x: t.x, y: unwrap(t.y)};
        } else if (t.mode === undefined || t.mode === null) {
            out.brokers.push({name: t.name, x: t.x, cum_lot: unwrap(t.y), cum_rp_str: t.customdata});
        }
    }
    return out;
}"""


def clean_code(name):
    return re.sub(r"[^\w]", "", name)


def extend_date_range_to_earliest(page, max_months_back=36):
    """Open the Start Date picker and walk it back month by month until hitting
    the "Not available" boundary, then click the last available day-1. Must be
    called with a ticker already selected (the date inputs aren't live until
    then). Returns the date string that was set, or None if nothing changed."""
    start_input = page.locator('input[aria-label="Start Date"]')
    start_input.click()
    page.wait_for_timeout(600)

    prev_btn = page.locator('div[aria-label="Move backward to switch to the previous month."]')
    last_available_month_start = None
    for _ in range(max_months_back):
        prev_btn.click()
        page.wait_for_timeout(200)
        label = page.evaluate("""() => {
            const cal = document.querySelector('.DateRangePicker_picker');
            const first = cal.querySelector('td[aria-label]');
            return first ? first.getAttribute('aria-label') : null;
        }""")
        if label and "Not available" in label:
            break
        last_available_month_start = label

    if last_available_month_start is None:
        page.keyboard.press("Escape")
        return None

    # click day 1 of the earliest still-available month (parse it back out of
    # the aria-label, e.g. "Choose Friday, August 1, 2025 as your check-in date...")
    m = re.search(r"([A-Z][a-z]+) 1, (\d{4})", last_available_month_start)
    if not m:
        page.keyboard.press("Escape")
        return None
    month_name, year = m.group(1), m.group(2)

    clicked = page.evaluate(
        """(needle) => {
            const cal = document.querySelector('.DateRangePicker_picker');
            const target = Array.from(cal.querySelectorAll('td[aria-label]'))
                .find(el => (el.getAttribute('aria-label')||'').includes(needle));
            if (!target) return null;
            target.click();
            return target.getAttribute('aria-label');
        }""",
        f"{month_name} 1, {year}",
    )
    page.wait_for_timeout(800)
    # close the picker on a blank part of the page (NOT the sidebar nav, which
    # will navigate away) so the Fetch button isn't blocked by the overlay
    page.mouse.click(1700, 300)
    page.wait_for_timeout(500)
    return clicked


# ─────────────────────────────────────────────
#  TICKER SELECTION + RENDER WAITING
#
#  The original implementation was two races stacked on each other:
#
#      page.keyboard.type(ticker); page.wait_for_timeout(1500)
#      page.keyboard.press("Enter")          # takes the HIGHLIGHTED option,
#                                            # not necessarily the typed one
#      page.click("#submit-button"); page.wait_for_timeout(15000)
#      return page.evaluate(EXTRACT_JS)      # may still be the PREVIOUS chart
#
#  That is how identical OHLCV rows ended up stored under two different tickers
#  (see price_audit.py). Measured 2026-08-21, it is a genuine race and not a
#  deterministic mis-pick: an unchanged rerun healed 899 of 1,400 contaminated
#  rows overnight, taking CDIA and COIN from 231 bad rows each to 0, while
#  breaking zero new ones. Winning a race by luck is not a fix, so both halves
#  now wait on a CONDITION instead of a duration.
# ─────────────────────────────────────────────

SELECT_TIMEOUT = 20_000
CHART_RENDER_TIMEOUT = 60_000
CHART_STABLE_TRIES = 20
CHART_STABLE_PAUSE = 500

# Shared by the read and the wait so both compute the identical fingerprint.
_FINGERPRINT_FN = """
function __fp() {
    const el = document.querySelector('.js-plotly-plot');
    if (!el || !el.data || !el.data.length) return null;
    const t = el.data.find(d => d.name === 'price');
    if (!t || !t.x || !t.x.length) return null;
    const unwrap = v => (v && v._inputArray) ? Object.values(v._inputArray) : v;
    const c = unwrap(t.close) || [];
    const lay = el.layout || {};
    const title = (lay.title && (lay.title.text !== undefined ? lay.title.text : lay.title)) || '';
    return JSON.stringify({n: t.x.length, x0: t.x[0], xN: t.x[t.x.length - 1],
                           c0: c[0], cN: c[c.length - 1],
                           title: String(title), traces: el.data.length});
}
"""

READ_FINGERPRINT_JS = "() => { %s return __fp(); }" % _FINGERPRINT_FN
CHART_CHANGED_JS = ("(prev) => { %s const fp = __fp(); "
                    "return fp !== null && fp !== prev; }" % _FINGERPRINT_FN)

# react-select renders options as .Select-option; the label may be just the
# code or "CODE - Company Name", so match the leading code either way.
_OPTION_CODE_JS = """
function __code(el) {
    return el.textContent.replace(/\s+/g, ' ').trim().toUpperCase().split(/[^A-Z0-9]+/)[0];
}
"""

OPTION_READY_JS = ("([want]) => { %s return Array.from("
                   "document.querySelectorAll('#tick .Select-option'))"
                   ".some(o => __code(o) === want.toUpperCase()); }" % _OPTION_CODE_JS)

FIND_OPTION_JS = ("([want]) => { %s "
                  "const opts = Array.from(document.querySelectorAll('#tick .Select-option'));"
                  "return {index: opts.findIndex(o => __code(o) === want.toUpperCase()),"
                  " texts: opts.slice(0, 8).map(o => o.textContent.replace(/\s+/g,' ').trim())}; }"
                  % _OPTION_CODE_JS)

VALUE_SETTLED_JS = ("([want]) => { %s "
                    "const el = document.querySelector('#tick .Select-value-label')"
                    " || document.querySelector('#tick .Select-value');"
                    "return !!el && __code(el) === want.toUpperCase(); }" % _OPTION_CODE_JS)


class TickerMismatch(RuntimeError):
    """The page is not showing the ticker we asked for. Never store the payload."""


def select_ticker(page, ticker):
    """Pick the option whose text actually matches `ticker`, then confirm it.

    Deliberately never presses Enter: Enter accepts whichever option happens to
    be highlighted at that instant, which is the original defect.
    """
    page.click("#tick .Select-control")
    page.wait_for_timeout(300)
    page.keyboard.press("Control+A")          # drop the previous ticker's text
    page.keyboard.type(ticker, delay=40)

    page.wait_for_function(OPTION_READY_JS, arg=[ticker], timeout=SELECT_TIMEOUT)

    found = page.evaluate(FIND_OPTION_JS, [ticker])
    if found["index"] < 0:
        raise TickerMismatch(f"{ticker}: no dropdown option matched (saw {found['texts']})")

    # Click through Playwright rather than el.click(): react-select v1 acts on
    # mousedown, which a synthetic click() alone would not deliver.
    page.locator("#tick .Select-option").nth(found["index"]).click()
    page.wait_for_function(VALUE_SETTLED_JS, arg=[ticker], timeout=SELECT_TIMEOUT)


def scrape_ticker(page, ticker, date_range_already_set, prev_fingerprint=None):
    """Returns (payload, fingerprint). Caller passes the PREVIOUS ticker's
    fingerprint so we can prove the chart actually turned over."""
    select_ticker(page, ticker)

    if not date_range_already_set:
        extend_date_range_to_earliest(page)

    page.click("#submit-button")

    # 1. Wait for the chart to stop being whatever was on screen before.
    page.wait_for_function(CHART_CHANGED_JS, arg=prev_fingerprint,
                           timeout=CHART_RENDER_TIMEOUT)

    # 2. Wait for it to stop moving, so extraction cannot catch a partial redraw.
    fingerprint = page.evaluate(READ_FINGERPRINT_JS)
    for _ in range(CHART_STABLE_TRIES):
        page.wait_for_timeout(CHART_STABLE_PAUSE)
        again = page.evaluate(READ_FINGERPRINT_JS)
        if again == fingerprint:
            break
        fingerprint = again
    else:
        raise TickerMismatch(f"{ticker}: chart never stopped redrawing")

    return page.evaluate(EXTRACT_JS), fingerprint




def insert_ticker_data(conn, ticker, data):
    # Hard gate: never write a payload the chart itself disagrees with.
    shown = ticker_from_title((data or {}).get("title"))
    if shown and shown != ticker.upper():
        raise TickerMismatch(
            f"chart is showing {shown} but we asked for {ticker} — refusing to store it")

    price = data.get("price")
    if not price or not data.get("brokers"):
        return 0, 0

    price_rows = [
        (d, ticker, price["open"][i], price["high"][i], price["low"][i], price["close"][i],
         data["volume"]["y"][i] if data["volume"] else None)
        for i, d in enumerate(price["x"])
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO price_history
           (date, ticker, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)""",
        price_rows,
    )

    price_by_date = dict(zip(price["x"], price["close"]))
    broker_rows = []
    for b in data["brokers"]:
        code = clean_code(b["name"])
        if code not in BROKER_FLOW_CODES:
            continue
        dates, cum_lot = b["x"], b["cum_lot"]
        for i in range(1, len(dates)):
            d = dates[i]
            if d > BACKFILL_END:
                continue
            close = price_by_date.get(d)
            if close is None:
                continue
            lot_diff = cum_lot[i] - cum_lot[i - 1]
            netval = (lot_diff * 100 * close) / 1e9
            broker_rows.append((d, ticker, code, None, None, netval, None, None))

    conn.executemany(
        """INSERT OR REPLACE INTO broker_flow
           (date, ticker, broker_code, bval, sval, netval, bavg, savg)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        broker_rows,
    )
    return len(broker_rows), len(price_rows)


def run_backfill(tickers):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            date TEXT NOT NULL, ticker TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (date, ticker)
        )
    """)

    failed = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, slow_mo=150)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        page.set_default_timeout(60000)
        login(page)
        page.goto(NEOBDM_INVENTORY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)

        prev_fingerprint = page.evaluate(READ_FINGERPRINT_JS)
        prev_signature = None
        prev_ticker = None

        for i, ticker in enumerate(tickers):
            print(f"=== {ticker} ===")
            try:
                data, prev_fingerprint = scrape_ticker(
                    page, ticker, date_range_already_set=(i > 0),
                    prev_fingerprint=prev_fingerprint)
                if data is None or not data.get("brokers"):
                    print("  no chart data")
                    continue

                # Last line of defence, and the one that needs no knowledge of
                # the page: two different stocks cannot have byte-identical
                # OHLCV. If they do, we are looking at a stale chart.
                signature = series_signature(data.get("price"))
                if signature is not None and signature == prev_signature:
                    raise TickerMismatch(
                        f"series identical to {prev_ticker} — stale chart, not stored")

                broker_n, price_n = insert_ticker_data(conn, ticker, data)
                conn.commit()
                prev_signature, prev_ticker = signature, ticker
                rng = f"{data['price']['x'][0]} to {data['price']['x'][-1]}" if data["price"] else "n/a"
                print(f"  {broker_n} broker_flow rows, {price_n} price_history rows ({rng})")
            except Exception as e:
                print(f"  FAILED: {e}")
                failed.append(ticker)
                # Re-read rather than carrying a fingerprint that may describe a
                # chart we rejected, which would make the NEXT wait trivially true.
                prev_fingerprint = page.evaluate(READ_FINGERPRINT_JS)
            time.sleep(2)

        browser.close()

    conn.close()
    print(f"\nFailed tickers: {failed}")


if __name__ == "__main__":
    targets = sys.argv[1:] or sorted(TRACKED_TICKERS)
    run_backfill(targets)
