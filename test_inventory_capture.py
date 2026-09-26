"""Plain-script tests for inventory_capture.py and the three collectors besides
broker_collect that use it (py -3 test_inventory_capture.py): harvest_inventory,
backfill_inventory and the bag-holder lookup in neobdm_scraper.

Offline only, synthetic payloads only. neobdm_scraper is imported for real, with
placeholder secrets set first (so a local .env is never read for them) and
stand-ins only for the third-party modules a machine lacks: CI's ml-health job
installs neither Playwright nor schedule nor pytz. Its login and every
sync_playwright a collector holds are replaced by stubs that refuse, so no test
can log in or start a browser; the collectors run against fake request
contexts. neobdm.db, the failure snapshot and the repository's own
_capture_manifest/ are pointed at nothing by default, so a test that forgot to
redirect them fails instead of writing there. broker_collect's use of the
manifest is tested in test_broker_collect.py.
"""

import contextlib
import glob
import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import types
import zoneinfo
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

HERE = os.path.dirname(os.path.abspath(__file__))

# First, before any stand-in exists: price_audit imports pandas, and pandas
# probes for pytz once, on import. A stand-in pytz there would make it fail to
# read a version; imported now, pandas settles on "no pytz" and never looks again.
from price_audit import bagholders_from_payloads, inventory_date_blocks   # noqa: E402


def _refuse(what):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test tried to {what}")
    return refuse


def _stand_in_if_missing(name, **attrs):
    """A stand-in for a third-party module this machine lacks; wherever the
    real one is installed, the real one is used."""
    try:
        __import__(name)
        return
    except ImportError:
        pass
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        sys.modules.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


_stand_in_if_missing("playwright.sync_api", sync_playwright=_refuse("start a browser"),
                     TimeoutError=type("TimeoutError", (Exception,), {}))
_stand_in_if_missing("schedule")
_stand_in_if_missing("pytz", timezone=zoneinfo.ZoneInfo)
for _secret in ("NEOBDM_USERNAME", "NEOBDM_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.setdefault(_secret, "test-placeholder")

import neobdm_scraper as ns          # noqa: E402

ns.login = _refuse("log in to NeoBDM")          # before the collectors bind it
ns.sync_playwright = _refuse("start a browser")
ns.log.disabled = True                          # the GIVE UP lines below are expected

import backfill_inventory as bf      # noqa: E402
import broker_collect as bc          # noqa: E402
import harvest_inventory as hi       # noqa: E402
import inventory_capture as ic       # noqa: E402

hi.sync_playwright = bf.sync_playwright = _refuse("start a browser")
bf.DB_PATH = bf.FAILURE_SNAPSHOT = None         # never the real neobdm.db or CWD
ic.NO_CACHE_ROOT = None                         # never the repository's own manifest

CODES = bc.load_codes()              # the real 101-code request list
SECRET = "TOPSECRET123"
FIELDS = ("blot", "bval", "slot", "sval", "nlot", "nval")
LOGIN_PAGE = "<!doctype html><title>Login</title>"


# ── helpers ────────────────────────────────────────────────────────────────

def payload(n=120, base=1000.0, brokers=("AK", "BK"), zero=(), start=date(2025, 10, 1)):
    """A data dict strict_ticker_frame accepts. Brokers in `zero` come back
    with explicit all-zero series; a broker not in `brokers` does not come
    back at all."""
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    data = {f: {} for f in FIELDS}
    data["date"] = dates
    for k, b in enumerate(brokers):
        blot = [0 if b in zero else (i + k) % 7 for i in range(n)]
        slot = [0 if b in zero else (3 * i + k) % 5 for i in range(n)]
        data["blot"][b], data["slot"][b] = blot, slot
        data["bval"][b] = [x * 100 * base for x in blot]
        data["sval"][b] = [y * 100 * base for y in slot]
        data["nlot"][b] = [x - y for x, y in zip(blot, slot)]
        data["nval"][b] = [x - y for x, y in zip(data["bval"][b], data["sval"][b])]
    data["ohlc"] = [{"date": x, "open": base + i, "high": base + i + 5, "low": base + i - 5,
                     "close": base + i, "volume": 1000} for i, x in enumerate(dates)]
    return data


class Resp:
    """Stands in for Playwright's APIResponse: .status, .text() and .body()."""

    def __init__(self, status=200, body=None, text=None):
        self.status = status
        self._text = text if text is not None else json.dumps(body)

    def text(self):
        return self._text

    def body(self):
        return self._text.encode("utf-8")


def ok(data, symbol):
    return Resp(200, {"success": True, "data": data, "meta": {"symbol": symbol}})


def only_manifest(root):
    (path,) = glob.glob(os.path.join(root, ic.MANIFEST_DIR, "*.jsonl"))
    return path


def manifest_text(root):
    with open(only_manifest(root), encoding="utf-8") as fh:
        return fh.read()


class FakeRequest:
    """A request context shared by every collector's fake: serves a script
    per ticker (the `symbol` param) and notes, at every request, whether the
    manifest's last line is already that exact request with no outcome yet
    (asserting here would only count as a failed attempt)."""

    def __init__(self, root, script):
        self.root, self.script = root, {t: list(v) for t, v in script.items()}
        self.urls, self.timeouts, self.recorded_first = [], [], []

    def get(self, url, timeout=None):
        base, qs = url.split("?", 1)
        assert base == f"{ns.API_BASE}/inventory", base
        self.urls.append(url)
        self.timeouts.append(timeout)
        last = json.loads(manifest_text(self.root).splitlines()[-1])
        self.recorded_first.append(last["event"] == "request"
                                   and last["query_sha256"] == ic.sha256_text(qs))
        item = self.script[parse_qs(qs)["symbol"][0]].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def params(self, k):
        return parse_qs(self.urls[k].split("?", 1)[1], keep_blank_values=True)


def fake_browser(request):
    """A sync_playwright() whose one page's context.request is `request`."""
    page = SimpleNamespace(goto=lambda *a, **k: None, wait_for_timeout=lambda ms: None,
                           set_default_timeout=lambda ms: None,
                           context=SimpleNamespace(request=request))
    ctx = SimpleNamespace(new_page=lambda: page, request=request)
    browser = SimpleNamespace(new_context=lambda **kw: ctx, close=lambda: None)

    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: browser))
    return sync_playwright


def run_harvest(root, script, tickers, refresh=False):
    """harvest_inventory.main() over `tickers`, cached to `root`, offline."""
    request = FakeRequest(root, script)
    saved = (hi.RAW, hi.sync_playwright, hi.login, hi.time, sys.argv)
    hi.RAW, hi.sync_playwright, hi.login = root, fake_browser(request), lambda page: None
    hi.time = SimpleNamespace(time=time.time, strftime=time.strftime, gmtime=time.gmtime,
                              sleep=lambda s: None)
    sys.argv = ["harvest_inventory.py", "--tickers", ",".join(tickers)] + (
        ["--refresh"] if refresh else [])
    try:
        hi.main()
    finally:
        hi.RAW, hi.sync_playwright, hi.login, hi.time, sys.argv = saved
    return request


