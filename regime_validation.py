"""
Multi-year validation of the foreign-flow regime rule on JCI.

Rule (ex-ante, same as the 9-month backtest, no tuning):
  - Signal: 20-day rolling sum of market-wide daily foreign net purchase.
  - Position: long JCI when rolling sum > 0 as of day t, else cash (0%).
  - Execution: position decided at close of t applies to the return of t+1
    (no lookahead).
  - Costs: 0.20% per position change (one side).

Data:
  - idx_foreign_daily.csv  (harvested from IDX DigitalStatistic API, billion IDR)
  - jci_daily.csv          (IDX:COMPOSITE daily via tvDatafeed)

Outputs overall + per-year stats vs buy-and-hold, and saves the daily
strategy frame to regime_validation_daily.csv.
"""
import numpy as np
import pandas as pd

ROLL = 20
COST_PER_SWITCH = 0.002


def stats(daily_ret, label):
    """CAGR, cumulative-return-derived max drawdown, and a NON-annualised
    ret_per_risk (daily_mean / daily_std). ret_per_risk is NOT a Sharpe ratio
    -- it carries no sqrt(252) or other annualising scalar and must not be
    compared against Sharpe benchmarks or any annualized-vol figure."""
    r = daily_ret.dropna()
    if len(r) < 30:
        return None
    ann_ret = (1 + r).prod() ** (252 / len(r)) - 1
    curve = (1 + r).cumprod()
    dd = (curve / curve.cummax() - 1).min()
    daily_mean = float(r.mean())
    daily_std = float(r.std())
    ret_per_risk = daily_mean / daily_std if daily_std > 0 else np.nan
    return dict(label=label, n_days=len(r), cagr=round(float(ann_ret), 4),
                max_dd=round(float(dd), 4),
                daily_mean=round(daily_mean, 6), daily_std=round(daily_std, 6),
                ret_per_risk=round(float(ret_per_risk), 3))


def main():
    flow = pd.read_csv("idx_foreign_daily.csv", parse_dates=["date"])
    jci = pd.read_csv("jci_daily.csv", parse_dates=["date"])

    df = jci.merge(flow, on="date", how="inner").sort_values("date").reset_index(drop=True)
    print(f"merged: {len(df)} days {df['date'].min().date()}..{df['date'].max().date()}")

    df["ret"] = df["close"].pct_change()
    df["roll_flow"] = df["foreign_net_purchase_b"].rolling(ROLL, min_periods=ROLL).sum()
    df["pos"] = (df["roll_flow"] > 0).astype(float)
    # position decided at close t -> earns return of t+1
    df["pos_lag"] = df["pos"].shift(1)
    df["switch"] = (df["pos_lag"].diff().abs() > 0).astype(float)
    df["strat_ret"] = df["pos_lag"] * df["ret"] - df["switch"] * COST_PER_SWITCH

    valid = df.dropna(subset=["strat_ret", "ret", "roll_flow"]).copy()
    n_switches = int(valid["switch"].sum())
    pct_long = float(valid["pos_lag"].mean())

    print(f"switches: {n_switches}, time in market: {pct_long:.0%}")
    print("\n=== OVERALL ===")
    for s in (stats(valid["strat_ret"], "regime strategy"),
              stats(valid["ret"], "buy & hold JCI")):
        if s:
            print(f"  {s['label']:16s} CAGR={s['cagr']:+.1%}  maxDD={s['max_dd']:.1%}  "
                  f"ret_per_risk={s['ret_per_risk']:+.3f} (daily, NOT Sharpe, unannualised)")

    # regime-conditional JCI returns (does the signal separate good/bad days?)
    on = valid.loc[valid["pos_lag"] == 1, "ret"]
    off = valid.loc[valid["pos_lag"] == 0, "ret"]
    print(f"\nJCI daily return when regime ON : mean={on.mean()*1e4:+.1f} bps  "
          f"(n={len(on)}, ann={(on.mean()*252):+.1%})")
    print(f"JCI daily return when regime OFF: mean={off.mean()*1e4:+.1f} bps  "
          f"(n={len(off)}, ann={(off.mean()*252):+.1%})")
    # Descriptive mean gap only -- no t-stat. Regime state is serially
    # persistent (ON/OFF runs, not iid daily draws), so a Welch-style SE
    # understates the true standard error and overstates significance. No
    # dependence-aware method (block bootstrap, Newey-West, etc.) exists
    # elsewhere in this repo, and building one for a validation utility
    # script would be over-engineering; report the gap and stop there.
    print(f"ON-OFF mean gap: {(on.mean()-off.mean())*1e4:+.1f} bps/day "
          f"(descriptive only -- no significance test, see note above)")

    print("\n=== BY YEAR ===")
    valid["year"] = valid["date"].dt.year
    rows = []
    for y, g in valid.groupby("year"):
        s = stats(g["strat_ret"], "strat")
        b = stats(g["ret"], "bh")
        if s and b:
            rows.append((y, s["cagr"], b["cagr"], s["ret_per_risk"], b["ret_per_risk"],
                         float(g["pos_lag"].mean())))
    print("  ret/risk columns below are daily, NOT Sharpe (no annualising scalar).")
    print(f"  {'year':4s} {'strat':>8s} {'b&h':>8s} {'stratR/R':>9s} {'bhR/R':>7s} {'inMkt':>6s}")
    for y, sc, bc, sr, br, im in rows:
        print(f"  {y}  {sc:+8.1%} {bc:+8.1%} {sr:9.3f} {br:7.3f} {im:6.0%}")

    valid.to_csv("regime_validation_daily.csv", index=False)
    print("\nSaved regime_validation_daily.csv")


if __name__ == "__main__":
    main()
