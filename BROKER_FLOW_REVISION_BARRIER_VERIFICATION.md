# Broker-flow historical revision barrier (M2)

Repository: `0xFridayy/DailyScraper`. Required base:
`cf8daf5fa4551f6788e0d7b76766338b710ef8bb`.
Branch: `feat/broker-flow-revision-barrier`.
The supplied workspace was already a clean, isolated Git worktree at that exact
base. Repository remote, HEAD, merge-base and worktree isolation were verified.
Other Corporate Action, price-revision and independent review worktrees were
not modified. NEW_HEAD is the single commit containing this report, reported
in the final response rather than embedding a self-referential commit hash.

## RED before production changes, GREEN after

Before editing production code, 51 new offline tests ran against the exact base:
**41 assertion failures, 10 passes, 0 skips, 0 unavailable**. An initial sandbox
temporary-directory setup error was corrected before recording this RED run;
setup errors are not financial evidence. Base writer blob:
`e8b3510dfac7e8bbc838b5d25c1dbde7d2fd9141`; base delta-verifier blob:
`1c078f0296f5d7c61eaf8fee94b3d92fa7563097`.

The pre-change synthetic VKTR/AI witness on 2026-04-23 accepted
`-0.15582 -> -0.31164`, with identical prices, one SQL change, and a successful
price-only database gate. The fixed writer refuses before any SQL mutation,
and preserves both tables including rowids after the caller catches and commits.

The independently pinned historical fixture was also available, read-only,
with SHA256 `6fc475e6db6be597a539a8cc30f6a0c44f05a5c14b263367c07a3aa417389be5`.
Only explicitly identified disposable copies were opened by the validation.
It contains 12,346 price rows and 235,716 broker rows. The proposed response was
reconstructed from its stored observations, not represented as an archived raw
API response. A later exact-base rerun and fixed-writer rerun used separate copies:

| Pinned VKTR/AI, 2026-04-23 | Exact base | Fixed writer |
|---|---:|---:|
| Identical close | 980 | 980 |
| Stored / proposed lots, reconstructed | -1590 / -3180 | -1590 / -3180 |
| Stored / proposed netval | -0.15582 / -0.31164 | -0.15582 / -0.31164 |
| Accepted | Yes | No |
| Persisted netval | -0.31164 | -0.15582 |
| SQL changes | 1 | 0 |
| Both financial tables and rowids unchanged | No | Yes |
| Gate on the resulting database | PASS (bypass) | PASS (no mutation) |

The separate direct-SQL bypass tests require the fixed gate to return failure
even after a successful writer invocation and with every price unchanged.
Structured hashes and validation results are in
`evidence/broker_flow_revision_barrier_validation.json`.

## Finite policy and five-field protection

An observation is keyed by `(date, ticker, broker_code)`. The writer stages all
proposals and compares them with stored observations before mutating either
financial table. Price admissibility, source representation, daily limits,
Corporate Action references and prior acceptance confer no broker authority.

| Field | Changed non-NULL | Non-NULL to NULL | NULL to value | Independent gate |
|---|---|---|---|---|
| bval | REFUSE | REFUSE | REFUSE without verified repair | All transitions checked |
| sval | REFUSE | REFUSE | REFUSE without verified repair | All transitions checked |
| netval | REFUSE | REFUSE | REFUSE without verified repair | All transitions checked |
| bavg | REFUSE | REFUSE | REFUSE without verified repair | All transitions checked |
| savg | REFUSE | REFUSE | REFUSE without verified repair | All transitions checked |

This path has no explicit verified broker correction or NULL-repair
authorization. None was added. A NULL-only proposed fill is classified
`UNVERIFIED_BROKER_NULL_REPAIR`. Other financial changes are classified
`UNAUTHORIZED_HISTORICAL_REVISION`. Refusals carry the observation key and
financial differences, never the authenticated response.