def run_backfill(root, script, tickers):
    """backfill_inventory.run_backfill() offline, with neobdm.db, the failure
    snapshot and the no-cache manifest all under `root`. (stdout, the exit
    message or None, the request context)."""
    request = FakeRequest(root, script)
    saved = (bf.sync_playwright, bf.login, bf.time, bf.DB_PATH, bf.FAILURE_SNAPSHOT,
             ic.NO_CACHE_ROOT)
    bf.sync_playwright, bf.login = fake_browser(request), lambda page: None
    bf.time = SimpleNamespace(sleep=lambda s: None)
    bf.DB_PATH = os.path.join(root, "neobdm.db")
    bf.FAILURE_SNAPSHOT = os.path.join(root, "topup-failure.json")
    ic.NO_CACHE_ROOT = root
    out, exit_message = io.StringIO(), None
    try:
        with contextlib.redirect_stdout(out):
            bf.run_backfill(tickers)
    except SystemExit as e:
        exit_message = e.code
    finally:
        (bf.sync_playwright, bf.login, bf.time, bf.DB_PATH, bf.FAILURE_SNAPSHOT,
         ic.NO_CACHE_ROOT) = saved
    return out.getvalue(), exit_message, request


def run_collect(root, responses):
    """broker_collect.collect() over AAAA, offline: its injected request
    function serves `responses` in order."""
    queue = list(responses)

    def get(qs):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return bc.collect(["AAAA"], "daily", raw_dir=root, sleep=lambda s: None,
                      now=datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc),
                      request_get=get, relogin=lambda: None)


def db_counts(root):
    """{ticker: rows} of price_history and of broker_flow in root/neobdm.db."""
    conn = sqlite3.connect(os.path.join(root, "neobdm.db"))
    try:
        prices = dict(conn.execute("SELECT ticker, COUNT(*) FROM price_history GROUP BY ticker"))
        flows = dict(conn.execute("SELECT ticker, COUNT(*) FROM broker_flow GROUP BY ticker"))
    finally:
        conn.close()
    return prices, flows


@contextmanager
def no_cache_root(root):
    saved, ic.NO_CACHE_ROOT = ic.NO_CACHE_ROOT, root
    try:
        yield
    finally:
        ic.NO_CACHE_ROOT = saved


def assert_selector_request_without_cache(cap, collector, source, selectors, mode=None):
    """The shape every capture of a selector path without a cache must have."""
    assert (cap["collector"], cap["mode"], cap["broker_list_source"]) == (collector, mode, source)
    assert cap["writes_cache"] is False
    assert cap["broker_request_kind"] == ic.SELECTOR and cap["requested_brokers"] is None
    assert cap["broker_selectors"] == selectors and cap["brokers_param"] == selectors
    assert cap["investor_type"] == "A" and cap["other_params"] == []
    assert cap["cache_ref"] is None and cap["cache_sha256"] is None, cap


# ── the request side ───────────────────────────────────────────────────────

def test_request_fields_keep_the_exact_explicit_list():
    qs = bc.build_query("SINI", CODES, "2025-09-29", "2026-09-24")
    # The two collectors spell the query differently and must send the same bytes.
    assert hi.build_query("SINI", CODES, "2025-09-29", "2026-09-24") == qs
    f = ic.request_fields(qs)
    assert (f["ticker"], f["start_date"], f["end_date"], f["investor_type"]) == (
        "SINI", "2025-09-29", "2026-09-24", "A")
    assert f["brokers_param"] == CODES and len(CODES) == 101     # as sent, in order
    assert f["broker_request_kind"] == ic.EXPLICIT_CODES
    assert f["requested_brokers"] == sorted(CODES)
    assert f["broker_selectors"] == [] and f["other_params"] == []
    assert f["query_sha256"] == hashlib.sha256(qs.encode("utf-8")).hexdigest()
    # Normalised consistently, never re-chosen: the set folds case, space and
    # repeats; the list keeps exactly what was sent.
    g = ic.request_fields(urlencode([("symbol", "SINI"), ("brokers", " ak"),
                                     ("brokers", "BK"), ("brokers", "AK")]))
    assert g["brokers_param"] == [" ak", "BK", "AK"] and g["requested_brokers"] == ["AK", "BK"]
    print("  ok test_request_fields_keep_the_exact_explicit_list")


def test_a_selector_is_never_passed_off_as_a_broker_set():
    sel = ["TOP_5_NB_LOT_C20", "TOP_5_NS_LOT_C20"]
    assert bf.INVENTORY_BROKERS == sel and ns.BAGHOLDER_BROKERS == sel   # what the two paths send
    q = urlencode([("symbol", "ENRG"), ("start_date", "2025-10-01"), ("end_date", "2026-09-26"),
                   ("investor_type", "A")] + [("brokers", b) for b in sel])
    f = ic.request_fields(q)
    assert f["broker_request_kind"] == ic.SELECTOR and f["requested_brokers"] is None
    assert f["broker_selectors"] == sel and f["brokers_param"] == sel
    m = ic.request_fields(q + "&brokers=AK")
    assert m["broker_request_kind"] == ic.MIXED and m["requested_brokers"] is None
    assert m["brokers_param"] == sel + ["AK"] and m["broker_selectors"] == sel
    n = ic.request_fields("symbol=ENRG")
    assert n["broker_request_kind"] == ic.NO_BROKERS and n["requested_brokers"] is None
    assert n["brokers_param"] == [] and n["start_date"] is None
    assert ic.request_fields("symbol=A&symbol=B")["ticker"] == ["A", "B"]   # as sent, not picked
    print("  ok test_a_selector_is_never_passed_off_as_a_broker_set")


def test_unknown_params_are_named_never_valued():
    with tempfile.TemporaryDirectory() as tmp:
        log = ic.CaptureLog(tmp, "test", writes_cache=True)
        log.begin(f"symbol=SINI&brokers=AK&api_key={SECRET}&session={SECRET}").finish(ic.ERROR, "x")
        text = manifest_text(tmp)
        (cap,) = ic.read_captures(log.path)
    assert cap["other_params"] == ["api_key", "session"], cap["other_params"]
    assert SECRET not in text
    print("  ok test_unknown_params_are_named_never_valued")


# ── the response side ──────────────────────────────────────────────────────

def test_returned_brokers_come_from_the_payload_not_the_echo():
    data = payload(brokers=("AK", "BK"), zero=("BK",))   # BK: returned, explicitly zero
    data["nlot"]["DD"] = None                            # a null series is not a returned broker
    echo = ["AK", "BK", "CC", "DD", "EE"]
    body = {"success": True, "data": data,
            "meta": {"symbol": "SINI", "brokers": echo, "investor_type": "A", "start_date": None,
                     "extra": {"nested": 1}, "user@example.com": "x"}}
    ev = ic.response_evidence(200, json.dumps(body), body)
    assert ev["returned_brokers"] == ["AK", "BK"], ev["returned_brokers"]
    assert all(v == 0 for f in FIELDS for v in data[f]["BK"])
    assert ev["vendor_meta"] == {"symbol": "SINI", "brokers": echo, "investor_type": "A",
                                 "start_date": None}
    assert ev["vendor_meta_unrecorded_keys"] == ["<non-identifier key>", "extra"]
    assert (ev["session_count"], ev["first_session"], ev["last_session"]) == (
        120, data["date"][0], data["date"][-1])
    assert ev["vendor_success"] is True and ev["http_status"] == 200
    # Nothing to derive from is not the same as nothing returned.
    assert ic.response_evidence(200, "[]", [])["returned_brokers"] is None
    assert ic.response_evidence(200, "{}", {"success": True, "data": {"date": []}})[
        "returned_brokers"] == []
    print("  ok test_returned_brokers_come_from_the_payload_not_the_echo")


