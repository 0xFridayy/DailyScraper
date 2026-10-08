"""Offline contract tests. All price/source fixtures are reconstructed inputs."""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3

import pandas as pd
import pytest

import price_contract as pc
import price_audit as pa
from price_contract_frame import default_registry


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("resolver tests must stay offline")
    monkeypatch.setattr(socket, "create_connection", refuse)


def document():
    return json.loads(Path(__file__).with_name("corporate_actions.json").read_text())


def registry(ticker="ENRG", start="2026-09-30", end="2026-10-09"):
    doc = document()
    doc["reviewed_coverage"] = [{"venue": "IDX", "market_scope": ["REGULAR"],
                                "tickers": [ticker], "from": start, "through": end,
                                "evidence_refs": ["RECONSTRUCTED_TEST_COVERAGE_ONLY"]}]
    return pc.parse_registry(doc)


def frame():
    # Audited observed Oct2/Oct5 anchors plus synthetic surrounding context.
    return pd.DataFrame([
        ["2026-09-30", "ENRG", 1400, 1420, 1390, 1400, 1000],
        ["2026-10-01", "ENRG", 1410, 1430, 1400, 1420, 1100],
        ["2026-10-02", "ENRG", 1425, 1440, 1400, 1440, 74202900],
        ["2026-10-05", "ENRG", 1080, 1085, 1000, 1030, 109977800],
        ["2026-10-06", "ENRG", 1040, 1060, 1020, 1050, 1000],
        ["2026-10-07", "ENRG", 1055, 1070, 1040, 1060, 1100],
        ["2026-10-08", "ENRG", 1065, 1080, 1050, 1070, 1100],
        ["2026-10-09", "ENRG", 1075, 1090, 1060, 1080, 1100],
    ], columns=["date", "ticker", "open", "high", "low", "close", "volume"])


def reference(ticker="ENRG", session="2026-10-05", market="REGULAR", previous=None, reg=None, **kwargs):
    kwargs.setdefault("input_representation", pc.RAW_ACTUAL)
    return pc.resolve_limit_reference(ticker, session, market, previous, reg or default_registry(), **kwargs)


def span(start, end, reg=None, **kwargs):
    return pc.return_span_status("ENRG", pc.Anchor(*start), pc.Anchor(*end), "REGULAR",
                                 reg or registry(), frame().date.tolist(), pc.RAW_ACTUAL, **kwargs)


def test_official_reference_is_scoped_and_never_an_economic_return():
    prev = pc.PreviousActual("2026-10-02", 1440, "reconstructed-observed-bar")
    ref = reference(previous=prev)
    assert ref.status == "RESOLVED" and ref.price == 1065
    assert ref.previous_actual.price == 1440
    assert pc.validate_actual_price(1030, ref).status == "IN_BAND"
    assert pc.validate_actual_price(1030, ref).limit_change == pytest.approx(-0.0328638497653)
    for ticker, day, market in [("BBBB", "2026-10-05", "REGULAR"), ("ENRG", "2026-10-05", "CASH"),
                                ("ENRG", "2026-10-02", "REGULAR"), ("IHSG", "2026-10-05", "REGULAR")]:
        r = reference(ticker, day, market, prev)
        assert r.kind != "OFFICIAL_CORPORATE_ACTION_REFERENCE"
        assert pc.validate_actual_price(1030, r).status != "IN_BAND"
    after = reference(session="2026-10-06", previous=pc.PreviousActual("2026-10-05", 1030, "fixture"))
    assert after.price == 1030 and after.event_id is None
    assert reference(input_representation="UNKNOWN").status == "UNRESOLVED"


@pytest.mark.parametrize("price,tier", [(199.99, .35), (200, .25), (5000, .25), (5000.01, .20)])
def test_existing_tiers_and_inclusive_tolerance(price, tier):
    assert pc.ara_bound(price) == tier
    ref = pc.ReferenceResult("RESOLVED", "", price)
    # Use prices exactly representable under the existing REAL quotient rule.
    assert pc.validate_actual_price(price, ref).status == "IN_BAND"
    assert pc.validate_actual_price(price * (1 + tier + pc.TOL + 1e-8), ref).status == "OUT_OF_BAND"
    assert pc.validate_actual_price(price * (1 + pc.ARB_BOUND - pc.TOL - 1e-8), ref).status == "OUT_OF_BAND"
    assert pc.validate_actual_price(price * (1 + tier + pc.TOL - 1e-8), ref).status == "IN_BAND"
    assert pc.validate_actual_price(price * (1 + pc.ARB_BOUND - pc.TOL + 1e-8), ref).status == "IN_BAND"


