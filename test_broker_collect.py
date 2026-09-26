"""Plain-script tests for broker_collect.py (py -3 test_broker_collect.py).

Offline only. collect() is driven through its injected request function with
fake responses; inventory_raw/ is read, never written. neobdm_scraper and
Playwright are blocked from importing below, so a test that forgot to inject
fails with ImportError instead of logging in with the local .env credentials.
"""

import glob
import gzip
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, parse_qsl

for _blocked in ("neobdm_scraper", "playwright", "playwright.sync_api"):
    sys.modules.setdefault(_blocked, None)

import broker_collect as bc
import inventory_capture as ic

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.join(HERE, "inventory_raw")
NOW = datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
SECRET = "sessionid=TOPSECRET123"

# The GIVE UP / cache warnings below are expected. Without a handler, logging's
# last-resort handler would print them between the "ok" lines.
bc.log.addHandler(logging.NullHandler())


# ── helpers ────────────────────────────────────────────────────────────────

def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def payload(n=120, base=1000.0, brokers=("AK", "BK"), start="2025-10-01"):
    """A small data dict that strict_ticker_frame accepts. `base` sets the
    price level, so two payloads with different bases are not clones."""
    dates = weekdays(start, n)
    data = {f: {} for f in ("blot", "bval", "slot", "sval", "nlot", "nval")}
    data["date"] = dates
    for k, b in enumerate(brokers):
        blot = [(i + k) % 7 for i in range(n)]
        slot = [(3 * i + k) % 5 for i in range(n)]
        bval = [x * 100 * base for x in blot]
        sval = [y * 100 * base for y in slot]
        data["blot"][b], data["slot"][b] = blot, slot
        data["bval"][b], data["sval"][b] = bval, sval
        data["nlot"][b] = [x - y for x, y in zip(blot, slot)]
        data["nval"][b] = [x - y for x, y in zip(bval, sval)]
    data["ohlc"] = [{"date": d, "open": base + i, "high": base + i + 5,
                     "low": base + i - 5, "close": base + i, "volume": 1000}
                    for i, d in enumerate(dates)]
    return data


def envelope(data, symbol=None, success=True):
    env = {"success": success, "data": data}
    if symbol is not None:
        env["meta"] = {"symbol": symbol}
    return env


def has_legacy(ticker="SINI"):
    return os.path.exists(os.path.join(LEGACY, f"{ticker}.json.gz"))


def legacy(ticker):
    """The ticker's real harvest payload, or a stand-in where the cache is absent.

    inventory_raw/ is gitignored, so CI (ml-health.yml) never has it. The
    stand-in is a full year with a ticker-specific price level, so two tickers
    are never clones of each other; tests that need the REAL files check
    has_legacy() themselves.
    """
    if not has_legacy(ticker):
        return payload(n=240, base=float(sum(map(ord, ticker))), brokers=("AK", "BK", "XL"))
    with gzip.open(os.path.join(LEGACY, f"{ticker}.json.gz"), "rt", encoding="utf-8") as f:
        return json.load(f)


class Resp:
    """Stands in for Playwright's APIResponse: .status and .text()."""

    def __init__(self, status=200, body=None, text=None):
        self.status = status
        self._text = text if text is not None else json.dumps(body)

    def text(self):
        return self._text


def ok(data, symbol):
    return Resp(200, envelope(data, symbol))


HTML = Resp(200, text="<!doctype html><title>Login</title>")


class FakeApi:
    """Serves a scripted list of responses per ticker and records the order of
    requests and re-logins."""

    def __init__(self, script, relogin_error=None):
        self.script = {t: list(v) for t, v in script.items()}
        self.queries, self.events = [], []
        self.relogin_error = relogin_error

    def get(self, qs):
        t = parse_qs(qs)["symbol"][0]
        self.queries.append(qs)
        self.events.append(t)
        item = self.script[t].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def relogin(self):
        self.events.append("RELOGIN")
        if self.relogin_error:
            raise self.relogin_error

    def calls(self, t=None):
        return [e for e in self.events if e != "RELOGIN" and (t is None or e == t)]


class RecordingApi(FakeApi):
    """A FakeApi that notes, at each request, whether the capture manifest
    already holds that exact request as its last line, i.e. whether it was
    recorded before it was sent. Asserting inside get() would not do: collect()
    catches the AssertionError as a failed attempt and retries."""

    def __init__(self, script, raw_dir):
        super().__init__(script)
        self.raw_dir, self.recorded_first = raw_dir, []

    def get(self, qs):
        paths = glob.glob(os.path.join(self.raw_dir, ic.MANIFEST_DIR, "*.jsonl"))
        with open(paths[-1], encoding="utf-8") as fh:
            last = json.loads(fh.read().splitlines()[-1])
        self.recorded_first.append(last["event"] == "request"
                                   and last["query_sha256"] == ic.sha256_text(qs))
        return super().get(qs)


def run(api, tickers, mode="daily", raw_dir=None, sleeps=None):
    return bc.collect(tickers, mode, raw_dir=raw_dir, sleep=(sleeps if sleeps is not None else []).append,
                      now=NOW, request_get=api.get, relogin=api.relogin)


def manifest(raw_dir):
    """Every capture recorded under raw_dir, oldest run first."""
    paths = sorted(glob.glob(os.path.join(raw_dir, ic.MANIFEST_DIR, "*.jsonl")))
    return [c for p in paths for c in ic.read_captures(p)]


class Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def capture_logs():
    h = Captured()
    bc.log.addHandler(h)
    bc.log.setLevel(logging.DEBUG)
    return h


def release_logs(h):
    bc.log.removeHandler(h)
    bc.log.setLevel(logging.NOTSET)


# ── import hygiene ─────────────────────────────────────────────────────────

def test_imports_with_no_env_and_no_heavy_modules():
    env = {k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}
    assert not any(k.startswith(("NEOBDM", "TELEGRAM")) for k in env)
    code = ("import sys, broker_collect as bc; "
            "heavy = [m for m in ('neobdm_scraper', 'playwright', 'pandas', 'numpy', "
            "'pyarrow', 'requests', 'price_audit', 'build_inventory_db') if m in sys.modules]; "
            "assert not heavy, heavy; print('ok', bc.MIN_SESSIONS)")
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "ok 100", r.stdout
    print("  ok test_imports_with_no_env_and_no_heavy_modules")