def test_the_digest_is_over_the_exact_bytes_where_the_response_has_them():
    text = '{"success": true}'
    wire = b'\xef\xbb\xbf{"success": true}'              # the bytes sent need not be the text
    exact = ic.response_evidence(200, text, {"success": True}, wire)
    assert exact["response_sha256"] == hashlib.sha256(wire).hexdigest()
    assert exact["response_bytes"] == len(wire) and exact["response_text_sha256"] is None
    decoded = ic.response_evidence(200, text, {"success": True})
    assert decoded["response_sha256"] is None and decoded["response_bytes"] is None
    assert decoded["response_text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert ic.response_evidence(None, None, None)["response_text_sha256"] is None

    class Guarded:                                      # raw_body() reads the body, nothing else
        @property
        def headers(self):
            raise AssertionError("headers were read")

        def body(self):
            return b"{}"
    assert ic.raw_body(Guarded()) == b"{}" and ic.raw_body(Resp(200, text=text)) == text.encode()
    assert ic.raw_body(SimpleNamespace(status=200)) is None                  # no .body()
    assert ic.raw_body(SimpleNamespace(body=_refuse("read a disposed body"))) is None
    assert ic.raw_body(SimpleNamespace(body=lambda: "not bytes")) is None
    print("  ok test_the_digest_is_over_the_exact_bytes_where_the_response_has_them")


def test_a_non_json_body_is_kept_as_a_digest_only():
    html = f'<html><form><input name="csrfmiddlewaretoken" value="{SECRET}"></form></html>'
    with tempfile.TemporaryDirectory() as tmp:
        log = ic.CaptureLog(tmp, "test", writes_cache=True)
        cap = log.begin("symbol=AAAA&brokers=AK")
        cap.response(200, html, None, html.encode("utf-8"))
        cap.finish(ic.NON_JSON, "HTTP 200, body is not JSON")
        text = manifest_text(tmp)
        (c,) = ic.read_captures(log.path)
    assert SECRET not in text and "csrfmiddlewaretoken" not in text
    assert c["response_sha256"] == hashlib.sha256(html.encode("utf-8")).hexdigest()
    assert c["response_bytes"] == len(html) and c["returned_brokers"] is None
    assert c["vendor_meta"] is None and c["status"] == ic.NON_JSON
    print("  ok test_a_non_json_body_is_kept_as_a_digest_only")


def test_reasons_never_carry_credentials():
    boom = RuntimeError(f"APIRequestContext.get: Timeout 120000ms exceeded.\nCall log:\n"
                        f"  - GET /api/inventory\n  - cookie: sessionid={SECRET}; csrftoken=abc")
    assert ic.safe_reason(boom) == "RuntimeError: APIRequestContext.get: Timeout 120000ms exceeded."
    for text in (f"cookie: sessionid={SECRET}", f"Authorization: Bearer {SECRET}",
                 f"x-csrftoken={SECRET}", f"password={SECRET}", f"GET /x?token={SECRET}&a=1",
                 f"Set-Cookie: sessionid={SECRET}; Path=/", f"api_key: {SECRET}"):
        out = ic.safe_reason(text)
        assert SECRET not in out and "<redacted>" in out, (text, out)
    assert ic.safe_reason("success=False message='Invalid token'") == \
        "success=False message='Invalid token'"
    assert len(ic.safe_reason("x" * 1000)) == ic.REASON_MAX and ic.safe_reason(None) is None
    print("  ok test_reasons_never_carry_credentials")


# ── the log ────────────────────────────────────────────────────────────────

def test_request_is_on_disk_before_the_outcome_and_both_read_back():
    with tempfile.TemporaryDirectory() as tmp:
        log = ic.CaptureLog(tmp, "test", writes_cache=True, mode="daily",
                            pipeline_run_id="daily-X", broker_list_source="broker_codes.json")
        assert not os.path.exists(os.path.join(tmp, ic.MANIFEST_DIR))   # nothing sent, nothing written
        qs = bc.build_query("SINI", CODES, "2025-09-29", "2026-09-24")
        cap = log.begin(qs, attempt=2)
        lines = manifest_text(tmp).splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["event"] == "request"
        body = {"success": True, "data": payload(), "meta": {"symbol": "SINI"}}
        cap.response(200, json.dumps(body), body)
        assert cap.finish(ic.OK, cache_ref="daily/SINI.json.gz", cache_sha256="0" * 64)
        assert not cap.finish(ic.ERROR, "a later catch-all")          # the first outcome stands
        try:
            cap.finish("GREAT")
            raise AssertionError("an unknown status was accepted")
        except ValueError:
            pass
        log.begin(qs, attempt=3)                                      # never finished: "died"
        done, pending = ic.read_captures(log.path)
    assert re.fullmatch(r"inv-\d{8}T\d{12}Z", log.run_id), log.run_id
    assert done["capture_id"] == f"{log.run_id}-00001" and done["run_id"] == log.run_id
    assert (done["status"], done["seq"], done["attempt"]) == (ic.OK, 1, 2)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", done["captured_at"])
    assert done["requested_at"] <= done["captured_at"]
    assert (done["schema"], done["source"], done["method"], done["endpoint"]) == (
        ic.SCHEMA_VERSION, "neobdm", "GET", "/api/inventory")
    assert (done["collector"], done["mode"], done["pipeline_run_id"]) == ("test", "daily", "daily-X")
    assert done["writes_cache"] is True
    assert done["requested_brokers"] == sorted(CODES) and done["returned_brokers"] == ["AK", "BK"]
    assert pending["status"] == ic.INCOMPLETE and pending["capture_id"] == f"{log.run_id}-00002"
    assert "captured_at" not in pending
    print("  ok test_request_is_on_disk_before_the_outcome_and_both_read_back")


def test_two_runs_in_the_same_instant_get_their_own_files():
    def fixed():
        return "2026-09-26T10:30:00.000000Z"
    with tempfile.TemporaryDirectory() as tmp:
        a = ic.CaptureLog(tmp, "t", writes_cache=True, clock=fixed)
        b = ic.CaptureLog(tmp, "t", writes_cache=True, clock=fixed)
        a.begin("symbol=AAAA")
        cb = b.begin("symbol=BBBB")
        assert a.run_id == "inv-20260926T103000000000Z" and b.run_id == a.run_id + "-2"
        assert a.path != b.path and cb.capture_id == b.run_id + "-00001"
        assert [c["ticker"] for c in ic.read_captures(a.path)] == ["AAAA"]
        assert [c["ticker"] for c in ic.read_captures(b.path)] == ["BBBB"]
    print("  ok test_two_runs_in_the_same_instant_get_their_own_files")


def test_a_torn_last_line_is_ignored_and_other_damage_is_not():
    with tempfile.TemporaryDirectory() as tmp:
        log = ic.CaptureLog(tmp, "t", writes_cache=True)
        log.begin("symbol=AAAA").finish(ic.ERROR, "x")
        good = manifest_text(tmp)
        with open(log.path, "a", encoding="utf-8") as fh:
            fh.write('{"schema":"inventory_capture_v1","event":"requ')   # killed mid-write
        assert [c["status"] for c in ic.read_captures(log.path)] == [ic.ERROR]
        result_line = good.splitlines()[1]
        for name, text in (("middle.jsonl", "{broken\n" + good),
                           ("orphan.jsonl", result_line + "\n"),
                           ("twice.jsonl", good + result_line + "\n")):
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            try:
                ic.read_captures(path)
                raise AssertionError(f"{name} was read without complaint")
            except ValueError:
                pass
    print("  ok test_a_torn_last_line_is_ignored_and_other_damage_is_not")


def test_the_repository_manifest_folder_is_gitignored():
    with open(os.path.join(HERE, ".gitignore"), encoding="utf-8") as f:
        lines = {line.strip() for line in f}
    assert f"{ic.MANIFEST_DIR}/" in lines
    assert {"inventory_raw/", "broker_learning_raw/"} <= lines        # the cache roots, as before
    print("  ok test_the_repository_manifest_folder_is_gitignored")


# ── harvest_inventory ──────────────────────────────────────────────────────

def test_harvest_records_every_attempt_beside_an_unchanged_cache():
    a = payload(base=1000.0)
    c = payload(base=3000.0, brokers=("AK", "BK", "XL"), zero=("XL",))
    boom = RuntimeError(f"APIRequestContext.get: Timeout 120000ms exceeded.\nCall log:\n"
                        f"  - cookie: sessionid={SECRET}")
    script = {"AAAA": [Resp(200, text=LOGIN_PAGE),
                       Resp(200, {"success": False, "message": "Abnormal usage"}),
                       ok(a, "AAAA")],
              "BBBB": [boom] * hi.MAX_RETRY,
              "CCCC": [ok(c, "CCCC")]}
    with tempfile.TemporaryDirectory() as tmp:
        request = run_harvest(tmp, script, ["AAAA", "BBBB", "CCCC"])
        assert [parse_qs(u.split("?", 1)[1])["symbol"][0] for u in request.urls] == \
            ["AAAA"] * 3 + ["BBBB"] * hi.MAX_RETRY + ["CCCC"]
        assert set(request.timeouts) == {120000}
        assert all(request.recorded_first), request.recorded_first
        # The cache is what it always was: bare data dicts, byte for byte what
        # json.dump wrote before, and readers see nothing else.
        assert sorted(os.listdir(tmp)) == ["AAAA.json.gz", "CCCC.json.gz", ic.MANIFEST_DIR]
        texts = {}
        for t, data in (("AAAA", a), ("CCCC", c)):
            with gzip.open(os.path.join(tmp, f"{t}.json.gz"), "rt", encoding="utf-8") as fh:
                texts[t] = fh.read()
            before = io.StringIO()
            json.dump(data, before)
            assert texts[t] == before.getvalue() and json.loads(texts[t]) == data
        assert sorted(os.path.basename(p) for p in glob.glob(os.path.join(tmp, "*.json.gz"))) == [
            "AAAA.json.gz", "CCCC.json.gz"]
        bad = {}
        assert list(dict(bc.iter_cached(None, "ignored", raw_dir=tmp, legacy=True,
                                        unreadable=bad))) == ["AAAA", "CCCC"] and bad == {}
        text = manifest_text(tmp)
        caps = ic.read_captures(only_manifest(tmp))

    assert [(x["ticker"], x["attempt"], x["status"]) for x in caps] == [
        ("AAAA", 1, ic.NON_JSON), ("AAAA", 2, ic.VENDOR_ERROR), ("AAAA", 3, ic.OK)] + [
        ("BBBB", k, ic.ERROR) for k in range(1, hi.MAX_RETRY + 1)] + [("CCCC", 1, ic.OK)]
    for x in caps:
        assert (x["collector"], x["mode"], x["broker_list_source"], x["writes_cache"]) == (
            "harvest_inventory", None, "broker_codes.json", True)
        assert x["brokers_param"] == CODES and x["requested_brokers"] == sorted(CODES)
        assert x["captured_at"].endswith("Z") and x["capture_id"].startswith(x["run_id"])
    ok_a, ok_c = caps[2], caps[-1]
    assert ok_a["cache_ref"] == "AAAA.json.gz" and ok_a["cache_sha256"] == ic.sha256_text(texts["AAAA"])
    assert ok_c["cache_sha256"] == ic.sha256_text(texts["CCCC"])
    assert ok_a["response_sha256"] == hashlib.sha256(script["AAAA"][2].body()).hexdigest()
    # XL came back at zero: returned. AD was asked for and never came back.
    assert ok_c["returned_brokers"] == ["AK", "BK", "XL"]
    assert "AD" in ok_c["requested_brokers"] and "AD" not in ok_c["returned_brokers"]
    assert caps[0]["reason"] == f"HTTP 200, body is not JSON ({len(LOGIN_PAGE)} chars)"
    assert caps[0]["response_bytes"] == len(LOGIN_PAGE) and caps[0]["returned_brokers"] is None
    assert caps[0]["response_sha256"] == hashlib.sha256(LOGIN_PAGE.encode()).hexdigest()
    assert caps[1]["reason"] == "RuntimeError: AAAA: Abnormal usage" and caps[1]["vendor_success"] is False
    assert caps[3]["reason"] == "RuntimeError: APIRequestContext.get: Timeout 120000ms exceeded."
    assert caps[3]["http_status"] is None
    assert SECRET not in text and "cookie" not in text.lower() and "<title>" not in text
    print("  ok test_harvest_records_every_attempt_beside_an_unchanged_cache")


def test_harvest_records_the_short_window_abort_before_raising():
    script = {"AAAA": [ok(payload(n=40), "AAAA")], "BBBB": [ok(payload(base=2000.0), "BBBB")]}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run_harvest(tmp, script, ["AAAA", "BBBB"])
            raise AssertionError("no abort")
        except SystemExit as e:
            assert "AAAA came back with 40 sessions" in str(e.code), e.code
        assert not os.path.exists(os.path.join(tmp, "AAAA.json.gz"))
        (cap,) = ic.read_captures(only_manifest(tmp))
    assert cap["status"] == ic.ABORTED and cap["session_count"] == 40 and cap["cache_ref"] is None
    assert cap["reason"].startswith("AAAA came back with 40 sessions"), cap["reason"]
    print("  ok test_harvest_records_the_short_window_abort_before_raising")


def test_harvest_records_a_failed_cache_write_and_still_retries():
    script = {"AAAA": [ok(payload(), "AAAA") for _ in range(hi.MAX_RETRY)]}
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "AAAA.json.gz"))       # a directory where the file must go
        request = run_harvest(tmp, script, ["AAAA"], refresh=True)
        caps = ic.read_captures(only_manifest(tmp))
    assert len(request.urls) == hi.MAX_RETRY                     # the retry policy is unchanged
    assert [x["status"] for x in caps] == [ic.CACHE_WRITE_FAILED] * hi.MAX_RETRY
    assert caps[0]["reason"].split(":")[0] in ("PermissionError", "IsADirectoryError"), caps[0]
    assert caps[0]["returned_brokers"] == ["AK", "BK"] and caps[0]["cache_ref"] is None
    print("  ok test_harvest_records_a_failed_cache_write_and_still_retries")