def test_tier_uses_resolved_reference_and_not_cum_close():
    ref = pc.ReferenceResult("RESOLVED", "", 150, "OFFICIAL_CORPORATE_ACTION_REFERENCE",
                             previous_actual=pc.PreviousActual("2026-10-02", 220, "fixture"))
    assert pc.validate_actual_price(195, ref).status == "IN_BAND"


def test_pending_revoked_conflicts_and_invalid_registry_fail_closed():
    r = reference("SINI", "2026-07-09", previous=pc.PreviousActual("2026-07-08", 10950, "fixture"))
    assert r.status == "UNRESOLVED" and r.reason == "PENDING_REFERENCE" and r.price is None
    assert pc.validate_actual_price(8100, r).status == "UNRESOLVED"
    for mutate in [lambda d: d["events"].append(copy.deepcopy(d["events"][0])),
                   lambda d: d["events"][0].update(reference_price="NaN"),
                   lambda d: d["events"][0].update(reference_price="1063.333", reference_kind="TERP"),
                   lambda d: d["events"][0].update(revision=True),
                   lambda d: d["events"][0].update(effective_session="2026-10-04")]:
        d = document()
        mutate(d)
        with pytest.raises(pc.PriceContractError):
            pc.parse_registry(d)
    d = document()
    d["events"][0].update(status="REVOKED", reference_price=None)
    revoked = pc.parse_registry(d)
    assert reference(reg=revoked).status == "UNRESOLVED"
    assert span(("2026-10-02", "CLOSE"), ("2026-10-05", "OPEN"), revoked).status == "WITHHELD"
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry('{"schema_version":1,"schema_version":1}')


