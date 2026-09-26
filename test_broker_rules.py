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
    assert br.RULESET == "v1" and br.RULE_IDS == ["R1", "R2", "R3", "R4", "R5", "R6"]
    assert [r["dir"] for r in br.RULES] == [1, -1, -1, 1, 1, 1]
    assert [r["name"] for r in br.RULES] == ["acc_near_cost", "winner_distributing",
                                             "holders_underwater", "fresh_accumulation",
                                             "seller_exhaustion", "stealth_accumulation"]
    assert br.RULES[5]["label"] == "Akumulasi diam-diam, harga sideways"
    assert all(r["label"] and r["desc"] for r in br.RULES)
    assert br.PRIMARY_H == {"R1": 10, "R2": 10, "R3": 10, "R4": 10, "R5": 10, "R6": 60}
    assert (br.MIN_SESSIONS, br.MIN_VAL20, br.COST_MAX_SELL_FRAC, br.H_K) == (80, 2e8, 0.5, 3)
    assert (br.R1_MIN_ADV, br.R1_GAP_LO, br.R1_GAP_HI) == (3.0, -0.02, 0.05)
    assert (br.R2_MIN_ADV, br.R2_MIN_GAIN, br.R2_SELL_FRAC) == (1.0, 0.20, 0.20)
    assert (br.R3_MIN_ADV, br.R3_GAP_MAX) == (3.0, -0.10)
    assert (br.R4_MIN_ADV, br.R4_SELL_FRAC, br.R4_GAP) == (1.0, 0.25, 0.03)
    assert (br.R5_K, br.R5_MIN_ADV) == (3, 3.0)
    assert (br.R6_MIN_ADV, br.R6_MAX_SELL_FRAC, br.R6_MIN_BUYDAYS, br.R6_COST_GAP,
            br.R6_MAX_RANGE, br.R6_MAX_DRIFT) == (2.0, 0.4, 24, 0.10, 0.30, 0.10)
    assert (br.EVT_K, br.EVT_MIN_ADV) == (3, 0.5)
    assert (bb.W_SHORT, bb.W_LONG, bb.BASIS_LOOKBACK) == (5, 60, 80)
    rows, _ = run(scenario([]))
    assert list(rows.columns) == br.COLUMNS and len(rows) == N
    for col in ("a_broker", "a_nl60", "a_nl60_adv", "a_cost60", "a_gap", "a_buydays",
                "range60", "drift60"):
        assert col in rows
    assert all(rows[r].dtype == bool for r in br.RULE_IDS)
    print("  ok v1 constants, rule order/dirs, PRIMARY_H and output columns")


# ── Eligibility (§4.2) ─────────────────────────────────────────────────────

def test_eligibility_boundaries():
    # VAL20 = bg * 100 * 2000: 1,000 lots is exactly Rp 2e8; 999 is under.
    assert last(scenario([], bg=1000))["eligible"]
    assert not last(scenario([], bg=999))["eligible"]
    # n_sessions: the 80th session is the first eligible one.
    rows, _ = run(scenario([]))
    assert not rows["eligible"][78] and rows["eligible"][79]
    # basis_ok80: a flag on day 20 is inside T=99's 80-session window (20..99); day 19 is not.
    d = _dates(N)
    assert not last(scenario([]), regimes={"TEST": [(d[20], d[20])]})["eligible"]
    assert last(scenario([]), regimes={"TEST": [(d[19], d[19])]})["eligible"]
    # close must be > 0
    bars = {N - 1: (0.0, 0.0, 0.0)}
    row = last(scenario([], bars=bars))
    assert not row["eligible"]
    # ineligible rows: every rule False and every explain column empty
    holders = sum((spread(b, range(60, 70), 10_000, P) for b in ("AA", "BB", "CC")), [])
    row = last(scenario(holders, bg=999))
    assert not row["eligible"] and _fires(row) == []
    assert np.isnan(row["NLH"]) and row["a_broker"] is None and np.isnan(row["range60"])
    print("  ok VAL20 >= 2e8, n_sessions >= 80, basis_ok80, close > 0; ineligible rows stay empty")


# ── R1 / R3: holder group cost (§4.1, §4.3) ────────────────────────────────

def _holders(cc_lots=10_000, dd_lots=0):
    """AA, BB, CC each buy 10,000 lots at 2,000 on days 60..69: NLH = 30,000 = 3 x ADV20."""
    t = spread("AA", range(60, 70), 10_000, P) + spread("BB", range(60, 70), 10_000, P)
    t += spread("CC", range(60, 70), cc_lots, P)
    if dd_lots:
        t += spread("DD", range(60, 70), dd_lots, P)
    return t


