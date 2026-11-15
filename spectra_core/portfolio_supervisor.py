"""Autonomous Spectra portfolio admission, VCAP servicing and rotation loop.

The Capital Governor remains the sole capital authority. Condor supplies the
candidate ranking; this supervisor only orchestrates existing governed entry,
executor and VCAP lifecycle services. It never bypasses a reservation or sends
an ungoverned order.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from .asset_authority import (
    canonical_kraken_usd_pair,
    load_approved_kraken_assets,
    write_discovered_kraken_assets,
)
from .models import BreakerMode
from .runtime import (
    get_accounting_health_supervisor,
    get_breaker_mode,
    get_capital_governor,
    get_economic_position_ledger,
    get_runtime_scheduler_store,
    get_spectra_accounting_health_snapshot,
    get_vcap_runtime_store,
    get_vcap_state_store,
    spectra_paper_only_enabled,
    spectra_state_dir,
)
from .vcap_entry import PAPER_VCAP_SEED_QUOTE, reserve_vcap_candidate_entry

logger = logging.getLogger(__name__)
GOVERNOR_POSITION_HARD_LIMIT = 83
DEFAULT_CONTROLLER_ID = "condor-portfolio"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be an explicit true/false value")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else float(raw)
    if value < minimum or not float("-inf") < value < float("inf"):
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return value


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


@dataclass(frozen=True)
class PortfolioSupervisorConfig:
    target_active_positions: int = 83
    max_new_entries_per_tick: int = 3
    scan_interval_seconds: float = 60.0
    tick_interval_seconds: float = 10.0
    discovery_top_n: int = 150
    discovery_show_n: int = 100
    account_name: str = "master_account"
    controller_id: str = DEFAULT_CONTROLLER_ID
    vcap_seed_quote: Decimal = Decimal("20")
    expected_exit_fee_bps: Decimal = Decimal("60")
    slippage_allowance_bps: Decimal = Decimal("10")
    buy_max_price_slippage_bps: Decimal = Decimal("10")
    buy_fee_allowance_bps: Decimal = Decimal("60")

    @classmethod
    def from_env(cls) -> "PortfolioSupervisorConfig":
        target = _env_int(
            "SPECTRA_TARGET_ACTIVE_POSITIONS",
            GOVERNOR_POSITION_HARD_LIMIT,
            minimum=1,
            maximum=GOVERNOR_POSITION_HARD_LIMIT,
        )
        return cls(
            target_active_positions=target,
            max_new_entries_per_tick=_env_int(
                "SPECTRA_MAX_NEW_ENTRIES_PER_TICK", 3, minimum=1, maximum=10
            ),
            scan_interval_seconds=_env_float(
                "SPECTRA_DISCOVERY_INTERVAL_SECONDS", 60.0, minimum=10.0
            ),
            tick_interval_seconds=_env_float(
                "SPECTRA_PORTFOLIO_TICK_SECONDS", 10.0, minimum=1.0
            ),
            discovery_top_n=_env_int(
                "SPECTRA_DISCOVERY_TOP_N", 150, minimum=1, maximum=150
            ),
            discovery_show_n=_env_int(
                "SPECTRA_DISCOVERY_SHOW_N", 100, minimum=1, maximum=100
            ),
            account_name=os.getenv("SPECTRA_ACCOUNT_NAME", "master_account").strip()
            or "master_account",
            controller_id=os.getenv("SPECTRA_PORTFOLIO_CONTROLLER_ID", DEFAULT_CONTROLLER_ID).strip()
            or DEFAULT_CONTROLLER_ID,
            vcap_seed_quote=_env_decimal("SPECTRA_VCAP_SEED_QUOTE", "20"),
            expected_exit_fee_bps=_env_decimal("SPECTRA_EXPECTED_EXIT_FEE_BPS", "60"),
            slippage_allowance_bps=_env_decimal("SPECTRA_SLIPPAGE_ALLOWANCE_BPS", "10"),
            buy_max_price_slippage_bps=_env_decimal("SPECTRA_BUY_MAX_PRICE_SLIPPAGE_BPS", "10"),
            buy_fee_allowance_bps=_env_decimal("SPECTRA_BUY_FEE_ALLOWANCE_BPS", "60"),
        )


@dataclass
class PortfolioSupervisorDependencies:
    scan: Callable[..., Any]
    write_discovery: Callable[..., Any]
    load_assets: Callable[..., Any]
    run_vcap_tick: Callable[..., Any]
    reserve_entry: Callable[..., Any]
    create_grid_executor: Callable[..., Any]
    get_client: Callable[..., Any]
    get_governor: Callable[..., Any]
    get_ledger: Callable[..., Any]
    get_vcap_state_store: Callable[..., Any]
    get_vcap_runtime_store: Callable[..., Any]
    get_runtime_scheduler_store: Callable[..., Any]
    get_accounting_supervisor: Callable[..., Any]
    get_accounting_health: Callable[..., Any]
    get_breaker_mode: Callable[..., Any]
    is_paper: Callable[..., Any]
    scheduler_policy: Callable[..., Any]
    clock: Callable[[], float] = time.time
    sleep: Callable[..., Any] = asyncio.sleep


def portfolio_autorun_enabled() -> bool:
    """Paper automation is on by default; live automation needs a second flag."""
    paper_only = spectra_paper_only_enabled()
    master_enabled = _env_bool("SPECTRA_PORTFOLIO_AUTORUN", True)
    if not master_enabled:
        return False
    if paper_only:
        return True
    return (
        _env_bool("SPECTRA_ALLOW_LIVE_EXECUTION", False)
        and _env_bool("SPECTRA_LIVE_AUTORUN", False)
    )


def default_dependencies() -> PortfolioSupervisorDependencies:
    from mcp_servers.hummingbot_api.hummingbot_client import hummingbot_client
    from mcp_servers.hummingbot_api.tools.executor_create import create_grid_executor
    from mcp_servers.hummingbot_api.tools.runtime_scheduler import run_vcap_scheduler_tick
    from spectra_core.market_discovery import scan_kraken_universe
    from spectra_core.runtime_scheduler import RuntimeSchedulerPolicy

    async def scan_with_asset_memory(client: Any, **kwargs: Any) -> dict[str, Any]:
        return await scan_kraken_universe(
            client,
            fingerprint_store=get_vcap_runtime_store(),
            **kwargs,
        )

    return PortfolioSupervisorDependencies(
        scan=scan_with_asset_memory,
        write_discovery=write_discovered_kraken_assets,
        load_assets=load_approved_kraken_assets,
        run_vcap_tick=run_vcap_scheduler_tick,
        reserve_entry=reserve_vcap_candidate_entry,
        create_grid_executor=create_grid_executor,
        get_client=hummingbot_client.get_client,
        get_governor=get_capital_governor,
        get_ledger=get_economic_position_ledger,
        get_vcap_state_store=get_vcap_state_store,
        get_vcap_runtime_store=get_vcap_runtime_store,
        get_runtime_scheduler_store=get_runtime_scheduler_store,
        get_accounting_supervisor=get_accounting_health_supervisor,
        get_accounting_health=get_spectra_accounting_health_snapshot,
        get_breaker_mode=get_breaker_mode,
        is_paper=spectra_paper_only_enabled,
        scheduler_policy=RuntimeSchedulerPolicy,
    )


def _mode_name(paper_only: bool) -> str:
    return "PAPER_ONLY" if paper_only else "LIVE_AUTHORIZED"


def _breaker_enum(value: Any) -> BreakerMode:
    if isinstance(value, BreakerMode):
        return value
    return BreakerMode(str(getattr(value, "value", value)).strip().upper())


def _snapshot_pair(ledger: Any, position_id: str) -> str | None:
    snapshot = ledger.snapshot(position_id)
    if snapshot is None:
        return None
    pair = snapshot.get("pair") if isinstance(snapshot, Mapping) else getattr(snapshot, "pair", None)
    if not pair:
        return None
    try:
        return canonical_kraken_usd_pair(str(pair))
    except ValueError:
        return str(pair).strip().upper() or None


def _json_summary(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_summary(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_summary(item) for item in value]
    return str(value)


def persist_portfolio_status(status: Mapping[str, Any]) -> Path:
    state_dir = spectra_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "portfolio_supervisor_state.json"
    if path.is_symlink():
        raise RuntimeError("portfolio supervisor status must not be a symlink")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=state_dir,
        prefix=".portfolio-status-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(_json_summary(dict(status)), handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return path


def get_portfolio_supervisor_status() -> dict[str, Any]:
    """Return the latest persisted supervisor status for the read-only dashboard."""
    path = spectra_state_dir() / "portfolio_supervisor_state.json"
    if path.is_symlink():
        return {
            "supervisor_state": "UNAVAILABLE",
            "error": "portfolio supervisor status path must not be a symlink",
        }
    if not path.is_file():
        return {
            "supervisor_state": "NOT_STARTED",
            "updated_at": None,
            "error": "portfolio supervisor has not published a status yet",
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read persisted portfolio supervisor status")
        return {
            "supervisor_state": "UNAVAILABLE",
            "error": "persisted portfolio supervisor status is unreadable",
        }
    if not isinstance(raw, dict):
        return {
            "supervisor_state": "UNAVAILABLE",
            "error": "persisted portfolio supervisor status has an invalid shape",
        }
    result = dict(raw)
    if result.get("blocked_reason"):
        result["supervisor_state"] = "BLOCKED"
    elif result.get("last_error"):
        result["supervisor_state"] = "ERROR"
    else:
        result["supervisor_state"] = "RUNNING"
    return result


async def run_portfolio_cycle(
    client: Any,
    *,
    scan_result: Mapping[str, Any] | None,
    config: PortfolioSupervisorConfig | None = None,
    dependencies: PortfolioSupervisorDependencies | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Service existing VCAP positions, then admit only eligible fleet vacancies."""
    cfg = config or PortfolioSupervisorConfig.from_env()
    deps = dependencies or default_dependencies()
    timestamp = deps.clock() if now is None else float(now)
    paper_only = bool(deps.is_paper())
    mode = _mode_name(paper_only)
    breaker = _breaker_enum(deps.get_breaker_mode())
    breaker_mode = breaker.value
    governor = deps.get_governor()
    ledger = deps.get_ledger()

    vcap_result: Any = None
    vcap_error: str | None = None
    try:
        vcap_result = await deps.run_vcap_tick(
            client,
            governor=governor,
            ledger=ledger,
            lifecycle_store=deps.get_vcap_state_store(),
            runtime_store=deps.get_vcap_runtime_store(),
            scheduler_store=deps.get_runtime_scheduler_store(),
            accounting_supervisor=deps.get_accounting_supervisor(),
            seed_quote=cfg.vcap_seed_quote,
            expected_exit_fee_bps=cfg.expected_exit_fee_bps,
            slippage_allowance_bps=cfg.slippage_allowance_bps,
            now=timestamp,
            breaker_mode=breaker,
            scheduler_policy=deps.scheduler_policy(),
            account_name=cfg.account_name,
            controller_id=cfg.controller_id,
            buy_max_price_slippage_bps=cfg.buy_max_price_slippage_bps,
            buy_fee_allowance_bps=cfg.buy_fee_allowance_bps,
        )
    except Exception as exc:
        vcap_error = f"{type(exc).__name__}: {exc}"
        logger.exception("VCAP lifecycle tick failed; new portfolio exposure is paused")
    if (
        vcap_error is None
        and breaker == BreakerMode.NORMAL
        and isinstance(vcap_result, Mapping)
    ):
        tick_results = vcap_result.get("results", [])
        if isinstance(tick_results, list) and any(
            isinstance(item, Mapping) and item.get("serviced") is False
            for item in tick_results
        ):
            vcap_error = "VCAP scheduler reported an unserviced active position"
            logger.error("%s; new portfolio exposure is paused", vcap_error)

    active_ids = list(ledger.list_active_position_ids())
    active_pairs: set[str] = set()
    ledger_uncertain = False
    for position_id in active_ids:
        try:
            pair = _snapshot_pair(ledger, position_id)
        except Exception:
            pair = None
            logger.exception("Could not read active economic position %s", position_id)
        if pair is None:
            ledger_uncertain = True
        else:
            active_pairs.add(pair)

    accounting = deps.get_accounting_health(now=timestamp)
    accounting_allows = (
        isinstance(accounting, Mapping)
        and accounting.get("allows_new_exposure") is True
    )
    unique_vacancies = max(0, cfg.target_active_positions - len(active_pairs))
    governor_slots = max(0, GOVERNOR_POSITION_HARD_LIMIT - len(active_ids))
    entry_capacity = min(
        unique_vacancies,
        governor_slots,
        cfg.max_new_entries_per_tick,
    )

    status: dict[str, Any] = {
        "schema_version": 1,
        "updated_at": timestamp,
        "mode": mode,
        "controller_id": cfg.controller_id,
        "target_active_positions": cfg.target_active_positions,
        "active_positions": len(active_ids),
        "unique_active_positions": len(active_pairs),
        "vacancies": unique_vacancies,
        "available_governor_slots": governor_slots,
        "breaker_mode": breaker_mode,
        "accounting_state": accounting.get("state") if isinstance(accounting, Mapping) else "UNKNOWN",
        "allows_new_exposure": accounting_allows,
        "scan_as_of": scan_result.get("as_of") if isinstance(scan_result, Mapping) else None,
        "fingerprint_observations": (
            int(scan_result.get("fingerprint_observations", 0))
            if isinstance(scan_result, Mapping)
            else 0
        ),
        "scan_candidate_count": 0,
        "new_entries_this_cycle": 0,
        "entries": [],
        "vcap": _json_summary(vcap_result),
        "vcap_error": vcap_error,
        "blocked_reason": None,
        "last_error": None,
    }

    if ledger_uncertain:
        status["blocked_reason"] = "active_position_ledger_unreadable"
        return status
    if vcap_error is not None:
        status["blocked_reason"] = "vcap_lifecycle_tick_failed"
        return status
    if breaker_mode != "NORMAL":
        status["blocked_reason"] = "capital_breaker_not_normal"
        return status
    if not accounting_allows:
        status["blocked_reason"] = "accounting_health_blocks_new_exposure"
        return status
    if scan_result is None:
        status["blocked_reason"] = "market_discovery_unavailable"
        return status

    lists = scan_result.get("lists")
    candidates = lists.get("mean_reversion_candidates") if isinstance(lists, Mapping) else None
    if not isinstance(candidates, list):
        status["blocked_reason"] = "mean_reversion_candidate_list_missing"
        return status
    status["scan_candidate_count"] = len(candidates)

    allowed_pairs: frozenset[str] | None = None
    if paper_only:
        try:
            policy = deps.load_assets(now=timestamp)
        except Exception as exc:
            status["blocked_reason"] = "paper_asset_authority_unavailable"
            status["last_error"] = f"{type(exc).__name__}: {exc}"
            return status
        if not policy.configured:
            status["blocked_reason"] = policy.reason
            return status
        allowed_pairs = policy.pairs

    connector_name = "kraken_paper_trade" if paper_only else "kraken"
    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        if entry_capacity <= 0:
            break
        if not isinstance(candidate, Mapping):
            continue
        if candidate.get("eligible") is not True or candidate.get("capital_authorized") is not False:
            continue
        if str(candidate.get("quote_asset") or "USD").strip().upper() != "USD":
            continue
        direction = str(candidate.get("direction") or candidate.get("side") or "BUY").strip().upper()
        if direction not in {"BUY", "LONG", "1"}:
            continue
        try:
            pair = canonical_kraken_usd_pair(str(candidate.get("trading_pair") or ""))
        except ValueError:
            continue
        if pair in active_pairs or (allowed_pairs is not None and pair not in allowed_pairs):
            continue
        try:
            price = float(candidate.get("current_price"))
            volume = float(candidate.get("volume_24h_usd"))
            if not (price > 0 and volume > 0):
                continue
        except (TypeError, ValueError):
            continue

        try:
            admission = await deps.reserve_entry(
                client,
                scan_result=scan_result,
                trading_pair=pair,
                account_name=cfg.account_name,
                governor=governor,
                now=timestamp,
            )
        except Exception as exc:
            entries.append({
                "pair": pair,
                "status": "reservation_rejected",
                "reason": f"{type(exc).__name__}: {exc}",
            })
            continue

        position_id = getattr(admission, "position_id", None)
        proposal_id = getattr(admission, "proposal_id", None)
        slot_opened = getattr(admission, "position_slot_opened", False)
        if not slot_opened or not position_id or not proposal_id:
            entries.append({
                "pair": pair,
                "status": "reservation_not_approved",
                "reason": str(getattr(admission, "decision_reason", "no_position_slot")),
            })
            continue

        # A successful reservation owns a slot even if the dispatch response is
        # uncertain. Never retry the same pair blindly after a possible send.
        active_pairs.add(pair)
        entry_capacity -= 1
        status["new_entries_this_cycle"] += 1
        entry_record = {
            "pair": pair,
            "proposal_id": str(proposal_id),
            "position_id": str(position_id),
            "amount_quote": str(getattr(admission, "amount_quote", PAPER_VCAP_SEED_QUOTE)),
            "status": "reserved",
            "executor_id": None,
        }
        try:
            executor_result = await deps.create_grid_executor(
                client,
                account_name=cfg.account_name,
                connector_name=connector_name,
                trading_pair=pair,
                side=1,
                start_price=price * 0.999,
                end_price=price * 1.001,
                limit_price=price * 0.998,
                total_amount_quote=float(getattr(admission, "amount_quote", PAPER_VCAP_SEED_QUOTE)),
                min_order_amount_quote=float(getattr(admission, "amount_quote", PAPER_VCAP_SEED_QUOTE)),
                max_open_orders=1,
                max_orders_per_batch=1,
                leverage=1,
                controller_id=cfg.controller_id,
                save_as_default=False,
                spectra_proposal_id=str(proposal_id),
                spectra_intent="ENTRY",
                spectra_position_id=str(position_id),
                spectra_max_price_slippage_bps=float(cfg.buy_max_price_slippage_bps),
                spectra_fee_allowance_bps=float(cfg.buy_fee_allowance_bps),
            )
            executor_id = (
                executor_result.get("executor_id")
                if isinstance(executor_result, Mapping)
                else None
            )
            if not executor_id or (isinstance(executor_result, Mapping) and executor_result.get("error")):
                entry_record["status"] = "dispatch_uncertain"
                entry_record["reason"] = str(
                    executor_result.get("error", "executor response omitted executor_id")
                    if isinstance(executor_result, Mapping)
                    else "executor response was not a mapping"
                )
                status["last_error"] = entry_record["reason"]
                entries.append(entry_record)
                # Stop this cycle after an uncertain dispatch; do not create
                # further exposure until the receipt and ledger reconcile.
                break
            entry_record["status"] = "submitted"
            entry_record["executor_id"] = str(executor_id)
            entries.append(entry_record)
            logger.info(
                "Spectra admitted %s via Governor proposal %s and executor %s",
                pair,
                proposal_id,
                executor_id,
            )
        except Exception as exc:
            entry_record["status"] = "dispatch_uncertain"
            entry_record["reason"] = f"{type(exc).__name__}: {exc}"
            entries.append(entry_record)
            status["last_error"] = entry_record["reason"]
            logger.exception(
                "Executor dispatch for %s is uncertain; reservation remains owned and will not be retried blindly",
                pair,
            )
            break

    status["entries"] = entries
    status["active_positions"] = len(active_ids) + sum(
        1 for entry in entries if entry.get("status") in {"reserved", "submitted", "dispatch_uncertain"}
    )
    status["unique_active_positions"] = len(active_pairs)
    status["vacancies"] = max(0, cfg.target_active_positions - len(active_pairs))
    status["available_governor_slots"] = max(
        0, GOVERNOR_POSITION_HARD_LIMIT - status["active_positions"]
    )
    if status["blocked_reason"] is None and not entries and status["vacancies"] > 0:
        status["blocked_reason"] = "no_new_candidate_admitted"
    return status


