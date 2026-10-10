# Corporate Action Contract verification

## CA-R01 / CA-R02 final local validation

Recorded on 2026-10-10 (Asia/Jakarta), against required base
`00d56cfa1445ae44f2a6ea452257763b28a84eb8` on
`feat/corporate-action-contract-pr83`. This record supersedes the historical
validation totals below. The existing eight remediation edits were preserved
through the Desktop Commander interruption; implementation was not restarted.

CA-R01 normalizes Unicode NFKC and separator spelling in the shared whole-field
placeholder predicate. N-A, N/A, NA, N_A, N A, N–A, empty text and the other tested
placeholder spellings cannot authorize registry, adapter or frame provenance.
Eleven meaningful longer identifiers remain accepted across all five provenance
contexts. Degenerate hashes remain rejected, and the existing chronology,
date-only precision, ENRG official-reference, SINI pending-reference and RAJA
mixed-basis controls pass. The adapter and frame consumers already use the shared
predicate; no duplicate validation or historical price change was needed.

CA-R02 adds named UnsupportedPriceContract guards to exactly four unmigrated APIs:
foreign_flow_signal_backtest.trade_stats and .date_balanced_hit_edge, plus
pattern_type_backtest.trade_level_stats and .date_balanced_hit_edge. Each refuses
before input inspection, empty-input branches, cost adjustment or financial
computation. All four routes are in the capability ledger. Tests block database
and file access, verify unchanged cached inputs, and independently detect ledger
omissions. Anonymous numerical utilities remain available.

The RED witnesses ran on the exact required base before production edits:
22 CA-R01 failures and 12 CA-R02 failures. The latter cover each of the four APIs
with cached, empty and unreadable inputs. The cached ENRG witness spans
2026-10-02 to 2026-10-05 with gross return 1030 / 1440 - 1.
GREEN results are 182 CA-R01 cases and 17 CA-R02 cases: 12 direct refusals,
four ledger checks and one anonymous numerical control.

Two existing pipeline tests were updated to assert these named refusals while
retaining their pooled and group-balanced numerical assertions through supported
anonymous helpers. No original case was deleted. The production diff is limited
to the shared predicate, four API entry guards and four ledger routes; the other
changes are focused tests, five semantic mutants and this record. Trust restart,
source self-healing, ML lineage and the other frozen subsystems were not changed.

Counts overlap between groups and must not be summed as unique test cases.

| Gate | PASS | FAIL | SKIP | UNAVAILABLE | Exit |
| --- | ---: | ---: | ---: | ---: | --- |
| CA-R01 focused | 182 | 0 | 0 | 0 | 0 |
| CA-R02 focused | 17 | 0 | 0 | 0 | 0 |
| Existing F02/F08/F11 | 96 | 0 | 0 | 0 | 0 |
| Callable coverage | 64 | 0 | 0 | 0 | 0 |
| Cold CLI | 38 | 0 | 0 | 0 | 0 |
| Price contract / trust restart | 52 | 0 | 0 | 0 | 0 |
| F16 monitor | 10 | 0 | 0 | 0 | 0 |
| Full verifier, 34 suites | 2177 | 0 | 0 | 4 | 2: INCOMPLETE |
| Verifier subtests | 329 | 0 | 0 | 0 | included above |
| Full semantic mutation catalogue | 59 killed | 0 | 0 | 0 | included above |
| ML Health DEFAULT | 859 | 0 | 4 | 0 | 0 |
| ML Health QUICK | 859 | 0 | 4 | 0 | 0 |
| Full prepared regression, 40 modules | 2600 | 0 | 0 | 6 | incomplete environment coverage |
| Regression subtests | 329 | 0 | 0 | 0 | included above |
| Pipeline | 108 | 0 | 0 | 0 | included verifier execution |
| Signal-integrity rerun | 11 | 0 | 0 | 0 | 0 |
| Morning / sender-stub rerun | 17 | 0 | 0 | 0 | 0 |
| DDQN/canonical | 44 | 0 | 0 | 0 | included verifier execution |
| Final integrity | 1 | 0 | 0 | 0 | 0 |

The combined mutation gate killed **59/59**, including all previous
54 mutants, one placeholder-normalization mutant and four individual guard-removal
mutants. Survivors, baseline failures and unavailable mutants are all zero.
The five new mutants were killed individually before broader validation.

Saved results confirm the interrupted run completed all 40 permitted regression
modules, signal integrity and morning. Finalization collected those results
without repeating completed tests. The 40-module matrix reuses the 34 exact suite
executions from this candidate's verifier and executes the six remaining modules.
Original PID 14800 and its Desktop Commander session no longer exist; the parent
shell exit code was not recoverable. Individual gate exit codes, native result
records and completed logs supply the accounting above.

