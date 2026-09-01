# ML V2 Experiment #1 — Results and Review

Results for the experiment specified in [[ML_NEXT_STEP_BROKER_IDENTITY_ROADMAP.md]].
Produced by `ml_v2_experiment_1.py` and `ml_v2_experiment_1_robustness.py`, run
on 2026-09-01. This is a record of a reviewed experiment; the numbers below are
historical and must not be edited by later work.

## Reproducibility fingerprint

| | |
|---|---|
| Clean common panel | 9,814 rows · 250 dates · 45 tickers |
| Walk-forward folds | 36 identical folds (expanding train, 6-session test blocks, 30-session minimum) |
| Split hash | `14cae7f9fc9ec403` |
| Seed | 17 (`RANDOM_SEED`), `n_jobs=1`, `tree_method=hist` |
| Prediction digest | `147d734749c71e2d` |
| Determinism check | **PASS** — full experiment repeated in-process, identical digest and identical result table |
| Scored rows | 8,514 out-of-sample predictions over 216 independent dates, **identical rows for A/B/C/D** (asserted in `run_experiment`) |

The digest `147d734749c71e2d` matches `ACCEPTED_PREDICTION_DIGEST` pinned in
`ml_v2_experiment_1_robustness.py`, and the robustness run re-derived it from
its own rebuild of the panel. The result is bit-reproducible across runs.

Prices sourced exclusively through `price_audit.clean_panel()`; target is the
clean one-session forward return (`fwd_1`). Any historical return figure not
produced through that path remains void.

## A/B/C/D absolute out-of-sample results

| Feature set | n_feat | Pooled IC | Mean daily IC | Median daily IC | % pos-IC days | Top-decile hit | Base hit | Hit edge | Return edge | MAE |
|---|---|---|---|---|---|---|---|---|---|---|
| **A** `price_only` | 2 | 0.028465 | 0.019793 | 0.032153 | 56.48% | 44.42% | 42.08% | +2.338% | +0.686% | 0.042082 |
| **B** `existing_broker_aggregate` | 8 | 0.048727 | 0.024879 | 0.021624 | 54.17% | 46.92% | 42.08% | +4.840% | +1.002% | 0.041703 |
| **C** `broker_identity` | 147 | **0.056557** | **0.045502** | **0.055836** | **58.80%** | **48.28%** | 42.08% | **+6.196%** | +0.993% | 0.041734 |
| **D** `C + observable_inventory` | 176 | 0.036783 | 0.033489 | 0.027158 | 55.09% | 45.57% | 42.08% | +3.485% | +0.744% | 0.041834 |

Base hit rate is identical (42.0836%) across all four variants, confirming the
same rows were scored. MAE is flat across variants and is a diagnostic only —
it is not the decision gate, and no Sharpe or substitute threshold was used.

### Delta versus A (`price_only`)

| Feature set | Δ pooled IC | Δ mean daily IC | Δ median daily IC | Δ % pos-IC days | Δ top-decile hit | Δ hit edge | Δ return edge |
|---|---|---|---|---|---|---|---|
| B | +0.020262 | +0.005086 | −0.010529 | −2.315pp | +2.503pp | +2.503pp | +0.317pp |
| C | +0.028093 | +0.025709 | +0.023683 | +2.315pp | +3.858pp | +3.858pp | +0.308pp |
| D | +0.008318 | +0.013696 | −0.004995 | −1.389pp | +1.147pp | +1.147pp | +0.059pp |

## Incremental comparisons required by the roadmap

### C − B — does broker identity beat the existing aggregates?

| Metric | Delta |
|---|---|
| pooled IC | **+0.007830** |
| mean daily IC | **+0.020623** |
| median daily IC | **+0.034213** |
| positive-IC days | **+4.630pp** |
| hit edge | **+1.356pp** |
| return edge | **−0.009pp** |

Positive on five of six headline metrics; return edge is flat-to-marginally
negative.

### D − C — does `observable_inventory` add anything?

| Metric | Delta |
|---|---|
| pooled IC | **−0.019774** |
| mean daily IC | **−0.012013** |
| median daily IC | **−0.028678** |
| positive-IC days | **−3.704pp** |
| hit edge | **−2.711pp** |
| return edge | **−0.249pp** |

