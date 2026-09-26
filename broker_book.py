"""Per-ticker broker frames, basis flags, the anchored average-cost book and the
rolling per-session features that ruleset v1 reads (BROKER_LEARNING.md §2-§4.1).

Everything here describes OBSERVABLE BROKER INVENTORY, NOT BENEFICIAL
OWNERSHIP: a broker code aggregates many clients, and a position below is only
what that code netted on this ticker since the anchor. Pure numpy/pandas; it
never imports neobdm_scraper, playwright or anything that needs a secret.

WHY THE PAYLOAD CHECK IS RE-IMPLEMENTED HERE
-------------------------------------------
build_inventory_db.strict_ticker_frame is the contract (fail closed on a
non-integral, non-finite, bool or str lot, a series whose length is not the
date axis, a broker present in some fields but not others, nlot != blot - slot,
and |nval - (bval - sval)| > 0.5). It checks value by value in Python, which is
fine for one experiment build and too slow for a weekly run over ~1,000
tickers x 101 brokers x 6 fields x ~240 sessions (~150M calls). The copy below
checks the same things once per field matrix: the set of Python types present
is read in one C-level pass, and finiteness/integrality are numpy masks.
test_broker_book proves the two agree on real cache files and that every
malformed case still raises. The date axis, the OHLC domain and the
partial-broker guard are cheap (one pass over ~240 dates or ~101 codes), so
those are the strict helpers themselves, not copies.

It is stricter in two places, both on inputs the real cache never contains: a
date must be spelled YYYY-MM-DD (fromisoformat also takes "20250822"), and a
lot beyond 2**53 is refused because float64 can no longer hold it exactly.

WHY AN ANCHOR
-------------
Lots and prices must be on the same share basis for a cost to mean anything.
A date inside a measured basis regime (observed_basis_factor.json) or whose
market VWAP falls outside the day's range is flagged, and the book starts the
session after the last flag. The rolling features never use the anchor: they
only need no flag in the last 80 sessions, so they come out the same whether
computed live on day T or from a later fetch (§2.4).

WHY CUMSUM DIFFERENCES
----------------------
A rolling sum at T is C[T] - C[T-w] of a running total that starts at the
first session. The running total up to T depends only on sessions <= T, so the
value at T is bit-identical whether the payload ends at T or runs on for
months. That is the no-look-ahead property test_broker_rules checks.
"""

import json
import os
import re
from dataclasses import dataclass
from itertools import chain

import numpy as np
import pandas as pd

import build_inventory_db as bidb

HERE = os.path.dirname(os.path.abspath(__file__))
BASIS_FILE = os.path.join(HERE, "observed_basis_factor.json")

SHARES_PER_LOT = 100
VWAP_LO, VWAP_HI = 0.99, 1.01    # §2.3(b): market VWAP must sit in [low*0.99, high*1.01]
BASIS_LOOKBACK = 80              # §2.3: rolling features need no flag in the last 80 sessions
W_SHORT, W_LONG = 5, 60          # §4.1 rolling windows
ADV_WINDOW = 20                  # ADV20 / VAL20
RV_WINDOW, RV_MIN_VALID = 20, 15
BOOK_AVG_WINDOW = 20             # bavg20 / savg20
CURVE_TOP_N = 5

BROKER_COLS = ["date", "ticker", "broker", "nlot", "nval", "blot", "bval", "slot", "sval"]
OHLC_COLS = ["date", "ticker", "open", "high", "low", "close", "volume"]
BOOK_COLS = ["broker", "position_lots", "avg_cost", "realized_rp", "unrealized_rp",
             "total_rp", "pct_vs_cost", "gross_buy_lots", "gross_sell_lots",
             "turnover_rp", "net1d", "net5d", "bavg20", "savg20", "first_date"]

