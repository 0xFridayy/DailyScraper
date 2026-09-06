"""Follow-up #2: the "Analisa Kepemilikan" (ownership) card on
/stock_detail/<TICKER>/ has its own in-card tabs (KDA 1% / PKDA 1% / KDA 5% /
PKDA 5%) distinct from the identically-labelled links in the LEFT SIDEBAR
(clicking those in discover_stock_detail_page.py fired no XHR — they likely
just anchor-scroll). This script scopes clicks to the card itself, extracts
full row text (names were hidden behind the sidebar overlay in the first
screenshot), and checks whether switching tabs triggers an AJAX call or is
baked into the initial page HTML.

Also extracts the Balance Position Chart panel's header metadata text
(scripless/free-float/holder-count summary numbers) and any "as of" / last-
updated markers near the ownership card, and the source IDX XLSX links.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import login  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TICKER = sys.argv[1] if len(sys.argv) > 1 else "BREN"
URL = f"https://neobdm.tech/stock_detail/{TICKER}/"
OUT = os.path.join(HERE, "discovery_stock_detail_ownership.json")


def main():
    calls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        login(page)

        def on_response(resp):
            req = resp.request
            if req.resource_type in ("xhr", "fetch"):
                try:
                    body = resp.text()
                except Exception:
                    body = None
                calls.append({"phase": phase[0], "method": req.method, "url": resp.url,
                              "post": (req.post_data or "")[:800], "status": resp.status,
                              "body_head": (body or "")[:8000]})
        page.on("response", on_response)

        phase = ["load"]
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        # collapse the left sidebar (its toggle button) so panel screenshots aren't
        # obscured, then find the ownership card by its heading text.
        try:
            page.click("[data-widget='pushmenu'], .sidebar-toggle", timeout=3000)
            page.wait_for_timeout(800)
        except Exception as e:
            print(f"sidebar collapse skipped: {e}")

        card_info = page.evaluate(
            """() => {
                const tables = [...document.querySelectorAll('table')].filter(
                    t => /Kepemilikan/i.test(t.innerText) && /Scripless/i.test(t.innerText));
                if (!tables.length) return {found:false, n_tables: document.querySelectorAll('table').length};
                const table = tables[0];
                const card = table.closest('.card') || table.parentElement.parentElement;
                const before = card.innerHTML.split(table.outerHTML)[0];
                const tabs = [...card.querySelectorAll("a,button,[role=tab],.nav-link,.nav-item,li,span")]
                    .map(e => (e.textContent||'').trim()).filter(t => t && t.length < 20 && t !== 'XLSX');
                return {
                    found: true,
                    card_class: card.className,
                    card_html_before_table: before.slice(-4000),
                    tabs: [...new Set(tabs)],
                    table_html: table.outerHTML.slice(0, 8000),
                    table_text: table.innerText.slice(0, 3000),
                };
            }"""
        )
        print("ownership card discovery:")
        print(json.dumps(card_info, indent=1)[:3000])

        # locate the Balance Position Chart panel header metadata (scripless /
        # free float / holder count) with exact text.
        bp_meta = page.evaluate(
            """() => {
                const hit = [...document.querySelectorAll('*')].find(
                    e => e.children.length === 0 && /Scripless:/i.test(e.textContent || ''));
                if (!hit) return null;
                let node = hit;
                for (let i=0;i<3 && node;i++) node = node.parentElement;
                return node ? node.textContent.replace(/\\s+/g,' ').trim().slice(0, 400) : hit.textContent;
            }"""
        )
        print("\nbalance position panel header text:")
        print(bp_meta)

        # IDX source links (XLSX / official filings) anywhere on the page
        idx_links = page.eval_on_selector_all(
            "a[href*='idx.co.id']",
            "els => els.map(e => ({text: e.textContent.trim(), href: e.href}))"
        )
        print(f"\nIDX source links found: {len(idx_links)}")
        for l in idx_links[:10]:
            print(f"  {l['text']!r} -> {l['href']}")

        # click each in-card tab, if any were found, scoped to the card
        bs_tabs = page.eval_on_selector_all(
            "[data-bs-toggle='tab'], [data-toggle='tab'], [data-bs-toggle='pill'], [data-toggle='pill']",
            """els => els.map(e => ({
                text: (e.textContent||'').trim(),
                target: e.getAttribute('data-bs-target') || e.getAttribute('href') || e.getAttribute('data-target'),
            }))"""
        )
        print(f"\nbootstrap data-bs-toggle=tab elements found: {len(bs_tabs)}")
        for t in bs_tabs:
            print(f"  {t}")

        results_per_tab = {}
        wanted_tabs = [t for t in bs_tabs
                       if t["text"].upper() in ("KDA 1%", "PKDA 1%", "KDA 5%", "PKDA 5%")]
        print(f"\nscoped ownership-card tabs to click: {wanted_tabs}")
        for tab in wanted_tabs:
            tab_text = tab["text"]
            phase[0] = f"tab:{tab_text}"
            try:
                clicked_ok = page.evaluate(
                    """(target) => {
                        const sels = ['tab', 'pill'].flatMap(k => [
                            `[data-bs-toggle='${k}'][data-bs-target='${target}']`,
                            `[data-bs-toggle='${k}'][href='${target}']`,
                            `[data-toggle='${k}'][href='${target}']`,
                        ]);
                        const el = document.querySelector(sels.join(','));
                        if (!el) return false;
                        el.scrollIntoView({block:'center'});
                        el.click();
                        return true;
                    }""",
                    tab["target"],
                )
                if not clicked_ok:
                    print(f"  tab {tab_text!r} not clickable via JS")
                    continue
                page.wait_for_timeout(2500)
                table_text = page.evaluate(
                    """() => {
                        const tables = [...document.querySelectorAll('table')].filter(
                            t => /Kepemilikan/i.test(t.innerText) && /Scripless/i.test(t.innerText)
                                 && t.offsetParent !== null);
                        return tables.length ? tables[0].innerText.slice(0, 3000) : null;
                    }"""
                )
                results_per_tab[tab_text] = table_text
                print(f"\n--- tab {tab_text!r} table text ---")
                print(table_text)
            except Exception as e:
                print(f"  click tab {tab_text!r} failed: {e}")

        out = {
            "ticker": TICKER, "card_info": card_info, "bp_meta": bp_meta,
            "idx_links": idx_links, "results_per_tab": results_per_tab,
            "xhr": calls,
        }
        json.dump(out, open(OUT, "w"), indent=1, default=str)
        print(f"\nwrote {OUT}")
        print(f"\nxhr/fetch calls captured during tab clicks: {len(calls)}")
        for c in calls:
            print(f"  [{c['phase']:20s}] {c['method']:5s} {c['status']} {c['url'][:100]}")
        browser.close()


if __name__ == "__main__":
    main()
