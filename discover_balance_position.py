"""Discovery pass for KSEI balance position (ownership by holder category).

balance_leadlag.py (in "Inventory + Broker Summary ML") needs a per-ticker,
per-month breakdown of shares held by local_individual, local_korporat,
foreign_korporat, etc. Nothing in this repo captures that today — market
summary / broker stalker / inventory are all broker transaction flow, not
KSEI ownership composition.

Step 1: log in, dump the sidebar nav so we know the real URL/label NeoBDM
uses for this feature (guessing the slug blind wastes requests and risks
tripping the ~50-request throttle).
Step 2: if a candidate page is found, open it for one ticker and record
every XHR, same pattern as discover_inventory_api.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import API_BASE, login  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

TICKER = sys.argv[1] if len(sys.argv) > 1 else "BREN"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discovery_balance.json")

KEYWORDS = ["balance", "posisi", "position", "kepemilikan", "ownership",
            "investor", "holder", "ksei", "komposisi", "shareholder"]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        login(page)

        page.goto("https://neobdm.tech/home/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: e.textContent.trim(), href: e.href}))",
        )
        candidates = [
            l for l in links
            if any(k in l["text"].lower() or k in l["href"].lower() for k in KEYWORDS)
        ]

        print(f"total nav links: {len(links)}")
        print(f"candidate links matching {KEYWORDS}:")
        for c in candidates:
            print(f"  {c['text']!r:40s} -> {c['href']}")

        result = {"ticker": TICKER, "all_links": links, "candidates": candidates}

        if candidates:
            target = candidates[0]["href"]
            calls = []

            def on_request(req):
                if req.resource_type in ("xhr", "fetch"):
                    calls.append({"method": req.method, "url": req.url,
                                  "post": (req.post_data or "")[:1200]})
            page.on("request", on_request)

            print(f"\nopening candidate page: {target}")
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)

            try:
                page.click("#tick .Select-control", timeout=5000)
                page.wait_for_timeout(400)
                page.keyboard.type(TICKER)
                page.wait_for_timeout(1500)
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)
                page.click("#submit-button", timeout=5000)
                page.wait_for_timeout(10000)
            except Exception as e:
                print(f"  (no standard ticker-select form found: {e})")

            result["xhr"] = calls
            print(f"\nXHR calls captured: {len(calls)}")
            for c in calls:
                print(f"  {c['method']:5s} {c['url'][:140]}")
                if c["post"]:
                    print(f"      body: {c['post'][:300]}")
        else:
            print("\nno nav link matched — dumping full nav text for manual inspection")
            for l in links[:80]:
                print(f"  {l['text']!r:40s} -> {l['href']}")

        json.dump(result, open(OUT, "w"), indent=1)
        print(f"\nwrote {OUT}")
        browser.close()


if __name__ == "__main__":
    main()
