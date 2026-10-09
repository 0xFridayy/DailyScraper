"""Adversarial regression fixtures; reconstructed observations, never live data."""

import copy
from datetime import datetime, timezone
import sqlite3
import importlib
import inspect
import runpy
from types import SimpleNamespace

import pandas as pd
import pytest

import price_contract as pc
import price_audit as pa
from price_contract_frame import annotate_prices, require_price_frame
from test_price_contract import document, frame, registry


@pytest.mark.parametrize("events", [{}, "", None, 1, [None], ["event"], [False]])
def test_f01_invalid_collection_is_not_an_empty_registry(events):
    doc = document()
    doc["events"] = events
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)


def test_f01_partial_revision_and_conflicting_records_are_atomic():
    for bad in [None, {}, copy.deepcopy(document()["events"][0])]:
        doc = document()
        doc["events"].append(bad)
        with pytest.raises(pc.PriceContractError):
            pc.parse_registry(doc)


@pytest.mark.parametrize("field,value", [
    ("source", []), ("source_document_id", 123), ("event_id", True),
    ("evidence_refs", ["https://example.invalid/live"]),
])
def test_f02_confirmed_identity_has_strict_types(field, value):
    doc = document()
    doc["events"][0][field] = value
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)


@pytest.mark.parametrize("mutation", [
    lambda e: e["source"].update(author=True),
    lambda e: e["source"].pop("published_on"),
    lambda e: e["source"].pop("retrieval_medium"),
    lambda e: e["evidence_refs"].update(report_content_sha256="bad"),
    lambda e: e.update(evidence_refs={"url": "https://example.invalid/live"}),
])
def test_f02_incomplete_provenance_cannot_authorize_reference(mutation):
    doc = document()
    mutation(doc["events"][0])
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)


def labeled(**kwargs):
    px = frame()
    return pa.add_forward_returns(px, px.date.tolist(), (1, 2), extremes=True,
                                  open_anchored=True, registry=registry(),
                                  representation=pc.RAW_ACTUAL, **kwargs)


def test_f03_old_labels_cannot_gain_new_registry_identity():
    px = labeled()
    doc = document()
    doc["reviewed_coverage"] = document_coverage()
    event = copy.deepcopy(doc["events"][0])
    event.update(event_id="SYNTHETIC:SECOND", effective_session="2026-10-08", reference_price="1070")
    doc["events"].append(event)
    new = pc.parse_registry(doc)
    with pytest.raises(pc.UnsupportedPriceContract):
        pa.add_lagged_returns(px, px.date.tolist(), registry=new, representation=pc.RAW_ACTUAL)
    fresh = pa.add_forward_returns(frame(), px.date.tolist(), registry=new, representation=pc.RAW_ACTUAL)
    assert pd.isna(fresh.loc[fresh.date.eq("2026-10-07"), "fwd_1"]).all()


def document_coverage():
    return [{"venue": "IDX", "market_scope": ["REGULAR"], "tickers": ["ENRG"],
             "from": "2026-09-30", "through": "2026-10-09",
             "evidence_refs": ["RECONSTRUCTED_TEST_COVERAGE_ONLY"]}]


def test_f03_annotation_does_not_certify_legacy_crossing_labels():
    px = frame()
    px["fwd_1"] = 1030 / 1440 - 1
    px.attrs["price_contract"] = registry().identity | {"input_representation": pc.RAW_ACTUAL}
    with pytest.raises(pc.UnsupportedPriceContract):
        annotate_prices(px, registry=registry(), representation=pc.RAW_ACTUAL)


def test_f03_augmentation_preserves_information_cutoff():
    cutoff = datetime(2026, 10, 9, tzinfo=timezone.utc)
    px = labeled(as_of=cutoff)
    out = pa.add_lagged_returns(px, px.date.tolist(), registry=registry())
    assert out.attrs["price_contract"]["knowledge_mode"] == "AS_OF"
    assert out.attrs["price_contract"]["as_of"] == cutoff.isoformat()


@pytest.mark.parametrize("field,value", [
    ("open", 1), ("high", 2000), ("low", 1), ("close", 1),
    ("date", "2026-10-09"), ("ticker", "SINI"),
    ("price_step_admissible", False), ("fwd_1", 99),
])
def test_f04_certificate_is_bound_to_observations_and_outputs(field, value):
    px = labeled()
    px.loc[4, field] = value
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(px, ("fwd_1",), registry=registry())


