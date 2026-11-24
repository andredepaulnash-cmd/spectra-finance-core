"""Authorized paper execution adapter for the Hummingbot API."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from .governor import CapitalAuthorization, CapitalGovernor
from .safety import PaperSafetySettings


TERMINAL_ORDER_STATES={"FILLED","CANCELED","CANCELLED","REJECTED","FAILED","EXPIRED"}


class ExecutionUncertain(RuntimeError):
    """Order state cannot be proven; reservation remains held for reconciliation."""


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
            raise ExecutionUncertain("Alpha 2.0 paper execution accepts USD-quoted markets only")
        try:
            result=await self._post("/market-data/trading-pair/add",{
                "connector_name":"kraken_paper_trade",
                "trading_pair":trading_pair,
                "account_name":"master_account",
                "timeout":30,
            },timeout=35)
        except httpx.HTTPError as exc:
            raise ExecutionUncertain(
                f"Could not initialize {trading_pair} in the Kraken paper connector"
            ) from exc
        if not isinstance(result,dict) or result.get("success") is not True:
            raise ExecutionUncertain(
                f"Hummingbot did not confirm paper-market initialization for {trading_pair}: {result!r}"
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
        if fee==0:
            return Decimal("0")
        currency=str(trade.get("fee_currency") or "").strip().upper()
        if not currency:
            raise ExecutionUncertain("Hummingbot reported a nonzero fee without its fee currency")
        base,quote=trading_pair.rsplit("-",1)
        aliases={"XBT":"BTC","XXBT":"BTC","ZUSD":"USD","ZEUR":"EUR","ZGBP":"GBP","ZAUD":"AUD","ZCAD":"CAD","ZJPY":"JPY"}
        currency=aliases.get(currency,currency)
        base=aliases.get(base.upper(),base.upper())
        quote=aliases.get(quote.upper(),quote.upper())
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
        started=datetime.now(timezone.utc)
        deadline=time.monotonic()+timeout_seconds
        order=None
        while time.monotonic() < deadline:
            now=datetime.now(timezone.utc)
            result=await self._search_orders(trading_pair,start_time=started-timedelta(minutes=2),end_time=now+timedelta(seconds=2))
            order=next((item for item in result.get("data",[]) if str(item.get("order_id"))==order_id),None)
            if order and str(order.get("status","")).upper() in TERMINAL_ORDER_STATES:
                break
            await asyncio.sleep(self.poll_interval_seconds)
        if order is None or str(order.get("status","")).upper() not in TERMINAL_ORDER_STATES:
            return ExecutionResult(authorization_id,order_id,"PENDING",Decimal("0"),Decimal("0"),Decimal("0"))

        end=datetime.now(timezone.utc)
        trades=await self._search_trades(trading_pair,start_time=started-timedelta(minutes=2),end_time=end+timedelta(seconds=2))
        fills=[item for item in trades.get("data",[]) if str(item.get("order_id"))==order_id]
        quantity=sum((Decimal(str(item.get("amount","0"))) for item in fills),Decimal("0"))
        gross=sum((Decimal(str(item.get("amount","0")))*Decimal(str(item.get("price","0"))) for item in fills),Decimal("0"))
        status=str(order.get("status","")).upper()
        if quantity<=0 or gross<=0:
            governor.release_unfilled_reservation(authorization_id,confirmed_no_fill=True)
            return ExecutionResult(authorization_id,order_id,status,Decimal("0"),Decimal("0"),Decimal("0"))
        average_price=gross/quantity
        actual_fee_quote=sum(
            (self._quote_fee_for_trade(item,trading_pair) for item in fills),
            Decimal("0"),
        )
        governor.settle_fill(
            authorization_id,
            actual_gross_notional=gross,
            actual_fee=actual_fee_quote,
            actual_quantity=quantity,
            average_price=average_price,
        )
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
        await self._ensure_paper_market_ready(str(request_body.get("trading_pair","")))
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
        started=datetime.now(timezone.utc)
        deadline=time.monotonic()+timeout_seconds
        order=None
        while time.monotonic()<deadline:
            now=datetime.now(timezone.utc)
            result=await self._search_orders(trading_pair,start_time=started-timedelta(minutes=2),end_time=now+timedelta(seconds=2))
            order=next((item for item in result.get("data",[]) if str(item.get("order_id"))==order_id),None)
            if order and str(order.get("status","")).upper() in TERMINAL_ORDER_STATES:
                break
            await asyncio.sleep(self.poll_interval_seconds)
        if order is None or str(order.get("status","")).upper() not in TERMINAL_ORDER_STATES:
            return ExecutionResult(authorization_id,order_id,"PENDING",Decimal("0"),Decimal("0"),Decimal("0"))
        end=datetime.now(timezone.utc)
        trades=await self._search_trades(trading_pair,start_time=started-timedelta(minutes=2),end_time=end+timedelta(seconds=2))
        fills=[item for item in trades.get("data",[]) if str(item.get("order_id"))==order_id]
        quantity=sum((Decimal(str(item.get("amount","0"))) for item in fills),Decimal("0"))
        gross=sum((Decimal(str(item.get("amount","0")))*Decimal(str(item.get("price","0"))) for item in fills),Decimal("0"))
        status=str(order.get("status","")).upper()
        if quantity<=0 or gross<=0:
            governor.release_exit_authorization(authorization_id,confirmed_no_fill=True)
            return ExecutionResult(authorization_id,order_id,status,Decimal("0"),Decimal("0"),Decimal("0"))
        average_price=gross/quantity
        actual_fee_quote=sum(
            (self._quote_fee_for_trade(item,trading_pair) for item in fills),
            Decimal("0"),
        )
        governor.settle_exit_fill(
            authorization_id,actual_quantity=quantity,average_price=average_price,actual_fee=actual_fee_quote
        )
        return ExecutionResult(authorization_id,order_id,status,quantity,gross,actual_fee_quote)

    async def reconcile_pending(self,governor:CapitalGovernor,*,timeout_seconds:float=0.1) -> list[ExecutionResult]:
        self.safety.assert_paper_mode()
        results=[]
        for pending in governor.list_pending_authorizations():
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