LOT_FIELDS = bidb.LOT_FIELDS
ALL_FIELDS = bidb.ALL_FIELDS
RUPIAH_TOLERANCE = bidb.RUPIAH_TOLERANCE
GROSS_FIELDS = ("blot", "slot", "bval", "sval")    # never negative; nlot/nval are signed
PRICE_KEYS = ("open", "high", "low", "close", "volume")
EXACT_LOT_LIMIT = 2 ** 53        # float64 holds every integer up to here exactly
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PayloadError(ValueError):
    """A payload outside the strict source contract. The ticker fails; nothing is repaired."""


# ── Payload -> frames ──────────────────────────────────────────────────────

def _strict(call, *args):
    """Run a build_inventory_db strict helper, re-raising its refusal as PayloadError."""
    try:
        return call(*args)
    except bidb.StrictSourceError as e:
        raise PayloadError(str(e)) from e


def _is_number_type(t):
    return issubclass(t, (int, float)) and not issubclass(t, bool)


def _first_bad(series, brokers, ok):
    """(broker, index, value) of the first value whose type fails ok(). Error path only."""
    for broker, values in zip(brokers, series):
        for i, v in enumerate(values):
            if not ok(v):
                return broker, i, v
    return None, None, None


def _field_matrix(field, src, brokers, n, ticker):
    """One field as a [n_brokers, n] matrix, validated without a per-value Python call.

    A broker absent from this field is only reachable when it is absent from
    EVERY field (partially_present_brokers has already refused the other case),
    so its row stays zero and the all-zero mask drops it, as in strict mode.

    The gross fields (blot, slot, bval, sval) must not be negative: a broker
    cannot buy or sell less than nothing. strict mode lets such a value
    through when nlot/nval stay consistent, and the book would then carry a
    negative trade into its average cost and return a silent NaN, which
    broker_profitability drops without a word. The net fields are signed.
    """
    is_lot = field in LOT_FIELDS
    rows, present = [], []
    for bi, broker in enumerate(brokers):
        series = src.get(broker)
        if series is None:
            continue
        if not isinstance(series, list):
            raise PayloadError(f"{ticker}/{broker}/{field}: {type(series).__name__}, not a list")
        if len(series) != n:
            raise PayloadError(
                f"{ticker}/{broker}/{field}: series length {len(series)} != {n} sessions; "
                "padding or truncating would invent data the source never asserted")
        rows.append(series)
        present.append(bi)
    matrix = np.zeros((len(brokers), n), dtype=np.int64 if is_lot else np.float64)
    if not rows:
        return matrix

    kind = "lot" if is_lot else "value"
    names = [brokers[i] for i in present]
    types = set(map(type, chain.from_iterable(rows)))
    if not all(_is_number_type(t) for t in types):
        broker, i, v = _first_bad(rows, names, lambda x: _is_number_type(type(x)))
        raise PayloadError(f"{ticker}/{broker}/{field}[{i}]: {type(v).__name__} is not a {kind} ({v!r})")
    try:
        values = np.array(rows, dtype=np.float64)
    except OverflowError:
        raise PayloadError(f"{ticker}/{field}: a {kind} too large for float64")
    finite = np.isfinite(values)
    if not finite.all():
        r, i = np.argwhere(~finite)[0]
        raise PayloadError(f"{ticker}/{names[r]}/{field}[{i}]: non-finite {kind} ({values[r, i]!r})")
    if field in GROSS_FIELDS and (values < 0).any():
        r, i = np.argwhere(values < 0)[0]
        raise PayloadError(f"{ticker}/{names[r]}/{field}[{i}]: negative gross {kind} "
                           f"({values[r, i]!r}); a buy or a sell cannot be below zero")
    if is_lot:
        if int in types and any(type(v) is int and abs(v) > EXACT_LOT_LIMIT
                                for v in chain.from_iterable(rows)):
            raise PayloadError(f"{ticker}/{field}: a lot beyond 2**53 cannot be held exactly")
        frac = values != np.rint(values)
        if frac.any():
            r, i = np.argwhere(frac)[0]
            raise PayloadError(f"{ticker}/{names[r]}/{field}[{i}]: non-integral lot ({values[r, i]!r})")
        if (np.abs(values) > EXACT_LOT_LIMIT).any():
            raise PayloadError(f"{ticker}/{field}: a lot beyond 2**53 cannot be held exactly")
        matrix[present] = values.astype(np.int64)
    else:
        matrix[present] = values
    return matrix


