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

from price_contract import UnsupportedPriceContract


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
