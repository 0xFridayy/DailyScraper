"""Ruleset v1: the frozen broker-inventory rules and track-record events
(BROKER_LEARNING.md §4.1-§4.4, §4.3.1).

Every threshold below is a named constant because the ruleset is preregistered:
changing any of them is a NEW ruleset version, and v1 rows are never rewritten
(§0). The rules read only a broker_book.RollingState, whose features at session
T use sessions <= T, so a rule row for T is the same whether the fetch ends at
T or later (test_broker_rules checks this on real and synthetic payloads).

All of it is OBSERVABLE BROKER INVENTORY, NOT BENEFICIAL OWNERSHIP. A broker
code is a securities firm with many clients; "accumulator" below means a code
whose net lots went up, not a person or a controller. Nothing here feeds
daily_picks.py, the ARB veto or any ML experiment.

WHY A TOLERANCE ON EVERY THRESHOLD
----------------------------------
The spec's boundaries are inclusive and exact ("close/costH - 1 <= 0.05"). In
float64, 1050/1000 - 1 is 0.05000000000000004, so an exact tie on round IDX
prices would fail an inclusive test by rounding alone. Comparisons therefore
allow RATIO_TOL = 1e-9 relative slack: millions of times wider than float64
rounding, and far narrower than one lot or one IDX price tick, so it only ever
decides exact mathematical ties, in the direction the spec wrote.

WHY TIES ARE BROKEN BY CODE, AND WHY ZERO NEVER WINS
----------------------------------------------------
"argmax NL" with a tie takes the lowest broker code: state.brokers is sorted,
and numpy's argmax and stable argsort both keep the first of equal values. The
broker list is every code that traded anywhere in the fetch, so a longer fetch
can add codes whose rows are all zero up to T. Such a code can only tie at
zero, so every selection that names a broker requires a strictly non-zero
value: that keeps the named broker (and so every explain column) identical
between a fetch ending at T and a longer one.
"""

import math

import numpy as np
import pandas as pd

RULESET = "v1"

# Tolerance for inclusive boundaries (see module docstring).
RATIO_TOL = 1e-9

# §4.1 window cost and holder group
COST_MAX_SELL_FRAC = 0.5    # cost_w valid only if SL_w <= 0.5*BL_w (mostly one-directional)
H_K = 3                     # holder group: top 3 by NL_60 among NL_60 > 0

# §4.2 eligibility
MIN_SESSIONS = 80
# Rp 200 juta/day (Amendment A2; was 1e9, and 2e9 before that). Most
# multi-baggers start illiquid: of the 17 ten-baggers in 2025-26, only 3
# traded >= Rp 1 bn/day when their run began, so a 1e9 bar hid the very
# moves R6 is about.
MIN_VAL20 = 2e8

# §4.3 R1 acc_near_cost
R1_MIN_ADV = 3.0
R1_GAP_LO = -0.02
R1_GAP_HI = 0.05
# R2 winner_distributing
R2_MIN_ADV = 1.0
R2_MIN_GAIN = 0.20
R2_SELL_FRAC = 0.20
# R3 holders_underwater
R3_MIN_ADV = 3.0
R3_GAP_MAX = -0.10
# R4 fresh_accumulation
R4_MIN_ADV = 1.0
R4_SELL_FRAC = 0.25
R4_GAP = 0.03
# R5 seller_exhaustion
R5_K = 3
R5_MIN_ADV = 3.0
R5_MIN_NL5 = 0.0
# §4.3.1 R6 stealth_accumulation
R6_MIN_ADV = 2.0
R6_MAX_SELL_FRAC = 0.4
R6_MIN_BUYDAYS = 24
R6_COST_GAP = 0.10
R6_MAX_RANGE = 0.30
R6_MAX_DRIFT = 0.10
# §4.4 broker track-record events
EVT_K = 3
EVT_MIN_ADV = 0.5

