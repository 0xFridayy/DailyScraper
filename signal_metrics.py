"""
Honest metrics for this project's signals, replacing the repo-wide Sharpe.

WHY SHARPE WAS REMOVED
----------------------
Four modules computed `returns.mean() / returns.std() * sqrt(252)`. That is
wrong here in two independent ways, and the two compound:

1. `returns` was mostly PER-TRADE, not daily. The sqrt(252) factor annualises
   daily observations; for a 3-day hold the factor is nearer sqrt(84). Applying
   the daily factor to multi-day trades inflates the number outright.

2. The observations OVERLAP CROSS-SECTIONALLY. Forty-five tickers on one day
   were counted as forty-five independent draws, when they are largely one draw
   moved by the same market. Measured on this panel, the mean pairwise
   correlation of daily returns is +0.275, so 39 tickers carry roughly 3.4
   independent series. That inflates n and shrinks std, and Sharpe divides by
   std.

Together they are a far simpler explanation for the recorded Sharpe of 5.36 and
6.95 than "the variant is good". Sharpe is also the wrong SHAPE of question: the
user explicitly does not want a portfolio or sizing model, and Sharpe is a
portfolio statistic. What is actually being asked is "is the signal accurate",
which needs no sizing assumption at all.

So nothing here annualises anything, and nothing here is called Sharpe.

THE EVALUATION BAR (user decision, 2026-08-30)
---------------------------------------------
`SATISFACTION_SHARPE = 1.5` is retired and is NOT being replaced by another
single number. The decision, in the user's words, is to keep IC, hit edge,
return edge and the base-rate comparison as the evaluation metrics — the SET is
the bar.

That is the right call and worth writing down so it does not drift back:

  - A single scalar is what let this project run for months on "Sharpe 6.95".
    One number invites a threshold, a threshold invites tuning toward it, and
    nothing in the number itself says which of its inputs moved.
  - The four disagree usefully. A signal can have a positive hit edge and a
    negative return edge (right more often, wrong when it is expensive) — that
    is a real and readable state, and any scalar collapsing them hides it. This
    panel is in exactly that state right now.
  - Nothing here is annualised or sized, so there is no scale on which to set
    "good". The base rate supplies the only meaningful zero: the same universe,
    same rows, no model.

evaluation_scorecard() below renders all four together for that reason, the
same way format_trade_stats() refuses to print a hit rate without its base
rate. Report the four; do not derive a verdict from one.

BASE RATE IS NOT OPTIONAL
-------------------------
A hit rate alone is unreadable. This repo recorded hit_rate 42.8% for months as
though it were a result; the base rate of the universe over the same rows is
also 42.8%, so the model was adding no directional information whatsoever. Every
hit rate below therefore ships with the base rate it must be read against, and
with the difference precomputed so it cannot be skimmed past again.

Deliberately depends on numpy and pandas only - no scipy - so the CI health
check can import it without installing the ML stack.
"""

import numpy as np
import pandas as pd

TOP_QUANTILE = 0.90   # "top decile", the slice a selective strategy would trade


def _as_series(x):
    return x if isinstance(x, pd.Series) else pd.Series(np.asarray(x, dtype=float))


def spearman_ic(pred, actual):
    """Rank correlation between prediction and realised return.

    Rank-based on purpose: a handful of extreme returns would dominate a Pearson
    correlation, and on IDX data those extremes are exactly the ARA/ARB days a
    signal cannot trade anyway. pandas .rank() averages ties, which matters
    because predictions cluster.
    """
    p, a = _as_series(pred).reset_index(drop=True), _as_series(actual).reset_index(drop=True)
    ok = p.notna() & a.notna()
    if ok.sum() < 3:
        return np.nan
    pr, ar = p[ok].rank(), a[ok].rank()
    if pr.std() == 0 or ar.std() == 0:
        return np.nan
    return float(np.corrcoef(pr, ar)[0, 1])


