"""Authorized paper execution adapter for the Hummingbot API."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from .governor import CapitalAuthorization, CapitalDenied, CapitalGovernor
from .safety import PaperSafetySettings

LOGGER = logging.getLogger("spectra_alpha2.execution")


TERMINAL_ORDER_STATES={"FILLED","CANCELED","CANCELLED","REJECTED","FAILED","EXPIRED"}
PAPER_CONNECTOR="kraken_paper_trade"
ZERO=Decimal("0")


class ExecutionUncertain(RuntimeError):
    """Order state cannot be proven; reservation remains held for reconciliation."""


class ExecutionRejected(RuntimeError):
    """Order was not submitted; the caller may safely release its reservation."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    authorization_id: str
    order_id: str
    status: str
    filled_quantity: Decimal
    gross_quote_notional: Decimal
    fee_quote: Decimal


class HummingbotPaperExecutionClient:
    """Only submits orders with a single-use receipt signed by CapitalGovernor."""

    def __init__(
        self,
        base_url: str,
        *,
        api_username: str,
        api_password: str,
        environment: dict[str,str],
        timeout_seconds: float=20,
        poll_interval_seconds: float=1,
        transport: httpx.AsyncBaseTransport | None=None,
    ):
        self.safety=PaperSafetySettings.from_environment(environment)
        self.safety.assert_paper_mode()
        if not base_url.startswith(("http://","https://")):
            raise ValueError("Hummingbot API base_url must be HTTP(S)")
        if not api_username or not api_password:
            raise ValueError("internal Hummingbot API credentials are required")
        self._client=httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(api_username,api_password),
            timeout=timeout_seconds,
            transport=transport,
        )
        self.poll_interval_seconds=poll_interval_seconds
        self.quarantined_authorization_ids=frozenset(
            item.strip() for item in str(environment.get("SPECTRA_QUARANTINED_AUTHORIZATION_IDS", "")).split(",") if item.strip()
        )
        self.quarantined_order_ids=frozenset(
            item.strip() for item in str(environment.get("SPECTRA_QUARANTINED_ORDER_IDS", "")).split(",") if item.strip()
        )

    async def _post(
        self,path:str,payload:dict[str,Any],*,headers:dict[str,str]|None=None,timeout:float|None=None
    ):
        self.safety.assert_paper_mode()
        response=await self._client.post(path,json=payload,headers=headers,timeout=timeout)
        response.raise_for_status()
        return response.json()

    async def _get(self,path:str):
        self.safety.assert_paper_mode()
        response=await self._client.get(path)
        response.raise_for_status()
        return response.json()

    async def _ensure_paper_market_ready(self,trading_pair:str)->None:
        """Initialize the pair in PaperTradeExchange before submitting an order.

        Public tickers/rules/order books do not automatically add a pair to the
        paper connector's simulated market set. Hummingbot requires its
        /market-data/trading-pair/add readiness operation before trading.
        """
        if not trading_pair.endswith("-USD"):
            raise ExecutionRejected("Alpha 2.0 paper execution accepts USD-quoted markets only")
        try:
            result=await self._post("/market-data/trading-pair/add",{
                "connector_name":"kraken_paper_trade",
                "trading_pair":trading_pair,
                "account_name":"master_account",
                "timeout":30,
            },timeout=35)
        except httpx.HTTPError as exc:
            raise ExecutionRejected(
                f"Could not initialize {trading_pair} in the Kraken paper connector; no order was submitted"
            ) from exc
        if not isinstance(result,dict) or result.get("success") is not True:
            raise ExecutionRejected(
                f"Hummingbot did not confirm paper-market initialization for {trading_pair}: {result!r}; no order was submitted"
            )

    async def _recover_order_id(self,governor:CapitalGovernor,authorization_id:str,*,is_exit:bool=False) -> str | None:
        try:
            result=await self._get(f"/spectra/dispatch-receipts/{authorization_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code==404:
                return None
            raise ExecutionUncertain("could not query the durable Hummingbot dispatch receipt") from exc
        order_id=result.get("order_id")
        if order_id:
            if is_exit:
                governor.bind_exit_order_id(authorization_id,order_id=str(order_id))
            else:
                governor.bind_order_id(authorization_id,order_id=str(order_id))
            return str(order_id)
        response_status=result.get("response_status")
        if isinstance(response_status,int) and 400<=response_status<500:
            if is_exit:
                governor.release_exit_authorization(authorization_id,confirmed_no_fill=True)
            else:
                governor.release_unfilled_reservation(authorization_id,confirmed_no_fill=True)
            return ""
        return None

    async def submit_authorized_buy(
        self,
        governor: CapitalGovernor,
        authorization: CapitalAuthorization,
        request_body: dict[str,str],
        *,
        shard_id: str | None=None,
        timeout_seconds: float=60,
    ) -> ExecutionResult:
        self.safety.assert_paper_mode()
        if request_body.get("connector_name")!="kraken_paper_trade":
            raise ExecutionUncertain("executor refuses any connector except kraken_paper_trade")
        await self._ensure_paper_market_ready(str(request_body.get("trading_pair","")))
        governor.consume_authorization(
            authorization.authorization_id,
            idempotency_key=authorization.idempotency_key,
            connector="kraken_paper_trade",
        )
        receipt=governor.issue_dispatch_receipt(
            authorization.authorization_id,
            request_body=request_body,
            now=time.time(),
        )
        try:
            response=await self._post(
                "/trading/orders",
                request_body,
                headers={"X-Spectra-Dispatch-Receipt":receipt.token},
            )
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                governor.release_unfilled_reservation(
                    authorization.authorization_id,
                    confirmed_no_fill=True,
                )
                raise ExecutionUncertain(f"Hummingbot rejected the authorized paper order: HTTP {exc.response.status_code}") from exc
            recovered=await self._recover_order_id(governor,authorization.authorization_id)
            if recovered=="":
                raise ExecutionUncertain("Hummingbot rejected the order before submission; capital reservation released") from exc
            if recovered is None:
                raise ExecutionUncertain("Hummingbot order submission outcome is uncertain; authorization remains reserved") from exc
            order_id=recovered
        except httpx.HTTPError as exc:
            recovered=await self._recover_order_id(governor,authorization.authorization_id)
            if recovered=="":
                raise ExecutionUncertain("Hummingbot rejected the order before submission; capital reservation released") from exc
            if recovered is None:
                raise ExecutionUncertain("Hummingbot order submission outcome is uncertain; authorization remains reserved") from exc
            order_id=recovered
        else:
            order_id=str(response.get("order_id") or "").strip()
            if not order_id:
                recovered=await self._recover_order_id(governor,authorization.authorization_id)
                if recovered in (None,""):
                    raise ExecutionUncertain("Hummingbot response did not include a recoverable order ID")
                order_id=recovered
            else:
                governor.bind_order_id(authorization.authorization_id,order_id=order_id)
        return await self._wait_and_settle(
            governor,
            authorization.authorization_id,
            order_id,
            request_body["trading_pair"],
            shard_id=shard_id,
            timeout_seconds=timeout_seconds,
        )

    async def _search_orders(self,trading_pair:str,*,start_time:datetime,end_time:datetime):
        # Hummingbot API TimeRangePaginationParams requires Unix milliseconds,
        # not ISO-8601 strings. Sending datetimes returns HTTP 422 and prevents
        # fill reconciliation even after an order has been accepted.
        return await self._post("/trading/orders/search",{
            "account_names":["master_account"],
            "connector_names":["kraken_paper_trade"],
            "trading_pairs":[trading_pair],
            "start_time":int(start_time.timestamp()*1000),
            "end_time":int(end_time.timestamp()*1000),
            "limit":200,
        })

    async def _search_trades(self,trading_pair:str,*,start_time:datetime,end_time:datetime):
        return await self._post("/trading/trades",{
            "account_names":["master_account"],
            "connector_names":["kraken_paper_trade"],
            "trading_pairs":[trading_pair],
            "start_time":int(start_time.timestamp()*1000),
            "end_time":int(end_time.timestamp()*1000),
            "limit":500,
        })

    @staticmethod
    def _quote_fee_for_trade(trade:dict[str,Any],trading_pair:str)->Decimal:
        raw_fee=trade.get("fee_paid",trade.get("fee"))
        if raw_fee is None:
            raise ExecutionUncertain("Hummingbot trade record is missing its fee amount")
        fee=Decimal(str(raw_fee))
        if not fee.is_finite() or fee<0:
            raise ExecutionUncertain("Hummingbot trade record contains an invalid fee amount")
        currency=str(trade.get("fee_currency") or "").strip().upper()
        if not currency:
            raise ExecutionUncertain("Hummingbot trade record is missing its fee currency")
        base,quote=trading_pair.rsplit("-",1)
        aliases={"XBT":"BTC","XXBT":"BTC","ZUSD":"USD","ZEUR":"EUR","ZGBP":"GBP","ZAUD":"AUD","ZCAD":"CAD","ZJPY":"JPY"}
        currency=aliases.get(currency,currency)
        base=aliases.get(base.upper(),base.upper())
        quote=aliases.get(quote.upper(),quote.upper())
        if fee==0 and currency in {base,quote}:
            return Decimal("0")
        if currency==quote:
            return fee
        if currency==base:
            price=Decimal(str(trade.get("price","0")))
            if not price.is_finite() or price<=0:
                raise ExecutionUncertain("Cannot convert base-denominated trade fee without a valid fill price")
            return fee*price
        raise ExecutionUncertain(
            f"Cannot reconcile {currency} fee for {trading_pair}; fee is neither base nor quote currency"
        )

    async def _wait_and_settle(
        self,
        governor: CapitalGovernor,
        authorization_id: str,
        order_id: str,
        trading_pair: str,
        *,
        shard_id: str | None,
        timeout_seconds: float,
    ) -> ExecutionResult:
        self.safety.assert_paper_mode()
        if not authorization_id or not str(authorization_id).strip():
            raise ExecutionUncertain("CapitalGovernor authorization ID is required")
        if not order_id or not str(order_id).strip():
            raise ExecutionUncertain("Hummingbot order ID is required for fill settlement")
        order_id=str(order_id).strip()
        authorization_id=str(authorization_id).strip()
        base, quote = trading_pair.rsplit("-", 1)
        base = base.upper()
        quote = quote.upper()
        started=datetime.now(timezone.utc)
        deadline=time.monotonic()+timeout_seconds
        order=None
        while time.monotonic() < deadline:
            now=datetime.now(timezone.utc)
            result=await self._search_orders(trading_pair,start_time=started-timedelta(hours=24),end_time=now+timedelta(seconds=2))
            order=next((item for item in result.get("data",[]) if str(item.get("order_id"))==order_id),None)
            if order and str(order.get("status","")).upper() in TERMINAL_ORDER_STATES:
                break
            await asyncio.sleep(self.poll_interval_seconds)
        if order is None:
            return ExecutionResult(authorization_id,order_id,"PENDING",Decimal("0"),Decimal("0"),Decimal("0"))
        status=str(order.get("status","")).upper()
        if status not in TERMINAL_ORDER_STATES:
            return ExecutionResult(authorization_id,order_id,"PENDING",Decimal("0"),Decimal("0"),Decimal("0"))

        # Evidence validation: the connector order must match the authorized market,
        # side, order type, connector, account, and signed base quantity.
        if str(order.get("trading_pair","")).strip() and str(order.get("trading_pair")).strip() != trading_pair:
            raise ExecutionUncertain("connector order trading pair does not match the authorized market")
        if str(order.get("connector_name","")).strip().lower() not in ("", PAPER_CONNECTOR):
            raise ExecutionUncertain("connector order is not from kraken_paper_trade")
        if str(order.get("account_name","")).strip() not in ("", "master_account"):
            raise ExecutionUncertain("connector order is not for the master paper account")
        if str(order.get("side","")).upper() not in ("", "BUY"):
            raise ExecutionUncertain("connector order side is not BUY")
        if str(order.get("trade_type","")).upper() not in ("", "BUY"):
            raise ExecutionUncertain("connector order trade type is not BUY")
        if str(order.get("order_type","")).upper() not in ("", "MARKET"):
            raise ExecutionUncertain("connector order is not a MARKET BUY")
        order_amount = None
        if order.get("amount") not in (None, ""):
            order_amount = Decimal(str(order.get("amount")))
            if not order_amount.is_finite() or order_amount <= ZERO:
                raise ExecutionUncertain("connector order amount is invalid")
        executed_base = None
        if order.get("executed_amount_base") not in (None, ""):
            executed_base = Decimal(str(order.get("executed_amount_base")))
            if not executed_base.is_finite() or executed_base < ZERO:
                raise ExecutionUncertain("connector executed base amount is invalid")
        executed_quote = None
        if order.get("executed_amount_quote") not in (None, ""):
            executed_quote = Decimal(str(order.get("executed_amount_quote")))
            if not executed_quote.is_finite() or executed_quote < ZERO:
                raise ExecutionUncertain("connector executed quote amount is invalid")

        end=datetime.now(timezone.utc)
        trades=await self._search_trades(trading_pair,start_time=started-timedelta(hours=24),end_time=end+timedelta(seconds=2))
        fills=[]
        seen_trade_ids=set()
        for item in trades.get("data", []):
            if str(item.get("order_id")) != order_id:
                continue
            trade_id = item.get("trade_id")
            dedup_key = trade_id if trade_id is not None else (item.get("timestamp"), item.get("amount"), item.get("price"))
            if dedup_key in seen_trade_ids:
                continue
            seen_trade_ids.add(dedup_key)
            fills.append(item)

        # Validate every trade before any settlement or capital release.
        for item in fills:
            if str(item.get("trading_pair","")).strip() and str(item.get("trading_pair")).strip() != trading_pair:
                raise ExecutionUncertain("trade trading pair does not match the authorized market")
            if str(item.get("connector_name","")).strip().lower() not in ("", PAPER_CONNECTOR):
                raise ExecutionUncertain("trade is not from kraken_paper_trade")
            if str(item.get("side","")).upper() not in ("", "BUY"):
                raise ExecutionUncertain("trade side is not BUY")
            if str(item.get("trade_type","")).upper() not in ("", "BUY"):
                raise ExecutionUncertain("trade type is not BUY")
            amount = Decimal(str(item.get("amount","0")))
            price = Decimal(str(item.get("price","0")))
            if not amount.is_finite() or amount <= ZERO:
                raise ExecutionUncertain("trade amount must be positive")
            if not price.is_finite() or price <= ZERO:
                raise ExecutionUncertain("trade price must be positive")
            timestamp = item.get("timestamp")
            if timestamp not in (None, ""):
                try:
                    if isinstance(timestamp, str):
                        trade_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    else:
                        trade_time = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
                    if trade_time < started - timedelta(minutes=2) or trade_time > end + timedelta(seconds=2):
                        raise ExecutionUncertain("trade timestamp is outside the authorized order window")
                except (TypeError, ValueError, OSError, OverflowError) as exc:
                    raise ExecutionUncertain("trade timestamp is invalid") from exc

        quantity=sum((Decimal(str(item.get("amount","0"))) for item in fills),Decimal("0"))
        gross=sum((Decimal(str(item.get("amount","0")))*Decimal(str(item.get("price","0"))) for item in fills),Decimal("0"))
        if quantity<=0 or gross<=0:
            governor.release_unfilled_reservation(authorization_id,confirmed_no_fill=True)
            return ExecutionResult(authorization_id,order_id,status,Decimal("0"),Decimal("0"),Decimal("0"))
        # Evidence-backed quantity checks.
        tolerance = Decimal("0.00000001")
        if order_amount is not None and quantity > order_amount + (tolerance * max(order_amount, Decimal("1"))):
            raise ExecutionUncertain("fill quantity exceeds the connector order amount")
        if executed_base is not None and abs(quantity - executed_base) > (tolerance * max(executed_base, Decimal("1"))):
            raise ExecutionUncertain("fill quantity does not match connector executed base amount")
        if executed_quote is not None and abs(gross - executed_quote) > (tolerance * max(executed_quote, Decimal("1"))):
            raise ExecutionUncertain("fill gross does not match connector executed quote amount")
        average_price=gross/quantity
        actual_fee_quote=sum(
            (self._quote_fee_for_trade(item,trading_pair) for item in fills),
            Decimal("0"),
        )
        # Fee sanity: Kraken paper fees should not exceed the configured buy fee plus a small tolerance.
        if actual_fee_quote > gross * Decimal("0.004") * Decimal("1.1"):
            raise ExecutionUncertain("fill fee exceeds the configured Kraken paper fee allowance")
        try:
            governor.settle_fill(
                authorization_id,
                actual_gross_notional=gross,
                actual_fee=actual_fee_quote,
                actual_quantity=quantity,
                average_price=average_price,
                order_id=order_id,
            )
        except CapitalDenied as exc:
            # Idempotent reconciliation: an already-settled authorization must not be
            # treated as a new fill or cause an unrecoverable cycle halt.
            if "submitted authorization" in str(exc).lower() or "already" in str(exc).lower():
                return ExecutionResult(authorization_id,order_id,status,quantity,gross,actual_fee_quote)
            raise ExecutionUncertain(f"Governor refused fill settlement: {exc}") from exc
        if shard_id:
            governor.activate_vcap_shard(shard_id)
        return ExecutionResult(authorization_id,order_id,status,quantity,gross,actual_fee_quote)


    async def submit_authorized_sell(
        self,
        governor: CapitalGovernor,
        authorization,
        request_body: dict[str,str],
        *,
        timeout_seconds: float=60,
    ) -> ExecutionResult:
        self.safety.assert_paper_mode()
        if request_body.get("connector_name")!="kraken_paper_trade" or request_body.get("trade_type")!="SELL":
            raise ExecutionUncertain("executor permits only Governor-authorized Kraken paper sells")
        try:
            await self._ensure_paper_market_ready(str(request_body.get("trading_pair","")))
        except ExecutionRejected:
            governor.release_exit_authorization(authorization.authorization_id,confirmed_no_fill=True)
            raise
        governor.consume_exit_authorization(
            authorization.authorization_id,
            idempotency_key=authorization.idempotency_key,
            connector="kraken_paper_trade",
        )
        receipt=governor.issue_exit_dispatch_receipt(
            authorization.authorization_id,request_body=request_body,now=time.time()
        )
        try:
            response=await self._post(
                "/trading/orders",request_body,
                headers={"X-Spectra-Dispatch-Receipt":receipt.token},
            )
        except httpx.HTTPStatusError as exc:
            if 400<=exc.response.status_code<500:
                governor.release_exit_authorization(authorization.authorization_id,confirmed_no_fill=True)
                raise ExecutionUncertain(f"Hummingbot rejected the authorized paper exit: HTTP {exc.response.status_code}") from exc
            recovered=await self._recover_order_id(governor,authorization.authorization_id,is_exit=True)
            if recovered in (None,""):
                raise ExecutionUncertain("paper exit submission is uncertain; exit quantity remains reserved") from exc
            order_id=recovered
        except httpx.HTTPError as exc:
            recovered=await self._recover_order_id(governor,authorization.authorization_id,is_exit=True)
            if recovered in (None,""):
                raise ExecutionUncertain("paper exit submission is uncertain; exit quantity remains reserved") from exc
            order_id=recovered
        else:
            order_id=str(response.get("order_id") or "").strip()
            if not order_id:
                order_id=await self._recover_order_id(governor,authorization.authorization_id,is_exit=True)
            else:
                governor.bind_exit_order_id(authorization.authorization_id,order_id=order_id)
            if not order_id:
                raise ExecutionUncertain("Hummingbot exit response has no recoverable order ID")
        return await self._wait_and_settle_exit(
            governor,authorization.authorization_id,order_id,request_body["trading_pair"],
            timeout_seconds=timeout_seconds,
        )

    async def _wait_and_settle_exit(
        self,governor:CapitalGovernor,authorization_id:str,order_id:str,trading_pair:str,
        *,timeout_seconds:float,
    ) -> ExecutionResult:
        self.safety.assert_paper_mode()
        if not authorization_id or not str(authorization_id).strip():
            raise ExecutionUncertain("CapitalGovernor exit authorization ID is required")
        if not order_id or not str(order_id).strip():
            raise ExecutionUncertain("Hummingbot exit order ID is required for settlement")
        order_id=str(order_id).strip()
        authorization_id=str(authorization_id).strip()
        base, quote = trading_pair.rsplit("-", 1)
        base = base.upper()
        quote = quote.upper()
        started=datetime.now(timezone.utc)
        deadline=time.monotonic()+timeout_seconds
        order=None
        while time.monotonic()<deadline:
            now=datetime.now(timezone.utc)
            result=await self._search_orders(trading_pair,start_time=started-timedelta(hours=24),end_time=now+timedelta(seconds=2))
            order=next((item for item in result.get("data",[]) if str(item.get("order_id"))==order_id),None)
            if order and str(order.get("status","")).upper() in TERMINAL_ORDER_STATES:
                break
            await asyncio.sleep(self.poll_interval_seconds)
        if order is None or str(order.get("status","")).upper() not in TERMINAL_ORDER_STATES:
            return ExecutionResult(authorization_id,order_id,"PENDING",Decimal("0"),Decimal("0"),Decimal("0"))
        status=str(order.get("status","")).upper()
        if str(order.get("trading_pair","")).strip() and str(order.get("trading_pair")).strip() != trading_pair:
            raise ExecutionUncertain("exit connector order trading pair does not match the authorized market")
        if str(order.get("connector_name","")).strip().lower() not in ("", PAPER_CONNECTOR):
            raise ExecutionUncertain("exit connector order is not from kraken_paper_trade")
        if str(order.get("account_name","")).strip() not in ("", "master_account"):
            raise ExecutionUncertain("exit connector order is not for the master paper account")
        if str(order.get("side","")).upper() not in ("", "SELL"):
            raise ExecutionUncertain("exit connector order side is not SELL")
        if str(order.get("trade_type","")).upper() not in ("", "SELL"):
            raise ExecutionUncertain("exit connector order trade type is not SELL")
        if str(order.get("order_type","")).upper() not in ("", "LIMIT"):
            raise ExecutionUncertain("exit connector order is not a LIMIT SELL")
        end=datetime.now(timezone.utc)
        trades=await self._search_trades(trading_pair,start_time=started-timedelta(hours=24),end_time=end+timedelta(seconds=2))
        fills=[]
        seen_trade_ids=set()
        for item in trades.get("data", []):
            if str(item.get("order_id")) != order_id:
                continue
            trade_id = item.get("trade_id")
            dedup_key = trade_id if trade_id is not None else (item.get("timestamp"), item.get("amount"), item.get("price"))
            if dedup_key in seen_trade_ids:
                continue
            seen_trade_ids.add(dedup_key)
            fills.append(item)
        for item in fills:
            if str(item.get("trading_pair","")).strip() and str(item.get("trading_pair")).strip() != trading_pair:
                raise ExecutionUncertain("exit trade trading pair does not match the authorized market")
            if str(item.get("connector_name","")).strip().lower() not in ("", PAPER_CONNECTOR):
                raise ExecutionUncertain("exit trade is not from kraken_paper_trade")
            if str(item.get("side","")).upper() not in ("", "SELL"):
                raise ExecutionUncertain("exit trade side is not SELL")
            if str(item.get("trade_type","")).upper() not in ("", "SELL"):
                raise ExecutionUncertain("exit trade type is not SELL")
            amount = Decimal(str(item.get("amount","0")))
            price = Decimal(str(item.get("price","0")))
            if not amount.is_finite() or amount <= ZERO:
                raise ExecutionUncertain("exit trade amount must be positive")
            if not price.is_finite() or price <= ZERO:
                raise ExecutionUncertain("exit trade price must be positive")
        quantity=sum((Decimal(str(item.get("amount","0"))) for item in fills),Decimal("0"))
        gross=sum((Decimal(str(item.get("amount","0")))*Decimal(str(item.get("price","0"))) for item in fills),Decimal("0"))
        if quantity<=0 or gross<=0:
            governor.release_exit_authorization(authorization_id,confirmed_no_fill=True)
            return ExecutionResult(authorization_id,order_id,status,Decimal("0"),Decimal("0"),Decimal("0"))
        average_price=gross/quantity
        actual_fee_quote=sum(
            (self._quote_fee_for_trade(item,trading_pair) for item in fills),
            Decimal("0"),
        )
        try:
            governor.settle_exit_fill(
                authorization_id,actual_quantity=quantity,average_price=average_price,actual_fee=actual_fee_quote,order_id=order_id
            )
        except CapitalDenied as exc:
            if "submitted" in str(exc).lower() or "already" in str(exc).lower():
                return ExecutionResult(authorization_id,order_id,status,quantity,gross,actual_fee_quote)
            raise ExecutionUncertain(f"Governor refused exit fill settlement: {exc}") from exc
        return ExecutionResult(authorization_id,order_id,status,quantity,gross,actual_fee_quote)

    async def reconcile_pending(self,governor:CapitalGovernor,*,timeout_seconds:float=0.1) -> list[ExecutionResult]:
        self.safety.assert_paper_mode()
        results=[]
        for pending in governor.list_pending_authorizations():
            if pending["authorization_id"] in self.quarantined_authorization_ids or str(pending.get("order_id") or "") in self.quarantined_order_ids:
                LOGGER.warning("skipping quarantined legacy authorization %s / order %s during reconciliation", pending["authorization_id"], pending.get("order_id"))
                continue
            order_id=pending["order_id"]
            if not order_id:
                recovered=await self._recover_order_id(
                    governor,pending["authorization_id"],is_exit=pending.get("kind")=="EXIT"
                )
                if recovered=="":
                    continue
                if recovered is None:
                    raise ExecutionUncertain(
                        f"authorization {pending['authorization_id']} has no recoverable Hummingbot order ID; manual reconciliation required"
                    )
                order_id=recovered
            if pending.get("kind")=="EXIT":
                results.append(await self._wait_and_settle_exit(
                    governor,pending["authorization_id"],order_id,pending["asset_id"],
                    timeout_seconds=timeout_seconds,
                ))
            else:
                results.append(await self._wait_and_settle(
                    governor,pending["authorization_id"],order_id,pending["asset_id"],
                    shard_id=pending["shard_id"],timeout_seconds=timeout_seconds,
                ))
        return results

    async def close(self) -> None:
        await self._client.aclose()
