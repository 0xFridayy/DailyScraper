# ML Experiment Review

Use this workflow to execute or review a predefined ML experiment.

## Before Running

1. Read the frozen experiment specification.
2. Confirm:
   - baseline
   - treatment feature sets
   - target
   - split protocol
   - seed
   - OOS rows
   - evaluation metrics
   - robustness tests
   - acceptance criteria
3. Do not change these after seeing results.
4. Verify point-in-time feature availability.
5. Verify no train/test contamination.

## Execution

6. Run all predefined variants using the same eligible OOS rows.
7. Report absolute metrics for every variant.
8. Report treatment-minus-baseline deltas.
9. Include at minimum, where applicable:
   - pooled IC
   - mean daily cross-sectional IC
   - median daily IC
   - positive-IC day rate
   - top-decile hit rate / hit edge
   - return edge
   - Sharpe
   - max drawdown
   - turnover
   - coverage / sample count

## Robustness

10. Run predefined robustness checks, including where applicable:
    - paired-date bootstrap
    - block bootstrap
    - regime splits
    - leave-one-ticker-out
    - leave-one-broker-out
    - leave-one-entity-out
11. Check whether results are concentrated in a small number of names, brokers, entities, or dates.

## Decision

12. Classify each tested feature/group as one of:
    - robust incremental information
    - directional but not robust
    - neutral / inconclusive
    - rejected
13. Do not promote research features to production automatically.
14. Do not start the next experiment automatically.
15. STOP for user review.