RULES = [
    {"id": "R1", "dir": +1, "name": "acc_near_cost",
     "label": "Harga dekat modal akumulator besar",
     "desc": "Tiga broker dengan beli bersih terbesar 60 hari memegang minimal 3x volume harian "
             "rata-rata, dan harga sekarang dalam -2% s/d +5% dari modal gabungan mereka."},
    {"id": "R2", "dir": -1, "name": "winner_distributing",
     "label": "Akumulator paling untung sedang ambil untung",
     "desc": "Broker akumulator 60 hari yang paling untung sudah naik minimal 20% dari modalnya, "
             "dan dalam 5 hari terakhir menjual minimal 20% dari posisinya."},
    {"id": "R3", "dir": -1, "name": "holders_underwater",
     "label": "Akumulator besar nyangkut",
     "desc": "Tiga broker dengan beli bersih terbesar 60 hari memegang minimal 3x volume harian "
             "rata-rata, dan harga sekarang minimal 10% di bawah modal gabungan mereka."},
    {"id": "R4", "dir": +1, "name": "fresh_accumulation",
     "label": "Akumulasi baru di harga pasar",
     "desc": "Satu broker membeli bersih minimal 1x volume harian rata-rata dalam 5 hari, hampir "
             "tanpa menjual, dan harga sekarang dalam 3% dari modal belinya."},
    {"id": "R5", "dir": +1, "name": "seller_exhaustion",
     "label": "Penjual besar berhenti jual",
     "desc": "Tiga penjual bersih terbesar 60 hari sudah melepas minimal 3x volume harian "
             "rata-rata, dan dalam 5 hari terakhir mereka tidak lagi menjual bersih."},
    {"id": "R6", "dir": +1, "name": "stealth_accumulation",
     "label": "Akumulasi diam-diam, harga sideways",
     "desc": "Satu broker mengumpulkan minimal 2x volume harian rata-rata selama 60 hari, beli "
             "bersih di minimal 24 hari, sementara harga sideways (range <= 30%, perubahan <= 10%) "
             "dan dekat modalnya. Dinilai pada horizon 60 sesi."},
]
RULE_IDS = [r["id"] for r in RULES]
RULE_DIR = {r["id"]: r["dir"] for r in RULES}

# The horizon each rule's status and weight are judged at (§4.5).
PRIMARY_H = {"R1": 10, "R2": 10, "R3": 10, "R4": 10, "R5": 10, "R6": 60}

EXPLAIN_FLOAT = ["NLH", "costH", "gapH", "w_nl60", "w_cost60", "w_gain", "w_nl5",
                 "f_nl5", "f_cost5", "f_gap", "s_nl60", "s_nl5",
                 "a_nl60", "a_nl60_adv", "a_cost60", "a_gap", "a_buydays", "range60", "drift60"]
EXPLAIN_STR = ["w_broker", "f_broker", "s_brokers", "a_broker"]
COLUMNS = (["date", "ticker", "close", "eligible", "rv20"] + RULE_IDS
           + ["NLH", "costH", "gapH",
              "w_broker", "w_nl60", "w_cost60", "w_gain", "w_nl5",
              "f_broker", "f_nl5", "f_cost5", "f_gap",
              "s_brokers", "s_nl60", "s_nl5",
              "a_broker", "a_nl60", "a_nl60_adv", "a_cost60", "a_gap", "a_buydays",
              "range60", "drift60",
              "adv20", "val20"])
EVENT_COLS = ["date", "ticker", "broker", "side", "nl5", "adv20"]


# ── Comparisons ────────────────────────────────────────────────────────────

def _ge(a, b):
    """a >= b, inclusive up to RATIO_TOL; NaN on either side is False."""
    with np.errstate(invalid="ignore"):
        return np.asarray(a >= b - RATIO_TOL * np.abs(b))


def _le(a, b):
    with np.errstate(invalid="ignore"):
        return np.asarray(a <= b + RATIO_TOL * np.abs(b))


def _ratio(a, b):
    with np.errstate(invalid="ignore", divide="ignore"):
        return a / b


def window_cost(NL, NV, BL, SL, low, high):
    """cost_w = NV/(NL*100), NaN unless NL > 0, SL <= 0.5*BL and low <= cost <= high (§4.1)."""
    cost = _ratio(NV, NL * 100.0)
    valid = (NL > 0) & _le(SL, COST_MAX_SELL_FRAC * BL) & _ge(cost, low) & _le(cost, high)
    return np.where(valid, cost, np.nan)


# ── Eligibility and rules ──────────────────────────────────────────────────

def eligibility(state):
    """§4.2: the rows that form the universe (and base rate) for a date."""
    with np.errstate(invalid="ignore"):
        return ((state.n_sessions >= MIN_SESSIONS)
                & (state.val20 >= MIN_VAL20)
                & (state.adv20 > 0)
                & (state.close > 0)
                & np.asarray(state.basis_ok80, dtype=bool))


