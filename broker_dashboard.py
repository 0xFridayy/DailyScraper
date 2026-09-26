"""Broker learning dashboard: one static HTML page per run, sent to a private
Telegram chat as a document (BROKER_LEARNING.md §6).

WHY A FILE, NOT A MESSAGE
-------------------------
A book table and a chart per ticker plus a rule scorecard do not fit a
4,096-character Telegram message. A document keeps the numbers together and
opens offline on a phone; the caption (<= 1,024 chars) carries the headline.

WHY SELF-CONTAINED
------------------
The repo is public, so the page is never committed (broker_dashboard_out/ is
ignored). It must also leak nothing when opened: a CDN script, a web font or
an image URL would be a request from the reader's phone announcing when the
private report was read. CSS, SVG and the small hover script are inline, and
render() output contains no URL at all (inline SVG needs no xmlns).

WHAT THE PAGE MUST NOT CLAIM (spec §8)
-------------------------------------
Positions, costs and P/L are observable broker inventory, not beneficial
ownership. Rule statuses are research labels; nothing is "proven". So the
disclaimer is on the page twice, every hit rate sits beside its base rate,
every status carries its spec label plus an explanation, and a missing number
prints as "—", never as 0.

WHY TWO CHART PANELS
--------------------
Price and cumulative net lots have unrelated scales. Two y-axes on one plot
invent a correlation wherever the scales happen to line up, so the chart
stacks a price panel over a net-lot panel on one shared date axis.

WHY THE ALPHA CASES CARRY SO MUCH WARNING TEXT
----------------------------------------------
A "kasus alpha" is picked AFTER the stock rose +50% in 60 sessions, so any
pattern in its snapshot is hindsight until it is compared with every stock
that looked the same that day. The page says so under the cases and points
at the h = 60 big-move rate vs its base rate in the scorecard, which is that
comparison done properly. Broker lift gets the same treatment, plus the
reminder that a broker code is a securities firm with many clients.

WHY EVERY h = 60 NUMBER CARRIES A SUSPENSION SHARE (Amendment A2)
-----------------------------------------------------------------
At h = 60 the return is the holder's (broker_learning.holder_returns): a
suspension inside the window is bridged, because the stocks that run hardest
are the ones IDX suspends. A bridged window is real but different: the money
is frozen and the reopening can go either way. So every h = 60 number on the
page sits beside "kena suspensi X%", and the scorecard says in Indonesian why
the h = 60 verdicts read "data belum cukup" for now: a CI needs 3 blocks of
60 event dates (180), and the API serves one year.

WHY THE PAGE IS KEPT UNDER 1 MB
-------------------------------
It travels as a Telegram document and opens on a phone. The first integration
page was 2.7 MB for 46 tickers, almost all of it chart: ~20 KB of hover JSON
and ~20 KB of path data per card, each session printed as "L123.4,56.7". The
path now drops points within PATH_EPS of the line through their neighbours
(invisible at this size) and is written in short relative steps; the hover
data is thinned to ~HOVER_POINTS sessions (about weekly over a year) with
integer values and dates as day offsets; and the chart's table twin keeps
~TABLE_ROWS rows (about monthly) of plain signed text. The book table and
the chart itself are unchanged.

WHY THE RULE CONSTANTS ARE MIRRORED, NOT IMPORTED
-------------------------------------------------
HORIZONS, PRIMARY_H, BIG, MIN_VAL20 and the LOW_N bar repeat broker_learning /
broker_rules so that
rendering a stored run needs nothing but the ctx; a rule dict's own
"primary_h" wins when the orchestrator supplies it.

Pure: standard library only. requests is imported lazily inside
send_document(), so check_ml_health.py can import and test this module
without secrets or network.
"""

import json
import math
import os
import re
import time
from datetime import date
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "broker_dashboard_out")
CAPTION_LIMIT = 1024
DISCLAIMER = ("Research-grade. Inventory broker yang terlihat, bukan kepemilikan sebenarnya. "
              "Bukan saran beli/jual. Status aturan adalah label riset, tidak ada yang 'terbukti'.")

HORIZONS = (5, 10, 20, 60)      # mirrors broker_learning.HORIZONS
PRIMARY_H = {"R1": 10, "R2": 10, "R3": 10, "R4": 10, "R5": 10, "R6": 60}   # broker_rules.PRIMARY_H
DEFAULT_PRIMARY_H = 10
BIG = {5: 0.15, 10: 0.20, 20: 0.30, 60: 0.50}   # broker_learning.BIG: a "big move" per horizon
ALPHA_H = 60                    # broker_learning.ALPHA_H / ALPHA_MIN: what a case is
ALPHA_MIN = 0.50
HOLD_H = 60                     # broker_learning.HOLD_H: h = 60 is the holder's return
VISIBLE_MIN = 80                # broker_learning.VISIBLE_MIN: a case with fewer sessions before T is invisible
MIN_VAL20 = 2e8                 # broker_rules.MIN_VAL20 (Amendment A2): Rp 200 juta/day
LOW_N_DATES, LOW_N_BLOCKS = 30, 3   # broker_learning: LOW_N = n_dates < max(30, 3h)
ROUND_TRIP_COST = 0.0073          # broker_learning.ROUND_TRIP_COST (transaction_cost_model moderate)
PATH_EPS = 0.35                 # viewBox units (~0.4 px on a phone) a dropped point may sit off the line
HOVER_POINTS = 50               # at most ~this many sessions in hover data: weekly over a year
TABLE_ROWS = 12                 # at most ~this many rows in a chart's table twin
ALPHA_ROWS = 20
LIFT_ROWS = 15
LIFT_MIN_CASES = 3              # a lift from one or two cases is noise, not a pattern
LIVE_H_DEFAULT = 10             # broker_learning_db.live_summary(h=10)
BROKER_H = 10                   # leaderboard horizon: the primary h of R1-R5
SEND_TRIES = 3
TRANSIENT_WAITS = (5, 15)       # seconds before try 2 and 3 after a network error or a 5xx
RETRY_AFTER_DEFAULT = 5         # seconds, when a 429 carries no usable retry_after
RETRY_AFTER_CAP = 30            # a cron job should not sit longer than this on one 429
CHART_PER_SIDE = 3              # lines per side on the chart; the book table has the rest
LEADERBOARD_ROWS = 10
CAPTION_ITEM_MAX = 80

MINUS = "−"
MISSING = "—"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")

# Spec §4.5 status table. None of these means "proven".
STATUS_LABEL = {
    "LOW_N": "data belum cukup",
    "CONSISTENT": "konsisten (retro), belum terbukti",
    "CONTRARY": "berlawanan dengan dugaan",
    "DIRECTIONAL": "searah, belum robust",
    "NEUTRAL": "tidak ada edge terlihat",
}
STATUS_NOTE = {
    "LOW_N": "Tanggal dengan event kurang dari max(30, 3×h), jadi h=60 butuh 180, atau CI "
             "belum bisa dihitung: angka apa pun belum bermakna.",
    "CONSISTENT": "Seluruh interval kepercayaan 95% searah dugaan pada evaluasi retrospektif "
                  "di horizon utama aturan (h=10; R6 h=60). Tetap label riset: bukan "
                  "point-in-time, belum terbukti.",
    "CONTRARY": "Seluruh interval kepercayaan 95% berlawanan dengan arah yang diduga.",
    "DIRECTIONAL": "Rata-rata excess searah dugaan, tetapi interval kepercayaan melewati nol.",
    "NEUTRAL": "Rata-rata excess tidak searah dugaan dan interval melewati nol.",
}
STATUS_ICON = {"LOW_N": "◌", "CONSISTENT": "●", "CONTRARY": "⊘",
               "DIRECTIONAL": "◐", "NEUTRAL": "○"}
UNRATED = ("belum dinilai", "Belum ada statistik retrospektif untuk aturan ini.")
RUN_KIND = {"daily": "harian", "weekly": "mingguan", "render": "render ulang"}
SIDE_LABEL = {"buy": "beli", "sell": "jual"}


# ── number formatting (Indonesian: '.' groups thousands, ',' is the decimal mark) ──

