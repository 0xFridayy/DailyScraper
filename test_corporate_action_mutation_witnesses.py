"""Behavioral witnesses using reconstructed bars and disposable databases."""

import copy
import pytest

import price_audit as pa
import price_contract as pc
from test_price_contract import frame, registry


@pytest.mark.parametrize("missing", ["high", "low"])
def test_f05_missing_actual_extrema_anchors_refuse(missing):
    prices = frame().drop(columns=missing)
    with pytest.raises(pc.UnsupportedPriceContract, match="Incomplete OHLCV"):
        pa.add_forward_returns(prices, prices.date.tolist(), (2,), extremes=True,
                               registry=registry(), representation=pc.RAW_ACTUAL)


def test_f11_direct_ranking_refuses_valid_unversioned_candidates():
    import daily_picks

    tagged = {"ENRG": {"tags": ["broad_buying", "inst_foreign", "bandar_3days"],
                       "row": {"clean_score": 5, "nr_dn_0": 2, "f_dn_0": 1, "tval": 10}}}
    with pytest.raises(pc.UnsupportedPriceContract, match="daily_picks.rank_picks"):
        daily_picks.rank_picks(tagged, {})


def test_f13_recovery_does_not_hide_bad_successor():
    from test_inventory_capture import bf, price_db, price_payload
    import check_signal_integrity as integrity

    with price_db() as conn:
        event_bars = frame().iloc[:4].drop(columns="ticker").to_dict("records")
        bf.insert_inventory(conn, "ENRG", price_payload(event_bars), representation=pc.RAW_ACTUAL)
        conn.execute("CREATE TABLE price_quarantine(date,ticker,reasons)")
        conn.execute("INSERT INTO price_quarantine VALUES('2026-10-05','ENRG','limit_violation')")
        bad = copy.deepcopy(frame().iloc[4].to_dict())
        bad.update(open=1800, high=1810, low=1790, close=1800)
        before = conn.total_changes
        with pytest.raises(bf.InventoryError):
            bf.insert_inventory(conn, "ENRG", price_payload([
                {key: value for key, value in bad.items() if key != "ticker"}
            ]), representation=pc.RAW_ACTUAL)
        assert conn.total_changes == before

        # Reconstruct contamination already on disk, without allowing the writer
        # to insert it. The derived cleaner and integrity monitor must reject it.
        conn.execute("INSERT INTO price_history(date,ticker,open,high,low,close,volume) "
                     "VALUES(?,?,?,?,?,?,?)", tuple(bad[key] for key in frame().columns))
        adjudicated, quarantined, _ = pa.adjudicate_quarantine(
            conn, registry=registry(), representation=pc.RAW_ACTUAL)
        assert ("2026-10-05", "ENRG") not in quarantined
        successor = adjudicated.loc[adjudicated.date.eq("2026-10-06")].iloc[0]
        assert successor.limit_reference_status == "RESOLVED"
        assert successor.limit_reference_price == 1030
        assert successor.limit_admission_status == "OUT_OF_BAND"
        assert successor.suspect
        clean = pa.load_clean(conn, registry=registry(), representation=pc.RAW_ACTUAL)
        assert "2026-10-05" in clean.date.tolist()
        assert "2026-10-06" not in clean.date.tolist()
        problems, notes, stats = [], [], {}
        integrity.check_new_contamination(conn, problems, notes, stats,
                                          registry=registry(), representation=pc.RAW_ACTUAL)
        assert stats["fresh_suspects"] == 1
        assert problems
