"""Tests for broker_book.py: payload validation, basis flags, the anchored
average-cost book and the rolling state (BROKER_LEARNING.md §2-§4.1).

Plain assert script, same collection loop as test_daily_picks.py. Tests that
read the gitignored inventory_raw/ cache skip cleanly where it is absent (CI);
everything else runs on synthetic payloads. inventory_raw/ is read, never
written.
"""

import gzip
import json
import os
import sys

import numpy as np
import pandas as pd

import broker_book as bb
import build_inventory_db as bidb

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "inventory_raw")
REAL = ("SINI", "BREN", "RAJA", "AADI", "ASRM", "BBCA", "TLKM")
FIELDS = ("nlot", "nval", "blot", "bval", "slot", "sval")
SKIPPED = []


def _skip(name, why):
    SKIPPED.append(f"{name}: {why}")
    print(f"  SKIP {name} ({why})")


def _real(ticker):
    path = os.path.join(RAW, f"{ticker}.json.gz")
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _dates(n, start="2025-01-01"):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def _payload(flows, n, close=1000.0, start="2025-01-01"):
    """flows: {broker: {"blot","bval","slot","sval": list}}; nlot/nval derived exactly."""
    dates = _dates(n, start)
    data = {"date": dates, "ohlc": [
        {"date": d, "open": close, "high": close * 1.1, "low": close * 0.9,
         "close": close, "volume": 1000.0} for d in dates]}
    for f in FIELDS:
        data[f] = {}
    for b, fl in flows.items():
        blot, slot = list(fl["blot"]), list(fl["slot"])
        bval, sval = list(fl["bval"]), list(fl["sval"])
        data["blot"][b], data["slot"][b] = [float(x) for x in blot], [float(x) for x in slot]
        data["bval"][b], data["sval"][b] = [float(x) for x in bval], [float(x) for x in sval]
        data["nlot"][b] = [float(x - y) for x, y in zip(blot, slot)]
        data["nval"][b] = [float(x - y) for x, y in zip(bval, sval)]
    return data


def _base():
    """Three sessions, two brokers, all valid; the malformed cases mutate a copy."""
    return _payload({
        "AA": {"blot": [10, 0, 5], "bval": [1e6, 0, 5e5], "slot": [0, 3, 0], "sval": [0, 3e5, 0]},
        "BB": {"blot": [0, 0, 0], "bval": [0, 0, 0], "slot": [10, 0, 5], "sval": [1e6, 0, 5e5]},
    }, 3)


def _copy(d):
    return json.loads(json.dumps(d))


def _frame(rows):
    """A broker frame from (date, broker, blot, bval, slot, sval) tuples, ticker T."""
    df = pd.DataFrame(rows, columns=["date", "broker", "blot", "bval", "slot", "sval"])
    df["ticker"] = "T"
    df["nlot"] = df["blot"] - df["slot"]
    df["nval"] = df["bval"] - df["sval"]
    for c in ("blot", "slot", "nlot"):
        df[c] = df[c].astype(np.int64)
    for c in ("bval", "sval", "nval"):
        df[c] = df[c].astype(np.float64)
    return df[bb.BROKER_COLS]


def _ohlc(dates, closes):
    return pd.DataFrame({"date": dates, "ticker": "T", "open": closes, "high": closes,
                         "low": closes, "close": closes, "volume": 1.0})[bb.OHLC_COLS]


# ── Payload -> frames ──────────────────────────────────────────────────────

def test_frames_match_strict_ticker_frame_on_real_cache():
    present = [t for t in REAL if _real(t) is not None]
    if len(present) < 5:
        return _skip("strict equivalence", "inventory_raw/ cache not present")
    for t in present:
        data = _real(t)
        mine, ohlc = bb.frames_from_payload(data, t)
        ref = bidb.strict_ticker_frame(data, t)
        key = ["date", "broker"]
        a = mine.sort_values(key).reset_index(drop=True)
        b = ref.sort_values(key).reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b, check_dtype=True)
        assert list(mine.columns) == bb.BROKER_COLS
        assert all(mine[c].dtype == np.int64 for c in ("nlot", "blot", "slot"))
        assert all(mine[c].dtype == np.float64 for c in ("nval", "bval", "sval"))
        assert ohlc["date"].tolist() == data["date"], t
        assert list(ohlc.columns) == bb.OHLC_COLS
        assert all(ohlc[c].dtype == np.float64 for c in ("open", "high", "low", "close", "volume"))
    print(f"  ok frames == strict_ticker_frame on {len(present)} real files ({', '.join(present)})")