def test_r1_acc_near_cost():
    row = last(scenario(_holders()))
    assert row["eligible"] and row["adv20"] == 10_000
    assert row["NLH"] == 30_000 and row["costH"] == 2000.0 and row["gapH"] == 0.0
    assert row["R1"] and not row["R3"]
    assert not last(scenario(_holders(cc_lots=9_999)))["R1"]        # NLH 29,999 < 3 x ADV
    row = last(scenario(_holders(dd_lots=500)))                      # 4th holder is not in H
    assert row["NLH"] == 30_000 and row["R1"]
    # gap window [-2%, +5%] around costH = 2,000, both ends inclusive
    for close, fire in ((2100, True), (2101, False), (1960, True), (1959, False)):
        row = last(scenario(_holders(), bars={N - 1: (close, 1800, 2200)}))
        assert bool(row["R1"]) is fire, (close, row["gapH"])
    print("  ok R1 fires at NLH = 3 x ADV and gap -2%/+5%, not one lot or one rupiah outside")


def test_r3_holders_underwater():
    row = last(scenario(_holders(), bars={N - 1: (1800, 1800, 2200)}))
    assert abs(row["gapH"] + 0.10) < 1e-12 and row["R3"] and not row["R1"]
    assert not last(scenario(_holders(), bars={N - 1: (1801, 1800, 2200)}))["R3"]
    assert not last(scenario(_holders(cc_lots=9_999), bars={N - 1: (1800, 1800, 2200)}))["R3"]
    print("  ok R3 fires at gap = -10% with NLH = 3 x ADV, not at -9.95% or NLH 29,999")


def test_rx_near_zero_position_has_no_cost():
    # RX buys 10,000 at 1,990 and sells 9,999 at 2,000: NL60 = 1 lot but
    # NV60 = 1.99e9 - 1.9998e9 = -9.8e6, an implied "cost" of Rp -98,000.
    trades = [("RX", 60, 10_000, 1990.0, 0, 0), ("RX", 61, 0, 0, 9_999, 2000.0)]
    row = last(scenario(trades))
    assert row["eligible"] and row["a_broker"] == "RX" and row["a_nl60"] == 1
    assert np.isnan(row["a_cost60"]) and np.isnan(row["a_gap"])
    assert row["NLH"] == 1 and np.isnan(row["costH"]) and np.isnan(row["gapH"])
    assert _fires(row) == [], _fires(row)
    # Same broker with a cost inside the band but a big two-way book: SL60 > 0.5*BL60.
    trades = [("RX", 60, 30_000, 2000.0, 0, 0), ("RX", 61, 0, 0, 15_001, 2000.0)]
    row = last(scenario(trades))
    assert row["a_nl60"] == 14_999 and np.isnan(row["a_cost60"])
    print("  ok RX: 1 lot net with a Rp -98,000 implied cost -> cost NaN, nothing fires")


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
    row = last(scenario(_winner(), bars=R2_BARS))
    assert row["w_broker"] == "WW" and row["w_nl60"] == 20_000 and row["w_nl5"] == -4000
    assert abs(row["w_cost60"] - 1920) < 1e-9 and abs(row["w_gain"] - 0.25) < 1e-12
    assert row["R2"]
    assert not last(scenario(_winner(sold=3999), bars=R2_BARS))["R2"]   # NL5 -3,999 > -0.2 x 20,001
    # gain boundary: 2304/1920 - 1 = +20% exactly
    bars = dict(R2_BARS)
    bars[N - 1] = (2304.0, 2300.0, 2420.0)
    assert last(scenario(_winner(), bars=bars))["R2"]
    bars[N - 1] = (2303.0, 2300.0, 2420.0)
    assert not last(scenario(_winner(), bars=bars))["R2"]
    # the argmax is the most profitable accumulator: if WW holds, VV selling does not fire
    row = last(scenario(_winner(vv=True, ww_sells=False), bars=R2_BARS))
    assert row["w_broker"] == "WW" and row["w_nl5"] == 0 and not row["R2"]
    assert last(scenario(_winner(vv=True), bars=R2_BARS))["R2"]
    print("  ok R2 fires at gain +20% and NL5 = -20% of NL60, only for the top winner")


# ── R4: fresh accumulation ─────────────────────────────────────────────────

