import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from decimal import Decimal

from spectra_alpha2.condor import CondorAnalyzer
from spectra_alpha2.execution import ExecutionResult, ExecutionUncertain
from spectra_alpha2.governor import CapitalGovernor
from spectra_alpha2.market_data import MarketCandidate
from spectra_alpha2.supervisor import PortfolioSupervisor


SAFE_ENV={"SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false"}


def candidate(index,price=Decimal("10")):
    pair=f"COIN{index}-USDT"
    return MarketCandidate(
        trading_pair=pair,base_asset=f"COIN{index}",quote_asset="USDT",
        last_price=price,best_bid=price*Decimal("0.999"),best_ask=price*Decimal("1.001"),
        quote_volume_24h=Decimal(str(1000000-index*1000)),base_volume_24h=Decimal("10000"),
        spread_fraction=Decimal("0.001"),ticker_timestamp=1,
        min_order_size=Decimal("0.001"),min_notional_size=Decimal("5"),
        base_amount_increment=Decimal("0.001"),price_increment=Decimal("0.01"),
    )


class FakeScanner:
    def __init__(self,items,snapshots=None): self.items=items; self.snapshots=snapshots or {}
    async def scan(self): return self.items
    async def inspect_pair(self,pair): return self.snapshots.get(pair)


class FakeExecution:
    def __init__(self,fail=False): self.fail=fail; self.orders=0; self.sell_orders=0
    async def reconcile_pending(self,governor,timeout_seconds=0.1): return []
    async def submit_authorized_buy(self,governor,authorization,request_body,timeout_seconds=60,shard_id=None):
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

    async def submit_authorized_sell(self,governor,authorization,request_body,timeout_seconds=60):
        self.sell_orders+=1
        governor.consume_exit_authorization(
            authorization.authorization_id,
            idempotency_key=authorization.idempotency_key,
            connector="kraken_paper_trade",
        )
        order_id=f"paper-sell-{self.sell_orders}"
        governor.bind_exit_order_id(authorization.authorization_id,order_id=order_id)
        quantity=Decimal(request_body["amount"])
        price=Decimal(request_body["price"])
        gross=quantity*price
        fee=gross*Decimal("0.004")
        governor.settle_exit_fill(
            authorization.authorization_id,
            actual_quantity=quantity,
            average_price=price,
            actual_fee=fee,
        )
        return SimpleNamespace(status="FILLED",filled_quantity=quantity)


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

    def test_supervisor_fills_fifty_position_target_from_gainer_candidates(self):
        execution=FakeExecution()
        supervisor=PortfolioSupervisor(
            self.gov,FakeScanner([candidate(i) for i in range(60)]),
            CondorAnalyzer(),execution,target_positions=50,max_new_positions_per_cycle=50,
        )
        report=asyncio.run(supervisor.run_once())
        self.assertEqual(report.admitted_positions,50)
        self.assertEqual(report.active_positions_after_cycle,50)
        self.assertEqual(self.gov.snapshot()["open_positions"],50)
        self.assertEqual(execution.orders,50)
        second=asyncio.run(supervisor.run_once())
        self.assertEqual(second.admitted_positions,0)
        self.assertEqual(second.available_slots,0)
        self.assertEqual(execution.orders,50)

    def test_fully_sold_position_is_replaced_in_the_same_cycle(self):
        self._open_position()
        up=candidate(1,Decimal("104"))
        replacement=candidate(2,Decimal("10"))
        scanner=FakeScanner([up,replacement],snapshots={"COIN1-USDT":up})
        execution=FakeExecution()
        supervisor=PortfolioSupervisor(
            self.gov,scanner,CondorAnalyzer(),execution,
            target_positions=1,max_new_positions_per_cycle=1,
        )
        report=asyncio.run(supervisor.run_once())
        self.assertEqual(report.realized_exit_fills,1)
        self.assertEqual(report.admitted_positions,1)
        self.assertEqual(report.active_positions_after_cycle,1)
        self.assertEqual([p["asset_id"] for p in self.gov.list_positions()],["COIN2-USDT"])
        self.assertEqual(execution.orders,1)
        self.assertEqual(execution.sell_orders,1)

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


    def _open_position(self,asset_id="COIN1-USDT"):
        auth=self.gov.reserve_initial_position(
            position_id="existing-1",asset_id=asset_id,reference_price=Decimal("100"),
            idempotency_key="existing-initial",
        )
        self.gov.consume_authorization(
            auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade"
        )
        self.gov.bind_order_id(auth.authorization_id,order_id="existing-paper-order")
        self.gov.settle_fill(
            auth.authorization_id,actual_gross_notional=Decimal("20"),actual_fee=Decimal("0.08"),
            actual_quantity=Decimal("0.2"),average_price=Decimal("100"),
        )

    def test_supervisor_does_not_execute_unapproved_dca_or_vcap_policy(self):
        self._open_position()
        down=candidate(1,Decimal("97"))
        scanner=FakeScanner([down],snapshots={"COIN1-USDT":down})
        execution=FakeExecution()
        supervisor=PortfolioSupervisor(
            self.gov,scanner,CondorAnalyzer(),execution,target_positions=1,
        )
        report=asyncio.run(supervisor.run_once())
        state=self.gov.get_position_state("existing-1")
        self.assertEqual(report.recovery_actions,0)
        self.assertEqual(state["completed_dca_steps"],set())
        self.assertEqual(state["completed_vcap_steps"],set())
        self.assertEqual(len(state["active_lots"]),1)
        self.assertEqual(execution.orders,0)

    def test_supervisor_submits_fixed_net_take_profit_limit_sell_for_top_gainer(self):
        self._open_position()
        up=candidate(1,Decimal("104"))
        scanner=FakeScanner([up],snapshots={"COIN1-USDT":up})
        execution=FakeExecution()
        supervisor=PortfolioSupervisor(
            self.gov,scanner,CondorAnalyzer(),execution,target_positions=1,
        )
        report=asyncio.run(supervisor.run_once())
        self.assertEqual(report.exit_orders,1)
        self.assertEqual(report.realized_exit_fills,1)
        self.assertEqual(execution.sell_orders,1)
        self.assertEqual(self.gov.list_positions(),[])

    def test_supervisor_flags_minus_24_percent_for_manual_review_without_patching(self):
        self._open_position()
        down=candidate(1,Decimal("75"))
        scanner=FakeScanner([down],snapshots={"COIN1-USDT":down})
        execution=FakeExecution()
        supervisor=PortfolioSupervisor(
            self.gov,scanner,CondorAnalyzer(),execution,target_positions=1,
        )
        report=asyncio.run(supervisor.run_once())
        state=self.gov.get_position_state("existing-1")
        self.assertEqual(report.manual_review_flags,1)
        self.assertTrue(state["manual_review_required"])
        self.assertEqual(execution.orders,0)

    def test_global_position_cap_cannot_be_exceeded_by_configuration(self):
        with self.assertRaises(ValueError):
            PortfolioSupervisor(
                self.gov,FakeScanner([]),CondorAnalyzer(),FakeExecution(),
                target_positions=75,
            )


if __name__=="__main__":
    unittest.main()
