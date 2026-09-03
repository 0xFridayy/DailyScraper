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

Experiment #2 is not allowed to run until ALL of the following are satisfied.
The 60-day clock is necessary but not sufficient; the signal-rate gates below
are the binding constraints based on what the data actually contains.

### Gate 1: 60 trading days of capture history

**At least 60 consecutive trading days of ownership captures** must be
ingested into `neobdm_ownership.db` via the daily ownership-capture workflow.
This provides:
- ~40 trading days where O2/O3 features are available with full depth
- A training window where ownership-change and persistence signals have
  enough history to differentiate from noise
- At least 30-session minimum training depth with O2/O3 features on ≥20 of
  those sessions

The 60-day window must include no gaps exceeding 3 consecutive trading
days. The ownership-capture workflow runs daily; brief gaps from workflow
failures are expected and tolerable, but a >3-day gap means a missing
observation that breaks the change/persistence feature chain. Short gaps
are handled by forward-filling the last known snapshot; long gaps require
investigation.

Entity canonicalization must be re-run on the accumulated data after the
60-day threshold is reached, to capture any new aliases or entities that
emerged in the ownership history.

With daily captures starting 2026-09-01, the earliest feasible date for
Gate 1 is approximately **2026-11-30** (60 calendar days of captures at
~20 trading days/month). This date will be refined based on actual capture
completeness.

### Gate 2: Effective-sample signal-rate requirement

The 60-day clock only tells us when history is *available*. It does not tell
us whether that history contains *enough real signal* for the experiment to
distinguish a real effect from noise. As of 2026-09-01, the current data
illustrates the problem:

- 975 PKDA-5% rows total, of which only 62 (6.4%) are actual
  `is_custodian_move=1` events. The other 913 are threshold adjustments
  and registration artifacts — the `own_net_lot_change_1d` and
  `own_flow_magnitude_1d` features are mostly zero or near-zero.
- 221 distinct change_dates but only 34 tickers with PKDA-5% data, meaning
  ~1.8 custodian moves per ticker per date on average across the whole
  history. 7 tickers have only one PKDA-5% row, and 5 tickers have 50+,
  so the signal is highly concentrated in a few names.
- 3 distinct snapshot dates across all tickers. O1 (level) and O3
  (persistence) features are technically computable but have
  near-zero historical depth.

These patterns are likely to persist (or grow slightly) with 60 days of
daily captures, because the underlying NeoBDM disclosure cadence is
monthly for the 1% pane and event-driven for the 5% pane. The
60-day clock alone would not produce a materially better training set than
what the data already shows.

**Gate 2 — all of the following must hold in the 60-day window:**

