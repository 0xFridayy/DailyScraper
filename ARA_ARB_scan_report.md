# ARA / ARB next-day scan — test report (rev 2, full IDX universe)

Run 2026-08-22. Full write-up: https://claude.ai/code/artifact/dbc5f78d-846d-41ca-92b5-cb1b95e51da0

Pipeline: `harvest_inventory.py` → `build_inventory_db.py` → `inventory_features.py` → `scan_ara_arb.py`.
Supporting tests: `test_inventory_signal.py`, `test_inventory_adds.py`.
(rev 1 ran on the 43-ticker konglomerat watchlist in `neobdm.db` via `ara_arb_scan.py`; superseded.)

## What changed from rev 1

**"ARA is not predictable" was wrong — it was an artifact of the 43-stock universe.** With 26
out-of-sample events no edge could have been detected. On the full market there are 165 and a fresh
ARA ranks at AUC 0.772 with a 12x lift in the top percentile. The ARB conclusion survived and got
sharper. What did *not* survive is the folk reading of the inventory chart (see below).

| | rev 1 (neobdm.db) | rev 2 (inventory API) |
|---|---|---|
| Tickers | 43 konglomerat | 1,037 → 913 usable → 790 liquid |
| Ticker-days | 10,365 | 206,254 → 122,424 liquid |
| ARA / ARB events | 110 / 124 | 1,367 / 1,327 |
| Broker codes | 29 | 101 |
| Broker fields | net value only (95% null) | buy + sell, lot + value, per broker per day |
| Contamination | ~6% price bars, ~9% broker rows cloned | none detected |

## The API (this is the important discovery)

```
GET /api/inventory?symbol=BREN&brokers=AD&brokers=AF&...&brokers=ZR
                  &start_date=2025-08-22&end_date=2026-08-22&investor_type=A

data = { date:[239], nlot:{BROKER:[...]}, nval:{...}, blot:{...}, bval:{...},
         slot:{...}, sval:{...}, ohlc:[{date,open,high,low,close,volume}] }
```

- One call per ticker = a full year of daily per-broker flow **plus clean OHLCV**. ~1.5s, 42 KB.
- `investor_type` ∈ {`F`, `D`, `A`}. History starts at `min_date_allowed` = 2025-08-22.
- **`brokers` must be REPEATED query params, not comma-joined.** A comma-joined list returns
  HTTP 200 with every series empty — a silent failure that cost one full harvest run.
- Ticker universe (1,042 incl. warrants; 1,037 real) comes from `/dashboard/~get-choices/`.
- Broker codes (101) come from the `#broker` dropdown in `_dash-layout`.
- Harvested 1,037 tickers in 48 min at ~22/min with zero failures. Raw cache 103 MB, gzipped.

The old `neobdm.db` contamination was an artifact of the page-scraping path, **not** NeoBDM's data.

## Bands, settled on 121,720 returns

| Prev close | ≥18% | ≥20% | ≥24% | ≥25% | ≥30% | ≥34% | ≥35% | Band |
|---|---|---|---|---|---|---|---|---|
| < Rp200 | 713 | 612 | 493 | 462 | 360 | 285 | 8 | **+35%** |
| Rp200–5,000 | 821 | 719 | 571 | 157 | 3 | 3 | 3 | **+25%** |
| > Rp5,000 | 68 | 1 | 1 | 1 | 0 | 0 | 0 | **+20%** |

Downside pooled: 2,473 ≤ −10%, 1,144 ≤ −14%, **88 ≤ −15%**, 8 ≤ −16%. **ARB = flat −15%**, confirmed
independently of the rev-1 sample.

## Base rates (liquid: 20d turnover ≥ Rp0.5bn)

| State at close of T−1 | n | P(event on T) | rev 1 |
|---|---|---|---|
| Unconditional ARA | 122,424 | 0.714% | 1.06% |
| Unconditional ARB | 122,424 | 0.866% | 1.20% |
| ARA yesterday → ARA | 931 | **23.63%** | 36.36% |
| No ARA in 3d → ARA | 119,910 | 0.51% | 0.70% |
| ARB yesterday → ARB | 1,073 | **22.46%** | 24.00% |
| No ARB in 3d → ARB | 119,737 | 0.58% | 0.66% |

The konglo names really do run harder (36% vs 24% ARA continuation) — that part of rev 1 was right.

**ARA and ARB live in different parts of the market.** Across liquidity deciles the ARA rate falls
0.85% → 0.45% while the ARB rate rises 0.43% → 1.45%. Limit-up is a small-cap phenomenon, limit-down
a large-cap one.

## Walk-forward OOS (43,080 rows, expanding window, 20d blocks, 2d embargo)

