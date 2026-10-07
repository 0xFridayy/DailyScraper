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
from urllib.parse import urlencode
import os
import pytz

# Pure, playwright-free helpers parked in price_audit so CI can test them.
from price_audit import (bagholders_from_payloads, inventory_date_blocks,
                         date_offset_holds, IDX_OPEN_HOUR_LOCAL)
import idx_calendar
import inventory_capture as ic
import neobdm_source_contract as nsc


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
# NeoBDM temporarily blocks "abnormal usage" (~50 rapid requests trips it), so the
# whole daily capture has to fit a small budget. We screen COMPOSITE down to the
# liquid, non-gorengan names server-side (~200 stocks = ~10 pages) and page that
# once: those rows are BOTH the Telegram Top-2 source and the ML feature capture.
API_PAGE_PAUSE    = 0.6   # seconds between page POSTs (anti-abuse)
MAX_CAPTURE_PAGES = 25    # hard ceiling (~500 names) so a mis-applied filter can
                          # never turn the capture into a full ~148-page walk

# Server-side "no gorengan" gate, mirroring the filters NeoBDM's own dashboard
# screeners use: liquid names only, excluding pinky (potensi berkepentingan
# khusus) and intraday crossing/tektok manipulation.
GORENGAN_FILTERS = [
    {"op": "=", "type": "boolean", "unit": "", "field": "is_liquid",   "value": "true"},
    {"op": "=", "type": "boolean", "unit": "", "field": "is_pinky",    "value": "false"},
    {"op": "=", "type": "boolean", "unit": "", "field": "is_crossing", "value": "false"},
]

# The screener accepts at most 15 columns (ERROR_SCREENER_004 above that) and the
# summary response returns ONLY the configured columns -- so these 15 slots ARE the
# daily feature set. Chosen to answer "does the telebot entry signal work?":
#   identity/outcome   symbol, close, high, low  (high/low give MFE/MAE per entry,
#                      so target/stop rules can be tested, not just close-to-close)
#   the three signals  m_dn_0 (akum bandar), nr_dn_0 (retail jualan), f_dn_0 (asing)
#   accumulation       m_cn_5 (5-candle cumulative), m_dn_3, top_5_buyer (who)
#   screen conditions  clean_score (is_unusual_volume RETIRED, see below)
#   confound controls  tval, market_cap_t, pct_5 (is the signal just momentum?)
# is_liquid/is_pinky/is_crossing are deliberately absent: GORENGAN_FILTERS makes
# them constant, so they would burn slots carrying zero information.
#
# 2026-09-14: NeoBDM removed is_unusual_volume from its column catalog (live probe
# 2026-09-15; evidence in neobdm_source_contract.FIELD_LIFECYCLE). It is no longer
# requested and NOT replaced: is_spike_volume_* exist but are not established as
# equivalent, and the freed slot stays empty until an explicit contract change.
# The request set lives in neobdm_source_contract so the integrity checks read
# the same contract without importing playwright.
ML_COLUMNS = list(nsc.ACTIVE_REQUEST_COLUMNS)
CAPTURE_SORT_FIELD = "symbol"   # stable order => pages don't shift mid-walk

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

# Set to 1 to store date-keyed rows even when the run is outside the pre-open
# window. Only do this if you know the offset is right for that particular run;
# the default refuses, because a wrong-dated row is worse than a missing one.
ALLOW_OFF_WINDOW = os.environ.get("NEOBDM_ALLOW_OFF_WINDOW") == "1"


def _offset_safe(what):
    """May we store rows keyed by TODAY's scrape date? Logs why not.

    The scrape itself, and the Telegram message, are fine at any hour — the
    numbers shown are real either way. What is NOT fine off-window is PERSISTING
    them under a date the rest of the pipeline reads as meaning the previous
    session (price_audit.date_offset_holds explains the invariant). So the run
    continues and still reports; only the date-keyed writes are skipped.
    """
    if date_offset_holds(datetime.now(pytz.timezone(TIMEZONE))) or ALLOW_OFF_WINDOW:
        return True
    log.error(
        f"SKIPPING {what}: this run started after IDX opened, so the screener "
        f"serves TODAY's close, not the previous session's. Storing it under "
        f"today's date would break the one-day offset every date join relies on "
        f"(HANDOFF Appendix Q). The scrape and the Telegram signal are unaffected. "
        f"Set NEOBDM_ALLOW_OFF_WINDOW=1 to override.")
    return False


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

# Broker codes to persist per-ticker flow for. Each code costs one page load
# and one submit (read_broker_flow_code: one callback answers both sides) —
# this list size drives the nightly job's runtime. Trim it if the GH Actions
# timeout gets tight.
BROKER_FLOW_CODES = sorted(set(
    RETAIL_BROKERS + STALKER_RETAIL + list(SMART_MONEY) +
    ALGO_BIG_PLAYERS_T1 + ALGO_BIG_PLAYERS_T2 +
    [WHALER_BROKER, SMOOTH_ACCUM_BROKER] +
    [c for codes in BANDAR_GROUPS.values() for c in codes]
))

# Known broker codes the live Broker Stalker "Today" source cannot serve. They
# stay in BROKER_FLOW_CODES (the intended taxonomy, also read by backfill and
# ownership code) but the live capture does not request them, so its
# all-or-nothing snapshot rule runs over BROKER_FLOW_CAPTURE_CODES. An excluded
# code's missing rows are NOT an observed zero. Re-enabling one takes a new live
# proof and an explicit change here.
BROKER_FLOW_LIVE_UNAVAILABLE = {
    "CS": ("2026-09-30: Broker Stalker Today answered two exact requests (broker.value "
           "['CS'], Today, foreign-only []) 60s apart with HTTP 500 text/html; no CS "
           "broker_flow row exists, live or backfill"),
}
BROKER_FLOW_CAPTURE_CODES = sorted(set(BROKER_FLOW_CODES) - set(BROKER_FLOW_LIVE_UNAVAILABLE))
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


def _safe_error(e, limit=120):
    """Exception text fit for the PUBLIC Actions log. A failed Playwright API
    request appends a 'Call log:' listing every request header -- X-CSRFToken and
    the NeoBDM session cookie among them -- so that part is never printed."""
    return f"{type(e).__name__}: {str(e).split('Call log:')[0].strip()[:limit]}"


def _json_of(recorder, method, endpoint, resp, schema, screener_id=None, page=None, select=None):
    """Parsed JSON of an API response. With a recorder, the response is also kept
    as immutable provenance (neobdm_source_contract.CaptureRecorder), limited to
    what `schema` allows: the fragment store is committed to a public repository.
    `select` narrows an account-scoped endpoint to the object this capture uses;
    the account's other screeners/universes are not market data."""
    if recorder is None:
        return _api_json(resp)
    return recorder.record(method, endpoint, resp, schema=schema, select=select,
                           screener_id=screener_id, page=page)


def _named(name):
    return (f"data[name={name}]", lambda o: str(o.get("name", "")).upper() == name.upper())


def _screener(sid):
    return (f"data[id={sid}|name={SCRAPER_SCREENER_NAME}]",
            lambda o: o.get("id") == sid or o.get("name") == SCRAPER_SCREENER_NAME)


def get_market_columns(req, recorder=None):
    """Field names of the live column catalog, or None if it could not be read."""
    j = _json_of(recorder, "GET", "/market-summary/columns",
                 req.get(f"{API_BASE}/market-summary/columns"), nsc.CATALOG_RESPONSE_SCHEMA)
    return nsc.catalog_field_names(j)


def get_universe_id(req, name=MARKET_UNIVERSE, recorder=None):
    j = _json_of(recorder, "GET", "/stock-universe", req.get(f"{API_BASE}/stock-universe"),
                 nsc.UNIVERSE_RESPONSE_SCHEMA, select=_named(name)) or {}
    for u in (j.get("data") or []):
        if str(u.get("name", "")).upper() == name.upper():
            return u.get("id")
    return None


def ensure_scraper_screener(req, headers, universe_id, recorder=None):
    """Find-or-create the DAILY_SCRAPER screener and point it at `universe_id`
    carrying ML_COLUMNS with GORENGAN_FILTERS applied. Returns its id, or None if
    NeoBDM rejected the config.

    The PATCH response MUST be checked: it answers HTTP 200 with success=False
    (e.g. ERROR_SCREENER_004 when more than 15 columns are sent) and silently
    keeps the previous config. That is exactly how this screener sat pinned to the
    ALL universe -- warrants included -- while every run assumed otherwise.
    """
    import json as _json
    j = _json_of(recorder, "GET", "/screeners", req.get(f"{API_BASE}/screeners"),
                 nsc.SCREENERS_RESPONSE_SCHEMA, select=_named(SCRAPER_SCREENER_NAME)) or {}
    mine = next((s for s in (j.get("data") or [])
                 if s.get("name") == SCRAPER_SCREENER_NAME), None)
    if not mine:
        r = req.post(f"{API_BASE}/screeners",
                     data=_json.dumps({"name": SCRAPER_SCREENER_NAME}), headers=headers)
        mine = (_json_of(recorder, "POST", "/screeners", r, nsc.SCREENER_RESPONSE_SCHEMA,
                         select=_named(SCRAPER_SCREENER_NAME)) or {}).get("data") or {}
        log.info(f"Created screener '{SCRAPER_SCREENER_NAME}'")
    sid = mine.get("id")
    body = {"columns": ML_COLUMNS, "filters": GORENGAN_FILTERS,
            "stock_universe_id": universe_id,
            "sort_field": CAPTURE_SORT_FIELD, "sort_direction": "asc"}
    # success=True is NOT proof the config was stored as sent: on 2026-09-14 NeoBDM
    # accepted a PATCH naming a column it had removed and silently dropped it. The
    # stored config is re-read after this call and checked (capture_market_summary).
    resp = _json_of(recorder, "PATCH", f"/screeners/{sid}",
                    req.patch(f"{API_BASE}/screeners/{sid}", data=_json.dumps(body), headers=headers),
                    nsc.SCREENER_RESPONSE_SCHEMA, screener_id=sid, select=_screener(sid)) or {}
    if not resp.get("success"):
        log.error(f"Screener config REJECTED: {resp.get('message')}")
        return None
    return sid