def test_r4_fresh_accumulation():
    # BG 9,500: FF buying b lots in days 95..99 gives ADV20 = 9,500 + b/20, so
    # b = 10,000 makes NL5 = ADV20 = 10,000 exactly; VAL20 = 1.9e9 + b*100*2000/20.
    row = last(scenario(spread("FF", range(95, 100), 10_000, P), bg=9500))
    assert row["adv20"] == 10_000 and row["f_broker"] == "FF" and row["f_nl5"] == 10_000
    assert row["f_cost5"] == 2000.0 and row["R4"]
    assert not last(scenario(spread("FF", range(95, 100), 9_999, P), bg=9500))["R4"]
    # sell fraction: buy 16,000 and sell 4,000 = 0.25 x BL5 -> fires; 4,001 does not
    for sold, fire in ((4000, True), (4001, False)):
        t = spread("FF", range(95, 100), 16_000, P) + [("FF", 97, 0, 0, sold, P)]
        assert bool(last(scenario(t, bg=9500))["R4"]) is fire, sold
    # |close/cost5 - 1| <= 3%
    t = spread("FF", range(95, 100), 12_000, P)
    for close, fire in ((2060, True), (2061, False), (1940, True), (1939, False)):
        assert bool(last(scenario(t, bg=9500, bars={N - 1: (close, 1800, 2200)}))["R4"]) is fire, close
    # cost5 outside [low5, high5] is invalid even when price is near it
    bars = {d: (2150.0, 2100.0, 2300.0) for d in range(95, 100)}
    row = last(scenario(t, bg=9500, bars=bars))
    assert np.isnan(row["f_cost5"]) and not row["R4"]
    print("  ok R4 fires at NL5 = ADV, SL5 = 0.25 BL5, gap +/-3%; cost outside the 5-day range is NaN")


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
    row = last(scenario(_sellers()))
    assert row["s_brokers"] == "SA,SB,SC" and row["s_nl60"] == -30_000 and row["s_nl5"] == 0
    assert row["R5"]
    assert not last(scenario(_sellers(sc=7999)))["R5"]                  # -29,999 > -3 x ADV
    assert not last(scenario(_sellers(sb_last5=1)))["R5"]               # still selling: NL5 = -1
    # buying 1 lot back lifts NL5 to +1 but also ADV20 by 1/20 (3 x ADV = 30,000.15),
    # so the sellers need -30,001 net: SC sells 8,002
    row = last(scenario(_sellers(sc=8002, sb_last5=-1)))
    assert row["s_nl5"] == 1 and row["R5"]
    # a 4th, smaller seller who keeps selling is not in S; an equal one loses on code
    extra = spread("SD", range(45, 55), 7000, P, "sell")
    assert last(scenario(_sellers(extra=extra)))["R5"]
    extra = spread("SZ", range(45, 55), 8000, P, "sell")
    assert last(scenario(_sellers(extra=extra)))["s_brokers"] == "SA,SB,SC"
    extra = spread("S0", range(45, 55), 8000, P, "sell")
    assert last(scenario(_sellers(extra=extra)))["s_brokers"] == "SA,SB,S0"
    print("  ok R5 fires at sum NL60 = -3 x ADV with NL5 >= 0; bottom-3 by NL60, ties by code")


# ── R6: stealth accumulation (§4.3.1) ──────────────────────────────────────

R6_BAR = (P, 1900.0, 2100.0)    # range60 = 2100/1900 - 1 = 10.5%


def _collector(days=range(50, 74), lots=24_000, sold=0, broker="AK"):
    """AK buys on each of 24 sessions inside the window but outside the ADV20 days (80..99)."""
    t = spread(broker, days, lots, P)
    if sold:
        t.append((broker, 76, 0, 0, sold, P))
    return t


