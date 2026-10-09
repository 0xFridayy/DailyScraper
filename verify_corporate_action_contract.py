"""Offline regressions and semantic mutations in disposable source checkouts.

Only the explicitly identified read-only historical SQLite fixture is copied.
Production databases, credentials and the BandarmoloNY data tree are never opened.
"""

import argparse
import json
from hashlib import sha256
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile

from corporate_action_mutations import FINDING_MUTANTS, RESTART_MUTANTS, PHASE5_MUTANTS
from corporate_action_validation import KEYS, evidence_status, validation_counts

ROOT = Path(__file__).parent
SUITES = [
    "test_price_contract.py", "test_inventory_capture.py", "test_walk_forward_canonical.py",
    "test_ddqn_canonical.py", "test_daily_picks.py", "test_arb_veto.py", "test_broker_book.py",
    "test_broker_rules.py", "test_broker_learning.py", "test_broker_learning_run.py",
    "test_inventory_evidence.py", "test_inventory_signal.py", "test_targeted_actor_observations.py",
    "test_experiment_1f_gate_b.py", "test_experiment_1f_phase2.py", "test_experiment_2a0_event_study.py",
    "test_idx_calendar.py", "test_neobdm_source_contract.py",
    "test_corporate_action_findings.py", "test_corporate_action_restart.py", "test_corporate_action_monitor.py",
    "test_corporate_action_cold_cli.py", "test_corporate_action_validation.py",
    "test_corporate_action_morning_status.py",
    "test_corporate_action_mutation_witnesses.py",
    "test_corporate_action_callable_coverage.py", "test_ml_health.py",
    "test_pipeline.py", "test_broker_dashboard.py", "test_targeted_actor_panel.py",
    "test_morning.py", "test_broker_collect.py", "test_bandarmolony_trade_capture.py",
    "test_bandarmolony_trade_lock.py",
]
HISTORICAL_DB_SHA256 = "6fc475e6db6be597a539a8cc30f6a0c44f05a5c14b263367c07a3aa417389be5"
MUTANTS = [
    ("official-reference-replaced-with-cum-close", "price_contract.py",
     'event.reference, "OFFICIAL_CORPORATE_ACTION_REFERENCE"',
     '(previous_actual.price if previous_actual else event.reference), "OFFICIAL_CORPORATE_ACTION_REFERENCE"',
     "test_official_reference_is_scoped_and_never_an_economic_return"),
    ("theoretical-terp-substituted", "corporate_actions.json", '"reference_price": "1065"',
     '"reference_price": "1063.333333"', "test_official_reference_is_scoped_and_never_an_economic_return"),
    ("pending-falls-through", "price_contract.py", 'if event.status != "CONFIRMED_REFERENCE":',
     'if event.status == "REVOKED":', "test_pending_revoked_conflicts_and_invalid_registry_fail_closed"),
    ("reference-carried-to-next-day", "price_contract.py", 'e.session == session', 'e.session <= session',
     "test_official_reference_is_scoped_and_never_an_economic_return"),
    ("oo-exit-boundary-mask-removed", "price_audit.py", '.where(oo_admitted & oo_span)', '.where(oo_admitted)',
     "test_observed_oo_exit_cc_gap_lag_and_oc_are_independent"),
    ("intermediate-boundary-ignored", "price_contract.py", 'if crossed:', 'if False:',
     "test_full_holding_phases_and_deleted_event_row"),
    ("withheld-return-replaced-with-zero", "price_audit.py", '.where(valid)', '.where(valid, 0.0)',
     "test_observed_oo_exit_cc_gap_lag_and_oc_are_independent"),
    ("tier-selected-from-cum-close", "price_contract.py", 'ara_bound(reference.price) + TOL',
     'ara_bound(reference.previous_actual.price if reference.previous_actual else reference.price) + TOL',
     "test_tier_uses_resolved_reference_and_not_cum_close"),
    ("actual-close-replaced-in-netval", "backfill_inventory.py", '(series[i] * 100 * close) / 1e9',
     '(series[i] * 100 * 1065) / 1e9', "test_writer_audit_parity_domain_refusals_and_actual_netval"),
]


def execute(command, env, cwd=ROOT):
    result = subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode, result.stdout