def test_f04_filtering_valid_rows_keeps_valid_certificates():
    px = labeled().iloc[4:].reset_index(drop=True)
    assert require_price_frame(px, ("fwd_1",), registry=registry())


@pytest.mark.parametrize("missing", ["open", "high", "low", "volume"])
def test_f05_projection_cannot_recertify_incomplete_bars(missing):
    px = labeled().drop(columns=missing)
    with pytest.raises(pc.UnsupportedPriceContract):
        pa.add_lagged_returns(px, px.date.tolist(), registry=registry())


@pytest.mark.parametrize("field,value", [("high", 2000), ("low", 500)])
def test_f05_extrema_require_full_bar_reference_admission(field, value):
    px = frame()
    px.loc[5, field] = value
    out = pa.add_forward_returns(px, px.date.tolist(), extremes=True,
                                 registry=registry(), representation=pc.RAW_ACTUAL)
    assert not out.loc[5, "price_step_admissible"]
    assert pd.isna(out.loc[4, "max_1"]) and pd.isna(out.loc[4, "mdd_1"])


def test_f07_pending_event_cannot_back_ordinary_session_or_labels():
    px = pd.DataFrame([
        ["2026-07-08", "SINI", 10950, 10950, 10950, 10950, 1000],
        ["2026-07-09", "SINI", 8100, 8100, 8100, 8100, 1000],
        ["2026-07-10", "SINI", 8150, 8150, 8150, 8150, 1000],
    ], columns=frame().columns)
    reg = registry("SINI", "2026-07-08", "2026-07-10")
    out = pa.add_forward_returns(px, px.date.tolist(), registry=reg, representation=pc.RAW_ACTUAL)
    assert out.loc[1, "limit_unresolved_reason"] == "PENDING_REFERENCE"
    assert out.loc[2, "limit_unresolved_reason"] == "UNTRUSTED_PREDECESSOR"
    assert pd.isna(out.loc[1, "fwd_1"])


def test_f08_unknown_event_input_cannot_receive_official_admission():
    out = pa.detect(frame(), registry=registry(), representation="UNKNOWN")
    event = out.loc[out.date.eq("2026-10-05")].iloc[0]
    assert event.limit_reference_status == "UNRESOLVED"
    assert pd.isna(event.limit_reference_price)
    assert not event.entry_open_admissible and not event.price_step_admissible


def test_f13_recovered_quarantine_has_shared_writer_and_audit_trust():
    from test_inventory_capture import bf, price_db, price_payload
    with price_db() as conn:
        bars = frame().iloc[:4].drop(columns="ticker").to_dict("records")
        bf.insert_inventory(conn, "ENRG", price_payload(bars), representation=pc.RAW_ACTUAL)
        conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
        conn.execute("INSERT INTO price_quarantine VALUES('2026-10-05','ENRG','limit_violation')")
        next_bar = frame().iloc[4:5].drop(columns="ticker").to_dict("records")
        assert bf.insert_inventory(conn, "ENRG", price_payload(next_bar), representation=pc.RAW_ACTUAL)[1] == 1
        bad = copy.deepcopy(next_bar)
        bad[0].update(open=1800, high=1810, low=1790, close=1800)
        with pytest.raises(bf.InventoryError):
            bf.insert_inventory(conn, "ENRG", price_payload(bad), representation=pc.RAW_ACTUAL)
        clean = pa.load_clean(conn, registry=registry(), representation=pc.RAW_ACTUAL)
        assert "2026-10-05" in clean.date.tolist()


def test_f14_duplicate_identity_is_not_a_predecessor():
    px = pd.DataFrame([
        ["2026-10-01", "MDIA", 95, 95, 95, 95, 1000],
        ["2026-10-01", "MDIA", 200, 200, 200, 200, 2000],
        ["2026-10-02", "MDIA", 210, 210, 210, 210, 2100],
    ], columns=frame().columns)
    out = pa.detect(px, representation=pc.RAW_ACTUAL)
    assert out.iloc[-1].limit_reference_status == "UNRESOLVED"
    assert not out.iloc[-1].limit_violation

    # A unique official anchor supplies trust even before a restart window.
    # Duplicating that identity must withdraw its successor reference.
    unique = frame()
    clean = pa.detect(unique, registry=registry(), representation=pc.RAW_ACTUAL)
    assert clean.loc[clean.date.eq("2026-10-06"), "close_anchor_admissible"].all()
    duplicate = unique.loc[unique.date.eq("2026-10-05")]
    duplicated = pd.concat([unique, duplicate], ignore_index=True)
    audited = pa.detect(duplicated, registry=registry(), representation=pc.RAW_ACTUAL)
    successor = audited.loc[audited.date.eq("2026-10-06")].iloc[0]
    assert successor.limit_reference_status == "UNRESOLVED"
    assert not successor.close_anchor_admissible


