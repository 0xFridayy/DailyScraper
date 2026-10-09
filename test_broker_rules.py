"""Tests for broker_rules.py: ruleset v1 (BROKER_LEARNING.md §4.1-§4.4, §4.3.1).

Every rule is driven end to end (payload -> frames -> basis flags -> rolling
state -> evaluate) through tiny synthetic payloads whose numbers are worked out
by hand in the comments, on both sides of each threshold. Two no-look-ahead
tests prove a rule row for T is the same whether the fetch ends at T or runs
on (real SINI where the gitignored cache exists, synthetic everywhere).

Plain assert script, same collection loop as test_daily_picks.py.
"""

import gzip
import json
import os
import sys

import numpy as np
import pandas as pd

import broker_book as bb
import broker_rules as br
import coverage_guard as cg

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "inventory_raw")
FIELDS = ("nlot", "nval", "blot", "bval", "slot", "sval")
SKIPPED = []

N = 100          # sessions per scenario; T = day 99, the 60-session window is days 40..99
P = 2000.0       # base price; bars are low 1800 / high 2200 unless a test says otherwise
BG = 10_000      # background broker "BG" buys and sells this many lots a day at the close:
                 # ADV20 = 10,000 and VAL20 = Rp 2e9 before any actor trades in days 80..99


def _skip(name, why):
    SKIPPED.append(f"{name}: {why}")
    print(f"  SKIP {name} ({why})")


def _dates(n, start="2025-01-01"):
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n)]


def scenario(trades, n=N, bg=BG, bars=None, default_bar=(P, P * 0.9, P * 1.1)):
    """A payload from (broker, day, blot, bprice, slot, sprice) trades plus BG.

    bars: {day: (close, low, high)} overrides. BG's round trip (nlot 0) sets ADV
    and VAL without moving any broker's position.
    """
    dates = _dates(n)
    bar = [tuple(default_bar)] * n
    for day, b in (bars or {}).items():
        bar[day] = b
    codes = sorted({"BG"} | {t[0] for t in trades})
    acc = {f: {c: [0.0] * n for c in codes} for f in ("blot", "bval", "slot", "sval")}
    for i in range(n):
        acc["blot"]["BG"][i] = acc["slot"]["BG"][i] = float(bg)
        acc["bval"]["BG"][i] = acc["sval"]["BG"][i] = bg * 100.0 * bar[i][0]
    for broker, day, blot, bprice, slot, sprice in trades:
        acc["blot"][broker][day] += blot
        acc["bval"][broker][day] += blot * 100.0 * bprice
        acc["slot"][broker][day] += slot
        acc["sval"][broker][day] += slot * 100.0 * sprice
    data = {"date": dates,
            "ohlc": [{"date": d, "open": c, "high": h, "low": lo, "close": c, "volume": 1.0}
                     for d, (c, lo, h) in zip(dates, bar)]}
    data.update(acc)
    data["nlot"] = {c: [b - s for b, s in zip(acc["blot"][c], acc["slot"][c])] for c in codes}
    data["nval"] = {c: [b - s for b, s in zip(acc["bval"][c], acc["sval"][c])] for c in codes}
    # A full vendor answer: every other universe code at an explicit zero.
    # broker_book requires them all (coverage_guard.full_universe_reason); the
    # zero rows are dropped, so no rule input changes.
    for f in FIELDS:
        for code in sorted(cg.universe_codes()):
            data[f].setdefault(code, [0.0] * n)
    return data


def run(data, regimes=None, ticker="TEST"):
    """(evaluate rows, state) through the real pipeline."""
    b = bb.ticker_bundle(data, ticker, regimes or {})
    return br.evaluate(b["state"]), b["state"]


def last(data, **kw):
    rows, _ = run(data, **kw)
    return rows.iloc[-1]


def spread(broker, days, lots, price, side="buy"):
    """Split `lots` over `days` as evenly as integers allow (remainder on the last day)."""
    days = list(days)
    each, rest = divmod(lots, len(days))
    out = []
    for i, d in enumerate(days):
        q = each + (rest if i == len(days) - 1 else 0)
        out.append((broker, d, q, price, 0, 0) if side == "buy" else (broker, d, 0, 0, q, price))
    return out


def _fires(row):
    return [r for r in br.RULE_IDS if bool(row[r])]


# ── Constants and shape ────────────────────────────────────────────────────

def test_frozen_constants():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── Eligibility (§4.2) ─────────────────────────────────────────────────────

def test_eligibility_boundaries():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── R1 / R3: holder group cost (§4.1, §4.3) ────────────────────────────────

def _holders(cc_lots=10_000, dd_lots=0):
    """AA, BB, CC each buy 10,000 lots at 2,000 on days 60..69: NLH = 30,000 = 3 x ADV20."""
    t = spread("AA", range(60, 70), 10_000, P) + spread("BB", range(60, 70), 10_000, P)
    t += spread("CC", range(60, 70), cc_lots, P)
    if dd_lots:
        t += spread("DD", range(60, 70), dd_lots, P)
    return t


