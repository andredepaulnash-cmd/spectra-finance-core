"""Persistent, fail-closed lifecycle controls for Spectra modules.

This store controls readiness/arming/activation intent only. It never dispatches
orders. A module must be registered as implemented and a caller must separately
enforce the Capital Governor before any capital-affecting action.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

MODULES = {
    "vcap": {"implemented": True, "default": "RUNNING", "triggered": False},
    "dttp": {"implemented": False, "default": "MONITORING", "triggered": True},
    "dtsl": {"implemented": False, "default": "BLOCKED", "triggered": True},
    "pssm": {"implemented": False, "default": "BLOCKED", "triggered": False},
    "mir": {"implemented": False, "default": "BLOCKED", "triggered": False},
}
VALID_STATES = {"RUNNING", "MONITORING", "ARMED", "ACTIVE", "BLOCKED"}


class ModuleControlError(ValueError):
    """A module lifecycle transition was not safe or not supported."""


class ModuleLifecycleStore:
    """SQLite-backed module states and append-only transition audit."""

    def __init__(self, path: str | Path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS module_controls (
                    module_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    trigger_enabled INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS module_control_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module_id TEXT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT NOT NULL,
                    trigger_enabled INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            now = float(self.clock())
            for module_id, spec in MODULES.items():
                state = spec["default"]
                reason = (
                    "runtime initialized; implementation not yet verified"
                    if not spec["implemented"] else "module runtime initialized"
                )
                db.execute(
                    """INSERT OR IGNORE INTO module_controls
                       (module_id,state,trigger_enabled,reason,updated_at)
                       VALUES (?,?,0,?,?)""",
                    (module_id, state, reason, now),
                )

    def snapshot(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT module_id,state,trigger_enabled,reason,updated_at "
                "FROM module_controls ORDER BY module_id"
            ).fetchall()
        return [
            {**dict(row), "trigger_enabled": bool(row["trigger_enabled"])}
            for row in rows
        ]

    def set_trigger(self, module_id: str, enabled: bool, *, reason: str = "operator"):
        if module_id not in MODULES or not MODULES[module_id]["triggered"]:
            raise ModuleControlError("module has no independent activation trigger")
        if type(enabled) is not bool:
            raise ModuleControlError("trigger state must be boolean")
        if not isinstance(reason, str) or not reason.strip():
            raise ModuleControlError("a reason is required")
        now = float(self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,trigger_enabled FROM module_controls WHERE module_id=?",
                (module_id,),
            ).fetchone()
            if row is None:
                raise ModuleControlError("unknown module")
            old_state = row["state"]
            # Turning a trigger off never silently terminates in-flight safety
            # work; it blocks new activations and returns to monitoring.
            new_state = "MONITORING" if not enabled else (
                "ARMED" if MODULES[module_id]["implemented"] else "BLOCKED"
            )
            detail = reason.strip()
            if enabled and not MODULES[module_id]["implemented"]:
                detail = "trigger requested; blocked because runtime implementation is unverified"
            db.execute(
                "UPDATE module_controls SET state=?,trigger_enabled=?,reason=?,updated_at=? WHERE module_id=?",
                (new_state, int(enabled), detail, now, module_id),
            )
            db.execute(
                """INSERT INTO module_control_events
                   (module_id,previous_state,new_state,trigger_enabled,reason,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (module_id, old_state, new_state, int(enabled), detail, now),
            )
        return next(x for x in self.snapshot() if x["module_id"] == module_id)

    def activate(self, module_id: str, *, reason: str):
        if module_id not in MODULES:
            raise ModuleControlError("unknown module")
        spec = MODULES[module_id]
        if not spec["implemented"]:
            raise ModuleControlError("module activation blocked: implementation is unverified")
        if spec["triggered"]:
            row = next(x for x in self.snapshot() if x["module_id"] == module_id)
            if not row["trigger_enabled"]:
                raise ModuleControlError("module activation blocked: trigger is disabled")
        if not isinstance(reason, str) or not reason.strip():
            raise ModuleControlError("activation reason is required")
        return self._transition(module_id, "ACTIVE", reason.strip())

    def _transition(self, module_id, state, reason):
        if state not in VALID_STATES:
            raise ModuleControlError("invalid lifecycle state")
        now = float(self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,trigger_enabled FROM module_controls WHERE module_id=?",
                (module_id,),
            ).fetchone()
            if row is None:
                raise ModuleControlError("unknown module")
            if state == "ACTIVE" and module_id in {"dttp", "dtsl"} and not row["trigger_enabled"]:
                raise ModuleControlError("module activation blocked: trigger is disabled")
            db.execute(
                "UPDATE module_controls SET state=?,reason=?,updated_at=? WHERE module_id=?",
                (state, reason, now, module_id),
            )
            db.execute(
                """INSERT INTO module_control_events
                   (module_id,previous_state,new_state,trigger_enabled,reason,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (module_id, row["state"], state, row["trigger_enabled"], reason, now),
            )
        return next(x for x in self.snapshot() if x["module_id"] == module_id)
