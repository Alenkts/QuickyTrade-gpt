"""Durable protection-order (stop-loss / take-profit) ledger and allocation math.

Purely additive to ``SubmissionRegistry``'s existing WAL-mode SQLite database,
exactly like ``ExecutionLedger`` -- constructed with the registry's
already-open ``sqlite3.Connection`` and already-held ``RLock`` rather than a
second connection to the same file.

Deterministic PK idempotency: ``protection_id`` is derived from
``(correlation_id, level_id or "STOP:<paired level_id>")`` so re-deriving the
same protection intent after a crash always lands on the same row. Per-slice
OCA pairing is load-bearing here: a stop-loss leg is placed per take-profit
slice (same quantity as its paired TP slice, sharing that slice's OCA group)
rather than as a single monolithic stop covering the whole position -- a
shared OCA group across all slices would wrongly cancel TP2/TP3's protection
the instant TP1 fills. ``level_id`` stays NULL on STOP_LOSS rows (only
TAKE_PROFIT rows populate it) per the schema contract; the paired level is
still recoverable via the shared ``oca_group``, and disambiguated in
``protection_id``/``order_ref`` (see ``stop_loss_protection_id`` and
``protection_order_ref`` below) since a monolithic ``"...:STOP"`` id/ref would
collide across every slice once stops are placed per-slice.

The claim()/finish() shape mirrors ``SubmissionRegistry`` exactly: durable
evidence (trigger/limit price, order_ref) is committed *before* the broker
call (``record_broker_call_evidence``, transitioning
``PENDING_FILL_CONFIRMATION`` -> ``SUBMITTING``), and ``finish()`` only ever
transitions a ``SUBMITTING`` row to a terminal status. A stuck ``SUBMITTING``
row from a prior crash is swept to ``SUBMISSION_UNKNOWN`` on construction,
identical to Phase 2's ``SubmissionRegistry`` sweep.

Phase 4 additive columns/methods: ``modify_status``/``pending_trigger_price``/
``pending_limit_price``/``modified_at`` track an in-place order *modification*
(IBKR's ``placeOrder`` re-sent with the same order id -- see
``ibapi_transport.modify_stop_limit_order``) of an already-``SUBMITTED`` leg,
used by ``ExecutionEngine.ensure_transitions`` (management-policy
``MOVE_STOP_TO_BREAKEVEN``/``TRAIL_FRESH_BID`` reactions). The same
evidence-before-broker-call discipline applies: ``record_modify_evidence``
durably records the *intended* new trigger/limit into the ``pending_*``
columns and flips ``modify_status`` to ``'MODIFYING'`` before any socket call;
``finish_modify_success`` promotes those pending values into the row's
authoritative ``trigger_price``/``limit_price`` (this ledger, not a live
``working_orders()`` read, is the single source of truth for a leg's "current
resting trigger", since IBKR's own ``WorkingOrder`` domain type carries no
price fields and only this app's code ever writes to these columns);
``finish_modify_unknown`` leaves the last-confirmed trigger/limit untouched
but marks the row unresolved (``has_unresolved_unknown`` below also checks
``modify_status``, so an ambiguous modify blocks new opens exactly like an
ambiguous initial placement); ``abandon_modify_attempt`` handles a definitive
(non-ambiguous) broker rejection of the modify itself by discarding the
pending evidence and leaving the still-accurate last-confirmed trigger/limit
in place. A row stuck ``'MODIFYING'`` from a prior crash is swept to
``'MODIFY_UNKNOWN'`` on construction, mirroring the ``SUBMITTING`` sweep.

Phase 5 additive column: ``cancel_status`` tracks a ``FULL_FLATTEN``
protection-leg *cancellation* (``ibapi_transport.cancel_order``) of an
already-``SUBMITTED`` leg -- ``record_cancel_intent`` durably records intent
(``cancel_status`` -> ``'CANCELLING'``) before any socket call, exactly like
every other broker side effect in this codebase; ``finish_cancel_confirmed``
promotes the row to the schema's existing terminal ``'CANCELLED'`` status on
a broker-*confirmed* cancellation; ``finish_cancel_unknown`` leaves ``status``
at ``'SUBMITTED'`` (broker truth about whether the leg is actually still
working is unknown either way) but marks ``cancel_status='CANCEL_UNKNOWN'``.
A row stuck ``'CANCELLING'`` from a prior crash is swept to
``'CANCEL_UNKNOWN'`` on construction, mirroring the ``SUBMITTING``/
``MODIFYING`` sweeps. Unlike ``has_unresolved_unknown`` (which blocks every
new open globally), an unresolved cancel is checked by the *contract-scoped*
``has_unresolved_cancel_unknown`` below -- a deliberate asymmetry: closing
one entry's stuck protection-cancel must never trap the operator out of an
unrelated open or a risk-reducing close on a different contract (see
``ExecutionEngine._verify_close_contract_not_ambiguous``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from threading import RLock
from typing import Any

PROTECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_protection_orders (
  protection_id TEXT PRIMARY KEY,
  correlation_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('STOP_LOSS','TAKE_PROFIT')),
  level_id TEXT,
  oca_group TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'PENDING_FILL_CONFIRMATION','SUBMITTING','SUBMITTED','BLOCKED',
    'SUBMISSION_UNKNOWN','CANCELLED','FILLED','SKIPPED_ZERO_ALLOCATION'
  )),
  quantity TEXT NOT NULL,
  trigger_price TEXT,
  limit_price TEXT,
  order_ref TEXT,
  broker_order_id TEXT,
  perm_id INTEGER,
  result_json TEXT,
  modify_status TEXT,
  pending_trigger_price TEXT,
  pending_limit_price TEXT,
  modified_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_protection_orders_correlation_id
  ON broker_protection_orders(correlation_id);
"""

