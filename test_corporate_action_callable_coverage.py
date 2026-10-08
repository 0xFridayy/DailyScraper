"""Direct-call refusal regressions using disposable inputs and reconstructed labels."""

import ast
import importlib
from pathlib import Path
import re
import socket
import sqlite3

import numpy as np
import pandas as pd
import pytest

from price_contract import UnsupportedPriceContract


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in callable contract tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


class UnreadableInput:
    """A consumer must refuse before it examines or iterates these inputs."""

    def __getattr__(self, name):
        raise AssertionError(f"input read before contract refusal: {name}")

    def __getitem__(self, key):
        raise AssertionError("input indexed before contract refusal")

    def __iter__(self):
        raise AssertionError("input iterated before contract refusal")

    def __len__(self):
        raise AssertionError("input length read before contract refusal")

    def __bool__(self):
        raise AssertionError("input truth value read before contract refusal")


@pytest.mark.parametrize("module,name,args", [
    ("ml_v2_experiment_1_robustness", "paired_date_differences", (UnreadableInput(), UnreadableInput())),
    ("experiment_1f_features", "daily_ic_capacity", (UnreadableInput(), UnreadableInput(), UnreadableInput(), 1, UnreadableInput())),
    ("broker_learning", "primary_status", (UnreadableInput(),)),
])
def test_final_discovered_financial_apis_refuse_before_input(module, name, args):
    route = module + "." + name
    with pytest.raises(UnsupportedPriceContract, match=re.escape(route)) as caught:
        getattr(importlib.import_module(module), name)(*args)
    assert caught.value.consumer == route


def test_report_refusal_is_importable_without_messaging_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    module = importlib.import_module("run_ml_reports")
    with pytest.raises(UnsupportedPriceContract) as caught:
        module.format_telegram_message(None, None, None, None)
    assert caught.value.consumer == "run_ml_reports.format_telegram_message"


@pytest.mark.parametrize("module,name,args", [
    ("broker_dashboard", "_header", ({}, [{"eligible": True, "fired": ["R1"]}])),
    ("broker_dashboard", "_rule_chip", ("R1", {"R1": {"status": "CONSISTENT", "weight": 1.4}})),
    ("ml_v2_experiment_1_robustness", "paired_bootstrap_report", (UnreadableInput(),)),
    ("experiment_1f_gate_b", "graduation_report", (UnreadableInput(),)),
    ("experiment_1f_gate_b", "sensitivity_report", (UnreadableInput(),)),
    # A cached outcome report (mean return, hit rate) is economic output.
    ("evaluate_signals", "format_report", (UnreadableInput(),)),
])
def test_cached_financial_result_presentations_refuse_without_identity(module, name, args):
    route = module + "." + name
    with pytest.raises(UnsupportedPriceContract, match=re.escape(route)) as caught:
        getattr(importlib.import_module(module), name)(*args)
    assert caught.value.consumer == route


@pytest.mark.parametrize("name,arguments", [
    ("rank_label", (UnreadableInput(),)),
    ("open_usable", (UnreadableInput(),)),
    ("close_step_in_band", (UnreadableInput(),)),
    ("select_top_k", (UnreadableInput(), "score")),
    ("portfolio_return", (UnreadableInput(), "HOLD_THROUGH")),
    ("benchmark_return", (UnreadableInput(), "HOLD_THROUGH")),
    ("daily_top3_excess", (UnreadableInput(), "score")),
])
def test_experiment_direct_api_refuses_before_reading_input(name, arguments):
    module = importlib.import_module("experiment_1f_evaluation")
    route = f"experiment_1f_evaluation.{name}"
    with pytest.raises(UnsupportedPriceContract, match=re.escape(route)):
        getattr(module, name)(*arguments)


