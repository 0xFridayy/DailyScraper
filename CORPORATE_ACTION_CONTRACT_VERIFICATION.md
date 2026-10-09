# Corporate Action Contract verification — Phase 5

Scope: 0xFridayy/DailyScraper, remediation base
`5619f0852d4bfcdb94b6397b4c216a626f9028de`, preserved previous HEAD
`32015ebbd10678719383182250514780ffa289ad`.
The four commits 26c382d, 084f2df, d10c727 and 32015eb remain intact.
Phase 5 changes verification accounting, operational morning reporting and regression
witnesses. It also closes the remaining `test_inventory_adds.wf`, `.main` and cold
CLI refusal gaps. It does not migrate refused financial analytics or change
production data.

## Trust and source semantics

A close becomes trusted through an admitted official action reference, an admitted
step from a trusted immediate-session predecessor, or the final session of a clean
10-session restart window. The first supplied row never gains positional trust.
A missing exchange session never bridges to an older observed close. An old
quarantine outside the required local window does not disable a current clean trade.

Series-break context requires 10 preceding rows in the same supported segment.
Restart and predecessor-consistency checks have bounded dependencies; the witnesses
compare complete history with slices and older-history changes. Removing context
can withhold trust, and cannot add it. Writer, cleaner, simulator and monitor use
the same adjudication rules.

An admitted official exchange reference anchors limit checks on its effective
session. It never replaces the observed actual price for economic payoff, cost or
netval. A theoretical TERP or the previous cum-action close cannot substitute for
that official reference. Known and pending event boundaries withhold economic
returns across the affected holding windows.

UNKNOWN basis permits ordinary-price diagnostics and source capture only. Matching
prices cannot establish raw-actual representation, authorize an action exception or
certify a financial return. Event boundaries remain barriers to Option A returns.
Cached labels and outputs require current value-bound identity. Direct unsupported
analytical calls and cold CLIs refuse before producing persistent artifacts.

The monitor rebuilds admission from stored OHLCV and quarantine evidence. It checks
immediate sessions, full paths, quarantined anchors, extrema and numerical values;
producer flags, blank withholding reasons and resealed forged labels do not suffice.

Placeholder identities such as UNKNOWN, N/A and TBD, empty or degenerate hashes,
and contradictory observation/verification/enrollment/publication chronology cannot
authorize action or source evidence. Date-only evidence retains its declared
precision. Repairing stored NULL OHLCV fields is a change and must validate both its
own step and affected successors.

Known limitation: a coherent wrong run of 10 or more sessions can regain diagnostic
trust if no source contradiction, quarantine, domain defect or discontinuity is
visible. Restart does not prove economic correctness or source representation.
Resolving that limitation requires new external evidence and remains outside scope.

## Operational eligibility on the preserved fixture

Read-only export of Git blob `532a3cfc58779101a63327e7da4ebdeeafd99f7c`;
SHA256 `6fc475e6db6be597a539a8cc30f6a0c44f05a5c14b263367c07a3aa417389be5`.
The working databases were not opened. This is historical evidence, not a live
production freshness check.

- 12,346 price rows; 45 total tickers; 6,895 trusted rows in 2026.
- Latest global session: 2026-10-05, with 44 tickers present.
- Latest row per ticker: 45 rows; 42 diagnostically eligible; one unresolved;
  two pending restart; zero in the exclusive refused, action-pending, quarantine,
  source-basis or other categories.
- Source-basis unverified: all 45 latest rows. This overlaps the diagnostic
  categories. Certified financial analytics eligible: zero.
- ENRG, 2026-10-05: INADMISSIBLE / UNRESOLVED_EVENT_REFERENCE;
  reference reason UNKNOWN_REPRESENTATION.
- SINI, 2026-10-05: RESTART_PENDING, 2 of 10 sessions;
  reference reason UNTRUSTED_PREDECESSOR.