def test_collect_imports_scraper_lazily_and_only_when_needed():
    assert sys.modules.get("neobdm_scraper") is None       # still the blocker
    with tempfile.TemporaryDirectory() as tmp:
        assert bc.collect([], "daily", raw_dir=tmp) == {"ok": [], "failed": {}, "sessions": {},
                                                         "empty": {}}
        try:
            bc.collect(["BREN"], "bogus", raw_dir=tmp)
            raise AssertionError("unknown mode accepted")
        except ValueError:
            pass
        try:
            bc.collect(["BREN"], "daily", raw_dir=tmp)       # un-injected: the real path
            raise AssertionError("real path ran without importing neobdm_scraper")
        except ImportError as e:
            assert "neobdm_scraper" in str(e), e
    print("  ok test_collect_imports_scraper_lazily_and_only_when_needed")


# ── dates, inputs, query ───────────────────────────────────────────────────

def test_start_and_end_date_are_utc():
    wib = timezone(timedelta(hours=7))
    late = datetime(2026, 9, 25, 2, 0, tzinfo=wib)           # 2026-09-24T19:00Z
    assert bc.end_date(late) == "2026-09-24"                 # not the local 25th
    assert bc.start_date(late) == "2025-09-29"               # 360 days before
    assert bc.end_date(datetime(2026, 9, 24, 23, 59)) == "2026-09-24"   # naive = UTC
    assert bc.end_date(datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)) == "2026-09-25"
    # Oracle: harvest_inventory's own start_date arithmetic, in epoch seconds.
    for epoch in (late.timestamp(), 1830297600.0, 1709251199.0, 1709251200.0, 1735689599.5):
        assert bc.start_date(epoch) == time.strftime(
            "%Y-%m-%d", time.gmtime(epoch - bc.LOOKBACK_DAYS * 86400)), epoch
        assert bc.end_date(epoch) == time.strftime("%Y-%m-%d", time.gmtime(epoch)), epoch
        assert bc.start_date(datetime.fromtimestamp(epoch, wib)) == bc.start_date(epoch)
    for bad in (True, "2026-09-24"):
        try:
            bc.start_date(bad)
            raise AssertionError(f"accepted {bad!r}")
        except TypeError:
            pass
    print("  ok test_start_and_end_date_are_utc")


def test_load_codes_universe_and_seed_watchlist():
    codes = bc.load_codes()
    assert len(codes) == 101 and len(set(codes)) == 101
    uni = bc.load_universe()
    assert "IHSG" not in uni and "ISSI" not in uni and len(uni) > 1000
    wl = bc.load_watchlist()
    # The owner edits the watchlist freely, so check that it is usable, not
    # that it still equals the seed it started from.
    assert wl and len(wl) == len(set(wl)), wl
    assert set(wl) <= set(uni), sorted(set(wl) - set(uni))
    with open(bc.WATCHLIST, encoding="utf-8") as f:
        assert "edit freely; watchlist is not part of the frozen ruleset" in json.load(f)["note"]
    # The note's promise: daily mode aborts when 3 of the first 5 are short, so
    # the head of the list must be long-listed names. Only the real harvest
    # cache can say that; CI has none.
    if all(has_legacy(t) for t in wl[:bc.DAILY_PROBE]):
        for t in wl[:bc.DAILY_PROBE]:
            assert bc.session_count(legacy(t)) >= bc.MIN_SESSIONS, t
    print("  ok test_load_codes_universe_and_seed_watchlist")


def test_gitignore_keeps_caches_html_and_history_out():
    # All three hold paid NeoBDM data or its renderings; this repo is public.
    with open(os.path.join(HERE, ".gitignore"), encoding="utf-8") as f:
        lines = {line.strip() for line in f}
    for d in ("broker_learning_raw/", "broker_dashboard_out/", "broker_history/"):
        assert d in lines, d
    assert "broker_learning.db" not in lines          # the ledger IS committed
    print("  ok test_gitignore_keeps_caches_html_and_history_out")


def test_weekly_history_release_is_a_draft():
    # Amendment A2: the history export is paid data and the repo is public.
    # Only a DRAFT release keeps its assets collaborator-only, the upload
    # stays behind the owner's opt-in variable, and a release that is not a
    # draft stops the upload rather than receiving the files.
    wf = os.path.join(HERE, ".github", "workflows")
    with open(os.path.join(wf, "broker-learning-weekly.yml"), encoding="utf-8") as f:
        text = f.read()
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())   # not the comments
    assert "gh release create broker-history --draft" in code
    assert code.count("gh release create") == 1
    assert "gh release upload broker-history broker_history/*.parquet --clobber" in code
    assert "if: vars.BROKER_HISTORY_UPLOAD == 'true'" in code
    assert "GH_TOKEN: ${{ github.token }}" in code
    assert code.index("--json isDraft") < code.index("gh release upload"), \
        "the draft check must run before any upload"
    with open(os.path.join(wf, "broker-learning-daily.yml"), encoding="utf-8") as f:
        assert "gh release" not in f.read(), "only the weekly job exports history"
    print("  ok test_weekly_history_release_is_a_draft")


def test_load_watchlist_drops_bad_codes_dedupes_and_keeps_order():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "w.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"note": "x", "tickers": ["pani", " BREN ", "PANI", "XX", "BBCA1",
                                                123, None, "IHSG", "SI-N", "sini", "BREN"]}, f)
        h = capture_logs()
        try:
            assert bc.load_watchlist(p) == ["PANI", "BREN", "SINI"]
        finally:
            release_logs(h)
        assert any("dropped 6" in line for line in h.lines), h.lines
        for bad_doc in ({"note": "no list"}, {"tickers": "BREN"}, ["BREN"]):
            with open(p, "w", encoding="utf-8") as f:
                json.dump(bad_doc, f)
            try:
                bc.load_watchlist(p)
                raise AssertionError(f"accepted {bad_doc!r}")
            except ValueError:
                pass
    print("  ok test_load_watchlist_drops_bad_codes_dedupes_and_keeps_order")