def _dates(data, ticker):
    dates = _strict(bidb.strict_dates, data.get("date") or [], ticker)
    bad = [d for d in dates if not ISO_DAY.match(d)]
    if bad:
        raise PayloadError(f"{ticker}: date {bad[0]!r} is not YYYY-MM-DD")
    return dates


def _ohlc_frame(data, dates, ticker):
    """OHLC rows paired 1:1, in order, with data.date. Missing prices stay NaN."""
    rows = data.get("ohlc")
    if rows is None:
        rows = []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise PayloadError(f"{ticker}: ohlc is not a list of rows")
    _strict(bidb.strict_ohlc_domain, {"ohlc": rows}, ticker)
    ohlc_dates = [r.get("date") for r in rows]
    if ohlc_dates != dates:
        # Broker series are paired to data.date by position and every rolling
        # feature is paired to the ohlc axis by position; a missing, extra or
        # reordered bar would silently shift one against the other.
        missing = sorted(set(dates) - set(ohlc_dates))
        extra = sorted(set(ohlc_dates) - set(dates))
        raise PayloadError(f"{ticker}: ohlc dates do not match data.date "
                           f"({len(rows)} bars vs {len(dates)} dates; missing {missing[:3]}, "
                           f"extra {extra[:3]})")
    cols = {"date": list(dates), "ticker": ticker}
    for key in PRICE_KEYS:
        cols[key] = np.array([np.nan if r.get(key) is None else r.get(key) for r in rows],
                             dtype=np.float64)
    return pd.DataFrame(cols, columns=OHLC_COLS)


def empty_brokers():
    frame = pd.DataFrame({c: pd.Series(dtype=object) for c in BROKER_COLS[:3]})
    for c in BROKER_COLS[3:]:
        frame[c] = pd.Series(dtype=np.int64 if c in LOT_FIELDS else np.float64)
    return frame[BROKER_COLS]


def frames_from_payload(data, ticker):
    """(brokers, ohlc) from one /api/inventory `data` dict, or PayloadError.

    `brokers` is row-for-row what build_inventory_db.strict_ticker_frame returns
    (broker-major, all-zero rows dropped), or an empty BROKER_COLS frame where
    strict mode returns None. `ohlc` is on the data.date axis, one bar per date.
    """
    if not isinstance(data, dict):
        raise PayloadError(f"{ticker}: payload is {type(data).__name__}, not a mapping")
    dates = _dates(data, ticker)
    n = len(dates)
    ohlc = _ohlc_frame(data, dates, ticker)

    present = {f: data.get(f) for f in ALL_FIELDS}
    for field, src in present.items():
        if src is not None and not isinstance(src, dict):
            raise PayloadError(f"{ticker}: {field} is {type(src).__name__}, not a mapping")
    brokers = sorted({b for src in present.values() if isinstance(src, dict) for b in src})
    if not brokers:
        return empty_brokers(), ohlc
    partial = bidb.partially_present_brokers(data)
    if partial:
        raise PayloadError(f"{ticker}: {len(partial)} broker(s) appear in some required fields "
                           f"but not all: {partial[:5]}. {bidb.ZERO_FILL_REFUSAL}")

    mats = {f: _field_matrix(f, present.get(f) or {}, brokers, n, ticker) for f in ALL_FIELDS}
    bad = mats["nlot"] != mats["blot"] - mats["slot"]
    if bad.any():
        raise PayloadError(f"{ticker}: {int(bad.sum())} rows violate nlot == blot - slot")
    drift = np.abs(mats["nval"] - (mats["bval"] - mats["sval"]))
    if (drift > RUPIAH_TOLERANCE).any():
        raise PayloadError(f"{ticker}: {int((drift > RUPIAH_TOLERANCE).sum())} rows violate "
                           f"nval == bval - sval beyond {RUPIAH_TOLERANCE} rupiah "
                           f"(worst {drift.max():g})")

    mask = np.zeros((len(brokers), n), dtype=bool)
    for f in ALL_FIELDS:
        mask |= mats[f] != 0
    mask = mask.ravel()
    if not mask.any():
        return empty_brokers(), ohlc
    frame = pd.DataFrame({
        "date": np.tile(np.asarray(dates, dtype=object), len(brokers))[mask],
        "ticker": ticker,
        "broker": np.repeat(np.asarray(brokers, dtype=object), n)[mask],
        **{f: mats[f].ravel()[mask] for f in ALL_FIELDS},
    })[BROKER_COLS]
    return frame, ohlc