- TEBE, 2026-09-30: RESTART_PENDING, 7 of 10 sessions;
  reference reason UNTRUSTED_PREDECESSOR; absent on the latest global session.

A read-only writer-validation witness proposed flat synthetic bars on 2026-10-06:
45 accepted, zero refused. Dispositions were 42 ORDINARY_DIAGNOSTIC_ONLY,
two SOURCE_CAPTURE_UNADJUDICATED and one RESTART_PENDING. It performed no database
writes and makes no claim about actual October 6 observations. Historical defects
therefore do not block all top-ups; capture acceptance does not certify analytics.

## Morning reporting

`.github/workflows/daily-scrape.yml` invokes `morning.py`, which runs the scraper
and calls `daily_picks.run_morning`. The real picks route currently refuses with its
named UNSUPPORTED contract identity. Morning emits that structured refusal plus a
nonfinancial operational report through the existing scraper sender. It reports
REPORT_CAPTURED or NO_REPORT_CAPTURED and data health UNVERIFIED.

Unexpected contract refusals, analytical exceptions and delivery failures also
produce operational status. Held raw financial reports never replace unavailable
analytics. Supported stale-warning retry and recording behavior remains covered.
The refused route never constructs the daily_picks credential-based sender or
records a stale pick warning. All delivery tests use stubs; live Telegram delivery
was not tested or sent.

## Evidence accounting

The verifier distinguishes PASS, FAIL, SKIP and UNAVAILABLE. Printed artifact gates,
silent bare early returns and mandatory formal skips withdraw their cases from PASS.
Returns after expected validation exceptions remain PASS; returns for missing files
or imports remain UNAVAILABLE. Explicit optional missing-Torch skips remain SKIP.
Zero-execution mandatory suites cannot pass. Subtests are counted separately, and
missing mandatory subtest evidence blocks success. A failed subtest does not
manufacture an additional failed parent.

Exit 0 means completed requested evidence; exit 1 means real failure or a surviving
semantic mutant; exit 2 means incomplete mandatory evidence. Missing fixture setup
returns UNAVAILABLE. Mutations report every killed, surviving or unavailable mutant
and baseline failure; import/collection errors never count as semantic kills.
Windows child output and source interpretation use UTF-8.

The ML Health checks retain their standalone accounting and expected named refusal
probes. Informal `test_name skipped (reason)` lines are counted as UNAVAILABLE and
deduplicated against repeated lines and explicit summary skip descriptors.
Their optional-artifact policy is narrower than the contract verifier's mandatory
evidence policy; an ML Health OK message alone cannot establish
a complete contract-verification PASS.

## Validation

Validation recorded on 2026-10-09 with Windows/Python 3.14.6 and pytest 9.1.1.
Counts are per group; groups overlap and must not be summed into a unique total.

| Group | PASS | FAIL | SKIP | UNAVAILABLE |
| --- | ---: | ---: | ---: | ---: |
| Focused catching regressions | 25 | 0 | 0 | 0 |
| Focused broker fixture/accounting cases | 5 | 0 | 0 | 0 |
| Contract verifier: 34 suites, primary cases | 1,933 | 0 | 0 | 49 |
| Contract verifier: subtests | 329 | 0 | 0 | 0 |
| ML Health DEFAULT: tests | 823 | 0 | 4 | 36 |
| ML Health QUICK: tests | 823 | 0 | 4 | 36 |
| Full available regression: 40 suites, primary cases | 2,355 | 0 | 0 | 52 |
| Full available regression: subtests | 329 | 0 | 0 | 0 |
| Signal-integrity witnesses | 11 | 0 | 0 | 0 |
| Morning pytest/stubbed operational route | 17 | 0 | 0 | 0 |
| Morning standalone, including five QUIET outcomes | 19 | 0 | 0 | 0 |
| Pipeline | 107 | 0 | 0 | 1 |
| Linux accounting and POSIX witnesses | 18 | 0 | 0 | 0 |

