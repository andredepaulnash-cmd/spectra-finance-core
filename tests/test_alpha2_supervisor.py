import asyncio
import tempfile
import unittest
from decimal import Decimal

from spectra_alpha2.condor import CondorAnalyzer
from spectra_alpha2.execution import ExecutionResult, ExecutionUncertain
from spectra_alpha2.governor import CapitalGovernor
from spectra_alpha2.market_data import MarketCandidate
from spectra_alpha2.supervisor import PortfolioSupervisor


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}


def candidate(index):
    pair=f"COIN{index}-USDT"
    return MarketCandidate(
        trading_pair=pair,base_asset=f"COIN{index}",quote_asset="USDT",
        last_price=Decimal("10"),best_bid=Decimal("9.99"),best_ask=Decimal("10"),
        quote_volume_24h=Decimal(str(1000000-index*1000)),base_volume_24h=Decimal("10000"),
        spread_fraction=Decimal("0.001"),ticker_timestamp=1,
        min_order_size=Decimal("0.001"),min_notional_size=Decimal("5"),
        base_amount_increment=Decimal("0.001"),price_increment=Decimal("0.01"),
    )


class FakeScanner:
    def __init__(self,items): self.items=items
    async def scan(self): return self.items


class FakeExecution:
    def __init__(self,fail=False): self.fail=fail; self.orders=0
    async def reconcile_pending(self,governor,timeout_seconds=0.1): return []
    async def submit_authorized_buy(self,governor,authorization,request_body,timeout_seconds=60):
        self.orders+=1
        governor.consume_authorization(
            authorization.authorization_id,
            idempotency_key=authorization.idempotency_key,
            connector="kraken_paper_trade",
        )
        if self.fail:
            raise ExecutionUncertain("simulated ambiguous order")
        order_id=f"paper-{self.orders}"
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


class PortfolioSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.gov=CapitalGovernor(f"{self.tmp.name}/alpha2.sqlite3",environment=SAFE_ENV)

    def tearDown(self):
        self.tmp.cleanup()

    def test_supervisor_admits_only_to_configured_ten_position_target(self):
        execution=FakeExecution()
        supervisor=PortfolioSupervisor(
            self.gov,FakeScanner([candidate(i) for i in range(12)]),
            CondorAnalyzer(),execution,target_positions=10,max_new_positions_per_cycle=10,
        )
        report=asyncio.run(supervisor.run_once())
        self.assertEqual(report.admitted_positions,10)
        self.assertEqual(report.active_positions_after_cycle,10)
        self.assertEqual(self.gov.snapshot()["open_positions"],10)
        second=asyncio.run(supervisor.run_once())
        self.assertEqual(second.admitted_positions,0)
        self.assertEqual(second.available_slots,0)
        self.assertEqual(execution.orders,10)

    def test_supervisor_stops_admission_when_order_outcome_is_uncertain(self):
        execution=FakeExecution(fail=True)
        supervisor=PortfolioSupervisor(
            self.gov,FakeScanner([candidate(i) for i in range(5)]),
            CondorAnalyzer(),execution,target_positions=5,max_new_positions_per_cycle=5,
        )
        with self.assertRaises(ExecutionUncertain):
            asyncio.run(supervisor.run_once())
        self.assertEqual(execution.orders,1)
        self.assertEqual(len(self.gov.list_positions()),1)

    def test_global_position_cap_cannot_be_exceeded_by_configuration(self):
        with self.assertRaises(ValueError):
            PortfolioSupervisor(
                self.gov,FakeScanner([]),CondorAnalyzer(),FakeExecution(),
                target_positions=75,
            )


if __name__=="__main__":
    unittest.main()
