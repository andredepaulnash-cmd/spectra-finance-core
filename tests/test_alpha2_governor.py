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

    def open_position(self, asset="BTC-USD", position="p1"):
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
            position_id="p1",asset_id="BTC-USD",reference_price=Decimal("100"),idempotency_key="receipt-i1"
        )
        body={
            "account_name":"master_account",
            "connector_name":"kraken_paper_trade",
            "trading_pair":"BTC-USD",
            "trade_type":"BUY","strategy_origin":"TOP_GAINER",
            "amount":"0.0003",
            "order_type":"MARKET",
            "max_execution_price":"65000",
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
        self.assertEqual(payload["amount"],"30")
        self.assertEqual(payload["connector_name"],"kraken_paper_trade")

    def test_governor_refuses_to_sign_order_above_reserved_quote_amount(self):
        auth=self.gov.reserve_initial_position(
            position_id="p1",asset_id="BTC-USD",reference_price=Decimal("100"),idempotency_key="receipt-i2"
        )
        self.gov.consume_authorization(auth.authorization_id,idempotency_key=auth.idempotency_key,connector="kraken_paper_trade")
        body={
            "account_name":"master_account","connector_name":"kraken_paper_trade",
            "trading_pair":"BTC-USD","trade_type":"BUY","strategy_origin":"TOP_GAINER","amount":"0.0005",
            "order_type":"MARKET","max_execution_price":"65000","position_action":"OPEN",
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


    def test_fixed_net_take_profit_is_authorized_only_for_top_gainer_positions(self):
        self.open_position()
        with self.assertRaises(CapitalDenied):
            self.gov.authorize_exit(
                "p1:INITIAL",quantity=Decimal("0.2"),limit_price=Decimal("100"),idempotency_key="below-target"
            )
        auth=self.gov.authorize_exit(
            "p1:INITIAL",quantity=Decimal("0.2"),limit_price=Decimal("105"),idempotency_key="top-gainer-tp"
        )
        self.assertGreater(auth.minimum_price,Decimal("100"))

        other=self.gov.reserve_initial_position(
            position_id="p2",asset_id="DOGE-USD",reference_price=Decimal("100"),
            idempotency_key="top-loser-initial",strategy_origin="TOP_LOSER_REVERSAL",
        )
        self.gov.consume_authorization(other.authorization_id,idempotency_key=other.idempotency_key,connector="kraken_paper_trade")
        self.gov.bind_order_id(other.authorization_id,order_id="paper-top-loser")
        self.gov.settle_fill(
            other.authorization_id,actual_gross_notional=Decimal("20"),actual_fee=Decimal("0.08"),
            actual_quantity=Decimal("0.2"),average_price=Decimal("100"),
        )
        with self.assertRaises(CapitalDenied):
            self.gov.authorize_exit(
                "p2:INITIAL",quantity=Decimal("0.2"),limit_price=Decimal("105"),idempotency_key="top-loser-static-tp"
            )

    def test_outer_boundary_flags_manual_review_without_resetting_reference_or_patching(self):
        self.open_position()
        before=self.gov.get_position_state("p1")
        self.assertEqual(before["reference_price"],Decimal("100"))
        self.gov.flag_manual_review(
            "p1",drawdown=Decimal("-0.24"),reason="vcap_outer_boundary_reached"
        )
        after=self.gov.get_position_state("p1")
        self.assertTrue(after["manual_review_required"])
        self.assertEqual(after["reference_price"],Decimal("100"))
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_addition(
                position_id="p1",action_id="DCA_1",gross_notional=Decimal("20"),
                current_price=Decimal("97.26"),idempotency_key="manual-review-dca",action_type="DCA"
            )
        self.assertEqual(self.gov.snapshot()["manual_review_positions"],1)
        self.assertEqual(self.gov.snapshot()["open_positions"],1)

    def test_pool_budgets_are_exact(self):
        snap = self.gov.snapshot()
        self.assertEqual(snap["pools"][CapitalPool.INITIAL.value]["budget"], Decimal("6250"))
        self.assertEqual(snap["pools"][CapitalPool.RECOVERY.value]["budget"], Decimal("18750"))

    def test_initial_reservation_requires_paper_authorization_before_open(self):
        auth = self.gov.reserve_initial_position(
            position_id="p1", asset_id="BTC-USD", reference_price=Decimal("100"), idempotency_key="i1"
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
                    position_id=f"p{i}", asset_id=f"ASSET{i}-USD",
                    reference_price=Decimal("1"), idempotency_key=f"key-{i}"
                )
                return True
            except CapitalDenied:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(reserve,range(100)))
        self.assertEqual(sum(results),74)
        self.assertEqual(self.gov.snapshot()["open_positions"],74)
        # 74th concurrent reservation can succeed when otherwise authorized; 75th rejected.
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_initial_position(
                position_id="overflow-75",asset_id="OVERFLOW-USD",
                reference_price=Decimal("1"),idempotency_key="overflow-75",
            )
        self.assertEqual(self.gov.snapshot()["open_positions"],74)

    def test_reserved_and_open_count_and_one_active_position_per_asset(self):
        a1=self.gov.reserve_initial_position(position_id="p1",asset_id="BTC-USD",reference_price=Decimal("100"),idempotency_key="i1")
        a2=self.gov.reserve_initial_position(position_id="p2",asset_id="ETH-USD",reference_price=Decimal("100"),idempotency_key="i2")
        # Both RESERVED positions count toward the concurrent limit.
        self.assertEqual(self.gov.snapshot()["open_positions"],2)
        self.assertEqual(len(self.gov.list_positions()),2)
        # One active position per asset while RESERVED.
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_initial_position(position_id="p3",asset_id="BTC-USD",reference_price=Decimal("100"),idempotency_key="i3")
        # Convert one to OPEN; OPEN + RESERVED still count.
        self.gov.consume_authorization(a2.authorization_id,idempotency_key=a2.idempotency_key,connector="kraken_paper_trade")
        self.settle(a2,order_id="paper-order-p2-initial")
        self.assertEqual(self.gov.snapshot()["open_positions"],2)
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_initial_position(position_id="p4",asset_id="ETH-USD",reference_price=Decimal("100"),idempotency_key="i4")
        # A slot is not reusable until exit is fully reconciled and close is confirmed.
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p2",confirmed_all_exits_reconciled=True)
        self.assertEqual(self.gov.snapshot()["open_positions"],2)

    def test_dca_and_unconfigured_vcap_recovery_are_rejected(self):
        self.open_position()
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_addition(
                position_id="p1",action_id="DCA_1",gross_notional=Decimal("20"),
                current_price=Decimal("97.26"),idempotency_key="dca-disabled",action_type="DCA"
            )
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_addition(
                position_id="p1",action_id="VCAP_SHARD",gross_notional=Decimal("20"),
                current_price=Decimal("92.23"),idempotency_key="vcap-disabled",
                action_type="VCAP",shard_id="p1:VCAP_SHARD"
            )

    def test_position_replacement_is_blocked_without_approved_exit_policy(self):
        self.open_position()
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1")
        with self.assertRaises(CapitalDenied):
            self.gov.close_position("p1",confirmed_all_exits_reconciled=True)
        with self.assertRaises(CapitalDenied):
            self.gov.reserve_initial_position(
                position_id="p2",asset_id="BTC-USD",reference_price=Decimal("101"),idempotency_key="initial-p2"
            )

    def test_submitted_reservation_cannot_be_released_without_cancel_confirmation(self):
        auth=self.gov.reserve_initial_position(
            position_id="p1",asset_id="BTC-USD",reference_price=Decimal("100"),idempotency_key="i1"
        )
        self.gov.consume_authorization(auth.authorization_id,idempotency_key="i1",connector="kraken_paper_trade")
        with self.assertRaises(CapitalDenied):
            self.gov.release_unfilled_reservation(auth.authorization_id)
        self.gov.release_unfilled_reservation(auth.authorization_id,confirmed_no_fill=True)
        self.assertEqual(self.gov.snapshot()["open_positions"],0)

    def test_initial_entry_reserves_30_gross_and_0_12_fee_reserve(self):
        from spectra_alpha2.policy import BUY_FEE_RATE, INITIAL_ENTRY_NOTIONAL

        # Phase 1: $30 initial-entry gross notional; $0.12 = $30 * 0.004 buy fee reserve.
        self.assertEqual(INITIAL_ENTRY_NOTIONAL,Decimal("30"))
        self.assertEqual(INITIAL_ENTRY_NOTIONAL*BUY_FEE_RATE,Decimal("0.12"))
        auth=self.gov.reserve_initial_position(
            position_id="p1",asset_id="BTC-USD",reference_price=Decimal("100"),idempotency_key="initial-30"
        )
        # Authorized gross purchase budget is exactly $30, NOT $30.12 (fee reserve is not purchase notional).
        self.assertEqual(auth.gross_notional,Decimal("30"))
        self.assertEqual(auth.fee_reserve,Decimal("0.12"))
        snap=self.gov.snapshot()
        self.assertEqual(snap["pools"]["INITIAL"]["budget"],Decimal("6250"))
        self.assertEqual(snap["pools"]["INITIAL"]["reserved"],Decimal("30.12"))
        self.assertEqual(snap["pools"]["RECOVERY"]["budget"],Decimal("18750"))
        self.assertEqual(snap["pools"]["RECOVERY"]["reserved"],Decimal("0"))


if __name__ == "__main__":
    unittest.main()