def test_knowledge_precision_and_identity_changes():
    doc = document()
    doc["events"][0].pop("enrolled_at")  # date-only prior review, synthetic registry
    date_only = pc.parse_registry(doc)
    before = datetime(2026, 10, 6, 16, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    assert reference(as_of=before, reg=date_only).reason == "EVENT_NOT_KNOWN_AS_OF"
    assert reference(as_of=after, reg=date_only).status == "RESOLVED"
    assert reference(as_of=after).reason == "EVENT_NOT_KNOWN_AS_OF"
    d = document()
    a = pc.parse_registry(d)
    d["registry_version"] += ".new"
    assert pc.parse_registry(d).content_sha256 != a.content_sha256
    assert reference(reg=a) == reference(reg=a)


def test_trusted_immediate_session_and_missing_data():
    for prev in [None, pc.PreviousActual("2026-10-02", 1440, "fixture"),
                 pc.PreviousActual("2026-10-05", 1030, "fixture", trusted=False),
                 pc.PreviousActual("2026-10-05", 1030, "fixture", representation="MIXED")]:
        assert reference(session="2026-10-06", previous=prev).status == "UNRESOLVED"
    assert reference().status == "RESOLVED"  # official reference needs no predecessor
    assert reference(session="2025-08-25").reason == "UNSUPPORTED_CALENDAR"
    assert span(("2026-10-05", "OPEN"), ("2026-10-05", "CLOSE")).status == "COMPARABLE"
    assert pc.return_span_status("ENRG", pc.Anchor("2026-10-05", "OPEN", False),
                                 pc.Anchor("2026-10-05", "CLOSE"), "REGULAR", registry()).status == "WITHHELD"
    assert pc.return_span_status("ENRG", pc.Anchor("2026-10-05", "OPEN"), pc.Anchor("2026-10-06", "CLOSE"),
                                 "REGULAR", registry(), ["2026-10-05"], pc.RAW_ACTUAL).reason == "MISSING_VERIFIED_SESSION"


def test_full_holding_phases_and_deleted_event_row():
    assert span(("2026-10-02", "OPEN"), ("2026-10-05", "OPEN")).reason == "CORPORATE_ACTION_BOUNDARY"
    assert span(("2026-10-02", "CLOSE"), ("2026-10-05", "CLOSE")).status == "WITHHELD"
    assert span(("2026-10-02", "CLOSE"), ("2026-10-09", "CLOSE")).status == "WITHHELD"
    assert span(("2026-10-05", "OPEN"), ("2026-10-05", "CLOSE")).status == "COMPARABLE"
    assert pc.return_span_status("ENRG", pc.Anchor("2026-10-02", "CLOSE"), pc.Anchor("2026-10-06", "OPEN"),
                                 "REGULAR", registry(), ["2026-10-02", "2026-10-06"], pc.RAW_ACTUAL).reason == "CORPORATE_ACTION_BOUNDARY"
    d = document()
    second = copy.deepcopy(d["events"][0])
    second.update(event_id="TEST:SECOND", effective_session="2026-10-07", reference_price="1440")
    d["events"].append(second)
    assert len(span(("2026-10-02", "CLOSE"), ("2026-10-09", "CLOSE"), pc.parse_registry(d)).event_ids) == 2


def evidenced(px):
    """The fixture plus the explicit restart window the trust contract requires."""
    from corporate_action_test_support import restart_evidence
    extended, axis = restart_evidence(px)
    return extended, axis, extended.attrs["restart_evidence"]


def test_observed_oo_exit_cc_gap_lag_and_oc_are_independent():
    from corporate_action_test_support import strip_restart_evidence
    px = frame()
    reg = registry()
    extended, axis, evidence = evidenced(px)
    result = pa.add_forward_returns(extended, axis, (1, 2, 3), extremes=True,
                                    open_anchored=True, registry=reg, representation=pc.RAW_ACTUAL)
    result = pa.add_lagged_returns(result, axis, (1, 2, 3), registry=reg, representation=pc.RAW_ACTUAL)
    result = strip_restart_evidence(result, evidence)
    by = result.set_index("date")
    assert by.loc["2026-10-05", "entry_open_admissible"]
    assert pd.isna(by.loc["2026-10-01", "fwd_oo_1"])
    assert by.loc["2026-10-01", "fwd_oo_1_reason"] == "CORPORATE_ACTION_BOUNDARY"
    for col in ["fwd_1", "gap_1", "fwd_2", "max_2", "mdd_2"]:
        assert pd.isna(by.loc["2026-10-02", col])
    for col in ["lag_1", "lag_2", "lag_3"]:
        assert pd.isna(by.loc["2026-10-05", col])
    assert by.loc["2026-10-02", "fwd_oc_1"] == pytest.approx(1030 / 1080 - 1)
    assert by.loc["2026-10-05", "lag_1_reason"] == "CORPORATE_ACTION_BOUNDARY"
    assert by.loc["2026-10-06", "limit_reference_price"] == 1030
    pd.testing.assert_frame_equal(result[px.columns], px)
    unknown = pa.add_forward_returns(px, px.date.tolist(), open_anchored=True)
    assert unknown.fwd_1.isna().all() and unknown.fwd_oc_1.isna().all()


def test_no_event_no_terp_and_independent_domain_defects():
    px = frame()
    d = document()
    d["events"] = []
    detected = pa.detect(px, registry=pc.parse_registry(d), representation=pc.RAW_ACTUAL)
    assert detected.loc[3, "limit_violation"]
    px.loc[3, "open"] = 0
    detected = pa.detect(px, representation=pc.RAW_ACTUAL)
    assert detected.loc[3, "domain_violation"] and detected.loc[3, "suspect"]
    assert not detected.loc[3, "entry_open_admissible"]


def test_quarantine_overlay_preserves_raw_and_independent_reasons():
    with sqlite3.connect(":memory:") as conn:
        frame().to_sql("price_history", conn, index=False)
        conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
        conn.execute("INSERT INTO price_quarantine VALUES('2026-10-05','ENRG','limit_violation')")
        result = pa.load_clean(conn, registry=registry(), representation=pc.RAW_ACTUAL)
        assert "2026-10-05" in result.date.tolist()
        assert result.loc[result.date.eq("2026-10-06"), "limit_reference_price"].iloc[0] == 1030
        conn.execute("UPDATE price_quarantine SET reasons='limit_violation+cross_ticker_dup'")
        assert "2026-10-05" not in pa.load_clean(conn, registry=registry(), representation=pc.RAW_ACTUAL).date.tolist()
        assert conn.execute("SELECT close FROM price_history WHERE date='2026-10-05'").fetchone()[0] == 1030
        conn.execute("UPDATE price_quarantine SET reasons=NULL")
        assert "2026-10-05" not in pa.load_clean(conn, representation=pc.RAW_ACTUAL).date.tolist()


def test_raja_remains_unknown_without_an_inferred_event():
    assert not default_registry().matching("RAJA", "REGULAR")
    r = reference("RAJA", "2025-08-25", previous=pc.PreviousActual("2025-08-22", 2710, "mixed"))
    assert r.status == "UNRESOLVED"
    assert pc.return_span_status("RAJA", pc.Anchor("2025-08-22", "CLOSE"), pc.Anchor("2025-08-25", "CLOSE"),
                                 "REGULAR", default_registry(), input_representation="MIXED").status == "WITHHELD"


def test_writer_audit_parity_domain_refusals_and_actual_netval():
    # This module supplies offline collector stand-ins and disposable schemas.
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    bars = frame().to_dict("records")
    for bar in bars:
        bar.pop("ticker")
    with price_db() as conn:
        body = price_payload(bars)
        before = copy.deepcopy(body)
        assert bf.insert_inventory(conn, "ENRG", body, representation=pc.RAW_ACTUAL)[1] == len(bars)
        pd.testing.assert_frame_equal(pa.load(conn)[frame().columns], frame(), check_dtype=False)
        assert body == before
        assert bf.insert_inventory(conn, "ENRG", body, representation=pc.RAW_ACTUAL)[1] == len(bars)
    for field, bad in [("open", 0), ("low", 1200), ("close", True), ("high", float("inf")),
                       ("volume", -1), ("close", 10 ** 400)]:
        body = price_payload([dict(bars[3], **{field: bad})])
        with price_db() as conn:
            with pytest.raises(bf.InventoryError):
                bf.insert_inventory(conn, "ENRG", body, representation=pc.RAW_ACTUAL)
            assert conn.total_changes == 0
    # Synthetic pre-cutoff event tests the proxy independently of October's
    # protected broker cutoff. This is never an asserted real ENRG event.
    doc = document()
    doc["events"] = [copy.deepcopy(doc["events"][0])]
    doc["events"][0].update(effective_session="2026-07-02", event_id="TEST:PROXY_EVENT")
    reg = pc.parse_registry(doc)
    with price_db() as conn:
        body = price_payload([price_bar("2026-07-01", 1440), price_bar("2026-07-02", 1030)])
        body["data"]["nlot"] = {"AK": [100, -100]}
        bf.insert_inventory(conn, "ENRG", body, registry=reg, representation=pc.RAW_ACTUAL)
        assert conn.execute("SELECT netval FROM broker_flow ORDER BY date").fetchall() == [(0.0144,), (-0.0103,)]
        assert conn.execute("SELECT close FROM price_history ORDER BY date").fetchall() == [(1440,), (1030,)]


def stored_disposition(capsys):
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return lines[-1]["price_dispositions"]


def test_unchanged_history_is_not_promoted_to_trusted_predecessor(capsys):
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    with price_db() as conn:
        conn.execute("INSERT INTO price_history VALUES ('2026-10-01','BBBB',1000,1000,1000,1000,1000)")
        conn.execute("INSERT INTO price_history VALUES ('2026-10-02','BBBB',500,500,500,500,1000)")
        # The stored -50% step is a violation; its later side never becomes a
        # reference. The next capture is checked for consistency only and kept
        # as unadjudicated restart evidence, never as a certified step.
        bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-10-05", 510)], "BBBB"))
        assert stored_disposition(capsys)[-1]["status"] == "RESTART_PENDING"
        out = pa.detect(pa.load(conn)).set_index("date")
        assert out.loc["2026-10-02", "limit_violation"] and not out.loc["2026-10-02", "close_anchor_admissible"]
        assert out.loc["2026-10-05", "limit_unresolved_reason"] == "UNTRUSTED_PREDECESSOR"
        assert out.loc["2026-10-05", "limit_reference_status"] == "UNRESOLVED"
        assert out.loc["2026-10-05", "consistency_status"] == "IN_BAND"
        assert not out.loc["2026-10-05", "limit_violation"]
        assert not out[["close_anchor_admissible", "price_step_admissible"]].any().any()

    # An independent event reference may admit its actual bar while the bad
    # predecessor remains unchanged and unavailable for ordinary comparison.
    with price_db() as conn:
        conn.execute("INSERT INTO price_history VALUES ('2026-10-02','ENRG',1440,1440,1440,0,1000)")
        event = frame().iloc[3].drop("ticker").to_dict()
        assert bf.insert_inventory(conn, "ENRG", price_payload([event]), representation=pc.RAW_ACTUAL)[1] == 1
        assert conn.execute("SELECT close FROM price_history WHERE date='2026-10-02'").fetchone()[0] == 0
    with price_db() as conn:
        conn.execute("INSERT INTO price_history VALUES ('2026-10-05','BBBB',2000,1000,1000,1000,1000)")
        bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-10-06", 1010)], "BBBB"))
        assert stored_disposition(capsys)[-1]["status"] == "SOURCE_CAPTURE_UNADJUDICATED"
        out = pa.detect(pa.load(conn)).set_index("date")
        assert out.loc["2026-10-05", "domain_violation"]
        assert out.loc["2026-10-06", "limit_unresolved_reason"] == "UNTRUSTED_PREDECESSOR"
        assert not out.close_anchor_admissible.any()


def test_writer_refuses_a_discontinuity_from_an_unadjudicated_predecessor():
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    with price_db() as conn:
        conn.executemany("INSERT INTO price_history VALUES (?,?,?,?,?,?,?)",
                         [("2026-10-01", "BBBB", 100, 100, 100, 100, 1000)])
        before = conn.total_changes
        with pytest.raises(bf.InventoryError, match="limit_violation.*UNADJUDICATED_PREVIOUS_ACTUAL_CLOSE"):
            bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-10-02", 180)], "BBBB"))
        assert conn.total_changes == before


def test_variable_exits_and_reset_stop_fills_remain_unavailable():
    import strategy_variants as sv
    import ara_arb_simulation as aa
    px, reg = frame(), registry()
    certified = pa.add_forward_returns(px, px.date.tolist(), open_anchored=True,
                                        registry=reg, representation=pc.RAW_ACTUAL)
    indexed, dates = sv._index_price_history(certified, registry=reg)
    assert sv.simulate_trade(indexed, dates, "ENRG", "2026-10-01", 2, None, .15, registry=reg) is None
    assert sv.simulate_trade(indexed, dates, "ENRG", "2026-10-02", 1, None, None, registry=reg) == pytest.approx(1030 / 1080 - 1)
    annotated = aa.annotate_limits(certified, registry=reg, representation=pc.RAW_ACTUAL)
    assert not annotated.loc[3, "at_arb"]
    # Delay a close exit to the event session. Even when admission and a
    # nominal target are maliciously retained, the actual interval must refuse.
    annotated["at_ara"] = False
    annotated["at_arb"] = False
    annotated.loc[2, "at_arb"] = True
    annotated["fwd_1"] = 0.01
    ix = {"ENRG": annotated}
    dm = {"ENRG": {d: i for i, d in enumerate(annotated.date)}}
    assert aa.simulate_trade_with_limits(ix, dm, "ENRG", "2026-10-01", registry=reg) is None


def test_episode_boundaries_and_projection_refusal():
    import ast
    from ddqn_episode_data import session_episode_ids
    from price_contract_frame import require_price_frame
    px = frame()
    ids = session_episode_ids(px, px.date.tolist())
    assert ids.iloc[3] > ids.iloc[2]
    removed = px.drop(index=3).reset_index(drop=True)
    ids = session_episode_ids(removed, removed.date.tolist())
    assert ids.iloc[3] > ids.iloc[2]  # explicit registry catches deletion too
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(px)
    audited = pa.add_forward_returns(px, px.date.tolist(), open_anchored=True)
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(audited.drop(columns="fwd_1"), ("fwd_1",))
    audited.attrs["price_contract"]["registry_sha256"] = "0" * 64
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(audited)
    # Execute the real constructor before any optional Torch imports. Removing
    # this guard must fail even on a host where the model cannot be imported.
    path = Path(__file__).with_name("ddqn_entry_exit.py")
    tree = ast.parse(path.read_text())
    env = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TickerEnv")
    constructor = next(n for n in env.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[constructor], type_ignores=[])), str(path), "exec"), namespace)
    with pytest.raises(pc.UnsupportedPriceContract, match="ddqn_entry_exit.TickerEnv"):
        namespace["__init__"](None, None, None, None, None)


