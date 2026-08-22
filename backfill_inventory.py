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

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from neobdm_scraper import login, NEOBDM_INVENTORY_URL, BROKER_FLOW_CODES, TRACKED_TICKERS, DB_PATH
from price_audit import series_signature, ticker_from_title, should_fail_run

BACKFILL_END = "2026-07-04"  # never overwrite live-scraped rows from 07-05 onward

# Above this share of failed tickers the run exits non-zero. A few failures are
# normal (suspended name, no chart); a wholesale failure must not look like
# success - see price_audit.should_fail_run for why that mattered.
MAX_FAILURE_RATE = 0.30
FAILURE_SNAPSHOT = "topup-failure"   # .png / .html, uploaded as a CI artifact

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
CHART_STABLE_TRIES = 20
CHART_STABLE_PAUSE = 500

# Run #15 (2026-08-22) showed the chart lagging exactly one request behind: the
# N-th ticker rendered the (N-1)-th chart, for 40 of 45 tickers. BBHI, first in
# the list and therefore with no predecessor, was the one early success. Cause is
# almost certainly Dash state propagation - #submit-button fires a callback that
# reads the dropdown value as State, and the click lands before the new selection
# has reached Dash's store, so the callback renders the previous value.
#
# Waiting for the chart to merely CHANGE cannot catch that: it does change, just
# to the wrong ticker. So the wait below checks chart IDENTITY, and re-clicks
# submit when the chart comes back wrong.
SUBMIT_SETTLE_PAUSE = 700     # throttle before submitting; see scrape_ticker
CHART_ATTEMPT_TIMEOUT = 20_000
MAX_SUBMIT_ATTEMPTS = 3       # 3 x 20s keeps the old 60s per-ticker budget

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

# Run #15 proved the chart title reliably carries the 4-letter code: every one
# of the 40 mismatches reported a clean code (CUAN, DEWA, DOOH, ELTY, ...).
# ticker_from_title() was written as a merely conditional guard because the title
# format could not be verified without site access. It now can be, which is what
# lets identity become the wait condition rather than a post-hoc check.
# RAW string on purpose. In a normal Python string "\\b" is a BACKSPACE
# character, which silently turns the word-boundary regex below into garbage —
# __titleCode() then returns null for every chart and every wait quietly falls
# back to the weak changed-fingerprint condition, undoing this whole fix. The
# other JS constants here survive only because "\\s" happens not to be a
# recognised Python escape; "\\b" is.
_TITLE_CODE_FN = r"""
function __titleCode() {
    const el = document.querySelector('.js-plotly-plot');
    if (!el) return null;
    const lay = el.layout || {};
    const t = (lay.title && (lay.title.text !== undefined ? lay.title.text : lay.title)) || '';
    const m = String(t).match(/\b([A-Z]{4})\b/);
    return m ? m[1] : null;
}
"""

READ_FINGERPRINT_JS = "() => { %s return __fp(); }" % _FINGERPRINT_FN
READ_TITLE_CODE_JS = "() => { %s return __titleCode(); }" % _TITLE_CODE_FN
CHART_CHANGED_JS = ("(prev) => { %s const fp = __fp(); "
                    "return fp !== null && fp !== prev; }" % _FINGERPRINT_FN)

# "Is the chart showing the ticker we asked for?" Falls back to the old
# changed-fingerprint condition when the title carries no extractable code, so a
# change in title format degrades to the previous behaviour instead of hanging.
CHART_IS_JS = ("([want, prev]) => { %s %s "
               "const code = __titleCode();"
               "if (code) return code === want.toUpperCase();"
               "const fp = __fp(); return fp !== null && fp !== prev; }"
               % (_FINGERPRINT_FN, _TITLE_CODE_FN))

# The option list and the selected-value label are the only two selectors here
# that were NOT already proven in production, so both are hedged with
# alternatives. react-select v1 markup comes first, since the sibling selector
# "#tick .Select-control" is proven and belongs to that same markup family; then
# the ARIA role, then the virtualized-list class Dash uses for long lists.
#
# All candidates are tested inside ONE predicate rather than probed in sequence.
# Sequential probing would multiply the timeout: 4 candidates x SELECT_TIMEOUT x
# 45 tickers is far past the workflow's 35-minute budget, so a wrong first guess
# would become a timeout rather than a fallback.
OPTION_SELECTORS = ["#tick .Select-option", "#tick [role='option']",
                    "#tick .VirtualizedSelectOption", "#tick .Select-menu-outer div"]
VALUE_SELECTORS = ["#tick .Select-value-label", "#tick .Select-value",
                   "#tick [role='combobox']"]

# The label may be just the code or "CODE - Company Name", so match the leading
# code either way. __pick returns the first selector that matches anything.
_OPTION_CODE_JS = """
function __code(el) {
    return el.textContent.replace(/\s+/g, ' ').trim().toUpperCase().split(/[^A-Z0-9]+/)[0];
}
function __pick(sels) {
    for (const s of sels) {
        const els = Array.from(document.querySelectorAll(s));
        if (els.length) return {selector: s, els: els};
    }
    return {selector: null, els: []};
}
"""

OPTION_READY_JS = ("([want, sels]) => { %s "
                   "return __pick(sels).els.some(o => __code(o) === want.toUpperCase()); }"
                   % _OPTION_CODE_JS)

