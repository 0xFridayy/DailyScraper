"""Production capture command for the ownership/custody dataset (Phase 2A).

One page load per ticker (`stock_detail/<TICKER>/`) plus one Dash-callback
POST for the Balance Position Chart's full trace data, extracted via the
same DOM-id / structured-payload techniques already validated in
discover_stock_detail_batch2.py, then ingested through ownership_ingest.py
into a SQLite DB with the reviewed schema (ownership_schema.py).

Idempotent + resumable:
  - Ingestion itself is idempotent (INSERT OR IGNORE on each table's
    natural key -- see ownership_ingest.py), so re-running this script
    against tickers already captured today is always safe.
  - Additionally, each ticker's raw extraction is cached to
    ownership_raw/<RUN_ID>/<TICKER>.json (RUN_ID defaults to today's
    date). If interrupted, re-running the same day skips tickers whose
    raw file already exists and just re-ingests from the cached file --
    no repeat page loads, no repeat load on NeoBDM. A new day gets a new
    RUN_ID and captures fresh point-in-time facts.
  - Raw caches hold only the already-extracted text/JSON fragments (pane
    innerText, badge text, chart trace arrays) -- never the full
    authenticated HTML page and never credentials.

Conservative pacing, modeled directly on harvest_inventory.py's
PACE/JITTER/REST_EVERY/REST_FOR pattern (that pattern is the one already
verified not to trip NeoBDM's rate limiting).

Usage:
    python ownership_capture.py --dry-run --tickers BREN,PANI,TPIA
    python ownership_capture.py                      # full TRACKED_TICKERS universe
    python ownership_capture.py --limit 5             # smoke test
    python ownership_capture.py --run-id 2026-08-30   # force/resume a specific run
"""
import argparse
import io
import json
import os
import random
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import login, TRACKED_TICKERS, log  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import ownership_ingest as ing  # noqa: E402
from ownership_schema import connect  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "ownership_raw")
DB_PATH = os.path.join(HERE, "neobdm_ownership.db")
BALANCE_DASH_URL = "https://neobdm.tech/django_plotly_dash/app/bp_app/_dash-update-component"
STOCK_DETAIL_URL = "https://neobdm.tech/stock_detail/{ticker}/"
PANE_IDS = ["insider-current", "insider-moves", "insider5p-current", "insider5p-moves"]

