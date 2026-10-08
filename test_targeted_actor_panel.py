"""Plain-script tests for the targeted broker actor panel (py -3 test_targeted_actor_panel.py):
targeted_selectors, targeted_actor_panel, targeted_actor_db, coverage_guard and the
optional TARGETED_SELECTORS / FULL_EXPLICIT fields of inventory_capture.

Offline only, synthetic payloads only. FakeVendor answers /api/inventory the way the
server was verified to (2026-09-27): it keeps the first 10 raw `brokers` values,
echoes them sorted in meta.brokers, and returns the exact UNION of each selector's
top n over its FULL universe, keyed alphabetically with every chosen broker's whole
series. Its selection is computed over the whole universe, independently of
targeted_actor_panel, which only ever sees the union. neobdm_scraper and Playwright
are blocked from importing, so a test that forgot to inject a request function fails
instead of logging in.
"""

import copy
import glob
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl
from unittest.mock import patch

for _blocked in ("neobdm_scraper", "playwright", "playwright.sync_api"):
    sys.modules.setdefault(_blocked, None)

import broker_book as bb                 # noqa: E402
import broker_collect as bc              # noqa: E402
import broker_learning_db as bldb        # noqa: E402
import coverage_guard as cg              # noqa: E402
import inventory_capture as ic           # noqa: E402
from price_contract import CONTRACT_VERSION, UnsupportedPriceContract  # noqa: E402
import targeted_actor_db as tdb          # noqa: E402
import targeted_actor_panel as tap       # noqa: E402
import targeted_selectors as ts          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
SD, ED = bc.start_date(NOW), bc.end_date(NOW)
FIELDS = ("blot", "bval", "slot", "sval", "nlot", "nval")
CODES = bc.load_codes()

tap.log.addHandler(logging.NullHandler())
bc.log.addHandler(logging.NullHandler())


# ── the synthetic market ───────────────────────────────────────────────────

def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def broker_series(net_lots, net_vals, churn=0, churn_val=0):
    """Six series from daily net lots and net values (nlot = blot - slot and
    nval = bval - sval exactly), with `churn` lots bought AND sold each day."""
    s = {f: [] for f in FIELDS}
    for d, v in zip(net_lots, net_vals):
        s["blot"].append(max(d, 0) + churn)
        s["slot"].append(max(-d, 0) + churn)
        s["bval"].append(max(v, 0) + churn_val)
        s["sval"].append(max(-v, 0) + churn_val)
        s["nlot"].append(d)
        s["nval"].append(v)
    return s


# The hand-built ticker AAAA: 120 sessions in four segments, so each horizon
# sees a different slice. Daily net lots per segment (S1, S2, S3, S4):
#   S1 = sessions 0..69 (ALL only)      S2 = 70..99 (C50, ALL)
#   S3 = 100..114 (C20, C50, ALL)       S4 = 115..119 (every horizon)
# Window sums: C5 = 5*d4, C20 = 15*d3 + C5, C50 = 30*d2 + C20, ALL = 70*d1 + C50.
N_SESSIONS = 120
SEGMENTS = ((0, 70), (70, 100), (100, 115), (115, 120))
DAILY = {            # d1, d2, d3, d4 (lots/day); rupiah per lot
    "AK": ((0, 0, 0, 100), 100_000),
    "BK": ((0, 0, 40, -10), 1_000_000),
    "CC": ((0, 30, 0, 0), 1_000_000),
    "DR": ((20, 0, 0, 0), 1_000_000),
    "EP": ((0, 0, 30, 0), 1_000_000),
    "XL": ((0, 0, 0, -80), 110_000),
    "YP": ((0, 0, -50, 20), 1_000_000),
    "KZ": ((0, -40, 0, 0), 1_000_000),
    "MG": ((-30, 0, 0, 0), 1_000_000),
    "NI": ((1, 1, 1, 1), 900_000),
    "OD": ((-1, -1, -1, -1), 900_000),
    "PD": ((0, 2, 2, 2), 1_000_000),
    "RX": ((0, -2, -2, -2), 1_000_000),
    "SQ": ((3, 0, 0, 3), 1_000_000),
    "TP": ((-3, 0, 0, -3), 1_000_000),
}

# Hand-computed from DAILY (see the window sums above): the answer, rank by rank.
EXPECTED = {
    ("C5", "NB_LOT"): [("AK", 500), ("YP", 100), ("SQ", 15), ("PD", 10), ("NI", 5)],
    ("C5", "NS_LOT"): [("XL", -400), ("BK", -50), ("TP", -15), ("RX", -10), ("OD", -5)],
    ("C5", "NB_VAL"): [("YP", 100e6), ("AK", 50e6), ("SQ", 15e6), ("PD", 10e6), ("NI", 4.5e6)],
    ("C5", "NS_VAL"): [("BK", -50e6), ("XL", -44e6), ("TP", -15e6), ("RX", -10e6), ("OD", -4.5e6)],
    ("C20", "NB_LOT"): [("BK", 550), ("AK", 500), ("EP", 450), ("PD", 40), ("NI", 20)],
    ("C20", "NS_LOT"): [("YP", -650), ("XL", -400), ("RX", -40), ("OD", -20), ("TP", -15)],
    ("C20", "NB_VAL"): [("BK", 550e6), ("EP", 450e6), ("AK", 50e6), ("PD", 40e6), ("NI", 18e6)],
    ("C20", "NS_VAL"): [("YP", -650e6), ("XL", -44e6), ("RX", -40e6), ("OD", -18e6), ("TP", -15e6)],
    ("C50", "NB_LOT"): [("CC", 900), ("BK", 550), ("AK", 500), ("EP", 450), ("PD", 100)],
    ("C50", "NS_LOT"): [("KZ", -1200), ("YP", -650), ("XL", -400), ("RX", -100), ("OD", -50)],
    ("C50", "NB_VAL"): [("CC", 900e6), ("BK", 550e6), ("EP", 450e6), ("PD", 100e6), ("AK", 50e6)],
    ("C50", "NS_VAL"): [("KZ", -1200e6), ("YP", -650e6), ("RX", -100e6), ("OD", -45e6), ("XL", -44e6)],
    ("ALL", "NB_LOT"): [("DR", 1400), ("CC", 900), ("BK", 550), ("AK", 500), ("EP", 450)],
    ("ALL", "NS_LOT"): [("MG", -2100), ("KZ", -1200), ("YP", -650), ("XL", -400), ("TP", -225)],
    ("ALL", "NB_VAL"): [("DR", 1400e6), ("CC", 900e6), ("BK", 550e6), ("EP", 450e6), ("SQ", 225e6)],
    ("ALL", "NS_VAL"): [("MG", -2100e6), ("KZ", -1200e6), ("YP", -650e6), ("TP", -225e6), ("OD", -108e6)],
}
UNION_A = {"AK", "BK", "EP", "NI", "OD", "PD", "RX", "SQ", "TP", "XL", "YP"}       # 11 > 10
UNION_B = {"AK", "BK", "CC", "DR", "EP", "KZ", "MG", "OD", "PD", "RX", "SQ", "TP", "XL", "YP"}


def ohlc_rows(dates, base):
    return [{"date": d, "open": base + i, "high": base + i + 5, "low": base + i - 5,
             "close": base + i, "volume": 100_000 + i, "volume_sma20": 100_000.0}
            for i, d in enumerate(dates)]


def segment_market(daily, base=1000.0):
    """A 120-session universe from {code: ((d1, d2, d3, d4) lots/day, rupiah
    per lot)} over SEGMENTS (window sums as in the comment above)."""
    dates = weekdays("2025-10-01", N_SESSIONS)
    brokers = {}
    for code, (per_seg, rp_per_lot) in daily.items():
        lots = [per_seg[k] for k, (lo, hi) in enumerate(SEGMENTS) for _ in range(lo, hi)]
        brokers[code] = broker_series(lots, [x * rp_per_lot for x in lots])
    return {"dates": dates, "ohlc": ohlc_rows(dates, base), "brokers": brokers}


def handbuilt_market(base=1000.0):
    """AAAA's full universe: the DAILY brokers, LG (active, net zero every day,
    so never selected) and ZP (never trades)."""
    market = segment_market(DAILY, base)
    market["brokers"]["LG"] = broker_series([0] * N_SESSIONS, [0] * N_SESSIONS, churn=7,
                                            churn_val=7_000_000)
    market["brokers"]["ZP"] = broker_series([0] * N_SESSIONS, [0] * N_SESSIONS)
    return market


def random_market(seed, n_brokers=30, n=120, base=500.0):
    """A seeded universe with prices that move, so value and lot rankings differ."""
    rng = random.Random(seed)
    dates = weekdays("2025-10-06", n)
    codes = sorted(rng.sample(CODES, n_brokers))
    price = [base * (1 + 0.3 * ((i * 7919 + seed) % 97) / 97) for i in range(n)]
    brokers = {}
    for code in codes:
        lots = [rng.randint(-5000, 5000) if rng.random() < 0.6 else 0 for _ in range(n)]
        vals = [x * round(100 * price[i]) for i, x in enumerate(lots)]
        brokers[code] = broker_series(lots, vals, churn=rng.randint(0, 50), churn_val=rng.randint(0, 10**7))
    return {"dates": dates, "ohlc": ohlc_rows(dates, base + seed), "brokers": brokers}


# ── the fake vendor ────────────────────────────────────────────────────────

class Resp:
    """Stands in for Playwright's APIResponse: .status, .text() and .body()."""

    def __init__(self, status=200, body=None, text=None):
        self.status = status
        self._text = text if text is not None else json.dumps(body)

    def text(self):
        return self._text

    def body(self):
        return self._text.encode("utf-8")