FIND_OPTION_JS = ("([want, sels]) => { %s "
                  "const hit = __pick(sels);"
                  "return {selector: hit.selector,"
                  " index: hit.els.findIndex(o => __code(o) === want.toUpperCase()),"
                  " texts: hit.els.slice(0, 8).map(o => o.textContent.replace(/\s+/g,' ').trim())}; }"
                  % _OPTION_CODE_JS)

VALUE_SETTLED_JS = ("([want, sels]) => { %s "
                    "const hit = __pick(sels);"
                    "return hit.els.some(el => __code(el) === want.toUpperCase()); }"
                    % _OPTION_CODE_JS)


# A stray Python escape inside any JS blob above becomes a control character and
# silently breaks the regex it sits in — __titleCode() then returns null for every
# chart and every identity wait quietly degrades to the weak fallback, undoing the
# fix without erroring. That happened once during development. Fail at import
# instead of discovering it 45 tickers into a scrape.
for _name, _js in (("_FINGERPRINT_FN", _FINGERPRINT_FN),
                   ("_TITLE_CODE_FN", _TITLE_CODE_FN),
                   ("_OPTION_CODE_JS", _OPTION_CODE_JS)):
    _bad = [c for c in _js if ord(c) < 32 and c not in "\n\r\t"]
    if _bad:
        raise RuntimeError(
            f"{_name} contains control character {_bad[0]!r} — a Python escape "
            f"leaked into the JS. Make it a raw string (r\"\"\"...\"\"\").")


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

    page.wait_for_function(OPTION_READY_JS, arg=[ticker, OPTION_SELECTORS],
                           timeout=SELECT_TIMEOUT)

    found = page.evaluate(FIND_OPTION_JS, [ticker, OPTION_SELECTORS])
    if found["index"] < 0:
        raise TickerMismatch(
            f"{ticker}: no dropdown option matched. tried={OPTION_SELECTORS} "
            f"matched_selector={found['selector']!r} saw={found['texts']}")

    # Click through Playwright rather than el.click(): react-select v1 acts on
    # mousedown, which a synthetic click() alone would not deliver. Click via the
    # selector that actually matched, not a hardcoded one.
    page.locator(found["selector"]).nth(found["index"]).click()
    page.wait_for_function(VALUE_SETTLED_JS, arg=[ticker, VALUE_SELECTORS],
                           timeout=SELECT_TIMEOUT)


def scrape_ticker(page, ticker, date_range_already_set, prev_fingerprint=None):
    """Returns (payload, fingerprint). Caller passes the PREVIOUS ticker's
    fingerprint so we can prove the chart actually turned over."""
    select_ticker(page, ticker)

    if not date_range_already_set:
        extend_date_range_to_earliest(page)

    # A throttle, NOT a correctness assumption - nothing is read on the strength
    # of it. It only makes the retry below rare, since a retry on every one of 45
    # tickers would not fit the workflow budget. Correctness comes from the
    # identity wait, which is what the fixed timeouts of the original code lacked.
    page.wait_for_timeout(SUBMIT_SETTLE_PAUSE)

    # 1. Submit, then wait for the chart to show THIS ticker - not merely a
    #    different one. If the callback ran against a stale selection the chart
    #    renders the previous ticker and then sits there, so waiting alone would
    #    time out; re-clicking submit is what actually resolves it.
    seen = None
    for _ in range(MAX_SUBMIT_ATTEMPTS):
        page.click("#submit-button")
        try:
            page.wait_for_function(CHART_IS_JS, arg=[ticker, prev_fingerprint],
                                   timeout=CHART_ATTEMPT_TIMEOUT)
            break
        except PlaywrightTimeout:
            seen = page.evaluate(READ_TITLE_CODE_JS)
    else:
        raise TickerMismatch(
            f"{ticker}: chart still showing {seen!r} after {MAX_SUBMIT_ATTEMPTS} "
            f"submits — the selection is not reaching the callback")

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
                if not failed:
                    # First failure only: 45 dumps would be noise, and one is
                    # enough to identify the markup. Wrapped so a failing
                    # screenshot never masks the error we actually care about.
                    try:
                        page.screenshot(path=f"{FAILURE_SNAPSHOT}.png", full_page=True)
                        with open(f"{FAILURE_SNAPSHOT}.html", "w", encoding="utf-8") as fh:
                            fh.write(page.content())
                        print(f"  saved {FAILURE_SNAPSHOT}.png / .html for diagnosis")
                    except Exception as snap_err:
                        print(f"  (could not save failure snapshot: {snap_err})")
                failed.append(ticker)
                # Re-read rather than carrying a fingerprint that may describe a
                # chart we rejected, which would make the NEXT wait trivially true.
                prev_fingerprint = page.evaluate(READ_FINGERPRINT_JS)
            time.sleep(2)

        browser.close()

    conn.close()
    print(f"\nFailed tickers: {failed}")

    if should_fail_run(len(failed), len(tickers), MAX_FAILURE_RATE):
        rate = len(failed) / len(tickers) if tickers else 1.0
        sys.exit(
            f"ABORT: {len(failed)}/{len(tickers)} tickers failed ({rate:.0%}, "
            f"limit {MAX_FAILURE_RATE:.0%}). Not a partial outage - treat this as "
            f"the scrape being broken, and check {FAILURE_SNAPSHOT}.png/.html."
        )


if __name__ == "__main__":
    targets = sys.argv[1:] or sorted(TRACKED_TICKERS)
    run_backfill(targets)