def _fetch_summary_pages(req, headers, sid, max_pages=None,
                         sort_field=CAPTURE_SORT_FIELD, sort_direction="asc", recorder=None):
    """Page through /market-summary/summary/{sid}, paced. Stops at last_page or
    max_pages, whichever comes first. Returns (rows, last_page, pages_fetched).
    A recorded page may store only the requested columns and tolerated aliases."""
    import json as _json
    schema = nsc.summary_response_schema(list(ML_COLUMNS) + sorted(nsc.TOLERATED_ADDITIVE_KEYS))
    rows, page_no, last, fetched = [], 1, 1, 0
    while page_no <= last and (max_pages is None or page_no <= max_pages):
        body = {"page": page_no, "size": MARKET_PAGE_SIZE,
                "sort_field": sort_field, "sort_direction": sort_direction}
        j = _json_of(recorder, "POST", f"/market-summary/summary/{sid}",
                     req.post(f"{API_BASE}/market-summary/summary/{sid}",
                              data=_json.dumps(body), headers=headers),
                     schema, screener_id=sid, page=page_no) or {}
        fetched += 1
        if not j.get("success"):
            log.error(f"Summary fetch failed (p{page_no}): {j.get('message')}")
            break
        last = (j.get("meta") or {}).get("last_page", page_no)
        d = j.get("data")
        rows.extend((d.get("data") if isinstance(d, dict) else d) or [])
        page_no += 1
        time.sleep(API_PAGE_PAUSE)
    return rows, last, fetched


def capture_market_summary(req, headers, recorder, capture_date, state):
    """The capture itself, against an authenticated request context. Fills `state`
    as it goes so a failure part-way still leaves an honest manifest. Adds two
    cheap reads to the budget: the column catalog and the stored screener config
    AFTER the PATCH."""
    state.update(requested=list(ML_COLUMNS), rows=[], last_page=None, pages_fetched=0,
                 screener_id=None, universe_id=None, catalog_fields=None, stored_columns=None)
    composite = get_universe_id(req, MARKET_UNIVERSE, recorder=recorder)
    state["universe_id"] = composite
    if not composite:
        log.error(f"Market summary: universe '{MARKET_UNIVERSE}' not found")
        return state
    sid = ensure_scraper_screener(req, headers, composite, recorder=recorder)
    state["screener_id"] = sid
    if not sid:
        return state
    state["catalog_fields"] = get_market_columns(req, recorder=recorder)
    stored = _json_of(recorder, "GET", "/screeners", req.get(f"{API_BASE}/screeners"),
                      nsc.SCREENERS_RESPONSE_SCHEMA, screener_id=sid, select=_screener(sid))
    state["stored_columns"] = nsc.stored_config_columns(stored, sid)

    rows, last, fetched = _fetch_summary_pages(req, headers, sid, max_pages=MAX_CAPTURE_PAGES,
                                               recorder=recorder)
    state.update(rows=rows, last_page=last, pages_fetched=fetched)
    log.info(f"Capture: {len(rows)} rows over {min(last, MAX_CAPTURE_PAGES)}/{last} "
             f"pages ({MARKET_UNIVERSE}, liquid + non-gorengan)")
    if last > MAX_CAPTURE_PAGES:
        log.error(f"Capture hit the page cap (last_page={last}) — the filters may "
                  f"not have applied; rows are truncated")
    return state


def fetch_market_summary(page, capture_date, recorder, state):
    """One bounded, paced walk of the gorengan-free COMPOSITE slice (~200 names,
    ~10 pages). These rows serve double duty: the Telegram Top-2 Akum pick AND the
    daily ML feature capture, so the whole job costs ~12 page POSTs plus 5 reads
    against NeoBDM's ~50-request abuse budget."""
    req, headers = _api_session(page)
    return capture_market_summary(req, headers, recorder, capture_date, state)


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


TOP_AKUM_SOURCE = "top_akum_bandar"
UNUSUAL_FIELD = "is_unusual_volume"


def screen_market_summary(rows, field_availability=None, capture_date=None, capture_id=None):
    """Top-2 Akum Bandar out of the whole captured universe, as a SignalResult.

    The screen filters on is_unusual_volume, so its status follows that field:
      RETIRED_SOURCE      the field is retired for this capture regime (today)
      SOURCE_UNAVAILABLE  capture failed, or the field is absent/null throughout
      NO_HITS             the field is healthy and no row is TRUE
      HITS                one or more TRUE rows
    A missing or null flag is UNKNOWN and never counts as "not unusual"."""
    if (nsc.lifecycle_state(UNUSUAL_FIELD, capture_date) == nsc.RETIRED
            or field_availability == nsc.RETIRED):
        entry = nsc.FIELD_LIFECYCLE.get(UNUSUAL_FIELD, {})
        detail = (f"{UNUSUAL_FIELD} RETIRED by NeoBDM (capture regime from "
                  f"{entry.get('effective_capture_date', '?')}); no replacement approved")
        log.warning(f"Top-2 Akum Bandar: {detail}")
        return nsc.SignalResult(TOP_AKUM_SOURCE, nsc.RETIRED_SOURCE, [], detail, capture_id)
    if not rows:
        return nsc.SignalResult(TOP_AKUM_SOURCE, nsc.SOURCE_UNAVAILABLE, [],
                                "no market summary rows were captured", capture_id)
    states = [nsc.flag_state(r.get(UNUSUAL_FIELD)) for r in rows]
    unknown = sum(1 for s in states if s is None)
    if field_availability == nsc.UNAVAILABLE or unknown == len(rows):
        return nsc.SignalResult(TOP_AKUM_SOURCE, nsc.SOURCE_UNAVAILABLE, [],
                                f"{UNUSUAL_FIELD} absent or null on all {len(rows)} captured rows",
                                capture_id)
    # The ranking inputs are source-backed numbers: a candidate whose m_dn_0 or
    # m_dn_3 is absent/null cannot be ranked, so it is excluded and counted rather
    # than ranked on an invented 0.0 (parse_num(None) == 0.0).
    cands, unrankable = [], 0
    for r, s in zip(rows, states):
        if s != nsc.TRUE:
            continue
        dn0, dn3 = nsc.source_number(r, "m_dn_0"), nsc.source_number(r, "m_dn_3")
        if dn0 is None or dn3 is None:
            unrankable += 1
            continue
        cands.append((dn0, dn3, r))
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    out = []
    for dn0, dn3, r in cands[:2]:
        out.append({
            "symbol":  r.get("symbol", ""),
            "unusual": "v",
            "dn-0":    round(dn0, 4),
            "dn-3":    round(dn3, 4),
            "likuid":  "v",   # guaranteed by the server-side filter
            "price":   r.get("close"),
            "_caution": dn0 < MARKET_DN0_MIN,
        })
    detail = "; ".join(part for part in (
        f"{unknown} row(s) with unknown {UNUSUAL_FIELD} excluded" if unknown else "",
        f"{unrankable} unusual row(s) with unavailable m_dn_0/m_dn_3 excluded from ranking"
        if unrankable else "") if part)
    if unrankable and not out:
        return nsc.SignalResult(TOP_AKUM_SOURCE, nsc.SOURCE_UNAVAILABLE, [],
                                f"every unusual row lacks m_dn_0/m_dn_3 ({detail})", capture_id)
    log.info(f"{len(cands)} unusual stock(s); top {len(out)} selected"
             + (f"; {detail}" if detail else ""))
    return nsc.SignalResult(TOP_AKUM_SOURCE, nsc.HITS if out else nsc.NO_HITS, out, detail, capture_id)


def _write_capture_provenance(recorder, capture_date, persisted, state, contract):
    conn = sqlite3.connect(DB_PATH)
    try:
        nsc.ensure_schema(conn)
        nsc.annotate_retired_signal_days(conn)
        nsc.apply_source_status_corrections(conn)
        nsc.write_capture(conn, recorder, capture_date=capture_date, persisted=persisted,
                          screener_id=state.get("screener_id"), universe_id=state.get("universe_id"),
                          requested=state["requested"], catalog_fields=state.get("catalog_fields"),
                          stored_columns=state.get("stored_columns"), rows=state.get("rows") or [],
                          pages_fetched=state.get("pages_fetched") or 0,
                          last_page=state.get("last_page"), contract=contract)
    finally:
        conn.close()


def scrape_market_summary(page, capture=None):
    """API-based Market Summary: one bounded capture, persist EVERY captured row,
    return the Top-2 SignalResult. The full capture is the ML panel -- signalled
    and unsignalled names alike -- so forward returns have a control group.

    Every API response is kept as raw provenance and summarised in a capture
    manifest (requested vs catalog vs stored config vs returned keys). A missing
    required field is recorded and reported but does not discard the other fields;
    only an identity/column ambiguity blocks the market_summary_daily write.

    `capture`, if given, receives this capture's "rows" and "capture_id" for
    scrape_dashboard_presets() -- but only when row identity is unambiguous, the
    same condition that allows the market_summary_daily write."""
    capture_date = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
    recorder = nsc.CaptureRecorder(root=os.path.dirname(DB_PATH))
    state = {"requested": list(ML_COLUMNS)}
    error = None
    try:
        fetch_market_summary(page, capture_date, recorder, state)
    except Exception as e:
        error = e
        log.error(f"Market summary API failed: {_safe_error(e)}")
    rows = state.get("rows") or []
    contract = nsc.evaluate_capture_contract(state["requested"], state.get("catalog_fields"),
                                             state.get("stored_columns"), rows,
                                             capture_date=capture_date)
    for issue in contract["issues"]:
        if issue["severity"] != nsc.INFO:
            log.warning(f"source contract {issue['severity']} {issue['code']} "
                        f"{issue['field'] or ''} {issue['detail'] or ''}".rstrip())
    log.info(f"source contract status: {contract['status']} (capture {recorder.capture_id})")

    if error is not None:
        result = nsc.SignalResult(TOP_AKUM_SOURCE, nsc.SOURCE_UNAVAILABLE, [],
                                  f"market summary capture failed: {_safe_error(error)}", recorder.capture_id)
    else:
        result = screen_market_summary(rows, contract["availability"].get(UNUSUAL_FIELD),
                                       capture_date, recorder.capture_id)
    if capture is not None and rows and not contract["blocking_codes"]:
        capture.update(rows=rows, capture_id=recorder.capture_id)

    persisted = False
    try:
        if contract["blocking_codes"]:
            log.error(f"market_summary_daily NOT written: {contract['blocking_codes']} "
                      f"makes row identity or a column ambiguous (raw + manifest kept)")
        elif rows and _offset_safe("market_summary_daily persistence"):
            save_market_summary_daily(capture_date, state["requested"], rows, keep_tickers=None)
            persisted = True
    except Exception as e:
        log.error(f"market_summary_daily persistence failed: {e}")
    try:
        _write_capture_provenance(recorder, capture_date, persisted, state, contract)
    except Exception as e:
        log.error(f"capture manifest persistence failed: {e}")
    return result


