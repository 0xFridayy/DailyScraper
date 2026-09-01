# ML V2 Experiment #2: Ownership Information

## Status

**Specification stage — not yet implemented.** This document is a reviewable spec.
No Experiment #2 code has been written. The stop condition from the original
roadmap is lifted only for entity canonicalization; the decision below carries
that forward with narrower scope: feature engineering for Experiment #2 is now
in-scope for design, but code implementation is blocked until this spec is
reviewed and approved, AND the minimum history requirement is satisfied.

## Question

Does point-in-time shareholder ownership information add incremental
out-of-sample next-session ranking information beyond the accepted Experiment #1
baseline (C: `broker_identity`)?

This is a feature-information experiment, not a strategy optimization.

## What this experiment does NOT do

| Constraint | Rationale |
|---|---|
| Does not revive `observable_inventory` | Experiment #1 rejected it. It degraded C on every headline metric. |
| Does not start owner-broker mapping | Blocked pending separate review. |
| Does not require all R4 alias pairs to be resolved | Unresolved pairs stay separate; the experiment is designed to run regardless. |
| Does not use `entity_alias_candidate` (per-ticker) | Global entity IDs from `entity_canonical` / `entity_alias` replace per-ticker QA candidates. |
| Does not infer ownership from broker net-lot flows | That was `observable_inventory`. Ownership comes from the NeoBDM ownership panes only. |
| Does not backfill `investor_name_canonical` | Source rows stay byte-untouched; identity is served by joining `entity_alias`. |

## Fixed data contract

- Source prices exclusively through `price_audit.clean_panel()`.
- Target: clean one-session forward return (`fwd_1`).
- Retain existing gap and physical ARA/ARB transition guards.
- Filter broker observations to clean `(date, ticker)` keys before deriving
  any feature.
- Ownership features use the `entity_alias` / `entity_canonical` join on
  `investor_name_raw` to resolve global entity IDs. The same entity may appear
  under multiple tickers (58 cross-ticker entities as of 2026-09-01); this is
  expected and is the reason IDs are global.
- One common panel, one row order, one precomputed chronological
  expanding-window split list for all variants.
- Use the same model seed and XGBoost parameters as Experiment #1 (`random_state=17`,
  `n_jobs=1`, `tree_method=hist`) for direct comparability of C-vs-C deltas.

## Ownership feature design

Features are organized into **level** (computable from one snapshot) and
**change/history** (requiring a sequence of snapshots). Each feature category
is intentionally distinct so ablations can isolate what contributes.

### Group O1: Ownership level — single-snapshot

Computable from one `ownership_snapshot` capture on the most recent available
date. Uses global entity IDs so the same entity across multiple appearances
(ticker + name) is counted once.

| Feature | Source | Description |
|---|---|---|
| `own_pct_total` | KDA 1% + 5% | Sum of disclosed ownership percentages across all entities on the latest snapshot. May be less than 100% — small holders and sub-threshold holders are invisible. |
| `own_pct_top5` | KDA 1% + 5% | Ownership % held by the top-5 entities by stake. |
| `own_pct_top10` | KDA 1% + 5% | Ownership % held by the top-10 entities by stake. |
| `own_hhi` | KDA 1% + 5% | Herfindahl–Hirschman Index of ownership concentration (sum of squared ownership fractions, normalized). Higher = more concentrated. |
| `own_institutional_pct` | KDA 1% + 5% | Fraction of disclosed ownership held by corporate/institutional entities (investor_category = 'Corporate F' or 'Corporate'). Individual/retail holders excluded. |
| `own_foreign_pct` | KDA 1% + 5% | Fraction of disclosed ownership held by foreign entities (is_foreign = 1). |
| `own_holder_count` | KDA 1% + 5% | Number of distinct disclosed entities on the latest snapshot. |

### Group O2: Ownership change — from PKDA (ownership_change)

Computed by differencing consecutive ownership snapshots for the same entity
on the same ticker. These capture observable movements in disclosed ownership.

| Feature | Source | Description |
|---|---|---|
| `own_net_lot_change_1d` | PKDA 5% | Net lot change from the most recent custodian transfer observation. Signed. |
| `own_flow_magnitude_1d` | PKDA 5% | Absolute value of the most recent lot change. Captures activity magnitude regardless of direction. |
| `own_abs_pct_change_1d` | PKDA 1% | Absolute change in ownership % from the most recent monthly disclosure change. |
| `own_new_entries_5d` | PKDA 5% | Count of entity names that appeared in the last 5 trading days but were absent from the immediately preceding snapshot. |
| `own_exits_5d` | PKDA 5% | Count of entity names that disappeared from the most recent snapshot relative to the one 5 days prior. |
| `own_turnover_5d` | PKDA 5% | (entries + exits) / total holder count over the trailing 5-day window. A measure of ownership churning. |
| `own_concentration_change_5d` | KDA 1% + 5% | Change in top-5 concentration (own_pct_top5) over the trailing 5 trading days. Positive = ownership is concentrating. |