# ── Basis flags and anchor (§2.3) ──────────────────────────────────────────

def load_basis_regimes(path=BASIS_FILE):
    """{ticker: [(first_date, last_date), ...]} from observed_basis_factor.json.

    Every classification counts: a regime means lots and price were measured on
    different share bases there, whatever was later decided about the ticker.
    A regime without both dates is refused rather than skipped, because skipping
    it would silently un-flag exactly the days it exists to flag.
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for reg in doc.get("regimes") or []:
        t, a, b = reg.get("ticker"), reg.get("regime_first_date"), reg.get("regime_last_date")
        if not (isinstance(t, str) and isinstance(a, str) and isinstance(b, str)):
            raise ValueError(f"{path}: regime without ticker/first/last date: {reg!r}")
        out.setdefault(t, []).append((a, b))
    return {t: sorted(v) for t, v in out.items()}


def _date_index(frame_dates, axis):
    idx = pd.Index(axis).get_indexer(frame_dates)
    if (idx < 0).any():
        raise ValueError("broker rows dated off the ohlc axis")
    return idx


def basis_flags(brokers, ohlc, regimes_for_ticker):
    """True where the session is basis-flagged: inside a regime, or VWAP outside the bar."""
    dates = ohlc["date"].tolist()
    axis = np.asarray(dates, dtype=object)
    flags = np.zeros(len(dates), dtype=bool)
    for first, last in regimes_for_ticker or []:
        flags |= (axis >= first) & (axis <= last)      # ISO strings order as dates

    di = _date_index(brokers["date"].to_numpy(), dates)
    lots = brokers["blot"].to_numpy(np.float64)
    # Rupiah on a broker-day with no round lot is an odd-lot fill (< 100 shares).
    # It has no lot to divide by, so counting it would lift the VWAP above the
    # bar on thin days and flag a basis break that is not there (INCI
    # 2025-09-19: 617.28 vs a 616.10 ceiling with it, 615.35 without).
    rupiah = np.where(lots > 0, brokers["bval"].to_numpy(np.float64), 0.0)
    blot = np.bincount(di, weights=lots, minlength=len(dates))
    bval = np.bincount(di, weights=rupiah, minlength=len(dates))
    low = ohlc["low"].to_numpy(np.float64)
    high = ohlc["high"].to_numpy(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = bval / (blot * SHARES_PER_LOT)
        checked = (blot > 0) & np.isfinite(low) & np.isfinite(high) & (low > 0) & (high > 0)
        outside = (vwap < low * VWAP_LO) | (vwap > high * VWAP_HI) | ~np.isfinite(vwap)
    flags |= checked & outside
    return pd.Series(flags, index=pd.Index(dates, name="date"), name="basis_flag")


def anchor_after_flags(dates, flags):
    """First book session: dates[0] if clean, the day after the last flag, None if T itself is flagged."""
    f = np.asarray(flags, dtype=bool)
    if not f.any():
        return dates[0]
    last = int(np.flatnonzero(f)[-1])
    return None if last == len(dates) - 1 else dates[last + 1]


# ── Anchored average-cost book (§3) ────────────────────────────────────────

def _trade(pos, cost, realized, lots, price):
    """Apply a signed residual trade to (position, avg cost, realized)."""
    if pos == 0:
        return lots, price, realized
    if (pos > 0) == (lots > 0):
        a, b = abs(pos), abs(lots)
        return pos + lots, (a * cost + b * price) / (a + b), realized
    q = min(abs(lots), abs(pos))
    sign = 1 if pos > 0 else -1
    realized += q * SHARES_PER_LOT * (price - cost) * sign
    rest = abs(lots) - q
    if rest > 0:                                    # sign flip: cost resets
        return (rest if lots > 0 else -rest), price, realized
    pos += lots
    return pos, (cost if pos != 0 else np.nan), realized


def average_cost_run(blot, bval, slot, sval):
    """(position_lots, avg_cost, realized_rp) after processing the days in order.

    Rupiah traded with no lots on that side (odd-lot fills under 100 shares:
    1,443 buy-side and 1,033 sell-side rows in the 2025-08..2026-08 cache, all
    under Rp 0.5 M) cannot enter a lot position, so it is booked straight to
    realized as cash. That keeps total == sum(nlot)*100*close - sum(nval) exact.
    """
    pos, cost, realized = 0, np.nan, 0.0
    for b, bv, s, sv in zip(blot, bval, slot, sval):
        pb = bv / (b * SHARES_PER_LOT) if b > 0 else np.nan
        ps = sv / (s * SHARES_PER_LOT) if s > 0 else np.nan
        m = min(b, s)
        if m > 0:
            realized += m * SHARES_PER_LOT * (ps - pb)
        if b == 0:
            realized -= bv
        if s == 0:
            realized += sv
        n = b - s
        if n > 0:
            pos, cost, realized = _trade(pos, cost, realized, n, pb)
        elif n < 0:
            pos, cost, realized = _trade(pos, cost, realized, n, ps)
    return int(pos), (float(cost) if pos != 0 else np.nan), float(realized)


def _vwap(lots, values):
    """Σvalue/(Σlots*100) over rows that traded lots; NaN when there are none."""
    keep = lots > 0
    total = lots[keep].sum()
    return float(values[keep].sum() / (total * SHARES_PER_LOT)) if total > 0 else np.nan


def average_cost_book(brokers, ohlc, anchor):
    """One row per broker that traded since the anchor (spec §3), largest position first."""
    axis = ohlc.loc[ohlc["date"] >= anchor, "date"].tolist()
    rows = brokers[brokers["date"] >= anchor]
    if rows.empty or not axis:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in BOOK_COLS})
    close_last = float(ohlc["close"].iloc[-1])
    last5 = set(axis[-W_SHORT:])
    last20 = set(axis[-BOOK_AVG_WINDOW:])
    last1 = axis[-1]
    rows = rows.sort_values(["broker", "date"], kind="stable")
    out = []
    for broker, g in rows.groupby("broker", sort=True):
        blot, slot = g["blot"].to_numpy(np.int64), g["slot"].to_numpy(np.int64)
        bval, sval = g["bval"].to_numpy(np.float64), g["sval"].to_numpy(np.float64)
        pos, cost, realized = average_cost_run(blot.tolist(), bval.tolist(),
                                               slot.tolist(), sval.tolist())
        unrealized = pos * SHARES_PER_LOT * (close_last - cost) if pos != 0 else 0.0
        d = g["date"].to_numpy()
        nlot = g["nlot"].to_numpy(np.int64)
        in20 = np.isin(d, list(last20))
        out.append({
            "broker": broker,
            "position_lots": pos,
            "avg_cost": cost,
            "realized_rp": realized,
            "unrealized_rp": float(unrealized),
            "total_rp": realized + float(unrealized),
            "pct_vs_cost": close_last / cost - 1 if pos != 0 and cost > 0 else np.nan,
            "gross_buy_lots": int(blot.sum()),
            "gross_sell_lots": int(slot.sum()),
            "turnover_rp": float(bval.sum() + sval.sum()),
            "net1d": int(nlot[d == last1].sum()),
            "net5d": int(nlot[np.isin(d, list(last5))].sum()),
            "bavg20": _vwap(blot[in20], bval[in20]),
            "savg20": _vwap(slot[in20], sval[in20]),
            "first_date": d[0],
        })
    book = pd.DataFrame(out, columns=BOOK_COLS)
    book["_neg"] = -book["position_lots"]
    book = book.sort_values(["_neg", "broker"], kind="stable").drop(columns="_neg")
    return book.reset_index(drop=True)


def _clean(x):
    return None if x is None or not np.isfinite(x) else float(x)


def cumulative_curves(brokers, ohlc, anchor, top_n=CURVE_TOP_N):
    """Cumulative net lots since the anchor for the top net buyers and sellers, plus close."""
    axis = ohlc.loc[ohlc["date"] >= anchor, "date"].tolist()
    closes = ohlc.loc[ohlc["date"] >= anchor, "close"].tolist()
    out = {"dates": axis, "close": [_clean(c) for c in closes], "series": []}
    rows = brokers[brokers["date"] >= anchor]
    if rows.empty or not axis:
        return out
    codes = sorted(rows["broker"].unique())
    bi = np.searchsorted(codes, rows["broker"].to_numpy())
    di = _date_index(rows["date"].to_numpy(), axis)
    daily = np.zeros((len(codes), len(axis)), dtype=np.int64)
    np.add.at(daily, (bi, di), rows["nlot"].to_numpy(np.int64))
    cum = np.cumsum(daily, axis=1)
    final = cum[:, -1]
    buyers = sorted((i for i in range(len(codes)) if final[i] > 0), key=lambda i: (-final[i], codes[i]))
    sellers = sorted((i for i in range(len(codes)) if final[i] < 0), key=lambda i: (final[i], codes[i]))
    for side, picks in (("buy", buyers[:top_n]), ("sell", sellers[:top_n])):
        for i in picks:
            out["series"].append({"broker": codes[i], "side": side,
                                  "cum_lots": [int(v) for v in cum[i]]})
    return out


# ── Rolling per-session state (§4.1) ───────────────────────────────────────

@dataclass
class RollingState:
    ticker: str
    dates: list
    brokers: list
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    NL5: np.ndarray
    NV5: np.ndarray
    BL5: np.ndarray
    SL5: np.ndarray
    NL60: np.ndarray
    NV60: np.ndarray
    BL60: np.ndarray
    SL60: np.ndarray
    adv20: np.ndarray
    val20: np.ndarray
    low5: np.ndarray
    high5: np.ndarray
    low60: np.ndarray
    high60: np.ndarray
    n_sessions: np.ndarray
    basis_ok80: np.ndarray
    rv20: np.ndarray
    BUYDAYS60: np.ndarray
    close_lag59: np.ndarray


def rolling_sum(matrix, w):
    """Sum over the last w sessions including T, along the last axis; NaN before w sessions exist.

    int64 input stays exact through the cumsum and is cast to float only at the
    end, so lot sums are exact integers in float64.
    """
    m = np.asarray(matrix)
    n = m.shape[-1]
    out = np.full(m.shape, np.nan)
    if n < w:
        return out
    zero = np.zeros(m.shape[:-1] + (1,), dtype=m.dtype)
    c = np.concatenate([zero, np.cumsum(m, axis=-1)], axis=-1)
    out[..., w - 1:] = c[..., w:] - c[..., :n - w + 1]
    return out


def _rolling_extreme(values, w, how):
    s = pd.Series(values, dtype=np.float64).rolling(w, min_periods=w)
    return (s.min() if how == "min" else s.max()).to_numpy(np.float64)


def rolling_state(brokers, ohlc, flags):
    """Every §4.1 feature for every session of one ticker, each using only sessions <= T."""
    dates = ohlc["date"].tolist()
    n = len(dates)
    codes = sorted(brokers["broker"].unique().tolist()) if len(brokers) else []
    nb = len(codes)
    shape = (nb, n)
    mats = {f: np.zeros(shape, dtype=np.int64 if f in LOT_FIELDS else np.float64) for f in ALL_FIELDS}
    if nb:
        bi = np.searchsorted(codes, brokers["broker"].to_numpy())
        di = _date_index(brokers["date"].to_numpy(), dates)
        for f in ALL_FIELDS:
            mats[f][bi, di] = brokers[f].to_numpy()

    px = {k: ohlc[k].to_numpy(np.float64) for k in ("open", "high", "low", "close")}
    close = px["close"]
    tot_blot = mats["blot"].sum(axis=0)
    tot_bval = mats["bval"].sum(axis=0)

    flag = np.asarray(flags, dtype=bool).astype(np.int64)
    if len(flag) != n:
        raise ValueError("flags are not on the ohlc axis")
    cf = np.concatenate([[0], np.cumsum(flag)])
    t = np.arange(n)
    recent_flags = cf[t + 1] - cf[np.maximum(0, t + 1 - BASIS_LOOKBACK)]

    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.full(n, np.nan)
        ret[1:] = close[1:] / close[:-1] - 1
    ret[~np.isfinite(ret)] = np.nan
    rv20 = pd.Series(ret).rolling(RV_WINDOW, min_periods=RV_MIN_VALID).std().to_numpy(np.float64)

    lag = W_LONG - 1
    close_lag59 = np.full(n, np.nan)
    if n > lag:
        close_lag59[lag:] = close[:n - lag]

    return RollingState(
        ticker=str(ohlc["ticker"].iloc[0]) if n else "",
        dates=dates, brokers=codes,
        open=px["open"], high=px["high"], low=px["low"], close=close,
        NL5=rolling_sum(mats["nlot"], W_SHORT), NV5=rolling_sum(mats["nval"], W_SHORT),
        BL5=rolling_sum(mats["blot"], W_SHORT), SL5=rolling_sum(mats["slot"], W_SHORT),
        NL60=rolling_sum(mats["nlot"], W_LONG), NV60=rolling_sum(mats["nval"], W_LONG),
        BL60=rolling_sum(mats["blot"], W_LONG), SL60=rolling_sum(mats["slot"], W_LONG),
        adv20=rolling_sum(tot_blot, ADV_WINDOW) / ADV_WINDOW,
        val20=rolling_sum(tot_bval, ADV_WINDOW) / ADV_WINDOW,
        low5=_rolling_extreme(px["low"], W_SHORT, "min"),
        high5=_rolling_extreme(px["high"], W_SHORT, "max"),
        low60=_rolling_extreme(px["low"], W_LONG, "min"),
        high60=_rolling_extreme(px["high"], W_LONG, "max"),
        n_sessions=np.arange(1, n + 1, dtype=np.int64),
        basis_ok80=recent_flags == 0,
        rv20=rv20,
        BUYDAYS60=rolling_sum((mats["nlot"] > 0).astype(np.int64), W_LONG),
        close_lag59=close_lag59,
    )


# ── One ticker, end to end ─────────────────────────────────────────────────

def ticker_bundle(data, ticker, regimes):
    """Frames, flags, anchor, book, curves and rolling state for one payload."""
    brokers, ohlc = frames_from_payload(data, ticker)
    flags = basis_flags(brokers, ohlc, (regimes or {}).get(ticker, []))
    dates = ohlc["date"].tolist()
    anchor = anchor_after_flags(dates, flags)
    book = curves = None
    if anchor is not None:
        book = average_cost_book(brokers, ohlc, anchor)
        curves = cumulative_curves(brokers, ohlc, anchor)
    return {
        "ticker": ticker, "brokers": brokers, "ohlc": ohlc, "flags": flags,
        "anchor": anchor, "basis_ok": anchor is not None,
        "book": book, "curves": curves,
        "state": rolling_state(brokers, ohlc, flags.to_numpy()),
    }