# ── backfill_inventory (selectors, no cache) ───────────────────────────────

def test_backfill_records_selector_captures_and_behaves_as_before():
    a = payload(base=1000.0, brokers=("AK", "BK", "XL"), zero=("XL",))
    boom = RuntimeError(f"APIRequestContext.get: Timeout 60000ms exceeded.\nCall log:\n"
                        f"  - cookie: sessionid={SECRET}")
    vendor_error = Resp(200, {"success": False, "message": "Abnormal usage"})
    no_ohlc = {f: {} for f in FIELDS}
    no_ohlc.update(date=[], ohlc=[])
    script = {"AAAA": [ok(a, "AAAA")],
              "BBBB": [vendor_error],
              "CCCC": [boom],
              "DDDD": [ok(no_ohlc, "DDDD")],
              "EEEE": [ok(a, "EEEE")],                  # AAAA's series under another name
              "FFFF": [Resp(200, text=LOGIN_PAGE)],
              "GGGG": [ok(payload(base=7000.0), "ZZZZ")]}
    names = sorted(script)
    with tempfile.TemporaryDirectory() as tmp:
        out, exit_message, request = run_backfill(tmp, script, names)
        conn = sqlite3.connect(os.path.join(tmp, "neobdm.db"))
        try:
            prices = conn.execute("SELECT ticker, COUNT(*) FROM price_history GROUP BY ticker").fetchall()
            flows = conn.execute("SELECT ticker, broker_code, COUNT(*) FROM broker_flow "
                                 "GROUP BY ticker, broker_code ORDER BY broker_code").fetchall()
        finally:
            conn.close()
        with open(os.path.join(tmp, "topup-failure.json"), encoding="utf-8") as fh:
            snapshot = fh.read()
        assert sorted(os.listdir(tmp)) == [ic.MANIFEST_DIR, "neobdm.db", "topup-failure.json"]
        text = manifest_text(tmp)
        caps = ic.read_captures(only_manifest(tmp))

    # What the backfill did is what it always did.
    assert len(request.urls) == len(names) and set(request.timeouts) == {60000}
    assert all(request.recorded_first), request.recorded_first
    assert "Failed tickers: ['BBBB', 'CCCC', 'EEEE', 'FFFF', 'GGGG']" in out, out
    assert "  no inventory data" in out and "series identical to AAAA" in out
    assert isinstance(exit_message, str) and exit_message.startswith("ABORT: 5/7 tickers failed (71%")
    assert prices == [("AAAA", 120)], prices
    assert flows == [("AAAA", "AK", 120), ("AAAA", "BK", 120), ("AAAA", "XL", 120)], flows
    assert snapshot == vendor_error.text()                        # the first failure's raw body

    # And every request left evidence of it.
    assert [(x["ticker"], x["status"]) for x in caps] == [
        ("AAAA", ic.OK), ("BBBB", ic.VENDOR_ERROR), ("CCCC", ic.ERROR), ("DDDD", ic.EMPTY),
        ("EEEE", ic.REJECTED), ("FFFF", ic.NON_JSON), ("GGGG", ic.REJECTED)]
    for k, x in enumerate(caps):
        assert_selector_request_without_cache(
            x, "backfill_inventory", "backfill_inventory.INVENTORY_BROKERS", bf.INVENTORY_BROKERS)
        sent = request.params(k)
        assert (x["start_date"], x["end_date"]) == (sent["start_date"][0], sent["end_date"][0])
        assert x["captured_at"].endswith("Z")
    good = caps[0]
    assert good["returned_brokers"] == ["AK", "BK", "XL"]      # what the selectors resolved to
    assert (good["session_count"], good["first_session"], good["last_session"]) == (
        120, a["date"][0], a["date"][-1])
    assert good["response_sha256"] == hashlib.sha256(script["AAAA"][0].body()).hexdigest()
    assert good["reason"] is None and good["vendor_meta"] == {"symbol": "AAAA"}
    assert caps[1]["reason"].startswith("InventoryError: BBBB: inventory API status=200 success=False")
    assert caps[2]["reason"] == "RuntimeError: APIRequestContext.get: Timeout 60000ms exceeded."
    assert caps[3]["reason"] == "no ohlc rows: nothing stored" and caps[3]["session_count"] == 0
    assert caps[4]["reason"] == "InventoryError: series identical to AAAA — stale response, not stored"
    assert caps[5]["reason"].startswith("InventoryError: FFFF: inventory API status=200")
    assert caps[6]["reason"] == "InventoryError: API returned symbol ZZZZ for requested GGGG — refusing to store"
    assert SECRET not in text and "cookie" not in text.lower() and "<title>" not in text
    print("  ok test_backfill_records_selector_captures_and_behaves_as_before")


