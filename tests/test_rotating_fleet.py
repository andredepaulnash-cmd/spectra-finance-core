from dataclasses import replace
from decimal import Decimal as D

from spectra_core.binance_kline_import import KlineDataset, MinuteBar
from spectra_core.rotating_fleet import FleetConfig, RotatingFleetSimulator
from spectra_core.vcap_profit_exit import VCAPProfitExitPolicy


def dataset(prices, *, quote_volume=D("1000000")):
    series = {}
    for pair, values in prices.items():
        bars = []
        for index, price in enumerate(values):
            value = D(str(price))
            base_volume = quote_volume / value
            bars.append(
                MinuteBar(
                    1_800_000_000 + index * 60,
                    value,
                    value,
                    value,
                    value,
                    base_volume,
                    quote_volume,
                    100,
                )
            )
        series[pair] = tuple(bars)
    return KlineDataset(
        series,
        {
            "complete_synchronized_grid": True,
            "source_fingerprint": "a" * 64,
            "kind": "synthetic",
        },
    )


def config(**kwargs):
    return replace(
        FleetConfig(
            initial_cash=D(100),
            target_positions=2,
            entry_budget=D(10),
            minimum_position_basis=D(10),
            recovery_seed_quote=D(5),
            reserve_fraction=D(0),
            pair_cap_fraction=D(".5"),
            fee_bps=D(0),
            slippage_bps=D(0),
            volume_participation=D(1),
            simulated_min_order_quote=D(1),
        ),
        **kwargs,
    )


def test_two_unique_positions_rotate_with_new_identity_and_linked_entry():
    data = dataset(
        {
            "AAA-USDT": [10, 10, 11, D("10.5"), D("10.5"), D("10.5")],
            "BBB-USDT": [10, 10, 10, 10, 10, 10],
            "CCC-USDT": [10, 10, 10, 10, 10, 10],
        }
    )
    policy = VCAPProfitExitPolicy(
        target_net_profit_bps=D(100),
        stagnation_seconds=7200,
        meaningful_progress_bps=D(25),
        max_profit_giveback_bps=D(50),
        continuation_threshold=.55,
        suggested_trailing_delta_bps=D(35),
    )
    result = RotatingFleetSimulator(config(), exit_policy=policy).run(data)
    entries = [fill for fill in result.fills if fill.kind == "ENTRY"]
    assert len(entries) == 3
    assert {fill.position_id for fill in entries} == {1, 2, 3}
    assert entries[-1].pair == "CCC-USDT"
    assert entries[-1].dependency_order_id is not None
    assert result.metrics["completed_positions"] == 1
    assert result.metrics["rotations"] == 1
    assert result.metrics["linked_replacements_filled"] == 1
    assert result.metrics["maximum_simultaneous_positions"] == 2
    assert result.metrics["minimum_entry_basis"] == D(10)
    assert all(
        sample.active_positions == sample.unique_base_assets for sample in result.samples
    )


def test_linked_replacement_retains_target_with_high_reserve():
    data = dataset(
        {
            "AAA-USDT": [10, 10, 12, D("11.5"), D("11.5"), D("11.5")],
            "BBB-USDT": [10, 10, 10, 10, 10, 10],
            "CCC-USDT": [10, 10, 10, 10, 10, 10],
        }
    )
    policy = VCAPProfitExitPolicy(
        target_net_profit_bps=D(100),
        stagnation_seconds=7200,
        meaningful_progress_bps=D(25),
        max_profit_giveback_bps=D(50),
        continuation_threshold=.55,
        suggested_trailing_delta_bps=D(35),
    )
    result = RotatingFleetSimulator(
        config(reserve_fraction=D(".8")), exit_policy=policy
    ).run(data)
    assert result.metrics["linked_replacements_filled"] == 1
    assert result.metrics["ending_positions"] == 2


def test_shared_cash_and_volume_gate_never_create_undersized_positions():
    data = dataset(
        {
            "AAA-USDT": [10, 10, 10],
            "BBB-USDT": [10, 10, 10],
            "CCC-USDT": [10, 10, 10],
        },
        quote_volume=D(5),
    )
    result = RotatingFleetSimulator(
        config(volume_participation=D(".1"))
    ).run(data)
    assert not result.fills
    assert result.cash == D(100)
    assert result.metrics["fleet_initialized"] is False
    assert result.rejections["insufficient_liquidity_eligible_assets"] >= 2


def test_future_prices_cannot_change_earlier_fills_or_samples():
    prefix = [10, 10, 10, 10]
    a = dataset(
        {
            "AAA-USDT": prefix + [20, 20],
            "BBB-USDT": prefix + [20, 20],
            "CCC-USDT": prefix + [20, 20],
        }
    )
    b = dataset(
        {
            "AAA-USDT": prefix + [1, 1],
            "BBB-USDT": prefix + [1, 1],
            "CCC-USDT": prefix + [1, 1],
        }
    )
    ra = RotatingFleetSimulator(config()).run(a)
    rb = RotatingFleetSimulator(config()).run(b)
    cutoff = 1_800_000_000 + 4 * 60
    assert [fill for fill in ra.fills if fill.timestamp <= cutoff] == [
        fill for fill in rb.fills if fill.timestamp <= cutoff
    ]
    assert [sample for sample in ra.samples if sample.timestamp <= cutoff] == [
        sample for sample in rb.samples if sample.timestamp <= cutoff
    ]