# Conservative pacing -- same shape as harvest_inventory.py's PACE/JITTER/
# REST_EVERY/REST_FOR, but wider margins: this page load is ~10x heavier
# (1.1 MB HTML) than the inventory API call that pattern was tuned for.
PACE = 8.0
JITTER = 4.0
REST_EVERY = 25
REST_FOR = 30
MAX_RETRY = 3


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def raw_path(run_id, ticker):
    d = os.path.join(RAW_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{ticker}.json")


def fetch_balance_position_full(req, csrf, ticker):
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
    j = json.loads(r.text())
    fig = (((j.get("response") or {}).get("balance-position-chart") or {}).get("figure") or {})
    data = fig.get("data") or []
    return [{"name": d.get("name"), "x": d.get("x") or [], "y": d.get("y") or []} for d in data]


def inspect_xlsx(req, url):
    """One GET on an already-discovered link. Returns published_at ONLY if
    independently verifiable (HTTP 200 + OOXML docProps/core.xml 'created').
    Per STOCK_DETAIL_OWNERSHIP_DISCOVERY.md 5.2 this currently always comes
    back None (link is a broken/shared placeholder, 403 on fetch) -- kept
    here so a future fix on NeoBDM/IDX's side is picked up automatically."""
    try:
        r = req.get(url, timeout=30000)
        if r.status != 200:
            return None
        body = r.body()
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            core = z.read("docProps/core.xml")
            ns = {"dcterms": "http://purl.org/dc/terms/"}
            created = ET.fromstring(core).findtext("dcterms:created", namespaces=ns)
            return created
    except Exception:
        return None


def capture_ticker(page, req, csrf, ticker):
    """One page load + one Dash POST. Returns the raw capture dict (no
    HTML, no credentials) ready for json.dump and for ingest_capture()."""
    page.goto(STOCK_DETAIL_URL.format(ticker=ticker), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)

    panes = {}
    for pane_id in PANE_IDS:
        panes[pane_id] = page.evaluate(
            "id => { const el = document.getElementById(id); return el ? el.innerText : null; }",
            pane_id,
        )

    badge = page.evaluate(
        """() => {
            const hit = [...document.querySelectorAll('*')].find(
                e => e.children.length === 0 && /Scripless:/i.test(e.textContent || ''));
            if (!hit) return null;
            let node = hit;
            for (let i=0;i<3 && node;i++) node = node.parentElement;
            return node ? node.textContent.replace(/\\s+/g,' ').trim().slice(0, 400) : hit.textContent;
        }"""
    )

    idx_links = page.eval_on_selector_all(
        "a[href*='idx.co.id']",
        "els => els.map(e => e.href)"
    )
    xlsx_url = next((h for h in idx_links if h.lower().endswith((".xlsx", ".xls"))), None)

    traces = fetch_balance_position_full(req, csrf, ticker)

    return {
        "ticker": ticker,
        "panes": panes,
        "balance_position_badge": badge,
        "balance_position_traces": traces,
        "source_xlsx_url": xlsx_url,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="", help="comma-separated; default is TRACKED_TICKERS")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--run-id", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true",
                     help="capture + ingest, but roll back the DB transaction at the end")
    a = ap.parse_args()

    syms = ([s.strip().upper() for s in a.tickers.split(",") if s.strip()]
            if a.tickers else list(TRACKED_TICKERS))
    if a.limit:
        syms = syms[:a.limit]

    conn = connect(a.db)
    conn.execute("BEGIN")
    log.info(f"ownership_capture: run_id={a.run_id} db={a.db} n_tickers={len(syms)} "
             f"dry_run={a.dry_run}")

    ok = fail = 0
    totals = {}
    t_start = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        login(page)
        req = ctx.request
        csrf = next((c["value"] for c in ctx.cookies() if c["name"] == "csrftoken"), "")

        for i, ticker in enumerate(syms, 1):
            cache_file = raw_path(a.run_id, ticker)
            capture = None
            if os.path.exists(cache_file):
                log.info(f"[{i}/{len(syms)}] {ticker}: resume -- using cached raw capture")
                cached = json.load(open(cache_file, encoding="utf-8"))
                capture, captured_at = cached["capture"], cached["captured_at"]
            else:
                delay = 2.0
                for attempt in range(MAX_RETRY):
                    try:
                        captured_at = now_iso()
                        capture = capture_ticker(page, req, csrf, ticker)
                        json.dump({"captured_at": captured_at, "capture": capture},
                                  open(cache_file, "w", encoding="utf-8"),
                                  indent=1, ensure_ascii=False, default=str)
                        break
                    except Exception as e:
                        msg = str(e)[:150]
                        if attempt == MAX_RETRY - 1:
                            log.warning(f"[{i}/{len(syms)}] {ticker} GIVE UP: {msg}")
                            fail += 1
                            capture = None
                        else:
                            log.info(f"[{i}/{len(syms)}] {ticker} retry {attempt+1}: {msg}")
                            time.sleep(delay)
                            delay *= 3

            if capture is not None:
                conn.execute("SAVEPOINT ticker_savepoint")
                try:
                    published_at = None
                    if capture.get("source_xlsx_url"):
                        published_at = inspect_xlsx(req, capture["source_xlsx_url"])
                    counts = ing.ingest_capture(
                        conn, ticker, capture, captured_at,
                        STOCK_DETAIL_URL.format(ticker=ticker),
                        xlsx_verified_published_at=published_at,
                    )
                    for k, v in counts.items():
                        totals[k] = totals.get(k, 0) + v
                    log.info(f"[{i}/{len(syms)}] {ticker}: {counts}")
                    # ingest_capture() no longer commits -- we own the transaction.
                    # In normal mode commit after each successful ticker so a crash
                    # mid-run does not lose all previously ingested tickers.
                    # In dry-run mode we never commit; the single rollback below
                    # undoes every INSERT across all tickers atomically.
                    conn.execute("RELEASE ticker_savepoint")
                    if not a.dry_run:
                        conn.commit()
                        conn.execute("BEGIN")
                    ok += 1
                except Exception as e:
                    conn.execute("ROLLBACK TO ticker_savepoint")
                    conn.execute("RELEASE ticker_savepoint")
                    log.warning(f"[{i}/{len(syms)}] {ticker} Ingestion failed, rolled back ticker transaction: {e}")
                    fail += 1

            if i % REST_EVERY == 0:
                time.sleep(REST_FOR)
            if i < len(syms):
                time.sleep(PACE + random.random() * JITTER)

        browser.close()

    if a.dry_run:
        conn.rollback()
        log.info("dry-run: rolled back DB writes (raw captures on disk are kept)")
    log.info(f"DONE ok={ok} fail={fail} in {(time.time()-t_start)/60:.1f} min -- totals: {totals}")
    conn.close()


if __name__ == "__main__":
    main()