def _num(x):
    """float, or None when the value is missing. Missing stays missing: it is
    printed as MISSING, never coerced to 0."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _digits(a, digits, strip=False):
    s = f"{abs(a):,.{digits}f}"
    if strip and "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.translate(str.maketrans(",.", ".,"))


def _sign(rounded, signed):
    # decided on the ROUNDED value, so a tiny negative never prints as "−0"
    return MINUS if rounded < 0 else ("+" if signed and rounded > 0 else "")


def _round_int(v):
    return int(math.copysign(math.floor(abs(v) + 0.5), v))


def fmt_int(x, signed=False):
    """117140 -> '117.140'; -33419 -> '−33.419'."""
    v = _num(x)
    if v is None:
        return MISSING
    r = _round_int(v)
    return _sign(r, signed) + _digits(r, 0)


def fmt_num(x, digits=2, signed=False):
    """1.07 -> '1,07'; signed -1 -> '−1,00'."""
    v = _num(x)
    if v is None:
        return MISSING
    r = round(v, digits)
    return _sign(r, signed) + _digits(r, digits)


def fmt_pct(x, digits=1, signed=False):
    """A fraction as percent: 0.052 -> '5,2%' (signed: '+5,2%')."""
    v = _num(x)
    return MISSING if v is None else fmt_num(v * 100, digits, signed) + "%"


def fmt_pp(x, digits=1):
    """A difference of two rates, in percentage points: 0.07 -> '+7,0 pp'."""
    v = _num(x)
    return MISSING if v is None else fmt_num(v * 100, digits, True) + " pp"


def fmt_lot(x, signed=False):
    """117140 -> '117.140 lot'."""
    s = fmt_int(x, signed)
    return s if s == MISSING else s + " lot"


def fmt_rp(x, signed=False):
    """A per-share price: 12077 -> 'Rp 12.077'."""
    v = _num(x)
    if v is None:
        return MISSING
    r = _round_int(v)
    return f"{_sign(r, signed)}Rp {_digits(r, 0)}"


_RP_SCALES = ((1e12, "T"), (1e9, "M"), (1e6, "jt"))     # triliun, miliar, juta


def fmt_rp_big(x, signed=False):
    """A Rupiah amount, compact: 1.23e9 -> 'Rp 1,23 M'; 161.8e9 -> 'Rp 161,8 M';
    1.2e12 -> 'Rp 1,2 T'. Two decimals under 10 units, one above, trailing
    zeros dropped. Below Rp 1 jt the full amount is printed."""
    v = _num(x)
    if v is None:
        return MISSING
    a = abs(v)
    if _round_int(a) >= _RP_SCALES[-1][0]:    # round first: 999.999,7 is 'Rp 1 jt'
        a = max(a, _RP_SCALES[-1][0])
    body = None
    for i, (scale, suffix) in enumerate(_RP_SCALES):
        if a >= scale:
            q = a / scale
            if i > 0 and q >= 999.95:            # would print '1.000 M': say '1 T'
                scale, suffix = _RP_SCALES[i - 1]
                q = a / scale
            body = f"{_digits(q, 2 if q < 10 else 1, strip=True)} {suffix}"
            break
    if body is None:
        r = _round_int(a)
        if r == 0:
            return "Rp 0"
        body = _digits(r, 0)
    return f"{_sign(v, signed)}Rp {body}"


def _lots_compact(x):
    """Chart labels have ~70px: 117140 -> '+117,1rb' (ribu), 1.25e6 -> '+1,25jt'."""
    v = _num(x)
    if v is None:
        return MISSING
    a = abs(v)
    # Round first, then pick the unit: 999.960 would print '1.000rb' and
    # 999,6 '1.000' if the unit were chosen on the unrounded value.
    if round(a / 1e3, 1) >= 1e3:
        body = _digits(a / 1e6, 2, strip=True) + "jt"
    elif _round_int(a) >= 1e3:
        body = _digits(a / 1e3, 1, strip=True) + "rb"
    else:
        body = _digits(_round_int(a), 0)
    if body == "0":
        return body
    return (MINUS if v < 0 else "+") + body


# ── small html helpers ──────────────────────────────────────────────────────

def _d(x):
    return x if isinstance(x, dict) else {}


def _l(x):
    return x if isinstance(x, (list, tuple)) else []


def _s(x):
    return "" if x is None else str(x)


def _e(x):
    return escape(_s(x), quote=True)


def _signed(text):
    """Colour AND sign. The class is read off the printed sign, so the colour
    can never disagree with the number, and never carries meaning alone."""
    if text == MISSING:
        return f'<span class="na">{MISSING}</span>'
    cls = "pos" if text.startswith("+") else "neg" if text.startswith(MINUS) else "zero"
    return f'<span class="{cls}">{_e(text)}</span>'


def _table(head, rows, cls=""):
    """Every table sits in its own horizontal scroller, so a wide book table
    never widens the page on a 375px phone. Cells are pre-escaped HTML, or
    (html, css_class) for a left-aligned text column."""
    th = "".join(f'<th scope="col">{h}</th>' for h in head)
    body = []
    for row in rows:
        tds = []
        for cell in row:
            if isinstance(cell, tuple):
                tds.append(f'<td class="{cell[1]}">{cell[0]}</td>')
            else:
                tds.append(f"<td>{cell}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    klass = f' class="{cls}"' if cls else ""
    return (f'<div class="tw"><table{klass}><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _status(code):
    code = _s(code)
    label, note = (STATUS_LABEL[code], STATUS_NOTE[code]) if code in STATUS_LABEL else UNRATED
    icon = STATUS_ICON.get(code, "·")
    return (f'<span class="st" data-status="{_e(code)}" title="{_e(note)}">'
            f'<span aria-hidden="true">{icon}</span> {_e(label)}</span>')


def _hit_base(hit, base):
    """The only way a hit rate reaches the page: always with its base rate."""
    return (f'<span class="hb">hit {_e(fmt_pct(hit, 0))} '
            f'<span class="vs">vs</span> base {_e(fmt_pct(base, 0))}</span>')


def _big_base(big, base, h):
    """Big-move rate beside its base rate, with the threshold that defines
    "big" at this horizon, so +50% at h=60 is never read as +15% at h=5."""
    thr = BIG.get(h)
    thr_txt = f" (≥ {fmt_pct(thr, 0, True)})" if thr is not None else ""
    return (f'<span class="hb">{_e(fmt_pct(big, 1))} <span class="vs">vs</span> '
            f'rata-rata {_e(fmt_pct(base, 1))}{_e(thr_txt)}</span>')


def _susp(rate):
    """The suspension share printed beside an h = 60 number (Amendment A2)."""
    return f'<span class="sp">kena suspensi {_e(fmt_pct(rate, 0))}</span>'


def _low_n_min(h):
    return max(LOW_N_DATES, LOW_N_BLOCKS * int(h))


def _primary_h(rule):
    """The rule's primary horizon: its own "primary_h" if the ctx carries a
    valid one, else the ruleset v1 table (R6 is judged at h=60)."""
    r = _d(rule)
    h = _num(r.get("primary_h"))
    if h is not None and int(h) in HORIZONS:
        return int(h)
    return PRIMARY_H.get(_s(r.get("id")), DEFAULT_PRIMARY_H)


def _ci(lo, hi):
    if _num(lo) is None and _num(hi) is None:
        return MISSING
    return f"[{_e(fmt_pct(lo, 2, True))}; {_e(fmt_pct(hi, 2, True))}]"


def _dir_label(direction):
    d = _num(direction)
    if d is None or d == 0:
        return ""
    return ('<span class="dir" title="arah excess yang diduga (preregistrasi)">↑ dugaan naik</span>'
            if d > 0 else
            '<span class="dir" title="arah excess yang diduga (preregistrasi)">↓ dugaan turun</span>')


def _date_long(s):
    s = _s(s)
    if not DATE_RE.fullmatch(s):
        return s or MISSING
    y, m, d = s.split("-")
    return f"{int(d)} {MONTHS[int(m) - 1]} {y}" if 1 <= int(m) <= 12 else s


def _date_tick(s, day=False):
    """'2026-07-31' -> "Jul ’26", or "31 Jul" when month ticks would repeat
    (a chart shorter than a few months)."""
    s = _s(s)
    if not DATE_RE.fullmatch(s) or not 1 <= int(s[5:7]) <= 12:
        return s[:10]
    mon = MONTHS[int(s[5:7]) - 1]
    return f"{int(s[8:10])} {mon}" if day else f"{mon} ’{s[2:4]}"


def _anchor_id(ticker):
    return "t-" + re.sub(r"[^A-Za-z0-9_-]", "_", _s(ticker))


def _fired(t):
    return [str(r) for r in _l(t.get("fired")) if isinstance(r, str) and r]


def _ordered(tickers):
    """Fired tickers first, strongest composite score first; then the rest by
    name. The caption and the page use the same order."""
    def key(t):
        score = _num(t.get("score"))
        return (0 if _fired(t) else 1, -abs(score) if score is not None else 0.0,
                _s(t.get("ticker")))
    return sorted(tickers, key=key)


# ── charts ──────────────────────────────────────────────────────────────────

_W, _H = 340, 284                  # viewBox; ~1:1 with CSS px on a 375px phone
_X0, _X1 = 46, 266                 # plot x range; the right 74 units hold end labels
_PT = (22, 104)                    # price panel (top, bottom)
_LT = (136, 262)                   # net-lot panel


def _nice_ticks(lo, hi, intervals=3):
    """Round tick values (step 1/2/2.5/5 x 10^k) covering [lo, hi]."""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        pad = abs(lo) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / intervals
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start, end = math.floor(lo / step) * step, math.ceil(hi / step) * step
    return [start + k * step for k in range(int(round((end - start) / step)) + 1)]


def _yscale(lo, hi, top, bottom):
    span = (hi - lo) or 1.0
    return lambda v: bottom - (v - lo) / span * (bottom - top)


def _keep(pts, eps=PATH_EPS):
    """Ramer-Douglas-Peucker: indices of the points that matter to the drawn
    line. A dropped point sits within `eps` viewBox units of the segment that
    replaces it, i.e. under a third of a pixel on a phone, so the line looks
    the same while a smooth cumulative-lot curve loses most of its points."""
    keep = {0, len(pts) - 1}
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1.0
        far, at = -1.0, None
        for i in range(a + 1, b):
            px, py = pts[i]
            dist = abs(dy * (px - ax) - dx * (py - ay)) / norm
            if dist > far:
                far, at = dist, i
        if at is not None and far > eps:
            keep.add(at)
            stack += [(a, at), (at, b)]
    return sorted(keep)


def _tenths(t):
    """An integer count of tenths as the shortest SVG number: 9 -> '.9',
    -2 -> '-.2', 10 -> '1', -125 -> '-12.5'."""
    sign, t = ("-" if t < 0 else ""), abs(t)
    whole, frac = divmod(t, 10)
    if not frac:
        return f"{sign}{whole}"
    return f"{sign}{whole or ''}.{frac}"


def _run_d(run):
    """One pen-down run as 'M x,y l dx dy ...': relative steps in tenths of a
    unit, computed from ROUNDED absolute positions so rounding never drifts."""
    pts = [(round(px * 10), round(py * 10)) for px, py in (run[i] for i in _keep(run))]
    out = [f"M{_tenths(pts[0][0])},{_tenths(pts[0][1])}l"]
    prev = None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        for tok in (_tenths(x1 - x0), _tenths(y1 - y0)):
            out.append(tok if prev is None or tok[0] == "-" else " " + tok)
            prev = tok
    return "".join(out)


def _path(xs, vals, y):
    """SVG path data with the pen lifted over missing values, plus the points
    with no finite neighbour: a path cannot draw those, so they become dots
    (a one-point series would otherwise vanish)."""
    segs, run, lone = [], [], []

    def flush():
        if len(run) == 1:
            lone.append(run[0])
        elif run:
            segs.append(_run_d(run))
        run.clear()

    for px, v in zip(xs, vals):
        if v is None:
            flush()
        else:
            run.append((px, y(v)))
    flush()
    return " ".join(segs), lone


def _spread(targets, top, bottom, gap=15.0):
    """Direct labels keep their line's height unless two would overlap. The
    caller draws a leader line for any label that moved, instead of leaving
    it floating away from its line."""
    order = sorted(range(len(targets)), key=lambda i: targets[i])
    ys = [targets[i] for i in order]
    for k in range(1, len(ys)):
        ys[k] = max(ys[k], ys[k - 1] + gap)
    if ys and ys[-1] > bottom:
        ys[-1] = bottom
        for k in range(len(ys) - 2, -1, -1):
            ys[k] = min(ys[k], ys[k + 1] - gap)
    out = [0.0] * len(targets)
    for k, i in enumerate(order):
        out[i] = max(ys[k], top)
    return out


def _last_point(xs, vals):
    for px, v in zip(reversed(xs), reversed(vals)):
        if v is not None:
            return px, v
    return None


def _pick_series(raw, n):
    """Top CHART_PER_SIDE buyers, then sellers, by final cumulative position.
    Colour slots go 1..N in that order with no gaps: the order the palette's
    adjacent pairs were validated in."""
    buy, sell = [], []
    for s in raw:
        s = _d(s)
        vals = [_num(v) for v in _l(s.get("cum_lots"))][:n]
        vals += [None] * (n - len(vals))
        last = next((v for v in reversed(vals) if v is not None), None)
        if last is None:
            continue
        side = s.get("side") if s.get("side") in ("buy", "sell") else ("buy" if last >= 0 else "sell")
        (buy if side == "buy" else sell).append(
            {"broker": _s(s.get("broker")), "side": side, "vals": vals, "last": last})
    buy.sort(key=lambda s: (-s["last"], s["broker"]))
    sell.sort(key=lambda s: (s["last"], s["broker"]))
    picked = buy[:CHART_PER_SIDE] + sell[:CHART_PER_SIDE]
    for k, s in enumerate(picked, 1):
        s["slot"] = k
    return picked


def _chart(curves, ticker, anchor=None):
    """Price panel over a cumulative-net-lot panel, one shared date axis.
    Returns "" when there is nothing to draw."""
    c = _d(curves)
    dates = [_s(x) for x in _l(c.get("dates"))]
    n = len(dates)
    if n == 0:
        return ""
    close = [_num(v) for v in _l(c.get("close"))][:n]
    close += [None] * (n - len(close))
    series = _pick_series(_l(c.get("series")), n)
    prices = [v for v in close if v is not None]
    if not prices and not series:
        return ""
    xs = [_X0 + (_X1 - _X0) * i / (n - 1) if n > 1 else (_X0 + _X1) / 2 for i in range(n)]
    g = []

    g.append(f'<text class="ttl" x="{_X0}" y="{_PT[0] - 9}">Harga penutupan (Rp)</text>')
    if prices:
        ticks = _nice_ticks(min(prices), max(prices), 2)
        y = _yscale(ticks[0], ticks[-1], *_PT)
        for t in ticks:
            g.append(f'<line class="gl" x1="{_X0}" x2="{_X1}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                     f'<text class="tk" x="{_X0 - 5}" y="{y(t):.1f}" dy=".32em" '
                     f'text-anchor="end">{_e(fmt_int(t))}</text>')
        d, lone = _path(xs, close, y)
        if d:
            g.append(f'<path class="ln px" d="{d}"/>')
        for px, py in lone:
            g.append(f'<circle class="px-d" cx="{px:.1f}" cy="{py:.1f}" r="2"/>')
        px, pv = _last_point(xs, close)
        g.append(f'<circle class="dot px-d" cx="{px:.1f}" cy="{y(pv):.1f}" r="4"/>'
                 f'<text class="lbl" x="{_X1 + 6}" y="{y(pv):.1f}" dy=".32em">'
                 f'{_e(fmt_int(pv))}</text>')
    else:
        g.append(f'<text class="tk" x="{_X0}" y="{(_PT[0] + _PT[1]) / 2:.0f}">harga tidak tersedia</text>')

    g.append(f'<text class="ttl" x="{_X0}" y="{_LT[0] - 9}">Kumulatif net lot sejak anchor</text>')
    lots = [v for s in series for v in s["vals"] if v is not None]
    if lots:
        ticks = _nice_ticks(min(lots + [0.0]), max(lots + [0.0]), 3)
        y = _yscale(ticks[0], ticks[-1], *_LT)
        for t in ticks:
            g.append(f'<line class="{"base" if t == 0 else "gl"}" x1="{_X0}" x2="{_X1}" '
                     f'y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                     f'<text class="tk" x="{_X0 - 5}" y="{y(t):.1f}" dy=".32em" '
                     f'text-anchor="end">{_e(_lots_compact(t) if t else "0")}</text>')
        ends = []
        for s in series:
            d, lone = _path(xs, s["vals"], y)
            if d:
                g.append(f'<path class="ln s{s["slot"]}" d="{d}"/>')
            for px, py in lone:
                g.append(f'<circle class="d{s["slot"]}" cx="{px:.1f}" cy="{py:.1f}" r="2"/>')
            px, v = _last_point(xs, s["vals"])
            ends.append((s, px, y(v), v))
        label_ys = _spread([e[2] for e in ends], _LT[0], _LT[1] + 8)
        for (s, px, py, v), ly in zip(ends, label_ys):
            if abs(ly - py) > 1.5 or px < _X1 - 1:
                g.append(f'<line class="ld" x1="{px + 5:.1f}" y1="{py:.1f}" '
                         f'x2="{_X1 + 4}" y2="{ly:.1f}"/>')
            name = s["broker"] if len(s["broker"]) <= 6 else s["broker"][:5] + "…"
            g.append(f'<circle class="dot d{s["slot"]}" cx="{px:.1f}" cy="{py:.1f}" r="4"/>'
                     f'<text class="lbl" x="{_X1 + 6}" y="{ly:.1f}" dy=".32em">'
                     f'{_e(name)} {_e(_lots_compact(v))}</text>')
    else:
        g.append(f'<text class="tk" x="{_X0}" y="{(_LT[0] + _LT[1]) / 2:.0f}">belum ada posisi</text>')

    idx = sorted({0, n // 3, (2 * n) // 3, n - 1}) if n > 3 else list(range(n))
    ticks = [_date_tick(dates[i]) for i in idx]
    if len(set(ticks)) < len(ticks):
        ticks = [_date_tick(dates[i], day=True) for i in idx]
    for i, tick in zip(idx, ticks):
        anchor_ = "start" if i == 0 and n > 1 else "end" if i == n - 1 and n > 1 else "middle"
        g.append(f'<text class="tk" x="{xs[i]:.1f}" y="{_H - 4}" '
                 f'text-anchor="{anchor_}">{_e(tick)}</text>')
    g.append(f'<line class="xh" x1="0" x2="0" y1="{_PT[0]}" y2="{_LT[1]}" visibility="hidden"/>')

    data = _hover(dates, close, series)
    label = f"Grafik harga dan kumulatif net lot {ticker}"
    legend = []
    for side, head in (("buy", "Beli bersih"), ("sell", "Jual bersih")):
        items = [s for s in series if s["side"] == side]
        if items:
            legend.append(f'<span class="lgh">{head}</span>' + "".join(
                f'<span><i class="k k{s["slot"]}"></i>{_e(s["broker"])}</span>' for s in items))
    anchor_txt = f" {_e(_date_long(anchor))}" if anchor else ""
    return (
        f'<figure class="ch" data-x0="{_X0}" data-x1="{_X1}" data-w="{_W}" '
        f'data-json="{_e(json.dumps(data, separators=(",", ":"), allow_nan=False))}">'
        f'<svg viewBox="0 0 {_W} {_H}" role="img" tabindex="0" aria-label="{_e(label)}">'
        f'<title>{_e(label)}</title>{"".join(g)}</svg>'
        f'<div class="tip" hidden></div>'
        f'<div class="lg">{"".join(legend)}</div>'
        f'<figcaption>Garis 0 = posisi saat anchor{anchor_txt}. {CHART_PER_SIDE} pembeli dan '
        f'{CHART_PER_SIDE} penjual bersih terbesar; broker lain ada di tabel. Geser/ketuk grafik '
        f'untuk nilai per tanggal.</figcaption>'
        f'{_chart_table(dates, close, series)}</figure>')


def _sample(n, points):
    """Evenly spaced session indices, at most ~`points`, always with the last."""
    step = max(1, math.ceil(n / points))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx, step


def _int_or_none(v):
    v = _num(v)
    return None if v is None else int(round(v))


def _hover(dates, close, series):
    """The hover script's data, kept small (module docstring): about
    HOVER_POINTS sessions, every "st"-th of the "n" on the x axis plus the
    last (the script rebuilds that list exactly as _sample does), integer
    prices and lots, and dates as day offsets from the first ("d0" + "g")
    when every date parses (else the date strings themselves, "d")."""
    idx, step = _sample(len(dates), HOVER_POINTS)
    data = {"n": len(dates), "st": step}
    try:
        days = [date.fromisoformat(dates[i]) for i in idx]
        data["d0"] = days[0].isoformat()
        data["g"] = [(d - days[0]).days for d in days]
    except (TypeError, ValueError):
        data["d"] = [dates[i] for i in idx]
    data["c"] = [_int_or_none(close[i]) for i in idx]
    data["s"] = [{"b": s["broker"], "k": s["slot"], "v": [_int_or_none(s["vals"][i]) for i in idx]}
                 for s in series]
    return data


def _chart_table(dates, close, series):
    """The chart's table twin: every value is reachable without hover or
    colour. Sampled (~TABLE_ROWS rows, about monthly over a year) and printed
    as plain signed text: the sign is in the text, so colour spans would only
    add weight."""
    n = len(dates)
    idx, step = _sample(n, TABLE_ROWS)
    head = ["Tanggal", "Harga"] + [_e(s["broker"]) for s in series]
    rows = [[_e(dates[i]), _e(fmt_int(close[i]))] + [_e(fmt_int(s["vals"][i], True))
                                                     for s in series] for i in idx]
    note = f"tiap {step} sesi + sesi terakhir" if step > 1 else "semua sesi"
    return (f'<details class="tv"><summary>Tabel data grafik ({note})</summary>'
            f'{_table(head, rows)}</details>')


CI_DOMAIN_CAP = 0.60              # h=60 excess runs to tens of percent; beyond this, clip and say so
CI_PANELS = ((5, 10, 20), (60,))  # one x-scale per panel, shared by every rule


def _ci_domains(rules):
    """One x-scale per horizon panel, shared by every rule so widths compare
    across rules. h=60 gets its own panel: sixty sessions move several times
    more than five, and on one scale the short horizons would shrink to dots."""
    doms = {}
    for panel in CI_PANELS:
        vals = []
        for r in rules:
            for h in panel:
                st = _d(_stat_for(r.get("stats"), h))
                vals += [abs(v) for v in (_num(st.get("mean_excess")), _num(st.get("ci_lo")),
                                          _num(st.get("ci_hi"))) if v is not None]
        doms[panel] = min(max(max(vals, default=0.0) * 1.1, 0.005), CI_DOMAIN_CAP)
    return doms


def _ci_svg(stats, doms, primary=DEFAULT_PRIMARY_H):
    """Mean excess with its 95% bootstrap CI per horizon, against zero, as two
    small multiples (h=5/10/20 and h=60), each with its own labelled axis. The
    rule's primary horizon is in the accent, the others muted. A value past
    the domain is pinned to the edge with an open end marker; the printed
    label and the table keep the true number."""
    if not any(_num(_d(_stat_for(stats, h)).get("mean_excess")) is not None for h in HORIZONS):
        return ""
    x0, x1, rh = 52, 272, 20
    g, top = [], 6
    for panel in CI_PANELS:
        dom = doms.get(panel) or CI_DOMAIN_CAP
        rows = [(h, _d(_stat_for(stats, h))) for h in panel]

        def x(v, dom=dom):
            v = max(-dom, min(dom, v))
            return x0 + (v + dom) / (2 * dom) * (x1 - x0)

        g.append(f'<line class="base" x1="{x(0):.1f}" x2="{x(0):.1f}" y1="{top}" '
                 f'y2="{top + rh * len(rows)}"/>')
        for k, (h, st) in enumerate(rows):
            cy = top + rh * k + rh / 2
            cls = "pri" if h == primary else "sec"
            mean, lo, hi = (_num(st.get("mean_excess")), _num(st.get("ci_lo")),
                            _num(st.get("ci_hi")))
            tip = (f"h={h}: rata-rata excess {fmt_pct(mean, 2, True)}, CI 95% "
                   f"[{fmt_pct(lo, 2, True)}; {fmt_pct(hi, 2, True)}]"
                   + (f", kena suspensi {fmt_pct(st.get('susp_rate'), 0)}" if h == HOLD_H else ""))
            g.append(f'<g><title>{_e(tip)}</title>'
                     f'<text class="tk" x="{x0 - 6}" y="{cy:.1f}" dy=".32em" text-anchor="end">'
                     f'h={h}</text>')
            if lo is not None and hi is not None:
                g.append(f'<line class="wk {cls}" x1="{x(lo):.1f}" x2="{x(hi):.1f}" '
                         f'y1="{cy:.1f}" y2="{cy:.1f}"/>')
                for v in (lo, hi):
                    if abs(v) > dom:
                        g.append(f'<circle class="clip" cx="{x(v):.1f}" cy="{cy:.1f}" r="3"/>')
            if mean is not None:
                g.append(f'<circle class="dot {cls}" cx="{x(mean):.1f}" cy="{cy:.1f}" r="4"/>')
            g.append(f'<text class="lbl" x="{x1 + 8}" y="{cy:.1f}" dy=".32em">'
                     f'{_e(fmt_pct(mean, 2, True))}</text></g>')
        ya = top + rh * len(rows) + 12
        for v, anchor in ((-dom, "start"), (0.0, "middle"), (dom, "end")):
            g.append(f'<text class="tk" x="{x(v):.1f}" y="{ya}" text-anchor="{anchor}">'
                     f'{_e(fmt_pct(v, 1, True) if v else "0")}</text>')
        top = ya + 14
    return (f'<figure class="ci"><svg viewBox="0 0 {_W} {top - 8}" role="img" '
            f'aria-label="Rata-rata excess dan CI 95% per horizon">{"".join(g)}</svg>'
            f'<figcaption>Titik = rata-rata excess, garis = CI 95%. h=60 (return pemegang, '
            f'suspensi dijembatani) punya skala sendiri.'
            f'</figcaption></figure>')


def _stat_for(stats, h):
    stats = _d(stats)
    return stats.get(str(h), stats.get(h))


# ── page sections ───────────────────────────────────────────────────────────

def _header(ctx, tickers):
    run = _d(ctx.get("run"))
    dt = _s(ctx.get("data_through"))
    gen = _s(ctx.get("generated_utc"))
    gen_txt = gen.replace("T", " ").rstrip("Z") + " UTC" if gen else MISSING
    kind = _s(run.get("kind"))
    fail = _num(run.get("tickers_fail"))
    status = ("lengkap" if fail == 0 else f"{fmt_int(fail)} ticker gagal" if fail is not None
              else "status tidak diketahui")
    n_fired = sum(1 for t in tickers if _fired(t))
    n_elig = sum(1 for t in tickers if t.get("eligible") is True)
    tiles = [("Data s.d.", _date_long(dt)), ("Sinyal aktif", fmt_int(n_fired)),
             ("Ticker eligible", f"{fmt_int(n_elig)} / {fmt_int(len(tickers))}"),
             ("Ticker gagal", fmt_int(run.get("tickers_fail")))]
    extra = _s(ctx.get("disclaimer")).strip()
    extra_html = f"<br>{_e(extra)}" if extra and extra != DISCLAIMER else ""
    return (
        '<header>'
        '<h1>Dashboard Broker <span class="h1s">pembelajaran aturan</span></h1>'
        f'<p class="sub">Data s.d. <b>{_e(_date_long(dt))}</b> · dibuat {_e(gen_txt)} · '
        f'ruleset {_e(ctx.get("ruleset") or MISSING)} · run {_e(RUN_KIND.get(kind, kind or MISSING))}: '
        f'{_e(status)} ({_e(fmt_int(run.get("tickers_ok")))} ticker OK)</p>'
        f'<p class="disc" role="note"><span aria-hidden="true">⚠</span> {_e(DISCLAIMER)}{extra_html}</p>'
        '<div class="kpis">' + "".join(
            f'<div class="kpi"><div class="kl">{_e(k)}</div><div class="kv">{_e(v)}</div></div>'
            for k, v in tiles) + '</div>'
        '<nav><a href="#sinyal">Sinyal</a><a href="#ticker">Per ticker</a>'
        '<a href="#belajar">Dipelajari</a><a href="#alpha">Kasus alpha</a><a href="#broker">Broker</a>'
        '<a href="#kualitas">Kualitas data</a></nav>'
        '</header>')


def _rule_chip(rid, rule_map):
    r = _d(rule_map.get(rid))
    parts = [f"<b>{_e(rid)}</b>"]
    if r:
        parts.append(_dir_label(r.get("dir")))
        if r.get("label"):
            parts.append(f'<span class="rn">{_e(r.get("label"))}</span>')
        parts.append(_status(r.get("status")))
        parts.append(f'<span class="wt">bobot {_e(fmt_num(r.get("weight"), 2))}</span>')
    else:
        parts.append(_status(None))
    return '<li>' + " ".join(p for p in parts if p) + '</li>'


def _price_bits(t):
    return (f'<span class="px">{_e(fmt_rp(t.get("close")))}</span> '
            f'{_signed(fmt_pct(t.get("change_1d"), 1, True))}')


def _signals(tickers, rule_map):
    fired = [t for t in tickers if _fired(t)]
    out = ['<section id="sinyal"><h2>Sinyal hari ini</h2>']
    if not fired:
        n_el = sum(1 for t in tickers if t.get("eligible") is True)
        out.append(f'<p class="muted">Tidak ada aturan yang aktif pada sesi terakhir '
                   f'({_e(fmt_int(n_el))} ticker eligible dievaluasi).</p></section>')
        return "".join(out)
    out.append('<p class="note">Skor = jumlah aturan aktif × arah × bobot, hanya untuk '
               'mengurutkan. Status dan bobot berasal dari evaluasi retrospektif mingguan di horizon '
               'utama tiap aturan (h=10; R6 h=60).</p>'
               '<ul class="sigs">')
    for t in fired:
        tk = _s(t.get("ticker"))
        out.append(
            f'<li class="sig"><div class="sigh"><a href="#{_e(_anchor_id(tk))}"><b>{_e(tk)}</b></a> '
            f'{_price_bits(t)} <span class="sc">skor {_signed(fmt_num(t.get("score"), 2, True))}</span></div>'
            f'<ul class="rl">{"".join(_rule_chip(r, rule_map) for r in _fired(t))}</ul></li>')
    out.append('</ul></section>')
    return "".join(out)


def _facts(f):
    f = _d(f)
    items = []
    th = _d(f.get("top_holder"))
    if th:
        items.append(f'<dt>Pemegang terbesar</dt><dd>{_e(th.get("broker") or MISSING)} '
                     f'{_signed(fmt_lot(th.get("position_lots"), True))}, modal rata² '
                     f'{_e(fmt_rp(th.get("avg_cost")))}</dd>')
    bw = _d(f.get("biggest_winner"))
    if bw:
        items.append(f'<dt>Untung terbesar</dt><dd>{_e(bw.get("broker") or MISSING)} '
                     f'{_signed(fmt_rp_big(bw.get("total_rp"), True))}</dd>')
    dist = [_d(x) for x in _l(f.get("distributing"))]
    if f:
        body = " · ".join(f'{_e(x.get("broker") or MISSING)} {_signed(fmt_lot(x.get("net5d"), True))}'
                               for x in dist) or "tidak ada"
        items.append(f'<dt>Sedang distribusi (net 5 sesi)</dt><dd>{body}</dd>')
    return f'<dl class="facts">{"".join(items)}</dl>' if items else ""


def _book(rows, anchor):
    rows = [_d(r) for r in _l(rows)]
    if not rows:
        return '<p class="muted">Belum ada transaksi broker sejak anchor.</p>'
    rows.sort(key=lambda r: -(_num(r.get("position_lots")) or 0.0))
    head = ["Broker", "Posisi", "Modal rata²", "Harga vs modal", "Realized", "Unrealized",
            "Total P/L", "Net 1 sesi", "Net 5 sesi", "BAVG 20", "SAVG 20"]
    body, any_short = [], False
    for r in rows:
        pos = _num(r.get("position_lots"))
        star = ""
        if pos is not None and pos < 0:
            star, any_short = '<sup title="net jual sejak anchor, bukan short">*</sup>', True
        body.append([
            f'{_e(r.get("broker") or MISSING)}{star}',
            _signed(fmt_lot(pos, True)),
            _e(fmt_rp(r.get("avg_cost"))),
            _signed(fmt_pct(r.get("pct_vs_cost"), 1, True)),
            _signed(fmt_rp_big(r.get("realized_rp"), True)),
            _signed(fmt_rp_big(r.get("unrealized_rp"), True)),
            f'<b>{_signed(fmt_rp_big(r.get("total_rp"), True))}</b>',
            _signed(fmt_int(r.get("net1d"), True)),
            _signed(fmt_int(r.get("net5d"), True)),
            _e(fmt_rp(r.get("bavg20"))),
            _e(fmt_rp(r.get("savg20"))),
        ])
    note = (f'Inventory broker yang terlihat sejak anchor {_e(_date_long(anchor))}, bukan kepemilikan '
            f'sebenarnya. Total P/L = realized + unrealized (mark-to-market di harga penutupan terakhir). '
            f'Net dalam lot.')
    if any_short:
        note += (' <b>*</b> Posisi negatif = broker menjual bersih sejak anchor, yaitu '
                 'mendistribusikan barang yang dipegang sebelum anchor, <b>bukan short</b>; modalnya '
                 'adalah harga jual bersih rata-rata, jadi harga di atasnya berarti rugi bagi penjual.')
    return _table(head, body, "book") + f'<p class="note">{note}</p>'


def _card(t, rule_map, dt):
    tk = _s(t.get("ticker")) or "?"
    fired = _fired(t)
    badges = []
    if t.get("basis_ok") is False:
        badges.append('<span class="bd warn">⚠ basis putus</span>')
    if t.get("eligible") is not True:
        badges.append('<span class="bd" title="Aturan hanya dievaluasi pada baris eligible: '
                      f'≥80 sesi, nilai transaksi rata-rata 20 sesi ≥ {_e(fmt_rp_big(MIN_VAL20))}, '
                      'basis bersih 80 sesi.">tidak eligible</span>')
    tdt = _s(t.get("data_through"))
    if tdt and dt and tdt != dt:
        badges.append(f'<span class="bd warn">⚠ data s.d. {_e(tdt)}</span>')
    chips = "".join(f'<span class="rid">{_e(r)}</span>' for r in fired)
    score = (f'<span class="sc">skor {_signed(fmt_num(t.get("score"), 2, True))}</span>'
             if fired else "")
    head = (f'<summary><b class="tk">{_e(tk)}</b> {_price_bits(t)} {score} {chips} '
            f'{"".join(badges)}</summary>')
    body = []
    if t.get("basis_ok") is False:
        body.append('<p class="warnbox"><span aria-hidden="true">⚠</span> Basis lot dan harga '
                    'tidak sejalan sampai sesi terakhir (corporate action atau data vendor), jadi buku '
                    'modal rata-rata tidak dihitung dan aturan tidak dievaluasi. Ticker ini hanya '
                    'ditampilkan sebagai peringatan.</p>')
    else:
        if t.get("eligible") is not True:
            body.append('<p class="muted">Tidak eligible hari ini: aturan tidak dievaluasi. Buku di '
                        'bawah hanya deskriptif.</p>')
        explain = [x for x in _l(t.get("explain")) if x]
        if explain:
            body.append('<ul class="ex">' + "".join(f"<li>{_e(x)}</li>" for x in explain) + '</ul>')
        body.append(_facts(t.get("facts")))
        chart = _chart(t.get("curves"), tk, t.get("anchor"))
        body.append(chart or '<p class="muted">Grafik tidak tersedia.</p>')
        body.append(_book(t.get("book"), t.get("anchor")))
    return (f'<details class="card" id="{_e(_anchor_id(tk))}"{" open" if fired else ""}>'
            f'{head}<div class="cb">{"".join(body)}</div></details>')


def _cards(tickers, rule_map, dt):
    out = ['<section id="ticker"><h2>Per ticker</h2>']
    if not tickers:
        out.append('<p class="muted">Belum ada ticker watchlist pada run ini.</p>')
    else:
        out.append('<p class="note">Kartu dengan aturan aktif terbuka otomatis. Posisi, modal dan '
                   'P/L adalah inventory broker yang terlihat, bukan kepemilikan sebenarnya.</p>')
        out += [_card(t, rule_map, dt) for t in tickers]
    out.append('</section>')
    return "".join(out)


def _rule_block(r, doms):
    stats = _d(r.get("stats"))
    rid = _s(r.get("id")) or "?"
    primary = _primary_h(r)
    prev = _num(r.get("prev_weight"))
    parts = [f'<div class="rule"><div class="rh"><b>{_e(rid)}</b> {_dir_label(r.get("dir"))} '
             f'<span class="rn">{_e(r.get("label") or r.get("name") or "")}</span> '
             f'{_status(r.get("status"))} <span class="wt">bobot {_e(fmt_num(r.get("weight"), 2))}'
             f' (minggu lalu {_e(fmt_num(prev, 2))}) · horizon utama h={primary}</span></div>']
    if r.get("desc"):
        parts.append(f'<p class="rd">{_e(r.get("desc"))}</p>')
    rows = []
    window = None
    for h in HORIZONS:
        st = _stat_for(stats, h)
        if not isinstance(st, dict):
            continue
        window = window or (st.get("window_start"), st.get("window_end"))
        rows.append([
            f'h={h}' + (" (utama)" if h == primary else "")
            + (f'<br>{_susp(st.get("susp_rate"))}' if h == HOLD_H else ""),
            _e(fmt_int(st.get("n_events"))),
            _e(fmt_int(st.get("n_dates"))),
            _signed(fmt_pct(st.get("mean_excess"), 2, True)),
            _ci(st.get("ci_lo"), st.get("ci_hi")),
            _hit_base(st.get("hit_rate"), st.get("base_rate")),
            _signed(fmt_pp(st.get("hit_edge"))),
            _signed(fmt_pp(st.get("daily_hit_edge"))),
            _big_base(st.get("big_rate"), st.get("big_base_rate"), h),
            (_status(st.get("status")), "l"),
        ])
    if not rows:
        parts.append('<p class="muted">Belum ada statistik retrospektif untuk aturan ini.</p></div>')
        return "".join(parts)
    as_of = r.get("stats_as_of")
    win = (f" · jendela {_e(window[0] or MISSING)} s.d. {_e(window[1] or MISSING)}"
           if window and (window[0] or window[1]) else "")
    parts.append(f'<p class="meta">Retrospektif per {_e(as_of or MISSING)}{win} '
                 f'(bukan point-in-time). Excess ditampilkan mentah; arah dugaan di atas.</p>')
    parts.append(_ci_svg(stats, doms, primary))
    parts.append(_table(["Horizon", "n event", "n tanggal", "Rata² excess", "CI 95%",
                         "Hit vs base", "Edge hit", "Edge harian",
                         "Naik besar vs rata-rata", "Status"], rows))
    parts.append(_net_line(r.get("net"), primary))
    parts.append('</div>')
    return "".join(parts)


def _net_line(net, h):
    """§4.5's informational net-of-cost line for a buy rule: per trade, return
    minus the moderate round trip, never annualised, hit rate beside its base."""
    net = _d(net)
    n = _num(net.get("n_trades"))
    if not net or not n:
        return ""
    return (f'<p class="meta">Bersih biaya {_e(fmt_pct(ROUND_TRIP_COST, 2))} pulang-pergi '
            f'(informatif, per trade, h={h}, return mentah): n={_e(fmt_int(n))}, rata² '
            f'{_signed(fmt_pct(net.get("mean_ret"), 2, True))}, median '
            f'{_signed(fmt_pct(net.get("median_ret"), 2, True))}, hit '
            f'{_hit_base(net.get("hit_rate"), net.get("base_rate"))}.</p>')


def _live_h(rules):
    """The live ledger's horizon, from the rows if they all say the same h."""
    hs = {int(h) for h in (_num(_d(r.get("live")).get("h")) for r in rules) if h is not None}
    return hs.pop() if len(hs) == 1 else LIVE_H_DEFAULT


