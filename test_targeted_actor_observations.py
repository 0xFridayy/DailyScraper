"""Plain-script tests for targeted_actor_observations (py -3 test_targeted_actor_observations.py).

Offline and synthetic. Every panel is built the way production builds one:
targeted_actor_panel.collect against test_targeted_actor_panel's FakeVendor, the
/api/inventory model verified 2026-09-27, into a temporary directory. A test that
needs a panel the collector would refuse tampers with a COPY of a collected
database through its own sqlite3 connection; the reader under test never writes.
Importing test_targeted_actor_panel also blocks neobdm_scraper and Playwright, so
nothing here can log in.
"""

import copy
import glob
import hashlib
import inspect
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager

import test_targeted_actor_panel as tp          # FakeVendor and fixtures; blocks the scraper
import broker_book as bb                         # noqa: E402
import build_inventory_db as bidb                # noqa: E402
import targeted_actor_db as tdb                  # noqa: E402
import targeted_actor_observations as tao        # noqa: E402
import targeted_actor_panel as tap               # noqa: E402
import targeted_selectors as ts                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "targeted_actor_observations.py")

C, NL = tao.CALCULATED, tao.NO_LOTS
WV, WB = tao.WITHHELD_VALUE_WITHOUT_REPORTED_LOTS, tao.WITHHELD_KNOWN_BASIS_CONFLICT
KNOWN, NO_KNOWN = "KNOWN_CONFLICT", "NO_KNOWN_CONFLICT_NOT_VERIFIED"


# ── fixtures ───────────────────────────────────────────────────────────────

def tmpdir():
    # A failed assertion can leave a connection open, and Windows then cannot
    # delete the file: the cleanup error must not mask the assertion.
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def collect(tmp, markets, db="panel.db", **vendor_kw):
    """Collect every ticker of `markets` into tmp/db with the production
    collector; its result (db_path, run_id, ...)."""
    res = tp.run(tp.FakeVendor(markets, **vendor_kw), sorted(markets), tmp, db=db)
    assert res["status"] == "ok", res
    return res


def observe(db, ticker, as_of=None, include_series=True):
    conn = tao.open_readonly(db)
    try:
        return tao.observe(conn, ticker, as_of, include_series=include_series)
    finally:
        conn.close()


def refused(db, ticker, error, contains):
    """observe() must raise `error` saying `contains`; the message."""
    try:
        observe(db, ticker)
    except error as e:
        assert contains in str(e), (contains, str(e))
        return str(e)
    raise AssertionError(f"observed a panel that must fail: {contains}")


def fingerprint(path):
    """(sha256, mtime_ns) of a file and the listing of its directory."""
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return digest, os.stat(path).st_mtime_ns, sorted(os.listdir(os.path.dirname(path)))


def tampered(src, tmp, name, sql, params=(), unchecked=False):
    """A copy of the database `src` with one statement applied by the test's
    own connection (CHECK constraints ignored when `unchecked`)."""
    dst = os.path.join(tmp, name, "panel.db")
    os.makedirs(os.path.dirname(dst))
    shutil.copyfile(src, dst)
    conn = sqlite3.connect(dst)
    try:
        if unchecked:
            conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    return dst


def run_cli(args):
    return subprocess.run([sys.executable, CLI, *args], cwd=HERE, capture_output=True,
                          text=True, timeout=300)