| Target / features | ev | AUC | top-0.1% | top-1% | lift@1% |
|---|---|---|---|---|---|
| ARA · price | 202 | 0.800 | 22.73% | 8.58% | 18.3x |
| ARA · inventory only | 202 | 0.706 | 9.09% | 3.25% | 6.9x |
| **FRESH ARA · price** | 165 | **0.772** | 6.98% | **4.71%** | 12.1x |
| FRESH ARA · price+inv | 165 | 0.766 | 6.98% | 2.59% | 6.7x |
| ARB · price | 369 | 0.883 | 27.27% | 20.65% | 24.1x |
| **ARB · price+inv** | 369 | 0.883 | **45.45%** | 19.03% | 22.2x |
| **FRESH ARB · price+inv** | 267 | **0.851** | 27.91% | **14.93%** | 23.5x |
| FRESH ARB · inventory only | 267 | 0.780 | 18.60% | 9.95% | 15.7x |

## Does the inventory chart add over price alone?

Same walk-forward twice on identical rows, paired bootstrap of the AP difference over trading days:

| Target | AP px → px+inv | gain | 95% CI | verdict |
|---|---|---|---|---|
| **FRESH ARB** | 0.0784 → 0.0960 | **+22%** | [+0.0048, +0.0339] | **adds, P(gain>0)=1.00** |
| ARB | 0.1355 → 0.1426 | +5% | [−0.0064, +0.0215] | P(gain>0)=0.83 |
| ARA | 0.0518 → 0.0503 | −3% | [−0.0117, +0.0109] | no |
| FRESH ARA | 0.0212 → 0.0168 | −21% | [−0.0166, +0.0030] | hurts, P(gain>0)=0.10 |

**Read the inventory book on the sell side, not the buy side.**

## The inventory chart before a first limit hit

**Before a first ARB (n=717)** — the bull trap, measured:
- 5-day return at T−1: **+13.6%** (baseline +0.9%)
- Top accumulator's 5-day burst (`acc_accel`) at T−1: **1.76** (baseline 1.01)
- Volume vs 20d avg at T−1: **2.75x**
- Retail basket net positive (0.056 vs 0.046), no broker holding a real position

**Before a first ARA (n=714)** — nothing moves until the day itself. Volatility is elevated
throughout (that *is* the signal); accumulation, volume, retail flow and the 5-day return all sit at
or below baseline right up to T−1. **A fresh ARA has no visible footprint on the inventory chart.**

### The tempting misreading I am NOT reporting as a finding

Raw univariate screen says accumulation is strongly *inverted* — `top1_acc_60` AUC 0.408 (fresh ARA)
and 0.360 (fresh ARB), i.e. "limit moves come from stocks nobody is accumulating."

**It does not survive conditioning on volatility.** Splitting accumulation into terciles *within*
each realised-vol quintile, the fresh-ARA effect collapses to 1.09x and **reverses** in the top vol
quintile (0.98% low-accum vs 1.30% high-accum). Fresh ARB keeps a modest 1.32x. Accumulation is
normalised by average volume, and thin jumpy names both accumulate less and hit limits more — that
correlation was doing nearly all the work.

## Tradability (date-clustered bootstrap, 1,500 draws)

| Signal | n | gap at open | open→close | diff vs rest 95% CI | |
|---|---|---|---|---|---|
| ARA top-0.1% | 44 | +7.13% | −5.07% | [−7.76, −1.50] | sig |
| ARA top-1% | 431 | +3.97% | −3.05% | [−4.10, −1.35] | sig |
| **FRESH ARB top-1%** | 422 | **+2.49%** | **−2.88%** | [−3.77, −1.32] | **sig** |
| **FRESH ARB top-0.1%** | 43 | +1.98% | −4.96% | [−7.87, −1.10] | **sig** |
| ARB top-0.1% (close→close) | 44 | −5.19% | +0.94% | [−7.82, −1.85] | sig on c→c |
| FRESH ARA top-1% | 425 | +0.63% | −0.82% | [−1.54, +0.52] | ns |

Baseline open→close is −0.31%.

**The one clean setup: fresh-ARB candidates gap UP and then collapse.** Top-percentile names open
+2.49% and close −2.88% from that open. Unlike everything else here, the edge is on the near side of
the gap — the signal is known at the close of T−1 and the adverse move happens after you can act. A
fresh ARB has not been recognised yet (the stock is still opening green off a five-day run), which is
exactly why it is tradable.

ARA stays structurally unbuyable: top-ranked names open +7.13% and lose 5.07% into the close.

## Limits

- **One year of history** — the API won't go earlier than 2025-08-22. Single regime; the ARB model
  leans on the April sell-off.
- **Top-0.1% buckets are 43–44 picks.** The 45.5% ARB hit rate is 20 hits, CI 30–61%. Plan around the
  top-1% numbers.
- **Survivorship** — the ticker list is as of today, so names delisted during the year are missing.
- **No intraday tape** — still the biggest gap. Both a fresh ARA and the collapse leg of a fresh ARB
  resolve inside the session.
- **Foreign/domestic split unused** — `investor_type` accepts F and D; this run pulled only A. Two
  more calls per ticker would give a real foreign-flow inventory curve.

## Usage

```bash
python scan_ara_arb.py --top 12
```