ROLES = ("STOP_LOSS", "TAKE_PROFIT")
FINISH_STATUSES = ("SUBMITTED", "BLOCKED", "SUBMISSION_UNKNOWN")
# Only a leg actually confirmed placed (or filled) counts as already covering
# its slice of the target quantity. PENDING_FILL_CONFIRMATION is deliberately
# excluded -- it is handled separately (resumed, not double-counted) by the
# caller so a crash between claim() and the broker call can never be silently
# skipped by a later top-up computation.
COMMITTED_STATUSES = frozenset({"SUBMITTED", "FILLED"})


@dataclass(frozen=True)
class ProtectionClaim:
    claimed: bool
    row: dict[str, Any] | None


class ProtectionLedger:
    """Stop-loss / take-profit protection-order evidence and status."""

    def __init__(self, connection: sqlite3.Connection, lock: RLock):
        self._db = connection
        self._lock = lock
        with self._lock:
            self._db.executescript(PROTECTION_SCHEMA)
            self._migrate_columns()
            # A process loss after claim()/record_broker_call_evidence() but
            # before finish() is inherently ambiguous -- identical to
            # SubmissionRegistry's own SUBMITTING sweep. Never left claimable
            # again without a future reconciliation pass.
            now = _now()
            self._db.execute(
                "UPDATE broker_protection_orders SET status='SUBMISSION_UNKNOWN', updated_at=? "
                "WHERE status='SUBMITTING'",
                (now,),
            )
            # Phase 4: a modify() call that crashed mid-flight is exactly as
            # ambiguous as an initial-placement SUBMITTING row -- swept the
            # same way, and (via has_unresolved_unknown() below) blocks new
            # opens the same way.
            self._db.execute(
                "UPDATE broker_protection_orders SET modify_status='MODIFY_UNKNOWN', updated_at=? "
                "WHERE modify_status='MODIFYING'",
                (now,),
            )
            # Phase 5: a cancel_order() call that crashed mid-flight is
            # exactly as ambiguous -- swept the same way. Deliberately does
            # NOT feed has_unresolved_unknown() (global); only the
            # contract-scoped has_unresolved_cancel_unknown() below sees it.
            self._db.execute(
                "UPDATE broker_protection_orders SET cancel_status='CANCEL_UNKNOWN', updated_at=? "
                "WHERE cancel_status='CANCELLING'",
                (now,),
            )

    def _migrate_columns(self) -> None:
        """Additive-only migration for an already-populated pre-Phase-4
        database (mirrors SubmissionRegistry._migrate_columns): the
        CREATE TABLE IF NOT EXISTS above only benefits a brand-new file."""
        columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(broker_protection_orders)").fetchall()
        }
        for name in (
            "modify_status",
            "pending_trigger_price",
            "pending_limit_price",
            "modified_at",
            "cancel_status",
        ):
            if name not in columns:
                self._db.execute(f"ALTER TABLE broker_protection_orders ADD COLUMN {name} TEXT")

    # ---- claim / evidence / finish (mirrors SubmissionRegistry) ---------

    def claim_leg(
        self,
        protection_id: str,
        *,
        correlation_id: str,
        role: str,
        level_id: str | None,
        oca_group: str,
        quantity: int,
    ) -> ProtectionClaim:
        if role not in ROLES:
            raise ValueError("Unsupported protection role")
        now = _now()
        with self._lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO broker_protection_orders
                   (protection_id,correlation_id,role,level_id,oca_group,status,quantity,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,'PENDING_FILL_CONFIRMATION',?,?,?)""",
                (protection_id, correlation_id, role, level_id, oca_group, str(quantity), now, now),
            )
            claimed = cursor.rowcount == 1
            row = self._row_locked(protection_id)
        return ProtectionClaim(claimed=claimed, row=row)

    def mark_skipped_zero_allocation(
        self,
        protection_id: str,
        *,
        correlation_id: str,
        role: str,
        level_id: str | None,
        oca_group: str,
    ) -> bool:
        """Idempotent direct write for a level whose computed quantity is
        zero -- IBKR rejects zero-quantity orders, so this never reaches
        claim_leg()/the broker call at all."""
        if role not in ROLES:
            raise ValueError("Unsupported protection role")
        now = _now()
        with self._lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO broker_protection_orders
                   (protection_id,correlation_id,role,level_id,oca_group,status,quantity,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,'SKIPPED_ZERO_ALLOCATION','0',?,?)""",
                (protection_id, correlation_id, role, level_id, oca_group, now, now),
            )
        return cursor.rowcount == 1

    def record_broker_call_evidence(
        self,
        protection_id: str,
        *,
        trigger_price: str | None,
        limit_price: str,
        order_ref: str,
    ) -> None:
        """Commit final broker-call evidence before any socket side effect.
        Only transitions a claimed PENDING_FILL_CONFIRMATION row."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET status='SUBMITTING', trigger_price=?, limit_price=?, order_ref=?, updated_at=?
                   WHERE protection_id=? AND status='PENDING_FILL_CONFIRMATION'""",
                (trigger_price, limit_price, order_ref, _now(), protection_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not available for broker-call evidence")

    def mark_blocked_pending(self, protection_id: str, *, result: dict[str, Any]) -> None:
        """Transition a still-PENDING_FILL_CONFIRMATION row directly to
        BLOCKED -- used when market-rule/price computation fails *before*
        durable evidence is committed (no broker call was ever attempted).
        Distinct from finish(), which only ever transitions a SUBMITTING row
        (i.e. evidence was already committed before the broker call)."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET status='BLOCKED', result_json=?, updated_at=?
                   WHERE protection_id=? AND status='PENDING_FILL_CONFIRMATION'""",
                (
                    json.dumps(result, separators=(",", ":"), sort_keys=True, default=str),
                    _now(),
                    protection_id,
                ),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not in PENDING_FILL_CONFIRMATION state")

    def finish(
        self,
        protection_id: str,
        *,
        status: str,
        result: dict[str, Any],
        broker_order_id: str | None = None,
        perm_id: int | None = None,
    ) -> None:
        if status not in FINISH_STATUSES:
            raise ValueError("Unsupported protection finish status")
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET status=?,result_json=?,broker_order_id=?,perm_id=?,updated_at=?
                   WHERE protection_id=? AND status='SUBMITTING'""",
                (
                    status,
                    json.dumps(result, separators=(",", ":"), sort_keys=True, default=str),
                    broker_order_id,
                    perm_id,
                    _now(),
                    protection_id,
                ),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not in SUBMITTING state")

    # ---- in-place modification (Phase 4: management transitions) ---------
    #
    # Only ever applies to an already-SUBMITTED leg (a real, currently-placed
    # order). Mirrors claim()/record_broker_call_evidence()/finish() above:
    # durable evidence of the *intended* new trigger/limit is committed before
    # any socket call, and a stuck 'MODIFYING' row is swept to
    # 'MODIFY_UNKNOWN' on the next restart (see __init__).

    def record_modify_evidence(
        self, protection_id: str, *, pending_trigger_price: str, pending_limit_price: str
    ) -> None:
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET modify_status='MODIFYING', pending_trigger_price=?, pending_limit_price=?, updated_at=?
                   WHERE protection_id=? AND status='SUBMITTED' AND modify_status IS NULL""",
                (pending_trigger_price, pending_limit_price, _now(), protection_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not available for modify evidence")

    def finish_modify_success(self, protection_id: str) -> None:
        """A proven broker acknowledgement of the modify: the pending
        trigger/limit become the row's new authoritative (last-confirmed)
        values -- this ledger, not a live working_orders() read, is the
        single source of truth for a leg's "current resting trigger"."""
        now = _now()
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET trigger_price=pending_trigger_price, limit_price=pending_limit_price,
                       pending_trigger_price=NULL, pending_limit_price=NULL, modify_status=NULL,
                       modified_at=?, updated_at=?
                   WHERE protection_id=? AND modify_status='MODIFYING'""",
                (now, now, protection_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not in a MODIFYING state")

    def finish_modify_unknown(self, protection_id: str) -> None:
        """An ack timeout/indeterminate result on the modify call itself --
        exactly as ambiguous as an initial-placement SUBMISSION_UNKNOWN.
        The last-confirmed trigger/limit are left untouched (broker truth
        about whether the modify actually landed is unknown either way, so
        this ledger must not claim the new value took effect); the pending_*
        columns are left in place purely as audit evidence of what was
        attempted. Never automatically retried -- see has_unresolved_unknown."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders SET modify_status='MODIFY_UNKNOWN', updated_at=?
                   WHERE protection_id=? AND modify_status='MODIFYING'""",
                (_now(), protection_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not in a MODIFYING state")

    def abandon_modify_attempt(self, protection_id: str) -> None:
        """A *definitive* (non-ambiguous) broker rejection of the modify
        itself -- e.g. the order was no longer eligible to be modified. This
        is a resolved outcome, not an unresolved one: pending evidence is
        discarded and the leg's last-confirmed trigger/limit (still accurate
        broker truth -- the modify simply never took effect) is left as-is."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET pending_trigger_price=NULL, pending_limit_price=NULL, modify_status=NULL, updated_at=?
                   WHERE protection_id=? AND modify_status='MODIFYING'""",
                (_now(), protection_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not in a MODIFYING state")

    # ---- cancellation (Phase 5: FULL_FLATTEN protection-leg cancel) ------
    #
    # Only ever applies to an already-SUBMITTED leg. Mirrors the modify
    # methods above exactly: durable cancel-intent evidence is committed
    # before any socket call, and a stuck 'CANCELLING' row is swept to
    # 'CANCEL_UNKNOWN' on the next restart (see __init__).

    def record_cancel_intent(self, protection_id: str) -> None:
        """Durable cancel-intent evidence, committed before any
        ``cancel_order`` broker call. Only transitions an already-SUBMITTED
        leg that has never had a cancel attempted (``cancel_status IS
        NULL``) -- a leg whose cancel already resolved (``status='CANCELLED'``,
        terminal) or went ambiguous (``cancel_status='CANCEL_UNKNOWN'``) is
        never re-entered here automatically; ``ExecutionEngine.
        _verify_close_contract_not_ambiguous`` blocks any further close/
        flatten on a contract with an unresolved protection-cancel before
        this method could ever be reached again for that leg."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET cancel_status='CANCELLING', updated_at=?
                   WHERE protection_id=? AND status='SUBMITTED' AND cancel_status IS NULL""",
                (_now(), protection_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not available for cancel-intent evidence")

    def finish_cancel_confirmed(self, protection_id: str, *, broker_raw_status: str) -> None:
        """A proven broker cancellation (IBKR's orderStatus/error(202)
        confirmation channel -- see ``ibapi_transport.cancel_order``): the
        leg is definitively resolved and no longer working. ``status``
        transitions to the schema's existing terminal ``'CANCELLED'``
        value."""
        now = _now()
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET status='CANCELLED', cancel_status='CANCEL_CONFIRMED', result_json=?, updated_at=?
                   WHERE protection_id=? AND cancel_status='CANCELLING'""",
                (
                    json.dumps({"cancelRawStatus": broker_raw_status}, separators=(",", ":"), sort_keys=True),
                    now,
                    protection_id,
                ),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not in a CANCELLING state")

    def finish_cancel_unknown(self, protection_id: str) -> None:
        """An ambiguous/timed-out cancel outcome -- exactly as unresolved as
        an initial-placement SUBMISSION_UNKNOWN or a modify's MODIFY_UNKNOWN.
        ``status`` is deliberately left at ``'SUBMITTED'`` (broker truth
        about whether the leg is actually still working is unknown either
        way -- this ledger must never claim it is safely gone); only
        ``cancel_status`` records the unresolved attempt. Never automatically
        retried -- see ``has_unresolved_cancel_unknown``, which blocks all
        further close/flatten action on this specific contract until
        reconciled."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders SET cancel_status='CANCEL_UNKNOWN', updated_at=?
                   WHERE protection_id=? AND cancel_status='CANCELLING'""",
                (_now(), protection_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Protection leg is not in a CANCELLING state")

    def mark_filled(self, protection_id: str, *, execution_evidence: dict[str, Any]) -> bool:
        """Resolve a working leg to FILLED on positive broker execution evidence.

        Only ever called with real broker_executions rows for this leg's own
        order_ref -- never inferred from a leg's disappearance from the open
        order list, which is equally consistent with a cancel.
        """
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET status='FILLED', result_json=?, updated_at=?
                   WHERE protection_id=? AND status='SUBMITTED'""",
                (
                    json.dumps(execution_evidence, separators=(",", ":"), sort_keys=True),
                    _now(),
                    protection_id,
                ),
            )
        return changed.rowcount == 1

    def mark_cancelled_by_oca(self, protection_id: str, *, filled_sibling_id: str) -> bool:
        """Resolve a working leg to CANCELLED because its OCA sibling filled.

        A one-cancels-all group is a broker guarantee: once one leg of the
        group is confirmed filled, IBKR has cancelled the others. That makes
        this positive evidence rather than an inference from absence.
        """
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_protection_orders
                   SET status='CANCELLED', result_json=?, updated_at=?
                   WHERE protection_id=? AND status='SUBMITTED' AND cancel_status IS NULL""",
                (
                    json.dumps(
                        {"cancelledBy": "OCA_SIBLING_FILLED", "filledSibling": filled_sibling_id},
                        separators=(",", ":"), sort_keys=True,
                    ),
                    _now(),
                    protection_id,
                ),
            )
        return changed.rowcount == 1

    def working_legs(self) -> list[dict[str, Any]]:
        """Every leg this ledger still believes is working at IBKR."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM broker_protection_orders WHERE status='SUBMITTED' ORDER BY protection_id"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- reads ------------------------------------------------------------

    def get(self, protection_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._row_locked(protection_id)

    def legs(
        self,
        correlation_id: str,
        *,
        role: str,
        level_id: str | None = None,
        oca_group: str | None = None,
    ) -> list[dict[str, Any]]:
        """All rows (any top-up index) for one (correlation_id, role,
        level/oca) family, oldest first. TAKE_PROFIT families are queried by
        level_id (populated); STOP_LOSS families are queried by oca_group
        (level_id stays NULL on those rows per the schema contract)."""
        if role not in ROLES:
            raise ValueError("Unsupported protection role")
        with self._lock:
            if role == "TAKE_PROFIT":
                rows = self._db.execute(
                    """SELECT * FROM broker_protection_orders
                       WHERE correlation_id=? AND role='TAKE_PROFIT' AND level_id=?
                       ORDER BY protection_id""",
                    (correlation_id, level_id),
                ).fetchall()
            else:
                rows = self._db.execute(
                    """SELECT * FROM broker_protection_orders
                       WHERE correlation_id=? AND role='STOP_LOSS' AND oca_group=?
                       ORDER BY protection_id""",
                    (correlation_id, oca_group),
                ).fetchall()
        return [dict(row) for row in rows]

    def legs_for_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM broker_protection_orders WHERE correlation_id=? ORDER BY protection_id",
                (correlation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_unresolved_unknown(self) -> bool:
        # Covers both an ambiguous *initial placement* (status) and an
        # ambiguous *modify* of an already-placed leg (modify_status, Phase
        # 4) -- either kind of unresolved broker outcome must globally block
        # new opens identically (see ExecutionEngine._verify_readiness).
        with self._lock:
            row = self._db.execute(
                """SELECT 1 FROM broker_protection_orders
                   WHERE status='SUBMISSION_UNKNOWN' OR modify_status='MODIFY_UNKNOWN' LIMIT 1"""
            ).fetchone()
        return row is not None

    def has_unresolved_cancel_unknown(self, *, account: str, con_id: int) -> bool:
        """Contract-scoped (never global) ambiguity check -- deliberately
        distinct from ``has_unresolved_unknown`` above, which blocks every
        new open globally. An unresolved protection-leg cancel only blocks
        further close/flatten action (and further cancel attempts) on this
        *specific* contract; it must never block an unrelated open, or a
        close on a different contract, the way an unresolved protection
        *placement*/*modify* ambiguity does (see AGENTS.md's explicit
        close-ambiguity scoping and ExecutionEngine.
        _verify_close_contract_not_ambiguous).

        ``broker_protection_orders`` has no ``con_id`` column of its own --
        correlated here via the owning entry's persisted contract in
        ``broker_submissions`` (same shared connection/database)."""
        with self._lock:
            rows = self._db.execute(
                """SELECT s.contract_json FROM broker_protection_orders p
                   JOIN broker_submissions s ON s.correlation_id = p.correlation_id
                   WHERE p.cancel_status='CANCEL_UNKNOWN' AND s.account=?""",
                (account,),
            ).fetchall()
        for row in rows:
            contract_json = row["contract_json"]
            if not contract_json:
                continue
            contract = json.loads(contract_json)
            if contract.get("con_id") == con_id:
                return True
        return False

    def unresolved_cancel_unknown_legs(self) -> list[dict[str, Any]]:
        """Every protection leg with ``cancel_status='CANCEL_UNKNOWN'`` --
        the globally-unscoped read backing a reconciliation-summary count
        (GET /private/v1/reconciliation), distinct from
        ``has_unresolved_cancel_unknown``'s per-account/con_id gating check
        above, which remains the sole enforcement point for blocking a
        further close/flatten."""
        with self._lock:
            rows = self._db.execute(
                """SELECT protection_id, correlation_id, role, oca_group FROM broker_protection_orders
                   WHERE cancel_status='CANCEL_UNKNOWN'"""
            ).fetchall()
        return [dict(row) for row in rows]

    def _row_locked(self, protection_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM broker_protection_orders WHERE protection_id=?", (protection_id,)
        ).fetchone()
        return dict(row) if row else None


# ---- deterministic identifiers ------------------------------------------


def take_profit_protection_id(correlation_id: str, level_id: str, *, index: int = 1) -> str:
    base = f"{correlation_id}:TP:{level_id}"
    return base if index == 1 else f"{base}:{index}"


def stop_loss_protection_id(correlation_id: str, level_id: str, *, index: int = 1) -> str:
    # level_id here identifies which take-profit slice this stop-loss order
    # is quantity-matched and OCA-paired with (per-slice stops, not one
    # monolithic stop order -- see module docstring). The `level_id` *column*
    # on the row itself stays NULL for STOP_LOSS rows per the schema
    # contract; this is purely part of the deterministic primary-key string.
    base = f"{correlation_id}:STOP:{level_id}"
    return base if index == 1 else f"{base}:{index}"


def protection_oca_group(correlation_id: str, level_id: str) -> str:
    key = f"{correlation_id}:{level_id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return "QTOCA" + digest[:27]


def protection_order_ref(correlation_id: str, level_id_or_stop: str, role: str) -> str:
    key = f"{correlation_id}:{level_id_or_stop}:{role}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return "QT" + digest[:30]


# ---- largest-remainder allocation ----------------------------------------


def largest_remainder_allocation(quantity: int, levels: list[dict[str, Any]]) -> dict[str, int]:
    """Deterministic, exact (pure Decimal, no floats) largest-remainder
    distribution of ``quantity`` across take-profit ``levels`` by their
    ``allocationPercent``. Sigma(qty_i) == quantity always, since
    allocationPercent already sums to exactly 100 (enforced by
    management.validate_management_policy). Ties in the leftover-unit
    distribution favor the earlier (lower-trigger) level -- Python's sort is
    stable, so sorting descending by remainder preserves each level's
    original (ascending-trigger) relative order among equal remainders.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ValueError("quantity must be a non-negative integer")
    total = Decimal(quantity)
    raw: dict[str, int] = {}
    remainder: dict[str, Decimal] = {}
    allocated = 0
    for level in levels:
        level_id = level["levelId"]
        allocation_percent = Decimal(str(level["allocationPercent"]))
        exact = (total * allocation_percent) / Decimal("100")
        floor_qty = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        raw[level_id] = floor_qty
        remainder[level_id] = exact - floor_qty
        allocated += floor_qty
    leftover = quantity - allocated
    if leftover > 0:
        # Deficit is always < len(levels): each remainder is in [0, 1), so
        # their sum (which the integer deficit must equal, since `total` and
        # `allocated` are both integers) cannot reach len(levels).
        ordered = sorted(levels, key=lambda level: remainder[level["levelId"]], reverse=True)
        for level in ordered[:leftover]:
            raw[level["levelId"]] += 1
    return raw


def _now() -> str:
    return datetime.now(UTC).isoformat()
