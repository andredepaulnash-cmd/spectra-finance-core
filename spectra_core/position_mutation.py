"""Shared durable position mutation ownership; grants no capital authority."""

from __future__ import annotations


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("exact nonempty mutation identity required")
    return value


def initialize_position_mutation(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS selected_recovery_position_locks (
            position_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS selected_recovery_allowed_proposals (
            proposal_id TEXT PRIMARY KEY, position_id TEXT NOT NULL, owner_id TEXT NOT NULL);
    """)


def lock_owner(conn, position_id):
    row = conn.execute(
        "SELECT owner_id FROM selected_recovery_position_locks WHERE position_id=?",
        (position_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def claim_lock(ledger, position_id, owner_id):
    _text(position_id)
    _text(owner_id)
    with ledger._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        position = ledger._position_row(conn, position_id)
        if position["status"] != "ACTIVE":
            raise ValueError("position mutation admission requires ACTIVE inventory")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if (
            "economic_exit_manifests" in tables
            and conn.execute(
                "SELECT 1 FROM economic_exit_manifests WHERE position_id=? AND status='OPEN'",
                (position_id,),
            ).fetchone()
        ):
            raise ValueError("whole-position exit manifest already owns the position")
        if "partial_recycle_manifests" in tables:
            partials = conn.execute(
                "SELECT manifest_id FROM partial_recycle_manifests WHERE position_id=? AND status='OPEN'",
                (position_id,),
            ).fetchall()
            if any(owner_id != "partial:" + row[0] for row in partials):
                raise ValueError("partial recycle manifest already owns the position")
        current = lock_owner(conn, position_id)
        if current not in (None, owner_id):
            raise ValueError("another selected recovery owns the position lock")
        conn.execute(
            "INSERT OR IGNORE INTO selected_recovery_position_locks VALUES (?, ?)",
            (position_id, owner_id),
        )


def release_lock(ledger, position_id, owner_id):
    _text(position_id)
    _text(owner_id)
    with ledger._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = lock_owner(conn, position_id)
        if current is None:
            return False
        if current != owner_id:
            raise ValueError("cannot release another mutation owner's lock")
        conn.execute(
            "DELETE FROM selected_recovery_position_locks WHERE position_id=? AND owner_id=?",
            (position_id, owner_id),
        )
        return True


def allow_proposal(ledger, position_id, owner_id, proposal_id):
    _text(proposal_id)
    with ledger._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if lock_owner(conn, position_id) != owner_id:
            raise ValueError("selected recovery lock required")
        row = conn.execute(
            "SELECT position_id, owner_id FROM selected_recovery_allowed_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if row is not None and tuple(row) != (position_id, owner_id):
            raise ValueError("selected proposal owner changed")
        conn.execute(
            "INSERT OR IGNORE INTO selected_recovery_allowed_proposals VALUES (?, ?, ?)",
            (proposal_id, position_id, owner_id),
        )


def proposal_allowed(ledger, pair, proposal_id):
    from .economic_position import normalized_base_asset

    with ledger._connect() as conn:
        locks = conn.execute(
            """SELECT l.position_id, l.owner_id FROM selected_recovery_position_locks l
            JOIN economic_positions p ON p.position_id=l.position_id WHERE p.base_asset=?""",
            (normalized_base_asset(pair),),
        ).fetchall()
        if not locks:
            return True
        row = conn.execute(
            "SELECT position_id, owner_id FROM selected_recovery_allowed_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        return len(locks) == 1 and row is not None and tuple(row) == tuple(locks[0])