def _learned(ctx, rules):
    n_rules = len(rules) or len(PRIMARY_H)
    big_txt = ", ".join(f"h={h}: ≥ {fmt_pct(v, 0, True)}" for h, v in BIG.items())
    out = ['<section id="belajar"><h2>Apa yang sudah dipelajari</h2>',
           '<p class="note">Evaluasi retrospektif pasar penuh (mingguan) atas ruleset v1 yang '
           'dibekukan. Excess = return open→open h sesi dikurangi rata-rata saham eligible pada '
           'tanggal dan kuintil volatilitas yang sama. CI = bootstrap blok melingkar 95%, butuh '
           'minimal 3 blok (3×h tanggal). '
           f'Naik besar = return open→open {_e(big_txt)}; "rata-rata" = porsi yang sama di semua '
           f'saham eligible pada tanggal sinyal. <b>{n_rules} aturan × {len(HORIZONS)} horizon = '
           f'{n_rules * len(HORIZONS)} uji sekaligus</b>: sebagian akan tampak bagus karena '
           'kebetulan. Tidak ada promosi otomatis; hanya ledger live yang prospektif.</p>',
           f'<p class="note"><b>h={HOLD_H} = return pemegang.</b> Beli di open T+1, jual di open '
           f'sesi pertama yang diperdagangkan ≥{HOLD_H} sesi bursa kemudian. Suspensi di tengah '
           'jalan dijembatani (posisi dibekukan), karena saham yang naik paling kencang justru '
           'yang disuspensi; split tetap dibuang. Karena itu setiap angka h=60 diberi porsi '
           '<b>"kena suspensi"</b>: selama suspensi uang terkunci, dan saat dibuka lagi harga bisa '
           'ke arah mana pun.</p>',
           f'<p class="note"><b>Mengapa status h={HOLD_H} masih "data belum cukup".</b> Status butuh '
           f'minimal {_low_n_min(HOLD_H)} tanggal event (3 blok × {HOLD_H} sesi) supaya interval '
           'kepercayaannya berarti. API hanya menyediakan data sekitar 1 tahun (±240 sesi, dan '
           f'setelah pemanasan 80 sesi serta {HOLD_H} sesi ke depan tinggal ±100 tanggal yang bisa '
           f'dinilai). Jadi semua status h={HOLD_H} akan tetap "data belum cukup" sampai riwayat '
           'terkumpul minggu demi minggu. Itu jawaban yang jujur, bukan kegagalan aturan.</p>']
    if not rules:
        out.append('<p class="muted">Belum ada aturan atau statistik pada run ini.</p>')
    doms = _ci_domains(rules)
    out += [_rule_block(r, doms) for r in rules]

    # Each rule's live row is judged at its own primary h (R6 at 60), so when
    # the rows differ the horizon moves from the heading into a column.
    live_hs = {int(h) for h in (_num(_d(r.get("live")).get("h")) for r in rules) if h is not None}
    per_rule_h = len(live_hs) > 1
    head_h = "h utama tiap aturan" if per_rule_h else f"h={_live_h(rules)}"
    out.append(f'<h3>Ledger live (prospektif, {head_h})</h3>')
    if rules:
        rows = []
        for r in rules:
            lv = _d(r.get("live"))
            row = [_e(r.get("id") or "?")]
            if per_rule_h:
                row.append(_e(fmt_int(lv.get("h"))))
            mean = _signed(fmt_pct(lv.get("mean_excess"), 2, True))
            if _num(lv.get("h")) == HOLD_H:
                mean += " " + _susp(lv.get("susp_rate"))
            rows.append(row + [_e(fmt_int(lv.get("n_signals"))),
                               _e(fmt_int(lv.get("n_scored"))), mean,
                               _hit_base(lv.get("hit_rate"), lv.get("base_rate"))])
        out.append(_table(["Aturan"] + (["h"] if per_rule_h else [])
                          + ["Sinyal", "Sudah dinilai", "Rata² excess", "Hit vs base"], rows))
        out.append('<p class="note">Dicatat setiap hari sejak ruleset dibekukan; return tiap saham '
                   'dicatat sekali dan tidak pernah dihitung ulang. Excess = return dikurangi '
                   'rata-rata saham watchlist eligible pada tanggal yang sama yang return-nya sudah '
                   'tercatat; base = porsi naik di kelompok yang sama. Di h=60 saham yang jual-nya '
                   'tertunda suspensi tercatat belakangan, jadi angka tanggal itu masih bisa '
                   'bergeser sampai semuanya tercatat.</p>')
    else:
        out.append('<p class="muted">Belum ada sinyal live.</p>')

    out.append('<h3>Perubahan bobot vs minggu lalu</h3>')
    wrows = []
    for r in rules:
        w, p = _num(r.get("weight")), _num(r.get("prev_weight"))
        delta = fmt_num(w - p, 2, True) if w is not None and p is not None else MISSING
        wrows.append([_e(r.get("id") or "?"), _e(fmt_num(p, 2)), _e(fmt_num(w, 2)), _signed(delta)])
    if wrows:
        out.append(_table(["Aturan", "Minggu lalu", "Sekarang", "Perubahan"], wrows))
    learned = [x for x in _l(ctx.get("learned")) if x]
    if learned:
        out.append('<ul class="ln-list">' + "".join(f"<li>{_e(x)}</li>" for x in learned) + '</ul>')
    elif not wrows:
        out.append('<p class="muted">Belum ada bobot hasil belajar; semua aturan berbobot 1,00.</p>')
    out.append('</section>')
    return "".join(out)


