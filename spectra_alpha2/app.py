"""Application assembly for the autonomous paper-only Alpha 2.0 trading loop."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict
from decimal import Decimal

from .condor import CondorAnalyzer
from .config import Alpha2Config
from .execution import HummingbotPaperExecutionClient
from .governor import CapitalGovernor
from .market_data import HummingbotPaperDataClient, ResearchScanner
from .supervisor import PortfolioSupervisor


logging.basicConfig(
    level=os.getenv("SPECTRA_LOG_LEVEL","INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER=logging.getLogger("spectra_alpha2")


def build_runtime():
    config=Alpha2Config.from_environment()
    environment=dict(os.environ)
    governor=CapitalGovernor(config.database_path,environment=environment)
    data_client=HummingbotPaperDataClient(
        config.hummingbot_api_url,
        api_username=config.hummingbot_api_username,
        api_password=config.hummingbot_api_password,
        environment=environment,
    )
    scanner=ResearchScanner(
        data_client,
        min_quote_volume_24h=config.min_quote_volume_24h,
        entry_quote_assets=config.entry_quote_assets,
        max_spread_fraction=config.max_spread_fraction,
    )
    execution=HummingbotPaperExecutionClient(
        config.hummingbot_api_url,
        api_username=config.hummingbot_api_username,
        api_password=config.hummingbot_api_password,
        environment=environment,
    )
    supervisor=PortfolioSupervisor(
        governor,scanner,CondorAnalyzer(),execution,
        target_positions=config.target_positions,
        max_new_positions_per_cycle=config.max_new_positions_per_cycle,
        max_slippage_fraction=config.max_slippage_fraction,
        order_timeout_seconds=config.order_timeout_seconds,
    )
    return config,governor,data_client,execution,supervisor


async def _run_once() -> int:
    config,governor,data_client,execution,supervisor=build_runtime()
    try:
        report=await supervisor.run_once()
        print(json.dumps(asdict(report),indent=2,default=str))
        print(json.dumps(governor.snapshot(),indent=2,default=str))
        return 0
    finally:
        await execution.close()
        await data_client.close()


async def _run_forever() -> int:
    config,governor,data_client,execution,supervisor=build_runtime()
    LOGGER.info("Spectra Finance Alpha 2.0 paper loop starting; max_positions=%s",config.target_positions)
    try:
        while True:
            try:
                report=await supervisor.run_once()
                LOGGER.info("portfolio cycle: %s",json.dumps(asdict(report),default=str))
            except Exception:
                # An uncertain order leaves its reservation in place. Reconciliation runs
                # again next cycle; the supervisor cannot admit beyond reserved capital.
                LOGGER.exception("paper cycle halted; no unverified order will be retried")
            await asyncio.sleep(config.scan_interval_seconds)
    finally:
        await execution.close()
        await data_client.close()


async def _doctor() -> int:
    config,governor,data_client,execution,supervisor=build_runtime()
    try:
        tickers=await data_client.get_tickers()
        rules=await data_client.get_trading_rules()
        candidates=await supervisor.scanner.scan()
        usd_pair_intersection = sorted(
            (
                pair for pair in set(tickers).intersection(rules)
                if pair.endswith("-USD")
            ),
            key=lambda pair: (pair not in {"BTC-USD", "XBT-USD"}, pair),
        )
        ticker_quote_counts = {}
        for pair in tickers:
            if "-" in pair:
                quote = pair.rsplit("-", 1)[1]
                ticker_quote_counts[quote] = ticker_quote_counts.get(quote, 0) + 1
        preferred_pairs = [
            pair for pair in usd_pair_intersection
            if pair.split("-", 1)[0].upper() in {"BTC", "XBT"}
        ]
        probe_pair = (
            preferred_pairs[0]
            if preferred_pairs
            else (usd_pair_intersection[0] if usd_pair_intersection else None)
        )
        candle_count=0
        candle_error=None
        if probe_pair:
            try:
                candle_count=len(await data_client.get_hourly_candles(probe_pair,max_records=2))
            except Exception as exc:
                candle_error=f"{type(exc).__name__}: {exc}"
        discovery_configured=bool(config.entry_quote_assets and config.min_quote_volume_24h is not None)
        report={
            "paper_only":True,
            "live_execution":False,
            "connector":"kraken_paper_trade",
            "public_candle_connector":"kraken",
            "api_url":config.hummingbot_api_url,
            "ticker_count":len(tickers),
            "trading_rule_count":len(rules),
            "usd_ticker_pair_count":sum(1 for pair in tickers if pair.endswith("-USD")),
            "usd_trading_rule_pair_count":sum(1 for pair in rules if pair.endswith("-USD")),
            "usd_ticker_rule_intersection_count":len(usd_pair_intersection),
            "usd_pair_sample":usd_pair_intersection[:20],
            "ticker_pair_sample":sorted(tickers)[:40],
            "ticker_quote_counts":dict(sorted(ticker_quote_counts.items(), key=lambda item: (-item[1], item[0]))[:20]),
            "public_candle_probe_pair":probe_pair,
            "public_candle_probe_count":candle_count,
            "public_candle_probe_error":candle_error,
            "entry_discovery_configured":discovery_configured,
            "entry_quote_assets":sorted(config.entry_quote_assets),
            "min_quote_volume_24h":str(config.min_quote_volume_24h) if config.min_quote_volume_24h is not None else None,
            "eligible_market_count":len(candidates),
            "scanner_diagnostics":getattr(supervisor.scanner, "last_scan_diagnostics", {}),
            "portfolio":governor.snapshot(),
        }
        print(json.dumps(report,indent=2,default=str))
        return 0 if tickers and rules and usd_pair_intersection and candle_count > 0 and discovery_configured else 2
    finally:
        await execution.close()
        await data_client.close()


def _status() -> int:
    config=Alpha2Config.from_environment()
    governor=CapitalGovernor(config.database_path,environment=dict(os.environ))
    print(json.dumps({
        "paper_only":True,
        "live_execution":False,
        "target_positions":config.target_positions,
        "positions":governor.list_positions(),
        "pending_authorizations":governor.list_pending_authorizations(),
        "portfolio":governor.snapshot(),
    },indent=2,default=str))
    return 0


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(prog="spectra-alpha2")
    parser.add_argument("command",choices=("run","run-once","status","doctor"),nargs="?",default="run")
    args=parser.parse_args(argv)
    if args.command=="run": return asyncio.run(_run_forever())
    if args.command=="run-once": return asyncio.run(_run_once())
    if args.command=="doctor": return asyncio.run(_doctor())
    return _status()


if __name__=="__main__":
    raise SystemExit(main())
