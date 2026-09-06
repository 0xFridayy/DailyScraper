"""Second discovery pass, targeted at closing the n=1/n=2 gaps flagged in
STOCK_DETAIL_OWNERSHIP_DISCOVERY.md after the first pass (BREN, PANI).

Ticker selection (structural diversity, not random):
  TPIA  - same conglomerate as BREN (Prajogo) -> internal consistency check
  CDIA  - 2025 IPO (Prajogo group) -> Balance Position history-floor test on
          a young listing, and an IPO is itself an ownership-change event
  GOTO  - widely-held tech co, multiple foreign VC/PE holders, known lockup
          sell-downs -> fragmented structure + near-5% foreign holders +
          genuine ownership-change event
  UNVR  - dominant single FOREIGN parent (~84%) -> custodian-denominator
          stress test at the opposite extreme from BREN's 22.7% holder
  BBCA  - large, concentrated, DOMESTIC control -> third data point for the
          "KDA5%/PKDA5% is foreign-only" hypothesis (currently n=2)
  WIFI  - Hashim group, smaller/less liquid -> scale diversity + fresh
          near-threshold check
  ENRG  - Bakrie group, history of debt-to-equity conversions bringing in
          foreign creditor-holders -> another near-threshold + event case

For each ticker, captures (NOT modeled, NOT normalized, NOT aliased):
  - KDA 1% / PKDA 1% / KDA 5% / PKDA 5% pane text, verbatim
  - free-float / scripless / holder-count badge text, verbatim
  - Balance Position payload via the bp_app Dash callback (date range, trace
    names, point count) -- the one clean structured source
  - the IDX XLSX link if the page exposes one, plus (separately, one GET per
    link found, no bulk downloading) that file's HTTP Last-Modified header
    and its own internal docProps/core.xml timestamp if present -- checking
    whether "published_at" can ever be independently verified

Conservative pacing: one page load per ticker, explicit jittered sleep
between tickers, no retries-in-a-loop. Not trying to finish fast.
"""
import io
import json
import os
import random
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import login  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "discovery_batch2.json")
BALANCE_DASH_URL = "https://neobdm.tech/django_plotly_dash/app/bp_app/_dash-update-component"

TICKERS = ["TPIA", "CDIA", "GOTO", "UNVR", "BBCA", "WIFI", "ENRG"]
PANE_IDS = ["insider-current", "insider-moves", "insider5p-current", "insider5p-moves"]
PACE_BASE = 9.0   # seconds between tickers, before jitter
PACE_JITTER = 4.0


def fetch_balance_position(req, csrf, ticker):
    body = {
        "output": "balance-position-chart.figure",
        "outputs": {"id": "balance-position-chart", "property": "figure"},
        "inputs": [
            {"id": "tick", "property": "value", "value": ticker},
            {"id": "darkmode", "property": "value", "value": True},
        ],
        "changedPropIds": ["tick.value"],
    }
    headers = {"Content-Type": "application/json", "X-CSRFToken": csrf,
               "Referer": "https://neobdm.tech/balance_position_chart/"}
    r = req.post(BALANCE_DASH_URL, data=json.dumps(body), headers=headers, timeout=60000)
    try:
        j = json.loads(r.text())
    except Exception as e:
        return {"error": f"unparseable: {e}", "status": r.status}
    fig = (((j.get("response") or {}).get("balance-position-chart") or {}).get("figure") or {})
    data = fig.get("data") or []
    names = [d.get("name") for d in data]
    x_all = []
    for d in data:
        x = d.get("x")
        if isinstance(x, list) and x:
            x_all = x
            break
    return {
        "status": r.status, "n_traces": len(data), "names": names,
        "n_points": len(x_all), "x_min": (x_all[0] if x_all else None),
        "x_max": (x_all[-1] if x_all else None),
    }


