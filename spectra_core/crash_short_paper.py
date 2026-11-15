"""Independent, transactionally durable paper book for the frozen short candidate.

Every cycle is retrospective: its observed open drives fills using only the
preceding completed candle; its completed close drives end-of-interval valuation.
The current high/close/volume/trade count never choose that interval's fill.
No exchange client, credential, spot owner or capital reservation is accepted.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from pathlib import Path
from typing import Any, Iterable, Mapping

from .crash_short_model import (
    ASSETS,
    DAILY_ADVERSE_FUNDING,
    DAY,
    ENTRY_NOTIONAL,
    INITIAL_EQUITY,
    ONE_WAY_COST,
    POLICY_ID,
    PROTOCOL_SHA256,
    RESERVE,
    STEP,
    PaperSource,
    canonical,
    fingerprint,
)
from .crash_short_signal import Bar, DailySignal, evaluate_btc_daily_signal

D = Decimal
APPLICATION_ID = 0x53504353
SCHEMA_VERSION = 1
ZERO = D("0")
MONEY_CONTEXT = Context(
    prec=34,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


class ReconciliationRequired(ValueError):
    """Conflicting, incomplete or corrupt evidence; no state transition committed."""


def decode_receipt(payload: str, digest: str) -> dict[str, Any]:
    try:
        receipt = json.loads(payload)
        if fingerprint(receipt) != digest:
            raise ValueError("receipt checksum mismatch")
        return receipt
    except (ValueError, TypeError) as exc:
        raise ReconciliationRequired(
            f"Paper receipt reconciliation failed: {exc}"
        ) from exc


def bar_dict(bar: Bar) -> dict[str, Any]:
    return {
        "timestamp": bar.timestamp,
        **{
            key: None if getattr(bar, key) is None else str(getattr(bar, key))
            for key in ("open", "high", "low", "close", "volume", "trades")
        },
    }


def _initial_state() -> dict[str, Any]:
    return {
        "last_timestamp": None,
        "signal": None,
        "episode": 0,
        "episode_start": None,
        "entered_episode": {a: None for a in ASSETS},
        "positions": {},
        "marks": {},
        "previous_valid": {a: False for a in ASSETS},
        "gross_realized_pnl": "0",
        "cost_overlay": "0",
        "adverse_funding": "0",
        "cycle_count": 0,
        "event_count": 0,
        "first_timestamp": None,
    }


class PaperShortStore:
    """One paper-only database and immutable run identity, never a spot ledger.

    BEGIN IMMEDIATE serializes concurrent writers. State, BTC observations,
    receipt, signal, fill, skipped-entry and delay evidence commit together.
    Exact replay returns the stored receipt; changed evidence fails closed.
    """

    def __init__(self, path: str | Path, source: PaperSource, *, run_id: str):
        if not run_id or len(run_id) > 128:
            raise ValueError("A nonempty bounded run_id is required")
        self.path = str(path)
        self.source = source
        self.identity = {
            "run_id": run_id,
            "source": source.to_dict(),
            "policy_id": POLICY_ID,
            "protocol_sha256": PROTOCOL_SHA256,
            "execution_mode": "PAPER",
            "schema_version": SCHEMA_VERSION,
        }
        if self.path != ":memory:":
            path = Path(path)
            if path.is_symlink():
                raise ValueError("Paper state path must not be a symlink")
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.connection.row_factory = sqlite3.Row
        try:
            with self._transaction():
                app_id = self.connection.execute("PRAGMA application_id").fetchone()[0]
                tables = {
                    r[0]
                    for r in self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if tables and (
                    app_id != APPLICATION_ID
                    or tables
                    != {"paper_state", "paper_cycles", "paper_btc", "paper_events"}
                ):
                    raise ReconciliationRequired(
                        "Database is not this isolated paper book"
                    )
                if not tables:
                    if app_id not in {0, APPLICATION_ID}:
                        raise ReconciliationRequired(
                            "Database belongs to another application"
                        )
                    self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.connection.execute(
                        "CREATE TABLE paper_state (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, payload TEXT NOT NULL)"
                    )
                    self.connection.execute(
                        "CREATE TABLE paper_cycles (timestamp INTEGER PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL, receipt_digest TEXT NOT NULL)"
                    )
                    self.connection.execute(
                        "CREATE TABLE paper_btc (timestamp INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
                    )
                    self.connection.execute(
                        "CREATE TABLE paper_events (event_id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)"
                    )
                    self.connection.execute(
                        "CREATE INDEX paper_events_by_cycle ON paper_events(timestamp)"
                    )
                    self.connection.execute(
                        "INSERT INTO paper_state VALUES (1, ?, ?)",
                        (canonical(self.identity), canonical(_initial_state())),
                    )
                row = self.connection.execute(
                    "SELECT identity FROM paper_state WHERE id=1"
                ).fetchone()
                if row is None or json.loads(row[0]) != self.identity:
                    raise ReconciliationRequired(
                        "Run/source/policy identity does not match persisted paper book"
                    )
                self._reconcile(full=True)
        except Exception:
            self.connection.close()
            raise

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _reconcile(self, *, full: bool = False) -> dict[str, Any]:
        """Reject damaged state/evidence rather than silently minting paper cash.

        This detects incomplete/corrupt writes, not malicious replacement of an
        entire database by someone with filesystem write access. SQLite owns the
        atomicity guarantee; receipt hashes independently bind the restart state.
        """
        try:
            row = self.connection.execute(
                "SELECT identity,payload FROM paper_state WHERE id=1"
            ).fetchone()
            if row is None or json.loads(row[0]) != self.identity:
                raise ValueError("identity")
            state = json.loads(row[1])
            latest = self.connection.execute(
                "SELECT timestamp,receipt,receipt_digest FROM paper_cycles ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                if state != _initial_state():
                    raise ValueError("missing cycle evidence")
            else:
                receipt = decode_receipt(latest[1], latest[2])
                if (
                    latest[0] != state["last_timestamp"]
                    or receipt["timestamp"] != latest[0] + STEP
                    or receipt.get("state_digest") != fingerprint(state)
                ):
                    raise ValueError("state does not match last committed receipt")
                actual_events = {
                    r[0]: json.loads(r[1])
                    for r in self.connection.execute(
                        "SELECT event_id,payload FROM paper_events WHERE timestamp=?",
                        (latest[0],),
                    )
                }
                if actual_events != {e["event_id"]: e for e in receipt["events"]}:
                    raise ValueError("last cycle event evidence")
                btc = self.connection.execute(
                    "SELECT payload FROM paper_btc WHERE timestamp=?", (latest[0],)
                ).fetchone()
                if btc is None or fingerprint(json.loads(btc[0])) != receipt.get(
                    "btc_digest"
                ):
                    raise ValueError("last BTC evidence")
            if full:
                count, first, last = self.connection.execute(
                    "SELECT count(*),min(timestamp),max(timestamp) FROM paper_cycles"
                ).fetchone()
                if (
                    count != state["cycle_count"]
                    or first != state["first_timestamp"]
                    or last != state["last_timestamp"]
                ):
                    raise ValueError("cycle continuity")
                if count and count != (last - first) // STEP + 1:
                    raise ValueError("missing historical cycle")
                if (
                    self.connection.execute(
                        "SELECT count(*) FROM paper_events"
                    ).fetchone()[0]
                    != state["event_count"]
                ):
                    raise ValueError("event count")
                if self.connection.execute("SELECT count(*) FROM paper_btc").fetchone()[
                    0
                ] != min(count, 8 * DAY // STEP + 1):
                    raise ValueError("BTC history count")
            return state
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            raise ReconciliationRequired(
                f"Paper state/evidence reconciliation failed: {exc}"
            ) from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PaperShortStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _read_snapshot(self):
        self.connection.execute("BEGIN")
        try:
            yield
        finally:
            self.connection.rollback()

    @classmethod
    def inspect(cls, path: str | Path) -> dict[str, Any]:
        """A read-only snapshot with the same reconciliation as writer restart."""
        connection = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            # Build only a read view; __init__ is intentionally not invoked, so
            # inspection cannot initialize schema or create directories/files.
            view = cls.__new__(cls)
            view.connection = connection
            view.path = str(path)
            with view._read_snapshot():
                if (
                    connection.execute("PRAGMA application_id").fetchone()[0]
                    != APPLICATION_ID
                ):
                    raise ReconciliationRequired("Not an isolated paper-short database")
                row = connection.execute(
                    "SELECT identity FROM paper_state WHERE id=1"
                ).fetchone()
                if row is None:
                    raise ReconciliationRequired("Missing paper identity")
                identity = json.loads(row[0])
                view.source = PaperSource(**identity["source"])
                view.identity = {
                    "run_id": identity["run_id"],
                    "source": view.source.to_dict(),
                    "policy_id": POLICY_ID,
                    "protocol_sha256": PROTOCOL_SHA256,
                    "execution_mode": "PAPER",
                    "schema_version": SCHEMA_VERSION,
                }
                view._reconcile(full=True)
                return view._status_unlocked()
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            raise ReconciliationRequired(
                f"Read-only paper reconciliation failed: {exc}"
            ) from exc
        finally:
            connection.close()

    def read_events(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._read_snapshot():
            self._reconcile(full=True)
            rows = self.connection.execute(
                "SELECT e.event_id,e.payload,c.receipt,c.receipt_digest FROM paper_events e LEFT JOIN paper_cycles c ON e.timestamp=c.timestamp"
                + (" WHERE e.kind=?" if kind else "")
                + " ORDER BY e.timestamp,e.event_id",
                (kind,) if kind else (),
            )
            result = []
            for row in rows:
                receipt = decode_receipt(row[2], row[3])
                event = json.loads(row[1])
                if event != {e["event_id"]: e for e in receipt["events"]}.get(row[0]):
                    raise ReconciliationRequired("Event does not match its receipt")
                result.append(event)
            return result

    def read_receipts(self) -> Iterable[dict[str, Any]]:
        with self._read_snapshot():
            self._reconcile(full=True)
            for row in self.connection.execute(
                "SELECT receipt,receipt_digest FROM paper_cycles ORDER BY timestamp"
            ):
                yield decode_receipt(row[0], row[1])

    def _status_unlocked(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT receipt,receipt_digest FROM paper_cycles ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if row:
            return decode_receipt(row[0], row[1])
        return {
            **self.identity,
            "status": "WARMING_UP",
            "account": _account(_initial_state(), 0, {}),
        }

    def status(self) -> dict[str, Any]:
        with self._read_snapshot():
            self._reconcile(full=True)
            return self._status_unlocked()

    def process_cycle(
        self,
        timestamp: int,
        bars: Mapping[str, Bar],
        *,
        control: str = "RUN",
    ) -> dict[str, Any]:
        """Process one observed interval, atomically, using the frozen policy.

        control=PAUSE or CLOSE_ONLY blocks new entries; existing positions still
        mark and obey original exits. CLOSE_ONLY irreversibly requests a paper
        close, first eligible observed open >= five minutes later. Resume never
        cancels such a close. This operational override is identified in evidence
        and is outside the unchanged historical performance claim.
        """
        self.source.validate_time(timestamp)
        if control not in {"RUN", "PAUSE", "CLOSE_ONLY"}:
            raise ValueError("control must be RUN, PAUSE or CLOSE_ONLY")
        if set(bars) != set(ASSETS):
            raise ValueError(
                "Exactly BTC and ETH observations are required, including missing bars"
            )
        if any(
            not isinstance(b, Bar) or b.timestamp != timestamp for b in bars.values()
        ):
            raise ValueError("Observation timestamps must match the cycle")
        payload = {
            "timestamp": timestamp,
            "bars": {a: bar_dict(bars[a]) for a in ASSETS},
            "control": control,
        }
        digest = fingerprint(payload)
        with self._transaction(), localcontext(MONEY_CONTEXT):
            state = self._reconcile()
            previous = self.connection.execute(
                "SELECT digest, receipt, receipt_digest FROM paper_cycles WHERE timestamp=?",
                (timestamp,),
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ReconciliationRequired(
                        "Duplicate timestamp has conflicting observation/control evidence"
                    )
                return decode_receipt(previous[1], previous[2])
            last = state["last_timestamp"]
            if last is None:
                if timestamp % DAY:
                    raise ReconciliationRequired(
                        "Start at UTC midnight to preserve daily episode history"
                    )
            elif timestamp != last + STEP:
                raise ReconciliationRequired(
                    "Intervals must be contiguous; submit explicit missing observations for gaps"
                )
            events: list[dict[str, Any]] = []
            if timestamp % DAY == 0:
                history = []
                for raw in self.connection.execute(
                    "SELECT b.payload,c.receipt,c.receipt_digest FROM paper_btc b LEFT JOIN paper_cycles c ON b.timestamp=c.timestamp WHERE b.timestamp>=? ORDER BY b.timestamp",
                    (timestamp - 8 * DAY,),
                ):
                    observed = json.loads(raw[0])
                    if raw[1] is None or fingerprint(observed) != decode_receipt(
                        raw[1], raw[2]
                    ).get("btc_digest"):
                        raise ReconciliationRequired(
                            "BTC history does not match committed input evidence"
                        )
                    history.append(Bar(**observed))
                signal = evaluate_btc_daily_signal(history, timestamp)
                prior_label = state["signal"]["label"] if state["signal"] else None
                state["signal"] = signal.to_dict()
                events.append({"kind": "SIGNAL", **state["signal"]})
                if signal.label == "CRASH" and prior_label != "CRASH":
                    state["episode"] += 1
                    state["episode_start"] = timestamp
                if signal.label != "CRASH" and prior_label == "CRASH":
                    for position in state["positions"].values():
                        if position["exit_requested_at"] is None:
                            position["exit_requested_at"] = timestamp
                            position["exit_reason"] = "DAILY_CRASH_CEASED"
            if control == "CLOSE_ONLY":
                for position in state["positions"].values():
                    if position["exit_requested_at"] is None:
                        position["exit_requested_at"] = timestamp
                        position["exit_reason"] = "OPERATIONAL_CLOSE_ONLY"
            exited: set[str] = set()
            for asset in ASSETS:
                position = state["positions"].get(asset)
                if position is None or position["exit_requested_at"] is None:
                    continue
                if timestamp < position["exit_requested_at"] + STEP:
                    continue
                price = bars[asset].valid_open
                if state["previous_valid"][asset] and price is not None:
                    quantity = D(position["quantity"])
                    notional = quantity * price
                    pnl = quantity * (D(position["entry_price"]) - price)
                    fee = notional * ONE_WAY_COST
                    state["gross_realized_pnl"] = str(
                        D(state["gross_realized_pnl"]) + pnl
                    )
                    state["cost_overlay"] = str(D(state["cost_overlay"]) + fee)
                    events.append(
                        _fill(
                            asset,
                            "EXIT",
                            timestamp,
                            position["exit_requested_at"],
                            position,
                            price,
                            notional,
                            fee,
                            pnl,
                        )
                    )
                    del state["positions"][asset]
                    exited.add(asset)
                else:
                    events.append(
                        {
                            "kind": "EXIT_DELAY",
                            "asset": asset,
                            "reason": "NO_ELIGIBLE_OBSERVED_OPEN",
                            "requested_at": position["exit_requested_at"],
                        }
                    )
            # Match the original gross (not cost/funding) current-open gate.
            gate_equity: Decimal | None = INITIAL_EQUITY + D(
                state["gross_realized_pnl"]
            )
            gate_stale: list[str] = []
            for asset, position in state["positions"].items():
                price = bars[asset].valid_open
                if price is None:
                    mark = state["marks"].get(asset)
                    if mark is None:
                        gate_equity = None
                        break
                    price = D(mark["price"])
                    gate_stale.append(asset)
                gate_equity += D(position["quantity"]) * (
                    D(position["entry_price"]) - price
                )
            signal = state["signal"]
            fresh = signal["return_1d"] is not None and D(
                str(signal["return_1d"])
            ) <= D("-0.05")
            if (
                signal["label"] == "CRASH"
                and timestamp >= state["episode_start"] + STEP
            ):
                for asset in ASSETS:
                    if (
                        asset in state["positions"]
                        or asset in exited
                        or state["entered_episode"][asset] == state["episode"]
                    ):
                        continue
                    price = bars[asset].valid_open
                    reason = (
                        "PAUSED"
                        if control != "RUN"
                        else (
                            "FRESH_24H_GATE_FALSE"
                            if not fresh
                            else (
                                "MISSING_DECISIVE_MARK"
                                if gate_equity is None
                                else (
                                    "GROSS_EQUITY_GATE"
                                    if gate_equity <= RESERVE
                                    else (
                                        "NO_ELIGIBLE_OBSERVED_OPEN"
                                        if not state["previous_valid"][asset]
                                        or price is None
                                        else "FILLED"
                                    )
                                )
                            )
                        )
                    )
                    events.append(
                        {
                            "kind": "ENTRY_OPPORTUNITY",
                            "asset": asset,
                            "episode": state["episode"],
                            "reason": reason,
                            "daily_decision_at": signal["decision_at"],
                            "return_1d": signal["return_1d"],
                            "gross_gate_equity": (
                                None if gate_equity is None else str(gate_equity)
                            ),
                            "gate_carried_marks": gate_stale,
                        }
                    )
                    if reason == "FILLED":
                        quantity = ENTRY_NOTIONAL / price
                        position = {
                            "position_id": f"paper:{self.identity['run_id']}:{state['episode']}:{asset}",
                            "asset": asset,
                            "episode": state["episode"],
                            "entry_at": timestamp,
                            "entry_signal_at": state["episode_start"],
                            "entry_price": str(price),
                            "quantity": str(quantity),
                            "reference_entry_notional": str(ENTRY_NOTIONAL),
                            "exit_requested_at": None,
                            "exit_reason": None,
                        }
                        state["positions"][asset] = position
                        state["entered_episode"][asset] = state["episode"]
                        fee = ENTRY_NOTIONAL * ONE_WAY_COST
                        state["cost_overlay"] = str(D(state["cost_overlay"]) + fee)
                        events.append(
                            _fill(
                                asset,
                                "ENTRY",
                                timestamp,
                                state["episode_start"],
                                position,
                                price,
                                ENTRY_NOTIONAL,
                                fee,
                                ZERO,
                            )
                        )
            funding_sources: dict[str, Any] = {}
            for asset, position in state["positions"].items():
                if position["entry_at"] == timestamp:
                    funding_price, age = D(position["entry_price"]), 0
                else:
                    mark = state["marks"].get(asset)
                    if mark is None:
                        raise ReconciliationRequired(
                            "Open liability has no prior valuation mark"
                        )
                    funding_price, age = D(mark["price"]), timestamp - mark["end_at"]
                charge = (
                    D(position["quantity"])
                    * funding_price
                    * DAILY_ADVERSE_FUNDING
                    / D(288)
                )
                state["adverse_funding"] = str(D(state["adverse_funding"]) + charge)
                funding_sources[asset] = {
                    "price": str(funding_price),
                    "age_seconds": age,
                    "debit": str(charge),
                }
            for asset in ASSETS:
                bar = bars[asset]
                if bar.is_valid:
                    state["marks"][asset] = {
                        "price": str(bar.close),
                        "bar_open_at": timestamp,
                        "end_at": timestamp + STEP,
                    }
                state["previous_valid"][asset] = bar.is_valid
            state["last_timestamp"] = timestamp
            if state["first_timestamp"] is None:
                state["first_timestamp"] = timestamp
            state["cycle_count"] += 1
            state["event_count"] += len(events)
            account = _account(state, timestamp + STEP, bars)
            status = (
                "RECONCILIATION_REQUIRED"
                if account["unvalued_positions"]
                else (
                    "EXIT_PENDING"
                    if any(
                        p["exit_requested_at"] is not None
                        for p in state["positions"].values()
                    )
                    else (
                        "STALE_DATA"
                        if account["stale_open_position_marks"]
                        or not all(b.is_valid for b in bars.values())
                        else (
                            "WARMING_UP"
                            if signal["label"] == "UNKNOWN"
                            else "PAPER_READY"
                        )
                    )
                )
            )
            receipt = {
                **self.identity,
                "status": status,
                "control": control,
                "timestamp": timestamp + STEP,
                "input_digest": digest,
                "state_digest": fingerprint(state),
                "btc_digest": fingerprint(bar_dict(bars["BTC"])),
                "signal": signal,
                "episode": state["episode"] if signal["label"] == "CRASH" else 0,
                "positions": state["positions"],
                "account": account,
                "funding_sources": funding_sources,
                "events": events,
            }
            for index, event in enumerate(events):
                event["timestamp"] = timestamp
                event["event_id"] = f"{self.identity['run_id']}:{timestamp}:{index}"
                self.connection.execute(
                    "INSERT INTO paper_events VALUES (?, ?, ?, ?)",
                    (event["event_id"], timestamp, event["kind"], canonical(event)),
                )
            self.connection.execute(
                "INSERT INTO paper_btc VALUES (?, ?)",
                (timestamp, canonical(bar_dict(bars["BTC"]))),
            )
            # Retain exactly enough raw observations for the next 7-day anchor.
            self.connection.execute(
                "DELETE FROM paper_btc WHERE timestamp<?", (timestamp - 8 * DAY,)
            )
            self.connection.execute(
                "UPDATE paper_state SET payload=? WHERE id=1", (canonical(state),)
            )
            self.connection.execute(
                "INSERT INTO paper_cycles VALUES (?, ?, ?, ?)",
                (timestamp, digest, canonical(receipt), fingerprint(receipt)),
            )
            return receipt


def _fill(
    asset: str,
    action: str,
    timestamp: int,
    decision: int,
    position: dict[str, Any],
    price: Decimal,
    notional: Decimal,
    fee: Decimal,
    pnl: Decimal,
) -> dict[str, Any]:
    return {
        "kind": "FILL",
        "asset": asset,
        "action": action,
        "position_id": position["position_id"],
        "episode": position["episode"],
        "decision_timestamp": decision,
        "fill_timestamp": timestamp,
        "observed_open": str(price),
        "quantity": position["quantity"],
        "reference_notional": str(notional),
        "cost_overlay": str(fee),
        "gross_realized_pnl": str(pnl),
        "exit_reason": position["exit_reason"] if action == "EXIT" else None,
        "previous_observed_bar_valid": True,
        "current_open_observed": True,
    }


def _account(state: dict[str, Any], at: int, bars: Mapping[str, Bar]) -> dict[str, Any]:
    unrealized, high_pnl, notional = ZERO, ZERO, ZERO
    stale, max_age, unvalued = 0, 0, []
    marked_positions = {}
    for asset, position in state["positions"].items():
        mark = state["marks"].get(asset)
        if mark is None:
            unvalued.append(asset)
            continue
        price, quantity, entry = (
            D(mark["price"]),
            D(position["quantity"]),
            D(position["entry_price"]),
        )
        age = at - mark["end_at"]
        pnl = quantity * (entry - price)
        unrealized += pnl
        notional += quantity * price
        high = bars[asset].high if asset in bars and bars[asset].is_valid else price
        high_pnl += quantity * (entry - high)
        stale += int(age > 0)
        max_age = max(max_age, age)
        marked_positions[asset] = {
            **mark,
            "age_seconds": age,
            "unrealized_pnl": str(pnl),
            "marked_notional": str(quantity * price),
        }
    realized, cost, funding = (
        D(state[k]) for k in ("gross_realized_pnl", "cost_overlay", "adverse_funding")
    )
    cash = INITIAL_EQUITY + realized
    result = {
        "initial_equity": str(INITIAL_EQUITY),
        "gross_realized_pnl": str(realized),
        "gross_open_pnl": None if unvalued else str(unrealized),
        "cost_overlay": str(cost),
        "adverse_funding": str(funding),
        "gross_cash": str(cash),
        "cost_cash": str(cash - cost),
        "funding_cash": str(cash - cost - funding),
        "active_positions": len(state["positions"]),
        "aggregate_reference_entry_notional": str(
            ENTRY_NOTIONAL * len(state["positions"])
        ),
        "marked_short_notional": None if unvalued else str(notional),
        "policy_undeployed_reserve": str(RESERVE),
        "reserve_deployed": "0",
        "repair_orders": 0,
        "margin_requirement": "NOT_MODELED",
        "liquidation": "NOT_MODELED",
        "stale_open_position_marks": stale,
        "maximum_open_mark_age_seconds": max_age,
        "unvalued_positions": unvalued,
        "marks": marked_positions,
        "valuation": "OBSERVED_CLOSE_OR_EXPLICITLY_AGED_LAST_VALID_CLOSE",
        "execution_model": "HYPOTHETICAL_FRACTIONAL_SPOT_PRICE_PATH",
    }
    for mode, deductions in (
        ("gross", ZERO),
        ("cost", cost),
        ("funding", cost + funding),
    ):
        equity = None if unvalued else cash + unrealized - deductions
        result[f"{mode}_equity"] = None if equity is None else str(equity)
        result[f"{mode}_observed_high_stress_equity"] = (
            None if unvalued else str(cash + high_pnl - deductions)
        )
        result[f"{mode}_unfunded_liability"] = (
            None if equity is None else str(max(ZERO, -equity))
        )
    return result
