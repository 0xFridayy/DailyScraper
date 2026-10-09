"""Safe morning operational delivery: stubs only, no real Telegram."""
import io
import json
from contextlib import redirect_stdout
from price_contract import UnsupportedPriceContract
from test_morning import run, RAW


def test_unsupported_route_delivers_nonfinancial_operational_status():
    with redirect_stdout(io.StringIO()) as output:
        sent = run(raises=UnsupportedPriceContract("unmigrated",
                   consumer="daily_picks.run_morning"))
    assert len(sent) == 1
    assert "Morning operational status" in sent[0]
    assert "UNSUPPORTED" in sent[0]
    assert "health: UNVERIFIED" in sent[0]
    assert RAW not in sent[0]
    result = json.loads(output.getvalue())
    assert result["daily_picks"]["status"] == "UNSUPPORTED"
    assert result["operational_report"]["analytics"] == "UNSUPPORTED"
    assert result["operational_report"]["scrape"] == "REPORT_CAPTURED"


def test_unavailable_analytics_never_falls_back_to_financial_report():
    for kwargs in ({"status": "send_failed"},
                   {"raises": ValueError("secret URL")},
                   {"raises": UnsupportedPriceContract("wrong route")}):
        with redirect_stdout(io.StringIO()):
            sent = run(**kwargs)
        assert len(sent) == 1
        assert "Morning operational status" in sent[0]
        assert RAW not in sent[0]
        assert "secret URL" not in sent[0]


def test_operational_report_exists_without_a_held_raw_report():
    with redirect_stdout(io.StringIO()):
        sent = run(raises=UnsupportedPriceContract("unmigrated",
                   consumer="daily_picks.run_morning"), scrape_messages=())
    assert len(sent) == 1
    assert "NO_REPORT_CAPTURED" in sent[0]