def test_f15_missing_event_row_does_not_join_median_segments():
    doc = document()
    doc["events"][0]["reference_price"] = "100"
    reg = pc.parse_registry(doc)
    dates = ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28",
             "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
             "2026-10-06", "2026-10-07", "2026-10-08"]
    px = pd.DataFrame([[d, "ENRG", *([1000 if d < "2026-10-05" else 100] * 4), 1000]
                       for d in dates], columns=frame().columns)
    out = pa.detect(px, registry=reg, representation=pc.RAW_ACTUAL)
    after = out.loc[out.date.gt("2026-10-05")]
    assert after.price_segment_id.eq(1).all()
    assert not after.series_break.any()


@pytest.mark.parametrize("revision", [
    {"open": 200, "high": 200}, {"high": 200}, {"low": 50}, {"volume": 0},
])
def test_f06_unchanged_close_does_not_grandfather_changed_fields(revision):
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    with price_db() as conn:
        bars = [price_bar("2026-07-01", 100), price_bar("2026-07-02", 100)]
        bf.insert_inventory(conn, "BBBB", price_payload(bars, "BBBB"))
        changed = copy.deepcopy(bars[-1])
        changed.update(revision)
        before = conn.total_changes
        with pytest.raises(bf.InventoryError):
            bf.insert_inventory(conn, "BBBB", price_payload([changed], "BBBB"))
        assert conn.total_changes == before


def test_f06_predecessor_revision_revalidates_stored_successor_ohlc():
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    with price_db() as conn:
        bars = [price_bar("2026-07-01", 100), price_bar("2026-07-02", 100),
                price_bar("2026-07-03", 100)]
        bars[-1].update(open=130, high=130)
        bf.insert_inventory(conn, "BBBB", price_payload(bars, "BBBB"))
        before = conn.total_changes
        with pytest.raises(bf.InventoryError):
            bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-07-02", 90)], "BBBB"))
        assert conn.total_changes == before


def strategy_trade(px, decision, hold=1, tp=None, sl=None):
    import strategy_variants as sv
    from corporate_action_test_support import restart_evidence, strip_restart_evidence
    extended, axis = restart_evidence(px)
    certified = pa.add_forward_returns(extended, axis, open_anchored=True,
                                       registry=registry(), representation=pc.RAW_ACTUAL)
    certified = strip_restart_evidence(certified, extended.attrs["restart_evidence"])
    ix, dates = sv._index_price_history(certified, registry=registry())
    return sv.simulate_trade(ix, dates, "ENRG", decision, hold, tp, sl, registry=registry())


def test_f09_rejected_same_session_close_cannot_be_payoff():
    px = frame()
    px.loc[5, "close"] = 0
    assert strategy_trade(px, "2026-10-06") is None


def limit_trade(px, entry):
    import ara_arb_simulation as aa
    out = pa.add_forward_returns(px, px.date.tolist(), registry=registry(), representation=pc.RAW_ACTUAL)
    out = aa.annotate_limits(out, registry=registry(), representation=pc.RAW_ACTUAL)
    ix = {"ENRG": out}
    dates = {"ENRG": {d: i for i, d in enumerate(out.date)}}
    return aa.simulate_trade_with_limits(ix, dates, "ENRG", entry, registry=registry())


def test_f09_delayed_exit_final_non_arb_bar_requires_admission():
    px = frame().iloc[3:6].reset_index(drop=True)
    px.loc[1, ["open", "high", "low", "close"]] = [875, 880, 875, 875]
    px.loc[2, ["open", "high", "low", "close"]] = [2000, 2000, 2000, 2000]
    assert limit_trade(px, "2026-10-05") is None


def test_f10_incomplete_timed_hold_is_not_shortened():
    px = frame().iloc[:3].copy()
    assert strategy_trade(px, "2026-10-01", hold=2) is None
    assert strategy_trade(px, "2026-10-01", hold=1) == pytest.approx(1440 / 1425 - 1)


def test_f10_real_early_barrier_exit_does_not_need_later_timed_bar():
    px = frame().iloc[:3].copy()
    assert strategy_trade(px, "2026-10-01", hold=2, tp=.005) == pytest.approx(.005)


