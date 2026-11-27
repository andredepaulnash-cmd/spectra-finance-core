import asyncio
import tempfile
import unittest
from decimal import Decimal

from spectra_alpha2.condor import CondorAnalyzer
from spectra_alpha2.execution import ExecutionResult
from spectra_alpha2.governor import CapitalDenied, CapitalGovernor
from spectra_alpha2.market_data import MarketCandidate
from spectra_alpha2.supervisor import PortfolioSupervisor


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}


def market(index):
    return MarketCandidate(
        trading_pair=f"ASSET{index:02d}-USDT",base_asset=f"ASSET{index:02d}",quote_asset="USDT",
        last_price=Decimal("10"),best_bid=Decimal("9.99"),best_ask=Decimal("10"),
        quote_volume_24h=Decimal(str(1000000-index*1000)),base_volume_24h=Decimal("10000"),
        spread_fraction=Decimal("0.001"),ticker_timestamp=1,
        min_order_size=Decimal("0.001"),min_notional_size=Decimal("5"),
        base_amount_increment=Decimal("0.001"),price_increment=Decimal("0.01"),
    )


class Scanner:
    def __init__(self,items): self.items=items
    async def scan(self): return self.items
    async def inspect_pair(self,pair): return None


class PaperExecution:
    def __init__(self): self.counter=0
    async def reconcile_pending(self,governor,timeout_seconds=0.1): return []
    async def submit_authorized_buy(self,governor,authorization,request_body,timeout_seconds=60,shard_id=None):
        self.counter+=1
        governor.consume_authorization(
            authorization.authorization_id,idempotency_key=authorization.idempotency_key,
            connector="kraken_paper_trade",
        )
        order_id=f"staged-paper-{self.counter}"
        governor.bind_order_id(authorization.authorization_id,order_id=order_id)
        quantity=Decimal(request_body["amount"])
        price=Decimal(request_body["max_execution_price"])
        gross=quantity*price
        fee=gross*Decimal("0.004")
        governor.settle_fill(
            authorization.authorization_id,actual_gross_notional=gross,actual_fee=fee,
            actual_quantity=quantity,average_price=price,
        )
        return ExecutionResult(authorization.authorization_id,order_id,"FILLED",quantity,gross,fee)


class StagedCapacityTests(unittest.TestCase):
    def test_staged_position_capacity_10_25_50_74_and_reject_75th(self):
        with tempfile.TemporaryDirectory() as temp:
            governor=CapitalGovernor(f"{temp}/alpha2.sqlite3",environment=SAFE_ENV)
            execution=PaperExecution()
            scanner=Scanner([market(i) for i in range(80)])
            for target in (10,25,50,74):
                supervisor=PortfolioSupervisor(
                    governor,scanner,CondorAnalyzer(),execution,
                    target_positions=target,max_new_positions_per_cycle=10,
                )
                cycles=0
                while len(governor.list_positions())<target and cycles<10:
                    asyncio.run(supervisor.run_once())
                    cycles+=1
                self.assertEqual(len(governor.list_positions()),target)
                self.assertLessEqual(len(governor.list_positions()),74)
            with self.assertRaises(CapitalDenied):
                governor.reserve_initial_position(
                    position_id="overflow-75",asset_id="OVERFLOW-USDT",
                    reference_price=Decimal("1"),idempotency_key="overflow-75",
                )
            snapshot=governor.snapshot()
            self.assertEqual(snapshot["open_positions"],74)
            self.assertEqual(snapshot["open_lots"],74)
            self.assertEqual(snapshot["active_vcap_shards"],0)


if __name__=="__main__":
    unittest.main()
