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
from datetime import datetime
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
NEOBDM_LOGIN_URL   = "https://neobdm.tech/accounts/login/"
NEOBDM_DATA_URL    = "https://neobdm.tech/market_summary/"
NEOBDM_BROKER_URL  = "https://neobdm.tech/broker_stalker/"
NEOBDM_DASHBOARD_URL = "https://neobdm.tech/dashboard/screener/"

# Dashboard "Top Akum" presets to pull (label, dropdown value, emoji). The
# dashboard table is a Tabulator grid (#table-custom); rows keyed by
# tabulator-field: tick, price, chg, history (5d flow), tx (=%M, rank metric).
DASHBOARD_PRESETS = [
    ("Bandarmologi", "neobdm-m-d",  "🏦"),
    ("NonRetail",    "neobdm-nr-d", "🏢"),
    ("Foreign",      "neobdm-f-d",  "🌏"),
]

TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = _require_env("TELEGRAM_CHAT_ID")

TIMEZONE  = "Asia/Kuala_Lumpur"
SEND_TIME = "07:00"

# Retail-dominated brokers. Their combined net sell is the retail-distribution
# signal we screen for. Note: YP/PD also appear in the bandar groups below —
# contested codes are intentionally kept on both sides (per user choice), so a
# stock can show retail selling and bandar buying through the same code.
RETAIL_BROKERS = ["XL", "XC", "YP", "PD"]

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
    "VIVA": "Bakrie", "JGLE": "Bakrie", "BTEL": "Bakrie", "ELTY": "Bakrie",
    "MDIA": "Bakrie", "ALJI": "Bakrie", "DEWA": "Bakrie", "BNBR": "Bakrie",
    "ENRG": "Bakrie", "VKTR": "Bakrie", "BUMI": "Bakrie", "BRMS": "Bakrie",
    # Aguan  (broker codes TBD)
    "PDPP": "Aguan", "JIHD": "Aguan", "ERAL": "Aguan", "INPC": "Aguan",
    "ERAA": "Aguan", "CBDK": "Aguan", "PANI": "Aguan",
}
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
    page.screenshot(path="login_page.png")
    log.info("Saved login_page.png")

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
        page.screenshot(path="debug_login_fail.png")
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

    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    log.info(f"After login URL: {page.url}")
    page.screenshot(path="after_login.png")

    if "login" in page.url.lower():
        log.warning("Still on login page!")
        raise RuntimeError("Login failed")
    log.info("Login successful!")


# ── 2. MARKET SUMMARY (Top 3 Akum Bandar) ────