def test_build_query_repeats_brokers_and_asks_for_all_investors():
    codes = bc.load_codes()
    qs = bc.build_query("SINI", codes, "2025-09-29", "2026-09-24")
    pairs = parse_qsl(qs)
    assert pairs[0] == ("symbol", "SINI")
    assert [v for k, v in pairs if k == "brokers"] == codes     # repeated, in order
    assert "," not in qs and "%2C" not in qs                   # never comma-joined
    q = parse_qs(qs)
    assert q["investor_type"] == ["A"]
    assert q["start_date"] == ["2025-09-29"] and q["end_date"] == ["2026-09-24"]
    assert set(q) == {"symbol", "brokers", "start_date", "end_date", "investor_type"}
    try:
        bc.build_query("SINI", [], "2025-09-29", "2026-09-24")
        raise AssertionError("empty broker list accepted")
    except ValueError:
        pass
    print("  ok test_build_query_repeats_brokers_and_asks_for_all_investors")


# ── validation ─────────────────────────────────────────────────────────────

def _rejects(env, ticker="AAAA", retryable=None, contains=""):
    try:
        bc.validate_envelope(env, ticker)
    except bc.FetchRejected as e:
        assert contains in str(e), (contains, str(e))
        if retryable is not None:
            assert e.retryable is retryable, (str(e), e.retryable)
        return e
    raise AssertionError(f"accepted {str(env)[:200]}")


def test_validate_envelope_accepts_good_payloads():
    data = payload()
    assert bc.validate_envelope(envelope(data, "AAAA"), "AAAA") is data
    assert bc.validate_envelope(envelope(data, "aaaa"), "AAAA") is data   # case-insensitive
    assert bc.validate_envelope(envelope(data), "AAAA") is data           # meta absent
    assert bc.validate_envelope({"success": True, "data": data, "meta": {"symbol": ""}}, "AAAA") is data
    sini = legacy("SINI")
    assert bc.validate_envelope(envelope(sini, "SINI"), "SINI") is sini
    print("  ok test_validate_envelope_accepts_good_payloads")


def test_validate_envelope_rejects_per_spec():
    data = payload()
    e = _rejects({"success": False, "message": "Abnormal usage detected"},
                 retryable=True, contains="success=False")
    assert e.throttled
    assert not _rejects({"success": False, "message": "bad symbol"}, retryable=True).throttled
    _rejects({"data": data}, retryable=True, contains="success=None")          # success missing
    _rejects(envelope(data, "BBBB"), retryable=False, contains="meta.symbol BBBB")
    _rejects({"success": True, "meta": {"symbol": "AAAA"}}, retryable=False, contains="no data")
    _rejects({"success": True, "data": None}, retryable=False, contains="no data")
    _rejects({"success": True, "data": [data]}, retryable=False, contains="list")
    _rejects({"success": True, "data": "x"}, retryable=False, contains="str")
    _rejects({"success": True, "data": data, "meta": ["AAAA"]}, retryable=False, contains="meta")
    _rejects(["not", "an", "object"], retryable=False, contains="list")
    _rejects(None, retryable=False)

    broken = payload()
    broken["nlot"]["AK"][3] += 1                                  # nlot != blot - slot
    _rejects(envelope(broken, "AAAA"), retryable=False, contains="strict frame")
    short = payload()
    short["blot"]["BK"] = short["blot"]["BK"][:-1]                # series length != dates
    _rejects(envelope(short, "AAAA"), retryable=False, contains="strict frame")
    empty = payload()
    empty["date"] = []
    e = _rejects(envelope(empty, "AAAA"), retryable=False, contains="empty date axis")
    assert isinstance(e, bc.EmptyResponse), "zero sessions is `empty`, not a failure"
    partial = payload()
    del partial["sval"]["BK"]                                     # partially present broker
    _rejects(envelope(partial, "AAAA"), retryable=False, contains="strict frame")
    print("  ok test_validate_envelope_rejects_per_spec")


def test_ohlc_signature_and_short_window_rules():
    a, b = payload(base=1000.0), payload(base=2000.0)
    assert bc.ohlc_signature(a) == bc.ohlc_signature(payload(base=1000.0))
    assert bc.ohlc_signature(a) != bc.ohlc_signature(b)
    assert bc.ohlc_signature({"date": [], "ohlc": []}) is None
    sig = bc.ohlc_signature(a)
    assert sig[0] == 120 and len(sig[1]) == 3 and sig[3] == (1000.0, 1001.0, 1002.0)

    m, d = "market", "daily"
    assert not bc.short_window_abort([], m) and not bc.short_window_abort([], d)
    assert bc.short_window_abort([99], m) and bc.short_window_abort([99, 240], m)
    assert not bc.short_window_abort([100], m)
    assert not bc.short_window_abort([240, 5, 5, 5], m)      # only the first counts
    assert not bc.short_window_abort([50, 50], d)
    assert bc.short_window_abort([50, 50, 50], d)
    assert bc.short_window_abort([50, 240, 50, 240, 50], d)
    assert not bc.short_window_abort([50, 240, 50, 240, 240, 50, 50], d)   # first 5 only
    assert not bc.short_window_abort([99, 100, 99, 100, 100], d)
    try:
        bc.short_window_abort([50], "weekly")
        raise AssertionError("unknown mode accepted")
    except ValueError:
        pass
    print("  ok test_ohlc_signature_and_short_window_rules")


# ── collect() ──────────────────────────────────────────────────────────────

def test_collect_happy_path_real_payloads_query_cache_and_pacing():
    names = ["SINI", "BREN", "PANI"]
    api = FakeApi({t: [ok(legacy(t), t)] for t in names})
    sleeps = []
    old = bc.REST_EVERY
    bc.REST_EVERY = 2
    try:
        with tempfile.TemporaryDirectory() as tmp:
            res = run(api, [" sini", "BREN", "PANI", "SINI"], raw_dir=tmp, sleeps=sleeps)
            assert res["ok"] == names and res["failed"] == {}, res
            assert res["sessions"] == {t: len(legacy(t)["date"]) for t in names}
            assert api.calls() == names                               # deduped, one GET each
            for t in names:
                assert os.path.exists(bc.cache_path(tmp, "daily", t))
    finally:
        bc.REST_EVERY = old
    codes = bc.load_codes()
    for qs in api.queries:
        q = parse_qs(qs)
        assert q["brokers"] == codes and q["investor_type"] == ["A"]
        assert q["start_date"] == ["2025-09-29"] and q["end_date"] == ["2026-09-24"]
    # after ticker 1: pace; after 2: rest + pace; after the last: nothing
    assert sleeps.count(bc.REST_FOR) == 1 and len(sleeps) == 3, sleeps
    pace = [s for s in sleeps if s != bc.REST_FOR]
    assert all(bc.PACE <= s < bc.PACE + bc.JITTER for s in pace), sleeps
    print("  ok test_collect_happy_path_real_payloads_query_cache_and_pacing")


