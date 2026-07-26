"""Broker-truth execution/commission capture and reconciliation ledger.

This module is purely additive to ``SubmissionRegistry``'s existing WAL-mode
SQLite database: it is constructed with that registry's already-open
``sqlite3.Connection`` and its already-held ``RLock`` (see
``SubmissionRegistry.connection``/``SubmissionRegistry.lock``) rather than
opening a second connection to the same file.

Everything here is rebuildable from ``broker_submissions`` +
``broker_executions`` + ``broker_commissions`` alone. ``position_state`` is a
cache, never a second source of truth -- ``rebuild_position_state`` always
recomputes it from the raw rows rather than incrementally mutating it, so it
can never silently diverge.

Concurrency note: ``record_execution``/``record_commission`` are called
directly from the ibapi reader thread (inside ``execDetails``/
``commissionAndFeesReport`` EWrapper callbacks). They must stay fast,
non-blocking, and free of any network or broker-socket calls -- only local
sqlite writes under the shared lock.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_executions (
  exec_id TEXT PRIMARY KEY,
  correlation_id TEXT,
  order_ref TEXT,
  order_id INTEGER,
  perm_id INTEGER,
  account TEXT,
  con_id INTEGER,
  symbol TEXT,
  side TEXT,
  shares TEXT,
  price TEXT,
  cum_qty TEXT,
  avg_price TEXT,
  exec_time TEXT,
  received_at TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('LIVE_CALLBACK','RECONCILE_SWEEP')),
  raw_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_executions_order_ref ON broker_executions(order_ref);
CREATE INDEX IF NOT EXISTS idx_broker_executions_correlation_id ON broker_executions(correlation_id);
CREATE INDEX IF NOT EXISTS idx_broker_executions_con_id ON broker_executions(con_id);

CREATE TABLE IF NOT EXISTS broker_commissions (
  exec_id TEXT PRIMARY KEY,
  commission TEXT,
  currency TEXT,
  realized_pnl TEXT,
  received_at TEXT NOT NULL,
  raw_json TEXT
);

-- Rebuildable per-correlation_id cache. Always recomputed wholesale from
-- broker_executions/broker_commissions in rebuild_position_state -- never
-- incrementally patched -- so it can never silently diverge from broker
-- truth. lifecycle_status is deliberately plain TEXT (app-validated in
-- _VALID_LIFECYCLE_STATUSES below) rather than a CHECK, so a later phase can
-- add PROTECTING/PROTECTED/MANAGING without an ALTER-time CHECK migration.
CREATE TABLE IF NOT EXISTS position_state (
  correlation_id TEXT PRIMARY KEY,
  account TEXT,
  con_id INTEGER,
  symbol TEXT,
  opened_quantity TEXT,
  closed_quantity TEXT,
  open_quantity TEXT,
  entry_avg_price TEXT,
  realized_pnl TEXT,
  total_commission TEXT,
  lifecycle_status TEXT,
  last_reconciled_at TEXT,
  closed_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  "trigger" TEXT NOT NULL CHECK("trigger" IN ('STARTUP','PERIODIC')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  executions_ingested INTEGER,
  unresolved_after INTEGER,
  notes TEXT
);
"""