Combined semantic mutations: **54 killed / 54 total; 0 survived;
0 baseline failures; 0 unavailable**. Exact survivor list: empty.
There were 114 passing unchanged cases in 50 witness groups and 101 expected
assertion failures under mutations; no import/collection failure was counted as
a kill. The three newly added inventory-comparison mutants were also individually
killed, with three passing baselines.

The verifier exited **2: INCOMPLETE**, because mandatory evidence is unavailable.
Both ML Health modes exited 0 under their explicit optional-artifact policy,
imported 32 modules and recorded their expected named unsupported routes rather
than producing analytics. Their optional UNAVAILABLE counts remain in the table.

The recovered DEFAULT/QUICK runs and 11 unaffected completed full-suite groups were
retained. The current full verifier and every unfinished/affected regression group
ran after reconnection. The original matrix failure is preserved in
matrix-recovered.json and the original broker-flow log, with the correct-fixture
rerun recorded separately. No final real failure or mutant survivor remains.

Unavailable primary-case accounting:

- Contract verifier: 45 missing artifact checks and four Windows POSIX checks.
  The four POSIX witnesses separately passed on Linux.
- Full regression: the same 49, one additional pinned broker-daily Parquet check,
  one Windows symlink-privilege check, and one zero-collection legacy utility.
- test_inventory_adds.py has no pytest cases: its exit 5 is UNAVAILABLE, not PASS.
  Its direct model, reader and cold CLI refusals have three passing regression
  cases in callable coverage and three killed mutants.
- Pipeline: one missing candidate-artifact witness; never counted as PASS.

Code and available evidence are settled. Status: **READY FOR LOCAL FINAL VALIDATION**.
Mandatory artifact/environment evidence remains incomplete, so this is not a
complete independent delta-review claim.

The Phase 5 regression additions were demonstrated failing before their fixes.
The 25 distinct focused catching cases pass on Windows. The three additional
inventory-comparison witnesses failed before their guards and now pass; each guard
also has a killed mutant. Five focused broker-manifest cases pass against their
own audited fixture. All 20 ML Health tests also pass, including the new informal-skip
accounting regression. The duplicate-date
witness now proves withdrawal of an otherwise admitted successor. The recovery
monitor witness pins its fixture clock; the DDQN CLI witness requires the current
named refusal and zero persistent artifacts. The invariant suites exercise all
13 requested areas: restart, old quarantine, contaminated starts, slice monotonicity,
+80% audit and monitor detection, missing sessions, bounded history, provenance,
chronology, independent label validation, NULL repair, direct refusals and cold CLIs.

## Reproduction and remaining local evidence

Run from the preserved worktree with Python 3.14.6 / pytest 9.1.1 on Windows.
Set PYTHONUTF8=1, PYTHONIOENCODING=utf-8 and PYTHONDONTWRITEBYTECODE=1.
CA_HISTORICAL_FIXTURE must name the explicitly reviewed read-only temporary export.
Never substitute a working or production database.

```powershell
$fixtureRoot = Join-Path $env:TEMP 'ca-phase5'
$env:CA_HISTORICAL_FIXTURE = Join-Path $fixtureRoot 'neobdm.db'
py verify_corporate_action_contract.py --fixture-root $fixtureRoot
py verify_corporate_action_contract.py --mutants-only
```

The recorded matrix is in the temporary ca-phase5 directory: `matrix.json`,
`matrix-recovered.json`, `resume_matrix.py`, `resume-*.log`, the recovered ML Health
and full-suite logs, `fixture_report.py` and `eligibility.json`.
`py $fixtureRoot\resume_matrix.py matrix` runs the current verifier and resumes
unfinished/affected groups in disposable source snapshots with placeholder
credentials. It preserves recovered results for unchanged completed groups and
sends no real Telegram.