def signal_stats(pred, actual, top_q=TOP_QUANTILE):
    """Does a higher prediction actually mean a higher realised return?

    Returns, for the top-quantile slice and for everything:
      ic          rank correlation over all rows
      base_rate   fraction of ALL rows with a positive return
      hit_rate    fraction of TOP-slice rows with a positive return
      hit_edge    hit_rate - base_rate, the number that says whether the
                  direction call beats simply being in this universe
      top_mean    mean realised return in the top slice
      all_mean    mean realised return over all rows
      edge        top_mean - all_mean
    """
    p, a = _as_series(pred).reset_index(drop=True), _as_series(actual).reset_index(drop=True)
    ok = p.notna() & a.notna()
    p, a = p[ok], a[ok]
    n = len(p)
    empty = dict(n=0, n_top=0, ic=np.nan, base_rate=np.nan, hit_rate=np.nan,
                 hit_edge=np.nan, top_mean=np.nan, all_mean=np.nan, edge=np.nan)
    if n == 0:
        return empty

    cut = p.quantile(top_q)
    top = a[p >= cut]
    if len(top) == 0:
        return {**empty, "n": n, "ic": spearman_ic(p, a),
                "base_rate": float((a > 0).mean()), "all_mean": float(a.mean())}

    base_rate = float((a > 0).mean())
    hit_rate = float((top > 0).mean())
    all_mean, top_mean = float(a.mean()), float(top.mean())
    return dict(
        n=n, n_top=len(top), ic=spearman_ic(p, a),
        base_rate=base_rate, hit_rate=hit_rate, hit_edge=hit_rate - base_rate,
        top_mean=top_mean, all_mean=all_mean, edge=top_mean - all_mean,
    )


def trade_stats(returns, base_rate=None):
    """Summary of a set of realised trade returns, with no annualisation.

    `ret_per_risk` is mean/std: dimensionless, fine for RANKING variants against
    each other, and deliberately not scaled by sqrt(anything). It is not a
    Sharpe ratio and must not be compared against Sharpe benchmarks - the
    observations here are neither daily nor independent.

    Pass `base_rate` when the caller knows the universe's positive-return rate
    over the same rows; the hit rate is close to meaningless without it.
    """
    r = _as_series(returns).dropna()
    n = len(r)
    if n == 0:
        return dict(n_trades=0, mean_ret=np.nan, median_ret=np.nan, hit_rate=np.nan,
                    ret_per_risk=np.nan, base_rate=base_rate, hit_edge=np.nan)
    std = float(r.std())
    hit = float((r > 0).mean())
    return dict(
        n_trades=n,
        mean_ret=float(r.mean()),
        median_ret=float(r.median()),
        hit_rate=hit,
        ret_per_risk=(float(r.mean()) / std) if std > 0 else np.nan,
        base_rate=base_rate,
        hit_edge=(hit - base_rate) if base_rate is not None else np.nan,
    )


def evaluation_scorecard(ic, base_rate, hit_rate, top_mean, all_mean):
    """The four evaluation metrics on one line, always together.

    IC, hit edge (vs base rate), return edge (vs universe mean), and the base
    rate itself. See THE EVALUATION BAR above: these are reported as a set
    because no one of them is the verdict.
    """
    ic_s = "n/a" if ic is None or np.isnan(ic) else f"{ic:+.3f}"
    return (f"IC {ic_s} | top-decile hit {hit_rate:.1%} vs base {base_rate:.1%} "
            f"(edge {hit_rate - base_rate:+.1%}) | return {top_mean:+.2%} vs "
            f"{all_mean:+.2%} (edge {top_mean - all_mean:+.2%})")


def format_trade_stats(s, label=""):
    """One-line rendering, always showing the base rate next to the hit rate."""
    head = f"{label}: " if label else ""
    if not s["n_trades"]:
        return f"{head}no trades"
    hit = f"hit {s['hit_rate']:.1%}"
    if s.get("base_rate") is not None and not np.isnan(s.get("hit_edge", np.nan)):
        hit += f" (base {s['base_rate']:.1%}, edge {s['hit_edge']:+.1%})"
    rpr = "n/a" if np.isnan(s["ret_per_risk"]) else f"{s['ret_per_risk']:.2f}"
    return (f"{head}n={s['n_trades']} mean {s['mean_ret']:+.2%} "
            f"med {s['median_ret']:+.2%} {hit} ret/risk {rpr}")


def format_signal_stats(s, label=""):
    head = f"{label}: " if label else ""
    if not s["n"]:
        return f"{head}no rows"
    return (f"{head}n={s['n']} top{int((1 - TOP_QUANTILE) * 100)}% n={s['n_top']} "
            + evaluation_scorecard(s["ic"], s["base_rate"], s["hit_rate"],
                                   s["top_mean"], s["all_mean"]))
