"""Execute corrected Kraken method bodies with inert transport and a real journal.

These are source contract tests. They do not load a compiled Hummingbot build,
connect to Kraken, or establish installed/live readiness.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import re
import sys
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCHES = ROOT / "deploy" / "hummingbot_core" / "patches"
CONNECTOR = PATCHES / "kraken_exchange.py"
GUARD = PATCHES / "spectra_attempt_guard.py"
PROBE_SPEC = importlib.util.spec_from_file_location(
    "spectra_connector_tracker_fixture", ROOT / "scripts" / "probe_limit_lifecycle.py"
)
PROBE = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(PROBE)


def corrected_tracker_environment():
    _, sources = PROBE.verify_source_integrity()
    env = PROBE.primary_environment(sources)
    # The test's order enums share exact names with the compiled primary classes.
    env["TradeType"], env["OrderType"] = TradeType, OrderType
    env["safe_ensure_future"] = asyncio.create_task
    PROBE.compile_primary_classes(
        (PATCHES / "client_order_tracker.py").read_text(),
        {"ClientOrderTracker"},
        env,
        "<corrected-client-order-tracker>",
    )
    return env


class TradeType(Enum):
    BUY = 1
    SELL = 2


class OrderType(Enum):
    MARKET = 1
    LIMIT = 2
    LIMIT_MAKER = 3


class RESTMethod(Enum):
    GET = "GET"
    POST = "POST"


CONSTANTS = SimpleNamespace(
    ADD_ORDER_PATH_URL="/private/AddOrder",
    OPEN_ORDERS_PATH_URL="/private/OpenOrders",
    QUERY_ORDERS_PATH_URL="/private/QueryOrders",
)


def load_guard():
    name = "spectra_kraken_guard_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(name, GUARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def corrected_class(claim, tracker_env=None):
    if tracker_env is None:
        tracker_env = corrected_tracker_environment()
    tree = ast.parse(CONNECTOR.read_text())
    marker = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SPECTRA_EXECUTION_CONTRACT"
            for target in node.targets
        )
    )
    klass = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KrakenExchange"
    )
    selected = {
        "_place_order",
        "_api_request_with_retry",
        "_api_post",
        "is_cloudflare_exception",
        "_is_request_exception_related_to_time_synchronizer",
        "get_open_orders_with_userref",
        "_spectra_require_trade_signature",
        "_create_trade_update_with_order_fill_data",
        "_process_trade_message",
        "_initialize_trading_pair_symbols_from_exchange_info",
        "_spectra_require_order_signature",
        "_create_order_update_with_order_status_data",
        "_process_order_message",
        "_request_order_status",
        "buy",
        "sell",
        "_spectra_validate_order_options",
    }
    klass.bases = []
    klass.body = [
        node
        for node in klass.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id in {"SPECTRA_EXECUTION_CONTRACT", "REQUEST_ATTEMPTS"}
                for target in node.targets
            )
        )
        or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in selected
        )
    ]
    sleeps = []

    async def inert_sleep(seconds):
        sleeps.append(seconds)

    env = {
        "Decimal": Decimal,
        "TradeType": TradeType,
        "OrderType": OrderType,
        "PositionAction": tracker_env["PositionAction"],
        "RESTMethod": RESTMethod,
        "CONSTANTS": SimpleNamespace(
            **vars(CONSTANTS),
            ORDER_STATE={
                "pending": tracker_env["OrderState"].OPEN,
                "open": tracker_env["OrderState"].OPEN,
                "closed": tracker_env["OrderState"].FILLED,
                "canceled": tracker_env["OrderState"].CANCELED,
                "expired": tracker_env["OrderState"].FAILED,
            },
        ),
        "s_decimal_NaN": Decimal("NaN"),
        "safe_ensure_future": asyncio.create_task,
        "OrderUpdate": tracker_env["OrderUpdate"],
        "re": re,
        "claim_order": claim,
        "asyncio": SimpleNamespace(sleep=inert_sleep),
        "TradeUpdate": tracker_env["TradeUpdate"],
        # Framework fee/conversion seams are explicit inert fixtures. The
        # corrected alias-registry and signature methods themselves execute.
        "TradeFeeBase": SimpleNamespace(
            new_spot_fee=lambda **kwargs: PROBE.ZeroFeeFixture()
        ),
        "TokenAmount": lambda **kwargs: SimpleNamespace(**kwargs),
        "bidict": dict,
        "web_utils": SimpleNamespace(
            is_exchange_information_valid=lambda row: not row.get(
                "altname", ""
            ).endswith(".d")
        ),
        "convert_from_exchange_trading_pair": {
            "ALT/USD": "ALT-USD",
            "OTHER/USD": "OTHER-USD",
            "ALT/EUR": "ALT-EUR",
            "XBT/USD": "BTC-USD",
            "XBT/EUR": "BTC-EUR",
        }.__getitem__,
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            marker,
            klass,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(CONNECTOR), "exec"), env)
    return env["KrakenExchange"], sleeps


@pytest.fixture
def harness(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    guard = load_guard()
    journal_path = str(tmp_path / "attempts.sqlite")
    journal_id = guard.initialize_journal(journal_path)
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL", journal_path)
    monkeypatch.setenv("SPECTRA_ATTEMPT_JOURNAL_ID", journal_id)
    claims = []

    def claim(**kwargs):
        reservation = guard.claim_order(**kwargs)
        claims.append((kwargs, reservation))
        return reservation

    env = corrected_tracker_environment()
    klass, sleeps = corrected_class(claim, env)
    connector = klass()
    connector.current_timestamp = 123
    connector.REQUEST_ATTEMPTS = 5
    connector.logger = lambda: logging.getLogger("offline-corrected-kraken")
    connector._order_tracker = env["ClientOrderTracker"](connector)
    connector.trigger_event = lambda *args: None
    connector.trade_fee_schema = lambda: None
    connector._set_trading_pair_symbol_map = lambda mapping: setattr(
        connector, "_trading_pair_symbol_map", mapping
    )
    connector._initialize_trading_pair_symbols_from_exchange_info(
        {
            "XALTZUSD": {"altname": "ALTUSD", "wsname": "ALT/USD"},
            "XOTHERZUSD": {"altname": "OTHERUSD", "wsname": "OTHER/USD"},
        }
    )
    calls = []

    async def symbol(trading_pair):
        assert trading_pair == "ALT-USD"
        return "ALTUSD"

    async def transport(**kwargs):
        calls.append(kwargs)
        return {"error": [], "result": {"txid": ["FIXTURE-ACCEPTED-ORDER"]}}

    connector.exchange_symbol_associated_to_pair = symbol
    connector._api_request = transport
    return SimpleNamespace(
        guard=guard,
        connector=connector,
        calls=calls,
        claims=claims,
        sleeps=sleeps,
        claim=claim,
        env=env,
    )


def start_registered_order(
    h, order_type=OrderType.LIMIT, side=TradeType.BUY, order_id="123"
):
    existing = h.connector._order_tracker.fetch_order(client_order_id=order_id)
    if existing is not None:
        return existing
    order = h.env["InFlightOrder"](
        client_order_id=order_id,
        trading_pair="ALT-USD",
        order_type=order_type,
        trade_type=side,
        amount=Decimal("10"),
        price=Decimal("NaN") if order_type is OrderType.MARKET else Decimal("1.0174"),
        creation_timestamp=1,
        initial_state=h.env["OrderState"].OPEN,
    )
    h.connector._order_tracker.start_tracking_order(order)
    return order


async def place(h, order_type=OrderType.LIMIT, side=TradeType.BUY, order_id="123"):
    start_registered_order(h, order_type, side, order_id)
    return await h.connector._place_order(
        order_id,
        "ALT-USD",
        Decimal("10"),
        side,
        order_type,
        Decimal("NaN") if order_type is OrderType.MARKET else Decimal("1.0174"),
    )


def body(**overrides):
    result = {
        "pair": "ALTUSD",
        "type": "buy",
        "ordertype": "limit",
        "volume": "10",
        "userref": "123",
        "price": "1.0174",
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("order_type", list(OrderType))
@pytest.mark.parametrize("side", list(TradeType))
def test_each_order_kind_claims_before_single_transport(harness, order_type, side):
    h = harness

    async def transport(**kwargs):
        assert len(h.claims) == 1  # Durable claim already exists before I/O.
        with pytest.raises(h.guard.AttemptAlreadyClaimed):
            h.guard.claim_order(**h.claims[0][0])
        h.calls.append(kwargs)
        return {"error": [], "result": {"txid": ["FIXTURE-ACCEPTED-ORDER"]}}

    h.connector._api_request = transport
    assert asyncio.run(place(h, order_type, side)) == ("FIXTURE-ACCEPTED-ORDER", 123)
    assert len(h.calls) == len(h.claims) == 1
    scope, reservation = h.claims[0]
    assert scope["trading_pair"] == "ALT-USD"
    assert scope["trade_type"] is side
    assert scope["amount"] == Decimal("10")
    assert scope["price"] == (
        None if order_type is OrderType.MARKET else Decimal("1.0174")
    )
    request = h.calls[0]["data"]
    assert request["pair"] == "ALTUSD"
    assert request["type"] == side.name.lower()
    if order_type is OrderType.MARKET:
        assert "price" not in request
    else:
        assert request["price"] == "1.0174"
    assert request.get("oflags") == (
        "post" if order_type is OrderType.LIMIT_MAKER else None
    )
    evidence = reservation.describe()
    assert evidence["exchange_order_ids"] == ["FIXTURE-ACCEPTED-ORDER"]
    assert evidence["submission_claimed"] is True
    assert evidence["venue_finality_established"] is False
    assert not h.sleeps


@pytest.mark.parametrize("order_type", list(OrderType))
@pytest.mark.parametrize(
    "error",
    [
        IOError("HTTP status is 502."),
        TimeoutError("unknown acceptance"),
        OSError("lost response"),
    ],
)
def test_ambiguous_transport_never_queries_open_orders_or_retries(
    harness, order_type, error
):
    h = harness

    async def transport(**kwargs):
        h.calls.append(kwargs)
        assert kwargs["path_url"] == CONSTANTS.ADD_ORDER_PATH_URL
        raise error

    h.connector._api_request = transport
    with pytest.raises(type(error)):
        asyncio.run(place(h, order_type))
    assert len(h.calls) == len(h.claims) == 1
    assert h.claims[0][1].describe()["uncertain"] is True
    assert not h.sleeps
    with pytest.raises(h.guard.AttemptAlreadyClaimed):
        asyncio.run(place(h, order_type))
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"result": {"txid": ["ORDER"]}},
        {"error": "", "result": {"txid": ["ORDER"]}},
        {"error": ["EAPI:Invalid nonce"], "result": {"txid": ["ORDER"]}},
        {"error": [], "result": {"open": {"ORDER": {"userref": 123}}}},
        {"error": [], "result": {"txid": "ORDER"}},
        {"error": [], "result": {"txid": []}},
        {"error": [], "result": {"txid": ["FIRST", "SECOND"]}},
        {"error": [], "result": {"txid": [None]}},
        {"error": [], "result": {"txid": [True]}},
        {"error": [], "result": {"txid": [""]}},
        {"error": [], "result": {"txid": [" "]}},
        {"error": [], "result": {"txid": ["ORDER\n"]}},
        {"error": [], "result": {"txid": ["-"]}},
        {"error": [], "result": {"txid": ["A" * 129]}},
        {"error": [], "result": "ORDER"},
    ],
)
def test_only_actual_success_envelope_with_one_txid_can_return(harness, response):
    h = harness

    async def transport(**kwargs):
        h.calls.append(kwargs)
        return response

    h.connector._api_request = transport
    with pytest.raises(IOError, match="AddOrder"):
        asyncio.run(place(h))
    assert len(h.calls) == len(h.claims) == 1
    assert h.claims[0][1].describe()["uncertain"] is True
    with pytest.raises(h.guard.AttemptAlreadyClaimed):
        asyncio.run(place(h))
    assert len(h.calls) == 1


def test_cancelled_coroutine_leaves_durable_unknown_attempt(harness):
    h = harness

    async def scenario():
        submitted = asyncio.Event()
        never = asyncio.Future()

        async def transport(**kwargs):
            h.calls.append(kwargs)
            submitted.set()
            await never

        h.connector._api_request = transport
        task = asyncio.create_task(place(h))
        await submitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(h.guard.AttemptAlreadyClaimed):
            await place(h)

    asyncio.run(scenario())
    assert len(h.calls) == len(h.claims) == 1
    assert h.claims[0][1].describe()["uncertain"] is True
    assert not h.sleeps


@pytest.mark.parametrize("accepted", [True, False])
def test_process_restart_cannot_resubmit_client_id(harness, accepted):
    h = harness

    async def first_transport(**kwargs):
        h.calls.append(kwargs)
        if not accepted:
            raise TimeoutError("acceptance unknown")
        return {"error": [], "result": {"txid": ["FIRST-ORDER"]}}

    h.connector._api_request = first_transport
    if accepted:
        assert asyncio.run(place(h))[0] == "FIRST-ORDER"
    else:
        with pytest.raises(TimeoutError):
            asyncio.run(place(h))
    restarted_guard = load_guard()
    restarted_class, _ = corrected_class(restarted_guard.claim_order)
    restarted = restarted_class()
    restarted.current_timestamp = 456
    restarted.REQUEST_ATTEMPTS = 5
    restarted.exchange_symbol_associated_to_pair = (
        h.connector.exchange_symbol_associated_to_pair
    )
    restarted._order_tracker = h.connector._order_tracker

    async def forbidden_second_transport(**kwargs):
        raise AssertionError(
            "A restarted connector cannot resubmit an existing client ID"
        )

    restarted._api_request = forbidden_second_transport
    old = h.connector
    h.connector = restarted
    with pytest.raises(restarted_guard.AttemptAlreadyClaimed):
        asyncio.run(place(h))
    h.connector = old
    assert len(h.calls) == 1


def test_direct_low_level_add_order_is_also_durably_guarded(harness):
    h = harness

    async def scenario():
        start_registered_order(h)
        kwargs = {
            "path_url": CONSTANTS.ADD_ORDER_PATH_URL,
            "data": body(),
            "is_auth_required": True,
            "_spectra_trading_pair": "ALT-USD",
        }
        assert (await h.connector._api_post(**kwargs))["txid"] == [
            "FIXTURE-ACCEPTED-ORDER"
        ]
        with pytest.raises(h.guard.AttemptAlreadyClaimed):
            await h.connector._api_post(**kwargs)

    asyncio.run(scenario())
    assert len(h.calls) == len(h.claims) == 1
    assert h.claims[0][0]["trading_pair"] == "ALT-USD"


@pytest.mark.parametrize(
    "overrides",
    [
        {"userref": None},
        {"userref": True},
        {"userref": "not-numeric"},
        {"userref": "000123"},
        {"userref": "00"},
        {"userref": "2147483648"},
        {"pair": None},
        {"pair": ""},
        {"type": "short"},
        {"ordertype": "stop-loss"},
        {"volume": "NaN"},
        {"volume": "Infinity"},
        {"volume": "0"},
        {"volume": "-1"},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"price": "0"},
        {"close": {"ordertype": "market"}},
        {"leverage": "5"},
        {"oflags": "fcib"},
    ],
)
def test_invalid_or_unbounded_mutation_fails_before_claim_and_transport(
    harness, overrides
):
    h = harness
    with pytest.raises((ValueError, ArithmeticError)):
        asyncio.run(
            h.connector._api_post(
                path_url=CONSTANTS.ADD_ORDER_PATH_URL,
                data=body(**overrides),
                is_auth_required=True,
            )
        )
    assert not h.calls and not h.claims


@pytest.mark.parametrize(
    "method,authenticated,params",
    [
        (RESTMethod.GET, True, None),
        (RESTMethod.POST, False, None),
        (RESTMethod.POST, True, {"userref": 999}),
    ],
)
def test_add_order_requires_authenticated_post_without_query_overrides(
    harness, method, authenticated, params
):
    h = harness
    with pytest.raises(ValueError):
        asyncio.run(
            h.connector._api_request_with_retry(
                method=method,
                path_url=CONSTANTS.ADD_ORDER_PATH_URL,
                data=body(),
                is_auth_required=authenticated,
                params=params,
            )
        )
    assert not h.calls and not h.claims


def test_journal_missing_blocks_before_transport(harness, monkeypatch):
    h = harness
    monkeypatch.delenv("SPECTRA_ATTEMPT_JOURNAL")
    with pytest.raises(h.guard.AttemptJournalError):
        asyncio.run(place(h))
    assert not h.calls


def test_recording_accepted_id_failure_cannot_authorize_a_second_submission(harness):
    h = harness
    real_claim = h.claim

    def claim_with_record_failure(**kwargs):
        reservation = real_claim(**kwargs)

        class RecordFailure:
            def record_exchange_order(self, txid):
                raise OSError("journal update failure")

            def mark_uncertain(self):
                reservation.mark_uncertain()

        return RecordFailure()

    klass, _ = corrected_class(claim_with_record_failure)
    h.connector._api_request_with_retry = klass._api_request_with_retry.__get__(
        h.connector
    )
    with pytest.raises(OSError, match="journal update"):
        asyncio.run(place(h))
    with pytest.raises(h.guard.AttemptAlreadyClaimed):
        asyncio.run(place(h))
    assert len(h.calls) == 1


def test_read_only_cloudflare_retry_remains_available_without_attempt_claim(harness):
    h = harness

    async def transport(**kwargs):
        h.calls.append(kwargs)
        assert kwargs["path_url"] == CONSTANTS.OPEN_ORDERS_PATH_URL
        if len(h.calls) < 3:
            raise IOError("HTTP status is 502.")
        return {"error": [], "result": {"open": {}}}

    h.connector._api_request = transport
    assert asyncio.run(h.connector.get_open_orders_with_userref(123)) == {"open": {}}
    assert len(h.calls) == 3
    assert len(h.sleeps) == 2
    assert not h.claims


def test_derivative_contract_marker_is_present(harness):
    assert harness.connector.SPECTRA_EXECUTION_CONTRACT == "spectra-single-attempt-v1"


def test_pinned_kraken_override_disables_inherited_time_sync_retries(harness):
    # ExchangePyBase retries only when this connector override returns True.
    assert (
        harness.connector._is_request_exception_related_to_time_synchronizer(
            IOError("HTTP status is 502.")
        )
        is False
    )


class FixtureHTTPResponse:
    def __init__(self, status):
        self.status = status
        self.released = False

    def release(self):
        self.released = True

    async def json(self):
        # Even a redirect carrying a success-shaped JSON body is rejected.
        return {"error": [], "result": {"txid": ["HTTP-RESPONSE-ORDER"]}}


class FixtureRESTResponse:
    def __init__(self, response):
        self.response = response
        self.status = response.status

    async def json(self):
        return await self.response.json()


class RedirectAwareSession:
    """Inert transport models the dangerous aiohttp redirect default."""

    def __init__(self, status):
        self.response = FixtureHTTPResponse(status)
        self.calls = []
        self.followup_submissions = 0

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        if 300 <= self.response.status < 400 and kwargs.get("allow_redirects", True):
            self.followup_submissions += 1
            return FixtureHTTPResponse(200)
        return self.response


def corrected_rest_connection():
    source = PATCHES / "rest_connection.py"
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.Assign, ast.ClassDef))]
    env = {"RESTResponse": FixtureRESTResponse}
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), env)
    return env["RESTConnection"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_actual_rest_connection_refuses_redirects_without_followup_post(status):
    session = RedirectAwareSession(status)
    connection = corrected_rest_connection()(session)
    request = SimpleNamespace(
        method=RESTMethod.POST,
        url="https://offline.invalid/private/AddOrder",
        params=None,
        data="signed-body-fixture",
        headers={"fixture-auth": "inert"},
    )
    with pytest.raises(IOError, match="redirects are disabled"):
        asyncio.run(connection.call(request))
    assert len(session.calls) == 1
    assert session.calls[0]["allow_redirects"] is False
    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["data"] == "signed-body-fixture"
    assert session.followup_submissions == 0
    assert session.response.released is True


@pytest.mark.parametrize(
    "status", [200, 201, 204, 400, 401, 403, 408, 429, 500, 502, 503]
)
def test_actual_rest_connection_nonredirect_response_never_retries(status):
    session = RedirectAwareSession(status)
    connection = corrected_rest_connection()(session)
    request = SimpleNamespace(
        method=RESTMethod.POST,
        url="https://offline.invalid/private/AddOrder",
        params=None,
        data="signed-body-fixture",
        headers={},
    )
    assert asyncio.run(connection.call(request)).status == status
    assert len(session.calls) == 1
    assert session.calls[0]["allow_redirects"] is False
    assert session.followup_submissions == 0


@pytest.mark.parametrize("status", [307, 308])
def test_corrected_connector_and_actual_transport_keep_redirect_attempt_quarantined(
    harness, status
):
    h = harness
    session = RedirectAwareSession(status)
    connection = corrected_rest_connection()(session)

    async def actual_transport_methods(**kwargs):
        h.calls.append(kwargs)
        assert h.claims[0][1].describe()["submission_claimed"] is True
        request = SimpleNamespace(
            method=kwargs["method"],
            url="https://offline.invalid/private/AddOrder",
            params=kwargs["params"],
            data=kwargs["data"],
            headers={},
        )
        response = await connection.call(request)
        return await response.json()

    h.connector._api_request = actual_transport_methods
    with pytest.raises(IOError, match="redirects are disabled"):
        asyncio.run(place(h))
    assert h.claims[0][1].describe()["uncertain"] is True
    with pytest.raises(h.guard.AttemptAlreadyClaimed):
        asyncio.run(place(h))
    assert len(h.calls) == len(session.calls) == 1
    assert session.followup_submissions == 0


def test_rest_connection_derivative_retains_exact_upstream_baseline():
    import hashlib

    source = (PATCHES / "rest_connection.py").read_text()
    # Reverse exactly the three intentional derivative additions. The full
    # residual must equal the fetched core9af primary file, not a retyped stub.
    notice = (
        "# Modified for Spectra Finance on 2026-10-10.\n"
        "# Changes: disable implicit HTTP redirects and reject redirect responses.\n"
        "# Original: Hummingbot core 9af100d6822da7d2d0291a906c730ef172284ee2 (Apache-2.0).\n\n"
    )
    assert source.startswith(notice)
    baseline = (
        source.removeprefix(notice)
        .replace(
            '\n\nSPECTRA_EXECUTION_CONTRACT = "spectra-single-attempt-v1"\n\n\nclass RESTConnection:\n    SPECTRA_EXECUTION_CONTRACT = SPECTRA_EXECUTION_CONTRACT',
            "\n\nclass RESTConnection:",
        )
        .replace("            allow_redirects=False,\n", "")
        .replace(
            '        if 300 <= aiohttp_resp.status < 400:\n            aiohttp_resp.release()\n            raise IOError("Implicit REST redirects are disabled for mutation safety")\n\n',
            "",
        )
        .encode()
    )
    assert len(baseline) == 798
    assert (
        hashlib.sha256(baseline).hexdigest()
        == "2fb82f279f951f2a33ec7e3eb76bc9b188805742680cc24f6ab208b311d97eaa"
    )
    assert (
        hashlib.sha1(
            b"blob " + str(len(baseline)).encode() + b"\0" + baseline
        ).hexdigest()
        == "7c67e0d2de579475c7d113f326bf8582c5a156fa"
    )


def test_missing_corrected_tracker_blocks_after_durable_claim(harness):
    h = harness
    start_registered_order(h)
    h.connector._order_tracker = SimpleNamespace()
    with pytest.raises(IOError, match="corrected order tracker"):
        asyncio.run(
            h.connector._place_order(
                "123",
                "ALT-USD",
                Decimal("10"),
                TradeType.BUY,
                OrderType.LIMIT,
                Decimal("1.0174"),
            )
        )
    assert not h.calls
    assert len(h.claims) == 1
    assert h.claims[0][1].describe()["uncertain"] is True
    with pytest.raises(h.guard.AttemptAlreadyClaimed):
        asyncio.run(
            h.connector._place_order(
                "123",
                "ALT-USD",
                Decimal("10"),
                TradeType.BUY,
                OrderType.LIMIT,
                Decimal("1.0174"),
            )
        )
    assert not h.calls


def test_untracked_direct_add_order_cannot_submit(harness):
    h = harness
    with pytest.raises(ValueError, match="existing tracked order"):
        asyncio.run(
            h.connector._api_post(
                path_url=CONSTANTS.ADD_ORDER_PATH_URL,
                data=body(),
                is_auth_required=True,
                _spectra_trading_pair="ALT-USD",
            )
        )
    assert not h.calls
    assert len(h.claims) == 1
    assert h.claims[0][1].describe()["uncertain"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"volume": "9"},
        {"volume": "10.000000000000000000000000000001"},
        {"price": "1.017400000000000000000000000001"},
        {"type": "sell"},
        {"ordertype": "market"},
        {"oflags": "post"},
        {"pair": "OTHERUSD"},
    ],
)
def test_registered_scope_changes_cannot_reach_transport(harness, overrides):
    h = harness
    start_registered_order(h)
    with pytest.raises(IOError, match="authorization"):
        asyncio.run(
            h.connector._api_post(
                path_url=CONSTANTS.ADD_ORDER_PATH_URL,
                data=body(**overrides),
                is_auth_required=True,
                _spectra_trading_pair="ALT-USD",
            )
        )
    assert not h.calls
    assert h.claims[0][1].describe()["uncertain"] is True


def test_unknown_canonical_pair_on_direct_payload_cannot_bypass_registration(harness):
    h = harness
    start_registered_order(h)
    with pytest.raises(IOError, match="registered order authorization"):
        asyncio.run(
            h.connector._api_post(
                path_url=CONSTANTS.ADD_ORDER_PATH_URL,
                data=body(),
                is_auth_required=True,
            )
        )
    assert not h.calls
    assert h.claims[0][1].describe()["uncertain"] is True


def test_tracker_capacity_failure_cannot_release_claim_or_submit(harness):
    h = harness
    h.connector._order_tracker.MAX_RETAINED_SPECTRA_ORDERS = 0
    with pytest.raises(ValueError, match="capacity"):
        asyncio.run(place(h))
    assert not h.calls
    assert h.claims[0][1].describe()["uncertain"] is True


def test_connector_registration_keeps_actual_late_fill_after_tracker_cache_expiry(
    harness,
):
    h = harness
    tracker = h.connector._order_tracker
    clock = [0]
    tracker._cached_orders = PROBE.TTLCache(
        maxsize=tracker.MAX_CACHE_SIZE,
        ttl=tracker.CACHED_ORDER_TTL,
        timer=lambda: clock[0],
    )
    exchange_order_id, _ = asyncio.run(place(h))
    actual_order = tracker.fetch_order(client_order_id="123")
    actual_order.update_exchange_order_id(exchange_order_id)
    assert tracker._spectra_retained_orders["123"] is actual_order
    tracker.stop_tracking_order("123")
    assert tracker.cached_orders["123"] is actual_order
    clock[0] = 31
    tracker._cached_orders.expire()
    assert "123" not in tracker.cached_orders
    assert "123" not in tracker.active_orders
    assert "123" not in tracker.all_updatable_orders
    assert tracker.fetch_order(client_order_id="123") is actual_order
    assert tracker.all_fillable_orders["123"] is actual_order
    assert (
        tracker.all_fillable_orders_by_exchange_order_id[exchange_order_id]
        is actual_order
    )
    trade = h.env["TradeUpdate"](
        trade_id="AFTER-TTL-TRADE",
        client_order_id="123",
        exchange_order_id=exchange_order_id,
        trading_pair="ALT-USD",
        fill_timestamp=31,
        fill_price=Decimal("1.0174"),
        fill_base_amount=Decimal("3"),
        fill_quote_amount=Decimal("3.0522"),
        fee=PROBE.ZeroFeeFixture(),
    )
    tracker.process_trade_update(trade)
    tracker.process_trade_update(trade)  # Same venue trade cannot count twice.
    assert actual_order.executed_amount_base == Decimal("3")
    assert actual_order.executed_amount_quote == Decimal("3.0522")
    assert list(actual_order.order_fills) == ["AFTER-TTL-TRADE"]
    with pytest.raises(h.guard.AttemptAlreadyClaimed):
        asyncio.run(place(h))
    assert len(h.calls) == 1


@pytest.mark.parametrize("order_id", ["0", "2147483647"])
def test_canonical_numeric_client_id_boundaries_are_guarded(harness, order_id):
    h = harness
    assert asyncio.run(place(h, order_id=order_id))[0] == "FIXTURE-ACCEPTED-ORDER"
    assert h.calls[0]["data"]["userref"] == order_id
    assert h.claims[0][0]["client_order_id"] == order_id
    with pytest.raises(h.guard.AttemptAlreadyClaimed):
        asyncio.run(place(h, order_id=order_id))
    assert len(h.calls) == 1


def accepted_registered_order(h):
    exchange_order_id, _ = asyncio.run(place(h))
    order = h.connector._order_tracker.fetch_order(client_order_id="123")
    order.update_exchange_order_id(exchange_order_id)
    return order


def raw_venue_trade(order, **overrides):
    # Shape from the pinned core9af Kraken ownTrades/QueryTrades fixtures:
    # pair, ordertxid, type, vol, price, time, fee; QueryTrades omits userref.
    data = {
        "trade_id": "RAW-VENUE-TRADE",
        "ordertxid": order.exchange_order_id,
        "pair": "ALT/USD",
        "type": "buy",
        "vol": "3",
        "price": "1.0174",
        "time": 31,
        "fee": "0",
        "userref": order.client_order_id,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("alias", ["XALTZUSD", "ALTUSD", "ALT/USD"])
@pytest.mark.parametrize("with_userref", [True, False])
def test_verified_venue_pair_aliases_accept_matching_actual_trade(
    harness, alias, with_userref
):
    h = harness
    order = accepted_registered_order(h)
    raw = raw_venue_trade(order, pair=alias)
    if not with_userref:
        del raw["userref"]  # QueryTrades source fixtures do not supply userref.
    update = h.connector._create_trade_update_with_order_fill_data(raw, order)
    assert update.trading_pair == order.trading_pair
    assert update.exchange_order_id == order.exchange_order_id
    h.connector._order_tracker.process_trade_update(update)
    assert order.executed_amount_base == Decimal("3")
    assert not h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"pair": "OTHER/USD"}, "venue_pair_mismatch"),
        ({"pair": "OTHERUSD"}, "venue_pair_mismatch"),
        ({"pair": "ALT-USD"}, "venue_signature_unavailable"),
        ({"pair": "alt/usd"}, "venue_signature_unavailable"),
        ({"pair": None}, "venue_signature_unavailable"),
        ({"pair": True}, "venue_signature_unavailable"),
        ({"ordertxid": "FOREIGN-ORDER"}, "venue_order_id_mismatch"),
        ({"ordertxid": None}, "venue_signature_unavailable"),
        ({"ordertxid": 123}, "venue_signature_unavailable"),
        ({"userref": "FOREIGN"}, "venue_userref_mismatch"),
        ({"userref": "000123"}, "venue_userref_mismatch"),
        ({"userref": True}, "venue_userref_mismatch"),
        ({"type": "sell"}, "venue_side_mismatch"),
        ({"type": None}, "venue_signature_unavailable"),
        ({"trade_id": None}, "trade_id_unavailable"),
        ({"trade_id": True}, "trade_id_unavailable"),
        ({"trade_id": " "}, "trade_id_unavailable"),
    ],
)
def test_raw_fill_signature_cannot_be_reinterpreted_as_expected_order(
    harness, overrides, reason
):
    h = harness
    order = accepted_registered_order(h)
    with pytest.raises(IOError, match=reason):
        h.connector._create_trade_update_with_order_fill_data(
            raw_venue_trade(order, **overrides), order
        )
    assert order.executed_amount_base == Decimal("0")
    assert not order.order_fills
    assert reason in h.connector._order_tracker.spectra_order_ambiguity("123")


def test_unknown_exchange_order_identity_cannot_bind_from_nonunique_userref(harness):
    h = harness
    asyncio.run(place(h))
    order = h.connector._order_tracker.fetch_order(client_order_id="123")
    assert order.exchange_order_id is None
    with pytest.raises(IOError, match="venue_signature_unavailable"):
        h.connector._create_trade_update_with_order_fill_data(
            raw_venue_trade(order, ordertxid="OTHER-ACCEPTED-ORDER"), order
        )
    assert order.exchange_order_id is None
    assert order.executed_amount_base == Decimal("0")
    assert h.connector._order_tracker.spectra_order_ambiguity("123")


def test_missing_verified_alias_catalog_quarantines_instead_of_guessing_pair(harness):
    h = harness
    order = accepted_registered_order(h)
    del h.connector._spectra_venue_pair_aliases
    with pytest.raises(IOError, match="venue_signature_unavailable"):
        h.connector._create_trade_update_with_order_fill_data(
            raw_venue_trade(order), order
        )
    assert order.executed_amount_base == Decimal("0")
    assert h.connector._order_tracker.spectra_order_ambiguity("123")


def test_colliding_asset_pair_alias_is_not_an_authoritative_match(harness):
    h = harness
    order = accepted_registered_order(h)
    h.connector._initialize_trading_pair_symbols_from_exchange_info(
        {
            "XALTZUSD": {"altname": "COLLISION", "wsname": "ALT/USD"},
            "XOTHERZUSD": {"altname": "COLLISION", "wsname": "OTHER/USD"},
        }
    )
    assert h.connector._spectra_venue_pair_aliases["COLLISION"] is None
    with pytest.raises(IOError, match="venue_signature_unavailable"):
        h.connector._create_trade_update_with_order_fill_data(
            raw_venue_trade(order, pair="COLLISION"), order
        )
    assert order.executed_amount_base == Decimal("0")
    assert h.connector._order_tracker.spectra_order_ambiguity("123")


def test_websocket_trade_dispatch_uses_validated_raw_signature_and_deduplicates(
    harness,
):
    h = harness
    order = accepted_registered_order(h)
    update = {"RAW-WS-TRADE": raw_venue_trade(order)}
    h.connector._process_trade_message([update])
    h.connector._process_trade_message([update])
    assert order.executed_amount_base == Decimal("3")
    assert list(order.order_fills) == ["RAW-WS-TRADE"]
    assert not h.connector._order_tracker.spectra_order_ambiguity("123")


def test_websocket_foreign_raw_pair_never_reaches_tracker_as_copied_expected_pair(
    harness,
):
    h = harness
    order = accepted_registered_order(h)
    with pytest.raises(IOError, match="venue_pair_mismatch"):
        h.connector._process_trade_message(
            [{"FOREIGN-WS-TRADE": raw_venue_trade(order, pair="OTHER/USD")}]
        )
    assert order.executed_amount_base == Decimal("0")
    assert h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("vol", value, "trade_geometry_invalid")
        for value in ("-3", "0", "NaN", "Infinity", "bad", None, True)
    ]
    + [
        ("price", value, "trade_geometry_invalid")
        for value in ("-1", "0", "NaN", "Infinity", "bad", None, True)
    ]
    + [
        ("fee", value, "trade_fee_unavailable")
        for value in ("-1", "NaN", "Infinity", "bad", None, True)
    ],
)
def test_raw_financial_fields_quarantine_before_fee_factory_or_native_mutation(
    harness, field, value, reason
):
    h = harness
    order = accepted_registered_order(h)
    fee_factory = (
        h.connector._create_trade_update_with_order_fill_data.__func__.__globals__[
            "TradeFeeBase"
        ]
    )
    fee_calls = []
    fee_factory.new_spot_fee = lambda **kwargs: fee_calls.append(kwargs)
    with pytest.raises(IOError, match=reason):
        h.connector._process_trade_message(
            [{"INVALID-RAW-TRADE": raw_venue_trade(order, **{field: value})}]
        )
    assert not fee_calls
    assert not order.order_fills
    assert order.executed_amount_base == Decimal("0")
    assert reason in h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize("field", ["vol", "price", "fee"])
def test_missing_raw_financial_field_requires_reconciliation(harness, field):
    h = harness
    order = accepted_registered_order(h)
    raw = raw_venue_trade(order)
    del raw[field]
    with pytest.raises(IOError):
        h.connector._create_trade_update_with_order_fill_data(raw, order)
    assert not order.order_fills
    assert h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize(
    "raw,outer_id,reason",
    [
        (
            {"status": "canceled", "userref": 123},
            "FOREIGN-ORDER",
            "venue_order_id_mismatch",
        ),
        ({"status": "canceled"}, None, "venue_signature_unavailable"),
        (
            {"status": "canceled", "userref": "000123"},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_userref_mismatch",
        ),
        (
            {"status": "canceled", "userref": True},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_userref_mismatch",
        ),
        (
            {"status": "canceled", "descr": {"pair": "OTHER/USD"}},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_pair_mismatch",
        ),
        (
            {"status": "canceled", "pair": "OTHERUSD"},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_pair_mismatch",
        ),
        (
            {"status": "canceled", "descr": {"pair": "ALT-USD"}},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_signature_unavailable",
        ),
        (
            {"status": "canceled", "descr": {"type": "sell"}},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_side_mismatch",
        ),
        (
            {"status": "canceled", "type": "sell"},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_side_mismatch",
        ),
        (
            {"status": "canceled", "descr": None},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_signature_unavailable",
        ),
        (
            {"status": "unknown"},
            "FIXTURE-ACCEPTED-ORDER",
            "venue_signature_unavailable",
        ),
        ({"status": []}, "FIXTURE-ACCEPTED-ORDER", "venue_signature_unavailable"),
        (None, "FIXTURE-ACCEPTED-ORDER", "order_signature_ambiguous"),
    ],
)
def test_raw_status_identity_conflicts_cannot_change_native_state(
    harness, raw, outer_id, reason
):
    h = harness
    order = accepted_registered_order(h)
    with pytest.raises(IOError, match=reason):
        h.connector._create_order_update_with_order_status_data(
            raw, order, exchange_order_id=outer_id
        )
    assert order.current_state is h.env["OrderState"].OPEN
    assert reason in h.connector._order_tracker.spectra_order_ambiguity("123")


def test_status_unknown_venue_identity_never_binds_from_userref(harness):
    h = harness
    asyncio.run(place(h))
    order = h.connector._order_tracker.fetch_order(client_order_id="123")
    with pytest.raises(IOError, match="venue_signature_unavailable"):
        h.connector._process_order_message(
            [[{"OTHER-ORDER": {"status": "canceled", "userref": 123}}]]
        )
    assert order.exchange_order_id is None
    assert order.current_state is h.env["OrderState"].OPEN


def test_websocket_sparse_known_status_uses_actual_outer_id_without_userref(harness):
    h = harness
    order = accepted_registered_order(h)

    async def scenario():
        tasks = []
        h.env["safe_ensure_future"] = (
            lambda coroutine: tasks.append(asyncio.create_task(coroutine)) or tasks[-1]
        )
        h.connector._process_order_message(
            [[{order.exchange_order_id: {"status": "canceled"}}]]
        )
        assert len(tasks) == 1
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert order.current_state is h.env["OrderState"].CANCELED
    assert not h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize(
    "outer_id,metadata,reason",
    [
        ("FOREIGN-ORDER", {"userref": 123}, "venue_order_id_mismatch"),
        ("FIXTURE-ACCEPTED-ORDER", {"userref": 321}, "venue_userref_mismatch"),
        (
            "FIXTURE-ACCEPTED-ORDER",
            {"userref": 123, "descr": {"pair": "OTHER/USD"}},
            "venue_pair_mismatch",
        ),
    ],
)
def test_websocket_status_rejects_conflicts_before_async_tracker_mutation(
    harness, outer_id, metadata, reason
):
    h = harness
    order = accepted_registered_order(h)
    with pytest.raises(IOError, match=reason):
        h.connector._process_order_message(
            [[{outer_id: dict(metadata, status="canceled")}]]
        )
    assert order.current_state is h.env["OrderState"].OPEN
    assert reason in h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize("pair", ["XALTZUSD", "ALTUSD", "ALT/USD"])
def test_rest_status_creator_validates_returned_metadata_and_preserves_real_identity(
    harness, pair
):
    h = harness
    order = accepted_registered_order(h)

    async def read_status(**kwargs):
        assert kwargs["path_url"] == CONSTANTS.QUERY_ORDERS_PATH_URL
        assert kwargs["data"] == {"txid": order.exchange_order_id}
        return {
            "error": [],
            "result": {
                order.exchange_order_id: {
                    "status": "open",
                    "userref": 123,
                    "descr": {"pair": pair, "type": "buy"},
                    "vol_exec": "3",
                }
            },
        }

    h.connector._api_request = read_status
    update = asyncio.run(h.connector._request_order_status(order))
    assert update.exchange_order_id == order.exchange_order_id
    assert update.trading_pair == order.trading_pair
    assert update.misc_updates["spectra_executed_amount_base"] == Decimal("3")
    assert order.executed_amount_base == Decimal("0")
    assert not h.connector._order_tracker.spectra_order_ambiguity("123")


def test_rest_status_missing_known_result_key_cannot_use_foreign_returned_order(
    harness,
):
    h = harness
    order = accepted_registered_order(h)

    async def foreign_result(**kwargs):
        return {
            "error": [],
            "result": {"OTHER-ORDER": {"status": "canceled", "userref": 123}},
        }

    h.connector._api_request = foreign_result
    with pytest.raises(IOError, match="order_signature_ambiguous"):
        asyncio.run(h.connector._request_order_status(order))
    assert order.current_state is h.env["OrderState"].OPEN


@pytest.mark.parametrize("status", ["open", "closed", "canceled"])
@pytest.mark.parametrize("volume", ["0", "3", "13"])
def test_cumulative_status_volume_is_evidence_without_clamp_or_synthetic_fill(
    harness, status, volume
):
    h = harness
    order = accepted_registered_order(h)
    update = h.connector._create_order_update_with_order_status_data(
        {"status": status, "vol_exec": volume},
        order,
        exchange_order_id=order.exchange_order_id,
    )
    assert update.misc_updates == {"spectra_executed_amount_base": Decimal(volume)}
    assert order.executed_amount_base == Decimal("0")
    assert not h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize("volume", ["-1", "NaN", "Infinity", "bad", None, True])
def test_invalid_cumulative_status_volume_quarantines_before_native_mutation(
    harness, volume
):
    h = harness
    order = accepted_registered_order(h)
    with pytest.raises(IOError, match="invalid cumulative"):
        h.connector._create_order_update_with_order_status_data(
            {"status": "canceled", "vol_exec": volume},
            order,
            exchange_order_id=order.exchange_order_id,
        )
    assert order.current_state is h.env["OrderState"].OPEN
    assert h.connector._order_tracker.spectra_order_ambiguity("123")


@pytest.mark.parametrize(
    "options",
    [
        {"validate": True},
        {"validate": False},
        {"time_in_force": "IOC"},
        {"leverage": "2"},
        {"oflags": "post"},
    ],
)
def test_unsupported_place_options_reject_before_claim_then_normal_attempt_succeeds(
    harness, options
):
    h = harness
    start_registered_order(h)
    with pytest.raises(ValueError, match="additional order options"):
        asyncio.run(
            h.connector._place_order(
                "123",
                "ALT-USD",
                Decimal("10"),
                TradeType.BUY,
                OrderType.LIMIT,
                Decimal("1.0174"),
                **options,
            )
        )
    assert not h.claims and not h.calls
    assert asyncio.run(place(h))[0] == "FIXTURE-ACCEPTED-ORDER"
    assert len(h.claims) == len(h.calls) == 1


@pytest.mark.parametrize("method", ["buy", "sell"])
@pytest.mark.parametrize("action", ["NIL", "OPEN"])
def test_source_verified_strategy_metadata_reaches_guarded_native_placement(
    harness, method, action
):
    h = harness
    connector_env = h.connector.buy.__func__.__globals__
    scheduled = []
    native_kwargs = []
    connector_env["CONSTANTS"].MAX_ID_BIT_COUNT = 31
    connector_env["get_new_numeric_client_order_id"] = lambda **kwargs: 123
    h.connector._client_order_id_nonce_provider = object()

    async def inert_base_create(**kwargs):
        # Framework creation seam: actual tracker registration and corrected
        # native placement execute; compiled Strategy hops are source-audited.
        native_kwargs.append(kwargs)
        start_registered_order(
            h, kwargs["order_type"], kwargs["trade_type"], kwargs["order_id"]
        )
        await h.connector._place_order(**kwargs)

    h.connector._create_order = inert_base_create

    async def scenario():
        connector_env["safe_ensure_future"] = (
            lambda coroutine: scheduled.append(asyncio.create_task(coroutine))
            or scheduled[-1]
        )
        order_id = getattr(h.connector, method)(
            "ALT-USD",
            Decimal("10"),
            OrderType.LIMIT,
            Decimal("1.0174"),
            position_action=h.env["PositionAction"][action],
            expiration_ts=float("nan"),
        )
        assert order_id == "123" and len(scheduled) == 1
        await asyncio.gather(*scheduled)

    asyncio.run(scenario())
    assert len(h.claims) == len(h.calls) == 1
    assert native_kwargs[0]["trade_type"] is getattr(TradeType, method.upper())
    assert "position_action" not in native_kwargs[0]
    assert "expiration_ts" not in native_kwargs[0]
    assert set(h.calls[0]["data"]) == set(body())


@pytest.mark.parametrize("method", ["buy", "sell"])
@pytest.mark.parametrize(
    "options",
    [
        {"validate": True},
        {"position_action": "OPEN"},
        {"position_action": "CLOSE"},
        {"expiration_ts": 123},
        {"expiration_ts": float("inf")},
        {"expiration_ts": None},
    ],
)
def test_native_buy_sell_reject_mutating_options_before_nonce_or_scheduling(
    harness, method, options
):
    h = harness
    env = h.connector.buy.__func__.__globals__
    env["get_new_numeric_client_order_id"] = lambda **kwargs: pytest.fail(
        "nonce must not be requested"
    )
    env["safe_ensure_future"] = lambda coroutine: pytest.fail(
        "order must not be scheduled"
    )
    with pytest.raises(ValueError):
        getattr(h.connector, method)(
            "ALT-USD",
            Decimal("10"),
            OrderType.LIMIT,
            Decimal("1.0174"),
            **options,
        )
    assert not h.claims and not h.calls


@pytest.mark.parametrize("method", ["buy", "sell"])
def test_real_close_position_metadata_is_rejected_for_spot(harness, method):
    h = harness
    with pytest.raises(ValueError, match="NIL or OPEN"):
        getattr(h.connector, method)(
            "ALT-USD",
            Decimal("10"),
            OrderType.LIMIT,
            Decimal("1.0174"),
            position_action=h.env["PositionAction"].CLOSE,
            expiration_ts=float("nan"),
        )
    assert not h.claims and not h.calls


@pytest.mark.parametrize("expire_cache", [False, True])
def test_late_sparse_status_volume_retains_cached_cancel_evidence_until_actual_fills(
    harness, expire_cache
):
    h = harness
    tracker = h.connector._order_tracker
    order = accepted_registered_order(h)
    clock = [0]
    tracker._cached_orders = PROBE.TTLCache(
        maxsize=tracker.MAX_CACHE_SIZE,
        ttl=tracker.CACHED_ORDER_TTL,
        timer=lambda: clock[0],
    )

    async def deliver(raw):
        tasks = []
        h.env["safe_ensure_future"] = (
            lambda coroutine: tasks.append(asyncio.create_task(coroutine)) or tasks[-1]
        )
        h.connector._process_order_message([[{order.exchange_order_id: raw}]])
        assert len(tasks) == 1
        await asyncio.gather(*tasks)

    asyncio.run(deliver({"status": "canceled", "vol_exec": "0"}))
    assert order.current_state is h.env["OrderState"].CANCELED
    assert tracker.cached_orders["123"] is order
    assert not tracker.spectra_order_fill_lag("123")
    if expire_cache:
        clock[0] = 31
        tracker._cached_orders.expire()
        assert "123" not in tracker.cached_orders
    # A late delta with no status/userref is identified by its known venue ID.
    asyncio.run(deliver({"vol_exec": "3"}))
    asyncio.run(deliver({"vol_exec": "0"}))  # Reordering cannot lower evidence.
    assert tracker.spectra_order_fill_watermark("123") == Decimal("3")
    assert tracker.spectra_order_fill_lag("123")
    assert order.current_state is h.env["OrderState"].CANCELED
    assert order.executed_amount_base == Decimal("0")
    assert "123" not in tracker.active_orders
    assert "123" not in tracker.all_updatable_orders
    assert tracker.all_fillable_orders["123"] is order
    assert not tracker.spectra_order_ambiguity("123")
    h.connector._process_trade_message(
        [{"MATCHING-LATE-TRADE": raw_venue_trade(order)}]
    )
    assert order.executed_amount_base == Decimal("3")
    assert not tracker.spectra_order_fill_lag("123")
    assert len(h.claims) == len(h.calls) == 1
    assert "123" not in tracker.active_orders


def test_active_sparse_status_volume_preserves_open_state_and_unclamped_watermark(
    harness,
):
    h = harness
    tracker = h.connector._order_tracker
    order = accepted_registered_order(h)
    update = h.connector._create_order_update_with_order_status_data(
        {"vol_exec": "13"},
        order,
        exchange_order_id=order.exchange_order_id,
    )
    assert update.new_state is h.env["OrderState"].OPEN

    async def scenario():
        await tracker.process_order_update(update)

    asyncio.run(scenario())
    assert tracker.spectra_order_fill_watermark("123") == Decimal("13")
    assert tracker.spectra_order_fill_lag("123")
    assert order.executed_amount_base == Decimal("0")
    assert order.current_state is h.env["OrderState"].OPEN
    assert not tracker.spectra_order_ambiguity("123")
    assert len(h.calls) == 1


def test_raw_status_duplicate_known_venue_identity_quarantines_all_retained_refs(
    harness,
):
    h = harness
    first = accepted_registered_order(h)
    exchange_order_id, _ = asyncio.run(place(h, order_id="124"))
    second = h.connector._order_tracker.fetch_order(client_order_id="124")
    second.update_exchange_order_id(exchange_order_id)
    assert first.exchange_order_id == second.exchange_order_id
    with pytest.raises(IOError, match="ambiguous retained venue identities"):
        h.connector._process_order_message([[{exchange_order_id: {"vol_exec": "3"}}]])
    for order in (first, second):
        assert order.current_state is h.env["OrderState"].OPEN
        assert not order.order_fills
        assert h.connector._order_tracker.spectra_order_ambiguity(order.client_order_id)
        assert (
            h.connector._order_tracker.spectra_order_fill_watermark(
                order.client_order_id
            )
            is None
        )
    assert len(h.calls) == 2


def test_raw_status_conflicting_known_venue_and_userref_quarantines_both_attempts(
    harness,
):
    h = harness

    async def unique_transport(**kwargs):
        h.calls.append(kwargs)
        return {
            "error": [],
            "result": {"txid": ["VENUE-ORDER-" + kwargs["data"]["userref"]]},
        }

    h.connector._api_request = unique_transport
    first = accepted_registered_order(h)
    exchange_order_id, _ = asyncio.run(place(h, order_id="124"))
    second = h.connector._order_tracker.fetch_order(client_order_id="124")
    second.update_exchange_order_id(exchange_order_id)
    with pytest.raises(IOError, match="conflicting retained identity hints"):
        h.connector._process_order_message(
            [
                [
                    {
                        first.exchange_order_id: {
                            "status": "canceled",
                            "userref": "124",
                            "vol_exec": "3",
                        }
                    }
                ]
            ]
        )
    for order in (first, second):
        assert order.current_state is h.env["OrderState"].OPEN
        assert h.connector._order_tracker.spectra_order_ambiguity(order.client_order_id)
        assert (
            h.connector._order_tracker.spectra_order_fill_watermark(
                order.client_order_id
            )
            is None
        )
    assert len(h.calls) == 2


@pytest.mark.parametrize("hint", [None, "456"])
def test_known_venue_trade_after_ttl_cannot_disappear_behind_missing_or_foreign_hint(
    harness, hint
):
    h = harness
    tracker = h.connector._order_tracker
    order = accepted_registered_order(h)
    clock = [0]
    tracker._cached_orders = PROBE.TTLCache(
        maxsize=tracker.MAX_CACHE_SIZE,
        ttl=tracker.CACHED_ORDER_TTL,
        timer=lambda: clock[0],
    )
    tracker.stop_tracking_order("123")
    clock[0] = 31
    tracker._cached_orders.expire()
    assert "123" not in tracker.cached_orders
    assert "123" not in tracker.all_updatable_orders
    raw = raw_venue_trade(order)
    if hint is None:
        del raw["userref"]
        h.connector._process_trade_message([{"KNOWN-ID-LATE-TRADE": raw}])
        assert order.executed_amount_base == Decimal("3")
        assert list(order.order_fills) == ["KNOWN-ID-LATE-TRADE"]
        assert not tracker.spectra_order_ambiguity("123")
    else:
        raw["userref"] = hint
        with pytest.raises(IOError, match="venue_userref_mismatch"):
            h.connector._process_trade_message([{"KNOWN-ID-LATE-TRADE": raw}])
        assert order.executed_amount_base == Decimal("0")
        assert not order.order_fills
        assert tracker.spectra_order_ambiguity("123") == "venue_userref_mismatch"
    assert tracker.all_fillable_orders["123"] is order
    assert "123" not in tracker.active_orders
    assert len(h.calls) == 1


def test_raw_trade_duplicate_known_venue_identity_quarantines_all_relevant_refs(
    harness,
):
    h = harness
    first = accepted_registered_order(h)
    exchange_order_id, _ = asyncio.run(place(h, order_id="124"))
    second = h.connector._order_tracker.fetch_order(client_order_id="124")
    second.update_exchange_order_id(exchange_order_id)
    raw = raw_venue_trade(first)
    del raw["userref"]
    with pytest.raises(IOError, match="ambiguous retained venue identities"):
        h.connector._process_trade_message([{"AMBIGUOUS-VENUE-TRADE": raw}])
    for order in (first, second):
        assert order.executed_amount_base == Decimal("0")
        assert not order.order_fills
        assert (
            h.connector._order_tracker.spectra_order_ambiguity(order.client_order_id)
            == "trade_signature_ambiguous"
        )
    assert len(h.calls) == 2


def test_raw_trade_conflicting_known_venue_and_userref_quarantines_both_attempts(
    harness,
):
    h = harness

    async def unique_transport(**kwargs):
        h.calls.append(kwargs)
        return {
            "error": [],
            "result": {"txid": ["VENUE-ORDER-" + kwargs["data"]["userref"]]},
        }

    h.connector._api_request = unique_transport
    first = accepted_registered_order(h)
    exchange_order_id, _ = asyncio.run(place(h, order_id="124"))
    second = h.connector._order_tracker.fetch_order(client_order_id="124")
    second.update_exchange_order_id(exchange_order_id)
    with pytest.raises(IOError, match="conflicting retained identity hints"):
        h.connector._process_trade_message(
            [{"CONFLICTING-HINT-TRADE": raw_venue_trade(first, userref="124")}]
        )
    for order in (first, second):
        assert order.executed_amount_base == Decimal("0")
        assert not order.order_fills
        assert h.connector._order_tracker.spectra_order_ambiguity(order.client_order_id)
    assert len(h.calls) == 2
