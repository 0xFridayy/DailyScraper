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
                         date_offset_holds)
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


def get_inventory_bagholders(page, ticker, n=STALKER_BUYERS):
    """Top-n observable accumulators over ~60 trading sessions.

    This is broker-level observable inventory, not beneficial ownership.

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
    def fetch(start_date, end_date):
        query = [("symbol", ticker), ("start_date", start_date),
                 ("end_date", end_date), ("investor_type", "A")]
        query += [("brokers", b) for b in BAGHOLDER_BROKERS]
        resp = page.context.request.get(
            f"{INVENTORY_API}?{urlencode(query)}", timeout=60000)
        payload = _api_json(resp)
        if not payload or not payload.get("success"):
            raise RuntimeError(
                f"{ticker}: inventory API status={resp.status} "
                f"success={(payload or {}).get('success')}")
        shown = str((payload.get("meta") or {}).get("symbol") or "").upper()
        if shown and shown != ticker.upper():
            raise RuntimeError(f"inventory API returned {shown} for requested {ticker}")
        return payload

    # Discover actual exchange sessions first. Each exact 20-session block uses
    # the verified C20 selector, then the observed flows are aggregated across
    # the latest ~60 trading days.
    start_date, end_date = _inventory_window()
    discovery = fetch(start_date, end_date)
    blocks = inventory_date_blocks(
        discovery, BAGHOLDER_TRADING_DAYS, BAGHOLDER_BLOCK_TRADING_DAYS)
    if not blocks:
        raise RuntimeError(f"{ticker}: inventory API returned no trading dates")
    payloads = [fetch(start, end) for start, end in blocks]
    return bagholders_from_payloads(payloads, n)


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
    for r in top:
        symbol = r["symbol"]
        # One ticker failing must not kill the whole daily signal, but the failure
        # must stay VISIBLE: printing a broken fetch as "-" is exactly how this
        # feature stayed dead for weeks after /inventory/ was retired.
        failed = False
        try:
            holders = get_inventory_bagholders(page, symbol)
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
    statuses.append(nsc.signal_status_from_rows("broker_stalker", list(bs_data)))
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
    if not data and not isinstance(data, nsc.SignalResult):
        return (
            f"⚠️ NeoBDM Market Summary\n{now}\n\n"
            f"No data scraped today. Check screenshots."
        )

    return "\n".join(_market_summary_lines(data))


def _market_summary_lines(data):
    lines = [
        "📊 Top 2 Akum Bandar (Daily)",
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
        if data.status == nsc.NO_HITS:
            lines.append("No unusual-volume candidates today (source healthy).")
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
            lines.append(f"   ⚠️ caution: dn-0 < {MARKET_DN0_MIN}, akumulasi lemah hari ini")
    return lines


def format_broker_stalker_message(data):
    return "\n".join(_broker_stalker_lines(data))


def _broker_stalker_lines(data):
    lines = [
        "🕵️ Broker Stalker — Retail (XL+XC) Net Sell → top 2 bag holder",
        "(observable inventory ~60 hari bursa; bukan beneficial ownership)",
    ]
    if not data:
        lines.append("Tidak ada retail net sell hari ini.")
        return lines
    for i, row in enumerate(data, 1):
        holders = row.get("holders", [])
        bag = ", ".join(
            f"{h['code']} {_fmt_lot(h['cum'])} @{h.get('avg', 0):.0f}" for h in holders
        ) or ("⚠️ gagal ambil" if row.get("holders_failed") else "tidak ada akumulator")
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
        elif getattr(rows, "status", None) == nsc.SOURCE_UNAVAILABLE:
            tickers = "⚠️ unavailable"
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
            capture = {}
            ms_data = scrape_market_summary(page, capture)
            dash_data = scrape_dashboard_presets(page, capture.get("rows"), capture.get("capture_id"))
            bs_data = scrape_broker_stalker(page)

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

        message = format_combined_message(ms_data, dash_data, bs_data)
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