async def run_portfolio_supervisor(
    *,
    config: PortfolioSupervisorConfig | None = None,
    dependencies: PortfolioSupervisorDependencies | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the autonomous fleet loop as a persistent Core background task."""
    cfg = config or PortfolioSupervisorConfig.from_env()
    deps = dependencies or default_dependencies()
    scan_result: Mapping[str, Any] | None = None
    last_scan_at = 0.0
    logger.info(
        "Spectra portfolio supervisor initialized: target=%s, initial-entry=%s USD, tick=%ss",
        cfg.target_active_positions,
        PAPER_VCAP_SEED_QUOTE,
        cfg.tick_interval_seconds,
    )

    while stop_event is None or not stop_event.is_set():
        try:
            if not portfolio_autorun_enabled():
                await deps.sleep(15.0)
                continue

            client = await deps.get_client()
            now = deps.clock()
            if scan_result is None or now - last_scan_at >= cfg.scan_interval_seconds:
                try:
                    fresh_scan = await deps.scan(
                        client,
                        top_n=cfg.discovery_top_n,
                        show_n=cfg.discovery_show_n,
                    )
                    if deps.is_paper():
                        deps.write_discovery(
                            fresh_scan,
                            now=now,
                            max_assets=cfg.discovery_show_n,
                        )
                    scan_result = fresh_scan
                    last_scan_at = now
                    logger.info(
                        "Spectra market discovery refreshed: %s mean-reversion candidates",
                        len(
                            fresh_scan.get("lists", {}).get("mean_reversion_candidates", [])
                        ),
                    )
                except Exception:
                    scan_result = None
                    last_scan_at = now
                    logger.exception("Spectra market discovery failed; entry admission paused")

            status = await run_portfolio_cycle(
                client,
                scan_result=scan_result,
                config=cfg,
                dependencies=deps,
                now=now,
            )
            persist_portfolio_status(status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Spectra portfolio supervisor cycle failed")
            try:
                persist_portfolio_status({
                    "schema_version": 1,
                    "updated_at": deps.clock(),
                    "mode": "UNKNOWN",
                    "target_active_positions": cfg.target_active_positions,
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "blocked_reason": "supervisor_cycle_exception",
                })
            except Exception:
                logger.exception("Could not persist portfolio supervisor error state")

        await deps.sleep(cfg.tick_interval_seconds)
