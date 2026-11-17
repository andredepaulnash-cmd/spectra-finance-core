import base64
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from decimal import Decimal
from pathlib import Path

from spectra_alpha2.governor import CapitalDenied, CapitalGovernor, CapitalPool
from spectra_alpha2.safety import PaperSafetyError


SAFE_ENV = {"SPECTRA_PAPER_ONLY": "true", "SPECTRA_ALLOW_LIVE_EXECUTION": "false"}


class CapitalGovernorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "governor.sqlite3"
        self.gov = CapitalGovernor(self.db, environment=SAFE_ENV)

    def tearDown(self):
        self.tmp.cleanup()

    def settle(self, auth, *, gross=Decimal("20"), fee=Decimal("0.08"), quantity=Decimal("0.2"), price=Decimal("100"), order_id="paper-order"):
        self.gov.bind_order_id(auth.authorization_id,order_id=order_id)
        self.gov.settle_fill(
            auth.authorization_id,
            actual_gross_notional=gross,
            actual_fee=fee,
            actual_quantity=quantity,
            average_price=price,
        )

    def sell_lot(self,lot_id,*,quantity,price,fee,idempotency_key,order_id):
        auth=self.gov.authorize_exit(
            lot_id,quantity=quantity,limit_price=price,idempotency_key=idempotency_key
        )
        self.gov.consume_exit_authorization(
            auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade"
        )
        self.gov.bind_exit_order_id(auth.authorization_id,order_id=order_id)
        return self.gov.settle_exit_fill(
            auth.authorization_id,actual_quantity=quantity,average_price=price,actual_fee=fee
        )

    def open_position(self, asset="BTC-USDT", position="p1"):
        auth = self.gov.reserve_initial_position(
            position_id=position,
            asset_id=asset,
            reference_price=Decimal("100"),
            idempotency_key=f"initial-{position}",
        )
        self.gov.consume_authorization(auth.authorization_id, idempotency_key=auth.idempotency_key, connector="kraken_paper_trade")
        self.settle(auth,order_id=f"paper-order-{position}-initial")
        return position

    def test_governor_refuses_missing_or_live_execution_environment(self):
        with self.assertRaises(PaperSafetyError):
            CapitalGovernor(self.db, environment={"SPECTRA_PAPER_ONLY": "true", "SPECTRA_ALLOW_LIVE_EXECUTION": "true"})


    def test_only_submitted_governor_reservation_can_sign_exact_paper_order(self):
        auth=self.gov.reserve_initial_position(
            position_id="p1",asset_id="BTC-USDT",reference_price=Decimal("100"),idempotency_key="receipt-i1"
        )
        body={
            "account_name":"master_account",
            "connector_name":"kraken_paper_trade",
            "trading_pair":"BTC-USDT",
            "trade_type":"BUY",
            "amount":"0.0003",
            "order_type":"LIMIT",
            "price":"65000",
            "position_action":"OPEN",
        }
        with self.assertRaises(CapitalDenied):
            self.gov.issue_dispatch_receipt(auth.authorization_id,request_body=body,now=time.time())
        self.gov.consume_authorization(auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade")
        private=Ed25519PrivateKey.generate()
        raw=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        encoded=base64.urlsafe_b64encode(raw).decode().rstrip("=")
        env={
            "SPECTRA_PAPER_ONLY":"true",
            "SPECTRA_ALLOW_LIVE_EXECUTION":"false",
            "SPECTRA_DISPATCH_POLICY_VERSION":"spectra-alpha2-v1",
            "SPECTRA_DISPATCH_PRIVATE_KEY":encoded,
        }
        with patch.dict(os.environ,env,clear=True):
            receipt=self.gov.issue_dispatch_receipt(auth.authorization_id,request_body=body,now=time.time())
        token_payload,signature=receipt.token.split(".")
        decode=lambda value: base64.urlsafe_b64decode(value+"="*(-len(value)%4))
        payload=json.loads(decode(token_payload))
        private.public_key().verify(decode(signature),__import__("spectra_alpha2.dispatch_receipt",fromlist=["canonical_json"]).canonical_json(payload))
        self.assertEqual(payload["proposal_id"],auth.authorization_id)
        self.assertEqual(payload["amount"],"20")
        self.assertEqual(payload["connector_name"],"kraken_paper_trade")

    def test_governor_refuses_to_sign_order_above_reserved_quote_amount(self):
        auth=self.gov.reserve_initial_position(
            position_id="p1",asset_id="BTC-USDT",reference_price=Decimal("100"),idempotency_key="receipt-i2"
        )
        self.gov.consume_authorization(auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade")
        body={
            "account_name":"master_account","connector_name":"kraken_paper_trade",
            "trading_pair":"BTC-USDT","trade_type":"BUY","amount":"0.0004",
            "order_type":"LIMIT","price":"65000","position_action":"OPEN",
        }
        private=Ed25519PrivateKey.generate()
        raw=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        env={
            "SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false",
            "SPECTRA_DISPATCH_POLICY_VERSION":"spectra-alpha2-v1",
            "SPECTRA_DISPATCH_PRIVATE_KEY":base64.urlsafe_b64encode(raw).decode().rstrip("="),
        }
        with patch.dict(os.environ,env,clear=True), self.assertRaises(CapitalDenied):
            self.gov.issue_dispatch_receipt(auth.authorization_id,request_body=body,now=time.time())


    def test_exit_authorization_enforces_net_target_and_prevents_double_selling(self):
        self.open_position()
        with self.assertRaises(CapitalDenied):
            self.gov.authorize_exit("p1:INITIAL",quantity=Decimal("0.1"),limit_price=Decimal("100"),idempotency_key="bad-price")
        with self.assertRaises(CapitalDenied):
            self.gov.authorize_exit("p1:INITIAL",quantity=Decimal("0.3"),limit_price=Decimal("110"),idempotency_key="too-much")
        auth=self.gov.authorize_exit(
            "p1:INITIAL",quantity=Decimal("0.1"),limit_price=Decimal("105"),idempotency_key="exit-one"
        )
        with self.assertRaises(CapitalDenied):
            self.gov.authorize_exit(
                "p1:INITIAL",quantity=Decimal("0.2"),limit_price=Decimal("105"),idempotency_key="exit-two"
            )
        self.gov.consume_exit_authorization(
            auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade"
        )
        body={
            "account_name":"master_account","connector_name":"kraken_paper_trade",
            "trading_pair":"BTC-USDT","trade_type":"SELL","amount":"0.1",
            "order_type":"LIMIT","price":"105","position_action":"OPEN",
        }
        with self.assertRaises(CapitalDenied):
            self.gov.issue_dispatch_receipt(auth.authorization_id,request_body=body,now=time.time())
        private=Ed25519PrivateKey.generate()
        raw=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        env={
            "SPECTRA_PAPER_ONLY":"true","SPECTRA_ALLOW_LIVE_EXECUTION":"false",
            "SPECTRA_DISPATCH_POLICY_VERSION":"spectra-alpha2-v1",
            "SPECTRA_DISPATCH_PRIVATE_KEY":base64.urlsafe_b64encode(raw).decode().rstrip("="),
        }
        with patch.dict(os.environ,env,clear=True):
            receipt=self.gov.issue_exit_dispatch_receipt(auth.authorization_id,request_body=body,now=time.time())
        self.assertTrue(receipt.token)
        self.gov.bind_exit_order_id(auth.authorization_id,order_id="paper-exit-one")
        realized=self.gov.settle_exit_fill(
            auth.authorization_id,actual_quantity=Decimal("0.1"),
            average_price=Decimal("105"),actual_fee=Decimal("0.042"),
        )
        self.assertEqual(realized,Decimal("0.418"))

    def test_pool_budgets_are_exact(self):
        snap = self.gov.snapshot()
        self.assertEqual(snap["pools"][CapitalPool.INITIAL.value]["budget"], Decimal("6250"))
        self.assertEqual(snap["pools"][CapitalPool.RECOVERY.value]["budget"], Decimal("18750"))

    def test_initial_reservation_requires_paper_authorization_before_open(self):
        auth = self.gov.reserve_initial_position(
            position_id="p1", asset_id="BTC-USDT", reference_price=Decimal("100"), idempotency_key="i1"
        )
        self.assertEqual(self.gov.snapshot()["open_positions"], 1)
        with self.assertRaises(CapitalDenied):
            self.gov.consume_authorization(auth.authorization_id, idempotency_key="i1", connector="kraken")
        self.gov.consume_authorization(auth.authorization_id, idempotency_key="i1", connector="kraken_paper_trade")
        self.settle(auth,order_id="paper-order-initial-test")
        self.assertEqual(self.gov.snapshot()["open_positions"], 1)

    def test_74_position_limit_is_atomic_under_concurrent_admission(self):
        def reserve(i):
            try:
                self.gov.reserve_initial_position(
                    position_id=f"p{i}", asset_id=f"ASSET{i}-USDT",
                    reference_price=Decimal("1"), idempotency_key=f"key-{i}"
                )
                return True
            except CapitalDenied:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(reserve,range(90)))
        self.assertEqual(sum(results),74)
        self.assertEqual(self.gov.snapshot()["open_positions"],74)

    def test_dca_uses_recovery_pool_and_asset_cap_includes_reservations(self):
        self.open_position()
        for i, price in enumerate((Decimal("97.26"),Decimal("95.56"),Decimal("93.26"))):
            auth=self.gov.reserve_addition(
                position_id="p1",action_id=f"DCA_{i+1}",gross_notional=Decimal("20"),
                current_price=price,idempotency_key=f"dca-{i}",action_type="DCA"
            )
            self.gov.consume_authorization(auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade")
            self.settle(auth,order_id=f"paper-dca-{i}")
        self.assertEqual(self.gov.snapshot()["pools"]["INITIAL"]["spent"],Decimal("20.08"))
        self.assertEqual(self.gov.snapshot()["pools"]["RECOVERY"]["spent"],Decimal("60.24"))

    def test_vcap_uses_recovery_pool_and_enforces_two_shards_per_asset(self):
        self.open_position()
        for i, price in enumerate((Decimal("97.26"),Decimal("95.56"),Decimal("93.26"))):
            dca=self.gov.reserve_addition(
                position_id="p1",action_id=f"DCA_{i+1}",gross_notional=Decimal("20"),
                current_price=price,idempotency_key=f"pre-vcap-dca-{i}",action_type="DCA"
            )
            self.gov.consume_authorization(dca.authorization_id,idempotency_key=dca.idempotency_key,connector="kraken_paper_trade")
            self.settle(dca,order_id=f"paper-pre-vcap-dca-{i}")
        shards=[]
        for i,(amount,price,step_id) in enumerate((
            (Decimal("20"),Decimal("92.23"),"VCAP_FAT1_1"),
            (Decimal("40"),Decimal("90.26"),"VCAP_FAT1_2"),
        )):
            auth=self.gov.reserve_addition(
                position_id="p1",action_id=step_id,gross_notional=amount,
                current_price=price,idempotency_key=f"vcap-{i}",action_type="VCAP",shard_id=f"shard-{i}"
            )
            self.gov.consume_authorization(auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade")
            self.settle(auth,gross=amount,fee=amount*Decimal("0.004"),quantity=amount/Decimal("100"),price=Decimal("100"),order_id=f"paper-vcap-{i}")
            self.gov.activate_vcap_shard(f"shard-{i}")
            shards.append(f"shard-{i}")
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_addition(
                position_id="p1",action_id="VCAP_FAT1_3",gross_notional=Decimal("80"),
                current_price=Decimal("89.26"),idempotency_key="vcap-3",action_type="VCAP",shard_id="shard-2"
            )
        self.sell_lot(shards[0],quantity=Decimal("0.2"),price=Decimal("110"),fee=Decimal("0.088"),idempotency_key="sell-vcap-0",order_id="paper-sell-vcap-0")
        self.gov.resolve_vcap_shard(shards[0])
        allowed=self.gov.reserve_addition(
            position_id="p1",action_id="VCAP_FAT1_3",gross_notional=Decimal("80"),
            current_price=Decimal("89.26"),idempotency_key="vcap-3",action_type="VCAP",shard_id="shard-2"
        )
        self.assertEqual(allowed.pool,CapitalPool.RECOVERY)

    def test_asset_cap_includes_unfilled_reservations(self):
        self.open_position()
        auth=self.gov.reserve_addition(
            position_id="p1",action_id="DCA_1",gross_notional=Decimal("20"),
            current_price=Decimal("97.26"),idempotency_key="dca-1",action_type="DCA"
        )
        self.assertIsNotNone(auth.authorization_id)
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_addition(
                position_id="p1",action_id="too-much",gross_notional=Decimal("961"),
                current_price=Decimal("97.26"),idempotency_key="too-much",action_type="DCA"
            )


    def test_asset_replacement_requires_reconciled_close(self):
        self.open_position()
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1")
        before=self.gov.snapshot()["pools"]["INITIAL"]["available"]
        self.assertEqual(before,Decimal("6250")-Decimal("20.08"))
        first=self.sell_lot("p1:INITIAL",quantity=Decimal("0.1"),price=Decimal("105"),fee=Decimal("0.042"),idempotency_key="sell-initial-1",order_id="paper-sell-initial-1")
        self.assertEqual(first,Decimal("0.418"))
        after_first=self.gov.snapshot()["pools"]["INITIAL"]["available"]
        self.assertEqual(after_first,before+Decimal("10.458"))
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1",confirmed_all_exits_reconciled=True)
        second=self.sell_lot("p1:INITIAL",quantity=Decimal("0.1"),price=Decimal("105"),fee=Decimal("0.042"),idempotency_key="sell-initial-2",order_id="paper-sell-initial-2")
        self.assertEqual(second,Decimal("0.418"))
        realized=self.gov.close_position("p1",confirmed_all_exits_reconciled=True)
        self.assertEqual(realized,Decimal("0.836"))
        replacement=self.gov.reserve_initial_position(
            position_id="p2",asset_id="BTC-USDT",reference_price=Decimal("101"),idempotency_key="initial-p2"
        )
        self.assertEqual(replacement.asset_id,"BTC-USDT")

    def test_submitted_reservation_cannot_be_released_without_cancel_confirmation(self):
        auth=self.gov.reserve_initial_position(
            position_id="p1",asset_id="BTC-USDT",reference_price=Decimal("100"),idempotency_key="i1"
        )
        self.gov.consume_authorization(auth.authorization_id,idempotency_key="i1",connector="kraken_paper_trade")
        with self.assertRaises(CapitalDenied):
            self.gov.release_unfilled_reservation(auth.authorization_id)
        self.gov.release_unfilled_reservation(auth.authorization_id,confirmed_no_fill=True)
        self.assertEqual(self.gov.snapshot()["open_positions"],0)


if __name__ == "__main__":
    unittest.main()