### Group O3: Ownership persistence

Requires multiple consecutive snapshots. Measures how stable the ownership
base is — a signal distinct from level or change.

| Feature | Source | Description |
|---|---|---|
| `own_persistent_holder_pct` | KDA 1% + 5% | Fraction of current entities that were also present in K of the last N snapshots (K/N tunable; default 3/5). High persistence = stable base, low persistence = frequent reshuffling. |
| `own_stable_concentration` | KDA 1% + 5% | HHI computed using only the persistent-holder subset. Captures whether concentration is driven by stable holders or transient ones. |

### Group O4: Cross-sectional holder behavior

Requires entity-level global IDs across tickers. Measures whether the entity
base of this ticker overlaps meaningfully with the broader market — a proxy
for institutional connectedness.

| Feature | Source | Description |
|---|---|---|
| `own_crowding` | KDA 1% + 5% | Fraction of this ticker's disclosed entities that also appear under at least one other ticker (using global entity IDs). High crowding = the holder base is typical of the market, low = unusual concentration. |
| `own_common_holder_pct` | KDA 1% + 5% | Fraction of this ticker's entities that appear in the global top-50 holder list (by frequency across all tickers). High overlap with the most common holders may signal institutional attention. |

### Feature availability and the history constraint

| Feature group | Minimum consecutive ownership captures needed | Earliest computable date (if daily captures continue from 2026-09-01) |
|---|---|---|
| O1 (level) | 1 | 2026-09-01 (available immediately) |
| O2 (change) | 2 (for 1d); 5+ (for 5d) | ~2026-09-08 |
| O3 (persistence) | 5+ (default K=3, N=5) | ~2026-09-08 |
| O4 (cross-sectional) | 1 (global IDs already exist) | 2026-09-01 |

However, the **minimum viable experiment** requires that the walk-forward
training window has enough rows where ownership features are available to learn
a meaningful signal, not just that they are technically computable. With a
minimum 30-session training window, at least 20 of those sessions should have
ownership-change history (Groups O2/O3) available, not just level features.

## Minimum history requirement

Experiment #2 is not allowed to run until:

1. **At least 60 consecutive trading days of ownership captures** have been
   ingested into `neobdm_ownership.db` via the daily ownership-capture workflow.
   This provides:
   - ~40 trading days where O2/O3 features are available with full depth
   - A training window where ownership-change and persistence signals have
     enough history to differentiate from noise
   - At least 30-session minimum training depth with O2/O3 features on ≥20 of
     those sessions

2. **The 60-day window must include no gaps exceeding 3 consecutive trading
   days.** The ownership-capture workflow runs daily; brief gaps from workflow
   failures are expected and tolerable, but a >3-day gap means a missing
   observation that breaks the change/persistence feature chain. Short gaps
   are handled by forward-filling the last known snapshot; long gaps require
   investigation.

3. **Entity canonicalization must be re-run** on the accumulated data after
   the 60-day threshold is reached, to capture any new aliases or entities
   that emerged in the ownership history.

With daily captures starting 2026-09-01, the earliest feasible Experiment #2
run date is approximately **2026-11-30** (60 calendar days of captures at
~20 trading days/month). This date will be refined based on actual capture
completeness.

## Walk-forward protocol

Identical to Experiment #1, extended to support ownership features that require
lookback:

- Expanding chronological training window.
- Fixed six-session test blocks after a minimum 30-session training history.
- Last 20% of each training window = early-stopping validation slice.
- All variants receive identical fit/evaluation/test dates.
- Pool every out-of-sample prediction once; no period selection.

**Adaptation for ownership features:** In the expanding window, rows where an
ownership feature is not yet available (because the capture history is too
short) have that feature set to `NaN` / missing. XGBoost's native missing-value
handling is used, consistent with Experiment #1. The model learns from the
subset of rows where each feature is available without requiring imputation.

## Baseline and treatment groups

