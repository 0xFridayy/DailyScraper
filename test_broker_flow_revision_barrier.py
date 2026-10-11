"""Offline M2 witnesses against the real writer and independent SQLite gate."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import json
from pathlib import Path
import socket
import sqlite3

import pytest

from price_contract import RAW_ACTUAL
from test_inventory_capture import bf, price_db, price_payload, price_bar
from test_price_history_revision_barrier import financial_snapshot, seed
import price_history_revision as revision


FIELDS = ("bval", "sval", "netval", "bavg", "savg")
KEY = ("2026-04-23", "VKTR", "AI")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("broker revision tests must stay offline")
    monkeypatch.setattr(socket, "create_connection", refuse)


def observations():
    body = price_payload([price_bar("2026-04-23", 42), price_bar("2026-04-24", 42)], "VKTR")
    body["data"]["nlot"] = {"AI": [-37100, -100], "AK": [100, 200]}
    return body


def changed_lots(body, sessions=1):
    proposal = copy.deepcopy(body)
    proposal["data"]["nlot"] = {"AI": [-74200, -200][:sessions]}
    return proposal


def vktr_lot_witness(tmp_path):
    path, baseline = tmp_path / "candidate.sqlite", tmp_path / "before.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        body = observations()
        seed(conn, body)
        revision.snapshot_database(path, baseline)
        before, changes = financial_snapshot(conn), conn.total_changes
        refusal = None
        try:
            bf.insert_inventory(conn, "VKTR", changed_lots(body), representation=RAW_ACTUAL)
        except bf.InventoryError as exc:
            refusal = str(exc)
        conn.commit()  # Deliberately catch and commit, as a real caller can.
        after = financial_snapshot(conn)
        return {"key": KEY, "stored_netval": before["broker_flow"][0][6],
                "proposed_netval": -0.31164, "persisted_netval": after["broker_flow"][0][6],
                "accepted": refusal is None, "refusal": refusal,
                "prices_unchanged": before["price_history"] == after["price_history"],
                "financial_snapshot_unchanged": before == after,
                "total_changes_delta": conn.total_changes - changes,
                "delta_gate_refusals": revision.verify_database_delta(baseline, path)}


def test_vktr_identical_price_changed_lots_refused(tmp_path):
    result = vktr_lot_witness(tmp_path)
    assert not result["accepted"], result
    assert result["financial_snapshot_unchanged"], result
    assert result["total_changes_delta"] == 0, result
    assert result["delta_gate_refusals"] == [], result


@pytest.mark.parametrize("sessions", [1, 2])
@pytest.mark.parametrize("representation", [RAW_ACTUAL, "UNKNOWN", "ADJUSTED", "MIXED"])
def test_one_broker_one_or_multiple_sessions_refused(sessions, representation):
    with contextlib.closing(price_db()) as conn:
        body = observations()
        seed(conn, body)
        before, changes = financial_snapshot(conn), conn.total_changes
        with pytest.raises(bf.InventoryError, match="broker_flow.*historical_revision"):
            bf.insert_inventory(conn, "VKTR", changed_lots(body, sessions), representation=representation)
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


@pytest.mark.parametrize("field", FIELDS)
def test_writer_protects_every_stored_financial_field(field):
    with contextlib.closing(price_db()) as conn:
        body = observations()
        seed(conn, body)
        # The backfill proposes NULL for four fields. It may not erase values
        # recorded by another source/unit convention, even with identical prices.
        conn.execute(f"UPDATE broker_flow SET {field}=42 WHERE date=? AND ticker=? AND broker_code=?", KEY)
        conn.commit()
        before, changes = financial_snapshot(conn), conn.total_changes
        with pytest.raises(bf.InventoryError, match="broker_flow.*historical_revision"):
            bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


def test_broker_null_repair_requires_existing_authority():
    with contextlib.closing(price_db()) as conn:
        body = observations()
        seed(conn, body)
        conn.execute("UPDATE broker_flow SET netval=NULL WHERE date=? AND ticker=? AND broker_code=?", KEY)
        conn.commit()
        before, changes = financial_snapshot(conn), conn.total_changes
        with pytest.raises(bf.InventoryError, match="UNVERIFIED_BROKER_NULL_REPAIR"):
            bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


@pytest.mark.parametrize("missing", ["broker", "session", "null", "short", "all"])
def test_absent_observation_is_preserved_never_zero(missing):
    with contextlib.closing(price_db()) as conn:
        body = observations()
        seed(conn, body)
        before, changes = financial_snapshot(conn), conn.total_changes
        proposal = copy.deepcopy(body)
        if missing == "broker":
            proposal["data"]["nlot"].pop("AI")
        elif missing == "session":
            proposal["data"]["date"] = body["data"]["date"][:1]
        elif missing == "null":
            proposal["data"]["nlot"]["AI"] = [None, None]
        elif missing == "short":
            proposal["data"]["nlot"]["AI"] = []
        else:
            proposal["data"]["nlot"] = {}
        try:
            bf.insert_inventory(conn, "VKTR", proposal, representation=RAW_ACTUAL)
        except bf.InventoryError as exc:
            raise AssertionError("absent broker observations must be preserved without zero proposals") from exc
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


def test_explicit_zero_is_an_observation_not_absence():
    with contextlib.closing(price_db()) as conn:
        body = observations()
        body["data"]["nlot"]["AI"][0] = 0
        seed(conn, body)
        assert conn.execute("SELECT netval FROM broker_flow WHERE date=? AND ticker=? AND broker_code=?", KEY).fetchone() == (0.0,)
        before = financial_snapshot(conn)
        body["data"]["nlot"]["AI"][0] = 1
        with pytest.raises(bf.InventoryError):
            bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before


def test_new_broker_and_session_insert_exact_replay_has_no_sql_mutation():
    with contextlib.closing(price_db()) as conn:
        body = observations()
        seed(conn, body)
        body["data"]["nlot"]["BK"] = [0, -100]
        bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        extension = price_payload([price_bar("2026-04-27", 42)], "VKTR")
        extension["data"]["nlot"] = {"AI": [500]}
        bf.insert_inventory(conn, "VKTR", extension, representation=RAW_ACTUAL)
        conn.commit()
        before, changes = financial_snapshot(conn), conn.total_changes
        for table in ("broker_flow", "price_history"):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER forbid_{table}_{operation} BEFORE {operation} ON {table} "
                             "BEGIN SELECT RAISE(ABORT,'identical observation mutated'); END")
        result = bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        assert result[:3] == (6, 2, ["AI", "AK", "BK"])
        bf.insert_inventory(conn, "VKTR", extension, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


def test_price_null_repair_with_identical_brokers_still_allowed():
    with contextlib.closing(price_db()) as conn:
        body = observations()
        seed(conn, body)
        broker_before = financial_snapshot(conn)["broker_flow"]
        conn.execute("UPDATE price_history SET open=NULL WHERE date=? AND ticker=?", KEY[:2])
        conn.commit()
        rid = conn.execute("SELECT rowid FROM price_history WHERE date=? AND ticker=?", KEY[:2]).fetchone()
        bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn)["broker_flow"] == broker_before
        assert conn.execute("SELECT rowid FROM price_history WHERE date=? AND ticker=?", KEY[:2]).fetchone() == rid
        assert conn.execute("SELECT open FROM price_history WHERE date=? AND ticker=?", KEY[:2]).fetchone() == (42.0,)


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("outer", [False, True])
def test_refusal_precedes_all_writes_and_preserves_caller_transaction(tmp_path, autocommit, outer):
    path = tmp_path / "atomic.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        body = observations()
        seed(conn, body)
        conn.isolation_level = None if autocommit else ""
        if outer:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO price_history VALUES('2026-04-23','BBBB',200,200,200,200,9999)")
        before, changes = financial_snapshot(conn), conn.total_changes
        proposal = changed_lots(body, 2)
        # A valid price NULL repair and new session must both wait for the guard.
        conn.execute("UPDATE price_history SET open=NULL WHERE date=? AND ticker=?", KEY[:2])
        if not outer:
            conn.commit()
        before, changes = financial_snapshot(conn), conn.total_changes
        proposal["data"]["ohlc"].append(price_bar("2026-04-27", 42))
        with pytest.raises(bf.InventoryError):
            bf.insert_inventory(conn, "VKTR", proposal, representation=RAW_ACTUAL)
        assert conn.in_transaction == outer
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes
        conn.commit()
    with contextlib.closing(sqlite3.connect(path)) as reader:
        assert financial_snapshot(reader) == before


def test_repeated_concurrent_calls_cannot_authorize_a_revision(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    body = observations()
    with contextlib.closing(price_db(str(path))) as conn:
        seed(conn, body)
        before = financial_snapshot(conn)

    def write(changed):
        with contextlib.closing(sqlite3.connect(path, timeout=15)) as conn:
            try:
                bf.insert_inventory(conn, "VKTR", changed_lots(body) if changed else body,
                                    representation=RAW_ACTUAL)
                result = "accepted"
            except bf.InventoryError:
                result = "refused"
            conn.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [False, True, False, True]))
    assert results == ["accepted", "refused", "accepted", "refused"]
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert financial_snapshot(conn) == before


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("change", ["revise", "erase", "repair"])
def test_broker_gate_catches_all_five_fields_without_price_changes(tmp_path, field, change):
    path, baseline = tmp_path / "candidate.sqlite", tmp_path / "before.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        seed(conn, observations())
        conn.execute(f"UPDATE broker_flow SET {field}=? WHERE date=? AND ticker=? AND broker_code=?",
                     (None if change == "repair" else 42, *KEY))
        conn.commit()
        revision.snapshot_database(path, baseline)
        prices = financial_snapshot(conn)["price_history"]
        # Deliberately bypass the writer, with an otherwise successful ticker.
        conn.execute(f"UPDATE broker_flow SET {field}=? WHERE date=? AND ticker=? AND broker_code=?",
                     (None if change == "erase" else 43, *KEY))
        conn.commit()
        before = financial_snapshot(conn)
        refusals = revision.verify_database_delta(baseline, path)
        assert refusals, (field, change)
        found = [r for r in refusals if r.get("table") == "broker_flow"]
        assert len(found) == 1, refusals
        assert (found[0]["session"], found[0]["ticker"], found[0]["broker_code"]) == KEY
        assert field in found[0]["changes"]
        assert revision.main(["verify", "--database", str(path), "--baseline", str(baseline)]) == 1
        assert financial_snapshot(conn) == before
        assert before["price_history"] == prices


def test_broker_gate_catches_deleted_row_and_allows_new_and_identical(tmp_path):
    path, baseline = tmp_path / "candidate.sqlite", tmp_path / "before.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        seed(conn, observations())
        revision.snapshot_database(path, baseline)
        assert revision.verify_database_delta(baseline, path) == []
        conn.execute("INSERT INTO broker_flow VALUES('2026-04-23','VKTR','BK',NULL,NULL,0,NULL,NULL)")
        conn.commit()
        assert revision.verify_database_delta(baseline, path) == []
        conn.execute("DELETE FROM broker_flow WHERE date=? AND ticker=? AND broker_code=?", KEY)
        conn.commit()
        refusals = revision.verify_database_delta(baseline, path)
        assert len(refusals) == 1, refusals
        assert refusals[0]["reason"] == "DELETED_OBSERVATION"
        assert refusals[0]["table"] == "broker_flow"


@pytest.mark.parametrize("wrong", ["empty", "unrelated", "missing_table", "corrupt", "missing", "same"])
def test_wrong_or_missing_broker_baseline_cannot_pass(tmp_path, wrong):
    path, baseline = tmp_path / "candidate.sqlite", tmp_path / "before.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        seed(conn, observations())
    if wrong in {"empty", "unrelated", "missing_table"}:
        other = tmp_path / "other.sqlite"
        with contextlib.closing(price_db(str(other))) as conn:
            if wrong == "unrelated":
                body = observations()
                body["meta"]["symbol"] = "BBBB"
                seed(conn, body)
            if wrong == "missing_table":
                conn.execute("DROP TABLE broker_flow")
                conn.commit()
        # A snapshot made for a different candidate cannot be substituted.
        if wrong == "missing_table":
            with pytest.raises(sqlite3.Error):
                revision.snapshot_database(other, baseline)
        else:
            revision.snapshot_database(other, baseline)
    elif wrong == "corrupt":
        baseline.write_text("not SQLite")
    elif wrong == "same":
        baseline = path
    assert revision.main(["verify", "--database", str(path), "--baseline", str(baseline)]) == 1


def test_broker_run_refusal_evidence_is_sanitized(tmp_path):
    from test_inventory_capture import run_backfill, Resp, ic, only_manifest
    path = tmp_path / "neobdm.db"
    body = observations()
    with contextlib.closing(price_db(str(path))) as conn:
        seed(conn, body)
        before = financial_snapshot(conn)
    proposal = changed_lots(body)
    proposal["authentication"] = {"cookie": "DO_NOT_EXPOSE"}
    output, exit_message, _ = run_backfill(str(tmp_path), {"VKTR": [Resp(200, body=proposal)]}, ["VKTR"])
    assert "unauthorized historical revisions" in str(exit_message)
    evidence_text = (tmp_path / "topup-failure.json").read_text()
    assert "DO_NOT_EXPOSE" not in output + evidence_text
    evidence = json.loads(evidence_text)
    assert evidence["table"] == "broker_flow"
    assert evidence["refusals"][0]["broker_code"] == "AI"
    assert ic.read_captures(only_manifest(str(tmp_path)))[0]["status"] == ic.REJECTED
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert financial_snapshot(conn) == before


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("transition", ["revision", "erase", "repair", "null"])
def test_broker_preflight_five_field_policy(field, transition):
    with contextlib.closing(price_db()) as conn:
        values = dict.fromkeys(FIELDS, 10.0)
        if transition in {"repair", "null"}:
            values[field] = None
        conn.execute("INSERT INTO broker_flow VALUES(?,?,?,?,?,?,?,?)", (*KEY, *values.values()))
        conn.commit()
        proposed = dict(values)
        proposed[field] = None if transition in {"erase", "null"} else 20.0
        before, changes = financial_snapshot(conn), conn.total_changes
        rows = [(*KEY, *proposed.values())]
        if transition == "null":
            assert bf.validate_inventory_brokers(conn, "VKTR", rows) == []
        else:
            with pytest.raises(bf.HistoricalRevisionError) as error:
                bf.validate_inventory_brokers(conn, "VKTR", rows)
            refusal = error.value.evidence["refusals"][0]
            assert list(refusal["changes"]) == [field]
            assert refusal["reason"] == ("UNVERIFIED_BROKER_NULL_REPAIR" if transition == "repair"
                                         else "UNAUTHORIZED_HISTORICAL_REVISION")
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes


@pytest.mark.parametrize("change", ["missing", "tampered", "missing_receipt", "bad_receipt", "wrong_key", "corrupt"])
def test_baseline_receipt_and_broker_schema_fail_closed(tmp_path, change):
    path, baseline = tmp_path / "candidate.sqlite", tmp_path / "before.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        seed(conn, observations())
        revision.snapshot_database(path, baseline)
        receipt = Path(str(baseline) + ".json")
        if change in {"missing", "wrong_key"}:
            conn.execute("DROP TABLE broker_flow")
            if change == "wrong_key":
                conn.execute("CREATE TABLE broker_flow(date,ticker,broker_code,bval,sval,netval,bavg,savg)")
            conn.commit()
        elif change == "tampered":
            baseline.chmod(0o666)
            with contextlib.closing(sqlite3.connect(baseline)) as bad:
                bad.execute("DROP TABLE broker_flow")
                bad.commit()
        elif change == "missing_receipt":
            receipt.chmod(0o666)
            receipt.unlink()
        elif change == "bad_receipt":
            receipt.chmod(0o666)
            receipt.write_text("[]")
        else:
            # Close before overwriting a disposable candidate on Windows.
            conn.close()
            path.write_text("corrupt SQLite")
        assert revision.main(["verify", "--database", str(path), "--baseline", str(baseline)]) == 1


def test_independent_gate_blocks_commit_after_successful_writer(tmp_path):
    from test_price_history_revision_barrier import test_workflow_commit_requires_both_gates_and_successful_writer
    path, baseline = tmp_path / "candidate.sqlite", tmp_path / "before.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        body = observations()
        seed(conn, body)
        revision.snapshot_database(path, baseline)
        result = bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        assert result[1] == 2
        conn.commit()
        prices = financial_snapshot(conn)["price_history"]
        conn.execute("UPDATE broker_flow SET netval=netval*2 WHERE date=? AND ticker=? AND broker_code=?", KEY)
        conn.commit()
        assert financial_snapshot(conn)["price_history"] == prices
        assert revision.main(["verify", "--database", str(path), "--baseline", str(baseline)]) == 1
        test_workflow_commit_requires_both_gates_and_successful_writer()


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("failed_table", ["price_history", "broker_flow"])
def test_failure_after_partial_mutation_caller_catches_and_commits(tmp_path, autocommit, failed_table):
    path = tmp_path / "failure.sqlite"
    with contextlib.closing(price_db(str(path))) as conn:
        seed(conn, observations())
        conn.isolation_level = None if autocommit else ""
        conn.execute("BEGIN")
        conn.execute("INSERT INTO price_history VALUES('2026-04-23','BBBB',200,200,200,200,9999)")
        before = financial_snapshot(conn)
        condition = "NEW.date='2026-04-28'" if failed_table == "price_history" else "NEW.broker_code='AK'"
        conn.execute(f"CREATE TRIGGER injected_failure BEFORE INSERT ON {failed_table} WHEN {condition} "
                     "BEGIN SELECT RAISE(ABORT,'injected financial failure'); END")
        body = price_payload([price_bar("2026-04-27", 42), price_bar("2026-04-28", 42)], "VKTR")
        body["data"]["nlot"] = {"AI": [1, 2], "AK": [3, 4]}
        with pytest.raises(sqlite3.IntegrityError, match="injected financial failure"):
            bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        assert conn.in_transaction
        assert financial_snapshot(conn) == before
        conn.commit()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert financial_snapshot(conn) == before


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_lots_cannot_create_a_missing_financial_value(invalid):
    with contextlib.closing(price_db()) as conn:
        body = observations()
        body["data"]["nlot"]["AI"][0] = invalid
        before, changes = financial_snapshot(conn), conn.total_changes
        with pytest.raises(bf.InventoryError, match="INVALID_FINANCIAL_VALUE"):
            bf.insert_inventory(conn, "VKTR", body, representation=RAW_ACTUAL)
        conn.commit()
        assert financial_snapshot(conn) == before
        assert conn.total_changes == changes
