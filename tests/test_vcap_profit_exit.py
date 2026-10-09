import asyncio
from decimal import Decimal

from mcp_servers.hummingbot_api.tools.vcap_profit_exit import (
    execute_vcap_profit_exit,
)
from spectra_core.capital_governor import CapitalGovernor
from spectra_core.models import (
    CapitalPolicy,
    CapitalProposal,
    CapitalSnapshot,
    DecisionStatus,
    IntentKind,
)
from spectra_core.recovery_fingerprint import VCAPPositionState
from spectra_core.vcap_profit_exit import (
    VCAPExitCostModel,
    VCAPProfitExitAction,
    VCAPProfitExitPolicy,
    plan_vcap_profit_exit,
)


D = Decimal


def position(price: str) -> VCAPPositionState:
    return VCAPPositionState(
        pair="BTC-USD",
        position_base=D("10"),
        cost_basis_quote=D("1000"),
        current_price=D(price),
    )


def costs() -> VCAPExitCostModel:
    return VCAPExitCostModel(
        accrued_fees_quote=D("2"),
        expected_exit_fee_bps=D("20"),
        slippage_allowance_bps=D("10"),
    )


def test_174_bps_target_is_net_of_accrued_and_expected_exit_costs():
    decision = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=1_000,
    )

    # Naive +1.74% on a $100 average entry would be $101.74. The actual
    # required exit must be higher because the policy accounts for entry fees,
    # expected exit fees, and slippage.
    assert decision.target_exit_price > D("101.74")
    assert decision.action == VCAPProfitExitAction.WAIT
    assert decision.estimated_net_profit_bps < D("174")


def test_reaching_net_profit_floor_extends_under_profit_protection():
    base = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=1_000,
    )
    price = base.target_exit_price + D("0.10")

    decision = plan_vcap_profit_exit(
        position(str(price)),
        costs=costs(),
        now=1_060,
    )

    assert decision.action == VCAPProfitExitAction.EXTEND
    assert decision.estimated_net_profit_bps >= D("174")
    assert decision.profit_floor_reached
    assert decision.protected_floor_price == decision.target_exit_price
    assert "net_profit_floor_protected" in decision.reasons


def test_once_profit_floor_was_reached_a_breach_exits_instead_of_recovering():
    base = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=1_000,
    )
    reached = plan_vcap_profit_exit(
        position(str(base.target_exit_price + D("0.20"))),
        costs=costs(),
        now=1_100,
    )

    breached = plan_vcap_profit_exit(
        position(str(base.target_exit_price - D("0.05"))),
        costs=costs(),
        now=1_200,
        previous_state=reached.state,
    )

    assert breached.action == VCAPProfitExitAction.EXIT
    assert "protected_profit_floor_breached" in breached.reasons


def test_profitable_position_stagnating_two_hours_recycles_capital():
    probe = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=2_000,
    )
    profitable_price = probe.target_exit_price + D("0.35")

    first = plan_vcap_profit_exit(
        position(str(profitable_price)),
        costs=costs(),
        now=2_100,
    )
    assert first.action == VCAPProfitExitAction.EXTEND

    stagnant = plan_vcap_profit_exit(
        position(str(profitable_price)),
        costs=costs(),
        now=2_100 + 2 * 60 * 60,
        previous_state=first.state,
    )

    assert stagnant.action == VCAPProfitExitAction.EXIT
    assert "profitable_capital_stagnant" in stagnant.reasons
    assert "recycle_profitable_capital" in stagnant.reasons


def test_material_profit_giveback_harvests_before_stagnation_timer():
    probe = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=3_000,
    )

    high = plan_vcap_profit_exit(
        position(str(probe.target_exit_price + D("1.50"))),
        costs=costs(),
        now=3_100,
    )
    assert high.action == VCAPProfitExitAction.EXTEND

    lower = plan_vcap_profit_exit(
        position(str(probe.target_exit_price + D("0.80"))),
        costs=costs(),
        now=3_160,
        previous_state=high.state,
    )

    assert lower.action == VCAPProfitExitAction.EXIT
    assert "profit_giveback_limit_reached" in lower.reasons


