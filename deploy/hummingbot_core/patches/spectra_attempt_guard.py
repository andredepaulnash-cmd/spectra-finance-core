"""Durable, non-releasable claims made before Spectra backend submissions.

This journal is a replay barrier, not capital authority or venue finality.
Missing storage never creates a fresh journal during a trading call. Initial
creation is an explicit offline setup operation; preserve it across rebuilds.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

SPECTRA_EXECUTION_CONTRACT = "spectra-single-attempt-v1"
SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 65536
MAX_ANCHOR_BYTES = 16 * 1024 * 1024
_BOUND_JOURNAL = None
_BOUND_ANCHOR_PREFIX = ()
_BINDING_LOCK = threading.Lock()
_DDL = (
    "CREATE TABLE metadata(journal_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL)",
    "CREATE TABLE claims(namespace TEXT NOT NULL, attempt_id TEXT NOT NULL, scope TEXT NOT NULL, uncertain INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(namespace, attempt_id))",
    "CREATE TABLE identities(namespace TEXT NOT NULL, attempt_id TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(namespace, attempt_id, kind, identity), FOREIGN KEY(namespace, attempt_id) REFERENCES claims(namespace, attempt_id))",
    "CREATE TABLE snapshots(sequence INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, attempt_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(namespace, attempt_id, digest), FOREIGN KEY(namespace, attempt_id) REFERENCES claims(namespace, attempt_id))",
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/+=-]{1,128}\Z")
_SNAPSHOT_FIELDS = {
    "tracked_orders",
    "held_position_orders",
    "executed_amount_base",
    "filled_amount_quote",
    "cum_fees_quote",
    "reconciliation_required",
    "venue_finality_established",
    "accounting_available",
    "accounting_uncertainty_reason",
    "venue_fill_watermarks",
}


class AttemptJournalError(RuntimeError):
    """Storage/configuration uncertainty prohibits submission."""


class AttemptAlreadyClaimed(AttemptJournalError):
    """This attempt has already consumed its one submission claim."""


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AttemptJournalError("Invalid execution identity")
    return value


def _number(value: Any, *, positive: bool = True) -> str:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal, int)):
        raise AttemptJournalError("Invalid exact execution amount")
    try:
        number = Decimal(value)
    except Exception:
        raise AttemptJournalError("Invalid exact execution amount") from None
    digits = number.as_tuple()
    if not number.is_finite() or len(digits.digits) > 128 or abs(digits.exponent) > 128:
        raise AttemptJournalError("Invalid exact execution amount")
    if positive and number <= 0:
        raise AttemptJournalError("Execution amount must be positive")
    return format(number, "f")


def _side(value: Any) -> str:
    name = getattr(value, "name", value)
    if name not in ("BUY", "SELL"):
        raise AttemptJournalError("Invalid execution direction")
    return name


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AttemptJournalError("A durable journal path is required")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AttemptJournalError("The journal path must be absolute")
    try:
        # Do not follow a symlink to another journal or a replacement mount.
        for part in (path.parent, *path.parent.parents):
            if part.is_symlink():
                raise AttemptJournalError("Journal directories must not be symlinks")
        parent = path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
            raise AttemptJournalError("A private owned journal directory is required")
        if stat.S_IMODE(parent.st_mode) & 0o077:
            raise AttemptJournalError("A private owned journal directory is required")
    except AttemptJournalError:
        raise
    except Exception:
        raise AttemptJournalError("The journal directory is unavailable") from None
    return path


def _open_file(path: Path) -> tuple[int, tuple[int, int]]:
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise AttemptJournalError("A private regular journal is required")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AttemptJournalError("A private regular journal is required")
        return fd, (info.st_dev, info.st_ino)
    except Exception:
        if fd is not None:
            os.close(fd)
        raise AttemptJournalError(
            "Existing private execution journal is unavailable"
        ) from None


def _verify_file(path: Path) -> tuple[int, int]:
    fd, identity = _open_file(path)
    os.close(fd)
    return identity


def _anchor_path(path: Path) -> Path:
    return path.with_name(path.name + ".claims")


def _line(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _anchor_facts(
    fd: int, connection: sqlite3.Connection, journal_id: str
) -> tuple[str, tuple[str, ...]]:
    os.lseek(fd, 0, os.SEEK_SET)
    pieces, size = [], 0
    while True:
        part = os.read(fd, 65536)
        if not part:
            break
        size += len(part)
        if size > MAX_ANCHOR_BYTES:
            raise AttemptJournalError("Execution claim anchor exceeds its bound")
        pieces.append(part)
    raw = b"".join(pieces)
    if not raw.endswith(b"\n"):
        raise AttemptJournalError("Execution claim anchor is incomplete")
    lines = raw.splitlines(keepends=True)
    expected_header = _line(
        {"journal_id": journal_id, "schema_version": SCHEMA_VERSION}
    )
    if not lines or lines[0] != expected_header:
        raise AttemptJournalError("Execution claim anchor identity mismatch")
    previous = hashlib.sha256(lines[0]).hexdigest()
    tips = [previous]
    claims = {}
    for line in lines[1:]:
        if len(line) > 1024:
            raise AttemptJournalError("Execution claim anchor record exceeds its bound")
        value = json.loads(line)
        if not isinstance(value, dict) or set(value) != {
            "namespace",
            "attempt_id",
            "scope_sha256",
            "previous",
        }:
            raise AttemptJournalError("Invalid execution claim anchor")
        if (
            value["namespace"] not in ("executor", "kraken-order")
            or value["previous"] != previous
        ):
            raise AttemptJournalError("Execution claim anchor chain mismatch")
        key = (value["namespace"], _identifier(value["attempt_id"]))
        if (
            key in claims
            or not isinstance(value["scope_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["scope_sha256"])
        ):
            raise AttemptJournalError("Invalid execution claim anchor identity")
        if line != _line(value):
            raise AttemptJournalError("Execution claim anchor is not canonical")
        claims[key] = value["scope_sha256"]
        previous = hashlib.sha256(line).hexdigest()
        tips.append(previous)
    rows = connection.execute(
        "SELECT namespace, attempt_id, scope FROM claims"
    ).fetchall()
    observed = {
        (namespace, attempt_id): hashlib.sha256(scope.encode()).hexdigest()
        for namespace, attempt_id, scope in rows
    }
    if len(observed) != len(rows) or observed != claims:
        raise AttemptJournalError("Execution claim history disagrees with its anchor")
    return previous, tuple(tips)


class _JournalConnection(sqlite3.Connection):
    def close(self):
        try:
            super().close()
        finally:
            for descriptor in getattr(self, "_guard_fds", ()):
                os.close(descriptor)
            self._guard_fds = ()

    def verify_identity(self):
        _path(str(self._guard_path))
        if (
            _verify_file(self._guard_path),
            _verify_file(_anchor_path(self._guard_path)),
        ) != self._guard_inodes:
            raise AttemptJournalError("Execution journal was replaced")

    def verify_history(self):
        global _BOUND_ANCHOR_PREFIX
        previous, tips = _anchor_facts(self._guard_fds[1], self, self._guard_journal_id)
        with _BINDING_LOCK:
            if tips[: len(_BOUND_ANCHOR_PREFIX)] != _BOUND_ANCHOR_PREFIX:
                raise AttemptJournalError("Execution claim history was rolled back")
            _BOUND_ANCHOR_PREFIX = tips
        self._guard_previous = previous


def _connect(
    path: Path, journal_id: str, *, exclusive: bool = False
) -> _JournalConnection:
    global _BOUND_JOURNAL
    path = _path(str(path))
    db_fd, db_identity = _open_file(path)
    anchor_fd = None
    try:
        anchor_fd, anchor_identity = _open_file(_anchor_path(path))
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(
                    anchor_fd,
                    (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
                )
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AttemptJournalError("Execution journal is busy")
                time.sleep(0.01)
        connection = sqlite3.connect(
            path.as_uri() + "?mode=rw",
            uri=True,
            timeout=2.0,
            factory=_JournalConnection,
        )
        connection._guard_fds = (db_fd, anchor_fd)
        connection._guard_path = path
        connection._guard_journal_id = journal_id
        connection._guard_inodes = (db_identity, anchor_identity)
        connection.verify_identity()
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute(
            "SELECT journal_id, schema_version FROM metadata"
        ).fetchall()
        if row != [(journal_id, SCHEMA_VERSION)]:
            raise AttemptJournalError("Execution journal identity mismatch")
        tables = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if {value[0] for value in tables} != set(_DDL) or len(tables) != len(_DDL):
            raise AttemptJournalError("Execution journal schema mismatch")
        if connection.execute("PRAGMA user_version").fetchone() != (SCHEMA_VERSION,):
            raise AttemptJournalError("Execution journal schema version mismatch")
        if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            raise AttemptJournalError(
                "Execution journal requires durable rollback mode"
            )
        binding = (str(path), journal_id, db_identity, anchor_identity)
        with _BINDING_LOCK:
            if _BOUND_JOURNAL is not None and _BOUND_JOURNAL != binding:
                raise AttemptJournalError(
                    "Execution journal cannot rotate within a process"
                )
            _BOUND_JOURNAL = binding
        connection.verify_history()
        return connection
    except Exception as error:
        if "connection" in locals():
            connection.close()
        else:
            os.close(db_fd)
            if anchor_fd is not None:
                os.close(anchor_fd)
        if isinstance(error, AttemptJournalError):
            raise
        raise AttemptJournalError("Execution journal cannot be verified") from None


def initialize_journal(path: str, *, journal_id: str | None = None) -> str:
    """Explicit offline initialization. Never overwrites an existing journal."""
    target = _path(path)
    identity = uuid.uuid4().hex if journal_id is None else journal_id
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise AttemptJournalError("Invalid journal identity")
    try:
        descriptor = os.open(
            target, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        anchor = os.open(
            _anchor_path(target),
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(
                anchor,
                _line({"journal_id": identity, "schema_version": SCHEMA_VERSION}),
            )
            os.fsync(anchor)
        finally:
            os.close(anchor)
        with sqlite3.connect(target, timeout=2.0) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            for statement in _DDL:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?)", (identity, SCHEMA_VERSION)
            )
            connection.commit()
        # Persist directory entry too; a committed claim later uses SQLite FULL sync.
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        # Keep a failed/incomplete file for operator inspection, never silently reset.
        raise AttemptJournalError(
            "Journal initialization failed or file already exists"
        ) from None
    return identity


def _configured() -> tuple[Path, str]:
    target = _path(os.environ.get("SPECTRA_ATTEMPT_JOURNAL"))
    identity = os.environ.get("SPECTRA_ATTEMPT_JOURNAL_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise AttemptJournalError("A configured journal identity is required")
    return target, identity


def verify_journal() -> None:
    target, identity = _configured()
    connection = _connect(target, identity)
    connection.close()


def _json_value(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        budget = [8192]
    budget[0] -= 1
    if depth > 24 or budget[0] < 0:
        raise AttemptJournalError("Execution snapshot exceeds structural bounds")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return _number(value, positive=False)
    if isinstance(value, (float, int)):
        if isinstance(value, float) and not math.isfinite(value):
            raise AttemptJournalError("Non-finite execution snapshot")
        if isinstance(value, int) and value.bit_length() > 1024:
            raise AttemptJournalError("Execution snapshot integer is too large")
        return value
    if isinstance(value, str):
        if len(value) > 2048:
            raise AttemptJournalError("Execution snapshot string is too large")
        return value
    if isinstance(value, list):
        return [_json_value(item, depth + 1, budget) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise AttemptJournalError("Invalid execution snapshot key")
        return {
            key: _json_value(item, depth + 1, budget) for key, item in value.items()
        }
    raise AttemptJournalError("Unsupported execution snapshot value")


@dataclass(frozen=True)
class AttemptReservation:
    path: Path
    journal_id: str
    namespace: str
    attempt_id: str

    def _write(self, statement: str, parameters: tuple[Any, ...]) -> None:
        connection = _connect(self.path, self.journal_id, exclusive=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM claims WHERE namespace=? AND attempt_id=?",
                (self.namespace, self.attempt_id),
            ).fetchone():
                raise AttemptJournalError("Execution claim is missing")
            connection.execute(statement, parameters)
            connection.commit()
            connection.verify_identity()
            connection.verify_history()
        except Exception:
            connection.rollback()
            raise AttemptJournalError(
                "Execution evidence could not be persisted"
            ) from None
        finally:
            connection.close()

    def _record_identity(self, kind: str, identity: str) -> None:
        self._write(
            "INSERT OR IGNORE INTO identities VALUES (?, ?, ?, ?)",
            (self.namespace, self.attempt_id, kind, _identifier(identity)),
        )

    def record_order(self, order_id: str) -> None:
        self._record_identity("client_order_id", order_id)

    def record_exchange_order(self, order_id: str) -> None:
        self._record_identity("exchange_order_id", order_id)

    def mark_uncertain(self) -> None:
        # No method clears uncertainty or releases the claim.
        self._write(
            "UPDATE claims SET uncertain=1 WHERE namespace=? AND attempt_id=?",
            (self.namespace, self.attempt_id),
        )

    def record_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict) or not set(snapshot) <= _SNAPSHOT_FIELDS:
            raise AttemptJournalError("Unsupported execution snapshot fields")
        if snapshot.get("venue_finality_established") is not False:
            raise AttemptJournalError("A snapshot cannot grant venue finality")
        if (
            "accounting_available" in snapshot
            and type(snapshot["accounting_available"]) is not bool
        ):
            raise AttemptJournalError("Accounting availability must be a boolean")
        if "accounting_uncertainty_reason" in snapshot and not isinstance(
            snapshot["accounting_uncertainty_reason"], str
        ):
            raise AttemptJournalError("Accounting uncertainty must have a text reason")
        if "venue_fill_watermarks" in snapshot:
            watermarks = snapshot["venue_fill_watermarks"]
            if not isinstance(watermarks, dict) or len(watermarks) > 256:
                raise AttemptJournalError("Invalid venue fill watermarks")
            for client_id, value in watermarks.items():
                _identifier(client_id)
                if (
                    not isinstance(value, str)
                    or _number(value, positive=False) != value
                    or Decimal(value) < 0
                ):
                    raise AttemptJournalError(
                        "Venue fill watermark must be canonical and nonnegative"
                    )
        payload = json.dumps(
            _json_value(snapshot),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise AttemptJournalError("Execution snapshot exceeds byte limit")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._write(
            "INSERT OR IGNORE INTO snapshots(namespace, attempt_id, digest, payload) VALUES (?, ?, ?, ?)",
            (self.namespace, self.attempt_id, digest, payload),
        )

    def describe(self) -> dict[str, Any]:
        return read_claim(
            namespace=self.namespace,
            attempt_id=self.attempt_id,
            path=str(self.path),
            journal_id=self.journal_id,
        )


def _read_claim(
    *,
    namespace: str,
    attempt_id: str,
    path: str | None = None,
    journal_id: str | None = None,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    if namespace not in ("executor", "kraken-order"):
        raise AttemptJournalError("Invalid claim namespace")
    if path is None:
        target, identity = _configured()
    else:
        target, identity = _path(path), journal_id
    connection = _connect(target, identity, exclusive=True)
    try:
        row = connection.execute(
            "SELECT scope, uncertain FROM claims WHERE namespace=? AND attempt_id=?",
            (namespace, _identifier(attempt_id)),
        ).fetchone()
        if row is None:
            if missing_ok:
                return None
            raise AttemptJournalError("Execution claim is missing")
        identities = connection.execute(
            "SELECT kind, identity FROM identities WHERE namespace=? AND attempt_id=? ORDER BY kind, identity",
            (namespace, attempt_id),
        ).fetchall()
        latest = connection.execute(
            "SELECT payload FROM snapshots WHERE namespace=? AND attempt_id=? ORDER BY sequence DESC LIMIT 1",
            (namespace, attempt_id),
        ).fetchone()
        return {
            "namespace": namespace,
            "attempt_id": attempt_id,
            "scope": json.loads(row[0]),
            "submission_claimed": True,
            "uncertain": bool(row[1]),
            "client_order_ids": [
                value for kind, value in identities if kind == "client_order_id"
            ],
            "exchange_order_ids": [
                value for kind, value in identities if kind == "exchange_order_id"
            ],
            "snapshot": json.loads(latest[0]) if latest else None,
            "venue_finality_established": False,
        }
    except AttemptJournalError:
        raise
    except Exception:
        raise AttemptJournalError("Execution evidence cannot be read") from None
    finally:
        connection.close()


def read_claim(
    *,
    namespace: str,
    attempt_id: str,
    path: str | None = None,
    journal_id: str | None = None,
) -> dict[str, Any]:
    return _read_claim(
        namespace=namespace, attempt_id=attempt_id, path=path, journal_id=journal_id
    )


def find_executor_claim(executor_id: str) -> dict[str, Any] | None:
    """Return proven absence only after verifying the configured journal history."""
    return _read_claim(namespace="executor", attempt_id=executor_id, missing_ok=True)


@dataclass(frozen=True)
class ExecutorEvidence:
    """Accounting checkpoint access to an existing spent attempt, with no new IDs."""

    _reservation: AttemptReservation

    def record_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._reservation.record_snapshot(snapshot)

    def mark_uncertain(self) -> None:
        self._reservation.mark_uncertain()

    def describe(self) -> dict[str, Any]:
        return self._reservation.describe()


def open_executor_evidence(executor_id: str) -> ExecutorEvidence:
    """Open verified existing accounting evidence; never create or reclaim authority."""
    target, identity = _configured()
    read_claim(
        namespace="executor",
        attempt_id=executor_id,
        path=str(target),
        journal_id=identity,
    )
    return ExecutorEvidence(
        AttemptReservation(target, identity, "executor", executor_id)
    )


def _claim(
    namespace: str, attempt_id: str, scope: dict[str, Any]
) -> AttemptReservation:
    identifier = _identifier(attempt_id)
    target, identity = _configured()
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"), allow_nan=False)
    connection = _connect(target, identity, exclusive=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO claims(namespace, attempt_id, scope) VALUES (?, ?, ?)",
            (namespace, identifier, payload),
        )
        connection.commit()  # Must finish before the caller can reach transport.
        anchor_fd = connection._guard_fds[1]
        row = _line(
            {
                "namespace": namespace,
                "attempt_id": identifier,
                "scope_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "previous": connection._guard_previous,
            }
        )
        if os.fstat(anchor_fd).st_size + len(row) > MAX_ANCHOR_BYTES:
            raise AttemptJournalError("Execution claim anchor exceeds its bound")
        os.lseek(anchor_fd, 0, os.SEEK_END)
        written = 0
        while written < len(row):
            count = os.write(anchor_fd, row[written:])
            if count <= 0:
                raise AttemptJournalError("Execution claim anchor write failed")
            written += count
        os.fsync(anchor_fd)
        connection.verify_identity()
        connection.verify_history()
    except sqlite3.IntegrityError:
        connection.rollback()
        raise AttemptAlreadyClaimed(
            "Execution attempt already claimed; reconciliation required"
        ) from None
    except Exception:
        connection.rollback()
        raise AttemptJournalError(
            "Execution attempt cannot be durably claimed"
        ) from None
    finally:
        connection.close()
    return AttemptReservation(target, identity, namespace, identifier)


def claim_executor(
    *,
    executor_id: str,
    connector_name: str,
    trading_pair: str,
    side: Any,
    amount: Any,
    price: Any,
) -> AttemptReservation:
    return _claim(
        "executor",
        executor_id,
        {
            "connector_name": _identifier(connector_name),
            "trading_pair": _identifier(trading_pair),
            "side": _side(side),
            "amount": _number(amount),
            "price": _number(price),
        },
    )


def claim_order(
    *, client_order_id: str, trading_pair: str, trade_type: Any, amount: Any, price: Any
) -> AttemptReservation:
    return _claim(
        "kraken-order",
        client_order_id,
        {
            "connector_name": "kraken",
            "trading_pair": _identifier(trading_pair),
            "side": _side(trade_type),
            "amount": _number(amount),
            "price": None if price is None else _number(price),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Explicit offline attempt journal setup"
    )
    parser.add_argument(
        "--initialize", required=True, help="New private journal path; never overwrite"
    )
    args = parser.parse_args()
    try:
        print(initialize_journal(args.initialize))
    except AttemptJournalError as error:
        parser.exit(2, str(error) + "\n")
