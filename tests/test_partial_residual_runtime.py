"""Complete offline partial SELL -> terminal dust -> runtime retirement proof."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

import mcp_servers.hummingbot_api.tools.accounting_health as health_tool
import mcp_servers.hummingbot_api.tools.economic_exit as exit_tool
import mcp_servers.hummingbot_api.tools.partial_recycle as partial_tool
import mcp_servers.hummingbot_api.tools.vcap_runtime as runtime_tool
from spectra_core.accounting_health import AccountingHealthSupervisor
from spectra_core.asset_fingerprint import AssetFingerprint, AssetObservation
from spectra_core.economic_position import EconomicPositionLedger, EconomicPositionStatus
from spectra_core.executor_provenance_bridge import ExecutorOrderEvidence, ExecutorOrderSnapshot, ExecutorTradeSnapshot
from spectra_core.fill_reconciliation import TradeFillEvidence
from spectra_core.models import IntentKind
from spectra_core.partial_recycle import PartialRecycleAction, PartialRecycleStore, plan_partial_recycle
from spectra_core.vcap_lifecycle import VCAPLifecycleAction
from spectra_core.vcap_runtime import VCAPRuntimePhase, VCAPRuntimePolicy, VCAPRuntimeStore
from spectra_core.vcap_state import VCAPStateStore
from test_residual_position_lifecycle import fixture, D
from test_vcap_runtime_supervisor import lifecycle_state


def test_terminal_partial_residue_retires_through_real_runtime_and_durable_source_proof(tmp_path, monkeypatch):
    governor, ledger, inputs = fixture(tmp_path, intent=IntentKind.RECOVERY, remaining='10.04')
    original = ledger.snapshot('position')
    assert original.active_base == D('10.04')
    rule = inputs['trading_rule']
    now = time.time()
    planned = plan_partial_recycle(
        ledger=ledger, governor=governor, position_id='position', trading_rule=rule,
        current_price=D('1'), expected_exit_fee_bps=D('0'), slippage_allowance_bps=D('0'), now=now,
    )
    assert planned.action == PartialRecycleAction.RECYCLE
    manifest = planned.manifest
    assert manifest.target_base == D('10.04')
    partials = PartialRecycleStore(ledger.path)
    claimed = partials.claim_dispatch(manifest, ledger=ledger, governor=governor, trading_rule=rule,
                                     current_price=D('1'), now=time.time(), market_observed_at=now)
    assert claimed.allowed

    source = inputs['executor_evidence'][0]
    event_time = float(int(time.time()))
    sell_order = ExecutorOrderEvidence(
        manifest.sell_executor_id, 'partial-sale', 'MARKET', 'SELL', D('10'), None,
        'FILLED', D('10'), D('1'), event_time, event_time,
    )
    sell_trade = TradeFillEvidence(
        'partial-sale-fill', 'partial-sale', 'account', 'kraken', 'XRP-USD', 'SELL',
        D('10'), D('1'), D('0'), 'USD', event_time,
    )
    partial_orders = ExecutorOrderSnapshot(manifest.sell_executor_id, (sell_order,))
    partial_trades = ExecutorTradeSnapshot(manifest.sell_executor_id, (sell_trade,))
    calls = {'create': 0, 'market': 0, 'source_orders': 0, 'source_trades': 0}

    async def orders(client, *, executor_id):
        if executor_id == manifest.sell_executor_id:
            return partial_orders
        assert executor_id == 'executor'
        calls['source_orders'] += 1
        return source.order_snapshot

    async def trades(client, *, executor_id):
        if executor_id == manifest.sell_executor_id:
            return partial_trades
        assert executor_id == 'executor'
        calls['source_trades'] += 1
        return source.trade_snapshot

    async def get_executor(executor_id):
        if executor_id == manifest.sell_executor_id:
            return {'id': executor_id, 'status': 'TERMINATED', 'executor_type': 'order_executor',
                    'close_type': 'COMPLETED', 'filled_amount_quote': '10', 'cum_fees_quote': '0',
                    'net_pnl_quote': '0', 'custom_info': {'realized_pnl_quote': '0'}}
        assert executor_id == 'executor'
        return {'id': executor_id, 'status': 'TERMINATED', 'executor_type': 'dca_executor',
                'close_type': 'POSITION_HOLD', 'filled_amount_quote': '149.99', 'cum_fees_quote': '0.3',
                'custom_info': {'position_size_quote': '0.005'}}

    async def forbidden_create(**kwargs):
        calls['create'] += 1
        pytest.fail('reconciliation of a previously dispatched partial must not create again')

    async def market(*args, **kwargs):
        calls['market'] += 1
        fingerprint = AssetFingerprint.cold_start(AssetObservation(
            trading_pair='XRP-USD', timestamp=time.time(), price=1.0, quote_volume_24h=1000000,
            spread_bps=5, depth_quote=50000, depth_imbalance=0.1, microprice_edge_bps=0.5,
        ))
        return fingerprint, rule, D('1')

    for module in (health_tool, exit_tool, partial_tool):
        monkeypatch.setattr(module, 'fetch_executor_order_snapshot', orders)
        monkeypatch.setattr(module, 'fetch_executor_trade_snapshot', trades)
    monkeypatch.setattr(runtime_tool, '_refresh_market', market)
    client = SimpleNamespace(executors=SimpleNamespace(get_executor=get_executor, create_executor=forbidden_create))
    lifecycle = VCAPStateStore(tmp_path / 'lifecycle.db')
    state = replace(lifecycle_state(), recovery_proposal_ids=('owner',), position_base=original.active_base,
                    cost_basis_quote=original.active_cost_basis_quote, last_known_price=D('1'), updated_at=now)
    lifecycle.save(state)
    runtime = VCAPRuntimeStore(tmp_path / 'runtime.db')
    cycle = runtime.begin_or_resume(position_id='position', envelope_proposal_id=state.envelope_proposal_id, now=now)
    phases = (VCAPRuntimePhase.ACCOUNTING_PRECHECKED, VCAPRuntimePhase.MARKET_REFRESHED,
              VCAPRuntimePhase.PLANNED, VCAPRuntimePhase.MUTATION_DISPATCHING,
              VCAPRuntimePhase.MUTATION_DISPATCHED, VCAPRuntimePhase.AWAITING_RECONCILIATION)
    for phase in phases:
        cycle = runtime.transition(
            cycle.cycle_id, phase=phase, now=time.time(), expected_phases=(cycle.phase,),
            planned_action=VCAPLifecycleAction.PARTIAL_RECYCLE.value,
            mutation_key=f'PARTIAL_RECYCLE:{manifest.manifest_id}',
            execution_result={'manifest_id': manifest.manifest_id},
        )
    supervisor = AccountingHealthSupervisor(ledger.path)
    result = asyncio.run(runtime_tool._resume_reconciliation(
        client, cycle=cycle, governor=governor, ledger=ledger, lifecycle_store=lifecycle,
        runtime_store=runtime, accounting_supervisor=supervisor, policy=VCAPRuntimePolicy(), now=time.time(),
    ))
    assert result['phase'] == VCAPRuntimePhase.COMPLETED.value, result
    assert result['accounting_state'] == 'HEALTHY'
    assert result['terminal'] is True
    assert calls['create'] == 0
    assert calls['market'] == 1
    assert calls['source_orders'] >= 2 and calls['source_trades'] >= 2
    after = ledger.snapshot('position')
    assert after.status == EconomicPositionStatus.RESIDUAL
    assert after.active_base == D('0.04')
    assert after.active_cost_basis_quote == original.active_cost_basis_quote * D('0.04') / D('10.04')
    assert after.active_entry_fees_quote == original.active_entry_fees_quote * D('0.04') / D('10.04')
    assert after.recovery_proposal_ids == ('owner',)
    certificate = ledger.get_retirement_certificate('position')
    assert certificate['kind'] == 'RESIDUAL'
    assert certificate['capital_released_quote'] == '0'
    partial_proofs = certificate['proof']['partial_terminal_certificates']
    assert len(partial_proofs) == 1
    assert partial_proofs[0]['manifest_id'] == manifest.manifest_id
    assert partial_proofs[0]['executor_status'] == 'TERMINATED'
    assert partials.terminal_proof(manifest.manifest_id) == partial_proofs[0]
    assert EconomicPositionLedger(ledger.path).get_retirement_certificate('position') == certificate
    assert runtime.load(cycle.cycle_id).phase == VCAPRuntimePhase.COMPLETED
