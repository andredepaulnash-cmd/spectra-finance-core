from decimal import Decimal
from types import SimpleNamespace

import pytest

from spectra_core.models import BreakerMode
from spectra_core.portfolio_supervisor import (
    PortfolioSupervisorConfig,
    PortfolioSupervisorDependencies,
    portfolio_autorun_enabled,
    run_portfolio_cycle,
)


class FakeLedger:
    def __init__(self, pairs=()):
        self._pairs = list(pairs)

    def list_active_position_ids(self):
        return [f"position-{index}" for index in range(len(self._pairs))]

    def snapshot(self, position_id):
        index = int(position_id.rsplit("-", 1)[1])
        return SimpleNamespace(pair=self._pairs[index])


def _candidate(pair, score=90.0):
    return {
        "trading_pair": pair,
        "quote_asset": "USD",
        "current_price": 100.0,
        "minimum_order_quote": 1.0,
        "volume_24h_usd": 1_000_000.0,
        "selection_score": score,
        "eligible": True,
        "capital_authorized": False,
        "direction": "BUY",
    }


def _scan(*pairs):
    return {
        "source": "Kraken spot market data via Hummingbot API",
        "as_of": 1000.0,
        "paper_only": True,
        "orders_created": 0,
        "lists": {"mean_reversion_candidates": [_candidate(pair) for pair in pairs]},
    }


def _config(**overrides):
    values = {
        "target_active_positions": 83,
        "max_new_entries_per_tick": 3,
        "scan_interval_seconds": 60.0,
        "tick_interval_seconds": 10.0,
        "discovery_top_n": 150,
        "discovery_show_n": 100,
        "account_name": "master_account",
        "controller_id": "condor-portfolio",
        "vcap_seed_quote": Decimal("20"),
        "expected_exit_fee_bps": Decimal("60"),
        "slippage_allowance_bps": Decimal("10"),
        "buy_max_price_slippage_bps": Decimal("10"),
        "buy_fee_allowance_bps": Decimal("60"),
    }
    values.update(overrides)
    return PortfolioSupervisorConfig(**values)


def _dependencies(
    *,
    active_pairs=(),
    paper=True,
    breaker=BreakerMode.NORMAL,
    accounting_allows=True,
    vcap_result=None,
    allowed_pairs=None,
):
    ledger = FakeLedger(active_pairs)
    reserved = []
    created = []
    vcap_calls = []

    async def run_vcap(client, **kwargs):
        vcap_calls.append(kwargs)
        return {"results": []} if vcap_result is None else vcap_result

    async def reserve(client, **kwargs):
        pair = kwargs["trading_pair"]
        reserved.append(pair)
        return SimpleNamespace(
            position_slot_opened=True,
            position_id="new-" + pair,
            proposal_id="proposal-" + pair,
            amount_quote=Decimal("20"),
            decision_reason="reserved",
        )

    async def create(client, **kwargs):
        created.append(kwargs)
        ledger._pairs.append(kwargs["trading_pair"])
        return {"executor_id": "executor-" + kwargs["trading_pair"]}

    assets = SimpleNamespace(
        configured=True,
        pairs=frozenset(allowed_pairs or {"BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD"}),
        reason="automatic_discovery_asset_list_loaded",
    )

    deps = PortfolioSupervisorDependencies(
        scan=lambda *args, **kwargs: _scan("BTC-USD"),
        write_discovery=lambda *args, **kwargs: None,
        load_assets=lambda **kwargs: assets,
        run_vcap_tick=run_vcap,
        reserve_entry=reserve,
        create_grid_executor=create,
        get_client=lambda: None,
        get_governor=lambda: object(),
        get_ledger=lambda: ledger,
        get_vcap_state_store=lambda: object(),
        get_vcap_runtime_store=lambda: object(),
        get_runtime_scheduler_store=lambda: object(),
        get_accounting_supervisor=lambda: object(),
        get_accounting_health=lambda **kwargs: {
            "state": "HEALTHY" if accounting_allows else "STALE",
            "allows_new_exposure": accounting_allows,
        },
        get_breaker_mode=lambda: breaker,
        is_paper=lambda: paper,
        scheduler_policy=lambda: object(),
        clock=lambda: 1000.0,
        sleep=lambda _: None,
    )
    return deps, reserved, created, vcap_calls


@pytest.mark.asyncio
async def test_supervisor_fills_only_three_vacancies_per_tick_and_uses_governed_grid_executor():
    active = [f"OLD{index:02d}-USD" for index in range(80)]
    deps, reserved, created, vcap_calls = _dependencies(active_pairs=active)
    candidates = _scan("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD")

    status = await run_portfolio_cycle(
        object(),
        scan_result=candidates,
        config=_config(),
        dependencies=deps,
        now=1000.0,
    )

    assert reserved == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert len(created) == 3
    assert status["new_entries_this_cycle"] == 3
    assert status["active_positions"] == 83
    assert status["unique_active_positions"] == 83
    assert status["vacancies"] == 0
    assert status["available_governor_slots"] == 0
    assert all(call["connector_name"] == "kraken_paper_trade" for call in created)
    assert all(call["total_amount_quote"] == 20.0 for call in created)
    assert all(call["spectra_intent"] == "ENTRY" for call in created)
    assert all(call["controller_id"] == "condor-portfolio" for call in created)
    assert vcap_calls[0]["breaker_mode"] == BreakerMode.NORMAL
    assert vcap_calls[0]["seed_quote"] == Decimal("20")


