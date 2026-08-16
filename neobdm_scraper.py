"""
NeoBDM - Market Summary + Broker Stalker Scraper + Telegram Bot
Uses Playwright (built-in Chromium).
Sends daily data at 7:00 AM (Malaysia Time) to Telegram.
Supports /scrape Telegram command to run both jobs on demand.
"""

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import requests
import schedule
import time
import logging
import re
import sqlite3
from datetime import datetime, timedelta
import os
import pytz


# ─────────────────────────────────────────────
#  SECRETS
#  Loaded from environment variables — never hardcoded.
#  Local runs: put them in a .env file next to this script (git-ignored).
#  GitHub Actions: set them as encrypted repository Secrets.
# ─────────────────────────────────────────────
def _load_dotenv():
    """Minimal .env loader (no extra dependency). Does nothing if no .env."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _require_env(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required secret '{name}'. Set it in a local .env file "
            f"or as a GitHub Actions repository secret."
        )
    return val


_load_dotenv()

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
NEOBDM_USERNAME    = _require_env("NEOBDM_USERNAME")
NEOBDM_PASSWORD    = _require_env("NEOBDM_PASSWORD")
NEOBDM_LOGIN_URL   = "https://neobdm.tech/accounts/login/?next=/home/"
NEOBDM_DATA_URL    = "https://neobdm.tech/market_summary/"
NEOBDM_BROKER_URL  = "https://neobdm.tech/broker_stalker/"
NEOBDM_DASHBOARD_URL = "https://neobdm.tech/dashboard/screener/"

# 2026-07 patch: the old /market_summary/ Dash page was retired (now 500s).
# Market Summary is a Tabulator grid backed by a JSON screener API (see the
# section 2 comment). These drive the API-based replacement.
NEOBDM_NEWMARKET_URL  = "https://neobdm.tech/new-market-summary/"
API_BASE              = "https://neobdm.tech/api"
MARKET_UNIVERSE       = "COMPOSITE"       # whole market (old Stock Universe = ALL)
SCRAPER_SCREENER_NAME = "DAILY_SCRAPER"   # reusable screener the scraper owns
MARKET_PAGE_SIZE      = 20                # API only accepts sizes 10/15/20
# NeoBDM temporarily blocks "abnormal usage" (~50 rapid requests trips it), so we
# do NOT walk all ~148 market pages. Top-2 needs only the top of the m_dn_0-desc
# sort (first few pages); full-column persistence uses a small custom universe of
# just the tracked tickers (~3 pages). Pace requests to stay under the throttle.
MAX_TOP_PAGES         = 6                 # COMPOSITE pages scanned for Top-2 Akum
API_PAGE_PAUSE        = 0.6               # seconds between page POSTs (anti-abuse)
TRACKED_UNIVERSE_NAME = "DAILY_SCRAPER_TICKERS"  # custom universe = TRACKED_TICKERS

# Raw broker-flow history for the ML backtest pipeline (Roadmap #2). Committed
# back to the repo by the GH Actions workflow after each run — see
# .github/workflows/daily-scrape.yml.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neobdm.db")
BACKFILL_TARGET_DAYS = 30

# Dashboard "Top Akum" presets to pull (label, dropdown value, emoji). The
# dashboard table is a Tabulator grid (#table-custom); rows keyed by
# tabulator-field: tick, price, chg, history (5d flow), tx (=%M, rank metric).
# Dashboard "Top Akum" lists. Post-patch these are dashboard SCREENERS served by
# GET /api/screeners/dashboard + POST /api/market-summary/summary/{id}. Each tuple
# is (display label, dashboard-screener name, rank field "%M", emoji).
DASHBOARD_PRESETS = [
    ("Bandarmologi", "Top Akum Bandar",   "m_dn_0",  "🏦"),
    ("NonRetail",    "Top Retail Jualan", "nr_dn_0", "🏢"),
    ("Foreign",      "Top Akum Asing",    "f_dn_0",  "🌏"),
]
DASH_TOP_N = 5  # tickers shown per dashboard list

TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = _require_env("TELEGRAM_CHAT_ID")

TIMEZONE  = "Asia/Kuala_Lumpur"
SEND_TIME = "07:00"

# Retail-dominated brokers. Their combined net sell is the retail-distribution
# signal we screen for. Note: YP/PD also appear in the bandar groups below —
# contested codes are intentionally kept on both sides (per user choice), so a
# stock can show retail selling and bandar buying through the same code.
RETAIL_BROKERS = ["XL", "XC", "YP", "PD"]

# Broker-stalker retail set: ONLY XL + XC (the purest retail). Their net sell on
# the broker_stalker page (Foreign Only unchecked) gives the top retail-dumped
# tickers; we then read who's accumulating them on the broker_summary page.
STALKER_RETAIL = ["XL", "XC"]
STALKER_TOP_N  = 3   # how many top retail-sold tickers to report
STALKER_BUYERS = 2   # top buyers to show per timeframe

# Special broker-behavior tags used to annotate the broker-stalker analisa.
# (See memory: idx-broker-behavior-taxonomy)
WHALER_BROKER       = "MG"   # top BUY today -> usually sells tomorrow (short-term)
SMOOTH_ACCUM_BROKER = "SS"   # distributes gently -> still time to exit

# "PPR" (owner proxy) is NOT a broker code — it's the broker an owner usually
# trades through. We model it via STOCK_OWNER (below): a stock's owner-proxy
# brokers ARE its owning bandar's codes, so net buy by the owning bandar =
# owner buyback. No separate scan needed; it reuses the bandar cross-reference.

# Smart-money / institutional accumulators (Stockbit + Hengky Adinata mentoring).
# The core "TOD" bullish signal = these net-BUYING a stock that retail is net
# selling (smart money absorbing retail). Per-broker note captures the nuance:
# IF is the lead/strongest read; BB's accumulation is sometimes trading-only.
SMART_MONEY = {
    "IF": "💎 IF (smart money) akumulasi dari ritel — bullish",
    "AZ": "💎 AZ (smart money) akumulasi dari ritel",
    "BB": "💎 BB akumulasi dari ritel (catatan: BB kadang trading-only)",
}

# Foreign algo/institutional "big players", two tiers (user-defined).
#   Tier 1 (primary signal): AK=UBS, BK=J.P. Morgan, ZP=Maybank, RX=Macquarie
#   Tier 2 (secondary):      YP, YU=CGS Intl, AI=UOB Kay Hian, CC (foreign flow)
# Note YP/CC also appear in retail / bandar groups — overlap kept on purpose.
ALGO_BIG_PLAYERS_T1 = ["AK", "BK", "ZP", "RX"]
ALGO_BIG_PLAYERS_T2 = ["YP", "YU", "AI", "CC"]

# "Big player" absorber bloc for the broker-stalker signal: smart money + algo
# T1 + T2. Retail codes (XL/XC/YP/PD) are EXCLUDED so the absorber and the retail
# seller stay distinct parties (no double-counting in the buy-vs-sell magnitude).
# The owning bandar is largely covered here (CC/AK/ZP/PD… already included).
BIG_PLAYER_ABSORBERS = [
    c for c in dict.fromkeys(
        list(SMART_MONEY) + ALGO_BIG_PLAYERS_T1 + ALGO_BIG_PLAYERS_T2
    )
    if c not in RETAIL_BROKERS
]

# Bandar group -> broker codes mapping (used to cross-reference big-fund buy side)
BANDAR_GROUPS = {
    "Prajogo":       ["DX", "NI"],
    "Bakrie":        ["LG", "DH"],
    "Hengky":        ["CP", "YB", "AO", "YP", "XL", "PD", "CC", "HP"],
    "Hapsoro":       ["YP", "AK", "SQ"],
    "Hashim":        ["YP", "CC", "AK"],
    "Haji Isam":     ["CC", "SQ"],
    "Salim":         ["CC"],
    "Astra":         ["YP", "AK"],
    "Barito":        ["YP", "CP"],
    "Sinarmas":      ["ZP", "CS"],
    "Djarum":        ["SQ"],
    "Lippo":         ["PD", "NI"],
    "Happy Hapsoro": ["CC", "YP", "PD", "LG", "ES"],
    "EMTEK":         ["BB", "CC"],
    "Aguan":         ["TP", "RB", "KI", "PD"],
}

# Stock -> controlling owner group ("usual / known by public", NOT 100% certain).
# Used for owner-proxy ("PPR") detection: if the stock's owning bandar net-buys
# it, that's an owner buyback. Owner-proxy reuses the BANDAR_GROUPS scans, so a
# stock only gets a buyback signal if its owner has broker codes defined above.
STOCK_OWNER = {
    # Hashim Djojohadikusumo
    "KIOS": "Hashim", "DOOH": "Hashim", "WIFI": "Hashim", "COIN": "Hashim",
    # Hapsoro  (PADI flagged 'redflag' by user)
    "PADI": "Hapsoro", "PSKT": "Hapsoro", "MINA": "Hapsoro", "UANG": "Hapsoro",
    "SINI": "Hapsoro", "RATU": "Hapsoro", "RAJA": "Hapsoro", "BUVA": "Hapsoro",
    # Haji Isam
    "FAST": "Haji Isam", "TEBE": "Haji Isam", "JARR": "Haji Isam", "PGUN": "Haji Isam",
    # Emtek  (broker codes TBD)
    "RSGK": "EMTEK", "CASS": "EMTEK", "SAME": "EMTEK", "BUKA": "EMTEK",
    "SCMA": "EMTEK", "BBHI": "EMTEK", "EMTK": "EMTEK",
    # Prajogo
    "PTRO": "Prajogo", "CDIA": "Prajogo", "CUAN": "Prajogo", "BRPT": "Prajogo",
    "TPIA": "Prajogo", "BREN": "Prajogo",
    # Bakrie
    "VIVA": "Bakrie", "JGLE": "Bakrie", "ELTY": "Bakrie",
    "MDIA": "Bakrie", "DEWA": "Bakrie", "BNBR": "Bakrie",
    "ENRG": "Bakrie", "VKTR": "Bakrie", "BRMS": "Bakrie",
    # Aguan  (broker codes TBD)
    "PDPP": "Aguan", "JIHD": "Aguan", "ERAL": "Aguan", "INPC": "Aguan",
    "ERAA": "Aguan", "CBDK": "Aguan", "PANI": "Aguan",
}

# Tickers to persist daily broker_flow rows for (walk-forward backtest input).
# PLACEHOLDER: defaults to every STOCK_OWNER ticker since that's the only
# explicit ticker universe already in this file — confirm/replace with your
# actual watchlist.
TRACKED_TICKERS = sorted(set(STOCK_OWNER.keys()))

# Hard ceiling on the tracked-universe fetch: its page count plus one page of
# slack. The custom universe should hold exactly TRACKED_TICKERS, so this cap is
# never reached on a healthy run. It exists because a silently-ignored universe
# filter turns that fetch into a full ~148-page market walk, which trips NeoBDM's
# ~50-request abuse block mid-run and empties everything scraped after it.
MAX_TRACKED_PAGES = -(-len(TRACKED_TICKERS) // MARKET_PAGE_SIZE) + 1

# Broker codes to persist per-ticker flow for. Each code costs 2 get_netflow
# calls (akum side + dist side), each a full page reload (~10-15s) — this
# list size drives the nightly job's runtime. Trim it if the GH Actions
# timeout gets tight.
BROKER_FLOW_CODES = sorted(set(
    RETAIL_BROKERS + STALKER_RETAIL + list(SMART_MONEY) +
    ALGO_BIG_PLAYERS_T1 + ALGO_BIG_PLAYERS_T2 +
    [WHALER_BROKER, SMOOTH_ACCUM_BROKER] +
    [c for codes in BANDAR_GROUPS.values() for c in codes]
))
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def parse_num(s):
    if s is None:
        return 0.0
    s = str(s).replace(",", "").replace("(", "-").replace(")", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ── 1. LOGIN ──────────────────────────────────

def login(page):
    log.info("Going to login page...")
    page.goto(NEOBDM_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    try:
        page.screenshot(path="login_page.png", timeout=10000)
        log.info("Saved login_page.png")
    except Exception as e:
        log.warning(f"Could not save login_page.png (non-fatal): {e}")

    selectors = [
        'input[name="username"]',
        'input[type="text"]',
        '#id_username',
        'input[id="username"]',
    ]
    username_filled = False
    for sel in selectors:
        try:
            if page.is_visible(sel, timeout=5000):
                page.fill(sel, NEOBDM_USERNAME)
                log.info(f"Filled username using selector: {sel}")
                username_filled = True
                break
        except Exception:
            continue

    if not username_filled:
        log.error("Could not find username field!")
        try:
            page.screenshot(path="debug_login_fail.png", timeout=10000)
        except Exception:
            pass
        raise RuntimeError("Username field not found")

    pass_selectors = [
        'input[name="password"]',
        'input[type="password"]',
        '#id_password',
    ]
    for sel in pass_selectors:
        try:
            if page.is_visible(sel, timeout=5000):
                page.fill(sel, NEOBDM_PASSWORD)
                log.info(f"Filled password using selector: {sel}")
                break
        except Exception:
            continue

    try:
        page.click('button[type="submit"]', timeout=5000)
    except Exception:
        try:
            page.click('input[type="submit"]', timeout=5000)
        except Exception:
            page.keyboard.press("Enter")

    # networkidle is unreliable on pages with any background polling/websockets
    # (common on a live trading dashboard) — wait for the deterministic thing
    # we actually care about instead: having navigated away from the login URL.
    try:
        page.wait_for_url(lambda url: "login" not in url.lower(), timeout=30000)
    except PlaywrightTimeout:
        pass  # fall through to the explicit check below, which raises clearly
    page.wait_for_timeout(2000)

    log.info(f"After login URL: {page.url}")
    try:
        page.screenshot(path="after_login.png", timeout=10000)
    except Exception as e:
        log.warning(f"Could not save after_login.png (non-fatal): {e}")

    if "login" in page.url.lower():
        log.warning("Still on login page!")
        raise RuntimeError("Login failed")
    log.info("Login successful!")


# ── 2. MARKET SUMMARY (Top 3 Akum Bandar) ────

# The old Dash page was retired in the 2026-07 patch (/market_summary/ now 500s).
# Market Summary is now a Tabulator grid backed by a JSON screener API:
#   GET  /api/market-summary/columns       -> 385 column definitions (fields)
#   GET  /api/stock-universe               -> universes (COMPOSITE = whole market)
#   GET  /api/screeners                    -> saved screeners
#   POST /api/screeners {name}             -> create a screener
#   PATCH/api/screeners/{id} {columns,filters,stock_universe_id,sort_field,sort_direction}
#   POST /api/market-summary/summary/{id}  -> rows (remote pagination)
#        body {page,size,sort_field,sort_direction}; size MUST be 10/15/20;
#        needs the X-CSRFToken header; resp {data:{data:[...]}, meta:{last_page}}
# We keep a reusable screener (DAILY_SCRAPER) carrying ALL columns over COMPOSITE,
# page through it, persist the full rows, and reduce to Top-2 Akum for Telegram.

def _api_session(page):
    """(request_ctx, headers) from the authenticated Playwright context. Visiting
    the page first ensures the csrftoken cookie is set for this path."""
    page.goto(NEOBDM_NEWMARKET_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    ctx = page.context
    csrf = next((c["value"] for c in ctx.cookies() if c["name"] == "csrftoken"), "")
    headers = {"Content-Type": "application/json", "X-CSRFToken": csrf,
               "Referer": NEOBDM_NEWMARKET_URL}
    return ctx.request, headers


def _api_json(resp):
    import json as _json
    try:
        return _json.loads(resp.text())
    except Exception:
        return None


def get_market_columns(req):
    j = _api_json(req.get(f"{API_BASE}/market-summary/columns")) or {}
    return [c["field"] for c in (j.get("data") or [])
            if isinstance(c, dict) and c.get("field")]


def get_universe_id(req, name=MARKET_UNIVERSE):
    j = _api_json(req.get(f"{API_BASE}/stock-universe")) or {}
    for u in (j.get("data") or []):
        if str(u.get("name", "")).upper() == name.upper():
            return u.get("id")
    return None


def ensure_scraper_screener(req, headers, columns, universe_id):
    """Find-or-create the DAILY_SCRAPER screener and (re)set it to carry all
    columns over the chosen universe with no filters. Returns its id."""
    import json as _json
    j = _api_json(req.get(f"{API_BASE}/screeners")) or {}
    mine = next((s for s in (j.get("data") or [])
                 if s.get("name") == SCRAPER_SCREENER_NAME), None)
    if not mine:
        r = req.post(f"{API_BASE}/screeners",
                     data=_json.dumps({"name": SCRAPER_SCREENER_NAME}), headers=headers)
        mine = (_api_json(r) or {}).get("data") or {}
        log.info(f"Created screener '{SCRAPER_SCREENER_NAME}'")
    sid = mine.get("id")
    body = {"columns": columns, "filters": [], "stock_universe_id": universe_id,
            "sort_field": "m_dn_0", "sort_direction": "desc"}
    req.patch(f"{API_BASE}/screeners/{sid}", data=_json.dumps(body), headers=headers)
    return sid


def ensure_tracked_universe(req, headers, tickers):
    """Find-or-create a custom stock-universe holding exactly `tickers`, so the
    persistence fetch returns ~len(tickers) rows (a few pages) instead of the
    whole ~3k-stock market. Returns its id, or None on failure (caller falls back
    to persisting whatever tracked tickers appear in the top scan)."""
    import json as _json
    try:
        j = _api_json(req.get(f"{API_BASE}/stock-universe")) or {}
        mine = next((u for u in (j.get("data") or [])
                     if u.get("name") == TRACKED_UNIVERSE_NAME), None)
        body = {"name": TRACKED_UNIVERSE_NAME, "stocks": sorted(set(tickers))}
        if mine:
            r = req.put(f"{API_BASE}/stock-universe/{mine['id']}",
                        data=_json.dumps(body), headers=headers)
        else:
            r = req.post(f"{API_BASE}/stock-universe",
                         data=_json.dumps(body), headers=headers)
        # Check the write actually landed. Previously the id was returned
        # unconditionally, so a rejected write left the screener on COMPOSITE and
        # the "tracked" fetch silently walked the whole market.
        j2 = _api_json(r) or {}
        if not j2.get("success", r.ok):
            log.warning(f"tracked universe write rejected: {j2.get('message')}")
            return None
        return ((j2.get("data") or {}).get("id")) or (mine or {}).get("id")
    except Exception as e:
        log.warning(f"tracked universe setup failed (will fall back): {e}")
        return None


def _fetch_summary_pages(req, headers, sid, max_pages=None):
    """Page through /market-summary/summary/{sid} (sorted m_dn_0 desc), paced.
    Stops at last_page or max_pages, whichever comes first."""
    import json as _json
    rows, page_no, last = [], 1, 1
    while page_no <= last and (max_pages is None or page_no <= max_pages):
        body = {"page": page_no, "size": MARKET_PAGE_SIZE,
                "sort_field": "m_dn_0", "sort_direction": "desc"}
        j = _api_json(req.post(f"{API_BASE}/market-summary/summary/{sid}",
                               data=_json.dumps(body), headers=headers)) or {}
        if not j.get("success"):
            log.error(f"Summary fetch failed (p{page_no}): {j.get('message')}")
            break
        last = (j.get("meta") or {}).get("last_page", page_no)
        d = j.get("data")
        rows.extend((d.get("data") if isinstance(d, dict) else d) or [])
        page_no += 1
        time.sleep(API_PAGE_PAUSE)
    return rows, last


def fetch_market_summary(page):
    """Two bounded, paced fetches (kept small to avoid NeoBDM's abuse throttle):
      1. COMPOSITE, first MAX_TOP_PAGES pages -> top-of-market rows for Top-2 Akum.
      2. Tracked-ticker universe, all pages   -> full-column rows to persist.
    Returns (columns, top_rows, tracked_rows)."""
    import json as _json
    req, headers = _api_session(page)
    columns = get_market_columns(req)
    if not columns:
        log.error("Market summary: no columns returned from API")
        return [], [], []
    composite = get_universe_id(req, MARKET_UNIVERSE)
    sid = ensure_scraper_screener(req, headers, columns, composite)

    top_rows, last = _fetch_summary_pages(req, headers, sid, max_pages=MAX_TOP_PAGES)
    log.info(f"Market top scan: {len(top_rows)} rows (first {MAX_TOP_PAGES} of {last} pages)")

    tracked_rows = []
    tracked_uni = ensure_tracked_universe(req, headers, TRACKED_TICKERS)
    if tracked_uni:
        req.patch(f"{API_BASE}/screeners/{sid}", headers=headers, data=_json.dumps(
            {"columns": columns, "filters": [], "stock_universe_id": tracked_uni,
             "sort_field": "m_dn_0", "sort_direction": "desc"}))
        tracked_rows, t_last = _fetch_summary_pages(req, headers, sid,
                                                    max_pages=MAX_TRACKED_PAGES)
        # last_page still reporting the whole market means the universe filter was
        # ignored: these rows are top-of-market, not our tracked set. Drop them and
        # let the caller fall back to tracked tickers found in the top scan.
        if t_last > MAX_TRACKED_PAGES:
            log.error(f"Tracked universe not applied (last_page={t_last} > "
                      f"{MAX_TRACKED_PAGES}) — discarding {len(tracked_rows)} rows")
            tracked_rows = []
        else:
            log.info(f"Tracked-universe rows: {len(tracked_rows)}")
    return columns, top_rows, tracked_rows


# ── 2a. MARKET SUMMARY PERSISTENCE (full 385-col rows) ────────
# Wide table whose schema is auto-derived from the API's column list and
# auto-migrated when NeoBDM adds columns. List/dict values (e.g. top_5_buyer)
# are JSON-encoded. keep_tickers bounds the DB (whole COMPOSITE market is ~3k
# rows/day — too heavy for a git-committed .db), so by default we store only the
# tracked konglo universe + today's screen hits. Set keep_tickers=None to store
# the whole market.

def _ms_ensure_columns(conn, fields):
    have = {r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")}
    if not have:
        conn.execute("CREATE TABLE market_summary_daily "
                     "(date TEXT NOT NULL, ticker TEXT NOT NULL, "
                     "PRIMARY KEY(date, ticker))")
        have = {"date", "ticker"}
    for f in fields:
        if f not in have and f not in ("date", "ticker"):
            conn.execute(f'ALTER TABLE market_summary_daily ADD COLUMN "{f}"')
    conn.commit()


def save_market_summary_daily(date_str, columns, rows, keep_tickers):
    import json as _json
    if not rows:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        _ms_ensure_columns(conn, columns)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(market_summary_daily)")]
        payload = []
        for row in rows:
            tick = row.get("symbol")
            if not tick or (keep_tickers is not None and tick not in keep_tickers):
                continue
            rec = {"date": date_str, "ticker": tick}
            for f in columns:
                v = row.get(f)
                rec[f] = _json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
            payload.append(tuple(rec.get(c) for c in cols))
        ph = ",".join("?" * len(cols))
        quoted = ",".join('"' + c + '"' for c in cols)
        conn.executemany(
            f"INSERT OR REPLACE INTO market_summary_daily ({quoted}) VALUES ({ph})",
            payload)
        conn.commit()
        log.info(f"market_summary_daily: saved {len(payload)} rows for {date_str}")
    finally:
        conn.close()


# ── 2b. SCREEN -> Top-2 Akum for Telegram ────────────────────
# Old rule: filter unusual=v, rank dn-0 desc > dn-3 desc > likuid, top 2,
# caution if dn-0<10. New fields: is_unusual_volume(bool), m_dn_0/m_dn_3
# (fractional net-flow), is_liquid(bool), close. NOTE: the net-flow SCALE
# changed (old dn-0 ~10-35 integers; new m_dn_0 ~ -0.1..0.1 fraction), so
# MARKET_DN0_MIN below is a fresh calibration knob, NOT the old 10.
MARKET_DN0_MIN = 0.0  # TODO recalibrate strong-accumulation threshold on m_dn_0


def _truthy(v):
    return v is True or str(v).strip().lower() in ("true", "v", "1")


def screen_market_summary(rows):
    cands = [r for r in rows if _truthy(r.get("is_unusual_volume"))]
    cands.sort(key=lambda r: (parse_num(r.get("m_dn_0")), parse_num(r.get("m_dn_3")),
                              1 if _truthy(r.get("is_liquid")) else 0), reverse=True)
    out = []
    for r in cands[:2]:
        out.append({
            "symbol":  r.get("symbol", ""),
            "unusual": "v" if _truthy(r.get("is_unusual_volume")) else "x",
            "dn-0":    round(parse_num(r.get("m_dn_0")), 4),
            "dn-3":    round(parse_num(r.get("m_dn_3")), 4),
            "likuid":  "v" if _truthy(r.get("is_liquid")) else "x",
            "price":   r.get("close"),
            "_caution": parse_num(r.get("m_dn_0")) < MARKET_DN0_MIN,
        })
    log.info(f"{len(cands)} unusual stock(s); top {len(out)} selected")
    return out


def scrape_market_summary(page):
    """API-based Market Summary: bounded fetch, persist tracked rows, return Top-2."""
    try:
        columns, top_rows, tracked_rows = fetch_market_summary(page)
    except Exception as e:
        log.error(f"Market summary API failed: {e}")
        return []
    top = screen_market_summary(top_rows)
    try:
        date_str = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
        hits = {t["symbol"] for t in top}
        # tracked-universe rows (full set) + any Top-2 hit that isn't tracked.
        # Fallback: if the tracked universe failed, keep tracked tickers that
        # happened to appear in the bounded top scan.
        rows_to_save = list(tracked_rows)
        seen = {r.get("symbol") for r in rows_to_save}
        for r in top_rows:
            sym = r.get("symbol")
            if sym in seen:
                continue
            if sym in hits or (not tracked_rows and sym in set(TRACKED_TICKERS)):
                rows_to_save.append(r)
                seen.add(sym)
        save_market_summary_daily(date_str, columns, rows_to_save, keep_tickers=None)
    except Exception as e:
        log.error(f"market_summary_daily persistence failed: {e}")
    return top


# ── 2b. DASHBOARD "TOP AKUM" PRESETS ──────────

def _is_real_ticker(sym):
    """Filter out warrants/rights/derivative codes (e.g. AADIBQCQ6A) — keep plain
    <=4-letter equity tickers, matching what the dashboard lists used to show."""
    return bool(sym) and sym.isalpha() and len(sym) <= 4


def scrape_dashboard_presets(page):
    """Dashboard 'Top Akum' lists via the screener API. The Bandarmologi/NonRetail/
    Foreign lists are now dashboard SCREENERS (GET /api/screeners/dashboard); we
    POST /market-summary/summary/{id} for each and take the top tickers by its %M
    rank field. Returns [(label, emoji, [rows with 'tick' + 'tx']), ...] — same
    shape _dashboard_lines() expects."""
    import json as _json
    req, headers = _api_session(page)
    try:
        j = _api_json(req.get(f"{API_BASE}/screeners/dashboard")) or {}
        by_name = {s.get("name"): s for s in (j.get("data") or [])}
    except Exception as e:
        log.error(f"Dashboard screeners list failed: {e}")
        return [(lbl, emo, []) for lbl, _s, _f, emo in DASHBOARD_PRESETS]

    results = []
    for label, sname, field, emoji in DASHBOARD_PRESETS:
        rows = []
        s = by_name.get(sname)
        if not s:
            log.warning(f"Dashboard screener '{sname}' not found")
        else:
            try:
                jj = _api_json(req.post(
                    f"{API_BASE}/market-summary/summary/{s['id']}",
                    data=_json.dumps({"page": 1, "size": MARKET_PAGE_SIZE,
                                      "sort_field": field, "sort_direction": "desc"}),
                    headers=headers)) or {}
                d = jj.get("data")
                batch = (d.get("data") if isinstance(d, dict) else d) or []
                for r in batch:
                    sym = r.get("symbol")
                    if _is_real_ticker(sym):
                        rows.append({"tick": sym, "tx": f"{parse_num(r.get(field)) * 100:.1f}%"})
                    if len(rows) >= DASH_TOP_N:
                        break
                time.sleep(API_PAGE_PAUSE)
            except Exception as e:
                log.error(f"Dashboard preset {label} failed: {e}")
        log.info(f"Dashboard {label}: {[r['tick'] for r in rows]}")
        results.append((label, emoji, rows))
    return results


# ── 3. BROKER STALKER ─────────────────────────

def clear_broker_chips(page):
    # NeoBDM's #broker is a react-select v1 widget — "Clear all" (x) button
    # removes every chip at once when present.
    try:
        clear_btn = page.locator("#broker .Select-clear-zone")
        if clear_btn.count() > 0:
            clear_btn.first.click(timeout=2000)
            page.wait_for_timeout(300)
    except Exception:
        pass


def add_broker_chip(page, code):
    # react-select v1: click the control to focus/open, type the code,
    # then Enter selects the (single) filtered/focused option as a chip.
    page.click("#broker .Select-control")
    page.wait_for_timeout(400)
    page.keyboard.type(code)
    page.wait_for_timeout(800)
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)


def set_broker_codes(page, codes):
    clear_broker_chips(page)
    for code in codes:
        add_broker_chip(page, code)


def set_duration(page, label="Today"):
    try:
        page.locator(f"#duration-picker label:has-text('{label}')").first.click(timeout=5000)
    except Exception as e:
        log.warning(f"Could not click duration '{label}': {e}")


# The broker_stalker page splits results into two tables:
#   #broker-akum-stalker  -> net BUYS  (positive netval) = accumulation
#   #broker-dist-stalker  -> net SELLS (negative netval) = distribution
# Accumulation checks MUST read the akum side; retail-sell / SS read the dist side.
SIDE_CONTAINER = {"akum": "#broker-akum-stalker", "dist": "#broker-dist-stalker"}


def parse_stalker_table(page, side="dist"):
    container = SIDE_CONTAINER[side]
    page.wait_for_selector(f"{container} table", timeout=15000)
    trs = page.query_selector_all(f"{container} table tr")
    data = []
    for tr in trs:
        symbol_el = tr.query_selector('td[data-dash-column="symbol"]')
        if not symbol_el:
            continue

        def get(col):
            el = tr.query_selector(f'td[data-dash-column="{col}"]')
            return el.inner_text().strip() if el else ""

        data.append({
            "symbol": symbol_el.inner_text().strip(),
            "netval": get("netval"),
            "bval":   get("bval"),
            "sval":   get("sval"),
            "bavg":   get("bavg"),
            "savg":   get("savg"),
        })
    return data


def get_netflow(page, codes, duration="Today", side="dist"):
    page.goto(NEOBDM_BROKER_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    set_broker_codes(page, codes)
    set_duration(page, duration)
    try:
        page.click("#submit-button", timeout=5000)
    except Exception as e:
        log.warning(f"Could not click #submit-button: {e}")
    page.wait_for_timeout(4000)
    try:
        rows = parse_stalker_table(page, side)
    except Exception as e:
        log.error(f"Broker stalker {side} table parse failed for {codes}: {e}")
        rows = []
    return {r["symbol"]: r for r in rows if r.get("symbol")}


# ── INVENTORY (per-ticker top "bag holders") ──

NEOBDM_INVENTORY_URL = "https://neobdm.tech/inventory/"


def _parse_rp(s):
    """'Rp 1.66Trl' -> 1.66e12, 'Rp -31.86Mlr' -> -31.86e9, 'Rp 0.24Trl', etc."""
    if not s:
        return 0.0
    s = s.replace("Rp", "").replace(" ", "").strip()
    mult = 1.0
    for suf, m in (("Trl", 1e12), ("Mlr", 1e9), ("jt", 1e6), ("rb", 1e3)):
        if s.endswith(suf):
            s, mult = s[:-len(suf)], m
            break
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def get_inventory_bagholders(page, ticker, n=STALKER_BUYERS):
    """Top-n 'bag holders' of a ticker = brokers with the highest cumulative net
    inventory (Net Akum) over the page's default ~3-month window, from the
    Plotly inventory chart. Each broker has a 'markers+text' end-point trace
    whose last y = cumulative net (lot) and customdata = [cum lot str, cum Rp str];
    avg buy price (cost basis) = cum Rp / (cum lot * 100 shares)."""
    page.goto(NEOBDM_INVENTORY_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    # ticker = react-select dropdown #tick (same widget family as #broker)
    page.click("#tick .Select-control")
    page.wait_for_timeout(400)
    page.keyboard.type(ticker)
    page.wait_for_timeout(1500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    page.click("#submit-button")     # Fetch (default date range; no calendar touched)
    page.wait_for_timeout(11000)     # Plotly render
    holders = page.evaluate(
        """() => {
            const el = document.querySelector('.js-plotly-plot');
            if (!el || !el.data) return [];
            return el.data
                .filter(t => (t.mode||'').includes('markers') && t.name &&
                             Array.isArray(t.y) && t.y.length)
                .map(t => ({
                    code: t.name,
                    cum: t.y[t.y.length-1],
                    netval: (t.customdata && t.customdata.length)
                            ? t.customdata[t.customdata.length-1][1] : ''
                }))
                .filter(t => typeof t.cum === 'number')
                .sort((a, b) => b.cum - a.cum);
        }"""
    )
    for h in holders:
        shares = h["cum"] * 100  # 1 lot = 100 shares
        h["avg"] = (_parse_rp(h.get("netval", "")) / shares) if shares else 0
    return holders[:n]


def _fmt_lot(v):
    av = abs(v)
    if av >= 1e6:
        return f"{v/1e6:.2f}M lot"
    if av >= 1e3:
        return f"{v/1e3:.0f}k lot"
    return f"{v:.0f} lot"


def scrape_broker_stalker(page):
    """Retail (XL+XC) top net-sell tickers, then the top-2 'bag holders' of each
    (highest cumulative net inventory) from the inventory chart."""
    # 1) retail net SELL (XL+XC, Foreign Only unchecked) -> top tickers
    log.info(f"Retail net sell scan ({'+'.join(STALKER_RETAIL)})...")
    try:
        retail_sell = get_netflow(page, STALKER_RETAIL, "Today", side="dist")
    except Exception as e:
        log.error(f"Retail net sell scan failed: {e}")
        return []

    top = sorted(retail_sell.values(), key=lambda r: parse_num(r.get("netval", "")))
    top = [r for r in top if parse_num(r.get("netval", "")) < 0][:STALKER_TOP_N]
    log.info(f"Top retail net sell: {[(r['symbol'], r['netval']) for r in top]}")
    if not top:
        return []

    # 2) per ticker -> top bag holders from the inventory chart
    results = []
    for r in top:
        symbol = r["symbol"]
        try:
            holders = get_inventory_bagholders(page, symbol)
        except Exception as e:
            log.error(f"inventory bagholders {symbol} failed: {e}")
            holders = []
        log.info(f"{symbol} bag holders: {[(h['code'], round(h['cum'])) for h in holders]}")
        results.append({
            "symbol":  symbol,
            "netval":  r.get("netval", ""),
            "savg":    r.get("savg", ""),
            "holders": holders,
        })
    return results


# ── 3b. PERSISTENCE (SQLite) ───────────────────
# Raw per-broker/per-ticker flow, stored as scraped — no feature engineering
# here. Feature computation (broker_concentration, retail_presence_pct, etc.)
# happens downstream once there's enough history to build/validate them.

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_flow (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            broker_code TEXT NOT NULL,
            bval REAL,
            sval REAL,
            netval REAL,
            bavg REAL,
            savg REAL,
            PRIMARY KEY (date, ticker, broker_code)
        )
    """)
    conn.commit()
    return conn


