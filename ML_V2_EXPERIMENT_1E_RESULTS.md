# ML V2 Experiment #1E — Results (Executable Contract)

This is a **separate document from [[ML_V2_EXPERIMENT_1_RESULTS.md]]**, which
remains the unedited historical record of the original (non-executable)
close(T)→close(T+1) contract. Experiment #1E re-audits the same question —
does broker identity, and then `observable_inventory`, add out-of-sample
ranking information beyond price and the existing aggregate broker features —
under a contract that can actually be traded. Nothing in the legacy document
was changed to produce this one.

Produced by `ml_v2_experiment_1.py` (embargo=0 headline and embargo=5
robustness runs) and `ml_v2_experiment_1_robustness.py` (digest gate), run on
2026-09-05.

## Post-review validity-window audit (2026-09-05)

PR review of the executable contract found the `fwd_oo_1`/`fwd_oc_1` close-step
validity mask in `price_audit.add_forward_returns()` was checking the wrong
window: for `fwd_oo_1 = open(T+1) -> open(T+2)`, the code validated
`close(T+1) -> close(T+2)` (information from *after* the `open(T+2)` exit)
instead of `close(T) -> close(T+1)` (the step that actually backs
`open(T+2)`'s own previous-close reference). `fwd_oc_1` performed no close-step
check at all. Both were fixed to reuse the same close(T)→...→close(T+h) chain
already computed for the legacy `fwd_h`, per four new regression tests in
`test_pipeline.py` using deliberately corrupt closes (a corrupt close(T+1)
that individually-valid opens on both sides could previously mask). The fixed
panel was rebuilt and the full headline (embargo=0) and robustness (embargo=5)
runs were repeated, with determinism re-proven the same two ways as before.

**Result: byte-identical.** Panel rows (9,861), fold reports, both split
digests, and both prediction digests (`4dac8d153b33f7aa` headline,
`ee420553c5991f66` embargo=5) are unchanged. On this specific dataset,
`load_clean()`'s existing quarantine detectors already remove any row corrupt
enough to trigger the bug before `add_forward_returns()` runs, so the defect
— real, and now closed by test — had zero effect on this panel's actual rows.
Every table, delta, and conclusion below is unchanged and still applies. This
is reported, not assumed: the two independent-run and in-process
determinism proofs were both rerun after the fix, not just before it.

## Why the contract changed

The legacy Experiment #1 target was `fwd_1`, the clean close(T)→close(T+1)
return. That is not a return the model's own decision could realize: the
decision is made **at EOD(T)**, using price and broker-flow features that are
only fully known once session T has closed — so the earliest price the
decision could actually transact at is **open(T+1)**, not close(T). As
measured on this panel and recorded in `walk_forward_backtest.py`'s
`_price_features_and_target` docstring, the pre-entry close(T)→open(T+1) gap
carried +0.5463%/day while the reachable open(T+1)→close(T+1) window carried
−0.1945%/day — the entire legacy edge sat in a window the signal could never
have traded.

- **Decision timing:** EOD(T). All features (price and broker-flow) are as of
  close(T) and are unchanged from Experiment #1.
- **Price anchor:** open(T+1) — the earliest session the decision can reach.
- **Headline target:** `fwd_oo_1` = open(T+1) → open(T+2).
- The old close-anchored value survives on the panel only as `target_cc`
  (diagnostic, never fit against).

## Source-availability caveat for `broker_flow`

Two independent limits bound what any broker-derived feature (B, C, or D) can
possibly know, regardless of model quality:

1. **Buy/sell separation is mostly unavailable.** Per `HANDOFF.md` ¶4 (Lampiran
   history), only 10,950 of 218,988 `broker_flow` rows (~5%) are *live* rows
   carrying `bval`/`sval`/`bavg`/`savg`, and those only start ~2026-07-05 (~30
   trading days). The remaining ~95% are backfilled rows exposing only
   `netval`. `ml_v2_experiment_1.py::_historical_net_lots` reconstructs net
   lots for those rows from `netval / close`, which is an approximation
   wherever the true execution VWAP differs from the clean close — it cannot
   recover the buy/sell split itself.
2. **Absence is not evidence of no trading.** A broker code missing from a
   given day's captured flow is an unobserved/zero *captured* flow, not proof
   the broker did not trade that day (`ML_NEXT_STEP_BROKER_IDENTITY_ROADMAP.md`).
   This bounds `observable_inventory` in particular: it is a cumulative sum of
   *observable* broker net lots, bounded by top-broker capture coverage,
   affected by nominees and transfers, and starting from an arbitrary finite
   history. It is explicitly not beneficial ownership, and no conclusion about
   real shareholder-ownership data may be drawn from D's result below.

## Open-anchor validity guard

`price_audit._open_anchor_valid` is a guard the legacy contract never needed:
`detect()` and the close-to-close `_step_valid` mask validate `close` only, so
a corrupt `open` can hide behind an ordinary close. Audited case (FAST
2025-10-14): `prev_close=580`, `open=870` (+50%), `close=720`. Close-to-close
is +24.1%, inside the 25% band, so the close-only guard **passes** — yet an
entry anchored on 870 is fabricated. `_open_anchor_valid` requires `open > 0`,
`low <= open <= high`, and the open-vs-previous-close change inside
`[ARB_BOUND − TOL, ara_bound(prev_close) + TOL]`. A row failing this test has
its `fwd_oo_1` / `fwd_oc_1` / `gap_1` labels set to NaN — the **row is never
deleted and the price is never "repaired"**, only the derived target is
withheld (same philosophy as the existing close-based `_step_valid`).

## Outer / internal realization purge

A label decided at EOD(T) enters at open(T+1) and realizes at open(T+1+h).
`walk_forward_backtest.make_walk_forward_splits` enforces two mandatory
purges of length `horizon` (not `horizon + 1` — the invariant is a **timestamp**
ordering, `max realized_at < min decision_at`, and open(k) precedes EOD(k)
within the same session):

```
[ FIT ] -- purge h -- [ EVAL ] -- purge h (+embargo) -- [ TEST ]
```

- **Outer (train → test):** without it, the last training rows' labels realize
  inside the test window.
- **Internal (fit → eval):** the one a plain date-level 80/20 cut misses.
  Without it, labels from the tail of FIT realize inside EVAL, so the fitted
  model has already seen outcomes belonging to its own early-stopping
  validation slice.

`embargo` is a separate control from purge and is applied at the outer
boundary only: serial correlation (+0.275 mean pairwise ticker correlation,
per `signal_metrics.py`) is a dependence/inference-optimism concern, not a
leakage one, so embargo defaults to 0 (headline) and embargo=5 is a
robustness variant, never the number of record.

## `min_fit_days=24` / `min_eval_days=3`

24 is the exact training depth every published Experiment #1 result was
actually computed at: the pre-contract protocol's fit block was
`0.8 * train_min = 0.8 * 30 = 24` dates at h=1 with no purges. Requiring 24
after both purges preserves that depth instead of quietly training on
thinner and thinner data as folds are purged. `min_eval_days=3` exists
because a smaller eval slice lets XGBoost's early stopping collapse to a coin
flip rather than a criterion. Folds that don't clear either minimum, or whose
FIT block is arithmetically empty, are **skipped and counted**, never
silently scored — see the fold reports below.

## Determinism (proven, not asserted)

The headline run (embargo=0) was proven deterministic two independent ways:

1. **Two fully separate OS-process invocations** of
   `py ml_v2_experiment_1.py --embargo 0` produced **byte-identical stdout** —
   panel shape, split digest, the full result table, and the prediction
   digest all matched exactly (`Compare-Object` reported zero differences).
2. **An in-process harness** rebuilt the panel from two independent sqlite
   connections, recomputed the splits, and reran the full four-variant
   experiment twice, then asserted exact `pandas.DataFrame.equals()` on: the
   built panel (9,861 rows), the feature-set definitions, the fold list
   (fit/eval/test date tuples — not just the digest), every feature set's
   prediction rows (8,439 rows each), and the summary table.

Both methods agree on prediction digest **`4dac8d153b33f7aa`**, split digest
`1856490d23c5a5c0`. `ml_v2_experiment_1_robustness.py::EXECUTABLE_V1_PREDICTION_DIGEST`
is now pinned to this value; `LEGACY_CLOSE_CONTRACT_DIGEST = "147d734749c71e2d"`
is unchanged.

The embargo=5 robustness run is a different, non-canonical split (fewer
scored folds by construction) and is reported for robustness only; its
prediction digest is `ee420553c5991f66` / split digest `a3b51914f19a8d95`.

## Reproducibility fingerprint

| | embargo=0 (headline) | embargo=5 (robustness) |
|---|---|---|
| Clean common panel | 9,861 rows · 253 dates · 45 tickers | same panel |
| Split digest | `1856490d23c5a5c0` | `a3b51914f19a8d95` |
| Fold report | scored=36, skipped_too_thin=1, infeasible=0, nominal=37 | scored=35, skipped_too_thin=2, infeasible=0, nominal=37 |
| `min_fit_days` / `min_eval_days` | 24 / 3 | 24 / 3 |
| Scored rows / independent dates | 8,439 / 216 | 8,233 / 210 |
| Prediction digest | **`4dac8d153b33f7aa`** (pinned) | `ee420553c5991f66` |
| Seed | 17 (`RANDOM_SEED`), `n_jobs=1`, `tree_method=hist` — unchanged from Experiment #1 | same |

Same rows scored for A/B/C/D in both runs (asserted in `run_experiment`; base
hit rate identical across all four variants within each run).

## A/B/C/D absolute out-of-sample results — embargo=0 (headline)

| Feature set | n_feat | Pooled IC | Mean daily IC | Median daily IC | % pos-IC days | Top-decile hit | Base hit | Hit edge | Return edge | MAE |
|---|---|---|---|---|---|---|---|---|---|---|
| **A** `price_only` | 2 | −0.030262 | +0.000908 | +0.022804 | 54.46% | 40.11% | 42.67% | −2.566% | −0.090% | 0.045818 |
| **B** `existing_broker_aggregate` | 8 | +0.000105 | +0.010637 | −0.001594 | 49.07% | 43.16% | 42.67% | +0.487% | −0.030% | 0.045747 |
| **C** `broker_identity` | 147 | −0.006890 | +0.013183 | +0.003715 | 51.39% | 42.42% | 42.67% | −0.250% | +0.383% | 0.045704 |
| **D** `C + observable_inventory` | 176 | −0.025898 | +0.015019 | +0.018571 | 55.40% | 42.21% | 42.67% | −0.460% | +0.264% | 0.045754 |

### Delta versus A — embargo=0

| Feature set | Δ pooled IC | Δ mean daily IC | Δ median daily IC | Δ % pos-IC days | Δ top-decile hit | Δ hit edge | Δ return edge |
|---|---|---|---|---|---|---|---|
| B | +0.030368 | +0.009728 | −0.024398 | −5.386pp | +3.053pp | +3.053pp | +0.061pp |
| C | +0.023372 | +0.012275 | −0.019089 | −3.071pp | +2.316pp | +2.316pp | +0.474pp |
| D | +0.004365 | +0.014111 | −0.004232 | +0.939pp | +2.105pp | +2.105pp | +0.354pp |

## A/B/C/D absolute out-of-sample results — embargo=5 (robustness)

| Feature set | n_feat | Pooled IC | Mean daily IC | Median daily IC | % pos-IC days | Top-decile hit | Base hit | Hit edge | Return edge | MAE |
|---|---|---|---|---|---|---|---|---|---|---|
| **A** `price_only` | 2 | −0.030481 | −0.006635 | +0.000591 | 50.00% | 41.14% | 42.56% | −1.416% | −0.213% | 0.045742 |
| **B** `existing_broker_aggregate` | 8 | +0.011888 | +0.010513 | −0.003072 | 49.28% | 42.22% | 42.56% | −0.336% | +0.034% | 0.045605 |
| **C** `broker_identity` | 147 | −0.027392 | +0.012744 | −0.006911 | 48.31% | 43.74% | 42.56% | +1.176% | +0.225% | 0.045715 |
| **D** `C + observable_inventory` | 176 | −0.033682 | +0.014877 | +0.014700 | 52.20% | 40.93% | 42.56% | −1.632% | +0.069% | 0.045854 |

### Delta versus A — embargo=5

| Feature set | Δ pooled IC | Δ mean daily IC | Δ median daily IC | Δ % pos-IC days | Δ top-decile hit | Δ hit edge | Δ return edge |
|---|---|---|---|---|---|---|---|
| B | +0.042369 | +0.017149 | −0.003663 | −0.718pp | +1.080pp | +1.080pp | +0.247pp |
| C | +0.003088 | +0.019380 | −0.007502 | −1.691pp | +2.592pp | +2.592pp | +0.437pp |
| D | −0.003201 | +0.021513 | +0.014109 | +2.195pp | −0.216pp | −0.216pp | +0.281pp |

## Incremental comparisons: B−A, C−B, D−C

| Comparison | Metric | embargo=0 | embargo=5 |
|---|---|---|---|
| **B − A** | pooled IC | +0.030368 | +0.042369 |
| | mean daily IC | +0.009728 | +0.017149 |
| | hit edge | +3.053pp | +1.080pp |
| | return edge | +0.061pp | +0.247pp |
| **C − B** | pooled IC | **−0.007000** | **−0.039300** |
| | mean daily IC | +0.002500 | +0.002200 |
| | median daily IC | +0.005300 | −0.003800 |
| | % pos-IC days | +2.31pp | −0.97pp |
| | hit edge | **−0.740pp** | **+1.510pp** |
| | return edge | +0.410pp | +0.190pp |
| **D − C** | pooled IC | **−0.019000** | **−0.006300** |
| | mean daily IC | +0.001800 | +0.002100 |
| | median daily IC | +0.014900 | +0.021600 |
| | % pos-IC days | +4.01pp | +3.89pp |
| | hit edge | **−0.210pp** | **−2.810pp** |
| | return edge | −0.120pp | −0.160pp |

## Findings

**Does the aggregate broker feature set (B) add anything beyond price alone (A)?**

B − A is directionally positive on pooled IC, mean daily IC, top-decile hit,
and hit edge in *both* embargo settings, but the magnitude is not stable
(pooled IC +0.030 vs +0.042; hit edge +3.05pp vs +1.08pp), median daily IC is
*negative* versus A in both settings, and return edge is barely above zero.
**Aggregate broker retains only weak evidence** — a consistent sign on some
headline metrics, no consistent magnitude, and no support from the metric
(median daily IC) that is least sensitive to a handful of extreme dates.

**Does retaining broker identity (C) add anything beyond the existing
aggregate (B) — the actual Experiment #1E question?**

C − B is **negative on pooled IC in both embargo settings** (−0.0070 and
−0.0393), and hit edge **flips sign** between them (−0.74pp at embargo=0,
+1.51pp at embargo=5). Only mean daily IC is consistently (weakly) positive.
**Broker identity is unproven** under the executable contract: the one
metric that survives both settings is small, and the metric the legacy
review flagged as decisive (pooled IC) now points the other way.

**Does `observable_inventory` (D) add anything beyond broker identity (C)?**

D − C is **negative on pooled IC in both settings** (−0.0190 and −0.0063) and
**negative on hit edge in both settings** (−0.21pp and −2.81pp, the second
one large). Mean daily IC is again the only metric weakly favouring D.
**`observable_inventory` V1 is unproven** — same conclusion as the legacy
review, now under a contract that removes the confound of an unreachable
close-to-close window, and it is if anything less favourable than before.

**No feature or hyperparameter rescue was performed.** `EXPERIMENT_XGB_PARAMS`,
`FLOW_WINDOWS`, `AGGREGATE_FEATURES`, and all four feature-set definitions in
`ml_v2_experiment_1.py` are byte-identical to the legacy Experiment #1 run;
the only changes between the legacy and #1E pipelines are the target/contract
(`fwd_oo_1` via `open_anchored=True`), the two realization purges, and the
`min_fit_days`/`min_eval_days` fold-thinness gate. Weak and sign-flipping
results were reported as measured, not tuned, re-parameterized, or re-run
until favourable.

## Review status

Experiment #1E is a **negative/weak finding, accepted as such**. It does not
overwrite or reinterpret the legacy Experiment #1 record
(`ML_V2_EXPERIMENT_1_RESULTS.md`), which remains the historical result under
its own (non-executable) contract. Under the executable contract:

- `existing_broker_aggregate`: weak, direction-only evidence of value over
  price alone. Not promotable.
- `broker_identity`: unproven incremental value over the aggregate. Not
  promotable, and the roadmap's earlier "retain as a research feature"
  characterization does not carry over to this contract.
- `observable_inventory` (V1): unproven, negative on the primary pooled-IC
  and hit-edge metrics. Not promotable. As under the legacy review, this is
  not evidence against real shareholder-ownership data — `observable_inventory`
  remains a distinct, coverage-bounded, non-ownership quantity.

No further tuning, feature engineering, or rescue attempt is authorized by
this result. Any next step (ownership/controller work, a revised feature
definition, a different horizon) requires its own explicit proposal and
review, per `ML_NEXT_STEP_BROKER_IDENTITY_ROADMAP.md`'s stop condition.