def _rules_txt(x):
    """'R1,R6' -> 'R1, R6'; empty -> 'tidak ada'. The caller escapes it."""
    ids = [p.strip() for p in _s(x).split(",") if p.strip()]
    return ", ".join(ids) if ids else "tidak ada"


def _case(c):
    """One alpha case: the snapshot a trader could have seen at T."""
    c = _d(c)
    tk = _s(c.get("ticker")) or "?"
    adv = _num(c.get("top_nl60_adv"))
    items = [
        ("Aturan aktif di T", _e(_rules_txt(c.get("rules_at_t")))),
        ("Aturan aktif 20 sesi sebelumnya", _e(_rules_txt(c.get("rules_prior20")))),
        ("Broker teratas (net 60 sesi)", _e(c.get("top_broker") or MISSING)),
        ("Net 60 sesi broker itu",
         _signed(fmt_num(adv, 1, True) + "× ADV20") if adv is not None else _signed(MISSING)),
        ("Harga vs modal broker itu", _signed(fmt_pct(c.get("top_cost_gap"), 1, True))),
        ("Rentang harga 60 sesi (high/low)", _e(fmt_pct(c.get("range60"), 0))),
        ("Nilai transaksi rata² 20 sesi", _e(fmt_rp_big(c.get("val20")))),
    ]
    items.append(("Sesi sebelum T dalam data", _e(fmt_int(c.get("sessions_before")))))
    body = "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in items)
    susp = _num(c.get("susp_60"))
    badges = ""
    if susp == 1:
        badges += ' <span class="bd warn">kena suspensi</span>'
    invisible = _num(c.get("visible")) == 0
    if invisible:
        badges += ' <span class="bd warn">tidak terlihat</span>'
    note = (f'<p class="muted">Fase akumulasi sebelum data dimulai — tidak terlihat: kurang dari '
            f'{VISIBLE_MIN} sesi data sebelum T, jadi snapshot ini tidak bisa menunjukkan siapa '
            f'yang mengumpulkan. Tidak dihitung di lift broker.</p>' if invisible else "")
    return (f'<li class="case"><div class="sigh"><b>{_e(tk)}</b> '
            f'<span class="muted">T = {_e(_date_long(c.get("session_date")))}</span> '
            f'<span>naik {_signed(fmt_pct(c.get("hold_60"), 0, True))} dalam {ALPHA_H} sesi</span>'
            f'{badges}</div>{note}<dl class="facts">{body}</dl></li>')


