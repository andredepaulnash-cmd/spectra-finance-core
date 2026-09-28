from dataclasses import replace
from decimal import Decimal as D
import random

import pytest

from spectra_core.strategy_replay import (
    Candidate,
    Order,
    Quote,
    ReplayConfig,
    compare,
    load_csv,
    replay,
)


def frames(prices, **kwargs):
    return tuple(
        (
            Quote(
                i * 3600,
                "BTC-USD",
                D(str(p)),
                D(str(p)),
                D("1000"),
                D("0.001"),
                D("1"),
                D("0.000001"),
                **kwargs
            ),
        )
        for i, p in enumerate(prices)
    )


def config(**kwargs):
    return replace(
        ReplayConfig(
            initial_cash=D("100"),
            fee_bps=D(0),
            slippage_bps=D(0),
            reserve_fraction=D(0),
            pair_cap_fraction=D(1),
        ),
        **kwargs
    )


def roundtrip(view):
    if len(view.history) == 1:
        return (Order("BTC-USD", "BUY", D("20")),)
    if len(view.history) == 2 and view.lots:
        return (
            Order("BTC-USD", "SELL", view.lots[0].quantity, (view.lots[0].lot_id,)),
        )
    return ()


def test_later_quote_fills_and_exact_profit():
    result = replay(frames([10, 20, 30]), roundtrip, config())
    assert result.fills[0].timestamp == 3600
    assert result.fills[0].price == D(20)
    assert result.cash == D(110)
    assert result.metrics["realized_pnl"] == D(10)
    assert result.metrics["capital_hours"] == D(20)
    assert result.lots == ()


def test_fees_spread_slippage_and_marked_losses():
    data = tuple((replace(f[0], ask=D(11)),) for f in frames([10, 10, 10]))
    result = replay(data, roundtrip, config(fee_bps=D(100), slippage_bps=D(100)))
    buy, sell = result.fills
    assert buy.price == D("11.11") and sell.price == D("9.9")
    assert (
        result.cash
        == D(100)
        - buy.quantity * buy.price
        - buy.fee
        + sell.quantity * sell.price
        - sell.fee
    )
    assert result.metrics["total_pnl"] < 0
    assert result.metrics["fee_drag_quote"] == buy.fee + sell.fee


def test_partial_fill_and_exact_lot_basis_release():
    def policy(v):
        if len(v.history) == 1:
            return (Order("BTC-USD", "BUY", D(20)),)
        if len(v.history) == 2:
            return (Order("BTC-USD", "SELL", v.lots[0].quantity, (1,)),)
        return ()

    r = replay(frames([10, 10, 20]), policy, config(fill_fraction=D(".5")))
    assert r.fills[0].quantity == D(1)
    assert r.fills[1].quantity == D(".5")
    assert r.fills[1].basis_released == D(5)
    assert r.cash == D(100) and r.lots[0].basis == D(5)
    assert r.metrics["realized_pnl"] == D(5)
    assert r.metrics["unrealized_pnl"] == D(5)


def test_zero_depth_never_fills_even_with_full_fill_fraction():
    data = tuple((replace(f[0], depth_base=D(0)),) for f in frames([10, 10, 10]))
    r = replay(data, Candidate("dca"), config())
    assert r.fills == () and r.cash == D(100)


def test_partial_fill_can_be_below_order_minimum():
    data = tuple(
        (replace(f[0], depth_base=D(".01"), min_base=D(1)),) for f in frames([10, 10])
    )
    r = replay(data, lambda v: (Order("BTC-USD", "BUY", D(20)),), config())
    assert r.fills[0].quantity == D(".01")


def test_cash_reserve_and_pair_cap():
    r = replay(
        frames([10] * 8),
        Candidate("dca", ticket=D(100), interval=1),
        config(reserve_fraction=D(".25"), pair_cap_fraction=D(".4")),
    )
    assert r.cash >= D(60)
    assert sum(l.basis for l in r.lots) <= D(40)


def test_minima_rechecked_at_fill_time():
    data = frames([10, 10])
    data = (data[0], (replace(data[1][0], min_quote=D(30)),))
    r = replay(data, lambda v: (Order("BTC-USD", "BUY", D(20)),), config())
    assert not r.fills


def test_oversell_unknown_and_duplicate_ownership_rejected():
    for ids, amount in [((99,), D(1)), ((1,), D(100))]:

        def policy(v):
            if len(v.history) == 1:
                return (Order("BTC-USD", "BUY", D(20)),)
            return (Order("BTC-USD", "SELL", amount, ids),)

        r = replay(frames([10, 10, 10]), policy, config())
        assert len(r.fills) == 1 and r.lots[0].quantity == D(2)
    with pytest.raises(ValueError):
        Order("BTC-USD", "SELL", D(1), (1, 1))