@contextmanager
def basis(tmp, regimes, newline="\n"):
    """BASIS_FILE pointed at a temporary reference holding `regimes`."""
    path = os.path.join(tmp, "basis", "observed_basis_factor.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        json.dump({"note": "test reference", "regimes": regimes}, fh, indent=1)
    old, tao.BASIS_FILE = tao.BASIS_FILE, path
    try:
        yield path
    finally:
        tao.BASIS_FILE = old


def pad_with_zp(ticker, kept, env):
    """ZP comes back from request A with explicit all-zero series: observed,
    and a member of nothing (as in test_targeted_actor_panel)."""
    if "TOP_5_NB_LOT_C5" in kept:
        for f in tp.FIELDS:
            env["data"][f]["ZP"] = [0] * tp.N_SESSIONS
            env["data"][f] = dict(sorted(env["data"][f].items()))
    return env


def earlier(market, k):
    """The market as it stood k sessions earlier."""
    m = copy.deepcopy(market)
    m["dates"], m["ohlc"] = m["dates"][:-k], m["ohlc"][:-k]
    for s in m["brokers"].values():
        for f in s:
            s[f] = s[f][:-k]
    return m


# PRCE: 60 sessions, so C5 = 55..59, C20 = 40..59, C50 = 10..59, ALL = 0..59.
# (session, blot, bval, slot, sval) per broker; every other session is zero,
# and nlot = blot - slot, nval = bval - sval exactly.
P_N = 60
P_DATES = tp.weekdays("2025-10-06", P_N)
P_TRADES = {
    "AK": [(30, 5, 250_000, 0, 0), (55, 10, 1_000_000, 0, 0), (56, 30, 6_000_000, 0, 0),
           (57, 0, 0, 20, 6_000_000)],
    "BK": [(45, 0, 50_000, 0, 0), (58, 10, 1_100_000, 0, 0)],      # buy value, no buy lots, at 45
    "CC": [(20, 0, 0, 0, 7_000), (59, 0, 0, 5, 500_000)],          # sell value, no sell lots, at 20
    "DR": [(12, 9, 900_000, 0, 0), (50, 2, 300_000, 0, 0)],
}


def trade_market(trades, n=P_N):
    dates = tp.weekdays("2025-10-06", n)
    brokers = {}
    for code, rows in trades.items():
        s = {f: [0] * n for f in tp.FIELDS}
        for i, blot, bval, slot, sval in rows:
            s["blot"][i], s["bval"][i], s["slot"][i], s["sval"][i] = blot, bval, slot, sval
            s["nlot"][i], s["nval"][i] = blot - slot, bval - sval
        brokers[code] = s
    return {"dates": dates, "ohlc": tp.ohlc_rows(dates, 700.0), "brokers": brokers}


# broker -> horizon -> (buy, sell), each (status, implied price, value without reported lots),
# with no known basis conflict. Hand-computed from P_TRADES.
PRICES = {
    "AK": {"C5": ((C, 7_000_000 / 4_000, 0.0), (C, 6_000_000 / 2_000, 0.0)),
           "C20": ((C, 7_000_000 / 4_000, 0.0), (C, 6_000_000 / 2_000, 0.0)),
           "C50": ((C, 7_250_000 / 4_500, 0.0), (C, 6_000_000 / 2_000, 0.0)),
           "ALL": ((C, 7_250_000 / 4_500, 0.0), (C, 6_000_000 / 2_000, 0.0))},
    "BK": {"C5": ((C, 1_100_000 / 1_000, 0.0), (NL, None, 0.0)),
           "C20": ((WV, None, 50_000.0), (NL, None, 0.0)),
           "C50": ((WV, None, 50_000.0), (NL, None, 0.0)),
           "ALL": ((WV, None, 50_000.0), (NL, None, 0.0))},
    "CC": {"C5": ((NL, None, 0.0), (C, 500_000 / 500, 0.0)),
           "C20": ((NL, None, 0.0), (C, 500_000 / 500, 0.0)),
           "C50": ((NL, None, 0.0), (WV, None, 7_000.0)),
           "ALL": ((NL, None, 0.0), (WV, None, 7_000.0))},
    "DR": {"C5": ((NL, None, 0.0), (NL, None, 0.0)),
           "C20": ((C, 300_000 / 200, 0.0), (NL, None, 0.0)),
           "C50": ((C, 1_200_000 / 1_100, 0.0), (NL, None, 0.0)),
           "ALL": ((C, 1_200_000 / 1_100, 0.0), (NL, None, 0.0))},
}


def check_prices(doc, table):
    for b, by_horizon in table.items():
        for h, (buy, sell) in by_horizon.items():
            window = doc["brokers"][b]["windows"][h]
            for side, (status, price, unpriced) in (("gross_buy", buy), ("gross_sell", sell)):
                assert window[side] == {"status": status, tao.PRICE: price, tao.UNPRICED: unpriced}, (
                    b, h, side, window[side])


# ── 1. identity ────────────────────────────────────────────────────────────

def test_identity_is_ticker_and_discovery_as_of_and_run_id_is_provenance():
    market = tp.handbuilt_market()
    early = earlier(market, 1)
    late_as_of, early_as_of = market["dates"][-1], early["dates"][-1]
    with tmpdir() as tmp:
        first = collect(tmp, {"AAAA": early})
        second = collect(tmp, {"AAAA": market})                 # the same database, a later snapshot
        db = first["db_path"]
        # a second database filled in the reverse order: latest is by date, not by recording
        rev_late = collect(tmp, {"AAAA": market}, db="reverse.db")
        collect(tmp, {"AAAA": early}, db="reverse.db")
        conn = tao.open_readonly(db)
        try:
            listed = tao.list_snapshots(conn)
            assert [(r["ticker"], r["discovery_as_of"], r["run_id"]) for r in listed] == [
                ("AAAA", early_as_of, first["run_id"]), ("AAAA", late_as_of, second["run_id"])]
            assert tao.list_snapshots(conn, "AAAA") == listed and tao.list_snapshots(conn, "BBBB") == []
            latest = tao.observe(conn, "AAAA")
            explicit = tao.observe(conn, "AAAA", late_as_of)
            older = tao.observe(conn, "AAAA", early_as_of)
            for ticker, as_of in (("ZZZZ", None), ("AAAA", "2026-03-18"), ("AAAA", "2025-10-01")):
                try:
                    tao.observe(conn, ticker, as_of)
                except tao.SnapshotNotFoundError:
                    continue
                raise AssertionError((ticker, as_of))
        finally:
            conn.close()
        reverse = observe(rev_late["db_path"], "AAAA")
    assert latest["snapshot"] == {"ticker": "AAAA", "discovery_as_of": late_as_of}
    assert tao.observation_json(latest) == tao.observation_json(explicit)       # byte-identical
    assert latest["source"]["run_id"] == second["run_id"] != first["run_id"] == older["source"]["run_id"]
    assert older["snapshot"]["discovery_as_of"] == early_as_of
    assert len(older["axis"]["sessions"]) == tp.N_SESSIONS - 1
    assert reverse["snapshot"]["discovery_as_of"] == late_as_of
    assert reverse["source"]["run_id"] == rev_late["run_id"]
    # run_id is provenance: never an argument, never part of the identity
    assert "run_id" not in latest["snapshot"]
    assert list(inspect.signature(tao.observe).parameters) == [
        "conn", "ticker", "discovery_as_of", "include_series"]
    assert list(inspect.signature(tao.list_snapshots).parameters) == ["conn", "ticker"]
    assert list(inspect.signature(tao.open_readonly).parameters) == ["path"]
    assert not hasattr(tao, "AmbiguousSnapshotError") and not hasattr(tao, "resolve_snapshot")
    print("  ok test_identity_is_ticker_and_discovery_as_of_and_run_id_is_provenance")


def test_bad_arguments_are_refused_before_any_query():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        conn = tao.open_readonly(db)
        try:
            calls = ([lambda t=t: tao.observe(conn, t) for t in ("aaaa", "AAAA ", "AAAAA", "AAA", 1234, None)]
                     + [lambda d=d: tao.observe(conn, "AAAA", d)
                        for d in ("2026-3-17", "20260317", "2026-02-30", 20260317, "")]
                     + [lambda: tao.observe(conn, "AAAA", include_series="yes"),
                        lambda: tao.list_snapshots(conn, "aaaa")])
            for call in calls:
                try:
                    call()
                except ValueError:
                    continue
                raise AssertionError("a bad argument was accepted")
        finally:
            conn.close()
    print("  ok test_bad_arguments_are_refused_before_any_query")


# ── 2. membership by horizon ───────────────────────────────────────────────

def test_membership_by_horizon_follows_the_stored_rows():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()}, mutate=pad_with_zp)["db_path"]
        doc = observe(db, "AAAA")
        conn = tao.open_readonly(db)
        try:
            stored = tdb.membership(conn, "AAAA")
        finally:
            conn.close()
    brokers = doc["brokers"]
    # lossless: each stored membership row is one selection reason, field for field
    assert sum(len(e["selection_reasons"]) for e in brokers.values()) == len(stored) == sum(
        len(s["members"]) for s in doc["selectors"])
    for m in stored:
        (r,) = [r for r in brokers[m["broker"]]["selection_reasons"]
                if (r["horizon"], r["metric"]) == (m["horizon"], m["metric"])]
        for col in tao.REASON_COLUMNS:
            assert r[col] == m[col], (m["broker"], col, r[col], m[col])
    ak = brokers["AK"]
    assert [(r["horizon"], r["metric"], r["rank"], r["window_value"]) for r in ak["selection_reasons"]] == [
        ("C5", "NB_LOT", 1, 500), ("C5", "NB_VAL", 2, 50e6), ("C20", "NB_LOT", 2, 500),
        ("C20", "NB_VAL", 3, 50e6), ("C50", "NB_LOT", 3, 500), ("C50", "NB_VAL", 5, 50e6),
        ("ALL", "NB_LOT", 4, 500)]
    assert all(type(r["window_value"]) is (int if r["unit"] == "LOT" else float)
               and r["tied"] is False and r["provenance"] == "DERIVED_FROM_SELECTOR_UNION"
               for r in ak["selection_reasons"])

    def states(entry, metric):
        return [entry["membership_by_horizon"][metric][h]["state"] for h in ts.HORIZONS]
    assert states(ak, "NB_LOT") == ["MEMBER"] * 4
    assert states(ak, "NB_VAL") == ["MEMBER", "MEMBER", "MEMBER", "NOT_MEMBER"]   # ALL: DR CC BK EP SQ
    assert states(ak, "NS_LOT") == states(ak, "NS_VAL") == ["NOT_MEMBER"] * 4
    assert ak["resolved_member_horizons"] == {"NB_LOT": ["C5", "C20", "C50", "ALL"], "NS_LOT": [],
                                              "NB_VAL": ["C5", "C20", "C50"], "NS_VAL": []}
    assert (ak["horizons_selected"], ak["directions_selected"], ak["units_selected"]) == (
        ["C5", "C20", "C50", "ALL"], ["NB"], ["LOT", "VAL"])
    # every cell against its selector's stored members (all 16 are RESOLVED here)
    members = {(s["horizon"], s["metric"]): {m["broker"] for m in s["members"]} for s in doc["selectors"]}
    assert all(s["status"] == "RESOLVED" for s in doc["selectors"])
    for b, e in brokers.items():
        for metric in ts.METRICS:
            for h in ts.HORIZONS:
                assert e["membership_by_horizon"][metric][h] == {
                    "state": "MEMBER" if b in members[(h, metric)] else "NOT_MEMBER",
                    "selector_status": "RESOLVED", "boundary_tie_candidate": False}, (b, metric, h)
    xl = brokers["XL"]
    assert states(xl, "NB_LOT") == ["NOT_MEMBER"] * 4 and states(xl, "NS_LOT")[0] == "MEMBER"
    # ZP was returned with explicit zeros: observed, never selected, never zero-filled away
    zp = brokers["ZP"]
    assert doc["unexplained_brokers"] == ["ZP"] and "ZP" not in doc["selected_brokers"]
    assert zp["selection_reasons"] == [] and zp["horizons_selected"] == []
    assert zp["resolved_member_horizons"] == {m: [] for m in ts.METRICS}
    assert all(states(zp, m) == ["NOT_MEMBER"] * 4 for m in ts.METRICS)
    assert zp["nonzero_sessions"] == 0 and set(zp["series"]["coverage"]) == {tdb.OBSERVED_ZERO}
    assert zp["returned_by"] == ["A"]
    assert zp["windows"]["ALL"]["sums"] == {"blot": 0, "bval": 0.0, "slot": 0, "sval": 0.0,
                                            "nlot": 0, "nval": 0.0}
    assert zp["windows"]["ALL"]["gross_buy"]["status"] == zp["windows"]["ALL"]["gross_sell"]["status"] == NL
    # the partition; absent brokers are UNOBSERVED, with no entry and no list
    assert sorted(doc["selected_brokers"] + doc["unexplained_brokers"]) == doc["observed_brokers"]
    assert not set(doc["selected_brokers"]) & set(doc["unexplained_brokers"])
    assert sorted(brokers) == doc["observed_brokers"] and len(doc["observed_brokers"]) == 16
    assert "LG" not in brokers and "AD" not in brokers              # LG traded, never selected
    assert "unobserved_brokers" not in doc
    assert (doc["contract"]["absent_from_observed"], doc["contract"]["unobserved_is_zero"]) == (
        "UNOBSERVED", False)
    assert doc["contract"]["selector_resolution_scope"] == (
        "RETURNED_UNION_UNDER_RECORDED_SELECTOR_ASSUMPTIONS")
    # a capture's own unexplained list, carried as stored
    assert [c["unexplained_brokers"] for c in doc["source"]["captures"]] == [["ZP"], []]
    print("  ok test_membership_by_horizon_follows_the_stored_rows")