def scrape_broker_flow_for_db(page, tickers):
    """For each code in BROKER_FLOW_CODES, pull today's net flow on both sides
    (akum=net-buy, dist=net-sell) across all symbols, keeping only rows for
    `tickers`. Reuses the already-authenticated `page` — no separate login."""
    tickers = set(tickers)
    rows = []
    for code in BROKER_FLOW_CODES:
        for side in ("akum", "dist"):
            try:
                flow = get_netflow(page, [code], "Today", side=side)
            except Exception as e:
                log.error(f"broker_flow scan failed for {code}/{side}: {e}")
                continue
            for symbol, r in flow.items():
                if symbol not in tickers:
                    continue
                rows.append({
                    "ticker": symbol,
                    "broker_code": code,
                    "bval": parse_num(r.get("bval")),
                    "sval": parse_num(r.get("sval")),
                    "netval": parse_num(r.get("netval")),
                    "bavg": parse_num(r.get("bavg")),
                    "savg": parse_num(r.get("savg")),
                })
    return rows


def save_broker_flow(conn, date_str, rows):
    conn.executemany(
        """INSERT OR REPLACE INTO broker_flow
           (date, ticker, broker_code, bval, sval, netval, bavg, savg)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (date_str, r["ticker"], r["broker_code"], r["bval"], r["sval"],
             r["netval"], r["bavg"], r["savg"])
            for r in rows
        ],
    )
    conn.commit()


def log_backfill_progress(conn, tickers, target_days=BACKFILL_TARGET_DAYS):
    """Logs full per-ticker progress and returns a short Telegram-friendly
    summary (only logs are checked otherwise, and nobody checks CI logs daily)."""
    tickers = sorted(set(tickers))
    if not tickers:
        return None
    placeholders = ",".join("?" * len(tickers))
    cur = conn.execute(
        f"SELECT ticker, COUNT(DISTINCT date) FROM broker_flow "
        f"WHERE ticker IN ({placeholders}) GROUP BY ticker",
        tickers,
    )
    counts = dict(cur.fetchall())
    log.info(f"=== Backfill progress (days of history / {target_days} target) ===")
    pending = []
    for t in tickers:
        days = counts.get(t, 0)
        status = "done" if days >= target_days else f"{target_days - days} to go"
        log.info(f"  {t}: {days}/{target_days} days ({status})")
        if days < target_days:
            pending.append((t, days))

    if not pending:
        return f"✅ Backfill: all {len(tickers)}/{len(tickers)} tickers past {target_days}d target."

    pending.sort(key=lambda x: x[1])
    lines = [f"⏳ Backfill: {len(tickers) - len(pending)}/{len(tickers)} tickers past {target_days}d target."]
    lines += [f"  {t}: {d}/{target_days}d ({target_days - d} to go)" for t, d in pending]
    return "\n".join(lines)


def record_konglo_signals(conn, date_str, ms_data, dash_data, bs_data):
    """Whenever a tracked 'konglo' ticker (TRACKED_TICKERS) shows up in any
    of today's radar sections (Top Akum Bandar, Dashboard presets, Broker
    Stalker), record it so its forward performance (highest % and drawdown
    % from the signal-day close, over the next few trading days) can be
    tracked later once price_history has caught up to those dates - see
    run_konglo_watch_report() in run_ml_reports.py for the actual tracking/
    Sharpe computation. This function only detects and persists the flag;
    it does no price lookups itself (today's close for a just-flagged
    ticker won't exist in price_history yet)."""
    tracked = set(TRACKED_TICKERS)
    hits = {}  # ticker -> set of source labels

    for r in ms_data:
        t = r.get("symbol")
        if t in tracked:
            hits.setdefault(t, set()).add("top_akum_bandar")

    for label, _emoji, rows in dash_data:
        for r in rows:
            t = r.get("tick")
            if t in tracked:
                hits.setdefault(t, set()).add(f"dashboard_{label}")

    for r in bs_data:
        t = r.get("symbol")
        if t in tracked:
            hits.setdefault(t, set()).add("broker_stalker")

    if not hits:
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS konglo_signal_watch (
            flag_date TEXT NOT NULL, ticker TEXT NOT NULL, sources TEXT,
            PRIMARY KEY (flag_date, ticker)
        )
    """)
    for t, sources in hits.items():
        conn.execute(
            "INSERT OR IGNORE INTO konglo_signal_watch (flag_date, ticker, sources) VALUES (?, ?, ?)",
            (date_str, t, ",".join(sorted(sources))),
        )
    conn.commit()
    log.info(f"konglo radar hits today: { {t: sorted(s) for t, s in hits.items()} }")


def save_daily_broker_flow(page):
    date_str = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
    conn = init_db()
    try:
        rows = scrape_broker_flow_for_db(page, TRACKED_TICKERS)
        save_broker_flow(conn, date_str, rows)
        log.info(f"broker_flow: saved {len(rows)} rows for {date_str}")
        return log_backfill_progress(conn, TRACKED_TICKERS)
    finally:
        conn.close()


# ── 4. FORMAT MESSAGES ────────────────────────

def now_str():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).strftime("%d %b %Y, %I:%M %p")


def format_market_summary_message(data):
    now = now_str()
    if not data:
        return (
            f"⚠️ NeoBDM Market Summary\n{now}\n\n"
            f"No data scraped today. Check screenshots."
        )

    return "\n".join(_market_summary_lines(data))


def _market_summary_lines(data):
    lines = [
        "📊 Top 2 Akum Bandar (Daily)",
        "Filter: unusual=v | Strong: dn-0≥10 | Rank: dn-0 > dn-3 > liquid",
    ]
    if not data:
        lines.append("No data scraped today.")
        return lines
    label_map = {"likuid": "liquid"}  # display "liquid" for site's "likuid"
    for i, row in enumerate(data, 1):
        symbol = row.get("symbol", f"#{i}")
        details = "  ".join(
            f"{label_map.get(k, k)}: {row[k]}"
            for k in ["unusual", "dn-0", "dn-3", "likuid", "price"] if row.get(k)
        )
        flag = " ⚠️" if row.get("_caution") else ""
        lines.append(f"{i}. {symbol}{flag} | {details}")
        if row.get("_caution"):
            lines.append("   ⚠️ caution: dn-0 < 10, akumulasi lemah hari ini")
    return lines


def format_broker_stalker_message(data):
    return "\n".join(_broker_stalker_lines(data))


def _broker_stalker_lines(data):
    lines = [
        "🕵️ Broker Stalker — Retail (XL+XC) Net Sell → top 2 bag holder",
        "(bag holder = akumulasi inventory terbesar, ~3 bln)",
    ]
    if not data:
        lines.append("Tidak ada retail net sell hari ini.")
        return lines
    for i, row in enumerate(data, 1):
        holders = row.get("holders", [])
        bag = ", ".join(
            f"{h['code']} {_fmt_lot(h['cum'])} @{h.get('avg', 0):.0f}" for h in holders
        ) or "-"
        lines.append(f"{i}. {row['symbol']} | retail jual {row['netval']}  savg: {row['savg']}")
        lines.append(f"   🎒 Bag holder: {bag}")
    return lines


def _dashboard_lines(data):
    lines = ["📋 Dashboard Top Akum (EOD) — ticker (%M)"]
    if not data:
        lines.append("No dashboard data.")
        return lines
    for label, emoji, rows in data:
        if rows:
            tickers = " ".join(f"{r.get('tick')}({r.get('tx','')})" for r in rows)
        else:
            tickers = "-"
        lines.append(f"{emoji} {label}: {tickers}")
    return lines


def format_combined_message(ms_data, dash_data, bs_data):
    """All sections in ONE Telegram message with a single timestamp."""
    lines = [
        "📈 NeoBDM Daily Signal",
        f"🕗 {now_str()}",
        "═════════════════════",
    ]
    lines += _market_summary_lines(ms_data)
    lines.append("─────────────────────")
    lines += _dashboard_lines(dash_data)
    lines.append("─────────────────────")
    lines += _broker_stalker_lines(bs_data)
    lines.append("═════════════════════")
    lines.append("neobdm.tech")
    return "\n".join(lines)


# ── 5. SEND TELEGRAM ──────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.ok:
        log.info("✅ Sent to Telegram!")
    else:
        log.error(f"Telegram error {resp.status_code}: {resp.text}")


# ── 6. JOB RUNNER ──────────────────────────────

def run_all_jobs():
    log.info("=== Daily job starting ===")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, slow_mo=300)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080}
            )
            page = context.new_page()
            page.set_default_timeout(60000)

            login(page)
            ms_data = scrape_market_summary(page)
            dash_data = scrape_dashboard_presets(page)
            bs_data = scrape_broker_stalker(page)

            backfill_progress = None
            try:
                backfill_progress = save_daily_broker_flow(page)
            except Exception as e:
                log.error(f"broker_flow persistence failed: {e}")

            try:
                date_str = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
                watch_conn = init_db()
                try:
                    record_konglo_signals(watch_conn, date_str, ms_data, dash_data, bs_data)
                finally:
                    watch_conn.close()
            except Exception as e:
                log.error(f"konglo signal tracking failed: {e}")

            browser.close()

        message = format_combined_message(ms_data, dash_data, bs_data)
        if backfill_progress:
            message = f"{message}\n\n{backfill_progress}"
        send_telegram(message)
    except Exception as e:
        log.error(f"Job failed: {e}")
        try:
            send_telegram(f"NeoBDM error: {str(e)[:200]}")
        except Exception:
            pass


# ── 7. TELEGRAM COMMAND POLLING ────────────────

def get_telegram_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=15)
        if resp.ok:
            return resp.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates error: {e}")
    return []


def poll_telegram_commands(offset):
    updates = get_telegram_updates(offset)
    new_offset = offset
    for upd in updates:
        new_offset = upd["update_id"] + 1
        msg = upd.get("message", {})
        text = (msg.get("text") or "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if text == "/scrape" and chat_id == TELEGRAM_CHAT_ID:
            log.info("Received /scrape command")
            send_telegram("⏳ scraping, tunggu sebentar...")
            run_all_jobs()
    return new_offset


# ── 8. SCHEDULER ───────────────────────────────

def run_scheduler():
    log.info(f"Scheduler active — sending at {SEND_TIME} {TIMEZONE} daily. Listening for /scrape.")
    schedule.every().day.at(SEND_TIME).do(run_all_jobs)
    offset = 0
    while True:
        schedule.run_pending()
        offset = poll_telegram_commands(offset)
        time.sleep(5)


# ── 9. ENTRY POINT ──────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        log.info("=== TEST MODE (--now) ===")
        run_all_jobs()
    else:
        run_scheduler()
