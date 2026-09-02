"""Does the inventory curve predict tomorrow's ARA / ARB, across the whole IDX?

Runs on panel.parquet (built by inventory_features.py). Sections:
  0  re-verify the auto-rejection bands empirically on the full universe
  1  base rates by liquidity, and the fresh-vs-continuation regime split
  2  univariate screen, inventory features flagged separately from price ones
  3  walk-forward OOS: price-only vs inventory-only vs both
  4  event study of the inventory curve before a first ARA / first ARB
  5  economics of acting on the top-ranked names
"""
import argparse
import os

import numpy as np
import pandas as pd
from scipy.stats import beta as beta_dist
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, roc_auc_score

HERE = os.path.dirname(os.path.abspath(__file__))
rng = np.random.default_rng(17)

INV_PREFIX = ("top1_acc", "top3_acc", "bot1_acc", "bot3_acc", "hhi_acc", "n_acc",
              "n_dist", "net_acc", "dumping", "retail_acc", "tier1_acc", "tier2_acc",
              "smart_acc", "cp_acc", "mg_acc", "ss_acc", "px_vs_cost", "haka",
              "acc_persist", "acc_accel", "top_is_stable", "n_active_brk", "conc_today",
              "acc_share", "acc_efficiency", "absorb")
PX_FEATS = ["r1", "r2", "r3", "r5", "r10", "r20", "r60", "up_streak",
            "ara_c3", "ara_c5", "ara_c20", "arb_c3", "arb_c5", "arb_c20",
            "clv", "rng", "gap", "hi20", "hi60", "lo20", "vr5", "vr20",
            "rv10", "rv20", "rv60", "compress", "rng_compress", "logp", "band", "logturn20"]


def ci(k, n):
    if n == 0:
        return (np.nan, np.nan)
    lo = beta_dist.ppf(.025, k, n - k + 1) if k > 0 else 0.0
    hi = beta_dist.ppf(.975, k + 1, n - k) if k < n else 1.0
    return lo, hi


def load(min_turn):
    d = pd.read_parquet(os.path.join(HERE, "panel.parquet"))
    d["date"] = pd.to_datetime(d["date"])
    n0 = len(d)
    d = d[(d.turn20 >= min_turn) & (d.volume > 0) & d.close.notna()].copy()
    print(f"panel {n0:,} -> {len(d):,} rows after liquidity filter "
          f"(20d turnover >= Rp{min_turn}bn), {d.ticker.nunique()} tickers, "
          f"{d.date.min().date()}..{d.date.max().date()}")
    return d


def section0(d):
    print("\n" + "=" * 96)
    print("0. AUTO-REJECTION BANDS, re-verified on the full universe")
    print("=" * 96)
    r = d.ret.dropna()
    print(f"  {len(r):,} daily returns")
    print("  upside tail — count of returns >= x, by price band of the previous close")
    for lo, hi, lbl in [(0, 200, "<200"), (200, 5000, "200-5000"), (5000, 1e9, ">5000")]:
        s = d[(d.prev_close >= lo) & (d.prev_close < hi)].ret.dropna()
        cnts = " ".join(f"{x:.0%}:{(s>=x).sum():5d}" for x in
                        [.18, .20, .24, .25, .30, .34, .35, .40])
        print(f"    {lbl:9s} n={len(s):7,d}  {cnts}")
    print("  downside tail — count of returns <= -x (all bands pooled)")
    s = d.ret.dropna()
    print("    " + " ".join(f"-{x:.0%}:{(s<=-x).sum():5d}" for x in
                            [.10, .14, .15, .16, .20, .25, .35]))
    print("  If ARB were tiered like ARA there would be mass between -15% and -25%.")