def test_every_malformed_payload_raises():
    def ohlc_date(d, v):
        d["ohlc"][1]["date"] = v

    cases = {
        # name: (mutator, strict_ticker_frame refuses it too)
        "non-integral lot": (lambda d: d["blot"]["AA"].__setitem__(1, 0.5), True),
        "nan lot": (lambda d: d["blot"]["AA"].__setitem__(1, float("nan")), True),
        "inf lot": (lambda d: d["nlot"]["AA"].__setitem__(1, float("inf")), True),
        "bool lot": (lambda d: d["slot"]["AA"].__setitem__(0, False), True),
        "str lot": (lambda d: d["blot"]["AA"].__setitem__(0, "10"), True),
        "null lot": (lambda d: d["nlot"]["BB"].__setitem__(1, None), True),
        "bool value": (lambda d: d["bval"]["AA"].__setitem__(1, True), True),
        "str value": (lambda d: d["sval"]["BB"].__setitem__(0, "1e6"), True),
        "inf value": (lambda d: d["nval"]["AA"].__setitem__(1, float("-inf")), True),
        "short series": (lambda d: d["sval"]["AA"].pop(), True),
        "long series": (lambda d: d["bval"]["BB"].append(0.0), True),
        "series not a list": (lambda d: d["blot"].__setitem__("AA", {"0": 10}), True),
        "field not a mapping": (lambda d: d.__setitem__("sval", [0, 0, 0]), True),
        "partially present broker": (lambda d: d["sval"].pop("BB"), True),
        "field absent": (lambda d: d.pop("nval"), True),
        "nlot != blot - slot": (lambda d: d["nlot"]["AA"].__setitem__(0, 9.0), True),
        "nval drift > 0.5": (lambda d: d["nval"]["AA"].__setitem__(0, 1e6 + 0.51), True),
        "empty date axis": (lambda d: d.__setitem__("date", []), True),
        "non-string date": (lambda d: d["date"].__setitem__(1, 20250102), True),
        "not a calendar date": (lambda d: d["date"].__setitem__(1, "2025-02-30"), True),
        "duplicate date": (lambda d: d["date"].__setitem__(1, d["date"][0]), True),
        "descending dates": (lambda d: d["date"].reverse(), True),
        "bool price": (lambda d: d["ohlc"][0].__setitem__("close", True), True),
        "str price": (lambda d: d["ohlc"][2].__setitem__("low", "990"), True),
        "inf price": (lambda d: d["ohlc"][1].__setitem__("high", float("inf")), True),
        # Refused here only: the ohlc axis and date spelling are this module's contract.
        "compact date": (lambda d: [d["date"].__setitem__(0, "20250101"),
                                    ohlc_date(d, d["ohlc"][1]["date"])], False),
        "ohlc bar missing": (lambda d: d["ohlc"].pop(1), False),
        "ohlc bar extra": (lambda d: d["ohlc"].append(dict(d["ohlc"][2], date="2025-01-06")), False),
        "ohlc bars reordered": (lambda d: d["ohlc"].reverse(), False),
        "ohlc date differs": (lambda d: ohlc_date(d, "2025-01-05"), False),
        "ohlc not a list": (lambda d: d.__setitem__("ohlc", {"date": "x"}), False),
        "payload not a mapping": (None, False),
        "lot beyond 2**53": (lambda d: [d["blot"]["AA"].__setitem__(0, 2 ** 60),
                                        d["nlot"]["AA"].__setitem__(0, 2 ** 60)], False),
        # Review finding: consistent negatives passed both validators and the
        # book returned a silent NaN. Gross fields are never below zero.
        "neg blot+slot consistent": (lambda d: [d["blot"]["AA"].__setitem__(1, -5),
                                                d["slot"]["AA"].__setitem__(1, -2),
                                                d["nlot"]["AA"].__setitem__(1, -3)], False),
        "neg bval only": (lambda d: [d["bval"]["AA"].__setitem__(1, -5e4),
                                     d["nval"]["AA"].__setitem__(1, -5e4 - 3e5)], False),
        "neg sval": (lambda d: [d["sval"]["BB"].__setitem__(1, -1e5),
                                d["nval"]["BB"].__setitem__(1, 1e5)], False),
    }
    for name, (mutate, strict_too) in cases.items():
        d = _copy(_base())
        if mutate is None:
            d = [d]
        else:
            mutate(d)
        try:
            bb.frames_from_payload(d, "T")
        except bb.PayloadError:
            pass
        else:
            raise AssertionError(f"accepted malformed payload: {name}")
        if strict_too:
            try:
                bidb.strict_ticker_frame(d, "T")
            except bidb.StrictSourceError:
                pass
            else:
                raise AssertionError(f"strict_ticker_frame accepted {name}; case is mislabelled")
    assert issubclass(bb.PayloadError, ValueError)
    print(f"  ok PayloadError on all {len(cases)} malformed cases "
          f"({sum(s for _, s in cases.values())} also refused by strict_ticker_frame)")


