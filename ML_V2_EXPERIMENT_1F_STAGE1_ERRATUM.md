# Experiment #1F Stage-1 — Erratum v1: bootstrap p-values for negative effects

**Status:** erratum to a CLOSED, FROZEN experiment. The frozen records are **not modified**.
The corrected values below are **normal-approximation estimates**, not exact bootstrap results,
and must not be substituted into the frozen closeout. No rerun was performed.

## Binding

| Identity | Value |
|---|---|
| Frozen boundary | commit `ee79e23c70046351201f313ca4b7e44f3cbb58b7`, tag `exp-1f-gate-b-stage1-frozen` |
| Gate-B contract (canonical) | `e69ffd1fe4d4274f2f9052fc2bd5b86f94b7b927beb683381a77e04c6a4950ce` |
| Execution manifest | `gate_b_execution_manifest_v1.json` sha256 `017a2662f49602ec18bdd072a84415b9d556a27997d8161dbd5dee6baae1cf43` |
| Stage-1 closeout | `experiment_1f_stage1_closeout_v1.json` sha256 `2f2caa1f846f36d5a181a08538a48e590a2d5904e6dba0227de4c40bb79df36a` |
| Graduation report | `stage1_ee79e23c7004/graduation_report.json` sha256 `8f25d5a90543409016839c84ce96273471cba5cbc18533041468b5a6bac7e1eb` |
| Result ledger (unaffected) | `stage1_ee79e23c7004/result_ledger.json` sha256 `0adf9f00f19043c68d09bdbfc08ace01b9b6340bb812fa77d2e7391a858cedc0` |
| Durable archive | `experiment_1f_stage1_ee79e23.tar` sha256 `2edcd7f4ea5b4e5cd0d4bd3f2c5c29c98abf6389368e472ae07f63e1c8510580` (345 files) |
| Per-file checksums | `ML_V2_EXPERIMENT_1F_STAGE1_ARCHIVE_CHECKSUMS.sha256` (sha256 `38013f1b99131e4caa2aa252acab42a45718c9192aa4f50855ba1ad80d3ec6e3`) |
| Defect fixed in | commit `cf78195` (post-freeze), regression test `test_bootstrap_pvalue_is_symmetric_for_negative_effects` |

## Defect

`experiment_1f_evaluation.basic_bootstrap_pvalue` compared the centred bootstrap distribution against the
signed thresholds `+theta_hat` and `-theta_hat`. That is correct only when `theta_hat > 0`. For a negative
estimate both tail counts span the centre of the distribution, so the two-sided p collapses to (near) 1 even
when the basic 95% interval excludes zero. The fix uses `|theta_hat|`.

The error is **conservative**: it could hide a significant negative effect, never manufacture a significant
one. Estimates (`theta`), standard errors, basic confidence intervals, placebo statistics and the G2–G5
conditions do not use this function and are unaffected. Every affected field has `theta < 0`; values with
`theta > 0` were computed correctly.

## Affected fields

Approximate p = `2·(1 − Φ(|θ| / SE))`, using each entry's recorded θ and bootstrap SE. Holm-adjusted rows
re-apply the registered Holm step-down to the block-10 primary p-values (B−A keeps its recorded,
correctly-computed p).

