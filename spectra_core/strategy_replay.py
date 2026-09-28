"""Offline, quote-funded replay. No API client, credentials or live dispatch.

Snapshots are executable quotes, not OHLC candles. Decisions see a prefix only;
orders are IOC at a later snapshot. Candidate policies are research baselines,
not replicas of the production VCAP controller.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable, Sequence

from .strategy_lab import EquitySample, calculate_performance_metrics, base_asset

D = Decimal
ZERO = D(0)
ONE = D(1)
BPS = D(10000)


def number(value: object, name: str, minimum: Decimal = ZERO) -> Decimal:
    value = D(str(value))
    if not value.is_finite() or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return value


@dataclass(frozen=True)
class Quote:
    timestamp: int
    pair: str
    bid: Decimal
    ask: Decimal
    depth_base: Decimal
    min_base: Decimal
    min_quote: Decimal
    step_base: Decimal
    quote_volume_24h: Decimal | None = None

    def __post_init__(self):
        if type(self.timestamp) is not int or self.timestamp < 0:
            raise ValueError("timestamp must be a nonnegative integer")
        if self.quote_volume_24h is not None:
            object.__setattr__(
                self,
                "quote_volume_24h",
                number(self.quote_volume_24h, "quote_volume_24h"),
            )
        pair = self.pair.strip().upper()
        if len(pair.split("-")) != 2 or not all(pair.split("-")):
            raise ValueError("pair must be BASE-QUOTE")
        object.__setattr__(self, "pair", pair)
        for field in ("bid", "ask", "depth_base", "min_base", "min_quote", "step_base"):
            object.__setattr__(self, field, number(getattr(self, field), field))
        if self.bid <= ZERO or self.ask < self.bid or self.step_base <= ZERO:
            raise ValueError("positive bid/step and ask >= bid required")


@dataclass(frozen=True)
class ReplayConfig:
    initial_cash: Decimal = D("200")
    fee_bps: Decimal = D("40")
    slippage_bps: Decimal = D("5")
    reserve_fraction: Decimal = D("0.25")
    pair_cap_fraction: Decimal = D("0.25")
    fill_fraction: Decimal = ONE
    latency_steps: int = 1

    def __post_init__(self):
        for field in (
            "initial_cash",
            "fee_bps",
            "slippage_bps",
            "reserve_fraction",
            "pair_cap_fraction",
            "fill_fraction",
        ):
            object.__setattr__(self, field, number(getattr(self, field), field))
        if self.initial_cash <= ZERO or self.fee_bps >= BPS or self.slippage_bps >= BPS:
            raise ValueError("positive cash and costs < 10000 bps required")
        if any(
            getattr(self, f) > ONE
            for f in ("reserve_fraction", "pair_cap_fraction", "fill_fraction")
        ):
            raise ValueError("fractions must be <= 1")
        if type(self.latency_steps) is not int or self.latency_steps < 1:
            raise ValueError("latency_steps must be a positive integer")


@dataclass(frozen=True)
class Lot:
    lot_id: int
    pair: str
    quantity: Decimal
    basis: Decimal  # includes allocated buy fees
    recovery: bool
    opened_at: int


@dataclass(frozen=True)
class Order:
    pair: str
    side: str
    amount: Decimal  # all-in quote budget for BUY, base quantity for SELL
    lot_ids: tuple[int, ...] = ()  # SELL must name exact ownership
    recovery: bool = False

    def __post_init__(self):
        object.__setattr__(self, "pair", self.pair.strip().upper())
        object.__setattr__(self, "amount", number(self.amount, "amount"))
        object.__setattr__(self, "lot_ids", tuple(self.lot_ids))
        if self.side not in ("BUY", "SELL") or self.amount <= ZERO:
            raise ValueError("positive BUY/SELL order required")
        if len(set(self.lot_ids)) != len(self.lot_ids):
            raise ValueError("duplicate lot ownership")
        if (self.side == "SELL" and not self.lot_ids) or (
            self.side == "BUY" and self.lot_ids
        ):
            raise ValueError("SELL requires lot ids; BUY cannot select lots")


@dataclass(frozen=True)
class View:
    history: tuple[tuple[Quote, ...], ...]
    cash: Decimal
    lots: tuple[Lot, ...]
    pending_pairs: tuple[str, ...]


@dataclass(frozen=True)
class Fill:
    timestamp: int
    pair: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    basis_released: Decimal
    realized_pnl: Decimal
    lot_ids: tuple[int, ...]


@dataclass(frozen=True)
class ReplayResult:
    cash: Decimal
    lots: tuple[Lot, ...]
    fills: tuple[Fill, ...]
    samples: tuple[EquitySample, ...]
    rejected: tuple[tuple[int, str, str], ...]
    expired_orders: int
    metrics: dict
    fingerprint: str


Policy = Callable[[View], Sequence[Order]]


def validate_frames(frames: Sequence[Sequence[Quote]]) -> tuple[tuple[Quote, ...], ...]:
    result = tuple(tuple(sorted(frame, key=lambda q: q.pair)) for frame in frames)
    if len(result) < 2 or any(not frame for frame in result):
        raise ValueError("at least two nonempty frames required")
    universe = tuple(q.pair for q in result[0])
    if len(set(universe)) != len(universe):
        raise ValueError("duplicate pair")
    if len({base_asset(p) for p in universe}) != len(universe):
        raise ValueError("duplicate normalized base asset")
    if len({p.split("-")[1] for p in universe}) != 1:
        raise ValueError("single quote currency required; no implicit FX")
    previous = -1
    for frame in result:
        if tuple(q.pair for q in frame) != universe:
            raise ValueError("complete synchronized universe required; no stale marks")
        stamp = frame[0].timestamp
        if any(q.timestamp != stamp for q in frame) or stamp <= previous:
            raise ValueError(
                "timestamps must agree within frames and strictly increase"
            )
        previous = stamp
    return result


def replay(
    frames: Sequence[Sequence[Quote]],
    policy: Policy,
    config: ReplayConfig | None = None,
) -> ReplayResult:
    config = config or ReplayConfig()
    frames = validate_frames(frames)
    cash = config.initial_cash
    lots: list[Lot] = []
    fills: list[Fill] = []
    samples: list[EquitySample] = []
    rejected: list[tuple[int, str, str]] = []
    pending: list[tuple[int, Order]] = []
    next_id = 0
    realized = ZERO
    fees = ZERO
    turnover = ZERO
    fee_rate = config.fee_bps / BPS
    slip = config.slippage_bps / BPS
    for index, frame in enumerate(frames):
        quotes = {q.pair: q for q in frame}
        stamp = frame[0].timestamp
        due = [o for i, o in pending if i == index]
        pending = [(i, o) for i, o in pending if i > index]
        for order in due:
            q = quotes[order.pair]
            owned = [lot for lot in lots if lot.pair == order.pair]
            price = (
                q.ask * (ONE + slip) if order.side == "BUY" else q.bid * (ONE - slip)
            )
            if order.side == "BUY":
                if bool(owned) != order.recovery:
                    rejected.append((stamp, order.pair, "entry_recovery_ownership"))
                    continue
                # Reserve and pair caps are based on equity; actual spending is cash-only.
                equity = cash + sum(
                    (lot.quantity * quotes[lot.pair].bid for lot in lots), ZERO
                )
                pair_basis = sum((lot.basis for lot in owned), ZERO)
                budget = min(
                    order.amount,
                    max(ZERO, cash - equity * config.reserve_fraction),
                    max(ZERO, equity * config.pair_cap_fraction - pair_basis),
                )
                requested = budget / (price * (ONE + fee_rate))
                selected: list[Lot] = []
            else:
                selected = [lot for lot in owned if lot.lot_id in order.lot_ids]
                if len(selected) != len(order.lot_ids):
                    rejected.append((stamp, order.pair, "unknown_lot"))
                    continue
                requested = order.amount
                if requested > sum((lot.quantity for lot in selected), ZERO):
                    rejected.append((stamp, order.pair, "oversell"))
                    continue
            requested = (requested / q.step_base).to_integral_value(
                rounding=ROUND_DOWN
            ) * q.step_base
            # Venue minima apply to submitted orders, not each partial fill.
            if (
                requested < q.min_base
                or requested * price < q.min_quote
                or requested <= ZERO
            ):
                rejected.append((stamp, order.pair, "exchange_minimum_or_budget"))
                continue
            quantity = min(requested * config.fill_fraction, q.depth_base)
            quantity = (quantity / q.step_base).to_integral_value(
                rounding=ROUND_DOWN
            ) * q.step_base
            if quantity <= ZERO:
                rejected.append((stamp, order.pair, "no_liquidity"))
                continue
            gross = quantity * price
            fee = gross * fee_rate
            basis = ZERO
            pnl = ZERO
            ids: list[int] = []
            if order.side == "BUY":
                cash -= gross + fee
                next_id += 1
                lots.append(
                    Lot(
                        next_id,
                        order.pair,
                        quantity,
                        gross + fee,
                        order.recovery,
                        stamp,
                    )
                )
                ids.append(next_id)
            else:
                remaining = quantity
                for lot in selected:
                    take = min(remaining, lot.quantity)
                    if take == ZERO:
                        break
                    released = (
                        lot.basis
                        if take == lot.quantity
                        else lot.basis * take / lot.quantity
                    )
                    basis += released
                    ids.append(lot.lot_id)
                    lots.remove(lot)
                    if take < lot.quantity:
                        lots.append(
                            replace(
                                lot,
                                quantity=lot.quantity - take,
                                basis=lot.basis - released,
                            )
                        )
                    remaining -= take
                cash += gross - fee
                pnl = gross - fee - basis
                realized += pnl
            fees += fee
            turnover += gross
            fills.append(
                Fill(
                    stamp,
                    order.pair,
                    order.side,
                    quantity,
                    price,
                    fee,
                    basis,
                    pnl,
                    tuple(ids),
                )
            )
        committed = sum((lot.basis for lot in lots), ZERO)
        value = sum((lot.quantity * quotes[lot.pair].bid for lot in lots), ZERO)
        equity = cash + value
        # Cash + remaining basis equals initial cash + realized P&L, including fees.
        if cash < ZERO or abs(cash + committed - config.initial_cash - realized) > D(
            "1e-18"
        ):
            raise ArithmeticError("replay accounting invariant failed")
        samples.append(EquitySample(stamp, equity, committed))
        view = View(
            frames[: index + 1], cash, tuple(lots), tuple(o.pair for _, o in pending)
        )
        used = set(view.pending_pairs)
        for order in policy(view):
            if order.pair not in quotes:
                raise ValueError("order outside dataset universe")
            if order.pair in used:
                rejected.append((stamp, order.pair, "one_pending_mutation_per_pair"))
                continue
            used.add(order.pair)
            pending.append((index + config.latency_steps, order))
    metrics = asdict(
        calculate_performance_metrics(
            samples,
            realized_net_profit_quote=realized,
            turnover_quote=turnover,
            fee_drag_quote=fees,
        )
    )
    metrics.update(
        realized_pnl=realized,
        unrealized_pnl=value - committed,
        total_pnl=equity - config.initial_cash,
        open_basis=committed,
        open_bid_value=value,
        fill_count=len(fills),
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            [[asdict(q) for q in f] for f in frames], default=str, sort_keys=True
        ).encode()
    ).hexdigest()
    return ReplayResult(
        cash,
        tuple(lots),
        tuple(fills),
        tuple(samples),
        tuple(rejected),
        len(pending),
        metrics,
        fingerprint,
    )


@dataclass(frozen=True)
class Candidate:
    """Simple, explicit ablations; adaptive variants are VCAP-inspired proxies."""

    mode: str
    ticket: Decimal = D("10")
    interval: int = 6
    dip: Decimal = D("0.03")
    target: Decimal = D("0.02")
    max_age_seconds: int = 86400
    exit_cost_bps: Decimal = D("50")

    def __post_init__(self):
        if self.mode not in ("cash", "dca", "recovery", "recycling", "capital_time"):
            raise ValueError("unknown candidate")
        for field in ("ticket", "dip", "target", "exit_cost_bps"):
            object.__setattr__(self, field, number(getattr(self, field), field))
        if self.ticket <= ZERO or self.dip >= ONE or self.exit_cost_bps >= BPS:
            raise ValueError("invalid candidate size/threshold")
        if type(self.interval) is not int or self.interval < 1:
            raise ValueError("interval must be positive integer")
        if type(self.max_age_seconds) is not int or self.max_age_seconds < 1:
            raise ValueError("max_age_seconds must be positive integer")

    def __call__(self, view: View) -> tuple[Order, ...]:
        if self.mode == "cash":
            return ()
        current = view.history[-1]
        previous = (
            {q.pair: q for q in view.history[-2]} if len(view.history) > 1 else {}
        )
        orders = []
        for q in current:
            if q.pair in view.pending_pairs:
                continue
            lots = [lot for lot in view.lots if lot.pair == q.pair]
            if not lots:
                orders.append(Order(q.pair, "BUY", self.ticket))
                continue
            quantity = sum((lot.quantity for lot in lots), ZERO)
            basis = sum((lot.basis for lot in lots), ZERO)
            proceeds = quantity * q.bid * (ONE - self.exit_cost_bps / BPS)
            if proceeds >= basis * (ONE + self.target):
                orders.append(
                    Order(q.pair, "SELL", quantity, tuple(l.lot_id for l in lots))
                )
                continue
            if self.mode in ("recycling", "capital_time"):
                selected = [
                    l
                    for l in lots
                    if l.recovery
                    and l.quantity * q.bid * (ONE - self.exit_cost_bps / BPS)
                    >= l.basis * (ONE + self.target)
                ]
                if selected:
                    orders.append(
                        Order(
                            q.pair,
                            "SELL",
                            sum((l.quantity for l in selected), ZERO),
                            tuple(l.lot_id for l in selected),
                        )
                    )
                    continue
            if (
                self.mode == "capital_time"
                and q.timestamp - min(l.opened_at for l in lots) >= self.max_age_seconds
            ):
                orders.append(
                    Order(q.pair, "SELL", quantity, tuple(l.lot_id for l in lots))
                )
                continue
            if self.mode == "dca":
                if (len(view.history) - 1) % self.interval == 0:
                    orders.append(Order(q.pair, "BUY", self.ticket, recovery=True))
            elif (
                q.ask < (basis / quantity) * (ONE - self.dip)
                and q.pair in previous
                and q.bid > previous[q.pair].bid
            ):
                # Size grows with observed drawdown, bounded to 3 tickets and simulator caps.
                multiplier = (
                    min(D(3), ONE + (ONE - q.ask * quantity / basis) / self.dip)
                    if self.dip
                    else ONE
                )
                orders.append(
                    Order(q.pair, "BUY", self.ticket * multiplier, recovery=True)
                )
        # Capital-time variant prioritizes observed positive momentum when cash is scarce.
        if self.mode == "capital_time":
            now = {q.pair: q for q in current}
            orders.sort(
                key=lambda o: (
                    o.side == "SELL",
                    (
                        (now[o.pair].bid / previous[o.pair].bid - ONE)
                        if o.pair in previous
                        else ZERO
                    ),
                ),
                reverse=True,
            )
        return tuple(orders)


def load_csv(path: str | Path) -> tuple[tuple[Quote, ...], ...]:
    """Input must be time-sorted; never silently sorts away temporal errors."""
    frames: list[list[Quote]] = []
    with Path(path).open(newline="") as stream:
        for row in csv.DictReader(stream):
            quote = Quote(
                int(row["timestamp"]),
                row["pair"],
                quote_volume_24h=(
                    D(row["quote_volume_24h"]) if row.get("quote_volume_24h") else None
                ),
                **{
                    field: D(row[field])
                    for field in (
                        "bid",
                        "ask",
                        "depth_base",
                        "min_base",
                        "min_quote",
                        "step_base",
                    )
                },
            )
            if not frames or frames[-1][0].timestamp != quote.timestamp:
                frames.append([])
            frames[-1].append(quote)
    return validate_frames(frames)


def compare(frames, config: ReplayConfig) -> dict:
    exit_cost = (
        ONE - (ONE - config.slippage_bps / BPS) * (ONE - config.fee_bps / BPS)
    ) * BPS
    return {
        mode: replay(frames, Candidate(mode, exit_cost_bps=exit_cost), config)
        for mode in ("cash", "dca", "recovery", "recycling", "capital_time")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cash", default="200")
    parser.add_argument(
        "--fee-bps", required=True, help="Research input, not a current venue fee claim"
    )
    parser.add_argument("--slippage-bps", default="5")
    args = parser.parse_args()
    config = ReplayConfig(
        initial_cash=D(args.cash),
        fee_bps=D(args.fee_bps),
        slippage_bps=D(args.slippage_bps),
    )
    results = compare(load_csv(args.csv), config)
    report = {
        "schema": 1,
        "engine": "offline-replay-v1",
        "config": asdict(config),
        "warning": "Research proxies; not production VCAP or evidence of live profitability. Open positions marked at bid, not forcibly liquidated.",
        "candidate_parameters": {
            name: asdict(
                Candidate(
                    name,
                    exit_cost_bps=(
                        ONE
                        - (ONE - config.slippage_bps / BPS)
                        * (ONE - config.fee_bps / BPS)
                    )
                    * BPS,
                )
            )
            for name in results
        },
        "engine_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "results": {name: asdict(result) for name, result in results.items()},
    }
    args.output.write_text(json.dumps(report, default=str, indent=2) + "\n")


if __name__ == "__main__":
    main()
