# Phase 1: Trading Foundations
### 4-week algo trading prep — quick-read version

---

## Week 1 — How markets actually work

**Mon** — What is price? Buyers, sellers, market makers create every price tick.

**Tue** — The order book: bid, ask, depth, spread. The bid/ask spread is a cost your bot pays on every trade.

**Wed** — Candlestick anatomy: open, high, low, close, volume.

**Thu** — Key candle patterns: doji, engulfing, hammer, pin bar.

**Fri** — Timeframes: M1, M15, H1, D1 — shorter timeframes = more noise, more trades, more spread cost.

**Weekend drill** — Read 30 charts. Identify candle patterns without indicators.

> Algo relevance: slippage and spread eat into every trade your bot makes. Model them or your backtest lies.

---

## Week 2 — Core indicators (learn the math, not just the look)

**Mon** — RSI: formula, overbought/oversold, divergence. It's a ratio of average gains vs losses — and it lags because it's smoothed over past data.

**Tue** — MACD: line, signal, histogram, crossover logic.

**Wed** — Moving averages: SMA vs EMA, golden cross / death cross.

**Thu** — Bollinger Bands: volatility squeeze and breakout signals.

**Fri** — ATR: true range, volatility measure, used for stop placement. Also lagging — it widens *after* volatility spikes, not before.

**Weekend drill** — Write each indicator's formula in plain English. Then write the signal as an IF/THEN rule.

> Key mindset: every indicator is a math formula on price data. Know the formula and you can code it.
>
> Reminder on lag: shorten lookback periods for faster (noisier) signals, or pair lagging indicators with a leading one — order book imbalance, volume spikes, or price action react in real time instead of averaging the past.

---

## Week 3 — Strategy types: pick your edge

**Mon** — Trend following: identify and ride a trend.

**Tue** — Mean reversion: why price tends to return to average.

**Wed** — Breakout strategies: support, resistance, volume confirmation.

**Thu** — Market regimes: trending vs ranging, how to detect which one you're in.

**Fri** — Crypto-specific: funding rates, liquidation cascades, BTC dominance — these are leading, codeable signals most beginners ignore.

**Weekend drill** — Write 3 complete strategies as IF/THEN/EXIT rules, one of each type above.

> Example rule: "IF RSI crosses above 30 AND price is above 50 EMA AND volume is above 20-day average → BUY. Exit when RSI hits 70 OR price drops 2×ATR below entry." That's a real algo.

---

## Week 4 — Risk management (the most important week)

**Mon** — Position sizing: fixed %, Kelly criterion, volatility-scaled sizing.

**Tue** — Stop-loss types: hard, trailing, volatility-based (ATR multiples).

**Wed** — Risk:reward ratio — why 1:2 minimum changes your long-run outcome.

**Thu** — Max drawdown: define your personal limit before you ever go live.

**Fri** — Expectancy, Sharpe ratio, win rate — expectancy matters more than win rate alone.

**Weekend drill** — Add complete risk rules to your 3 strategies from Week 3.

> A bot without risk management is a time bomb. One bad trade (or a bug) can wipe a huge chunk of the account. Risk rules are mandatory code, not optional.

---

## Order book & spread — quick recap

The order book is a live ledger of unfilled buy orders (bids) and sell orders (asks). The highest bid and lowest ask define the spread.

Market makers profit from that gap as compensation for holding inventory risk. Wider spread = market thinks the asset is riskier or less liquid right now.

**How it hits your bot:**
- Market orders always cross the spread — buy at ask, sell at bid. Cost is paid twice per round trip, win or lose.
- Slippage stacks on top of spread when your order eats through multiple book levels.
- High-frequency strategies are most exposed — if your edge per trade is smaller than 2× the spread, you can lose money even with a 50%+ win rate.

**Fixes:**
- Trade liquid pairs only (BTC/USDT, ETH/USDT — often under 5 bps spread vs 50-200 bps on illiquid alts).
- Use limit orders where possible — you may capture the spread instead of paying it.
- Always backtest with spread + slippage assumptions baked in.
- Reduce trade frequency if your per-trade edge is thin.

---

## Phase 1 exit checklist

Move to Phase 2 (Python) only when you can do all of these:

- [ ] Explain what RSI, MACD, and ATR calculate in plain math — not just what they look like on a chart
- [ ] Write a complete strategy as IF/THEN rules with entry, exit, and stop-loss defined
- [ ] Calculate max position size for a $1,000 account using a 2% risk-per-trade rule
- [ ] Explain slippage and spread, and why they must be included in any honest backtest
- [ ] Identify trending vs ranging market conditions on a live chart
- [ ] Define your personal max drawdown limit and what happens to your bot if it's hit

---

*Tip: save this to your phone's notes app or a PDF reader for offline reading.*