def test_meaningful_new_profit_high_resets_stagnation_clock():
    probe = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=4_000,
    )

    first = plan_vcap_profit_exit(
        position(str(probe.target_exit_price + D("0.20"))),
        costs=costs(),
        now=4_100,
    )

    improved = plan_vcap_profit_exit(
        position(str(probe.target_exit_price + D("0.60"))),
        costs=costs(),
        now=4_100 + 60 * 60,
        previous_state=first.state,
    )

    assert improved.action == VCAPProfitExitAction.EXTEND
    assert improved.state.peak_at == 4_100 + 60 * 60

    still_working = plan_vcap_profit_exit(
        position(str(probe.target_exit_price + D("0.60"))),
        costs=costs(),
        now=4_100 + 2 * 60 * 60,
        previous_state=improved.state,
    )
    assert still_working.action == VCAPProfitExitAction.EXTEND


def governor(tmp_path):
    return CapitalGovernor(
        tmp_path / "vcap-profit-exit.sqlite3",
        CapitalPolicy(
            reserve_floor_quote=D("0"),
            max_total_committed_quote=D("1000"),
            max_pair_quote=D("1000"),
            max_position_quote=D("1000"),
            max_recovery_quote=D("500"),
            max_leverage=D("1"),
            reservation_ttl_seconds=120,
        ),
    )


def prepare_recovery_execution(g):
    proposal = CapitalProposal(
        proposal_id="recovery-exit-1",
        pair="BTC-USD",
        intent=IntentKind.RECOVERY,
        amount_quote=D("100"),
        leverage=D("1"),
    )
    decision = g.reserve(
        proposal,
        CapitalSnapshot(
            free_quote=D("1000"),
            committed_quote=D("0"),
            pair_exposure_quote=D("0"),
            position_exposure_quote=D("0"),
            recovery_exposure_quote=D("0"),
        ),
    )
    assert decision.status == DecisionStatus.RESERVED
    assert g.commit(proposal.proposal_id)
    assert g.reconcile_execution(
        proposal_id=proposal.proposal_id,
        executor_id="exec-profit-1",
    )
    return proposal


class Executors:
    def __init__(self):
        self.stop_calls = []
        self.get_calls = []

    async def stop_executor(self, *, executor_id, keep_position):
        self.stop_calls.append((executor_id, keep_position))
        return {
            "status": "stopped",
            "executor_id": executor_id,
        }

    async def get_executor(self, executor_id):
        self.get_calls.append(executor_id)
        return {
            "id": executor_id,
            "executor_type": "dca_executor",
            "status": "TERMINATED",
            "close_type": "TAKE_PROFIT",
            "filled_amount_quote": "100",
            "cum_fees_quote": "0.45",
            "net_pnl_quote": "2.10",
            "custom_info": {
                "realized_pnl_quote": "2.10",
            },
        }


class Client:
    def __init__(self):
        self.executors = Executors()


def test_profit_exit_releases_recovery_budget_only_after_terminal_accounting(
    tmp_path,
):
    g = governor(tmp_path)
    prepare_recovery_execution(g)

    probe = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=5_000,
    )
    reached = plan_vcap_profit_exit(
        position(str(probe.target_exit_price + D("0.30"))),
        costs=costs(),
        now=5_100,
    )
    exit_decision = plan_vcap_profit_exit(
        position(str(probe.target_exit_price - D("0.01"))),
        costs=costs(),
        now=5_200,
        previous_state=reached.state,
    )
    assert exit_decision.action == VCAPProfitExitAction.EXIT

    client = Client()
    result = asyncio.run(
        execute_vcap_profit_exit(
            client,
            decision=exit_decision,
            governor=g,
            proposal_ids=["recovery-exit-1"],
        )
    )

    assert result["exit_requested"] is False
    assert result["all_recovery_budget_released"] is True
    assert result["capital_recycling_state"] == "RELEASED"
    assert result["executors"][0]["realized_pnl_quote"] == "2.10"
    assert result["executors"][0]["remaining_recovery_exposure_quote"] == "0"
    assert client.executors.stop_calls == []
    assert "legacy_profit_exit_requires_unified_protected_manifest" in result["reasons"]
    assert g.current_recovery_exposure_quote() == D("0")


def test_non_exit_decision_never_mutates_executor(tmp_path):
    g = governor(tmp_path)
    prepare_recovery_execution(g)
    client = Client()

    decision = plan_vcap_profit_exit(
        position("100"),
        costs=costs(),
        now=6_000,
    )
    assert decision.action == VCAPProfitExitAction.WAIT

    result = asyncio.run(
        execute_vcap_profit_exit(
            client,
            decision=decision,
            governor=g,
            proposal_ids=["recovery-exit-1"],
        )
    )

    assert result["exit_requested"] is False
    assert client.executors.stop_calls == []
