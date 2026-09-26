"""Fetch NeoBDM's /api/inventory for the broker learning dashboard.

One authenticated GET per ticker returns a rolling year of daily per-broker
buy/sell lots and values for ALL 101 broker codes, plus OHLCV. The payload
shape is documented in harvest_inventory.py; what the rest of the pipeline
assumes about it is BROKER_LEARNING.md section 2.

This is the only network code in the broker learning pipeline, and the
network stays inside collect(). neobdm_scraper refuses to import without all
four secrets, and Playwright is a browser. Both are imported inside collect(),
so the validation, cache and watchlist code here imports with neither, and
check_ml_health.py can test it. Module scope is standard library only.

WHY IT IS STRICTER THAN harvest_inventory.py
--------------------------------------------
The harvest cache feeds research that is rebuilt offline, where
build_inventory_db finds a bad file later. Here a response goes straight into
a dashboard and an insert-only ledger that is never recomputed. So a ticker
is rejected at fetch time (spec 2.2) when:

  - success is false, or meta.symbol names a different ticker;
  - build_inventory_db.strict_ticker_frame refuses the payload. A missing
    broker row can only be read as "no trades that day" because every code was
    requested AND the frame fails closed on anything it cannot read;
  - its OHLC signature equals another ticker's from the same run. Two IDX
    names cannot share a year of dates and closes; a stale response stored
    under the wrong name is exactly how price_history got contaminated (see
    price_audit.py).

SHORT WINDOWS
-------------
The API serves a ROLLING year. Asked for dates outside it, it answers with
success and a month of data rather than an error, which is how the 2026-09-23
harvest fetched 1,040 tickers cleanly and built an empty panel (commit
4b99b18). The market-wide run therefore aborts when its first good ticker is
short, as the harvest does. A watchlist may hold a recent listing, so one short
name there proves nothing: daily mode aborts only when 3 of the first 5 are.

ZERO-SESSION RESPONSES (Amendment A2)
-------------------------------------
A delisted or long-suspended ticker answers success with an empty date axis.
That is a fact about the ticker, not a broken fetch, so it is counted as
`empty`, not `failed`: it stays in the failure-rate denominator (it was
attempted) but never in the numerator, so a universe with many dead names
cannot trip the 30% exit. It is not cached, and it never enters the
short-window probe either: a moved window answers with a SHORT year, which
short_window_abort still catches, while a dead first ticker must not abort
the whole market run. A run in which no ticker came back with data at all is
still a failure (should_fail), however it splits into empty and failed.

Empty has a ceiling of its own, though. A vendor regression that answers
success with an empty date axis for most tickers would otherwise pass as a
clean run with ~5% of the universe, and the weekly tables it wrote are keyed
by as_of and insert-only, so they could not be corrected later. More than
MAX_EMPTY_SHARE of the attempted tickers empty is therefore a failed run too
(too_many_empty). The legacy cache has 104 of 1,040 (10%).

PACING
------
NeoBDM flags "abnormal usage" at about 50 rapid requests. So collect() uses
the harvest's proven pacing, jitter, rests, exponential backoff and 120 s
throttle cooldown. It also retries HTTP >= 400 and non-JSON bodies. An expired
session answers with the HTML login page rather than JSON, so after two
non-JSON bodies in a row collect() logs in again. It does that ONCE per run:
further re-logins would only hide a real outage behind more requests.

Logs carry counts, tickers and short reasons only. A failed Playwright request
appends a 'Call log:' listing every request header, the session cookie among
them, and the Actions logs are public. So exceptions are printed through
neobdm_scraper._safe_error, which cuts that part off.
"""
import glob
import gzip
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "broker_learning_raw")
WATCHLIST = os.path.join(HERE, "broker_watchlist.json")

LOOKBACK_DAYS = 360     # the API serves a ROLLING year; see start_date()
MIN_SESSIONS = 100      # a year is ~240 sessions
PACE = 1.0              # seconds between tickers, before jitter
JITTER = 0.5
REST_EVERY = 50         # tickers
REST_FOR = 15           # seconds
MAX_RETRY = 4
NOT_A_TICKER = {"IHSG", "ISSI"}    # indices that share the 4-letter shape