def test_valid_edges_are_accepted_as_strict_mode_does():
    d = _base()
    d["nval"]["AA"][0] = 1e6 + 0.5                         # drift exactly at tolerance
    d["blot"]["AA"][2], d["nlot"]["AA"][2] = 5, 5           # python int lots
    d["ohlc"][1]["volume"] = None                           # absent price stays NaN
    for f in FIELDS:                                        # an all-zero broker is dropped
        d[f]["ZZ"] = [0.0, 0.0, 0.0]
    mine, ohlc = bb.frames_from_payload(d, "T")
    ref = bidb.strict_ticker_frame(d, "T")
    pd.testing.assert_frame_equal(mine.reset_index(drop=True), ref.reset_index(drop=True))
    assert "ZZ" not in set(mine["broker"])
    assert np.isnan(ohlc.loc[1, "volume"]) and ohlc.loc[0, "volume"] == 1000.0
    # rows where a broker did nothing are dropped: BB traded on day 0 and 2 only
    assert mine[mine.broker == "BB"]["date"].tolist() == [d["date"][0], d["date"][2]]

    empty = _payload({}, 4)
    brokers, ohlc = bb.frames_from_payload(empty, "T")
    assert brokers.empty and list(brokers.columns) == bb.BROKER_COLS and len(ohlc) == 4
    assert bidb.strict_ticker_frame(empty, "T") is None
    zeros = _payload({"AA": {k: [0, 0] for k in ("blot", "bval", "slot", "sval")}}, 2)
    assert bb.frames_from_payload(zeros, "T")[0].empty
    print("  ok tolerance edge, int lots, absent price, all-zero and broker-less payloads")


# ── Basis flags and anchor ─────────────────────────────────────────────────

def test_basis_flags_regimes_and_vwap_containment():
    dates = _dates(6)
    ohlc = _ohlc(dates, [100.0] * 6)
    ohlc.loc[:, "low"], ohlc.loc[:, "high"] = 95.0, 105.0
    ohlc.loc[4, "low"] = np.nan                             # no valid range: not checked
    rows = [(dates[0], "AA", 10, 10 * 100 * 100.0, 0, 0.0),        # VWAP 100: fine
            (dates[1], "AA", 10, 10 * 100 * 94.05, 0, 0.0),        # 95*0.99 exactly: fine
            (dates[2], "AA", 10, 10 * 100 * 94.0, 0, 0.0),         # below the band: flag
            (dates[3], "AA", 0, 0.0, 10, 10 * 100 * 500.0),        # no buy lots: not checked
            (dates[4], "AA", 10, 10 * 100 * 500.0, 0, 0.0),        # low NaN: not checked
            (dates[5], "AA", 10, 10 * 100 * 106.05, 0, 0.0)]       # 105*1.01 exactly: fine
    flags = bb.basis_flags(_frame(rows), ohlc, [])
    assert flags.index.tolist() == dates
    assert flags.tolist() == [False, False, True, False, False, False], flags.tolist()
    flags = bb.basis_flags(_frame(rows), ohlc, [(dates[4], dates[5])])
    assert flags.tolist() == [False, False, True, False, True, True]

    assert bb.anchor_after_flags(dates, [False] * 6) == dates[0]
    assert bb.anchor_after_flags(dates, [False, False, True, False, False, False]) == dates[3]
    assert bb.anchor_after_flags(dates, [False] * 5 + [True]) is None
    print("  ok regime days (inclusive) and VWAP outside [low*0.99, high*1.01] are flagged")