def vendor_picks(market, token, tie="asc"):
    """[(broker, window sum)] the verified server returns for one selector, over
    the WHOLE universe: top n by the horizon sum, NB > 0, NS < 0. How the real
    server breaks a tie is unverified; `tie` picks among equal sums by broker
    code ascending or descending, so a test can show the panel does not
    depend on it."""
    m = re.fullmatch(r"TOP_(\d+)_(NB|NS)_(LOT|VAL)_(C\d+|ALL)", token)
    n, tx, unit, period = int(m.group(1)), m.group(2), m.group(3), m.group(4)
    field = "nlot" if unit == "LOT" else "nval"
    size = len(market["dates"])
    k = size if period == "ALL" else int(period[1:])
    totals = []
    for code in sorted(market["brokers"]):
        total = 0
        for i in range(max(0, size - k), size):
            total += market["brokers"][code][field][i]
        totals.append((code, total))
    sign = 1 if tx == "NB" else -1
    chosen = [t for t in totals if sign * t[1] > 0]
    chosen.sort(key=lambda t: t[0], reverse=(tie == "desc"))       # the tie policy...
    chosen.sort(key=lambda t: -sign * t[1])                       # ...under a stable sort
    return chosen[:n]


class FakeVendor:
    """Serves scripted markets per ticker like the verified /api/inventory, and
    records every query. `mutate(ticker, kept_values, env)` may edit a response;
    `responses[(ticker, n)]` replaces the n-th (0-based) response for a ticker."""

    def __init__(self, markets, mutate=None, responses=None, tie="asc"):
        self.markets = markets
        self.mutate = mutate
        self.tie = tie
        self.responses = dict(responses or {})
        self.queries = []
        self.per_ticker = {}

    def picks(self, ticker, token):
        return vendor_picks(self.markets[ticker], token, self.tie)

    def get(self, qs):
        pairs = parse_qsl(qs)
        self.queries.append(qs)
        symbol = dict(pairs)["symbol"]
        k = self.per_ticker.get(symbol, 0)
        self.per_ticker[symbol] = k + 1
        if (symbol, k) in self.responses:
            return self.responses[(symbol, k)]
        kept = [v for name, v in pairs if name == "brokers"][:10]     # the server's cap
        market = self.markets[symbol]
        chosen = set()
        for value in kept:
            if re.fullmatch(r"[A-Z]{2}", value):
                if value in market["brokers"]:
                    chosen.add(value)
            else:
                chosen |= {b for b, _ in vendor_picks(market, value, self.tie)}
        data = {"date": list(market["dates"]), "ohlc": copy.deepcopy(market["ohlc"])}
        for f in FIELDS:
            data[f] = {b: list(market["brokers"][b][f]) for b in sorted(chosen)}
        q = dict(pairs)
        env = {"success": True, "message": "Success Get Inventory", "errors": [], "data": data,
               "meta": {"symbol": symbol, "brokers": sorted(kept), "start_date": q["start_date"],
                        "end_date": q["end_date"], "investor_type": q["investor_type"]}}
        if self.mutate is not None:
            env = self.mutate(symbol, kept, env)
        return Resp(200, env)


def tmpdir():
    # A failed assertion leaves a connection open, and Windows then cannot
    # delete the file: the cleanup error must not mask the assertion.
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def run(vendor, tickers, tmp, db="panel.db", sleeps=None, **kw):
    return tap.collect(tickers, os.path.join(tmp, db), sleep=(sleeps if sleeps is not None else []).append,
                       now=NOW, request_get=vendor.get, **kw)


def captures(result):
    return ic.read_captures(result["manifest_path"])


def lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def snapshot_rows(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def assert_nothing_stored(db):
    for table in ("panel_snapshots",) + tdb.SNAPSHOT_TABLES:
        assert snapshot_rows(db, f"SELECT COUNT(*) FROM {table}") == [(0,)], table


# ── 1. planner ─────────────────────────────────────────────────────────────

def test_selector_grammar_and_the_v1_allowlist():
    assert len(ts.VENDOR_LISTED_SELECTORS) == 168                 # 3 x 4 x 2 x 7, as listed
    assert len(ts.V1_ALLOWLIST) == 16 and ts.V1_ALLOWLIST <= ts.VENDOR_LISTED_SELECTORS
    assert ts.V1_ALLOWLIST == {f"TOP_5_{tx}_{u}_{p}" for tx in ("NB", "NS") for u in ("LOT", "VAL")
                               for p in ("C5", "C20", "C50", "ALL")}
    sel = ts.parse_selector("TOP_5_NS_VAL_C50")
    assert (sel.n, sel.tx, sel.unit, sel.period, sel.metric, sel.field, sel.sign, sel.sessions) == (
        5, "NS", "VAL", "C50", "NS_VAL", "nval", -1, 50)
    assert ts.parse_selector("TOP_5_NB_LOT_ALL").sessions is None
    refused = ["TOP_5_NB_LOT_C60", "TOP_10_NB_LOT_C20", "TOP_5_NB_VALUE_C20", "top_5_nb_lot_c20",
               " TOP_5_NB_LOT_C20", "TOP_5_BUY_LOT_C20", "TOP_3_NB_LOT_C20", "TOP_5_NB_LOT_C10",
               "AK", "ALL", "", None, 5]
    for token in refused:
        try:
            ts.parse_selector(token)
        except ts.SelectorPlanError:
            pass
        else:
            raise AssertionError(f"{token!r} accepted")
        try:
            ts.check_selector_tokens([token])
        except ts.SelectorPlanError:
            continue
        raise AssertionError(f"{token!r} passed the request guard")
    # C60 and TOP_10 work at the vendor but are not listed: not even vendor-listed here
    assert "TOP_5_NB_LOT_C60" not in ts.VENDOR_LISTED_SELECTORS
    assert "TOP_10_NB_LOT_C20" not in ts.VENDOR_LISTED_SELECTORS
    print("  ok test_selector_grammar_and_the_v1_allowlist")


def test_the_plan_is_two_requests_of_eight_grouped_by_horizon():
    plan = ts.build_plan()
    assert plan == ts.PLAN and [s.group for s in plan] == ["A", "B"]
    assert plan[0].horizons == ("C5", "C20") and plan[1].horizons == ("C50", "ALL")
    assert [len(s.tokens) for s in plan] == [8, 8]
    assert plan[0].tokens == ("TOP_5_NB_LOT_C5", "TOP_5_NS_LOT_C5", "TOP_5_NB_VAL_C5",
                              "TOP_5_NS_VAL_C5", "TOP_5_NB_LOT_C20", "TOP_5_NS_LOT_C20",
                              "TOP_5_NB_VAL_C20", "TOP_5_NS_VAL_C20")
    assert set(plan[0].tokens) | set(plan[1].tokens) == ts.V1_ALLOWLIST
    assert not set(plan[0].tokens) & set(plan[1].tokens)
    assert ts.build_plan() == plan                                   # deterministic
    print("  ok test_the_plan_is_two_requests_of_eight_grouped_by_horizon")


def test_more_than_ten_tokens_is_refused_before_the_network():
    eleven = list(ts.PLAN[0].tokens + ts.PLAN[1].tokens[:3])
    for bad in (eleven, list(ts.V1_ALLOWLIST), ["TOP_5_NB_LOT_C5"] * 2, []):
        try:
            ts.build_query("AAAA", bad, SD, ED)
        except ts.SelectorPlanError as e:
            assert bad or "no selector" in str(e)
            continue
        raise AssertionError(f"{len(bad)} tokens built a query")
    try:
        ts.check_explicit_codes(CODES[:11], CODES)
    except ts.SelectorPlanError as e:
        assert "never truncated" in str(e)
    else:
        raise AssertionError("11 explicit codes passed")
    for bad in (["ALL"], ["AK", "AK"], ["QQ"], []):
        try:
            ts.check_explicit_codes(bad, CODES)
        except ts.SelectorPlanError:
            continue
        raise AssertionError(bad)
    assert ts.check_explicit_codes(CODES[:10], CODES) == tuple(CODES[:10])

    # And through the collector: a plan over the cap never reaches get(), and
    # leaves no request line behind.
    bad_plan = (ts.RequestSpec("A", ("C5", "C20", "C50"), tuple(eleven)),)
    vendor = FakeVendor({"AAAA": handbuilt_market()})
    with tmpdir() as tmp:
        log = ic.CaptureLog(tmp, "t", writes_cache=False, collection_mode=ic.TARGETED_SELECTORS,
                            selector_plan=ts.PLAN_ID)
        conn = tdb.connect(os.path.join(tmp, "p.db"))
        tdb.start_run(conn, "r", ts.PLAN_ID, "now")
        session = tap._Session(vendor.get, lambda: None, bc._safe_error_local, [].append, log)
        try:
            tap._run(["AAAA"], conn, session, NOW, bad_plan, CODES, "r")
        except ts.SelectorPlanError:
            pass
        else:
            raise AssertionError("an 11-token plan ran")
        finally:
            conn.close()
        assert vendor.queries == [] and log.path is None
    print("  ok test_more_than_ten_tokens_is_refused_before_the_network")


def test_a_valid_eight_token_request():
    vendor = FakeVendor({"AAAA": handbuilt_market()})
    with tmpdir() as tmp:
        res = run(vendor, ["AAAA"], tmp)
        assert res["ok"] == ["AAAA"] and res["failed"] == {}, res
        assert len(vendor.queries) == 2 and res["requests"] == 2
        for qs, spec in zip(vendor.queries, ts.PLAN):
            pairs = parse_qsl(qs)
            assert [v for k, v in pairs if k == "brokers"] == list(spec.tokens)   # in plan order
            q = dict(pairs)
            assert (q["symbol"], q["start_date"], q["end_date"], q["investor_type"]) == (
                "AAAA", SD, ED, "A")
        reqs = [e for e in lines(res["manifest_path"]) if e["event"] == "request"]
        for ev, spec in zip(reqs, ts.PLAN):
            assert ev["brokers_param"] == list(spec.tokens) == ev["broker_selectors"]
            assert ev["broker_request_kind"] == ic.SELECTOR and ev["requested_brokers"] is None
            assert ev["collection_mode"] == ic.TARGETED_SELECTORS == tap.COLLECTION_MODE
            assert ev["selector_plan"] == ts.PLAN_ID and ev["request_group"] == spec.group
            assert ev["collector"] == "targeted_actor_panel" and ev["writes_cache"] is False
            assert ev["pipeline_run_id"] == res["run_id"]
    print("  ok test_a_valid_eight_token_request")


# ── 2. expansion, union, membership ────────────────────────────────────────

def test_selector_expansion_past_ten_brokers_is_accepted_as_the_exact_union():
    market = handbuilt_market()
    vendor = FakeVendor({"AAAA": market})
    for spec, union in zip(ts.PLAN, (UNION_A, UNION_B)):      # the fixture is what it claims
        assert {b for t in spec.tokens for b, _ in vendor_picks(market, t)} == union
    with tmpdir() as tmp:
        res = run(vendor, ["AAAA"], tmp)
        db = res["db_path"]
        conn = tdb.connect(db)
        snap = tdb.snapshot(conn, "AAAA")
        by_group = {c["request_group"]: c for c in snap["captures"]}
        assert set(by_group["A"]["expanded_brokers"]) == UNION_A and len(UNION_A) == 11
        assert set(by_group["B"]["expanded_brokers"]) == UNION_B and len(UNION_B) == 14
        assert by_group["A"]["vendor_meta_brokers"] == sorted(ts.PLAN[0].tokens)   # tokens, not brokers
        observed = {r["broker"]: r for r in tdb.observed_brokers(conn, "AAAA")}
        assert set(observed) == UNION_A | UNION_B and len(observed) == 15
        assert snap["observed_broker_count"] == 15
        for b, r in observed.items():
            assert (r["in_request_a"], r["in_request_b"]) == (int(b in UNION_A), int(b in UNION_B)), b
        assert "LG" not in observed and "ZP" not in observed        # never selected
        caps = captures(res)
        assert [set(c["returned_brokers"]) for c in caps] == [UNION_A, UNION_B]
        conn.close()
    print("  ok test_selector_expansion_past_ten_brokers_is_accepted_as_the_exact_union")


def test_membership_is_reconstructed_per_horizon_sign_and_unit():
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": handbuilt_market()}), ["AAAA"], tmp)
        conn = tdb.connect(res["db_path"])
        rows = tdb.membership(conn, "AAAA")
        dates = weekdays("2025-10-01", N_SESSIONS)
        got = {}
        for r in rows:
            got.setdefault((r["horizon"], r["metric"]), []).append(r)
            assert r["provenance"] == "DERIVED_FROM_SELECTOR_UNION" and r["tied"] == 0
            assert r["window_last_session"] == dates[-1] == r["discovery_as_of"]
            n = {"C5": 5, "C20": 20, "C50": 50, "ALL": N_SESSIONS}[r["horizon"]]
            assert r["window_sessions"] == n and r["window_first_session"] == dates[-n]
            assert r["selector_token"] == f"TOP_5_{r['metric']}_{r['horizon']}"
            assert r["request_group"] == ("A" if r["horizon"] in ("C5", "C20") else "B")
        assert set(got) == set(EXPECTED)
        for key, expected in EXPECTED.items():
            assert [(r["rank"], r["broker"], r["window_value"]) for r in got[key]] == [
                (i, b, v) for i, (b, v) in enumerate(expected, 1)], (key, got[key])
        # the same broker, several reasons: AK is a top-5 member of 7 selections
        assert sum(1 for r in rows if r["broker"] == "AK") == sum(
            1 for picks in EXPECTED.values() for b, _ in picks if b == "AK") == 7
        # LOT and VAL disagree where prices differ (AK bought cheap, XL sold cheap)
        assert [b for b, _ in EXPECTED[("C5", "NB_LOT")]][:2] == ["AK", "YP"]
        assert [r["broker"] for r in got[("C5", "NB_VAL")]][:2] == ["YP", "AK"]
        # NB is positive, largest first; NS negative, most negative first
        for (h, metric), rs in got.items():
            values = [r["window_value"] for r in rs]
            if metric.startswith("NB"):
                assert all(v > 0 for v in values) and values == sorted(values, reverse=True)
            else:
                assert all(v < 0 for v in values) and values == sorted(values)
        # every capture reference resolves to the manifest's captures
        ids = {c["capture_id"]: c for c in captures(res)}
        for r in rows:
            assert ids[r["selector_capture_id"]]["request_group"] == r["request_group"]
            assert set(r["union_capture_ids"]) == set(ids)
        conn.close()
    print("  ok test_membership_is_reconstructed_per_horizon_sign_and_unit")


def test_reconstruction_matches_the_vendor_on_random_universes():
    """Over the union alone, every selector's derived top 5 is the vendor's top 5
    over the whole universe, in order, with the same window sums."""
    for seed in range(1, 7):
        market = random_market(seed)
        vendor = FakeVendor({"RAND": market})
        with tmpdir() as tmp:
            res = run(vendor, ["RAND"], tmp)
            assert res["ok"] == ["RAND"], (seed, res["failed"])
            conn = tdb.connect(res["db_path"])
            got = {}
            for r in tdb.membership(conn, "RAND"):
                got.setdefault(f"TOP_5_{r['metric']}_{r['horizon']}", []).append(
                    (r["broker"], r["window_value"], r["tied"]))
            for token in ts.V1_ALLOWLIST:
                truth = vendor_picks(market, token)
                assert [(b, float(v)) for b, v, _ in got.get(token, [])] == [
                    (b, float(v)) for b, v in truth], (seed, token)
                assert not any(t for _, _, t in got.get(token, [])), (seed, token)
            union = {b for t in ts.V1_ALLOWLIST for b, _ in vendor_picks(market, t)}
            assert {r["broker"] for r in tdb.observed_brokers(conn, "RAND")} == union
            assert len(union) < len(market["brokers"])           # a union, not the universe
            conn.close()
    print("  ok test_reconstruction_matches_the_vendor_on_random_universes")


def test_a_broker_chosen_by_many_selectors_is_stored_once():
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": handbuilt_market()}), ["AAAA"], tmp)
        db = res["db_path"]
        assert snapshot_rows(db, "SELECT COUNT(*) FROM observed_series WHERE broker = 'AK'") == [
            (N_SESSIONS,)]
        assert snapshot_rows(db, "SELECT COUNT(*) FROM observed_series") == [(15 * N_SESSIONS,)]
        assert snapshot_rows(db, "SELECT COUNT(*) FROM observed_brokers WHERE broker = 'AK'") == [(1,)]
        assert snapshot_rows(db, "SELECT COUNT(*) FROM panel_sessions") == [(N_SESSIONS,)]
        conn = tdb.connect(db)
        series = tdb.broker_series(conn, "AAAA", "AK")
        market = handbuilt_market()
        assert [s["nlot"] for s in series] == market["brokers"]["AK"]["nlot"]
        assert [s["bval"] for s in series] == [float(x) for x in market["brokers"]["AK"]["bval"]]
        conn.close()
    print("  ok test_a_broker_chosen_by_many_selectors_is_stored_once")


# ── 3. coverage ────────────────────────────────────────────────────────────

def test_observed_zero_is_observed_and_unobserved_is_never_zero():
    def pad_with_zp(ticker, kept, env):
        # The vendor padding a selection with a broker that did nothing (the
        # behaviour when fewer than n qualify is not verified): ZP comes back
        # with explicit all-zero series.
        if "TOP_5_NB_LOT_C5" in kept:
            for f in FIELDS:
                env["data"][f]["ZP"] = [0] * N_SESSIONS
                env["data"][f] = dict(sorted(env["data"][f].items()))
        return env
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": handbuilt_market()}, mutate=pad_with_zp), ["AAAA"], tmp)
        assert res["ok"] == ["AAAA"], res
        conn = tdb.connect(res["db_path"])
        as_of = res["discovery_as_of"]["AAAA"]
        dates = weekdays("2025-10-01", N_SESSIONS)
        zp = {r["broker"]: r for r in tdb.observed_brokers(conn, "AAAA")}["ZP"]
        assert zp["nonzero_sessions"] == 0 and zp["in_request_a"] == 1
        for d in (dates[0], dates[-1]):
            state = tdb.coverage(conn, "AAAA", as_of, "ZP", d)
            assert state["state"] == tdb.OBSERVED_ZERO and set(state["values"].values()) == {0}
        assert all(s["coverage"] == tdb.OBSERVED_ZERO for s in tdb.broker_series(conn, "AAAA", "ZP"))
        assert "ZP" not in {m["broker"] for m in tdb.membership(conn, "AAAA")}
        snap = tdb.snapshot(conn, "AAAA")
        assert {c["request_group"]: c["unexplained_brokers"] for c in snap["captures"]} == {
            "A": ["ZP"], "B": []}
        # CC is observed: all zero in S1, S3, S4, trading in S2
        assert tdb.coverage(conn, "AAAA", as_of, "CC", dates[0])["state"] == tdb.OBSERVED_ZERO
        assert tdb.coverage(conn, "AAAA", as_of, "CC", dates[80]) == {
            "state": tdb.OBSERVED_NONZERO,
            "values": {"blot": 30, "bval": 30e6, "slot": 0, "sval": 0.0, "nlot": 30, "nval": 30e6}}
        # LG traded every day but was never selected: UNOBSERVED, no values, no rows
        for broker in ("LG", "AD"):
            assert tdb.coverage(conn, "AAAA", as_of, broker, dates[5]) == {
                "state": tdb.UNOBSERVED, "values": None}
            assert tdb.broker_series(conn, "AAAA", broker) == []
        assert snapshot_rows(res["db_path"], "SELECT COUNT(*) FROM observed_series "
                                             "WHERE broker IN ('LG', 'AD')") == [(0,)]
        try:
            tdb.coverage(conn, "AAAA", as_of, "AK", "2024-01-02")
        except KeyError:
            pass
        else:
            raise AssertionError("a date off the axis got a coverage state")
        conn.close()
    print("  ok test_observed_zero_is_observed_and_unobserved_is_never_zero")


# ── 4. source guardrails ───────────────────────────────────────────────────

def _refused(mutate, contains, tickers=("AAAA",), status=ic.REJECTED, responses=None):
    """Run AAAA with `mutate` applied; assert the ticker failed, nothing was
    stored, and the refused capture says `contains`."""
    vendor = FakeVendor({"AAAA": handbuilt_market()}, mutate=mutate, responses=responses)
    with tmpdir() as tmp:
        res = run(vendor, list(tickers), tmp)
        assert res["ok"] == [] and "AAAA" in res["failed"], res
        assert contains in res["failed"]["AAAA"], res["failed"]
        assert_nothing_stored(res["db_path"])
        caps = captures(res)
        assert any(c["status"] == status and contains in (c["reason"] or "") for c in caps), [
            (c["status"], c["reason"]) for c in caps]
        return res, caps, vendor


def test_meta_selector_echo_must_match_the_tokens_sent():
    def drop_one(ticker, kept, env):
        env["meta"]["brokers"] = sorted(kept)[:-1]
        return env
    res, caps, vendor = _refused(drop_one, "does not echo")
    assert len(vendor.queries) == 1 and [c["status"] for c in caps] == [ic.REJECTED]   # not retried; B never sent

    def swap(ticker, kept, env):
        env["meta"]["brokers"] = sorted(kept[:-1] + ["TOP_5_NB_LOT_C60"])
        return env
    _refused(swap, "does not echo")

    def no_meta(ticker, kept, env):
        del env["meta"]
        return env
    _refused(no_meta, "no meta")

    def other_symbol(ticker, kept, env):
        env["meta"]["symbol"] = "BBBB"
        return env
    _refused(other_symbol, "meta.symbol")

    def other_window(ticker, kept, env):
        env["meta"]["end_date"] = "2026-09-30"
        return env
    _refused(other_window, "meta.end_date")
    print("  ok test_meta_selector_echo_must_match_the_tokens_sent")


def test_invalid_expanded_keys_are_refused():
    for fake in ("ALL", "TOP_5_NB_LOT_C5", "QQ", "ak", "A1"):
        def add_key(ticker, kept, env, fake=fake):
            for f in FIELDS:
                env["data"][f][fake] = list(env["data"][f]["AK"])
            return env
        _refused(add_key, "not known broker codes")
    print("  ok test_invalid_expanded_keys_are_refused")


def test_broker_keys_must_agree_across_the_six_maps():
    for field in FIELDS:
        def drop(ticker, kept, env, field=field):
            del env["data"][field]["AK"]
            return env
        _refused(drop, "differ across the six maps")

    def missing_map(ticker, kept, env):
        del env["data"]["sval"]
        return env
    _refused(missing_map, "not a broker map")

    def null_series(ticker, kept, env):
        for f in FIELDS:
            env["data"][f]["AK"] = None
        return env
    _refused(null_series, "null series")

    def short_series(ticker, kept, env):
        for f in FIELDS:
            env["data"][f]["AK"] = env["data"][f]["AK"][:-1]
        return env
    _refused(short_series, "strict frame")

    def broken_identity(ticker, kept, env):
        env["data"]["nlot"]["AK"][3] += 1
        return env
    _refused(broken_identity, "nlot == blot - slot")
    print("  ok test_broker_keys_must_agree_across_the_six_maps")


def test_session_axis_and_ohlc_are_checked_per_response():
    def unsorted(ticker, kept, env):
        env["data"]["date"][3], env["data"]["date"][4] = env["data"]["date"][4], env["data"]["date"][3]
        return env
    _refused(unsorted, "date axis")

    def ohlc_off_axis(ticker, kept, env):
        env["data"]["ohlc"] = env["data"]["ohlc"][:-1]
        return env
    _refused(ohlc_off_axis, "one for one")

    def outside_window(ticker, kept, env):
        env["data"]["date"][-1] = "2026-09-30"          # after the requested end_date
        env["data"]["ohlc"][-1]["date"] = "2026-09-30"
        return env
    _refused(outside_window, "outside the requested")

    def not_success(ticker, kept, env):
        env["success"] = False
        return env
    res, caps, vendor = _refused(not_success, "success=False", status=ic.VENDOR_ERROR)
    assert len(caps) == tap.MAX_RETRY                           # retried, as broker_collect
    print("  ok test_session_axis_and_ohlc_are_checked_per_response")


def test_explicit_code_responses_must_return_exactly_the_codes_requested():
    market = handbuilt_market()
    vendor = FakeVendor({"AAAA": market})
    codes = ["AK", "BK", "LG", "ZP"]
    qs = ts.build_explicit_query("AAAA", codes, CODES, SD, ED)
    env = json.loads(vendor.get(qs).text())
    obs = tap.validate_response(env, "AAAA", codes, ic.EXPLICIT_CODES, SD, ED, CODES)
    assert obs.brokers == ("AK", "BK", "LG", "ZP")
    assert obs.series["ZP"]["nlot"] == [0] * N_SESSIONS             # returned zero: observed
    truncated = copy.deepcopy(env)
    for f in FIELDS:
        del truncated["data"][f]["ZP"]
    try:
        tap.validate_response(truncated, "AAAA", codes, ic.EXPLICIT_CODES, SD, ED, CODES)
    except bc.FetchRejected as e:
        assert "!= requested" in str(e)
    else:
        raise AssertionError("a response missing a requested code was accepted")
    # The server's cap, reproduced: a request of 11 codes (built by hand; the
    # guard would refuse it) comes back with the first 10 and a 10-value echo.
    # Checked against what was SENT, that is a refusal, never a quiet 10 of 11.
    eleven = sorted(market["brokers"])[:11]
    raw = "&".join(["symbol=AAAA"] + [f"brokers={c}" for c in eleven] +
                   [f"start_date={SD}", f"end_date={ED}", "investor_type=A"])
    env11 = json.loads(vendor.get(raw).text())
    assert len(env11["meta"]["brokers"]) == 10 and len(env11["data"]["nlot"]) == 10
    try:
        tap.validate_response(env11, "AAAA", eleven, ic.EXPLICIT_CODES, SD, ED, CODES)
    except bc.FetchRejected as e:
        assert "does not echo" in str(e)
    else:
        raise AssertionError("a truncated explicit response was accepted")
    print("  ok test_explicit_code_responses_must_return_exactly_the_codes_requested")


# ── 5. the pair ────────────────────────────────────────────────────────────

def test_inconsistent_a_and_b_refuse_the_whole_ticker():
    def b_close(ticker, kept, env):
        if "TOP_5_NB_LOT_ALL" in kept:
            env["data"]["ohlc"][7]["close"] += 1
        return env
    res, caps, _ = _refused(b_close, "OHLC differs")
    assert [c["status"] for c in caps] == [ic.REJECTED, ic.REJECTED]

    def b_axis(ticker, kept, env):
        if "TOP_5_NB_LOT_ALL" in kept:        # B one session shorter at the front
            env["data"]["date"] = env["data"]["date"][1:]
            env["data"]["ohlc"] = env["data"]["ohlc"][1:]
            for f in FIELDS:
                env["data"][f] = {b: s[1:] for b, s in env["data"][f].items()}
        return env
    res, caps, _ = _refused(b_axis, "session axes differ")
    assert [c["status"] for c in caps] == [ic.REJECTED, ic.REJECTED]

    def b_series(ticker, kept, env):
        if "TOP_5_NB_LOT_ALL" in kept:
            env["data"]["blot"]["AK"][0] += 1
            env["data"]["slot"]["AK"][0] += 1
        return env
    _refused(b_series, "AK's series differ")

    def b_later_session(ticker, kept, env):
        if "TOP_5_NB_LOT_ALL" in kept:        # the market moved on between A and B
            env["data"]["date"][-1] = "2026-03-18"
            env["data"]["ohlc"][-1]["date"] = "2026-03-18"
        return env
    _refused(b_later_session, "session axes differ")

    # B fails at the source: A was accepted but never used -> ABORTED
    responses = {("AAAA", 1 + k): Resp(500, text="Server Error") for k in range(tap.MAX_RETRY)}
    res, caps, _ = _refused(None, "HTTP 500", status=ic.HTTP_ERROR, responses=responses)
    assert caps[0]["status"] == ic.ABORTED and "request B failed" in caps[0]["reason"]
    assert [c["status"] for c in caps[1:]] == [ic.HTTP_ERROR] * tap.MAX_RETRY
    print("  ok test_inconsistent_a_and_b_refuse_the_whole_ticker")


def test_a_contradiction_of_the_selector_semantics_refuses_the_ticker():
    # The vendor's C5 is computed over a different window than verified: its
    # picks then disagree with the derivation from the union.
    def wrong_window(ticker, kept, env):
        if "TOP_5_NB_LOT_C5" in kept:
            for f in FIELDS:
                env["data"][f].pop("AK")                     # AK is C5's rank 1
        return env
    res, caps, _ = _refused(wrong_window, "semantics do not hold")
    assert [c["status"] for c in caps] == [ic.REJECTED, ic.REJECTED]
    print("  ok test_a_contradiction_of_the_selector_semantics_refuses_the_ticker")


def test_a_cross_ticker_clone_is_refused():
    market = handbuilt_market()
    vendor = FakeVendor({"AAAA": market, "BBBB": copy.deepcopy(market)})
    with tmpdir() as tmp:
        res = run(vendor, ["AAAA", "BBBB"], tmp)
        assert res["ok"] == ["AAAA"] and "cross-ticker clone" in res["failed"]["BBBB"]
    print("  ok test_a_cross_ticker_clone_is_refused")


# ── 6. persistence and the manifest ────────────────────────────────────────

def test_manifest_lifecycle_and_discovery_as_of():
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": handbuilt_market()}), ["AAAA"], tmp)
        events = lines(res["manifest_path"])
        assert [e["event"] for e in events] == ["request", "request", "persisting", "persisting",
                                                "result", "result"]
        assert all(e["schema"] == "inventory_capture_v1" for e in events)
        as_of = weekdays("2025-10-01", N_SESSIONS)[-1]
        for e in events[2:4]:
            assert e["target"] == "panel.db" and e["intended_cache_ref"] is None
        for c in captures(res):
            assert c["status"] == ic.OK and c["discovery_as_of"] == as_of
            assert c["response_sha256"] and c["cache_ref"] is None and c["cache_sha256"] is None
            assert c["session_count"] == N_SESSIONS and c["last_session"] == as_of
        conn = tdb.connect(res["db_path"])
        snap = tdb.snapshot(conn, "AAAA")
        run_row = dict(zip(*[[d[0] for d in conn.execute("SELECT * FROM panel_runs").description],
                             conn.execute("SELECT * FROM panel_runs").fetchone()]))
        conn.close()
        manifest_caps = {c["capture_id"]: c for c in captures(res)}
        for c in snap["captures"]:
            m = manifest_caps[c["capture_id"]]
            assert c["response_sha256"] == m["response_sha256"] and c["query_sha256"] == m["query_sha256"]
            assert c["manifest_run_id"] == m["run_id"] and c["selector_tokens"] == m["brokers_param"]
        assert snap["coverage_scope"] == "TARGETED_SELECTOR_UNION"
        assert snap["collection_mode"] == "TARGETED_SELECTORS" and snap["selector_plan"] == ts.PLAN_ID
        assert run_row["status"] == "ok" and run_row["manifest_path"] == res["manifest_path"]
        assert run_row["tickers_ok"] == 1 and run_row["collection_mode"] == "TARGETED_SELECTORS"
        assert set(res["seconds"]) == {"AAAA"} and len(res["latencies"]) == 2
        # the collector writes no vendor-shaped file anywhere, only the DB and the manifest
        written = sorted(os.path.relpath(p, tmp).replace(os.sep, "/")
                         for p in glob.glob(os.path.join(tmp, "**", "*"), recursive=True)
                         if os.path.isfile(p))
        assert written == ["_capture_manifest/" + os.path.basename(res["manifest_path"]),
                           "panel.db"], written
    print("  ok test_manifest_lifecycle_and_discovery_as_of")


def test_a_rerun_is_identical_and_a_restatement_is_refused():
    with tmpdir() as tmp:
        market = handbuilt_market()
        first = run(FakeVendor({"AAAA": market}), ["AAAA"], tmp)
        again = run(FakeVendor({"AAAA": market}), ["AAAA"], tmp)
        assert first["ok"] == ["AAAA"] and again["identical"] == ["AAAA"] and again["ok"] == []
        assert [c["status"] for c in captures(again)] == [ic.OK, ic.OK]
        assert all("identical" in c["reason"] for c in captures(again))
        restated = copy.deepcopy(market)
        restated["brokers"]["AK"]["blot"][0] += 1
        restated["brokers"]["AK"]["slot"][0] += 1
        third = run(FakeVendor({"AAAA": restated}), ["AAAA"], tmp)
        assert "insert-only" in third["failed"]["AAAA"]
        assert [c["status"] for c in captures(third)] == [ic.REJECTED, ic.REJECTED]
        db = first["db_path"]
        assert snapshot_rows(db, "SELECT COUNT(*) FROM panel_snapshots") == [(1,)]
        assert snapshot_rows(db, "SELECT blot FROM observed_series WHERE broker = 'AK' "
                                 "ORDER BY session_date LIMIT 1") == [(0,)]
        assert snapshot_rows(db, "SELECT status FROM panel_runs ORDER BY started_utc") == [
            ("ok",), ("ok",), ("failed",)]                 # the refused rerun stored nothing
    print("  ok test_a_rerun_is_identical_and_a_restatement_is_refused")


def test_empty_and_pilot_guards():
    def empty(ticker, kept, env):
        env["data"] = {"date": [], "ohlc": [], **{f: {} for f in FIELDS}}
        return env
    with tmpdir() as tmp:
        vendor = FakeVendor({"AAAA": handbuilt_market()}, mutate=empty)
        res = run(vendor, ["AAAA"], tmp)
        assert res["empty"] and "AAAA" in res["empty"] and res["failed"] == {}
        assert len(vendor.queries) == 1 and [c["status"] for c in captures(res)] == [ic.EMPTY]
        try:
            run(vendor, ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF"], tmp)
        except ValueError as e:
            assert "pilot-only" in str(e)
        else:
            raise AssertionError("six tickers ran")
        for bad in ("IHSG", "BBCA1", "bb"):
            try:
                run(vendor, [bad], tmp)
            except ValueError:
                continue
            raise AssertionError(bad)
        assert len(vendor.queries) == 1
    print("  ok test_empty_and_pilot_guards")


def test_inventory_capture_v1_lines_are_unchanged_without_a_mode():
    base = {"schema", "event", "capture_id", "run_id", "seq", "attempt", "requested_at", "source",
            "method", "endpoint", "collector", "mode", "pipeline_run_id", "broker_list_source",
            "writes_cache", "ticker", "start_date", "end_date", "investor_type", "brokers_param",
            "broker_request_kind", "requested_brokers", "broker_selectors", "other_params",
            "query_sha256"}
    with tmpdir() as tmp:
        log = ic.CaptureLog(tmp, "legacy", writes_cache=False)
        cap = log.begin("symbol=AAAA&brokers=TOP_5_NB_LOT_C20&brokers=TOP_5_NS_LOT_C20")
        cap.finish(ic.ERROR, "x")
        req, result = lines(log.path)
        assert set(req) == base, set(req) ^ base
        assert "discovery_as_of" not in result
        # declared modes refuse the wrong kind of query before writing a line
        full = ic.CaptureLog(tmp, "full", writes_cache=True, collection_mode=ic.EXPLICIT_CODES)
        targeted = ic.CaptureLog(tmp, "t", writes_cache=False, collection_mode=ic.TARGETED_SELECTORS,
                                 selector_plan=ts.PLAN_ID)
        for log_, query in ((full, "symbol=AAAA&brokers=TOP_5_NB_LOT_C20"),
                            (targeted, "symbol=AAAA&brokers=AK"),
                            (targeted, "symbol=AAAA&brokers=AK&brokers=TOP_5_NB_LOT_C20")):
            try:
                log_.begin(query)
            except ValueError:
                assert log_.path is None
                continue
            raise AssertionError(query)
        assert set(lines(full.begin("symbol=AAAA&brokers=AK").log.path)[0]) == base | {"collection_mode"}
        for bad in ({"collection_mode": "MIXED"}, {"selector_plan": "x"},
                    {"collection_mode": ic.TARGETED_SELECTORS}):
            try:
                ic.CaptureLog(tmp, "x", writes_cache=False, **bad)
            except ValueError:
                continue
            raise AssertionError(bad)
    print("  ok test_inventory_capture_v1_lines_are_unchanged_without_a_mode")


def test_broker_collect_declares_explicit_codes_and_claims_no_full_universe():
    """broker_collect's manifest says EXPLICIT_CODES: what was asked, at most
    10 codes. Nothing in it claims full coverage, and its 10-code cache is
    refused by broker_book for exactly that reason."""
    ten = tuple(CODES[:10])
    dates = weekdays("2025-10-01", 120)
    body = {"success": True, "meta": {"symbol": "AAAA"}, "data": {
        "date": dates, "ohlc": ohlc_rows(dates, 900.0),
        **{f: {b: [0] * 120 for b in ten} for f in FIELDS}}}
    with tmpdir() as tmp:
        res = bc.collect(["AAAA"], "daily", raw_dir=tmp, sleep=[].append, now=NOW,
                         request_get=lambda qs: Resp(200, body), codes=ten)
        assert res["ok"] == ["AAAA"], res
        (path,) = glob.glob(os.path.join(tmp, ic.MANIFEST_DIR, "*.jsonl"))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        req = lines(path)[0]
        (cached,) = bc.iter_cached(None, "daily", raw_dir=tmp)
    assert req["collection_mode"] == ic.EXPLICIT_CODES and req["brokers_param"] == list(ten)
    assert "selector_plan" not in req and "request_group" not in req
    assert not hasattr(ic, "FULL_EXPLICIT") and "FULL" not in text
    try:
        bb.frames_from_payload(cached[1], "AAAA")
    except bb.CoveragePayloadError as e:
        assert "10 of the 101" in str(e), e
    else:
        raise AssertionError("broker_book read a 10-code cache as full coverage")
    print("  ok test_broker_collect_declares_explicit_codes_and_claims_no_full_universe")


# ── 7. full-universe guard ─────────────────────────────────────────────────

def test_targeted_data_cannot_enter_a_full_universe_metric_path():
    market = handbuilt_market()
    vendor = FakeVendor({"AAAA": market})
    env = json.loads(vendor.get(ts.build_query("AAAA", ts.PLAN[1].tokens, SD, ED)).text())

    # (a) the in-memory snapshot and a marked payload never reach broker_book
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": market}), ["AAAA"], tmp)
        assert res["ok"] == ["AAAA"]
    marked = dict(env["data"], coverage_scope=cg.TARGETED_SELECTOR_UNION)
    for payload in (marked, {"coverage_scope": cg.TARGETED_SELECTOR_UNION, "brokers": {}},
                    dict(env["data"], coverage_scope="ANYTHING_BUT_FULL")):
        try:
            bb.frames_from_payload(payload, "AAAA")
        except bb.TargetedPayloadError as e:
            assert isinstance(e, bb.PayloadError) and isinstance(e, cg.TargetedCoverageError)
            continue
        raise AssertionError("broker_book accepted targeted data")
    # A bare selector union, meta and marks stripped, is refused as well: the
    # absence of a mark is not evidence of full coverage.
    bare = copy.deepcopy(env["data"])
    assert cg.targeted_reason(bare) is None and set(bare["nlot"]) == UNION_B
    try:
        bb.frames_from_payload(bare, "AAAA")
    except bb.CoveragePayloadError as e:
        assert isinstance(e, bb.PayloadError) and "14 of the 101" in str(e), e
    else:
        raise AssertionError("broker_book accepted a bare selector union")
    # ...and so is the truncated 10-of-101 shape of the 2026-09 caches
    ten = {**bare, **{f: dict(sorted(bare[f].items())[:10]) for f in FIELDS}}
    try:
        bb.frames_from_payload(ten, "AAAA")
    except bb.CoveragePayloadError:
        pass
    else:
        raise AssertionError("broker_book accepted 10 of the 101 codes")
    # Only a payload that returned every universe broker (inactive ones at an
    # explicit zero, as the complete 2026-08-22 harvest has them) is accepted.
    full = copy.deepcopy(bare)
    for f in FIELDS:
        for code in cg.universe_codes():
            full[f].setdefault(code, [0] * len(full["date"]))
    brokers, _ = bb.frames_from_payload(full, "AAAA")
    assert set(brokers["broker"]) <= UNION_B

    # (b) a selector envelope in the broker learning cache is refused, not read
    with tmpdir() as tmp:
        os.makedirs(os.path.join(tmp, "daily"))
        for t, doc in (("AAAA", {"fetched_utc": "x", "meta": env["meta"], "data": env["data"]}),
                       ("BBBB", marked),
                       ("CCCC", {"fetched_utc": "x", "meta": {"symbol": "CCCC", "brokers": CODES[:10]},
                                 "data": env["data"]})):
            import gzip
            with gzip.open(os.path.join(tmp, "daily", f"{t}.json.gz"), "wt", encoding="utf-8") as f:
                json.dump(doc, f)
        bad = {}
        got = dict(bc.iter_cached(None, "daily", raw_dir=tmp, unreadable=bad))
        assert list(got) == ["CCCC"], list(got)                  # the unmarked one, as before
        assert set(bad) == {"AAAA", "BBBB"} and all("TargetedCoverageError" in r for r in bad.values())
        try:
            bb.frames_from_payload(got["CCCC"], "CCCC")
        except bb.CoveragePayloadError as e:
            assert "14 of the 101 universe brokers" in str(e), e
        else:
            raise AssertionError("broker_book accepted the unmarked cached selector union")

    # (c) the databases refuse each other
    with tmpdir() as tmp:
        panel = os.path.join(tmp, "panel.db")
        tdb.connect(panel).close()
        try:
            bldb.connect(panel)
        except ValueError as e:
            assert "targeted actor panel" in str(e)
        else:
            raise AssertionError("broker_learning_db opened a targeted panel")
        ledger = os.path.join(tmp, "ledger.db")
        bldb.connect(ledger).close()
        for path in (ledger, os.path.join(tmp, "broker_learning.db"), os.path.join(tmp, "neobdm.db")):
            try:
                tdb.connect(path)
            except tdb.NotTargetedPanelError:
                continue
            raise AssertionError(f"targeted_actor_db opened {path}")
        assert not os.path.exists(os.path.join(tmp, "broker_learning.db"))
        conn = tdb.connect(panel)
        meta = tdb.panel_meta(conn)
        conn.close()
        assert meta["full_universe"] == "false" and meta["coverage_scope"] == "TARGETED_SELECTOR_UNION"
        try:
            with sqlite3.connect(panel) as conn:
                conn.execute("INSERT INTO panel_runs (run_id, collection_mode, selector_plan, "
                             "started_utc, status) VALUES ('x', 'FULL_EXPLICIT', 'p', 't', 's')")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("a non-targeted run row was accepted")

    # (d) the collector refuses to write into a full-universe store
    for d in tap.FULL_UNIVERSE_DIRS:
        try:
            tap.collect(["AAAA"], os.path.join(HERE, d, "panel.db"), request_get=vendor.get,
                        sleep=[].append, now=NOW)
        except ValueError as e:
            assert "full-universe store" in str(e)
            continue
        raise AssertionError(d)
    assert not os.path.exists(os.path.join(HERE, "broker_learning_raw", "panel.db"))

    # (e) the registry names every family of full-universe metric
    text = " ".join(" ".join(row) for row in cg.FULL_UNIVERSE_METRICS).lower()
    for needle in ("adv20", "val20", "n_active_brk", "hhi", "concentration", "groups",
                   "top/bottom 3 by nl5", "profitability", "lift", "broker_scores", "rule_stats"):
        assert needle in text, needle
    print("  ok test_targeted_data_cannot_enter_a_full_universe_metric_path")


def test_unmigrated_ticker_bundle_refuses_before_reading_any_coverage_shape():
    vendor = FakeVendor({"AAAA": handbuilt_market()})
    env = json.loads(vendor.get(ts.build_query("AAAA", ts.PLAN[1].tokens, SD, ED)).text())
    bare = env["data"]
    marked = dict(bare, coverage_scope=cg.TARGETED_SELECTOR_UNION)
    full = copy.deepcopy(bare)
    for f in FIELDS:
        for code in cg.universe_codes():
            full[f].setdefault(code, [0] * len(full["date"]))
    payloads, regimes = (marked, bare, full), {"AAAA": []}
    before = copy.deepcopy((payloads, regimes))
    with patch.object(bb, "frames_from_payload",
                      side_effect=AssertionError("ticker_bundle read an unmigrated payload")) as reader:
        for payload in payloads:
            try:
                bb.ticker_bundle(payload, "AAAA", regimes)
            except UnsupportedPriceContract as e:
                assert str(e).startswith("broker_book.ticker_bundle:"), e
                assert e.consumer == "broker_book.ticker_bundle"
                assert e.status == "UNSUPPORTED" and e.contract_version == CONTRACT_VERSION
                assert e.as_dict() == {"status": "UNSUPPORTED", "consumer": "broker_book.ticker_bundle",
                                       "contract_version": CONTRACT_VERSION, "reason": str(e)}
            else:
                raise AssertionError("ticker_bundle accepted an unsupported price contract")
        reader.assert_not_called()
    assert (payloads, regimes) == before, "contract refusal changed the inputs"
    print("  ok test_unmigrated_ticker_bundle_refuses_before_reading_any_coverage_shape")


def test_unmigrated_learning_loader_refuses_without_cache_or_result_side_effects():
    import broker_learning_run as blr
    tickers, regimes = ["AAAA"], {"AAAA": []}
    failed, empty = {"prior": "failure"}, {"prior": "empty"}
    before = copy.deepcopy((tickers, regimes, failed, empty))
    with tmpdir() as tmp:
        os.makedirs(os.path.join(tmp, "daily"))
        cache = os.path.join(tmp, "daily", "AAAA.json.gz")
        contents = b"existing cache bytes must not be read or changed"
        with open(cache, "wb") as fh:
            fh.write(contents)
        paths_before = sorted(glob.glob(os.path.join(tmp, "**", "*"), recursive=True))
        with patch.object(bc, "iter_cached",
                          side_effect=AssertionError("load_items read an unmigrated cache")) as reader:
            try:
                blr.load_items(tickers, "daily", tmp, False, regimes, failed, empty)
            except UnsupportedPriceContract as e:
                assert str(e).startswith("broker_learning_run.load_items:"), e
                assert e.consumer == "broker_learning_run.load_items"
                assert e.status == "UNSUPPORTED" and e.contract_version == CONTRACT_VERSION
                assert e.as_dict() == {"status": "UNSUPPORTED", "consumer": "broker_learning_run.load_items",
                                       "contract_version": CONTRACT_VERSION, "reason": str(e)}
            else:
                raise AssertionError("load_items accepted an unsupported price contract")
            reader.assert_not_called()
        assert sorted(glob.glob(os.path.join(tmp, "**", "*"), recursive=True)) == paths_before
        with open(cache, "rb") as fh:
            assert fh.read() == contents, "contract refusal changed the cache"
    assert (tickers, regimes, failed, empty) == before, "contract refusal changed the inputs or results"
    print("  ok test_unmigrated_learning_loader_refuses_without_cache_or_result_side_effects")


# ── 8. hygiene ─────────────────────────────────────────────────────────────

def test_imports_are_light_and_the_database_is_ignored():
    env = {k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}
    code = ("import sys, targeted_selectors, targeted_actor_db, coverage_guard, targeted_actor_panel; "
            "heavy = [m for m in ('neobdm_scraper', 'playwright', 'pandas', 'numpy', 'pyarrow', "
            "'requests', 'price_audit', 'build_inventory_db') if m in sys.modules]; "
            "assert not heavy, heavy; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-2000:]
    with open(os.path.join(HERE, ".gitignore"), encoding="utf-8") as f:
        ignored = {line.strip() for line in f}
    assert tdb.DB_NAME in ignored and "_capture_manifest/" in ignored
    for name in (tdb.DB_NAME, tdb.DB_NAME + "-journal", tdb.DB_NAME + "-wal"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=HERE, capture_output=True)
        assert r.returncode == 0, f"{name} is paid per-broker data and must stay out of git"
    print("  ok test_imports_are_light_and_the_database_is_ignored")


def test_cli_plan_is_a_dry_run():
    lines_ = tap._plan_lines(["BBCA", "BREN"], now=NOW)
    text = "\n".join(lines_)
    assert "4 requests" in text and "no network" in text
    queries = [x for x in lines_ if "/api/inventory?" in x]
    assert len(queries) == 4
    for q in queries:
        assert len(re.findall(r"brokers=", q)) == 8
    print("  ok test_cli_plan_is_a_dry_run")


# ── 9. ties, short history, output paths, run status, request accounting ───

# Six brokers tie at 100 lots (1e8 rupiah) on every NB selector of request A
# and on C50; ALL separates them through S1. Sums: AD ALL 3,600, AF..AK ALL
# 170..450; XL -250 everywhere; YP C20/C50/ALL -150; KZ C50/ALL -180.
TIE_DAILY = {
    "AD": ((50, 0, 0, 20), 1_000_000), "AF": ((1, 0, 0, 20), 1_000_000),
    "AG": ((2, 0, 0, 20), 1_000_000), "AH": ((3, 0, 0, 20), 1_000_000),
    "AI": ((4, 0, 0, 20), 1_000_000), "AK": ((5, 0, 0, 20), 1_000_000),
    "XL": ((0, 0, 0, -50), 1_000_000), "YP": ((0, 0, -10, 0), 1_000_000),
    "KZ": ((0, -6, 0, 0), 1_000_000),
}
# Only S4 trades, so every horizon ranks the same: NB AK 500, BK 300, CC 300,
# DR 200, EP 100 and NI 50 clear below the boundary. The BK/CC tie is inside
# the top 5.
INSIDE_DAILY = {
    "AK": ((0, 0, 0, 100), 1_000_000), "BK": ((0, 0, 0, 60), 1_000_000),
    "CC": ((0, 0, 0, 60), 1_000_000), "DR": ((0, 0, 0, 40), 1_000_000),
    "EP": ((0, 0, 0, 20), 1_000_000), "NI": ((0, 0, 0, 10), 1_000_000),
    "XL": ((0, 0, 0, -70), 1_000_000), "YP": ((0, 0, 0, -30), 1_000_000),
    "KZ": ((0, 0, 0, -10), 1_000_000),
}
# C5 NB_LOT: AK 500, BK 400, CC 300, DR 200, then EP and NI both 100 across the
# 5/6 boundary. NI's dearer lots break the tie in VAL (110M vs 100M), so C5
# NB_VAL returns NI; EP's S3 lot breaks it in C20+ (115 vs 100), so C20 returns
# EP. Both tied brokers are therefore in request A whichever of them the
# vendor's own (unverified) tie policy picks for C5 NB_LOT, and only C5 NB_LOT
# is unresolved.
BOUNDARY_DAILY = {
    "AK": ((0, 0, 0, 100), 1_000_000), "BK": ((0, 0, 0, 80), 1_000_000),
    "CC": ((0, 0, 0, 60), 1_000_000), "DR": ((0, 0, 0, 40), 1_000_000),
    "EP": ((0, 0, 1, 20), 1_000_000), "NI": ((0, 0, 0, 20), 1_100_000),
    "XL": ((0, 0, 0, -70), 1_000_000), "YP": ((0, 0, 0, -30), 1_000_000),
    "KZ": ((0, 0, 0, -10), 1_000_000),
}
DERIVED_KEYS = ("horizon", "metric", "rank", "broker", "window_value", "tied",
                "window_first_session", "window_sessions", "selector_token", "request_group")
STATUS_KEYS = ("horizon", "metric", "status", "qualifying_count", "member_count",
               "boundary_tie_value", "boundary_tie_brokers", "sessions_required",
               "sessions_available")


def panel_of(res, ticker="AAAA"):
    """(membership, statuses, snapshot) of a stored ticker, without capture ids."""
    conn = tdb.connect(res["db_path"])
    try:
        rows = [tuple(r[k] for k in DERIVED_KEYS) for r in tdb.membership(conn, ticker)]
        statuses = [tuple(json.dumps(st[k]) for k in STATUS_KEYS)
                    for st in tdb.selection_status(conn, ticker)]
        return rows, statuses, tdb.snapshot(conn, ticker)
    finally:
        conn.close()


def status_map(res, ticker="AAAA"):
    conn = tdb.connect(res["db_path"])
    try:
        return {(st["horizon"], st["metric"]): st for st in tdb.selection_status(conn, ticker)}
    finally:
        conn.close()


def members(res, horizon, metric, ticker="AAAA"):
    conn = tdb.connect(res["db_path"])
    try:
        return [(r["rank"], r["broker"], r["window_value"], r["tied"])
                for r in tdb.membership(conn, ticker, horizon=horizon, metric=metric)]
    finally:
        conn.close()


def test_a_tie_across_the_boundary_attributes_nothing_even_via_the_other_request():
    """The reviewer's case: six brokers tie on C5 NB_LOT. The vendor (tie
    policy "desc") returns five of them in request A but not AD; request B
    returns AD through ALL. Breaking the tie by code would have picked AD and
    credited it to capture A, which never returned it."""
    market = segment_market(TIE_DAILY)
    a_union = {b for t in ts.PLAN[0].tokens for b, _ in vendor_picks(market, t, "desc")}
    assert "AD" not in a_union and {"AF", "AG", "AH", "AI", "AK"} <= a_union   # as claimed
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": market}, tie="desc"), ["AAAA"], tmp)
        assert res["ok"] == ["AAAA"], res                           # accepted, not a contradiction
        conn = tdb.connect(res["db_path"])
        rows = tdb.membership(conn, "AAAA")
        ad = {r["broker"]: r for r in tdb.observed_brokers(conn, "AAAA")}["AD"]
        conn.close()
        st = status_map(res)
        _, _, snap = panel_of(res)
        assert (ad["in_request_a"], ad["in_request_b"]) == (0, 1)
        assert not [r for r in rows if r["broker"] == "AD" and r["request_group"] == "A"]
        for h in ("C5", "C20", "C50"):
            for m in ("NB_LOT", "NB_VAL"):
                s = st[(h, m)]
                assert s["status"] == tdb.UNRESOLVED_BOUNDARY_TIE and s["member_count"] == 0, s
                assert s["boundary_tie_brokers"] == ["AD", "AF", "AG", "AH", "AI", "AK"], s
                assert s["boundary_tie_value"] == (100 if m == "NB_LOT" else 100e6)
                assert s["qualifying_count"] == 6
                assert members(res, h, m) == []
        assert members(res, "ALL", "NB_LOT") == [(1, "AD", 3600, 0), (2, "AK", 450, 0),
                                                 (3, "AI", 380, 0), (4, "AH", 310, 0),
                                                 (5, "AG", 240, 0)]
        assert members(res, "C5", "NS_LOT") == [(1, "XL", -250, 0)]      # fewer than 5 qualify
        assert st[("C5", "NS_LOT")]["qualifying_count"] == 1
        assert members(res, "C50", "NS_LOT") == [(1, "XL", -250, 0), (2, "KZ", -180, 0),
                                                 (3, "YP", -150, 0)]
        assert {c["request_group"]: c["unexplained_brokers"] for c in snap["captures"]} == {
            "A": ["AF", "AG", "AH", "AI", "AK"], "B": ["AF"]}
        assert sum(1 for s in st.values() if s["status"] == tdb.RESOLVED) == 16 - 6
    print("  ok test_a_tie_across_the_boundary_attributes_nothing_even_via_the_other_request")


def test_a_tie_inside_the_top_five_shares_a_rank_and_is_never_broken():
    market = segment_market(INSIDE_DAILY)
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": market}), ["AAAA"], tmp)
        assert res["ok"] == ["AAAA"], res
        st = status_map(res)
        conn = tdb.connect(res["db_path"])
        assert "NI" not in {r["broker"] for r in tdb.observed_brokers(conn, "AAAA")}
        conn.close()
        for h in ("C5", "C20", "C50", "ALL"):
            assert members(res, h, "NB_LOT") == [(1, "AK", 500, 0), (2, "BK", 300, 1),
                                                (2, "CC", 300, 1), (4, "DR", 200, 0),
                                                (5, "EP", 100, 0)], h
            assert members(res, h, "NB_VAL") == [(1, "AK", 500e6, 0), (2, "BK", 300e6, 1),
                                                (2, "CC", 300e6, 1), (4, "DR", 200e6, 0),
                                                (5, "EP", 100e6, 0)], h
            assert (st[(h, "NB_LOT")]["status"], st[(h, "NB_LOT")]["member_count"],
                    st[(h, "NB_LOT")]["qualifying_count"]) == (tdb.RESOLVED, 5, 5)
            assert members(res, h, "NS_LOT") == [(1, "XL", -350, 0), (2, "YP", -150, 0),
                                                (3, "KZ", -50, 0)], h

    # The inside tie still names certain members: if request A had not
    # returned CC, that contradicts the contract and the ticker is refused.
    def drop_cc_from_a(ticker, kept, env):
        if "TOP_5_NB_LOT_C5" in kept:
            for f in FIELDS:
                env["data"][f].pop("CC")
        return env
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": market}, mutate=drop_cc_from_a), ["AAAA"], tmp)
        assert "semantics do not hold" in res["failed"]["AAAA"], res
        assert_nothing_stored(res["db_path"])
    print("  ok test_a_tie_inside_the_top_five_shares_a_rank_and_is_never_broken")


def test_a_tie_at_the_rank_five_six_boundary_resolves_that_selector_only():
    market = segment_market(BOUNDARY_DAILY)
    for tie in ("asc", "desc"):                                   # the fixture is what it claims
        assert {"EP", "NI"} <= {b for t in ts.PLAN[0].tokens
                                for b, _ in vendor_picks(market, t, tie)}
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": market}), ["AAAA"], tmp)
        assert res["ok"] == ["AAAA"], res
        st = status_map(res)
        unresolved = {k: s for k, s in st.items() if s["status"] != tdb.RESOLVED}
        assert list(unresolved) == [("C5", "NB_LOT")], unresolved
        s = unresolved[("C5", "NB_LOT")]
        assert (s["boundary_tie_brokers"], s["boundary_tie_value"], s["qualifying_count"],
                s["member_count"]) == (["EP", "NI"], 100, 6, 0)
        assert s["window_first_session"] is not None and s["sessions_required"] == 5
        assert members(res, "C5", "NB_LOT") == []
        assert [b for _, b, _, _ in members(res, "C5", "NB_VAL")] == ["AK", "BK", "CC", "DR", "NI"]
        assert [b for _, b, _, _ in members(res, "C20", "NB_LOT")] == ["AK", "BK", "CC", "DR", "EP"]
        conn = tdb.connect(res["db_path"])
        n_rows = len(tdb.membership(conn, "AAAA"))
        conn.close()
        # 15 resolved selectors: 7 NB with 5 members, and 8 NS with 3 (XL, YP, KZ)
        assert n_rows == 7 * 5 + 8 * 3, n_rows
    print("  ok test_a_tie_at_the_rank_five_six_boundary_resolves_that_selector_only")


def test_membership_is_deterministic_given_the_returned_union():
    """The panel is a function of the returned union's window values alone. The
    vendor's (unverified) tie policy may change WHICH brokers come back; where
    it leaves the union unchanged, as in these fixtures, no rank, member or
    status changes. Neither the order the brokers arrive in nor a rerun
    changes anything."""
    for daily in (TIE_DAILY, BOUNDARY_DAILY, INSIDE_DAILY):
        market = segment_market(daily)
        panels, unions = [], []
        for tie in ("asc", "desc", "desc"):
            with tmpdir() as tmp:
                res = run(FakeVendor({"AAAA": market}, tie=tie), ["AAAA"], tmp)
                assert res["ok"] == ["AAAA"], (tie, res)
                rows, statuses, snap = panel_of(res)
                panels.append((rows, statuses))
                unions.append((snap["observed_broker_count"], sorted(
                    b for c in snap["captures"] for b in c["expanded_brokers"])))
        assert unions[0][0] == unions[1][0] and panels[0] == panels[1] == panels[2]
    # derive_membership directly, the union in three different orders
    market = segment_market(BOUNDARY_DAILY)
    base = [(b, {"returned_by": ("A", "B"), "series": s}) for b, s in market["brokers"].items()]
    ids = {"A": "cap-a", "B": "cap-b"}
    outs = []
    for order in (base, base[::-1], sorted(base, key=lambda kv: kv[0][::-1])):
        rows, statuses, _ = tap.derive_membership(market["dates"], dict(order), ts.PLAN, ids)
        outs.append((rows, statuses))
    assert outs[0] == outs[1] == outs[2]
    print("  ok test_membership_is_deterministic_whatever_the_vendor_tie_policy_or_order")


def test_insufficient_history_fails_closed_per_horizon():
    """Fewer sessions than a Ck horizon needs: that horizon derives nothing
    (the vendor's behaviour is unverified) and says so. ALL is whatever
    window came back."""
    for n, short in ((49, {"C50"}), (20, {"C50"}), (4, {"C5", "C20", "C50"})):
        market = random_market(40 + n, n=n)
        with tmpdir() as tmp:
            res = run(FakeVendor({"RAND": market}), ["RAND"], tmp)
            assert res["ok"] == ["RAND"], (n, res)
            st = status_map(res, "RAND")
            conn = tdb.connect(res["db_path"])
            rows = tdb.membership(conn, "RAND")
            conn.close()
        for (h, m), s in st.items():
            if h in short:
                assert (s["status"], s["member_count"], s["qualifying_count"]) == (
                    tdb.INSUFFICIENT_HISTORY, 0, None), (n, h, m, s)
                assert (s["sessions_required"], s["sessions_available"]) == (
                    ts.HORIZON_SESSIONS[h], n)
                assert s["window_first_session"] is None and s["window_last_session"] is None
            else:
                assert s["status"] == tdb.RESOLVED, (n, h, m, s)
        assert not [r for r in rows if r["horizon"] in short], n
        for r in rows:
            want = n if r["horizon"] == "ALL" else ts.HORIZON_SESSIONS[r["horizon"]]
            assert r["window_sessions"] == want <= n, (n, r)
        for token in ts.V1_ALLOWLIST:                      # the resolved ones match the vendor
            sel = ts.parse_selector(token)
            if sel.period in short:
                continue
            got = [(r["broker"], r["window_value"]) for r in rows
                   if r["horizon"] == sel.period and r["metric"] == sel.metric]
            assert got == [(b, float(v)) for b, v in vendor_picks(market, token)], (n, token)
    print("  ok test_insufficient_history_fails_closed_per_horizon")


def test_paid_output_never_lands_unignored_in_a_git_work_tree():
    vendor = FakeVendor({"AAAA": handbuilt_market()})
    # an arbitrary repo-local name: refused before a byte is written
    stray = os.path.join(HERE, "tap_review_probe.db")
    assert not os.path.exists(stray)
    try:
        tap.collect(["AAAA"], stray, request_get=vendor.get, sleep=[].append, now=NOW)
    except ValueError as e:
        assert "public repository" in str(e), e
    else:
        raise AssertionError("an unignored repo-local database was accepted")
    assert not any(os.path.exists(stray + s) for s in ("",) + tdb.SIDECARS)
    assert vendor.queries == []
    # the canonical name, ignored with its sidecars, passes (checked, not written)
    assert tap.check_private_output(tdb.DB_PATH, tdb.SIDECARS, repo_local=tdb.DB_PATH) == tdb.DB_PATH
    tap.check_private_output(os.path.join(HERE, ic.MANIFEST_DIR, "inv-probe.jsonl"))
    # any other git work tree: the database AND every sidecar must be ignored
    with tmpdir() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True)
        db = os.path.join(tmp, "panel.db")
        for ignore, allowed in (("", False), ("*.db\n", False),
                                ("*.db\n*.db-journal\n*.db-wal\n*.db-shm\n", True)):
            with open(os.path.join(tmp, ".gitignore"), "w", encoding="utf-8") as fh:
                fh.write(ignore)
            try:
                tap.check_private_output(db, tdb.SIDECARS)
            except ValueError as e:
                assert not allowed and "is not ignored" in str(e), (ignore, e)
                continue
            assert allowed, ignore
        # the manifest root is checked too: _capture_manifest/ is not ignored here
        try:
            tap.collect(["AAAA"], db, request_get=vendor.get, sleep=[].append, now=NOW)
        except ValueError as e:
            assert "inv-probe.jsonl is not ignored" in str(e), e
        else:
            raise AssertionError("an unignored manifest root was accepted")
        assert not os.path.exists(db) and vendor.queries == []
        assert not os.path.exists(os.path.join(tmp, ic.MANIFEST_DIR))
    print("  ok test_paid_output_never_lands_unignored_in_a_git_work_tree")


def test_run_status_counts_empty_tickers():
    def empty_for(target):
        def mutate(ticker, kept, env):
            if ticker == target:
                env["data"] = {"date": [], "ohlc": [], **{f: {} for f in FIELDS}}
            return env
        return mutate

    def bad_echo(ticker, kept, env):
        env["meta"]["brokers"] = []
        return env
    markets = {"AAAA": handbuilt_market(), "BBBB": handbuilt_market(2000.0)}
    cases = [("ok", None, ["AAAA", "BBBB"]), ("empty", empty_for("AAAA"), ["AAAA"]),
             ("partial", empty_for("BBBB"), ["AAAA", "BBBB"]), ("failed", bad_echo, ["AAAA"])]
    with tmpdir() as tmp:
        for i, (want, mutate, tickers) in enumerate(cases):
            res = run(FakeVendor(markets, mutate=mutate), tickers, tmp, db=f"r{i}.db")
            assert res["status"] == want, (want, res)
            assert snapshot_rows(res["db_path"], "SELECT status, tickers_empty FROM panel_runs") == [
                (want, len(res["empty"]))]
    blank = {"ok": [], "identical": [], "failed": {}, "empty": {}}
    assert tap.run_status(dict(blank, empty={"A": "x", "B": "x"}), 2) == "empty"
    assert tap.run_status(dict(blank, empty={"A": "x"}, failed={"B": "x"}), 2) == "failed"
    assert tap.run_status(dict(blank, identical=["A"], empty={"B": "x"}), 2) == "partial"
    assert tap.run_status(dict(blank, ok=["A"], identical=["B"]), 2) == "ok"
    print("  ok test_run_status_counts_empty_tickers")


def test_request_count_is_actual_http_attempts():
    def failing_b():
        return {("AAAA", 1): Resp(500, text="busy"), ("AAAA", 2): Resp(500, text="busy")}
    sleeps = []
    with tmpdir() as tmp:
        res = run(FakeVendor({"AAAA": handbuilt_market()}, responses=failing_b()), ["AAAA"], tmp,
                  sleeps=sleeps)
        caps = captures(res)
        run_note = json.loads(snapshot_rows(res["db_path"], "SELECT note FROM panel_runs")[0][0])
    assert res["ok"] == ["AAAA"] and res["requests"] == 4 == len(caps) == len(res["latencies"])
    assert run_note["requests"] == 4
    assert [(c["request_group"], c["attempt"], c["status"]) for c in caps] == [
        ("A", 1, ic.OK), ("B", 1, ic.HTTP_ERROR), ("B", 2, ic.HTTP_ERROR), ("B", 3, ic.OK)]
    # one pace before B's first attempt; the retries are spaced by backoff only
    assert len(sleeps) == 3 and tap.PACE <= sleeps[0] < tap.PACE + tap.JITTER, sleeps
    assert sleeps[1:] == [1.0, 3.0], sleeps
    # REST_EVERY counts attempts sent, retries included
    old = tap.REST_EVERY
    tap.REST_EVERY = 2
    sleeps = []
    try:
        with tmpdir() as tmp:
            run(FakeVendor({"AAAA": handbuilt_market()}, responses=failing_b()), ["AAAA"], tmp,
                sleeps=sleeps)
    finally:
        tap.REST_EVERY = old
    assert sleeps[1:] == [1.0, tap.REST_FOR, 3.0], sleeps
    print("  ok test_request_count_is_actual_http_attempts")


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def _repository_manifest():
    return sorted(glob.glob(os.path.join(HERE, ic.MANIFEST_DIR, "*")))


def main():
    before = _repository_manifest()
    db_before = os.path.exists(tdb.DB_PATH)
    print(f"targeted actor panel: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
    assert _repository_manifest() == before, "a test wrote into the repository's manifest"
    assert os.path.exists(tdb.DB_PATH) == db_before, "a test wrote the repository's panel database"
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
