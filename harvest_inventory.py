"""Harvest NeoBDM's inventory API across the whole IDX universe.

One GET per ticker returns a full year of daily per-broker buy/sell lot and
value plus clean OHLCV:

    GET /api/inventory?symbol=<T>&brokers=<c1,c2,...>
                       &start_date=<YYYY-MM-DD>&end_date=<YYYY-MM-DD>
                       &investor_type=A            # 'F' foreign / 'D' domestic / 'A' all

    data = {date:[...], blot:{BROKER:[...]}, bval:{...}, slot:{...}, sval:{...},
            nlot:{...}, nval:{...}, ohlc:[{date,open,high,low,close,volume,...}]}

This is the same data the /inventory-chart/ Plotly chart draws, so the
"inventory curve" per broker is just a cumsum of nlot. It is strictly better
than the old broker_flow capture: real buy/sell split (so cost basis is
computable), 101 broker codes instead of 29, and no cross-ticker cloning.

Raw responses are cached gzipped under inventory_raw/ so the expensive part is
done once. build_inventory_db.py turns the cache into parquet.

NeoBDM rate-limits aggressive clients ("abnormal usage" at ~50 rapid requests),
so this paces itself, jitters, pauses every REST_EVERY tickers, and backs off
exponentially on failure. It is resumable: cached tickers are skipped.

Usage:
    python harvest_inventory.py                 # all tickers
    python harvest_inventory.py --limit 50      # smoke test
    python harvest_inventory.py --tickers BREN,PANI
"""
import argparse
import gzip
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from neobdm_scraper import API_BASE, INVENTORY_CHART_URL, login, log  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "inventory_raw")
START_DATE = "2025-08-22"          # the API's min_date_allowed
PACE = 1.0                         # seconds between requests, before jitter
JITTER = 0.5
REST_EVERY = 50                    # tickers
REST_FOR = 15                      # seconds
MAX_RETRY = 4
NOT_A_TICKER = {"IHSG", "ISSI"}    # indices that share the 4-letter shape


def end_date():
    return time.strftime("%Y-%m-%d")


def load_universe():
    p = os.path.join(HERE, "all_tickers.json")
    syms = json.load(open(p))
    return [s for s in syms if s not in NOT_A_TICKER]


def load_codes():
    return json.load(open(os.path.join(HERE, "broker_codes.json")))


def cached(t):
    return os.path.join(RAW, f"{t}.json.gz")


def fetch(req, ticker, codes, ed):
    # brokers MUST be repeated query params (brokers=AK&brokers=BK&...).
    # A comma-joined list is accepted with HTTP 200 but returns empty series.
    bq = "&".join(f"brokers={c}" for c in codes)
    qs = (f"symbol={ticker}&{bq}"
          f"&start_date={START_DATE}&end_date={ed}&investor_type=A")
    r = req.get(f"{API_BASE}/inventory?{qs}", timeout=120000)
    txt = r.text()
    j = json.loads(txt)
    if not j.get("success"):
        raise RuntimeError(f"{ticker}: {str(j.get('message'))[:120]}")
    return j["data"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tickers", default="")
    ap.add_argument("--refresh", action="store_true", help="re-fetch cached tickers")
    a = ap.parse_args()

    os.makedirs(RAW, exist_ok=True)
    codes = load_codes()
    syms = ([s.strip().upper() for s in a.tickers.split(",") if s.strip()]
            if a.tickers else load_universe())
    if not a.refresh:
        syms = [s for s in syms if not os.path.exists(cached(s))]
    if a.limit:
        syms = syms[:a.limit]
    ed = end_date()
    log.info(f"harvest: {len(syms)} tickers to fetch, {len(codes)} broker codes, "
             f"{START_DATE}..{ed}")
    if not syms:
        log.info("nothing to do - cache is complete")
        return

    ok = fail = 0
    t_start = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        login(page)
        page.goto(INVENTORY_CHART_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        req = ctx.request

        for i, t in enumerate(syms, 1):
            delay = 1.0
            for attempt in range(MAX_RETRY):
                try:
                    d = fetch(req, t, codes, ed)
                    with gzip.open(cached(t), "wt", encoding="utf-8") as f:
                        json.dump(d, f)
                    ok += 1
                    break
                except Exception as e:
                    msg = str(e)[:130]
                    if attempt == MAX_RETRY - 1:
                        log.warning(f"[{i}/{len(syms)}] {t} GIVE UP: {msg}")
                        fail += 1
                    else:
                        log.info(f"[{i}/{len(syms)}] {t} retry {attempt+1}: {msg}")
                        time.sleep(delay)
                        delay *= 3
                        if "abnormal" in msg.lower() or "429" in msg:
                            log.warning("rate limit hit - cooling down 120s")
                            time.sleep(120)
            if i % 25 == 0:
                rate = i / max(time.time() - t_start, 1)
                eta = (len(syms) - i) / max(rate, 1e-9) / 60
                log.info(f"[{i}/{len(syms)}] ok={ok} fail={fail} "
                         f"{rate*60:.0f}/min eta {eta:.0f}min")
            if i % REST_EVERY == 0:
                time.sleep(REST_FOR)
            time.sleep(PACE + random.random() * JITTER)
        b.close()
    log.info(f"DONE ok={ok} fail={fail} in {(time.time()-t_start)/60:.1f} min")


if __name__ == "__main__":
    main()
