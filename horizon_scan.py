"""
Multi-horizon signal-quality scan on the neobdm.db panel, quarantine-filtered.

Prices come from price_audit.clean_panel(), not from price_history directly:
~12% of that table is cross-ticker contaminated, and the naive fix (filter the
bad rows, then shift) silently bridges the holes into targets that never
happened. Both problems are handled there, once.

Question: does the broker-flow signal show up at longer horizons than T+1,
and does it show up better against a "max upside within the window" target
(what a trailing-stop swing trade actually captures) than against a
close-to-close target?

Metrics deliberately avoid the sqrt(252)-on-per-trade-returns problem:
  - IC        : Spearman rank corr between prediction and realized target
  - hit_rate  : fraction of top-decile predictions with positive return
  - top_ret   : mean realized target among top-decile predictions
  - base_ret  : mean realized target across ALL rows (the do-nothing baseline)
  - edge      : top_ret - base_ret   <-- the number that matters
"""
import sqlite3, warnings
import numpy as np, pandas as pd
from xgboost import XGBRegressor
from walk_forward_backtest import (
    DB_PATH, _broker_day_aggregates, _broker_correlation_1d, FEATURES, XGB_PARAMS,
)
from price_audit import clean_panel
from signal_metrics import signal_stats

warnings.filterwarnings("ignore")

# konglomerat -> broker codes (from project notes)
CLUSTERS = {
    "prajogo": ["DX", "NI"], "bakrie": ["LG", "DH"],
    "hengky": ["CP", "YB", "AO", "YP", "CC", "HP", "PD"],
    "hapsoro": ["YP", "AK", "SQ"], "hashim": ["YP", "CC", "AK"],
    "hajiisam": ["CC", "SQ"], "sinarmas": ["ZP", "CS"], "lippo": ["PD", "NI"],
}
FOREIGN = ["CS", "ZP", "AK", "KZ", "BK", "ML", "RX", "DB", "MS", "YU"]
HORIZONS = [1, 3, 5, 10, 20]


def cluster_features(bf):
    bf = bf.copy()
    tot = bf.groupby(["ticker", "date"])["netval"].apply(lambda s: s.abs().sum()).rename("abs_tot")
    out = tot.reset_index()
    for name, codes in CLUSTERS.items():
        sub = bf[bf["broker_code"].isin(codes)].groupby(["ticker", "date"])["netval"].sum()
        out = out.merge(sub.rename(f"cl_{name}").reset_index(), on=["ticker", "date"], how="left")
        out[f"cl_{name}"] = out[f"cl_{name}"].fillna(0) / out["abs_tot"].replace(0, np.nan)
    sub = bf[bf["broker_code"].isin(FOREIGN)].groupby(["ticker", "date"])["netval"].sum()
    out = out.merge(sub.rename("cl_foreign").reset_index(), on=["ticker", "date"], how="left")
    out["cl_foreign"] = out["cl_foreign"].fillna(0) / out["abs_tot"].replace(0, np.nan)
    # max single-cluster conviction
    ccols = [c for c in out.columns if c.startswith("cl_")]
    out["cl_max"] = out[ccols].max(axis=1)
    return out.drop(columns=["abs_tot"])


def build(conn):
    bf = pd.read_sql("SELECT date,ticker,broker_code,netval FROM broker_flow", conn)

    agg = _broker_day_aggregates(bf)
    agg = agg.merge(_broker_correlation_1d(bf), on=["ticker", "date"], how="left")
    agg = agg.merge(cluster_features(bf), on=["ticker", "date"], how="left")

    # Quarantine-filtered prices with gap-guarded forward/backward returns.
    # Reading price_history directly here would put ~12% contaminated rows into
    # both X and y, and computing shift(-h) after filtering would bridge the
    # removed rows into targets that never happened - see price_audit.py.
    px = clean_panel(conn, horizons=HORIZONS, lags=(1, 5, 10), extremes=True)
    g = px.groupby("ticker")
    px["momentum_1d"] = px["lag_1"]
    px["momentum_5d"] = px["lag_5"]
    px["vol_ma5"] = g["volume"].transform(lambda s: s.shift(1).rolling(5).mean())
    px["vol_ma5"] = px["vol_ma5"].where(px["lag_5"].notna())
    px["volume_ratio"] = px["volume"] / px["vol_ma5"]
    px["atr_pct"] = g.apply(
        lambda d: ((d["high"] - d["low"]) / d["close"]).rolling(10).mean()
    ).reset_index(level=0, drop=True)
    px["atr_pct"] = px["atr_pct"].where(px["lag_10"].notna())

    for h in HORIZONS:
        px[f"ret_{h}"] = px[f"fwd_{h}"]

    keep = ["ticker", "date", "momentum_1d", "momentum_5d", "volume_ratio", "atr_pct"]
    keep += [f"ret_{h}" for h in HORIZONS] + [f"max_{h}" for h in HORIZONS] + [f"mdd_{h}" for h in HORIZONS]
    return agg.merge(px[keep], on=["ticker", "date"], how="inner").sort_values("date").reset_index(drop=True)