EXECUTION_SOURCES = ("LIVE_CALLBACK", "RECONCILE_SWEEP")
RECONCILIATION_TRIGGERS = ("STARTUP", "PERIODIC")
RECONCILIATION_OUTCOMES = ("CONFIRMED_FILLED", "CONFIRMED_NO_FILL")
# PROTECTING/PROTECTED/MANAGING are Phase 3/4's concern -- deliberately absent.
_VALID_LIFECYCLE_STATUSES = frozenset(
    {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CLOSING", "CLOSED"}
)
def _normalized_execution_time(value: str | None) -> str | None:
    """Return an absolute execution timestamp, or None when IBKR evidence is ambiguous.

    Accepts ISO-8601 with an offset, and IBKR's own execution format with an
    explicit zone ("20260724 10:17:23 US/Eastern").

    A *naive* IBKR timestamp ("20260724 10:17:23") deliberately returns None.
    Reporting in New York time does not prove the callback used New York time,
    and this value becomes ``positions.closed_at``, which keys the operator's
    daily realized P&L. Guessing a zone here would silently re-date a fill; a
    None is rendered as Unavailable, which is the honest outcome.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except ValueError:
        pass

    parts = text.split()
    if len(parts) < 3:
        return None
    date_str, time_str, tz_str = parts[0], parts[1], parts[2]
    date_clean = date_str.replace("-", "")
    if len(date_clean) != 8 or len(time_str) != 8:
        return None
    try:
        zone = ZoneInfo("America/New_York" if tz_str == "US/Eastern" else tz_str)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    try:
        naive = datetime.strptime(f"{date_clean} {time_str}", "%Y%m%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=zone).astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ExecutionRecord:
    exec_id: str
    order_ref: str | None
    order_id: int | None
    perm_id: int | None
    account: str
    con_id: int | None
    symbol: str
    side: str
    shares: str
    price: str
    cum_qty: str | None
    avg_price: str | None
    exec_time: str
    source: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class CommissionRecord:
    exec_id: str
    commission: str
    currency: str
    realized_pnl: str | None
    raw: dict[str, Any]


class ExecutionLedger:
    """Executions, commissions, rebuildable position cache, and reconciliation audit trail."""

    def __init__(self, connection: sqlite3.Connection, lock: RLock):
        self._db = connection
        self._lock = lock
        with self._lock:
            self._db.executescript(LEDGER_SCHEMA)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(position_state)").fetchall()}
            if "closed_at" not in columns:
                self._db.execute("ALTER TABLE position_state ADD COLUMN closed_at TEXT")

    # ---- ingestion (called from the ibapi reader thread) ---------------

    def record_execution(self, record: ExecutionRecord) -> bool:
        """Idempotent append-only insert. Returns False for a duplicate
        redelivery of an already-known exec_id (a no-op, not an error)."""
        if record.source not in EXECUTION_SOURCES:
            raise ValueError("Unsupported execution source")
        now = _now()
        with self._lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO broker_executions
                   (exec_id,correlation_id,order_ref,order_id,perm_id,account,con_id,symbol,side,
                    shares,price,cum_qty,avg_price,exec_time,received_at,source,raw_json,created_at)
                   VALUES (?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.exec_id,
                    record.order_ref,
                    record.order_id,
                    record.perm_id,
                    record.account,
                    record.con_id,
                    record.symbol,
                    record.side,
                    record.shares,
                    record.price,
                    record.cum_qty,
                    record.avg_price,
                    record.exec_time,
                    now,
                    record.source,
                    json.dumps(record.raw, separators=(",", ":"), sort_keys=True, default=str),
                    now,
                ),
            )
            if cursor.rowcount == 0:
                return False
            correlation_id = self._match_order_ref(record.order_ref) if record.order_ref else None
            if correlation_id:
                self._db.execute(
                    "UPDATE broker_executions SET correlation_id=? WHERE exec_id=?",
                    (correlation_id, record.exec_id),
                )
            if correlation_id:
                self._rebuild_position_state_locked(
                    correlation_id, mark_reconciled=(record.source == "RECONCILE_SWEEP")
                )
        return True

    def record_commission(self, record: CommissionRecord) -> bool:
        """Idempotent insert. Commission may legitimately arrive before or
        after its execution row -- both orderings are handled: if the
        execution isn't known yet, the row is still stored (never dropped)
        and position_state is simply not rebuilt until the execution arrives
        and its own record_execution call rebuilds using this commission."""
        now = _now()
        with self._lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO broker_commissions
                   (exec_id,commission,currency,realized_pnl,received_at,raw_json)
                   VALUES (?,?,?,?,?,?)""",
                (
                    record.exec_id,
                    record.commission,
                    record.currency,
                    record.realized_pnl,
                    now,
                    json.dumps(record.raw, separators=(",", ":"), sort_keys=True, default=str),
                ),
            )
            if cursor.rowcount == 0:
                return False
            row = self._db.execute(
                "SELECT correlation_id FROM broker_executions WHERE exec_id=?", (record.exec_id,)
            ).fetchone()
            correlation_id = row["correlation_id"] if row else None
            if correlation_id:
                self._rebuild_position_state_locked(correlation_id, mark_reconciled=False)
        return True

    # ---- correlation backfill -------------------------------------------

    def backfill_missing_correlation_ids(self) -> int:
        """Re-attempt order_ref -> correlation_id matching for any execution
        still missing it (a race with the entry's own durable write, or an
        execution that predates the matching order_ref becoming known).
        Returns the number of distinct correlation_ids newly attributed."""
        with self._lock:
            rows = self._db.execute(
                """SELECT exec_id, order_ref FROM broker_executions
                   WHERE correlation_id IS NULL AND order_ref IS NOT NULL AND order_ref != ''"""
            ).fetchall()
            resolved: set[str] = set()
            for row in rows:
                correlation_id = self._match_order_ref(row["order_ref"])
                if correlation_id:
                    self._db.execute(
                        "UPDATE broker_executions SET correlation_id=? WHERE exec_id=?",
                        (correlation_id, row["exec_id"]),
                    )
                    resolved.add(correlation_id)
            for correlation_id in resolved:
                self._rebuild_position_state_locked(correlation_id, mark_reconciled=False)
        return len(resolved)

    def _match_order_ref(self, order_ref: str) -> str | None:
        row = self._db.execute(
            "SELECT correlation_id FROM broker_submissions WHERE order_ref=?", (order_ref,)
        ).fetchone()
        if row and row["correlation_id"]:
            return row["correlation_id"]
        try:
            row = self._db.execute(
                "SELECT correlation_id FROM broker_protection_orders WHERE order_ref=?", (order_ref,)
            ).fetchone()
            return row["correlation_id"] if row else None
        except sqlite3.OperationalError:
            return None

    # ---- position_state (rebuildable cache) -----------------------------

    def rebuild_position_state(self, correlation_id: str, *, mark_reconciled: bool = False) -> None:
        with self._lock:
            self._rebuild_position_state_locked(correlation_id, mark_reconciled=mark_reconciled)

    def _rebuild_position_state_locked(self, correlation_id: str, *, mark_reconciled: bool) -> None:
        submission = self._db.execute(
            "SELECT account,contract_json,quantity FROM broker_submissions WHERE correlation_id=?",
            (correlation_id,),
        ).fetchone()
        exec_rows = self._db.execute(
            "SELECT side,shares,price,con_id,symbol,account,exec_time FROM broker_executions WHERE correlation_id=? "
            "ORDER BY exec_id",
            (correlation_id,),
        ).fetchall()
        now = _now()
        if not exec_rows:
            # Nothing to rebuild from. Never leave a stale cache row behind.
            self._db.execute("DELETE FROM position_state WHERE correlation_id=?", (correlation_id,))
            return
        opened = Decimal("0")
        closed = Decimal("0")
        opened_notional = Decimal("0")
        con_id = None
        symbol = None
        account = None
        close_times: list[str] = []
        for row in exec_rows:
            shares = Decimal(row["shares"])
            price = Decimal(row["price"])
            con_id = row["con_id"] if con_id is None else con_id
            symbol = row["symbol"] if symbol is None else symbol
            account = row["account"] if account is None else account
            if row["side"] == "BOT":
                opened += shares
                opened_notional += shares * price
            elif row["side"] == "SLD":
                closed += shares
                normalized = _normalized_execution_time(row["exec_time"])
                if normalized is not None:
                    close_times.append(normalized)
        entry_avg_price = str(opened_notional / opened) if opened > 0 else None
        open_quantity = opened - closed

        commission_rows = self._db.execute(
            """SELECT c.commission, c.realized_pnl FROM broker_commissions c
               JOIN broker_executions e ON e.exec_id = c.exec_id
               WHERE e.correlation_id=?""",
            (correlation_id,),
        ).fetchall()
        commissions = [Decimal(row["commission"]) for row in commission_rows if row["commission"] is not None]
        total_commission = str(sum(commissions, Decimal("0"))) if commissions else None
        realized_values = [Decimal(row["realized_pnl"]) for row in commission_rows if row["realized_pnl"] is not None]
        realized_pnl = str(sum(realized_values, Decimal("0"))) if realized_values else None

        target_quantity = None
        if submission is not None and submission["quantity"] is not None:
            try:
                target_quantity = Decimal(submission["quantity"])
            except InvalidOperation:
                target_quantity = None

        if closed > 0 and open_quantity <= 0:
            lifecycle_status = "CLOSED"
        elif closed > 0 and open_quantity > 0:
            lifecycle_status = "CLOSING"
        elif target_quantity is not None and target_quantity > 0 and opened >= target_quantity:
            lifecycle_status = "FILLED"
        else:
            lifecycle_status = "PARTIALLY_FILLED"
        assert lifecycle_status in _VALID_LIFECYCLE_STATUSES

        submission_account = submission["account"] if submission else None
        last_reconciled_at = now if mark_reconciled else None
        # Never substitute cache/reconciliation update time for the broker
        # execution timestamp. A CLOSED row without a parseable close fill is
        # intentionally surfaced as unavailable to date-scoped operator views.
        closed_at = max(close_times) if lifecycle_status == "CLOSED" and close_times else None
        self._db.execute(
            """INSERT INTO position_state
               (correlation_id,account,con_id,symbol,opened_quantity,closed_quantity,open_quantity,
                entry_avg_price,realized_pnl,total_commission,lifecycle_status,last_reconciled_at,closed_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(correlation_id) DO UPDATE SET
                 account=excluded.account, con_id=excluded.con_id, symbol=excluded.symbol,
                 opened_quantity=excluded.opened_quantity, closed_quantity=excluded.closed_quantity,
                 open_quantity=excluded.open_quantity, entry_avg_price=excluded.entry_avg_price,
                 realized_pnl=excluded.realized_pnl, total_commission=excluded.total_commission,
                 lifecycle_status=excluded.lifecycle_status,
                 last_reconciled_at=COALESCE(excluded.last_reconciled_at, position_state.last_reconciled_at),
                 closed_at=excluded.closed_at,
                 updated_at=excluded.updated_at""",
            (
                correlation_id,
                submission_account or account,
                con_id,
                symbol,
                str(opened),
                str(closed),
                str(open_quantity),
                entry_avg_price,
                realized_pnl,
                total_commission,
                lifecycle_status,
                last_reconciled_at,
                closed_at,
                now,
            ),
        )

    def position_state(self, correlation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM position_state WHERE correlation_id=?", (correlation_id,)
            ).fetchone()
        return dict(row) if row else None

    def positions(self) -> list[dict[str, Any]]:
        """Every current ``position_state`` row (all correlation_ids), most
        recently updated first -- the read backing the operator UI's Active
        Positions list. Same rebuildable cache used everywhere else in this
        module, never a second source of truth; ``last_reconciled_at`` is
        whatever this cache actually holds (frequently ``None`` for a
        correlation_id whose evidence has only ever arrived via
        LIVE_CALLBACK, never a RECONCILE_SWEEP) -- never fabricated here."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM position_state ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def filled_app_managed_correlation_ids(self) -> list[str]:
        """Candidates for protection placement (Phase 3): every correlation_id
        whose rebuildable position_state cache currently reads FILLED and
        whose immutably-snapshotted broker_submissions row opted into
        APP_MANAGED. Level-triggered re-evaluation (not edge-triggered from a
        fill callback) relies on this being cheap to recompute every sweep."""
        with self._lock:
            rows = self._db.execute(
                """SELECT p.correlation_id FROM position_state p
                   JOIN broker_submissions s ON s.correlation_id = p.correlation_id
                   WHERE p.lifecycle_status='FILLED' AND s.management_mode='APP_MANAGED'"""
            ).fetchall()
        return [row["correlation_id"] for row in rows]

    # ---- reconciliation read helpers -------------------------------------

    def unresolved_unknown_submissions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT correlation_id, order_ref, account, contract_json
                   FROM broker_submissions
                   WHERE status='SUBMISSION_UNKNOWN' AND reconciliation_outcome IS NULL"""
            ).fetchall()
        return [
            {
                "correlation_id": row["correlation_id"],
                "order_ref": row["order_ref"],
                "account": row["account"],
                "contract": json.loads(row["contract_json"]) if row["contract_json"] else None,
            }
            for row in rows
        ]

    def executions_for_order_ref(self, order_ref: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT exec_id, shares, cum_qty FROM broker_executions WHERE order_ref=?", (order_ref,)
            ).fetchall()
        return [dict(row) for row in rows]

    def executions_for_con_id(self, con_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT exec_id, correlation_id FROM broker_executions WHERE con_id=?", (con_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def executions_for_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        """Full fill history for one entry -- the operator UI's Executions
        read. Oldest first, so a UI rendering avg-price/fill progression in
        order needs no client-side re-sort."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM broker_executions WHERE correlation_id=? ORDER BY exec_id", (correlation_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def commissions_for_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        """Every commission row attached (via exec_id) to one entry's fill
        history -- joined through broker_executions since broker_commissions
        itself carries no correlation_id column."""
        with self._lock:
            rows = self._db.execute(
                """SELECT c.* FROM broker_commissions c
                   JOIN broker_executions e ON e.exec_id = c.exec_id
                   WHERE e.correlation_id=? ORDER BY c.exec_id""",
                (correlation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_execution(self, exec_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM broker_executions WHERE exec_id=?", (exec_id,)).fetchone()
        return dict(row) if row else None

    def get_commission(self, exec_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM broker_commissions WHERE exec_id=?", (exec_id,)).fetchone()
        return dict(row) if row else None

    def mark_reconciliation_outcome(self, correlation_id: str, outcome: str) -> bool:
        """Resolve an unresolved SUBMISSION_UNKNOWN row. Returns False (no-op)
        if the row is missing, already resolved, or not SUBMISSION_UNKNOWN --
        this never overwrites an already-recorded outcome."""
        if outcome not in RECONCILIATION_OUTCOMES:
            raise ValueError("Unsupported reconciliation outcome")
        with self._lock:
            cursor = self._db.execute(
                """UPDATE broker_submissions SET reconciliation_outcome=?, reconciled_at=?
                   WHERE correlation_id=? AND status='SUBMISSION_UNKNOWN' AND reconciliation_outcome IS NULL""",
                (outcome, _now(), correlation_id),
            )
        return cursor.rowcount == 1

    # ---- reconciliation audit trail --------------------------------------

    def start_reconciliation_run(self, trigger: str) -> int:
        if trigger not in RECONCILIATION_TRIGGERS:
            raise ValueError("Unsupported reconciliation trigger")
        with self._lock:
            cursor = self._db.execute(
                """INSERT INTO reconciliation_runs ("trigger",started_at,executions_ingested,unresolved_after)
                   VALUES (?,?,0,0)""",
                (trigger, _now()),
            )
            return int(cursor.lastrowid)

    def complete_reconciliation_run(
        self, run_id: int, *, executions_ingested: int, unresolved_after: int, notes: str | None = None
    ) -> None:
        with self._lock:
            self._db.execute(
                """UPDATE reconciliation_runs
                   SET completed_at=?, executions_ingested=?, unresolved_after=?, notes=?
                   WHERE run_id=?""",
                (_now(), executions_ingested, unresolved_after, notes, run_id),
            )

    def get_reconciliation_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM reconciliation_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def recent_reconciliation_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent runs first -- the operator UI's reconciliation-summary
        read. Read-only; never used to drive any scheduling decision itself
        (see __main__.py's own periodic-sweep loop for that)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reconciliation_runs ORDER BY run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


def _now() -> str:
    return datetime.now(UTC).isoformat()
