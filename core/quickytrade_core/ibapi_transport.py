"""Official Interactive Brokers TWS Python API transport.

``ibapi`` is intentionally optional at import time so the deterministic unit
suite can run without redistributing IBKR artifacts.  Production startup fails
closed unless the user has installed the official API package.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .config import LIVE_PORTS, PAPER_PORTS, CoreConfig, is_live_account, is_paper_account
from .domain import (
    BrokerAcknowledgement,
    BrokerAmbiguousError,
    BrokerDefinitiveError,
    CancelAcknowledgement,
    LimitOrderRequest,
    OptionChain,
    Position,
    PriceIncrement,
    QualifiedContract,
    Quote,
    Readiness,
    StopLimitOrderRequest,
    WorkingOrder,
)
from .execution_ledger import CommissionRecord, ExecutionLedger, ExecutionRecord

logger = logging.getLogger(__name__)

try:  # Official package; never silently substitute a third-party wrapper.
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.execution import ExecutionFilter
    from ibapi.order import Order
    from ibapi.order_cancel import OrderCancel
    from ibapi.wrapper import EWrapper
    _IBAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised through availability check
    _IBAPI_AVAILABLE = False

    class EWrapper:  # type: ignore[no-redef]
        pass

    class EClient:  # type: ignore[no-redef]
        pass

    Contract = None  # type: ignore[assignment]
    Order = None  # type: ignore[assignment]
    ExecutionFilter = None  # type: ignore[assignment]
    OrderCancel = None  # type: ignore[assignment]

# reqCompletedOrders/completedOrder/completedOrdersEnd are present in the
# installed ibapi 10.37.2 (confirmed by reading ibapi/client.py and
# ibapi/wrapper.py directly). Guarded via hasattr rather than assumed, so a
# different installed version that lacks it degrades to "documented gap,
# skipped" (see _reconcile_completed_orders) instead of guessing at a
# workaround.
_COMPLETED_ORDERS_AVAILABLE = _IBAPI_AVAILABLE and hasattr(EClient, "reqCompletedOrders")


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    values: list[Any] = field(default_factory=list)
    error: tuple[int, str] | None = None


@dataclass
class _OrderAck:
    event: threading.Event = field(default_factory=threading.Event)
    raw_status: str | None = None
    perm_id: int | None = None
    parent_id: int | None = None
    oca_group: str = ""
    error: tuple[int, str] | None = None
    warnings: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class _CancelAck:
    """Mirrors ``_OrderAck``'s wait/resolve shape for ``cancelOrder`` --
    tracked in a dedicated map (``_cancel_acks``) rather than reusing
    ``_order_acks``, since an already-placed order's original placement ack
    is long gone (popped in ``_submit_order``'s ``finally``) by the time it
    is ever cancelled."""

    event: threading.Event = field(default_factory=threading.Event)
    raw_status: str | None = None
    error: tuple[int, str] | None = None


ACK_STATUSES = {"PendingSubmit", "PreSubmitted", "Submitted"}
TERMINAL_ORDER_STATUSES = {"ApiCancelled", "Cancelled", "Filled", "Inactive"}
# Only these two terminal statuses count as a *confirmed cancellation* for
# cancel_order() below. "Filled"/"Inactive" are also terminal but are a
# different (non-cancellation) resolved outcome -- deliberately still
# treated as ambiguous by cancel_order() (see its docstring) so a caller
# that must never oversell (FULL_FLATTEN) always fails closed rather than
# guessing what a same-race fill implies for the still-open position.
CANCEL_CONFIRMED_STATUSES = {"Cancelled", "ApiCancelled"}
# IBKR's historical cancel-confirmation channel: some TWS/Gateway versions
# report a successful cancellation only via the informational error()
# callback with code 202 ("Order Canceled - Reason:"), not (only) via
# orderStatus. Both channels are treated as equally authoritative below.
IBKR_CANCEL_CONFIRMATION_ERROR_CODE = 202
WARNING_CODES = {399, 2109, 2137, 2150, 2168, 2169}
INFORMATIONAL_CODES = {2104, 2106, 2107, 2108, 2158}
CONNECTION_LOSS_CODES = {1100, 1101, 1102, 1300}
# A completed-order terminal status that unambiguously means "no execution
# ever happened" for reconciliation purposes. 'Filled' obviously implies a
# fill (handled via broker_executions instead, which carries the actual fill
# detail). 'Inactive' is deliberately excluded -- it can follow a partial
# fill (e.g. rejected after a partial execution), so it is not safe to treat
# as "definitely no fill" without execution evidence.
NO_FILL_TERMINAL_STATUSES = {"ApiCancelled", "Cancelled"}
# IBKR's UNSET_DOUBLE sentinel (sys.float_info.max) marks a CommissionAndFeesReport
# field -- most commonly realizedPNL -- as "not applicable to this execution",
# never a real value of that magnitude. Compare with a generous relative
# threshold rather than exact equality so it also catches the sentinel
# surviving a lossy float round-trip.
_UNSET_DOUBLE_THRESHOLD = sys.float_info.max / 2


class OfficialIbapiTransport(EWrapper, EClient):  # type: ignore[misc]
    def __init__(
        self,
        config: CoreConfig,
        ledger: ExecutionLedger,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if not _IBAPI_AVAILABLE:
            raise RuntimeError("Official IBKR TWS Python API package 'ibapi' is not installed")
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self.config = config
        self.ledger = ledger
        self.clock = clock
        self._lock = threading.RLock()
        self._handshake = threading.Event()
        self._server_time = threading.Event()
        self._managed_accounts_event = threading.Event()
        self._requests: dict[int, _Pending] = {}
        self._order_acks: dict[int, _OrderAck] = {}
        self._cancel_acks: dict[int, _CancelAck] = {}
        self._next_request_id = 10_000
        self._next_order_id: int | None = None
        self._managed_accounts: tuple[str, ...] = ()
        self._positions: list[Position] = []
        self._position_event = threading.Event()
        self._working_orders: dict[int, WorkingOrder] = {}
        self._open_order_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconciled = False
        self._blocking_reason: str | None = None
        # Reconciliation state -- see execDetails/_reconcile_executions for the
        # reqId used to distinguish an unsolicited live fill from an execution
        # returned in response to our own reqExecutions sweep call.
        self._exec_sweep_request_id: int | None = None
        self._completed_orders: dict[str, dict[str, Any]] = {}
        self._completed_orders_event = threading.Event()

    @property
    def client_id(self) -> int:
        return self.config.client_id

    def start(self) -> None:
        self.config.validate()
        if self.isConnected():
            return
        self.connect(self.config.ibkr_host, self.config.ibkr_port, clientId=self.config.client_id)
        if not self.isConnected():
            raise RuntimeError("Could not start the official IBKR socket connection")
        self._thread = threading.Thread(target=self.run, name="quickytrade-ibapi", daemon=True)
        self._thread.start()
        timeout = self.config.request_timeout_seconds
        if not self._handshake.wait(timeout):
            self.disconnect()
            raise RuntimeError("IBKR nextValidId handshake timed out")
        self.reqManagedAccts()
        self.reqCurrentTime()
        if not self._managed_accounts_event.wait(timeout) or not self._server_time.wait(timeout):
            self.disconnect()
            raise RuntimeError("IBKR managed-account/server-time readiness timed out")
        self._refresh_positions()
        self._refresh_working_orders()
        self.reconcile("STARTUP")
        self._reconciled = True

    def stop(self) -> None:
        self._reconciled = False
        if self.isConnected():
            try:
                self.cancelPositions()
            finally:
                self.disconnect()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def readiness(self) -> Readiness:
        verified_paper = (
            self.config.ibkr_port in PAPER_PORTS
            and is_paper_account(self.config.selected_account)
            and self.config.selected_account in self._managed_accounts
        )
        verified_live = (
            self.config.ibkr_port in LIVE_PORTS
            and is_live_account(self.config.selected_account)
            and self.config.selected_account in self._managed_accounts
        )
        environment = "PAPER" if verified_paper else "LIVE" if verified_live else "UNKNOWN"
        return Readiness(
            connected=bool(self.isConnected()),
            handshake_complete=self._handshake.is_set(),
            server_time_received=self._server_time.is_set(),
            reconciled=self._reconciled,
            managed_accounts=self._managed_accounts,
            environment=environment,
            # The socket protocol does not expose TWS's read-only checkbox.  A
            # read-only rejection is still handled as a definitive broker block.
            read_only=None,
            blocking_reason=self._blocking_reason,
        )

    def qualify_underlying(self, symbol: str) -> QualifiedContract:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        details = self._contract_details(contract)
        if len(details) != 1:
            raise BrokerDefinitiveError(
                "UNDERLYING_QUALIFICATION_AMBIGUOUS", "IBKR did not return exactly one underlying"
            )
        return _qualified(details[0])

    def option_chains(self, underlying: QualifiedContract) -> tuple[OptionChain, ...]:
        request_id, pending = self._new_request()
        self.reqSecDefOptParams(request_id, underlying.symbol, "", underlying.sec_type, underlying.con_id)
        self._wait(pending, request_id, "OPTION_CHAIN_REQUEST_FAILED")
        return tuple(pending.values)

    def qualify_option(
        self,
        *,
        underlying: QualifiedContract,
        expiry: str,
        strike: Decimal,
        right: str,
        exchange: str,
        trading_class: str,
        multiplier: str,
    ) -> QualifiedContract:
        contract = Contract()
        contract.symbol = underlying.symbol
        contract.secType = "OPT"
        contract.lastTradeDateOrContractMonth = expiry
        contract.strike = float(strike)
        contract.right = right
        contract.multiplier = multiplier
        contract.exchange = exchange
        contract.currency = "USD"
        contract.tradingClass = trading_class
        details = self._contract_details(contract)
        if len(details) != 1:
            raise BrokerDefinitiveError(
                "OPTION_QUALIFICATION_AMBIGUOUS", "IBKR did not return exactly one option contract"
            )
        return _qualified(details[0])

    def quote(self, contract: QualifiedContract) -> Quote:
        request_id, pending = self._new_request()
        pending.values.append({
            "bid": None, "ask": None, "delta": None,
            "market_data_type": "LIVE", "received_at": self.clock(),
        })
        # Generic tick 106 requests option-computation ticks (tickOptionComputation,
        # including delta) alongside the plain bid/ask; IBKR ignores it for non-option
        # contracts, so it is safe to request unconditionally. IBKR rejects snapshot=True
        # combined with any non-empty generic tick list ("Snapshot market data subscription
        # is not applicable to generic ticks"), so this must be a streaming request --
        # tickPrice() already completes the wait once both bid and ask arrive, and the
        # `finally` below cancels the subscription regardless.
        self.reqMktData(request_id, _ib_contract(contract), "106", False, False, [])
        try:
            self._wait(pending, request_id, "MARKET_DATA_REQUEST_FAILED")
        finally:
            self.cancelMktData(request_id)
        value = pending.values[0]
        return Quote(
            bid=value["bid"],
            ask=value["ask"],
            received_at=value["received_at"],
            market_data_type=value["market_data_type"],
            delta=value["delta"],
        )

    def market_rule(self, contract: QualifiedContract) -> tuple[PriceIncrement, ...]:
        rule_id = _market_rule_id(contract)
        request_id, pending = self._new_request(explicit_id=rule_id)
        self.reqMarketRule(rule_id)
        self._wait(pending, request_id, "MARKET_RULE_REQUEST_FAILED")
        return tuple(pending.values)

    def positions(self, account: str) -> tuple[Position, ...]:
        self._refresh_positions()
        return tuple(position for position in self._positions if position.account == account)

    def working_orders(self, account: str) -> tuple[WorkingOrder, ...]:
        self._refresh_working_orders()
        return tuple(order for order in self._working_orders.values() if order.account == account)

    def place_limit_order(self, request: LimitOrderRequest) -> BrokerAcknowledgement:
        order = self._base_order(
            account=request.account,
            action=request.action,
            quantity=request.quantity,
            tif=request.tif,
            outside_rth=request.outside_rth,
            order_ref=request.order_ref,
            oca_group=request.oca_group,
            oca_type=request.oca_type,
        )
        order.orderType = "LMT"
        order.lmtPrice = float(request.limit_price)
        return self._submit_order(request.contract, order)

    def place_stop_limit_order(self, request: StopLimitOrderRequest) -> BrokerAcknowledgement:
        # STP LMT, never a plain market-triggered STP -- this codebase never
        # places market-priced orders (see docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md).
        order = self._base_order(
            account=request.account,
            action=request.action,
            quantity=request.quantity,
            tif=request.tif,
            outside_rth=request.outside_rth,
            order_ref=request.order_ref,
            oca_group=request.oca_group,
            oca_type=request.oca_type,
        )
        order.orderType = "STP LMT"
        order.auxPrice = float(request.trigger_price)
        order.lmtPrice = float(request.limit_price)
        return self._submit_order(request.contract, order)

    def modify_stop_limit_order(self, order_id: int, request: StopLimitOrderRequest) -> BrokerAcknowledgement:
        """Modify an existing working stop-limit order **in place**: re-sends
        placeOrder with the SAME order id and updated trigger/limit price --
        IBKR's own order-modification mechanism (never cancel+replace), so
        the leg is never briefly unprotected mid-modification. Used only by
        Phase 4's management-transition application (MOVE_STOP_TO_BREAKEVEN /
        TRAIL_FRESH_BID reactions in ExecutionEngine.ensure_transitions);
        place_stop_limit_order's own behavior/signature is unchanged for
        every other (fresh-order) caller."""
        order = self._base_order(
            account=request.account,
            action=request.action,
            quantity=request.quantity,
            tif=request.tif,
            outside_rth=request.outside_rth,
            order_ref=request.order_ref,
            oca_group=request.oca_group,
            oca_type=request.oca_type,
        )
        order.orderType = "STP LMT"
        order.auxPrice = float(request.trigger_price)
        order.lmtPrice = float(request.limit_price)
        return self._submit_order(request.contract, order, order_id=order_id)

    def cancel_order(self, order_id: int) -> CancelAcknowledgement:
        """Cancel an existing working order and wait for a definitive
        broker confirmation (used only by Phase 5's FULL_FLATTEN protection-
        leg cancellation -- ExecutionEngine._cancel_protection_leg). Mirrors
        _submit_order's ack-wait/ambiguity handling, but against the
        dedicated _cancel_acks map and IBKR's own cancellation-confirmation
        channels: an orderStatus callback reporting Cancelled/ApiCancelled,
        or the informational error() code 202 some TWS/Gateway versions use
        instead. Deliberately conservative: a timeout, disconnect, or any
        other resolved-but-not-Cancelled terminal status (e.g. the order was
        Filled during the cancel race) all raise BrokerAmbiguousError rather
        than ever returning a value -- see CANCEL_CONFIRMED_STATUSES above.
        """
        if not self.isConnected():
            raise BrokerAmbiguousError("IBKR_DISCONNECTED_BEFORE_CANCEL", "IBKR socket is disconnected")
        ack = _CancelAck()
        with self._lock:
            self._cancel_acks[order_id] = ack
        try:
            self.cancelOrder(order_id, OrderCancel())
            if not ack.event.wait(self.config.acknowledgement_timeout_seconds):
                raise BrokerAmbiguousError(
                    "IBKR_CANCEL_ACK_TIMEOUT", "No definitive IBKR cancel confirmation arrived before timeout"
                )
            if ack.error:
                _code, message = ack.error
                raise BrokerAmbiguousError("IBKR_CANCEL_ACK_ERROR", message)
            if ack.raw_status not in CANCEL_CONFIRMED_STATUSES:
                raise BrokerAmbiguousError(
                    "IBKR_CANCEL_OUTCOME_UNCONFIRMED", "IBKR callback did not prove order cancellation"
                )
            return CancelAcknowledgement(order_id=order_id, raw_status=ack.raw_status)
        finally:
            with self._lock:
                self._cancel_acks.pop(order_id, None)

    def _base_order(
        self,
        *,
        account: str,
        action: str,
        quantity: int,
        tif: str,
        outside_rth: bool,
        order_ref: str,
        oca_group: str,
        oca_type: int,
    ):
        order = Order()
        order.account = account
        order.action = action
        order.totalQuantity = quantity
        order.tif = tif
        order.outsideRth = outside_rth
        order.orderRef = order_ref
        order.transmit = True
        if oca_group:
            order.ocaGroup = oca_group
            order.ocaType = oca_type
        return order

    def _submit_order(
        self, contract: QualifiedContract, order, *, order_id: int | None = None
    ) -> BrokerAcknowledgement:
        """Places (order_id is None -- a fresh id is allocated) or modifies
        (order_id is an existing, already-acknowledged order id -- IBKR
        treats a repeated placeOrder on the same id as an in-place
        modification) an order. Either way the acknowledgement wait/error/
        ambiguity handling below is identical, since IBKR's callback protocol
        for a modify is the same openOrder/orderStatus/error flow as a fresh
        placement."""
        if not self.isConnected():
            raise BrokerAmbiguousError("IBKR_DISCONNECTED_BEFORE_PLACE", "IBKR socket is disconnected")
        if order_id is None:
            order_id = self._allocate_order_id()
        ack = _OrderAck()
        with self._lock:
            self._order_acks[order_id] = ack
        try:
            self.placeOrder(order_id, _ib_contract(contract), order)
            if not ack.event.wait(self.config.acknowledgement_timeout_seconds):
                raise BrokerAmbiguousError(
                    "IBKR_ACK_TIMEOUT", "No definitive IBKR acknowledgement arrived before timeout"
                )
            if ack.error:
                code, message = ack.error
                raise BrokerDefinitiveError("IBKR_ORDER_REJECTED", message, broker_code=code)
            if ack.raw_status not in ACK_STATUSES:
                raise BrokerAmbiguousError(
                    "IBKR_ACK_INDETERMINATE", "IBKR callback did not prove order acknowledgement"
                )
            return BrokerAcknowledgement(
                order_id=order_id,
                client_id=self.config.client_id,
                perm_id=ack.perm_id,
                raw_status=ack.raw_status,
                parent_id=ack.parent_id,
                oca_group=ack.oca_group,
                warnings=tuple(ack.warnings),
            )
        finally:
            with self._lock:
                self._order_acks.pop(order_id, None)

    # ---- Reconciliation sweep -----------------------------------------
    #
    # Runs once at startup (before _reconciled flips True) and periodically
    # thereafter (see __main__.py). Pure data capture + resolution of
    # already-ambiguous SUBMISSION_UNKNOWN rows -- this never places,
    # modifies, or cancels an order.

    def reconcile(self, trigger: str) -> dict[str, Any]:
        run_id = self.ledger.start_reconciliation_run(trigger)
        executions_ingested = self._reconcile_executions()
        completed_orders = self._reconcile_completed_orders() if _COMPLETED_ORDERS_AVAILABLE else {}
        self.ledger.backfill_missing_correlation_ids()
        self._resolve_unknown_submissions(completed_orders)
        # Fresh position evidence for the cross-day fallback below -- IBKR is
        # always re-queried rather than trusting whatever self._positions held
        # before this sweep began.
        self._refresh_positions()
        flagged = self._flag_unattributed_positions()
        if flagged:
            logger.warning(
                "Reconciliation sweep (%s) found %d unattributed broker position(s) with no "
                "corresponding CONFIRMED_FILLED evidence in the ledger; left unresolved by design: %s",
                trigger, len(flagged), flagged,
            )
        unresolved_after = len(self.ledger.unresolved_unknown_submissions())
        notes = json.dumps({"unattributed_positions": flagged}) if flagged else None
        self.ledger.complete_reconciliation_run(
            run_id, executions_ingested=executions_ingested, unresolved_after=unresolved_after, notes=notes
        )
        return {
            "run_id": run_id,
            "executions_ingested": executions_ingested,
            "unresolved_after": unresolved_after,
            "unattributed_positions": flagged,
        }

    def _reconcile_executions(self) -> int:
        """Backfill same-day fills via reqExecutions.

        Documented, permanent limitation: per IBKR's own reqExecutions
        documentation ("To view executions beyond the past 24 hours, open the
        Trade Log in TWS..."), this call is scoped to roughly the current
        trading day for the account. It cannot resolve a SUBMISSION_UNKNOWN
        left over from a prior calendar day -- that gap is handled (as an
        explicitly unresolved, operator-visible flag, never an auto-resolved
        guess) by the cross-day fallback in _flag_unattributed_positions.
        """
        if not self.isConnected():
            return 0
        request_id, pending = self._new_request()
        self._exec_sweep_request_id = request_id
        try:
            execution_filter = ExecutionFilter()
            execution_filter.clientId = self.config.client_id
            self.reqExecutions(request_id, execution_filter)
            if not pending.event.wait(self.config.request_timeout_seconds):
                return 0
        finally:
            with self._lock:
                self._requests.pop(request_id, None)
                self._exec_sweep_request_id = None
        return len(pending.values)

    def _reconcile_completed_orders(self) -> dict[str, dict[str, Any]]:
        """Backfill order-level completion status via reqCompletedOrders.

        Confirmed present in the installed ibapi 10.37.2
        (EClient.reqCompletedOrders / EWrapper.completedOrder /
        completedOrdersEnd); guarded by _COMPLETED_ORDERS_AVAILABLE so a
        future/older install lacking it degrades to a documented skip rather
        than a guessed workaround.
        """
        if not self.isConnected():
            return {}
        with self._lock:
            self._completed_orders = {}
        self._completed_orders_event.clear()
        self.reqCompletedOrders(apiOnly=True)
        if not self._completed_orders_event.wait(self.config.request_timeout_seconds):
            return {}
        with self._lock:
            return dict(self._completed_orders)

    def _resolve_unknown_submissions(self, completed_orders: dict[str, dict[str, Any]]) -> None:
        """Resolve unresolved SUBMISSION_UNKNOWN rows strictly from broker
        evidence just ingested. Never guesses: a row with no matching
        evidence either way is left unresolved (still globally blocking)."""
        for row in self.ledger.unresolved_unknown_submissions():
            order_ref = row["order_ref"]
            if not order_ref:
                continue
            if self.ledger.executions_for_order_ref(order_ref):
                self.ledger.mark_reconciliation_outcome(row["correlation_id"], "CONFIRMED_FILLED")
                continue
            completed = completed_orders.get(order_ref)
            if completed and completed["status"] in NO_FILL_TERMINAL_STATUSES:
                self.ledger.mark_reconciliation_outcome(row["correlation_id"], "CONFIRMED_NO_FILL")

    def _flag_unattributed_positions(self) -> list[dict[str, Any]]:
        """Cross-day fallback for whatever remains unresolved after the above.

        If IBKR reports a live (nonzero) position for a still-unresolved
        row's contract, with no broker_executions evidence anywhere in the
        ledger for that con_id, this is an unattributed-position discrepancy:
        it is never auto-resolved either way (not CONFIRMED_FILLED, not
        CONFIRMED_NO_FILL) -- per AGENTS.md, modification or interpretation
        of a manual/external position is not automated. It stays permanently
        blocking (has_unresolved_unknown() still returns True for it) and is
        only surfaced here for operator visibility via the reconciliation_runs
        audit trail and logs.
        """
        flagged: list[dict[str, Any]] = []
        for row in self.ledger.unresolved_unknown_submissions():
            contract = row["contract"]
            if not contract or not row["account"]:
                continue
            con_id = contract.get("con_id")
            if not con_id:
                continue
            live_quantity = sum(
                (position.quantity for position in self._positions
                 if position.account == row["account"] and position.contract.con_id == con_id),
                Decimal("0"),
            )
            if live_quantity != 0 and not self.ledger.executions_for_con_id(con_id):
                flagged.append({
                    "correlation_id": row["correlation_id"],
                    "account": row["account"],
                    "con_id": con_id,
                    "live_quantity": str(live_quantity),
                })
        return flagged

    # ---- Official EWrapper callbacks ---------------------------------

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IBKR callback name
        with self._lock:
            self._next_order_id = max(orderId, self._next_order_id or orderId)
        self._handshake.set()

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
        self._managed_accounts = tuple(account.strip() for account in accountsList.split(",") if account.strip())
        self._managed_accounts_event.set()

    def currentTime(self, time: int) -> None:  # noqa: N802,A002
        self._server_time.set()

    def connectionClosed(self) -> None:  # noqa: N802
        self._reconciled = False
        self._blocking_reason = "IBKR socket connection closed; reconciliation is required"
        for ack in tuple(self._order_acks.values()):
            ack.event.set()

    def error(self, reqId, *args) -> None:  # noqa: N802
        # API 10.33 added errorTime between reqId and errorCode.  Accept both
        # official signatures so an upgrade cannot turn broker rejects into a
        # callback TypeError and an apparent acknowledgement timeout.
        if isinstance(reqId, Exception) and not args:
            self._reconciled = False
            self._blocking_reason = "The official IBKR client reported a local transport exception"
            return
        if len(args) >= 3 and isinstance(args[1], int):
            _, errorCode, errorString, *remainder = args
        elif len(args) >= 2:
            errorCode, errorString, *remainder = args
        else:
            self._reconciled = False
            self._blocking_reason = "The official IBKR client emitted an unrecognized error callback"
            return
        advanced_order_reject_json = remainder[0] if remainder else ""
        message = str(errorString)
        if advanced_order_reject_json:
            # IBKR's own diagnostic detail for an adaptive-algo/advanced-order
            # rejection -- never silently discarded (AGENTS.md: broker
            # evidence must not be dropped), but only ever logged, never fed
            # into any decision: pending/ack/cancel_ack error handling below
            # is driven entirely by errorCode/errorString, exactly as before.
            logger.warning(
                "IBKR error callback %d for reqId %s included advancedOrderRejectJson: %s",
                errorCode, reqId, advanced_order_reject_json,
            )
        if errorCode in CONNECTION_LOSS_CODES:
            self._reconciled = False
            self._blocking_reason = f"IBKR connectivity code {errorCode}: {message}"
        with self._lock:
            pending = self._requests.get(int(reqId)) if isinstance(reqId, int) else None
            ack = self._order_acks.get(int(reqId)) if isinstance(reqId, int) else None
            cancel_ack = self._cancel_acks.get(int(reqId)) if isinstance(reqId, int) else None
        if pending and errorCode not in INFORMATIONAL_CODES:
            pending.error = (int(errorCode), message)
            pending.event.set()
        if ack:
            if errorCode in WARNING_CODES or errorCode in INFORMATIONAL_CODES:
                ack.warnings.append((int(errorCode), message))
            else:
                ack.error = (int(errorCode), message)
                ack.event.set()
        if cancel_ack:
            if errorCode == IBKR_CANCEL_CONFIRMATION_ERROR_CODE:
                # Some TWS/Gateway versions confirm a successful cancellation
                # only via this informational error code, never orderStatus.
                cancel_ack.raw_status = "Cancelled"
                cancel_ack.event.set()
            elif errorCode in WARNING_CODES or errorCode in INFORMATIONAL_CODES:
                pass  # non-fatal noise; keep waiting for a definitive outcome.
            else:
                cancel_ack.error = (int(errorCode), message)
                cancel_ack.event.set()

    def contractDetails(self, reqId: int, contractDetails) -> None:  # noqa: N802
        pending = self._get_request(reqId)
        if pending:
            pending.values.append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        self._set_request(reqId)

    def securityDefinitionOptionParameter(  # noqa: N802
        self, reqId, exchange, underlyingConId, tradingClass, multiplier, expirations, strikes
    ) -> None:
        pending = self._get_request(reqId)
        if pending:
            pending.values.append(OptionChain(
                exchange=str(exchange),
                underlying_con_id=int(underlyingConId),
                trading_class=str(tradingClass),
                multiplier=str(multiplier),
                expirations=tuple(sorted(str(value) for value in expirations)),
                strikes=tuple(sorted(Decimal(str(value)) for value in strikes)),
            ))

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:  # noqa: N802
        self._set_request(reqId)

    def marketRule(self, marketRuleId: int, priceIncrements) -> None:  # noqa: N802
        pending = self._get_request(marketRuleId)
        if pending:
            pending.values.extend(
                PriceIncrement(Decimal(str(item.lowEdge)), Decimal(str(item.increment)))
                for item in priceIncrements
            )
            pending.event.set()

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        pending = self._get_request(reqId)
        if pending and pending.values:
            pending.values[0]["market_data_type"] = {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}.get(
                marketDataType, f"UNKNOWN_{marketDataType}"
            )

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib) -> None:  # noqa: N802
        pending = self._get_request(reqId)
        if not pending or not pending.values or price <= 0:
            return
        if tickType == 1:
            pending.values[0]["bid"] = Decimal(str(price))
        elif tickType == 2:
            pending.values[0]["ask"] = Decimal(str(price))
        pending.values[0]["received_at"] = self.clock()
        if pending.values[0]["bid"] is not None and pending.values[0]["ask"] is not None:
            pending.event.set()

    def tickSnapshotEnd(self, reqId: int) -> None:  # noqa: N802
        self._set_request(reqId)

    def tickOptionComputation(  # noqa: N802
        self, reqId, tickType, tickAttrib, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice
    ) -> None:
        pending = self._get_request(reqId)
        if not pending or not pending.values:
            return
        # IBKR sends a DBL_MAX-style sentinel (or NaN) before a greek is actually
        # computed; only accept a finite delta in [-1, 1].
        if isinstance(delta, (int, float)) and math.isfinite(delta) and abs(delta) <= 1:
            pending.values[0]["delta"] = Decimal(str(delta))

    def position(self, account: str, contract, position, avgCost: float) -> None:  # noqa: N802
        with self._lock:
            self._positions.append(Position(
                account=str(account),
                contract=_qualified_contract_only(contract),
                quantity=Decimal(str(position)),
                average_cost=Decimal(str(avgCost)) if avgCost is not None else None,
            ))

    def positionEnd(self) -> None:  # noqa: N802
        self._position_event.set()

    def openOrder(self, orderId: int, contract, order, orderState) -> None:  # noqa: N802
        raw_status = str(getattr(orderState, "status", ""))
        if raw_status not in TERMINAL_ORDER_STATUSES:
            quantity = getattr(order, "totalQuantity", None)
            remaining = Decimal(str(quantity)) if quantity is not None else None
            self._working_orders[int(orderId)] = WorkingOrder(
                account=str(getattr(order, "account", "")),
                contract=_qualified_contract_only(contract),
                action=str(getattr(order, "action", "")),
                remaining=remaining,
                order_id=int(orderId),
                client_id=int(getattr(order, "clientId", -1)),
                perm_id=_optional_positive_int(getattr(order, "permId", None)),
                order_ref=str(getattr(order, "orderRef", "")),
                raw_status=raw_status,
            )
        with self._lock:
            ack = self._order_acks.get(int(orderId))
        if ack and raw_status in ACK_STATUSES:
            ack.raw_status = raw_status
            ack.perm_id = _optional_positive_int(getattr(order, "permId", None))
            ack.parent_id = _optional_positive_int(getattr(order, "parentId", None))
            ack.oca_group = str(getattr(order, "ocaGroup", ""))
            ack.event.set()

    def openOrderEnd(self) -> None:  # noqa: N802
        self._open_order_event.set()

    def orderStatus(  # noqa: N802
        self, orderId, status, filled, remaining, avgFillPrice, permId, parentId,
        lastFillPrice, clientId, whyHeld, mktCapPrice=0.0
    ) -> None:
        with self._lock:
            ack = self._order_acks.get(int(orderId))
            cancel_ack = self._cancel_acks.get(int(orderId))
        if ack and str(status) in ACK_STATUSES:
            ack.raw_status = str(status)
            ack.perm_id = _optional_positive_int(permId)
            ack.parent_id = _optional_positive_int(parentId)
            ack.event.set()
        if cancel_ack and str(status) in TERMINAL_ORDER_STATUSES:
            cancel_ack.raw_status = str(status)
            cancel_ack.event.set()
        working = self._working_orders.get(int(orderId))
        if working:
            if str(status) in TERMINAL_ORDER_STATUSES:
                self._working_orders.pop(int(orderId), None)
            else:
                self._working_orders[int(orderId)] = WorkingOrder(
                    **{**working.__dict__, "remaining": Decimal(str(remaining)), "raw_status": str(status)}
                )

    def execDetails(self, reqId: int, contract, execution) -> None:  # noqa: N802
        # Fired both unsolicited (a real-time fill, on whatever reqId IBKR
        # chooses for that -- not necessarily -1, so this is deliberately not
        # hardcoded) and in response to our own reqExecutions sweep call
        # (reqId == self._exec_sweep_request_id, set only while that request
        # is outstanding). Either way: a fast, lock-protected, non-blocking
        # ledger write only -- never a socket call from this thread.
        source = "RECONCILE_SWEEP" if reqId == self._exec_sweep_request_id else "LIVE_CALLBACK"
        order_ref = str(getattr(execution, "orderRef", "") or "") or None
        self.ledger.record_execution(ExecutionRecord(
            exec_id=str(execution.execId),
            order_ref=order_ref,
            order_id=_optional_positive_int(getattr(execution, "orderId", None)),
            perm_id=_optional_positive_int(getattr(execution, "permId", None)),
            account=str(getattr(execution, "acctNumber", "")),
            con_id=_optional_positive_int(getattr(contract, "conId", None)),
            symbol=str(getattr(contract, "symbol", "")),
            side=str(getattr(execution, "side", "")),
            shares=str(getattr(execution, "shares", "")),
            price=str(Decimal(str(getattr(execution, "price", 0)))),
            cum_qty=str(getattr(execution, "cumQty", "")) if getattr(execution, "cumQty", None) is not None else None,
            avg_price=(
                str(Decimal(str(execution.avgPrice))) if getattr(execution, "avgPrice", None) is not None else None
            ),
            exec_time=str(getattr(execution, "time", "")),
            source=source,
            raw={
                "execId": execution.execId,
                "time": execution.time,
                "acctNumber": execution.acctNumber,
                "exchange": execution.exchange,
                "side": execution.side,
                "shares": str(execution.shares),
                "price": execution.price,
                "permId": execution.permId,
                "clientId": execution.clientId,
                "orderId": execution.orderId,
                "cumQty": str(execution.cumQty),
                "avgPrice": execution.avgPrice,
                "orderRef": execution.orderRef,
            },
        ))
        pending = self._get_request(reqId)
        if pending:
            pending.values.append(execution)

    def execDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        self._set_request(reqId)

    def commissionAndFeesReport(self, commissionAndFeesReport) -> None:  # noqa: N802
        # NOTE: the installed ibapi (10.37.2) renamed the classic
        # `CommissionReport`/`commissionReport()` callback to
        # `CommissionAndFeesReport`/`commissionAndFeesReport()`, and its
        # `.commission` field to `.commissionAndFees` -- confirmed by reading
        # ibapi/commission_and_fees_report.py and ibapi/wrapper.py directly
        # rather than assumed from older API documentation.
        realized_raw = getattr(commissionAndFeesReport, "realizedPNL", None)
        realized_pnl = _sanitize_realized_pnl(realized_raw)
        self.ledger.record_commission(CommissionRecord(
            exec_id=str(commissionAndFeesReport.execId),
            commission=str(Decimal(str(commissionAndFeesReport.commissionAndFees))),
            currency=str(commissionAndFeesReport.currency),
            realized_pnl=realized_pnl,
            raw={
                "execId": commissionAndFeesReport.execId,
                "commissionAndFees": commissionAndFeesReport.commissionAndFees,
                "currency": commissionAndFeesReport.currency,
                "realizedPNL": realized_raw,
            },
        ))

    def completedOrder(self, contract, order, orderState) -> None:  # noqa: N802
        order_ref = str(getattr(order, "orderRef", "") or "")
        if not order_ref:
            return
        with self._lock:
            self._completed_orders[order_ref] = {
                "status": str(getattr(orderState, "status", "")),
                "con_id": _optional_positive_int(getattr(contract, "conId", None)),
            }

    def completedOrdersEnd(self) -> None:  # noqa: N802
        self._completed_orders_event.set()

    # ---- request helpers ---------------------------------------------

    def _contract_details(self, contract) -> list[Any]:
        request_id, pending = self._new_request()
        self.reqContractDetails(request_id, contract)
        self._wait(pending, request_id, "CONTRACT_QUALIFICATION_FAILED")
        return pending.values

    def _new_request(self, explicit_id: int | None = None) -> tuple[int, _Pending]:
        with self._lock:
            if explicit_id is None:
                request_id = self._next_request_id
                self._next_request_id += 1
            else:
                request_id = explicit_id
            if request_id in self._requests:
                raise BrokerDefinitiveError("IBKR_REQUEST_ID_CONFLICT", "An IBKR request ID is already active")
            pending = _Pending()
            self._requests[request_id] = pending
            return request_id, pending

    def _wait(self, pending: _Pending, request_id: int, code: str) -> None:
        try:
            if not pending.event.wait(self.config.request_timeout_seconds):
                raise BrokerDefinitiveError(code, "IBKR pre-submission request timed out")
            if pending.error:
                broker_code, message = pending.error
                raise BrokerDefinitiveError(code, message, broker_code=broker_code)
        finally:
            with self._lock:
                self._requests.pop(request_id, None)

    def _get_request(self, request_id: int) -> _Pending | None:
        with self._lock:
            return self._requests.get(int(request_id))

    def _set_request(self, request_id: int) -> None:
        pending = self._get_request(request_id)
        if pending:
            pending.event.set()

    def _refresh_positions(self) -> None:
        with self._lock:
            self._positions = []
            self._position_event.clear()
        self.reqPositions()
        if not self._position_event.wait(self.config.request_timeout_seconds):
            self._reconciled = False
            raise BrokerDefinitiveError("POSITION_RECONCILIATION_TIMEOUT", "IBKR positions request timed out")
        self.cancelPositions()

    def _refresh_working_orders(self) -> None:
        with self._lock:
            self._working_orders = {}
            self._open_order_event.clear()
        self.reqAllOpenOrders()
        if not self._open_order_event.wait(self.config.request_timeout_seconds):
            self._reconciled = False
            raise BrokerDefinitiveError("ORDER_RECONCILIATION_TIMEOUT", "IBKR open-orders request timed out")

    def _allocate_order_id(self) -> int:
        with self._lock:
            if self._next_order_id is None:
                raise BrokerAmbiguousError("IBKR_ORDER_ID_UNAVAILABLE", "No IBKR nextValidId is available")
            result = self._next_order_id
            self._next_order_id += 1
            return result


def _qualified(details) -> QualifiedContract:
    contract = details.contract
    valid_exchanges = tuple(
        part.strip() for part in str(getattr(details, "validExchanges", "")).split(",") if part.strip()
    )
    raw_rule_ids = str(getattr(details, "marketRuleIds", "")).split(",")
    rule_ids: tuple[int | None, ...] = tuple(int(value) if value.strip().isdigit() else None for value in raw_rule_ids)
    return QualifiedContract(
        con_id=int(contract.conId),
        symbol=str(contract.symbol),
        sec_type=str(contract.secType),
        exchange=str(contract.exchange),
        currency=str(contract.currency),
        primary_exchange=str(getattr(contract, "primaryExchange", "")),
        local_symbol=str(getattr(contract, "localSymbol", "")),
        expiry=str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8],
        strike=Decimal(str(getattr(contract, "strike", 0))),
        right=str(getattr(contract, "right", "")),
        multiplier=str(getattr(contract, "multiplier", "")),
        trading_class=str(getattr(contract, "tradingClass", "")),
        valid_exchanges=valid_exchanges,
        market_rule_ids=rule_ids,
        min_tick=Decimal(str(details.minTick)) if getattr(details, "minTick", None) else None,
    )


def _qualified_contract_only(contract) -> QualifiedContract:
    return QualifiedContract(
        con_id=int(getattr(contract, "conId", 0)),
        symbol=str(getattr(contract, "symbol", "")),
        sec_type=str(getattr(contract, "secType", "")),
        exchange=str(getattr(contract, "exchange", "")),
        currency=str(getattr(contract, "currency", "")),
        primary_exchange=str(getattr(contract, "primaryExchange", "")),
        local_symbol=str(getattr(contract, "localSymbol", "")),
        expiry=str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8],
        strike=Decimal(str(getattr(contract, "strike", 0))),
        right=str(getattr(contract, "right", "")),
        multiplier=str(getattr(contract, "multiplier", "")),
        trading_class=str(getattr(contract, "tradingClass", "")),
    )


def _ib_contract(contract: QualifiedContract):
    result = Contract()
    result.conId = contract.con_id
    result.symbol = contract.symbol
    result.secType = contract.sec_type
    result.exchange = contract.exchange
    result.currency = contract.currency
    if contract.primary_exchange:
        result.primaryExchange = contract.primary_exchange
    if contract.local_symbol:
        result.localSymbol = contract.local_symbol
    if contract.expiry:
        result.lastTradeDateOrContractMonth = contract.expiry
    if contract.strike:
        result.strike = float(contract.strike)
    if contract.right:
        result.right = contract.right
    if contract.multiplier:
        result.multiplier = contract.multiplier
    if contract.trading_class:
        result.tradingClass = contract.trading_class
    return result


def _market_rule_id(contract: QualifiedContract) -> int:
    if not contract.valid_exchanges or len(contract.valid_exchanges) != len(contract.market_rule_ids):
        raise BrokerDefinitiveError("MARKET_RULE_UNAVAILABLE", "IBKR exchange/market-rule mapping is unavailable")
    exchange = contract.exchange.upper()
    matches = [
        rule_id for valid_exchange, rule_id in zip(contract.valid_exchanges, contract.market_rule_ids)
        if valid_exchange.upper() == exchange and rule_id is not None
    ]
    if len(matches) != 1:
        raise BrokerDefinitiveError(
            "MARKET_RULE_AMBIGUOUS", "IBKR did not provide one exact market rule for the execution exchange"
        )
    return matches[0]


def _optional_positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _sanitize_realized_pnl(value: Any) -> str | None:
    """IBKR sends its UNSET_DOUBLE sentinel (sys.float_info.max) -- or NaN --
    for realizedPNL when it does not apply to this execution (e.g. an
    opening trade). Per AGENTS.md, a missing broker value must never be
    rendered as a fabricated number: the sentinel is sanitized to a real
    ``NULL`` (missing row/column), never stored as-is and never coerced to 0.
    """
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or abs(value) >= _UNSET_DOUBLE_THRESHOLD:
        return None
    return str(Decimal(str(value)))
