"""Offline regressions and semantic mutations in disposable source checkouts.

Only the explicitly identified read-only historical SQLite fixture is copied.
Production databases, credentials and the BandarmoloNY data tree are never opened.
"""

import argparse
from hashlib import sha256
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile

from corporate_action_mutations import FINDING_MUTANTS, RESTART_MUTANTS

ROOT = Path(__file__).parent
SUITES = [
    "test_price_contract.py", "test_inventory_capture.py", "test_walk_forward_canonical.py",
    "test_ddqn_canonical.py", "test_daily_picks.py", "test_arb_veto.py", "test_broker_book.py",
    "test_broker_rules.py", "test_broker_learning.py", "test_broker_learning_run.py",
    "test_inventory_evidence.py", "test_inventory_signal.py", "test_targeted_actor_observations.py",
    "test_experiment_1f_gate_b.py", "test_experiment_1f_phase2.py", "test_experiment_2a0_event_study.py",
    "test_idx_calendar.py", "test_neobdm_source_contract.py",
    "test_corporate_action_findings.py", "test_corporate_action_restart.py",
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
    result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
            raise RuntimeError("Full verification needs an explicit read-only historical fixture under /tmp")
        if sha256(fixture.read_bytes()).hexdigest() != HISTORICAL_DB_SHA256:
            raise RuntimeError("Historical database fixture does not match the reviewed committed snapshot")
        shutil.copyfile(fixture, case / "neobdm.db")
        (case / "neobdm.db").chmod(0o444)
    return case


def pytest_command(target, *, show_output=False):
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-rs", *(["-s"] if show_output else []), target]


def semantic_assertion_failure(status, output):
    """A collection/import failure never counts as evidence against a mutant."""
    return (status == 1 and "failed" in output
            and re.search(r"(?m)^E\s+(?:AssertionError\b|assert\b|Failed: DID NOT RAISE\b)", output)
            and "ERROR collecting" not in output and "ImportError" not in output
            and "ModuleNotFoundError" not in output and "SyntaxError" not in output)


def verify_mutants(snapshot, env, selected=None):
    original = [(name, file, old, new, f"test_price_contract.py::{test}")
                for name, file, old, new, test in MUTANTS]
    mutations = original + FINDING_MUTANTS + RESTART_MUTANTS
    if selected:
        mutations = [mutation for mutation in mutations if any(
            mutation[0].startswith(prefix) for prefix in selected)]
        if not mutations:
            raise RuntimeError("No semantic mutants match the requested selection")
    checked_witnesses = set()
    killed = 0
    unchanged_cases = 0
    failing_mutant_cases = 0
    for mutation in mutations:
        name, filename, old, new, target = mutation[:5]
        additional = mutation[5] if len(mutation) > 5 else ()
        if target not in checked_witnesses:
            status, output = execute(pytest_command(target), env, snapshot)
            if status:
                print(f"UNCHANGED WITNESS FAILED: {target}\n{output}", flush=True)
                return 1
            baseline_summary = output.strip().splitlines()[-1] if output.strip() else ""
            passing = re.search(r"\b(\d+) passed\b", baseline_summary)
            if not passing:
                print(f"UNCHANGED WITNESS DID NOT EXECUTE: {target}\n{output}", flush=True)
                return 1
            unchanged_cases += int(passing.group(1))
            checked_witnesses.add(target)
        with tempfile.TemporaryDirectory(prefix="ca-mutant-") as folder:
            case = Path(folder)
            for file in snapshot.iterdir():
                if file.is_file() and file.suffix in {".py", ".json"}:
                    shutil.copyfile(file, case / file.name)
            path = case / filename
            source = path.read_text()
            finding_mutant = name.startswith(("F", "R"))  # exactly one source match
            for before, after in ((old, new), *additional):
                occurrences = source.count(before)
                if not occurrences or finding_mutant and occurrences != 1:
                    raise RuntimeError(f"mutant {name} requires one source match, found {occurrences}")
                source = source.replace(before, after, 1 if finding_mutant else occurrences)
            path.write_text(source)
            mutant_env = dict(env)
            mutant_env["PYTHONPATH"] = os.pathsep.join([str(case), env["PYTHONPATH"]])
            status, output = execute(pytest_command(target), mutant_env, case)
            if not semantic_assertion_failure(status, output):
                print(f"MUTANT NOT SEMANTICALLY KILLED: {name}\n{output}", flush=True)
                return 1
            killed += 1
            mutant_summary = output.strip().splitlines()[-1]
            failed = re.search(r"\b(\d+) failed\b", mutant_summary)
            failing_mutant_cases += int(failed.group(1)) if failed else 0
            print(f"MUTANT KILLED: {name} ({mutant_summary})", flush=True)
    print(f"Semantic mutants killed: {killed}/{len(mutations)}; unchanged witnesses passed: "
          f"{unchanged_cases} cases in {len(checked_witnesses)} groups; "
          f"mutated pytest cases failed: {failing_mutant_cases}",
          flush=True)
    return 0


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
    with tempfile.TemporaryDirectory(prefix="ca-verification-") as folder:
        snapshot = prepare_snapshot(folder, env, None if args.mutants_only else args.fixture_root)
        paths = [str(snapshot)] + ([args.test_deps] if args.test_deps else [])
        paths.extend(path for path in env.get("PYTHONPATH", "").split(os.pathsep)
                     if path and Path(path).resolve() != ROOT.resolve())
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["MPLCONFIGDIR"] = str(snapshot / "mpl-config")
        if not args.mutants_only:
            for suite in SUITES:
                status, output = execute(pytest_command(suite, show_output=True), env, snapshot)
                print(f"{suite}: {output.strip().splitlines()[-1] if output.strip() else status}", flush=True)
                # Some retained standalone suites report optional artifacts by
                # returning early. Expose those messages instead of hiding them.
                for line in output.splitlines():
                    if "SKIP " in line or re.search(r"\bskip .*:", line):
                        print(line.strip(), flush=True)
                if status:
                    print(output)
                    return 1
        if verify_mutants(snapshot, env, args.mutant):
            return 1
    print("Required regressions and semantic mutations passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
