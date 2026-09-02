"""
Harvest daily market-wide foreign net purchase from the IDX digital-statistics
API (the JSON feed behind 'Total Trading by Investor's Type and Net Purchase
by Foreigners').

Endpoint (discovered via network intercept, 2026-07-07):
  https://www.idx.co.id/primary/DigitalStatistic/GetApiData
    ?urlName=LINK_DPS_TOTAL_NET_PURCHASE
    &query=<base64 {"year":"YYYY","month":"M","quarter":0,"type":"monthly"}>
    &isPrint=False&cumulative=false
Returns {"seriesData": [{"x": "YYYY-MM-DD", "y": <net purchase, billion IDR>}]}
per trading day of the requested month.

Loops months newest -> oldest, stops after 6 consecutive empty months
(= reached the start of their archive). Saves idx_foreign_daily.csv.
"""
import base64
import csv
import json
import sys
import time

from playwright.sync_api import sync_playwright

PAGE = ("https://www.idx.co.id/en/market-data/statistical-reports/digital-statistic/"
        "monthly/equity-trading-by-investor/"
        "total-trading-by-investor-s-type-and-net-purchase-by-foreigners")
API = "https://www.idx.co.id/primary/DigitalStatistic/GetApiData"
OUT = "idx_foreign_daily.csv"

START_YEAR = 2026
START_MONTH = 7          # walk backwards from here
MAX_EMPTY_STREAK = 6


def month_seq(year, month):
    while True:
        yield year, month
        month -= 1
        if month == 0:
            year, month = year - 1, 12


def main():
    rows = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/148.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        page.goto(PAGE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)   # let cookies/CF settle

        empty_streak = 0
        for year, month in month_seq(START_YEAR, START_MONTH):
            q = base64.b64encode(json.dumps(
                {"year": str(year), "month": str(month), "quarter": 0,
                 "type": "monthly"}).encode()).decode()
            url = f"{API}?urlName=LINK_DPS_TOTAL_NET_PURCHASE&query={q}&isPrint=False&cumulative=false"
            try:
                # fetch inside the page: same session/fingerprint that passed
                # Cloudflare on page load (ctx.request gets blocked)
                data = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {headers: {'Accept': 'application/json'}});
                        if (!r.ok) return {__status: r.status};
                        return await r.json();
                    }""", url)
                if data and data.get("__status"):
                    print(f"{year}-{month:02d}: HTTP {data['__status']}", flush=True)
                    data = None
            except Exception as e:
                print(f"{year}-{month:02d}: request error {e}", flush=True)
                data = None

            series = (data or {}).get("seriesData") or []
            n = 0
            for pt in series:
                d, y = pt.get("x"), pt.get("y")
                if d and y is not None:
                    rows[d] = float(y)
                    n += 1
            print(f"{year}-{month:02d}: {n} days", flush=True)

            if n == 0:
                empty_streak += 1
                if empty_streak >= MAX_EMPTY_STREAK:
                    print(f"{MAX_EMPTY_STREAK} empty months in a row — stopping.")
                    break
            else:
                empty_streak = 0
            time.sleep(0.4)
        browser.close()

    if not rows:
        print("No data harvested."); sys.exit(1)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "foreign_net_purchase_b"])
        for d in sorted(rows):
            w.writerow([d, rows[d]])
    print(f"\nSaved {len(rows)} daily rows {min(rows)}..{max(rows)} -> {OUT}")


if __name__ == "__main__":
    main()