# ── 2b. DASHBOARD "TOP AKUM" PRESETS ──────────

def _is_real_ticker(sym):
    """Filter out warrants/rights/derivative codes (e.g. AADIBQCQ6A) — keep plain
    <=4-letter equity tickers, matching what the dashboard lists used to show."""
    return bool(sym) and sym.isalpha() and len(sym) <= 4


def _unambiguous_rows_by_symbol(rows):
    """symbol -> row; a symbol that occurs more than once is ambiguous and left out."""
    seen, dup = {}, set()
    for r in rows or []:
        sym = r.get("symbol")
        if sym in seen:
            dup.add(sym)
        seen[sym] = r
    return {s: r for s, r in seen.items() if s and s not in dup}


def scrape_dashboard_presets(page, capture_rows=None, capture_id=None):
    """Dashboard 'Top Akum' lists via the screener API. The Bandarmologi/NonRetail/
    Foreign lists are now dashboard SCREENERS (GET /api/screeners/dashboard); we
    POST /market-summary/summary/{id} for each and take the top tickers, ranked
    server-side by the %M field. Returns [(label, emoji, SignalResult of rows with
    'tick' + 'tx'), ...] — the shape _dashboard_lines() and record_konglo_signals()
    expect.

    A screener's response only carries its own configured columns, and 'Top Akum
    Bandar' has not always carried m_dn_0 even though it sorts by it: on
    2026-09-16 every Bandarmologi value read 0.0% (parse_num's zero for a missing
    key), on 2026-09-17 the list emptied after PR #45, and on 2026-09-18 the
    field was back. A row whose %M is missing is therefore KEPT -- the ticker and its rank are
    source-backed -- and its value is read from this run's market-summary capture
    (`capture_rows`, same session, same fields; NonRetail/Foreign values matched it
    exactly on 2026-09-16), or shown as n/a if the capture lacks it. Never 0.0%.

    Status per list: SOURCE_UNAVAILABLE when the session, the request or the
    response failed (non-2xx, non-JSON, success=false, malformed data, rows with no
    symbol at all); EMPTY_UNVERIFIED when a well-formed response yields no plain
    equity ticker; HITS otherwise."""
    import json as _json

    def result(label, status, rows=(), detail="", cid=None):
        return nsc.SignalResult(f"dashboard_{label}", status, list(rows), detail, cid)

    def api_object(resp, what):
        # _api_json ignores the HTTP status, and this API also answers 200 with
        # success=false (cf. _fetch_summary_pages): neither is an empty screen.
        status = getattr(resp, "status", 200)
        if not 200 <= status < 300:
            raise ValueError(f"{what}: HTTP {status}")
        j = _api_json(resp)
        if not isinstance(j, dict):
            raise ValueError(f"{what}: response is not a JSON object")
        if j.get("success") is False:
            raise ValueError(f"{what}: API error {str(j.get('message'))[:100]}")
        return j

    try:
        req, headers = _api_session(page)
        screeners = api_object(req.get(f"{API_BASE}/screeners/dashboard"), "screeners list").get("data")
        if not isinstance(screeners, list):
            raise ValueError("screeners list: data is not a list")
        by_name = {s.get("name"): s for s in screeners if isinstance(s, dict)}
    except Exception as e:
        err = _safe_error(e)
        log.error(f"Dashboard screeners list failed: {err}")
        return [(lbl, emo, result(lbl, nsc.SOURCE_UNAVAILABLE, detail=f"screeners list failed: {err}"))
                for lbl, _s, _f, emo in DASHBOARD_PRESETS]

    captured = _unambiguous_rows_by_symbol(capture_rows)
    results = []
    for label, sname, field, emoji in DASHBOARD_PRESETS:
        s = by_name.get(sname)
        if not s:
            log.warning(f"Dashboard screener '{sname}' not found")
            results.append((label, emoji, result(label, nsc.SOURCE_UNAVAILABLE,
                                                 detail=f"dashboard screener '{sname}' not found")))
            continue
        try:
            d = api_object(req.post(
                f"{API_BASE}/market-summary/summary/{s['id']}",
                data=_json.dumps({"page": 1, "size": MARKET_PAGE_SIZE,
                                  "sort_field": field, "sort_direction": "desc"}),
                headers=headers), "summary").get("data")
            batch = d.get("data") if isinstance(d, dict) else d
            if not isinstance(batch, list):
                raise ValueError("summary: data is not a list")
            if batch and not any(isinstance(r, dict) and r.get("symbol") for r in batch):
                raise ValueError(f"summary: identity key symbol absent on {len(batch)}/{len(batch)} rows")
            rows, missing, valued, na = [], [], [], []
            for r in batch:
                sym = r.get("symbol")
                if _is_real_ticker(sym):
                    # The rank value is source-backed: absent/null is not 0.0%.
                    value = nsc.source_number(r, field)
                    if value is None:
                        missing.append(sym)
                        value = nsc.source_number(captured.get(sym, {}), field)
                        (na if value is None else valued).append(sym)
                    rows.append({"tick": sym, "tx": "n/a" if value is None else f"{value * 100:.1f}%"})
                if len(rows) >= DASH_TOP_N:
                    break
            time.sleep(API_PAGE_PAUSE)
        except Exception as e:
            err = _safe_error(e)
            log.error(f"Dashboard preset {label} failed: {err}")
            results.append((label, emoji, result(label, nsc.SOURCE_UNAVAILABLE, detail=f"request failed: {err}")))
            continue
        detail, consulted = "", bool(missing and captured)
        if missing:
            detail = f"{field} absent/null in the dashboard response for {missing}; "
            if consulted:
                detail += f"valued from market-summary capture {capture_id}: {valued}"
                detail += f"; n/a (absent/null in that capture too): {na}" if na else ""
            else:
                detail += "no usable market-summary capture this run, shown as n/a"
            log.warning(f"Dashboard {label}: {detail}")
        elif not rows:
            detail = (f"{len(batch)} row(s) returned, no plain equity ticker" if batch
                      else "screener returned no rows")
        log.info(f"Dashboard {label}: {[r['tick'] for r in rows]}")
        results.append((label, emoji, result(label, nsc.HITS if rows else nsc.EMPTY_UNVERIFIED, rows, detail,
                                             capture_id if consulted else None)))
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


def set_duration(page, label="Today", strict=False):
    """strict=False (legacy get_netflow) logs a failed click and carries on, as it
    always has. strict=True raises: the table would then show whatever duration the page
    defaulted to, which the caller must not report as `label`."""
    try:
        page.locator(f"#duration-picker label:has-text('{label}')").first.click(timeout=5000)
    except Exception as e:
        log.warning(f"Could not click duration '{label}': {e}")
        if strict:
            raise RuntimeError(f"duration '{label}' switch failed: {_safe_error(e)}") from e


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


# Strict submit contract (Broker Stalker only), pinned to the live capture of
# 2026-09-29. One submit fires exactly one relevant Dash callback:
#   POST /django_plotly_dash/app/bs_app/_dash-update-component
#   request  changedPropIds ["submit-button.n_clicks"]
#            outputs [{"id": "broker-akum-stalker", "property": "children"},
#                     {"id": "broker-dist-stalker", "property": "children"}]
#   response HTTP 200 {"multi": true, "response": {
#              "broker-akum-stalker": {"children": [<Label>, <DataTable stalker-akum-table>]},
#              "broker-dist-stalker": {"children": [<Label>, <DataTable stalker-dist-table>]}}}
# with no job / cacheKey / sideUpdate (background callback) and no Patch.
#
# With a response waiter armed BEFORE the click, the scan accepts only a
# response that is
#   POST, URL containing "_dash-update-component",
#   request body valid JSON with "submit-button.n_clicks" in changedPropIds,
#   request body whose structured `outputs` list holds exactly
#     {"id": <SIDE_CONTAINER without "#">, "property": "children"}
#     (the "output" string is not consulted: `outputs` is the proof),
# and fails closed if none arrives within STALKER_CALLBACK_TIMEOUT_MS; a
# submit-triggered callback for another component or property is ignored. The
# matched response must then prove it supplied the side table (see
# _side_response_data): HTTP 200 (204 = Dash PreventUpdate = nothing updated),
# the live multi-output body, and the side's children list holding the side's
# DataTable with a list `data`. Its contents are not compared with anything: an
# unchanged result is a valid result.
#
# That `data` IS the strict result: each entry becomes the row dict
# parse_stalker_table returns (see _stalker_rows_from_response), and every entry
# must carry a Markdown ticker link as its symbol (reduced to the plain ticker
# the DOM shows) and a numeric value for every column, or the scan fails
# closed. The DOM table is never read on the strict path: a stable old table
# does not prove the validated response was rendered.
DASH_CALLBACK_ENDPOINT = "_dash-update-component"
SUBMIT_TRIGGER = "submit-button.n_clicks"
SIDE_TABLE_ID = {"akum": "stalker-akum-table", "dist": "stalker-dist-table"}
DASH_BACKGROUND_KEYS = ("job", "cacheKey", "sideUpdate")
DASH_PATCH_MARKER = "__dash_patch_update"
STALKER_ROW_FIELDS = ("symbol", "netval", "bval", "sval", "bavg", "savg")
STALKER_CALLBACK_TIMEOUT_MS = 20000


def _side_output(side):
    """The structured output the live submit callback declares for `side`."""
    return {"id": SIDE_CONTAINER[side].lstrip("#"), "property": "children"}