def test_illegal_crossing_labels_cannot_be_ranked_or_scored_directly():
    module = importlib.import_module("experiment_1f_evaluation")
    crossing = 1080 / 1425 - 1
    panel = pd.DataFrame({
        "ticker": ["ENRG"] + [f"T{i:03d}" for i in range(9)],
        "date": ["2026-10-01"] * 10,
        "fwd_oo_1": [crossing] + [i / 100 for i in range(9)],
    })
    with pytest.raises(UnsupportedPriceContract, match="rank_label"):
        module.rank_label(panel)
    selected = pd.DataFrame({"ticker": ["ENRG"], "return_hold_through": [crossing]})
    with pytest.raises(UnsupportedPriceContract, match="portfolio_return"):
        module.portfolio_return(selected, "HOLD_THROUGH", k=1)


@pytest.mark.parametrize("name,arguments", [
    ("simulate_predictions", (UnreadableInput(), UnreadableInput(), UnreadableInput())),
    ("evaluate", (UnreadableInput(), UnreadableInput(), UnreadableInput(), UnreadableInput())),
    ("select_threshold", (UnreadableInput(),)),
])
def test_regime_direct_api_refuses_before_reading_input(name, arguments):
    module = importlib.import_module("regime_gated_momentum")
    route = f"regime_gated_momentum.{name}"
    with pytest.raises(UnsupportedPriceContract, match=re.escape(route)):
        getattr(module, name)(*arguments)


def test_implied_price_window_refuses_a_real_unversioned_series():
    module = importlib.import_module("targeted_actor_observations")
    series = {
        "blot": [10, 10], "bval": [1440000, 1030000],
        "slot": [0, 0], "sval": [0, 0],
        "nlot": [10, 10], "nval": [1440000, 1030000],
    }
    with pytest.raises(UnsupportedPriceContract, match="targeted_actor_observations._window"):
        module._window(series, 0, False)


def test_implied_price_window_refuses_before_reading_series():
    module = importlib.import_module("targeted_actor_observations")
    with pytest.raises(UnsupportedPriceContract, match="targeted_actor_observations._window"):
        module._window(UnreadableInput(), 0, False)


@pytest.mark.parametrize("name,arguments", [
    ("fit_normalizer", (UnreadableInput(),)),
    ("normalize_features", (UnreadableInput(), UnreadableInput(), UnreadableInput())),
    ("evaluate_policy", (UnreadableInput(), UnreadableInput())),
    ("evaluate_policy_with_trade_log", (UnreadableInput(), UnreadableInput())),
])
def test_ddqn_direct_api_refuses_before_model_or_environment(name, arguments):
    route = f"ddqn_entry_exit.{name}"
    with pytest.raises(UnsupportedPriceContract, match=re.escape(route)):
        ddqn_callable(name)(*arguments)