| Label | Feature set | Purpose |
|---|---|---|
| **C** | `broker_identity` (reproduced from Experiment #1) | The accepted baseline. All OOS deltas are measured against this. |
| **E1** | C + O1 (ownership level) | Does the snapshot composition add anything? This is the lowest-barrier test: it needs only one ownership capture date. |
| **E2** | C + O1 + O2 (level + change) | Does adding observable ownership movements add beyond level alone? Isolates the change signal. |
| **E3** | C + O1 + O2 + O3 (level + change + persistence) | Does holder stability add beyond level and change? |
| **E4** | C + O1 + O2 + O3 + O4 (all ownership) | Full ownership feature set vs C. The headline comparison. |

**Not a separate group:** `observable_inventory` (the rejected Experiment #1
feature) is not revived as a treatment or control.

### Ablation groups (require approval during implementation)

| Label | Feature set | Question answered |
|---|---|---|
| **A1** | C + O2 + O3 (change + persistence only, no level) | Do level features matter, or is the signal entirely in the dynamics? |
| **A2** | C + O1 + O3 (level + persistence, no change) | Do observable flows add beyond the current snapshot? |
| **A3** | C + O1 + O2 + O4 (level + change + cross-section, no persistence) | Is persistence the key, or does cross-sectional behavior suffice? |

Ablation groups are approved for inclusion in the spec here but may be dropped
during implementation if the headline results (E1–E4 vs C) make them
unnecessary.

## Primary evaluation

Same metrics as Experiment #1, all measured against C (the accepted baseline):

- Pooled Spearman IC
- Mean daily cross-sectional IC
- Median daily cross-sectional IC
- Percentage of positive-IC dates
- Top-decile hit rate
- Base hit rate over the same rows
- Hit edge
- Top-decile return edge
- Number of independent test dates

**Incremental comparisons:** E1−C, E2−E1, E3−E2, E4−E3. Same structure as
C−B and D−C in Experiment #1: an isolated improvement in one statistic is not
sufficient; the daily cross-sectional and top-decile diagnostics must be read
together.

**No Sharpe or substitute satisfaction threshold is permitted.**

## Robustness checks (mirrors Experiment #1)

| Check | Method |
|---|---|
| Paired per-date bootstrap (i.i.d. and weekly-block) | 20,000 resamples; same as Experiment #1 |
| Leave-one-ticker-out | Drop one ticker, refit, measure influence |
| Leave-one-entity-out | Drop one global entity (e.g. TIRTA ORISA YASA), refit — tests whether the ownership signal is concentrated in a few cross-ticker names |
| Leave-one-broker-group-out | Same as Experiment #1 — ensures ownership features are not merely absorbing broker identity's signal via a confound |
| IHSG regime audit | Down-regime vs up-regime — ownership information may be more informative during sell-offs when holder behavior is more information-rich |

## Decision framework

The same framework as Experiment #1: read the headline metrics together, qualify
with bootstrap intervals, and report influences. No single metric is a pass/fail
gate.

**Likely interpretability constraints:**
- Ownership data may be more informative for large-cap tickers where disclosure
  coverage is better (sub-threshold holders are invisible, so small-cap
  ownership is structurally incomplete).
- Level features (O1) may have low marginal value if concentration is already
  captured by broker aggregate features; the interesting signal is likely in
  O2 (change) and O3 (persistence).
- O4 (cross-sectional) is exploratory — it may not add ranking information
  but could be useful for regime detection or position sizing if the project
  advances.

## Open questions for implementation review

1. **O1 normalization:** Should `own_pct_total` / `own_hhi` be normalized
   cross-sectionally (z-scored across tickers each date) or left raw?
   Cross-sectional normalization prevents the model from learning "ticker X
   always has high concentration" but may discard level information.

2. **O2 lag structure:** The spec above uses 1d and 5d lags. Should 10d and
   20d lags also be included (consistent with Experiment #1's 1d/3d/5d/10d/20d
   broker lags), or are ownership flows too sparse for longer lookbacks?

3. **KDA 5% vs KDA 1% coverage:** The 5% pane (insider5p) is a subset of the
   1% pane (insider-current) but has richer custody detail. Should level
   features use the 1% pane only (broader coverage) or the union of both?

4. **Entity category mapping:** The `investor_category` field uses Indonesian
   labels (Corporate, Corporate F, Individual). Should these be mapped to
   a fixed taxonomy for `own_institutional_pct`, or used as-is?

5. **Minimum history target:** 60 trading days is the floor. Should the spec
   recommend running at 90 days (~2027-01-01) instead, to give O2/O3 features
   deeper lookback in the first training windows?
