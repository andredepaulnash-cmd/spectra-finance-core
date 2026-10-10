"""Offline price/cash bounds for the protected single-parcel LIMIT adapter."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
from decimal import Inexact, Rounded, localcontext

import pytest

import mcp_servers.hummingbot_api.tools.execution_book as book_tool
from spectra_core.execution_policy import (
    build_protected_buy_limit_config,
    build_protected_sell_limit_price,
)
from spectra_core.kraken_market_fabric import KrakenTradingRule


def rule(**changes):
    base = KrakenTradingRule(
        trading_pair="XRP-USD",
        min_order_size=D("0.1"),
        min_notional_size=D("1"),
        min_order_value=D("1"),
        min_price_increment=D("0.01"),
        min_base_amount_increment=D("0.001"),
        min_quote_amount_increment=D("0.01"),
        supports_limit_orders=True,
        supports_market_orders=True,
    )
    return replace(base, **changes)


def dca(**changes):
    config = {
        "type": "dca_executor",
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "side": 1,
        "leverage": 1,
        "amounts_quote": ["40"],
        "prices": ["10"],
        "mode": "TAKER",
        "id": "proposal-1:buy",
        "controller_id": "spectra",
        "level_id": "recovery-1",
    }
    return dict(config, **changes)


def buy(config=None, *, executor_type="dca_executor", **changes):
    inputs = dict(
        trading_rule=rule(),
        best_ask=D("10.041"),
        market_observed_at=1000.0,
        now=1001.0,
        max_price_slippage_bps=D("50"),
        fee_allowance_bps=D("40"),
    )
    inputs.update(changes)
    return build_protected_buy_limit_config(executor_type, config or dca(), **inputs)


def test_single_dca_parcel_has_fixed_price_and_fee_inclusive_budget():
    native = dca(take_profit="0.0174", stop_loss="0.02", time_limit=3600)
    untouched = deepcopy(native)
    result = buy(native)
    assert native == untouched
    assert result.authorized_quote_budget == D(40)
    assert result.reference_price == D(10)
    assert result.price_cap == D("10.05")
    assert result.limit_price == D("10.05")
    assert result.amount_base == D("3.964")
    assert result.max_total_quote == D("39.9975528")
    assert result.max_total_quote <= result.authorized_quote_budget
    assert result.executor_config == {
        "id": "proposal-1:buy",
        "controller_id": "spectra",
        "level_id": "recovery-1",
        "type": "order_executor",
        "connector_name": "kraken",
        "trading_pair": "XRP-USD",
        "side": 1,
        "leverage": 1,
        "position_action": "OPEN",
        "amount": "3.964",
        "price": "10.05",
        "execution_strategy": "LIMIT",
    }


def test_authority_stays_native_budget_when_venue_step_and_fees_shrink_order():
    result = buy(trading_rule=rule(min_base_amount_increment=D(1)))
    assert result.authorized_quote_budget == D(40)
    assert result.amount_base == D(3)
    assert result.max_principal_quote == D("30.15")
    assert result.max_fee_quote == D("0.1206")
    assert result.max_total_quote == D("30.2706")
    assert result.authorized_quote_budget - result.max_total_quote > D(0)


def test_price_and_fee_geometry_is_exact_despite_tiny_ambient_context():
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        result = buy()
        sell_price = sell()
        assert context.prec == 2
        assert context.traps[Inexact] is True
    assert result.amount_base == D("3.964")
    assert result.max_principal_quote == D("39.8382")
    assert result.max_fee_quote == D("0.1593528")
    assert result.max_total_quote == D("39.9975528")
    assert sell_price == D("10.04")


@pytest.mark.parametrize("ask", ["10.05001", "10.06", "99"])
def test_ask_cannot_raise_original_reference_or_cross_price_cap(ask):
    with pytest.raises(ValueError, match="immutable buy price cap"):
        buy(best_ask=D(ask))


def test_reference_slippage_cap_rounds_down_before_ask_rounds_up():
    with pytest.raises(ValueError, match="immutable buy price cap"):
        buy(dca(prices=["10.004"]), best_ask=D("10.004"), max_price_slippage_bps=D(0))
    result = buy(
        dca(prices=["10.004"]), best_ask=D("10.00"), max_price_slippage_bps=D(0)
    )
    assert result.price_cap == D("10.00")
    assert result.limit_price == D("10.00")


@pytest.mark.parametrize(
    "config",
    [
        dca(amounts_quote=["20", "20"], prices=["10", "9"]),
        dca(amounts_quote=["40"], prices=["10", "9"]),
        dca(amounts_quote=[], prices=[]),
    ],
)
def test_multilevel_dca_never_collapses_or_reallocates(config):
    with pytest.raises(ValueError, match="exactly one funded parcel"):
        buy(config)


def test_explicit_single_grid_parcel_converts_without_autonomous_closes():
    native = dict(
        type="grid_executor",
        connector_name="kraken",
        trading_pair="XRP-USD",
        side=1,
        leverage=1,
        total_amount_quote="40",
        min_order_amount_quote="40",
        max_open_orders=1,
        max_orders_per_batch=1,
        start_price="9.8",
        end_price="10.2",
        triple_barrier_config={"take_profit": "0.0174", "open_order_type": 1},
    )
    result = buy(native, executor_type="grid_executor")
    assert result.authorized_quote_budget == D(40)
    assert result.reference_price == D(10)
    assert result.executor_config["type"] == "order_executor"
    assert "triple_barrier_config" not in result.executor_config
    assert result.executor_config["execution_strategy"] == "LIMIT"


@pytest.mark.parametrize(
    "changes",
    [
        dict(min_order_amount_quote="20"),
        dict(max_open_orders=2),
        dict(max_open_orders=True),
        dict(max_orders_per_batch=2),
    ],
)
def test_grid_without_explicit_single_parcel_fails_closed(changes):
    native = dict(
        type="grid_executor",
        connector_name="kraken",
        trading_pair="XRP-USD",
        side=1,
        leverage=1,
        total_amount_quote="40",
        min_order_amount_quote="40",
        max_open_orders=1,
        max_orders_per_batch=1,
        start_price="9.8",
        end_price="10.2",
    )
    with pytest.raises(ValueError, match="parcel"):
        buy(dict(native, **changes), executor_type="grid_executor")


def test_native_order_price_cap_cannot_be_relaxed_by_slippage():
    native = dict(
        type="order_executor",
        connector_name="kraken",
        trading_pair="XRP-USD",
        side=1,
        leverage=1,
        amount="4",
        price="10",
        execution_strategy="MARKET",
    )
    with pytest.raises(ValueError, match="immutable buy price cap"):
        buy(native, executor_type="order_executor", best_ask=D("10.01"))
    result = buy(native, executor_type="order_executor", best_ask=D("9.99"))
    assert result.authorized_quote_budget == D(40)
    assert result.price_cap == D(10)
    assert result.executor_config["execution_strategy"] == "LIMIT"
    cheaper = buy(native, executor_type="order_executor", best_ask=D("9.00"))
    assert cheaper.amount_base == D(4)
    assert cheaper.max_total_quote <= D(40)


@pytest.mark.parametrize(
    "field,value",
    [
        ("connector_name", "binance"),
        ("side", 2),
        ("side", True),
        ("side", None),
        ("leverage", 2),
        ("trading_pair", "BTC-USD"),
        ("execution_mode", "MAKER"),
        ("timeinforce", "IOC"),
        ("time_in_force", "IOC"),
    ],
)
def test_wrong_scope_or_unsupported_fields_fail_closed(field, value):
    with pytest.raises(ValueError):
        buy(dca(**{field: value}))


@pytest.mark.parametrize(
    "changes",
    [
        dict(supports_limit_orders=False),
        dict(min_price_increment=D(0)),
        dict(min_base_amount_increment=D("NaN")),
        dict(min_order_size=D(5)),
        dict(min_order_value=D(41)),
    ],
)
def test_invalid_or_unfundable_current_rules_fail_closed(changes):
    with pytest.raises(ValueError):
        buy(trading_rule=rule(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        dict(best_ask=D("NaN")),
        dict(best_ask=D(0)),
        dict(max_price_slippage_bps=D(-1)),
        dict(max_price_slippage_bps=D(10000)),
        dict(fee_allowance_bps=D("Infinity")),
        dict(fee_allowance_bps=D(-1)),
        dict(market_observed_at=None),
        dict(market_observed_at=True),
        dict(market_observed_at=970.9),
        dict(market_observed_at=1001.1),
    ],
)
def test_invalid_price_cost_or_untrusted_observation_cannot_build_order(changes):
    with pytest.raises(ValueError):
        buy(**changes)


def sell(floor="10.025", bid="10.041", **changes):
    inputs = dict(
        best_bid=D(bid), trading_rule=rule(), market_observed_at=1000, now=1001
    )
    inputs.update(changes)
    return build_protected_sell_limit_price(D(floor), **inputs)


def test_sell_price_is_at_or_above_floor_and_at_or_below_executable_bid():
    price = sell()
    assert price == D("10.04")
    assert D("10.025") <= price <= D("10.041")


@pytest.mark.parametrize("floor,bid", [("10.045", "10.049"), ("10.05", "10.04")])
def test_sell_never_rounds_profit_floor_down_to_force_marketability(floor, bid):
    with pytest.raises(ValueError, match="immutable protected SELL floor"):
        sell(floor, bid)


@pytest.mark.parametrize(
    "changes",
    [
        dict(best_bid=D("NaN")),
        dict(best_bid=D(-1)),
        dict(market_observed_at=970.9),
        dict(market_observed_at=1001.1),
        dict(trading_rule=rule(supports_limit_orders=False)),
    ],
)
def test_sell_invalid_or_old_market_proof_fails_closed(changes):
    with pytest.raises(ValueError):
        sell(**changes)


def venue_responses():
    return {
        "AssetPairs": {
            "error": [],
            "result": {
                "XXRPZUSD": {
                    "wsname": "XRP/USD",
                    "altname": "XRPUSD",
                    "aclass_base": "currency",
                    "aclass_quote": "currency",
                    "base": "XXRP",
                    "quote": "ZUSD",
                    "ordermin": "0.1",
                    "costmin": "1",
                }
            },
        },
        "Depth": {
            "error": [],
            "result": {
                "XXRPZUSD": {
                    "bids": [["10.04", "5", 995], ["10.03", "9", 990]],
                    "asks": [["10.05", "4", 996], ["10.06", "8", 991]],
                }
            },
        },
    }


def fetch_book(*, responses=None, clock=None):
    replies = deepcopy(responses or venue_responses())
    calls = []

    async def fake_fetcher(endpoint, params):
        calls.append((endpoint, params))
        return replies[endpoint]

    result = asyncio.run(
        book_tool.fetch_protected_kraken_book(
            rule(), fetcher=fake_fetcher, clock=clock or (lambda: 1000.0)
        )
    )
    return result, calls


def test_fresh_book_proves_pair_and_uses_public_venue_depth_without_cached_api():
    book, calls = fetch_book()
    assert calls == [
        ("AssetPairs", {"pair": "XRPUSD"}),
        ("Depth", {"pair": "XRPUSD", "count": 10}),
    ]
    assert book.pair == "XRP-USD"
    assert book.exchange_pair == "XRPUSD"
    assert book.best_bid == D("10.04")
    assert book.best_ask == D("10.05")
    assert book.source == "kraken_public_rest_depth"
    assert book.requested_at == book.received_at == book.market_observed_at == 1000.0
    assert len(book.response_sha256) == 64
    assert book.venue_min_order_size == D("0.1")
    assert book.venue_min_cost_quote == D("1")
    assert book.venue_base_asset == "XRP"
    assert book.venue_quote_asset == "USD"
    assert book.venue_rules_observed_at == 1000.0
    assert len(book.venue_rules_sha256) == 64
    assert buy(
        best_ask=book.best_ask, market_observed_at=book.market_observed_at
    ).limit_price == D("10.05")


@pytest.mark.parametrize(
    "mutation",
    [
        "venue_error",
        "wrong_wsname",
        "ambiguous_mapping",
        "wrong_depth_pair",
        "empty_bids",
        "nan_ask",
        "negative_amount",
        "unsorted_bids",
        "duplicate_bids",
        "crossed_book",
        "future_clock",
        "malformed_level",
    ],
)
def test_malformed_or_cross_scope_venue_book_fails_closed(mutation):
    replies = venue_responses()
    depth = replies["Depth"]["result"]["XXRPZUSD"]
    clock = None
    if mutation == "venue_error":
        replies["Depth"]["error"] = ["EGeneral:Unavailable"]
    elif mutation == "wrong_wsname":
        replies["AssetPairs"]["result"]["XXRPZUSD"]["wsname"] = "BTC/USD"
    elif mutation == "ambiguous_mapping":
        replies["AssetPairs"]["result"]["XRPUSD"] = {
            "wsname": "XRP/USD",
            "altname": "XRPUSD",
        }
    elif mutation == "wrong_depth_pair":
        replies["Depth"]["result"] = {"XXBTZUSD": depth}
    elif mutation == "empty_bids":
        depth["bids"] = []
    elif mutation == "nan_ask":
        depth["asks"][0][0] = "NaN"
    elif mutation == "negative_amount":
        depth["asks"][0][1] = "-1"
    elif mutation == "unsorted_bids":
        depth["bids"].reverse()
    elif mutation == "duplicate_bids":
        depth["bids"][1][0] = depth["bids"][0][0]
    elif mutation == "crossed_book":
        depth["bids"][0][0] = "10.05"
    elif mutation == "malformed_level":
        depth["asks"][0] = ["10.05", "4"]
    elif mutation == "future_clock":
        values = iter([1000, 1000, 999])
        clock = lambda: next(values)
    with pytest.raises(ValueError):
        fetch_book(responses=replies, clock=clock)


def test_venue_request_over_five_seconds_is_rejected_even_if_fetcher_returns():
    values = iter([1000, 1000, 1006])
    with pytest.raises(ValueError, match="five seconds"):
        fetch_book(clock=lambda: next(values))


def test_transport_error_and_timeout_are_bounded_without_any_trading_call(monkeypatch):
    async def failed(endpoint, params):
        raise ConnectionError("offline unavailable")

    with pytest.raises(ValueError, match="failed or timed out"):
        asyncio.run(
            book_tool.fetch_protected_kraken_book(
                rule(), fetcher=failed, clock=lambda: 1000
            )
        )
    monkeypatch.setattr(book_tool, "REQUEST_TIMEOUT_SECONDS", 0.001)

    async def stalled(endpoint, params):
        await asyncio.Future()

    with pytest.raises(ValueError, match="failed or timed out"):
        asyncio.run(
            book_tool.fetch_protected_kraken_book(
                rule(), fetcher=stalled, clock=lambda: 1000
            )
        )


def test_canonical_btc_mapping_resolves_xbt_without_cross_asset_scope():
    calls = []

    async def fake(endpoint, params):
        calls.append((endpoint, params))
        if endpoint == "AssetPairs":
            return {
                "error": [],
                "result": {
                    "XXBTZUSD": {
                        "wsname": "XBT/USD",
                        "altname": "XBTUSD",
                        "aclass_base": "currency",
                        "aclass_quote": "currency",
                        "base": "XXBT",
                        "quote": "ZUSD",
                        "ordermin": "0.0001",
                        "costmin": "0.5",
                    }
                },
            }
        return {
            "error": [],
            "result": {
                "XXBTZUSD": {"bids": [["99", "1", 995]], "asks": [["100", "1", 995]]}
            },
        }

    book = asyncio.run(
        book_tool.fetch_protected_kraken_book(
            rule(trading_pair="BTC-USD"), fetcher=fake, clock=lambda: 1000
        )
    )
    assert book.pair == "BTC-USD"
    assert book.exchange_pair == "XBTUSD"
    assert calls[0] == ("AssetPairs", {"pair": "XBTUSD"})


@pytest.mark.parametrize("field", ["ordermin", "costmin"])
@pytest.mark.parametrize(
    "value", [None, "", "invalid", "NaN", "Infinity", "0", "-1", True]
)
def test_fresh_venue_minima_must_be_present_finite_and_strictly_positive(field, value):
    replies = venue_responses()
    row = replies["AssetPairs"]["result"]["XXRPZUSD"]
    if value is None:
        row.pop(field)
    else:
        row[field] = value
    with pytest.raises(ValueError, match=field):
        fetch_book(responses=replies)


@pytest.mark.parametrize(
    "field,value",
    [
        ("quote", "ZEUR"),
        ("quote", None),
        ("base", "XXBT"),
        ("aclass_base", "tokenized_asset"),
        ("aclass_quote", "tokenized_asset"),
    ],
)
def test_minimum_evidence_cannot_cross_its_quote_base_or_spot_scope(field, value):
    replies = venue_responses()
    replies["AssetPairs"]["result"]["XXRPZUSD"][field] = value
    with pytest.raises(ValueError, match="base/quote|spot scope"):
        fetch_book(responses=replies)


def test_minimum_hash_changes_independently_of_unchanged_depth():
    original, _ = fetch_book()
    replies = venue_responses()
    replies["AssetPairs"]["result"]["XXRPZUSD"]["costmin"] = "39.84"
    book, _ = fetch_book(responses=replies)
    assert book.venue_min_cost_quote == D("39.84")
    assert book.venue_rules_sha256 != original.venue_rules_sha256
    assert book.response_sha256 == original.response_sha256


def test_freshness_covers_older_minimum_metadata_before_depth_receipt():
    moments = iter([1000, 1000, 1002, 1003, 1005])
    book, _ = fetch_book(clock=lambda: next(moments))
    assert book.received_at == 1005
    assert book.venue_rules_observed_at == book.market_observed_at == 1002
    with pytest.raises(ValueError, match="stale"):
        buy(
            best_ask=book.best_ask,
            market_observed_at=book.market_observed_at,
            now=1032.01,
        )


def test_venue_cost_minimum_applies_to_quantized_principal_without_fee_or_budget_credit():
    # Q=40 remains authorized, but legal base-step rounding plus fee allowance
    # makes principal only 39.8382. Cost minimum must inspect that principal.
    replies = venue_responses()
    replies["AssetPairs"]["result"]["XXRPZUSD"]["costmin"] = "39.84"
    book, _ = fetch_book(responses=replies)
    api_rule = rule(min_notional_size=D(0), min_order_value=D(0))
    trusted = replace(
        api_rule,
        min_order_size=max(api_rule.min_order_size, book.venue_min_order_size),
        min_notional_size=max(api_rule.effective_min_quote, book.venue_min_cost_quote),
    )
    with pytest.raises(ValueError, match="venue minimums"):
        buy(trading_rule=trusted, best_ask=book.best_ask)
    exact = replace(trusted, min_notional_size=D("39.8382"))
    admitted = buy(trading_rule=exact, best_ask=book.best_ask)
    assert admitted.max_principal_quote == D("39.8382")
    assert admitted.max_total_quote < admitted.authorized_quote_budget


def test_venue_base_minimum_is_checked_after_base_step_rounding():
    replies = venue_responses()
    replies["AssetPairs"]["result"]["XXRPZUSD"]["ordermin"] = "3.9641"
    book, _ = fetch_book(responses=replies)
    with pytest.raises(ValueError, match="venue minimums"):
        buy(trading_rule=rule(min_order_size=book.venue_min_order_size))


def test_crypto_quote_minimum_keeps_pair_quote_units_instead_of_fee_volume_currency():
    async def fake(endpoint, params):
        if endpoint == "AssetPairs":
            return {
                "error": [],
                "result": {
                    "XETHXXBT": {
                        "wsname": "ETH/XBT",
                        "altname": "ETHXBT",
                        "aclass_base": "currency",
                        "aclass_quote": "currency",
                        "base": "XETH",
                        "quote": "XXBT",
                        "fee_volume_currency": "ZUSD",
                        "ordermin": "0.01",
                        "costmin": "0.00002",
                    }
                },
            }
        assert params == {"pair": "ETHXBT", "count": 10}
        return {
            "error": [],
            "result": {
                "XETHXXBT": {
                    "bids": [["0.019", "1", 995]],
                    "asks": [["0.02", "1", 995]],
                }
            },
        }

    book = asyncio.run(
        book_tool.fetch_protected_kraken_book(
            rule(trading_pair="ETH-BTC"), fetcher=fake, clock=lambda: 1000
        )
    )
    assert book.venue_base_asset == "ETH"
    assert book.venue_quote_asset == "BTC"
    assert book.venue_min_order_size == D("0.01")
    assert book.venue_min_cost_quote == D("0.00002")
