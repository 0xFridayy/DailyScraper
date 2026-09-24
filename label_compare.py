"""The original question, asked with a label that can answer it.

This session started from "find stocks that rise more than 15% over T+1..T+5"
and never got a straight answer, because every test of it measured the wrong
thing. Two problems, both fixed here.

The label. "Price touched +15%" is not directional: on neobdm.db's 45 names it
came back 22.61%, and the matching "touched -15%" came back 17.26%. Features
that lifted one lifted the other -- the top range_pct decile scored 35.89% up
against 36.47% down. That is a volatility reading wearing a direction's
clothes. A triple barrier asks the question properly: does +UP arrive BEFORE
-DOWN, or not at all?

The universe. Those 45 tickers were chosen, not sampled -- annualised
volatility 36% to 152%, the base rate inflated by the picking. panel.parquet
carries 790 names over a year, so the same question can be asked of a market
rather than of a watchlist.

The entry is open(T+1), the earliest anchor a decision taken at the close of T
can actually transact on -- price_audit.add_forward_returns makes the same
argument for its executable contract, and #51-#59 measured the ARA side the
same way. A window whose T+1 opens locked at the limit is dropped, not scored
as a win.

The returns are what the rule itself would have paid: +UP when the target is
hit first, -DOWN when the stop is, and the close of T+H otherwise. So
'tradability' here is the rule's own P&L, not an upper bound on it.

Every headline is repeated inside rv20 quintiles. That guard has killed three
findings across this work -- the accumulation result in ARA_ARB_scan_report.md
and both of the first two labels tried here -- so it runs by default rather
than on request.

Usage:
    python label_compare.py                      # +15% before -7% over T+1..T+5
    python label_compare.py --up 0.20 --down 0.10
    python label_compare.py --horizon 10 --min-turn 2.0
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ara_multiday import (COST, MAX_SPAN_DAYS, _tag, boot_mean_diff,  # noqa: E402
                          report, walk_forward)
from test_inventory_signal import INV_PREFIX, PX_FEATS  # noqa: E402

PANEL = os.path.join(HERE, "panel.parquet")


def load(panel_path, min_turn):
    if not os.path.exists(panel_path):
        sys.exit(f"{panel_path} not found -- run harvest_inventory.py -> "
                 f"build_inventory_db.py -> inventory_features.py first")
    d = pd.read_parquet(panel_path)
    d["date"] = pd.to_datetime(d["date"])
    n0 = len(d)
    d = d[(d.turn20 >= min_turn) & (d.volume > 0) & d.close.notna()]
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"panel {n0:,} -> {len(d):,} rows (20d turnover >= Rp{min_turn}bn), "
          f"{d.ticker.nunique()} tickers, {d.date.min().date()}..{d.date.max().date()}")
    return d


def add_labels(d, up, down, h):
    """Triple barrier from the T+1 open, plus the plain touch labels it replaces.

    tb      1 when +up is reached before -down inside the window, else 0. A
            window that reaches neither is a 0: the money was tied up and the
            target never came.
    tb_ret  what the rule paid -- +up, -down, or the close of T+h -- net of
            costs, so the barriers are priced as the exits they are.
    up_touch / down_touch  whether each barrier was reached at all, which is
            the old label's whole vocabulary. Their rates are not comparable to
            each other when the barriers sit at different distances -- the
            point is that neither records which came FIRST, so a window that
            stopped out on day 2 and rallied on day 4 scores as a winner.
    """
    g = d.groupby("ticker", group_keys=False)
    entry = g["open"].shift(-1)
    hi = [g["high"].shift(-k) for k in range(1, h + 1)]
    lo = [g["low"].shift(-k) for k in range(1, h + 1)]
    close_h = g["close"].shift(-h)
    end_date = g["date"].shift(-h)

    e = entry.to_numpy(dtype=float)
    hit = np.full(len(d), np.nan)      # 1 target first, 0 stop first
    ret = np.full(len(d), np.nan)
    for k in range(h):
        up_k = (hi[k].to_numpy(dtype=float) / e - 1) >= up
        dn_k = (lo[k].to_numpy(dtype=float) / e - 1) <= -down
        open_yet = np.isnan(hit)
        # Both barriers inside one session: assume the stop went first. The
        # daily bars cannot say, and the optimistic reading is how a backtest
        # invents an edge.
        d_first = open_yet & dn_k
        hit[d_first], ret[d_first] = 0.0, -down
        u_first = np.isnan(hit) & up_k
        hit[u_first], ret[u_first] = 1.0, up

    timed_out = np.isnan(hit)
    hit[timed_out] = 0.0
    ret[timed_out] = (close_h.to_numpy(dtype=float) / e - 1)[timed_out]

    d["entry_open"] = entry
    d["tb"] = hit
    d["tb_ret"] = ret - COST
    d["up_touch"] = pd.concat(hi, axis=1).max(axis=1) / entry - 1 >= up
    d["down_touch"] = pd.concat(lo, axis=1).min(axis=1) / entry - 1 <= -down
    d[["up_touch", "down_touch"]] = d[["up_touch", "down_touch"]].astype(float)

    span = (end_date - d.date).dt.days
    complete = pd.concat(hi + lo + [close_h], axis=1).notna().all(axis=1)
    d["window_ok"] = complete & span.le(MAX_SPAN_DAYS) & entry.notna()
    d["entry_blocked"] = entry >= g["ara_px"].shift(-1) - 1e-6
    for c in ("tb", "tb_ret", "up_touch", "down_touch"):
        d.loc[~d.window_ok, c] = np.nan
    return d


def section_rates(d, up, down, h):
    print("\n" + "=" * 96)
    print(f"1. THE TWO LABELS over T+1..T+{h}, target +{up:.0%} / stop -{down:.0%}")
    print("=" * 96)
    u = d[d.window_ok]
    print(f"  usable windows                {len(u):,}")
    print(f"  touched +{up:.0%}                   {u.up_touch.mean()*100:6.2f}%")
    print(f"  touched -{down:.0%}                    {u.down_touch.mean()*100:6.2f}%")
    both = u[(u.up_touch > 0) & (u.down_touch > 0)]
    print(f"  touched BOTH                  {len(both)/len(u)*100:6.2f}%   "
          f"<- the touch label scores every one of these a win")
    print(f"  +{up:.0%} BEFORE -{down:.0%} (triple barrier) {u.tb.mean()*100:6.2f}%")
    print(f"  of those, T+1 opens locked     {u[u.tb > 0].entry_blocked.mean()*100:5.1f}% "
          f"(excluded below)")
    print(f"\n  rule P&L per window, net of {COST*100:.2f}% costs: "
          f"{u.tb_ret.mean()*100:+.2f}%")


def section_vol(d):
    print("\n" + "=" * 96)
    print("2. VOLATILITY STRATIFICATION (rv20 quintiles)")
    print("=" * 96)
    u = d[d.window_ok & d.rv20.notna()].copy()
    u["vq"] = pd.qcut(u.rv20, 5, labels=False, duplicates="drop")
    print(f"  {'rv20':>8s} {'n':>8s} {'+touch':>9s} {'-touch':>9s} "
          f"{'both':>8s} {'triple':>9s} {'rule P&L':>10s}")
    print("  " + "-" * 62)
    for q, s in u.groupby("vq"):
        a, b = s.up_touch.mean(), s.down_touch.mean()
        both = ((s.up_touch > 0) & (s.down_touch > 0)).mean()
        print(f"  {'Q'+str(int(q)+1):>8s} {len(s):8,d} {a*100:8.2f}% {b*100:8.2f}% "
              f"{both*100:7.2f}% {s.tb.mean()*100:8.2f}% "
              f"{s.tb_ret.mean()*100:+9.2f}%")
    print("\n  'both' is where the touch label lies: those windows hit the stop AND\n"
          "  the target, and it counts every one of them as a win. It grows with\n"
          "  volatility, which is how a volatility reading gets mistaken for a\n"
          "  directional one. The triple and P&L columns price them honestly.")


def section_model(d, h):
    print("\n" + "=" * 96)
    print(f"3. WALK-FORWARD RANKING of P(target before stop within T+1..T+{h})")
    print("=" * 96)
    u = d[d.window_ok & ~d.entry_blocked].copy()
    inv = [f for f in u.columns if f.startswith(INV_PREFIX)]
    px = [f for f in PX_FEATS if f in u.columns]
    out = {}
    for name, feats in [("price", px), ("price+inv", px + inv)]:
        o = walk_forward(u.dropna(subset=["tb"]), "tb", feats, h)
        if report(o, "tb", f"triple barrier · {name}") is not None:
            out[name] = o
    # The old label, same features, so the difference is the label alone.
    o = walk_forward(u.dropna(subset=["up_touch"]), "up_touch", px, h)
    report(o, "up_touch", "touched +target · price")
    return out


def section_trade(preds):
    print("\n" + "=" * 96)
    print("4. WOULD IT HAVE PAID -- buy the T+1 open, target or stop or T+h close")
    print("=" * 96)
    o = preds.get("price")
    if o is None or len(o) == 0:
        print("  no model output")
        return
    o = o.dropna(subset=["tb_ret"])
    print(f"  baseline over all {len(o):,} scored rows: {o.tb_ret.mean()*100:+.2f}%")
    print(f"\n  {'bucket':>10s} {'n':>7s} {'P(target)':>10s} {'rule P&L':>10s} "
          f"{'95% CI vs rest':>22s}")
    print("  " + "-" * 62)
    for q in [0.999, 0.99, 0.95, 0.90]:
        thr = o.p.quantile(q)
        sel, rest = o[o.p >= thr], o[o.p < thr]
        if len(sel) < 10:
            continue
        lo, hi = boot_mean_diff(sel, rest, "tb_ret")
        sig = "sig" if lo > 0 or hi < 0 else "ns"
        print(f"  {_tag(q):>10s} {len(sel):7d} {sel.tb.mean()*100:9.2f}% "
              f"{sel.tb_ret.mean()*100:+9.2f}%   [{lo*100:+.2f},{hi*100:+.2f}] {sig}")
    print("\n  A bucket is worth trading only when rule P&L is positive AND the\n"
          "  interval clears zero. Everything this project has measured so far\n"
          "  failed the second test, which is the one that costs money.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--up", type=float, default=0.15)
    ap.add_argument("--down", type=float, default=0.07)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--min-turn", type=float, default=0.5)
    a = ap.parse_args()

    d = load(a.panel, a.min_turn)
    d = add_labels(d, a.up, a.down, a.horizon)
    section_rates(d, a.up, a.down, a.horizon)
    section_vol(d)
    section_trade(section_model(d, a.horizon))


if __name__ == "__main__":
    main()
