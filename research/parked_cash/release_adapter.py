"""Isolated native-replay funding permission; no exchange or production edits.

The sole policy change is a bounded permission for exact parked cash to fund
specified native buy kinds. Signals, ranking, native recovery/tranche planners,
execution floors, fees, physical reserve and ordering remain native. OFF runs
the original hydrated kernel. Claims are virtual: total cash never increases.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict
from decimal import Decimal as D
from types import MethodType

Z = D(0)
TOL = D("1e-18")
MODES = {
    "OFF": (frozenset(), Z),
    "ENTRY_ONLY": (frozenset({"ENTRY"}), D(100)),
    "RECOVERY_ONLY": (frozenset({"RECOVERY"}), D(100)),
    "BOTH": (frozenset({"ENTRY", "RECOVERY"}), D(100)),
    "BOTH_TIGHT": (frozenset({"ENTRY", "RECOVERY"}), D(50)),
}


@dataclass(frozen=True)
class Claim:
    order_id: int
    pair: str
    kind: str
    amount: D
    ordinary: D
    parked: D
    position_id: int | None
    conditional: bool = False


@dataclass(frozen=True)
class SourceLot:
    lot_id: str
    order_id: int | None
    quantity: D
    total_basis: D
    parked_basis: D


class ReleaseController:
    """Small source ledger beside the unchanged native owned-lot ledger.

    ``cap`` limits outstanding parked-source fee-inclusive acquisition basis
    plus reserved parked claims. Returned sale cash is P again; it must obtain
    a new permission. This is an outstanding cap, not a lifetime-turnover cap.
    The controller never selects an asset or calls a signal/planner itself.
    """
    def __init__(self, dataset, arm):
        if arm not in MODES:
            raise ValueError(f"Unknown fixed release arm: {arm}")
        self.dataset = dataset
        self.arm = arm
        self.allowed, self.cap = MODES[arm]
        self.enabled = bool(self.allowed)
        self.claims: dict[int, Claim] = {}
        self.contingent = {}
        self.terminal = {}
        self.lots: dict[str, SourceLot] = {}
        self.productive_receipts = {}
        self.events = []
        self.counts = Counter()
        self.context = None
        self.parked_spent = Z
        self.parked_whole_returned = Z
        self.parked_partial_returned = Z
        self.parked_basis_retired = Z
        self.peak_committed = Z
        self._original_buy = dataset.record_buy
        self._original_full = dataset.record_full_exit
        self._original_partial = dataset.fill_partial
        # The frozen PARK baseline has never used its parked cash. Every
        # inherited remaining lot is ordinary-funded; no ownership is invented.
        for lot_id, lot in dataset.ledger.lots.items():
            if lot.base_remaining > Z:
                self.lots[lot_id] = SourceLot(lot_id, None, lot.base_remaining,
                    lot.cost_basis_remaining_quote + lot.entry_fees_remaining_quote, Z)
        self.initial_parked = dataset.parked_cash()
        self.initial_source_owned_basis = Z

    @property
    def owned_parked_basis(self):
        return sum((lot.parked_basis for lot in self.lots.values()), Z)

    @property
    def reserved_parked(self):
        return sum((claim.parked for claim in self.claims.values()), Z)

    @property
    def reserved_ordinary(self):
        return sum((claim.ordinary for claim in self.claims.values()), Z)

    @property
    def reserved_total(self):
        return sum((claim.amount for claim in self.claims.values()), Z)

    def begin_planning(self, cash, equity, cfg, positions, timestamp):
        # All immediate-next-bar due orders must be terminal before planning
        # another bar. A forgotten claim invalidates the experiment.
        if self.claims or self.contingent:
            raise ArithmeticError("A due buy claim survived its terminal frame")
        self.context = dict(cash=cash, equity=equity, cfg=cfg,
                            positions=positions, timestamp=timestamp)
        self.check(cash=cash)

    def authority(self, kind):
        if self.context is None:
            raise ArithmeticError("Funding authority requires native planning context")
        c = self.context
        physical = max(Z, c["cash"] - c["equity"] * c["cfg"].reserve_fraction
                       - self.reserved_total)
        ordinary = max(Z, c["cash"] - self.dataset.parked_cash()
                       - self.reserved_ordinary)
        permitted = Z
        if kind in self.allowed:
            permitted = min(max(Z, self.dataset.parked_cash() - self.reserved_parked),
                            max(Z, self.cap - self.owned_parked_basis - self.reserved_parked))
        return min(physical, ordinary + permitted)

    def _make_claim(self, order, *, cash, conditional=False):
        if order.order_id in self.claims or order.order_id in self.terminal:
            raise ArithmeticError("Duplicate or terminal funding order ID")
        amount = order.budget
        ordinary_free = max(Z, cash - self.dataset.parked_cash() - self.reserved_ordinary)
        ordinary = min(amount, ordinary_free)
        parked = amount - ordinary
        if parked > Z:
            if order.kind not in self.allowed:
                return None
            if parked > self.dataset.parked_cash() - self.reserved_parked + TOL:
                return None
            if self.owned_parked_basis + self.reserved_parked + parked > self.cap + TOL:
                return None
        claim = Claim(order.order_id, order.pair, order.kind, amount, ordinary,
                      parked, order.position_id, conditional)
        self.claims[order.order_id] = claim
        self.counts["claims"] += 1
        self.counts["parked_claims"] += bool(parked)
        self.peak_committed = max(self.peak_committed,
                                 self.owned_parked_basis + self.reserved_parked)
        self.events.append(dict(event="CLAIM", **asdict(claim),
                               total_cash_added=Z, cap=self.cap))
        return claim

    def claim_order(self, order):
        if order.kind not in {"ENTRY", "RECOVERY"}:
            return
        if not self.enabled:
            return
        if self.context is None:
            raise ArithmeticError("Native buy scheduled outside planning context")
        if order.order_id in self.claims or order.order_id in self.terminal or order.order_id in self.contingent:
            raise ArithmeticError("Duplicate native buy identity")
        if order.depends_on_order_id is not None:
            # Native alternative replacements depend on the SAME not-yet-sold
            # parent. Reserving each now would triple-count the future receipt.
            self.contingent[order.order_id] = order
            self.counts["contingent_replacements"] += 1
            return
        if order.budget > self.authority(order.kind) + TOL:
            raise ArithmeticError("Native scheduled buy exceeds reserved source authority")
        if self._make_claim(order, cash=self.context["cash"]) is None:
            raise ArithmeticError("Native buy failed exact source claim")

    def may_fill(self, order, total, cash, positions, index):
        """Virtual source check only; native execution guards still run next."""
        if not self.enabled:
            return total <= cash - self.dataset.parked_cash() + TOL
        if order.order_id in self.terminal:
            raise ArithmeticError("Terminal order offered another buy fill")
        claim = self.claims.get(order.order_id)
        if claim is None:
            contingent = self.contingent.get(order.order_id)
            if contingent is None:
                raise ArithmeticError("Native buy fill lacks its scheduled claim")
            # Sells have already settled and native dependency checks passed.
            # This claim is made against actual settled cash, never a forecast.
            claim = self._make_claim(order, cash=cash, conditional=True)
            if claim is None:
                self.counts["source_fill_denials"] += 1
                return False
            self.contingent.pop(order.order_id, None)
        if (claim.pair, claim.kind, claim.position_id) != (order.pair, order.kind, order.position_id):
            raise ArithmeticError("Funding claim/native order mismatch")
        if abs(total - claim.amount) > TOL:
            raise ArithmeticError("Native FOK actual cost differs from exact order budget")
        ordinary_other = self.reserved_ordinary - claim.ordinary
        parked_other = self.reserved_parked - claim.parked
        ordinary_actual = max(Z, total - claim.parked)
        if ordinary_actual > cash - self.dataset.parked_cash() - ordinary_other + TOL:
            self.counts["source_fill_denials"] += 1
            return False
        if claim.parked > self.dataset.parked_cash() - parked_other + TOL:
            self.counts["source_fill_denials"] += 1
            return False
        if self.owned_parked_basis + self.reserved_parked > self.cap + TOL:
            self.counts["source_fill_denials"] += 1
            return False
        return True

    def reject(self, order_id, reason):
        if not self.enabled:
            return False
        if order_id in self.terminal:
            self.counts["idempotent_terminal_notifications"] += 1
            return False
        claim = self.claims.pop(order_id, None)
        contingent = self.contingent.pop(order_id, None)
        if claim is None and contingent is None:
            return False  # This is a native sale rejection, not a buy claim.
        self.terminal[order_id] = "REJECTED"
        self.counts["claim_rejections"] += 1
        self.events.append(dict(event="CLAIM_ZERO_FILL_RELEASE", order_id=order_id,
                               reason=reason, parked_cash_debited=Z,
                               released_parked_claim=Z if claim is None else claim.parked))
        return True

    def expire(self, order_id, reason="research_window_end"):
        released = self.reject(order_id, reason)
        if released:
            self.terminal[order_id] = "EXPIRED"
            self.counts["claim_expirations"] += 1
        return released

    def record_buy(self, fill, position, cash_before):
        lot_id = f"lot:{fill.order_id}"
        if lot_id in self.lots or self.terminal.get(fill.order_id) == "FILLED":
            raise ArithmeticError("Duplicate owned-lot buy disposal identity")
        total = fill.gross_quote + fill.fee_quote
        claim = self.claims.get(fill.order_id)
        if self.enabled and claim is None:
            raise ArithmeticError("Successful native buy has no exact source claim")
        parked_used = Z if claim is None else min(total, claim.parked)
        ordinary_used = total - parked_used
        if claim is not None and ordinary_used > claim.ordinary + TOL:
            raise ArithmeticError("Buy consumes another order's ordinary claim")
        if parked_used > self.dataset.parked_cash() + TOL:
            raise ArithmeticError("Committed buy exceeds actual parked cash")
        before_principal = self.dataset.principal_cash
        before_profit = self.dataset.profit_cash
        before_parked = self.dataset.parked_cash()
        # Preserve the distinction between parked recovered principal and past
        # partial profit. The last component receives the rounding residual.
        used_principal = Z if parked_used == Z else parked_used * before_principal / before_parked
        used_profit = parked_used - used_principal
        self.dataset.principal_cash -= used_principal
        self.dataset.profit_cash -= used_profit
        try:
            # This is the original call, once, with its original ownership and
            # actual-fill checks. Cash did not change when permission changed.
            self._original_buy(fill, position, cash_before)
        except BaseException:
            self.dataset.principal_cash = before_principal
            self.dataset.profit_cash = before_profit
            raise
        self.dataset.principal_reused += used_principal
        self.dataset.profit_reused += used_profit
        self.lots[lot_id] = SourceLot(lot_id, fill.order_id, fill.quantity,
                                     total, parked_used)
        self.claims.pop(fill.order_id, None)
        self.contingent.pop(fill.order_id, None)
        self.terminal[fill.order_id] = "FILLED"
        self.parked_spent += parked_used
        self.counts["buy_fills"] += 1
        self.counts["parked_funded_buy_fills"] += bool(parked_used)
        self.counts[f"parked_funded_{fill.kind}_fills"] += bool(parked_used)
        self.events.append(dict(event="BUY_SOURCE_COMMIT", order_id=fill.order_id,
            pair=fill.pair, kind=fill.kind, lot_id=lot_id, actual_all_in=total,
            ordinary_used=ordinary_used, parked_used=parked_used,
            parked_principal_used=used_principal, parked_profit_used=used_profit,
            parked_before=before_parked, parked_after=self.dataset.parked_cash(),
            source_owned_quantity=Z if total == Z else fill.quantity * parked_used / total,
            total_cash_added=Z, owned_parked_basis=self.owned_parked_basis))
        self.peak_committed = max(self.peak_committed,
                                 self.owned_parked_basis + self.reserved_parked)
        self.check(cash=cash_before-total)

    @staticmethod
    def _lot_receipts(lots, net):
        quantity = sum((lot.base_remaining for lot in lots), Z)
        if quantity <= Z:
            raise ArithmeticError("Sale lacks exact owned quantity")
        remaining = net
        result = []
        for i, lot in enumerate(lots):
            receipt = remaining if i == len(lots)-1 else net * lot.base_remaining / quantity
            remaining -= receipt
            result.append((lot, receipt))
        return result

    def _source_sale(self, lots, net):
        records = []
        for native, receipt in self._lot_receipts(lots, net):
            source = self.lots.get(native.lot_id)
            if source is None:
                raise ArithmeticError("Native disposal lacks exact funding ownership")
            basis = native.cost_basis_remaining_quote + native.entry_fees_remaining_quote
            if abs(source.quantity-native.base_remaining) > TOL or abs(source.total_basis-basis) > TOL:
                raise ArithmeticError("Native disposal/source quantity or basis mismatch")
            parked_net = Z if source.parked_basis == Z else receipt * source.parked_basis / basis
            records.append(dict(lot_id=native.lot_id, quantity=native.base_remaining,
                native_basis=basis, actual_net=receipt, parked_basis=source.parked_basis,
                parked_net=parked_net, ordinary_net=receipt-parked_net))
        return records

    def _retire(self, records, kind, order_id):
        for record in records:
            source = self.lots.pop(record["lot_id"], None)
            if source is None:
                raise ArithmeticError("Duplicate source lot disposal")
            self.parked_basis_retired += source.parked_basis
        self.events.append(dict(event="SOURCE_SALE_FINAL", order_id=order_id,
                               kind=kind, lots=records, owned_parked_basis=self.owned_parked_basis))

    def record_full_exit(self, fill):
        lots = self.dataset.ledger.list_active_lots(str(fill.position_id))
        net = fill.gross_quote - fill.fee_quote
        records = self._source_sale(lots, net)
        self._original_full(fill)
        principal = sum((min(row["parked_basis"], row["parked_net"]) for row in records), Z)
        profit = sum((max(Z, row["parked_net"]-row["parked_basis"]) for row in records), Z)
        parked_net = principal + profit
        self.dataset.principal_cash += principal
        self.dataset.profit_cash += profit
        self.parked_whole_returned += parked_net
        self.productive_receipts[fill.order_id] = net - parked_net
        self._retire(records, "EXIT", fill.order_id)
        self.counts["whole_source_disposals"] += 1
        self.counts["funded_whole_source_disposals"] += bool(parked_net)

    def productive_exit_proceeds(self, fill):
        if fill.order_id not in self.productive_receipts:
            raise ArithmeticError("Linked replacement precedes exact finalized source receipt")
        return self.productive_receipts[fill.order_id]

    def fill_partial(self, order, position, index, timestamp, cfg, capacity):
        manifest = self.dataset.orders.get(order.order_id)
        lots = [] if manifest is None else [self.dataset.ledger.lots[lot_id]
                                             for lot_id in manifest.lot_ids]
        # The native method validates every manifest and execution condition.
        # Compute source receipts only AFTER it returns a successful native fill
        # using pre-sale immutable lot snapshots captured here.
        result, reason = self._original_partial(order, position, index, timestamp, cfg, capacity)
        if result is None:
            return result, reason
        fill, updated = result
        records = self._source_sale(lots, fill.gross_quote-fill.fee_quote)
        parked_net = sum((row["parked_net"] for row in records), Z)
        # Native partial method already parked ALL actual net proceeds. Adding
        # source-colored cash again here would mint/double-count cash.
        self.parked_partial_returned += parked_net
        self._retire(records, "PARTIAL_RECYCLE", order.order_id)
        self.counts["partial_source_disposals"] += 1
        self.counts["funded_partial_source_disposals"] += bool(parked_net)
        return result, reason

    def check(self, *, cash=None):
        parked = self.dataset.parked_cash()
        if self.dataset.principal_cash < -TOL or self.dataset.profit_cash < -TOL:
            raise ArithmeticError("Negative parked cash component")
        if cash is not None and (cash < -TOL or parked > cash + TOL):
            raise ArithmeticError("Parked source balance exceeds actual cash")
        if self.owned_parked_basis + self.reserved_parked > self.cap + TOL:
            raise ArithmeticError("Outstanding parked-source basis/claims exceeds fixed cap")
        if self.reserved_parked > parked + TOL:
            raise ArithmeticError("Reserved parked claims exceed settled parked cash")
        if cash is not None and self.reserved_ordinary > cash - parked + TOL:
            raise ArithmeticError("Reserved ordinary claims exceed actual ordinary cash")
        native = {lot_id: lot for lot_id, lot in self.dataset.ledger.lots.items()
                  if lot.base_remaining > Z}
        if set(native) != set(self.lots):
            raise ArithmeticError("Source/native active owned-lot sets differ")
        for lot_id, lot in native.items():
            source = self.lots[lot_id]
            basis = lot.cost_basis_remaining_quote + lot.entry_fees_remaining_quote
            if abs(basis-source.total_basis) > TOL or abs(lot.base_remaining-source.quantity) > TOL:
                raise ArithmeticError("Source/native active owned basis/quantity differs")
            if source.parked_basis < -TOL or source.parked_basis > source.total_basis + TOL:
                raise ArithmeticError("Invalid owned parked basis")
        if abs(self.parked_spent-self.parked_basis_retired-self.owned_parked_basis) > TOL:
            raise ArithmeticError("Exact outstanding source basis conservation failed")
        return True

    def finalize_window(self, pending):
        pending_ids = {order.order_id for order in pending}
        if not set(self.claims).issubset(pending_ids) or not set(self.contingent).issubset(pending_ids):
            raise ArithmeticError("Claim retained for absent native pending order")
        for order_id in sorted(set(self.claims) | set(self.contingent)):
            self.expire(order_id)
        return self.audit_snapshot()

    def audit_snapshot(self):
        self.check()
        return dict(research_only=True, arm=self.arm, permitted_buy_kinds=sorted(self.allowed),
            fixed_outstanding_basis_cap=self.cap, cap_semantics="owned acquisition basis plus virtual parked claims",
            owned_parked_basis=self.owned_parked_basis, reserved_parked=self.reserved_parked,
            reserved_ordinary=self.reserved_ordinary, reserved_total=self.reserved_total,
            peak_owned_basis_plus_claims=self.peak_committed,
            parked_spent=self.parked_spent, parked_basis_retired=self.parked_basis_retired,
            parked_whole_returned=self.parked_whole_returned,
            parked_partial_returned=self.parked_partial_returned,
            remaining_parked_cash=self.dataset.parked_cash(), counts=dict(self.counts),
            pending_claims=[asdict(claim) for claim in self.claims.values()],
            contingent_order_ids=sorted(self.contingent),
            active_source_lots=[asdict(lot) for lot in self.lots.values()],
            total_cash_minted=Z, predictive_winner_selector=False,
            production_restart_or_exchange_semantics_proven=False)


def attach(dataset, arm):
    """Attach source-color observer/funding controller AFTER typed hydration."""
    if hasattr(dataset, "release"):
        raise ArithmeticError("A funding controller is already attached")
    controller = ReleaseController(dataset, arm)
    dataset.release = controller
    dataset.record_buy = controller.record_buy
    dataset.record_full_exit = controller.record_full_exit
    dataset.fill_partial = controller.fill_partial
    controller.check()
    return controller


def build_source(source):
    """Patch exact native funding boundaries; fail on missing/duplicate anchors."""
    changes = []
    def change(before, after, purpose):
        nonlocal source
        if source.count(before) != 1:
            raise ArithmeticError(f"Funding overlay boundary mismatch: {purpose}")
        source = source.replace(before, after)
        changes.append(dict(purpose=purpose, before=before, after=after))
    change('        rejection_counts[reason] += 1',
           '        dataset.release.reject(order.order_id, reason)\n        rejection_counts[reason] += 1',
           'terminal zero-fill claim release; original rejection remains')
    before = '''        return FleetOrder(
            next_order_id,
            timestamp,
            pair,
            kind,
            budget,
            position_id,
            depends_on,
        )'''
    after = before.replace('return FleetOrder(', 'order = FleetOrder(') + '\n        dataset.release.claim_order(order)\n        return order'
    change(before, after, 'claim native-order source budget without choosing assets')
    change('''            if total > cash - dataset.parked_cash():
                reject(index, order, "parked_partial_proceeds" if dataset.park_proceeds else "insufficient_shared_cash")
                continue''',
           '''            if not dataset.release.may_fill(order, total, cash, positions, index):
                reject(index, order, "parked_source_authority")
                continue''',
           'virtual source authority; all native later execution guards unchanged')
    change('                released_by_exit[order.order_id] = proceeds',
           '                released_by_exit[order.order_id] = dataset.release.productive_exit_proceeds(fills[-1])',
           'linked replacement exemption uses only productive actual whole-sale receipt')
    change('        spendable = min(max(ZERO, cash - equity * cfg.reserve_fraction), max(ZERO, cash - dataset.parked_cash()))',
           '''        dataset.release.begin_planning(cash, equity, cfg, positions, timestamp)
        spendable = dataset.release.authority("RECOVERY")''',
           'source-specific native planning authority with physical reserve')
    change('            average = position.basis / position.quantity',
           '''            spendable = dataset.release.authority("RECOVERY")
            average = position.basis / position.quantity''',
           'recompute shared source claims before each native recovery proposal')
    change('        # Rank only from observations available at this close.',
           '''        spendable = dataset.release.authority("ENTRY")
        # Rank only from observations available at this close.''',
           'ENTRY receives only its source permission after recovery claims')
    return source, changes


def compile_hydrated(module, dataset, simulator):
    """Return the same configured simulator with isolated funding overlay run.

    Root must call hydration.hydrate_native first, then attach, then this. The
    exact executed source is available as dataset.release_generated_source.
    OFF preserves the original hydrated class/run/source entirely.
    """
    if not hasattr(dataset, "release"):
        raise ArithmeticError("Attach exact source controller before compilation")
    if not dataset.release.enabled:
        dataset.release_generated_source = dataset.hydration_generated_source
        dataset.release_source_changes = []
        return simulator
    source, changes = build_source(dataset.hydration_generated_source)
    namespace = dict(module.a.original.__dict__)
    namespace["FleetOrder"] = module.ResearchOrder
    exec(compile(source, __file__.replace('release_adapter.py', 'generated_release_body.py'), 'exec'), namespace)
    cls = type("ResearchParkedReleaseSimulator", (type(simulator),), {"run": namespace["run"]})
    simulator.__class__ = cls
    dataset.release_generated_source = source
    dataset.release_source_changes = changes
    dataset.manifest.update(parked_release_arm=dataset.release.arm,
        parked_release_policy="fixed outstanding source-owned basis/claims; ordinary-first; exact finalized receipts repark",
        parked_release_cap=str(dataset.release.cap), native_signal_planners_preserved=True,
        native_movement_entry_selector_preserved=True, predictive_winner_selector=False,
        no_new_history_or_wallet_freshness_gate=True)
    return simulator