def test_odd_lot_rupiah_does_not_fake_a_basis_break():
    dates = _dates(2)
    ohlc = _ohlc(dates, [100.0, 100.0])
    ohlc.loc[:, "low"], ohlc.loc[:, "high"] = 99.0, 101.0
    # Day 0: 1 lot at the high plus Rp 300 of odd-lot fills with no lot. Counted,
    # the rupiah would give a VWAP of (10,100 + 300) / 100 = 104 > 101*1.01.
    rows = [(dates[0], "AA", 1, 1 * 100 * 101.0, 0, 0.0),
            (dates[0], "BB", 0, 300.0, 0, 0.0),                    # odd lot only
            (dates[1], "AA", 1, 1 * 100 * 120.0, 0, 0.0)]          # a real break
    flags = bb.basis_flags(_frame(rows), ohlc, [])
    assert flags.tolist() == [False, True], flags.tolist()
    print("  ok odd-lot rupiah is left out of the VWAP check; a real break still flags")


def test_load_basis_regimes():
    assert bb.load_basis_regimes(os.path.join(HERE, "no_such_basis_file.json")) == {}
    regs = bb.load_basis_regimes()
    with open(bb.BASIS_FILE, encoding="utf-8") as fh:
        n = len(json.load(fh)["regimes"])
    assert sum(len(v) for v in regs.values()) == n
    for spans in regs.values():
        assert all(isinstance(a, str) and a <= b for a, b in spans)
    print(f"  ok {n} regimes loaded, every classification kept; missing file -> {{}}")


# ── Anchored average-cost book ─────────────────────────────────────────────

def _hand_book():
    dates = _dates(8)
    rows = [(dates[0], "AA", 10, 100000.0, 0, 0.0),      # buy 10 @100      P=10  C=100
            (dates[1], "AA", 10, 120000.0, 0, 0.0),      # buy 10 @120      P=20  C=110
            (dates[2], "AA", 0, 0.0, 5, 65000.0),        # sell 5 @130      R=+10,000
            (dates[3], "AA", 4, 40000.0, 6, 63000.0),    # 4 round trip +2,000; sell 2 @105 -1,000
            (dates[4], "AA", 0, 0.0, 20, 180000.0),      # close 13 @90 -26,000; flip: P=-7 C=90
            (dates[5], "AA", 7, 56000.0, 0, 0.0),        # cover 7 @80 +7,000; flat, C=NaN
            (dates[6], "AA", 0, 5000.0, 0, 0.0),         # odd-lot value, no lots: -5,000 cash
            (dates[7], "AA", 3, 60000.0, 0, 0.0)]        # buy 3 @200       P=3  C=200
    closes = [100.0, 120.0, 130.0, 105.0, 90.0, 80.0, 80.0, 210.0]
    return dates, _frame(rows), _ohlc(dates, closes)


def test_book_hand_computed():
    dates, frame, ohlc = _hand_book()
    row = bb.average_cost_book(frame, ohlc, dates[0]).iloc[0]
    assert row["position_lots"] == 3 and row["avg_cost"] == 200.0
    assert row["realized_rp"] == -13000.0, row["realized_rp"]
    assert row["unrealized_rp"] == 3000.0 and row["total_rp"] == -10000.0
    assert abs(row["pct_vs_cost"] - 0.05) < 1e-12
    assert row["gross_buy_lots"] == 34 and row["gross_sell_lots"] == 31
    assert row["turnover_rp"] == 689000.0
    assert row["net1d"] == 3 and row["net5d"] == -12
    assert abs(row["bavg20"] - 376000.0 / 3400) < 1e-9     # odd-lot Rp 5,000 left out
    assert abs(row["savg20"] - 308000.0 / 3100) < 1e-9
    assert row["first_date"] == dates[0]

    # Mid-way: the day-4 sign flip leaves a net seller since the anchor, cost reset to 90.
    upto = frame[frame.date <= dates[4]]
    mid = bb.average_cost_book(upto, ohlc.iloc[:5], dates[0]).iloc[0]
    assert mid["position_lots"] == -7 and mid["avg_cost"] == 90.0
    assert mid["realized_rp"] == -15000.0 and mid["unrealized_rp"] == 0.0
    flat = bb.average_cost_book(frame[frame.date <= dates[5]], ohlc.iloc[:6], dates[0]).iloc[0]
    assert flat["position_lots"] == 0 and np.isnan(flat["avg_cost"]) and np.isnan(flat["pct_vs_cost"])
    assert flat["unrealized_rp"] == 0.0 and flat["realized_rp"] == -8000.0

    # The anchor drops everything before it.
    late = bb.average_cost_book(frame, ohlc, dates[2]).iloc[0]
    assert late["first_date"] == dates[2] and late["gross_buy_lots"] == 14
    assert late["position_lots"] == -5 - 2 - 20 + 7 + 3
    print("  ok hand-computed path: round trip, reduce, sign flip, flat -> NaN, odd-lot cash")