def test_backfill_rolls_back_a_rejected_ticker_before_the_next_commit():
    """Review blocker: insert_inventory writes before the stale-series check can
    reject the ticker, and nothing rolled the rejected rows back, so the NEXT
    ticker's commit() stored them after all while the manifest said REJECTED.
    On master BBBB's rows end up in neobdm.db; here they must not."""
    a, c = payload(base=1000.0), payload(base=3000.0)
    script = {"AAAA": [ok(a, "AAAA")],
              "BBBB": [ok(a, "BBBB")],             # AAAA's series again: stale, rejected
              "CCCC": [ok(c, "CCCC")]}             # commits right after the rejection
    with tempfile.TemporaryDirectory() as tmp:
        out, exit_message, _ = run_backfill(tmp, script, ["AAAA", "BBBB", "CCCC"])
        prices, flows = db_counts(tmp)
        caps = ic.read_captures(only_manifest(tmp))
    assert [(x["ticker"], x["status"]) for x in caps] == [
        ("AAAA", ic.OK), ("BBBB", ic.REJECTED), ("CCCC", ic.OK)]
    assert prices == {"AAAA": 120, "CCCC": 120}, prices          # BBBB gone; AAAA, CCCC kept
    assert flows == {"AAAA": 240, "CCCC": 240}, flows
    assert "FAILED: series identical to AAAA" in out and "Failed tickers: ['BBBB']" in out
    assert exit_message.startswith("ABORT: 1/3 tickers failed (33%"), exit_message   # as before
    print("  ok test_backfill_rolls_back_a_rejected_ticker_before_the_next_commit")


