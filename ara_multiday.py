"""ARA potential over T+1..T+5, and whether it is buyable.

`scan_ara_arb.py` ranks the probability of an ARA *tomorrow*. It works
(fresh-ARA OOS AUC 0.772, 12.1x lift at top-1%) and is still untradable: the
top-ranked names open +7.13% and give back 5.07% into the close, so the whole
edge sits on the far side of the opening gap.

That verdict is specific to the one-day horizon. It does not answer the
question this script asks: if a name is going to hit ARA at some point in the
next five sessions, can you buy it at the T+1 open and be paid for the run-up
into that limit? The gap you cannot cross on the ARA day itself is only one of
five days here, and 23.6% of ARAs are followed by another one.

Labels (all built from the daily ARA flags `inventory_features.py` already
writes, so the bands and tick-snapping are the verified ones):

    y_ara_h5        an ARA close on any of T+1..T+5
    y_ara_touch_h5  the ARA price touched intraday on any of T+1..T+5
    y_ara_first     which of the five days carried the first ARA (NaN if none)

Entry is the T+1 open, which is knowable at the T close. Rows where T+1 opens
locked at the limit are dropped, not counted as wins -- you cannot buy into a
one-sided queue, and keeping them is how a backtest invents its edge.

Two guards are mandatory here, because both have already produced false
positives on this data set:

  1. Volatility stratification. A raw lift table cannot distinguish "predicts
     limit-up" from "predicts a wide daily range". Every headline number is
     repeated within rv20 quintiles; a signal that survives only in the pooled
     column is a volatility proxy. (The rev-2 report killed the accumulation
     finding exactly this way.)
  2. The ARB side of the same window. A feature that raises P(ARA) and P(ARB)
     together is not directional.

Usage:
    python ara_multiday.py                 # full report
    python ara_multiday.py --horizon 3
    python ara_multiday.py --min-turn 1.0
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_inventory_signal import INV_PREFIX, PX_FEATS, ci  # noqa: E402

COST = 0.0015 + 0.0025      # round trip, same as pattern_backtest.py
MAX_SPAN_DAYS = 12          # calendar span allowed for a 5-session window
rng = np.random.default_rng(17)


def model():
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=250, learning_rate=0.06, min_samples_leaf=60,
        l2_regularization=1.0, class_weight="balanced", random_state=0)


def load(min_turn):
    path = os.path.join(HERE, "panel.parquet")
    if not os.path.exists(path):
        sys.exit("panel.parquet not found -- run inventory_features.py first "
                 "(harvest_inventory.py -> build_inventory_db.py -> inventory_features.py)")
    d = pd.read_parquet(path)
    d["date"] = pd.to_datetime(d["date"])
    n0 = len(d)
    d = d[(d.turn20 >= min_turn) & (d.volume > 0) & d.close.notna()].copy()
    print(f"panel {n0:,} -> {len(d):,} rows (20d turnover >= Rp{min_turn}bn), "
          f"{d.ticker.nunique()} tickers, {d.date.min().date()}..{d.date.max().date()}")
    return d.sort_values(["ticker", "date"]).reset_index(drop=True)


def add_horizon_labels(d, h):
    """Forward ARA/ARB over T+1..T+h, plus the entry price and realised exits."""
    g = d.groupby("ticker", group_keys=False)

    fwd_ara = pd.concat([g["ARA"].shift(-k) for k in range(1, h + 1)], axis=1)
    fwd_arb = pd.concat([g["ARB"].shift(-k) for k in range(1, h + 1)], axis=1)
    fwd_tch = pd.concat([g["ARA_touch"].shift(-k) for k in range(1, h + 1)], axis=1)
    fwd_close = pd.concat([g["close"].shift(-k) for k in range(1, h + 1)], axis=1)
    fwd_close.columns = range(h)

    d["y_ara_h"] = fwd_ara.max(axis=1)
    d["y_arb_h"] = fwd_arb.max(axis=1)
    d["y_ara_touch_h"] = fwd_tch.max(axis=1)
    # index of the first ARA day within the window, NaN when there is none
    first = fwd_ara.to_numpy(dtype=float)
    has = np.nansum(first, axis=1) > 0
    d["y_ara_first"] = np.where(has, np.nanargmax(np.nan_to_num(first), axis=1) + 1, np.nan)

    d["entry_open"] = g["open"].shift(-1)
    d["entry_date"] = g["date"].shift(-1)
    d["end_close"] = g["close"].shift(-h)
    d["end_date"] = g["date"].shift(-h)
    d["entry_ara_px"] = g["ara_px"].shift(-1)

    span = (d.end_date - d.date).dt.days
    complete = fwd_close.notna().all(axis=1)
    d["window_ok"] = complete & span.le(MAX_SPAN_DAYS) & d.entry_open.notna()

    # exit at the close of the first ARA day, else at the close of T+h
    idx = d.y_ara_first.to_numpy()
    take = np.full(len(d), np.nan)
    arr = fwd_close.to_numpy(dtype=float)
    ok = ~np.isnan(idx)
    take[ok] = arr[np.arange(len(d))[ok], idx[ok].astype(int) - 1]
    d["exit_on_ara"] = np.where(ok, take, d.end_close)

    # you cannot buy a limit-up open
    d["entry_blocked"] = d.entry_open >= d.entry_ara_px - 1e-6

    d["gap_open"] = d.entry_open / d.close - 1
    d["ret_hold"] = d.end_close / d.entry_open - 1 - COST
    d["ret_ara_exit"] = d.exit_on_ara / d.entry_open - 1 - COST
    return d


def section_base(d, h):
    print("\n" + "=" * 96)
    print(f"1. BASE RATES over T+1..T+{h}")
    print("=" * 96)
    u = d[d.window_ok]
    print(f"  usable windows            {len(u):,}")
    print(f"  P(ARA close in window)    {u.y_ara_h.mean()*100:6.3f}%   "
          f"({int(u.y_ara_h.sum()):,} events)")
    print(f"  P(ARA touch in window)    {u.y_ara_touch_h.mean()*100:6.3f}%")
    print(f"  P(ARB close in window)    {u.y_arb_h.mean()*100:6.3f}%   <- directional control")
    blocked = u[u.y_ara_h > 0].entry_blocked.mean()
    print(f"  of the ARA windows, T+1 already opens locked: {blocked*100:.1f}% (excluded below)")

    print("\n  when the first ARA lands inside the window:")
    vc = u.loc[u.y_ara_first.notna(), "y_ara_first"].value_counts().sort_index()
    for k, n in vc.items():
        print(f"    day T+{int(k)}: {n:5,d}  ({n/vc.sum()*100:5.1f}%)")


def section_vol(d, h):
    """The guard: does anything survive inside a volatility bucket?"""
    print("\n" + "=" * 96)
    print("2. VOLATILITY STRATIFICATION (rv20 quintiles)")
    print("=" * 96)
    u = d[d.window_ok & d.rv20.notna()].copy()
    u["vq"] = pd.qcut(u.rv20, 5, labels=False, duplicates="drop")
    print(f"  {'rv20 quintile':>14s} {'n':>8s} {'P(ARA)':>9s} {'P(ARB)':>9s} {'ARA/ARB':>9s}")
    print("  " + "-" * 52)
    for q, s in u.groupby("vq"):
        a, b = s.y_ara_h.mean(), s.y_arb_h.mean()
        print(f"  {'Q'+str(int(q)+1):>14s} {len(s):8,d} {a*100:8.3f}% {b*100:8.3f}% "
              f"{a/max(b,1e-9):9.2f}")
    print("\n  A ratio that stays near 1.0 across quintiles means the window label is\n"
          "  tracking volatility, not direction.")


def walk_forward(data, tgt, feats, horizon, min_train=140, step=20, embargo=1):
    """Expanding walk-forward, cut in sessions rather than calendar days.

    The label spans `horizon` trading days forward, so a training row dated
    within `horizon` sessions of a test block already knows part of that
    block's outcome. The first version of this offset the cut by 2 calendar
    days, which left roughly a full horizon of overlapping labels in training
    -- and on exactly the rows most similar to the test set, so it inflated
    AUC and lift. Dropping `horizon + embargo` sessions closes the overlap.
    """
    dates = np.sort(data.date.unique())
    X = data[feats].replace([np.inf, -np.inf], np.nan)
    preds = []
    for i in range(min_train, len(dates), step):
        cut = i - 1 - horizon - embargo
        if cut < 20:
            continue
        tr = data[data.date <= dates[cut]]
        ted = data[data.date.isin(dates[i:i + step])]
        if tr[tgt].sum() < 25 or len(ted) == 0:
            continue
        clf = model()
        clf.fit(X.loc[tr.index], tr[tgt].astype(int))
        preds.append(ted.assign(p=clf.predict_proba(X.loc[ted.index])[:, 1], cycle=i))
    return pd.concat(preds) if preds else pd.DataFrame()


def report(o, tgt, tag):
    if len(o) == 0 or o[tgt].sum() < 10:
        print(f"  {tag:28s} -- too few OOS events")
        return None
    y = o[tgt].astype(int)
    base = y.mean()
    print(f"  {tag:28s} n={len(o):6,d} ev={int(y.sum()):4d} "
          f"AUC={roc_auc_score(y, o.p):.3f} AP={average_precision_score(y, o.p):.4f} "
          f"(base {base*100:.3f}%)")
    for q in [0.999, 0.99, 0.95, 0.90]:
        sel = o[o.p >= o.p.quantile(q)]
        if len(sel) == 0:
            continue
        k, n = int(sel[tgt].sum()), len(sel)
        lo, hi = ci(k, n)
        print(f"      top{(1-q)*100:4.1f}%: n={n:5d} hit={k/n*100:5.2f}% "
              f"[{lo*100:.1f},{hi*100:.1f}] lift={k/n/base:5.1f}x")
    return o


def section_model(d, h):
    print("\n" + "=" * 96)
    print(f"3. WALK-FORWARD RANKING of P(ARA within T+1..T+{h})")
    print("=" * 96)
    u = d[d.window_ok & ~d.entry_blocked].copy()
    inv = [f for f in u.columns if f.startswith(INV_PREFIX)]
    px = [f for f in PX_FEATS if f in u.columns]
    out = {}
    for tgt in ["y_ara_h", "y_arb_h"]:
        sub = u.dropna(subset=[tgt]).copy()
        for name, feats in [("price", px), ("price+inv", px + inv)]:
            o = walk_forward(sub, tgt, feats, h)
            lbl = f"{'ARA' if tgt=='y_ara_h' else 'ARB'} · {name}"
            r = report(o, tgt, lbl)
            if r is not None and tgt == "y_ara_h":
                out[name] = r
    return out


def _tag(q):
    """'top0.1%' / 'top1%' / 'top5%' -- a plain %.0f prints the 0.1% bucket as 0%."""
    pct = (1 - q) * 100
    return f"top{pct:g}%" if pct < 1 else f"top{pct:.0f}%"


def boot_mean_diff(sel, rest, col, n=1500):
    """Date-clustered bootstrap of the mean difference."""
    def draw(df):
        days = df.date.unique()
        pick = rng.choice(days, len(days), replace=True)
        return df[df.date.isin(pick)][col].mean()
    diffs = [draw(sel) - draw(rest) for _ in range(n)]
    return np.percentile(diffs, [2.5, 97.5])


def section_trade(preds, h):
    print("\n" + "=" * 96)
    print(f"4. TRADABILITY -- buy the T+1 open, exit at the first ARA close "
          f"(else T+{h} close)")
    print("=" * 96)
    if not preds:
        print("  no model output")
        return
    o = preds.get("price")
    if o is None or len(o) == 0:
        print("  no model output")
        return
    o = o.dropna(subset=["gap_open", "ret_ara_exit", "ret_hold"])
    print(f"  baseline over all {len(o):,} scored rows: "
          f"gap {o.gap_open.mean()*100:+.2f}%  "
          f"ara-exit {o.ret_ara_exit.mean()*100:+.2f}%  "
          f"hold-{h}d {o.ret_hold.mean()*100:+.2f}%  (net of {COST*100:.2f}% costs)")
    print(f"\n  {'bucket':>12s} {'n':>6s} {'gap@open':>10s} {'ara-exit':>10s} "
          f"{'hold':>9s} {'hit ARA':>9s} {'95% CI vs rest':>22s}")
    print("  " + "-" * 82)
    for q in [0.999, 0.99, 0.95, 0.90]:
        thr = o.p.quantile(q)
        sel, rest = o[o.p >= thr], o[o.p < thr]
        if len(sel) < 10:
            continue
        lo, hi = boot_mean_diff(sel, rest, "ret_ara_exit")
        sig = "sig" if lo > 0 or hi < 0 else "ns"
        print(f"  {_tag(q):>12s} {len(sel):6d} "
              f"{sel.gap_open.mean()*100:+9.2f}% {sel.ret_ara_exit.mean()*100:+9.2f}% "
              f"{sel.ret_hold.mean()*100:+8.2f}% {sel.y_ara_h.mean()*100:8.2f}% "
              f"  [{lo*100:+.2f},{hi*100:+.2f}] {sig}")

    print("\n  Decomposition -- where the money actually goes. A negative 'ara-exit'\n"
          "  can mean the winners are small or that the misses bleed; only this\n"
          "  table says which, and it is the number a stop-loss would act on.")
    print(f"\n  {'bucket':>12s} {'n':>6s} {'P(ARA)':>8s} {'win ret':>9s} "
          f"{'miss ret':>9s} {'implied':>9s} {'need':>8s} {'short by':>10s}")
    print("  " + "-" * 80)
    for q in [0.999, 0.99, 0.95, 0.90, 0.0]:
        sel = o[o.p >= o.p.quantile(q)] if q else o
        if len(sel) < 10:
            continue
        win = sel[sel.y_ara_h > 0].ret_ara_exit
        miss = sel[sel.y_ara_h == 0].ret_ara_exit
        if not len(win) or not len(miss):
            continue
        p, w, m = sel.y_ara_h.mean(), win.mean(), miss.mean()
        implied = p * w + (1 - p) * m
        need = -m / (w - m) if w > m else np.nan
        print(f"  {(_tag(q) if q else 'all'):>12s} {len(sel):6d} {p*100:7.2f}% "
              f"{w*100:+8.2f}% {m*100:+8.2f}% {implied*100:+8.2f}% "
              f"{need*100:7.2f}% {(p - need)*100:+9.2f}pp")

    print("\n  'need' is the hit rate at which this bucket's own win/miss pair breaks\n"
          "  even: |miss| / (win - miss). 'short by' is P(ARA) minus that. A bucket\n"
          "  only pays when it is positive, and the two ways to get there are a\n"
          "  higher hit rate (rank harder) or a smaller miss (cut the losers).")

    print("\n  The one-day scan died on the gap: ARA names opened +7.13% and lost 5.07%\n"
          "  into the close. If the gap here is small but 'ara-exit' is still negative,\n"
          "  the five-day version dies of something else -- read 'miss ret' for it.")


def section_robust(preds, cut=0.999):
    """The top bucket is ~36 rows. Before anyone sizes a trade on it, ask the
    four questions that separate an edge from a lucky corner of the data."""
    print("\n" + "=" * 96)
    print("5. IS THE TOP BUCKET REAL? -- cut sensitivity, concentration, outliers, stability")
    print("=" * 96)
    o = preds.get("price")
    if o is None or len(o) == 0:
        print("  no model output")
        return
    o = o.dropna(subset=["ret_ara_exit"]).copy()

    print("\n  (a) CUT SENSITIVITY -- a real edge decays smoothly across neighbouring")
    print("      cuts. One cut that pays while its neighbours do not is the shape of")
    print("      noise landing on the threshold we happened to pick.")
    print(f"\n      {'cut':>9s} {'n':>6s} {'P(ARA)':>8s} {'win ret':>9s} "
          f"{'miss ret':>9s} {'implied':>9s}")
    print("      " + "-" * 56)
    for q in [0.9995, 0.999, 0.9985, 0.998, 0.997, 0.995, 0.99]:
        s = o[o.p >= o.p.quantile(q)]
        win, miss = s[s.y_ara_h > 0].ret_ara_exit, s[s.y_ara_h == 0].ret_ara_exit
        if len(s) < 5 or not len(win) or not len(miss):
            continue
        print(f"      {_tag(q):>9s} {len(s):6d} {s.y_ara_h.mean()*100:7.2f}% "
              f"{win.mean()*100:+8.2f}% {miss.mean()*100:+8.2f}% "
              f"{s.ret_ara_exit.mean()*100:+8.2f}%")

    sel = o[o.p >= o.p.quantile(cut)].copy()
    if len(sel) < 5:
        return

    print(f"\n  (b) CONCENTRATION of the {len(sel)} names in {_tag(cut)}")
    tk = sel.ticker.value_counts()
    mo = sel.date.dt.to_period("M").value_counts().sort_index()
    share = tk.head(3).sum() / len(sel)
    print(f"      distinct tickers {sel.ticker.nunique():3d} | most frequent: "
          + ", ".join(f"{t}x{c}" for t, c in tk.head(5).items()))
    print(f"      distinct months  {len(mo):3d} | " + ", ".join(f"{m}:{c}" for m, c in mo.items()))
    print(f"      top-3 tickers hold {tk.head(3).sum()}/{len(sel)} rows ({share*100:.0f}%)"
          + ("   <- one episode, not a strategy" if share > 0.5 else ""))

    win = sel[sel.y_ara_h > 0].ret_ara_exit.sort_values(ascending=False)
    if len(win):
        print(f"\n  (c) THE {len(win)} WINNERS -- mean far above median means one name carries it")
        print(f"      mean {win.mean()*100:+.2f}%   median {win.median()*100:+.2f}%")
        print("      each: " + ", ".join(f"{v*100:+.1f}%" for v in win))
        print(f"      drop the single best -> bucket implied "
              f"{sel.drop(win.index[:1]).ret_ara_exit.mean()*100:+.2f}%")

    if "cycle" in sel.columns:
        print("\n  (d) PER-FOLD STABILITY -- how much of the edge is one lucky block?")
        g = sel.groupby("cycle").ret_ara_exit.agg(["size", "mean"])
        pos = int((g["mean"] > 0).sum())
        print(f"      {len(g)} walk-forward folds carry rows: {pos} positive, "
              f"{len(g) - pos} negative")
        best = g["mean"].idxmax()
        rest = sel[sel.cycle != best]
        print(f"      best fold alone : n={int(g.loc[best, 'size']):3d} "
              f"mean {g.loc[best, 'mean']*100:+.2f}%")
        if len(rest):
            print(f"      excluding it    : n={len(rest):3d} "
                  f"mean {rest.ret_ara_exit.mean()*100:+.2f}%")

    print("\n  Sizing a trade needs all four to hold: a smooth decay in (a), names and\n"
          "  months spread in (b), a median near the mean in (c), and an edge in (d)\n"
          "  that survives dropping its best fold.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--min-turn", type=float, default=0.5)
    a = ap.parse_args()

    d = load(a.min_turn)
    d = add_horizon_labels(d, a.horizon)
    section_base(d, a.horizon)
    section_vol(d, a.horizon)
    preds = section_model(d, a.horizon)
    section_trade(preds, a.horizon)
    section_robust(preds)


if __name__ == "__main__":
    main()