def test_screener_capture_is_not_a_source_session():
    from neobdm_source_contract import price_source_context
    raw = price_source_context("2026-10-06")
    assert raw["session_status"] == "UNKNOWN" and raw["source_session"] is None
    evidence = {"source_document_id": "RECONSTRUCTED_VERIFIED_FIXTURE", "sha256": "1" * 64}
    valid = price_source_context("2026-10-06", source_session="2026-10-05", session_evidence=evidence,
                                 representation=pc.RAW_ACTUAL, representation_evidence=evidence)
    assert valid["session_status"] == "VERIFIED" and valid["source_session"] == "2026-10-05"
    unknown = reference(session="2026-10-06", input_representation=raw["input_representation"])
    assert unknown.status == "UNRESOLVED" and unknown.price is None


def test_all_registered_refusal_routes_stop_before_using_inputs():
    # Execute actual function bodies without optional model dependencies or a
    # live collector. Missing guards must fail this semantic refusal assertion.
    import ast
    import inspect
    root = Path(__file__).parent
    routes = json.loads((root / "corporate_action_consumer_routes.json").read_text())["routes"]
    expected = {"walk_forward_backtest.build_panel", "ddqn_episode_data.build_episode_frame",
                "daily_picks.run_morning", "evaluate_signals.outcome", "inventory_features.build",
                "arb_veto.score", "broker_book.ticker_bundle", "broker_rules.evaluate",
                "broker_learning.holder_returns", "broker_learning_db.live_summary",
                "experiment_1f_gate_b.execute_stage1", "experiment_2a0_event_study.event_return",
                "inventory_evidence._market_measurement", "targeted_actor_observations.observe",
                "pattern_backtest.simulate_trades", "foreign_flow_signal_backtest.generate_trades",
                "run_ml_reports.run_konglo_watch_report"}
    assert expected <= {f"{m}.{f}" for m, fs in routes.items() for f in fs}
    for module, names in routes.items():
        tree = ast.parse((root / f"{module}.py").read_text())
        for name in names:
            definition = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            definition.decorator_list = []
            definition.returns = None
            for parameter in (definition.args.posonlyargs + definition.args.args + definition.args.kwonlyargs):
                parameter.annotation = None
            # Defaults may depend on optional model globals; explicit sentinel
            # arguments ensure no inputs are accessed before the refusal.
            definition.args.defaults = [ast.Constant(None) for _ in definition.args.defaults]
            definition.args.kw_defaults = [ast.Constant(None) if d is not None else None for d in definition.args.kw_defaults]
            namespace = {}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[definition], type_ignores=[])), str(root / f"{module}.py"), "exec"), namespace)
            function = namespace[name]
            args, kwargs = [], {}
            for p in inspect.signature(function).parameters.values():
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                    args.append(None)
                elif p.kind == p.KEYWORD_ONLY:
                    kwargs[p.name] = None
            with pytest.raises(pc.UnsupportedPriceContract, match=f"{module}.{name}"):
                function(*args, **kwargs)