def test_backfill_rolls_back_a_ticker_that_fails_part_way_through_its_writes():
    """The same leak through any other failure: BBBB's price_history rows are
    written, then building its broker_flow rows raises. A later commit must
    not store them either; the manifest says ERROR and nothing of BBBB is kept."""
    broken = payload(base=2000.0)
    broken["nlot"]["AK"][5] = "x"
    script = {"AAAA": [ok(payload(base=1000.0), "AAAA")], "BBBB": [ok(broken, "BBBB")],
              "CCCC": [ok(payload(base=3000.0), "CCCC")]}
    with tempfile.TemporaryDirectory() as tmp:
        out, _, _ = run_backfill(tmp, script, ["AAAA", "BBBB", "CCCC"])
        prices, flows = db_counts(tmp)
        caps = ic.read_captures(only_manifest(tmp))
    assert [(x["ticker"], x["status"]) for x in caps] == [
        ("AAAA", ic.OK), ("BBBB", ic.ERROR), ("CCCC", ic.OK)]
    assert caps[1]["reason"].startswith("TypeError"), caps[1]["reason"]
    assert prices == {"AAAA": 120, "CCCC": 120} and flows == {"AAAA": 240, "CCCC": 240}
    assert "Failed tickers: ['BBBB']" in out
    print("  ok test_backfill_rolls_back_a_ticker_that_fails_part_way_through_its_writes")


def test_backfill_rolls_back_a_ticker_whose_commit_fails():
    """And through a failed commit(): 'database is locked' leaves the
    transaction open, so on master the next ticker's commit stored BBBB after
    all, although BBBB was reported FAILED. Now the manifest's ERROR and the DB
    agree: none of BBBB is kept."""
    class CommitFailsOnce(sqlite3.Connection):
        commits = 0

        def commit(self):
            CommitFailsOnce.commits += 1
            if CommitFailsOnce.commits == 2:                # BBBB's commit
                raise sqlite3.OperationalError("database is locked")
            return super().commit()

    script = {t: [ok(payload(base=1000.0 * k), t)] for k, t in enumerate(("AAAA", "BBBB", "CCCC"), 1)}
    saved = bf.sqlite3
    bf.sqlite3 = SimpleNamespace(connect=lambda path: sqlite3.connect(path, factory=CommitFailsOnce))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = run_backfill(tmp, script, ["AAAA", "BBBB", "CCCC"])
            prices, flows = db_counts(tmp)
            caps = ic.read_captures(only_manifest(tmp))
    finally:
        bf.sqlite3 = saved
    assert [(x["ticker"], x["status"]) for x in caps] == [
        ("AAAA", ic.OK), ("BBBB", ic.ERROR), ("CCCC", ic.OK)]
    assert caps[1]["reason"] == "OperationalError: database is locked"
    assert prices == {"AAAA": 120, "CCCC": 120} and flows == {"AAAA": 240, "CCCC": 240}
    assert "Failed tickers: ['BBBB']" in out
    print("  ok test_backfill_rolls_back_a_ticker_whose_commit_fails")


# ── neobdm_scraper bag holders (selectors, no cache) ───────────────────────

def test_bagholders_record_every_block_and_rank_as_before():
    discovery = payload(n=80, brokers=("AK", "BK"))
    blocks = [payload(n=20, base=1000.0 * (k + 1), brokers=brokers,
                      start=date(2026, 1, 5) + timedelta(weeks=4 * k))
              for k, brokers in enumerate((("AK", "XL"), ("BK", "XL"), ("CC", "AK")))]
    served = [ok(discovery, "AAAA")] + [ok(b, "AAAA") for b in blocks]
    with tempfile.TemporaryDirectory() as tmp, no_cache_root(tmp):
        request = FakeRequest(tmp, {"AAAA": list(served)})
        page = SimpleNamespace(context=SimpleNamespace(request=request))
        holders = ns.get_inventory_bagholders(page, "AAAA")       # captures=None: its own log
        caps = ic.read_captures(only_manifest(tmp))

    # Ranked exactly as before, from the three blocks the discovery defined.
    assert holders == bagholders_from_payloads([json.loads(r.text()) for r in served[1:]], 2)
    expected_blocks = inventory_date_blocks(json.loads(served[0].text()),
                                            ns.BAGHOLDER_TRADING_DAYS, ns.BAGHOLDER_BLOCK_TRADING_DAYS)
    assert len(expected_blocks) == 3 and all(request.recorded_first)
    assert set(request.timeouts) == {60000}
    assert [x["status"] for x in caps] == [ic.OK] * 4
    for k, x in enumerate(caps):
        assert_selector_request_without_cache(
            x, "neobdm_scraper", "neobdm_scraper.BAGHOLDER_BROKERS", ns.BAGHOLDER_BROKERS,
            mode="bagholders")
        sent = request.params(k)
        assert (x["ticker"], x["start_date"], x["end_date"]) == (
            "AAAA", sent["start_date"][0], sent["end_date"][0])
    window = [(x["start_date"], x["end_date"]) for x in caps]
    first, last = (date.fromisoformat(d) for d in window[0])
    assert (last - first).days == ns.BAGHOLDER_DISCOVERY_CALENDAR_DAYS    # the discovery window
    assert window[1:] == expected_blocks                                   # then each block
    assert [x["returned_brokers"] for x in caps] == [["AK", "BK"], ["AK", "XL"], ["BK", "XL"],
                                                     ["AK", "CC"]]
    assert [x["session_count"] for x in caps] == [80, 20, 20, 20]
    assert caps[1]["first_session"] == blocks[0]["date"][0]
    print("  ok test_bagholders_record_every_block_and_rank_as_before")


def test_bagholder_failures_are_recorded_and_raised_as_before():
    empty = payload(n=0)
    boom = RuntimeError(f"APIRequestContext.get: Timeout 60000ms exceeded.\nCall log:\n"
                        f"  - cookie: sessionid={SECRET}")
    cases = [("BBBB", Resp(200, text=LOGIN_PAGE), ic.NON_JSON,
              "BBBB: inventory API status=200 success=None"),
             ("CCCC", Resp(200, {"success": False}), ic.VENDOR_ERROR,
              "CCCC: inventory API status=200 success=False"),
             ("DDDD", ok(payload(), "ZZZZ"), ic.REJECTED,
              "inventory API returned ZZZZ for requested DDDD"),
             ("EEEE", boom, ic.ERROR, "APIRequestContext.get: Timeout 60000ms exceeded."),
             ("FFFF", ok(empty, "FFFF"), ic.OK,                 # fetched fine; nothing to rank
              "FFFF: inventory API returned no trading dates")]
    with tempfile.TemporaryDirectory() as tmp:
        log = ic.CaptureLog(tmp, "neobdm_scraper", writes_cache=False, mode="bagholders",
                            broker_list_source="neobdm_scraper.BAGHOLDER_BROKERS")
        request = FakeRequest(tmp, {t: [r] for t, r, _, _ in cases})
        page = SimpleNamespace(context=SimpleNamespace(request=request))
        for ticker, _, _, message in cases:
            try:
                ns.get_inventory_bagholders(page, ticker, captures=log)
                raise AssertionError(f"{ticker} did not raise")
            except RuntimeError as e:
                assert str(e).split("\nCall log:")[0] == message, (ticker, str(e))
        text = manifest_text(tmp)
        caps = ic.read_captures(log.path)
    assert [(x["ticker"], x["status"]) for x in caps] == [(t, s) for t, _, s, _ in cases]
    assert caps[-1]["session_count"] == 0 and caps[-1]["cache_ref"] is None
    assert caps[3]["reason"] == "RuntimeError: APIRequestContext.get: Timeout 60000ms exceeded."
    assert all(x["requested_brokers"] is None and x["writes_cache"] is False for x in caps)
    assert SECRET not in text and "<title>" not in text
    print("  ok test_bagholder_failures_are_recorded_and_raised_as_before")