The real backfill proposes lot-derived netval and NULL for the other four fields;
it cannot erase values written under another source/unit convention. Five-field
preflight tests additionally exercise individual changed values, erasures,
repairs and unchanged NULLs; gate tests independently change each field in SQL.

New observations retain the existing broker allowlist, source/date coverage and
`BACKFILL_END` contracts. Exact repeats are removed from SQL write batches;
rowids, contents and `total_changes` stay unchanged, with INSERT/UPDATE/DELETE
triggers proving that a replay attempts no financial mutation. Missing broker
codes, sessions, short lot series and NULL lots create no zero observations.
Explicit observed zero is a real value, protected on later replay. Nonfinite
derived netval is refused. Existing admitted price NULL repairs remain allowed
when the broker observations are identical or absent.

Mandatory regression witnesses (all PASS in the focused runs):

| Required scenario | Test witness |
|---|---|
| Identical-price changed-lot VKTR revision | `test_vktr_identical_price_changed_lots_refused` |
| Netval-only change | `test_writer_protects_every_stored_financial_field[netval]` |
| Individual bval/sval/bavg/savg changes | `test_broker_preflight_five_field_policy` and `test_writer_protects_every_stored_financial_field` |
| Non-NULL erasure | `test_broker_preflight_five_field_policy` and `test_broker_gate_catches_all_five_fields_without_price_changes` |
| Partial broker/session coverage | `test_one_broker_one_or_multiple_sessions_refused` |
| Absent broker versus explicit observed zero | `test_absent_observation_is_preserved_never_zero` and `test_explicit_zero_is_an_observation_not_absence` |
| New valid broker/session | `test_new_broker_and_session_insert_exact_replay_has_no_sql_mutation` |
| Exact replay, contents and rowids | `test_new_broker_and_session_insert_exact_replay_has_no_sql_mutation` |
| Existing NULL handling | `test_broker_null_repair_requires_existing_authority`, `test_broker_preflight_five_field_policy` and `test_price_null_repair_with_identical_brokers_still_allowed` |
| Concurrent/repeated calls | `test_repeated_concurrent_calls_cannot_authorize_a_revision` |
| Partial price/broker write failure | `test_failure_after_partial_mutation_caller_catches_and_commits` |
| Refusal caught followed by caller commit | `test_refusal_precedes_all_writes_and_preserves_caller_transaction` |
| Outer transaction preserved | `test_refusal_precedes_all_writes_and_preserves_caller_transaction` and `test_successful_ticker_waits_for_caller_commit` |
| Autocommit rollback | `test_broker_failure_rolls_back_prices_and_earlier_broker_writes` and `test_failure_after_partial_mutation_caller_catches_and_commits` |
| Workflow gate bypass attempt | `test_independent_gate_blocks_commit_after_successful_writer` |

Except for the explicitly named price/transaction regressions in
`test_price_history_revision_barrier.py`, these are in
`test_broker_flow_revision_barrier.py`. All rejected-writer cases compare complete
financial rows and rowids; preflight refusals also require zero SQL changes.

## Atomicity and workflow gate

The existing ticker savepoint and caller transaction ownership are retained.
Tests cover repeated/concurrent connections, ordinary and autocommit mode,
pending caller work, caller catch-and-commit, injected failures after partial
price/broker writes, savepoint-release failure and successful writes waiting for
caller commit. Rejected revisions leave neither financial table changed.

`price_history_revision.py` now independently joins baseline and candidate
`broker_flow` on all three key columns, while preserving the OHLCV comparison.
It detects deletions and every financial difference, including missing-field
repairs. Both databases must have intact required tables and observation keys.
The SQLite backup receives an exclusive, read-only JSON receipt recording the
candidate path and baseline SHA256; missing, substituted, altered or reused
baselines fail closed. This receipt detects accidental substitution/tampering;
it does not authenticate malicious forgery of both local files.