def _check_invariant(frame, ohlc, anchor):
    book = bb.average_cost_book(frame, ohlc, anchor)
    since = frame[frame.date >= anchor]
    close_last = float(ohlc["close"].iloc[-1])
    worst = 0.0
    for _, r in book.iterrows():
        g = since[since.broker == r["broker"]]
        assert r["position_lots"] == int(g["nlot"].sum())
        assert np.isnan(r["avg_cost"]) == (r["position_lots"] == 0)
        mtm = g["nlot"].sum() * 100 * close_last - g["nval"].sum()
        # identity to 1 Rp per 1e9 of turnover, plus the 0.5 Rp/row the source may drift
        tol = max(r["turnover_rp"] / 1e9, 1e-6) + 0.5 * float(
            (g["nval"] - (g["bval"] - g["sval"])).abs().gt(0).sum())
        err = abs(r["total_rp"] - mtm)
        assert err <= tol, (r["broker"], r["total_rp"], mtm, err, tol)
        worst = max(worst, err / max(r["turnover_rp"], 1.0))
    return book, worst


def test_book_invariant_on_random_sequences():
    rng = np.random.default_rng(20260925)
    flips = zero_crossings = odd = 0
    for trial in range(400):
        n = int(rng.integers(3, 50))
        dates = _dates(n)
        closes = np.round(rng.uniform(50, 5000, n), 0)
        rows = []
        for b in ("AA", "BB", "CC")[: int(rng.integers(1, 4))]:
            path = 0
            for i in range(n):
                if rng.random() < 0.3:
                    continue
                B = int(rng.integers(0, 60)) * int(rng.random() < 0.7)
                S = int(rng.integers(0, 60)) * int(rng.random() < 0.7)
                pb = closes[i] * rng.uniform(0.95, 1.05)
                ps = closes[i] * rng.uniform(0.95, 1.05)
                bval, sval = B * 100 * pb, S * 100 * ps
                if B == 0 and rng.random() < 0.1:
                    bval, odd = float(rng.integers(1, 99)) * closes[i], odd + 1
                if B == S == 0 and bval == 0:
                    continue
                new = path + B - S
                flips += (path > 0 > new) or (path < 0 < new)
                zero_crossings += path != 0 and new == 0
                path = new
                rows.append((dates[i], b, B, bval, S, sval))
        if not rows:
            continue
        frame, ohlc = _frame(rows), _ohlc(dates, closes)
        anchor = dates[int(rng.integers(0, n))]
        if (frame.date >= anchor).any():
            _check_invariant(frame, ohlc, anchor)
    assert flips > 100 and zero_crossings > 20 and odd > 20, (flips, zero_crossings, odd)
    print(f"  ok total_rp == sum(nlot)*100*close - sum(nval) on 400 random books "
          f"({flips} sign flips, {zero_crossings} returns to flat, {odd} odd-lot rows)")


def test_book_invariant_on_real_cache():
    present = [t for t in REAL if _real(t) is not None]
    if not present:
        return _skip("real-cache invariant", "inventory_raw/ cache not present")
    regs = bb.load_basis_regimes()
    checked = 0
    for t in present:
        b = bb.ticker_bundle(_real(t), t, regs)
        if b["anchor"] is None:
            continue
        book, worst = _check_invariant(b["brokers"], b["ohlc"], b["anchor"])
        assert list(book.columns) == bb.BOOK_COLS
        pos = book["position_lots"].tolist()
        assert pos == sorted(pos, reverse=True)
        checked += len(book)
    print(f"  ok invariant holds for {checked} real broker books ({', '.join(present)})")