MODE_DAILY = "daily"    # the watchlist, weekdays
MODE_MARKET = "market"  # the whole universe, Saturdays
MODES = (MODE_DAILY, MODE_MARKET)
DAILY_PROBE = 5         # daily mode looks at the first 5 fetched tickers...
DAILY_PROBE_SHORT = 3   # ...and aborts when this many of them are short

RELOGIN_AFTER_NON_JSON = 2
THROTTLE_COOLDOWN = 120            # seconds, as harvest_inventory
MAX_FAILURE_RATE = 0.30            # price_audit.should_fail_run threshold
MAX_EMPTY_SHARE = 0.25             # too_many_empty(): more than this empty is a broken run
REQUEST_TIMEOUT_MS = 120000
INVESTOR_TYPE = "A"                # A = all investors (foreign + domestic)

TICKER_RE = re.compile(r"^[A-Z]{4}$")
BROKER_RE = re.compile(r"^[A-Z]{2}$")

log = logging.getLogger("broker_collect")


class FetchRejected(Exception):
    """The API answered, but not with data this ticker can be used on.

    `retryable` separates "ask again" from a verdict on the payload itself.
    Ask again covers an HTTP error, a non-JSON body, or success=false, which is
    also how the throttle answers. A verdict covers a wrong symbol, an
    unreadable frame or a clone; a retry would fetch the same thing again and
    spend the request budget. `throttled` asks the caller to cool down first.
    """

    def __init__(self, msg, retryable=False, throttled=False):
        super().__init__(msg)
        self.retryable = retryable
        self.throttled = throttled


class EmptyResponse(FetchRejected):
    """A successful answer for the right ticker with zero sessions: delisted
    or suspended for the whole window. Counted as `empty`, not as a failure
    (module docstring). Never retried: asking again returns the same nothing."""


# ── dates ─────────────────────────────────────