def test_f17_unrelated_history_does_not_disable_valid_local_trade():
    assert limit_trade(frame(), "2026-10-05") == pytest.approx((1050 / 1030 - 1, 1))
    assert limit_trade(frame().iloc[3:].reset_index(drop=True), "2026-10-05") == pytest.approx((1050 / 1030 - 1, 1))


def test_f17_unresolved_inside_required_span_still_withholds():
    px = frame()
    px.loc[4, "volume"] = 0
    assert limit_trade(px, "2026-10-05") is None


def test_f18_first_complete_extrema_window_and_incomplete_tail():
    from corporate_action_test_support import restart_evidence, strip_restart_evidence
    px = frame().iloc[4:].reset_index(drop=True)
    extended, axis = restart_evidence(px)
    out = pa.add_forward_returns(extended, axis, (2,), extremes=True,
                                 registry=registry(), representation=pc.RAW_ACTUAL)
    out = strip_restart_evidence(out, extended.attrs["restart_evidence"])
    assert out.loc[0, "max_2"] == pytest.approx(1080 / 1050 - 1)
    assert out.loc[0, "mdd_2"] == pytest.approx(1040 / 1050 - 1)
    assert pd.isna(out.loc[2, "max_2"]) and pd.isna(out.loc[2, "mdd_2"])


def test_f11_real_model_api_refuses_unversioned_reset_targets():
    import ml_v2_experiment_1 as ml
    panel = pd.DataFrame({"ticker": ["ENRG"] * 6, "date": frame().date.iloc[:6],
                          "x": [0., 1.] * 3, "target": [1030 / 1440 - 1] * 6})
    splits = [{"fit": panel.date[:2].tolist(), "eval": panel.date[2:4].tolist(),
               "test": panel.date[4:].tolist()}]
    with pytest.raises(pc.UnsupportedPriceContract):
        ml.run_feature_set(panel, ["x"], splits)


@pytest.mark.parametrize("module,name", [
    ("ml_v2_experiment_1", "build_broker_identity_features"),
    ("strategy_variants", "get_walk_forward_predictions"),
    ("strategy_variants", "run_strategy_search"),
    ("feature_ablation", "walk_forward"), ("horizon_scan", "walk_forward"),
    ("multiday_features", "walk_forward"), ("txchart_backtest", "walk_forward"),
    ("daily_picks", "tag_snapshot"), ("daily_picks", "rank_picks"),
    ("daily_picks", "format_morning"), ("daily_picks", "format_scoreboard"),
    ("broker_learning_run", "ticker_ctx"), ("broker_learning_run", "brokers_ctx"),
    ("broker_rules", "window_cost"), ("broker_book", "average_cost_run"),
])
def test_f11_direct_call_refuses_before_accessing_unversioned_input(module, name):
    fn = getattr(importlib.import_module(module), name)
    args, kwargs = [], {}
    for p in inspect.signature(fn).parameters.values():
        if p.default is not inspect.Parameter.empty:
            continue
        if p.kind == p.KEYWORD_ONLY:
            kwargs[p.name] = None
        else:
            args.append(None)
    with pytest.raises(pc.UnsupportedPriceContract, match=f"{module}.{name}"):
        fn(*args, **kwargs)


def test_f11_veto_persistence_requires_current_contract(tmp_path):
    import arb_veto
    target = tmp_path / "picks.db"
    top = pd.DataFrame({"ticker": ["ENRG"], "p": [.999]})
    with pytest.raises(pc.UnsupportedPriceContract):
        arb_veto.write("2026-10-05", top, picks_db=str(target))
    assert not target.exists()


@pytest.mark.parametrize("module", ["horizon_scan", "feature_ablation", "multiday_features"])
def test_f12_cli_refusal_creates_no_database(module, tmp_path, monkeypatch):
    import walk_forward_backtest as wfb
    target = tmp_path / "missing.db"
    monkeypatch.setattr(wfb, "DB_PATH", str(target))
    with pytest.raises(pc.UnsupportedPriceContract):
        runpy.run_module(module, run_name="__main__")
    assert list(tmp_path.iterdir()) == []


