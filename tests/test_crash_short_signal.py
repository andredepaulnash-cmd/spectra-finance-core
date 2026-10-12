"""Synthetic signal checks and an opt-in, hash-pinned Jan–June differential test."""

import csv
import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from spectra_core.crash_short_signal import Bar, DailySignal, evaluate_btc_daily_signal

D = Decimal
DAY = 86_400
DECISION = 10 * DAY
LATEST = DECISION - 300


def bar(timestamp=LATEST, close="100", **changes):
    values = dict(
        open=close, high=close, low=close, close=close, volume="1", trades="1"
    )
    values.update(changes)
    return Bar(timestamp=timestamp, **values)


def history(
    *, close="100", anchor_7d="100", anchor_1d="100", peak="100", latest=LATEST
):
    return [
        bar(latest - 7 * DAY, anchor_7d),
        bar(DECISION - 2 * DAY, peak),
        bar(latest - DAY, anchor_1d),
        bar(latest, close),
    ]


def evaluate(bars):
    return evaluate_btc_daily_signal(bars, DECISION)


def test_bar_normalizes_numbers_without_rounding_money_or_fractional_volume():
    observation = Bar(LATEST, "100.001", 102, D("99.5"), 100.125, 0.25, "2.0")
    assert observation.open == D("100.001")
    assert observation.close == D("100.125")
    assert observation.volume == D("0.25")
    assert observation.is_valid
    with pytest.raises(FrozenInstanceError):
        observation.close = D("101")


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume", "trades"])
@pytest.mark.parametrize(
    "value", [None, "NaN", "sNaN", "Infinity", "-Infinity", "0", "-1", True, "bad"]
)
def test_invalid_fields_are_retained_as_unusable_observations(field, value):
    observation = replace(bar(), **{field: value})
    assert not observation.is_valid
    if field == "open":
        assert observation.valid_open is None
    else:
        assert observation.valid_open == D("100")


@pytest.mark.parametrize(
    "changes",
    [
        {"trades": "1.5"},
        {"high": "99"},
        {"low": "101"},
        {"open": "101"},
        {"close": "99"},
        {"high": "98", "low": "99"},
    ],
)
def test_original_trade_and_ohlc_bounds(changes):
    assert not replace(bar(), **changes).is_valid


@pytest.mark.parametrize(
    "timestamp", [True, False, 300.5, 300.0, "300", D("300"), None, float("nan")]
)
def test_bar_requires_an_integer_timestamp(timestamp):
    with pytest.raises(ValueError, match="integer Unix timestamp"):
        bar(timestamp=timestamp)


def test_bar_requires_five_minute_open_alignment():
    with pytest.raises(ValueError, match="aligned"):
        bar(timestamp=LATEST + 1)


@pytest.mark.parametrize(
    "decision", [True, float(DECISION), DECISION + 300, DECISION + 1]
)
def test_daily_decision_requires_integer_utc_midnight(decision):
    with pytest.raises(ValueError):
        evaluate_btc_daily_signal([], decision)


def test_non_bar_inputs_are_rejected():
    with pytest.raises(TypeError, match="Bar"):
        evaluate([{"timestamp": LATEST}])


@pytest.mark.parametrize(
    "prices, label",
    [
        ({}, "SIDEWAYS"),
        ({"close": "105", "anchor_1d": "105", "peak": "105"}, "RISING"),
        ({"close": "104.999", "anchor_1d": "104.999", "peak": "104.999"}, "SIDEWAYS"),
        ({"close": "95", "anchor_1d": "95", "peak": "95"}, "FALLING"),
        ({"close": "95.001", "anchor_1d": "95.001", "peak": "95.001"}, "SIDEWAYS"),
        ({"close": "95", "anchor_7d": "95"}, "CRASH"),
        ({"close": "95.000000001", "anchor_7d": "95.000000001"}, "SIDEWAYS"),
        ({"close": "90", "anchor_7d": "90", "anchor_1d": "90"}, "SIDEWAYS"),
        ({"close": "89.999", "anchor_7d": "89.999", "anchor_1d": "89.999"}, "CRASH"),
        ({"close": "94", "anchor_7d": "50"}, "CRASH"),
    ],
)
def test_frozen_label_thresholds_and_crash_precedence(prices, label):
    result = evaluate(history(**prices))
    assert result.label == label
    assert result.last_source_bar_open == LATEST
    assert result.unknown_reason is None


def test_threshold_arithmetic_deliberately_preserves_binary64_rounding():
    result = evaluate(history(close="90", anchor_7d="90", anchor_1d="90"))
    assert result.drawdown_7d == D(str(90.0 / 100.0 - 1.0))
    assert result.drawdown_7d > D("-0.10")
    assert result.label == "SIDEWAYS"
    result = evaluate(history(close="95", anchor_7d="95"))
    assert result.return_1d == D(str(95.0 / 100.0 - 1.0))
    assert result.return_1d < D("-0.05")
    assert result.label == "CRASH"