def test_boundary_ties_and_short_history_are_undetermined():
    with tmpdir() as tmp:
        tie = observe(collect(tmp, {"AAAA": tp.segment_market(tp.TIE_DAILY)}, tie="desc")["db_path"],
                      "AAAA")
        short = observe(collect(tmp, {"RAND": tp.random_market(60, n=20)}, db="short.db")["db_path"],
                        "RAND")
        tiny = observe(collect(tmp, {"RAND": tp.random_market(44, n=4)}, db="tiny.db")["db_path"],
                       "RAND")
    tied = ["AD", "AF", "AG", "AH", "AI", "AK"]
    unresolved = {(h, m) for h in ("C5", "C20", "C50") for m in ("NB_LOT", "NB_VAL")}
    for s in tie["selectors"]:
        if (s["horizon"], s["metric"]) in unresolved:
            assert s["status"] == "UNRESOLVED_BOUNDARY_TIE" and s["members"] == [], s
            assert s["boundary_tie_brokers"] == tied and s["member_count"] == 0
            assert s["boundary_tie_value"] == (100 if s["unit"] == "LOT" else 100e6)
            assert type(s["boundary_tie_value"]) is (int if s["unit"] == "LOT" else float)
        else:
            assert s["status"] == "RESOLVED", s
    for b, e in tie["brokers"].items():
        for h, metric in unresolved:
            assert e["membership_by_horizon"][metric][h] == {
                "state": "UNDETERMINED", "selector_status": "UNRESOLVED_BOUNDARY_TIE",
                "boundary_tie_candidate": b in tied}, (b, metric, h)
    ak = tie["brokers"]["AK"]
    assert [ak["membership_by_horizon"]["NB_LOT"][h]["state"] for h in ts.HORIZONS] == [
        "UNDETERMINED"] * 3 + ["MEMBER"]
    assert ak["resolved_member_horizons"]["NB_LOT"] == ["ALL"]
    # the tie names AD although request A never returned it; AD is a member only through ALL
    ad = tie["brokers"]["AD"]
    assert ad["returned_by"] == ["B"] and ad["membership_by_horizon"]["NB_LOT"]["C5"]["boundary_tie_candidate"]
    assert ad["resolved_member_horizons"]["NB_LOT"] == ["ALL"]
    # AF: tied, never a stored member: unexplained, and NOT_MEMBER only where RESOLVED
    af = tie["brokers"]["AF"]
    assert tie["unexplained_brokers"] == ["AF"] and af["selection_reasons"] == []
    assert af["membership_by_horizon"]["NB_LOT"]["ALL"]["state"] == "NOT_MEMBER"
    assert not tie["brokers"]["XL"]["membership_by_horizon"]["NB_LOT"]["C5"]["boundary_tie_candidate"]
    for doc, short_horizons in ((short, {"C50"}), (tiny, {"C5", "C20", "C50"})):
        sessions = doc["axis"]["sessions"]
        n = len(sessions)
        for h in ts.HORIZONS:
            if h in short_horizons:
                assert doc["horizons"][h] == {
                    "required_sessions": ts.HORIZON_SESSIONS[h], "available_sessions": n,
                    "window_first_session": None, "window_last_session": None,
                    "history_status": "INSUFFICIENT"}, doc["horizons"][h]
                assert doc["basis_reference"]["known_basis_conflict_by_horizon"][h] is None
            else:
                k = ts.HORIZON_SESSIONS[h] or n
                assert doc["horizons"][h] == {
                    "required_sessions": ts.HORIZON_SESSIONS[h], "available_sessions": n,
                    "window_first_session": sessions[n - k], "window_last_session": sessions[-1],
                    "history_status": "SUFFICIENT"}, doc["horizons"][h]
        for s in doc["selectors"]:
            assert (s["status"] == "INSUFFICIENT_HISTORY") == (s["horizon"] in short_horizons), s
        for b, e in doc["brokers"].items():
            for h in ts.HORIZONS:
                assert (e["windows"][h] is None) == (h in short_horizons), (b, h)
                for metric in ts.METRICS:
                    if h in short_horizons:
                        assert e["membership_by_horizon"][metric][h] == {
                            "state": "UNDETERMINED", "selector_status": "INSUFFICIENT_HISTORY",
                            "boundary_tie_candidate": False}
                        assert h not in e["resolved_member_horizons"][metric]
    print("  ok test_boundary_ties_and_short_history_are_undetermined")


# ── 3. implied price ───────────────────────────────────────────────────────

def test_implied_price_is_a_ratio_of_sums_over_reported_lots():
    with tmpdir() as tmp:
        doc = observe(collect(tmp, {"PRCE": trade_market(P_TRADES)})["db_path"], "PRCE")
    assert doc["observed_brokers"] == sorted(P_TRADES)
    check_prices(doc, PRICES)
    ak = doc["brokers"]["AK"]["windows"]["C5"]
    assert ak["sums"] == {"blot": 40, "bval": 7e6, "slot": 20, "sval": 6e6, "nlot": 20, "nval": 1e6}
    price = ak["gross_buy"][tao.PRICE]
    assert price == 1750.0
    assert price != (1_000 + 2_000) / 2                                        # not a mean of daily prices
    assert price != ak["sums"]["nval"] / (tao.SHARES_PER_LOT * ak["sums"]["nlot"])   # not nval / nlot
    # value without reported lots is reported beside the raw sums and never priced
    bk = doc["brokers"]["BK"]["windows"]
    assert bk["C20"]["sums"]["blot"] == 10 and bk["C20"]["sums"]["bval"] == 1_150_000.0
    assert bk["C20"]["gross_buy"][tao.PRICE] is None and bk["C20"]["gross_buy"][tao.UNPRICED] == 50_000.0
    assert bk["C5"]["gross_buy"] == {"status": C, tao.PRICE: 1_100.0, tao.UNPRICED: 0.0}
    # the rules, window by window: a price only for CALCULATED, and only over reported lots
    for e in doc["brokers"].values():
        for w in e["windows"].values():
            for side, lot_field, _ in tao.SIDES:
                x = w[side]
                assert (x[tao.PRICE] is not None) == (x["status"] == C)
                assert (x[tao.UNPRICED] > 0) == (x["status"] == WV)
                if x["status"] == NL:
                    assert w["sums"][lot_field] == 0
    assert doc["contract"]["price_method"] == "RATIO_OF_SUMS_OVER_REPORTED_LOTS"
    assert (doc["contract"]["shares_per_lot"], doc["contract"]["price_unit"]) == (100, "IDR_PER_SHARE")
    print("  ok test_implied_price_is_a_ratio_of_sums_over_reported_lots")


def test_a_known_basis_conflict_withholds_only_what_nothing_else_explains():
    def regime(first, last, ticker="PRCE", cls="QUARANTINE"):
        return {"ticker": ticker, "classification": cls, "regime_first_date": P_DATES[first],
                "regime_last_date": P_DATES[last]}
    with tmpdir() as tmp:
        db = collect(tmp, {"PRCE": trade_market(P_TRADES)})["db_path"]
        with basis(tmp, [regime(20, 25), regime(0, 59, ticker="OTHR")]):
            doc = observe(db, "PRCE")
        assert doc["basis_reference"]["known_conflict_intervals"] == [[P_DATES[20], P_DATES[25]]]
        assert doc["basis_reference"]["known_basis_conflict_by_horizon"] == {
            "C5": NO_KNOWN, "C20": NO_KNOWN, "C50": KNOWN, "ALL": KNOWN}
        conflicted = copy.deepcopy(PRICES)
        for h in ("C50", "ALL"):
            conflicted["AK"][h] = ((WB, None, 0.0), (WB, None, 0.0))
            conflicted["DR"][h] = ((WB, None, 0.0), (NL, None, 0.0))
            # BK and CC keep WITHHELD_VALUE_WITHOUT_REPORTED_LOTS and NO_LOTS: they come first
        check_prices(doc, conflicted)
        # inclusive boundaries; a RECONSTRUCTIBLE regime is a known conflict too
        for regimes, want in (
                ([regime(5, 10, cls="RECONSTRUCTIBLE")], (NO_KNOWN, NO_KNOWN, KNOWN, KNOWN)),
                ([regime(0, 9)], (NO_KNOWN, NO_KNOWN, NO_KNOWN, KNOWN)),
                ([regime(59, 59)], (KNOWN, KNOWN, KNOWN, KNOWN)),
                ([regime(0, 59, ticker="OTHR")], (NO_KNOWN,) * 4),
                ([], (NO_KNOWN,) * 4)):
            with basis(tmp, regimes):
                got = observe(db, "PRCE")["basis_reference"]["known_basis_conflict_by_horizon"]
            assert got == dict(zip(ts.HORIZONS, want)), (regimes, got)
    text = tao.observation_json(doc)
    assert doc["contract"]["basis_absence_means"] == NO_KNOWN
    assert not re.search(r"(?<!NOT_)VERIFIED", text)          # absence is never "verified"
    print("  ok test_a_known_basis_conflict_withholds_only_what_nothing_else_explains")


# ── 4. integrity ───────────────────────────────────────────────────────────