def test_f12_weekly_inner_wrapper_does_not_commit_on_refusal(tmp_path):
    import broker_learning_run as run
    import broker_learning_db as db
    with sqlite3.connect(":memory:") as conn:
        db.ensure_schema(conn)
        before = conn.total_changes
        args = SimpleNamespace(tickers="ENRG", no_fetch=True, raw_dir=str(tmp_path), legacy_cache=True,
                               history_dir=str(tmp_path / "history"))
        with pytest.raises(pc.UnsupportedPriceContract):
            run._weekly(args, conn, {}, "2026-10-07T00:00:00+00:00", 0)
        assert conn.total_changes == before
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("column", ["fwd_1", "max_1", "mdd_1", "fwd_oo_1", "lag_1", "gap_1"])
def test_f16_integrity_checks_actual_spans_with_blank_producer_reasons(column, monkeypatch):
    import check_signal_integrity as integrity
    with sqlite3.connect(":memory:") as conn:
        frame().to_sql("price_history", conn, index=False)
        px = labeled()
        px = pa.add_lagged_returns(px, px.date.tolist(), registry=registry())
        day = "2026-10-01" if column == "fwd_oo_1" else "2026-10-05" if column == "lag_1" else "2026-10-02"
        px.loc[px.date.eq(day), column] = 1030 / 1440 - 1
        if column + "_reason" in px:
            px.loc[px.date.eq(day), column + "_reason"] = ""
        monkeypatch.setattr(pa, "clean_panel", lambda *a, **k: px)
        problems, notes, stats = [], [], {}
        integrity.check_price_contract(conn, problems, notes, stats, registry=registry(), representation=pc.RAW_ACTUAL)
        assert any(column in message for message in problems)


def test_f03_changed_trust_cannot_reseal_existing_labels():
    px = labeled()
    with pytest.raises(pc.UnsupportedPriceContract):
        annotate_prices(px, registry=registry(), trusted=[False] * len(px))


def test_f03_changed_session_axis_cannot_reuse_old_labels():
    px = labeled()
    changed_axis = [d for d in px.date if d != "2026-10-06"]
    with pytest.raises(pc.UnsupportedPriceContract):
        pa.add_lagged_returns(px, changed_axis, registry=registry())


@pytest.mark.parametrize("column", ["target", "daily_return", "payoff", "label", "y", "rule_ret"])
def test_f03_annotation_does_not_certify_arbitrary_cached_outputs(column):
    px = frame()
    px[column] = 1030 / 1440 - 1
    out = annotate_prices(px, registry=registry(), representation=pc.RAW_ACTUAL)
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(out, (column,), registry=registry())


def test_f04_appending_a_label_does_not_add_it_to_the_certificate():
    px = labeled()
    px["fwd_3"] = 99
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(px, ("fwd_3",), registry=registry())


def test_f04_duplicate_replay_is_not_a_valid_frame():
    px = labeled()
    replay = pd.concat([px.iloc[:1], px], ignore_index=True)
    replay.attrs = copy.deepcopy(px.attrs)
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(replay, registry=registry())


@pytest.mark.parametrize("identity", [True, 123, ["fake"], {"fake": "fake"}, "UNKNOWN"])
def test_f08_malformed_source_evidence_does_not_verify_session(identity):
    from neobdm_source_contract import price_source_context
    evidence = {"source_document_id": identity, "sha256": "1" * 64}
    out = price_source_context("2026-10-06", source_session="2026-10-05", session_evidence=evidence,
                               representation=pc.RAW_ACTUAL, representation_evidence=evidence)
    assert out["session_status"] == "UNKNOWN" and out["input_representation"] == "UNKNOWN"


def test_f13_independent_duplicates_cannot_be_recovered_by_the_writer():
    from test_inventory_capture import bf, price_db, price_payload
    with price_db() as conn:
        bars = frame().iloc[:4].drop(columns="ticker").to_dict("records")
        bf.insert_inventory(conn, "ENRG", price_payload(bars), representation=pc.RAW_ACTUAL)
        bar = bars[-1]
        conn.execute("INSERT INTO price_history VALUES (?,?,?,?,?,?,?)",
                     (bar["date"], "AAAA", bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]))
        conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
        conn.execute("INSERT INTO price_quarantine VALUES('2026-10-05','ENRG','limit_violation')")
        # The duplicated event bar is never recovered as a reference, so its
        # successor is only an unadjudicated capture: no comparison, no trust.
        bf.insert_inventory(conn, "ENRG", price_payload(frame().iloc[4:5].drop(columns="ticker").to_dict("records")),
                            representation=pc.RAW_ACTUAL)
        audited, quarantined, _ = pa.adjudicate_quarantine(conn, registry=registry(), representation=pc.RAW_ACTUAL)
        assert ("2026-10-05", "ENRG") in quarantined
        enrg = audited[audited.ticker.eq("ENRG")].set_index("date")
        assert enrg.loc["2026-10-05", "anchor_trust_reason"] == "CROSS_TICKER_DUPLICATE"
        assert enrg.loc["2026-10-06", "limit_unresolved_reason"] == "UNTRUSTED_PREDECESSOR"
        assert not enrg.loc[["2026-10-05", "2026-10-06"], "close_anchor_admissible"].any()
        assert not enrg.loc["2026-10-06", "price_step_admissible"]


