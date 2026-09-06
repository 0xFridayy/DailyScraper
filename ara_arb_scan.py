"""
ARA / ARB next-day scanner for the NeoBDM broker-stalker universe.

Built 2026-08-22 from neobdm.db. Everything here is computed from information
available at the close of T-1 and scores the probability of an auto-rejection
close on T, so the signal is actionable at the T open.

Empirically confirmed against price_history (not taken from memory):
  ARA band = +35% (prev close < Rp200) / +25% (Rp200-5000) / +20% (> Rp5000)
  ARB band = flat -15% regardless of price tier
Auto-rejection prices snap to the IDX tick ladder (1/2/5/10/25), which is why
the label compares against an exact tick price rather than a raw percentage.

Two data-quality problems in neobdm.db are corrected here; both are scraper
bugs, not market events, and both badly distort any naive backtest:
  1. price_history contains bars copied across tickers (one stock's OHLCV
     written into several tickers on the same date). KIOS and RSGK are >55%
     contaminated and are dropped outright; elsewhere the offending bars are
     removed, ~6% of rows in total.
  2. broker_flow contains rows where one broker's market-wide net is written
     into every ticker on that date (~9% of rows). Detected as an identical
     (date, broker_code, netval) triple spanning >=5 tickers.

Headline backtest result (see the accompanying report): the ARB side carries a
real out-of-sample edge, the ARA side does not. Use accordingly.

Usage:  python ara_arb_scan.py [--top 10]
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

DB = r"C:/Users/jason/Desktop/VsCode/Claude/neobdm.db"
ARB_LIM = -0.15

GROUPS = {
    'tier1': ['AK', 'BK', 'ZP', 'RX'],
    'tier2': ['YP', 'YU', 'AI', 'CC'],
    'retail': ['XL', 'XC', 'YP', 'PD'],
    'smart': ['IF', 'BB', 'AZ'],
    'cp': ['CP'], 'mg': ['MG'], 'ss': ['SS'],
}
_OWNER_STOCKS = {
    'Hashim': 'KIOS DOOH WIFI COIN',
    'Hapsoro': 'PADI PSKT MINA UANG SINI RATU RAJA BUVA',
    'HajiIsam': 'FAST TEBE JARR PGUN',
    'EMTEK': 'RSGK CASS SAME BUKA SCMA BBHI EMTK',
    'Prajogo': 'PTRO CDIA CUAN BRPT TPIA BREN',
    'Bakrie': 'VIVA JGLE BTEL ELTY MDIA ALJI DEWA BNBR ENRG VKTR BUMI BRMS',
    'Aguan': 'PDPP JIHD ERAL INPC ERAA CBDK PANI',
}
OWNER = {t: o for o, tks in _OWNER_STOCKS.items() for t in tks.split()}
OWNER_BROKERS = {
    'Hashim': ['YP', 'CC', 'AK'], 'Hapsoro': ['CC', 'YP', 'PD', 'LG', 'ES'],
    'HajiIsam': ['CC', 'SQ'], 'EMTEK': ['BB', 'CC'], 'Prajogo': ['DX', 'NI'],
    'Bakrie': ['LG', 'DH'], 'Aguan': ['TP', 'RB', 'KI', 'PD'],
}


# ------------------------------------------------------------------ limits
def ara_limit(p):
    return 0.35 if p < 200 else (0.25 if p <= 5000 else 0.20)


def tick(p):
    return 1 if p < 200 else (2 if p < 500 else (5 if p < 2000 else (10 if p < 5000 else 25)))


def _snap(raw, down):
    t = tick(raw)
    v = np.floor(raw / t) * t if down else np.ceil(raw / t) * t
    if tick(v) != t:                       # the snap crossed a tick boundary
        t = tick(v)
        v = np.floor(raw / t) * t if down else np.ceil(raw / t) * t
    return v


def ara_price(prev):
    return _snap(prev * (1 + ara_limit(prev)), True)


def arb_price(prev):
    return _snap(prev * (1 + ARB_LIM), False)


# ------------------------------------------------------------------ loading
def load_prices(con):
    px = pd.read_sql("select * from price_history", con)
    px['date'] = pd.to_datetime(px['date'])
    px = px.sort_values(['ticker', 'date']).reset_index(drop=True)

    key = px[['open', 'high', 'low', 'close', 'volume']].round(4).astype(str).agg('|'.join, axis=1)
    px['_dup'] = px.assign(k=key).groupby(['date', 'k'])['ticker'].transform('size') > 1
    frac = px.groupby('ticker')['_dup'].mean()
    px = px[~px.ticker.isin(frac[frac > 0.25].index)].copy()

    px['k'] = px[['open', 'high', 'low', 'close', 'volume']].round(4).astype(str).agg('|'.join, axis=1)
    px['_dup'] = px.groupby(['date', 'k'])['ticker'].transform('size') > 1
    ref = px['close'].where(~px['_dup']).groupby(px['ticker']).transform(lambda s: s.ffill().bfill())
    px['valid'] = ~(px['_dup'] & ~(px['close'] / ref).between(1 / 2.5, 2.5))

    for _ in range(6):                     # iteratively drop impossible moves
        v = px[px.valid].copy()
        v['pc'] = v.groupby('ticker')['close'].shift(1)
        v['ret'] = v['close'] / v['pc'] - 1
        m = v['pc'].notna()
        lim = v.loc[m, 'pc'].map(ara_limit)
        imp = pd.Series(False, index=v.index)
        imp[m] = (v.loc[m, 'ret'] > lim + 0.01) | (v.loc[m, 'ret'] < ARB_LIM - 0.01)
        if not imp.any():
            break
        lvl = v.groupby('ticker')['close'].transform(
            lambda s: s.rolling(21, center=True, min_periods=5).median())
        kill = []
        for i in v[imp].index:
            prev_i = v.index[v.index.get_loc(i) - 1]
            di = abs(np.log(v.at[i, 'close'] / lvl[i])) if lvl[i] > 0 else 9
            dp = abs(np.log(v.at[prev_i, 'close'] / lvl[prev_i])) if lvl[prev_i] > 0 else 9
            kill.append(i if di >= dp else prev_i)
        px.loc[kill, 'valid'] = False

    d = px[px.valid].drop(columns=['_dup', 'k', 'valid']).copy()
    d['prev_close'] = d.groupby('ticker')['close'].shift(1)
    d['gap_days'] = (d['date'] - d.groupby('ticker')['date'].shift(1)).dt.days
    d = d[d.prev_close.notna()].copy()
    d['contig'] = d.gap_days <= 5
    d['ret'] = np.where(d.contig, d.close / d.prev_close - 1, np.nan)
    d['ARA'] = (d.close >= d.prev_close.map(ara_price) - 1e-6) & d.contig
    d['ARB'] = (d.close <= d.prev_close.map(arb_price) + 1e-6) & d.contig
    return d


def load_brokers(con, px):
    bf = pd.read_sql("select date,ticker,broker_code,netval from broker_flow", con)
    bf['date'] = pd.to_datetime(bf['date'])
    bf = bf[bf.date.isin(set(px.date.unique())) & bf.ticker.isin(set(px.ticker.unique()))].copy()
    span = bf.groupby(['date', 'broker_code', 'netval'])['ticker'].transform('size')
    bf = bf[~((span >= 5) & (bf.netval.abs() > 1.0))].copy()

    bf['is_ppr'] = [c in OWNER_BROKERS.get(OWNER.get(t, ''), [])
                    for t, c in zip(bf.ticker, bf.broker_code)]
    cols = {f'{g}_net': bf[bf.broker_code.isin(c)].groupby(['date', 'ticker'])['netval'].sum()
            for g, c in GROUPS.items()}
    cols['ppr_net'] = bf[bf.is_ppr].groupby(['date', 'ticker'])['netval'].sum()
    gb = bf.groupby(['date', 'ticker'])['netval']
    pos = bf[bf.netval > 0].groupby(['date', 'ticker'])['netval']
    neg = bf[bf.netval < 0].groupby(['date', 'ticker'])['netval']
    brk = pd.DataFrame({
        **cols,
        'gross': gb.apply(lambda s: s.abs().sum()), 'net_all': gb.sum(), 'n_brk': gb.size(),
        'buy_sum': pos.sum(), 'top1_buy': pos.max(), 'n_pos': pos.size(), 'n_neg': neg.size(),
        'hhi_buy': pos.apply(lambda s: ((s / s.sum()) ** 2).sum()),
    }).reset_index()

    piv = bf.pivot_table(index='date', columns=['ticker', 'broker_code'],
                         values='netval', aggfunc='sum').reindex(sorted(px.date.unique())).fillna(0.0)
    for win, name in [(5, 'inv5'), (20, 'inv20')]:
        s = piv.rolling(win, min_periods=2).sum().stack(['ticker', 'broker_code'],
                                                        future_stack=True).rename(name).reset_index()
        agg = s.groupby(['date', 'ticker'])[name].agg(['max', 'min']).rename(
            columns={'max': f'{name}_max', 'min': f'{name}_min'}).reset_index()
        brk = brk.merge(agg, on=['date', 'ticker'], how='left')
    return brk


# ------------------------------------------------------------------ features
def build(con):
    px = load_prices(con)
    brk = load_brokers(con, px)
    d = px.merge(brk, on=['date', 'ticker'], how='left').sort_values(['ticker', 'date']).reset_index(drop=True)
    g = d.groupby('ticker')
    d['turn'] = d.close * d.volume / 1e9
    d['turn20'] = g['turn'].transform(lambda s: s.rolling(20, min_periods=5).mean())
    T = d.turn20.replace(0, np.nan)

    for k in [1, 2, 3, 5, 10, 20]:
        d[f'r{k}'] = g['close'].transform(lambda s, k=k: s / s.shift(k) - 1)
    d['up_streak'] = g['ret'].transform(lambda s: s.gt(0).groupby((~s.gt(0)).cumsum()).cumcount())
    for k in [3, 5, 10, 20]:
        d[f'ara_c{k}'] = g['ARA'].transform(lambda s, k=k: s.rolling(k, min_periods=1).sum())
        d[f'arb_c{k}'] = g['ARB'].transform(lambda s, k=k: s.rolling(k, min_periods=1).sum())
    d['ara_1'], d['arb_1'] = d.ARA.astype(float), d.ARB.astype(float)
    rng = (d.high - d.low).replace(0, np.nan)
    d['clv'] = (d.close - d.low) / rng
    d['rng'] = rng / d.close
    d['gap'] = d.open / d.prev_close - 1
    d['hi20'] = d.close / g['high'].transform(lambda s: s.rolling(20, min_periods=5).max()) - 1
    d['hi60'] = d.close / g['high'].transform(lambda s: s.rolling(60, min_periods=10).max()) - 1
    d['lo20'] = d.close / g['low'].transform(lambda s: s.rolling(20, min_periods=5).min()) - 1
    d['vr5'] = d.volume / g['volume'].transform(lambda s: s.rolling(5, min_periods=3).mean())
    d['vr20'] = d.volume / g['volume'].transform(lambda s: s.rolling(20, min_periods=5).mean())
    d['rv10'] = g['ret'].transform(lambda s: s.rolling(10, min_periods=5).std())
    d['rv20'] = g['ret'].transform(lambda s: s.rolling(20, min_periods=8).std())
    d['logp'] = np.log(d.close)
    d['band'] = np.where(d.close < 200, .35, np.where(d.close <= 5000, .25, .20))
    d['logturn20'] = np.log1p(d.turn20)

    for gname in list(GROUPS) + ['ppr']:
        c = f'{gname}_net'
        d[c] = d[c].fillna(0.0)
        d[f'{gname}_1'] = d[c] / T
        for k in [3, 5, 20]:
            d[f'{gname}_{k}'] = d.groupby('ticker')[c].transform(
                lambda s, k=k: s.rolling(k, min_periods=2).sum()) / (T * np.sqrt(k))
    d['brk_gross'] = d.gross / T
    d['brk_net'] = d.net_all / T
    d['top1_share'] = d.top1_buy / d.buy_sum.replace(0, np.nan)
    d['top1_norm'] = d.top1_buy / T
    d['hhi'] = d.hhi_buy
    d['pos_ratio'] = d.n_pos / (d.n_pos + d.n_neg).replace(0, np.nan)
    for w, name in [(5, 'inv5'), (20, 'inv20')]:
        d[f'{name}_max_n'] = d[f'{name}_max'] / (T * np.sqrt(w))
        d[f'{name}_min_n'] = d[f'{name}_min'] / (T * np.sqrt(w))
    d['has_brk'] = d.n_brk.notna().astype(float)

    for t in ['ARA', 'ARB']:
        d[f'y_{t}'] = d.groupby('ticker')[t].shift(-1)
    d['y_contig'] = d.groupby('ticker')['contig'].shift(-1)
    return d.copy()


FEATS = (['r1', 'r2', 'r3', 'r5', 'r10', 'r20', 'up_streak', 'ara_1', 'arb_1',
          'ara_c3', 'ara_c5', 'ara_c10', 'ara_c20', 'arb_c3', 'arb_c5', 'arb_c10', 'arb_c20',
          'clv', 'rng', 'gap', 'hi20', 'hi60', 'lo20', 'vr5', 'vr20', 'rv10', 'rv20',
          'logp', 'band', 'logturn20', 'brk_gross', 'brk_net', 'top1_share', 'top1_norm',
          'hhi', 'pos_ratio', 'n_brk', 'inv5_max_n', 'inv5_min_n', 'inv20_max_n',
          'inv20_min_n', 'has_brk']
         + [f'{g}_{k}' for g in list(GROUPS) + ['ppr'] for k in [1, 3, 5, 20]])


def model():
    return HistGradientBoostingClassifier(max_depth=3, max_iter=180, learning_rate=0.06,
                                          min_samples_leaf=40, l2_regularization=1.0,
                                          class_weight='balanced', random_state=0)


def main(top):
    con = sqlite3.connect(DB)
    d = build(con)
    con.close()
    last = d.date.max()
    train = d[(d.date < last) & d.y_contig.fillna(False).astype(bool)]
    today = d[d.date == last]
    X = d[FEATS].replace([np.inf, -np.inf], np.nan)

    print(f"universe {d.ticker.nunique()} tickers | last session {str(last)[:10]} | "
          f"train rows {len(train)}")
    for tgt, name in [('y_ARB', 'ARB'), ('y_ARA', 'ARA')]:
        clf = model().fit(X.loc[train.index], train[tgt].astype(int))
        t = today.assign(p=clf.predict_proba(X.loc[today.index])[:, 1]).sort_values('p', ascending=False)
        t['limit_px'] = t.close.map(arb_price if name == 'ARB' else ara_price)
        print(f"\n--- {name} risk ranking for the next session ---")
        print(t.head(top)[['ticker', 'close', 'limit_px', 'r1', 'vr20', 'rv20',
                           f'{name.lower()}_c5', 'retail_1', 'tier1_5', 'p']]
              .round(4).to_string(index=False))
    print("\nreminder: the ARB ranking is the one that validated out-of-sample. "
          "The ARA ranking did not (OOS AUC ~0.57, top-decile lift ~1x) - treat it as "
          "a watchlist, not a signal.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=10)
    main(ap.parse_args().top)