def permitted_snapshot_file(path):
    """Never read protected files even while preparing an isolated checkout."""
    name = str(path).lower()
    if path == Path(".gitignore") or path.parts[:2] == (".github", "workflows"):
        return True  # Static workflow/ignore contracts are required regression inputs.
    synthetic_trade_modules = {"test_bandarmolony_trade_capture.py", "test_bandarmolony_trade_lock.py", "bandarmolony_trade_contract.py",
                               "bandarmolony_trade_capture.py"}
    return (("bandarmolony" not in name or len(path.parts) == 1 and name in synthetic_trade_modules)
            and not any(part.startswith(".") for part in path.parts)
            and not any(word in name for word in ("credentials", "credential", ".env"))
            and not re.search(r"\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal))?$", name))


def prepare_snapshot(folder, env, fixture_root=None):
    case = Path(folder)
    status, output = execute(["git", "clone", "--shared", "--no-checkout", "--quiet",
                              str(ROOT), str(case)], env)
    if status:
        raise RuntimeError(f"Local verification checkout failed: {output}")
    status, output = execute(["git", "read-tree", "HEAD"], env, case)
    if status:
        raise RuntimeError(f"Local verification index failed: {output}")
    status, output = execute(["git", "ls-files", "-z"], env)
    if status:
        raise RuntimeError(f"Cannot enumerate verification source: {output}")
    files = {Path(name) for name in output.split("\0") if name}
    files.update(path.relative_to(ROOT) for path in ROOT.glob("*.py"))
    for relative in sorted(files):
        if not permitted_snapshot_file(relative):
            continue
        source = ROOT / relative
        if not source.is_file() or source.is_symlink():
            continue
        destination = case / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    if fixture_root is not None:
        fixture = Path(fixture_root).resolve() / "neobdm.db"
        if (not fixture.is_relative_to(Path(tempfile.gettempdir()).resolve())
                or not fixture.is_file() or fixture.is_symlink()
                or fixture.stat().st_mode & 0o222):
            raise RuntimeError("Full verification needs an explicit read-only historical fixture under the system temporary directory")
        if sha256(fixture.read_bytes()).hexdigest() != HISTORICAL_DB_SHA256:
            raise RuntimeError("Historical database fixture does not match the reviewed committed snapshot")
        shutil.copyfile(fixture, case / "neobdm.db")
        (case / "neobdm.db").chmod(0o444)
    return case


def pytest_command(target, *, show_output=False):
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-p", "corporate_action_validation", "-rs", target]



def semantic_assertion_failure(status, output):
    """A collection/import failure never counts as evidence against a mutant."""
    return (status == 1 and "failed" in output
            and re.search(r"(?m)^E\s+(?:AssertionError\b|assert\b|Failed: DID NOT RAISE\b)", output)
            and "ERROR collecting" not in output and "ImportError" not in output
            and "ModuleNotFoundError" not in output and "SyntaxError" not in output)


def verify_mutants(snapshot, env, selected=None):
    original = [(name, file, old, new, f"test_price_contract.py::{test}")
                for name, file, old, new, test in MUTANTS]
    mutations = original + FINDING_MUTANTS + RESTART_MUTANTS + PHASE5_MUTANTS
    if selected:
        mutations = [mutation for mutation in mutations if any(
            mutation[0].startswith(prefix) for prefix in selected)]
        if not mutations:
            raise RuntimeError("No semantic mutants match the requested selection")
    witnesses = {}
    killed, survived, unavailable = [], [], []
    baseline_failures = []
    unchanged_cases = failing_mutant_cases = failing_mutant_subcases = 0
    for mutation in mutations:
        name, filename, old, new, target = mutation[:5]
        additional = mutation[5] if len(mutation) > 5 else ()
        if target not in witnesses:
            status, output = execute(pytest_command(target), env, snapshot)
            counts = validation_counts(status, output)
            witnesses[target] = evidence_status(counts)
            unchanged_cases += counts["passed"]
            if witnesses[target] != "PASS":
                print(f"UNCHANGED WITNESS {witnesses[target]}: {target}\n{output}", flush=True)
                if witnesses[target] == "FAIL":
                    baseline_failures.append(target)
        if witnesses[target] != "PASS":
            unavailable.append(name)
            continue
        with tempfile.TemporaryDirectory(prefix="ca-mutant-") as folder:
            case = Path(folder)
            for file in snapshot.iterdir():
                if file.is_file() and file.suffix in {".py", ".json"}:
                    shutil.copyfile(file, case / file.name)
            path = case / filename
            source = path.read_text(encoding="utf-8")
            finding_mutant = name.startswith(("F", "R", "P5"))
            for before, after in ((old, new), *additional):
                occurrences = source.count(before)
                if not occurrences or finding_mutant and occurrences != 1:
                    unavailable.append(name)
                    print(f"MUTANT UNAVAILABLE: {name}; source matches: {occurrences}", flush=True)
                    break
                source = source.replace(before, after, 1 if finding_mutant else occurrences)
            else:
                path.write_text(source, encoding="utf-8")
                mutant_env = dict(env)
                mutant_env["PYTHONPATH"] = os.pathsep.join([str(case), env.get("PYTHONPATH", "")])
                status, output = execute(pytest_command(target), mutant_env, case)
                counts = validation_counts(status, output)
                if semantic_assertion_failure(status, output):
                    killed.append(name)
                    failing_mutant_cases += counts["failed"]
                    failing_mutant_subcases += counts.get("subtests", {}).get("failed", 0)
                    print(f"MUTANT KILLED: {name}", flush=True)
                elif evidence_status(counts) == "PASS":
                    survived.append(name)
                    print(f"MUTANT SURVIVED: {name}\n{output}", flush=True)
                else:
                    unavailable.append(name)
                    print(f"MUTANT UNAVAILABLE: {name}\n{output}", flush=True)
    print(f"Semantic mutants: total: {len(mutations)}; killed: {len(killed)}; "
          f"survived: {len(survived)}; unavailable: {len(unavailable)}; "
          f"unchanged witnesses: {unchanged_cases} cases in {len(witnesses)} groups; "
          f"mutated cases failed: {failing_mutant_cases}; "
          f"mutated subtests failed: {failing_mutant_subcases}", flush=True)
    print("MUTATION_RESULTS: " + json.dumps({"total": len(mutations), "killed": killed,
          "survived": survived, "unavailable": unavailable,
          "baseline_failures": baseline_failures, "unchanged_cases": unchanged_cases,
          "witness_groups": len(witnesses), "mutated_cases_failed": failing_mutant_cases,
          "mutated_subtests_failed": failing_mutant_subcases}, sort_keys=True), flush=True)
    return 1 if survived or baseline_failures else 2 if unavailable else 0