def _alpha(a, rules):
    a = _d(a)
    cases = [_d(c) for c in _l(a.get("cases")) if isinstance(c, dict)]
    cases.sort(key=lambda c: (_num(c.get("hold_60")) is None,
                              -(_num(c.get("hold_60")) or 0.0), _s(c.get("ticker"))))
    cases = cases[:ALPHA_ROWS]
    n_cases = _num(a.get("n_cases"))
    base = _d(a.get("base"))
    base60 = _num(base.get("big_base_rate_60"))
    out = ['<section id="alpha"><h2>Kasus alpha (retrospektif)</h2>',
           f'<p class="meta">Per {_e(a.get("as_of") or MISSING)} · {_e(fmt_int(n_cases))} kasus, '
           f'{_e(fmt_int(a.get("n_visible")))} terlihat · {_susp(a.get("susp_rate"))}. '
           f'Kasus = saham eligible dengan return pemegang ≥ {_e(fmt_pct(ALPHA_MIN, 0, True))} '
           f'(open T+1 → open, suspensi dijembatani) dalam {ALPHA_H} sesi setelah T; hanya T paling '
           f'awal per episode. Semua angka di kartu adalah kondisi pada T, yaitu yang bisa dilihat '
           f'trader sebelum harga naik. Kasus dengan kurang dari {VISIBLE_MIN} sesi data sebelum T '
           f'ditandai "tidak terlihat": fase akumulasinya terjadi sebelum data dimulai.</p>']
    if cases:
        out.append('<ul class="cases">' + "".join(_case(c) for c in cases) + '</ul>')
        if n_cases is not None and n_cases > len(cases):
            out.append(f'<p class="note">Ditampilkan {len(cases)} kasus dengan kenaikan terbesar.</p>')
    else:
        out.append('<p class="muted">Belum ada kasus alpha pada evaluasi mingguan terakhir.</p>')

    base_txt = (f' Sebagai pembanding: rata-rata {_e(fmt_pct(base60, 1))} saham eligible naik '
                f'≥ {_e(fmt_pct(ALPHA_MIN, 0, True))} dalam {ALPHA_H} sesi '
                f'({_susp(base.get("susp_rate_60"))}).'
                if base60 is not None else "")
    out.append(
        '<div class="warnbox"><p><b>Cara membaca.</b> Kasus-kasus ini dipilih <i>setelah</i> '
        'harganya naik. Pola yang terlihat di sini (misalnya satu broker terus membeli sementara '
        'harga datar) juga muncul di banyak saham yang tidak naik. Sebuah pola baru berarti kalau '
        'ia muncul <b>jauh lebih sering</b> di kasus dibanding di semua saham eligible pada hari '
        f'yang sama.{base_txt}</p>'
        '<p>Ujian yang sebenarnya ada di kartu skor aturan di atas: kolom <b>naik besar vs '
        f'rata-rata</b> pada h={ALPHA_H}. Sebuah aturan baru menarik kalau porsi naik besarnya '
        'jelas di atas rata-rata, dengan jumlah tanggal yang cukup.</p></div>')

    rows = []
    for r in rules:
        st = _d(_stat_for(r.get("stats"), ALPHA_H))
        if not st:
            continue
        rows.append([_e(r.get("id") or "?"), _e(fmt_int(st.get("n_events"))),
                     _e(fmt_int(st.get("n_dates"))),
                     _big_base(st.get("big_rate"), st.get("big_base_rate"), ALPHA_H),
                     _susp(st.get("susp_rate"))])
    if rows:
        out.append(f'<h3>Naik besar per aturan (h={ALPHA_H})</h3>')
        out.append('<p class="note">Pengganti hitungan look-alike per kasus: dari saham yang '
                   'ditandai aturan, berapa yang naik besar, dibanding semua saham eligible pada '
                   'tanggal yang sama.</p>')
        out.append(_table(["Aturan", "n event", "n tanggal", "Naik besar vs rata-rata",
                           "Suspensi"], rows))

    out.append('<h3>Lift broker</h3>')
    out.append('<p class="note"><b>Kode broker = perusahaan sekuritas berisi banyak nasabah, bukan '
               'identitas bandar.</b> Lift = seberapa sering broker ini menjadi broker teratas '
               '(net 60 sesi) pada kasus <b>yang terlihat</b>, dibagi seberapa sering ia menjadi '
               'broker teratas pada semua baris eligible. Kasus yang tidak terlihat tidak dihitung: '
               'broker teratasnya pada T adalah yang ikut menaikkan harga, bukan yang mengumpulkan. '
               f'Lift 2,0× = dua kali lebih sering. Minimal {LIFT_MIN_CASES} kasus. Deskriptif saja, '
               'bukan sinyal.</p>')
    lift = [x for x in (_d(x) for x in _l(a.get("lift")))
            if (_num(x.get("n_cases_top")) or 0) >= LIFT_MIN_CASES]
    lift.sort(key=lambda x: (_num(x.get("lift")) is None, -(_num(x.get("lift")) or 0.0),
                             _s(x.get("broker"))))
    if lift:
        out.append(_table(
            ["Broker", "Kasus (teratas)", "Semua baris (teratas)", "Porsi di kasus",
             "Porsi di semua baris", "Lift"],
            [[_e(x.get("broker") or MISSING), _e(fmt_int(x.get("n_cases_top"))),
              _e(fmt_int(x.get("n_rows_top"))), _e(fmt_pct(x.get("case_share"), 1)),
              _e(fmt_pct(x.get("row_share"), 1)),
              _e(fmt_num(x.get("lift"), 2) + ("×" if _num(x.get("lift")) is not None else ""))]
             for x in lift[:LIFT_ROWS]]))
    else:
        out.append(f'<p class="muted">Belum ada broker dengan ≥ {LIFT_MIN_CASES} kasus.</p>')
    out.append('</section>')
    return "".join(out)