def test_signal_is_independent_of_callers_decimal_precision():
    observations = history(close="95", anchor_7d="95")
    expected = evaluate(observations)
    with localcontext() as context:
        context.prec = 3
        assert evaluate(observations) == expected


def test_current_execution_bar_cannot_supply_its_future_close_or_volume():
    observations = history()
    expected = evaluate(observations)
    at_open = Bar(DECISION, "100", None, None, None, None, None)
    completed_later = bar(DECISION, close="1", open="100", high="100", volume="999")
    assert at_open.valid_open == completed_later.valid_open == D("100")
    assert not at_open.is_valid and completed_later.is_valid
    assert evaluate([*observations, at_open]) == expected
    assert evaluate([*observations, completed_later]) == expected


def test_appended_future_rows_and_future_conflicts_do_not_change_past_signal():
    observations = history()
    expected = evaluate(observations)
    future = [bar(DECISION, "1"), bar(DECISION, "999999"), bar(DECISION + DAY, "0.01")]
    assert evaluate([*observations, *future]) == expected
    assert evaluate(reversed([*future, *observations])) == expected


def test_duplicate_equal_past_rows_are_idempotent_and_input_order_is_irrelevant():
    observations = history()
    expected = evaluate(observations)
    assert evaluate(reversed(observations + observations)) == expected
    nan_row = bar(DECISION - 600, volume="sNaN")
    assert evaluate([*observations, nan_row, nan_row]) == expected
    assert (
        evaluate([*observations, replace(observations[-1], close=D("100.00"))])
        == expected
    )


@pytest.mark.parametrize(
    "changes", [{"close": "99"}, {"volume": "0"}, {"volume": "NaN"}]
)
def test_contradictory_past_timestamp_is_rejected_even_if_one_row_is_invalid(changes):
    with pytest.raises(ValueError, match="Contradictory observations"):
        evaluate([*history(), bar(**changes)])


def test_no_history_is_unknown_without_invented_metrics():
    result = evaluate([bar(DECISION), bar(DECISION + DAY), bar(volume="NaN")])
    assert result == DailySignal(
        DECISION, "UNKNOWN", unknown_reason="NO_COMPLETED_VALID_BARS"
    )


def test_first_eight_midnights_remain_warmup_without_prior_period_data():
    observations = [bar(timestamp=timestamp) for timestamp in range(0, 9 * DAY, 300)]
    labels = [
        evaluate_btc_daily_signal(observations, day * DAY).label for day in range(9)
    ]
    assert labels == ["UNKNOWN"] * 8 + ["SIDEWAYS"]


@pytest.mark.parametrize(
    "age, expected", [(300, "SIDEWAYS"), (600, "SIDEWAYS"), (900, "UNKNOWN")]
)
def test_latest_observation_freshness_is_inclusive(age, expected):
    result = evaluate(history(latest=DECISION - age))
    assert result.label == expected
    if expected == "UNKNOWN":
        assert result.unknown_reason == "STALE_LATEST_BAR"
        assert result.last_source_bar_open == DECISION - age
        assert result.return_7d is result.return_1d is result.drawdown_7d is None


@pytest.mark.parametrize("anchor_index, name", [(0, "7D"), (2, "1D")])
@pytest.mark.parametrize("age", [0, 300, 600, 900])
def test_anchor_freshness_is_inclusive(anchor_index, name, age):
    observations = history()
    observations[anchor_index] = replace(
        observations[anchor_index], timestamp=observations[anchor_index].timestamp - age
    )
    result = evaluate(observations)
    assert result.label == ("SIDEWAYS" if age <= 600 else "UNKNOWN")
    if age > 600:
        assert result.unknown_reason == f"STALE_{name}_ANCHOR"


def test_anchor_targets_are_relative_to_latest_not_to_decision_midnight():
    observations = history(latest=DECISION - 600)
    observations += [bar(DECISION - 7 * DAY, "500"), bar(DECISION - DAY, "500")]
    result = evaluate(observations)
    assert result.return_7d == result.return_1d == D("0.0")
    assert result.drawdown_7d == D("-0.8")
    assert result.label == "CRASH"


def test_invalid_anchors_are_not_forward_filled_or_repaired():
    observations = history()
    observations[0] = replace(observations[0], volume=D("0"))
    result = evaluate(observations)
    assert result.label == "UNKNOWN"
    assert result.unknown_reason == "MISSING_7D_ANCHOR"


def test_missing_1d_anchor_uses_the_original_older_valid_observation_then_fails_freshness():
    observations = history()
    observations.pop(2)
    assert evaluate(observations).unknown_reason == "STALE_1D_ANCHOR"


def test_drawdown_window_uses_completed_closes_and_includes_start_boundary():
    observations = history(anchor_7d="1000")
    observations[-1] = replace(observations[-1], high=D("10000"))
    result = evaluate(observations)
    assert result.drawdown_7d == D("0.0")
    assert result.label == "FALLING"  # Older anchor/high are not drawdown closes.
    result = evaluate([*observations, bar(DECISION - 7 * DAY, "1000")])
    assert result.drawdown_7d == D("-0.9")
    assert result.label == "CRASH"