def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--test-deps", help="Optional directory containing installed test dependencies")
    parser.add_argument("--fixture-root", default=os.environ.get(
        "CA_CONTRACT_FIXTURE_ROOT", "/tmp/ca-independent-review"),
        help="Directory with the reviewed read-only historical neobdm.db fixture")
    parser.add_argument("--mutants-only", action="store_true", help="Run unchanged witnesses and semantic mutants only")
    parser.add_argument("--mutant", action="append", help="Select mutant name prefixes for focused verification")
    args = parser.parse_args()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory(prefix="ca-verification-") as folder:
        try:
            snapshot = prepare_snapshot(folder, env, None if args.mutants_only else args.fixture_root)
        except RuntimeError as exc:
            print(f"VERIFICATION UNAVAILABLE: {exc}; no PASS claim", flush=True)
            return 2
        paths = [str(snapshot)] + ([args.test_deps] if args.test_deps else [])
        paths.extend(path for path in env.get("PYTHONPATH", "").split(os.pathsep)
                     if path and Path(path).resolve() != ROOT.resolve())
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["MPLCONFIGDIR"] = str(snapshot / "mpl-config")
        totals = dict.fromkeys(KEYS, 0)
        totals["subtests"] = dict.fromkeys(KEYS, 0)
        suite_results = []
        if not args.mutants_only:
            for suite in SUITES:
                status, output = execute(pytest_command(suite), env, snapshot)
                counts = validation_counts(status, output)
                result = evidence_status(counts)
                suite_results.append({"suite": suite, "status": result, **counts})
                for key in KEYS:
                    totals[key] += counts[key]
                    totals["subtests"][key] += counts.get("subtests", {}).get(key, 0)
                print(f"{suite}: {result}; {json.dumps(counts, sort_keys=True)}", flush=True)
                if result == "FAIL":
                    print(output, flush=True)
            print("REGRESSION_RESULTS: " + json.dumps({"counts": totals, "suites": suite_results},
                                                     sort_keys=True), flush=True)
        mutation_status = verify_mutants(snapshot, env, args.mutant)
        failed = totals["failed"] or any(r["status"] == "FAIL" for r in suite_results)
        incomplete = totals["unavailable"] or any(r["status"] == "UNAVAILABLE" for r in suite_results)
        if failed or mutation_status == 1:
            print("VERIFICATION FAIL; see failed evidence above.", flush=True)
            return 1
        if incomplete or mutation_status == 2:
            print("VERIFICATION INCOMPLETE: mandatory evidence UNAVAILABLE; no PASS claim.", flush=True)
            return 2
    print("Selected semantic mutations passed." if args.mutants_only else
          "Required regressions and semantic mutations passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