def test_frame_identity_changes_with_registry_and_actual_source(tmp_path, monkeypatch):
    import price_contract_frame as adapter
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document()))
    monkeypatch.setattr(adapter, "REGISTRY_PATH", path)
    px = adapter.annotate_prices(frame())
    old = px.attrs["price_contract"]["source_basis_snapshot_sha256"]
    changed = frame()
    changed.loc[0, "volume"] += 1
    assert adapter.annotate_prices(changed).attrs["price_contract"]["source_basis_snapshot_sha256"] != old
    doc = document()
    doc["registry_version"] += ".revoked"
    doc["events"][0].update(status="REVOKED", reference_price=None)
    path.write_text(json.dumps(doc))
    with pytest.raises(pc.UnsupportedPriceContract):
        adapter.require_price_frame(px)


def test_integrity_reports_boundaries_with_zero_contamination():
    import check_signal_integrity as integrity
    with sqlite3.connect(":memory:") as conn:
        frame().to_sql("price_history", conn, index=False)
        problems, notes, stats = [], [], {}
        integrity.check_new_contamination(conn, problems, notes, stats)
        assert stats["fresh_suspects"] == 0 and stats["corporate_action_boundaries"] == 1
        assert any("boundary" in note for note in notes)
        integrity.check_price_contract(conn, problems, notes, stats, registry=registry(), representation=pc.RAW_ACTUAL)
        assert not problems and stats["withheld_price_labels"] > 0