def test_cumulative_curves():
    dates = _dates(5)
    rows = [(dates[0], "AA", 10, 1e5, 0, 0.0), (dates[3], "AA", 5, 5e4, 0, 0.0),
            (dates[1], "BB", 0, 0.0, 7, 7e4), (dates[2], "CC", 3, 3e4, 0, 0.0),
            (dates[2], "DD", 3, 3e4, 0, 0.0), (dates[4], "EE", 2, 2e4, 2, 2e4)]
    ohlc = _ohlc(dates, [100.0, 101.0, np.nan, 103.0, 104.0])
    cur = bb.cumulative_curves(_frame(rows), ohlc, dates[1], top_n=2)
    assert cur["dates"] == dates[1:] and cur["close"] == [101.0, None, 103.0, 104.0]
    got = [(s["broker"], s["side"], s["cum_lots"]) for s in cur["series"]]
    assert got == [("AA", "buy", [0, 0, 5, 5]),              # anchor drops day-0 buy
                   ("CC", "buy", [0, 3, 3, 3]),              # tie with DD: code order
                   ("BB", "sell", [-7, -7, -7, -7])], got
    assert all(isinstance(v, int) for s in cur["series"] for v in s["cum_lots"])
    json.dumps(cur)
    print("  ok curves carry forward, start at the anchor, rank buyers/sellers, JSON-able")


# ── Rolling state ──────────────────────────────────────────────────────────

def _random_payload(n, rng, brokers=("AA", "BB", "CC", "DD"), start="2025-01-01"):
    closes = np.round(1000 * np.cumprod(1 + rng.normal(0, 0.02, n)), 0)
    flows = {}
    for b in brokers:
        blot = rng.integers(0, 50, n) * (rng.random(n) < 0.6)
        slot = rng.integers(0, 50, n) * (rng.random(n) < 0.6)
        flows[b] = {"blot": blot.tolist(), "slot": slot.tolist(),
                    "bval": (blot * 100.0 * closes).tolist(), "sval": (slot * 100.0 * closes).tolist()}
    data = _payload(flows, n, start=start)
    for i, row in enumerate(data["ohlc"]):
        row.update(open=closes[i], close=closes[i], low=closes[i] * 0.97, high=closes[i] * 1.03)
    return data


def test_rolling_state_matches_naive_windows():
    rng = np.random.default_rng(7)
    data = _random_payload(130, rng)
    brokers, ohlc = bb.frames_from_payload(data, "T")
    flags = np.zeros(130, dtype=bool)
    flags[[10, 95]] = True
    st = bb.rolling_state(brokers, ohlc, flags)
    assert st.brokers == ["AA", "BB", "CC", "DD"] and st.dates == data["date"]
    daily = {f: np.array([data[f][b] for b in st.brokers]) for f in FIELDS}
    close = ohlc["close"].to_numpy()
    for t in range(130):
        for w in (5, 60):
            for key, f in (("NL", "nlot"), ("NV", "nval"), ("BL", "blot"), ("SL", "slot")):
                got = getattr(st, f"{key}{w}")[:, t]
                if t + 1 < w:
                    assert np.isnan(got).all()
                else:
                    want = daily[f][:, t + 1 - w: t + 1].sum(axis=1)
                    assert np.allclose(got, want, rtol=1e-12, atol=0), (key, w, t)
            lo, hi = getattr(st, f"low{w}")[t], getattr(st, f"high{w}")[t]
            if t + 1 < w:
                assert np.isnan(lo) and np.isnan(hi)
            else:
                assert lo == ohlc["low"][t + 1 - w: t + 1].min() and hi == ohlc["high"][t + 1 - w: t + 1].max()
        if t >= 19:
            assert st.adv20[t] == daily["blot"][:, t - 19: t + 1].sum() / 20
            assert abs(st.val20[t] - daily["bval"][:, t - 19: t + 1].sum() / 20) <= 1e-6 * st.val20[t]
        else:
            assert np.isnan(st.adv20[t]) and np.isnan(st.val20[t])
        if t >= 59:
            want = (daily["nlot"][:, t - 59: t + 1] > 0).sum(axis=1)
            assert (st.BUYDAYS60[:, t] == want).all() and st.close_lag59[t] == close[t - 59]
        else:
            assert np.isnan(st.BUYDAYS60[:, t]).all() and np.isnan(st.close_lag59[t])
        assert st.basis_ok80[t] == (not flags[max(0, t - 79): t + 1].any())
        rets = pd.Series(close).pct_change().to_numpy()[max(1, t - 19): t + 1]
        rets = rets[np.isfinite(rets)]
        if len(rets) >= 15:
            assert abs(st.rv20[t] - np.std(rets, ddof=1)) < 1e-12
        else:
            assert np.isnan(st.rv20[t])
    assert st.n_sessions.tolist() == list(range(1, 131))
    print("  ok cumsum windows == naive slices for NL/NV/BL/SL 5/60, ADV/VAL20, "
          "low/high, BUYDAYS60, close_lag59, basis_ok80, rv20")