def inspect_xlsx(req, url):
    """One GET on this specific, already-discovered link. No enumeration,
    no bulk download. Checks (a) HTTP Last-Modified and (b) the OOXML
    package's own docProps/core.xml creation/modification timestamps --
    the only two independently-verifiable-publication-date candidates."""
    try:
        r = req.get(url, timeout=30000)
        headers = {k: v for k, v in (r.headers or {}).items()
                   if k.lower() in ("last-modified", "date", "content-type", "content-length")}
        result = {"status": r.status, "headers": headers}
        if r.status == 200:
            body = r.body()
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as z:
                    core = z.read("docProps/core.xml")
                    ns = {
                        "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
                        "dcterms": "http://purl.org/dc/terms/",
                        "dc": "http://purl.org/dc/elements/1.1/",
                    }
                    root = ET.fromstring(core)
                    created = root.findtext("dcterms:created", namespaces=ns)
                    modified = root.findtext("dcterms:modified", namespaces=ns)
                    creator = root.findtext("dc:creator", namespaces=ns)
                    result["ooxml_core_props"] = {
                        "created": created, "modified": modified, "creator": creator,
                    }
            except Exception as e:
                result["ooxml_core_props_error"] = str(e)
        return result
    except Exception as e:
        return {"error": str(e)[:200]}


def main():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        login(page)
        req = ctx.request
        csrf = next((c["value"] for c in ctx.cookies() if c["name"] == "csrftoken"), "")

        for i, ticker in enumerate(TICKERS):
            print(f"\n{'='*20} [{i+1}/{len(TICKERS)}] {ticker} {'='*20}", flush=True)
            entry = {"ticker": ticker}
            try:
                page.goto(f"https://neobdm.tech/stock_detail/{ticker}/",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)

                panes = {}
                for pane_id in PANE_IDS:
                    panes[pane_id] = page.evaluate(
                        "id => { const el = document.getElementById(id); "
                        "return el ? el.innerText : null; }",
                        pane_id,
                    )
                entry["panes"] = panes

                bp_badge = page.evaluate(
                    """() => {
                        const hit = [...document.querySelectorAll('*')].find(
                            e => e.children.length === 0 && /Scripless:/i.test(e.textContent || ''));
                        if (!hit) return null;
                        let node = hit;
                        for (let i=0;i<3 && node;i++) node = node.parentElement;
                        return node ? node.textContent.replace(/\\s+/g,' ').trim().slice(0, 300) : hit.textContent;
                    }"""
                )
                entry["balance_position_badge"] = bp_badge

                idx_links = page.eval_on_selector_all(
                    "a[href*='idx.co.id']",
                    "els => els.map(e => ({text: e.textContent.trim(), href: e.href}))"
                )
                entry["idx_links"] = idx_links

                print(f"  panes captured: { {k: (len(v) if v else 0) for k, v in panes.items()} }")
                print(f"  badge: {bp_badge}")
                print(f"  idx links: {idx_links}")

                # Balance Position structured payload (separate call, same session)
                entry["balance_position"] = fetch_balance_position(req, csrf, ticker)
                print(f"  balance_position: {entry['balance_position']}")

                # XLSX inspection -- only links actually found on THIS ticker's page
                xlsx_checks = []
                for link in idx_links:
                    if link["href"].lower().endswith((".xlsx", ".xls")):
                        print(f"  inspecting XLSX: {link['href']}")
                        xlsx_checks.append({"url": link["href"], **inspect_xlsx(req, link["href"])})
                        time.sleep(1.0)
                entry["xlsx_checks"] = xlsx_checks

            except Exception as e:
                entry["error"] = str(e)[:300]
                print(f"  ERROR: {e}")

            results[ticker] = entry
            json.dump(results, open(OUT, "w"), indent=1, ensure_ascii=False, default=str)

            if i < len(TICKERS) - 1:
                delay = PACE_BASE + random.random() * PACE_JITTER
                print(f"  pacing: sleeping {delay:.1f}s before next ticker", flush=True)
                time.sleep(delay)

        browser.close()
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