def _submit_callback_for(side):
    """Predicate: is a response the submit-triggered Dash callback whose
    structured outputs include the `side` container's children? Never raises."""
    expected = _side_output(side)

    def is_it(response):
        import json as _json
        try:
            request = response.request
            if request.method != "POST" or DASH_CALLBACK_ENDPOINT not in response.url:
                return False
            payload = _json.loads(request.post_data or "")
            if not isinstance(payload, dict):
                return False
            changed, outputs = payload.get("changedPropIds"), payload.get("outputs")
            return (isinstance(changed, list) and SUBMIT_TRIGGER in changed
                    and isinstance(outputs, list) and expected in outputs)
        except Exception:
            return False
    return is_it


def _has_patch_marker(value):
    if isinstance(value, dict):
        return DASH_PATCH_MARKER in value or any(_has_patch_marker(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_patch_marker(v) for v in value)
    return value == DASH_PATCH_MARKER


def _side_response_data(body, side):
    """The `side` DataTable's props.data from `body` (the submit callback's parsed
    JSON). Raises ValueError unless `body` is the live multi-output response that
    supplies the `side` table: a plain (not background, not Patch) update of the
    side container's children, a list holding exactly one DataTable with the
    side's table id and a list `data`."""
    if not isinstance(body, dict):
        raise ValueError("returned no JSON object")
    background = [k for k in DASH_BACKGROUND_KEYS if k in body]
    if background:
        raise ValueError(f"is a background-callback response ({', '.join(background)})")
    if _has_patch_marker(body):
        raise ValueError("is a Patch (partial) update")
    if body.get("multi") is not True:
        raise ValueError('is not a multi-output response ("multi" is not true)')
    response = body.get("response")
    if not isinstance(response, dict):
        raise ValueError('has no "response" object')
    component = SIDE_CONTAINER[side].lstrip("#")
    update = response.get(component)
    if not isinstance(update, dict):
        raise ValueError(f"does not update {component}")
    if "children" not in update:
        raise ValueError(f"does not update {component}.children")
    children = update["children"]
    if not isinstance(children, list):
        raise ValueError(f"{component}.children is not a list")
    table_id = SIDE_TABLE_ID[side]
    tables = [c for c in children
              if isinstance(c, dict) and c.get("namespace") == "dash_table"
              and c.get("type") == "DataTable" and isinstance(c.get("props"), dict)
              and c["props"].get("id") == table_id]
    if len(tables) != 1:
        raise ValueError(f"{component}.children has {len(tables) or 'no'} {table_id} DataTable")
    data = tables[0]["props"].get("data")
    if not isinstance(data, list):
        raise ValueError(f"{table_id} props.data is not a list")
    return data


def _stalker_cell(value):
    """A response cell as the text parse_stalker_table would read from the DOM.
    Raises ValueError for anything that is not a finite number: parse_num reads
    None, "", "-" or "N/A" as 0, which would invent a value."""
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{value!r} is not a number")
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text.replace(",", "").replace("(", "-").replace(")", ""))
        except ValueError:
            raise ValueError(f"{value!r} is not a number") from None
    else:
        number = float(value)
        text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    if not math.isfinite(number):
        raise ValueError(f"{value!r} is not a finite number")
    return text


# The symbol column is presentation="markdown": each live cell is a link
# [TICKER](/<route>/TICKER) that the DOM renders as the plain TICKER. The route
# is not part of the contract; the label and the path's last segment are.
STALKER_SYMBOL_LINK_RE = re.compile(r"\[([^\[\]]*)\]\(([^()\s\x00-\x1f\x7f]*)\)")
STALKER_TICKER_RE = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*")


def _stalker_symbol(value):
    """The canonical ticker of a response `symbol` cell: LABEL of exactly
    [LABEL](PATH), where LABEL is an uppercase ticker token and PATH is a
    site-relative /path (query and fragment ignored) whose last non-empty
    segment is LABEL. Raises ValueError otherwise; the raw link is never a ticker."""
    match = STALKER_SYMBOL_LINK_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{value!r} is not a Markdown link [TICKER](/path/TICKER)")
    label, path = match.groups()
    if not STALKER_TICKER_RE.fullmatch(label):
        raise ValueError(f"link label {label!r} is not a ticker")
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError(f"link target {path!r} is not a site-relative /path")
    segments = [s for s in re.split(r"[?#]", path)[0].split("/") if s]
    if not segments or segments[-1] != label:
        raise ValueError(f"link target {path!r} does not end in its label {label!r}")
    return label


def _stalker_rows_from_response(data, side):
    """The validated response's `side` DataTable data as parse_stalker_table rows
    ({symbol, netval, bval, sval, bavg, savg}, all text). Every entry must be an
    object whose symbol is the live Markdown ticker link (see _stalker_symbol)
    and whose other columns are numbers; otherwise ValueError, never a guessed
    zero, blank or raw link."""
    table_id = SIDE_TABLE_ID[side]
    rows = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"{table_id} row {i} is not an object")
        missing = [f for f in STALKER_ROW_FIELDS if f not in entry]
        if missing:
            raise ValueError(f"{table_id} row {i} lacks {', '.join(missing)}")
        try:
            row = {"symbol": _stalker_symbol(entry["symbol"])}
        except ValueError as e:
            raise ValueError(f"{table_id} row {i} symbol: {e}") from None
        for field in STALKER_ROW_FIELDS[1:]:
            try:
                row[field] = _stalker_cell(entry[field])
            except ValueError as e:
                raise ValueError(f"{table_id} row {i} {field}: {e}") from None
        rows.append(row)
    return rows


def _submit_and_confirm_callback(page, side, timeout_ms=STALKER_CALLBACK_TIMEOUT_MS):
    """Click #submit-button with a response waiter already armed, require the
    submit-triggered Dash callback that outputs the `side` container's children
    to complete successfully and supply the side table, and return that table's
    rows (parse_stalker_table's row shape). Raises otherwise."""
    component_id = SIDE_CONTAINER[side].lstrip("#")
    try:
        with page.expect_response(_submit_callback_for(side), timeout=timeout_ms) as info:
            try:
                page.click("#submit-button", timeout=5000)
            except Exception as e:
                log.warning(f"Could not click #submit-button: {e}")
                raise RuntimeError(f"submit failed: {_safe_error(e)}") from e
        response = info.value
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"no {SUBMIT_TRIGGER} Dash callback for {component_id} within "
                           f"{timeout_ms // 1000}s of submit: {_safe_error(e)}") from e
    if response.status != 200:
        raise RuntimeError(f"{SUBMIT_TRIGGER} Dash callback failed: HTTP {response.status}")
    try:
        data = _side_response_data(_api_json(response), side)
        return _stalker_rows_from_response(data, side)
    except ValueError as e:
        raise RuntimeError(f"{SUBMIT_TRIGGER} Dash callback response {e}") from e


