"""
Fractal + volume pattern detection on k-line data.

Detects:
1. Bill Williams fractals (5-bar reversals)
2. Volume breakout confirmation (vol spike on breakout bar)
3. Consolidation breakouts (low volatility then breakout)
4. Support/resistance bounces

Per ADES visual:
- Oct 2025-Jan 2026: consolidation + volume buildup
- Late Jan: breakout (high vol, close above resistance)
- Feb-Apr: impulsive up with 2-bar pullbacks, then volume spike continues
- Apr-May: consolidation into support
- May-Jun: breakout + acceleration (institution/smart money in)

Outputs tradeable setups: (date, ticker, pattern_type, signal, strength_0to1)
"""
import sqlite3
import numpy as np
import pandas as pd

DB = "txchart_history.db"

def load_data():
    con = sqlite3.connect(DB)
    px = pd.read_sql("SELECT * FROM ohlcv ORDER BY ticker, date", con)
    con.close()
    return px

def compute_patterns(ticker, group):
    """Per-ticker: detect patterns in px OHLCV data."""
    g = group.sort_values("date").reset_index(drop=True).copy()
    n = len(g)

    # compute derived features
    g["range"] = g["high"] - g["low"]
    g["body"] = abs(g["close"] - g["open"])
    g["upper_wick"] = g["high"] - g[["open", "close"]].max(axis=1)
    g["lower_wick"] = g[["open", "close"]].min(axis=1) - g["low"]

    g["ret"] = g["close"].pct_change()
    g["vol_ma20"] = g["volume"].rolling(20, min_periods=10).mean()
    g["vol_zscore"] = (g["volume"] - g["vol_ma20"]) / (g["vol_ma20"].std() + 1)

    g["high_20"] = g["high"].rolling(20, min_periods=5).max()
    g["low_20"] = g["low"].rolling(20, min_periods=5).min()
    g["atr"] = g["range"].rolling(14, min_periods=7).mean()

    patterns = []

    for i in range(5, n):
        row = g.iloc[i]
        date = row["date"]

        # 1. Bill Williams fractal: 5-bar reversal
        if i >= 4:
            frac_up = (g.iloc[i-2]["high"] < g.iloc[i-1]["high"] > g.iloc[i]["high"] and
                       g.iloc[i-3]["high"] < g.iloc[i-1]["high"] and
                       g.iloc[i-4]["high"] < g.iloc[i-1]["high"])
            frac_dn = (g.iloc[i-2]["low"] > g.iloc[i-1]["low"] < g.iloc[i]["low"] and
                       g.iloc[i-3]["low"] > g.iloc[i-1]["low"] and
                       g.iloc[i-4]["low"] > g.iloc[i-1]["low"])
            if frac_up or frac_dn:
                vol_conf = min(1.0, row["vol_zscore"] / 2)
                patterns.append({
                    "date": date, "ticker": ticker, "pattern": "fractal",
                    "type": "up_reversal" if frac_up else "dn_reversal",
                    "strength": 0.4 + 0.3 * vol_conf
                })

        # 2. Volume breakout: price closes above 20-bar high + vol spike
        if i >= 5:
            prev_high = g.iloc[i-1]["high_20"]
            vol_spike = row["vol_zscore"] > 1.0
            breakout_up = row["close"] > prev_high and vol_spike
            breakout_dn = row["close"] < g.iloc[i-1]["low_20"] and vol_spike

            if breakout_up or breakout_dn:
                strength = 0.5 + 0.2 * min(row["vol_zscore"] / 3, 1.0) + \
                           0.3 * (row["body"] / max(row["atr"], 0.01))
                patterns.append({
                    "date": date, "ticker": ticker, "pattern": "vol_breakout",
                    "type": "breakout_up" if breakout_up else "breakout_dn",
                    "strength": min(strength, 1.0)
                })

        # 3. Consolidation + breakout (low vol, then vol + range expansion)
        if i >= 10:
            vol_last5 = g.iloc[i-4:i]["volume"].mean()
            vol_before5 = g.iloc[i-9:i-4]["volume"].mean()
            range_last3 = g.iloc[i-2:i]["range"].mean()
            range_before = g.iloc[i-7:i-4]["range"].mean()

            consol = vol_last5 < vol_before5 * 0.8 and range_last3 < range_before * 0.7
            if consol and row["vol_zscore"] > 0.5 and row["body"] > row["atr"]:
                breakout_dir = "up" if row["close"] > g.iloc[i-1]["close"] else "dn"
                patterns.append({
                    "date": date, "ticker": ticker, "pattern": "consolidation_break",
                    "type": f"break_{breakout_dir}",
                    "strength": 0.6 + 0.2 * min(row["vol_zscore"] / 2, 1.0)
                })

        # 4. Support/resistance bounce
        if i >= 3:
            touch_support = row["low"] < g.iloc[i-1]["low_20"] * 1.01 and row["close"] > row["low"] * 1.02
            touch_resist = row["high"] > g.iloc[i-1]["high_20"] * 0.99 and row["close"] < row["high"] * 0.98

            if (touch_support or touch_resist) and row["body"] > row["atr"] * 0.5:
                vol_conf = min(1.0, max(0.3, row["vol_zscore"] / 1.5))
                patterns.append({
                    "date": date, "ticker": ticker, "pattern": "support_bounce",
                    "type": "bounce_up" if touch_support else "bounce_dn",
                    "strength": 0.5 + 0.3 * vol_conf
                })

    return pd.DataFrame(patterns) if patterns else pd.DataFrame()

def main():
    px = load_data()
    all_patterns = []

    for ticker, group in px.groupby("ticker"):
        pats = compute_patterns(ticker, group)
        if not pats.empty:
            all_patterns.append(pats)

    patterns_df = pd.concat(all_patterns, ignore_index=True)
    patterns_df = patterns_df.sort_values(["date", "strength"], ascending=[True, False])

    # summary by ticker
    print("Pattern summary by ticker:")
    summary = patterns_df.groupby(["ticker", "pattern"]).size().unstack(fill_value=0)
    print(summary.to_string())

    print("\n\nTop 30 strongest patterns:")
    top = patterns_df.nlargest(30, "strength")[["date", "ticker", "pattern", "type", "strength"]]
    print(top.to_string(index=False))

    patterns_df.to_csv("patterns_detected.csv", index=False)
    print(f"\nSaved {len(patterns_df)} patterns to patterns_detected.csv")

if __name__ == "__main__":
    main()
