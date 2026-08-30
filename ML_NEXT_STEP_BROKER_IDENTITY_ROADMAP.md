# ML V2: Broker Identity Roadmap

## Status and scope

The clean-price integration, invalid-Sharpe removal, and approximately 60-session
bag-holder coverage work were completed and committed before this roadmap was
added. Historical return figures produced outside `price_audit.clean_panel()`
remain void.

The only authorized next step is **Experiment #1**. Ownership, controller,
custodian, beneficial-owner, and owner-broker-affinity work is explicitly
blocked until Experiment #1 has been reviewed.

## Experiment #1 question

Does retaining broker identity add out-of-sample next-session ranking
information beyond price features and the existing aggregate broker features?
Does a strictly historical cumulative-net-lot representation called
`observable_inventory` add anything beyond broker identity?

This is a feature-information experiment, not a strategy optimization. It does
not use a strategy grid, DDQN, TP/SL tuning, position sizing, or holdout-driven
selection.

## Fixed data contract

- Source prices exclusively through `price_audit.clean_panel()`.
- Predict the clean one-session forward return (`fwd_1`).
- Retain the existing gap and physical ARA/ARB transition guards.
- Filter broker observations to clean `(date, ticker)` keys before deriving any
  feature.
- Use one common panel, one row order, and one precomputed chronological
  expanding-window split list for all variants.
- Use the same target rows for A/B/C/D. XGBoost's native missing-value handling
  is used rather than changing the sample per feature set.
- Lock the model seed and use one CPU worker so repeated local runs are stable
  enough for paired comparison.

## Feature sets

Every broker variant retains the exact A price controls. The labels below name
the broker representation added to those controls.

### A: `price_only`

- `momentum_1d`
- `volume_ratio`, calculated from prior volume only

### B: `existing_broker_aggregate`

A plus the existing aggregate features:

- broker concentration
- total net flow
- observed broker count
- net-buy ratio
- retail presence proxy
- one-session broker-vector correlation

### C: `broker_identity`

A plus broker-code-preserving normalized net-lot flows. For every broker code
present in the clean historical data, create 1d/3d/5d/10d/20d trailing net-lot
features normalized by the ticker's traded lots over the same window. Broker
codes remain in column names; they are not collapsed into anonymous summary
statistics.

Where live buy/sell value and average-price columns exist, historical net lots
are reconstructed as buy lots minus sell lots. For net-value-only backfill rows,
net lots are recovered using that day's clean close. A missing broker in the
captured broker set is an unobserved/zero captured flow, not proof that the
broker did not trade.

### D: `broker_identity + observable_inventory`

C plus per-broker historical cumulative net lots, normalized by trailing
20-session average traded lots. Accumulation restarts after a clean-panel gap so
an unobserved or quarantined interval is never bridged.

The feature name is always `observable_inventory`. It is not beneficial
ownership. It can be affected by nominees, transfers, incomplete top-broker
coverage, and the finite beginning of the available history. No cost basis,
ownership label, or inferred controller enters Experiment #1.

## Walk-forward protocol

- Expanding chronological training window.
- Fixed six-session test blocks after a minimum 30-session training history.
- The last 20% of each training window, split by date, is the early-stopping
  validation slice.
- All A/B/C/D fits receive the identical fit/evaluation/test dates.
- Pool every out-of-sample prediction once. Do not choose a favorable terminal
  period or optimize on the test blocks.

## Primary evaluation

Report each variant and its delta versus A:

- pooled Spearman IC
- mean daily cross-sectional IC
- median daily cross-sectional IC
- percentage of positive-IC dates
- top-decile hit rate
- base hit rate over the same rows
- hit edge
- top-decile return edge
- number of independent test dates

MAE may be retained as a diagnostic but is not the decision gate. No Sharpe
threshold or substitute satisfaction threshold is permitted.

The review must also show C minus B and D minus C. An isolated improvement in
one pooled statistic is not enough for a strong incremental-information claim;
the daily cross-sectional and top-decile diagnostics must be read together and
sample limitations must remain explicit.

## Stop condition

After Experiment #1, stop. Report whether broker identity and
`observable_inventory` add incremental out-of-sample information. Do not start
owner/controller mapping, ownership attribution, custodian mapping, or
owner-broker affinity until the experiment has been reviewed.
