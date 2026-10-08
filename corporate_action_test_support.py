"""Regression assertion for intentionally unsupported v1 consumer routes.

Earlier tests expected v0 economic outputs from uncertified source fixtures.
Their v1 expectation is an explicit refusal before reading or writing inputs.
The old formulas and accepted artifacts remain in Git under their old contract.
"""

import importlib
import inspect
import ast
import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch
from types import ModuleType

from price_contract import UnsupportedPriceContract


def restart_evidence(px, axis=None):
    """Prepend one explicit restart window per ticker to a synthetic fixture.

    The restart contract never trusts a frame's first row. Fixtures that test
    behaviour after an anchor therefore supply the contract's evidence:
    DEPENDENCY_ROWS consecutive verified sessions ending on the session before
    the ticker's first bar, flat at that bar's close with a ticker-specific
    traded volume (never a cross-ticker duplicate). Returns the extended frame
    and session axis; strip_restart_evidence() removes these rows from outputs.
    """
    import pandas as pd
    from datetime import date, timedelta
    from price_contract import DEPENDENCY_ROWS, is_idx_session, IdxCalendarUnavailable
    pieces, evidence = [], set()
    for number, (ticker, rows) in enumerate(px.groupby("ticker", sort=True)):
        first = rows.sort_values("date").iloc[0]
        sessions, day = [], date.fromisoformat(first["date"])
        while len(sessions) < DEPENDENCY_ROWS:
            day -= timedelta(days=1)
            try:
                if is_idx_session(day):
                    sessions.append(day.isoformat())
            except IdxCalendarUnavailable:
                raise AssertionError(f"{ticker}: fixture starts too early for restart evidence")
        close = float(first["close"])
        for session in sorted(sessions):
            pieces.append(dict(first.to_dict(), date=session, open=close, high=close, low=close,
                               close=close, volume=10_000.0 + number))
            evidence.add((ticker, session))
    extended = pd.concat([pd.DataFrame(pieces, columns=list(px.columns)).astype(px.dtypes.to_dict(), errors="ignore"),
                          px], ignore_index=True).sort_values(["ticker", "date"], kind="mergesort")
    extended = extended.reset_index(drop=True)
    extended.attrs["restart_evidence"] = sorted(evidence)
    dates = sorted(set(axis if axis is not None else px.date) | {d for _, d in evidence})
    return extended, dates


def strip_restart_evidence(out, evidence):
    evidence = set(map(tuple, evidence))
    keep = [(t, d) not in evidence for t, d in zip(out.ticker, out.date)]
    stripped = out.loc[keep].reset_index(drop=True)
    stripped.attrs = dict(out.attrs)
    return stripped


def assert_unmigrated(route):
    module, name = route.rsplit(".", 1)
    function = getattr(importlib.import_module(module), name)
    parameters = inspect.signature(function).parameters
    args, kwargs = [], {}
    for parameter in parameters.values():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            args.append(None)
        elif parameter.kind == inspect.Parameter.KEYWORD_ONLY:
            kwargs[parameter.name] = None
    try:
        function(*args, **kwargs)
    except UnsupportedPriceContract as exc:
        assert route in str(exc)
        assert "unsupported" in str(exc) and "Frozen artifacts" in str(exc)
    else:
        raise AssertionError(f"{route} produced an uncertified output instead of refusing")


def frozen_evidence_fixture(*args, **kwargs):
    """Construct synthetic v0 documents to test retained v0 SQL validators.

    This is a test-only reconstruction of the exact fixed base, never a v1
    source adapter. It preserves the existing acceptance/hardening regression
    tests independently of the refusal of new price-contract calculations.
    """
    import inventory_evidence as ie
    source = subprocess.check_output(
        ["git", "show", "5a2ec4181ad897e3d6455c5b7855e0d9830b1768:inventory_evidence.py"],
        cwd=Path(__file__).parent)
    assert hashlib.sha256(source).hexdigest() == "2d9f7805667bb73cbdf0f889cc0bc3f6ed04f68a03ca304bf039b328602b5773"
    tree = ast.parse(source)
    definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_market_measurement")
    namespace = dict(vars(ie))
    exec(compile(ast.Module(body=[definition], type_ignores=[]), "frozen-v0-market-fixture", "exec"), namespace)
    with patch.object(ie, "_market_measurement", namespace["_market_measurement"]):
        return ie.build_inventory_evidence(*args, **kwargs)


def frozen_daily_numeric_fixture():
    """Retain reviewed legacy numerical assertions separately from v1 refusal.

    This exact Git object is test-only and supplies synthetic, in-memory legacy
    calculations. It never replaces the imported production module or certifies
    new price output. Each caller also exercises the current real refusal.
    """
    source = subprocess.check_output([
        "git", "show", "9e32b14e7e828d7fb300bad051af491ea4c8cac2:daily_picks.py"], cwd=Path(__file__).parent)
    assert hashlib.sha256(source).hexdigest() == "8c3e6e938b023b031c03d5a5d1d6b97b870d11777396e91aa1c67dff2c77bc63"
    fixture = ModuleType("frozen_daily_numeric_fixture")
    fixture.__file__ = str(Path(__file__).with_name("daily_picks.py"))
    exec(compile(source, "frozen-reviewed-daily-numeric-fixture", "exec"), vars(fixture))
    return fixture


def frozen_reviewed_numeric_function(module, name):
    """Pinned in-memory legacy arithmetic, never a production adapter."""
    pins = {
        ("broker_learning", "primary_status"): "4126c12f68fba8efcbf43bce1298d282226411e482e257bce8c897316d0cdc1f",
        ("ml_v2_experiment_1_robustness", "paired_date_differences"): "ebc903775ff5696947ea63b4c33860b438822b90e62e0811a94d5932b4f4adab",
        ("experiment_1f_gate_b", "graduation_report"): "114c1303f88b420833b518ad420f03554d4911ad7623e1e7c718f6afa0d706d5",
        ("experiment_1f_gate_b", "sensitivity_report"): "114c1303f88b420833b518ad420f03554d4911ad7623e1e7c718f6afa0d706d5",
    }
    expected = pins[module, name]
    source = subprocess.check_output(["git", "show", f"9e32b14e7e828d7fb300bad051af491ea4c8cac2:{module}.py"], cwd=Path(__file__).parent)
    assert hashlib.sha256(source).hexdigest() == expected
    definition = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(vars(importlib.import_module(module)))
    exec(compile(ast.Module(body=[definition], type_ignores=[]), "frozen-reviewed-numeric-fixture", "exec"), namespace)
    return namespace[name]
