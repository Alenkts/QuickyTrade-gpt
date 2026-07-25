"""Durable management-transition ledger (Phase 4).

Reacts to a take-profit leg's broker-confirmed fill by applying the entry's
``transitions[]`` management-policy entries (``MOVE_STOP_TO_BREAKEVEN`` /
``TRAIL_FRESH_BID``) to the position's remaining working stop-loss legs.

Purely additive to ``SubmissionRegistry``'s existing WAL-mode SQLite
database, exactly like ``ExecutionLedger``/``ProtectionLedger`` -- constructed
with the registry's already-open ``sqlite3.Connection`` and already-held
``RLock`` rather than a second connection to the same file.

Deterministic PK idempotency: ``transition_id = f"{correlation_id}:{after}"``
*is* the idempotency mechanism -- a given transition can apply at most once
per trade by construction, since it is a primary key.

State machine (mirrors the claim()/finish() shape used elsewhere in this
package):

  (no row) --[TP leg confirmed filled]--> PENDING
  PENDING  --[pre-broker-call block, e.g. a stale quote]--> PENDING (retried
             next sweep; nothing was touched)
  PENDING  --[mark_applying, durably, before any broker call]--> APPLYING
  APPLYING --[every attempted leg modify resolved (MODIFIED/no-op/rejected,
             none ambiguous)]--> APPLIED (terminal)
  APPLYING --[any leg modify is ambiguous]--> FAILED_UNKNOWN (terminal)
  APPLYING --[process restart while stuck]--> FAILED_UNKNOWN (terminal)

``FAILED_UNKNOWN`` is a genuinely terminal state: per AGENTS.md ("never add
automatic retry for an uncertain submission"), ``ensure_transitions`` never
automatically re-attempts a transition that landed there -- re-attempting a
modify against an order whose prior modify outcome is unknown could compound
the ambiguity (a second placeOrder racing an in-flight first one). It
requires the same kind of out-of-band reconciliation this codebase already
leaves for an ambiguous protection *placement* (Phase 3's own
SUBMISSION_UNKNOWN has no automated resolution path either). A level whose
only take-profit leg was ``SKIPPED_ZERO_ALLOCATION`` (Phase 3: a zero-quantity
allocation, never actually submitted to IBKR) can never produce fill
evidence -- such a transition is resolved directly to ``APPLIED`` the first
time it is observed, with a ``details_json`` note explaining why, rather than
left permanently pending and re-evaluated forever for a fill that structurally
cannot happen.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any

TRANSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS management_transitions (
  transition_id TEXT PRIMARY KEY,
  correlation_id TEXT NOT NULL,
  "after" TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('MOVE_STOP_TO_BREAKEVEN','TRAIL_FRESH_BID')),
  status TEXT NOT NULL CHECK(status IN ('PENDING','APPLYING','APPLIED','FAILED_UNKNOWN')),
  applied_at TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_management_transitions_correlation_id
  ON management_transitions(correlation_id);
"""

TERMINAL_STATUSES = ("APPLIED", "FAILED_UNKNOWN")


@dataclass(frozen=True)
class TransitionClaim:
    claimed: bool
    row: dict[str, Any] | None


