"""Reproduce SIG1 counterexamples and remove each guard in isolated processes.

Run: python verify_sig1_corrective.py --before-dir scratchpad/sig1_corrective
No implementation file or production database is changed. JSON goes to stdout;
--out may save JSON evidence. The pre-correction files are preserved local copies
of the five implementation files supplied at the start of this corrective pass.
"""

import argparse
import ast
from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).absolute().parent
AS_OF = date(2026, 10, 14)
MUTATIONS = {
    "fixed_universe_removed": ("signal_integrity_health", "required = universe.required(session)",
        'required = set(accepted.loc[accepted.date == sorted(accepted.date.unique())[-2], "ticker"])',
        "test_high1_persistent_dropout_refused"),
    "quarantine_filter_removed": ("signal_integrity_health", "accepted = load_clean(conn)",
        "accepted = load(conn)", "test_high2_quarantined_latest_session_refused"),
    "coverage_refusal_removed": ("signal_integrity_health", "if missing:", "if False:",
        "test_high3_partial_topup_full_health_and_cli_refuse"),
    "historical_calendar_removed": ("signal_integrity_health", "if not is_idx_session(session):",
        "if False:", "test_medium1_historical_non_session_rejected"),
    "numeric_source_contract_removed": ("check_signal_integrity", None, None,
        "test_medium2_numeric_contract_without_type_baseline"),
    "numeric_storage_guard_removed": ("signal_integrity_health", None, None,
        "test_numeric_looking_sqlite_text_is_not_a_numeric_price"),
    "unknown_exception_status_guard_removed": ("sig1_coverage_contract", "self.verified is not True",
        "not self.verified", "test_unknown_exception_evidence_does_not_waive_dropout[unknown_status]"),
    "type_drift_removed": ("check_signal_integrity", "if before and now - before:", "if False:",
        "test_type_drift_guard_has_an_isolated_witness"),
    "constant_collapse_removed": ("check_signal_integrity", "if rows >= MIN_ROWS_FOR_COLLAPSE:", "if False:",
        "test_constant_field_collapse_guard_has_an_isolated_witness"),
    "missing_signal_close_threshold_removed": ("check_signal_integrity", "MAX_UNMEASURABLE_SIGNALS = 0.30",
        "MAX_UNMEASURABLE_SIGNALS = 1.00", "test_missing_signal_close_threshold_has_an_isolated_witness"),
    "comparison_evidence_guard_removed": ("check_signal_integrity",
        '_finding(stats, "CROSS_SOURCE_UNVERIFIED", detail)', "pass",
        "test_insufficient_comparison_evidence_never_healthy"),
    "report_admission_removed": ("run_ml_reports", "admission = report_admission(conn)",
        'admission = {"datasets": {"price_history": {"usable_latest": "2026-10-13", "expected": "2026-10-13"}}}' ,
        "test_orchestrator_refuses_before_every_report_side_effect[quarantine]"),
    "report_snapshot_removed": ("run_ml_reports", "with reporting_snapshot(DB_PATH) as (conn, snapshot_path):",
        "with unverified_source(DB_PATH) as (conn, snapshot_path):",
        "test_orchestrator_consumes_the_admitted_frozen_file_even_if_source_changes"),
    "n1_historical_short_circuit": ("signal_integrity_health", "    required_sessions = [expected]",
        '    if unsupported: return dict(result, freshness="CALENDAR_UNSUPPORTED")\n    required_sessions = [expected]',
        "test_n1_historical_calendar_does_not_hide_current_coverage"),
    "m1_empty_columns_lost": ("price_audit", "return px.loc[pd.Series(keep, index=px.index, dtype=bool)].reset_index(drop=True)",
        "return px[keep].reset_index(drop=True)", "test_m1_schema_empty_and_quarantine_are_separate_failures[empty-EMPTY]"),
    "m2_required_collision_check_removed": ("signal_integrity_health", 'result["collisions"] = price_collisions(conn, required_sessions)',
        'result["collisions"] = []', "test_m2_clone_with_stale_quarantine_shared_evidence[2026-10-13]"),
    "m2_equality_claimed_proven": ("signal_integrity_health", '"CORRUPTION_PRESENT" if conflicts else "REVIEW_REQUIRED"',
        '"CORRUPTION_PRESENT"', "test_m2_unproven_whole_bar_equality_requires_review[0]"),
    "m3_freshness_refusal_removed": ("signal_integrity_health", 'if item["freshness"] != "CURRENT":',
        'if False:', "test_m3_stale_inputs_stop_orchestrator_before_side_effects"),
    "m3_signal_family_check_removed": ("signal_integrity_health", '"SOURCE_UNAVAILABLE" if bad else "CURRENT"',
        '"CURRENT"', "test_m3_unavailable_konglo_family_is_never_a_zero_signal_day[SOURCE_UNAVAILABLE]"),
    "m4_ci_discovery_removed": ("check_ml_health", '"-m", "pytest", "-q"',
        '"-q"', "test_m4_health_runner_collects_sig1_with_pytest"),
}


