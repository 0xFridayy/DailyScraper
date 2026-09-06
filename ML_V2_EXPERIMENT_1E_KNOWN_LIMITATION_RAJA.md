# Experiment #1E — known limitation: mixed broker-flow basis on RAJA

**Status:** documented, not remediated. #1E's results, artifacts and pinned digests are
**unchanged** and are not to be re-run or re-pinned.

This note is deliberately a separate file. `ML_V2_EXPERIMENT_1_RESULTS.md`, the #1E results
document, `ml_v2_experiment_1.py`, and the historical digests
(`EXECUTABLE_V1_PREDICTION_DIGEST = 4dac8d153b33f7aa`, split `1856490d23c5a5c0`,
`LEGACY_CLOSE_CONTRACT_DIGEST = 147d734749c71e2d`) are frozen and were not touched.

## What was found

Discovered during Experiment #1F Phase 1, while tracing an upstream data-contract defect.
It was not known when #1E was run.

`ml_v2_experiment_1._historical_net_lots` recovers broker lots by two paths:

| path | formula | condition |
|---|---|---|
| live | `bval / (bavg * 100)` − `sval / (savg * 100)` | `bval`, `sval`, `bavg`, `savg` all present and positive |
| fallback | `netval / (close * 100)` | otherwise |

Both are then normalised by `volume / 100` in `build_broker_identity_features`.

The two paths sit on **different price bases**:

- `bavg` / `savg` are **as-traded** average execution prices.
- `close` and `volume` in `neobdm.db.price_history` are **back-adjusted**.

Measured on RAJA (2026-07-15, ahead of its 2026-07-16 basis transition, factor r = 5):
`bavg` = 4,571.80 against `close` = 920 — a ratio of ~4.97 — and `bavg` matches the raw
broker-implied VWAP of 4,654 rather than the close. Post-transition the two agree
(875 vs 894, 885 vs 897).

Consequences:

- **fallback rows are correct by cancellation** — `netval` is adjustment-invariant, so
  `netval/(close·100)` yields `r ×` true lots, which is then divided by an equally
  `r`-scaled `volume/100`. The factor cancels.
- **live rows are understated by `r`** — they yield true as-traded lots divided by an
  adjusted volume.

So one feature column mixes two scales, ticker- and date-correlated.

## Measured footprint

`broker_flow` has **no lot columns** (`date, ticker, broker_code, bval, sval, netval,
bavg, savg`), so the exact-lot path does not exist in #1E at all; only the live/fallback
split matters.

Of #1E's 45 tickers, three carry a basis regime: RAJA (r = 5.0), BNBR (r = 1.191),
PDPP (r ≈ 1.0024).

| ticker | regime window | live rows inside it | exposed |
|---|---|---|---|
| RAJA | → 2026-07-15 | **159 rows on 11 dates** (2026-07-05 → 2026-07-15) | yes |
| BNBR | → 2026-06-24 | 0 (live rows begin 2026-07-05) | no |
| PDPP | → 2026-08-20 | r ≈ 1.0024, within source rounding | negligible |

Contamination therefore reaches **one ticker on eleven decision dates**, propagating
forward through the 1/3/5/10/20-day rolling flow windows and through the
observable-inventory cumulative sum to the end of RAJA's clean segment.

Against a panel of ~9,861 scored prediction rows across 45 tickers, that is on the order
of **0.1–0.3% of rows, on a single name**.

## Assessment

**#1E's negative/weak conclusion is not overturned.** A contamination confined to one of
45 tickers over eleven dates cannot manufacture or erase the reported result
(IC +0.088, daily IC +0.023, top-hit 48.7%, hit edge −1.5%).

## Why #1E is not re-run

1. `neobdm.db` receives daily top-ups (10,236 → 10,261 rows over the course of this work),
   so a re-run today would confound this defect with data drift and could not be compared
   against the pinned digest.
2. The standing instruction is that the historical digest stays documented as historical
   provenance and is **not** re-pinned.

## Where the fix lives

The upstream remediation is Experiment #1F Phase 2, which does **not** touch #1E:

- `build_inventory_db.py` — lossless int64 lot ingest.
- `normalize_market_data.py` — `volume_repair_ledger.json` and `observed_basis_factor.json`,
  which record the basis factor per ticker regime and classify RAJA/MLPT/RMKE as
  RECONSTRUCTIBLE and the rest as QUARANTINE.

`ml_v2_experiment_1.py`, `price_audit.py` (including `detect()`'s three-column `suspect`
definition) and `neobdm.db` were verified unchanged. `ml_v2_experiment_1.py` reads neither
parquet, so the rebuild cannot reach it.