def scrape_market_summary(page):
    log.info("Loading market summary...")
    page.goto(NEOBDM_DATA_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    try:
        page.select_option("#summary-mode", "d")
        log.info("Set summary-mode to Daily ('d')")
        page.wait_for_timeout(3000)
    except Exception as e:
        log.warning(f"Could not set #summary-mode dropdown: {e}")

    page.screenshot(path="market_page.png")
    log.info("Saved market_page.png")

    # NOTE: this is a Dash DataTable — header <th> text is rendered via CSS
    # sprites/icons (inner_text() comes back blank), and data cells live in
    # wrapper divs that inner_text() also can't see reliably. The real column
    # name is in data-dash-column, and text_content() (unlike inner_text())
    # reads it regardless of visibility. The table is also PAGINATED (~12 rows
    # per page, ~35 pages); we must walk every page or we'd only ever rank the
    # first 12 stocks — missing higher-scoring ones deeper in the list.
    rows = []
    try:
        if not page.query_selector_all("table"):
            log.warning("No tables found — check market_page.png")
            return rows

        try:
            last_page = int(page.query_selector(".last-page").text_content().strip())
        except Exception:
            last_page = 1
        log.info(f"Market summary has {last_page} page(s).")

        for _ in range(last_page):
            table = page.query_selector_all("table")[0]
            for tr in table.query_selector_all("tr")[1:]:
                tds = tr.query_selector_all("td")
                if not tds:
                    continue
                row = {}
                for td in tds:
                    col = td.get_attribute("data-dash-column")
                    if col:
                        row[col] = td.text_content().strip()
                if row:
                    # symbol cell is prefixed with a ⭐ watchlist link
                    row["symbol"] = row.get("symbol", "").replace("⭐", "").strip()
                    rows.append(row)

            nxt = page.query_selector(".next-page")
            if not nxt or nxt.get_attribute("disabled") is not None:
                break
            nxt.click()
            page.wait_for_timeout(600)

        log.info(f"Scraped {len(rows)} rows across all pages.")
    except Exception as e:
        log.error(f"Market summary scrape error: {e}")

    # Screening rules:
    #   FILTER (must-have): unusual == 'v'
    #   RANK survivors by, highest first:
    #     P1 dn-0   (today's net flow)
    #     P2 dn-3   (net flow 3 days ago)
    #     P3 likuid ('v' ranks above 'x')
    #   pinky is ignored entirely.
    #   Always return TOP_N: stocks with dn-0 >= 10 are the strong picks; if
    #   fewer than TOP_N clear that bar, fill the rest with the next-best
    #   unusual=v stocks and flag them with a caution note (_caution).
    DN0_MIN = 10
    TOP_N = 2
    def is_v(val):
        return str(val).strip().lower() == "v"

    candidates = [r for r in rows if is_v(r.get("unusual"))]
    strong = [r for r in candidates if parse_num(r.get("dn-0", "")) >= DN0_MIN]
    log.info(
        f"{len(candidates)} stock(s) pass unusual=v "
        f"({len(strong)} with dn-0>={DN0_MIN})."
    )

    candidates.sort(
        key=lambda r: (
            parse_num(r.get("dn-0", "")),
            parse_num(r.get("dn-3", "")),
            1 if is_v(r.get("likuid")) else 0,
        ),
        reverse=True,
    )
    top = candidates[:TOP_N]
    for r in top:
        r["_caution"] = parse_num(r.get("dn-0", "")) < DN0_MIN
    return top


# ── 2b. DASHBOARD "TOP AKUM" PRESETS ──────────

def scrape_dashboard_presets(page):
    """Pull the Dashboard Transaksi 'Top Akum' lists (Bandarmologi / NonRetail /
    Foreign) from the Tabulator grid. Returns [(label, emoji, [rows]), ...]."""
    log.info("Loading dashboard screener...")
    page.goto(NEOBDM_DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)

    results = []
    for label, value, emoji in DASHBOARD_PRESETS:
        rows = []
        try:
            page.select_option("#preset-dropdown-custom", value)
            page.wait_for_timeout(3500)
            rows = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('#table-custom .tabulator-row').forEach(r => {
                    const c = {};
                    r.querySelectorAll('.tabulator-cell').forEach(cell => {
                        c[cell.getAttribute('tabulator-field')] = cell.textContent.trim();
                    });
                    if (c.tick) out.push(c);
                });
                return out;
            }""")
        except Exception as e:
            log.error(f"Dashboard preset {label} failed: {e}")
        log.info(f"Dashboard {label}: {[r.get('tick') for r in rows]}")
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


def scrape_broker_stalker(page):
    """Absorption signal: stocks where retail is net SELLING and the big-player
    bloc is net BUYING MORE than retail sells (strong hands soaking up supply)."""

    def safe_netflow(codes, side):
        try:
            return get_netflow(page, codes, "Today", side=side)
        except Exception as e:
            log.error(f"Scan {codes} ({side}) failed: {e}")
            return {}

    # 1) retail net SELL (dist side, negative netval)
    log.info(f"Retail net sell scan ({'+'.join(RETAIL_BROKERS)})...")
    retail_sell = safe_netflow(RETAIL_BROKERS, "dist")

    # 2) big-player net BUY (akum side, positive netval) — the absorber bloc
    log.info(f"Big-player absorber scan ({'+'.join(BIG_PLAYER_ABSORBERS)})...")
    big_buy = safe_netflow(BIG_PLAYER_ABSORBERS, "akum")

    # 3) qualify: retail selling AND big player buying MORE than retail sells
    candidates = []
    for symbol, rrow in retail_sell.items():
        sell_val = abs(parse_num(rrow.get("netval", "")))
        if sell_val <= 0:
            continue
        brow = big_buy.get(symbol)
        if not brow:
            continue
        buy_val = parse_num(brow.get("netval", ""))
        if buy_val > sell_val:  # strict: absorption exceeds distribution
            candidates.append({
                "symbol":   symbol,
                "netval":   rrow.get("netval", ""),     # retail sell
                "savg":     rrow.get("savg", ""),
                "big_buy":  buy_val,
                "sell_val": sell_val,
                "ratio":    buy_val / sell_val,
            })
    candidates.sort(key=lambda r: r["ratio"], reverse=True)
    top = candidates[:5]
    log.info(f"Absorption candidates: {[(c['symbol'], round(c['ratio'],1)) for c in top]}")

    if not top:
        return []

    # 4) attribution — scan each big-player code INDIVIDUALLY (akum) so we can
    # name the exact broker codes accumulating each stock.
    log.info("Attribution scans (per big-player code / MG / SS)...")
    code_data = {code: safe_netflow([code], "akum") for code in BIG_PLAYER_ABSORBERS}
    mg_data   = safe_netflow([WHALER_BROKER], "akum")        # whaler buying = caution
    ss_data   = safe_netflow([SMOOTH_ACCUM_BROKER], "dist")  # smooth distributing

    # owner-proxy: only scan owners of candidates that HAVE a known owner, then
    # tag only if that owning bandar is actually net-buying (real buyback).
    owners_needed = {STOCK_OWNER[r["symbol"]] for r in top if r["symbol"] in STOCK_OWNER}
    owner_data = {name: safe_netflow(BANDAR_GROUPS[name], "akum") for name in owners_needed}

    for r in top:
        symbol = r["symbol"]

        # exact broker codes accumulating this stock, by buy value (desc)
        accum = []
        for code in BIG_PLAYER_ABSORBERS:
            row = code_data.get(code, {}).get(symbol)
            val = parse_num(row.get("netval", "")) if row else 0
            if val > 0:
                accum.append((code, val))
        accum.sort(key=lambda x: x[1], reverse=True)
        r["accum_codes"] = accum

        tags = []
        # which smart money is accumulating (core TOD attribution)
        for code, note in SMART_MONEY.items():
            if any(c == code for c, _ in accum):
                tags.append(note)
        if any(c in ALGO_BIG_PLAYERS_T1 for c, _ in accum):
            tags.append("🤖 Algo big player T1 akumulasi (strong)")
        if any(c in ALGO_BIG_PLAYERS_T2 for c, _ in accum):
            tags.append("🤖 Algo big player T2 akumulasi")
        owner = STOCK_OWNER.get(symbol)
        if owner:
            orow = owner_data.get(owner, {}).get(symbol)
            if orow and parse_num(orow.get("netval", "")) > 0:
                codes = "+".join(BANDAR_GROUPS[owner])
                tags.append(f"🟢 Owner {owner} ({codes}) ikut serap — buyback!")
            else:
                tags.append(f"🏷️ Owner group: {owner} (tdk ikut serap hari ini)")
        if parse_num((mg_data.get(symbol) or {}).get("netval", "")) > 0:
            tags.append("🐋 MG (whaler) ikut beli — hati2, besok biasa dijual")
        if parse_num((ss_data.get(symbol) or {}).get("netval", "")) < 0:
            tags.append("🟡 SS distribusi halus — masih ada waktu exit")

        r["analisa"] = (
            f"Retail jual {r['sell_val']:.1f}, big player serap {r['big_buy']:.1f} "
            f"({r['ratio']:.1f}x) — strong hand akumulasi"
        )
        r["tags"] = tags

    return top


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
        "🕵️ Broker Stalker — Big Player Absorbing Retail (Today)",
        "Signal: retail net sell + big player net buy > retail sell",
    ]
    if not data:
        lines.append("Tidak ada sinyal hari ini — belum ada big player yang "
                     "serap ritel lebih besar dari jualan ritel.")
        return lines
    for i, row in enumerate(data, 1):
        lines.append(f"{i}. {row['symbol']} | savg: {row['savg']}\n   {row['analisa']}")
        accum = row.get("accum_codes", [])
        if accum:
            codes_str = ", ".join(f"{c} ({v:.1f})" for c, v in accum)
            lines.append(f"   🔑 Akum by: {codes_str}")
        for tag in row.get("tags", []):
            lines.append(f"   {tag}")
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

            browser.close()

        send_telegram(format_combined_message(ms_data, dash_data, bs_data))
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