def test_broker_stalker_shares_one_manifest_and_reports_as_before():
    """scrape_broker_stalker end to end, with the DOM netflow scan faked: one
    run file for all its lookups, and the same results and failure flag."""
    def blocks_for(base):
        return [ok(payload(n=80, base=base), "X")] + [
            ok(payload(n=20, base=base + k, start=date(2026, 1, 5) + timedelta(weeks=4 * k)), "X")
            for k in range(3)]
    script = {"AAAA": blocks_for(1000.0), "BBBB": [Resp(200, text=LOGIN_PAGE)],
              "CCCC": blocks_for(3000.0)}
    for t in ("AAAA", "CCCC"):                     # each answer names the ticker it is for
        script[t] = [ok(json.loads(r.text())["data"], t) for r in script[t]]
    rows = {"AAAA": "-5.0", "BBBB": "-3.0", "CCCC": "-1.0", "DDDD": "2.0"}
    saved = ns.get_netflow
    ns.get_netflow = lambda page, codes, duration="Today", side="dist": {
        t: {"symbol": t, "netval": v, "savg": "100"} for t, v in rows.items()}
    try:
        with tempfile.TemporaryDirectory() as tmp, no_cache_root(tmp):
            request = FakeRequest(tmp, script)
            page = SimpleNamespace(context=SimpleNamespace(request=request),
                                   goto=lambda *a, **k: None, wait_for_timeout=lambda ms: None)
            results = ns.scrape_broker_stalker(page)
            caps = ic.read_captures(only_manifest(tmp))          # exactly one run file
    finally:
        ns.get_netflow = saved
    assert [(r["symbol"], r["netval"], r["holders_failed"]) for r in results] == [
        ("AAAA", "-5.0", False), ("BBBB", "-3.0", True), ("CCCC", "-1.0", False)]
    expected = bagholders_from_payloads([json.loads(r.text()) for r in script["AAAA"][1:]], 2)
    assert results[0]["holders"] == expected and results[1]["holders"] == []
    assert [(x["ticker"], x["status"]) for x in caps] == (
        [("AAAA", ic.OK)] * 4 + [("BBBB", ic.NON_JSON)] + [("CCCC", ic.OK)] * 4)
    assert len({x["run_id"] for x in caps}) == 1
    assert all(x["mode"] == "bagholders" and x["cache_ref"] is None for x in caps)
    print("  ok test_broker_stalker_shares_one_manifest_and_reports_as_before")


# ── HTTP status classification (review blocker) ────────────────────────────

HTTP_ERRORS = [   # (label, response, vendor_success the body carries)
    ("HTTP 500 + HTML", Resp(500, text="<html><body>Internal Server Error</body></html>"), None),
    ("HTTP 500 + JSON success=false", Resp(500, {"success": False, "message": "server error"}), False),
    ("HTTP 429 + JSON throttle", Resp(429, {"success": False, "message": "Abnormal usage detected"}),
     False),
]


def test_http_error_outranks_every_refusal_but_not_what_was_kept():
    """The rule itself, over every status and code: a refusal of an HTTP >= 400
    answer is HTTP_ERROR; an outcome about kept or used data is left alone, and
    without a response there is no code to go by."""
    cases = []
    with tempfile.TemporaryDirectory() as tmp:
        log = ic.CaptureLog(tmp, "t", writes_cache=True)
        for code in (None, 200, 399, 400, 429, 500):
            for status in ic.STATUSES:
                cap = log.begin("symbol=AAAA")
                if code is not None:
                    cap.response(code, '{"success": false}', {"success": False}, b'{"success": false}')
                cap.finish(status, "the check that fired")
                cases.append((code, status))
        caps = ic.read_captures(log.path)
    assert set(ic.HTTP_ERROR_OUTRANKS) == {ic.NON_JSON, ic.VENDOR_ERROR, ic.REJECTED, ic.ERROR}
    for (code, sent), cap in zip(cases, caps):
        want = (ic.HTTP_ERROR if sent in ic.HTTP_ERROR_OUTRANKS and code is not None and code >= 400
                else sent)
        assert cap["status"] == want, (code, sent, cap["status"])
        assert cap["reason"] == "the check that fired" and cap["http_status"] == code
    print("  ok test_http_error_outranks_every_refusal_but_not_what_was_kept")


def test_an_http_error_is_http_error_on_every_collector():
    """HTTP 500 + HTML, HTTP 500 + JSON success=false and HTTP 429 + a JSON
    throttle answer, through each of the four collectors. Every collector
    refuses these attempts, so every one is HTTP_ERROR,
    with the status code, the digest, vendor_success and the reason each check
    gave kept; and each collector reacts exactly as it did before."""
    for label, bad, vendor_success in HTTP_ERRORS:
        code, text = bad.status, bad.text()
        msg = None if vendor_success is None else json.loads(text)["message"]
        expected = {
            "broker_collect": f"HTTP {code}",
            "harvest_inventory": (f"HTTP {code}, body is not JSON ({len(text)} chars)" if msg is None
                                  else f"RuntimeError: AAAA: {msg}"),
            "backfill_inventory": (f"InventoryError: AAAA: inventory API status={code} "
                                   f"success={vendor_success} message={msg!r}"),
            "neobdm_scraper": f"RuntimeError: AAAA: inventory API status={code} success={vendor_success}",
        }
        got = {}
        # broker_collect refuses it and retries, as before
        with tempfile.TemporaryDirectory() as tmp:
            res = run_collect(tmp, [bad, ok(payload(), "AAAA")])
            got["broker_collect"] = ic.read_captures(only_manifest(tmp))
        assert res["ok"] == ["AAAA"] and res["failed"] == {}, (label, res)
        # harvest_inventory refuses it and retries, as before
        with tempfile.TemporaryDirectory() as tmp:
            request = run_harvest(tmp, {"AAAA": [bad, ok(payload(), "AAAA")]}, ["AAAA"])
            assert len(request.urls) == 2 and os.path.exists(os.path.join(tmp, "AAAA.json.gz"))
            got["harvest_inventory"] = ic.read_captures(only_manifest(tmp))
        # backfill_inventory fails that ticker and stores the rest, as before
        with tempfile.TemporaryDirectory() as tmp:
            script = {"AAAA": [bad]}
            script.update({t: [ok(payload(base=1000.0 * k), t)]
                           for k, t in enumerate(("BBBB", "CCCC", "DDDD"), 1)})
            out, exit_message, _ = run_backfill(tmp, script, sorted(script))
            prices, _ = db_counts(tmp)
            got["backfill_inventory"] = ic.read_captures(only_manifest(tmp))
        assert "Failed tickers: ['AAAA']" in out and exit_message is None, (label, out, exit_message)
        assert sorted(prices) == ["BBBB", "CCCC", "DDDD"], prices
        # the bag-holder lookup raises, as before
        with tempfile.TemporaryDirectory() as tmp:
            log = ic.CaptureLog(tmp, "neobdm_scraper", writes_cache=False, mode="bagholders")
            page = SimpleNamespace(context=SimpleNamespace(request=FakeRequest(tmp, {"AAAA": [bad]})))
            try:
                ns.get_inventory_bagholders(page, "AAAA", captures=log)
                raise AssertionError(f"{label}: the bag-holder lookup did not raise")
            except RuntimeError as e:
                assert str(e) == f"AAAA: inventory API status={code} success={vendor_success}", str(e)
            got["neobdm_scraper"] = ic.read_captures(log.path)

        for collector, caps in got.items():
            first = caps[0]
            assert first["status"] == ic.HTTP_ERROR, (label, collector, first["status"])
            assert first["http_status"] == code and first["vendor_success"] is vendor_success
            assert first["response_sha256"] == hashlib.sha256(bad.body()).hexdigest()
            assert first["reason"] == expected[collector], (label, collector, first["reason"])
            assert first["cache_ref"] is None
    print("  ok test_an_http_error_is_http_error_on_every_collector")


