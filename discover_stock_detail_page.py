"""Follow-up discovery: https://neobdm.tech/stock_detail/<TICKER> answered
HTTP 200 in discover_stock_detail.py's page probes (the only 200 among 8
guesses). Open it for real, capture every XHR/fetch request+response, dump
visible tab/section labels, and screenshot each tab so KDA/PKDA/free float/
holder count/scripless/KSEI-participant sections can be identified visually.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import login  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TICKER = sys.argv[1] if len(sys.argv) > 1 else "BREN"
URL = f"https://neobdm.tech/stock_detail/{TICKER}"
OUT = os.path.join(HERE, "discovery_stock_detail_page.json")


def main():
    calls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1200})
        page = ctx.new_page()
        login(page)

        def on_response(resp):
            req = resp.request
            if req.resource_type in ("xhr", "fetch"):
                try:
                    body = resp.text()
                except Exception:
                    body = None
                calls.append({
                    "phase": phase[0], "method": req.method, "url": resp.url,
                    "post": (req.post_data or "")[:800],
                    "status": resp.status,
                    "body_head": (body or "")[:6000],
                })
        page.on("response", on_response)

        phase = ["load"]
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        page.screenshot(path=os.path.join(HERE, "scratch_stock_detail_0_load.png"), full_page=True)

        # dump every visible tab/button/nav-link label so we know what to click
        labels = page.eval_on_selector_all(
            "a, button, [role='tab'], .nav-link, .tab, li",
            """els => els.map(e => ({
                tag: e.tagName, text: (e.textContent || '').trim().slice(0,60),
                cls: e.className, id: e.id,
                href: e.getAttribute('href')
            })).filter(x => x.text)"""
        )
        # dedupe by text
        seen = {}
        for l in labels:
            seen.setdefault(l["text"], l)
        labels = list(seen.values())
        print(f"page loaded, {len(labels)} distinct labeled elements")
        for l in labels[:120]:
            print(f"  [{l['tag']:6s}] {l['text']:40s} cls={l['cls'][:40]!r} href={l['href']}")

        full_html_len = len(page.content())
        print(f"full HTML length: {full_html_len}")

        # click every plausible tab-like element whose text matches our targets
        targets = ["kda", "pkda", "float", "holder", "scripless", "ksei",
                   "kepemilikan", "balance", "posisi", "investor", "custod",
                   "partisipan", "overview", "ringkasan", "profil", "profile"]
        clicked = []
        for l in labels:
            t = l["text"].lower()
            if any(k in t for k in targets):
                clicked.append(l["text"])
        print(f"\ncandidate tab labels matching keywords: {clicked}")

        for name in clicked[:15]:
            phase[0] = f"click:{name}"
            try:
                page.get_by_text(name, exact=True).first.click(timeout=5000)
                page.wait_for_timeout(3000)
                safe = "".join(c if c.isalnum() else "_" for c in name)[:40]
                page.screenshot(
                    path=os.path.join(HERE, f"scratch_stock_detail_tab_{safe}.png"),
                    full_page=True,
                )
                print(f"  clicked {name!r} -> screenshot saved")
            except Exception as e:
                print(f"  click {name!r} failed: {e}")
            time.sleep(0.5)

        result = {
            "ticker": TICKER, "url": URL, "labels": labels,
            "full_html_len": full_html_len, "xhr": calls,
        }
        json.dump(result, open(OUT, "w"), indent=1, default=str)
        print(f"\nwrote {OUT}")
        print(f"total xhr/fetch calls captured: {len(calls)}")
        for c in calls:
            print(f"  [{c['phase']:30s}] {c['method']:5s} {c['status']} {c['url'][:110]}")
        browser.close()


if __name__ == "__main__":
    main()