def section1(d):
    print("\n" + "=" * 96)
    print("1. BASE RATES")
    print("=" * 96)
    for t in ["y_ARA", "y_ARB", "y_ARA_touch", "y_ARB_touch"]:
        k, n = int(d[t].sum()), len(d)
        lo, hi = ci(k, n)
        print(f"  {t:14s} {k:5d} events  {k/n*100:6.3f}%  [{lo*100:.3f},{hi*100:.3f}]")
    print("\n  by liquidity decile (20d turnover):")
    q = pd.qcut(d.turn20, 10, labels=False, duplicates="drop")
    g = d.groupby(q).agg(n=("y_ARA", "size"), ara=("y_ARA", "mean"),
                         arb=("y_ARB", "mean"), turn=("turn20", "median"))
    g[["ara", "arb"]] *= 100
    print(g.round(3).to_string())
    print("\n  regime split:")
    for lbl, m, t in [("ARA on T-1 -> ARA", d.ARA.astype(bool), "y_ARA"),
                      ("no ARA in 3d -> ARA", d.ara_c3 == 0, "y_ARA"),
                      ("ARB on T-1 -> ARB", d.ARB.astype(bool), "y_ARB"),
                      ("no ARB in 3d -> ARB", d.arb_c3 == 0, "y_ARB")]:
        s = d[m]
        k, n = int(s[t].sum()), len(s)
        lo, hi = ci(k, n)
        print(f"    {lbl:24s} n={n:7,d}  {k/n*100:6.2f}%  [{lo*100:.2f},{hi*100:.2f}]  ev={k}")


