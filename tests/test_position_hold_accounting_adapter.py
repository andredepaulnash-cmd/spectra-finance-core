from decimal import Decimal

import pytest

from mcp_servers.hummingbot_api.tools.executors import (
    fetch_active_position_hold_accounting,
)
from spectra_core.position_hold_accounting import (
    position_hold_accounting_from_raw,
)


D = Decimal


def raw_hold():
    return {
        "account_name": "master_account",
        "connector_name": "kraken",
        "trading_pair": "BTC-USD",
        "controller_id": "spectra",
        "status": "ACTIVE",
        "buy_amount_base": "0.001234567890123456",
        "buy_amount_quote": "190.000000000000000001",
        "sell_amount_base": "0.000234567890123456",
        "sell_amount_quote": "40.000000000000000001",
        "realized_pnl_quote": "-1.250000000000000001",
        "cum_fees_quote": "0.220000000000000001",
        "executor_ids": ["exec-recovery-1"],
        "last_updated": "2026-09-22T13:00:00+00:00",
    }


def test_strict_hold_adapter_preserves_exact_decimal_strings():
    hold = position_hold_accounting_from_raw(
        raw_hold(),
        expected_account_name="master_account",
        expected_connector_name="kraken",
        expected_trading_pair="BTC-USD",
        expected_controller_id="spectra",
    )

    assert hold.status == "ACTIVE"
    assert hold.executor_ids == ("exec-recovery-1",)
    assert hold.buy_amount_base == D("0.001234567890123456")
    assert hold.buy_amount_quote == D("190.000000000000000001")
    assert hold.sell_amount_base == D("0.000234567890123456")
    assert hold.sell_amount_quote == D("40.000000000000000001")
    assert hold.realized_pnl_quote == D("-1.250000000000000001")
    assert hold.cum_fees_quote == D("0.220000000000000001")


def test_strict_hold_adapter_rejects_float_accounting():
    raw = raw_hold()
    raw["buy_amount_quote"] = 190.0

    with pytest.raises(
        ValueError,
        match="buy_amount_quote must be an exact decimal string",
    ):
        position_hold_accounting_from_raw(raw)


def test_strict_hold_adapter_rejects_non_active_state():
    raw = raw_hold()
    raw["status"] = "CLEARED"

    with pytest.raises(
        ValueError,
        match="status must be ACTIVE",
    ):
        position_hold_accounting_from_raw(raw)


def test_strict_hold_adapter_rejects_ambiguous_executor_ownership():
    raw = raw_hold()
    raw["executor_ids"] = [
        "exec-recovery-1",
        "exec-recovery-1",
    ]

    with pytest.raises(
        ValueError,
        match="executor_ids must be unique",
    ):
        position_hold_accounting_from_raw(raw)


def test_strict_hold_adapter_rejects_filter_mismatch():
    with pytest.raises(
        ValueError,
        match="trading_pair .* does not match requested",
    ):
        position_hold_accounting_from_raw(
            raw_hold(),
            expected_trading_pair="ETH-USD",
        )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.raised = False

    def raise_for_status(self):
        self.raised = True

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.response = None

    async def get(self, url, *, params):
        self.calls.append((url, params))
        self.response = FakeResponse(self.payload)
        return self.response


class FakeExecutors:
    def __init__(self, payload):
        self.base_url = "http://127.0.0.1:8000"
        self.session = FakeSession(payload)


class FakeClient:
    def __init__(self, payload):
        self.executors = FakeExecutors(payload)


@pytest.mark.asyncio
async def test_fetch_helper_preserves_scope_and_validates_response():
    client = FakeClient([raw_hold()])

    holds = await fetch_active_position_hold_accounting(
        client,
        account_name="master_account",
        connector_name="kraken",
        trading_pair="btc-usd",
        controller_id="spectra",
    )

    assert len(holds) == 1
    assert holds[0].executor_ids == ("exec-recovery-1",)

    assert client.executors.session.calls == [
        (
            "http://127.0.0.1:8000/executors/positions/accounting",
            {
                "account_name": "master_account",
                "connector_name": "kraken",
                "trading_pair": "BTC-USD",
                "controller_id": "spectra",
            },
        )
    ]

    assert client.executors.session.response.raised is True


@pytest.mark.asyncio
async def test_fetch_helper_rejects_non_list_payload():
    client = FakeClient({"positions": [raw_hold()]})

    with pytest.raises(
        ValueError,
        match="response must be a list",
    ):
        await fetch_active_position_hold_accounting(
            client,
            account_name="master_account",
            connector_name="kraken",
            trading_pair="BTC-USD",
            controller_id="spectra",
        )