def test_f14_direct_label_builder_does_not_chain_duplicate_identity():
    px = frame().iloc[3:5].copy()
    duplicate = px.iloc[:1].copy()
    duplicate.loc[:, ["open", "high", "low", "close"]] = 1200
    px = pd.concat([px.iloc[:1], duplicate, px.iloc[1:]], ignore_index=True)
    out = pa.add_forward_returns(px, sorted(px.date.unique()), registry=registry(), representation=pc.RAW_ACTUAL)
    assert out.iloc[-1].limit_reference_status == "UNRESOLVED"
    assert out.fwd_1.isna().all()


def test_f09_stale_date_index_cannot_select_another_payoff():
    import strategy_variants as sv
    px = labeled()
    ix, dates = sv._index_price_history(px, registry=registry())
    dates["ENRG"]["2026-10-01"] = 4
    assert sv.simulate_trade(ix, dates, "ENRG", "2026-10-01", 1, None, None, registry=registry()) is None


@pytest.mark.parametrize("value", ["1_065", " 1065 ", "+1065"])
def test_f02_reference_text_is_not_coerced(value):
    doc = document()
    doc["events"][0]["reference_price"] = value
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)


@pytest.mark.parametrize("field,value", [
    ("source_identity", "UNKNOWN"), ("source_document_id", True),
    ("source_session", "UNKNOWN"), ("source_session", "2026-10-02"),
    ("source_session_status", "UNKNOWN"), ("representation", "MIXED"),
    ("input_representation", "UNKNOWN"),
])
def test_f08_explicit_unknown_or_contradictory_source_metadata_refuses_event(field, value):
    px = frame()
    px[field] = value
    out = annotate_prices(px, registry=registry(), representation=pc.RAW_ACTUAL)
    event = out.loc[out.date.eq("2026-10-05")].iloc[0]
    assert event.limit_reference_status != "RESOLVED"
    assert not event.price_step_admissible
    assert not event.entry_open_admissible
    assert not event.close_anchor_admissible


@pytest.mark.parametrize("column", ["at_ara", "at_arb", "suspect", "prev_close", "pct_chg"])
def test_f03_annotation_does_not_own_uncomputed_derived_flags(column):
    px = frame()
    px[column] = False
    out = annotate_prices(px, registry=registry(), representation=pc.RAW_ACTUAL)
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(out, (column,), registry=registry())


def test_f03_closed_forward_labels_do_not_certify_cached_open_admission():
    px = frame()
    px["next_entry_open_admissible"] = True
    out = pa.add_forward_returns(px, px.date.tolist(), registry=registry(), representation=pc.RAW_ACTUAL)
    with pytest.raises(pc.UnsupportedPriceContract):
        require_price_frame(out, ("next_entry_open_admissible",), registry=registry())


def test_f09_limit_simulator_rejects_stale_decision_index():
    import ara_arb_simulation as aa
    px = aa.annotate_limits(labeled(), registry=registry(), representation=pc.RAW_ACTUAL)
    dates = {d: i for i, d in enumerate(px.date)}
    dates["2026-10-01"] = 4
    assert aa.simulate_trade_with_limits({"ENRG": px}, {"ENRG": dates}, "ENRG", "2026-10-01", registry=registry()) is None


@pytest.mark.parametrize("tp,sl", [(-.5, None), (None, -.5), (float("nan"), None), (None, 1)])
def test_f09_invalid_barrier_distances_cannot_fabricate_payoff(tp, sl):
    with pytest.raises(ValueError):
        strategy_trade(frame(), "2026-10-06", tp=tp, sl=sl)


# ── F02/F08: placeholder-shaped provenance and incoherent chronology ──