def screen(d, tgt, mask, label, feats):
    sub = d[mask]
    base = sub[tgt].mean()
    print(f"\n--- {label}: base {base*100:.3f}% ({int(sub[tgt].sum())} ev / {len(sub):,} obs)")
    rows = []
    for f in feats:
        s = sub[[f, tgt]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(s) < 5000 or s[tgt].sum() < 25:
            continue
        auc = roc_auc_score(s[tgt].astype(int), s[f])
        try:
            q = pd.qcut(s[f], 20, labels=False, duplicates="drop")
        except Exception:
            continue
        g = s.groupby(q)[tgt].agg(["mean", "size"])
        if len(g) < 8:
            continue
        rows.append(dict(feat=f, kind="INV" if f.startswith(INV_PREFIX) else "px",
                         auc=auc, dev=abs(auc - .5),
                         top5pct=g.iloc[-1]["mean"], n_top=int(g.iloc[-1]["size"]),
                         bot5pct=g.iloc[0]["mean"]))
    r = pd.DataFrame(rows)
    if r.empty:
        print("  (nothing passed the support filter)")
        return r
    r = r.sort_values("dev", ascending=False)
    r["lift_hi"] = r.top5pct / base
    r["lift_lo"] = r.bot5pct / base
    print(r.head(16)[["feat", "kind", "auc", "top5pct", "lift_hi", "n_top",
                      "bot5pct", "lift_lo"]].round(4).to_string(index=False))
    inv = r[r.kind == "INV"].head(8)
    if not inv.empty:
        print("  best inventory-only features:")
        print("   " + inv[["feat", "auc", "lift_hi"]].round(3).to_string(index=False)
              .replace("\n", "\n   "))
    return r


def walk_forward(data, tgt, feats, dates, min_train=140, step=20, embargo=2):
    preds = []
    X = data[feats].replace([np.inf, -np.inf], np.nan)
    for i in range(min_train, len(dates), step):
        tr_end, te = dates[i - 1], dates[i:i + step]
        tr = data[data.date <= tr_end - np.timedelta64(embargo, "D")]
        ted = data[data.date.isin(te)]
        if tr[tgt].sum() < 25 or len(ted) == 0:
            continue
        clf = HistGradientBoostingClassifier(
            max_depth=3, max_iter=250, learning_rate=0.06, min_samples_leaf=60,
            l2_regularization=1.0, class_weight="balanced", random_state=0)
        clf.fit(X.loc[tr.index], tr[tgt].astype(int))
        preds.append(ted.assign(p=clf.predict_proba(X.loc[ted.index])[:, 1]))
    return pd.concat(preds) if preds else pd.DataFrame()


def report(o, tgt, tag):
    if len(o) == 0 or o[tgt].sum() < 10:
        print(f"  {tag:26s} -- too few OOS events")
        return None
    y = o[tgt].astype(int)
    base = y.mean()
    line = (f"  {tag:26s} n={len(o):6,d} ev={int(y.sum()):4d} "
            f"AUC={roc_auc_score(y, o.p):.3f} AP={average_precision_score(y, o.p):.4f} "
            f"(base {base*100:.3f}%)")
    for q in [0.999, 0.99, 0.95]:
        sel = o[o.p >= o.p.quantile(q)]
        k, n = int(sel[tgt].sum()), len(sel)
        if n == 0:
            continue
        lo, hi = ci(k, n)
        line += (f"\n      top{(1-q)*100:.1f}%: n={n:5d} hit={k/n*100:5.2f}% "
                 f"[{lo*100:.1f},{hi*100:.1f}] lift={k/n/base:5.1f}x")
    print(line)
    return o


def boot_diff(sel, rest, col, n=1500):
    def m(df):
        days = df.date.unique()
        idx = {u: df.index[df.date == u] for u in days}
        out = []
        for _ in range(n):
            p = rng.choice(days, len(days), True)
            ii = np.concatenate([idx[q] for q in p])
            out.append(df.loc[ii, col].mean())
        return np.array(out)
    dd = m(sel) - m(rest)
    return sel[col].mean(), rest[col].mean(), np.percentile(dd, 2.5), np.percentile(dd, 97.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-turn", type=float, default=0.5, help="Rp bn 20d avg turnover")
    a = ap.parse_args()
    d = load(a.min_turn)
    section0(d)
    section1(d)

    ALL = [f for f in d.columns if f.startswith(INV_PREFIX)] + \
          [f for f in PX_FEATS if f in d.columns]
    INV = [f for f in d.columns if f.startswith(INV_PREFIX)]
    PX = [f for f in PX_FEATS if f in d.columns]
    print(f"\nfeature counts: {len(INV)} inventory, {len(PX)} price/volume")

    print("\n" + "=" * 96)
    print("2. UNIVARIATE SCREEN")
    print("=" * 96)
    screen(d, "y_ARA", d.ara_c3 == 0, "FRESH ARA", ALL)
    screen(d, "y_ARB", d.arb_c3 == 0, "FRESH ARB", ALL)

    print("\n" + "=" * 96)
    print("3. WALK-FORWARD OUT-OF-SAMPLE")
    print("=" * 96)
    dates = np.sort(d.date.unique())
    out = {}
    for tgt, lbl, mask in [("y_ARA", "ARA", None), ("y_ARB", "ARB", None),
                           ("y_ARA", "FRESH ARA", d.ara_c3 == 0),
                           ("y_ARB", "FRESH ARB", d.arb_c3 == 0)]:
        sub = d if mask is None else d[mask]
        print(f"\n### {lbl}")
        for feats, nm in [(PX, "price only"), (INV, "inventory only"), (ALL, "price + inventory")]:
            o = walk_forward(sub, tgt, feats, dates)
            r = report(o, tgt, f"GBM {nm}")
            if nm == "price + inventory" and r is not None:
                out[lbl] = r

    print("\n" + "=" * 96)
    print("4. WHAT THE INVENTORY CURVE LOOKS LIKE BEFORE A FIRST LIMIT HIT")
    print("=" * 96)
    g = d.groupby("ticker")
    d["first_ARA"] = d.ARA & (g["ARA"].transform(
        lambda s: s.shift(1).rolling(3, min_periods=1).sum().fillna(0)) == 0)
    d["first_ARB"] = d.ARB & (g["ARB"].transform(
        lambda s: s.shift(1).rolling(3, min_periods=1).sum().fillna(0)) == 0)
    show = ["top1_acc_20", "top3_acc_20", "hhi_acc_20", "acc_persist", "acc_accel",
            "retail_acc_20", "tier1_acc_20", "smart_acc_20", "mg_acc_20", "cp_acc_20",
            "px_vs_cost_20", "haka_20", "dumping_20", "acc_share_20",
            "acc_efficiency_20", "absorb_20", "compress", "n_acc_20", "n_dist_20", "conc_today",
            "top1_acc_60", "bot1_acc_60", "vr20", "rv20", "r5"]
    d = d.reset_index(drop=True)
    for ev, nm in [("first_ARA", "FIRST ARA"), ("first_ARB", "FIRST ARB")]:
        pos = np.flatnonzero(d[ev].fillna(False).to_numpy())
        tick_arr = d.ticker.to_numpy()
        print(f"\n{nm}: n={len(pos)} events   (feature mean at each lag vs all-sample mean)")
        print(f"{'feature':16s}" + "".join(f"{f'T{k:+d}':>11s}" for k in
                                           [-10, -5, -3, -1, 0]) + f"{'baseline':>11s}")
        for f in show:
            if f not in d.columns:
                continue
            base = d[f].replace([np.inf, -np.inf], np.nan)
            line = f"{f:16s}"
            for k in [-10, -5, -3, -1, 0]:
                src = pos + k
                valid = (src >= 0) & (src < len(d))
                same = np.zeros(len(pos), bool)
                same[valid] = tick_arr[src[valid]] == tick_arr[pos[valid]]
                v = base.iloc[src[same]].dropna()
                line += f"{v.mean():>11.3f}" if len(v) >= 10 else f"{'-':>11s}"
            line += f"{base.dropna().mean():>11.3f}"
            print(line)

    print("\n" + "=" * 96)
    print("5. ECONOMICS of the top-ranked names (next session)")
    print("=" * 96)
    for lbl in ["ARA", "ARB", "FRESH ARA", "FRESH ARB"]:
        o = out.get(lbl)
        if o is None or len(o) == 0:
            continue
        o = o.reset_index(drop=True)
        o["cc"] = o.y_close / o.close - 1
        o["oc"] = o.y_close / o.y_open - 1
        o["gapn"] = o.y_open / o.close - 1
        for q in [0.999, 0.99]:
            sel = o[o.p >= o.p.quantile(q)].dropna(subset=["cc", "oc"])
            rest = o[o.p < o.p.quantile(q)].dropna(subset=["cc", "oc"])
            if len(sel) < 20:
                continue
            for col, nm in [("cc", "close->close"), ("oc", "open->close")]:
                a_, b_, lo, hi = boot_diff(sel, rest, col)
                sig = "SIGNIFICANT" if (lo > 0 or hi < 0) else "ns"
                print(f"  {lbl:10s} top{(1-q)*100:.1f}% {nm:12s} "
                      f"picks {a_*100:+6.2f}%  rest {b_*100:+6.2f}%  "
                      f"diff CI [{lo*100:+.2f},{hi*100:+.2f}] {sig}  n={len(sel)}")
            print(f"  {lbl:10s}            gap at open {sel.gapn.mean()*100:+.2f}%")

    print("\n" + "=" * 96)
    print("6. PERMUTATION IMPORTANCE (best target, price + inventory)")
    print("=" * 96)
    best = "ARB" if "ARB" in out else next(iter(out), None)
    if best:
        sub = d
        tgt = "y_" + best.replace("FRESH ", "")
        cut = dates[int(len(dates) * .7)]
        tr, te = sub[sub.date < cut], sub[sub.date >= cut]
        X = sub[ALL].replace([np.inf, -np.inf], np.nan)
        clf = HistGradientBoostingClassifier(
            max_depth=3, max_iter=250, learning_rate=0.06, min_samples_leaf=60,
            l2_regularization=1.0, class_weight="balanced", random_state=0)
        clf.fit(X.loc[tr.index], tr[tgt].astype(int))
        r = permutation_importance(clf, X.loc[te.index], te[tgt].astype(int),
                                   scoring="roc_auc", n_repeats=4, random_state=0)
        imp = pd.Series(r.importances_mean, index=ALL).sort_values(ascending=False)
        imp = imp.to_frame("auc_drop")
        imp["kind"] = ["INV" if i.startswith(INV_PREFIX) else "px" for i in imp.index]
        print(imp.head(20).round(4).to_string())


if __name__ == "__main__":
    main()