def test_collect_retries_5xx_and_non_json_then_succeeds():
    api = FakeApi({"AAAA": [Resp(503, {"success": False, "message": "busy"}),
                            HTML,
                            Resp(502, text="<html>Bad gateway</html>"),
                            ok(payload(), "AAAA")]})
    sleeps = []
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA"], raw_dir=tmp, sleeps=sleeps)
    assert res["ok"] == ["AAAA"], res
    assert api.calls() == ["AAAA"] * 4
    assert sleeps == [1.0, 3.0, 9.0], sleeps             # exponential backoff, no cooldown
    # 503 had a JSON body, so HTML + 502-HTML are two in a row -> one re-login
    assert api.events == ["AAAA", "AAAA", "AAAA", "RELOGIN", "AAAA"], api.events
    print("  ok test_collect_retries_5xx_and_non_json_then_succeeds")


def test_collect_relogs_in_once_after_two_non_json_bodies():
    api = FakeApi({"AAAA": [HTML, HTML, ok(payload(base=1000.0), "AAAA")],
                   "BBBB": [HTML, HTML, HTML, HTML],
                   "CCCC": [HTML, Resp(500, {"success": False}), HTML, ok(payload(base=3000.0), "CCCC")]})
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA", "BBBB", "CCCC"], raw_dir=tmp)
    assert res["ok"] == ["AAAA", "CCCC"], res
    assert res["failed"] == {"BBBB": "non-JSON body"}, res
    assert api.events.count("RELOGIN") == 1                   # once per run
    assert api.events[:4] == ["AAAA", "AAAA", "RELOGIN", "AAAA"], api.events
    assert len(api.calls("BBBB")) == bc.MAX_RETRY
    print("  ok test_collect_relogs_in_once_after_two_non_json_bodies")


def test_non_json_counter_resets_on_any_json_body():
    api = FakeApi({"AAAA": [HTML, Resp(503, {"success": False}), HTML, ok(payload(), "AAAA")]})
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA"], raw_dir=tmp)
    assert res["ok"] == ["AAAA"] and "RELOGIN" not in api.events, api.events
    print("  ok test_non_json_counter_resets_on_any_json_body")


def test_relogin_failure_is_logged_safely_and_not_fatal():
    api = FakeApi({"AAAA": [HTML, HTML, ok(payload(), "AAAA")]},
                  relogin_error=RuntimeError(f"Login failed\nCall log:\n  - cookie: {SECRET}"))
    h = capture_logs()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            res = run(api, ["AAAA"], raw_dir=tmp)
    finally:
        release_logs(h)
    assert res["ok"] == ["AAAA"], res
    assert any("re-login failed: RuntimeError: Login failed" in x for x in h.lines), h.lines
    assert not any("TOPSECRET" in x for x in h.lines), h.lines
    print("  ok test_relogin_failure_is_logged_safely_and_not_fatal")


def test_give_up_after_max_retry_with_backoff_and_safe_reason():
    boom = RuntimeError(f"APIRequestContext.get: Timeout 120000ms exceeded.\nCall log:\n"
                        f"  - GET /api/inventory\n  - cookie: {SECRET}; csrftoken=abc")
    api = FakeApi({"AAAA": [boom] * bc.MAX_RETRY, "BBBB": [Resp(500, text="")] * bc.MAX_RETRY})
    sleeps = []
    h = capture_logs()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            res = run(api, ["AAAA", "BBBB"], raw_dir=tmp, sleeps=sleeps)
            assert not os.path.exists(bc.cache_path(tmp, "daily", "AAAA"))
    finally:
        release_logs(h)
    assert res["ok"] == [] and set(res["failed"]) == {"AAAA", "BBBB"}, res
    assert res["failed"]["AAAA"].startswith("RuntimeError: APIRequestContext.get: Timeout")
    assert res["failed"]["BBBB"] == "HTTP 500"
    assert len(api.calls("AAAA")) == len(api.calls("BBBB")) == bc.MAX_RETRY
    assert sleeps[:3] == [1.0, 3.0, 9.0], sleeps
    everything = json.dumps(res) + "\n".join(h.lines)
    assert "TOPSECRET" not in everything and "csrftoken" not in everything
    assert any("AAAA GIVE UP" in x for x in h.lines), h.lines
    print("  ok test_give_up_after_max_retry_with_backoff_and_safe_reason")


def test_throttle_answers_trigger_the_cooldown():
    api = FakeApi({"AAAA": [Resp(200, {"success": False, "message": "Abnormal usage, try later"}),
                            Resp(429, text="Too Many Requests"),
                            ok(payload(), "AAAA")]})
    sleeps = []
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA"], raw_dir=tmp, sleeps=sleeps)
    assert res["ok"] == ["AAAA"], res
    assert sleeps == [1.0, bc.THROTTLE_COOLDOWN, 3.0, bc.THROTTLE_COOLDOWN], sleeps
    print("  ok test_throttle_answers_trigger_the_cooldown")


def test_payload_verdicts_are_not_retried():
    broken = payload(base=2000.0)
    broken["nval"]["AK"][5] += 10.0                       # nval != bval - sval
    api = FakeApi({"AAAA": [ok(payload(base=1000.0), "BBBB")],   # wrong symbol
                   "CCCC": [ok(broken, "CCCC")],
                   "DDDD": [Resp(200, {"success": True})]})       # no data
    sleeps = []
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA", "CCCC", "DDDD"], raw_dir=tmp, sleeps=sleeps)
        # Nothing cached. The one thing written is the evidence of the verdicts.
        assert os.listdir(tmp) == [ic.MANIFEST_DIR], os.listdir(tmp)
        assert [c["status"] for c in manifest(tmp)] == [ic.REJECTED] * 3
    assert res["ok"] == [] and api.calls() == ["AAAA", "CCCC", "DDDD"], api.events
    assert "meta.symbol BBBB" in res["failed"]["AAAA"]
    assert res["failed"]["CCCC"].startswith("strict frame: StrictSourceError")
    assert res["failed"]["DDDD"] == "no data in response"
    assert all(bc.PACE <= s < bc.PACE + bc.JITTER for s in sleeps), sleeps   # pacing only
    print("  ok test_payload_verdicts_are_not_retried")


