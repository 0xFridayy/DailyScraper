"""Read the inventory chart the way a bandarmologist does, but numerically.

For every ticker-day this builds the state of the cumulative per-broker
inventory curve as of the close of T-1, then labels what happened on T (ARA /
ARB). Nothing uses information from T or later.

The core quantity is a broker's trailing net accumulation expressed in **days of
average traded volume** — the same thing your eye does when it judges whether a
rising line on the inventory chart is "big" relative to how much the stock
actually trades:

    acc[b, W] = sum(nlot[b] over last W days) / avg_daily_lot_20

On top of that: how concentrated the accumulation is, how persistent, whether it
is accelerating, where the accumulator's average cost sits versus the current
price, and whether the accumulation is happening while retail sells (absorption)
and while the price is flat or falling (divergence).

Outputs panel.parquet.
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = (5, 20, 60)
ARB_LIM = -0.15          # re-verified against the full universe in scan_ara_arb.py

GROUPS = {
    "retail": ["XL", "XC", "YP", "PD"],
    "tier1": ["AK", "BK", "ZP", "RX"],
    "tier2": ["YP", "YU", "AI", "CC"],
    "smart": ["IF", "BB", "AZ"],
    "cp": ["CP"], "mg": ["MG"], "ss": ["SS"],
}


def ara_limit(p):
    return np.where(p < 200, 0.35, np.where(p <= 5000, 0.25, 0.20))


def _tick(p):
    return np.where(p < 200, 1, np.where(p < 500, 2, np.where(p < 2000, 5,
                    np.where(p < 5000, 10, 25))))


def _snap(raw, down):
    t = _tick(raw)
    v = np.floor(raw / t) * t if down else np.ceil(raw / t) * t
    t2 = _tick(v)
    v2 = np.floor(raw / t2) * t2 if down else np.ceil(raw / t2) * t2
    return np.where(t2 == t, v, v2)


def ara_price(prev):
    return _snap(prev * (1 + ara_limit(prev)), True)


def arb_price(prev):
    return _snap(prev * (1 + ARB_LIM), False)


def roll_sum(a, w):
    """Trailing w-row sum down axis 0, NaN until w rows exist."""
    c = np.cumsum(np.vstack([np.zeros((1,) + a.shape[1:]), a]), axis=0)
    out = c[w:] - c[:-w]
    pad = np.full((w - 1,) + a.shape[1:], np.nan)
    return np.vstack([pad, out])


def roll_mean_1d(a, w, minp=None):
    minp = minp or w
    s = pd.Series(a)
    return s.rolling(w, min_periods=minp).mean().to_numpy()


def build():
    o = pd.read_parquet(os.path.join(HERE, "ohlc.parquet"))
    b = pd.read_parquet(os.path.join(HERE, "broker_daily.parquet"))
    o["date"] = pd.to_datetime(o["date"])
    b["date"] = pd.to_datetime(b["date"])
    o = o.sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"ohlc {o.shape}, broker {b.shape}, tickers {o.ticker.nunique()}")

    brokers = np.array(sorted(b.broker.unique()))
    bidx = {c: i for i, c in enumerate(brokers)}
    gmask = {g: np.array([c in codes for c in brokers]) for g, codes in GROUPS.items()}

    b["bi"] = b.broker.map(bidx).astype(np.int32)
    out = []
    for tk, ob in o.groupby("ticker", sort=False):
        ob = ob.sort_values("date")
        dates = ob.date.to_numpy()
        nd = len(dates)
        if nd < 70:
            continue
        di = {d: i for i, d in enumerate(dates)}
        sub = b[b.ticker == tk]
        if len(sub) == 0:
            continue
        ri = sub.date.map(di).to_numpy()
        ok = ~pd.isna(ri)
        ri = ri[ok].astype(np.int32)
        ci = sub.bi.to_numpy()[ok]
        M = {}
        for f in ["nlot", "nval", "blot", "bval", "slot", "sval"]:
            m = np.zeros((nd, len(brokers)), dtype=np.float64)
            np.add.at(m, (ri, ci), sub[f].to_numpy()[ok].astype(np.float64))
            M[f] = m

        close = ob.close.to_numpy(dtype=float)
        high = ob.high.to_numpy(dtype=float)
        low = ob.low.to_numpy(dtype=float)
        openp = ob.open.to_numpy(dtype=float)
        vol = ob.volume.to_numpy(dtype=float)
        adv_lot = roll_mean_1d(vol / 100.0, 20, 10)
        denom = np.where(adv_lot > 0, adv_lot, np.nan)[:, None]

        f = {"date": dates, "ticker": tk, "open": openp, "high": high,
             "low": low, "close": close, "volume": vol}

        for W in WINDOWS:
            cn = roll_sum(M["nlot"], W)            # net lot per broker over W
            acc = cn / (denom * W ** 0.5)          # scaled so windows are comparable
            pos = np.where(acc > 0, acc, 0.0)
            srt = np.sort(acc, axis=1)
            f[f"top1_acc_{W}"] = srt[:, -1]
            f[f"top3_acc_{W}"] = srt[:, -3:].sum(axis=1)
            f[f"bot1_acc_{W}"] = srt[:, 0]
            f[f"bot3_acc_{W}"] = srt[:, :3].sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                tot = pos.sum(axis=1)
                f[f"hhi_acc_{W}"] = np.where(tot > 0, ((pos / tot[:, None]) ** 2).sum(axis=1), np.nan)
            f[f"n_acc_{W}"] = (acc > 0.5).sum(axis=1).astype(float)
            f[f"n_dist_{W}"] = (acc < -0.5).sum(axis=1).astype(float)
            f[f"net_acc_{W}"] = acc.sum(axis=1)
            for g, m in gmask.items():
                f[f"{g}_acc_{W}"] = acc[:, m].sum(axis=1)
            # cost basis of the current top accumulator
            cv = roll_sum(M["nval"], W)
            top = np.argmax(acc, axis=1)
            rows_ = np.arange(nd)
            tl, tv = cn[rows_, top], cv[rows_, top]
            with np.errstate(invalid="ignore", divide="ignore"):
                cost = np.where(tl > 0, tv / (tl * 100.0), np.nan)
                f[f"px_vs_cost_{W}"] = close / cost - 1
            # HAKA proxy: does the top accumulator pay UP versus the market?
            # (a market-wide buy-vwap vs sell-vwap comparison is structurally
            # zero -- every buy is someone's sell -- so it must be per broker.)
            cbl, cbv = roll_sum(M["blot"], W), roll_sum(M["bval"], W)
            csl, csv = roll_sum(M["slot"], W), roll_sum(M["sval"], W)
            mkt_lot = cbl.sum(axis=1)
            mkt_val = cbv.sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                mkt_vwap = np.where(mkt_lot > 0, mkt_val / (mkt_lot * 100.0), np.nan)
                tb_lot, tb_val = cbl[rows_, top], cbv[rows_, top]
                acc_vwap = np.where(tb_lot > 0, tb_val / (tb_lot * 100.0), np.nan)
                f[f"haka_{W}"] = acc_vwap / mkt_vwap - 1
                # and does the biggest seller dump below vwap?
                bot = np.argmin(acc, axis=1)
                ts_lot, ts_val = csl[rows_, bot], csv[rows_, bot]
                dist_vwap = np.where(ts_lot > 0, ts_val / (ts_lot * 100.0), np.nan)
                f[f"dumping_{W}"] = dist_vwap / mkt_vwap - 1

            # share of everything traded in the window that the top accumulator
            # took off the market -- the "how much of the float did he eat" read
            win_lot = roll_sum((vol / 100.0)[:, None], W)[:, 0]
            with np.errstate(invalid="ignore", divide="ignore"):
                f[f"acc_share_{W}"] = np.where(win_lot > 0, tl / win_lot, np.nan)
            # curve smoothness: a clean staircase has net close to gross
            gross_b = roll_sum(np.abs(M["nlot"]), W)
            with np.errstate(invalid="ignore", divide="ignore"):
                gb = gross_b[rows_, top]
                f[f"acc_efficiency_{W}"] = np.where(gb > 0, tl / gb, np.nan)
            # bandar up while retail down
            f[f"absorb_{W}"] = f[f"top1_acc_{W}"] - f[f"retail_acc_{W}"]

        # persistence + acceleration of the current top-20d accumulator
        cn20 = roll_sum(M["nlot"], 20)
        acc20 = cn20 / (denom * 20 ** 0.5)
        top20 = np.argmax(acc20, axis=1)
        rows = np.arange(nd)
        posday = (M["nlot"] > 0).astype(float)
        pd20 = roll_sum(posday, 20) / 20.0
        f["acc_persist"] = pd20[rows, top20]
        f["top_code_20"] = brokers[top20]          # who the accumulator actually is
        f["bot_code_20"] = brokers[np.argmin(acc20, axis=1)]
        cn5 = roll_sum(M["nlot"], 5)
        with np.errstate(invalid="ignore", divide="ignore"):
            f["acc_accel"] = (cn5[rows, top20] * 4.0) / np.abs(cn20[rows, top20])
        f["top_is_stable"] = np.r_[np.nan, (top20[1:] == top20[:-1]).astype(float)]
        f["n_active_brk"] = (M["nlot"] != 0).sum(axis=1).astype(float)
        gross = np.abs(M["nval"]).sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            f["conc_today"] = np.max(np.abs(M["nval"]), axis=1) / np.where(gross > 0, gross, np.nan)
        out.append(pd.DataFrame(f))

    d = pd.concat(out, ignore_index=True)
    print(f"raw panel {d.shape}")

    # ---------------- price features + labels ----------------------------
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = d.groupby("ticker")
    d["prev_close"] = g["close"].shift(1)
    d["gap_days"] = (d.date - g["date"].shift(1)).dt.days
    d["contig"] = d.gap_days <= 5
    d["ret"] = np.where(d.contig, d.close / d.prev_close - 1, np.nan)
    pc = d.prev_close.to_numpy(dtype=float)
    d["ara_px"] = ara_price(pc)
    d["arb_px"] = arb_price(pc)
    d["ARA"] = (d.close >= d.ara_px - 1e-6) & d.contig & d.prev_close.notna()
    d["ARB"] = (d.close <= d.arb_px + 1e-6) & d.contig & d.prev_close.notna()
    d["ARA_touch"] = (d.high >= d.ara_px - 1e-6) & d.contig & d.prev_close.notna()
    d["ARB_touch"] = (d.low <= d.arb_px + 1e-6) & d.contig & d.prev_close.notna()

    for k in [1, 2, 3, 5, 10, 20, 60]:
        d[f"r{k}"] = g["close"].transform(lambda s, k=k: s / s.shift(k) - 1)
    d["up_streak"] = g["ret"].transform(
        lambda s: s.gt(0).groupby((~s.gt(0)).cumsum()).cumcount())
    for k in [3, 5, 20]:
        d[f"ara_c{k}"] = g["ARA"].transform(lambda s, k=k: s.rolling(k, min_periods=1).sum())
        d[f"arb_c{k}"] = g["ARB"].transform(lambda s, k=k: s.rolling(k, min_periods=1).sum())
    rng = (d.high - d.low).replace(0, np.nan)
    d["clv"] = (d.close - d.low) / rng
    d["rng"] = rng / d.close
    d["gap"] = d.open / d.prev_close - 1
    d["hi20"] = d.close / g["high"].transform(lambda s: s.rolling(20, min_periods=5).max()) - 1
    d["hi60"] = d.close / g["high"].transform(lambda s: s.rolling(60, min_periods=10).max()) - 1
    d["lo20"] = d.close / g["low"].transform(lambda s: s.rolling(20, min_periods=5).min()) - 1
    d["vr5"] = d.volume / g["volume"].transform(lambda s: s.rolling(5, min_periods=3).mean())
    d["vr20"] = d.volume / g["volume"].transform(lambda s: s.rolling(20, min_periods=5).mean())
    d["rv10"] = g["ret"].transform(lambda s: s.rolling(10, min_periods=5).std())
    d["rv20"] = g["ret"].transform(lambda s: s.rolling(20, min_periods=8).std())
    d["rv60"] = g["ret"].transform(lambda s: s.rolling(60, min_periods=20).std())
    d["compress"] = d.rv20 / d.rv60
    d["rng20"] = g["rng"].transform(lambda s: s.rolling(20, min_periods=8).mean())
    d["rng_compress"] = d.rng / d.rng20
    d["logp"] = np.log(d.close.clip(lower=1))
    d["band"] = ara_limit(d.close.to_numpy(dtype=float))
    d["turn20"] = g.apply(lambda x: (x.close * x.volume / 1e9).rolling(20, min_periods=5).mean(),
                          include_groups=False).reset_index(level=0, drop=True)
    d["logturn20"] = np.log1p(d.turn20)
    d["dead"] = (d.volume == 0).astype(float)

    for t in ["ARA", "ARB", "ARA_touch", "ARB_touch"]:
        d[f"y_{t}"] = g[t].shift(-1)
    d["y_contig"] = g["contig"].shift(-1)
    d["y_date"] = g["date"].shift(-1)
    d["y_open"] = g["open"].shift(-1)
    d["y_high"] = g["high"].shift(-1)
    d["y_low"] = g["low"].shift(-1)
    d["y_close"] = g["close"].shift(-1)

    d = d[d.y_contig.fillna(False).astype(bool) & d.prev_close.notna()].copy()
    d.to_parquet(os.path.join(HERE, "panel.parquet"), compression="zstd")
    print(f"panel {d.shape}  tickers {d.ticker.nunique()}  "
          f"{d.date.min().date()}..{d.date.max().date()}")
    for t in ["ARA", "ARB", "ARA_touch", "ARB_touch"]:
        print(f"  y_{t}: {int(d[f'y_{t}'].sum())} events "
              f"({d[f'y_{t}'].mean()*100:.3f}%)")
    return d


if __name__ == "__main__":
    build()