| Sub-gate | Requirement | Why |
|---|---|---|
| 2a. Custodian-move rate | At least **5 custodian-move events per ticker** on average across tickers with PKDA-5% history. | Below this, `own_net_lot_change_1d` and `own_flow_magnitude_1d` are too sparse for XGBoost to learn a split. Targets the bottom of the distribution: at least 29 of 34 tickers (the 85th percentile) must clear this. |
| 2b. Active-ticker coverage | At least **30 tickers** have ≥1 PKDA-5% row in the 60-day window. | Matches the current 34; if new tickers appear, that's fine. If coverage shrinks, the universe has changed and the experiment should not run. |
| 2c. Snapshot depth | At least **20 distinct snapshot dates** in `ownership_snapshot` across the window. | O1 and O3 features require snapshot history. With only 3 distinct snapshot dates currently, this is the binding gate — daily captures alone will not produce 20 snapshot dates because the snapshot pane is monthly, not daily. The 60-day window will yield ~3 monthly snapshots at most. This gate is **almost certainly not satisfiable** with the current capture cadence. |
| 2d. New-entry / exit sample | At least **50 new-entry events** and **50 exit events** (O2's `own_new_entries_5d` / `own_exits_5d`) across the window. | Entry/exit features need a baseline rate of ~1 per trading day; 50 events is a minimum for the model to learn the split. |
| 2e. Persistent-holder sample | At least **100 ticker-dates** where the persistent-holder feature is computable (i.e. ≥3 distinct snapshot dates are available for that ticker). | O3 features require lookback; below this, the feature is mostly NaN. |

**Gate 3: Snapshot cadence**

Sub-gate 2c almost certainly blocks Experiment #2 with the current
ownership-capture cadence. The snapshot pane is monthly, so 60 days
of daily captures yields at most 2–3 snapshot dates per ticker. The
ownership-capture workflow should be modified to capture KDA 1% and
KDA 5% snapshots at the daily frequency NeoBDM makes available, not
just when a new month closes. Until then, the experiment is blocked
on Gate 3, regardless of Gate 1 and Gate 2.

This is a separate, earlier piece of work: modify the capture pipeline
to snapshot on a more frequent cadence, run for 60 days, then evaluate
Gate 1 and Gate 2.

### What this changes about the roadmap

The "60 trading days" threshold is now a **necessary but not sufficient**
condition. The binding constraint is **Gate 3** (snapshot cadence) followed
by **Gate 2c** (20 distinct snapshot dates). With the current monthly
cadence, neither is achievable in 60 days.

The corrected sequencing is:

1. **First** (separate task, blocked until approved): modify
   ownership_capture.py to capture snapshots on NeoBDM's available
   frequency, not just at month-close. Run the new cadence for
   60 trading days.
2. **Then** (this experiment): evaluate Gate 1 and Gate 2. If both pass,
   Experiment #2 is allowed to run. If Gate 2 fails on any sub-gate,
   the experiment does not run and the data is re-assessed in 30
   days.
3. **Implementation is still blocked** until Gate 1, Gate 2, and Gate 3
   are all satisfied. The estimated earliest feasible date with the
   current cadence is **never** (Gate 2c unachievable). With a
   higher-frequency capture pipeline in place, the earliest feasible
   date is approximately 60 trading days after that pipeline change.

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

---

## Experiment #2A0: frozen exploratory event study

**Approval status:** approved with final corrections on 2026-09-02. This is a
separate exploratory event study. Experiment #2A ML remains **FAILED** for
insufficient breadth/effective sample. No #2A0 result can revive #2A, authorize
ML training, or promote an ownership feature to production.

### Frozen source population and unit of analysis

The source filter is immutable:

```sql
threshold = '5pct'
AND is_custodian_move = 1
AND lot_change IS NOT NULL
AND lot_change != 0
```

The confirmed source population is 45 rows (18 positive, 27 negative), 36
unique `(ticker, change_date)` primary events, 39 canonical holder-events, and
11 tickers. The earlier 62-row count is not the source population because it
includes rows that fail the non-null/non-zero economic-event requirement.
Multiple rows or holders on one ticker-date collapse to one equal-weighted
primary event; no sign or magnitude weighting is allowed.

The filter is evaluated **as of the freeze**, not as a standing query. The
frozen population is the set of matching rows first captured strictly before
`2026-09-02`, the day the 45/36/11 audit was confirmed. Without that bound the
filter has no upper bound in time and each subsequent daily ownership capture
would enlarge the cohort the spec calls immutable — as happened on 2026-09-03,
when a newly disclosed SINI move took the unbounded query to 46 rows / 37
events / 12 tickers. Rows matching the filter but first captured on or after
the boundary are post-freeze accretion: they are reported alongside the audit,
they never enter this study, and their arrival is not cohort drift. Drift now
means what it was meant to mean — the frozen 45 rows themselves changed — and
is detected by both the counts above and a content digest over those rows.

Concentration is frozen as BREN = 13/36 (36.1%) and the top five tickers
(BREN, BRMS, EMTK, FAST, SCMA) = 28/36 (77.8%). Every outcome must be reported
for the full sample (36 events/11 tickers), ex-BREN (23/10), all 11
leave-one-ticker-out samples (23--35 events/10 tickers), and ex-top-five as a
stress test (8/6).

### Mandatory observability gate

This gate must pass before any price or return analysis. Verify what
`ownership_change.change_date` represents and when each underlying fact became
observable to market participants. Strict-next-session alignment may use
`change_date` only if it is independently established as an observable-date
proxy. If publication/availability is later, use the earliest defensible
observable date instead. Historical facts first discovered in a backfill must
not be backdated. If event-level observability cannot be established, stop and
report the failure; do not compute returns, placebos, or inference.

**Current status (2026-09-02): BLOCKED.** The stored provenance does not
establish historical event-level publication times. A publication-timestamp
recovery audit was started against official BEI/KSEI/OJK records, then stopped
before all 36 primary events could be adjudicated. Recovery is therefore
**INCOMPLETE**: no candidate match is accepted as verified, no observability
manifest has been approved, and the event study must not run.

### Timing and outcomes

For an event that passes the observability gate, `t0` is the first ticker
session strictly after the defensible observable date with a valid positive
close. The entry/base price is the close at `t0`. Frozen horizons are 1, 5, 10,
and 20 ticker trading sessions after `t0`:

```text
R(i,h) = P(i,t0+h) / P(i,t0) - 1,  h in {1, 5, 10, 20}
AR(i,h) = R(i,h) - R(IHSG,t0 to t0+h)
```

The IHSG return uses the identical calendar endpoints. No transaction costs
apply. Coverage is horizon-specific: an event missing an endpoint remains in
the frozen manifest but is missing only for that horizon, with no imputation,
replacement, or shorter substitute horizon.

For raw and IHSG-adjusted returns at every horizon and required concentration
sample, report the equal-weighted mean (primary estimator), median, percentage
above zero, N events, N tickers, minimum, and maximum.

### Sign restriction

`is_custodian_move=1` is an inclusion flag only. Positive/negative
`lot_change` must not be interpreted as bullish/bearish,
accumulation/distribution, or inflow/outflow until both fields' economic
semantics are independently verified. Mixed-sign events are not netted. Any
signed, magnitude-weighted, or long/short study requires a separately approved
preregistration.

### Same-ticker placebo

Use 10,000 Monte Carlo repetitions with seed `20260200`. For each real event
and each horizon separately, sample an eligible pseudo-date for the same ticker
within the same calendar month. If insufficient unused eligible dates remain
there to preserve the without-replacement per-ticker event count, use the
predefined +/-40 IDX-session window around the real event. Always exclude every
actual event date for that ticker. Sampling is without replacement within a
repetition and preserves each included ticker's exact event count.

Eligibility is horizon-specific and must mirror the real sample: 1d pseudo
tests require valid 1d forward coverage, 5d require 5d, 10d require 10d, and
20d require 20d. Shorter-horizon pseudo-events must not be required to have 20d
coverage. Apply the identical observable-date alignment and outcome rules.
Report the observed mean's placebo percentile and two-sided empirical p-value:

```text
p = (1 + count(abs(placebo mean) >= abs(observed mean))) / 10001
```

Run this for full, ex-BREN, all leave-one-ticker-out, and ex-top-five samples.

### Uncertainty, multiplicity, and frozen interpretation

Do not headline IID event-level standard errors. Report separate 95% percentile
intervals from (a) a whole-ticker cluster bootstrap, 10,000 repetitions, seed
`20260201`, and (b) a bootstrap of non-overlapping 20-IDX-session date blocks
assigned by `t0`, 10,000 repetitions, seed `20260202`. Never select whichever
interval is narrower.

For every outcome/horizon, the leave-one-ticker-out table must include all 11
estimates, its minimum/maximum, sign stability, and the omission producing the
largest absolute change from the full-sample estimate. Report nominal placebo
p-values and Holm-adjusted p-values across the eight full-sample tests (two
outcomes x four horizons); sensitivity samples are secondary.

The following interpretations are frozen:

- A nominal full-sample result that becomes non-significant or reverses sign
  ex-BREN is concentration-driven.
- The eight-event ex-top-five result cannot independently establish a signal.
- Single-horizon significance is exploratory only.
- Leave-one-ticker-out sign instability is ticker dependence.
- No result is causal, establishes economic direction/tradability, promotes a
  feature, authorizes ML, or changes Experiment #2A's failed status.