def _utc(now=None):
    """`now` as an aware UTC datetime: the clock if None, epoch seconds, or a
    datetime (a naive one is taken as UTC, the only clock this pipeline uses)."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, bool):
        raise TypeError("now must be a datetime or epoch seconds, not bool")
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, timezone.utc)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
    raise TypeError(f"now must be a datetime or epoch seconds, not {type(now).__name__}")


def start_date(now=None):
    """LOOKBACK_DAYS before today, in UTC.

    The API's min_date_allowed is a rolling one-year window, so asking from
    just inside it is what keeps the request from ageing out (see
    harvest_inventory.start_date for the incident).
    """
    return (_utc(now) - timedelta(days=LOOKBACK_DAYS)).date().isoformat()


def end_date(now=None):
    """Today in UTC. harvest_inventory.end_date reads local time while its
    start_date reads UTC. Here both read UTC, so the window is exactly
    LOOKBACK_DAYS on every runner (CI runs in UTC)."""
    return _utc(now).date().isoformat()


def session_count(data):
    """How many sessions a response actually carried."""
    return len((data or {}).get("date") or [])


# ── inputs ────────────────────────────────────

def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_codes():
    """broker_codes.json, checked.

    Spec 2.1 reads a missing broker row as "zero trades that day" only because
    every code was requested. A malformed or duplicated list must stop the run
    rather than quietly narrow it.
    """
    codes = _read_json(os.path.join(HERE, "broker_codes.json"))
    if not isinstance(codes, list) or not codes:
        raise ValueError("broker_codes.json must be a non-empty list")
    bad = [c for c in codes if not isinstance(c, str) or not BROKER_RE.match(c)]
    if bad:
        raise ValueError(f"broker_codes.json holds malformed codes: {bad[:5]}")
    if len(set(codes)) != len(codes):
        raise ValueError("broker_codes.json holds duplicate codes")
    return codes


def load_universe():
    """all_tickers.json without the index symbols, in file order."""
    syms = _read_json(os.path.join(HERE, "all_tickers.json"))
    return [s for s in syms if s not in NOT_A_TICKER]


def _dedupe(items):
    seen = set()
    return [x for x in items if not (x in seen or seen.add(x))]


def load_watchlist(path=WATCHLIST):
    """Watchlist tickers: upper-cased, 4-letter codes only, first occurrence kept.

    The file is hand-edited, so a typo is dropped with a warning instead of
    becoming a request that can only fail. A file without a "tickers" list is
    an error: an empty watchlist would otherwise look like a quiet day.
    """
    doc = _read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("tickers"), list):
        raise ValueError(f"{os.path.basename(path)}: expected {{\"tickers\": [...]}}")
    kept, dropped = [], []
    for raw in doc["tickers"]:
        t = raw.strip().upper() if isinstance(raw, str) else None
        if t and TICKER_RE.match(t) and t not in NOT_A_TICKER:
            kept.append(t)
        else:
            dropped.append(raw)
    if dropped:
        log.warning(f"watchlist: dropped {len(dropped)} invalid entries: {dropped[:10]}")
    return _dedupe(kept)


# ── request and validation ────────────────────

def build_query(ticker, codes, sd, ed):
    """The /api/inventory query string.

    brokers MUST be repeated params (brokers=AK&brokers=BK&...). A comma-joined
    list is accepted with HTTP 200 but returns empty series (harvest_inventory).
    """
    if not codes:
        raise ValueError("no broker codes: every code must be requested (spec 2.1)")
    query = [("symbol", ticker)]
    query += [("brokers", c) for c in codes]
    query += [("start_date", sd), ("end_date", ed), ("investor_type", INVESTOR_TYPE)]
    return urlencode(query)


def _looks_throttled(text):
    text = str(text or "")
    return "abnormal" in text.lower() or "429" in text


def validate_envelope(env, ticker):
    """env["data"] if this response can be used for `ticker`, else FetchRejected.

    Spec 2.2, except the cross-ticker clone guard, which needs the whole run
    and lives in collect().
    """
    if not isinstance(env, dict):
        raise FetchRejected(f"response is {type(env).__name__}, not an object")
    if not env.get("success"):
        msg = str(env.get("message"))[:120]
        raise FetchRejected(f"success={env.get('success')!r} message={msg!r}",
                            retryable=True, throttled=_looks_throttled(msg))
    meta = env.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise FetchRejected(f"meta is {type(meta).__name__}, not an object")
    shown = str((meta or {}).get("symbol") or "").upper()
    if shown and shown != ticker.upper():
        raise FetchRejected(f"meta.symbol {shown} != requested {ticker}")
    data = env.get("data")
    if data is None:
        raise FetchRejected("no data in response")
    if not isinstance(data, dict):
        raise FetchRejected(f"data is {type(data).__name__}, not an object")
    if isinstance(data.get("date"), list) and not data["date"]:
        raise EmptyResponse("empty date axis: 0 sessions (delisted or long-suspended)")

    # pandas/pyarrow come in with it; module scope stays standard library.
    from build_inventory_db import strict_ticker_frame
    try:
        strict_ticker_frame(data, ticker)
    except Exception as e:
        raise FetchRejected(f"strict frame: {type(e).__name__}: {str(e)[:160]}") from None
    return data


def ohlc_signature(data):
    """price_audit.series_signature of the payload's OHLC: (len, first 3 dates,
    last 3 dates, first 3 closes, last 3 closes), or None without OHLC. Reusing
    the backfill's own identity keeps one definition of "the same series".
    Expects data that validate_envelope has accepted."""
    from price_audit import series_signature
    rows = (data or {}).get("ohlc") or []
    return series_signature({"x": [r.get("date") for r in rows],
                             "close": [r.get("close") for r in rows]})


def short_window_abort(session_counts, mode):
    """Should the run stop because the API's rolling window has moved?

    `session_counts` are the session counts of the successfully fetched tickers
    so far, in fetch order.
      market: the FIRST one is under MIN_SESSIONS. The universe is mostly
              year-old listings, so one short answer already means the window.
      daily:  DAILY_PROBE_SHORT or more of the first DAILY_PROBE are short. A
              watchlist can hold a new listing, so one short name is not enough.
    """
    if mode == MODE_MARKET:
        return bool(session_counts) and session_counts[0] < MIN_SESSIONS
    if mode == MODE_DAILY:
        short = sum(1 for n in session_counts[:DAILY_PROBE] if n < MIN_SESSIONS)
        return short >= DAILY_PROBE_SHORT
    raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")


# ── cache ─────────────────────────────────────

def cache_path(raw_dir, mode, ticker):
    return os.path.join(raw_dir, mode, f"{ticker}.json.gz")


def save_cached(raw_dir, mode, ticker, env):
    """Write {"fetched_utc", "meta", "data"} for one ticker.

    Written to a temp file and renamed, so an interrupted run cannot leave a
    truncated gzip behind for the next render to trip over.
    """
    path = cache_path(raw_dir, mode, ticker)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "meta": env.get("meta"), "data": env["data"]}
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.replace(tmp, path)


def _data_of(obj, ticker):
    """The data dict of a cache file: a bare data dict (the legacy
    inventory_raw/ layout) or an envelope written by save_cached()."""
    if isinstance(obj, dict) and "date" in obj:
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        shown = str((obj.get("meta") or {}).get("symbol") or "").upper()
        if shown and shown != ticker.upper():
            raise ValueError(f"cached envelope is for {shown}")
        return obj["data"]
    raise ValueError("neither a data dict nor an envelope")


def iter_cached(tickers, mode, raw_dir=RAW_DIR, legacy=False, unreadable=None):
    """Yield (ticker, data) one file at a time; see load_cached().

    The market-wide cache is ~1,000 files and a few GB as Python objects, so a
    caller that handles one ticker at a time should stream from here instead
    of holding the whole dict.

    unreadable: an optional dict that receives {ticker: reason} for every file
    that exists but cannot be read. With tickers=None the caller has no list
    to diff against, so without it a corrupt file would vanish from the
    failure count instead of counting as a failed ticker.
    """
    folder = raw_dir if legacy else os.path.join(raw_dir, mode)
    if tickers is None:
        names = [os.path.basename(p)[:-len(".json.gz")]
                 for p in sorted(glob.glob(os.path.join(folder, "*.json.gz")))]
        tickers = [t for t in names if TICKER_RE.match(t) and t not in NOT_A_TICKER]
    missing = n_unreadable = 0
    for t in tickers:
        path = os.path.join(folder, f"{t}.json.gz")
        if not os.path.exists(path):
            missing += 1
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = _data_of(json.load(f), t)
        except Exception as e:
            n_unreadable += 1
            log.warning(f"cache {t}: unreadable, skipped ({type(e).__name__}: {str(e)[:120]})")
            if unreadable is not None:
                unreadable[t] = f"unreadable cache ({type(e).__name__})"
            continue
        yield t, data
    if missing or n_unreadable:
        log.info(f"cache: {missing} missing, {n_unreadable} unreadable of {len(tickers)} requested")


def load_cached(tickers, mode, raw_dir=RAW_DIR, legacy=False):
    """{ticker: data} from the cache.

    legacy=True reads <raw_dir>/<T>.json.gz (the harvest's bare data dicts);
    otherwise <raw_dir>/<mode>/<T>.json.gz envelopes. tickers=None means every
    file present. Missing and unreadable files are left out and logged, so the
    caller sees them as absent keys. A ticker that failed its latest fetch keeps
    its previous file, so after a fetch pass collect()["ok"], not the request.
    """
    return dict(iter_cached(tickers, mode, raw_dir=raw_dir, legacy=legacy))


# ── fetch ─────────────────────────────────────

def _safe_error_local(e, limit=120):
    """Same cut as neobdm_scraper._safe_error, for runs with an injected
    request function, where neobdm_scraper is deliberately not imported."""
    return f"{type(e).__name__}: {str(e).split('Call log:')[0].strip()[:limit]}"


def _status_and_json(resp):
    """(HTTP status, parsed body, whether the body was JSON)."""
    status = int(resp.status)
    try:
        return status, json.loads(resp.text()), True
    except Exception:
        return status, None, False


def _short_window_message(accepted, mode, sd, ed):
    if mode == MODE_MARKET:
        t, n = accepted[0]
        what = f"{t} came back with {n} sessions"
    else:
        probe = accepted[:DAILY_PROBE]
        short = [f"{t}={n}" for t, n in probe if n < MIN_SESSIONS]
        what = (f"{len(short)} of the first {len(probe)} watchlist tickers came back "
                f"short ({', '.join(short)})")
    return (f"{what} for {sd}..{ed}, under the {MIN_SESSIONS} sessions this "
            f"expects. The API's rolling window has probably moved again -- "
            f"check LOOKBACK_DAYS before spending a full run.")


def _run(tickers, mode, raw_dir, sleep, now, get, relogin, safe_error):
    """The fetch loop, independent of how requests are made (see collect)."""
    codes = load_codes()
    sd, ed = start_date(now), end_date(now)
    ok, failed, sessions, empty = [], {}, {}, {}
    accepted = []          # (ticker, sessions) of successful fetches, in order
    seen = {}              # OHLC signature -> the ticker it was served for
    non_json = 0           # consecutive non-JSON bodies, across tickers
    relogged = False
    total = len(tickers)
    t_start = time.time()
    log.info(f"broker collect ({mode}): {total} tickers, {len(codes)} broker "
             f"codes, {sd}..{ed}")

    for i, t in enumerate(tickers, 1):
        qs = build_query(t, codes, sd, ed)
        env = data = reason = None
        is_empty = False
        delay = 1.0
        for attempt in range(MAX_RETRY):
            throttled = False
            try:
                status, body, is_json = _status_and_json(get(qs))
                if is_json:
                    non_json = 0
                else:
                    non_json += 1
                    if non_json >= RELOGIN_AFTER_NON_JSON and not relogged:
                        relogged, non_json = True, 0
                        log.warning(f"[{i}/{total}] {t}: {RELOGIN_AFTER_NON_JSON} "
                                    f"non-JSON bodies in a row, session probably "
                                    f"expired - logging in again (once per run)")
                        try:
                            relogin()
                        except Exception as e:
                            log.error(f"re-login failed: {safe_error(e)}")
                if status >= 400:
                    raise FetchRejected(
                        f"HTTP {status}", retryable=True,
                        throttled=status == 429 or (
                            isinstance(body, dict) and _looks_throttled(body.get("message"))))
                if not is_json:
                    raise FetchRejected("non-JSON body", retryable=True)
                data = validate_envelope(body, t)
                env = body
                break
            except FetchRejected as e:
                reason, throttled = str(e), e.throttled
                is_empty = isinstance(e, EmptyResponse)
                if not e.retryable:
                    break
            except Exception as e:
                reason = safe_error(e)
                throttled = _looks_throttled(reason)
            if attempt < MAX_RETRY - 1:
                log.info(f"[{i}/{total}] {t} retry {attempt + 1}: {reason}")
                sleep(delay)
                delay *= 3
                if throttled:
                    log.warning(f"rate limit hit - cooling down {THROTTLE_COOLDOWN}s")
                    sleep(THROTTLE_COOLDOWN)

        if data is not None:
            n = session_count(data)
            sig = ohlc_signature(data)
            if sig is not None and sig in seen:
                data, reason = None, f"OHLC identical to {seen[sig]} (cross-ticker clone)"
            elif sig is not None:
                seen[sig] = t
        if data is not None:
            accepted.append((t, n))
            if short_window_abort([c for _, c in accepted], mode):
                raise SystemExit(_short_window_message(accepted, mode, sd, ed))
            try:
                save_cached(raw_dir, mode, t, env)
            except Exception as e:
                data, reason = None, f"cache write failed: {safe_error(e)}"
        if data is not None:
            ok.append(t)
            sessions[t] = n
        elif is_empty:
            empty[t] = reason
            log.info(f"[{i}/{total}] {t}: 0 sessions (delisted or long-suspended), "
                     "counted as empty, not as a failure")
        else:
            failed[t] = reason or "no usable response"
            log.warning(f"[{i}/{total}] {t} GIVE UP: {failed[t]}")

        if i % 25 == 0:
            rate = i / max(time.time() - t_start, 1)
            eta = (total - i) / max(rate, 1e-9) / 60
            log.info(f"[{i}/{total}] ok={len(ok)} fail={len(failed)} empty={len(empty)} "
                     f"{rate * 60:.0f}/min eta {eta:.0f}min")
        if i < total:
            if i % REST_EVERY == 0:
                sleep(REST_FOR)
            sleep(PACE + random.random() * JITTER)

    log.info(f"broker collect ({mode}) DONE ok={len(ok)} fail={len(failed)} "
             f"empty={len(empty)} in {(time.time() - t_start) / 60:.1f} min")
    return {"ok": ok, "failed": failed, "sessions": sessions, "empty": empty}


def collect(tickers, mode, raw_dir=RAW_DIR, sleep=time.sleep, now=None,
            request_get=None, relogin=None):
    """Fetch, validate and cache each ticker; {"ok", "failed", "sessions", "empty"}.

    "failed" maps each failed ticker to a short, log-safe reason, and "empty"
    each ticker that answered with zero sessions (module docstring). Raises
    SystemExit when short_window_abort() fires. The failure-rate exit is
    separate (should_fail / exit_if_failed), so a caller can record the run
    before it exits.

    request_get and relogin exist for the offline tests only. request_get(qs)
    takes the query string and returns an object with .status and .text(), like
    Playwright's APIResponse; relogin() is called for the one re-login. Left as
    None, collect() logs in to NeoBDM through Playwright.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    tickers = _dedupe([str(t).strip().upper() for t in tickers])
    if not tickers:
        log.warning(f"broker collect ({mode}): no tickers requested, nothing fetched")
        return {"ok": [], "failed": {}, "sessions": {}, "empty": {}}
    if request_get is not None:
        return _run(tickers, mode, raw_dir, sleep, now, request_get,
                    relogin or (lambda: None), _safe_error_local)

    # Imported here, never at module scope: neobdm_scraper raises on import
    # without all four secrets, and nothing else in this module needs a browser.
    from neobdm_scraper import API_BASE, INVENTORY_CHART_URL, _safe_error, login
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()

            def sign_in():
                login(page)
                # Priming the chart page sets the cookies for the inventory
                # path; the GETs below ride the same context.
                page.goto(INVENTORY_CHART_URL, wait_until="domcontentloaded",
                          timeout=60000)
                page.wait_for_timeout(5000)

            # The re-login keeps the old cookies on purpose. Two HTML bodies
            # can also be a proxy error page during an outage. If login() then
            # fails, the session it leaves in place may still be valid once the
            # site recovers; a cleared jar would fail every remaining ticker.
            def get(qs):
                return ctx.request.get(f"{API_BASE}/inventory?{qs}",
                                       timeout=REQUEST_TIMEOUT_MS)

            try:
                sign_in()
            except Exception as e:
                raise RuntimeError(f"NeoBDM login failed: {_safe_error(e)}") from None
            return _run(tickers, mode, raw_dir, sleep, now, get, sign_in, _safe_error)
        finally:
            browser.close()


def too_many_empty(n_empty, n_total, max_share=MAX_EMPTY_SHARE):
    """True when more than max_share of the attempted tickers came back with
    zero sessions (module docstring: "ZERO-SESSION RESPONSES")."""
    return n_total > 0 and n_empty / n_total > max_share


def should_fail(result, max_failure_rate=MAX_FAILURE_RATE):
    """price_audit.should_fail_run over a collect() result.

    Zero-session tickers ("empty") count as attempted, in the denominator,
    but not as failures. Nothing attempted counts as a failure, and so does a
    run where nothing came back with data: an empty run must not look like a
    clean one. Nor may a mostly-empty one (too_many_empty)."""
    from price_audit import should_fail_run
    n_ok = len(result.get("ok") or [])
    n_failed = len(result.get("failed") or {})
    n_empty = len(result.get("empty") or {})
    n_total = n_ok + n_failed + n_empty
    return (should_fail_run(n_failed, n_total, max_failure_rate) or n_ok == 0
            or too_many_empty(n_empty, n_total))


def exit_if_failed(result, max_failure_rate=MAX_FAILURE_RATE):
    """SystemExit (exit status 1) when should_fail(result); otherwise None."""
    if not should_fail(result, max_failure_rate):
        return
    n_failed = len(result.get("failed") or {})
    n_empty = len(result.get("empty") or {})
    n_total = len(result.get("ok") or []) + n_failed + n_empty
    rate = n_failed / n_total if n_total else 1.0
    if too_many_empty(n_empty, n_total):
        raise SystemExit(
            f"ABORT: {n_empty}/{n_total} tickers came back with 0 sessions (limit "
            f"{MAX_EMPTY_SHARE:.0%}). Dead names are never that many -- treat this as the "
            f"fetch being broken.")
    raise SystemExit(
        f"ABORT: {n_failed}/{n_total} tickers failed ({rate:.0%}, limit "
        f"{max_failure_rate:.0%}). Not a partial outage -- treat this as the "
        f"fetch being broken.")