def _score_key(s):
    direction = -1.0 if s.get("side") == "sell" else 1.0
    v = _num(s.get("shrunk"))
    return (v is None, -(direction * v) if v is not None else 0.0, _s(s.get("broker")))


def _brokers(b):
    b = _d(b)
    out = ['<section id="broker"><h2>Papan peringkat broker</h2>',
           f'<p class="meta">Retrospektif pasar penuh per {_e(b.get("as_of") or MISSING)}. Kode broker '
           f'bukan entitas: satu kode menggabungkan banyak klien. Research-grade.</p>',
           '<h3>Track record (setelah broker beli/jual besar)</h3>']
    scores = [_d(s) for s in _l(b.get("scores")) if isinstance(s, dict)]
    hs = sorted({int(h) for h in (_num(s.get("h")) for s in scores) if h is not None})
    h_show = BROKER_H if BROKER_H in hs else (hs[0] if hs else None)
    if h_show is None:
        out.append('<p class="muted">Belum ada skor broker.</p>')
    else:
        out.append(f'<p class="note">h={h_show} sesi. Beli: excess positif = searah. Jual: excess '
                   f'negatif = searah (saham tertinggal setelah broker menjual). Shrunk = excess '
                   f'dikecilkan menurut jumlah tanggal. Diurutkan dari yang paling searah.</p>')
        for side in ("buy", "sell"):
            rows = sorted((s for s in scores if s.get("side") == side and _num(s.get("h")) == h_show),
                          key=_score_key)[:LEADERBOARD_ROWS]
            out.append(f'<h4>Sisi {SIDE_LABEL[side]}</h4>')
            if not rows:
                out.append('<p class="muted">Belum ada.</p>')
                continue
            out.append(_table(
                ["Broker", "n event", "n tanggal", "n ticker", "Rata² excess", "CI 95%",
                 "Shrunk", "Data"],
                [[_e(s.get("broker") or MISSING), _e(fmt_int(s.get("n_events"))),
                  _e(fmt_int(s.get("n_dates"))), _e(fmt_int(s.get("n_tickers"))),
                  _signed(fmt_pct(s.get("mean_excess"), 2, True)), _ci(s.get("ci_lo"), s.get("ci_hi")),
                  _signed(fmt_pct(s.get("shrunk"), 2, True)),
                  (_status("LOW_N") if s.get("low_n") in (True, 1) else
                   f'<span class="muted">n ≥ {_low_n_min(h_show)} tgl</span>'
                   if s.get("low_n") in (False, 0)
                   else MISSING, "l")]
                 for s in rows]))

    out.append('<h3>Profitabilitas trading</h3>')
    prof = [_d(p) for p in _l(b.get("profitability")) if isinstance(p, dict)]
    if not prof:
        out.append('<p class="muted">Belum ada data profitabilitas.</p>')
    else:
        prof.sort(key=lambda p: (_num(p.get("total_pnl_rp")) is None,
                                 -(_num(p.get("total_pnl_rp")) or 0.0), _s(p.get("broker"))))
        shown = prof if len(prof) <= 2 * LEADERBOARD_ROWS else (
            prof[:LEADERBOARD_ROWS] + [None] + prof[-LEADERBOARD_ROWS:])
        rows = []
        for p in shown:
            if p is None:
                rows.append([("⋮", "l")] + [""] * 5)
                continue
            rows.append([_e(p.get("broker") or MISSING), _e(fmt_int(p.get("n_tickers"))),
                         f'<b>{_signed(fmt_rp_big(p.get("total_pnl_rp"), True))}</b>',
                         _e(fmt_rp_big(p.get("turnover_rp"))),
                         _signed(fmt_pct(p.get("pnl_per_turnover"), 2, True)),
                         _e(fmt_pct(p.get("share_profitable"), 0))])
        out.append(_table(["Broker", "n ticker", "Total P/L", "Turnover", "P/L per turnover",
                           "Ticker untung"], rows))
        out.append('<p class="note">P/L trading mark-to-market dalam jendela bergulir, pada ticker '
                   'yang eligible dengan basis bersih di sesi terakhir run (ticker yang datanya '
                   'berhenti lebih awal tidak dihitung). Bukan klaim tentang siapa memiliki '
                   'apa.</p>')
    out.append('</section>')
    return "".join(out)