def test_cross_ticker_clone_is_rejected_within_a_run():
    bren = legacy("BREN")
    api = FakeApi({"BREN": [ok(bren, "BREN")],
                   "PANI": [ok(bren, "PANI")],               # BREN's series under PANI
                   "SINI": [ok(legacy("SINI"), "SINI")]})
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["BREN", "PANI", "SINI"], raw_dir=tmp)
        assert not os.path.exists(bc.cache_path(tmp, "daily", "PANI"))
    assert res["ok"] == ["BREN", "SINI"], res
    assert res["failed"] == {"PANI": "OHLC identical to BREN (cross-ticker clone)"}, res
    assert api.calls("PANI") == ["PANI"]                       # a verdict, not retried
    # A different run has nothing to compare against.
    with tempfile.TemporaryDirectory() as tmp:
        again = run(FakeApi({"PANI": [ok(bren, "PANI")]}), ["PANI"], raw_dir=tmp)
    assert again["ok"] == ["PANI"], again
    print("  ok test_cross_ticker_clone_is_rejected_within_a_run")


def test_market_mode_aborts_when_first_good_ticker_is_short():
    api = FakeApi({"AAAA": [ok(payload(n=40), "AAAA")], "BBBB": [ok(payload(base=2000.0), "BBBB")]})
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run(api, ["AAAA", "BBBB"], mode="market", raw_dir=tmp)
            raise AssertionError("no abort")
        except SystemExit as e:
            msg = str(e.code)
            assert "AAAA came back with 40 sessions" in msg and "LOOKBACK_DAYS" in msg, msg
            assert "2025-09-29..2026-09-24" in msg, msg
        assert api.calls() == ["AAAA"]
        assert not os.path.exists(bc.cache_path(tmp, "market", "AAAA"))

    # The first SUCCESSFUL ticker is what counts: a failure before it does not.
    api = FakeApi({"AAAA": [ok(payload(), "ZZZZ")], "BBBB": [ok(payload(n=40, base=2000.0), "BBBB")]})
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run(api, ["AAAA", "BBBB"], mode="market", raw_dir=tmp)
            raise AssertionError("no abort")
        except SystemExit as e:
            assert "BBBB came back with 40" in str(e.code), e.code

    # A long first ticker settles it; a later short one is just a short ticker.
    api = FakeApi({"AAAA": [ok(payload(), "AAAA")], "BBBB": [ok(payload(n=40, base=2000.0), "BBBB")]})
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA", "BBBB"], mode="market", raw_dir=tmp)
        assert os.path.exists(bc.cache_path(tmp, "market", "BBBB"))
    assert res["ok"] == ["AAAA", "BBBB"] and res["sessions"]["BBBB"] == 40, res
    print("  ok test_market_mode_aborts_when_first_good_ticker_is_short")


def test_daily_mode_aborts_on_three_short_of_first_five():
    names = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"]
    lengths = [40, 240, 40, 240, 40, 240]
    api = FakeApi({t: [ok(payload(n=n, base=1000.0 * (k + 1)), t)]
                   for k, (t, n) in enumerate(zip(names, lengths))})
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run(api, names, mode="daily", raw_dir=tmp)
            raise AssertionError("no abort")
        except SystemExit as e:
            msg = str(e.code)
            assert "3 of the first 5 watchlist tickers came back short" in msg, msg
            assert "AAAA=40, CCCC=40, EEEE=40" in msg, msg
        assert api.calls() == names[:5]
        assert not os.path.exists(bc.cache_path(tmp, "daily", "EEEE"))

    # Two short names in the first five is a watchlist with new listings, not a moved window.
    lengths = [40, 240, 240, 40, 240, 40, 40]
    names = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF", "GGGG"]
    api = FakeApi({t: [ok(payload(n=n, base=1000.0 * (k + 1)), t)]
                   for k, (t, n) in enumerate(zip(names, lengths))})
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, names, mode="daily", raw_dir=tmp)
    assert res["ok"] == names and res["sessions"]["GGGG"] == 40, res
    print("  ok test_daily_mode_aborts_on_three_short_of_first_five")


def test_zero_session_ticker_is_empty_not_failed():
    # Amendment A2: a delisted / long-suspended ticker answers success with no
    # sessions. It is `empty`: attempted, never retried, not cached, not a
    # failure, and not the "first good ticker" of the short-window probe.
    dead = payload(n=0)
    api = FakeApi({"AAAA": [ok(dead, "AAAA")], "BBBB": [ok(payload(base=2000.0), "BBBB")],
                   "CCCC": [ok(payload(base=3000.0), "ZZZZ")]})
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA", "BBBB", "CCCC"], mode="market", raw_dir=tmp)
        assert not os.path.exists(bc.cache_path(tmp, "market", "AAAA"))
    assert res["ok"] == ["BBBB"] and list(res["failed"]) == ["CCCC"], res
    assert list(res["empty"]) == ["AAAA"] and "0 sessions" in res["empty"]["AAAA"]
    assert api.calls("AAAA") == ["AAAA"], "an empty answer is not retried"

    # the window guard still works behind a dead first ticker
    api = FakeApi({"AAAA": [ok(dead, "AAAA")], "BBBB": [ok(payload(n=40, base=2000.0), "BBBB")]})
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run(api, ["AAAA", "BBBB"], mode="market", raw_dir=tmp)
            raise AssertionError("no abort")
        except SystemExit as e:
            assert "BBBB came back with 40" in str(e.code), e.code

    # in the denominator, not the numerator: 3 failed of 12 attempted is 25%,
    # where 3 of the 9 non-empty would have been 33% and failed the run
    r = {"ok": ["T"] * 6, "failed": {"X": "", "Y": "", "Z": ""},
         "empty": {"E1": "", "E2": "", "E3": ""}}
    assert not bc.should_fail(r) and bc.exit_if_failed(r) is None
    assert bc.should_fail(dict(r, empty={}))
    # but a run where nothing came back with data is still a failed run
    assert bc.should_fail({"ok": [], "failed": {}, "empty": {"E1": ""}})
    print("  ok test_zero_session_ticker_is_empty_not_failed")