def test_gross_data_the_collector_accepts_can_still_fail_the_reader():
    """These rows pass the collector's source contract (it checks neither sign
    nor lots without value) and are stored; the reader refuses to observe them."""
    cases = (
        ("DR", [(12, -3, 0, 0, 0), (50, 2, 300_000, 0, 0)], "negative gross lots"),
        ("BK", [(45, 0, -50_000, 0, 0), (58, 10, 1_100_000, 0, 0)], "negative gross value"),
        ("AK", [(30, 5, 0, 0, 0)] + P_TRADES["AK"][1:], "buy lots reported with zero buy value"),
        ("CC", [(20, 0, 0, 0, 7_000), (59, 0, 0, 5, 0)], "sell lots reported with zero sell value"),
    )
    with tmpdir() as tmp:
        for i, (broker, trades, contains) in enumerate(cases):
            res = collect(tmp, {"PRCE": trade_market(dict(P_TRADES, **{broker: trades}))}, db=f"g{i}.db")
            assert res["ok"] == ["PRCE"], res                          # stored by the collector
            message = refused(res["db_path"], "PRCE", tao.PanelIntegrityError, contains)
            assert message.startswith(f"PRCE {P_DATES[-1]}: {broker} "), message
    print("  ok test_gross_data_the_collector_accepts_can_still_fail_the_reader")


# (what the reader must say, SQL, params, CHECK constraints ignored)
TAMPER = (
    ("!= blot - slot", "UPDATE observed_series SET nlot = nlot + 1 WHERE broker = 'AK' "
                       "AND session_date = ?", (P_DATES[55],), False),
    ("|nval - (bval - sval)| exceeds", "UPDATE observed_series SET nval = nval + 0.75 "
                                       "WHERE broker = 'AK' AND session_date = ?", (P_DATES[55],), False),
    ("one for one", "DELETE FROM observed_series WHERE broker = 'AK' AND session_date = ?",
     (P_DATES[0],), False),
    ("disagrees with its values", "UPDATE observed_series SET coverage = 'OBSERVED_NONZERO' "
                                  "WHERE broker = 'AK' AND session_date = ?", (P_DATES[0],), False),
    ("stored membership rows disagree", "DELETE FROM selection_membership WHERE horizon = 'C5' "
                                        "AND metric = 'NB_LOT' AND broker = 'BK'", (), False),
    ("rank 2 but 0 stored member(s)", "UPDATE selection_membership SET rank = 2 WHERE horizon = 'C5' "
                                      "AND metric = 'NB_LOT' AND broker = 'AK'", (), False),
    ("tied 1 but 1 stored member(s)", "UPDATE selection_membership SET tied = 1 WHERE horizon = 'C5' "
                                      "AND metric = 'NB_LOT' AND broker = 'AK'", (), False),
    ("does not have the NB sign", "UPDATE selection_membership SET window_value = -window_value "
                                  "WHERE horizon = 'C20' AND metric = 'NB_LOT'", (), False),
    ("union_capture_ids", "UPDATE selection_membership SET union_capture_ids = '[\"x\", \"y\"]' "
                          "WHERE horizon = 'C5' AND metric = 'NB_LOT' AND broker = 'AK'", (), False),
    ("provenance 'VENDOR_RANK'", "UPDATE selection_membership SET provenance = 'VENDOR_RANK' "
                                 "WHERE horizon = 'C5' AND metric = 'NB_LOT' AND broker = 'AK'", (), True),
    ("window fields", "UPDATE selection_membership SET window_first_session = ? WHERE horizon = 'C5' "
                      "AND metric = 'NB_LOT' AND broker = 'AK'", (P_DATES[0],), False),
    ("not exactly the 16", "DELETE FROM selection_status WHERE horizon = 'C5' AND metric = 'NS_VAL'",
     (), False),
    ("but has membership rows", "UPDATE selection_status SET status = 'UNRESOLVED_BOUNDARY_TIE', "
                                "member_count = 0, boundary_tie_value = 10, "
                                "boundary_tie_brokers = '[\"AK\", \"BK\"]' "
                                "WHERE horizon = 'C5' AND metric = 'NB_LOT'", (), False),
    ("expanded_brokers disagree", "UPDATE observed_brokers SET in_request_a = 0 WHERE broker = 'AK'",
     (), False),
    ("unexplained_brokers ['AK'] disagree", "UPDATE panel_captures SET unexplained_brokers = '[\"AK\"]' "
                                            "WHERE request_group = 'A'", (), False),
    ("selector_plan 'targeted_actor_v2'", "UPDATE panel_snapshots SET selector_plan = 'targeted_actor_v2'",
     (), False),
    ("horizons and tokens", "UPDATE panel_captures SET selector_tokens = '[]' WHERE request_group = 'B'",
     (), False),
    ("session_index", "UPDATE panel_sessions SET session_index = session_index + 100 "
                      "WHERE session_date = ?", (P_DATES[3],), False),
)


def test_the_stored_structure_is_checked_and_never_rederived():
    with tmpdir() as tmp:
        db = collect(tmp, {"PRCE": trade_market(P_TRADES)})["db_path"]
        good = tao.observation_json(observe(db, "PRCE"))
        for i, (contains, sql, params, unchecked) in enumerate(TAMPER):
            copy_ = tampered(db, tmp, f"t{i}", sql, params, unchecked)
            refused(copy_, "PRCE", tao.PanelIntegrityError, contains)
        meta = tampered(db, tmp, "meta", "UPDATE panel_meta SET value = 'true' WHERE key = 'full_universe'")
        try:
            tao.open_readonly(meta)
        except tdb.NotTargetedPanelError:
            pass
        else:
            raise AssertionError("a panel claiming full_universe was opened")
        # the source tolerance is kept exactly: half a rupiah of drift passes, as at collection
        edge = tampered(db, tmp, "edge", "UPDATE observed_series SET nval = nval + 0.5 "
                                         "WHERE broker = 'AK' AND session_date = ?", (P_DATES[55],))
        assert observe(edge, "PRCE")["brokers"]["AK"]["series"]["nval"][55] == 1_000_000.5
        # Stored values are the product. Doubling every C20 NB_LOT window value keeps the
        # stored rows consistent with each other (ranks, ties, signs), so the reader reports
        # them as stored; it never recomputes them from the series.
        doubled = tampered(db, tmp, "doubled", "UPDATE selection_membership SET window_value = "
                                               "2 * window_value WHERE horizon = 'C20' AND metric = 'NB_LOT'")
        doc = observe(doubled, "PRCE")
        (reason,) = [r for r in doc["brokers"]["AK"]["selection_reasons"]
                     if (r["horizon"], r["metric"]) == ("C20", "NB_LOT")]
        assert reason["window_value"] == 40 and doc["brokers"]["AK"]["windows"]["C20"]["sums"]["nlot"] == 20
        # and the collector's derivation may be broken outright: nothing here calls it
        real = tap.derive_membership

        def broken(*a, **k):
            raise AssertionError("the reader re-derived the selection")
        tap.derive_membership = broken
        try:
            assert tao.observation_json(observe(db, "PRCE")) == good
        finally:
            tap.derive_membership = real
    print("  ok test_the_stored_structure_is_checked_and_never_rederived")


# ── 5. read-only ───────────────────────────────────────────────────────────

def test_the_reader_never_writes_and_never_creates():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        before = fingerprint(db)
        real_connect = tdb.connect

        def writer(*a, **k):
            raise AssertionError("the reader called targeted_actor_db.connect()")
        tdb.connect = writer
        try:
            conn = tao.open_readonly(db)
            try:
                tao.list_snapshots(conn)
                tao.observe(conn, "AAAA")
                tao.observe(conn, "AAAA", include_series=False)
                try:
                    tao.observe(conn, "AAAA", "2020-01-01")
                except tao.SnapshotNotFoundError:
                    pass
                assert fingerprint(db)[2] == before[2]           # no sidecar while it is open
                for sql in ("INSERT INTO panel_meta (key, value) VALUES ('x', 'y')",
                            "DELETE FROM panel_runs", "CREATE TABLE t (x)"):
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError:
                        continue
                    raise AssertionError(f"the read-only connection ran {sql}")
            finally:
                conn.close()
            for args in (["list", "--db", db], ["observe", "--db", db, "AAAA"],
                         ["observe", "--db", db, "--no-series", "AAAA"], ["observe", "--db", db, "ZZZZ"]):
                run_cli(args)
        finally:
            tdb.connect = real_connect
        assert fingerprint(db) == before                           # bytes, mtime, no sidecar

        # a missing database fails and is never created
        for name in ("missing.db", "targeted_actor_panel.db"):
            path = os.path.join(tmp, "absent", name)
            try:
                tao.open_readonly(path)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError(path)
            assert not os.path.exists(os.path.dirname(path))
        r = run_cli(["observe", "--db", os.path.join(tmp, "absent.db"), "AAAA"])
        assert (r.returncode, r.stdout) == (1, "") and "FileNotFoundError" in r.stderr, r
        assert not os.path.exists(os.path.join(tmp, "absent.db"))
        # another product's name or content is refused and left as it was
        for name in ("broker_learning.db", "neobdm.db"):
            try:
                tao.open_readonly(os.path.join(tmp, name))
            except tdb.NotTargetedPanelError:
                assert not os.path.exists(os.path.join(tmp, name))
                continue
            raise AssertionError(name)
        other = os.path.join(tmp, "other", "x.db")
        os.makedirs(os.path.dirname(other))
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE foo (x)")
        conn.close()
        mark = fingerprint(other)
        try:
            tao.open_readonly(other)
        except tdb.NotTargetedPanelError:
            pass
        else:
            raise AssertionError("a foreign database was opened")
        assert fingerprint(other) == mark
        # a connection that can write is refused before anything is begun on it
        copy_ = os.path.join(tmp, "writer", "panel.db")
        os.makedirs(os.path.dirname(copy_))
        shutil.copyfile(db, copy_)
        for conn in (sqlite3.connect(copy_), tdb.connect(copy_)):
            try:
                for call in (lambda: tao.observe(conn, "AAAA"), lambda: tao.list_snapshots(conn)):
                    try:
                        call()
                    except ValueError as e:
                        assert "query_only" in str(e) and not conn.in_transaction, e
                        continue
                    raise AssertionError("a writable connection was read")
            finally:
                conn.close()
    print("  ok test_the_reader_never_writes_and_never_creates")