class UndecodableResp:
    """Like Playwright's APIResponse, whose text() is body().decode(): an error
    page that is not UTF-8 makes text() raise."""

    def __init__(self, status, raw):
        self.status, self._raw = status, raw

    def body(self):
        return self._raw

    def text(self):
        return self._raw.decode("utf-8")


def test_an_undecodable_http_error_is_http_error_on_every_collector():
    """Review follow-up: harvest read r.text() before putting the response on
    record, so an HTTP 500 page that is not UTF-8 went down as ERROR with no
    status code or digest, where the other collectors said HTTP_ERROR. The
    status and bytes now go on record first; the runtime is unchanged."""
    bad = UndecodableResp(500, "<html>Erreur interne du serveur: é</html>".encode("latin-1"))
    digest = hashlib.sha256(bad.body()).hexdigest()
    got = {}
    with tempfile.TemporaryDirectory() as tmp:
        res = run_collect(tmp, [bad, ok(payload(), "AAAA")])
        got["broker_collect"] = ic.read_captures(only_manifest(tmp))[0]
    assert res["ok"] == ["AAAA"], res
    with tempfile.TemporaryDirectory() as tmp:
        request = run_harvest(tmp, {"AAAA": [bad, ok(payload(), "AAAA")]}, ["AAAA"])
        assert len(request.urls) == 2 and os.path.exists(os.path.join(tmp, "AAAA.json.gz"))
        got["harvest_inventory"] = ic.read_captures(only_manifest(tmp))[0]
    with tempfile.TemporaryDirectory() as tmp:
        out, _, _ = run_backfill(tmp, {"AAAA": [bad], "BBBB": [ok(payload(), "BBBB")],
                                       "CCCC": [ok(payload(base=2000.0), "CCCC")],
                                       "DDDD": [ok(payload(base=3000.0), "DDDD")]},
                                 ["AAAA", "BBBB", "CCCC", "DDDD"])
        assert "Failed tickers: ['AAAA']" in out
        got["backfill_inventory"] = ic.read_captures(only_manifest(tmp))[0]
    with tempfile.TemporaryDirectory() as tmp:
        log = ic.CaptureLog(tmp, "neobdm_scraper", writes_cache=False, mode="bagholders")
        page = SimpleNamespace(context=SimpleNamespace(request=FakeRequest(tmp, {"AAAA": [bad]})))
        try:
            ns.get_inventory_bagholders(page, "AAAA", captures=log)
            raise AssertionError("the bag-holder lookup did not raise")
        except RuntimeError as e:
            assert str(e) == "AAAA: inventory API status=500 success=None", str(e)
        got["neobdm_scraper"] = ic.read_captures(log.path)[0]
    for collector, cap in got.items():
        assert (cap["status"], cap["http_status"]) == (ic.HTTP_ERROR, 500), (collector, cap["status"])
        assert cap["response_sha256"] == digest and cap["response_bytes"] == len(bad.body()), collector
    assert got["harvest_inventory"]["reason"].startswith("UnicodeDecodeError"), got["harvest_inventory"]
    print("  ok test_an_undecodable_http_error_is_http_error_on_every_collector")


def test_a_payload_used_despite_an_http_error_keeps_its_outcome():
    """status is the collector's outcome, http_status the transport fact.
    harvest_inventory, backfill_inventory and the bag-holder lookup never look
    at the status code, so a JSON success=true answer with HTTP 500 is cached,
    stored or ranked, as it always was. Each capture is OK with http_status 500;
    calling it HTTP_ERROR beside a cache_ref would contradict the cache."""
    def odd(n=120, base=1000.0):
        return Resp(500, {"success": True, "data": payload(n=n, base=base), "meta": {"symbol": "AAAA"}})

    with tempfile.TemporaryDirectory() as tmp:
        run_harvest(tmp, {"AAAA": [odd()]}, ["AAAA"])
        assert os.path.exists(os.path.join(tmp, "AAAA.json.gz"))
        (cap,) = ic.read_captures(only_manifest(tmp))
    assert (cap["status"], cap["http_status"], cap["cache_ref"]) == (ic.OK, 500, "AAAA.json.gz")

    with tempfile.TemporaryDirectory() as tmp:
        out, exit_message, _ = run_backfill(tmp, {"AAAA": [odd()]}, ["AAAA"])
        prices, _ = db_counts(tmp)
        (cap,) = ic.read_captures(only_manifest(tmp))
    assert prices == {"AAAA": 120} and exit_message is None          # stored, as before
    assert (cap["status"], cap["http_status"], cap["cache_ref"]) == (ic.OK, 500, None)

    with tempfile.TemporaryDirectory() as tmp:
        served = [odd(n=80)] + [odd(n=20, base=1000.0 + k) for k in range(3)]
        log = ic.CaptureLog(tmp, "neobdm_scraper", writes_cache=False, mode="bagholders")
        page = SimpleNamespace(context=SimpleNamespace(request=FakeRequest(tmp, {"AAAA": served})))
        holders = ns.get_inventory_bagholders(page, "AAAA", captures=log)   # ranked, as before
        caps = ic.read_captures(log.path)
    assert holders == bagholders_from_payloads([json.loads(r.text()) for r in served[1:]], 2)
    assert [(x["status"], x["http_status"], x["cache_ref"]) for x in caps] == [(ic.OK, 500, None)] * 4
    print("  ok test_a_payload_used_despite_an_http_error_keeps_its_outcome")


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def _repository_manifest():
    return sorted(glob.glob(os.path.join(HERE, ic.MANIFEST_DIR, "*")))


def main():
    before = _repository_manifest()
    print(f"inventory capture: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
    assert _repository_manifest() == before, "a test wrote into the repository's manifest"
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