def get_netflow(page, codes, duration="Today", side="dist", strict=False):
    """symbol -> row of the broker_stalker `side` table for `codes` over `duration`.

    strict=False is the legacy DOM path and is unchanged (broker_flow no longer
    uses it: see read_broker_flow_code): a failed duration
    switch or submit click is logged and ignored, a fixed 4s wait follows the
    submit, the rows are parsed from the DOM table, and a failed parse returns
    {}. strict=True (Broker Stalker) raises on a failed duration switch or
    submit instead, and takes its rows from the submit contract above (the
    validated submit-button Dash callback response for the `side` table), never
    from the DOM, so neither a technical failure, a submit the server never
    answered for this table, nor a stale rendered table can be read as this
    query's result."""
    page.goto(NEOBDM_BROKER_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    set_broker_codes(page, codes)
    set_duration(page, duration, strict=strict)
    if strict:
        rows = _submit_and_confirm_callback(page, side)
    else:
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

INVENTORY_CHART_URL = "https://neobdm.tech/inventory-chart/"
INVENTORY_API = f"{API_BASE}/inventory"

# Resolve the intended ~3-month concept as exchange sessions, not a renamed
# calendar-day constant.
BAGHOLDER_DISCOVERY_CALENDAR_DAYS = 120
BAGHOLDER_TRADING_DAYS = 60
BAGHOLDER_BLOCK_TRADING_DAYS = 20

# Which brokers the endpoint resolves. This is the site's OWN request grammar and
# the exact pair /inventory-chart/ sends, so it is guaranteed accepted — no risk
# of a 400 burning a run. It resolves to the 5 largest net buyers + 5 largest net
# sellers BY LOT over the last 20 candles, then returns their per-day flow across
# its requested window. We apply it to three separate 20-session blocks and
# aggregate them, instead of inventing an unverified C60/ALL selector. A broker
# selected two months ago remains observable even if it later stopped buying.
# This is still not beneficial ownership or complete custody coverage.
BAGHOLDER_BROKERS = ["TOP_5_NB_LOT_C20", "TOP_5_NS_LOT_C20"]


def _inventory_window(days=BAGHOLDER_DISCOVERY_CALENDAR_DAYS):
    end = datetime.now(pytz.timezone(TIMEZONE)).date()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


def _bagholder_captures():
    """The capture manifest of the bag-holder lookups (inventory_capture). They
    keep no cache file, so it sits under the repository root's
    _capture_manifest/, and every result carries cache_ref null."""
    return ic.CaptureLog(ic.NO_CACHE_ROOT, "neobdm_scraper", writes_cache=False,
                         mode="bagholders",
                         broker_list_source="neobdm_scraper.BAGHOLDER_BROKERS")


def get_inventory_bagholders(page, ticker, n=STALKER_BUYERS, captures=None):
    """Top-n observable accumulators over ~60 trading sessions.

    This is broker-level observable inventory, not beneficial ownership.

    Every request (the discovery window, then each block) is recorded in
    `captures` (inventory_capture.CaptureLog; a new one when None) before it
    is sent: the two selectors as selectors, the brokers they resolved to, the
    session range. A refused response is recorded as refused where it is
    refused. An accepted one is recorded only once it has been used: the
    discovery is OK once it defined the blocks (EMPTY when it held no trading
    dates, ERROR when deriving them failed), and each block is OK once the
    ranking used it (ERROR when the ranking failed, ABORTED when a later block
    failed, so it was never ranked). Recording changes nothing that is fetched
    or ranked.

    REWRITTEN OFF THE RETIRED PAGE. This used to drive /inventory/ — a
    react-select dropdown, a #submit-button and a Plotly chart read out of the
    DOM. NeoBDM retired that page (HANDOFF Appendices H/I/M), so every lookup
    either found no elements or threw, the caller swallowed it into `holders =
    []`, and the Telegram signal printed "Bag holder: -" every single day. The
    feature had been dead since the page went away; nothing failed loudly because
    an empty list formats as "-".

    backfill_inventory.py was migrated to the /api/inventory JSON endpoint at the
    time and this caller was left as follow-up (Appendix N, item 2). This is that
    follow-up: authenticated JSON GETs, no DOM and no render race.
    """
    if captures is None:
        captures = _bagholder_captures()

    def fetch(start_date, end_date):
        """(payload, the Capture recording it). A refusal is recorded here
        before it is raised; an accepted payload's capture is left for the
        caller to finish once the payload has been used."""
        query = [("symbol", ticker), ("start_date", start_date),
                 ("end_date", end_date), ("investor_type", "A")]
        query += [("brokers", b) for b in BAGHOLDER_BROKERS]
        qs = urlencode(query)
        cap = captures.begin(qs)
        try:
            resp = page.context.request.get(f"{INVENTORY_API}?{qs}", timeout=60000)
            payload = _api_json(resp)
            cap.response(resp.status, None, payload, ic.raw_body(resp))
            if not payload or not payload.get("success"):
                err = RuntimeError(
                    f"{ticker}: inventory API status={resp.status} "
                    f"success={(payload or {}).get('success')}")
                cap.finish(ic.source_refusal(resp.status, payload), err)
                raise err
            shown = str((payload.get("meta") or {}).get("symbol") or "").upper()
            if shown and shown != ticker.upper():
                err = RuntimeError(f"inventory API returned {shown} for requested {ticker}")
                cap.finish(ic.REJECTED, err)
                raise err
        except Exception as e:
            cap.finish(ic.ERROR, e)      # only if nothing above recorded it
            raise
        return payload, cap

    # Discover actual exchange sessions first. Each exact 20-session block uses
    # the verified C20 selector, then the observed flows are aggregated across
    # the latest ~60 trading days.
    start_date, end_date = _inventory_window()
    discovery, found = fetch(start_date, end_date)
    try:
        blocks = inventory_date_blocks(
            discovery, BAGHOLDER_TRADING_DAYS, BAGHOLDER_BLOCK_TRADING_DAYS)
    except Exception as e:
        found.finish(ic.ERROR, e)
        raise
    if not blocks:
        found.finish(ic.EMPTY, "no trading dates")
        raise RuntimeError(f"{ticker}: inventory API returned no trading dates")
    found.finish(ic.OK)                  # used: it defined the blocks
    fetched, ranking = [], False         # (payload, capture) per block, finished once ranked
    try:
        for start, end in blocks:
            fetched.append(fetch(start, end))
        ranking = True
        holders = bagholders_from_payloads([p for p, _ in fetched], n)
    except Exception as e:
        for _, cap in fetched:
            if ranking:
                cap.finish(ic.ERROR, e)
            else:
                cap.finish(ic.ABORTED, "lookup abandoned: a later block failed")
        raise
    for _, cap in fetched:
        cap.finish(ic.OK)                # used: ranked
    return holders


def _fmt_lot(v):
    av = abs(v)
    if av >= 1e6:
        return f"{v/1e6:.2f}M lot"
    if av >= 1e3:
        return f"{v/1e3:.0f}k lot"
    return f"{v:.0f} lot"


BROKER_STALKER_SOURCE = "broker_stalker"


def scrape_broker_stalker(page):
    """Retail (XL+XC) top net-sell tickers, then the top-2 'bag holders' of each
    (highest cumulative net inventory) from the inventory chart, as a SignalResult.

    The scan uses get_netflow's strict path, so the status is:
      SOURCE_UNAVAILABLE  the duration switch, submit, callback response or row
                          validation, or any other part of the scan failed
      EMPTY_UNVERIFIED    the validated response held no negative-netval row.
                          Never NO_HITS: this table cannot prove a genuine
                          zero-sell session strongly enough
      HITS                one or more retail net-sell rows"""
    def result(status, hits=(), detail=""):
        return nsc.SignalResult(BROKER_STALKER_SOURCE, status, list(hits), detail)

    # 1) retail net SELL (XL+XC, Foreign Only unchecked) -> top tickers
    log.info(f"Retail net sell scan ({'+'.join(STALKER_RETAIL)})...")
    try:
        retail_sell = get_netflow(page, STALKER_RETAIL, "Today", side="dist", strict=True)
    except Exception as e:
        err = _safe_error(e)
        log.error(f"Retail net sell scan failed: {err}")
        return result(nsc.SOURCE_UNAVAILABLE, detail=f"retail net sell scan failed: {err}")

    top = sorted(retail_sell.values(), key=lambda r: parse_num(r.get("netval", "")))
    top = [r for r in top if parse_num(r.get("netval", "")) < 0][:STALKER_TOP_N]
    log.info(f"Top retail net sell: {[(r['symbol'], r['netval']) for r in top]}")
    if not top:
        return result(nsc.EMPTY_UNVERIFIED, detail=(
            f"{len(retail_sell)} row(s) parsed, none with a negative netval" if retail_sell
            else "dist table parsed with no rows"))

    # 2) per ticker -> top bag holders from the inventory JSON API.
    # Prime the inventory path ONCE so its session cookies are set; the GETs below
    # all ride this same authenticated context, so there is no per-ticker page
    # load (the retired DOM version did one goto + an 18s wait per ticker).
    try:
        page.goto(INVENTORY_CHART_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
    except Exception as e:
        log.error(f"inventory-chart prime failed: {e}")

    results = []
    captures = _bagholder_captures()      # one manifest for this run's lookups
    for r in top:
        symbol = r["symbol"]
        # One ticker failing must not kill the whole daily signal, but the failure
        # must stay VISIBLE: printing a broken fetch as "-" is exactly how this
        # feature stayed dead for weeks after /inventory/ was retired.
        failed = False
        try:
            holders = get_inventory_bagholders(page, symbol, captures=captures)
        except Exception as e:
            log.error(f"inventory bagholders {symbol} failed: {_safe_error(e, 300)}")
            holders, failed = [], True
        log.info(f"{symbol} bag holders: {[(h['code'], round(h['cum'])) for h in holders]}")
        results.append({
            "symbol":  symbol,
            "netval":  r.get("netval", ""),
            "savg":    r.get("savg", ""),
            "holders": holders,
            "holders_failed": failed,
        })
    return result(nsc.HITS, results)


# ── 3b. PERSISTENCE (SQLite) ───────────────────
# Raw per-broker/per-ticker flow, stored as scraped — no feature engineering
# here. Feature computation (broker_concentration, retail_presence_pct, etc.)
# happens downstream once there's enough history to build/validate them.

BROKER_FLOW_DDL = """
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
"""

# Provenance of every broker_flow capture, one row per code per run (additive;
# no model reads it). status is the code's own scan (OK / SOURCE_FAILURE, the
# reason in detail); snapshot is what the RUN did to broker_flow for scrape_date:
# PERSISTED (its rows are the live snapshot), REJECTED (broker_flow untouched,
# the run's reason in detail), SUPERSEDED (a later run replaced the snapshot).
# run_started_utc is in the key so a rejected rerun never overwrites the record
# of the snapshot that is still in broker_flow. expected_session_date is the IDX
# session the run had to hold (idx_calendar.latest_idx_session_before, NULL when
# the calendar could not establish it) and calendar_version the calendar asked;
# both are NULL on rows recorded before they existed.
BROKER_FLOW_SCAN_DDL = """
    CREATE TABLE IF NOT EXISTS broker_flow_scan (
        scrape_date TEXT NOT NULL,
        broker_code TEXT NOT NULL,
        run_started_utc TEXT NOT NULL,
        status TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        session_date TEXT,
        akum_rows_returned INTEGER,
        dist_rows_returned INTEGER,
        tracked_rows INTEGER,
        method TEXT NOT NULL,
        started_utc TEXT,
        completed_utc TEXT,
        detail TEXT,
        expected_session_date TEXT,
        calendar_version TEXT,
        PRIMARY KEY (scrape_date, broker_code, run_started_utc)
    )
"""
BROKER_FLOW_SCAN_ADDED_COLUMNS = ("expected_session_date", "calendar_version")


def _ensure_broker_flow_tables(conn):
    """Create broker_flow and broker_flow_scan, and add the scan columns a table
    created before them lacks (nullable: existing rows keep NULL)."""
    conn.execute(BROKER_FLOW_DDL)
    conn.execute(BROKER_FLOW_SCAN_DDL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(broker_flow_scan)")}
    for column in BROKER_FLOW_SCAN_ADDED_COLUMNS:
        if column not in have:
            conn.execute(f"ALTER TABLE broker_flow_scan ADD COLUMN {column} TEXT")
    conn.commit()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    _ensure_broker_flow_tables(conn)
    return conn


# broker_flow capture contract (dash_callback_v1), pinned to the live probe of
# 2026-09-30 (XL, AK, IF). One submit answers BOTH sides: the matched callback's
# request carries inputs duration-picker.value "Today" and
# foreign-only-checkbox.value [] and state broker.value [code], and its response
# is the Broker Stalker multi-output body (see _side_response_data) whose side
# children are a dbc Label and the side DataTable. props.data holds the whole
# table (hundreds of rows) although the DOM renders page_size=15 of them, so the
# DOM is never read. Each Label names the session the table covers:
#   "Stalking Net Buy from 29 Sep 2026 to 29 Sep 2026"   (akum)
#   "Stalking Net Sell from 29 Sep 2026 to 29 Sep 2026"  (dist)
# That is the only source-side proof of the session, so the clock is not used.
BROKER_FLOW_METHOD = "dash_callback_v1"
BROKER_FLOW_DURATION = "Today"
FLOW_OK, FLOW_SOURCE_FAILURE = "OK", "SOURCE_FAILURE"
SNAPSHOT_PERSISTED, SNAPSHOT_REJECTED, SNAPSHOT_SUPERSEDED = "PERSISTED", "REJECTED", "SUPERSEDED"
FLOW_LABEL_NAMESPACE = "dash_bootstrap_components"
FLOW_LABEL_SIDE = {"akum": "Buy", "dist": "Sell"}
FLOW_LABEL_RE = re.compile(
    r"Stalking Net (\w+) from (\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) to (\d{1,2}) ([A-Z][a-z]{2}) (\d{4})")
# English abbreviations, independent of the process locale.
FLOW_LABEL_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_ABSENT = object()


def _broker_flow_callback(response):
    """Waiter predicate: the submit-triggered Dash callback whose structured
    outputs include BOTH side containers' children. Never raises."""
    return _submit_callback_for("akum")(response) and _submit_callback_for("dist")(response)


def _dash_values(items, section):
    """{(id, property): value} of a Dash request's `inputs` or `state` list.
    Raises ValueError on a malformed list or a repeated id.property."""
    if not isinstance(items, list):
        raise ValueError(f"request {section} is not a list")
    values = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or "property" not in item:
            raise ValueError(f"request {section} holds a malformed entry")
        key = (item["id"], item["property"])
        if key in values:
            raise ValueError(f"request {section} repeats {key[0]}.{key[1]}")
        values[key] = item.get("value", _ABSENT)
    return values


def _broker_flow_request_proof(payload, code):
    """Raise ValueError unless the matched request (parsed JSON) proves it is the
    submit for exactly `code`, Today, foreign-only unchecked, answering both
    sides. The UI clicks succeeding is not proof."""
    if not isinstance(payload, dict):
        raise ValueError("request body is not a JSON object")
    changed, outputs = payload.get("changedPropIds"), payload.get("outputs")
    if not isinstance(changed, list) or SUBMIT_TRIGGER not in changed:
        raise ValueError(f"request changedPropIds lacks {SUBMIT_TRIGGER}")
    for side in ("akum", "dist"):
        if not isinstance(outputs, list) or _side_output(side) not in outputs:
            raise ValueError(f"request outputs lack {_side_output(side)['id']}.children")
    inputs, state = _dash_values(payload.get("inputs"), "inputs"), _dash_values(payload.get("state"), "state")
    for values, section, component, want in ((inputs, "inputs", "duration-picker", BROKER_FLOW_DURATION),
                                             (inputs, "inputs", "foreign-only-checkbox", []),
                                             (state, "state", "broker", [code])):
        got = values.get((component, "value"), _ABSENT)
        if got is _ABSENT:
            raise ValueError(f"request {section} lacks {component}.value")
        if type(got) is not type(want) or got != want:
            raise ValueError(f"request {section} {component}.value is not {want!r}")


def _label_day(day, month, year):
    if month not in FLOW_LABEL_MONTHS:
        raise ValueError(f"month {month!r} is not an English month abbreviation")
    try:
        return datetime(int(year), FLOW_LABEL_MONTHS[month], int(day)).date()
    except ValueError:
        raise ValueError(f"{day} {month} {year} is not a date") from None


def _flow_label_session(text, side):
    """The one session date a `side` Label covers. Raises ValueError unless it is
    exactly "Stalking Net Buy|Sell from D Mon YYYY to D Mon YYYY" with the side's
    word and from == to."""
    match = FLOW_LABEL_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"{side} Label is not 'Stalking Net {FLOW_LABEL_SIDE[side]} from D Mon YYYY "
                         f"to D Mon YYYY'")
    word, *parts = match.groups()
    if word != FLOW_LABEL_SIDE[side]:
        raise ValueError(f"{side} Label says Net {word}, not Net {FLOW_LABEL_SIDE[side]}")
    start, end = _label_day(*parts[:3]), _label_day(*parts[3:])
    if start != end:
        raise ValueError(f"{side} Label spans {start} to {end}, not one session")
    return start


def _side_session_date(body, side):
    """The session date of `side` in a response body _side_response_data has
    already accepted: its container's children hold exactly one dbc Label."""
    component = SIDE_CONTAINER[side].lstrip("#")
    labels = [c for c in body["response"][component]["children"]
              if isinstance(c, dict) and c.get("namespace") == FLOW_LABEL_NAMESPACE
              and c.get("type") == "Label"]
    if len(labels) != 1:
        raise ValueError(f"{component}.children has {len(labels) or 'no'} Label")
    return _flow_label_session((labels[0].get("props") or {}).get("children"), side)


# The broker picker as a user sees it (dcc.Dropdown #broker, react-virtualized-select).
FLOW_PICKER_OPTION = "#broker .Select-menu-outer .VirtualizedSelectOption"
FLOW_PICKER_FOCUSED = "#broker .Select-menu-outer .VirtualizedSelectFocusedOption"
FLOW_PICKER_CHIP = "#broker .Select-value-label"
FLOW_SELECT_ATTEMPTS = 2
FLOW_FILTER_TIMEOUT_MS = 3000
FLOW_FILTER_POLL_MS = 100
FLOW_SELECT_SETTLE_MS = 1500     # > the ~1.1s debounce after which the server may write broker.value


def _picker_texts(page, selector):
    return [t.strip() for t in page.locator(selector).all_inner_texts()]


def _await_exact_filter(page, code):
    """Poll the picker menu until it shows exactly one option, `code`, and that
    option is focused, so Enter can only pick `code`. False if it has not within
    FLOW_FILTER_TIMEOUT_MS."""
    waited = 0
    while not (_picker_texts(page, FLOW_PICKER_OPTION) == [code]
               and _picker_texts(page, FLOW_PICKER_FOCUSED) == [code]):
        if waited >= FLOW_FILTER_TIMEOUT_MS:
            return False
        page.wait_for_timeout(FLOW_FILTER_POLL_MS)
        waited += FLOW_FILTER_POLL_MS
    return True


def select_broker_for_flow(page, code):
    """Select exactly `code` in the broker picker, proving every step from what a
    user sees. Raises RuntimeError after FLOW_SELECT_ATTEMPTS failed attempts.

    Live 2026-09-30: typing fires broker.search_value callbacks and ~1.1s later a
    debounce-interval callback may itself write broker.value, so add_broker_chip's
    Enter after a blind 0.8s can race it (KI was once submitted as another
    broker). Each attempt clears the chips, types `code`, waits until the menu
    shows only `code`, focused, presses Enter, lets the debounce write settle and
    requires the chips to read exactly [code]. Options are never clicked: a click
    can also pick the option drawn under the pointer after the menu redraws. The
    submitted request stays the final authority (_broker_flow_request_proof)."""
    problems = []
    for attempt in range(1, FLOW_SELECT_ATTEMPTS + 1):
        if attempt > 1:
            page.keyboard.press("Escape")
            page.wait_for_timeout(FLOW_SELECT_SETTLE_MS)
        clear_broker_chips(page)
        leftover = _picker_texts(page, FLOW_PICKER_CHIP)
        if leftover:
            problems.append(f"try {attempt}: chips {leftover} left after clearing")
            continue
        page.click("#broker .Select-control")
        page.wait_for_timeout(400)
        page.keyboard.type(code)
        if not _await_exact_filter(page, code):
            problems.append(f"try {attempt}: the menu never showed only {code!r}, focused")
            continue
        page.keyboard.press("Enter")
        page.wait_for_timeout(FLOW_SELECT_SETTLE_MS)
        chips = _picker_texts(page, FLOW_PICKER_CHIP)
        if chips == [code]:
            return
        problems.append(f"try {attempt}: chips {chips} after Enter")
    raise RuntimeError(f"broker picker never selected exactly [{code!r}]: {'; '.join(problems)}")


def read_broker_flow_code(page, code, timeout_ms=STALKER_CALLBACK_TIMEOUT_MS):
    """One broker_flow scan: one page load and ONE submit for `code`, Today.
    Returns {"session_date": date, "akum": rows, "dist": rows}, rows being every
    row of each side's props.data in parse_stalker_table's shape (validated by
    _stalker_rows_from_response). Raises RuntimeError unless the picker shows
    exactly [code] after selection (select_broker_for_flow) and again just before
    the submit, the matched callback proves its request
    (_broker_flow_request_proof, checked before its HTTP status so a failure says
    whether the request itself was exact), answers 200, supplies both side
    tables, both Labels name the same single session, no ticker repeats within a
    side and no ticker is in both sides (never deduped: a net flow has one sign)."""
    import json as _json
    page.goto(NEOBDM_BROKER_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    select_broker_for_flow(page, code)
    set_duration(page, BROKER_FLOW_DURATION, strict=True)
    chips = _picker_texts(page, FLOW_PICKER_CHIP)
    if chips != [code]:
        raise RuntimeError(f"broker picker changed before submit: chips {chips}, not [{code!r}]")
    try:
        with page.expect_response(_broker_flow_callback, timeout=timeout_ms) as info:
            try:
                page.click("#submit-button", timeout=5000)
            except Exception as e:
                raise RuntimeError(f"submit failed: {_safe_error(e)}") from e
        response = info.value
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"no {SUBMIT_TRIGGER} Dash callback for both sides within "
                           f"{timeout_ms // 1000}s of submit: {_safe_error(e)}") from e
    try:
        try:
            payload = _json.loads(response.request.post_data or "")
        except ValueError:
            raise ValueError("request body is not JSON") from None
        _broker_flow_request_proof(payload, code)
    except ValueError as e:
        raise RuntimeError(f"{SUBMIT_TRIGGER} Dash callback {e}; its HTTP {response.status} "
                           f"response was not used") from e
    if response.status != 200:
        raise RuntimeError(f"{SUBMIT_TRIGGER} Dash callback failed: HTTP {response.status} "
                           f"for the exact [{code!r}] request")
    try:
        body = _api_json(response)
        sides = {side: _stalker_rows_from_response(_side_response_data(body, side), side)
                 for side in ("akum", "dist")}
        sessions = {side: _side_session_date(body, side) for side in sides}
        if sessions["akum"] != sessions["dist"]:
            raise ValueError(f"akum Label session {sessions['akum']} != dist Label session {sessions['dist']}")
        for side, rows in sides.items():
            if len({r["symbol"] for r in rows}) != len(rows):
                raise ValueError(f"{side} table repeats a ticker")
        both = {r["symbol"] for r in sides["akum"]} & {r["symbol"] for r in sides["dist"]}
        if both:
            raise ValueError(f"{len(both)} ticker(s) are in both the akum and the dist table")
    except ValueError as e:
        raise RuntimeError(f"{SUBMIT_TRIGGER} Dash callback response {e}") from e
    return {"session_date": sessions["akum"], "akum": sides["akum"], "dist": sides["dist"]}


def _flow_number(text):
    """A validated response cell (_stalker_cell's text) as a float. Raises
    ValueError; never parse_num, which reads a missing or malformed cell as 0.0.
    An explicit source 0 stays an observed (source-rounded) 0.0."""
    import math
    if not isinstance(text, str):
        raise ValueError(f"{text!r} is not number text")
    number = float(text.replace(",", "").replace("(", "-").replace(")", ""))
    if not math.isfinite(number):
        raise ValueError(f"{text!r} is not a finite number")
    return number


def capture_broker_flow(page, tickers, codes=None):
    """Read every broker code (read_broker_flow_code) and touch no table. Returns
    {"scrape_started_at", "scrape_date", "expected_session_date",
    "calendar_version", "scans", "rows", "reason"}: one scan record per code, the
    `tickers` rows of every code that succeeded, and why the snapshot may NOT be
    persisted (None when it may). scrape_date is the MYT date the capture
    started, the live broker_flow.date convention; expected_session_date is the
    IDX session such a capture must hold (None if the calendar cannot establish
    it). `codes` defaults to BROKER_FLOW_CAPTURE_CODES: an excluded code
    (BROKER_FLOW_LIVE_UNAVAILABLE) is neither requested nor recorded, and it gets
    no rows at all, never zeros."""
    codes = list(BROKER_FLOW_CAPTURE_CODES if codes is None else codes)
    tickers = set(tickers)
    started = datetime.now(pytz.timezone(TIMEZONE))
    expected, calendar_problem = expected_source_session(started.date())
    scans, rows = [], []
    for code in codes:
        scan = {"broker_code": code, "status": FLOW_SOURCE_FAILURE, "session_date": None,
                "akum_rows_returned": None, "dist_rows_returned": None, "tracked_rows": None,
                "started_utc": nsc.utc_now(), "completed_utc": None, "detail": ""}
        try:
            got = read_broker_flow_code(page, code)
            code_rows = [{"ticker": r["symbol"], "broker_code": code,
                          **{f: _flow_number(r[f]) for f in STALKER_ROW_FIELDS[1:]}}
                         for side in ("akum", "dist") for r in got[side] if r["symbol"] in tickers]
        except Exception as e:
            scan["detail"] = _safe_error(e, 300)
            log.error(f"broker_flow scan failed for {code}: {scan['detail']}")
        else:
            scan.update(status=FLOW_OK, session_date=got["session_date"].isoformat(),
                        akum_rows_returned=len(got["akum"]), dist_rows_returned=len(got["dist"]),
                        tracked_rows=len(code_rows))
            rows.extend(code_rows)
        scan["completed_utc"] = nsc.utc_now()
        scans.append(scan)
    scrape_date = started.date().isoformat()
    return {"scrape_started_at": started, "scrape_date": scrape_date,
            "expected_session_date": expected.isoformat() if expected else None,
            "calendar_version": idx_calendar.CALENDAR_VERSION, "scans": scans, "rows": rows,
            "reason": _snapshot_rejection(scans, scrape_date, codes, expected, calendar_problem)}


def expected_source_session(scrape_date):
    """(session, None) with the IDX session a capture dated `scrape_date` must
    hold, idx_calendar.latest_idx_session_before: the latest official session
    STRICTLY before it, whatever the clock time of the capture. (None, why) if
    the calendar cannot establish it, for any reason: the caller then rejects."""
    try:
        return idx_calendar.latest_idx_session_before(scrape_date), None
    except Exception as e:
        return None, _safe_error(e, 300)


def _snapshot_rejection(scans, scrape_date, codes, expected, calendar_problem):
    """Why a capture's rows may not replace broker_flow for scrape_date, or None.
    All codes must have succeeded, on one source session, and that session must
    be exactly `expected`, the latest IDX session strictly before scrape_date
    (expected_source_session). An earlier session is stale; a later one is
    same-day (breaking the live "previous session" date convention even after
    the close) or not an IDX session per the calendar. Weekend/holiday copies of
    the last session are allowed. Without an expected session nothing is."""
    if not codes:
        return "no broker codes configured"
    failed = [s["broker_code"] for s in scans if s["status"] != FLOW_OK]
    if failed:
        return f"{len(failed)}/{len(scans)} code(s) failed: {', '.join(failed)}"
    sessions = sorted({s["session_date"] for s in scans})
    if len(sessions) != 1:
        return f"codes report different source sessions: {', '.join(sessions)}"
    session = sessions[0]
    if expected is None:
        return f"cannot establish the latest IDX session before scrape date {scrape_date}: {calendar_problem}"
    want, version = expected.isoformat(), idx_calendar.CALENDAR_VERSION
    if session >= scrape_date:
        return f"source session {session} is not before scrape date {scrape_date}"
    if session < want:
        return (f"source session {session} is stale: the latest IDX session before scrape date "
                f"{scrape_date} is {want} ({version})")
    if session > want:
        return (f"source session {session} is not an IDX session per {version} "
                f"(latest before scrape date {scrape_date} is {want})")
    return None


def _record_flow_scans(conn, capture, snapshot, reason=None):
    run = capture["scrape_started_at"].astimezone(pytz.utc).isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO broker_flow_scan (scrape_date, broker_code, run_started_utc, status, "
        "snapshot, session_date, akum_rows_returned, dist_rows_returned, tracked_rows, method, "
        "started_utc, completed_utc, detail, expected_session_date, calendar_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(capture["scrape_date"], s["broker_code"], run, s["status"], snapshot, s["session_date"],
          s["akum_rows_returned"], s["dist_rows_returned"], s["tracked_rows"], BROKER_FLOW_METHOD,
          s["started_utc"], s["completed_utc"],
          f"snapshot rejected: {reason}" if reason and s["status"] == FLOW_OK else s["detail"],
          capture["expected_session_date"], capture["calendar_version"])
         for s in capture["scans"]])