def _holder_group(NL60, NV60, close, low60, high60):
    k = min(H_K, NL60.shape[0])
    top = np.argsort(-NL60, axis=0, kind="stable")[:k]
    nl = np.take_along_axis(NL60, top, axis=0)
    nv = np.take_along_axis(NV60, top, axis=0)
    inH = nl > 0
    any_h = inH.any(axis=0)
    NLH = np.where(inH, nl, 0.0).sum(axis=0)
    NVH = np.where(inH, nv, 0.0).sum(axis=0)
    cost = _ratio(NVH, NLH * 100.0)
    valid = (NLH > 0) & _ge(cost, low60) & _le(cost, high60)
    costH = np.where(valid, cost, np.nan)
    return np.where(any_h, NLH, np.nan), costH, _ratio(close, costH) - 1


def _codes(brokers, idx, mask):
    return np.array([brokers[i] if m else None for i, m in zip(idx, mask)], dtype=object)


def evaluate(state):
    """One row per session: eligibility, R1..R6 and the numbers behind each (see COLUMNS).

    Explain columns are filled on every eligible row where they are defined,
    whether or not the rule fires (the alpha library reads the a_* set on all
    of them), and are NaN/None elsewhere. Rules are False on ineligible rows.
    """
    n = len(state.dates)
    elig = eligibility(state)
    out = {c: np.full(n, np.nan) for c in EXPLAIN_FLOAT}
    out.update({c: np.full(n, None, dtype=object) for c in EXPLAIN_STR})
    fired = {r: np.zeros(n, dtype=bool) for r in RULE_IDS}
    e = np.flatnonzero(elig)
    nb = len(state.brokers)

    if len(e):
        c = state.close[e]
        adv = state.adv20[e]
        lo5, hi5 = state.low5[e], state.high5[e]
        lo60, hi60 = state.low60[e], state.high60[e]
        range60 = _ratio(hi60, lo60) - 1
        drift60 = _ratio(c, state.close_lag59[e]) - 1
        out["range60"][e], out["drift60"][e] = range60, drift60

    if len(e) and nb:
        NL5, NV5 = state.NL5[:, e], state.NV5[:, e]
        BL5, SL5 = state.BL5[:, e], state.SL5[:, e]
        NL60, NV60 = state.NL60[:, e], state.NV60[:, e]
        BL60, SL60 = state.BL60[:, e], state.SL60[:, e]
        cols = np.arange(len(e))

        # R1 / R3: the holder group's combined cost.
        NLH, costH, gapH = _holder_group(NL60, NV60, c, lo60, hi60)
        big = np.isfinite(costH)
        fired["R1"][e] = (big & _ge(NLH, R1_MIN_ADV * adv)
                          & _ge(gapH, R1_GAP_LO) & _le(gapH, R1_GAP_HI))
        fired["R3"][e] = big & _ge(NLH, R3_MIN_ADV * adv) & _le(gapH, R3_GAP_MAX)
        out["NLH"][e], out["costH"][e], out["gapH"][e] = NLH, costH, gapH

        # R2: the most profitable 60-session accumulator, if it is selling.
        cost60 = window_cost(NL60, NV60, BL60, SL60, lo60, hi60)
        inW = np.isfinite(cost60) & _ge(NL60, R2_MIN_ADV * adv)
        gain_rp = np.where(inW, NL60 * 100.0 * (c - cost60), -np.inf)
        w = np.argmax(gain_rp, axis=0)
        any_w = inW.any(axis=0)
        w_nl60 = np.where(any_w, NL60[w, cols], np.nan)
        w_cost60 = np.where(any_w, cost60[w, cols], np.nan)
        w_gain = _ratio(c, w_cost60) - 1
        w_nl5 = np.where(any_w, NL5[w, cols], np.nan)
        fired["R2"][e] = (any_w & _ge(w_gain, R2_MIN_GAIN)
                          & _le(w_nl5, -R2_SELL_FRAC * w_nl60))
        out["w_broker"][e] = _codes(state.brokers, w, any_w)
        out["w_nl60"][e], out["w_cost60"][e] = w_nl60, w_cost60
        out["w_gain"][e], out["w_nl5"][e] = w_gain, w_nl5

        # R4: the biggest 5-session net buyer, buying near its own cost.
        f = np.argmax(NL5, axis=0)
        f_nl5 = NL5[f, cols]
        has_f = f_nl5 > 0
        f_cost5 = window_cost(f_nl5, NV5[f, cols], BL5[f, cols], SL5[f, cols], lo5, hi5)
        f_gap = _ratio(c, f_cost5) - 1
        fired["R4"][e] = (has_f & _ge(f_nl5, R4_MIN_ADV * adv)
                          & _le(SL5[f, cols], R4_SELL_FRAC * BL5[f, cols])
                          & np.isfinite(f_cost5) & _le(np.abs(f_gap), R4_GAP))
        out["f_broker"][e] = _codes(state.brokers, f, has_f)
        out["f_nl5"][e] = np.where(has_f, f_nl5, np.nan)
        out["f_cost5"][e], out["f_gap"][e] = f_cost5, f_gap

        # R5: the three biggest 60-session net sellers have stopped selling.
        k = min(R5_K, nb)
        low_k = np.argsort(NL60, axis=0, kind="stable")[:k]
        s60 = np.take_along_axis(NL60, low_k, axis=0)
        s5 = np.take_along_axis(NL5, low_k, axis=0)
        inS = s60 < 0
        any_s = inS.any(axis=0)
        s_nl60 = np.where(any_s, np.where(inS, s60, 0.0).sum(axis=0), np.nan)
        s_nl5 = np.where(any_s, np.where(inS, s5, 0.0).sum(axis=0), np.nan)
        fired["R5"][e] = (any_s & _le(s_nl60, -R5_MIN_ADV * adv) & _ge(s_nl5, R5_MIN_NL5))
        names = [",".join(state.brokers[low_k[j, i]] for j in range(k) if inS[j, i]) or None
                 for i in range(len(e))]
        out["s_brokers"][e] = np.array(names, dtype=object)
        out["s_nl60"][e], out["s_nl5"][e] = s_nl60, s_nl5

        # R6: one steady collector while price goes sideways (§4.3.1).
        a = np.argmax(NL60, axis=0)
        a_nl60 = NL60[a, cols]
        has_a = a_nl60 > 0
        a_bl60, a_sl60 = BL60[a, cols], SL60[a, cols]
        a_cost60 = window_cost(a_nl60, NV60[a, cols], a_bl60, a_sl60, lo60, hi60)
        a_gap = _ratio(c, a_cost60) - 1
        a_buydays = state.BUYDAYS60[:, e][a, cols]
        fired["R6"][e] = (has_a & _ge(a_nl60, R6_MIN_ADV * adv)
                          & _le(a_sl60, R6_MAX_SELL_FRAC * a_bl60)
                          & _ge(a_buydays, R6_MIN_BUYDAYS)
                          & np.isfinite(a_cost60) & _le(np.abs(a_gap), R6_COST_GAP)
                          & _le(range60, R6_MAX_RANGE) & _le(np.abs(drift60), R6_MAX_DRIFT))
        out["a_broker"][e] = _codes(state.brokers, a, has_a)
        out["a_nl60"][e] = np.where(has_a, a_nl60, np.nan)
        out["a_nl60_adv"][e] = np.where(has_a, _ratio(a_nl60, adv), np.nan)
        out["a_cost60"][e] = np.where(has_a, a_cost60, np.nan)
        out["a_gap"][e] = np.where(has_a, a_gap, np.nan)
        out["a_buydays"][e] = np.where(has_a, a_buydays, np.nan)

    # Code columns stay object dtype so a missing code is None under pandas 2 and 3
    # alike (pandas 3 would otherwise infer a string dtype and turn None into NaN).
    for c in EXPLAIN_STR:
        out[c] = pd.Series(out[c], dtype=object)
    frame = pd.DataFrame({
        "date": list(state.dates), "ticker": state.ticker,
        "close": state.close, "eligible": elig, "rv20": state.rv20,
        **fired, **out,
        "adv20": state.adv20, "val20": state.val20,
    })
    return frame[COLUMNS]