# ── 6. determinism ─────────────────────────────────────────────────────────

def test_the_document_is_canonical_and_free_of_paths_machines_and_time():
    as_of = tp.weekdays("2025-10-01", tp.N_SESSIONS)[-1]
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        elsewhere = os.path.join(tmp, "some other place #2", "renamed.db")
        os.makedirs(os.path.dirname(elsewhere))
        shutil.copyfile(db, elsewhere)
        default = tao.observation_json(observe(db, "AAAA"))
        explicit = tao.observation_json(observe(elsewhere, "AAAA", as_of))
        # the same reference with LF and with CRLF line ends: different bytes, one sha256
        with open(tao.BASIS_FILE, encoding="utf-8") as fh:
            content = fh.read()
        seen = {}
        for newline in ("\n", "\r\n"):
            path = os.path.join(tmp, f"basis{len(newline)}", "observed_basis_factor.json")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8", newline=newline) as fh:
                fh.write(content)
            with open(path, "rb") as fh:
                raw = hashlib.sha256(fh.read()).hexdigest()
            old, tao.BASIS_FILE = tao.BASIS_FILE, path
            try:
                seen[newline] = (raw, tao.observation_json(observe(elsewhere, "AAAA")))
            finally:
                tao.BASIS_FILE = old
        r = run_cli(["observe", "--db", elsewhere, "AAAA"])
    assert seen["\n"][0] != seen["\r\n"][0]
    assert default == explicit == seen["\n"][1] == seen["\r\n"][1]
    canonical = json.dumps(json.loads(content), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    doc = json.loads(default)
    assert doc["basis_reference"]["canonical_json_sha256"] == hashlib.sha256(canonical.encode("ascii")).hexdigest()
    assert "sha256" not in doc["basis_reference"]
    assert doc["basis_reference"]["source_id"] == "observed_basis_factor.json"
    # no path of any kind: not the database, not the reference, not this checkout
    for fragment in (tmp, db, elsewhere, HERE, os.path.dirname(tao.BASIS_FILE)):
        assert fragment not in default and json.dumps(fragment)[1:-1] not in default, fragment
    assert not re.search(r"[A-Za-z]:\\\\", default) and "/home/" not in default
    assert "worktrees" not in default and "_capture_manifest" not in default
    # canonical: sorted keys, compact separators, ASCII, no NaN; the CLI prints those bytes
    assert default == json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert default.isascii() and "NaN" not in default and "Infinity" not in default
    assert (r.returncode, r.stdout, r.stderr) == (0, default + "\n", ""), r.stderr
    print("  ok test_the_document_is_canonical_and_free_of_paths_machines_and_time")


# ── 7. the v1 output schema ────────────────────────────────────────────────

_CAPTURE = ("request_group", "horizons", "selector_tokens", "capture_id", "manifest_run_id",
            "attempt", "query_sha256", "http_status", "response_bytes", "response_sha256",
            "response_text_sha256", "captured_at", "vendor_success", "vendor_meta_brokers",
            "expanded_brokers", "unexplained_brokers", "source_status")
_SELECTOR = ("selector_token", "horizon", "metric", "direction", "unit", "request_group",
             "selector_capture_id", "status", "sessions_required", "sessions_available",
             "window_first_session", "window_last_session", "qualifying_count", "member_count",
             "boundary_tie_value", "boundary_tie_brokers", "members")
_REASON = ("selector_token", "horizon", "metric", "direction", "unit", "rank", "tied",
           "window_value", "window_first_session", "window_last_session", "window_sessions",
           "request_group", "selector_capture_id", "union_capture_ids", "provenance")
_SIX = ("blot", "bval", "slot", "sval", "nlot", "nval")
_B = "brokers.<broker>"
# Every key path of a v1 document, written out here so that a schema change
# has to change this test as well as the module.
ALLOWED_PATHS = frozenset(
    ["schema", "schema_version", "contract", "snapshot", "snapshot.ticker", "snapshot.discovery_as_of",
     "source", "source.captures", "basis_reference", "basis_reference.source_id",
     "basis_reference.canonical_json_sha256", "basis_reference.known_conflict_intervals",
     "basis_reference.known_basis_conflict_by_horizon",
     "basis_reference.known_basis_conflict_by_horizon.<horizon>", "series_included", "axis",
     "axis.sessions", "axis.ohlc", "horizons", "horizons.<horizon>", "selectors",
     "observed_brokers", "selected_brokers", "unexplained_brokers", "brokers", _B]
    + [f"contract.{k}" for k in (
        "coverage_scope", "full_universe", "absent_from_observed", "unobserved_is_zero",
        "selected_means", "unexplained_means", "rank_provenance", "qualifying_count_scope",
        "selector_resolution_scope", "horizon_order", "metric_order", "horizons_nested",
        "horizon_anchor", "shares_per_lot", "value_unit", "price_unit", "price_method",
        "basis_absence_means")]
    + [f"source.{k}" for k in (
        "product", "panel_schema_version", "collection_mode", "selector_plan", "investor_type",
        "requested_start_date", "requested_end_date", "run_id", "content_sha256", "recorded_utc")]
    + [f"source.captures[].{k}" for k in _CAPTURE]
    + [f"axis.ohlc.{k}" for k in ("open", "high", "low", "close", "volume", "volume_sma20")]
    + [f"horizons.<horizon>.{k}" for k in ("required_sessions", "available_sessions",
                                           "window_first_session", "window_last_session",
                                           "history_status")]
    + [f"selectors[].{k}" for k in _SELECTOR]
    + [f"selectors[].members[].{k}" for k in ("rank", "broker", "window_value", "tied")]
    + [f"{_B}.{k}" for k in ("returned_by", "nonzero_sessions", "selection_reasons",
                             "horizons_selected", "directions_selected", "units_selected",
                             "membership_by_horizon", "resolved_member_horizons", "windows", "series")]
    + [f"{_B}.selection_reasons[].{k}" for k in _REASON]
    + [f"{_B}.membership_by_horizon.<metric>", f"{_B}.membership_by_horizon.<metric>.<horizon>"]
    + [f"{_B}.membership_by_horizon.<metric>.<horizon>.{k}"
       for k in ("state", "selector_status", "boundary_tie_candidate")]
    + [f"{_B}.resolved_member_horizons.<metric>", f"{_B}.windows.<horizon>",
       f"{_B}.windows.<horizon>.sums"]
    + [f"{_B}.windows.<horizon>.sums.{k}" for k in _SIX]
    + [f"{_B}.windows.<horizon>.{side}" for side in ("gross_buy", "gross_sell")]
    + [f"{_B}.windows.<horizon>.{side}.{k}" for side in ("gross_buy", "gross_sell")
       for k in ("status", "implied_price_from_reported_lots_rp_per_share",
                 "value_without_reported_lots_rp")]
    + [f"{_B}.series.{k}" for k in _SIX + ("coverage",)])

# Interpretive fields that must not exist anywhere in the document.
FORBIDDEN_KEYS = ("owner", "bandar", "retail_class", "smart_money", "signal", "recommendation",
                  "entry", "exit", "market_concentration", "market_share")
HORIZON_MAPS = ("horizons", "windows", "known_basis_conflict_by_horizon")
METRIC_MAPS = ("membership_by_horizon", "resolved_member_horizons")


def key_paths(value, path="", out=None):
    """Every key path of a document, with <broker>, <horizon> and <metric>
    standing for those map keys (each checked to be one)."""
    out = set() if out is None else out
    if isinstance(value, dict):
        last = path.rsplit(".", 1)[-1]
        for key, sub in value.items():
            if path == "brokers":
                assert re.fullmatch(r"[A-Z]{2}", key), key
                seg = "<broker>"
            elif last in HORIZON_MAPS or last == "<metric>":
                assert key in ts.HORIZONS, (path, key)
                seg = "<horizon>"
            elif last in METRIC_MAPS:
                assert key in ts.METRICS, (path, key)
                seg = "<metric>"
            else:
                seg = key
            child = f"{path}.{seg}" if path else seg
            out.add(child)
            key_paths(sub, child, out)
    elif isinstance(value, list):
        for item in value:
            key_paths(item, path + "[]", out)
    return out


def all_keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        out.update(value)
        for sub in value.values():
            all_keys(sub, out)
    elif isinstance(value, list):
        for sub in value:
            all_keys(sub, out)
    return out


def test_the_output_schema_is_exactly_the_v1_allowlist():
    with tmpdir() as tmp:
        db = collect(tmp, {"PRCE": trade_market(P_TRADES)})["db_path"]
        full = observe(db, "PRCE")
        lean = observe(db, "PRCE", include_series=False)
        short = observe(collect(tmp, {"RAND": tp.random_market(60, n=20)}, db="short.db")["db_path"],
                        "RAND")
        tie = observe(collect(tmp, {"AAAA": tp.segment_market(tp.TIE_DAILY)}, tie="desc",
                              db="tie.db")["db_path"], "AAAA")
    assert key_paths(full) == ALLOWED_PATHS, sorted(key_paths(full) ^ ALLOWED_PATHS)
    for doc in (lean, short, tie):
        assert key_paths(doc) <= ALLOWED_PATHS, sorted(key_paths(doc) - ALLOWED_PATHS)
    # without the series, only the series and the OHLC go
    assert lean["series_included"] is False and lean["axis"]["ohlc"] is None
    assert all(e["series"] is None for e in lean["brokers"].values())

    def strip(d):
        return {**d, "series_included": None, "axis": {**d["axis"], "ohlc": None},
                "brokers": {b: {**e, "series": None} for b, e in d["brokers"].items()}}
    assert strip(full) == strip(lean)
    for doc in (full, lean, short, tie):
        keys = all_keys(doc)
        assert not keys & set(FORBIDDEN_KEYS), keys & set(FORBIDDEN_KEYS)
        assert not [k for k in keys if any(w in k for w in ("score", "persistence", "odd", "cost"))]
        assert "shares_per_lot" in keys                         # a unit, and allowed
        for e in doc["brokers"].values():
            for row in e["membership_by_horizon"].values():
                assert {c["state"] for c in row.values()} <= {"MEMBER", "NOT_MEMBER", "UNDETERMINED"}
            for w in e["windows"].values():
                if w is not None:
                    assert {w["gross_buy"]["status"], w["gross_sell"]["status"]} <= set(tao.PRICE_STATUSES)
        assert {h["history_status"] for h in doc["horizons"].values()} <= {"SUFFICIENT", "INSUFFICIENT"}
        assert set(doc["basis_reference"]["known_basis_conflict_by_horizon"].values()) <= {
            KNOWN, NO_KNOWN, None}
        assert set(tao.PRICE_STATUSES) == {C, NL, WV, WB} and len(tao.PRICE_STATUSES) == 4
    # the module's own check refuses a document with a field added, removed or inconsistent
    for mutate in (lambda d: d["brokers"]["AK"].__setitem__("signal", "BUY"),
                   lambda d: d.__setitem__("market_share", 0.3),
                   lambda d: d["contract"].pop("unobserved_is_zero"),
                   lambda d: d["brokers"]["AK"]["windows"]["C5"]["gross_buy"].__setitem__(tao.PRICE, None),
                   lambda d: d["brokers"]["AK"]["membership_by_horizon"]["NB_LOT"]["C5"].__setitem__(
                       "state", "NOT_MEMBER"),
                   lambda d: d["unexplained_brokers"].append("AK")):
        bad = copy.deepcopy(full)
        mutate(bad)
        try:
            tao._check_document(bad)
        except tao.ObservationContractError:
            continue
        raise AssertionError("an off-contract document passed the module's own check")
    print("  ok test_the_output_schema_is_exactly_the_v1_allowlist")


# ── 8. the basis reference and shared constants ────────────────────────────

def test_the_basis_reference_follows_broker_book_and_fails_closed():
    reference = tao._basis_reference()
    assert reference["intervals"] and reference["intervals"] == {
        t: sorted(set(v)) for t, v in bb.load_basis_regimes().items()}
    assert tao.RUPIAH_TOLERANCE == bidb.RUPIAH_TOLERANCE
    assert tao.SHARES_PER_LOT == bb.SHARES_PER_LOT == 100
    bad_references = (None, "not json {", json.dumps([]), json.dumps({"regimes": "x"}),
                      json.dumps({"no": "regimes"}),
                      json.dumps({"regimes": [{"ticker": "PRCE", "regime_first_date": "2025-10-10"}]}),
                      json.dumps({"regimes": [{"ticker": "PRCE", "regime_first_date": "2025-10-10",
                                               "regime_last_date": "2025-10-01"}]}),
                      json.dumps({"regimes": [{"ticker": "PRCE", "regime_first_date": "10/10/2025",
                                               "regime_last_date": "2025-10-11"}]}),
                      json.dumps({"regimes": [{"ticker": "", "regime_first_date": "2025-10-10",
                                               "regime_last_date": "2025-10-11"}]}))
    with tmpdir() as tmp:
        db = collect(tmp, {"PRCE": trade_market(P_TRADES)})["db_path"]
        for i, text in enumerate(bad_references):
            path = os.path.join(tmp, f"b{i}", "observed_basis_factor.json")
            os.makedirs(os.path.dirname(path))
            if text is not None:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            old, tao.BASIS_FILE = tao.BASIS_FILE, path
            try:
                refused(db, "PRCE", tao.BasisReferenceError, "observed_basis_factor.json")
            finally:
                tao.BASIS_FILE = old
    print("  ok test_the_basis_reference_follows_broker_book_and_fails_closed")


# ── 9. hygiene and the CLI ─────────────────────────────────────────────────

def test_imports_are_light_and_the_collector_is_never_loaded():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        env = {k: v for k, v in os.environ.items() if k.upper() == "SYSTEMROOT"}
        code = ("import sys, targeted_actor_observations as tao; "
                "conn = tao.open_readonly(sys.argv[1]); doc = tao.observe(conn, 'AAAA'); conn.close(); "
                "bad = [m for m in ('targeted_actor_panel', 'broker_collect', 'inventory_capture', "
                "'broker_book', 'build_inventory_db', 'price_audit', 'pandas', 'numpy', 'pyarrow', "
                "'requests', 'neobdm_scraper', 'playwright') if m in sys.modules]; "
                "assert not bad, bad; print('ok', len(doc['observed_brokers']))")
        r = subprocess.run([sys.executable, "-c", code, db], cwd=HERE, env=env, capture_output=True,
                           text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok 15", (r.stdout, r.stderr[-2000:])
    print("  ok test_imports_are_light_and_the_collector_is_never_loaded")


def test_the_cli_prints_json_and_fails_without_output():
    as_of = tp.weekdays("2025-10-01", tp.N_SESSIONS)[-1]
    with tmpdir() as tmp:
        res = collect(tmp, {"AAAA": tp.handbuilt_market()})
        db = res["db_path"]
        conn = tao.open_readonly(db)
        try:
            doc, lean = tao.observe(conn, "AAAA"), tao.observe(conn, "AAAA", include_series=False)
            listed = tao.list_snapshots(conn)
        finally:
            conn.close()
        r = run_cli(["observe", "--db", db, "--as-of", as_of, "AAAA"])
        assert (r.returncode, r.stdout, r.stderr) == (0, tao.observation_json(doc) + "\n", ""), r.stderr
        r = run_cli(["observe", "--db", db, "--no-series", "AAAA"])
        assert (r.returncode, r.stdout) == (0, tao.observation_json(lean) + "\n"), r.stderr
        for args in (["list", "--db", db], ["list", "--db", db, "AAAA"]):
            r = run_cli(args)
            assert r.returncode == 0 and json.loads(r.stdout) == listed, r.stderr
        for args, error in ((["observe", "--db", db, "ZZZZ"], "SnapshotNotFoundError"),
                            (["observe", "--db", db, "--as-of", "2020-01-01", "AAAA"],
                             "SnapshotNotFoundError"),
                            (["observe", "--db", db, "aaaa"], "ValueError"),
                            (["observe", "--db", db, "--as-of", "2026/03/17", "AAAA"], "ValueError")):
            r = run_cli(args)
            assert (r.returncode, r.stdout) == (1, "") and error in r.stderr, (args, r.stderr)
        r = run_cli(["observe", "--db", db, "--run-id", res["run_id"], "AAAA"])
        assert (r.returncode, r.stdout) == (2, "") and "--run-id" in r.stderr, r.stderr
    print("  ok test_the_cli_prints_json_and_fails_without_output")


# ── 10. source states: only a quiescent source is read ─────────────────────
#
# v1 reads only a quiescent source (no -wal, -shm or -journal beside it: rollback
# journal, or WAL fully checkpointed), always opened with immutable=1; every other
# state is refused before SQLite opens the file. The WAL fixtures are real WAL
# databases: another process commits the newest snapshot through the WAL, then closes
# (checkpointed), exits without closing (the frames stay in -wal beside -shm) or stays
# open. The expected document is the same snapshot read from a plain rollback-journal
# copy, so "correct" means byte-identical to that.

WAL_WRITER = r"""
import os, sqlite3, sys
db, src, finish = sys.argv[1], sys.argv[2], sys.argv[3]
c = sqlite3.connect(db, isolation_level=None)
assert c.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("ATTACH DATABASE ? AS src", (src,))
c.execute("BEGIN")
for table in ("panel_runs", "panel_snapshots", "panel_captures", "panel_sessions",
              "observed_brokers", "observed_series", "selection_status", "selection_membership"):
    c.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table}")
c.execute("COMMIT")
c.execute("DETACH DATABASE src")
if finish == "close":
    c.close()
    sys.exit(0)
if finish == "hold":
    print("ready", flush=True)
    sys.stdin.readline()
os._exit(0)          # never closed: the committed frames stay in -wal, beside -shm
"""

SQL_WRITER = r"""
import os, sqlite3, sys
db, finish, script = sys.argv[1], sys.argv[2], sys.argv[3]
c = sqlite3.connect(db, isolation_level=None)
c.executescript(script)
if finish == "close":
    c.close()
    sys.exit(0)
os._exit(0)
"""


def wal_panel(tmp, finish):
    """(db, expected latest document, holder process or None): a panel whose
    newest snapshot another process committed through WAL, then `finish`ed:
    close, crash or hold (the caller releases a holder)."""
    market = tp.handbuilt_market()
    source, newer = os.path.join(tmp, "source"), os.path.join(tmp, "newer")
    os.makedirs(source)
    os.makedirs(newer)
    db = collect(source, {"AAAA": earlier(market, 1)})["db_path"]
    newer_db = collect(newer, {"AAAA": market})["db_path"]
    expected = tao.observation_json(observe(newer_db, "AAAA"))
    args = [sys.executable, "-c", WAL_WRITER, db, newer_db, finish]
    if finish == "hold":
        holder = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert holder.stdout.readline().strip() == "ready"
        return db, expected, holder
    subprocess.run(args, check=True, timeout=120)
    return db, expected, None


def release(holder):
    holder.stdin.write("\n")
    holder.stdin.flush()
    holder.wait(timeout=60)


def sql_writer(db, finish, script):
    subprocess.run([sys.executable, "-c", SQL_WRITER, db, finish, script], check=True, timeout=120)


def source_files(db):
    """The database directory's file set, and each file's size and sha256."""
    d = os.path.dirname(db)
    out = {}
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                out[name] = (os.path.getsize(path), hashlib.sha256(fh.read()).hexdigest())
        else:
            out[name] = "directory"
    return out


def header_format(db):
    with open(db, "rb") as fh:
        header = fh.read(100)
    return header[18], header[19]            # (1, 1) rollback journal, (2, 2) WAL


def main_file_only(db):
    """The snapshots in the main database file alone, the WAL ignored: the
    test's own immutable read, which creates and changes nothing."""
    conn = sqlite3.connect(pathlib.Path(db).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT discovery_as_of FROM panel_snapshots ORDER BY 1")]
    finally:
        conn.close()


@contextmanager
def recorded_opens():
    """Every database URI sqlite3.connect is given while the block runs."""
    seen, real = [], sqlite3.connect

    def spy(database, *args, **kwargs):
        seen.append(database)
        return real(database, *args, **kwargs)
    sqlite3.connect = spy
    try:
        yield seen
    finally:
        sqlite3.connect = real


def refused_state(db, contains):
    """observe() must raise ReadOnlySourceStateError saying `contains` before
    SQLite opens the file, and leave every file of the directory as it was."""
    before = source_files(db)
    with recorded_opens() as opens:
        try:
            observe(db, "AAAA")
        except tao.ReadOnlySourceStateError as e:
            assert contains in str(e), (contains, str(e))
        else:
            raise AssertionError(f"read an unsupported source state ({contains}); files now "
                                 f"{sorted(source_files(db))}")
    assert opens == [], opens                                        # refused before SQLite opened it
    assert source_files(db) == before, (sorted(before), sorted(source_files(db)))


def read_supported(db, as_of=None):
    """(list_snapshots, the canonical document, the URIs SQLite was given, the
    directory while the connection was open) for a supported source."""
    with recorded_opens() as opens:
        conn = tao.open_readonly(db)
    try:
        listed = [r["discovery_as_of"] for r in tao.list_snapshots(conn)]
        text = tao.observation_json(tao.observe(conn, "AAAA", as_of))
        during = source_files(db)
    finally:
        conn.close()
    return listed, text, opens, during


def test_a_stable_rollback_panel_is_read_immutable_without_sidecars():
    market = tp.handbuilt_market()
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": market})["db_path"]
        plain = observe(db, "AAAA")
        before = source_files(db)
        assert header_format(db) == (1, 1) and not any(n.startswith("panel.db-") for n in before)
        listed, text, opens, during = read_supported(db)
        after = source_files(db)
    assert len(opens) == 1 and opens[0].endswith("?mode=ro&immutable=1"), opens
    assert listed == [market["dates"][-1]] and text == tao.observation_json(plain)
    assert during == before and after == before, sorted(set(after) ^ set(before))
    print("  ok test_a_stable_rollback_panel_is_read_immutable_without_sidecars")


def test_a_checkpointed_wal_panel_is_read_without_creating_sidecars():
    """Codex's first reproduction: a valid WAL-mode panel with no sidecars (its
    last writer closed and checkpointed)."""
    late, early = tp.handbuilt_market()["dates"][-1], tp.handbuilt_market()["dates"][-2]
    with tmpdir() as tmp:
        db, expected, _ = wal_panel(tmp, "close")
        before = source_files(db)
        assert header_format(db) == (2, 2) and not any(n.startswith("panel.db-") for n in before)
        assert main_file_only(db) == [early, late]                  # checkpointed into the main file
        listed, got, opens, during = read_supported(db)
        _, older, _, _ = read_supported(db, early)
        after = source_files(db)
    assert len(opens) == 1 and opens[0].endswith("?mode=ro&immutable=1"), opens
    assert listed == [early, late] and got == expected and json.loads(older)["snapshot"]["discovery_as_of"] == early
    assert during == before, sorted(set(during) - set(before))     # nothing while it is open
    assert after == before, sorted(set(after) ^ set(before))       # no -wal, -shm or -journal
    print("  ok test_a_checkpointed_wal_panel_is_read_without_creating_sidecars")


def test_a_wal_with_shm_is_refused_before_sqlite_opens_it():
    """The newest snapshot is committed only in the -wal (a writer still open, or
    gone without closing): live WAL is unsupported in v1, so it is refused before
    any open, and the database, -wal and -shm stay byte-identical."""
    early = tp.handbuilt_market()["dates"][-2]
    for finish in ("hold", "crash"):
        with tmpdir() as tmp:
            db, _, holder = wal_panel(tmp, finish)
            try:
                assert {"panel.db-wal", "panel.db-shm"} <= set(source_files(db))
                assert main_file_only(db) == [early]                # the late snapshot is only in the WAL
                refused_state(db, "-wal and -shm present beside the database")
            finally:
                if holder is not None:
                    release(holder)
    print("  ok test_a_wal_with_shm_is_refused_before_sqlite_opens_it")


def test_a_wal_without_its_shm_is_refused_not_repaired():
    with tmpdir() as tmp:
        db, _, _ = wal_panel(tmp, "crash")
        os.remove(db + "-shm")                  # committed frames in -wal, and no -shm to read them with
        refused_state(db, "-wal present beside the database")
        assert not os.path.exists(db + "-shm")
    print("  ok test_a_wal_without_its_shm_is_refused_not_repaired")


def test_other_unsupported_source_states_are_refused_without_writing():
    with tmpdir() as tmp:
        plain = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]

        def case(name, sidecars=(), wal=False):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            db = os.path.join(d, "panel.db")
            shutil.copyfile(plain, db)
            if wal:
                sql_writer(db, "close", "PRAGMA journal_mode=WAL;")
                assert header_format(db) == (2, 2) and not os.path.exists(db + "-wal")
            for suffix in sidecars:
                with open(db + suffix, "wb") as fh:
                    fh.write(b"\0" * 4096)
            return db
        refused_state(case("rollback_with_wal", ["-wal"]), "-wal present beside the database")
        refused_state(case("rollback_with_shm", ["-shm"]), "-shm present beside the database")
        refused_state(case("wal_shm_only", ["-shm"], wal=True), "-shm present beside the database")
        refused_state(case("wal_with_journal", ["-journal"], wal=True), "-journal present beside the database")
        refused_state(case("rollback_with_journal", ["-journal"]), "-journal present beside the database")
        # a writer that died mid-transaction leaves a hot rollback journal
        hot = case("hot_journal")
        sql_writer(hot, "crash", "PRAGMA cache_size=1; BEGIN; CREATE TABLE spill (x); "
                                 "INSERT INTO spill WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL "
                                 "SELECT i + 1 FROM r WHERE i < 20000) SELECT i FROM r;")
        assert os.path.exists(hot + "-journal") and header_format(hot) == (1, 1)
        refused_state(hot, "-journal present beside the database")
        # an unknown header format
        odd = case("unknown_format")
        with open(odd, "r+b") as fh:
            fh.seek(18)
            fh.write(bytes([3, 3]))
        refused_state(odd, "unknown file format")
    print("  ok test_other_unsupported_source_states_are_refused_without_writing")


def test_a_same_process_writer_keeps_its_sidecars_and_the_reader_refuses():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        writer = sqlite3.connect(db, isolation_level=None)
        try:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE panel_runs SET note = 'held by a writer in this process'")
            assert {"panel.db-wal", "panel.db-shm"} <= set(source_files(db))
            refused_state(db, "-wal and -shm present beside the database")   # -shm untouched too
        finally:
            writer.close()
    print("  ok test_a_same_process_writer_keeps_its_sidecars_and_the_reader_refuses")


def test_a_writer_between_inspection_and_open_is_refused_without_sidecar_writes():
    """Codex's H1: the source is inspected as a quiescent rollback-journal file,
    then a writer switches it to WAL before SQLite opens it. The reader must
    neither create nor change a sidecar, and must refuse rather than return data."""
    scripts = {"crash": "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; "
                        "UPDATE panel_runs SET note = 'written between inspection and open';",
               "close": "PRAGMA journal_mode=WAL; "
                        "UPDATE panel_runs SET note = 'written between inspection and open';"}
    for finish, script in scripts.items():
        with tmpdir() as tmp:
            db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
            real, calls, left = tao._source_state, [], {}

            def paused(path):
                state = real(path)
                if not calls:                       # after the first inspection, before the open
                    calls.append(state)
                    sql_writer(db, finish, script)
                    left.update(source_files(db))
                return state
            tao._source_state = paused
            try:
                with recorded_opens() as opens:
                    try:
                        conn = tao.open_readonly(db)        # the check right after the open refuses
                    except tao.ReadOnlySourceStateError as e:
                        assert "changed since it was opened" in str(e), (finish, e)
                    else:
                        conn.close()
                        raise AssertionError(f"{finish}: open_readonly returned a connection to a "
                                             "source that changed between inspection and open")
            finally:
                tao._source_state = real
            after = source_files(db)
        assert calls == [tao.ROLLBACK_JOURNAL], (finish, calls)
        if finish == "crash":
            assert {"panel.db-wal", "panel.db-shm"} <= set(left), (finish, sorted(left))
        assert after == left, (finish, "sidecars created or changed by the reader:",
                               sorted(n for n in set(after) | set(left) if after.get(n) != left.get(n)))
        assert opens and all(u.endswith("?mode=ro&immutable=1") for u in opens), (finish, opens)
    print("  ok test_a_writer_between_inspection_and_open_is_refused_without_sidecar_writes")


def test_a_source_changed_during_a_public_read_is_refused():
    """The source changes while observe() is reading it: the final check refuses
    the result instead of returning it."""
    with tmpdir() as tmp:
        plain_dir = os.path.join(tmp, "plain")
        os.makedirs(plain_dir)
        rollback = collect(plain_dir, {"AAAA": tp.handbuilt_market()})["db_path"]
        checkpointed, _, _ = wal_panel(tmp, "close")
        for db in (rollback, checkpointed):
            conn = tao.open_readonly(db)
            real = tao._load

            def load_then_write(conn_, ticker, as_of, db=db):
                rows = real(conn_, ticker, as_of)
                sql_writer(db, "close", "UPDATE panel_runs SET note = 'written during a read';")
                return rows
            tao._load = load_then_write
            try:
                tao.observe(conn, "AAAA")
            except tao.ReadOnlySourceStateError as e:
                assert "changed since it was opened" in str(e), e
            else:
                raise AssertionError(f"returned a read of {os.path.basename(os.path.dirname(db))} "
                                     "although the source changed during it")
            finally:
                tao._load = real
                conn.close()
    print("  ok test_a_source_changed_during_a_public_read_is_refused")


def test_a_source_that_changes_after_it_was_opened_is_refused():
    with tmpdir() as tmp:
        # checkpointed WAL, read without locks: any change to the file refuses the next read
        db, expected, _ = wal_panel(tmp, "close")
        conn = tao.open_readonly(db)
        real_load, loads = tao._load, []

        def counted(*args):
            loads.append(args[1:])
            return real_load(*args)
        try:
            assert tao.observation_json(tao.observe(conn, "AAAA")) == expected
            sql_writer(db, "close", "UPDATE panel_runs SET note = 'written after the reader opened';")
            before = source_files(db)
            tao._load = counted
            try:
                tao.observe(conn, "AAAA")
            except tao.ReadOnlySourceStateError as e:
                assert "changed since it was opened" in str(e), e
            else:
                raise AssertionError("read a checkpointed WAL panel that changed under the reader")
            assert loads == [], loads                   # refused before the read began
            assert source_files(db) == before
        finally:
            tao._load = real_load
            conn.close()
        assert tao.observation_json(observe(db, "AAAA")) == expected          # reopened: fine
        # rollback journal opened, then switched to WAL with frames left in -wal/-shm
        plain_dir = os.path.join(tmp, "plain")
        os.makedirs(plain_dir)
        plain = collect(plain_dir, {"AAAA": tp.handbuilt_market()})["db_path"]
        conn = tao.open_readonly(plain)
        try:
            tao.observe(conn, "AAAA")
            sql_writer(plain, "crash", "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; "
                                       "UPDATE panel_runs SET note = 'switched to WAL';")
            before = source_files(plain)
            assert {"panel.db-wal", "panel.db-shm"} <= set(before)
            try:
                tao.list_snapshots(conn)
            except tao.ReadOnlySourceStateError as e:
                assert "changed since it was opened" in str(e), e
            else:
                raise AssertionError("read a panel whose journal mode changed under the reader")
            assert source_files(plain) == before
        finally:
            conn.close()
    print("  ok test_a_source_that_changes_after_it_was_opened_is_refused")


def test_only_connections_from_open_readonly_are_read():
    with tmpdir() as tmp:
        db = collect(tmp, {"AAAA": tp.handbuilt_market()})["db_path"]
        conn = sqlite3.connect(pathlib.Path(db).as_uri() + "?mode=ro", uri=True, isolation_level=None)
        conn.execute("PRAGMA query_only = ON")
        try:
            for call in (lambda: tao.observe(conn, "AAAA"), lambda: tao.list_snapshots(conn)):
                try:
                    call()
                except ValueError as e:
                    assert "open_readonly" in str(e), e
                    continue
                raise AssertionError("read through a connection open_readonly did not make")
        finally:
            conn.close()
    print("  ok test_only_connections_from_open_readonly_are_read")


def test_a_read_only_directory_changes_nothing():
    """POSIX only (Windows cannot make a directory unwritable without changing its
    ACL): with the directory unwritable, the supported state still reads and the
    unsupported ones are still refused."""
    if os.name != "posix" or os.geteuid() == 0:
        print("  ok test_a_read_only_directory_changes_nothing (skipped: needs a non-root POSIX user)")
        return
    with tmpdir() as tmp:
        cases = []
        for finish in ("close", "crash", "no_shm"):
            sub = os.path.join(tmp, finish)
            os.makedirs(sub)
            db, expected, _ = wal_panel(sub, "close" if finish == "close" else "crash")
            if finish == "no_shm":
                os.remove(db + "-shm")
            cases.append((finish, db, expected))
        dirs = [os.path.dirname(db) for _, db, _ in cases]
        for d in dirs:
            os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
        try:
            for finish, db, expected in cases:
                before = source_files(db)
                if finish == "crash":
                    refused_state(db, "-wal and -shm present beside the database")
                elif finish == "no_shm":
                    refused_state(db, "-wal present beside the database")
                else:
                    assert tao.observation_json(observe(db, "AAAA")) == expected, finish
                assert source_files(db) == before, finish
        finally:
            for d in dirs:
                os.chmod(d, stat.S_IRWXU)
    print("  ok test_a_read_only_directory_changes_nothing")


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def _guarded():
    """What no test may change: the repository's panel database, its manifest
    and the basis reference."""
    state = {"basis_file": tao.BASIS_FILE,
             "manifest": sorted(glob.glob(os.path.join(HERE, tp.ic.MANIFEST_DIR, "*")))}
    for path in (tdb.DB_PATH, tao.BASIS_FILE):
        state[path] = fingerprint(path)[:2] if os.path.exists(path) else None
    return state


def main():
    before = _guarded()
    print(f"targeted actor observations: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
    assert _guarded() == before, "a test changed the repository's panel database, manifest or basis reference"
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