The existing workflow snapshots before the writer and verifies before its
commit/push step. Static and behavioral witnesses require successful writer,
contamination gate and financial revision gate. No schedule, permissions,
activation state or workflow command was changed; only explanatory comments
and the gate step's display name changed. The workflow was never dispatched.

## Validation and semantic mutations

Validation results are finalized in the accompanying JSON and matrix below.
The full verifier distinguishes PASS, FAIL, SKIP and UNAVAILABLE; missing private
artifacts and platform restrictions are never counted as executed assertions.
The original 85 mutation definitions are preserved. M2 adds 22 semantic mutations:
comparison removal, each ignored field in writer and gate, erasure/repair
permission, absent-as-zero, identical-price bypass, gate removal/suppression,
deletion blindness, wrong baseline acceptance and partial-write leakage.

The prior price-refusal witness now also runs without broker proposals so broker
protection cannot conceal a disabled price barrier. A prior test that expected
a broker UPDATE trigger during an unauthorized refresh now requires the earlier
broker refusal and retains its unchanged-financial-snapshot assertion. New
injected INSERT failure cases retain proof of rollback after partial writes.

## Scope and release gates

The Corporate Action registry and provenance implementation are unchanged.
ENRG's official reference remains 1065, SINI's reference remains pending, RAJA's
mixed-basis history remains unresolved, and VKTR historical price revisions and
the existing operational-only morning refusal remain protected by regression
assertions. No Source Self-Healing, ML Lineage, SPECTRA, Sentinel or Market
Intelligence implementation was changed.

M1 remains a separate release gate: one refused ticker prevents the aggregate
scheduled commit. L1 remains a separate release gate: malformed/empty ticker
responses can leave the aggregate run green. No aggregate-run policy was changed.

No production database was accessed or modified. No live source, authenticated
request, real Telegram delivery, remote branch change, deployment or workflow
activation occurred. VKTR database history was not restored. This work is
limited to M2 and its evidence.

Changed files:

- `backfill_inventory.py`
- `price_history_revision.py`
- `.github/workflows/price-history-topup.yml`
- `test_broker_flow_revision_barrier.py`
- `test_price_history_revision_barrier.py`
- `test_corporate_action_validation.py`
- `corporate_action_mutations.py`
- `verify_corporate_action_contract.py`
- `BROKER_FLOW_REVISION_BARRIER_VERIFICATION.md`
- `evidence/broker_flow_revision_barrier_validation.json`

`git diff --check` passed before the single local commit. Git status and
the resulting commit identity are reported after committing. Disposable databases
and raw execution logs remain under ignored `scratchpad/broker-revision/`.

## Recorded validation matrix