def test_recovery_recycling_preserves_entry():
    def policy(v):
        n = len(v.history)
        if n == 1:
            return (Order("BTC-USD", "BUY", D(20)),)
        if n == 2:
            return (Order("BTC-USD", "BUY", D(10), recovery=True),)
        if n == 3:
            return (Order("BTC-USD", "SELL", v.lots[-1].quantity, (2,)),)
        return ()

    r = replay(frames([10, 10, 5, 8]), policy, config())
    assert len(r.lots) == 1 and r.lots[0].lot_id == 1
    assert r.metrics["realized_pnl"] == D(6)
    assert r.metrics["unrealized_pnl"] == D(-4)
    assert r.metrics["total_pnl"] == D(2)


def test_causal_prefix_identical_when_future_changes():
    p = Candidate("recycling")
    a = replay(frames([10, 10, 8, 9, 11, 12]), p, config())
    b = replay(frames([10, 10, 8, 9, 2, 1]), p, config())
    assert a.samples[:4] == b.samples[:4]
    assert [f for f in a.fills if f.timestamp <= 10800] == [
        f for f in b.fills if f.timestamp <= 10800
    ]


def test_latency_and_end_orders_are_not_fabricated_fills():
    r = replay(frames([10, 20, 30]), Candidate("dca"), config(latency_steps=2))
    assert r.fills[0].price == D(30)
    assert len(r.fills) == 1
    assert (
        replay(
            frames([10, 10]), Candidate("dca"), config(latency_steps=3)
        ).expired_orders
        == 1
    )


def test_same_pair_one_pending_mutation():
    r = replay(
        frames([10, 10]), lambda v: (Order("BTC-USD", "BUY", D(10)),) * 2, config()
    )
    assert len(r.fills) == 1
    assert any(reason == "one_pending_mutation_per_pair" for _, _, reason in r.rejected)


@pytest.mark.parametrize("mutation", ["time", "missing", "quote", "alias"])
def test_bad_universe_and_time_rejected(mutation):
    a = frames([10, 10])
    if mutation == "time":
        a = (a[0], a[0])
    elif mutation == "missing":
        a = (a[0], ())
    elif mutation == "quote":
        a = tuple((f[0], replace(f[0], pair="ETH-EUR")) for f in a)
    else:
        a = tuple((f[0], replace(f[0], pair="XBT-USD")) for f in a)
    with pytest.raises(ValueError):
        replay(a, Candidate("cash"), config())


def test_shared_cash_multiasset_never_double_spent():
    data = tuple(
        tuple(replace(f[0], pair=p) for p in ("BTC-USD", "ETH-USD"))
        for f in frames([10] * 4)
    )
    r = replay(data, Candidate("dca", ticket=D(80)), config())
    assert r.cash == D(0)
    assert sum(l.basis for l in r.lots) == D(100)


def test_seeded_stress_accounting_all_candidates():
    randomizer = random.Random(7)
    prices = [D(10)]
    for _ in range(150):
        prices.append(prices[-1] * (D(1) + D(str(randomizer.uniform(-0.12, 0.12)))))
    data = frames(prices)
    results = compare(data, config(fee_bps=D(40), fill_fraction=D(".37")))
    assert results == compare(data, config(fee_bps=D(40), fill_fraction=D(".37")))
    for result in results.values():
        assert abs(
            result.metrics["total_pnl"]
            - result.metrics["realized_pnl"]
            - result.metrics["unrealized_pnl"]
        ) < D("1e-18")
        assert result.cash >= 0
    assert results["cash"].metrics["total_pnl"] == 0


def test_csv_validation(tmp_path):
    path = tmp_path / "quotes.csv"
    path.write_text(
        "timestamp,pair,bid,ask,depth_base,min_base,min_quote,step_base\n0,BTC-USD,10,10,10,.01,1,.01\n3600,BTC-USD,11,11,10,.01,1,.01\n"
    )
    assert len(load_csv(path)) == 2


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_invalid_costs_fail_closed(value):
    with pytest.raises(ValueError):
        ReplayConfig(fee_bps=D(value))


def test_cli_report_is_reproducible_and_contains_audit_inputs(tmp_path, monkeypatch):
    import json
    import sys
    from spectra_core.strategy_replay import main

    source = tmp_path / "input.csv"
    output = tmp_path / "result.json"
    source.write_text(
        "timestamp,pair,bid,ask,depth_base,min_base,min_quote,step_base\n0,BTC-USD,10,11,5,.01,1,.01\n3600,BTC-USD,10,11,5,.01,1,.01\n7200,BTC-USD,12,13,5,.01,1,.01\n"
    )
    monkeypatch.setattr(
        sys, "argv", ["replay", str(source), "--fee-bps", "40", "--output", str(output)]
    )
    main()
    first = output.read_bytes()
    main()
    assert output.read_bytes() == first
    report = json.loads(first)
    assert len(report["results"]) == 5
    assert report["candidate_parameters"]["recovery"]["mode"] == "recovery"
    assert len(report["engine_source_sha256"]) == 64
    assert report["results"]["cash"]["metrics"]["total_pnl"] == "0"
