# Corporate-action contract verification

Verified on 2026-10-07 in the requested feature branch. The full required runner exited 0 after the final source and test edits.

## Branch and scope

- Branch: `feat/corporate-action-contract-pr83`.
- HEAD: `d7889749c2a6232e33c5ec3f82468eb593b269e6`.
- Sole parent/base: `5a2ec4181ad897e3d6455c5b7855e0d9830b1768`.
- The sole pre-existing branch difference over that base is the added `Corporate_action_contract_impact_audit.md`. Its complete A–Q specification was read before implementation.
- Audit SHA-256: `a432d91a4477353bb779c519b57f4d3b201a9e4f62e9d74bc1f77ba80819b361`; audit bytes still match HEAD.
- Implementation changes are uncommitted in this working tree. HEAD and its parent remain unchanged. Only the requested feature branch was fetched; no master substitute, merge or deployment was used.

`git diff --check` passed. Syntax parsing passed for all 72 changed/new Python files. Protected-path checks found no BandarmoloNY, database, parquet, frozen-pin, accepted-universe, or measured-basis-ledger changes. Tests used disposable databases; no production repair was performed.

## Required regressions

Command:

```sh
PYTHONPATH=/tmp/ca-test-deps python verify_corporate_action_contract.py --test-deps /tmp/ca-test-deps
```

The optional `--test-deps` directory contains environment test dependencies outside the repository. For a fresh environment, install `requirements-test.txt` and run `python verify_corporate_action_contract.py`. CI invokes that same runner.

Environment: Python 3.12.14, pandas 2.2.3, NumPy 2.3.5, pytest 9.1.1.

Result: **1,230 passed, 138 additional subtests passed, 2 optional Torch-dependent tests skipped; all 18 required suites exited 0.**

| Suite | Result |
| --- | --- |
| `test_price_contract.py` | 28 passed in 3.91s |
| `test_inventory_capture.py` | 56 passed in 1.46s |
| `test_walk_forward_canonical.py` | 56 passed in 6.74s |
| `test_ddqn_canonical.py` | 42 passed, 2 skipped in 3.14s |
| `test_daily_picks.py` | 60 passed in 0.12s |
| `test_arb_veto.py` | 10 passed in 0.97s |
| `test_broker_book.py` | 13 passed in 0.37s |
| `test_broker_rules.py` | 16 passed in 0.31s |
| `test_broker_learning.py` | 31 passed in 0.56s |
| `test_broker_learning_run.py` | 4 passed in 0.33s |
| `test_inventory_evidence.py` | 106 passed, 138 subtests passed in 4.88s |
| `test_inventory_signal.py` | 2 passed in 1.01s |
| `test_targeted_actor_observations.py` | 25 passed in 0.67s |
| `test_experiment_1f_gate_b.py` | 61 passed in 5.88s |
| `test_experiment_1f_phase2.py` | 105 passed, 5 warnings in 4.19s |
| `test_experiment_2a0_event_study.py` | 11 passed in 0.32s |
| `test_idx_calendar.py` | 40 passed in 0.03s |
| `test_neobdm_source_contract.py` | 564 passed in 4.81s |

The two skips are the Torch-import DDQN environment test and CLI manifest-argument test. Torch is absent in this environment. The real environment constructor body, `make_envs` and training refusal guards are executed without Torch in the contract suite; episode boundary/deleted-row tests also run. This verifies the current explicit-refusal behavior, not a migrated DDQN training/reward implementation. Five warnings in the retained Phase-2 suite are non-failing numerical warnings.

Tests cover the reconstructed ENRG actual anchors, official reference 1065, writer/audit agreement, exact existing bands and REAL normalization, unchanged-history dispositions, invalid OHLCV, corrupt/quarantined predecessors, calendar/session gaps and suspensions, scoped pending/revoked/conflicting records, date-only availability and enrollment, CC/gap/lag/extrema masks, OO exit events and independence from later closes, eligible event-open OC arithmetic, multiple/deleted boundaries, variable exits/TP/SL, DDQN cuts/refusal, source-session evidence, actual-close netval and the broker cutoff, RAJA negative controls, quarantine overlays, snapshot/registry identity, downstream refusal, and per-ticker integrity monitoring.

## Semantic mutations

All nine mutants were killed by assertion failures in disposable copies. The runner rejects import/collection failures as mutation evidence.

- `official-reference-replaced-with-cum-close`
- `theoretical-terp-substituted`
- `pending-falls-through`
- `reference-carried-to-next-day`
- `oo-exit-boundary-mask-removed`
- `intermediate-boundary-ignored`
- `withheld-return-replaced-with-zero`
- `tier-selected-from-cum-close`
- `actual-close-replaced-in-netval`

## Supported review state

The shared reference/admission/span contract and its core adapters are implemented. The registry certifies no complete historical action-free coverage; legacy representation defaults to UNKNOWN. Confirmed ENRG references remain one-session limit diagnostics. SINI remains pending with a null reference, and no RAJA event or automatic repair is inferred.

The initial implementation uses the explicit-refusal option authorized by section L: **111 functions in 47 downstream modules**, plus the `TickerEnv` constructor, refuse uncertified analytics before inputs, model fitting or persistence. The exact route ledger is `corporate_action_consumer_routes.json`. These routes remain unavailable for production until their source/session/basis/window/output contracts are migrated. Their previous-output test expectations now assert named refusal; unaffected source/flow/calendar and frozen evidence-validator tests remain active. A test-only v0 evidence fixture is pinned to the exact base source hash and cannot certify v1 economic output.

ENRG fixtures are reconstructed from the audited observed anchors, with synthetic surrounding context explicitly identified. They do not establish the missing original dated vendor response or certify every legacy source as raw. No frozen experiment digest, manifest or result was repinned.
