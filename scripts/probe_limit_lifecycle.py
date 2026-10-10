"""Reproduce pinned LIMIT lifecycle behavior without external I/O.

Whole primary source files are hash checked before selected AST classes/methods
are executed. OrderExecutor, TrackedOrder, InFlightOrder and ClientOrderTracker
logic is primary source; framework startup, callbacks, clocks, fees and transport
are explicit inert fixtures. Findings record observed deployment blockers.
Passing these reproductions does not establish runtime safety or live approval.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import hashlib
import json
import logging
import math
import re
import socket
import subprocess
import urllib.request
from collections import defaultdict
from contextlib import ExitStack
from decimal import Decimal
from enum import Enum
from itertools import chain
from pathlib import Path
from types import SimpleNamespace as NS
from typing import NamedTuple
from unittest.mock import patch

from cachetools import TTLCache

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "hummingbot_limit_contract"
)
CORE_PIN = "9af100d6822da7d2d0291a906c730ef172284ee2"
SOURCE_SCOPE = "source_logic_only"
CASES = (
    "fixed_limit_partial_cancel_retry",
    "plain_cancel_recreates_running_executor",
    "zero_fill_cancel_then_late_fill",
    "partial_then_replacement_then_late_fill",
    "sell_cancel_then_late_fill_can_overcommit",
    "stop_cancel_then_late_fill",
    "stop_known_partial_preserves_hold",
    "partial_failure_fixed_limit_retry",
    "zero_fill_failure_then_late_fill",
    "retry_exhaustion_preserves_known_partial",
    "indeterminate_shutdown_is_not_venue_finality",
    "cached_order_late_fill_after_ttl",
    "kraken_limit_request_drops_ioc",
    "kraken_validate_request_flag_dropped",
    "cloudflare_open_order_shape_mismatch",
    "cloudflare_missing_open_order_retries_add",
)


def git_blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def verify_source_integrity():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    if manifest["core_revision"] != CORE_PIN:
        raise AssertionError("Wrong primary source revision")
    verified = {}
    for record in manifest["files"]:
        raw = (FIXTURES / record["file"]).read_bytes()
        if (
            git_blob_sha(raw) != record["git_blob_sha"]
            or hashlib.sha256(raw).hexdigest() != record["sha256"]
            or len(raw) != record["size_bytes"]
        ):
            raise AssertionError(f"Primary source integrity failed: {record['file']}")
        verified[record["file"]] = raw.decode()
    if set(verified) != {
        "common.py",
        "order_types.py",
        "order_executor.py",
        "kraken_exchange.py",
        "core_models_executors.py",
        "in_flight_order.py",
        "client_order_tracker.py",
        "LICENSE",
    }:
        raise AssertionError("Primary source inventory differs from expected")
    return manifest, verified


def compile_primary_classes(source, classes, env, filename):
    nodes = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name in classes
    ]
    if {node.name for node in nodes} != classes:
        raise AssertionError(f"Missing primary classes: {filename}")
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            )
        ]
        + nodes,
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), env)


def deny_external_io(*args, **kwargs):
    raise AssertionError("External I/O is forbidden in source-only LIMIT probes")


def io_trap():
    stack = ExitStack()
    for target in (
        "socket.socket",
        "socket.create_connection",
        "socket.getaddrinfo",
        "subprocess.Popen",
        "urllib.request.urlopen",
    ):
        stack.enter_context(patch(target, deny_external_io))
    return stack


class RunnableStatus(Enum):
    RUNNING = 1
    SHUTTING_DOWN = 2
    TERMINATED = 3


class InertExecutorBase:
    """Fixture for framework initialization; never starts a background executor."""

    def __init__(self, strategy, config, connectors, update_interval, max_retries):
        self._strategy, self.config = strategy, config
        self.connectors = strategy.connectors
        self._status = RunnableStatus.RUNNING
        self._max_retries, self._current_retries = max_retries, 0
        self._held_position_orders = []
        self.close_type = None

    @property
    def status(self):
        return self._status

    def stop(self):
        self._status = RunnableStatus.TERMINATED
        # Mirrors inspected ExecutorBase.stop listener removal; startup itself
        # is inert and the full event bus is not being executed.
        self._strategy.executor_callbacks_attached = False

    def place_order(self, **kwargs):
        return self._strategy.place_order(**kwargs)

    def get_in_flight_order(self, connector_name, order_id):
        return self.connectors[connector_name]._order_tracker.fetch_order(
            client_order_id=order_id
        )


class ZeroFeeFixture:
    """Fee fixture: no venue fee schedule is inferred or tested here."""

    fee_asset = "USD"

    def fee_amount_in_token(self, **kwargs):
        return Decimal(0)

    def to_json(self):
        return {"fixture_fee": "zero"}


async def inert_sleep(delay):
    return None


def primary_environment(sources):
    env = {
        "__name__": "spectra_pinned_limit_source_probe",
        "Enum": Enum,
        "Decimal": Decimal,
        "logging": logging,
        "re": re,
        "asyncio": asyncio,
        "copy": copy,
        "math": math,
        "NamedTuple": NamedTuple,
        "TTLCache": TTLCache,
        "defaultdict": defaultdict,
        "chain": chain,
        "s_decimal_0": Decimal(0),
        "cot_logger": None,
        "RunnableStatus": RunnableStatus,
        "ExecutorBase": InertExecutorBase,
        "GatewayBase": type("GatewayBase", (), {}),
    }
    compile_primary_classes(
        sources["common.py"],
        {"OrderType", "TradeType", "PriceType", "PositionAction"},
        env,
        "<pinned-common>",
    )
    compile_primary_classes(
        sources["order_types.py"], {"ExecutionStrategy"}, env, "<pinned-order-types>"
    )
    compile_primary_classes(
        sources["in_flight_order.py"],
        {"OrderState", "OrderUpdate", "TradeUpdate", "InFlightOrder"},
        env,
        "<pinned-in-flight-order>",
    )
    compile_primary_classes(
        sources["core_models_executors.py"],
        {"TrackedOrder", "CloseType"},
        env,
        "<pinned-tracked-order>",
    )
    compile_primary_classes(
        sources["client_order_tracker.py"],
        {"ClientOrderTracker"},
        env,
        "<pinned-client-order-tracker>",
    )
    compile_primary_classes(
        sources["order_executor.py"], {"OrderExecutor"}, env, "<pinned-order-executor>"
    )
    names = (
        "OrderCancelled",
        "OrderFilled",
        "OrderFailure",
        "BuyOrderCreated",
        "SellOrderCreated",
        "BuyOrderCompleted",
        "SellOrderCompleted",
    )
    env["MarketEvent"] = NS(**{name: name for name in names})
    for name in (
        "OrderCancelledEvent",
        "OrderFilledEvent",
        "MarketOrderFailureEvent",
        "BuyOrderCreatedEvent",
        "SellOrderCreatedEvent",
        "BuyOrderCompletedEvent",
        "SellOrderCompletedEvent",
    ):
        env[name] = NS
    return env


class InertStrategy:
    """Only fills transport and event callback seams; primary tracker owns state."""

    def __init__(self, env):
        self.env, self.submissions, self.cancels = env, [], []
        self.current_timestamp = 1
        self._order_tracker = env["ClientOrderTracker"](self)
        self.connectors = {"kraken": self}
        self.executor = None
        self.executor_callbacks_attached = True

    def place_order(self, **kwargs):
        order_id = "fixture-" + str(len(self.submissions) + 1)
        self.submissions.append(dict(kwargs))
        order = self.env["InFlightOrder"](
            client_order_id=order_id,
            trading_pair=kwargs["trading_pair"],
            order_type=kwargs["order_type"],
            trade_type=kwargs["side"],
            amount=kwargs["amount"],
            price=kwargs["price"],
            creation_timestamp=1,
            exchange_order_id="exchange-" + order_id,
            initial_state=self.env["OrderState"].OPEN,
        )
        self._order_tracker.start_tracking_order(order)
        return order_id

    def cancel(self, **kwargs):
        self.cancels.append(kwargs)
        order = self._order_tracker.fetch_order(client_order_id=kwargs["order_id"])
        order.update_with_order_update(
            self.env["OrderUpdate"](
                trading_pair="ALT-USD",
                update_timestamp=2,
                new_state=self.env["OrderState"].PENDING_CANCEL,
                client_order_id=order.client_order_id,
            )
        )

    def trigger_event(self, tag, event):
        if not self.executor_callbacks_attached:
            return
        callback = {
            "OrderCancelled": "process_order_canceled_event",
            "OrderFilled": "process_order_filled_event",
            "OrderFailure": "process_order_failed_event",
        }.get(tag)
        if callback:
            getattr(self.executor, callback)(None, self, event)


def new_executor(env, side="BUY"):
    strategy = InertStrategy(env)
    config = NS(
        id="offline-executor",
        connector_name="kraken",
        trading_pair="ALT-USD",
        side=env["TradeType"][side],
        amount=Decimal("10"),
        price=Decimal("1.0174"),
        execution_strategy=env["ExecutionStrategy"].LIMIT,
        position_action=env["PositionAction"].OPEN,
        slippage_pct=Decimal("0.05"),
    )
    executor = env["OrderExecutor"](strategy, config)
    strategy.executor = executor
    executor._sleep = inert_sleep
    executor.place_open_order()
    executor.update_tracked_order_with_order_id("fixture-1")
    return executor, strategy


def fill(strategy, order_id, amount, trade_id="trade-1"):
    env = strategy.env
    strategy._order_tracker.process_trade_update(
        env["TradeUpdate"](
            trade_id=trade_id,
            client_order_id=order_id,
            exchange_order_id="exchange-" + order_id,
            trading_pair="ALT-USD",
            fill_timestamp=3,
            fill_price=Decimal("1.0174"),
            fill_base_amount=Decimal(amount),
            fill_quote_amount=Decimal(amount) * Decimal("1.0174"),
            fee=ZeroFeeFixture(),
        )
    )


async def terminal_update(strategy, state, order_id="fixture-1"):
    await strategy._order_tracker._process_order_update(
        strategy.env["OrderUpdate"](
            trading_pair="ALT-USD",
            update_timestamp=2,
            new_state=strategy.env["OrderState"][state],
            client_order_id=order_id,
        )
    )


def result(case, blocker=False, **facts):
    return {
        "case": case,
        "outcome": "deployment_blocker" if blocker else "observed_source_behavior",
        "source_scope": SOURCE_SCOPE,
        "installed_backend_verified": False,
        "live_approval_eligible": False,
        **facts,
    }


async def lifecycle_probe(case, env):
    ex, st = new_executor(
        env, "SELL" if case == "sell_cancel_then_late_fill_can_overcommit" else "BUY"
    )
    if case == "fixed_limit_partial_cancel_retry":
        fill(st, "fixture-1", "3")
        await terminal_update(st, "CANCELED")
        await ex.control_task()
        assert st.submissions[1]["amount"] == Decimal("7")
        assert st.submissions[1]["price"] == Decimal("1.0174")
        assert st.submissions[1]["order_type"] == env["OrderType"].LIMIT
        return result(
            case,
            first_fill="3",
            replacement="7",
            fixed_price="1.0174",
            market_fallback=False,
        )
    if case == "plain_cancel_recreates_running_executor":
        await terminal_update(st, "CANCELED")
        await ex.control_task()
        assert len(st.submissions) == 2
        return result(
            case,
            True,
            replacement="10",
            reason="Cancellation clears _order while RUNNING; next tick places again",
        )
    if case in {
        "zero_fill_cancel_then_late_fill",
        "sell_cancel_then_late_fill_can_overcommit",
    }:
        await terminal_update(st, "CANCELED")
        old = st._order_tracker.fetch_cached_order("fixture-1")
        fill(st, "fixture-1", "3")
        assert old.executed_amount_base == Decimal("3")
        assert ex.executed_amount_base == 0
        await ex.control_task()
        assert st.submissions[1]["amount"] == Decimal("10")
        return result(
            case,
            True,
            tracker_cached_fill="3",
            executor_counted_fill="0",
            replacement="10",
            potential_total="13",
            authorized_total="10",
            reason="Late fill stays in canceled order; executor total excludes canceled list",
        )
    if case == "partial_then_replacement_then_late_fill":
        fill(st, "fixture-1", "3")
        await terminal_update(st, "CANCELED")
        await ex.control_task()
        assert st.submissions[1]["amount"] == Decimal("7")
        fill(st, "fixture-1", "2", "trade-2")
        assert ex.executed_amount_base == Decimal("5")
        return result(
            case,
            True,
            old_fills_after_update="5",
            replacement_already_sent="7",
            potential_total="12",
            authorized_total="10",
            reason="Existing replacement is not resized after old cached order receives late fill",
        )
    if case == "stop_cancel_then_late_fill":
        ex.early_stop(keep_position=True)
        await ex.control_task()
        assert len(st.cancels) == 1
        await terminal_update(st, "CANCELED")
        await ex.control_task()
        assert (
            ex.status == RunnableStatus.TERMINATED
            and ex.close_type == env["CloseType"].EARLY_STOP
        )
        fill(st, "fixture-1", "3")
        assert st._order_tracker.fetch_cached_order(
            "fixture-1"
        ).executed_amount_base == Decimal("3")
        assert ex.executed_amount_base == 0 and not ex._held_position_orders
        return result(
            case,
            True,
            new_placements=0,
            terminal_snapshot="EARLY_STOP without exposure",
            cached_late_fill="3",
            reason="Terminal executor does not prove final order/trade coverage",
        )
    if case == "stop_known_partial_preserves_hold":
        fill(st, "fixture-1", "3")
        ex.early_stop(keep_position=True)
        await ex.control_task()
        await terminal_update(st, "CANCELED")
        await ex.control_task()
        assert ex.close_type == env["CloseType"].POSITION_HOLD
        assert ex._held_position_orders[0]["executed_amount_base"] == "3"
        assert len(st.submissions) == 1
        return result(case, held_base="3", new_placements=0, close_type="POSITION_HOLD")
    if case == "partial_failure_fixed_limit_retry":
        fill(st, "fixture-1", "3")
        await terminal_update(st, "FAILED")
        await ex.control_task()
        assert st.submissions[1]["amount"] == Decimal("7")
        assert st.submissions[1]["order_type"] == env["OrderType"].LIMIT
        assert st.submissions[1]["price"] == Decimal("1.0174")
        return result(
            case,
            retained_fill="3",
            replacement="7",
            fixed_price="1.0174",
            market_fallback=False,
        )
    if case == "zero_fill_failure_then_late_fill":
        await terminal_update(st, "FAILED")
        fill(st, "fixture-1", "3")
        assert st._order_tracker.fetch_cached_order(
            "fixture-1"
        ).executed_amount_base == Decimal("3")
        assert ex.executed_amount_base == 0
        await ex.control_task()
        assert st.submissions[1]["amount"] == Decimal("10")
        return result(
            case,
            True,
            cached_late_fill="3",
            replacement="10",
            potential_total="13",
            reason="Failed zero-fill order is excluded from executor quantity aggregate",
        )
    if case == "retry_exhaustion_preserves_known_partial":
        fill(st, "fixture-1", "3")
        await terminal_update(st, "FAILED")
        ex._current_retries = 11
        ex.evaluate_max_retries()
        assert (
            ex.status == RunnableStatus.TERMINATED
            and ex.close_type == env["CloseType"].POSITION_HOLD
        )
        assert ex._held_position_orders[0]["executed_amount_base"] == "3"
        return result(
            case,
            held_base="3",
            close_type="POSITION_HOLD",
            retry_exhaustion_comparison="current_retries > max_retries",
        )
    if case == "indeterminate_shutdown_is_not_venue_finality":
        ex._order.order = None
        ex.early_stop(keep_position=True)
        for _ in range(ex.MAX_SHUTDOWN_TICKS):
            await ex.control_task()
        assert (
            ex.status == RunnableStatus.TERMINATED
            and ex.close_type == env["CloseType"].FAILED
        )
        assert not st.cancels
        return result(
            case,
            True,
            shutdown_ticks=12,
            close_type="FAILED",
            cancel_requests=0,
            reason="Missing tracked order object prevents cancellation; stopped executor is not absence proof",
        )
    if case == "cached_order_late_fill_after_ttl":
        clock = [0]
        st._order_tracker._cached_orders = TTLCache(
            maxsize=1000, ttl=30, timer=lambda: clock[0]
        )
        await terminal_update(st, "CANCELED")
        old = st._order_tracker.fetch_cached_order("fixture-1")
        clock[0] = 31
        fill(st, "fixture-1", "3")
        assert old.executed_amount_base == 0
        assert not st._order_tracker.all_fillable_orders
        return result(
            case,
            True,
            primary_cached_order_ttl_seconds=30,
            injected_clock_seconds=31,
            untracked_trade_amount="3",
            reason="Expired cached order is absent from all_fillable_orders; source tracker drops later trade update",
        )
    raise AssertionError(f"Unknown lifecycle probe: {case}")


def kraken_class(sources, env):
    cls = next(
        node
        for node in ast.parse(sources["kraken_exchange.py"]).body
        if isinstance(node, ast.ClassDef) and node.name == "KrakenExchange"
    )
    selected = {
        "_place_order",
        "_api_request_with_retry",
        "is_cloudflare_exception",
        "get_open_orders_with_userref",
    }
    cls.body = [
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in selected
    ]
    cls.bases = []
    constants = NS(
        ADD_ORDER_PATH_URL="/private/AddOrder",
        OPEN_ORDERS_PATH_URL="/private/OpenOrders",
    )
    local_env = dict(
        env,
        CONSTANTS=constants,
        RESTMethod=NS(POST="POST"),
        asyncio=NS(sleep=inert_sleep),
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            cls,
        ],
        type_ignores=[],
    )
    exec(
        compile(ast.fix_missing_locations(module), "<pinned-kraken-methods>", "exec"),
        local_env,
    )
    return local_env["KrakenExchange"], constants


async def transport_probe(case, env, sources):
    Kraken, constants = kraken_class(sources, env)
    obj, calls = Kraken(), []
    obj.REQUEST_ATTEMPTS, obj.current_timestamp = 5, 123
    obj.logger = lambda: logging.getLogger("offline-kraken")

    async def symbol(trading_pair):
        return "ALTUSD"

    obj.exchange_symbol_associated_to_pair = symbol

    async def response(**kwargs):
        calls.append(kwargs)
        if case in {
            "kraken_limit_request_drops_ioc",
            "kraken_validate_request_flag_dropped",
        }:
            return {"error": [], "result": {"txid": ["fixture-exchange-id"]}}
        if kwargs["path_url"] == constants.OPEN_ORDERS_PATH_URL:
            orders = (
                {"accepted-order": {"userref": 123}}
                if case == "cloudflare_open_order_shape_mismatch"
                else {}
            )
            return {"error": [], "result": {"open": orders}}
        if sum(x["path_url"] == constants.ADD_ORDER_PATH_URL for x in calls) == 1:
            raise IOError("HTTP status is 502.")
        return {"error": [], "result": {"txid": ["second-order-id"]}}

    obj._api_request = response
    error = None
    try:
        await obj._place_order(
            "123",
            "ALT-USD",
            Decimal("10"),
            env["TradeType"].BUY,
            env["OrderType"].LIMIT,
            Decimal("1.0174"),
            timeinforce="IOC",
            validate=True,
        )
    except KeyError as exc:
        error = str(exc)
    add_calls = [x for x in calls if x["path_url"] == constants.ADD_ORDER_PATH_URL]
    if case in {
        "kraken_limit_request_drops_ioc",
        "kraken_validate_request_flag_dropped",
    }:
        body = add_calls[0]["data"]
        assert (
            error is None and body["ordertype"] == "limit" and body["price"] == "1.0174"
        )
        assert (
            body["volume"] == "10"
            and "timeinforce" not in body
            and "oflags" not in body
            and "validate" not in body
        )
        return result(
            case,
            blocker=case == "kraken_validate_request_flag_dropped",
            request_body=body,
            supplied_ioc_dropped=True,
            supplied_validation_dropped=True,
            effective_tif="venue_default",
            no_order_assurance_valid=False,
            reason="Connector ignores validate in kwargs; use an explicit isolated transport, never this keyword, for no-order assurance",
        )
    if case == "cloudflare_open_order_shape_mismatch":
        assert error == "'txid'" and len(add_calls) == 1 and len(calls) == 2
        return result(
            case,
            True,
            error="KeyError: txid",
            add_order_calls=1,
            observed_accepted_open_order=True,
            reason="OpenOrders-shaped ambiguity recovery is consumed as AddOrder txid response",
        )
    assert case == "cloudflare_missing_open_order_retries_add"
    assert (
        error is None
        and len(add_calls) == 2
        and add_calls[0]["data"] == add_calls[1]["data"]
    )
    return result(
        case,
        True,
        add_order_calls=2,
        same_userref=True,
        reason="No open order causes another AddOrder without closed-order/trade lookup; accepted fully filled order can be absent",
    )


async def probe_case(case):
    if case not in CASES:
        raise AssertionError(f"Unknown probe: {case}")
    _, sources = verify_source_integrity()
    with io_trap():
        env = primary_environment(sources)
        if case.startswith("kraken_") or case.startswith("cloudflare_"):
            return await transport_probe(case, env, sources)
        return await lifecycle_probe(case, env)


async def run_probes():
    findings = [await probe_case(case) for case in CASES]
    manifest, _ = verify_source_integrity()
    return {
        "result": "SOURCE_PROBES_COMPLETED_WITH_DEPLOYMENT_BLOCKERS",
        "source_scope": SOURCE_SCOPE,
        "core_revision": CORE_PIN,
        "installed_backend_verified": False,
        "live_approval_eligible": False,
        "live_orders_created": 0,
        "external_io_allowed": False,
        "probe_count": len(findings),
        "deployment_blocker_count": sum(
            x["outcome"] == "deployment_blocker" for x in findings
        ),
        "source_manifest": manifest,
        "observations": findings,
        "limitations": [
            "AST source logic with inert framework startup, event construction, clock and transport fixtures; no full installed core/API runtime.",
            "Late fills are primary ClientOrderTracker.process_trade_update and InFlightOrder.update_with_trade_update, not arbitrary amount mutations.",
            "Potential total quantities assume replacement orders subsequently fill; venue balance checks may reject an overcommit, but safety cannot depend on unrelated inventory absence.",
            "No deployment, live venue request, fee schedule, account credentials or Bazzite host was exercised.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional local JSON evidence path")
    args = parser.parse_args()
    receipt = asyncio.run(run_probes())
    text = json.dumps(receipt, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