def persist_broker_flow_capture(conn, capture):
    """All or nothing. If the capture is eligible (reason None), ONE transaction
    deletes scrape_date's live rows (bval IS NOT NULL; backfill rows carry NULL
    bval and are kept), inserts the new snapshot and records the scans as
    PERSISTED (earlier PERSISTED runs of the date become SUPERSEDED). Otherwise,
    or if that transaction fails, broker_flow is not touched and the scans are
    recorded, on their own, as REJECTED with the reason. Returns True iff the
    snapshot was persisted."""
    _ensure_broker_flow_tables(conn)
    date, reason = capture["scrape_date"], capture["reason"]
    if reason is None:
        try:
            with conn:
                conn.execute("DELETE FROM broker_flow WHERE date = ? AND bval IS NOT NULL", (date,))
                conn.executemany(
                    "INSERT INTO broker_flow (date, ticker, broker_code, bval, sval, netval, bavg, savg) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(date, r["ticker"], r["broker_code"], r["bval"], r["sval"], r["netval"],
                      r["bavg"], r["savg"]) for r in capture["rows"]])
                conn.execute("UPDATE broker_flow_scan SET snapshot = ? WHERE scrape_date = ? AND snapshot = ?",
                             (SNAPSHOT_SUPERSEDED, date, SNAPSHOT_PERSISTED))
                _record_flow_scans(conn, capture, SNAPSHOT_PERSISTED)
            return True
        except Exception as e:
            reason = f"broker_flow write failed: {_safe_error(e)}"
    log.error(f"broker_flow NOT written for {date}: {reason}. Existing rows are unchanged.")
    with conn:
        _record_flow_scans(conn, capture, SNAPSHOT_REJECTED, reason)
    return False


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
    """Record EVERY ticker that appears in any of today's radar sections (Top
    Akum Bandar, Dashboard presets, Broker Stalker), with the sources that
    flagged it, so forward performance can be measured later.

    This used to gate hits on TRACKED_TICKERS, which is why four days of signals
    produced six rows: the telebot signals fire across the whole liquid market and
    the 45 konglo names are rarely among them. Validating the entry strategy needs
    every signal, so `is_tracked` is a column now rather than a filter. Forward
    prices come from the daily market_summary_daily capture (close/high/low over
    the same ~200-name universe), not from price_history, which only ever covered
    the tracked names - see
    run_konglo_watch_report() in run_ml_reports.py for the actual tracking/
    Sharpe computation. This function only detects and persists the flag;
    it does no price lookups itself (today's close for a just-flagged
    ticker won't exist in price_history yet)."""
    tracked = set(TRACKED_TICKERS)
    hits = {}  # ticker -> set of source labels

    # Source availability per family, recorded even on zero hits, so a day whose
    # source was unavailable or retired is never read as a genuine no-signal day.
    statuses = [ms_data if isinstance(ms_data, nsc.SignalResult)
                else nsc.signal_status_from_rows(TOP_AKUM_SOURCE, list(ms_data))]
    statuses += [rows if isinstance(rows, nsc.SignalResult)
                 else nsc.signal_status_from_rows(f"dashboard_{label}", rows)
                 for label, _emoji, rows in dash_data]
    # Broker Stalker's own result carries SOURCE_UNAVAILABLE vs EMPTY_UNVERIFIED and
    # the reason; recomputing it from its (empty) rows would erase both.
    statuses.append(bs_data if isinstance(bs_data, nsc.SignalResult)
                    else nsc.signal_status_from_rows(BROKER_STALKER_SOURCE, list(bs_data)))
    nsc.record_signal_source_status(conn, date_str, statuses)

    for r in ms_data:
        t = r.get("symbol")
        if t:
            hits.setdefault(t, set()).add(TOP_AKUM_SOURCE)

    for label, _emoji, rows in dash_data:
        for r in rows:
            t = r.get("tick")
            if t:
                hits.setdefault(t, set()).add(f"dashboard_{label}")

    for r in bs_data:
        t = r.get("symbol")
        if t:
            hits.setdefault(t, set()).add("broker_stalker")

    if not hits:
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS konglo_signal_watch (
            flag_date TEXT NOT NULL, ticker TEXT NOT NULL, sources TEXT,
            PRIMARY KEY (flag_date, ticker)
        )
    """)
    have = {r[1] for r in conn.execute("PRAGMA table_info(konglo_signal_watch)")}
    if "is_tracked" not in have:
        conn.execute("ALTER TABLE konglo_signal_watch ADD COLUMN is_tracked INTEGER")
    for t, sources in hits.items():
        conn.execute(
            "INSERT OR REPLACE INTO konglo_signal_watch "
            "(flag_date, ticker, sources, is_tracked) VALUES (?, ?, ?, ?)",
            (date_str, t, ",".join(sorted(sources)), 1 if t in tracked else 0),
        )
    conn.commit()
    n_tracked = sum(1 for t in hits if t in tracked)
    log.info(f"signal watch: {len(hits)} ticker(s) flagged "
             f"({n_tracked} tracked): {sorted(hits)}")


def save_daily_broker_flow(page):
    """Capture every code first (capture_broker_flow), then replace scrape_date's
    live broker_flow snapshot only if the whole capture is eligible
    (persist_broker_flow_capture); a partial or unproven capture writes nothing
    to broker_flow, only its broker_flow_scan provenance."""
    capture = capture_broker_flow(page, TRACKED_TICKERS)
    conn = init_db()
    try:
        if persist_broker_flow_capture(conn, capture):
            log.info(f"broker_flow: replaced the live snapshot for {capture['scrape_date']} with "
                     f"{len(capture['rows'])} rows (source session {capture['scans'][0]['session_date']})")
        return log_backfill_progress(conn, TRACKED_TICKERS)
    finally:
        conn.close()


# ── 4. FORMAT MESSAGES ────────────────────────

def now_str():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).strftime("%d %b %Y, %I:%M %p")


def format_market_summary_message(data):
    now = now_str()
    if not data and not isinstance(data, nsc.SignalResult):
        return (
            f"⚠️ NeoBDM Market Summary\n{now}\n\n"
            f"No data scraped today. Check screenshots."
        )

    return "\n".join(_market_summary_lines(data))


def _market_summary_lines(data, in_window=True):
    """Off-window (in_window=False) the session is unverified, so no "(Daily)" or
    "today" claim is made about it."""
    lines = [
        "📊 Top 2 Akum Bandar (Daily)" if in_window else "📊 Top 2 Akum Bandar",
        "Universe: likuid, non-gorengan | Filter: unusual=v | Rank: dn-0 > dn-3",
    ]
    if isinstance(data, nsc.SignalResult):
        if data.status == nsc.RETIRED_SOURCE:
            lines.append("⛔ Source retired: NeoBDM no longer provides is_unusual_volume "
                         "(since the 2026-09-14 capture). No replacement approved, so no "
                         "Top-2 is produced. This is not a zero-candidate day.")
            return lines
        if data.status == nsc.SOURCE_UNAVAILABLE:
            lines.append(f"⚠️ Source unavailable: {data.detail}. This is not a "
                         f"zero-candidate day.")
            return lines
        if data.status == nsc.EMPTY_UNVERIFIED:
            lines.append(f"⚠️ Empty — unverified: {data.detail or 'no rows returned'}. The source "
                         f"cannot tell no candidates from a failed capture, so this is not "
                         f"proven to be a zero-candidate day.")
            return lines
        if data.status == nsc.NO_HITS:
            lines.append("No unusual-volume candidates today (source healthy)." if in_window
                         else "No unusual-volume candidates in this capture (source healthy).")
            return lines
        data = data.hits
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
            lines.append(f"   ⚠️ caution: dn-0 < {MARKET_DN0_MIN}, akumulasi lemah"
                         + (" hari ini" if in_window else ""))
    return lines


def format_broker_stalker_message(data):
    return "\n".join(_broker_stalker_lines(data))


def _broker_stalker_lines(data):
    lines = [
        "🕵️ Broker Stalker — Retail (XL+XC) Net Sell → top 2 bag holder",
        "(observable inventory ~60 hari bursa; bukan beneficial ownership)",
    ]
    status = getattr(data, "status", None)
    if status == nsc.SOURCE_UNAVAILABLE:
        lines.append(f"⚠️ Source unavailable: {data.detail}. This is not a zero-sell day.")
        return lines
    if not data:
        # EMPTY_UNVERIFIED, or a bare empty list: the stalker table cannot prove a
        # genuine zero-sell session, so nothing here may claim one.
        detail = getattr(data, "detail", "") or "no qualifying rows"
        lines.append(f"⚠️ Empty — unverified: {detail}. Cannot be told apart from a "
                     f"failed scan; not a confirmed zero-sell day.")
        return lines
    for i, row in enumerate(data, 1):
        holders = row.get("holders", [])
        bag = ", ".join(
            f"{h['code']} {_fmt_lot(h['cum'])} cost unavailable" for h in holders
        ) or ("⚠️ gagal ambil" if row.get("holders_failed") else "tidak ada akumulator")
        lines.append(f"{i}. {row['symbol']} | retail jual {row['netval']}  savg: {row['savg']}")
        lines.append(f"   🎒 Bag holder: {bag}")
    return lines


def _dashboard_cell(rows):
    if rows:
        return " ".join(f"{r.get('tick')}({r.get('tx','')})" for r in rows)
    status, detail = getattr(rows, "status", None), getattr(rows, "detail", "")
    if status == nsc.SOURCE_UNAVAILABLE:
        return f"⚠️ unavailable: {detail}" if detail else "⚠️ unavailable"
    if status == nsc.RETIRED_SOURCE:
        return "⛔ source retired"
    if status == nsc.NO_HITS:
        return "no hits (source healthy)"
    # EMPTY_UNVERIFIED, or a bare empty list: never a bare "-", which reads as a
    # confirmed empty list.
    return f"⚠️ empty — unverified: {detail}" if detail else "⚠️ empty — unverified"


def _dashboard_lines(data, in_window=True):
    lines = ["📋 Dashboard Top Akum (EOD) — ticker (%M)" if in_window
             else "📋 Dashboard Top Akum — ticker (%M)"]
    if not data:
        lines.append("No dashboard data.")
        return lines
    for label, emoji, rows in data:
        lines.append(f"{emoji} {label}: {_dashboard_cell(rows)}")
    return lines


def report_is_pre_open(scrape_started_at, report_sources_completed_at):
    """May the report claim the pre-open basis (the latest completed session)?
    Only if every source it shows was read inside the safe morning window: the
    scrape started AND the last report source finished before IDX open, on the
    same day. A run that starts at 09:59 and finishes at 10:01 may have read
    live-session data, so it is unverified."""
    return (date_offset_holds(scrape_started_at)
            and date_offset_holds(report_sources_completed_at)
            and scrape_started_at.date() == report_sources_completed_at.date())


def _scrape_span(started, completed):
    if started.date() == completed.date():
        return f"{started:%d %b %Y, %I:%M %p} – {completed:%I:%M %p} MYT"
    return f"{started:%d %b %Y, %I:%M %p} – {completed:%d %b %Y, %I:%M %p} MYT"


def _basis_lines(scrape_started_at, report_sources_completed_at):
    """Header lines stating when the report's sources were read and what session
    they describe. No trading date is named: nothing NeoBDM returns here says
    which session it served, and the previous weekday can be a holiday."""
    lines = [f"🕗 Scraped {_scrape_span(scrape_started_at, report_sources_completed_at)}"]
    if report_is_pre_open(scrape_started_at, report_sources_completed_at):
        lines.append("📅 Basis: latest completed session served by NeoBDM (pre-open capture)")
    else:
        lines.append(f"⚠️ Scrape ran (at least partly) after the safe morning window, after "
                     f"IDX open ({IDX_OPEN_HOUR_LOCAL:02d}:00 MYT): session UNVERIFIED — may be "
                     f"intraday or today's close; not a closing-session report.")
    return lines


def format_combined_message(ms_data, dash_data, bs_data, scrape_started_at,
                            report_sources_completed_at):
    """All sections in ONE Telegram message, headed by when the report's sources
    were read and the session basis that timing supports. Unless the whole read
    was pre-open, nothing in the message claims a Daily/EOD/current session."""
    pre_open = report_is_pre_open(scrape_started_at, report_sources_completed_at)
    lines = ["📈 NeoBDM Daily Signal" if pre_open else "📈 NeoBDM Signal — Session Unverified"]
    lines += _basis_lines(scrape_started_at, report_sources_completed_at)
    lines.append("═════════════════════")
    lines += _market_summary_lines(ms_data, pre_open)
    lines.append("─────────────────────")
    lines += _dashboard_lines(dash_data, pre_open)
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
    # The report's basis is when its sources were read, not when it is sent: from
    # before scraping starts until the last report source (Broker Stalker) is
    # done. broker_flow persistence runs later and feeds no report section, so
    # it cannot move the basis. report_is_pre_open() applies the same pre-open
    # rule _offset_safe uses for persistence, to BOTH ends.
    scrape_started_at = datetime.now(pytz.timezone(TIMEZONE))
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
            capture = {}
            ms_data = scrape_market_summary(page, capture)
            dash_data = scrape_dashboard_presets(page, capture.get("rows"), capture.get("capture_id"))
            bs_data = scrape_broker_stalker(page)
            report_sources_completed_at = datetime.now(pytz.timezone(TIMEZONE))

            backfill_progress = None
            try:
                backfill_progress = save_daily_broker_flow(page)
            except Exception as e:
                log.error(f"broker_flow persistence failed: {e}")

            try:
                if _offset_safe("konglo_signal_watch tracking"):
                    date_str = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
                    watch_conn = init_db()
                    try:
                        record_konglo_signals(watch_conn, date_str, ms_data, dash_data, bs_data)
                    finally:
                        watch_conn.close()
            except Exception as e:
                log.error(f"konglo signal tracking failed: {e}")

            browser.close()

        message = format_combined_message(ms_data, dash_data, bs_data,
                                          scrape_started_at, report_sources_completed_at)
        if backfill_progress:
            message = f"{message}\n\n{backfill_progress}"
        send_telegram(message)
    except Exception as e:
        log.error(f"Job failed: {_safe_error(e, 300)}")
        try:
            send_telegram(f"NeoBDM error: {_safe_error(e, 200)}")
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