def test_invalid_calendar_identity_and_quarantined_baselines_refuse_before_writes(capsys):
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    for ticker, day in [("ENRG", "2025-08-25"), ("ENRG", "2026-10-04"), ("INVALID", "2026-10-05")]:
        with price_db() as conn:
            with pytest.raises(bf.InventoryError):
                bf.insert_inventory(conn, ticker, price_payload([price_bar(day, 1030)], ticker))
            assert conn.total_changes == 0
    with price_db() as conn:
        conn.execute("INSERT INTO price_history VALUES ('2026-10-05','BBBB',1000,1000,1000,1000,1000)")
        conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
        conn.execute("INSERT INTO price_quarantine VALUES ('2026-10-05','BBBB','cross_ticker_dup')")
        # +80% versus the quarantined close: it is never the baseline, so the new
        # capture is neither compared with it nor refused because of it.
        bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-10-06", 1800)], "BBBB"))
        disposition = stored_disposition(capsys)[-1]
        assert disposition["status"] == "SOURCE_CAPTURE_UNADJUDICATED"
        assert disposition["consistency_reference_price"] is None
        audited, _, _ = pa.adjudicate_quarantine(conn)
        row = audited.set_index("date").loc["2026-10-06"]
        assert row.limit_unresolved_reason == "UNTRUSTED_PREDECESSOR" and not row.limit_violation
        assert not row.close_anchor_admissible