| Record | Field | Role | θ | Basic 95% CI | Recorded p | Approx. p |
|---|---|---|---|---|---|---|
| graduation_report | `report.increments.C-B.primary.block_5.p` | PRIMARY (graduation arm lag1/H1) | -0.005199 | [-0.01214, +0.00191] | 1.000 | 0.144 |
| graduation_report | `report.increments.C-B.primary.block_10.p` | PRIMARY (graduation arm lag1/H1) | -0.005199 | [-0.01123, +0.00150] | 1.000 | 0.109 |
| graduation_report | `report.increments.C-B.primary.block_20.p` | PRIMARY (graduation arm lag1/H1) | -0.005199 | [-0.01165, +0.00193] | 1.000 | 0.136 |
| graduation_report | `report.increments.D-C.primary.block_5.p` | PRIMARY (graduation arm lag1/H1) | -0.002489 | [-0.00713, +0.00232] | 1.000 | 0.299 |
| graduation_report | `report.increments.D-C.primary.block_10.p` | PRIMARY (graduation arm lag1/H1) | -0.002489 | [-0.00734, +0.00276] | 1.000 | 0.333 |
| graduation_report | `report.increments.D-C.primary.block_20.p` | PRIMARY (graduation arm lag1/H1) | -0.002489 | [-0.00722, +0.00263] | 1.000 | 0.328 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|BC\|C-B\|h2.ic.p` | SENSITIVITY (never decides graduation) | -0.005493 | [-0.01599, +0.00481] | 1.000 | 0.303 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|BC\|C-B\|h3.ic.p` | SENSITIVITY (never decides graduation) | -0.000284 | [-0.01355, +0.01037] | 0.990 | 0.963 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|BC\|C-B\|h4.ic.p` | SENSITIVITY (never decides graduation) | -0.007432 | [-0.02653, +0.01107] | 1.000 | 0.439 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|BC\|C-B\|h5.ic.p` | SENSITIVITY (never decides graduation) | -0.007419 | [-0.02701, +0.01144] | 1.000 | 0.449 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|CD\|D-C\|h2.ic.p` | SENSITIVITY (never decides graduation) | -0.000082 | [-0.00548, +0.00517] | 1.000 | 0.976 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|CD\|D-C\|h3.ic.p` | SENSITIVITY (never decides graduation) | -0.001291 | [-0.01028, +0.00744] | 1.000 | 0.776 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|CD\|D-C\|h4.ic.p` | SENSITIVITY (never decides graduation) | -0.000648 | [-0.01108, +0.01149] | 1.000 | 0.911 |
| graduation_report | `sensitivity_never_decides_graduation.exploratory\|lag1\|CD\|D-C\|h5.ic.p` | SENSITIVITY (never decides graduation) | -0.003050 | [-0.01457, +0.00836] | 1.000 | 0.606 |
| graduation_report | `sensitivity_never_decides_graduation.robust_all_variant\|lag1\|common_ABCD\|C-B\|h1.ic.p` | SENSITIVITY (never decides graduation) | -0.008245 | [-0.01436, -0.00245] | 1.000 | 0.006 |
| graduation_report | `sensitivity_never_decides_graduation.robust_all_variant\|lag1\|common_ABCD\|D-C\|h1.ic.p` | SENSITIVITY (never decides graduation) | -0.002489 | [-0.00746, +0.00279] | 1.000 | 0.332 |
| graduation_report | `sensitivity_never_decides_graduation.robust_timing_intersection\|lag0\|AB\|B-A\|h1.ic.p` | SENSITIVITY (never decides graduation) | -0.000027 | [-0.00511, +0.00520] | 1.000 | 0.992 |
| graduation_report | `sensitivity_never_decides_graduation.robust_timing_intersection\|lag1\|BC\|C-B\|h1.ic.p` | SENSITIVITY (never decides graduation) | -0.005393 | [-0.01219, +0.00191] | 1.000 | 0.133 |
| closeout | `registered_statistics_lag1_h1.C-B.raw_bootstrap_p` | REGISTERED (closeout) | -0.005199 | [-0.01123, +0.00150] | 1.000 | 0.109 |
| closeout | `registered_statistics_lag1_h1.D-C.raw_bootstrap_p` | REGISTERED (closeout) | -0.002489 | [-0.00734, +0.00276] | 1.000 | 0.333 |
| closeout | `registered_statistics_lag1_h1.B-A.holm_adjusted_p` | REGISTERED (closeout, derived via Holm) | +0.000773 | [-0.00405, +0.00538] | 1.000 | 0.743 |
| closeout | `registered_statistics_lag1_h1.C-B.holm_adjusted_p` | REGISTERED (closeout, derived via Holm) | -0.005199 | [-0.01123, +0.00150] | 1.000 | 0.328 |
| closeout | `registered_statistics_lag1_h1.D-C.holm_adjusted_p` | REGISTERED (closeout, derived via Holm) | -0.002489 | [-0.00734, +0.00276] | 1.000 | 0.666 |

## Method and limitations

- **Validation of the approximation:** on the positive B−A primary increment (computed correctly by the
  original code) the approximation gives 0.750 against the recorded bootstrap p 0.743.
- **Not exact.** Exact corrected bootstrap p-values need the per-date increment series and the registered
  resampling; Stage-1 did not persist those series. Recomputing them would require regenerating inputs from
  the archived prediction ledgers and is deliberately **not** done in this erratum.
- Sensitivity entries are exploratory and are **not** multiplicity-adjusted here.

## Consequence for the frozen classifications

**Unchanged.** Recorded: A_price = SENSITIVITY_ONLY, B_minus_A_aggregate_broker_information = REJECT_FOR_NOW, C_minus_B_broker_identity = REJECT_FOR_NOW, D_minus_C_bounded_broker_state = REJECT_FOR_NOW.

- **G1** requires a *positive* primary θ **and** Holm rejection. C−B (θ = −0.0052) and D−C (θ = −0.0025) are
  negative, so G1 is false for both regardless of p. B−A is positive but its recorded p (0.743) was correct.
- With the approximate p-values the Holm step-down rejects **none** of the three registered increments
  (approx. adjusted: B−A 0.743, C−B 0.328, D−C 0.666), identical in outcome to the recorded all-1.0 values.
- All three increments therefore remain **REJECT_FOR_NOW**; A remains **SENSITIVITY_ONLY**.

## Noted, not reclassified

`sensitivity_never_decides_graduation.robust_all_variant|lag1|common_ABCD|C-B|h1` has θ = −0.0082 with an
approximate p ≈ 0.006 (recorded 1.0), and its basic 95% interval [−0.01436, −0.00245] — computed correctly by
the original code — already excludes zero: under that robustness variant, adding broker identity **degraded**
ranking by a margin unlikely to be noise. This is a sensitivity arm that never decides graduation, the p is
approximate and not multiplicity-adjusted, and it is consistent with — not a revision of — the closeout's
conclusion C ("no evidence of a practically useful positive C−B H1 increment"). It is not a confirmatory
finding and must not be cited as one without an exact, pre-registered test.

## Archive verification

The archive is stored privately (it contains data derived from licensed NeoBDM inputs) and is not committed.
To verify a copy:

```bash
sha256sum -c experiment_1f_stage1_ee79e23.tar.sha256
tar -xf experiment_1f_stage1_ee79e23.tar
cd experiment_1f_stage1_ee79e23 && sha256sum -c ../ML_V2_EXPERIMENT_1F_STAGE1_ARCHIVE_CHECKSUMS.sha256
```

## What this erratum does not do

- It does not rerun, refit or re-score Experiment #1F.
- It does not edit the frozen closeout, graduation report, ledgers, contract or manifests.
- It does not open a new stage; any later stage needs a new execution identity.
