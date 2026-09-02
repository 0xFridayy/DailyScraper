"""
Pull 9-month daily history per ticker from NeoBDM /transaction_chart/.

Captures the Dash figure callback and stores, per ticker:
  - ohlcv: daily open/high/low/close/volume (real trading dates, no weekend rows)
  - category_flow: cumulative + daily money flow per NeoBDM category
      m (market maker), nr (non-retail), s (smart), r (retail),
      f (foreign), i (institution), z (zombie)
  - cross_index: NeoBDM's 0..1 cross index line

Writes to txchart_history.db — intentionally separate from neobdm.db, which the
nightly CI job owns and commits back; mixing them would create binary git
conflicts on every pull.

Usage: py txchart_history_pull.py [TICKER ...]   (defaults to UNIVERSE below)
"""
import base64
import json
import sqlite3
import struct
import sys
import time

from playwright.sync_api import sync_playwright

from neobdm_scraper import login

TXCHART_URL = "https://neobdm.tech/transaction_chart/"
DB_PATH = "txchart_history.db"
CATEGORIES = ("m", "nr", "s", "r", "f", "i", "z")

UNIVERSE = [
    "ADES", "BNBR", "BREN", "BRMS", "BRPT", "BUKA", "BUMI", "BUVA", "CBDK",
    "CDIA", "COIN", "CUAN", "DEWA", "DOOH", "ELTY", "EMTK", "ENRG", "ERAA",
    "JGLE", "KIOS", "MINA", "PADI", "PANI", "PTRO", "RAJA", "RATU", "SCMA",
    "TPIA", "VIVA", "VKTR", "WIFI",
]

_DTYPES = {"f8": ("d", 8), "f4": ("f", 4), "i8": ("q", 8), "i4": ("i", 4),
           "i2": ("h", 2), "i1": ("b", 1), "u1": ("B", 1), "u2": ("H", 2), "u4": ("I", 4)}


def decode(v):
    """Plotly typed array {'dtype','bdata'} -> list; lists pass through."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict) and "bdata" in v:
        fmt, size = _DTYPES[v["dtype"]]
        raw = base64.b64decode(v["bdata"])
        return list(struct.unpack(f"<{len(raw) // size}{fmt}", raw))
    return None


def find_fig(node):
    if isinstance(node, dict):
        if "data" in node and "layout" in node:
            return node
        for v in node.values():
            r = find_fig(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = find_fig(v)
            if r:
                return r
    return None


def parse_figure(fig):
    """Return (dates, ohlcv_rows_dict, cum, daily, cross) or None if no candles."""
    candle = next((t for t in fig["data"] if t.get("type") == "candlestick"), None)
    if not candle:
        return None
    dates = candle["x"]
    n = len(dates)
    o, h, l, c = (decode(candle[k]) for k in ("open", "high", "low", "close"))

    vol_tr = next((t for t in fig["data"]
                   if t.get("name") == "volume" and t.get("type") == "bar"), None)
    vol = decode(vol_tr.get("y")) if vol_tr else [None] * n

    cum = {}
    for t in fig["data"]:
        if t.get("type") == "scatter" and t.get("name") in CATEGORIES:
            ys = decode(t.get("y"))
            if ys and len(ys) == n:
                cum[t["name"]] = ys

    # Daily-flow bars: the one yaxis that carries ALL categories (participation
    # panels only have f/r/i/z, and may equal n for young listings).
    by_axis = {}
    for t in fig["data"]:
        if t.get("type") == "bar" and t.get("name") in CATEGORIES:
            ys = decode(t.get("y"))
            if ys and len(ys) == n:
                by_axis.setdefault(t.get("yaxis", "y"), {})[t["name"]] = ys
    daily = {}
    for axis, cats in sorted(by_axis.items(), key=lambda kv: -len(kv[1])):
        if len(cats) == len(CATEGORIES):
            daily = cats
            break

    cross_tr = next((t for t in fig["data"] if t.get("name") == "cross_index"), None)
    cross = decode(cross_tr.get("y")) if cross_tr else None
    if cross is not None and len(cross) != n:
        cross = None

    return dates, {"o": o, "h": h, "l": l, "c": c, "v": vol}, cum, daily, cross


def store(con, ticker, parsed):
    dates, px, cum, daily, cross = parsed
    n = len(dates)
    con.executemany(
        "INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?)",
        [(dates[i], ticker, px["o"][i], px["h"][i], px["l"][i], px["c"][i], px["v"][i])
         for i in range(n)],
    )
    rows = []
    for cat in CATEGORIES:
        cu, da = cum.get(cat), daily.get(cat)
        for i in range(n):
            rows.append((dates[i], ticker, cat,
                         cu[i] if cu else None, da[i] if da else None))
    con.executemany("INSERT OR REPLACE INTO category_flow VALUES (?,?,?,?,?)", rows)
    if cross:
        con.executemany("INSERT OR REPLACE INTO cross_index VALUES (?,?,?)",
                        [(dates[i], ticker, cross[i]) for i in range(n)])
    con.commit()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, PRIMARY KEY (date, ticker));
        CREATE TABLE IF NOT EXISTS category_flow (
            date TEXT, ticker TEXT, category TEXT, cum_flow REAL, daily_flow REAL,
            PRIMARY KEY (date, ticker, category));
        CREATE TABLE IF NOT EXISTS cross_index (
            date TEXT, ticker TEXT, value REAL, PRIMARY KEY (date, ticker));
    """)
    return con


def pull_ticker(page, ticker):
    """Load chart for one ticker at 9m range; return the largest parsed figure."""
    figures = []

    def on_response(resp):
        if "_dash-update-component" in resp.url and resp.request.method == "POST":
            try:
                body = resp.json()
            except Exception:
                return
            fig = find_fig(body)
            if fig:
                parsed = parse_figure(fig)
                if parsed:
                    figures.append(parsed)

    page.on("response", on_response)
    try:
        page.goto(TXCHART_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        try:
            page.click("#tick .Select-control", timeout=4000)
        except Exception:
            page.click("input", timeout=4000)
        page.wait_for_timeout(400)
        page.keyboard.type(ticker)
        page.wait_for_timeout(1200)
        page.keyboard.press("Enter")

        deadline = time.time() + 25
        while not figures and time.time() < deadline:
            page.wait_for_timeout(500)
        if not figures:
            raise RuntimeError("no figure callback after ticker select")

        seen = len(figures)
        page.click("text=9m", timeout=4000)
        deadline = time.time() + 25
        while len(figures) <= seen and time.time() < deadline:
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("response", on_response)

    return max(figures, key=lambda p: len(p[0]))


def main():
    tickers = sys.argv[1:] or UNIVERSE
    con = init_db()
    ok, failed = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()
        login(page)
        for idx, ticker in enumerate(tickers, 1):
            try:
                parsed = pull_ticker(page, ticker)
                store(con, ticker, parsed)
                dates = parsed[0]
                print(f"[{idx}/{len(tickers)}] {ticker}: {len(dates)} bars "
                      f"{dates[0]}..{dates[-1]}", flush=True)
                ok.append(ticker)
            except Exception as e:
                print(f"[{idx}/{len(tickers)}] {ticker}: FAILED — {e}", flush=True)
                failed.append(ticker)
        browser.close()
    print(f"\nDone. ok={len(ok)} failed={len(failed)} {failed if failed else ''}")


if __name__ == "__main__":
    main()