@pytest.mark.asyncio
async def test_supervisor_does_not_force_entries_when_no_candidate_is_eligible():
    active = [f"OLD{index:02d}-USD" for index in range(82)]
    deps, reserved, created, _ = _dependencies(active_pairs=active)
    status = await run_portfolio_cycle(
        object(),
        scan_result=_scan(),
        config=_config(),
        dependencies=deps,
        now=1000.0,
    )

    assert reserved == []
    assert created == []
    assert status["new_entries_this_cycle"] == 0
    assert status["vacancies"] == 1
    assert status["blocked_reason"] == "no_new_candidate_admitted"


@pytest.mark.asyncio
async def test_supervisor_pauses_new_entries_when_breaker_is_halted():
    deps, reserved, created, _ = _dependencies(breaker=BreakerMode.HALTED)
    status = await run_portfolio_cycle(
        object(),
        scan_result=_scan("BTC-USD"),
        config=_config(target_active_positions=1),
        dependencies=deps,
        now=1000.0,
    )

    assert reserved == []
    assert created == []
    assert status["blocked_reason"] == "capital_breaker_not_normal"


@pytest.mark.asyncio
async def test_supervisor_pauses_new_entries_when_vcap_cannot_service_existing_positions():
    deps, reserved, created, _ = _dependencies(
        active_pairs=["BTC-USD"],
        vcap_result={"results": [{"serviced": False, "reason": "vcap_envelope_unbound"}]},
    )
    status = await run_portfolio_cycle(
        object(),
        scan_result=_scan("ETH-USD"),
        config=_config(target_active_positions=2),
        dependencies=deps,
        now=1000.0,
    )

    assert reserved == []
    assert created == []
    assert status["blocked_reason"] == "vcap_lifecycle_tick_failed"


@pytest.mark.asyncio
async def test_supervisor_pauses_new_entries_when_accounting_health_is_not_clear():
    deps, reserved, created, _ = _dependencies(accounting_allows=False)
    status = await run_portfolio_cycle(
        object(),
        scan_result=_scan("BTC-USD"),
        config=_config(target_active_positions=1),
        dependencies=deps,
        now=1000.0,
    )

    assert reserved == []
    assert created == []
    assert status["blocked_reason"] == "accounting_health_blocks_new_exposure"


@pytest.mark.asyncio
async def test_live_mode_uses_live_connector_but_still_requires_governor_path():
    deps, reserved, created, _ = _dependencies(paper=False)
    status = await run_portfolio_cycle(
        object(),
        scan_result=_scan("BTC-USD"),
        config=_config(target_active_positions=1, max_new_entries_per_tick=1),
        dependencies=deps,
        now=1000.0,
    )

    assert reserved == ["BTC-USD"]
    assert len(created) == 1
    assert created[0]["connector_name"] == "kraken"
    assert created[0]["controller_id"] == "condor-portfolio"
    assert status["mode"] == "LIVE_AUTHORIZED"
    assert status["new_entries_this_cycle"] == 1


def test_paper_autorun_defaults_on_and_live_autorun_requires_a_second_flag(monkeypatch):
    monkeypatch.setenv("CONDOR_AGENT_SLUG", "spectra")
    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "true")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "false")
    monkeypatch.delenv("SPECTRA_PORTFOLIO_AUTORUN", raising=False)
    assert portfolio_autorun_enabled() is True

    monkeypatch.setenv("SPECTRA_PAPER_ONLY", "false")
    monkeypatch.setenv("SPECTRA_ALLOW_LIVE_EXECUTION", "true")
    monkeypatch.setenv("SPECTRA_PORTFOLIO_AUTORUN", "true")
    monkeypatch.setenv("SPECTRA_LIVE_AUTORUN", "false")
    assert portfolio_autorun_enabled() is False

    monkeypatch.setenv("SPECTRA_LIVE_AUTORUN", "true")
    assert portfolio_autorun_enabled() is True

    monkeypatch.setenv("SPECTRA_PORTFOLIO_AUTORUN", "false")
    assert portfolio_autorun_enabled() is False



@pytest.mark.asyncio
async def test_supervisor_fills_83_unique_slots_and_replenishes_a_closed_slot_automatically():
    pairs = [f"ASSET{index:03d}-USD" for index in range(84)]
    deps, reserved, created, _ = _dependencies(allowed_pairs=pairs)
    config = _config(target_active_positions=83, max_new_entries_per_tick=3)
    client = object()

    # The persistent ledger is updated by each successful executor creation.
    # The supervisor must fill the whole fleet over successive autonomous ticks.
    for tick in range(28):
        status = await run_portfolio_cycle(
            client,
            scan_result=_scan(*pairs[:83]),
            config=config,
            dependencies=deps,
            now=1_000.0 + tick,
        )

    ledger = deps.get_ledger()
    assert len(ledger._pairs) == 83
    assert len(set(ledger._pairs)) == 83
    assert len(reserved) == 83
    assert len(created) == 83
    assert status["active_positions"] == 83
    assert status["unique_active_positions"] == 83
    assert status["vacancies"] == 0

    # Simulate one position reaching its terminal close. The next scan excludes
    # that retired candidate and offers a different eligible asset. No operator
    # prompt is involved: the next supervisor cycle reserves and dispatches it.
    closed_pair = ledger._pairs.pop(0)
    replacement_scan = _scan(*pairs[1:])
    replacement = await run_portfolio_cycle(
        client,
        scan_result=replacement_scan,
        config=config,
        dependencies=deps,
        now=1_100.0,
    )

    assert closed_pair not in ledger._pairs
    assert pairs[83] in ledger._pairs
    assert len(ledger._pairs) == 83
    assert len(set(ledger._pairs)) == 83
    assert reserved[-1] == pairs[83]
    assert created[-1]["trading_pair"] == pairs[83]
    assert replacement["new_entries_this_cycle"] == 1
    assert replacement["vacancies"] == 0
    assert replacement["active_positions"] == 83
