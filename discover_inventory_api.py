"""Discovery pass: log in, open the inventory page for one ticker, and record
every XHR the page makes, so we know whether the inventory chart is backed by a
JSON endpoint we can call directly instead of driving Plotly per ticker.

Also probes a handful of likely REST paths under /api for price history and a
full-universe ticker list. Writes everything to discovery.json.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import (API_BASE, INVENTORY_CHART_URL, login)  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

TICKER = sys.argv[1] if len(sys.argv) > 1 else "BREN"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discovery.json")

calls = []


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        def on_request(req):
            if req.resource_type in ("xhr", "fetch"):
                calls.append({"phase": phase[0], "method": req.method, "url": req.url,
                              "post": (req.post_data or "")[:1200]})
        page.on("request", on_request)

        phase = ["login"]
        login(page)

        phase[0] = "inventory_load"
        page.goto(INVENTORY_CHART_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)

        phase[0] = "inventory_select"
        page.click("#tick .Select-control")
        page.wait_for_timeout(400)
        page.keyboard.type(TICKER)
        page.wait_for_timeout(1500)
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)

        phase[0] = "inventory_fetch"
        page.click("#submit-button")
        page.wait_for_timeout(14000)

        # what does the rendered chart actually carry?
        shape = page.evaluate(
            """() => {
                const el = document.querySelector('.js-plotly-plot');
                if (!el || !el.data) return {ok:false};
                const t = el.data.find(d => Array.isArray(d.x) && d.x.length > 5) || el.data[0];
                return {
                    ok: true,
                    n_traces: el.data.length,
                    modes: [...new Set(el.data.map(d => d.mode || ''))],
                    names: el.data.map(d => d.name).slice(0, 40),
                    sample: t ? {
                        name: t.name, mode: t.mode, n: (t.x||[]).length,
                        x_head: (t.x||[]).slice(0,3), x_tail: (t.x||[]).slice(-2),
                        y_head: (t.y||[]).slice(0,3), y_tail: (t.y||[]).slice(-2),
                        cd_head: (t.customdata||[]).slice(0,2),
                        cd_tail: (t.customdata||[]).slice(-1)
                    } : null
                };
            }"""
        )

        # probe likely REST endpoints
        phase[0] = "probe"
        req = ctx.request
        probes = {}
        for path in ["/stock-universe", "/inventory", "/inventory/brokers",
                     "/broker-inventory", "/stocks", "/tickers",
                     "/market-summary/columns", "/price-history", "/ohlcv",
                     "/broker-summary", "/broker-stalker"]:
            try:
                r = req.get(f"{API_BASE}{path}", timeout=20000)
                body = r.text()[:400]
                probes[path] = {"status": r.status, "body": body}
            except Exception as e:
                probes[path] = {"status": "ERR", "body": str(e)[:200]}

        json.dump({"ticker": TICKER, "xhr": calls, "chart": shape, "probes": probes},
                  open(OUT, "w"), indent=1)
        print(f"wrote {OUT}")
        print(f"\nXHR calls captured: {len(calls)}")
        for c in calls:
            print(f"  [{c['phase']:17s}] {c['method']:5s} {c['url'][:130]}")
            if c["post"]:
                print(f"      body: {c['post'][:300]}")
        print("\nchart shape:", json.dumps(shape, indent=1)[:1800])
        print("\nprobes:")
        for k, v in probes.items():
            print(f"  {k:28s} {v['status']}  {str(v['body'])[:110]}")
        browser.close()


if __name__ == "__main__":
    main()