Both ML Health modes return no problems and no failed suites. Their four explicit
optional skips remain SKIP under native accounting. Morning retains structured
nonfinancial refusal/status, no uncertified picks, no stale financial fallback
and no fake-zero signal. Delivery tests use stubs. No live scraping, real Telegram
send or production database access was performed.

The earlier broader run exposed two stale pipeline expectations and two temporary
directory collection errors. The corrected tests and external fixed pytest root
resolved them; those initial failed results remain archived. A later Windows
path-option collection error is also archived. No accounting production code was
changed. During finalization, the external integrity reader needed BOM-aware log
decoding and explicit handling of the verifier's existing snapshot exclusions.
Original logs and both earlier reader versions are preserved; repository source
and snapshot policy were unchanged.

All 1363 original artifact hashes and both pinned read-only historical
database hashes remain unchanged. All 153 permitted root Python/JSON files
match both disposable validated snapshots byte for byte. The existing snapshot
policy excludes .mcp.json and test_bandarmolony_trade_mutations.py; both retain
their required-base Git identities. The separate BandarmoloNY mutation runner
is not claimed as executed by this Corporate Action gate.

Remaining unavailable checks are not counted as PASS:

- Verifier: one POSIX close/connection-lock witness and three os.fork ownership
  witnesses cannot run on Windows. The verifier remains INCOMPLETE, exit 2.
- Regression additionally records the symlink-sidecar witness's Windows
  privilege error 1314, and test_inventory_adds.py zero collection / exit 5.
  Inventory direct-call and cold-CLI refusals remain covered elsewhere.
- No current full Linux run, GitHub workflow or live production delivery is claimed.

The eight-file binary-capable patch was verified outside Windows Temp:
`C:\Users\jason\Documents\CodexBackups\CorporateActionContract\20261010T200840+0700\ca-r01-r02-00d56cf.patch`.
SHA256: `0e9babf6dc9bb1fbdca036c68127763824bb497cfad7770e4550d0e27f38a5ea`. Its reverse-apply check passes without applying
anything, and backup creation left the worktree unchanged.

Current evidence is under `%TEMP%\ca-high-remediation-00d56cf`:
base-witness.json, r01/r02 RED and GREEN logs, new-mutants.log,
validation/results.json and verifier.log, health/results.json and both native
health JSON files, and integrity.json. Prepared drivers retain the exact offline
commands and distinct pinned Corporate Action and audited-broker fixtures.
Original artifacts and the frozen prior worktree are preserved.

git diff --check passes. The diff outside the eight remediation files and this
verification record is empty. All mandatory locally available gates are green.
Status: **READY FOR INDEPENDENT DELTA REVIEW**.

## Historical Phase-5 context
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

## Historical 2026-10-09 harness validation

Final test/harness validation recorded on 2026-10-09 with Windows/Python 3.14.6
and pytest 9.1.1, based on candidate HEAD
`e92b340823b61465630d5a95a527d5a5c789170b`.
The follow-up changes only five test files and this verification record.
Production source, capability ledger, refusal guards, certification requirements,
trust/restart rules and morning financial behavior remain unchanged.

All 24 formerly failing cases now pass: 13 stale financial expectations,
10 oversized metadata setups and one pin-drift/cache dependency. Financial tests
assert the named UnsupportedPriceContract before financial work or persistent
writes, while retaining narrower raw/structural assertions. Metadata tests use
supported control-plane helpers and an inert, hash-pinned legacy proposal fixture;
they do not create a current financial execution proposal. Gate-B caches only raw
inputs/daily rows. Bad-pin verification is independent of feature initialization,
test order and an empty or populated raw cache. No original test case was deleted.

Counts are per group; groups overlap and must not be summed into a unique total.

| Group | PASS | FAIL | SKIP | UNAVAILABLE | Exit |
| --- | ---: | ---: | ---: | ---: | --- |
| Former 24 failures, run first | 24 | 0 | 0 | 0 | 0 |
| Contract verifier: 34 suites, primary cases | 1,978 | 0 | 0 | 4 | 2: INCOMPLETE |
| Contract verifier: subtests | 329 | 0 | 0 | 0 | included above |
| Full semantic mutation catalogue | 54 killed | 0 | 0 | 0 | included above |
| ML Health DEFAULT | 859 | 0 | 4 | 0 | 0 |
| ML Health QUICK | 859 | 0 | 4 | 0 | 0 |
| Full regression: 40 modules, primary cases | 2,401 | 0 | 0 | 6 | incomplete environment coverage |
| Full regression: subtests | 329 | 0 | 0 | 0 | included above |
| Pipeline | 108 | 0 | 0 | 0 | 0 |
| Signal-integrity witnesses | 11 | 0 | 0 | 0 | 0 |
| Morning pytest/stubbed operational route | 17 | 0 | 0 | 0 | 0 |
| Corporate-action callable coverage | 47 | 0 | 0 | 0 | 0 |
| Cold CLI | 38 | 0 | 0 | 0 | 0 |
| DDQN/canonical | 44 | 0 | 0 | 0 | 0 |