| Check | PASS | FAIL | SKIP | UNAVAILABLE |
|---|---:|---:|---:|---:|
| Focused writer, price, inventory and accounting tests | 191 | 0 | 0 | 0 |
| test_broker_flow_revision_barrier.py | 85 | 0 | 0 | 0 |
| test_price_history_revision_barrier.py | 36 | 0 | 0 | 0 |
| test_price_contract.py | 29 | 0 | 0 | 0 |
| test_inventory_capture.py | 56 | 0 | 0 | 0 |
| test_walk_forward_canonical.py | 56 | 0 | 0 | 0 |
| test_ddqn_canonical.py | 44 | 0 | 0 | 0 |
| test_daily_picks.py | 60 | 0 | 0 | 0 |
| test_arb_veto.py | 10 | 0 | 0 | 0 |
| test_broker_book.py | 12 | 0 | 0 | 2 |
| test_broker_rules.py | 15 | 0 | 0 | 1 |
| test_broker_learning.py | 31 | 0 | 0 | 0 |
| test_broker_learning_run.py | 1 | 0 | 0 | 3 |
| test_inventory_evidence.py | 106 | 0 | 0 | 0 |
| test_inventory_signal.py | 2 | 0 | 0 | 0 |
| test_targeted_actor_observations.py | 25 | 0 | 0 | 0 |
| test_experiment_1f_gate_b.py | 52 | 0 | 0 | 9 |
| test_experiment_1f_phase2.py | 77 | 0 | 0 | 28 |
| test_experiment_2a0_event_study.py | 11 | 0 | 0 | 0 |
| test_idx_calendar.py | 40 | 0 | 0 | 0 |
| test_neobdm_source_contract.py | 564 | 0 | 0 | 0 |
| test_corporate_action_findings.py | 1206 | 0 | 0 | 0 |
| test_corporate_action_restart.py | 23 | 0 | 0 | 0 |
| test_corporate_action_monitor.py | 10 | 0 | 0 | 0 |
| test_corporate_action_cold_cli.py | 38 | 0 | 0 | 0 |
| test_corporate_action_validation.py | 14 | 0 | 0 | 0 |
| test_corporate_action_morning_status.py | 3 | 0 | 0 | 0 |
| test_corporate_action_mutation_witnesses.py | 4 | 0 | 0 | 0 |
| test_corporate_action_callable_coverage.py | 68 | 0 | 0 | 0 |
| test_ml_health.py | 20 | 0 | 0 | 0 |
| test_pipeline.py | 107 | 0 | 0 | 1 |
| test_broker_dashboard.py | 49 | 0 | 0 | 0 |
| test_targeted_actor_panel.py | 35 | 0 | 0 | 0 |
| test_morning.py | 14 | 0 | 0 | 0 |
| test_broker_collect.py | 35 | 0 | 0 | 0 |
| test_bandarmolony_trade_capture.py | 155 | 0 | 0 | 1 |
| test_bandarmolony_trade_lock.py | 11 | 0 | 0 | 3 |
| test_broker_flow_canonical.py | 145 | 0 | 0 | 0 |
| test_broker_flow_regime.py | 77 | 0 | 0 | 1 |
| test_broker_flow_manifest_refresh.py | 118 | 0 | 0 | 1 |
| Full verifier primary cases | 3104 | 0 | 0 | 48 |
| Full verifier subtests (separate accounting) | 329 | 0 | 0 | 0 |
| Original semantic mutants killed | 85 | 0 | 0 | 0 |
| New M2 semantic mutants killed | 22 | 0 | 0 | 0 |

Counts from focused runs overlap the full verifier and are not additive.
The extra broker suites have 340 passes and two unavailable checks.
The full verifier exits 2 (INCOMPLETE), solely for explicitly unavailable
private/platform evidence. There is no complete-verifier PASS claim.
All 107 semantic mutants were killed by assertion failures after passing
unchanged witnesses; none survived or was unavailable. Syntax/import/setup
errors were not counted as kills.

The raw successful inventory API response is unavailable. Both pinned
SQLite fixtures are available and hash-verified. The VKTR reconstruction
witnesses are identified as such. Missing raw caches, experiment 1f
candidate/gate_b artifacts and matching broker parquet remain unavailable.
Windows POSIX-fork and symlink-privilege cases remain unavailable.

Verdict: **READY FOR INDEPENDENT DELTA REVIEW** for M2. Broader mandatory
artifact/platform evidence and M1/L1 remain separate release gates.

Reproduce with Python 3.14.6 / pytest 9.1.1:
```powershell
python -m pytest -q -p no:cacheprovider -p corporate_action_validation test_broker_flow_revision_barrier.py test_price_history_revision_barrier.py test_inventory_capture.py test_corporate_action_validation.py
python verify_corporate_action_contract.py --fixture-root <reviewed-read-only-temporary-fixture-directory>
python verify_corporate_action_contract.py --mutants-only --mutant M2
```
The additional broker suites use the separate audited Git database blob
`d7798904fd3293cb8105c4d1b4030caf683e426e`, SHA256
`fd1eef8600a1a26a9888d02481cf44fe20e791a8960d611ffd73c618f000eaa0`.
Do not substitute the Corporate Action fixture for that older broker fixture.
Run only disposable source snapshots with placeholder credentials.
