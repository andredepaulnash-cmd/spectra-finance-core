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
from .policy import BUY_FEE_RATE, SELL_FEE_RATE
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
    estimated_fee: Decimal


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

    async def _post(self,path:str,payload:dict[str,Any],*,headers:dict[str,str]|None=None):
        self.safety.assert_paper_mode()
        response=await self._client.post(path,json=payload,headers=headers)
        response.raise_for_status()
        return response.json()

    async def _get(self,path:str):
        self.safety.assert_paper_mode()
        response=await self._client.get(path)
        response.raise_for_status()
        return response.json()

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
        return await self._post("/trading/orders/search",{
            "account_names":["master_account"],
            "connector_names":["kraken_paper_trade"],
            "trading_pairs":[trading_pair],
            "start_time":start_time.isoformat(),
            "end_time":end_time.isoformat(),
            "limit":200,
        })

    async def _search_trades(self,trading_pair:str,*,start_time:datetime,end_time:datetime):
        return await self._post("/trading/trades",{
            "account_names":["master_account"],
            "connector_names":["kraken_paper_trade"],
            "trading_pairs":[trading_pair],
            "start_time":start_time.isoformat(),
            "end_time":end_time.isoformat(),
            "limit":500,
        })

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
        estimated_fee=gross*BUY_FEE_RATE
        governor.settle_fill(
            authorization_id,
            actual_gross_notional=gross,
            actual_fee=estimated_fee,
            actual_quantity=quantity,
            average_price=average_price,
        )
        if shard_id:
            governor.activate_vcap_shard(shard_id)
        return ExecutionResult(authorization_id,order_id,status,quantity,gross,estimated_fee)


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
        estimated_fee=gross*SELL_FEE_RATE
        governor.settle_exit_fill(
            authorization_id,actual_quantity=quantity,average_price=average_price,actual_fee=estimated_fee
        )
        return ExecutionResult(authorization_id,order_id,status,quantity,gross,estimated_fee)

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