def test_invalid_closes_do_not_set_the_drawdown_maximum():
    result = evaluate([*history(), bar(DECISION - 600, "1000", volume="0")])
    assert result.drawdown_7d == D("0.0")
    assert result.label == "SIDEWAYS"


@pytest.mark.parametrize("price", ["1e999", "1e-999"])
def test_finite_decimal_prices_outside_binary64_range_fail_closed(price):
    observations = history(close=price, anchor_7d=price, anchor_1d=price, peak=price)
    assert all(observation.is_valid for observation in observations)
    result = evaluate(observations)
    assert result.label == "UNKNOWN"
    assert result.unknown_reason == "UNREPRESENTABLE_SIGNAL_INPUT"


def test_overflowed_ratio_fails_closed():
    result = evaluate(
        history(close="1e300", anchor_7d="1e-300", anchor_1d="1e300", peak="1e300")
    )
    assert result.label == "UNKNOWN"
    assert result.unknown_reason == "NON_FINITE_STATISTIC"


def test_json_evidence_preserves_decimal_values_and_unknown_nulls():
    result = evaluate(history(close="95", anchor_7d="95"))
    encoded = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert encoded["return_1d"] == str(result.return_1d)
    assert encoded["decision_at"] == DECISION
    assert encoded["last_source_bar_open"] == LATEST
    assert encoded["unknown_reason"] is None
    missing = json.loads(json.dumps(evaluate([]).to_dict(), allow_nan=False))
    assert missing["return_1d"] is None
    assert missing["last_source_bar_open"] is None


def test_all_181_frozen_daily_labels_and_statistics_match():
    """No data download; refuse any input other than the pinned Jan–June fixture.

    Set SPECTRA_FROZEN_SHORT_FIXTURE_DIR to the original audited input directory.
    The large historical data fixture is intentionally not committed to the repo.
    """
    location = os.environ.get("SPECTRA_FROZEN_SHORT_FIXTURE_DIR")
    if not location:
        pytest.skip(
            "Set SPECTRA_FROZEN_SHORT_FIXTURE_DIR for the pinned historical differential test"
        )
    root = Path(location)
    expected_hashes = {
        "ohlcvt_15.npy": "5f4b40da6cd468c0d66bce148e1648c71d29f41942d16b137ca55aa32c12eefd",
        "verified_daily_labels.csv": "db64d772808df52547d54aed351bf81c42b6ef1b0db751af5685a7dfb143839d",
        "input_manifest.json": "85840ac9f6c4779e303ed12415acd609cc6bff8cd6ffcf4126b42ac84be96690",
        "PRE_RUN_CONVENTIONS.json": "48ff971e71dc235e2365b99c5988c39f07ae3b3da22ad358aa8a1ea9acb78ee2",
    }
    for name, digest in expected_hashes.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    manifest = json.loads((root / "input_manifest.json").read_text())
    assert (manifest["start"], manifest["end"], manifest["interval_seconds"]) == (
        1767225600,
        1782864000,
        300,
    )
    assert manifest["assets"][0] == "BTC"
    import numpy as np

    data = np.load(root / "ohlcvt_15.npy", allow_pickle=False)
    assert data.shape == (52128, 15, 6)
    observations = [
        Bar(manifest["start"] + index * 300, *row)
        for index, row in enumerate(data[:, 0, :])
    ]
    btc = data[:, 0, :]
    frozen_valid = (
        np.isfinite(btc).all(axis=1)
        & (btc[:, :4] > 0).all(axis=1)
        & (btc[:, 4] > 0)
        & (btc[:, 5] > 0)
        & (btc[:, 5] == np.floor(btc[:, 5]))
        & (btc[:, 1] >= np.maximum(btc[:, 0], btc[:, 3]))
        & (btc[:, 2] <= np.minimum(btc[:, 0], btc[:, 3]))
        & (btc[:, 1] >= btc[:, 2])
    )
    assert [
        observation.is_valid for observation in observations
    ] == frozen_valid.tolist()
    frozen_open_valid = np.isfinite(btc[:, 0]) & (btc[:, 0] > 0)
    assert [
        observation.valid_open is not None for observation in observations
    ] == frozen_open_valid.tolist()
    with (root / "verified_daily_labels.csv").open(newline="") as source:
        expected = list(csv.DictReader(source))
    assert len(expected) == 181
    for day_index, row in enumerate(expected):
        decision = int(row["timestamp"])
        assert decision == manifest["start"] + day_index * DAY
        actual = evaluate_btc_daily_signal(observations, decision)
        assert actual.label == row["label"], row["day"]
        assert actual.last_source_bar_open == (
            int(row["last_source_bar_open"]) if row["last_source_bar_open"] else None
        )
        for field in ("return_7d", "return_1d", "drawdown_7d"):
            if row[field]:
                assert float(getattr(actual, field)) == pytest.approx(
                    float(row[field]), rel=0, abs=1e-15
                ), (row["day"], field)
            else:
                assert getattr(actual, field) is None
