"""Offline health integration checks; no source database, scraper or sender."""

from contextlib import redirect_stdout
import io
import subprocess
import unittest
from unittest.mock import patch

import check_ml_health as health
from price_contract import CONTRACT_VERSION, UnsupportedPriceContract, refuse_unmigrated
import walk_forward_backtest as wfb


class HealthTests(unittest.TestCase):
    def test_core_runtime_import_error_is_not_downgraded_to_compile_only(self):
        problems, notes, stats = [], [], {}
        with patch.object(health, "CORE_MODULES", ["price_audit"]), \
                patch.object(health, "OPTIONAL_MODULES", []), \
                patch("builtins.__import__", side_effect=RuntimeError("broken initialization")):
            health.check_imports(problems, notes, stats)
        self.assertIn("broken initialization", problems[0])
        self.assertEqual(stats["modules_compiled_only"], 0)

    def test_panel_executes_named_refusal_before_database_or_manifest(self):
        problems, notes, stats = [], [], {}
        with patch.object(health.sqlite3, "connect", side_effect=AssertionError("database opened")), \
                patch.object(health, "refresh_broker_flow_manifest", side_effect=AssertionError("manifest written")):
            self.assertIsNone(health.check_panel(problems, notes, stats))
        self.assertEqual(problems, [])
        self.assertEqual(len(stats["unsupported_routes"]), 1)
        result = stats["unsupported_routes"][0]
        self.assertEqual(result["consumer"], "walk_forward_backtest.build_panel")
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertEqual(result["contract_version"], CONTRACT_VERSION)

    def test_wrong_route_refusal_fails_health(self):
        problems, notes, stats = [], [], {}
        with patch.object(wfb, "build_panel", side_effect=lambda *a, **k: refuse_unmigrated("daily_picks.run_morning")):
            health.check_panel(problems, notes, stats)
        self.assertEqual(len(problems), 1)
        self.assertNotIn("unsupported_routes", stats)

    def test_uncertified_frame_refusal_is_a_real_failure(self):
        problems, notes, stats = [], [], {}
        with patch.object(wfb, "build_panel", side_effect=UnsupportedPriceContract("stale certificate")):
            health.check_panel(problems, notes, stats)
        self.assertIn("unexpected contract refusal", problems[0])
        self.assertNotIn("unsupported_routes", stats)

    def test_ordinary_guard_error_is_a_real_failure(self):
        problems, notes, stats = [], [], {}
        with patch.object(wfb, "build_panel", side_effect=ValueError("broken parser")):
            health.check_panel(problems, notes, stats)
        self.assertIn("ValueError: broken parser", problems[0])

    def test_a_supported_panel_database_error_remains_a_health_failure(self):
        problems, notes, stats = [], [], {}
        with patch.object(health, "_expects_refusal", return_value=False), \
                patch.object(health, "refresh_broker_flow_manifest", return_value="fixture.json"), \
                patch.object(health.sqlite3, "connect", side_effect=health.sqlite3.OperationalError("missing fixture")), \
                patch.object(health.traceback, "print_exc"):
            self.assertIsNone(health.check_panel(problems, notes, stats))
        self.assertIn("OperationalError: missing fixture", problems[0])
        self.assertNotIn("unsupported_routes", stats)

    def test_a_missing_guard_cannot_be_reported_as_expected_refusal(self):
        problems, notes, stats = [], [], {}
        with patch.object(wfb, "build_panel", return_value=None):
            health.check_panel(problems, notes, stats)
        self.assertIn("returned output", problems[0])
        self.assertNotIn("unsupported_routes", stats)

    def test_default_checks_the_real_model_guard_without_uncaught_refusal(self):
        with patch.object(health, "check_imports"), patch.object(health, "check_known_defects"), \
                patch.object(health, "check_unit_tests"):
            problems, notes, stats = health.check()
        self.assertEqual(problems, [])
        self.assertEqual([r["consumer"] for r in stats["unsupported_routes"]],
                         ["walk_forward_backtest.build_panel", "check_ml_health.check_model_runs"])
        self.assertNotIn("cycles", stats)
        self.assertNotIn("panel", stats)

    def test_quick_exercises_panel_guard_and_skips_model(self):
        with patch.object(health, "check_imports"), patch.object(health, "check_known_defects"), \
                patch.object(health, "check_unit_tests"), patch.object(health, "check_model_runs") as model:
            problems, notes, stats = health.check(quick=True)
        model.assert_not_called()
        self.assertEqual(problems, [])
        self.assertEqual(len(stats["unsupported_routes"]), 1)

    def test_missing_model_guard_fails_default(self):
        with patch.object(health, "check_imports"), patch.object(health, "check_known_defects"), \
                patch.object(health, "check_unit_tests"), patch.object(health, "check_model_runs"):
            problems, _, _ = health.check()
        self.assertTrue(any("check_ml_health.check_model_runs returned output" in p for p in problems))

    def test_an_undeclared_model_refusal_is_reported_as_failure_without_crashing(self):
        with patch.object(health, "check_imports"), patch.object(health, "check_known_defects"), \
                patch.object(health, "check_unit_tests"), patch.object(health, "check_panel", return_value=None), \
                patch.object(health, "_expects_refusal", return_value=False):
            problems, _, stats = health.check()
        self.assertIn("UnsupportedPriceContract", problems[0])
        self.assertNotIn("unsupported_routes", stats)

    def test_report_distinguishes_unavailable_routes_from_model_results(self):
        stats = {"unsupported_routes": [{"consumer": "walk_forward_backtest.build_panel",
                 "status": "UNSUPPORTED", "contract_version": CONTRACT_VERSION, "reason": "unmigrated"}]}
        report = health.format_report([], [], stats)
        self.assertIn("ML health OK", report)
        self.assertIn("Expected UNSUPPORTED: walk_forward_backtest.build_panel", report)
        self.assertIn("no analytics produced", report)
        self.assertNotIn("IC ", report)

    def test_failed_script_suite_does_not_count_partial_successes(self):
        problems, stats = [], {}
        def failed(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "fixture passed\n  ok partial\n", "real failure\n")
        with patch.object(health.subprocess, "run", side_effect=failed):
            health.check_unit_tests(problems, stats)
        self.assertGreater(len(problems), 0)
        self.assertEqual(stats["tests_passed"], 0)

    def test_script_summary_is_counted_once(self):
        problems, stats = [], {}
        def successful(args, **kwargs):
            return subprocess.CompletedProcess(args, 0,
                "  ok first\n  ok second\nAll 2 tests passed.\n", "")
        with patch.object(health.subprocess, "run", side_effect=successful):
            health.check_unit_tests(problems, stats)
        self.assertEqual(problems, [])
        self.assertEqual(stats["tests_passed"], 34)

    def test_every_health_suite_uses_a_fresh_process(self):
        launched = []
        def successful(args, **kwargs):
            self.assertEqual(args[0], health.sys.executable)
            self.assertEqual(kwargs["cwd"], health.HERE)
            launched.append(args[1])
            return subprocess.CompletedProcess(args, 0, "", "Ran 2 tests in 0.1s\nOK\n")
        problems, stats = [], {}
        with patch.object(health.subprocess, "run", side_effect=successful):
            health.check_unit_tests(problems, stats)
        self.assertEqual(problems, [])
        self.assertEqual(len(launched), len(set(launched)))
        self.assertEqual(stats["tests_passed"], 2 * len(launched))

    def test_default_cli_does_not_read_credentials_or_send(self):
        with patch.object(health, "check", return_value=([], [], {})), \
                patch.object(health, "_load_dotenv", side_effect=AssertionError("credentials read")), \
                patch.object(health, "send_telegram", side_effect=AssertionError("message sent")), \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            health.main([])
        self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
