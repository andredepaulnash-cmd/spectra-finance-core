"""Apply adaptive market policy to an owned-spot cash-wait episode.

The reducer's entry/cover names are INTERNAL market-reference terminology here.
References are bound to actual owned-spot SELL receipts. No derivative position,
borrow, short profit, cover fill or external order is created. A COVER intent only
qualifies a separate native/PSM rebuild check, which may still reject the buy.
"""
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
import json

from spectra_core.adaptive_reversal import policy as P
from spectra_core.adaptive_reversal.atr import WilderATR, update_atr, snapshot_atr, restore_atr
from spectra_core.adaptive_reversal.research_adapter import CausalFlag, count_evidence
from spectra_core.reversal_signals import HourBar, SignalObservation

D = Decimal


def research_config():
    return replace(P.PROVISIONAL_RESEARCH_CONFIG, execution_delay_seconds=300)


def policy_bar(bar: HourBar):
    bar.validate()
    return P.CompletedBar(bar.opened_at, bar.opened_at + 3600,
                          D(str(bar.high)), D(str(bar.low)), D(str(bar.close)))


class OwnedCashReversal:
    """One reference per immutable PSM episode; actual holdings live elsewhere."""
    def __init__(self, *, config, state, sale_receipts, sealed_at):
        self.config, self.state = config, state
        self.sale_receipts = tuple(dict(row) for row in sale_receipts)
        self.sealed_at = sealed_at
        self.closed_at = None
        self.actual_rebuild_receipts = ()

    @classmethod
    def from_settled_sales(cls, *, cycle_id, pair, sale_receipts, sealed_at, config=None):
        config = config or research_config()
        if type(sealed_at) is not int or not sale_receipts:
            raise ValueError("actual settled owned-sale receipts required")
        seen = set()
        normalized = []
        for row in sale_receipts:
            if (row["trade_type"] != "SELL" or row["trading_pair"] != pair
                    or row["fee_currency"] != "USD" or row["trade_id"] in seen):
                raise ValueError("foreign or duplicate owned-sale reference")
            occurred = datetime.fromisoformat(row["timestamp"]).timestamp()
            if occurred != int(occurred) or not 0 <= occurred <= sealed_at:
                raise ValueError("future or ambiguous sale timestamp")
            for key in ("amount", "price", "fee_paid"):
                if not isinstance(row[key], str) or not D(row[key]).is_finite() or D(row[key]) < 0:
                    raise ValueError("exact positive sale amounts required")
            if D(row["amount"]) <= 0 or D(row["price"]) <= 0:
                raise ValueError("empty owned sale")
            seen.add(row["trade_id"])
            normalized.append((int(occurred), row["trade_id"], dict(row)))
        normalized.sort(key=lambda item: item[:2])
        state = P.start(config, cycle_id, pair, normalized[0][0])
        for occurred, _, row in normalized:
            state = P.reduce(config, state, P.EntryFill(cycle_id, row["trade_id"], occurred,
                sealed_at, D(row["amount"]), D(row["price"]))).state
        state = P.reduce(config, state, P.SealEntry(sealed_at)).state
        if state.halted_reason:
            raise ValueError("owned-sale reference admission failed: " + state.halted_reason)
        return cls(config=config, state=state, sale_receipts=[item[2] for item in normalized], sealed_at=sealed_at)

    def observe(self, hour: HourBar, observation: SignalObservation, *, atr: D, now: int):
        if self.closed_at is not None:
            raise ValueError("completed owned-cash episode is immutable")
        if hour.pair != self.state.instrument or observation.pair != hour.pair:
            raise ValueError("foreign reversal evidence")
        hour.validate()
        if observation.completed_at != hour.opened_at + 3600 or observation.close != hour.close:
            raise ValueError("hour/signal source mismatch")
        if hour.opened_at < self.state.last_entry_fill_at:
            return {"eligible": False, "reason": "hour_overlaps_actual_sale"}
        families = observation.reversal_families()
        flags = tuple(CausalFlag(key, key, value, observation.completed_at,
                                observation.completed_at + 300)
                      for key, value in sorted(families.items()))
        event = count_evidence(evidence_id=f"{self.state.cycle_id}:hour:{observation.completed_at}",
            observed_at=now, bar=policy_bar(hour), atr=atr, flags=flags,
            confirmed_reversal=all(families.values()))
        self.state = P.reduce(self.config, self.state, event).state
        return self.qualification(now=now)

    def qualification(self, *, now: int):
        fresh = (self.state.last_bar_close_at is not None and
                 0 <= now - self.state.last_bar_close_at <= self.config.max_evidence_age_seconds)
        eligible = self.closed_at is None and self.state.halted_reason is None and fresh and self.state.phase == P.Phase.COVER
        reason = ("owned_cash_episode_closed" if self.closed_at is not None else
                  "reference_halted:" + self.state.halted_reason if self.state.halted_reason else
                  "no_fresh_completed_hour" if not fresh else
                  "adaptive_reversal_qualified" if eligible else "adaptive_reversal_not_confirmed")
        return {"eligible": eligible, "reason": reason, "phase": self.state.phase.value,
                "source_hour": self.state.last_bar_close_at,
                "intent_id": self.state.intent.intent_id if self.state.intent else None,
                "active_stop": self.state.active_stop,
                "semantic_scope": "OWNED_SPOT_REBUILD_SIGNAL_ONLY", "derivative_pnl": None}

    def complete_owned_rebuild(self, *, at, actual_rebuild_receipt_ids):
        if type(at) is not int or at < self.sealed_at or not actual_rebuild_receipt_ids:
            raise ValueError("actual confirmed rebuild receipt references required")
        ids = tuple(actual_rebuild_receipt_ids)
        if len(set(ids)) != len(ids) or any(not isinstance(x, str) or not x for x in ids):
            raise ValueError("unique actual rebuild receipt references required")
        if self.closed_at is not None:
            if self.closed_at != at or self.actual_rebuild_receipts != ids:
                raise ValueError("conflicting rebuild completion")
            return
        self.closed_at, self.actual_rebuild_receipts = at, ids

    def dumps(self):
        return json.dumps({"schema": 1, "scope": "OWNED_SPOT_MARKET_REFERENCE",
            "reducer": P.snapshot(self.config, self.state), "sale_receipts": self.sale_receipts,
            "sealed_at": self.sealed_at, "closed_at": self.closed_at,
            "actual_rebuild_receipts": self.actual_rebuild_receipts},
            sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def loads(cls, payload):
        raw = json.loads(payload)
        if set(raw) != {"schema", "scope", "reducer", "sale_receipts", "sealed_at", "closed_at", "actual_rebuild_receipts"} or raw["schema"] != 1 or raw["scope"] != "OWNED_SPOT_MARKET_REFERENCE":
            raise ValueError("unsupported owned-cash reference snapshot")
        config, state = P.restore(raw["reducer"])
        source = cls.from_settled_sales(cycle_id=state.cycle_id, pair=state.instrument,
            sale_receipts=raw["sale_receipts"], sealed_at=raw["sealed_at"], config=config)
        if state.entry_quantity != source.state.entry_quantity or state.entry_notional != source.state.entry_notional or state.covered_quantity != 0:
            raise ValueError("reference is not bound to owned sales or contains derivative cover fills")
        source.state = state
        if raw["closed_at"] is not None:
            source.complete_owned_rebuild(at=raw["closed_at"], actual_rebuild_receipt_ids=raw["actual_rebuild_receipts"])
        elif raw["actual_rebuild_receipts"]:
            raise ValueError("unclosed reference has completion receipts")
        if source.dumps() != payload:
            raise ValueError("noncanonical owned-cash reference snapshot")
        return source