def _same(a, b):
    return np.array_equal(np.asarray(a), np.asarray(b), equal_nan=True)


def test_rolling_state_prefix_is_identical_to_truncated_fetch():
    rng = np.random.default_rng(11)
    full = _random_payload(150, rng, brokers=("AA", "BB", "CC", "ZZ"))
    for f in FIELDS:                         # ZZ only trades after session 120
        full[f]["ZZ"][:120] = [0.0] * 120
    cases = [("synthetic", full, "T")]
    sini = _real("SINI")
    if sini is not None:
        cases.append(("SINI", sini, "SINI"))
    for name, data, t in cases:
        n = len(data["date"])
        b_full, o_full = bb.frames_from_payload(data, t)
        s_full = bb.rolling_state(b_full, o_full, bb.basis_flags(b_full, o_full, []))
        for cut in (90, 119, n - 1):
            short = _copy(data)
            short["date"] = short["date"][:cut]
            short["ohlc"] = short["ohlc"][:cut]
            for f in FIELDS:
                short[f] = {b: s[:cut] for b, s in short[f].items()}
            b_s, o_s = bb.frames_from_payload(short, t)
            s_s = bb.rolling_state(b_s, o_s, bb.basis_flags(b_s, o_s, []))
            rows = [s_full.brokers.index(b) for b in s_s.brokers]
            for field in ("open", "high", "low", "close", "adv20", "val20", "low5", "high5",
                          "low60", "high60", "n_sessions", "basis_ok80", "rv20", "close_lag59"):
                assert _same(getattr(s_full, field)[:cut], getattr(s_s, field)), (name, cut, field)
            for field in ("NL5", "NV5", "BL5", "SL5", "NL60", "NV60", "BL60", "SL60", "BUYDAYS60"):
                assert _same(getattr(s_full, field)[rows, :cut], getattr(s_s, field)), (name, cut, field)
    print(f"  ok every state array for T <= cut is bit-identical to the truncated fetch "
          f"({', '.join(c[0] for c in cases)})")


def test_ticker_bundle():
    data = _random_payload(100, np.random.default_rng(3))
    b = bb.ticker_bundle(data, "T", {})
    assert b["basis_ok"] and b["anchor"] == data["date"][0]
    assert isinstance(b["book"], pd.DataFrame) and b["curves"]["dates"] == data["date"]
    assert b["state"].NL60.shape == (4, 100)
    last = data["date"][-1]
    b = bb.ticker_bundle(data, "T", {"T": [(last, last)]})
    assert not b["basis_ok"] and b["anchor"] is None and b["book"] is None and b["curves"] is None
    assert not b["state"].basis_ok80[-1] and b["state"].basis_ok80[-2]
    b = bb.ticker_bundle(_payload({}, 90), "T", {})
    st = b["state"]
    assert st.brokers == [] and st.NL60.shape == (0, 90) and st.BUYDAYS60.shape == (0, 90)
    assert (st.adv20[19:] == 0).all() and b["book"].empty
    print("  ok bundle: book/curves when anchored, None when the last session is flagged, "
          "0-broker matrices")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    print(f"broker book: {len(ALL)} tests\n")
    for fn in ALL:
        print(fn.__name__)
        fn()
    print(f"\nAll {len(ALL)} tests passed."
          + (f" ({len(SKIPPED)} skipped: {'; '.join(SKIPPED)})" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