class TransitionLedger:
    """Management-transition (MOVE_STOP_TO_BREAKEVEN / TRAIL_FRESH_BID) evidence and status."""

    def __init__(self, connection: sqlite3.Connection, lock: RLock):
        self._db = connection
        self._lock = lock
        with self._lock:
            self._db.executescript(TRANSITIONS_SCHEMA)
            # A process loss after mark_applying() but before a terminal
            # mark_applied()/mark_failed_unknown() is inherently ambiguous --
            # identical to SubmissionRegistry/ProtectionLedger's own
            # SUBMITTING sweep. Never retried automatically once resolved
            # this way (see module docstring).
            self._db.execute(
                "UPDATE management_transitions SET status='FAILED_UNKNOWN', updated_at=? WHERE status='APPLYING'",
                (_now(),),
            )

    # ---- claim / evidence / finish ---------------------------------------

    def ensure_pending(
        self, transition_id: str, *, correlation_id: str, after: str, action: str
    ) -> TransitionClaim:
        """Idempotent: creates the durable PENDING row the first time this
        transition's trigger condition is observed as satisfied. A no-op
        (claimed=False) if the row already exists in any status -- callers
        must inspect the returned row's status themselves."""
        now = _now()
        with self._lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO management_transitions
                   (transition_id,correlation_id,"after",action,status,created_at,updated_at)
                   VALUES (?,?,?,?,'PENDING',?,?)""",
                (transition_id, correlation_id, after, action, now, now),
            )
            claimed = cursor.rowcount == 1
            row = self._row_locked(transition_id)
        return TransitionClaim(claimed=claimed, row=row)

    def mark_applying(self, transition_id: str) -> bool:
        """PENDING -> APPLYING, durably, before any broker modify call.
        Returns False (a safe no-op for the caller) if the row was not
        PENDING -- structurally impossible to double-enter APPLYING."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE management_transitions SET status='APPLYING', updated_at=?
                   WHERE transition_id=? AND status='PENDING'""",
                (_now(), transition_id),
            )
        return changed.rowcount == 1

    def mark_pending_retry(self, transition_id: str, *, details: dict[str, Any] | None = None) -> None:
        """A pre-broker-call block (e.g. a stale/missing quote for
        TRAIL_FRESH_BID, or a market-rule failure) -- the row stays PENDING
        for the next sweep and nothing at the broker was touched. Only ever
        transitions a still-PENDING row -- once mark_applying() has run, a
        broker call may already be in flight and this row can only move
        forward to a terminal status."""
        with self._lock:
            self._db.execute(
                """UPDATE management_transitions SET details_json=?, updated_at=?
                   WHERE transition_id=? AND status='PENDING'""",
                (_details(details), _now(), transition_id),
            )

    def mark_applied(self, transition_id: str, *, details: dict[str, Any] | None = None) -> None:
        """Terminal success. Reachable directly from PENDING (the
        SKIPPED_ZERO_ALLOCATION short-circuit -- no broker call was ever
        attempted) or from APPLYING (every attempted leg modify resolved,
        none ambiguous)."""
        now = _now()
        with self._lock:
            changed = self._db.execute(
                """UPDATE management_transitions SET status='APPLIED', applied_at=?, details_json=?, updated_at=?
                   WHERE transition_id=? AND status IN ('PENDING','APPLYING')""",
                (now, _details(details), now, transition_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Transition is not in a state that can be marked APPLIED")

    def mark_failed_unknown(self, transition_id: str, *, details: dict[str, Any] | None = None) -> None:
        """Terminal, never automatically retried (see module docstring).
        Only ever transitions an APPLYING row."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE management_transitions SET status='FAILED_UNKNOWN', details_json=?, updated_at=?
                   WHERE transition_id=? AND status='APPLYING'""",
                (_details(details), _now(), transition_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Transition is not in APPLYING state")

    # ---- reads ------------------------------------------------------------

    def get(self, transition_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._row_locked(transition_id)

    def has_unresolved_unknown(self) -> bool:
        # Mirrors SubmissionRegistry.has_unresolved_unknown() /
        # ProtectionLedger.has_unresolved_unknown(): a transition that landed
        # in FAILED_UNKNOWN (either a directly-ambiguous leg modify, or a
        # crash sweep of a row stuck APPLYING -- see __init__ above) means a
        # management action (e.g. MOVE_STOP_TO_BREAKEVEN) may never have
        # reached the broker, or reached it with an unknown outcome. Since
        # FAILED_UNKNOWN is a deliberately terminal state that this codebase
        # never automatically retries (module docstring), nothing else
        # surfaces or blocks on it -- without this check, a position could be
        # silently left with a less-protective stop forever, invisibly, while
        # new unrelated opens keep proceeding. Global (like
        # ProtectionLedger.has_unresolved_unknown), not contract-scoped (like
        # ProtectionLedger.has_unresolved_cancel_unknown) -- a silently-stuck
        # transition is the same class of unprotected-or-unknown-protected
        # risk the existing global checks exist to prevent.
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM management_transitions WHERE status='FAILED_UNKNOWN' LIMIT 1"
            ).fetchone()
        return row is not None

    def for_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM management_transitions WHERE correlation_id=? ORDER BY transition_id",
                (correlation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _row_locked(self, transition_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM management_transitions WHERE transition_id=?", (transition_id,)
        ).fetchone()
        return dict(row) if row else None


def transition_id(correlation_id: str, after: str) -> str:
    return f"{correlation_id}:{after}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _details(details: dict[str, Any] | None) -> str | None:
    if details is None:
        return None
    return json.dumps(details, separators=(",", ":"), sort_keys=True, default=str)
