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
(no args = all of TRACKED_TICKERS from neobdm_scraper, minus ones already
known to have no chart at all)
"""

import sys
import re
import sqlite3
import time

from playwright.sync_api import sync_playwright
from neobdm_scraper import login, NEOBDM_INVENTORY_URL, BROKER_FLOW_CODES, TRACKED_TICKERS, DB_PATH as _unused

DB_PATH = r"C:\Users\jason\Desktop\VsCode\Claude\neobdm.db"
KNOWN_NO_CHART = {"ALJI", "BTEL", "BUMI"}  # confirmed empty as of 2026-07-07; recheck if this list looks stale
BACKFILL_END = "2026-07-04"  # never overwrite live-scraped rows from 07-05 onward

EXTRACT_JS = """() => {
    const el = document.querySelector('.js-plotly-plot');
    function unwrap(v) {
        if (v && v._inputArray) return Object.values(v._inputArray);
        return v;
    }
    if (!el || !el.data) return null;
    const out = {brokers: [], price: null, volume: null};
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


def scrape_ticker(page, ticker, date_range_already_set):
    page.click("#tick .Select-control")
    page.wait_for_timeout(400)
    page.keyboard.type(ticker)
    page.wait_for_timeout(1500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)

    if not date_range_already_set:
        extend_date_range_to_earliest(page)

    page.click("#submit-button")
    page.wait_for_timeout(15000)
    return page.evaluate(EXTRACT_JS)


def insert_ticker_data(conn, ticker, data):
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

        for i, ticker in enumerate(tickers):
            print(f"=== {ticker} ===")
            try:
                data = scrape_ticker(page, ticker, date_range_already_set=(i > 0))
                if data is None or not data.get("brokers"):
                    print("  no chart data")
                    continue
                broker_n, price_n = insert_ticker_data(conn, ticker, data)
                conn.commit()
                rng = f"{data['price']['x'][0]} to {data['price']['x'][-1]}" if data["price"] else "n/a"
                print(f"  {broker_n} broker_flow rows, {price_n} price_history rows ({rng})")
            except Exception as e:
                print(f"  FAILED: {e}")
                failed.append(ticker)
            time.sleep(2)

        browser.close()

    conn.close()
    print(f"\nFailed tickers: {failed}")


if __name__ == "__main__":
    targets = sys.argv[1:] or sorted(set(TRACKED_TICKERS) - KNOWN_NO_CHART)
    run_backfill(targets)
