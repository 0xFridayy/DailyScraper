# Corporate-action contract remediation verification

Verified on 2026-10-08 on `feat/corporate-action-contract-pr83`. These results describe the final settled remediation source, not an intermediate subagent run.

The previous reviewed commit is `9e32b14e7e828d7fb300bad051af491ea4c8cac2`. Its parent remains specification commit `d7889749c2a6232e33c5ec3f82468eb593b269e6`, whose parent is original PR83 base `5a2ec4181ad897e3d6455c5b7855e0d9830b1768`. Remediation commits extend this history without rewriting it. No merge or deployment was performed.

## Final validation

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/ca-independent-harness:/tmp/ca-test-deps \
  python verify_corporate_action_contract.py --test-deps /tmp/ca-test-deps
```

The harness blocks network access. The runner uses disposable source snapshots and the explicit read-only historical fixture under `/tmp/ca-independent-review`, SHA256 `6fc475e6db6be597a539a8cc30f6a0c44f05a5c14b263367c07a3aa417389be5`. Production databases and credentials are excluded. Static workflow/ignore files and unchanged synthetic trade parsing/lock test modules are included; BandarmoloNY directories/data remain excluded.

Final result: **1,822 pytest passes, 330 additional subtests, 2 optional Torch skips; all 29 suites exited 0.**

| Suite | Final result |
| --- | --- |
| `test_price_contract.py` | 28 passed in 4.69s |
| `test_inventory_capture.py` | 56 passed in 1.79s |
| `test_walk_forward_canonical.py` | 56 passed in 4.68s |
| `test_ddqn_canonical.py` | 42 passed, 2 skipped in 2.30s |
| `test_daily_picks.py` | 60 passed in 0.32s |
| `test_arb_veto.py` | 10 passed in 1.10s |
| `test_broker_book.py` | 14 passed in 0.52s |
| `test_broker_rules.py` | 16 passed in 0.35s |
| `test_broker_learning.py` | 31 passed in 0.57s |
| `test_broker_learning_run.py` | 4 passed in 0.41s |
| `test_inventory_evidence.py` | 106 passed, 138 subtests passed in 5.90s |
| `test_inventory_signal.py` | 2 passed in 1.11s |
| `test_targeted_actor_observations.py` | 25 passed in 1.07s |
| `test_experiment_1f_gate_b.py` | 61 passed in 4.83s |
| `test_experiment_1f_phase2.py` | 105 passed, 5 warnings in 4.25s |
| `test_experiment_2a0_event_study.py` | 11 passed in 0.42s |
| `test_idx_calendar.py` | 40 passed in 0.06s |
| `test_neobdm_source_contract.py` | 564 passed in 4.61s |
| `test_corporate_action_findings.py` | 118 passed in 4.42s |
| `test_corporate_action_mutation_witnesses.py` | 4 passed in 0.89s |
| `test_corporate_action_callable_coverage.py` | 42 passed in 3.13s |
| `test_ml_health.py` | 16 passed in 2.06s |
| `test_pipeline.py` | 108 passed, 3 warnings in 4.57s |
| `test_broker_dashboard.py` | 49 passed in 0.66s |
| `test_targeted_actor_panel.py` | 35 passed in 2.57s |
| `test_morning.py` | 14 passed in 0.47s |
| `test_broker_collect.py` | 35 passed in 0.84s |
| `test_bandarmolony_trade_capture.py` | 156 passed, 188 subtests passed in 15.28s |
| `test_bandarmolony_trade_lock.py` | 14 passed, 3 warnings, 4 subtests passed in 6.56s |

The 1,822 pytest passes include 35 retained artifact-gated checks that return early because the optional inventory cache/candidate artifacts are absent in the isolated fixture. Those paths were not executed; their messages are listed below. They are not extra successful numerical validations. The formal Torch skips are the optional environment/CLI tests in `test_ddqn_canonical.py`. Five non-failing Phase2 numerical warnings, three pipeline convergence warnings and three lock-test warnings remain visible.

ML Health DEFAULT and QUICK were rerun from scratch against the same settled source, in a separate disposable snapshot. Both exited **0**, reporting **31 module imports and 859 standalone tests passed**. DEFAULT exercises the actual named model refusal and panel refusal, validates their consumer/status/contract fields, and produces structured `UNSUPPORTED` notes. QUICK exercises the same panel refusal and intentionally skips the model smoke test. Unexpected refusals and runtime/import/DB errors remain health failures. Torch is absent, so one health module is compile-checked instead of imported.

Pipeline: **108 passed**. Dashboard: **49 passed**. Actor panel: **35 passed**. Morning: **14 pytest tests plus five standalone quiet-status cases; all 19 standalone tests passed**. These are subsets/replays of the reported validation, not additional unique tests to add to 1,822.

## F01–F18 matrix

Every original finding was reproduced on the reviewed behavior during remediation. Every row below is CLOSED, with its catching regression passing on the settled tree. All named tests are in `test_corporate_action_findings.py`; additional direct-call regressions live in `test_corporate_action_callable_coverage.py`.

| Finding | Reproduced on old behavior | Result and correction | Catching regression | Passing now |
| --- | --- | --- | --- | --- |
| F01 | Yes | CLOSED — Invalid registry collections fail atomically | `test_f01_invalid_collection_is_not_an_empty_registry` | Yes |
| F02 | Yes | CLOSED — Confirmed evidence and reference text have strict identities/types | `test_f02_incomplete_provenance_cannot_authorize_reference` | Yes |
| F03 | Yes | CLOSED — Labels cannot inherit changed trust, registry, session axis or producer ownership | `test_f03_changed_trust_cannot_reseal_existing_labels` | Yes |
| F04 | Yes | CLOSED — Certificates bind values and reject duplicate replay | `test_f04_certificate_is_bound_to_observations_and_outputs` | Yes |
| F05 | Yes | CLOSED — Complete OHLC and full-bar admission are required | `test_f05_extrema_require_full_bar_reference_admission` | Yes |
| F06 | Yes | CLOSED — Writer checks all revised fields and stored successors before writes | `test_f06_predecessor_revision_revalidates_stored_successor_ohlc` | Yes |
| F07 | Yes | CLOSED — Unresolved event bars never become predecessors | `test_f07_pending_event_cannot_back_ordinary_session_or_labels` | Yes |
| F08 | Yes | CLOSED — UNKNOWN or contradictory source metadata cannot authorize an event | `test_f08_explicit_unknown_or_contradictory_source_metadata_refuses_event` | Yes |
| F09 | Yes | CLOSED — Entry, every payoff bar and decision indexes require admission | `test_f09_rejected_same_session_close_cannot_be_payoff` | Yes |
| F10 | Yes | CLOSED — Incomplete timed horizons are withheld; true earlier barriers remain supported | `test_f10_incomplete_timed_hold_is_not_shortened` | Yes |
| F11 | Yes | CLOSED — Direct analytical APIs and cached-result presentations refuse | `test_f11_direct_call_refuses_before_accessing_unversioned_input` | Yes |
| F12 | Yes | CLOSED — Research/weekly wrappers refuse before persistence and messages | `test_f12_weekly_inner_wrapper_does_not_commit_on_refusal` | Yes |
| F13 | Yes | CLOSED — Cleaner/writer/monitor share quarantine trust and retain independent defects | `test_f13_independent_duplicates_cannot_be_recovered_by_the_writer` | Yes |
| F14 | Yes | CLOSED — Independent duplicate observations cannot establish a baseline | `test_f14_direct_label_builder_does_not_chain_duplicate_identity` | Yes |
| F15 | Yes | CLOSED — Registry boundaries segment medians even when event rows are missing | `test_f15_missing_event_row_does_not_join_median_segments` | Yes |
| F16 | Yes | CLOSED — Integrity checks derive actual spans without trusting producer reasons | `test_f16_integrity_checks_actual_spans_with_blank_producer_reasons` | Yes |
| F17 | Yes | CLOSED — Refusal scope covers required trade windows, not unrelated history | `test_f17_unrelated_history_does_not_disable_valid_local_trade` | Yes |
| F18 | Yes | CLOSED — First complete extrema windows remain correctly aligned | `test_f18_first_complete_extrema_window_and_incomplete_tail` | Yes |

The matrix file has **118 passing parameter cases**. Extra checks cover changed source values/certificates, duplicate replay, malformed source evidence, explicit source/session/basis contradictions, producer ownership of derived fields, stale date indexes, invalid barriers, missing event rows, and independently bad successors after quarantine recovery. The pipeline regression `test_strategy_simulator_refuses_to_hold_across_a_clean_panel_gap` verifies immediate decision-to-entry calendar adjacency.

## Direct-call and refusal coverage

The final independent source search inspected **90 production modules and 1,429 definitions/methods**, plus previously traced workflow entry paths. It did not use the route registry as evidence of completeness. Financial publishers were distinguished from generic numerical/statistical operators, actual-source/cash ingestion, quantity/basis diagnostics, index-only analysis, schema/run-status records and raw chart scaling.

Actual module imports and entry calls produced **235 correctly named immediate function refusals, zero unexpected acceptances/errors**. The optional Torch module's exact entry definitions execute without Torch; separate tests cover `TickerEnv` reset/state/step and `QNet.forward`. All 235 function guards are the first executable action after the docstring and their pure guard import. Five CLI routes, the conditional analytical-table writer and class methods have separate coverage.

The final search caught and closed `paired_date_differences`, `daily_ic_capacity`, `primary_status`, cached paired-bootstrap/graduation/sensitivity publishers, and dashboard header/rule-chip publishers. The report module now imports without Telegram credentials; credentials are accessed only by an actual send operation. Five newly discovered presentation regressions failed before their guards and now pass. The direct-call suite has **42 passing cases**.

`broker_learning_db.insert_rows` refuses every analytical table before iterating rows, querying schema, connecting/writing/committing, including empty batches. `runs` keeps its separate status-only schema contract. Real disposable-DB tests verify unchanged write counters and zero `live_outcomes` rows after refusal. Weekly/research/CLI/veto and morning tests verify no output file/cache/manifest/persistence record/message before the actual named refusal. Expected unavailable morning analytics never fall back to a raw financial report or construct a credential-based sender.

Generic numerical helpers retain their math tests. Explicitly pinned reviewed-code fixtures retain the daily tagging/ranking/reason/presentation assertions, primary-status horizon selection, and GateB graduation/sensitivity assertions in separate in-memory test namespaces. They never patch production modules or certify v1 output. Other refusal regressions exercise the actual named API with representative unversioned inputs rather than claiming an unsupported analytical result.

## Semantic mutations

**Original mutants: 9/9 killed. F01–F18 adversarial mutations: 18/18 killed. Total: 27/27.**

Unchanged witnesses: **50 passing cases across 24 groups**. These replay suite tests and are not added to unique totals. Mutated executions produced **49 failing cases with genuine assertion evidence**; some parameter cases survive while the targeted mutation is killed by another case. Import/collection/syntax errors, skip-only witnesses and successful mutant executions never count as kills.

The original nine reference/TERP/pending/carry-forward/OO/full-span/zero-fill/tier/netval mutations remain. F09 removes both independently redundant entry and payoff guards to expose the unsafe payoff. F14 restores arbitrary last-record deduplication to manufacture a falsely unique predecessor; removal of just one mask remains blocked independently.

## Preserved semantics and scope

- ENRG reconstructed observed anchors remain Oct2 C1440 and Oct5 O1080/H1085/L1000/C1030/V109977800. Oct5 reference1065 is only a limit diagnostic, approximately **−3.2863849765%**. Oct2→Oct5 economic return is NaN/WITHHELD with `CORPORATE_ACTION_BOUNDARY`. Actual prices and actual-close broker netval are unchanged; the actual-price substitution mutant is killed.
- SINI July9 remains `PENDING_REFERENCE`, reference null. No theoretical7380 fallback or unresolved predecessor trust is admitted.
- RAJA remains mixed-basis history, with no inferred Aug25 event and no factor-five automatic repair.
- `Corporate_action_contract_impact_audit.md` is unchanged (SHA256 `a432d91a4477353bb779c519b57f4d3b201a9e4f62e9d74bc1f77ba80819b361`). `corporate_actions.json` is unchanged (SHA256 `fc273d921f7501d5cd99d9b571d1f90a860331e18b840aa7dbf0dc539007ddca`).
- Diff checks and parsing of all changed/new Python sources pass. No production DB, credential/token file, BandarmoloNY file, frozen accepted artifact, or unrelated feature change is included. The read-only historical health fixture hash is identical before and after both health runs. Only task-generated Python/pytest caches were removed.

## Retained artifact-gated checks not executed

- SKIP strict equivalence (inventory_raw/ cache not present)
- SKIP real-cache invariant (inventory_raw/ cache not present)
- SKIP no look-ahead on SINI (inventory_raw/ cache not present)
- skip unreadable cache: inventory_raw/ cache not present
- skip mostly empty: inventory_raw/ cache not present
- skip stale books: inventory_raw/ cache not present
- SKIP source_manifest (candidate snapshot not built here)
- SKIP frozen_artifacts (shared checkout not present)
- SKIP determinism (candidate snapshot not built here)
- SKIP validity_domains (candidate artifacts not built here)
- SKIP real_authorization (candidate artifacts not built here)
- SKIP sensitivity (candidate artifacts not built here)
- SKIP mode_artifacts (validity artifacts not built here)
- SKIP headline_counts (candidate artifacts not built here)
- SKIP date_mask (sensitivity artifact not built here)
- SKIP date_mask_nominal (sensitivity artifact not built here)
- SKIP manifest_separates_execution_from_provenance (candidate snapshot not built here)
- SKIP manifest_pins_code_identity_and_rejects_drift (candidate snapshot not built here)
- SKIP a_dirty_working_tree_blocks_establishment (candidate snapshot not built here)
- SKIP artifact_parentage_is_cryptographic_not_by_filename (candidate snapshot not built here)
- SKIP auth_identity (candidate artifacts not built here)
- SKIP the_manifest_is_not_established_by_building_it (candidate snapshot not built here)
- SKIP pit_observability (validity artifacts not built here)
- SKIP rename_neutral (candidate artifacts not built here)
- SKIP ohlc_snapshot (OHLC snapshot not built here)
- SKIP ohlc_full_market (OHLC snapshot not built here)
- SKIP establishment_requires_semantic_code_clean_not_whole_tree (candidate snapshot not built here)
- SKIP validity_reports_stay_derived_not_execution_inputs (candidate snapshot not built here)
- SKIP the_manifest_establishment_target_is_the_candidate_directory (candidate snapshot not built here)
- SKIP robustness (sensitivity artifact not built here)
- SKIP dirty_classification_is_fail_closed_not_py_only (candidate snapshot not built here)
- SKIP test_synthetic_manifest_matches_the_production_schema (real proposal absent)
- SKIP test_rupiah_round_trip_stays_far_inside_the_tolerance (candidate broker table absent)
- SKIP test_semantic_validation_does_not_depend_on_the_shared_checkout (candidate OHLC absent)
- SKIP test_real_297_has_no_partially_present_broker (snapshot absent)