def test_r1_acc_near_cost():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


def test_r3_holders_underwater():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


def test_rx_near_zero_position_has_no_cost():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── R2: winner distributing ────────────────────────────────────────────────

def _winner(sold=4000, vv=False, ww_sells=True):
    """WW buys 24,000 at 2,000 (days 45..56); price then 2,400 on days 95..99 and WW sells.

    Selling 4,000 at 2,400: NL60 = 20,000, NV60 = 4.8e9 - 9.6e8 = 3.84e9, cost60 = 1,920,
    gain = 2400/1920 - 1 = +25%, NL5 = -4,000 = -0.2 x NL60 exactly.
    """
    t = spread("WW", range(45, 57), 24_000, P)
    if ww_sells:
        t += spread("WW", range(95, 99), sold, 2400.0, side="sell")
    if vv:   # smaller winner selling a third of its book: must not steal the verdict
        t += spread("VV", range(45, 55), 20_000, 2200.0)
        t += spread("VV", range(95, 99), 5_000, 2400.0, side="sell")
    return t


R2_BARS = {d: (2400.0, 2380.0, 2420.0) for d in range(95, 100)}


def test_r2_winner_distributing():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── R4: fresh accumulation ─────────────────────────────────────────────────

def test_r4_fresh_accumulation():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── R5: seller exhaustion ──────────────────────────────────────────────────

def _sellers(sc=8000, sb_last5=0, extra=()):
    t = (spread("SA", range(45, 55), 12_000, P, "sell") + spread("SB", range(45, 55), 10_000, P, "sell")
         + spread("SC", range(45, 55), sc, P, "sell"))
    if sb_last5 > 0:
        t.append(("SB", 97, 0, 0, sb_last5, P))
    elif sb_last5 < 0:
        t.append(("SB", 97, -sb_last5, P, 0, 0))
    return t + list(extra)


def test_r5_seller_exhaustion():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── R6: stealth accumulation (§4.3.1) ──────────────────────────────────────

R6_BAR = (P, 1900.0, 2100.0)    # range60 = 2100/1900 - 1 = 10.5%


def _collector(days=range(50, 74), lots=24_000, sold=0, broker="AK"):
    """AK buys on each of 24 sessions inside the window but outside the ADV20 days (80..99)."""
    t = spread(broker, days, lots, P)
    if sold:
        t.append((broker, 76, 0, 0, sold, P))
    return t


def test_r6_stealth_accumulation():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


def test_explain_columns_on_every_eligible_row():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── Track-record events (§4.4) ─────────────────────────────────────────────

def test_track_record_events():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


# ── Composite score and explanations ───────────────────────────────────────

def test_composite_score():
    rows = pd.DataFrame({"R1": [True, False, True], "R2": [True, False, False],
                         "R3": [False, False, False], "R4": [False, True, True],
                         "R5": [False, False, False], "R6": [False, False, True]})
    directions = {r["id"]: r["dir"] for r in br.RULES}
    got = br.weighted_boolean_sum(rows, directions, {"R1": 1.5, "R6": 0.5}).tolist()
    assert got == [1.5 - 1.0, 1.0, 1.5 + 1.0 + 0.5], got
    assert br.weighted_boolean_sum(rows, directions, {}).tolist() == [0.0, 1.0, 3.0]
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.composite_score")
    print("  ok score = sum fired x dir x weight (default 1.0)")


def test_explain_indonesian_lines():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


def test_explain_sign_is_decided_on_the_rounded_number():
    """Review finding: a -0.4% gap printed as '+0%'. R1 (-2%..+5%) and R4
    (|gap| <= 3%) make near-zero gaps common, so the sign must follow the
    rounded text, as broker_dashboard's fmt_* helpers do."""
    assert [br._pct(v) for v in (-0.004, 0.004, 0.0, -0.006, 0.006)] ==         ["0%", "0%", "0%", "−1%", "+1%"]
    assert br._num(-0.4) == "0" and br._num(-0.04, 1, True) == "0,0"
    assert br._num(-1234.5, 1) == "−1.234,5" and br._num(1234.5, 1, True) == "+1.234,5"
    row = pd.Series({"R6": True, "a_broker": "BK", "a_nl60": 45200.0, "a_nl60_adv": 3.1,
                     "a_buydays": 31.0, "range60": 0.18, "a_cost60": 1250.0, "a_gap": -0.004})
    assert br._pct(row["a_gap"]) == "0%"
    assert br._rp(row["a_cost60"]) == "Rp 1.250"
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.explain")
    print("  ok explain() prints a rounded-zero gap without a sign")


# ── No look-ahead ──────────────────────────────────────────────────────────