def track_record_events(state, eligible):
    """§4.4: on eligible sessions, the top 3 net buyers and bottom 3 net sellers by NL_5
    that clear 0.5*ADV20, ties broken by code. Ordered by date, buys first, then rank."""
    e = np.flatnonzero(np.asarray(eligible, dtype=bool))
    nb = len(state.brokers)
    if not len(e) or not nb:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in EVENT_COLS})
    NL5 = state.NL5[:, e]
    adv = state.adv20[e]
    k = min(EVT_K, nb)
    parts = []
    for side_rank, (side, order) in enumerate((
            ("buy", np.argsort(-NL5, axis=0, kind="stable")[:k]),
            ("sell", np.argsort(NL5, axis=0, kind="stable")[:k]))):
        v = np.take_along_axis(NL5, order, axis=0)
        if side == "buy":
            hit = _ge(v, EVT_MIN_ADV * adv) & (v > 0)
        else:
            hit = _le(v, -EVT_MIN_ADV * adv) & (v < 0)
        rank, col = np.nonzero(hit)
        parts.append(pd.DataFrame({
            "_i": col, "_side": side_rank, "_rank": rank,
            "date": np.asarray(state.dates, dtype=object)[e[col]],
            "ticker": state.ticker,
            "broker": np.asarray(state.brokers, dtype=object)[order[rank, col]],
            "side": side, "nl5": v[rank, col].astype(float), "adv20": adv[col].astype(float),
        }))
    events = pd.concat(parts, ignore_index=True).sort_values(["_i", "_side", "_rank"], kind="stable")
    return events[EVENT_COLS].reset_index(drop=True)


