import asyncio
from decimal import Decimal

import pytest

from spectra_core.kraken_snapshot import build_kraken_capital_snapshot


D = Decimal


class Portfolio:
    def __init__(self, state):
        self.state = state
        self.calls = []

    async def get_state(self, **kwargs):
        self.calls.append(kwargs)
        return self.state


class Client:
    def __init__(self, state):
        self.portfolio = Portfolio(state)


def test_snapshot_uses_available_usd_and_values_holdings():
    client = Client({
        "master_account": {
            "kraken": [
                {
                    "token": "USD",
                    "units": 600,
                    "available_units": 550,
                    "value": 600,
                },
                {
                    "token": "BTC",
                    "units": 0.001,
                    "available_units": 0.001,
                    "value": 100,
                },
                {
                    "token": "ETH",
                    "units": 0.02,
                    "available_units": 0.02,
                    "value": 80,
                },
            ]
        }
    })

    snap = asyncio.run(
        build_kraken_capital_snapshot(
            client,
            account_name="master_account",
            trading_pair="BTC-USD",
            recovery_exposure_quote=D("0"),
        )
    )

    assert snap.free_quote == D("550")
    assert snap.committed_quote == D("230")  # $50 locked + $180 holdings
    assert snap.pair_exposure_quote == D("100")
    assert snap.position_exposure_quote == D("100")
    assert snap.recovery_exposure_quote == D("0")

    assert client.portfolio.calls == [{
        "account_names": ["master_account"],
        "connector_names": ["kraken"],
        "refresh": True,
    }]


def test_snapshot_fails_without_quote_balance():
    client = Client({
        "master_account": {
            "kraken": [{
                "token": "BTC",
                "units": 0.001,
                "available_units": 0.001,
                "value": 100,
            }]
        }
    })

    with pytest.raises(RuntimeError, match="no USD balance"):
        asyncio.run(
            build_kraken_capital_snapshot(
                client,
                account_name="master_account",
                trading_pair="BTC-USD",
            recovery_exposure_quote=D("0"),
            )
        )


def test_snapshot_fails_when_nonquote_value_is_unknown():
    client = Client({
        "master_account": {
            "kraken": [
                {
                    "token": "USD",
                    "units": 500,
                    "available_units": 500,
                    "value": 500,
                },
                {
                    "token": "BTC",
                    "units": 0.001,
                    "available_units": 0.001,
                    "value": None,
                },
            ]
        }
    })

    with pytest.raises(RuntimeError, match="no USD value"):
        asyncio.run(
            build_kraken_capital_snapshot(
                client,
                account_name="master_account",
                trading_pair="BTC-USD",
            recovery_exposure_quote=D("0"),
            )
        )


def test_snapshot_rejects_impossible_available_balance():
    client = Client({
        "master_account": {
            "kraken": [{
                "token": "USD",
                "units": 500,
                "available_units": 501,
                "value": 500,
            }]
        }
    })

    with pytest.raises(RuntimeError, match="exceeds total units"):
        asyncio.run(
            build_kraken_capital_snapshot(
                client,
                account_name="master_account",
                trading_pair="BTC-USD",
            recovery_exposure_quote=D("0"),
            )
        )


def test_recovery_attribution_does_not_inflate_economic_exposure():
    client = Client({
        "master_account": {
            "kraken": [
                {
                    "token": "USD",
                    "units": 500,
                    "available_units": 500,
                    "value": 500,
                },
                {
                    "token": "BTC",
                    "units": 0.001,
                    "available_units": 0.001,
                    "value": 100,
                },
            ]
        }
    })

    snap = asyncio.run(
        build_kraken_capital_snapshot(
            client,
            account_name="master_account",
            trading_pair="BTC-USD",
            recovery_exposure_quote=D("35"),
        )
    )

    # $35 is attribution within the existing $100 BTC exposure.
    # It is not another $35 of economic exposure.
    assert snap.committed_quote == D("100")
    assert snap.pair_exposure_quote == D("100")
    assert snap.position_exposure_quote == D("100")
    assert snap.recovery_exposure_quote == D("35")