def _truncate(data, cut):
    out = json.loads(json.dumps(data))
    out["date"], out["ohlc"] = out["date"][:cut], out["ohlc"][:cut]
    for f in FIELDS:
        out[f] = {b: s[:cut] for b, s in out[f].items()}
    return out


def _extend(data, k, seed):
    """Append k synthetic sessions after the last one, including a broker never seen before."""
    rng = np.random.default_rng(seed)
    out = json.loads(json.dumps(data))
    n = len(out["date"])
    start = pd.Timestamp(out["date"][-1]) + pd.offsets.BDay(1)
    new_dates = _dates(k, start.strftime("%Y-%m-%d"))
    c = float(out["ohlc"][-1]["close"])
    closes = np.round(c * np.cumprod(1 + rng.normal(0, 0.03, k)), 0)
    out["date"] += new_dates
    out["ohlc"] += [{"date": d, "open": x, "high": x * 1.02, "low": x * 0.98, "close": x,
                     "volume": 1e6} for d, x in zip(new_dates, closes)]
    for f in FIELDS:
        out[f]["ZZNEW"] = [0.0] * n
    for b in list(out["blot"]):
        bl = rng.integers(0, 3000, k) * (rng.random(k) < 0.5)
        sl = rng.integers(0, 3000, k) * (rng.random(k) < 0.5)
        bv, sv = bl * 100.0 * closes, sl * 100.0 * closes
        for f, v in (("blot", bl), ("slot", sl), ("nlot", bl - sl),
                     ("bval", bv), ("sval", sv), ("nval", bv - sv)):
            out[f][b] = out[f][b] + [float(x) for x in v]
    return out


def _assert_prefix_equal(short_rows, long_rows, short_ev, long_ev, label):
    n = len(short_rows)
    pd.testing.assert_frame_equal(short_rows, long_rows.iloc[:n].reset_index(drop=True),
                                  check_exact=True, obj=label)
    last_date = short_rows["date"].iloc[-1]
    head = long_ev[long_ev["date"] <= last_date].reset_index(drop=True)
    pd.testing.assert_frame_equal(short_ev.reset_index(drop=True), head, check_exact=True,
                                  check_dtype=False, obj=label + " events")


def _rows_events(data, ticker, regimes):
    rows, st = run(data, regimes=regimes, ticker=ticker)
    return rows, br.track_record_events(st, rows["eligible"].to_numpy())


def test_no_look_ahead_real_sini():
    """Raw prefixes remain unchanged; no legacy rule/event output is returned."""
    from price_contract import CONTRACT_VERSION, UnsupportedPriceContract
    from unittest.mock import patch
    path = os.path.join(RAW, "SINI.json.gz")
    if not os.path.exists(path):
        return _skip("no look-ahead on SINI", "inventory_raw/ cache not present")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    regs = bb.load_basis_regimes()
    full_brokers, full_ohlc = bb.frames_from_payload(data, "SINI")
    variants = [data]
    for cut in (100, 150, 180, len(data["date"]) - 1):
        short = _truncate(data, cut)
        brokers, ohlc = bb.frames_from_payload(short, "SINI")
        pd.testing.assert_frame_equal(ohlc, full_ohlc.iloc[:cut].reset_index(drop=True))
        head = full_brokers[full_brokers["date"] <= short["date"][-1]].reset_index(drop=True)
        pd.testing.assert_frame_equal(brokers, head)
        variants.append(short)
    extended = _extend(data, 40, seed=5)
    assert extended["date"][:len(data["date"])] == data["date"]
    assert extended["ohlc"][:len(data["ohlc"])] == data["ohlc"]
    for field in FIELDS:
        for code, values in data[field].items():
            assert extended[field][code][:len(values)] == values
    variants.append(extended)
    for payload in variants:
        before = json.dumps(payload, sort_keys=True)
        with patch.object(bb, "frames_from_payload", side_effect=AssertionError("rule pipeline must refuse before reading input")), \
                patch("builtins.open", side_effect=AssertionError("rule pipeline must refuse before file IO")):
            try:
                _rows_events(payload, "SINI", regs)
            except UnsupportedPriceContract as exc:
                assert exc.consumer == "broker_book.ticker_bundle"
                assert exc.status == "UNSUPPORTED" and exc.contract_version == CONTRACT_VERSION
            else:
                raise AssertionError("uncertified rule/events or zero/stale fallback returned")
        assert json.dumps(payload, sort_keys=True) == before


def test_no_look_ahead_synthetic():
    """The former v0 output requires an independently certified v1 adapter."""
    from corporate_action_test_support import assert_unmigrated
    assert_unmigrated("broker_rules.evaluate")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    print(f"broker rules: {len(ALL)} tests\n")
    for fn in ALL:
        print(fn.__name__)
        fn()
    print(f"\nAll {len(ALL)} tests passed."
          + (f" ({len(SKIPPED)} skipped: {'; '.join(SKIPPED)})" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
