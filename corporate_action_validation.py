"""Pytest evidence accounting, including legacy early-return artifact gates.

Load with ``-p corporate_action_validation``. Missing evidence withdraws a case
from PASS; subtests are counted separately from their enclosing test functions.
"""
import json
import re

PREFIX = "CA_VALIDATION_COUNTS: "
KEYS = ("passed", "failed", "skipped", "unavailable")
_cases = {}
_subtests = []
_collection_errors = []


def gate_messages(output):
    return list(dict.fromkeys(line.strip() for line in output.splitlines()
        if re.match(r"\s*(?:SKIP\b|skip [^\n]*:)", line, re.I)))


def optional_skip(reason):
    return bool(re.search(r"torch.*(?:not installed|unavailable|not available)|no torch", reason, re.I))


def validation_counts(status, output):
    for line in reversed(output.splitlines()):
        if line.startswith(PREFIX):
            counts = json.loads(line[len(PREFIX):])
            break
    else:
        # Compatibility for subprocess witnesses/mocks without this plugin.
        counts = dict.fromkeys(KEYS, 0)
        summary = next((s for s in reversed(output.splitlines())
                        if re.search(r"\b\d+ (?:passed|failed|skipped|error)", s)), "")
        for key in ("passed", "failed", "skipped"):
            match = re.search(rf"\b(\d+) {key}\b", summary)
            counts[key] = int(match[1]) if match else 0
        messages = gate_messages(output)
        counts["unavailable"] = counts["skipped"] + len(messages)
        counts["skipped"] = 0
        counts["passed"] = max(0, counts["passed"] - len(messages))
        counts["unavailable_checks"] = messages
        counts["subtests"] = dict.fromkeys(KEYS, 0)
    if (status not in (0, 5) and not counts["failed"]
            and not counts.get("subtests", {}).get("failed", 0)):
        counts["failed"] = 1
    if not sum(counts[k] for k in KEYS):
        counts["unavailable"] = 1
        counts.setdefault("unavailable_checks", []).append("No test executed")
    return counts


def evidence_status(counts):
    if counts["failed"] or counts.get("subtests", {}).get("failed", 0):
        return "FAIL"
    if (counts["unavailable"] or counts.get("subtests", {}).get("unavailable", 0)
            or not counts["passed"]):
        return "UNAVAILABLE"
    return "PASS"


def pytest_sessionstart(session):
    _cases.clear()
    _subtests.clear()
    _collection_errors.clear()


def pytest_collectreport(report):
    if report.failed:
        _collection_errors.append(report.nodeid)


def pytest_runtest_logreport(report):
    if report.when == "teardown" and not report.failed:
        return
    if report.when == "setup" and report.passed:
        return
    reason = str(report.longrepr) if report.skipped else ""
    gates = gate_messages(report.capstdout)
    if report.nodeid in _early_returns:
        gates.append("Test returned before completing its evidence checks")
    status = ("failed" if report.failed else
              "skipped" if report.skipped and optional_skip(reason) else
              "unavailable" if report.skipped or gates else "passed")
    record = {"nodeid": report.nodeid, "status": status,
              "reason": reason or "; ".join(gates)}
    if hasattr(report, "context"):
        _subtests.append(record)
    else:
        previous = _cases.get(report.nodeid)
        if previous is None or status == "failed":
            _cases[report.nodeid] = record


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    counts = dict.fromkeys(KEYS, 0)
    subtests = dict.fromkeys(KEYS, 0)
    for record in _cases.values():
        counts[record["status"]] += 1
    for record in _subtests:
        subtests[record["status"]] += 1
    counts["failed"] += len(_collection_errors)
    counts["subtests"] = subtests
    counts["unavailable_checks"] = [r["nodeid"] + ": " + r["reason"]
        for r in [*_cases.values(), *_subtests] if r["status"] == "unavailable"]
    if not sum(counts[k] for k in KEYS):
        counts["unavailable"] = 1
        counts["unavailable_checks"].append("No test executed")
    terminalreporter.write_line(PREFIX + json.dumps(counts, sort_keys=True))

# A few frozen-artifact suites return silently when their export is absent.
# Observe only explicit bare returns in test functions; falling off the end
# after completing a test is never a gate. This keeps legacy tests unchanged.
import ast
from pathlib import Path
import sys
import pytest

_bare_returns = {}
_early_returns = set()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    filename = str(item.path)
    if filename not in _bare_returns:
        tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
        missing_evidence_errors = {"FileNotFoundError", "OSError", "ImportError",
                                   "ModuleNotFoundError", "PermissionError", "TimeoutError",
                                   "ConnectionError", "Exception", "BaseException"}
        exception_returns = {n.lineno for handler in ast.walk(tree)
                             if isinstance(handler, ast.ExceptHandler) and handler.type is not None
                             and not any(getattr(t, "id", getattr(t, "attr", None)) in missing_evidence_errors
                                         for t in ast.walk(handler.type))
                             for n in ast.walk(handler) if isinstance(n, ast.Return)}
        _bare_returns[filename] = {n.lineno for n in ast.walk(tree)
                                  if isinstance(n, ast.Return) and n.value is None
                                  and n.lineno not in exception_returns}
    _early_returns.discard(item.nodeid)
    previous = sys.gettrace()

    def trace(frame, event, arg):
        if frame.f_code.co_filename != filename or not frame.f_code.co_name.startswith("test"):
            return None
        if event == "return" and frame.f_lineno in _bare_returns[filename]:
            _early_returns.add(item.nodeid)
        return trace

    sys.settrace(trace)
    try:
        return (yield)
    finally:
        sys.settrace(previous)
