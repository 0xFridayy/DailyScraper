"""Phase 5: mandatory unavailable evidence must never become a verifier PASS."""
import json
import sys
from unittest.mock import patch

import pytest
import verify_corporate_action_contract as verifier


def run_verifier(monkeypatch, tmp_path, status, output):
    monkeypatch.setattr(sys, "argv", ["verify", "--fixture-root", str(tmp_path)])
    monkeypatch.setattr(verifier, "prepare_snapshot", lambda *a: tmp_path)
    monkeypatch.setattr(verifier, "SUITES", ["mandatory.py"])
    monkeypatch.setattr(verifier, "execute", lambda *a, **k: (status, output))
    monkeypatch.setattr(verifier, "verify_mutants", lambda *a: 0)
    return verifier.main()


@pytest.mark.parametrize("status,output", [
    (0, "1 passed in 0.1s\nSKIP historical fixture: absent\n"),
    (0, "1 skipped in 0.1s\n"),
    (5, "no tests ran in 0.1s\n"),
    (0, "no tests ran in 0.1s\n"),
])
def test_mandatory_unavailable_blocks_success(monkeypatch, tmp_path, capsys, status, output):
    assert run_verifier(monkeypatch, tmp_path, status, output) == 2
    text = capsys.readouterr().out
    assert "UNAVAILABLE" in text
    assert "Required regressions and semantic mutations passed." not in text


def test_real_failure_remains_failure_with_unavailable(monkeypatch, tmp_path, capsys):
    assert run_verifier(monkeypatch, tmp_path, 1, "1 failed, 1 skipped in 0.1s\n") == 1
    assert "FAIL" in capsys.readouterr().out


def test_missing_fixture_is_incomplete_not_a_crash(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["verify", "--fixture-root", str(tmp_path)])
    monkeypatch.setattr(verifier, "prepare_snapshot",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("historical fixture absent")))
    assert verifier.main() == 2
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_counts_do_not_double_count_artifact_gates():
    counts = verifier.validation_counts(0, "3 passed, 1 skipped in 0.1s\n"
        "SKIP cache check: absent\n")
    assert counts["passed"] == 2
    assert counts["unavailable"] == 2
    assert counts["failed"] == 0


def test_mutant_baseline_with_no_assertions_is_unavailable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(verifier, "MUTANTS", [("probe", "probe.py", "old", "new", "test_probe")])
    monkeypatch.setattr(verifier, "FINDING_MUTANTS", [])
    monkeypatch.setattr(verifier, "RESTART_MUTANTS", [])
    monkeypatch.setattr(verifier, "PHASE5_MUTANTS", [])
    monkeypatch.setattr(verifier, "PRICE_REVISION_MUTANTS", [])
    monkeypatch.setattr(verifier, "execute", lambda *a: (0, "1 skipped in 0.1s\n"))
    assert verifier.verify_mutants(tmp_path, {"PYTHONPATH": ""}) == 2
    assert "unavailable: 1" in capsys.readouterr().out.lower()

def test_pytest_plugin_counts_silent_and_printed_gates(tmp_path):
    import os
    import subprocess
    from corporate_action_validation import PREFIX
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n"
        "def test_pass(): assert True\n"
        "def test_printed(): print('SKIP cache: absent')\n"
        "def test_silent():\n    if True: return\n    assert False\n"
        "def test_formal(): pytest.skip('UNAVAILABLE: fixture absent')\n"
        "def test_optional(): pytest.skip('torch not installed')\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(verifier.ROOT), env.get("PYTHONPATH", "")])
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p",
        "no:cacheprovider", "-p", "corporate_action_validation", str(probe)],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert result.returncode == 0, result.stdout
    counts = verifier.validation_counts(result.returncode, result.stdout)
    assert {k: counts[k] for k in ("passed", "failed", "skipped", "unavailable")} == {
        "passed": 1, "failed": 0, "skipped": 1, "unavailable": 3}


def test_verifier_decodes_child_output_as_utf8(monkeypatch):
    from types import SimpleNamespace
    def child(*args, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        return SimpleNamespace(returncode=0, stdout="✓")
    monkeypatch.setattr(verifier.subprocess, "run", child)
    assert verifier.execute(["stub"], {}) == (0, "✓")


def test_unavailable_subtest_blocks_success(monkeypatch, tmp_path, capsys):
    counts = {"passed": 1, "failed": 0, "skipped": 0, "unavailable": 0,
              "subtests": {"passed": 0, "failed": 0, "skipped": 0, "unavailable": 1},
              "unavailable_checks": ["mandatory subtest: missing fixture"]}
    output = "CA_VALIDATION_COUNTS: " + json.dumps(counts)
    assert run_verifier(monkeypatch, tmp_path, 0, output) == 2
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_failed_subtest_does_not_invent_a_failed_parent():
    from corporate_action_validation import evidence_status
    counts = {"passed": 1, "failed": 0, "skipped": 0, "unavailable": 0,
              "subtests": {"passed": 0, "failed": 1, "skipped": 0, "unavailable": 0}}
    result = verifier.validation_counts(1, "CA_VALIDATION_COUNTS: " + json.dumps(counts))
    assert result["failed"] == 0
    assert result["subtests"]["failed"] == 1
    assert evidence_status(result) == "FAIL"


def test_expected_validation_exception_return_counts_as_pass(tmp_path):
    import os
    import subprocess
    probe = tmp_path / "test_expected.py"
    probe.write_text("def test_expected():\n"
        "    try:\n        raise ValueError('validation refused')\n"
        "    except ValueError:\n        return\n"
        "    raise AssertionError('expected validation error')\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(verifier.ROOT), env.get("PYTHONPATH", "")])
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p",
        "no:cacheprovider", "-p", "corporate_action_validation", str(probe)],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    counts = verifier.validation_counts(result.returncode, result.stdout)
    assert counts["passed"] == 1
    assert counts["unavailable"] == 0


def test_missing_fixture_exception_return_is_unavailable(tmp_path):
    import os
    import subprocess
    probe = tmp_path / "test_absent.py"
    probe.write_text("from pathlib import Path\n"
        "def test_absent():\n    try:\n"
        "        Path(__file__).with_name('absent.csv').read_bytes()\n"
        "    except FileNotFoundError:\n        return\n"
        "    assert False\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(verifier.ROOT), env.get("PYTHONPATH", "")])
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p",
        "no:cacheprovider", "-p", "corporate_action_validation", str(probe)],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    counts = verifier.validation_counts(result.returncode, result.stdout)
    assert counts["passed"] == 0
    assert counts["unavailable"] == 1
