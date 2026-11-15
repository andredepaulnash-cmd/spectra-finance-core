import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import mcp_servers.hummingbot_api.tools.economic_exit as exit_tool
import mcp_servers.hummingbot_api.tools.vcap_lifecycle as lifecycle_tool
import mcp_servers.hummingbot_api.tools.vcap_runtime as runtime_tool
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.economic_position import EconomicPositionStatus
from spectra_core.vcap_lifecycle import VCAPLifecycleAction, VCAPLifecycleDecision
from spectra_core.vcap_runtime import VCAPRuntimeStore
from spectra_core.vcap_state import VCAPStateStore
from tests.test_residual_position_lifecycle import D, fixture
from tests.test_vcap_runtime_supervisor import fingerprint, healthy, lifecycle_state


def remote(monkeypatch, evidence, *, empty=False):
    async def get_executor(executor_id):
        return {
            "id": executor_id,
            "status": "TERMINATED",
            "executor_type": "dca_executor",
            "close_type": "POSITION_HOLD",
            "filled_amount_quote": "0" if empty else "149.99",
            "cum_fees_quote": "0" if empty else "0.3",
            "custom_info": {"position_size_quote": "0" if empty else "0.005"},
        }

    async def orders(*args, **kwargs):
        return evidence.order_snapshot

    async def trades(*args, **kwargs):
        return evidence.trade_snapshot

    monkeypatch.setattr(exit_tool, "fetch_executor_order_snapshot", orders)
    monkeypatch.setattr(exit_tool, "fetch_executor_trade_snapshot", trades)
    return SimpleNamespace(executors=SimpleNamespace(get_executor=get_executor))


def test_remote_terminal_retirement_uses_completion_clock(tmp_path, monkeypatch):
    governor, ledger, kwargs = fixture(tmp_path)
    tick_started = kwargs["now"]
    client = remote(monkeypatch, kwargs["executor_evidence"][0])
    certificate = asyncio.run(
        exit_tool.retire_terminal_economic_position(
            client,
            position_id="position",
            proposal_ids=("owner",),
            governor=governor,
            ledger=ledger,
            trading_rule=kwargs["trading_rule"],
            current_price=D("1"),
            market_observed_at=tick_started,
            now=tick_started,
        )
    )
    assert certificate["occurred_at"] > tick_started
    assert ledger.snapshot("position").status == EconomicPositionStatus.RESIDUAL
    assert certificate["capital_released_quote"] == "0"


def test_new_unreconciled_terminal_trade_keeps_position_active(tmp_path, monkeypatch):
    governor, ledger, kwargs = fixture(tmp_path)
    evidence = kwargs["executor_evidence"][0]
    extra = replace(evidence.trade_snapshot.trades[-1], trade_id="unseen-fill")
    evidence = replace(
        evidence,
        trade_snapshot=replace(
            evidence.trade_snapshot,
            trades=(*evidence.trade_snapshot.trades, extra),
        ),
    )
    client = remote(monkeypatch, evidence)
    with pytest.raises(ValueError, match="durably reconciled"):
        asyncio.run(
            exit_tool.retire_terminal_economic_position(
                client,
                position_id="position",
                proposal_ids=("owner",),
                governor=governor,
                ledger=ledger,
                trading_rule=kwargs["trading_rule"],
                current_price=D("1"),
                market_observed_at=kwargs["now"],
                now=kwargs["now"],
            )
        )
    assert ledger.snapshot("position").status == EconomicPositionStatus.ACTIVE


def test_runtime_retires_terminal_empty_owner_without_dispatch(tmp_path, monkeypatch):
    governor, ledger, kwargs = fixture(tmp_path, empty=True)
    client = remote(monkeypatch, kwargs["executor_evidence"][0], empty=True)
    lifecycle = VCAPStateStore(tmp_path / "lifecycle.db")
    state = lifecycle_state()
    lifecycle.save(state)
    runtime = VCAPRuntimeStore(tmp_path / "runtime.db")

    async def accounting(*args, **kwargs):
        return replace(healthy(time.time()), position_id="position")

    async def market(*args, **ignored):
        return fingerprint(time.time()), kwargs["trading_rule"], D("1")

    async def forbidden_dispatch(*args, **kwargs):
        pytest.fail("empty terminal cleanup must not create an executor")

    monkeypatch.setattr(runtime_tool, "_refresh_accounting", accounting)
    monkeypatch.setattr(runtime_tool, "_refresh_market", market)
    monkeypatch.setattr(
        runtime_tool, "execute_vcap_lifecycle_decision", forbidden_dispatch
    )
    result = asyncio.run(
        runtime_tool.run_vcap_runtime_cycle(
            client,
            position_id="position",
            envelope_proposal_id=state.envelope_proposal_id,
            governor=governor,
            ledger=ledger,
            lifecycle_store=lifecycle,
            runtime_store=runtime,
            accounting_supervisor=AccountingHealthSupervisor(ledger.path),
            seed_quote=D("10"),
            expected_exit_fee_bps=D("20"),
            slippage_allowance_bps=D("5"),
            now=time.time(),
        )
    )
    assert result["executed"] is False
    assert "terminal_empty_or_flat_position_retired" in result["reasons"]
    assert ledger.get_retirement_certificate("position")["kind"] == "EMPTY_TERMINAL"
    assert ledger.snapshot("position").status == EconomicPositionStatus.CLOSED


def test_lifecycle_forwards_current_combined_exit_cost(tmp_path, monkeypatch):
    captured = {}

    async def partial(*args, **kwargs):
        captured.update(kwargs)
        return {"executed": False}

    monkeypatch.setattr(lifecycle_tool, "execute_partial_recycle", partial)
    state = lifecycle_state()
    decision = VCAPLifecycleDecision(
        action=VCAPLifecycleAction.PARTIAL_RECYCLE,
        state=state,
        profit_decision=None,
        execution_decision=None,
        reasons=(),
        partial_recycle_decision=SimpleNamespace(),
    )
    asyncio.run(
        lifecycle_tool.execute_vcap_lifecycle_decision(
            SimpleNamespace(),
            decision=decision,
            governor=SimpleNamespace(),
            store=VCAPStateStore(tmp_path / "state.db"),
            now=2000,
            position_id="position-runtime-1",
            ledger=SimpleNamespace(),
            trading_rule=SimpleNamespace(),
            current_price=D("1"),
            accounting_health=healthy(),
            expected_exit_cost_bps=D("43"),
        )
    )
    assert captured["expected_exit_cost_bps"] == D("43")
