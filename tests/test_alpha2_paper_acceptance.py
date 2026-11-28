"""Full Alpha 2.0 paper path: market data -> scanner -> Condor -> Governor -> Hummingbot -> ledger.

This is an isolated deterministic acceptance fixture. Its USD/volume values are
test inputs only; it does not establish or change production entry configuration.
"""
import asyncio
import base64
import json
import time
from decimal import Decimal

import httpx

from spectra_alpha2.condor import CondorAnalyzer
from spectra_alpha2.execution import HummingbotPaperExecutionClient
from spectra_alpha2.governor import CapitalGovernor
from spectra_alpha2.market_data import HummingbotPaperDataClient, ResearchScanner
from spectra_alpha2.supervisor import PortfolioSupervisor


SAFE_ENV = {
    "SPECTRA_PAPER_ONLY": "true",
    "SPECTRA_ALLOW_LIVE_EXECUTION": "false",
    "SPECTRA_PAPER_CONNECTORS": "kraken_paper_trade",
    "SPECTRA_DISPATCH_POLICY_VERSION": "spectra-alpha2-v1",
}
API_USER = "alpha2-acceptance"
API_PASSWORD = "isolated-test-only"


class PaperVenue:
    """Deterministic Hummingbot API surface; never connects to an exchange."""

    def __init__(self):
        self.orders = []
        self.order_id = "paper-acceptance-order-1"

    def handle(self, request):
        path = request.url.path
        if path == "/market-data/tickers":
            now = time.time()
            return httpx.Response(200, json={"tickers": {"kraken_paper_trade": {
                "BTC-USD": {
                    "price": "100", "quote_volume": "15000000",
                    "base_volume": "150000", "timestamp": now,
                }
            }}})
        if path == "/connectors/kraken_paper_trade/trading-rules":
            return httpx.Response(200, json={"BTC-USD": {
                "supports_limit_orders": True, "min_order_size": "0.001",
                "min_notional_size": "5", "min_base_amount_increment": "0.001",
                "min_price_increment": "0.01",
            }})
        if path == "/market-data/candles":
            assert request.read().find(b'"connector_name":"kraken"') >= 0
            return httpx.Response(200, json=[
                {"timestamp": time.time() - 86400, "close": "90"},
                {"timestamp": time.time() - 82800, "close": "91"},
            ])
        if path == "/market-data/order-book":
            return httpx.Response(200, json={
                "bids": [{"price": "99.99", "amount": "100"}],
                "asks": [{"price": "100.01", "amount": "100"}],
            })
        if path == "/market-data/trading-pair/add":
            payload = json.loads(request.read())
            assert payload["connector_name"] == "kraken_paper_trade"
            assert payload["trading_pair"] == "BTC-USD"
            assert payload["account_name"] == "master_account"
            return httpx.Response(200, json={"success": True})
        if path == "/trading/orders":
            body = request.read().decode()
            assert request.headers.get("X-Spectra-Dispatch-Receipt")
            assert '"connector_name":"kraken_paper_trade"' in body
            assert '"trade_type":"BUY"' in body
            assert '"order_type":"MARKET"' in body
            self.orders.append(body)
            self.filled_amount = json.loads(body)["amount"]
            return httpx.Response(201, json={
                "order_id": self.order_id, "status": "submitted",
            })
        if path == "/trading/orders/search":
            filters = json.loads(request.read())
            assert isinstance(filters["start_time"], int)
            assert isinstance(filters["end_time"], int)
            assert filters["start_time"] < filters["end_time"]
            return httpx.Response(200, json={"data": [{
                "order_id": self.order_id, "status": "FILLED",
            }]})
        if path == "/trading/trades":
            filters = json.loads(request.read())
            assert isinstance(filters["start_time"], int)
            assert isinstance(filters["end_time"], int)
            assert filters["start_time"] < filters["end_time"]
            return httpx.Response(200, json={"data": [{
                "order_id": self.order_id, "amount": self.filled_amount,
                "price": "100", "fee_paid": "0.1196", "fee_currency": "USD",
            }]})
        raise AssertionError(f"unexpected paper API request: {request.method} {path}")


def test_alpha2_real_components_discover_submit_fill_and_survive_restart(tmp_path, monkeypatch):
    # Dispatch signing reads process environment independently of client config.
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.setenv("SPECTRA_PAPER_CONNECTORS", "kraken_paper_trade")
    monkeypatch.setenv("SPECTRA_DISPATCH_POLICY_VERSION", "spectra-alpha2-v1")
    monkeypatch.setenv("SPECTRA_DISPATCH_PRIVATE_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="))
    venue = PaperVenue()
    transport = httpx.MockTransport(venue.handle)
    db_path = tmp_path / "alpha2-acceptance.sqlite3"

    async def run_cycle(governor):
        data = HummingbotPaperDataClient(
            "http://paper-api", api_username=API_USER, api_password=API_PASSWORD,
            environment=SAFE_ENV, transport=transport,
        )
        execution = HummingbotPaperExecutionClient(
            "http://paper-api", api_username=API_USER, api_password=API_PASSWORD,
            environment=SAFE_ENV, transport=transport, poll_interval_seconds=0,
        )
        try:
            # Explicit fixture-only config; no production policy is inferred here.
            scanner = ResearchScanner(
                data, entry_quote_assets={"USD"},
                min_quote_volume_24h=Decimal("10000000"),
            )
            supervisor = PortfolioSupervisor(
                governor, scanner, CondorAnalyzer(), execution,
                target_positions=1, max_new_positions_per_cycle=1,
            )
            report = await supervisor.run_once()
            return report
        finally:
            await execution.close()
            await data.close()

    governor = CapitalGovernor(db_path, environment=SAFE_ENV)
    first = asyncio.run(run_cycle(governor))

    assert first.discovered_markets == 1
    assert first.eligible_markets == 1
    assert first.admitted_positions == 1
    assert first.active_positions_after_cycle == 1
    assert len(venue.orders) == 1

    position = governor.list_positions()[0]
    state = governor.get_position_state(position["position_id"])
    assert position["asset_id"] == "BTC-USD"
    assert position["status"] == "OPEN"
    assert state["total_quantity"] == Decimal("0.284")
    assert state["total_cost_basis_quote"] == Decimal("28.5196")

    # Reconstruct the Governor over the same durable database, as after restart.
    restarted_governor = CapitalGovernor(db_path, environment=SAFE_ENV)
    second = asyncio.run(run_cycle(restarted_governor))
    assert second.admitted_positions == 0
    assert second.skipped_existing_assets == 1
    assert len(venue.orders) == 1
    restarted_state = restarted_governor.get_position_state(position["position_id"])
    assert restarted_state["total_quantity"] == Decimal("0.284")
    assert restarted_state["total_cost_basis_quote"] == Decimal("28.5196")