def test_empty_price_history_produces_no_certified_outputs():
    from test_inventory_capture import price_db
    with price_db() as conn:
        px = pa.clean_panel(conn, open_anchored=True, lags=(1,))
        assert px.empty and "fwd_oo_1" in px and "lag_1" in px
        assert px.attrs["price_contract"]["input_representation"] == "UNKNOWN"


def test_unresolved_outcomes_cannot_be_coerced_to_boolean_approval():
    ref = reference(input_representation="UNKNOWN")
    admission = pc.validate_actual_price(1030, ref)
    held = span(("2026-10-02", "CLOSE"), ("2026-10-05", "OPEN"))
    for result in (ref, admission, held):
        with pytest.raises(TypeError, match="explicit status"):
            bool(result)


def test_suspension_observations_do_not_create_daily_comparisons_or_bridges():
    px = frame()
    px.loc[4, "volume"] = 0
    result = pa.add_forward_returns(px, px.date.tolist(), (1, 2), open_anchored=True,
                                    registry=registry(), representation=pc.RAW_ACTUAL)
    assert pd.isna(result.loc[3, "fwd_1"]) and pd.isna(result.loc[3, "fwd_oo_1"])
    assert pd.isna(result.loc[3, "fwd_2"])
    assert result.loc[5, "limit_unresolved_reason"] == "UNTRUSTED_PREDECESSOR"


def test_oo_exit_open_does_not_depend_on_its_later_close():
    px = frame()
    px.loc[5, "close"] = 0
    result = pa.add_forward_returns(px, px.date.tolist(), open_anchored=True,
                                    registry=registry(), representation=pc.RAW_ACTUAL)
    assert result.loc[5, "domain_violation"]
    assert result.loc[5, "entry_open_admissible"]
    assert result.loc[3, "fwd_oo_1"] == pytest.approx(1055 / 1040 - 1)
    assert pd.isna(result.loc[4, "fwd_1"])
    assert pd.isna(result.loc[1, "fwd_oo_1"])
    assert result.loc[1, "fwd_oo_1_reason"] == "CORPORATE_ACTION_BOUNDARY"


def test_freshness_checks_each_ticker_against_verified_sessions(monkeypatch):
    import check_signal_integrity as integrity
    from datetime import datetime

    class ReviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, 10, tzinfo=tz)

    monkeypatch.setattr(integrity, "datetime", ReviewClock)
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE price_history(date, ticker)")
        conn.executemany("INSERT INTO price_history VALUES(?,?)", [
            ("2026-10-08", "AAAA"), ("2026-10-02", "BBBB"), ("2026-10-04", "CCCC"),
            ("2028-01-03", "DDDD")])
        for table in ("market_summary_daily", "broker_flow"):
            conn.execute(f"CREATE TABLE {table}(date)")
            conn.execute(f"INSERT INTO {table} VALUES('2026-10-08')")
        problems = []
        integrity.check_freshness(conn, problems)
        assert any("BBBB STALE" in p and "verified sessions missing" in p for p in problems)
        assert any("CCCC" in p and "not a verified session" in p for p in problems)
        assert any("DDDD" in p and "UNRESOLVED" in p for p in problems)
        assert not any("AAAA" in p for p in problems)