EMPTY_CONTENT_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.mark.parametrize("mutation", [
    *[(lambda token: lambda e: e["source"].update(author=token))(t)
      for t in ("UNKNOWN", "N/A", "TBD", "PLACEHOLDER", "-", "unknown", "Unknown author", "--")],
    lambda e: e["source"].update(retrieval_medium="TBD"),
    *[(lambda url: lambda e: e["source"].update(url=url))(u)
      for u in ("N/A", "https://", "ftp://exchange.example/doc", "https://UNKNOWN", "not a url",
                "https://www.idx.co.id/tbd")],
    *[(lambda doc: lambda e: e.update(source_document_id=doc))(d) for d in ("UNKNOWN", "TBD", "-", "0000", "N/A")],
    lambda e: e.update(event_id="PLACEHOLDER"),
    lambda e: e.update(notes="TBD"),
    *[(lambda digest: lambda e: e["evidence_refs"].update(report_content_sha256=digest))(h)
      for h in ("0" * 64, "f" * 64, EMPTY_CONTENT_SHA256, "0123456789abcdef" * 4, "ab" * 32)],
    lambda e: e["evidence_refs"].update(investigation_report="N/A"),
    lambda e: e["evidence_refs"].update(issuer_document="UNKNOWN"),
])
def test_f02_placeholder_provenance_cannot_confirm_reference(mutation):
    doc = document()
    mutation(doc["events"][0])
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)


@pytest.mark.parametrize("mutation", [
    # Published after it was observed.
    lambda e: e["source"].update(published_on="2026-10-07"),
    # Observed after it was verified.
    lambda e: e.update(observed_at={"date": "2026-10-07", "precision": "DAY", "timezone": "Asia/Jakarta"}),
    # Enrolled before the verification day began in Jakarta.
    lambda e: e.update(enrolled_at={"timestamp": "2026-10-05T16:59:59+00:00", "precision": "INSTANT",
                                    "timezone": "UTC"}),
    # An instant verification strictly before the observation day.
    lambda e: e.update(verified_at={"timestamp": "2026-10-05T16:00:00+00:00", "precision": "INSTANT",
                                    "timezone": "UTC"}),
    # A confirmed reference needs known observation and verification clocks.
    lambda e: e.update(observed_at={"precision": "UNKNOWN"}),
    lambda e: e.update(verified_at={"precision": "UNKNOWN"}),
    lambda e: e["source"].pop("published_on"),
    lambda e: e["source"].update(published_on="2026/10/02"),
])
def test_f02_incoherent_chronology_cannot_confirm_reference(mutation):
    doc = document()
    mutation(doc["events"][0])
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)


@pytest.mark.parametrize("observed,verified,enrolled", [
    # Date-only clocks are intervals: same-day observation and verification agree.
    ({"date": "2026-10-06", "precision": "DAY", "timezone": "Asia/Jakarta"},
     {"timestamp": "2026-10-06T03:00:00+00:00", "precision": "INSTANT", "timezone": "UTC"},
     {"timestamp": "2026-10-06T03:00:00+00:00", "precision": "INSTANT", "timezone": "UTC"}),
    ({"date": "2026-10-06", "precision": "DAY", "timezone": "Asia/Jakarta"},
     {"date": "2026-10-06", "precision": "DAY", "timezone": "Asia/Jakarta"},
     {"timestamp": "2026-10-05T17:00:00+00:00", "precision": "INSTANT", "timezone": "UTC"}),
])
def test_f02_precision_aware_chronology_does_not_invent_contradictions(observed, verified, enrolled):
    doc = document()
    doc["events"][0].update(observed_at=observed, verified_at=verified, enrolled_at=enrolled)
    event = pc.parse_registry(doc).matching("ENRG", "REGULAR", "2026-10-05")[0]
    # Availability stays conservative: never before the whole observed day ends.
    assert event.available_at >= datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)


def test_f02_pending_records_cannot_carry_placeholder_provenance():
    doc = document()
    doc["events"][1]["source"]["author"] = "UNKNOWN"
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)
    doc = document()
    doc["events"][1]["evidence_refs"]["audit_sha256"] = "0" * 64
    with pytest.raises(pc.PriceContractError):
        pc.parse_registry(doc)


