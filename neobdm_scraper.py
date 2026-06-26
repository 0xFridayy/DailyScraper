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
    #   Always return 5: stocks with dn-0 >= 10 are the strong picks; if fewer
    #   than 5 clear that bar, fill the rest with the next-best unusual=v
    #   stocks and flag them with a caution note (_caution).
    DN0_MIN = 10
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
    top5 = candidates[:5]
    for r in top5:
        r["_caution"] = parse_num(r.get("dn-0", "")) < DN0_MIN
    return top5


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


def parse_stalker_table(page):
    page.wait_for_selector("#stalker-dist-table", timeout=15000)
    trs = page.query_selector_all("#stalker-dist-table tr")
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


def get_netflow(page, codes, duration="Today"):
    page.goto(NEOBDM_BROKER_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    set_broker_codes(page, codes)
    set_duration(page, duration)
    try:
        page.click("#submit-button", timeout=5000)
    except Exception as e:
        log.warning(f"Could not click #submit-button: {e}")
    page.wait_for_timeout(4000)
    page.screenshot(path=f"broker_stalker_{'_'.join(codes)}.png")
    try:
        rows = parse_stalker_table(page)
    except Exception as e:
        log.error(f"Broker stalker table parse failed for {codes}: {e}")
        rows = []
    return {r["symbol"]: r for r in rows if r.get("symbol")}


def scrape_broker_stalker(page):
    log.info(f"Scraping broker stalker (retail {'+'.join(RETAIL_BROKERS)} net sell, Today)...")
    retail = get_netflow(page, RETAIL_BROKERS, "Today")

    sells = []
    for v in retail.values():
        v = dict(v)
        v["netval_num"] = parse_num(v["netval"])
        if v["netval_num"] < 0:
            sells.append(v)
    sells.sort(key=lambda r: r["netval_num"])
    top5 = sells[:5]
    log.info(f"Top retail net sell candidates: {[r['symbol'] for r in top5]}")

    log.info("Cross-referencing bandar groups...")
    bandar_data = {}
    for name, codes in BANDAR_GROUPS.items():
        try:
            bandar_data[name] = get_netflow(page, codes, "Today")
        except Exception as e:
            log.error(f"Bandar group {name} failed: {e}")
            bandar_data[name] = {}

    # Special broker-behavior scans (smart money, whaler, smooth, algo, owner proxy).
    log.info("Scanning special brokers (smart money / MG / SS / algo)...")
    def safe_netflow(codes):
        try:
            return get_netflow(page, codes, "Today")
        except Exception as e:
            log.error(f"Special broker scan {codes} failed: {e}")
            return {}

    # smart money scanned per-broker so we can name which one is accumulating
    smart_data   = {code: safe_netflow([code]) for code in SMART_MONEY}
    mg_data      = safe_netflow([WHALER_BROKER])
    ss_data      = safe_netflow([SMOOTH_ACCUM_BROKER])
    algo_t1_data = safe_netflow(ALGO_BIG_PLAYERS_T1)
    algo_t2_data = safe_netflow(ALGO_BIG_PLAYERS_T2)

    results = []
    for r in top5:
        symbol = r["symbol"]

        # bandar accumulation (net buy)
        buyers = [
            name for name, data in bandar_data.items()
            if data.get(symbol) and parse_num(data[symbol].get("netval", "")) > 0
        ]

        # behavior tags
        tags = []
        # smart money accumulating from retail — the core TOD bullish signal (top)
        for code, note in SMART_MONEY.items():
            srow = smart_data.get(code, {}).get(symbol)
            if srow and parse_num(srow.get("netval", "")) > 0:
                tags.append(note)
        # owner-proxy ("PPR"): is the stock's OWN bandar accumulating? (buyback)
        owner = STOCK_OWNER.get(symbol)
        if owner and owner in bandar_data:
            orow = bandar_data[owner].get(symbol)
            if orow and parse_num(orow.get("netval", "")) > 0:
                codes = "+".join(BANDAR_GROUPS[owner])
                tags.append(f"🟢 PPR — {owner} ({codes}) netbuy, buyback owner, bullish")
        if parse_num((algo_t1_data.get(symbol) or {}).get("netval", "")) > 0:
            tags.append("🤖 Algo big player T1 akumulasi (strong)")
        if parse_num((algo_t2_data.get(symbol) or {}).get("netval", "")) > 0:
            tags.append("🤖 Algo big player T2 akumulasi")
        if parse_num((mg_data.get(symbol) or {}).get("netval", "")) > 0:
            tags.append("🐋 MG (whaler) top buy — hati2, besok biasa dijual")
        if parse_num((ss_data.get(symbol) or {}).get("netval", "")) < 0:
            tags.append("🟡 SS distribusi halus — masih ada waktu exit")

        # base line
        if buyers:
            analisa = f"Retail jual, {buyers[0]} akumulasi — potensi buy back"
        else:
            analisa = "Retail jual, belum ada akumulasi bandar terdeteksi"

        r["buyers"] = buyers
        r["analisa"] = analisa
        r["tags"] = tags
        results.append(r)

    return results


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

    lines = [
        "📊 NeoBDM — Top 5 Akum Bandar (Daily)",
        "Filter: unusual=v | Strong: dn-0≥10 | Rank: dn-0 > dn-3 > liquid",
        f"🕗 {now}",
        "─────────────────────",
    ]
    # display label "liquid" maps to the site's "likuid" column
    label_map = {"likuid": "liquid"}
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

    lines.append("─────────────────────")
    lines.append("neobdm.tech/market_summary/")
    return "\n".join(lines)


def format_broker_stalker_message(data):
    now = now_str()
    if not data:
        return (
            f"⚠️ NeoBDM Broker Stalker\n{now}\n\n"
            f"No data scraped today. Check screenshots."
        )

    lines = [
        f"🕵️ NeoBDM — Broker Stalker (Retail {'+'.join(RETAIL_BROKERS)} Net Sell, Today)",
        f"🕗 {now}",
        "─────────────────────",
    ]
    for i, row in enumerate(data, 1):
        lines.append(
            f"{i}. {row['symbol']} | netval: {row['netval']}  savg: {row['savg']}\n   {row['analisa']}"
        )
        for tag in row.get("tags", []):
            lines.append(f"   {tag}")

    lines.append("─────────────────────")
    lines.append("neobdm.tech/broker_stalker/")
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
            bs_data = scrape_broker_stalker(page)

            browser.close()

        send_telegram(format_market_summary_message(ms_data))
        send_telegram(format_broker_stalker_message(bs_data))
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
