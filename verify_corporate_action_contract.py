"""Offline regression and semantic mutation runner. Never fetches or writes DBs."""

import argparse
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).parent
SUITES = [
    "test_price_contract.py", "test_inventory_capture.py", "test_walk_forward_canonical.py",
    "test_ddqn_canonical.py", "test_daily_picks.py", "test_arb_veto.py", "test_broker_book.py",
    "test_broker_rules.py", "test_broker_learning.py", "test_broker_learning_run.py",
    "test_inventory_evidence.py", "test_inventory_signal.py", "test_targeted_actor_observations.py",
    "test_experiment_1f_gate_b.py", "test_experiment_1f_phase2.py", "test_experiment_2a0_event_study.py",
    "test_idx_calendar.py", "test_neobdm_source_contract.py",
]
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


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--test-deps", help="Optional directory containing installed test dependencies")
    args = parser.parse_args()
    env = dict(os.environ)
    paths = [str(ROOT)] + ([args.test_deps] if args.test_deps else [])
    env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for suite in SUITES:
        status, output = execute([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", suite], env)
        print(f"{suite}: {output.strip().splitlines()[-1] if output.strip() else status}", flush=True)
        if status:
            print(output)
            return 1
    for name, filename, old, new, test in MUTANTS:
        with tempfile.TemporaryDirectory(prefix="ca-mutant-") as folder:
            case = Path(folder)
            for file in ["price_contract.py", "price_contract_frame.py", "price_audit.py", "idx_calendar.py",
                         "corporate_actions.json", "backfill_inventory.py", "test_price_contract.py"]:
                shutil.copyfile(ROOT / file, case / file)
            path = case / filename
            original = path.read_text()
            if old not in original:
                raise RuntimeError(f"mutant {name} has no matching source")
            path.write_text(original.replace(old, new))
            mutant_env = dict(env)
            mutant_env["PYTHONPATH"] = os.pathsep.join([str(case), env["PYTHONPATH"]])
            status, output = execute([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                      f"test_price_contract.py::{test}"], mutant_env, case)
            if status != 1 or "AssertionError" not in output:
                print(f"MUTANT NOT SEMANTICALLY KILLED: {name}\n{output}")
                return 1
            print(f"MUTANT KILLED: {name}", flush=True)
    print("Required regressions and semantic mutations passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
