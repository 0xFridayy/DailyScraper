"""Unsupported CLIs refuse first: no font, cache, temp or output artifacts (F12).

Each route runs cold in a source-only copy (no databases) with fresh, isolated
HOME/TEMP/MPLCONFIGDIR/XDG/APPDATA directories. A usage error or input check
before the refusal, or any file written anywhere, fails the regression.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).parent
ROUTES = json.loads((ROOT / "corporate_action_consumer_routes.json").read_text())
UNSUPPORTED_CLIS = sorted({
    "analyze_ticker_patterns", "ara_arb_simulation", "ara_multiday", "arb_veto", "broker_learning_run",
    "ddqn_entry_exit", "evaluate_signals", "experiment_1f_candidate", "experiment_1f_features",
    "experiment_1f_gate_b", "experiment_1f_gate_b_contract", "experiment_1f_manifest",
    "experiment_1f_normalization", "experiment_1f_universe_gate", "experiment_1f_validation",
    "experiment_1f_validity", "experiment_2a0_event_study", "feature_ablation",
    "foreign_flow_signal_backtest", "horizon_scan", "inventory_features", "label_compare",
    "ml_v2_experiment_1", "ml_v2_experiment_1_robustness", "multiday_features", "pattern_backtest",
    "pattern_detector", "pattern_type_backtest", "regime_gated_momentum", "run_ml_reports",
    "scan_ara_arb", "shap_analysis", "smart_money_divergence", "strategy_variants",
    "txchart_backtest", "walk_forward_backtest"})
EXTRA_ARGS = {"daily_picks": ["--preview"]}


def cold_run(module):
    with tempfile.TemporaryDirectory(prefix="ca-cold-") as base:
        base = Path(base)
        work = base / "src"
        work.mkdir()
        for source in list(ROOT.glob("*.py")) + list(ROOT.glob("*.json")):
            shutil.copyfile(source, work / source.name)
        isolated = {name: base / name for name in ("home", "tmp", "mpl", "cache", "config")}
        for folder in isolated.values():
            folder.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "HOME": str(isolated["home"]), "USERPROFILE": str(isolated["home"]),
               "TEMP": str(isolated["tmp"]), "TMP": str(isolated["tmp"]), "TMPDIR": str(isolated["tmp"]),
               "MPLCONFIGDIR": str(isolated["mpl"]), "XDG_CACHE_HOME": str(isolated["cache"]),
               "XDG_CONFIG_HOME": str(isolated["config"]), "APPDATA": str(isolated["config"]),
               "LOCALAPPDATA": str(isolated["cache"]), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(work), "PYTHONUTF8": "1"}
        before = {p.relative_to(work) for p in work.rglob("*")}
        run = subprocess.run([sys.executable, str(work / f"{module}.py"), *EXTRA_ARGS.get(module, [])],
                             cwd=work, env=env, capture_output=True, text=True, timeout=600)
        created = sorted(str(p) for p in {p.relative_to(work) for p in work.rglob("*")} - before)
        leaked = {name: sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))
                  for name, folder in isolated.items() if any(folder.rglob("*"))}
        return run, created, leaked


@pytest.fixture(scope="module")
def cold_results():
    modules = UNSUPPORTED_CLIS + sorted(EXTRA_ARGS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(modules, pool.map(cold_run, modules)))


@pytest.mark.parametrize("module", UNSUPPORTED_CLIS + sorted(EXTRA_ARGS))
def test_unsupported_cold_cli_refuses_before_any_artifact(module, cold_results):
    run, created, leaked = cold_results[module]
    assert run.returncode != 0
    assert "price_contract.UnsupportedPriceContract: " + module + "." in run.stderr, run.stderr[-600:]
    assert created == [], f"{module} wrote {created}"
    assert leaked == {}, f"{module} initialised {leaked}"


def test_cli_routes_that_must_refuse_before_parsing_are_declared():
    declared = ROUTES["cli_routes"]
    for module in ("walk_forward_backtest", "shap_analysis", "ddqn_entry_exit", "ara_arb_simulation",
                   "experiment_1f_features"):
        assert declared.get(module) == "__main__"
        source = (ROOT / f"{module}.py").read_text(encoding="utf-8")
        main = source.split('if __name__ == "__main__":', 1)[1]
        assert main.lstrip().startswith("from price_contract import refuse_unmigrated\n"), module
        assert f'refuse_unmigrated("{module}.__main__")' in main.split("\n")[2]