# Failure reasons come from the collector and the cache reader in English (they
# are also log lines and runs.note keeps them raw). The page is Indonesian
# (§6), so the table shows a short label with the raw detail kept after it.
_REASONS = (
    (re.compile(r"^no cached payload$"), "tidak ada cache"),
    (re.compile(r"^unreadable cache(?: \((.*)\))?$"), "cache tidak terbaca"),
    (re.compile(r"^OHLC identical to (\S+) \(cross-ticker clone\)$"), "OHLC sama persis dengan {0} (klon)"),
    (re.compile(r"^non-JSON body$"), "jawaban bukan JSON"),
    (re.compile(r"^HTTP (\d+)$"), "server menolak (HTTP {0})"),
    (re.compile(r"^strict frame: (.*)$"), "gagal validasi ketat"),
    (re.compile(r"^no usable response$"), "tidak ada jawaban yang bisa dipakai"),
    (re.compile(r"^no data in response$"), "jawaban tanpa data"),
    (re.compile(r"^cache write failed: (.*)$"), "gagal menulis cache"),
    (re.compile(r"^success=(.*)$"), "API menjawab gagal"),
    (re.compile(r"^meta\.symbol (.*)$"), "simbol jawaban tidak cocok"),
    (re.compile(r"^(?:response|data|meta) is (.*)$"), "format jawaban salah"),
    (re.compile(r"^PayloadError: (.*)$"), "payload tidak valid"),
)


def fmt_reason(text):
    """'HTTP 503' -> 'server menolak (HTTP 503)'; 'strict frame: X' -> 'gagal
    validasi ketat · X'. Unknown text is shown as it is, never dropped."""
    raw = _s(text)
    for pat, label in _REASONS:
        m = pat.match(raw)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        if "{0}" in label:
            return label.format(*groups)
        return f"{label} · {groups[0]}" if groups else label
    return raw


def _quality(ctx, tickers):
    run = _d(ctx.get("run"))
    out = ['<section id="kualitas"><h2>Kualitas data</h2>',
           f'<p>Run {_e(RUN_KIND.get(_s(run.get("kind")), run.get("kind") or MISSING))}: '
           f'{_e(fmt_int(run.get("tickers_ok")))} ticker OK, {_e(fmt_int(run.get("tickers_fail")))} gagal'
           + (f', {_e(fmt_int(len(_d(run.get("empty")))))} kosong' if _d(run.get("empty")) else "")
           + '.</p>']
    failed = _d(run.get("failed"))
    if failed:
        out.append(_table(["Ticker gagal", "Alasan"],
                          [[_e(k), (_e(fmt_reason(v)), "l")]
                           for k, v in sorted(failed.items(), key=lambda kv: str(kv[0]))]))
    empty = _d(run.get("empty"))
    if empty:
        out.append('<p class="note">Kosong = API menjawab tanpa satu sesi pun (delisting atau '
                   'suspensi panjang). Bukan kegagalan ambil data: tidak dihitung sebagai gagal. '
                   + ", ".join(_e(k) for k in sorted(empty, key=str)) + '</p>')
    warnings = [w for w in _l(run.get("warnings")) if w]
    if warnings:
        out.append('<h3>Peringatan</h3><ul>' + "".join(f"<li>{_e(w)}</li>" for w in warnings) + '</ul>')
    broken = [t for t in tickers if t.get("basis_ok") is False]
    if broken:
        out.append('<h3>Basis putus</h3><p>' + ", ".join(_e(t.get("ticker")) for t in broken) +
                   ': sesi terakhir terkena basis break; buku dan aturan tidak dihitung.</p>')
    if tickers:
        out.append('<h3>Anchor per ticker</h3>')
        out.append(_table(["Ticker", "Anchor", "Data s.d.", "Basis", "Eligible"],
                          [[_e(t.get("ticker")), _e(t.get("anchor") or MISSING),
                            _e(t.get("data_through") or MISSING),
                            "OK" if t.get("basis_ok") is True else "⚠ putus"
                            if t.get("basis_ok") is False else MISSING,
                            "ya" if t.get("eligible") is True else "tidak"]
                           for t in sorted(tickers, key=lambda t: _s(t.get("ticker")))]))
        out.append('<p class="note">Anchor = sesi setelah basis break terakhir; buku dan kurva '
                   'dihitung sejak anchor. Aturan butuh 80 sesi tanpa basis break.</p>')
    if not (failed or empty or warnings or broken or tickers):
        out.append('<p class="muted">Tidak ada catatan.</p>')
    out.append('</section>')
    return "".join(out)


def _footer(ctx):
    gloss = "".join(f'<dt>{STATUS_ICON[k]} {_e(STATUS_LABEL[k])} <code>{k}</code></dt>'
                    f'<dd>{_e(STATUS_NOTE[k])}</dd>' for k in STATUS_LABEL)
    return (
        '<footer><h2 id="arti-status">Arti status</h2>'
        f'<dl class="gloss">{gloss}</dl>'
        '<p class="note">Retrospektif = dihitung ulang dari data setahun terakhir yang bisa direvisi '
        'vendor, jadi bukan point-in-time. Live = dicatat pada hari sinyal, tidak pernah diubah. '
        'Hit = porsi event dengan return open→open positif; base = porsi saham eligible pada '
        'tanggal yang sama. Tidak ada statistik tahunan.</p>'
        f'<p class="disc" role="note"><span aria-hidden="true">⚠</span> {_e(DISCLAIMER)}</p>'
        f'<p class="meta">Ruleset {_e(ctx.get("ruleset") or MISSING)} · dibuat '
        f'{_e(ctx.get("generated_utc") or MISSING)}</p></footer>')


# ── styles and the hover script (static; no dynamic text is ever inlined here) ──

_LIGHT = {
    "page": "#f9f9f7", "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e",
    "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "line": "rgba(11,11,11,.10)",
    "chip": "#f0efec", "pos": "#006300", "neg": "#d03b3b", "warn": "#fab219", "accent": "#2a78d6",
    "c1": "#2a78d6", "c2": "#eb6834", "c3": "#1baf7a", "c4": "#eda100", "c5": "#e87ba4",
    "c6": "#008300",
}
# Selected for the dark surface, not flipped: same hues, dark-band steps.
_DARK = {
    "page": "#0d0d0d", "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7",
    "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "line": "rgba(255,255,255,.10)",
    "chip": "#2c2c2a", "pos": "#0ca30c", "neg": "#e66767", "warn": "#fab219", "accent": "#3987e5",
    "c1": "#3987e5", "c2": "#d95926", "c3": "#199e70", "c4": "#c98500", "c5": "#d55181",
    "c6": "#008300",
}


def _tokens(t):
    return "".join(f"--{k}:{v};" for k, v in t.items())


