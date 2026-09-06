"""The decisive question: does the inventory chart add anything ON TOP OF price
and volume, or is it a volatility proxy dressed up?

Runs the same walk-forward twice per target -- price-only vs price+inventory --
keeps both prediction vectors on the identical rows, and bootstraps the paired
difference in average precision, resampling whole trading days.
"""
import os
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_inventory_signal import INV_PREFIX, PX_FEATS  # noqa: E402

rng = np.random.default_rng(23)


def wf(data, tgt, feats, dates, min_train=140, step=20, embargo=2):
    keys, ps = [], []
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
        keys.append(ted.index.to_numpy())
        ps.append(clf.predict_proba(X.loc[ted.index])[:, 1])
    return pd.Series(np.concatenate(ps), index=np.concatenate(keys))


def paired_boot(df, n=2000):
    """Bootstrap AP(price+inv) - AP(price), resampling trading days."""
    days = df.date.unique()
    idx = {u: df.index[df.date == u].to_numpy() for u in days}
    out = []
    for _ in range(n):
        pick = rng.choice(days, len(days), True)
        ii = np.concatenate([idx[q] for q in pick])
        s = df.loc[ii]
        if s.y.sum() < 5:
            continue
        out.append(average_precision_score(s.y, s.p_both)
                   - average_precision_score(s.y, s.p_px))
    return np.array(out)


def main():
    d = pd.read_parquet(os.path.join(HERE, "panel.parquet"))
    d["date"] = pd.to_datetime(d["date"])
    d = d[(d.turn20 >= 0.5) & (d.volume > 0)].copy()
    INV = [f for f in d.columns if f.startswith(INV_PREFIX)]
    PX = [f for f in PX_FEATS if f in d.columns]
    dates = np.sort(d.date.unique())
    print(f"{len(d):,} rows, {len(PX)} price features, {len(INV)} inventory features\n")

    for tgt, lbl, mask in [("y_ARA", "ARA", None), ("y_ARB", "ARB", None),
                           ("y_ARA", "FRESH ARA", d.ara_c3 == 0),
                           ("y_ARB", "FRESH ARB", d.arb_c3 == 0)]:
        sub = d if mask is None else d[mask]
        p_px = wf(sub, tgt, PX, dates)
        p_both = wf(sub, tgt, PX + INV, dates)
        common = p_px.index.intersection(p_both.index)
        o = pd.DataFrame({"y": sub.loc[common, tgt].astype(int),
                          "date": sub.loc[common, "date"],
                          "p_px": p_px.loc[common], "p_both": p_both.loc[common]})
        ap0, ap1 = (average_precision_score(o.y, o.p_px),
                    average_precision_score(o.y, o.p_both))
        auc0, auc1 = (roc_auc_score(o.y, o.p_px), roc_auc_score(o.y, o.p_both))
        b = paired_boot(o)
        lo, hi = np.percentile(b, [2.5, 97.5])
        sig = "SIGNIFICANT" if (lo > 0 or hi < 0) else "not significant"
        print(f"{lbl:10s} n={len(o):6,d} ev={int(o.y.sum()):4d}")
        print(f"   AUC  price {auc0:.3f} -> price+inventory {auc1:.3f}  ({auc1-auc0:+.3f})")
        print(f"   AP   price {ap0:.4f} -> price+inventory {ap1:.4f} ({ap1-ap0:+.4f})")
        print(f"   paired bootstrap of the AP gain: 95% CI [{lo:+.4f}, {hi:+.4f}] "
              f"-> {sig}   P(gain>0)={np.mean(b>0):.2f}\n")


if __name__ == "__main__":
    main()