def test_failure_rate_exit_uses_should_fail_run():
    good = {t: [ok(payload(base=1000.0 * (k + 1)), t)] for k, t in enumerate(["AAAA", "BBBB", "CCCC"])}
    bad = {t: [ok(payload(base=9000.0 + k), "XXXX")] for k, t in enumerate(["DDDD", "EEEE"])}
    with tempfile.TemporaryDirectory() as tmp:
        res = run(FakeApi({**good, **bad}), ["AAAA", "DDDD", "BBBB", "EEEE", "CCCC"], raw_dir=tmp)
    assert len(res["ok"]) == 3 and len(res["failed"]) == 2, res
    assert bc.should_fail(res)                                  # 40% > 30%
    try:
        bc.exit_if_failed(res)
        raise AssertionError("no exit")
    except SystemExit as e:
        assert isinstance(e.code, str) and "2/5 tickers failed (40%" in e.code, e.code

    fine = {"ok": ["A", "B", "C", "D"], "failed": {"E": "HTTP 500"}, "sessions": {}}
    assert not bc.should_fail(fine) and bc.exit_if_failed(fine) is None     # 20%
    edge = {"ok": ["A"] * 7, "failed": {"X": "", "Y": "", "Z": ""}, "sessions": {}}
    assert not bc.should_fail(edge)                              # exactly 30% is not over
    assert bc.should_fail({"ok": [], "failed": {}, "sessions": {}})   # nothing attempted
    # One definition of "too many failures": the backfill's own.
    from price_audit import should_fail_run
    for n_ok, n_bad in ((3, 2), (4, 1), (7, 3), (0, 0), (0, 4), (69, 31), (70, 30)):
        r = {"ok": ["T"] * n_ok, "failed": {f"F{k:03d}": "x" for k in range(n_bad)}}
        assert bc.should_fail(r) == should_fail_run(n_bad, n_ok + n_bad, 0.30), (n_ok, n_bad)
    print("  ok test_failure_rate_exit_uses_should_fail_run")


def test_mostly_empty_run_fails():
    """Review finding: empty had no ceiling, so 50 ok + 990 empty passed as a
    clean run and would have written a week of insert-only tables from ~5%
    of the universe. More than MAX_EMPTY_SHARE empty is a broken fetch."""
    assert bc.should_fail({"ok": ["T"] * 50, "failed": {}, "empty": {f"E{k}": "" for k in range(990)}})
    try:
        bc.exit_if_failed({"ok": ["T"] * 50, "failed": {}, "empty": {f"E{k}": "" for k in range(990)}})
        raise AssertionError("no exit")
    except SystemExit as e:
        assert "990/1040 tickers came back with 0 sessions" in str(e.code), e.code
    # the legacy cache's 104 of 1,040 (10%) is fine; exactly 25% is not over
    assert not bc.should_fail({"ok": ["T"] * 936, "failed": {}, "empty": {f"E{k}": "" for k in range(104)}})
    assert not bc.too_many_empty(1, 4) and bc.too_many_empty(2, 7) and not bc.too_many_empty(0, 0)
    print("  ok test_mostly_empty_run_fails")


def test_iter_cached_reports_unreadable_files():
    """Review finding: with tickers=None a corrupt cache file was logged and
    skipped, and the caller had no list to diff against, so it never counted
    as failed. iter_cached now hands the caller {ticker: reason}."""
    with tempfile.TemporaryDirectory() as tmp:
        with gzip.open(os.path.join(tmp, "SINI.json.gz"), "wt", encoding="utf-8") as fh:
            json.dump(payload(n=5), fh)
        with open(os.path.join(tmp, "RAJA.json.gz"), "wb") as fh:
            fh.write(b"not gzip")
        with gzip.open(os.path.join(tmp, "PANI.json.gz"), "wt", encoding="utf-8") as fh:
            fh.write("{broken json")
        bad = {}
        got = dict(bc.iter_cached(None, "ignored", raw_dir=tmp, legacy=True, unreadable=bad))
        assert list(got) == ["SINI"] and sorted(bad) == ["PANI", "RAJA"], (list(got), bad)
        assert all(v.startswith("unreadable cache (") for v in bad.values()), bad
        assert list(dict(bc.iter_cached(None, "ignored", raw_dir=tmp, legacy=True))) == ["SINI"]
    print("  ok test_iter_cached_reports_unreadable_files")


# ── cache ──────────────────────────────────────────────────────────────────

def test_cache_round_trip_envelope_and_legacy():
    sini = legacy("SINI")
    with tempfile.TemporaryDirectory() as tmp:
        env = {"success": True, "data": sini, "meta": {"symbol": "SINI", "investor_type": "A"}}
        bc.save_cached(tmp, "daily", "SINI", env)
        path = bc.cache_path(tmp, "daily", "SINI")
        assert path == os.path.join(tmp, "daily", "SINI.json.gz")
        with gzip.open(path, "rt", encoding="utf-8") as f:
            doc = json.load(f)
        assert set(doc) == {"fetched_utc", "meta", "data"}
        assert doc["meta"]["symbol"] == "SINI" and doc["fetched_utc"].endswith("Z")
        assert not any(n.endswith(".tmp") for n in os.listdir(os.path.join(tmp, "daily")))

        bc.save_cached(tmp, "daily", "PANI", {"success": True, "data": payload()})
        bc.save_cached(tmp, "market", "BREN", {"success": True, "data": payload(base=5.0)})
        # a cached envelope that claims another symbol, and a corrupt file
        bc.save_cached(tmp, "daily", "BBHI", {"data": payload(), "meta": {"symbol": "BREN"}})
        with open(bc.cache_path(tmp, "daily", "CASS"), "wb") as f:
            f.write(b"not gzip")

        got = bc.load_cached(["SINI", "PANI", "NOPE"], "daily", raw_dir=tmp)
        assert list(got) == ["SINI", "PANI"] and got["SINI"] == sini
        assert got["PANI"] == payload()
        every = bc.load_cached(None, "daily", raw_dir=tmp)
        assert sorted(every) == ["PANI", "SINI"], sorted(every)     # BBHI/CASS skipped
        assert list(bc.load_cached(None, "market", raw_dir=tmp)) == ["BREN"]
        assert dict(bc.iter_cached(["SINI"], "daily", raw_dir=tmp)) == {"SINI": sini}

        # The legacy layout (<raw_dir>/<T>.json.gz holding a bare data dict).
        legacy_dir = os.path.join(tmp, "legacy")
        os.makedirs(legacy_dir)
        with gzip.open(os.path.join(legacy_dir, "SINI.json.gz"), "wt", encoding="utf-8") as f:
            json.dump(sini, f)
        got = bc.load_cached(["SINI", "NOPE"], "ignored", raw_dir=legacy_dir, legacy=True)
        assert got == {"SINI": sini}

    if has_legacy("SINI") and has_legacy("BREN"):      # the real harvest cache, read-only
        before = os.stat(os.path.join(LEGACY, "SINI.json.gz")).st_mtime_ns
        got = bc.load_cached(["SINI", "BREN", "NOPE"], "ignored", raw_dir=LEGACY, legacy=True)
        assert list(got) == ["SINI", "BREN"]
        assert got["SINI"] == sini and len(got["BREN"]["nlot"]) == 101
        assert os.stat(os.path.join(LEGACY, "SINI.json.gz")).st_mtime_ns == before
    print("  ok test_cache_round_trip_envelope_and_legacy")