Negative on every single headline metric. No metric favours D.

## Robustness audit (C vs B)

The roadmap requires that daily cross-sectional and top-decile diagnostics be
read together, and that sample limitations stay explicit. The audit below is
what qualifies the C−B point estimate.

### Paired per-date bootstrap

Trading date is the independent unit. 20,000 resamples; `date_boot` is an
i.i.d. date bootstrap, `block_boot` uses 5-day (weekly) blocks to respect
serial correlation.

| Metric | n dates | Mean | Median | % C beats B | % ties | Date-boot mean CI | Block-boot mean CI |
|---|---|---|---|---|---|---|---|
| `daily_ic_delta` | 216 | +0.020623 | +0.023386 | 53.24% | 0.00% | **[−0.004839, +0.046095]** | **[−0.003767, +0.046034]** |
| `top_hit_delta` | 216 | +0.016898 | +0.000000 | 26.39% | 47.22% | **[−0.014892, +0.048688]** | **[−0.013196, +0.046838]** |
| `return_edge_delta` | 216 | +0.000023 | +0.000163 | 50.93% | 0.00% | **[−0.004421, +0.004454]** | **[−0.003604, +0.004079]** |

**Every confidence interval crosses zero**, under both the i.i.d. and the
block bootstrap. Per-date win rates are close to a coin flip (53.24% on daily
IC, 50.93% on return edge). Top-decile hit is tied on 47.22% of dates and C
wins outright on only 26.39%.

### Leave-one-ticker-out

| Metric | Full | LOO range | Largest deterioration | Largest absolute influence |
|---|---|---|---|---|
| pooled IC | +0.007830 | [+0.004327, +0.014152] | DEWA (−0.003503) | TEBE (+0.006322) |
| mean daily IC | +0.020623 | [+0.016350, +0.026459] | WIFI (−0.004273) | TEBE (+0.005837) |
| hit edge | +0.013556 | [+0.005225, +0.024160] | PADI (−0.008331) | TEBE (+0.010604) |
| return edge | −0.000093 | [−0.001259, +0.001206] | PADI (−0.001166) | SINI (+0.001298) |

Largest pooled-IC influences:

| Removed ticker | pooled IC | Influence | mean daily IC | hit edge | return edge |
|---|---|---|---|---|---|
| TEBE | +0.014152 | +0.006322 | +0.026459 | +0.024160 | +0.000974 |
| UANG | +0.013274 | +0.005444 | +0.025820 | +0.019937 | +0.001084 |
| COIN | +0.012005 | +0.004175 | +0.022574 | +0.014721 | −0.000756 |
| DEWA | +0.004327 | −0.003503 | +0.018462 | +0.011567 | −0.000658 |
| DOOH | +0.005159 | −0.002671 | +0.017547 | +0.012618 | −0.000229 |
| PGUN | +0.005451 | −0.002379 | +0.017185 | +0.015756 | +0.000081 |
| PSKT | +0.010119 | +0.002289 | +0.022451 | +0.011555 | +0.000067 |
| JARR | +0.010110 | +0.002280 | +0.021262 | +0.016824 | +0.000389 |

Pooled-IC delta stays positive across every single-ticker removal — the effect
is not the artefact of one name. It is, however, unevenly distributed: TEBE,
UANG and COIN dominate the influence ranking.

### Leave-one-broker-group-out

C refit from scratch with one broker code's entire feature group removed
(29 groups), unchanged splits and parameters, nothing tuned.

| Metric | Full | LOO range | Largest deterioration | Largest absolute influence |
|---|---|---|---|---|
| pooled IC | +0.007830 | **[−0.012832, +0.015529]** | TP (−0.020662) | TP (−0.020662) |
| mean daily IC | +0.020623 | [+0.001526, +0.017825] | AI (−0.019096) | AI (−0.019096) |
| hit edge | +0.013556 | **[−0.015641, +0.018770]** | BB (−0.029197) | BB (−0.029197) |
| return edge | −0.000093 | [−0.003786, +0.000031] | DH (−0.003693) | DH (−0.003693) |

Largest pooled-IC influences:

