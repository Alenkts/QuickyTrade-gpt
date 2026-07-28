"""Durable broker-boundary idempotency registry.

The TradingView ingress owns the primary signal ledger.  This small secondary
registry closes the crash/replay gap immediately around ``placeOrder``.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS broker_submissions (
  correlation_id TEXT PRIMARY KEY,
  node_intent_id INTEGER NOT NULL,
  payload_hash TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'TRADINGVIEW',
  ownership TEXT NOT NULL DEFAULT 'APP_OWNED',
  management_mode TEXT NOT NULL DEFAULT 'ENTRY_ONLY',
  management_policy_json TEXT,
  status TEXT NOT NULL CHECK(status IN ('SUBMITTING','SUBMITTED','BLOCKED','SUBMISSION_UNKNOWN')),
  result_json TEXT,
  account TEXT,
  action TEXT,
  contract_json TEXT,
  entry_correlation_id TEXT,
  side TEXT,
  quantity TEXT,
  limit_price TEXT,
  order_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""
# reconciliation_outcome/reconciled_at are additive columns (see _migrate_columns)
# rather than part of the initial CREATE TABLE, so an already-populated database
# upgrades in place. reconciliation_outcome intentionally keeps its own narrow
# CHECK vocabulary, separate from `status`: `status` records what was known at
# submission time (never rewritten), while reconciliation_outcome separately
# records how a SUBMISSION_UNKNOWN row was later resolved against broker truth.
RECONCILIATION_OUTCOME_VALUES = ("CONFIRMED_FILLED", "CONFIRMED_NO_FILL")


@dataclass(frozen=True)
class Claim:
    claimed: bool
    status: str
    result: dict[str, Any] | None


class SubmissionRegistry:
    def __init__(self, path: Path | str):
        self._lock = RLock()
        path = Path(path)
        is_memory = str(path) == ":memory:"
        created_parent = not is_memory and not path.parent.exists()
        self._process_lock_file = None
        previous_umask = os.umask(0o077)
        try:
            if not is_memory:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if created_parent:
                    os.chmod(path.parent, 0o700)
                lock_path = Path(f"{path}.lock")
                self._process_lock_file = lock_path.open("a+b")
                os.chmod(lock_path, 0o600)
                try:
                    fcntl.flock(self._process_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self._process_lock_file.close()
                    self._process_lock_file = None
                    raise RuntimeError("Another QuickyTrade core already owns this profile database") from None
            try:
                self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            except Exception:
                if self._process_lock_file is not None:
                    fcntl.flock(self._process_lock_file.fileno(), fcntl.LOCK_UN)
                    self._process_lock_file.close()
                    self._process_lock_file = None
                raise
        finally:
            os.umask(previous_umask)
        if not is_memory:
            os.chmod(path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate_columns()
        # A process loss after the claim is inherently ambiguous.  Never make it
        # claimable again without broker reconciliation.
        now = _now()
        self._db.execute(
            "UPDATE broker_submissions SET status='SUBMISSION_UNKNOWN', updated_at=? WHERE status='SUBMITTING'",
            (now,),
        )
        # A cancel_order() call (see record_entry_cancel_intent) that crashed
        # mid-flight is exactly as ambiguous as an initial-placement
        # SUBMITTING row -- same restart-recovery sweep, mirrored from
        # ProtectionLedger's own cancel_status handling.
        self._db.execute(
            "UPDATE broker_submissions SET entry_cancel_status='CANCEL_UNKNOWN', updated_at=? "
            "WHERE entry_cancel_status='CANCELLING'",
            (now,),
        )

    @property
    def connection(self) -> sqlite3.Connection:
        """The single shared sqlite3.Connection this registry owns.

        Exposed so a second, purely-additive component (e.g. the execution/
        reconciliation ledger) can extend the same WAL-mode database file
        without opening a second connection to it.
        """
        return self._db

    @property
    def lock(self) -> RLock:
        """The single process-wide write lock guarding ``self.connection``."""
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._db.close()
            if self._process_lock_file is not None:
                fcntl.flock(self._process_lock_file.fileno(), fcntl.LOCK_UN)
                self._process_lock_file.close()
                self._process_lock_file = None

    def claim(
        self,
        correlation_id: str,
        node_intent_id: int,
        payload_hash: str,
        *,
        source: str = "TRADINGVIEW",
        ownership: str = "APP_OWNED",
        management_mode: str = "ENTRY_ONLY",
        management_policy: dict[str, Any] | None = None,
    ) -> Claim:
        if source not in {"TRADINGVIEW", "MANUAL_UI"}:
            raise ValueError("Unsupported intent source")
        if ownership != "APP_OWNED":
            raise ValueError("Unsupported trade ownership")
        if management_mode not in {"APP_MANAGED", "ENTRY_ONLY"}:
            raise ValueError("Unsupported management mode")
        if management_mode == "APP_MANAGED" and management_policy is None:
            raise ValueError("APP_MANAGED requires a management policy")
        if management_mode == "ENTRY_ONLY" and management_policy is not None:
            raise ValueError("ENTRY_ONLY may not carry a management policy")
        policy_json = (
            json.dumps(management_policy, separators=(",", ":"), sort_keys=True)
            if management_policy is not None
            else None
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM broker_submissions WHERE correlation_id=?", (correlation_id,)
                ).fetchone()
                if row:
                    immutable_mismatch = (
                        row["payload_hash"] != payload_hash
                        or row["node_intent_id"] != node_intent_id
                        or row["source"] != source
                        or row["ownership"] != ownership
                        or row["management_mode"] != management_mode
                        or row["management_policy_json"] != policy_json
                    )
                    if immutable_mismatch:
                        self._db.execute("ROLLBACK")
                        return Claim(False, "CONFLICT", None)
                    self._db.execute("COMMIT")
                    return Claim(False, row["status"], _json(row["result_json"]))
                now = _now()
                self._db.execute(
                    """INSERT INTO broker_submissions
                       (correlation_id,node_intent_id,payload_hash,source,ownership,
                        management_mode,management_policy_json,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,'SUBMITTING',?,?)""",
                    (
                        correlation_id,
                        node_intent_id,
                        payload_hash,
                        source,
                        ownership,
                        management_mode,
                        policy_json,
                        now,
                        now,
                    ),
                )
                self._db.execute("COMMIT")
                return Claim(True, "SUBMITTING", None)
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def finish(
        self,
        correlation_id: str,
        *,
        status: str,
        result: dict[str, Any],
        account: str | None = None,
        action: str | None = None,
        contract: dict[str, Any] | None = None,
        entry_correlation_id: str | None = None,
    ) -> None:
        if status not in {"SUBMITTED", "BLOCKED", "SUBMISSION_UNKNOWN"}:
            raise ValueError("Unsupported registry status")
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_submissions
                   SET status=?,result_json=?,account=COALESCE(?,account),action=COALESCE(?,action),
                       contract_json=COALESCE(?,contract_json),entry_correlation_id=?,updated_at=?
                   WHERE correlation_id=? AND status='SUBMITTING'""",
                (
                    status,
                    json.dumps(result, separators=(",", ":"), sort_keys=True),
                    account,
                    action,
                    json.dumps(contract, separators=(",", ":"), sort_keys=True) if contract else None,
                    entry_correlation_id,
                    _now(),
                    correlation_id,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Submission is not in SUBMITTING state")

    def record_broker_call_evidence(
        self,
        correlation_id: str,
        *,
        account: str,
        action: str,
        contract: dict[str, Any],
        side: str,
        quantity: int,
        limit_price: str,
        order_ref: str,
        entry_correlation_id: str | None,
    ) -> None:
        """Commit final broker-call evidence before any socket side effect."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_submissions
                   SET account=?,action=?,contract_json=?,entry_correlation_id=?,
                       side=?,quantity=?,limit_price=?,order_ref=?,updated_at=?
                   WHERE correlation_id=? AND status='SUBMITTING'""",
                (
                    account,
                    action,
                    json.dumps(contract, separators=(",", ":"), sort_keys=True),
                    entry_correlation_id,
                    side,
                    str(quantity),
                    limit_price,
                    order_ref,
                    _now(),
                    correlation_id,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Submission is not available for broker-call evidence")

    def submission_evidence(self, correlation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT correlation_id,status,source,ownership,management_mode,
                          management_policy_json,account,action,contract_json,
                          entry_correlation_id,side,quantity,limit_price,order_ref,
                          reconciliation_outcome,reconciled_at
                   FROM broker_submissions WHERE correlation_id=?""",
                (correlation_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "correlation_id": row["correlation_id"],
            "status": row["status"],
            "source": row["source"],
            "ownership": row["ownership"],
            "management_mode": row["management_mode"],
            "management_policy": _json(row["management_policy_json"]),
            "account": row["account"],
            "action": row["action"],
            "contract": _json(row["contract_json"]),
            "entry_correlation_id": row["entry_correlation_id"],
            "side": row["side"],
            "quantity": row["quantity"],
            "limit_price": row["limit_price"],
            "order_ref": row["order_ref"],
            "reconciliation_outcome": row["reconciliation_outcome"],
            "reconciled_at": row["reconciled_at"],
        }

    def submission_evidence_for_order_ref(self, order_ref: str) -> dict[str, Any] | None:
        """Resolve an order_ref observed at the broker back to its submission.

        order_ref is generated by this app and carried on every order it
        places, so a working order with no matching row here is external and
        must be left alone (AGENTS.md: never modify or interpret a manual /
        external IBKR order).
        """
        if not order_ref:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT correlation_id,created_at,action FROM broker_submissions WHERE order_ref=?",
                (order_ref,),
            ).fetchone()
        if row is None:
            return None
        return {
            "correlation_id": row["correlation_id"],
            "created_at": row["created_at"],
            "action": row["action"] or "",
        }

    def lookup_entry(self, correlation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT correlation_id,status,account,action,contract_json
                   FROM broker_submissions WHERE correlation_id=?""",
                (correlation_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "correlation_id": row["correlation_id"],
            "status": row["status"],
            "account": row["account"],
            "action": row["action"],
            "contract": _json(row["contract_json"]),
        }

    def has_unresolved_unknown(self) -> bool:
        # A SUBMISSION_UNKNOWN row that reconciliation has since resolved
        # (either outcome) no longer represents an open question about what
        # happened at the broker, so it must stop globally blocking new
        # entries. `status` itself is left untouched -- it still truthfully
        # records "this was ambiguous at submission time".
        #
        # Phase 5: a CLOSE_* row's own ambiguity is deliberately excluded
        # here -- a close/flatten SELL going SUBMISSION_UNKNOWN must not
        # globally block unrelated new opens (or unrelated closes) the way
        # an ambiguous *open* submission does; it only blocks further close/
        # flatten action on that exact contract (see has_blocking_close and
        # ExecutionEngine._verify_close_contract_not_ambiguous). A row whose
        # action is not yet known (a crash between claim() and
        # record_broker_call_evidence(), action IS NULL) is conservatively
        # still treated as blocking, since it is not provably a close.
        with self._lock:
            row = self._db.execute(
                """SELECT 1 FROM broker_submissions
                   WHERE (status='SUBMISSION_UNKNOWN' AND reconciliation_outcome IS NULL
                          AND (action IS NULL OR substr(action, 1, 6) != 'CLOSE_'))
                      OR entry_cancel_status='CANCEL_UNKNOWN'
                   LIMIT 1"""
            ).fetchone()
        return row is not None

    def blocking_open_entries(self, account: str, symbol: str, right: str) -> list[dict[str, Any]]:
        """The row detail behind has_blocking_open(): every currently-
        blocking prior OPEN submission for this exact account/symbol/right,
        with enough evidence (correlation_id, order_ref, created_at) for
        ExecutionEngine._cancel_stale_blocking_entry to decide whether it is
        stale and locate its broker order. A row whose stale-entry cancel
        has already been broker-confirmed (entry_cancel_status=
        'CANCEL_CONFIRMED') no longer reserves the symbol/right -- see
        record_entry_cancel_intent/finish_entry_cancel_confirmed below."""
        with self._lock:
            try:
                rows = self._db.execute(
                    """SELECT s.correlation_id, s.action, s.contract_json, s.order_ref, s.created_at,
                              p.lifecycle_status
                       FROM broker_submissions s
                       LEFT JOIN position_state p ON p.correlation_id = s.correlation_id
                       WHERE s.account=? AND s.action IN ('OPEN_LONG_CALL','OPEN_LONG_PUT')
                         AND (s.entry_cancel_status IS NULL OR s.entry_cancel_status != 'CANCEL_CONFIRMED')
                         AND (
                           s.status='SUBMITTED'
                           OR (s.status='SUBMISSION_UNKNOWN'
                               AND (s.reconciliation_outcome IS NULL OR s.reconciliation_outcome='CONFIRMED_FILLED'))
                         )""",
                    (account,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = self._db.execute(
                    """SELECT correlation_id, action, contract_json, order_ref, created_at,
                              NULL AS lifecycle_status
                       FROM broker_submissions
                       WHERE account=? AND action IN ('OPEN_LONG_CALL','OPEN_LONG_PUT')
                         AND (entry_cancel_status IS NULL OR entry_cancel_status != 'CANCEL_CONFIRMED')
                         AND (
                           status='SUBMITTED'
                           OR (status='SUBMISSION_UNKNOWN'
                               AND (reconciliation_outcome IS NULL OR reconciliation_outcome='CONFIRMED_FILLED'))
                         )""",
                    (account,),
                ).fetchall()
        expected_action = "OPEN_LONG_CALL" if right == "C" else "OPEN_LONG_PUT"
        matches: list[dict[str, Any]] = []
        for row in rows:
            if row["lifecycle_status"] == "CLOSED":
                continue
            contract = _json(row["contract_json"])
            if row["action"] == expected_action and contract and contract.get("symbol") == symbol:
                matches.append(
                    {
                        "correlation_id": row["correlation_id"],
                        "order_ref": row["order_ref"],
                        "created_at": row["created_at"],
                    }
                )
        return matches

    def has_blocking_open(self, account: str, symbol: str, right: str) -> bool:
        # A SUBMISSION_UNKNOWN row resolved to CONFIRMED_NO_FILL never became a
        # broker position or working order, so it must stop reserving this
        # symbol/right for future entries. CONFIRMED_FILLED (or still-
        # unresolved, reconciliation_outcome IS NULL) keeps blocking, exactly
        # as an unconditional SUBMISSION_UNKNOWN did before reconciliation
        # existed. Fully CLOSED position_state rows no longer reserve the symbol/right.
        return bool(self.blocking_open_entries(account, symbol, right))

    # ---- stale-entry cancellation (mirrors ProtectionLedger's cancel_status
    # discipline exactly): durable cancel-intent evidence before any
    # cancel_order() broker call, and a stuck 'CANCELLING' row is swept to
    # 'CANCEL_UNKNOWN' on the next restart (see __init__). Only ever invoked
    # on-demand by ExecutionEngine._cancel_stale_blocking_entry, at the exact
    # moment a genuinely stale (past the signal-age TTL) prior entry would
    # otherwise block a brand-new one -- never a proactive background sweep.

    def record_entry_cancel_intent(self, correlation_id: str) -> None:
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_submissions
                   SET entry_cancel_status='CANCELLING', updated_at=?
                   WHERE correlation_id=? AND status='SUBMITTED' AND entry_cancel_status IS NULL""",
                (_now(), correlation_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Entry submission is not available for cancel-intent evidence")

    def finish_entry_cancel_confirmed(self, correlation_id: str) -> None:
        """A proven broker cancellation: this entry no longer reserves its
        symbol/right (see blocking_open_entries). `status` is deliberately
        left at 'SUBMITTED' -- it still truthfully records that an order was
        once placed; entry_cancel_status is the separate record of its final
        broker-confirmed fate."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_submissions SET entry_cancel_status='CANCEL_CONFIRMED', updated_at=?
                   WHERE correlation_id=? AND entry_cancel_status='CANCELLING'""",
                (_now(), correlation_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Entry submission is not in a CANCELLING state")

    def finish_entry_cancel_unknown(self, correlation_id: str) -> None:
        """An ambiguous/timed-out cancel outcome on the entry order itself --
        exactly as unresolved as an initial-placement SUBMISSION_UNKNOWN
        (unlike a close/flatten protection-leg cancel, this can mean an
        unknown *new* position now exists, so it globally blocks every new
        order via has_unresolved_unknown() above, not just this symbol/
        right). Never automatically retried."""
        with self._lock:
            changed = self._db.execute(
                """UPDATE broker_submissions SET entry_cancel_status='CANCEL_UNKNOWN', updated_at=?
                   WHERE correlation_id=? AND entry_cancel_status='CANCELLING'""",
                (_now(), correlation_id),
            )
        if changed.rowcount != 1:
            raise RuntimeError("Entry submission is not in a CANCELLING state")

    def has_blocking_close(self, account: str, con_id: int) -> bool:
        """Primarily an idempotency/double-click and crash-restart guard for
        a close/flatten intent on one exact account+contract -- NOT a
        live-concurrency mechanism (ExecutionEngine's own _execution_lock
        already fully serializes true concurrent execute() calls within one
        process). Only an in-flight (SUBMITTING -- observable only via a
        crash-recovery window, since the in-process lock otherwise means a
        second execute() call can never observe another one still
        SUBMITTING) or still-unresolved-ambiguous (SUBMISSION_UNKNOWN with
        no reconciliation outcome yet) prior close/flatten blocks a new one.

        Deliberately unlike has_blocking_open: a cleanly SUBMITTED prior
        close/flatten does *not* itself block a later, independent close on
        the same contract -- unlike a duplicate open, a second reducing
        action against the same contract is not inherently a problem, and
        the position it would act on is always freshly re-verified by the
        time it runs.
        """
        with self._lock:
            rows = self._db.execute(
                """SELECT contract_json FROM broker_submissions
                   WHERE account=? AND substr(action, 1, 6)='CLOSE_'
                     AND (status='SUBMITTING'
                          OR (status='SUBMISSION_UNKNOWN' AND reconciliation_outcome IS NULL))""",
                (account,),
            ).fetchall()
        for row in rows:
            contract = _json(row["contract_json"])
            if contract and contract.get("con_id") == con_id:
                return True
        return False

    def unresolved_close_submissions(self) -> list[dict[str, Any]]:
        """Every CLOSE_* row whose broker outcome is still unresolved (in-
        flight SUBMITTING, or SUBMISSION_UNKNOWN with no reconciliation
        outcome yet) -- the same WHERE clause has_blocking_close uses for one
        exact account+contract, but globally unscoped, for a reconciliation-
        summary read across every contract at once (GET /private/v1/
        reconciliation). Read-only; never itself used as a blocking gate --
        has_blocking_close remains the sole per-contract enforcement point."""
        with self._lock:
            rows = self._db.execute(
                """SELECT correlation_id, account, action, contract_json FROM broker_submissions
                   WHERE substr(action, 1, 6)='CLOSE_'
                     AND (status='SUBMITTING'
                          OR (status='SUBMISSION_UNKNOWN' AND reconciliation_outcome IS NULL))"""
            ).fetchall()
        return [
            {
                "correlation_id": row["correlation_id"],
                "account": row["account"],
                "action": row["action"],
                "contract": _json(row["contract_json"]),
            }
            for row in rows
        ]

    def _migrate_columns(self) -> None:
        columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(broker_submissions)").fetchall()
        }
        for name in (
            "source",
            "ownership",
            "management_mode",
            "management_policy_json",
            "side",
            "quantity",
            "limit_price",
            "order_ref",
            "reconciled_at",
            "entry_cancel_status",
        ):
            if name not in columns:
                self._db.execute(f"ALTER TABLE broker_submissions ADD COLUMN {name} TEXT")
        if "reconciliation_outcome" not in columns:
            # sqlite's ALTER TABLE ADD COLUMN supports a CHECK constraint on the
            # new column (verified against the installed sqlite3 module), so this
            # narrow vocabulary is enforced by the database itself, exactly like
            # `status` above -- not just at the application layer.
            self._db.execute(
                "ALTER TABLE broker_submissions ADD COLUMN reconciliation_outcome TEXT "
                "CHECK(reconciliation_outcome IN ('CONFIRMED_FILLED','CONFIRMED_NO_FILL') "
                "OR reconciliation_outcome IS NULL)"
            )
        # Legacy rows were capture-only TradingView entries.  Preserve that
        # behavior explicitly instead of claiming app-managed exits after an
        # upgrade.
        self._db.execute(
            """UPDATE broker_submissions
               SET source=COALESCE(source,'TRADINGVIEW'),
                   ownership=COALESCE(ownership,'APP_OWNED'),
                   management_mode=COALESCE(management_mode,'ENTRY_ONLY')"""
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: str | None) -> dict[str, Any] | None:
    return json.loads(value) if value else None