BROKER_F = ["broker_concentration", "net_flow_total", "n_brokers", "net_buy_ratio",
            "retail_presence_pct", "broker_correlation_1d"]
CLUSTER_F = [f"cl_{k}" for k in CLUSTERS] + ["cl_foreign", "cl_max"]
PRICE_F = ["momentum_1d", "momentum_5d", "volume_ratio", "atr_pct"]

SETS = {
    "price_only": PRICE_F,
    "broker_only": BROKER_F,
    "broker+cluster": BROKER_F + CLUSTER_F,
    "full": BROKER_F + CLUSTER_F + PRICE_F,
}


def walk_forward(panel, feats, target, train_min=60, test_window=10):
    df = panel.dropna(subset=[target] + feats).copy()
    dates = sorted(df["date"].unique())
    preds, actuals, test_dates = [], [], []
    te = train_min
    while te + test_window <= len(dates):
        tr = df[df["date"].isin(dates[:te])]
        ts = df[df["date"].isin(dates[te:te + test_window])]
        if len(tr) > 100 and len(ts) > 0:
            sp = int(len(tr) * 0.85)
            m = XGBRegressor(**XGB_PARAMS)
            m.fit(tr[feats].iloc[:sp], tr[target].iloc[:sp],
                  eval_set=[(tr[feats].iloc[sp:], tr[target].iloc[sp:])], verbose=False)
            preds.append(m.predict(ts[feats]))
            actuals.append(ts[target].values)
            test_dates.append(ts["date"].values)
        te += test_window
    if not preds:
        return None
    s = signal_stats(np.concatenate(preds), np.concatenate(actuals),
                     groups=np.concatenate(test_dates))
    return dict(n=s["n"], ic=s["ic"], daily_ic=s["daily_ic"],
                daily_ic_median=s["daily_ic_median"], n_top=s["n_top"],
                hit=s["hit_rate"], base_hit=s["base_rate"], hit_edge=s["hit_edge"],
                top_ret=s["top_mean"], base_ret=s["all_mean"], edge=s["edge"])


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    panel = build(conn)
    conn.close()
    print(f"Panel: {len(panel)} rows, {panel['date'].nunique()} dates, {panel['ticker'].nunique()} tickers\n")

    for tkind in ["ret", "max"]:
        print(f"===== TARGET = {tkind} (close-to-close)" if tkind == "ret"
              else f"===== TARGET = {tkind} (max upside in window)")
        rows = []
        for h in HORIZONS:
            for sname, feats in SETS.items():
                r = walk_forward(panel, feats, f"{tkind}_{h}")
                if r:
                    rows.append(dict(h=h, features=sname, **r))
        d = pd.DataFrame(rows)
        d["ic"] = d["ic"].round(4)
        for c in ["hit", "base_hit", "hit_edge", "top_ret", "base_ret", "edge"]:
            d[c] = (d[c] * 100).round(2)
        print(d.to_string(index=False), "\n")

    print("=== realistic stop width: median worst-drawdown within window ===")
    for h in HORIZONS:
        s = panel[f"mdd_{h}"].dropna()
        print(f"  h={h:2d}d  median={s.median()*100:6.2f}%  p25={s.quantile(.25)*100:6.2f}%")