_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-wrap:anywhere}
.wrap{max-width:880px;margin:0 auto;padding:12px 16px 40px}
h1{font-size:22px;margin:4px 0 2px;line-height:1.2}
.h1s{display:block;font-size:14px;font-weight:400;color:var(--ink2)}
h2{font-size:18px;margin:32px 0 8px;padding-top:8px;border-top:1px solid var(--grid)}
h3{font-size:15px;margin:20px 0 6px}
h4{font-size:14px;margin:14px 0 4px;color:var(--ink2)}
p{margin:6px 0}
a{color:inherit;text-decoration-color:var(--axis)}
.sub,.meta,.note,figcaption{color:var(--ink2);font-size:13px}
.muted{color:var(--ink2)}
.disc{background:var(--chip);border-left:4px solid var(--warn);border-radius:6px;padding:8px 10px;font-size:13px;margin:10px 0}
.warnbox{border-left:4px solid var(--warn);background:var(--chip);border-radius:6px;padding:8px 10px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:10px 0}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.kl{font-size:12px;color:var(--ink2)}
.kv{font-size:20px;font-weight:600}
nav{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;margin:8px 0}
.pos{color:var(--pos)}
.neg{color:var(--neg)}
.zero,.na{color:var(--ink2)}
.st{display:inline-block;padding:0 7px;border-radius:999px;background:var(--chip);border:1px solid var(--line);color:var(--ink2);font-size:12px;white-space:nowrap;cursor:help}
.bd{display:inline-block;padding:0 7px;border-radius:999px;border:1px solid var(--line);font-size:12px;color:var(--ink2)}
.rid{display:inline-block;padding:0 6px;border-radius:6px;background:var(--chip);font-size:12px;font-weight:600}
.dir{font-size:12px;color:var(--ink2);white-space:nowrap}
.wt{font-size:12px;color:var(--ink2);white-space:nowrap}
.rn{font-weight:500}
.sigs{list-style:none;padding:0;margin:8px 0}
.sig{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:6px 0}
.sigh{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.rl{margin:4px 0 0;padding-left:18px;font-size:14px}
.rl li{margin:2px 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;margin:8px 0;padding:0 12px}
.card>summary{cursor:pointer;padding:10px 0;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;list-style:none}
.card>summary::-webkit-details-marker{display:none}
.card>summary::before{content:"\\25B8";color:var(--ink2)}
.card[open]>summary::before{content:"\\25BE"}
.tk{font-size:16px}
.cb{padding-bottom:12px}
.ex{padding-left:18px;margin:6px 0}
.facts{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:14px;margin:8px 0}
.facts dt{color:var(--ink2)}
.facts dd{margin:0}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0;border:1px solid var(--line);border-radius:8px;background:var(--surface);max-width:100%}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--grid)}
th{font-weight:600;color:var(--ink2);font-size:12px}
tr:last-child td{border-bottom:0}
th:first-child,td:first-child{text-align:left;position:sticky;left:0;background:var(--surface)}
td.l{text-align:left}
.hb{white-space:nowrap}
.sp{white-space:nowrap;font-size:12px;color:var(--ink2)}
.hb .vs{color:var(--ink2)}
.rule{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:10px 0}
.rh{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.rd{font-size:14px}
.ch,.ci{margin:10px 0;position:relative;max-width:520px}
.ch svg,.ci svg{display:block;width:100%;height:auto;overflow:visible}
.ch svg{touch-action:pan-y}
.ch svg:focus{outline:2px solid var(--accent);outline-offset:2px}
.ch text,.ci text{font-size:11px;fill:var(--ink2)}
.ch .lbl,.ci .lbl{fill:var(--ink2)}
.ch .ttl{fill:var(--ink2);font-weight:600}
.gl{stroke:var(--grid);stroke-width:1}
.base{stroke:var(--axis);stroke-width:1}
.ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.ld{stroke:var(--axis);stroke-width:1}
.xh{stroke:var(--ink2);stroke-width:1}
.dot{stroke:var(--surface);stroke-width:2}
.px{stroke:var(--ink2)}
.px-d{fill:var(--ink2)}
.s1{stroke:var(--c1)}.s2{stroke:var(--c2)}.s3{stroke:var(--c3)}.s4{stroke:var(--c4)}.s5{stroke:var(--c5)}.s6{stroke:var(--c6)}
.d1{fill:var(--c1)}.d2{fill:var(--c2)}.d3{fill:var(--c3)}.d4{fill:var(--c4)}.d5{fill:var(--c5)}.d6{fill:var(--c6)}
.wk{stroke-width:2;stroke-linecap:round}
.wk.pri{stroke:var(--c1)}.wk.sec{stroke:var(--muted)}
circle.pri{fill:var(--c1)}circle.sec{fill:var(--muted)}
.clip{fill:var(--surface);stroke:var(--ink2);stroke-width:1.5}
.cases{list-style:none;padding:0;margin:8px 0}
.case{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:6px 0}
.warnbox p{margin:4px 0}
.lg{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:var(--ink2);margin:4px 0}
.lgh{font-weight:600}
.k{display:inline-block;width:14px;height:0;border-top:2px solid;vertical-align:middle;margin-right:4px}
.k1{border-color:var(--c1)}.k2{border-color:var(--c2)}.k3{border-color:var(--c3)}.k4{border-color:var(--c4)}.k5{border-color:var(--c5)}.k6{border-color:var(--c6)}
.tip{position:absolute;top:4px;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);pointer-events:none;font-variant-numeric:tabular-nums;z-index:2}
.tip .tr{display:flex;align-items:center;gap:6px;white-space:nowrap}
.tip b{font-weight:600}
.tip .th{color:var(--ink2);margin-bottom:2px}
.tv>summary{font-size:13px;color:var(--ink2);cursor:pointer;margin-top:4px}
.gloss dt{font-weight:600;margin-top:6px}
.gloss dd{margin:0 0 0 18px;font-size:14px;color:var(--ink2)}
code{font-size:12px;color:var(--ink2)}
footer{margin-top:24px}
@media (min-width:640px){.ch text,.ci text{font-size:8.5px}}
"""


def _style():
    dark = _tokens(_DARK)
    return ("<style>:root{color-scheme:light;" + _tokens(_LIGHT) + "}"
            "@media (prefers-color-scheme: dark){:root:not([data-theme=\"light\"]){color-scheme:dark;"
            + dark + "}}"
            ":root[data-theme=\"dark\"]{color-scheme:dark;" + dark + "}"
            + _CSS + "</style>")


# Crosshair + one tooltip listing every series at the hovered date. Data comes
# from the figure's data-json attribute and reaches the DOM through
# textContent only, so a hostile broker code cannot become markup.
_JS = """
(function(){
function f(v,sg){if(v===null||v===undefined||!isFinite(v))return"\\u2014";
var s=Math.round(Math.abs(v)).toLocaleString("id-ID");return(v<0?"\\u2212":(sg&&v>0?"+":""))+s;}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e;}
var figs=document.querySelectorAll("figure.ch");
for(var q=0;q<figs.length;q++)(function(fig){
var svg=fig.querySelector("svg"),tip=fig.querySelector(".tip"),xh=svg&&svg.querySelector(".xh"),d;
if(!svg||!tip||!xh)return;
try{d=JSON.parse(fig.getAttribute("data-json"));}catch(e){return;}
var n=d.n;if(!n||!(d.st>0))return;d.i=[];for(var k=0;k<n;k+=d.st)d.i.push(k);
if(d.i[d.i.length-1]!==n-1)d.i.push(n-1);var m=d.i.length;
if(!d.d){d.d=[];var b=d.d0.split("-");for(var g=0;g<m;g++)
d.d.push(new Date(Date.UTC(+b[0],b[1]-1,+b[2])+d.g[g]*864e5).toISOString().slice(0,10));}
var x0=+fig.getAttribute("data-x0"),x1=+fig.getAttribute("data-x1"),W=+fig.getAttribute("data-w"),cur=m-1;
function xAt(k){var i=d.i[k];return n>1?x0+(x1-x0)*i/(n-1):(x0+x1)/2;}
function near(i){var k=0;for(var j=1;j<m;j++)if(Math.abs(d.i[j]-i)<Math.abs(d.i[k]-i))k=j;return k;}
function row(val,label,k){var r=el("div","tr");var key=el("i","k"+(k?" k"+k:""));
if(!k)key.style.borderColor="currentColor";r.appendChild(key);r.appendChild(el("b",null,val));
r.appendChild(el("span",null,label));tip.appendChild(r);}
function show(i){cur=Math.max(0,Math.min(m-1,i));var x=xAt(cur);
xh.setAttribute("x1",x);xh.setAttribute("x2",x);xh.setAttribute("visibility","visible");
while(tip.firstChild)tip.removeChild(tip.firstChild);
tip.appendChild(el("div","th",d.d[cur]));row(f(d.c[cur],false),"harga",0);
for(var j=0;j<d.s.length;j++)row(f(d.s[j].v[cur],true)+" lot",d.s[j].b,d.s[j].k);
tip.hidden=false;var w=svg.getBoundingClientRect().width,px=x/W*w;
if(px>w/2){tip.style.left="";tip.style.right=(w-px+8)+"px";}else{tip.style.right="";tip.style.left=(px+8)+"px";}}
function hide(){xh.setAttribute("visibility","hidden");tip.hidden=true;}
function at(ev){var r=svg.getBoundingClientRect();var ux=(ev.clientX-r.left)*W/r.width;
show(near(Math.round((ux-x0)/((x1-x0)||1)*(n-1))));}
svg.addEventListener("pointermove",at);svg.addEventListener("pointerdown",at);
svg.addEventListener("pointerleave",hide);svg.addEventListener("focus",function(){show(cur);});
svg.addEventListener("blur",hide);
svg.addEventListener("keydown",function(ev){if(ev.key==="ArrowLeft"){show(cur-1);ev.preventDefault();}
else if(ev.key==="ArrowRight"){show(cur+1);ev.preventDefault();}else if(ev.key==="Escape")hide();});
})(figs[q]);
})();
"""


# ── public API ──────────────────────────────────────────────────────────────

def render(ctx):
    """The full HTML document for a dashboard context (contract in the module
    header of broker_learning_run.py / BROKER_LEARNING.md §6). Tolerates a
    missing or partial ctx: every section has an empty state."""
    ctx = _d(ctx)
    tickers = _ordered([t for t in _l(ctx.get("tickers")) if isinstance(t, dict)])
    rules = [r for r in _l(ctx.get("rules")) if isinstance(r, dict)]
    rule_map = {_s(r.get("id")): r for r in rules}
    dt = _s(ctx.get("data_through"))
    meta_dt = dt if DATE_RE.fullmatch(dt) else ""
    return "\n".join([
        "<!doctype html>",
        '<html lang="id"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        '<meta name="robots" content="noindex">',
        f'<meta name="data-through" content="{_e(meta_dt)}">',
        f"<title>Dashboard Broker {_e(dt)}</title>",
        _style(),
        '</head><body><div class="wrap">',
        _header(ctx, tickers),
        _signals(tickers, rule_map),
        _cards(tickers, rule_map, dt),
        _learned(ctx, rules),
        _alpha(ctx.get("alpha"), rules),
        _brokers(ctx.get("brokers")),
        _quality(ctx, tickers),
        _footer(ctx),
        "</div>",
        f"<script>{_JS}</script>",
        "</body></html>",
    ])


def caption(ctx):
    """Plain-text Telegram caption, at most CAPTION_LIMIT characters however
    many tickers fired: the list is cut to what fits and ends with
    '…dan N lainnya'. The page itself always has all of them."""
    ctx = _d(ctx)
    run = _d(ctx.get("run"))
    dt = _s(ctx.get("data_through"))[:20] or "?"
    kind = _s(run.get("kind"))[:20]
    tickers = _ordered([t for t in _l(ctx.get("tickers")) if isinstance(t, dict)])
    fired = [t for t in tickers if _fired(t)]
    head = [f"Dashboard broker (research-grade) · data s.d. {dt} · ruleset "
            f"{_s(ctx.get('ruleset'))[:10] or '?'}",
            f"Run {RUN_KIND.get(kind, kind or '?')}: {fmt_int(run.get('tickers_ok'))} ticker OK, "
            f"{fmt_int(run.get('tickers_fail'))} gagal"]
    tail = [DISCLAIMER]
    items = []
    for t in fired:
        item = (f"• {_s(t.get('ticker'))} {'+'.join(_fired(t))} · skor "
                f"{fmt_num(t.get('score'), 2, True)}")
        items.append(item if len(item) <= CAPTION_ITEM_MAX else item[:CAPTION_ITEM_MAX - 1] + "…")

    def build(k):
        body = ([f"Sinyal aktif ({len(fired)}):"] + items[:k] +
                ([f"…dan {len(items) - k} lainnya"] if k < len(items) else [])
                if fired else ["Tidak ada aturan aktif hari ini."])
        return "\n".join(head + body + tail)

    for k in range(len(items), -1, -1):
        text = build(k)
        if len(text) <= CAPTION_LIMIT:
            return text
    return text[:CAPTION_LIMIT - 1] + "…"


def write(html, out_dir=OUT_DIR, name=None):
    """Write the page and return its path. The default name comes from the
    page's own data-through meta tag, so the file is named after the data it
    shows, not the day it was rendered. Written via a temp file, so a crash
    never leaves a half page for the sender to pick up."""
    if name is None:
        m = re.search(r'<meta name="data-through" content="([^"]*)">', html)
        dt = m.group(1) if m and DATE_RE.fullmatch(m.group(1)) else "undated"
        name = f"broker_dashboard_{dt}.html"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, os.path.basename(name))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    os.replace(tmp, path)
    return path


def _retry_after(resp):
    try:
        wait = int(resp.json().get("parameters", {}).get("retry_after", RETRY_AFTER_DEFAULT))
    except (ValueError, TypeError, AttributeError):
        wait = RETRY_AFTER_DEFAULT
    return max(0, min(wait, RETRY_AFTER_CAP))


def _transient(e):
    """A connection error or a timeout (requests' or the builtin ones), by
    class name so no requests import is needed to recognise them."""
    return any(c.__name__ in ("ConnectionError", "Timeout", "TimeoutError")
               for c in type(e).__mro__)


def send_document(token, chat_id, path, caption_text, post=None, sleep=time.sleep):
    """POST the page to Telegram sendDocument (multipart), SEND_TRIES attempts
    in all. A 429 is retried after its retry_after (capped at RETRY_AFTER_CAP);
    a connection error, a timeout or a 5xx after TRANSIENT_WAITS, because one
    network blip must not lose the day's page and turn the workflow red. A
    timeout can mean the page did arrive, so the rare cost is a duplicate
    message, which beats none. Other 4xx answers are final.

    Never prints the URL: it contains the bot token. An exception's message
    can quote the URL (requests does), so only its type name is printed."""
    if post is None:
        import requests
        post = requests.post
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError as e:
        print(f"telegram sendDocument: cannot read the dashboard file ({type(e).__name__})")
        return False
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {"chat_id": str(chat_id), "caption": _s(caption_text)[:CAPTION_LIMIT]}
    last = "rate-limited"
    for attempt in range(1, SEND_TRIES + 1):
        wait = TRANSIENT_WAITS[min(attempt, len(TRANSIENT_WAITS)) - 1]
        try:
            resp = post(url, data=data,
                        files={"document": (os.path.basename(path), content, "text/html")},
                        timeout=60)
        except Exception as e:  # message may contain the URL - print the type only
            print(f"telegram sendDocument failed: {type(e).__name__}")
            if not _transient(e):
                return False
            last = type(e).__name__
            if attempt < SEND_TRIES:
                sleep(wait)
            continue
        if resp.status_code == 429:
            last = "rate-limited"
            if attempt < SEND_TRIES:
                sleep(_retry_after(resp))
            continue
        if resp.status_code >= 500:
            print(f"telegram sendDocument error {resp.status_code}")
            last = f"HTTP {resp.status_code}"
            if attempt < SEND_TRIES:
                sleep(wait)
            continue
        if not getattr(resp, "ok", resp.status_code < 400):
            print(f"telegram sendDocument error {resp.status_code}")
            return False
        return True
    print(f"telegram sendDocument: still {last} after {SEND_TRIES} tries")
    return False


def send_from_env(path, caption_text):
    """send_document() with TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the
    environment. Missing secrets are reported by name only and return False."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; dashboard not sent")
        return False
    return send_document(token, chat_id, path, caption_text)