def test_collect_cache_matches_what_load_cached_returns():
    data = payload(base=777.0)
    api = FakeApi({"AAAA": [Resp(200, {"success": True, "data": data,
                                       "meta": {"symbol": "AAAA", "brokers": ["AK", "BK"]}})]})
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA"], mode="market", raw_dir=tmp)
        assert res["ok"] == ["AAAA"]
        assert bc.load_cached(res["ok"], "market", raw_dir=tmp) == {"AAAA": data}
        with gzip.open(bc.cache_path(tmp, "market", "AAAA"), "rt", encoding="utf-8") as f:
            assert json.load(f)["meta"]["brokers"] == ["AK", "BK"]
    print("  ok test_collect_cache_matches_what_load_cached_returns")


# ── capture manifest (inventory_capture) ───────────────────────────────────

def test_collect_records_every_attempt_and_its_outcome():
    """Each attempt is on disk before it is sent and ends with one outcome;
    a failure is evidence, not an absence. The collect() result is unchanged."""
    a = payload(base=1000.0)
    boom = RuntimeError(f"APIRequestContext.get: Timeout 120000ms exceeded.\nCall log:\n"
                        f"  - GET /api/inventory\n  - cookie: {SECRET}; csrftoken=abc")
    names = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"]
    with tempfile.TemporaryDirectory() as tmp:
        api = RecordingApi({
            "AAAA": [Resp(503, {"success": False, "message": "busy"}), HTML, ok(a, "AAAA")],
            "BBBB": [Resp(200, {"success": False, "message": "bad symbol"}),
                     ok(payload(base=2000.0), "BBBB")],
            "CCCC": [ok(payload(base=3000.0), "ZZZZ")],        # another ticker's answer
            "DDDD": [ok(payload(n=0), "DDDD")],                # zero sessions
            "EEEE": [boom] * bc.MAX_RETRY,                     # never answers
            "FFFF": [ok(a, "FFFF")]}, tmp)                     # AAAA's series again
        res = bc.collect(names, "daily", raw_dir=tmp, sleep=[].append, now=NOW,
                         request_get=api.get, relogin=api.relogin,
                         pipeline_run_id="daily-2026-09-24T10:30:00Z")
        caps = manifest(tmp)
        with open(glob.glob(os.path.join(tmp, ic.MANIFEST_DIR, "*.jsonl"))[0], encoding="utf-8") as fh:
            text = fh.read()
        cached = {t: bc.cache_path(tmp, "daily", t) for t in ("AAAA", "BBBB")}
        digests = {}
        for t, path in cached.items():
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                digests[t] = hashlib.sha256(fh.read().encode("utf-8")).hexdigest()

    assert res["ok"] == ["AAAA", "BBBB"] and list(res["empty"]) == ["DDDD"], res
    assert sorted(res["failed"]) == ["CCCC", "EEEE", "FFFF"], res
    assert api.recorded_first and all(api.recorded_first), api.recorded_first
    assert len(api.recorded_first) == len(caps) == 3 + 2 + 1 + 1 + bc.MAX_RETRY + 1
    assert [(c["ticker"], c["attempt"], c["status"]) for c in caps] == [
        ("AAAA", 1, ic.HTTP_ERROR), ("AAAA", 2, ic.NON_JSON), ("AAAA", 3, ic.OK),
        ("BBBB", 1, ic.VENDOR_ERROR), ("BBBB", 2, ic.OK),
        ("CCCC", 1, ic.REJECTED), ("DDDD", 1, ic.EMPTY)] + [
        ("EEEE", k, ic.ERROR) for k in range(1, bc.MAX_RETRY + 1)] + [("FFFF", 1, ic.REJECTED)]

    run_ids = {c["run_id"] for c in caps}
    assert len(run_ids) == 1 and len({c["capture_id"] for c in caps}) == len(caps)
    for c in caps:
        assert c["capture_id"].startswith(c["run_id"] + "-") and c["captured_at"].endswith("Z")
        assert (c["collector"], c["mode"], c["writes_cache"]) == ("broker_collect", "daily", True)
        assert c["pipeline_run_id"] == "daily-2026-09-24T10:30:00Z"
        assert c["broker_list_source"] == "broker_codes.json"
        assert c["response_sha256"] is None      # these fakes have no .body(): text digest only
    by = {(c["ticker"], c["attempt"]): c for c in caps}
    for t, attempt in (("AAAA", 3), ("BBBB", 2)):
        good = by[(t, attempt)]
        assert good["cache_ref"] == f"daily/{t}.json.gz" and good["cache_sha256"] == digests[t]
        assert good["returned_brokers"] == ["AK", "BK"] and good["session_count"] == 120
        assert good["reason"] is None and good["http_status"] == 200
    assert by[("AAAA", 1)]["http_status"] == 503 and by[("AAAA", 1)]["reason"] == "HTTP 503"
    assert by[("AAAA", 2)]["returned_brokers"] is None
    assert by[("AAAA", 2)]["response_text_sha256"] == ic.sha256_text(HTML.text())
    assert "bad symbol" in by[("BBBB", 1)]["reason"] and by[("BBBB", 1)]["vendor_success"] is False
    assert by[("CCCC", 1)]["reason"] == "meta.symbol ZZZZ != requested CCCC"
    assert by[("DDDD", 1)]["session_count"] == 0 and by[("DDDD", 1)]["cache_ref"] is None
    assert by[("EEEE", 1)]["reason"] == "RuntimeError: APIRequestContext.get: Timeout 120000ms exceeded."
    assert by[("EEEE", 1)]["http_status"] is None and by[("EEEE", 1)]["response_text_sha256"] is None
    assert by[("FFFF", 1)]["reason"] == "OHLC identical to AAAA (cross-ticker clone)"
    assert "TOPSECRET" not in text and "csrftoken" not in text and "cookie" not in text.lower()
    print("  ok test_collect_records_every_attempt_and_its_outcome")