@pytest.mark.parametrize("evidence", [
    {"source_document_id": "VERIFIED_SESSION_EVIDENCE_FIXTURE", "sha256": "0" * 64},
    {"source_document_id": "VERIFIED_SESSION_EVIDENCE_FIXTURE", "sha256": "1" * 64},
    {"source_document_id": "VERIFIED_SESSION_EVIDENCE_FIXTURE", "sha256": EMPTY_CONTENT_SHA256},
    {"source_document_id": "N/A", "sha256": "5f2c" * 16},
    {"source_document_id": "TBD", "sha256": "9b1f6c2e" * 8},
    {"source_document_id": "-", "sha256": "9b1f6c2e" * 8},
])
def test_f08_placeholder_session_evidence_does_not_verify(evidence):
    from neobdm_source_contract import price_source_context
    out = price_source_context("2026-10-06", source_session="2026-10-05", session_evidence=evidence,
                               representation=pc.RAW_ACTUAL, representation_evidence=evidence)
    assert out["session_status"] == "UNKNOWN" and out["input_representation"] == "UNKNOWN"


@pytest.mark.parametrize("field,value", [
    ("source", "N/A"), ("source", "TBD"), ("source", "PLACEHOLDER"), ("source", "-"),
    ("source_identity", "unknown"), ("source_document_id", "0000"), ("source_document_id", ""),
])
def test_f08_placeholder_source_columns_cannot_admit_matching_event_prices(field, value):
    px = frame()                       # exactly the observed ENRG anchors
    px[field] = value
    out = annotate_prices(px, registry=registry(), representation=pc.RAW_ACTUAL)
    event = out.loc[out.date.eq("2026-10-05")].iloc[0]
    assert event.limit_reference_status != "RESOLVED"
    assert event.anchor_trust_status == "INADMISSIBLE"
    assert not event.close_anchor_admissible and not event.price_step_admissible


def test_f08_unknown_representation_never_gets_event_semantics_from_matching_prices():
    from test_inventory_capture import bf, price_db, price_payload
    px = frame()
    out = annotate_prices(px, registry=registry(), representation="UNKNOWN").set_index("date")
    assert out.loc["2026-10-05", "limit_reference_kind"] != "OFFICIAL_CORPORATE_ACTION_REFERENCE"
    assert out.loc["2026-10-05", "anchor_trust_reason"] == "UNRESOLVED_EVENT_REFERENCE"
    assert not out.loc["2026-10-05":, "price_step_admissible"].any()
    with price_db() as conn:
        with pytest.raises(bf.InventoryError, match="UNKNOWN_REPRESENTATION"):
            bf.insert_inventory(conn, "ENRG", price_payload(px.drop(columns="ticker").to_dict("records")))
        assert conn.total_changes == 0


# ── N01: repairing stored NULL fields is a validated change, never a crash ──

def null_history(conn, fields=("open", "high", "low", "volume")):
    from test_inventory_capture import price_bar
    for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
        bar = price_bar(day, 100)
        if day == "2026-07-02":
            bar.update({field: None for field in fields})
        conn.execute("INSERT INTO price_history VALUES (?,?,?,?,?,?,?)",
                     (day, "BBBB", bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]))


@pytest.mark.parametrize("fields", [("open",), ("high", "low"), ("volume",), ("open", "high", "low", "volume")])
def test_n01_null_ohlcv_repair_is_validated_without_crashing(fields):
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    with price_db() as conn:
        null_history(conn, fields)
        assert bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-07-02", 100)], "BBBB"))[1] == 1
        assert conn.execute("SELECT open, high, low, close, volume FROM price_history "
                            "WHERE date='2026-07-02'").fetchone() == (100, 100, 100, 100, 1000)


def test_n01_null_repair_still_checks_its_own_and_successor_transitions():
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    with price_db() as conn:
        null_history(conn)
        before = conn.total_changes
        with pytest.raises(bf.InventoryError, match="limit_violation"):   # +50% vs 07-01
            bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-07-02", 150)], "BBBB"))
        with pytest.raises(bf.InventoryError, match="limit_violation"):   # stored 07-03 is -23% from it
            bf.insert_inventory(conn, "BBBB", price_payload([price_bar("2026-07-02", 130)], "BBBB"))
        assert conn.total_changes == before
        assert conn.execute("SELECT open FROM price_history WHERE date='2026-07-02'").fetchone() == (None,)


def test_n01_null_only_repair_cannot_bypass_ohlc_admission():
    from test_inventory_capture import bf, price_db, price_payload, price_bar
    with price_db() as conn:
        null_history(conn)
        repaired = price_bar("2026-07-02", 100)
        repaired.update(open=140, high=140)
        before = conn.total_changes
        with pytest.raises(bf.InventoryError, match="limit_violation"):
            bf.insert_inventory(conn, "BBBB", price_payload([repaired], "BBBB"))
        assert conn.total_changes == before