def ddqn_callable(name, class_name=None):
    """Use the real module, or its exact entry function when Torch is absent.

    The fallback executes the production definition without supplying Torch or
    feature globals. Any computation before the required refusal fails.
    """
    if importlib.util.find_spec("torch") is not None:
        module = importlib.import_module("ddqn_entry_exit")
        owner = getattr(module, class_name) if class_name else module
        return getattr(owner, name)
    path = Path(__file__).with_name("ddqn_entry_exit.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == class_name) if class_name else tree
    definition = next(node for node in owner.body if isinstance(node, ast.FunctionDef)
                      and node.name == name)
    namespace = {"__name__": "ddqn_entry_exit"}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("name,arguments", [
    ("reset", (UnreadableInput(),)),
    ("_state", (UnreadableInput(),)),
    ("step", (UnreadableInput(), 1)),
])
def test_legacy_ddqn_environment_cannot_bypass_constructor_refusal(name, arguments):
    route = f"ddqn_entry_exit.TickerEnv.{name}"
    before = vars(arguments[0]).copy()
    with pytest.raises(UnsupportedPriceContract, match=re.escape(route)):
        ddqn_callable(name, "TickerEnv")(*arguments)
    assert vars(arguments[0]) == before


def test_ddqn_q_values_cannot_be_predicted_from_an_unversioned_model():
    with pytest.raises(UnsupportedPriceContract, match="ddqn_entry_exit.QNet.forward"):
        ddqn_callable("forward", "QNet")(UnreadableInput(), UnreadableInput())


@pytest.mark.parametrize("table", [
    "live_signals", "live_outcomes", "rule_stats", "alpha_cases", "rule_weights",
    "broker_scores", "broker_profitability", "broker_lift",
])
def test_analytical_insert_refuses_before_database_or_row_access(table):
    module = importlib.import_module("broker_learning_db")
    with pytest.raises(UnsupportedPriceContract, match="broker_learning_db.insert_rows"):
        module.insert_rows(UnreadableInput(), table, UnreadableInput())


def test_illegal_unversioned_return_is_not_committed(tmp_path):
    module = importlib.import_module("broker_learning_db")
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        module.ensure_schema(conn)
        before = conn.total_changes
        row = {"session_date": "2026-10-01", "ticker": "ENRG", "h": 1,
               "fwd_oo": 1080 / 1425 - 1, "exit_date": "2026-10-05"}
        with pytest.raises(UnsupportedPriceContract, match="broker_learning_db.insert_rows"):
            module.insert_rows(conn, "live_outcomes", [row])
        assert conn.total_changes == before
        assert conn.execute("SELECT COUNT(*) FROM live_outcomes").fetchone()[0] == 0


def test_empty_analytical_insert_is_still_an_unsupported_operation():
    module = importlib.import_module("broker_learning_db")
    with pytest.raises(UnsupportedPriceContract, match="broker_learning_db.insert_rows"):
        module.insert_rows(UnreadableInput(), "live_outcomes", [])


def test_run_log_and_table_allowlist_keep_their_ordinary_contract(tmp_path):
    module = importlib.import_module("broker_learning_db")
    with sqlite3.connect(tmp_path / "run-log.db") as conn:
        module.ensure_schema(conn)
        row = {"run_id": "fixture-run", "kind": "daily", "status": "expected_refusal"}
        assert module.insert_rows(conn, "runs", [row]) == 1
        assert module.insert_rows(conn, "runs", [row]) == 0
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "expected_refusal"
        with pytest.raises(ValueError, match="sqlite_master"):
            module.insert_rows(conn, "sqlite_master", [row])


def test_anonymous_numerical_helpers_retain_rank_slot_and_selection_semantics():
    experiment = importlib.import_module("experiment_1f_evaluation")
    regime = importlib.import_module("regime_gated_momentum")
    ranks = experiment.grouped_percentile_rank([1, 1, 3, np.nan, 2], ["a", "a", "a", "a", "b"], 3)
    assert np.allclose(ranks.iloc[:3], [1 / 3, 1 / 3, 5 / 6])
    assert ranks.iloc[3:].isna().all()
    assert np.array_equal(experiment.descending_positions([2, 2, 1], ["B", "A", "C"], 2), [1, 0])
    assert experiment.slot_average([0.03, np.nan], count=3) == pytest.approx(0.01)
    assert experiment.slot_average([0.03, np.nan], resolved_only=True) == pytest.approx(0.03)
    metrics = [[0.9, 0.8], [0.5, 0.4], [0.5, 0.7], [np.nan, 1]]
    assert regime.metric_selection_index(metrics, [1, 20, 20, 100], 20) == (2, True)
    assert regime.metric_selection_index(metrics, [1, 1, 1, 1], 20) == (0, False)


def test_stored_v0_rule_signals_are_not_returned_by_a_direct_reader(tmp_path):
    import broker_learning_db as db
    with pytest.raises(UnsupportedPriceContract) as caught:
        db.live_signal_frame(UnreadableInput())
    assert caught.value.consumer == "broker_learning_db.live_signal_frame"
    path = tmp_path / "bl.db"
    with sqlite3.connect(path) as conn:
        db.ensure_schema(conn)
        before = conn.total_changes
        with pytest.raises(UnsupportedPriceContract):
            db.live_signal_frame(conn)
        assert conn.total_changes == before