def mutation_worker(name, temp):
    # Recompile in memory. Every worker gets a fresh interpreter, and the
    # originals on disk are never changed, even if the test fails or crashes.
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.setdefault(key, "synthetic")
    import check_signal_integrity as csi
    import signal_integrity_health as health
    import run_ml_reports as reports
    import sig1_coverage_contract as contract
    import price_audit
    import check_ml_health
    modules = {"check_signal_integrity": csi, "signal_integrity_health": health,
               "run_ml_reports": reports, "sig1_coverage_contract": contract,
               "price_audit": price_audit, "check_ml_health": check_ml_health}
    module_name, old, new, witness = MUTATIONS[name]
    module = modules[module_name]
    if name == "numeric_source_contract_removed":
        csi.check_numeric_source_contract = lambda *args: None
    else:
        source = Path(module.__file__).read_text(encoding="utf-8")
        if name == "numeric_storage_guard_removed":
            tree = ast.parse(source)
            nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "bad_storage" for t in n.targets)]
            assert len(nodes) == 1
            nodes[0].value = ast.Constant(0)
            code = compile(ast.fix_missing_locations(tree), module.__file__, "exec")
        else:
            assert old in source, f"mutation target disappeared: {name}"
            code = compile(source.replace(old, new), module.__file__, "exec")
        exec(code, module.__dict__)
        if name == "report_snapshot_removed":
            from contextlib import contextmanager

            @contextmanager
            def unverified_source(path):
                conn = health.open_readonly(path)
                try:
                    yield conn, path
                finally:
                    conn.close()
            reports.unverified_source = unverified_source
        csi.table_freshness = health.table_freshness
        csi.inspect_price_rows = health.inspect_price_rows
        csi.price_collisions = health.price_collisions
        csi.report_admission = health.report_admission
        csi.AdmissionRefused = health.AdmissionRefused
        if module_name == "signal_integrity_health":
            reports.report_admission = health.report_admission
            reports.AdmissionRefused = health.AdmissionRefused
    import pytest
    return pytest.main([f"test_sig1_corrective.py::{witness}", "-q", "--tb=short",
                        f"--basetemp={temp}"])


def counterexamples(monitor, health, *, repaired):
    from test_sig1_corrective import current_fixture, SYNTHETIC_UNIVERSE
    from test_signal_integrity import fixture, add_price

    def gate(conn):
        try:
            if repaired:
                health.require_current_prices(conn, AS_OF, SYNTHETIC_UNIVERSE)
            else:
                health.require_current_prices(conn, AS_OF)
            return dict(admitted=True)
        except RuntimeError as exc:
            return dict(admitted=False, reason=str(exc))

    def row(name, conn):
        kwargs = dict(universe=SYNTHETIC_UNIVERSE) if repaired else {}
        problems, _, stats = monitor.check(conn, AS_OF, **kwargs)
        return dict(case=name, report_gate=gate(conn), health_exit=monitor.exit_code(problems, stats),
                    categories=sorted({f["category"] for f in stats.get("findings", [])}))

    conn = current_fixture()
    conn.execute("DELETE FROM price_history WHERE ticker='T015' AND date>'2026-10-09'")
    rows = [row("HIGH-1 persistent T015 dropout", conn)]
    conn = current_fixture()
    conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history WHERE date='2026-10-13'")
    rows.append(row("HIGH-2 all October 13 observations quarantined", conn))
    conn = fixture(price_date="2026-10-09")
    add_price(conn, "2026-10-13", "T000")
    rows.append(row("HIGH-3 fifteen stale tickers, one current", conn))
    conn = current_fixture()
    add_price(conn, "2026-08-17", "ZZZZ", close=300, volume=3000)
    problems = []
    monitor.check_value_sanity(conn, problems)
    rows.append(dict(case="MEDIUM-1 historical IDX holiday", target_refused=bool(problems), problems=problems))
    conn = fixture()
    conn.execute("UPDATE market_summary_daily SET m_dn_0='text'")
    problems = []
    monitor.check_schema_and_coverage(conn, problems, [], {})
    rows.append(dict(case="MEDIUM-2 text m_dn_0 without prior types", target_refused=bool(problems), problems=problems))
    return rows