The broker-flow manifest suite uses its own historical audited fixture from Git
commit `1aeca5313819e4843ce5cab210d0030dd8f58a78`, blob
`d7798904fd3293cb8105c4d1b4030caf683e426e`, file SHA256
`fd1eef8600a1a26a9888d02481cf44fe20e791a8960d611ffd73c618f000eaa0`.
Its 232,493 broker rows reproduce the contract's ordered hash and show no manifest
drift. The initial full-matrix manifest assertion failed because the runner paired
that older manifest with the separate Corporate Action fixture. The failure remains
in the recovered log; the rerun uses the correct pinned input. This validates the
historical manifest, without claiming that a production database still matches it.
Three audited-refusal cases retain all their assertions and express their optional
future-row check without a completed-test early return, so accounting counts them
as executed evidence rather than UNAVAILABLE.

Missing inputs are `inventory_raw/`, `backtest_out/experiment_1f_candidate/`,
`backtest_out/experiment_1f_candidate/gate_b_inputs/` and shared `ohlc.parquet` /
`broker_daily.parquet` under the reviewed `NEOBDM_SHARED_ROOT`. Supply them from
preserved, verified read-only evidence in an isolated checkout before repeating
artifact-dependent checks. The verifier does not implicitly copy ignored caches
from the live worktree. They must not be fetched from production merely to remove
UNAVAILABLE. In an isolated checkout populated only with reviewed read-only inputs,
run the artifact-dependent suites:

```powershell
py -m pytest -q -p no:cacheprovider -p corporate_action_validation test_broker_book.py test_broker_rules.py test_broker_learning_run.py test_experiment_1f_gate_b.py test_experiment_1f_phase2.py test_pipeline.py
```

The additional audited broker export needs BROKER_DAILY_PARQUET set to the reviewed
file with SHA256 `c8d1948f00d99ba96fe17376292f32a9cda2be36e2eb5ce303e680427f05cc32`.
The Windows symlink witness needs an authorized symlink privilege or Linux. Repeat
these exact offline witnesses once those inputs/capabilities are available:

```powershell
py -m pytest -q -p no:cacheprovider -p corporate_action_validation test_broker_flow_regime.py::test_exact_audited_baseline_reproduces_the_committed_manifest_byte_for_byte
py -m pytest -q -p no:cacheprovider -p corporate_action_validation test_broker_flow_manifest_refresh.py::test_the_sidecars_of_a_symlinked_source_are_protected
```

Docker checks require a running Docker Desktop Linux engine; `docker version`
currently reports that its named pipe is absent. The 14 accounting regressions
passed on Linux/Python 3.12.14. The four POSIX preflight/fork checks unavailable on
Windows also passed on Linux with PyArrow 25.0.1, using unchanged synthetic root
modules and tests whose Git blob identities match the Windows checkout. No
BandarmoloNY source or data tree was modified.

A full Linux equivalent still needs a valid Git checkout at the final Phase 5
commit, installed `requirements.txt` plus pytest, and the same hash-verified
read-only fixture under `/tmp/ca-phase5`. Run:

```sh
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 \
  python3.12 verify_corporate_action_contract.py --fixture-root /tmp/ca-phase5
python3.12 -m pytest -q -p no:cacheprovider -p corporate_action_validation \
  test_corporate_action_validation.py \
  test_bandarmolony_trade_capture.py::TradeCaptureTests::test_read_only_preflight_retains_same_process_writer_lock \
  test_bandarmolony_trade_lock.py::RawRootLockTests::test_fork_while_idle_refuses_inherited_store \
  test_bandarmolony_trade_lock.py::RawRootLockTests::test_fork_while_lock_held_refuses_inherited_store \
  test_bandarmolony_trade_lock.py::RawRootLockTests::test_fresh_store_in_forked_child_acquires_real_coordination
```

GitHub's full Ubuntu/Python 3.12 job and live production delivery remain unverified;
no workflow was dispatched. Repeat only offline/stubbed checks during final local
validation; `morning.py` and the scheduled workflow are live operational entrypoints.