def test_collect_manifest_keeps_requested_returned_and_echo_apart():
    """The request set is ours, the returned set is the payload's, the echo is
    the vendor's. A broker returned at zero stays distinguishable from one
    requested and never returned, and nothing is invented for the latter."""
    data = payload(brokers=("AK", "BK"))
    for f in ("blot", "bval", "slot", "sval", "nlot", "nval"):
        data[f]["BK"] = [0] * len(data["date"])              # returned, explicitly zero
    echo = ["AD", "AF", "AK", "BK"]                          # the vendor's account of the request
    api = FakeApi({"AAAA": [Resp(200, {"success": True, "data": data,
                                       "meta": {"symbol": "AAAA", "brokers": echo}})]})
    codes = bc.load_codes()
    omitted = next(c for c in codes if c not in ("AK", "BK"))
    with tempfile.TemporaryDirectory() as tmp:
        res = run(api, ["AAAA"], raw_dir=tmp)
        (cap,) = manifest(tmp)
        loaded = bc.load_cached(None, "daily", raw_dir=tmp)
        with gzip.open(bc.cache_path(tmp, "daily", "AAAA"), "rt", encoding="utf-8") as f:
            doc_text = f.read()
    assert res["ok"] == ["AAAA"], res
    assert cap["brokers_param"] == codes and cap["requested_brokers"] == sorted(codes)
    assert cap["broker_request_kind"] == ic.EXPLICIT_CODES
    assert cap["returned_brokers"] == ["AK", "BK"]
    assert cap["vendor_meta"] == {"symbol": "AAAA", "brokers": echo}
    assert omitted in cap["requested_brokers"] and omitted not in cap["returned_brokers"]
    assert all(v == 0 for v in data["nlot"]["BK"]) and "BK" in cap["returned_brokers"]
    # The cache is the envelope it always was, with no row made up for `omitted`.
    assert loaded == {"AAAA": data} and omitted not in loaded["AAAA"]["nlot"]
    assert set(json.loads(doc_text)) == {"fetched_utc", "meta", "data"}
    assert cap["cache_sha256"] == hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
    assert (cap["ticker"], cap["start_date"], cap["end_date"], cap["investor_type"]) == (
        "AAAA", "2025-09-29", "2026-09-24", "A")
    print("  ok test_collect_manifest_keeps_requested_returned_and_echo_apart")


def test_collect_records_the_window_abort_and_a_failed_cache_write():
    api = FakeApi({"AAAA": [ok(payload(n=40), "AAAA")]})
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run(api, ["AAAA"], mode="market", raw_dir=tmp)
            raise AssertionError("no abort")
        except SystemExit:
            pass
        (cap,) = manifest(tmp)
    assert cap["status"] == ic.ABORTED and cap["session_count"] == 40, cap
    assert cap["reason"].startswith("AAAA came back with 40 sessions") and cap["cache_ref"] is None

    api = FakeApi({"AAAA": [ok(payload(), "AAAA")]})
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(bc.cache_path(tmp, "market", "AAAA") + ".tmp")   # the temp file cannot be made
        res = run(api, ["AAAA"], mode="market", raw_dir=tmp)
        (cap,) = manifest(tmp)
    assert res["failed"]["AAAA"].startswith("cache write failed"), res
    assert cap["status"] == ic.CACHE_WRITE_FAILED and cap["cache_ref"] is None, cap
    assert cap["reason"].startswith("cache write failed") and cap["returned_brokers"] == ["AK", "BK"]
    print("  ok test_collect_records_the_window_abort_and_a_failed_cache_write")


def test_collect_digests_the_exact_body_bytes_when_the_response_has_them():
    """Playwright's APIResponse has .body(); the digest is then over the bytes
    as received, not over the decoded text, and the text digest stays empty."""
    class BytesResp(Resp):
        def body(self):
            return b"\xef\xbb\xbf" + self._text.encode("utf-8")    # differs from the text

    resp = BytesResp(200, envelope(payload(), "AAAA"))
    with tempfile.TemporaryDirectory() as tmp:
        res = run(FakeApi({"AAAA": [resp]}), ["AAAA"], raw_dir=tmp)
        (cap,) = manifest(tmp)
    assert res["ok"] == ["AAAA"] and cap["status"] == ic.OK, (res, cap)
    assert cap["response_sha256"] == hashlib.sha256(resp.body()).hexdigest()
    assert cap["response_bytes"] == len(resp.body()) and cap["response_text_sha256"] is None
    print("  ok test_collect_digests_the_exact_body_bytes_when_the_response_has_them")


def test_manifest_is_invisible_to_the_cache_readers():
    """The manifest directory sits inside both cache layouts; no reader sees it."""
    sini = payload(n=5)
    api = FakeApi({"AAAA": [ok(payload(base=5.0), "AAAA")]})
    with tempfile.TemporaryDirectory() as tmp:
        run(api, ["AAAA"], raw_dir=tmp)
        assert os.path.isdir(os.path.join(tmp, ic.MANIFEST_DIR))
        bad = {}
        assert list(dict(bc.iter_cached(None, "daily", raw_dir=tmp, unreadable=bad))) == ["AAAA"]
        assert list(dict(bc.iter_cached(None, "ignored", raw_dir=tmp, legacy=True,
                                        unreadable=bad))) == [] and bad == {}
        # the legacy layout, with a manifest from a harvest beside the bare dicts
        legacy_dir = os.path.join(tmp, "legacy")
        log = ic.CaptureLog(legacy_dir, "harvest_inventory", writes_cache=True)
        log.begin("symbol=SINI&brokers=AK").finish(ic.ERROR, "x")
        with gzip.open(os.path.join(legacy_dir, "SINI.json.gz"), "wt", encoding="utf-8") as f:
            json.dump(sini, f)
        got = dict(bc.iter_cached(None, "ignored", raw_dir=legacy_dir, legacy=True, unreadable=bad))
        assert got == {"SINI": sini} and bad == {}, (list(got), bad)
        # the glob build_inventory_db and normalize_market_data use
        assert [os.path.basename(p) for p in glob.glob(os.path.join(legacy_dir, "*.json.gz"))] == [
            "SINI.json.gz"]
    print("  ok test_manifest_is_invisible_to_the_cache_readers")


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    print(f"broker collect: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