The full verifier executed all 54 mutations: **54 killed / 54 total; 0 survived;
0 baseline failures; 0 unavailable**. There were 114 passing unchanged cases in
50 witness groups and 101 expected assertion failures under mutation. No
import/collection failure was counted as a kill. The 40-module regression records
reuse the 34 exact suite executions from this current verifier and execute all
six remaining modules; they do not reuse results from the prior candidate run.

Both ML Health modes have no problems or failed suites. Counts above come from
their native returned accounting, including four explicit skips; a generic pytest
log parser is not used to reclassify those skips. The optional-artifact policy does
not override the contract verifier's incomplete mandatory evidence.

All authoritative artifact gates are now available and pass, including raw caches,
candidate/Gate-B snapshots, historical databases, pipeline and pinned broker
Parquet. All 1,363 original artifact file hashes and both pinned historical
database hashes remained unchanged. The 153 root Python/JSON source/configuration
files match the validated disposable source snapshot; the 148 files outside the
five edited tests also match the previous validation snapshot byte for byte.
Production-semantic diff excluding the five tests and this record is empty.
Morning checks retain typed nonfinancial refusal/status, no uncertified picks,
no stale or fake-zero fallback, and stubbed delivery only. No live Telegram send,
production database access or BandarmoloNY data change was performed.

Remaining UNAVAILABLE checks are recorded honestly:

- Verifier: one POSIX close/connection-lock witness in test_bandarmolony_trade_capture.py
  and three real os.fork ownership witnesses in test_bandarmolony_trade_lock.py.
  These cannot execute on Windows. The verifier therefore remains **INCOMPLETE,
  exit 2** despite zero failures.
- Full regression additionally includes the symlink-sidecar witness in
  test_broker_flow_manifest_refresh.py (Windows privilege error 1314), and
  test_inventory_adds.py (no pytest cases, exit 5). Zero collection is UNAVAILABLE,
  never PASS; direct inventory model/reader/cold-CLI refusals remain covered
  elsewhere by passing tests and killed mutants.
- Prior Linux witnesses are historical evidence, not reruns of this remediation.
  A full Linux workflow and live delivery remain unverified.

Current result and log roots are `%TEMP%\\ca-harness-final-e92b3408` and
`%TEMP%\\ca-harness-health-e92b3408`. Each contains results.json; the first also
contains focused-24.log, verifier.log, per-module regression logs and integrity.json.
The second retains both native health accounting JSON files and logs.
All locally available gates are green. Status: **READY FOR INDEPENDENT DELTA REVIEW**.

## Historical reproduction and remaining local evidence

The final harness drivers retain the exact commands, pinned source locations and
disposable snapshot setup. From the preserved validation environment, the primary
driver modes are `focused`, `verifier` and `regression`; health uses the separate
snapshot and native-accounting observer. The full mutation gate is included in the
ordinary verifier command, so a duplicate mutation run is unnecessary.

```powershell
$harnessRoot = Join-Path $env:TEMP 'ca-harness-final-e92b3408'
$healthRoot = Join-Path $env:TEMP 'ca-harness-health-e92b3408'
py "$harnessRoot\\validate.py" focused
py "$harnessRoot\\validate.py" verifier
py "$harnessRoot\\validate.py" regression
py "$healthRoot\\validate.py" health
```

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

Historical Phase-5 results are in the temporary ca-phase5 directory: `matrix.json`,
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

These inputs were available for the final harness validation:
`inventory_raw/`, `backtest_out/experiment_1f_candidate/`,
`backtest_out/experiment_1f_candidate/gate_b_inputs/` and shared `ohlc.parquet` /
`broker_daily.parquet` under the reviewed `NEOBDM_SHARED_ROOT`.
Hash-verified copies populated disposable source checkouts. Original artifacts
were preserved; no production input was fetched to remove UNAVAILABLE.
The verifier does not implicitly copy ignored caches from the live worktree.
In an isolated checkout populated only with these reviewed inputs, run:

```powershell
py -m pytest -q -p no:cacheprovider -p corporate_action_validation test_broker_book.py test_broker_rules.py test_broker_learning_run.py test_experiment_1f_gate_b.py test_experiment_1f_phase2.py test_pipeline.py
```

The additional audited broker export needs BROKER_DAILY_PARQUET set to the reviewed
file with SHA256 `c8d1948f00d99ba96fe17376292f32a9cda2be36e2eb5ce303e680427f05cc32`.
The Windows symlink witness needs an authorized symlink privilege or Linux. The pinned Parquet witness passed; the symlink witness remains unavailable.
These are the exact offline witnesses:

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
