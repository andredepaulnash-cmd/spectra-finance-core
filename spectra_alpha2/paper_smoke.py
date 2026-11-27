"""Live Hummingbot Kraken paper-connector smoke test (never a live exchange order).

Uses a disposable SQLite ledger and a $30 USD authorization. It exercises the
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
            # Do not assume a single market symbol is exposed by the connector.
            # Kraken/Hummingbot symbol normalization can differ by connector version.
            # Select only a real USD-quoted pair present in BOTH live ticker and rule maps.
            tickers, rules = await asyncio.gather(
                data.get_tickers(),
                data.get_trading_rules(),
            )
            usd_pairs = sorted(
                (
                    pair for pair in set(tickers).intersection(rules)
                    if pair.endswith("-USD")
                ),
                key=lambda pair: (pair not in {"BTC-USD", "XBT-USD"}, pair),
            )
            candidate = None
            rejected = []
            now = scanner.clock()
            for pair in usd_pairs:
                ticker = tickers.get(pair)
                rule = rules.get(pair)
                if not isinstance(ticker, dict) or not isinstance(rule, dict):
                    continue
                try:
                    candidate = await scanner._candidate_from_snapshot(
                        pair, ticker, rule, now=now, require_gainer=False
                    )
                except Exception as exc:
                    rejected.append(f"{pair}: {type(exc).__name__}: {exc}")
                    continue
                if candidate is not None:
                    break
            if candidate is None:
                probe_pair = "BTC-USD" if "BTC-USD" in usd_pairs else (usd_pairs[0] if usd_pairs else None)
                probe = {"pair": probe_pair}
                if probe_pair:
                    ticker = tickers.get(probe_pair, {})
                    rule = rules.get(probe_pair, {})
                    probe["ticker"] = ticker
                    probe["rule"] = rule
                    try:
                        probe["order_book"] = await data.get_order_book(probe_pair, depth=5)
                    except Exception as exc:
                        probe["order_book_error"] = f"{type(exc).__name__}: {exc}"
                sample = {
                    "ticker_count": len(tickers),
                    "trading_rule_count": len(rules),
                    "usd_ticker_pairs": [p for p in tickers if p.endswith("-USD")][:25],
                    "usd_rule_pairs": [p for p in rules if p.endswith("-USD")][:25],
                    "usd_intersection_pairs": usd_pairs[:25],
                    "ticker_pair_sample": sorted(tickers)[:40],
                    "ticker_quote_counts": {
                        quote: sum(1 for pair in tickers if pair.endswith("-" + quote))
                        for quote in sorted({pair.rsplit("-", 1)[1] for pair in tickers if "-" in pair})
                    },
                    "probe": probe,
                    "rejected": rejected[:10],
                }
                raise RuntimeError(
                    "Kraken paper connector did not expose a usable USD-quoted "
                    "ticker/rule/order-book combination: " + json.dumps(sample, default=str)
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
            try:
                result = await execution.submit_authorized_buy(
                    governor,
                    authorization,
                    draft.request_body,
                    timeout_seconds=config.order_timeout_seconds,
                )
            except Exception as exc:
                # Preserve the exact connector failure state in CI logs. A generic
                # HTTP 500 is not enough to distinguish a startup race, a rejected
                # symbol, an empty snapshot, or a dead WebSocket tracker.
                try:
                    diagnostics = await data._get(
                        "/market-data/order-book/diagnostics/kraken_paper_trade"
                    )
                except Exception as diagnostic_exc:
                    diagnostics = {
                        "diagnostic_error": (
                            f"{type(diagnostic_exc).__name__}: {diagnostic_exc}"
                        )
                    }
                raise RuntimeError(
                    "Kraken paper MARKET BUY preflight failed; no order should be "
                    "assumed submitted. Connector diagnostics: "
                    + json.dumps(
                        {
                            "pair": candidate.trading_pair,
                            "error": f"{type(exc).__name__}: {exc}",
                            "diagnostics": diagnostics,
                        },
                        default=str,
                    )
                ) from exc
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
                "actual_fee_usd": str(result.fee_quote),
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