def final_counterexamples(monitor, health, *, current):
    """Final-review witnesses use the real fixed universe, never observed members."""
    from test_sig1_corrective import production_scope_fixture
    from test_signal_integrity import add_price
    rows = []
    for name in ("N1", "M1_empty", "M1_columns", "M1_quarantined", "M2", "M3"):
        conn = production_scope_fixture()
        if name == "N1":
            add_price(conn, "2025-01-02", "BBHI")
            conn.execute("DELETE FROM price_history WHERE date>'2026-10-09'")
        elif name == "M1_empty":
            conn.execute("DELETE FROM price_history")
        elif name == "M1_columns":
            conn.execute("ALTER TABLE price_history RENAME COLUMN close TO absent_close")
        elif name == "M1_quarantined":
            conn.execute("INSERT INTO price_quarantine SELECT * FROM price_history")
        elif name == "M2":
            conn.execute("UPDATE price_history SET open=100,high=101,low=99,close=100,volume=1000 WHERE ticker='BNBR' AND date='2026-10-13'")
        else:
            conn.execute("UPDATE broker_flow SET date='2026-10-01'")
            conn.execute("UPDATE market_summary_daily SET date='2026-10-01'")
        item = dict(case=name)
        try:
            result = health.report_admission(conn, AS_OF) if current else health.require_current_prices(conn, AS_OF)
            item["gate"] = dict(admitted=True, evidence=result)
        except Exception as exc:
            item["gate"] = dict(admitted=False, exception=type(exc).__name__, reason=str(exc), evidence=getattr(exc, "evidence", None))
        try:
            problems, notes, stats = monitor.check(conn, AS_OF, {"price-history-topup.yml": "disabled_manually"})
            item["monitor"] = dict(exit=monitor.exit_code(problems, stats),
                                   categories=sorted({f["category"] for f in stats.get("findings", [])}),
                                   price_health=stats.get("freshness", {}).get("price_history"),
                                   rendered=monitor.format_report(problems, notes, stats))
        except Exception as exc:
            item["monitor"] = dict(exception=type(exc).__name__, reason=str(exc))
        rows.append(item)
        conn.close()
    return rows

def preserved_modules(directory):
    names = tuple(n for n in ("price_audit", "signal_integrity_health", "check_signal_integrity") if (directory / (n + ".py")).is_file())
    original = {name: sys.modules[name] for name in names}
    copies = {}
    try:
        for name in names:
            spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            source = (directory / (name + ".py")).read_text(encoding="utf-8")
            exec(compile(source, f"preserved-before/{name}.py", "exec"), module.__dict__)
            copies[name] = module
    finally:
        sys.modules.update(original)
    return copies


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-dir", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--final-before-dir", type=Path, help="preserved files from the start of the final corrective pass")
    parser.add_argument("--worker", choices=MUTATIONS)
    parser.add_argument("--temp", default="scratchpad/sig1_corrective/mutation_tmp")
    args = parser.parse_args()
    if args.worker:
        Path(args.temp).parent.mkdir(parents=True, exist_ok=True)
        return mutation_worker(args.worker, args.temp)
    import check_signal_integrity as monitor
    import signal_integrity_health as health
    result = dict(after=counterexamples(monitor, health, repaired=True), mutations=[])
    result["final_after"] = final_counterexamples(monitor, health, current=True)
    if args.final_before_dir:
        before = preserved_modules(args.final_before_dir)
        result["final_before"] = final_counterexamples(before["check_signal_integrity"], before["signal_integrity_health"], current=False)
    if args.before_dir:
        before = preserved_modules(args.before_dir)
        result["before"] = counterexamples(before["check_signal_integrity"],
                                           before["signal_integrity_health"], repaired=False)
    with tempfile.TemporaryDirectory(prefix="sig1-mutations-") as work_dir:
        work = Path(work_dir)
        for name in MUTATIONS:
            run = subprocess.run([sys.executable, __file__, "--worker", name, "--temp",
                                  str(work / name)], cwd=ROOT,
                                 capture_output=True, text=True, timeout=120)
            killed = run.returncode == 1 and "failed" in run.stdout and "ERROR collecting" not in run.stdout
            result["mutations"].append(dict(name=name, witness=MUTATIONS[name][3],
                                             pytest_exit=run.returncode, killed=killed,
                                             output=run.stdout + run.stderr))
            print(f"{name}: {'KILLED' if killed else 'SURVIVED/ERROR'}", flush=True)
    result["all_mutations_killed"] = all(m["killed"] for m in result["mutations"])
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "mutations"}, indent=2))
    return 0 if result["all_mutations_killed"] else 1


if __name__ == "__main__":
    sys.exit(main())