def composite_score(rule_rows, weights):
    """Σ_r fired_r * dir_r * weight_r (dashboard ordering only). Missing weight = 1.0."""
    weights = weights or {}
    score = pd.Series(0.0, index=rule_rows.index)
    for r in RULES:
        if r["id"] not in rule_rows:
            continue
        hit = rule_rows[r["id"]].eq(True).fillna(False).astype(bool)
        score += hit.astype(float) * r["dir"] * float(weights.get(r["id"], 1.0))
    return score


# ── Indonesian explanations ────────────────────────────────────────────────

MINUS = "−"


def _ok(v):
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _num(v, decimals=0, signed=False):
    """Indonesian number: '.' thousands, ',' decimals, a real minus sign.

    The sign is decided on the ROUNDED text, as broker_dashboard does: a gap
    of -0.4% prints as '0%', never '+0%' (the wrong sign) or '-0%'."""
    if not _ok(v):
        return "?"
    v = float(v)
    text = f"{abs(v):,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    if text.strip("0,.") == "":
        return text
    if v < 0:
        return MINUS + text
    return ("+" + text) if signed else text


def _pct(v, signed=True):
    return "?%" if not _ok(v) else _num(100 * float(v), 0, signed) + "%"


def _rp(v):
    return "Rp ?" if not _ok(v) else "Rp " + _num(v)


def _div(a, b):
    return float(a) / float(b) if _ok(a) and _ok(b) and float(b) != 0 else None


def _fired(v):
    try:
        return v is not None and not pd.isna(v) and bool(v)
    except (TypeError, ValueError):
        return False


def explain(row):
    """Short Indonesian lines, one per fired rule, carrying the numbers behind it."""
    g = row.get
    adv = g("adv20")
    lines = []
    if _fired(g("R1")):
        lines.append(f"R1 · 3 akumulator terbesar pegang {_num(g('NLH'))} lot "
                     f"({_num(_div(g('NLH'), adv), 1)}x ADV), "
                     f"modal ~{_rp(g('costH'))}, harga {_pct(g('gapH'))} dari modal")
    if _fired(g("R2")):
        nl5, nl60 = g("w_nl5"), g("w_nl60")
        share = _div(nl5, nl60)
        lines.append(f"R2 · {g('w_broker')} untung {_pct(g('w_gain'))} dari modal "
                     f"{_rp(g('w_cost60'))}, jual {_num(-nl5 if _ok(nl5) else None)} lot "
                     f"dalam 5 hari ({_pct(share)} posisinya)")
    if _fired(g("R3")):
        lines.append(f"R3 · 3 akumulator terbesar pegang {_num(g('NLH'))} lot "
                     f"dengan modal ~{_rp(g('costH'))}, harga {_pct(g('gapH'))} "
                     "dari modal (nyangkut)")
    if _fired(g("R4")):
        f5 = g("f_nl5")
        lines.append(f"R4 · {g('f_broker')} beli bersih {_num(f5)} lot dalam 5 hari "
                     f"({_num(_div(f5, adv), 1)}x ADV), "
                     f"modal ~{_rp(g('f_cost5'))} ({_pct(g('f_gap'))})")
    if _fired(g("R5")):
        s60 = g("s_nl60")
        brokers = str(g("s_brokers") or "?").replace(",", ", ")
        lines.append(f"R5 · penjual terbesar ({brokers}) jual bersih "
                     f"{_num(-s60 if _ok(s60) else None)} lot dalam 60 hari "
                     f"({_num(_div(-s60 if _ok(s60) else None, adv), 1)}x ADV), "
                     f"5 hari terakhir {_num(g('s_nl5'), signed=True)} lot: berhenti jual")
    if _fired(g("R6")):
        lines.append(f"R6 · {g('a_broker')} kumpulkan {_num(g('a_nl60'))} lot "
                     f"({_num(g('a_nl60_adv'), 1)}x ADV) selama {_num(g('a_buydays'))} dari 60 hari, "
                     f"harga sideways (range {_pct(g('range60'), signed=False)}), "
                     f"modal ~{_rp(g('a_cost60'))} ({_pct(g('a_gap'))})")
    return lines