| Removed broker | pooled IC | Influence | mean daily IC | hit edge | return edge |
|---|---|---|---|---|---|
| TP | −0.012832 | −0.020662 | +0.002869 | −0.014599 | −0.003573 |
| YP | −0.004933 | −0.012763 | +0.012247 | +0.004171 | −0.000741 |
| BK | −0.004196 | −0.012026 | +0.011598 | +0.008342 | −0.002660 |
| SS | −0.003325 | −0.011155 | +0.004885 | −0.011470 | −0.002956 |
| CC | −0.000564 | −0.008394 | +0.008923 | +0.006257 | +0.000031 |
| YU | +0.000104 | −0.007726 | +0.013088 | +0.003128 | −0.001451 |
| MG | +0.015529 | +0.007699 | +0.006036 | −0.008342 | −0.002430 |
| RB | +0.015337 | +0.007506 | +0.013344 | −0.011470 | −0.002561 |
| HP | +0.000384 | −0.007446 | +0.006307 | −0.010428 | −0.001635 |
| AZ | +0.000652 | −0.007178 | +0.009876 | +0.001043 | −0.000912 |

**This is the weakest point in the C−B case.** Removing a single broker code
can reverse the sign: without TP, pooled IC goes to −0.012832; without BB, hit
edge goes to −0.015641. Four brokers (TP, YP, BK, SS) each carry more than one
full point estimate's worth of influence.

### IHSG regime audit

Existing local `jci_daily.csv` only; 186 of 216 OOS dates matched,
2025-09-23 … 2026-07-07. Regime is the sign of the trailing 20-day IHSG move.

| Regime | n dates | Mean daily-IC Δ | Median daily-IC Δ | % C beats B (daily IC) | Mean top-hit Δ | % C beats B (top hit) | Mean return-edge Δ | Median return-edge Δ | % C beats B (return edge) |
|---|---|---|---|---|---|---|---|---|---|
| negative_20d | 88 | +0.019241 | **−0.004284** | 48.86% | +0.026136 | 23.86% | +0.002159 | +0.000990 | 52.27% |
| positive_20d | 98 | +0.026065 | +0.041019 | 59.18% | +0.001871 | 29.59% | −0.002559 | −0.001960 | 45.92% |

The mean daily-IC delta is positive in both regimes, but the **median is
negative in down-regimes** (−0.004284) against strongly positive in up-regimes
(+0.041019), and the down-regime per-date win rate falls below 50%. The daily-IC
advantage is concentrated in rising-market conditions. Top-decile hit delta is
weak in both regimes (C wins 23.86% / 29.59% of dates).

## Findings

**Does retaining broker identity add out-of-sample ranking information beyond
price features and the existing aggregate broker features?**

Directionally yes. C beats B on pooled IC, mean and median daily IC,
positive-IC days and hit edge, and the pooled-IC advantage survives every
leave-one-ticker-out fold. But the evidence does not reach a confident
incremental-information claim: all paired bootstrap intervals cross zero under
both i.i.d. and block resampling, per-date win rates are near 50%, a single
broker-code removal can flip the sign, and the daily-IC advantage is
concentrated in positive IHSG regimes. Sample is 216 independent dates across
45 tickers.

**Does `observable_inventory` add anything beyond broker identity?**

No. D is worse than C on every headline metric without exception. This is the
cleanest result in the experiment.

## Review decision (2026-09-01)

- **`broker_identity`: retain as a research feature.** It shows directional
  incremental out-of-sample information, but is **not** approved for production
  promotion, because bootstrap confidence intervals cross zero and
  leave-one-broker-out can reverse the sign.
- **`observable_inventory`: rejected from the active feature set.** It degrades
  C across essentially every headline metric.
- **The `observable_inventory` failure is not evidence that actual shareholder
  ownership data lacks predictive value.** The two represent different economic
  information. `observable_inventory` is a cumulative sum of *observable broker
  net lots* — bounded by top-broker capture coverage, affected by nominees and
  transfers, and starting from an arbitrary finite history. It is explicitly not
  beneficial ownership. No conclusion about real shareholder-ownership data may
  be drawn from this result.

Experiment #1 is reviewed and closed. The roadmap's post-experiment gate is
lifted only as recorded in `ML_NEXT_STEP_BROKER_IDENTITY_ROADMAP.md`.