def test_r6_stealth_accumulation():
    row = last(scenario(_collector(), default_bar=R6_BAR))
    assert row["a_broker"] == "AK" and row["a_nl60"] == 24_000 and row["a_buydays"] == 24
    assert row["a_nl60_adv"] == 2.4 and row["a_cost60"] == 2000.0 and row["a_gap"] == 0.0
    assert abs(row["range60"] - 0.2 / 1.9) < 1e-12 and row["drift60"] == 0.0
    assert row["R6"]
    # 23 buy days (still 23,000 lots >= 2 x ADV) -> no; 24 days but 19,999 lots -> no
    assert not last(scenario(_collector(days=range(50, 73), lots=23_000), default_bar=R6_BAR))["R6"]
    assert last(scenario(_collector(lots=20_000), default_bar=R6_BAR))["R6"]
    assert not last(scenario(_collector(lots=19_999), default_bar=R6_BAR))["R6"]
    # SL60 <= 0.4 x BL60: buy 48,000, sell 19,200 (NL60 28,800) fires; 19,201 does not
    assert last(scenario(_collector(lots=48_000, sold=19_200), default_bar=R6_BAR))["R6"]
    assert not last(scenario(_collector(lots=48_000, sold=19_201), default_bar=R6_BAR))["R6"]
    # sideways: range60 <= 30% (low 2,000 / high 2,600), one tick wider does not
    assert last(scenario(_collector(), default_bar=(P, 2000.0, 2600.0)))["R6"]
    bars = {70: (P, 2000.0, 2601.0)}
    assert not last(scenario(_collector(), default_bar=(P, 2000.0, 2600.0), bars=bars))["R6"]
    # drift and cost gap both +10% at close 2,200 (close 59 sessions back = 2,000)
    bars = {N - 1: (2200.0, 2150.0, 2250.0)}
    row = last(scenario(_collector(), default_bar=R6_BAR, bars=bars))
    assert abs(row["drift60"] - 0.1) < 1e-12 and row["R6"]
    bars = {N - 1: (2201.0, 2150.0, 2250.0)}
    assert not last(scenario(_collector(), default_bar=R6_BAR, bars=bars))["R6"]
    # drift alone: close(T-59) = 1,800 -> +11.1% while cost gap stays 0
    bars = {40: (1800.0, 1795.0, 1805.0)}
    row = last(scenario(_collector(), default_bar=R6_BAR, bars=bars))
    assert row["a_gap"] == 0.0 and row["drift60"] > 0.1 and not row["R6"]
    # a tie on NL60 goes to the lower code
    t = _collector() + _collector(broker="AJ")
    assert last(scenario(t, default_bar=R6_BAR))["a_broker"] == "AJ"
    print("  ok R6 fires at 2 x ADV, 24 buy days, SL 0.4 BL, range 30%, drift/gap 10%; not beyond")


def test_explain_columns_on_every_eligible_row():
    rows, _ = run(scenario(_collector(lots=19_999), default_bar=R6_BAR))
    elig = rows[rows["eligible"]]
    assert len(elig) == 21 and not elig["R6"].any()                      # days 79..99
    assert (elig["a_broker"] == "AK").all() and elig["range60"].notna().all()
    assert elig["drift60"].notna().all() and elig["a_buydays"].notna().all()
    assert rows.loc[~rows["eligible"], "a_broker"].isna().all()
    print("  ok a_* / range60 / drift60 filled on every eligible row, fired or not")


# ── Track-record events (§4.4) ─────────────────────────────────────────────

def test_track_record_events():
    # Last 5 sessions: NL5 AA 6,000, BB 6,000, GG 6,000, CC 5,500, DD 4,000,
    # EE -5,000, FF -6,000. ADV20 = 10,000 + buys/20 in days 80..99.
    buys = {"AA": 6000, "BB": 6000, "GG": 6000, "CC": 5500, "DD": 4000}
    t = [(b, 97, q, P, 0, 0) for b, q in buys.items()]
    t += [("EE", 97, 0, 0, 5000, P), ("FF", 97, 0, 0, 6000, P)]
    rows, st = run(scenario(t))
    adv = 10_000 + sum(buys.values()) / 20                               # 11,375
    assert rows["adv20"].iloc[-1] == adv
    ev = br.track_record_events(st, rows["eligible"].to_numpy())
    assert list(ev.columns) == br.EVENT_COLS
    last_day = ev[ev["date"] == st.dates[-1]]
    got = list(zip(last_day["broker"], last_day["side"], last_day["nl5"]))
    # threshold 0.5 x 11,375 = 5,687.5: CC (5,500) is out; the tie AA/BB/GG is in code order
    assert got == [("AA", "buy", 6000), ("BB", "buy", 6000), ("GG", "buy", 6000),
                   ("FF", "sell", -6000)], got
    assert (last_day["adv20"] == adv).all()
    # dates before any eligible session carry no events; ineligible mask -> none at all
    assert set(ev["date"]) <= set(np.asarray(st.dates)[rows["eligible"].to_numpy()])
    assert br.track_record_events(st, np.zeros(N, dtype=bool)).empty
    print("  ok top-3 buyers / bottom-3 sellers past 0.5 x ADV, ties by code, eligible only")


# ── Composite score and explanations ───────────────────────────────────────

def test_composite_score():
    rows = pd.DataFrame({"R1": [True, False, True], "R2": [True, False, False],
                         "R3": [False, False, False], "R4": [False, True, True],
                         "R5": [False, False, False], "R6": [False, False, True]})
    got = br.composite_score(rows, {"R1": 1.5, "R6": 0.5}).tolist()
    assert got == [1.5 - 1.0, 1.0, 1.5 + 1.0 + 0.5], got
    assert br.composite_score(rows, {}).tolist() == [0.0, 1.0, 3.0]
    print("  ok score = sum fired x dir x weight (default 1.0)")


