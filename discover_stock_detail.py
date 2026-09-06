"""Discovery pass for a per-ticker "Stock Detail" surface (KDA/PKDA, free float,
holder count, scripless %, foreign/local/retail/institution lots, KSEI
participant/custody). Nothing in this repo captures any of that today.

Unlike inventory-chart and balance_position_chart, no nav link with a matching
label was found in the /home/ sidebar dump (discovery_balance.json). The site's
own meta description advertises "Kepemilikan saham" (share ownership) as a
feature, so this is probed by:
  1. Re-opening balance_position_chart/ and actually reading the RESPONSE body
     this time (the earlier discovery pass only recorded request payloads).
  2. Looking for a per-ticker detail affordance inside the market-summary grid
     (row click / icon / context menu) rather than the sidebar.
  3. Direct GET probes of plausible REST paths under /api/.

Writes everything to discovery_stock_detail.json plus PNG screenshots so the
result can be read back visually (no live NeoBDM view otherwise).
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import API_BASE, login  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TICKER = sys.argv[1] if len(sys.argv) > 1 else "BREN"
OUT = os.path.join(HERE, "discovery_stock_detail.json")
NEWMARKET_URL = "https://neobdm.tech/new-market-summary/"
BALANCE_URL = "https://neobdm.tech/balance_position_chart/"

REST_PROBES = [
    "/stock-detail", "/stock-detail/", "/company-profile", "/company",
    "/profile", "/ksei", "/shareholder", "/shareholders", "/holder",
    "/holders", "/holder-composition", "/free-float", "/freefloat",
    "/ownership", "/ownership-composition", "/balance-position",
    "/balance_position", "/kda", "/pkda", "/scripless", "/investor-type",
]
PAGE_PROBES = [
    "/stock-detail/", "/stock_detail/", "/company/", "/profile/",
    "/saham/", "/detail/", "/kepemilikan/", "/ownership/",
]


def dump(obj):
    print(json.dumps(obj, indent=1, default=str)[:2000])


def main():
    result = {"ticker": TICKER}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        login(page)
        req = ctx.request

        # ---- 1. balance_position_chart/: capture the RESPONSE this time ----
        print("=" * 70, "\n1. balance_position_chart/ — response bodies")
        bp_responses = []

        def on_response(resp):
            if "_dash-update-component" in resp.url or "/api/" in resp.url:
                try:
                    body = resp.text()
                except Exception:
                    body = None
                bp_responses.append({
                    "url": resp.url, "status": resp.status,
                    "body_head": (body or "")[:4000],
                })
        page.on("response", on_response)

        page.goto(BALANCE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        try:
            page.click("#tick .Select-control", timeout=5000)
            page.wait_for_timeout(400)
            page.keyboard.type(TICKER)
            page.wait_for_timeout(1500)
            page.keyboard.press("Enter")
            page.wait_for_timeout(6000)
        except Exception as e:
            print(f"  ticker-select failed: {e}")

        # what does the rendered chart actually carry (mirrors inventory-chart probe)
        shape = page.evaluate(
            """() => {
                const el = document.querySelector('.js-plotly-plot');
                if (!el || !el.data) return {ok:false};
                const arr = v => Array.isArray(v) ? v : (v == null ? [] : [v]);
                return {
                    ok: true,
                    n_traces: el.data.length,
                    names: el.data.map(d => d.name),
                    types: [...new Set(el.data.map(d => d.type || ''))],
                    modes: [...new Set(el.data.map(d => d.mode || ''))],
                    traces: el.data.slice(0, 40).map(d => {
                        const x = arr(d.x), y = arr(d.y);
                        return {
                            name: d.name, type: d.type, mode: d.mode,
                            n: x.length,
                            x_head: x.slice(0, 3), x_tail: x.slice(-2),
                            y_head: y.slice(0, 3), y_tail: y.slice(-2),
                        };
                    }),
                };
            }"""
        )
        # also dump the dash-layout for bp_app: reveals every input control
        # (date range? investor-type toggle? view mode?) the same way ia_app's
        # date-picker/broker fields were found for inventory-chart.
        try:
            lay = json.loads(req.get(
                "https://neobdm.tech/django_plotly_dash/app/bp_app/_dash-layout",
                timeout=40000).text())
        except Exception as e:
            lay = {"error": str(e)}

        def walk_controls(n, out):
            if isinstance(n, dict):
                pr = n.get("props", {})
                cid = pr.get("id")
                if cid and cid not in ("balance-position-chart",):
                    out.append({
                        "id": cid,
                        "type": n.get("type"),
                        "options": [o.get("value") for o in (pr.get("options") or [])][:30],
                        "value": pr.get("value"),
                        "min_date_allowed": pr.get("min_date_allowed"),
                        "max_date_allowed": pr.get("max_date_allowed"),
                    })
                for v in n.values():
                    walk_controls(v, out)
            elif isinstance(n, list):
                for v in n:
                    walk_controls(v, out)
        controls = []
        walk_controls(lay, controls)

        page.screenshot(path=os.path.join(HERE, "scratch_balance_position.png"), full_page=True)
        result["balance_position"] = {
            "responses": bp_responses,
            "chart_shape": shape,
            "dash_controls": controls,
        }
        print(f"  captured {len(bp_responses)} responses, chart ok={shape.get('ok')}, "
              f"{len(controls)} dash controls")
        dump(controls)
        page.remove_listener("response", on_response)

        # ---- 2. market-summary grid: look for a per-row detail affordance ----
        print("=" * 70, "\n2. new-market-summary/ — row structure + click probe")
        nav_calls = []

        def on_req(r):
            if r.resource_type in ("xhr", "fetch", "document"):
                nav_calls.append({"method": r.method, "url": r.url,
                                   "post": (r.post_data or "")[:500]})
        page.on("request", on_req)

        page.goto(NEWMARKET_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        page.screenshot(path=os.path.join(HERE, "scratch_market_summary.png"), full_page=True)

        row_html = page.evaluate(
            """() => {
                const row = document.querySelector('.tabulator-row, tr, [role="row"]');
                return row ? row.outerHTML.slice(0, 3000) : null;
            }"""
        )
        print("  first data row outerHTML (truncated):")
        print(f"  {row_html}")

        before_url = page.url
        clicked = False
        for sel in [".tabulator-cell", "td", "[role='gridcell']"]:
            try:
                if page.locator(sel).count() > 0:
                    page.locator(sel).first.click(timeout=5000)
                    clicked = True
                    print(f"  clicked selector {sel!r}")
                    break
            except Exception as e:
                print(f"  click {sel!r} failed: {e}")
        page.wait_for_timeout(4000)
        after_url = page.url
        modal_html = page.evaluate(
            """() => {
                const m = document.querySelector('.modal, [role="dialog"], .drawer, .MuiDrawer-root');
                return m ? m.outerHTML.slice(0, 4000) : null;
            }"""
        )
        page.screenshot(path=os.path.join(HERE, "scratch_after_click.png"), full_page=True)
        result["market_summary"] = {
            "row_html": row_html,
            "clicked": clicked,
            "url_before": before_url,
            "url_after": after_url,
            "modal_html": modal_html,
            "requests_after_click": nav_calls[-30:],
        }
        print(f"  url before={before_url} after={after_url}")
        print(f"  modal found: {modal_html is not None}")
        page.remove_listener("request", on_req)

        # ---- 3. direct REST probes -------------------------------------------
        print("=" * 70, "\n3. direct REST probes under /api/")
        rest_results = {}
        for path in REST_PROBES:
            for qs in (f"?symbol={TICKER}", ""):
                url = f"{API_BASE}{path}{qs}"
                try:
                    r = req.get(url, timeout=15000)
                    body = r.text()[:300]
                    rest_results[url] = {"status": r.status, "body": body}
                    tag = "FOUND?" if r.status == 200 and '"success": true' in body else ""
                    print(f"  {r.status:4d} {url:70s} {tag}")
                except Exception as e:
                    rest_results[url] = {"status": "ERR", "body": str(e)[:150]}
                time.sleep(0.4)

        print("=" * 70, "\n4. direct PAGE probes (server-rendered detail pages)")
        page_results = {}
        for path in PAGE_PROBES:
            url = f"https://neobdm.tech{path}{TICKER}"
            try:
                r = req.get(url, timeout=15000)
                page_results[url] = {"status": r.status, "len": len(r.text())}
                print(f"  {r.status:4d} {url}")
            except Exception as e:
                page_results[url] = {"status": "ERR", "body": str(e)[:150]}
            time.sleep(0.4)

        result["rest_probes"] = rest_results
        result["page_probes"] = page_results
        json.dump(result, open(OUT, "w"), indent=1, default=str)
        print(f"\nwrote {OUT}")
        browser.close()


if __name__ == "__main__":
    main()
