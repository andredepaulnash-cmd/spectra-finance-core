from decimal import Decimal as D
import gzip
import json

import pytest

from spectra_core.kraken_tardis_import import capture_second, convert
from spectra_core.strategy_replay import load_csv


def rule(status="online", **kw):
    return {
        "channel": "instrument",
        "type": "snapshot",
        "data": {
            "pairs": [
                {
                    "symbol": "BTC/USD",
                    "status": status,
                    "qty_min": ".00005",
                    "cost_min": ".5",
                    "qty_increment": ".00000001",
                    **kw,
                }
            ]
        },
    }


def tick(**kw):
    return {
        "channel": "ticker",
        "type": "update",
        "data": [
            {
                "symbol": "BTC/USD",
                "bid": "100",
                "ask": "101",
                "bid_qty": ".2",
                "ask_qty": ".4",
                "volume": "20",
                "vwap": "99",
                **kw,
            }
        ],
    }


def line(second, message):
    return (
        f"2026-09-01T00:{second // 60:02}:{second % 60:02}.0000001Z "
        + json.dumps(message)
        + "\n"
    )


def run(tmp_path, text, compressed=False):
    raw = tmp_path / ("raw.gz" if compressed else "raw.txt")
    raw.write_bytes(
        gzip.compress(text.encode(), mtime=0) if compressed else text.encode()
    )
    csv = tmp_path / "quotes.csv"
    manifest = tmp_path / "manifest.json"
    report = convert([raw], csv, manifest)
    return load_csv(csv), report


def test_rounds_capture_up_and_preserves_decimal_precision(tmp_path):
    frames, report = run(
        tmp_path, line(0, rule()) + line(1, tick()) + line(61, tick()), True
    )
    assert frames[0][0].timestamp == int(capture_second("2026-09-01T00:00:00Z")) + 2
    assert frames[0][0].depth_base == D(".2")
    assert frames[0][0].quote_volume_24h == D(1980)
    assert frames[0][0].step_base == D(".00000001")
    assert report["kind"] == "recorded" and report["rows"] == 2
    assert len(report["raw_sources"][0]["sha256"]) == 64


def test_future_rules_not_backfilled_and_updates_apply_causally(tmp_path):
    text = line(0, tick()) + line(1, rule()) + line(2, tick())
    update = {
        "channel": "instrument",
        "type": "update",
        "data": {"pairs": [{"symbol": "BTC/USD", "cost_min": "5"}]},
    }
    text += line(60, update) + line(62, tick())
    frames, report = run(tmp_path, text)
    assert report["import_stats"]["missing_rules"] == 1
    assert frames[0][0].min_quote == D(".5") and frames[1][0].min_quote == D(5)


def test_disconnect_clears_rules_and_inactive_has_zero_liquidity(tmp_path):
    text = line(0, rule()) + line(1, tick()) + "\n" + line(61, tick())
    text += line(62, rule("maintenance")) + line(63, tick())
    frames, report = run(tmp_path, text)
    assert report["import_stats"]["disconnects"] == 1
    assert report["import_stats"]["missing_rules"] == 1
    assert frames[1][0].depth_base == 0


def test_backward_capture_and_insufficient_evidence_rejected(tmp_path):
    with pytest.raises(ValueError, match="backwards"):
        run(tmp_path, line(2, rule()) + line(1, tick()))
    with pytest.raises(ValueError, match="insufficient"):
        run(tmp_path, line(0, tick()) + line(60, tick()))


def test_interval_decimation_never_forward_fills(tmp_path):
    frames, report = run(
        tmp_path,
        line(0, rule()) + line(1, tick()) + line(30, tick()) + line(80, tick()),
    )
    assert report["rows"] == 2
    assert frames[1][0].timestamp - frames[0][0].timestamp == 79
