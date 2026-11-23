"""Live Hummingbot Kraken paper-connector smoke test (never a live exchange order).

Uses a disposable SQLite ledger and a $20 USD authorization. It exercises the
actual running Hummingbot API and kraken_paper_trade connector, then requires
a reconciled simulated fill before returning success.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .config import Alpha2Config
from .execution import HummingbotPaperExecutionClient
from .governor import CapitalGovernor
from .market_data import HummingbotPaperDataClient, ResearchScanner
from .order_builder import build_buy_market_order


async def run_smoke() -> dict:
    config = Alpha2Config.from_environment()
    environment = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="spectra-alpha2-paper-smoke-") as temp_dir:
        governor = CapitalGovernor(
            Path(temp_dir) / "paper-smoke.sqlite3",
            environment=environment,
        )
        data = HummingbotPaperDataClient(
            config.hummingbot_api_url,
            api_username=config.hummingbot_api_username,
            api_password=config.hummingbot_api_password,
            environment=environment,
        )
        execution = HummingbotPaperExecutionClient(
            config.hummingbot_api_url,
            api_username=config.hummingbot_api_username,
            api_password=config.hummingbot_api_password,
            environment=environment,
            poll_interval_seconds=1,
        )
        try:
            scanner = ResearchScanner(
                data,
                entry_quote_assets={"USD"},
                min_quote_volume_24h=Decimal("1"),
                max_spread_fraction=config.max_spread_fraction,
            )
            candidate = await scanner.inspect_pair("BTC-USD")
            if candidate is None:
                raise RuntimeError(
                    "Kraken paper connector did not provide a valid BTC-USD ticker, "
                    "trading rule, and order book; cannot prove paper execution."
                )

            position_id = str(uuid.uuid4())
            authorization = governor.reserve_initial_position(
                position_id=position_id,
                asset_id=candidate.trading_pair,
                reference_price=candidate.last_price,
                idempotency_key=f"alpha2-connector-smoke-{position_id}",
                strategy_origin="TOP_GAINER",
            )
            draft = build_buy_market_order(
                candidate,
                gross_quote_budget=authorization.gross_notional,
                max_slippage_fraction=config.max_slippage_fraction,
            )
            result = await execution.submit_authorized_buy(
                governor,
                authorization,
                draft.request_body,
                timeout_seconds=config.order_timeout_seconds,
            )
            if result.status != "FILLED" or result.filled_quantity <= 0:
                raise RuntimeError(
                    "Hummingbot paper order did not reach a reconciled FILLED state: "
                    + json.dumps(asdict(result), default=str)
                )
            positions = governor.list_positions()
            if len(positions) != 1 or positions[0]["status"] != "OPEN":
                raise RuntimeError("Paper fill did not produce exactly one OPEN ledger position.")
            state = governor.get_position_state(position_id)
            if state is None or state["total_quantity"] <= 0:
                raise RuntimeError("Paper fill was not reconciled into the durable position ledger.")
            return {
                "paper_only": True,
                "connector": "kraken_paper_trade",
                "market": candidate.trading_pair,
                "order_id": result.order_id,
                "order_status": result.status,
                "filled_quantity": str(result.filled_quantity),
                "gross_quote_notional": str(result.gross_quote_notional),
                "estimated_fee_usd": str(result.estimated_fee),
                "ledger_position_status": positions[0]["status"],
                "ledger_quantity": str(state["total_quantity"]),
                "ledger_cost_basis_usd": str(state["total_cost_basis_quote"]),
                "result": "PASS",
            }
        finally:
            await execution.close()
            await data.close()


def main() -> int:
    report = asyncio.run(run_smoke())
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