def test_explain_indonesian_lines():
    row = pd.Series({"R2": True, "w_broker": "XL", "w_gain": 0.48, "w_cost60": 12077.0,
                     "w_nl5": -33419.0, "w_nl60": 119354.0, "adv20": 50000.0})
    assert br.explain(row) == ["R2 · XL untung +48% dari modal Rp 12.077, jual 33.419 lot "
                               "dalam 5 hari (−28% posisinya)"], br.explain(row)
    row = pd.Series({"R6": True, "a_broker": "BK", "a_nl60": 45200.0, "a_nl60_adv": 3.1,
                     "a_buydays": 31.0, "range60": 0.18, "a_cost60": 1250.0, "a_gap": 0.02})
    assert br.explain(row) == ["R6 · BK kumpulkan 45.200 lot (3,1x ADV) selama 31 dari 60 hari, "
                               "harga sideways (range 18%), modal ~Rp 1.250 (+2%)"], br.explain(row)
    rows, _ = run(scenario(_holders() + _sellers()))
    lines = br.explain(rows.iloc[-1])
    assert [s[:2] for s in lines] == ["R1", "R5"], lines
    assert "30.000 lot (3,0x ADV)" in lines[0] and "(SA, SB, SC)" in lines[1]
    assert br.explain(pd.Series({"R1": False, "R3": np.nan})) == []
    print("  ok explain() gives one Indonesian line per fired rule with the numbers")


def test_explain_sign_is_decided_on_the_rounded_number():
    """Review finding: a -0.4% gap printed as '+0%'. R1 (-2%..+5%) and R4
    (|gap| <= 3%) make near-zero gaps common, so the sign must follow the
    rounded text, as broker_dashboard's fmt_* helpers do."""
    assert [br._pct(v) for v in (-0.004, 0.004, 0.0, -0.006, 0.006)] ==         ["0%", "0%", "0%", "−1%", "+1%"]
    assert br._num(-0.4) == "0" and br._num(-0.04, 1, True) == "0,0"
    assert br._num(-1234.5, 1) == "−1.234,5" and br._num(1234.5, 1, True) == "+1.234,5"
    row = pd.Series({"R6": True, "a_broker": "BK", "a_nl60": 45200.0, "a_nl60_adv": 3.1,
                     "a_buydays": 31.0, "range60": 0.18, "a_cost60": 1250.0, "a_gap": -0.004})
    assert br.explain(row)[0].endswith("modal ~Rp 1.250 (0%)"), br.explain(row)
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
    path = os.path.join(RAW, "SINI.json.gz")
    if not os.path.exists(path):
        return _skip("no look-ahead on SINI", "inventory_raw/ cache not present")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    regs = bb.load_basis_regimes()
    full_rows, full_ev = _rows_events(data, "SINI", regs)
    fired_seen = 0
    for cut in (100, 150, 180, len(data["date"]) - 1):
        rows, ev = _rows_events(_truncate(data, cut), "SINI", regs)
        assert rows["eligible"].any()
        fired_seen += int(rows[br.RULE_IDS].to_numpy().sum())
        _assert_prefix_equal(rows, full_rows, ev, full_ev, f"SINI cut {cut}")
    ext_rows, ext_ev = _rows_events(_extend(data, 40, seed=5), "SINI", regs)
    _assert_prefix_equal(full_rows, ext_rows, full_ev, ext_ev, "SINI + 40 synthetic sessions")
    assert fired_seen > 0 and len(full_ev) > 0
    print(f"  ok SINI rows/events for T <= cut identical to the full fetch and to a 40-session "
          f"extension ({fired_seen} fired rule-rows compared)")


def test_no_look_ahead_synthetic():
    base = scenario(_collector() + _holders() + _sellers() + _winner(), default_bar=R6_BAR)
    full_rows, full_ev = _rows_events(base, "TEST", {})
    assert full_rows[br.RULE_IDS].to_numpy().sum() > 0
    for cut in (85, 95, N - 1):
        rows, ev = _rows_events(_truncate(base, cut), "TEST", {})
        _assert_prefix_equal(rows, full_rows, ev, full_ev, f"synthetic cut {cut}")
    ext_rows, ext_ev = _rows_events(_extend(base, 60, seed=9), "TEST", {})
    _assert_prefix_equal(full_rows, ext_rows, full_ev, ext_ev, "synthetic + 60 sessions")
    print("  ok synthetic rows/events for T <= cut identical when truncated or extended")


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
