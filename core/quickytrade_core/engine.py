"""TradingView/manual intent execution against a broker transport.

Paper by default; live requires an explicitly confirmed account (see
CoreConfig). Every invariant below applies identically to both.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from .config import CoreConfig, is_live_account
from .domain import (
    BrokerAcknowledgement,
    BrokerAmbiguousError,
    BrokerDefinitiveError,
    BrokerTransport,
    LimitOrderRequest,
    QualifiedContract,
    StopLimitOrderRequest,
)
from .execution_ledger import ExecutionLedger
from .management import ManagementContractError, parse_management_contract
from .protection import (
    COMMITTED_STATUSES,
    ProtectionLedger,
    largest_remainder_allocation,
    protection_oca_group,
    protection_order_ref,
    stop_loss_protection_id,
    take_profit_protection_id,
)
from .registry import SubmissionRegistry
from .selection import (
    SelectionError,
    applicable_increment,
    candidate_strikes,
    choose_chain_and_expiry,
    choose_listed_strike,
    choose_strike_by_target_range,
    marketable_limit,
    round_to_tick,
    validate_quote,
)
from .transitions import TransitionLedger, transition_id

logger = logging.getLogger(__name__)

# Two distinct, separately-invokable close actions (Phase 5) -- never one
# implicit "close" with hidden behavior. REDUCE_ONLY_PARTIAL is a bounded
# partial-or-full close that never touches existing protection orders.
# FULL_FLATTEN is a deliberately more consequential action: it cancels every
# working protection leg on the entry first, then sells the entire verified
# remaining quantity. The right (call/put) stays encoded in the action name,
# exactly like the pre-Phase-5 CLOSE_LONG_CALL/CLOSE_LONG_PUT vocabulary, so
# _prepare_close can still cross-validate the close signal's claimed right
# against the referenced entry's actual contract.
ACTIONS = {
    "OPEN_LONG_CALL",
    "OPEN_LONG_PUT",
    "CLOSE_LONG_CALL_REDUCE_ONLY_PARTIAL",
    "CLOSE_LONG_CALL_FULL_FLATTEN",
    "CLOSE_LONG_PUT_REDUCE_ONLY_PARTIAL",
    "CLOSE_LONG_PUT_FULL_FLATTEN",
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Last-resort sanity ceiling for the operator-configured capital_per_trade_dollars
# open-signal field -- this only exists to reject an obviously fat-fingered value
# (an extra zero or three). It is NOT the real safety mechanism: the real caps are
# the deployment-level max_contracts_per_order ceiling (CoreConfig, restart-gated)
# and the existing per-contract max_contract_premium_dollars check. $1,000,000 is
# comfortably above any plausible single-trade allocation in this paper-first,
# long-premium-only app, while still catching a stray extra digit.
MAX_CAPITAL_PER_TRADE_DOLLARS = Decimal("1000000")


class ExecutionBlocked(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    body: dict[str, Any]
    ambiguous: bool = False


class ExecutionEngine:
    def __init__(
        self,
        *,
        config: CoreConfig,
        transport: BrokerTransport,
        registry: SubmissionRegistry,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ledger: ExecutionLedger | None = None,
        protection_ledger: ProtectionLedger | None = None,
        transition_ledger: TransitionLedger | None = None,
    ):
        config.validate()
        self.config = config
        self.transport = transport
        self.registry = registry
        self.clock = clock
        # Optional: unused by preview()/execute()/health(); only
        # ensure_protection() (Phase 3) needs broker-truth fill evidence and a
        # place to durably record protection-leg intent/evidence. Production
        # (__main__.py) always wires both; a caller that never invokes
        # ensure_protection() (e.g. most of this suite's existing tests) may
        # omit them, in which case _verify_readiness()'s protection-unknown
        # check below is simply a no-op -- there is nothing to be unresolved
        # about a component that was never constructed.
        self.ledger = ledger
        self.protection_ledger = protection_ledger
        # Optional, same story: only ensure_transitions() (Phase 4) needs it.
        # Production always wires it; a caller that never invokes
        # ensure_transitions() may omit it.
        self.transition_ledger = transition_ledger
        self._execution_lock = RLock()

    def health(self) -> dict[str, Any]:
        """Private readiness summary with no account identifiers or balances."""
        environment = "LIVE" if is_live_account(self.config.selected_account) else "PAPER"
        # ibkrPort/ibkrHost let the Node/UI layer identify *which* connection
        # profile this core actually corresponds to, so it never marks a
        # different profile on the same environment as ready/unlocked too.
        summary = {
            "environment": environment,
            "accountMask": _mask_account(self.config.selected_account),
            "ibkrHost": self.config.ibkr_host,
            "ibkrPort": self.config.ibkr_port,
            # Read-only visibility for the operator UI. Editing still requires
            # a core restart with new env vars — this is not a live-editable
            # control, just so the operator isn't blind to what's active.
            "strikeSelection": {
                "metric": self.config.strike_target_metric,
                "lo": str(self.config.strike_target_lo),
                "hi": str(self.config.strike_target_hi),
                "candidateCount": self.config.strike_candidate_count,
            },
        }
        try:
            self._verify_readiness()
        except ExecutionBlocked as error:
            return {"ready": False, **summary, "code": error.code}
        except Exception:
            logger.exception("Unexpected error while verifying readiness for health()")
            return {"ready": False, **summary, "code": "IBKR_READINESS_UNAVAILABLE"}
        return {"ready": True, **summary}

    def operator_positions(self, correlation_id: str | None = None) -> list[dict[str, Any]]:
        """Return selected-account ledger rows only after a fresh IBKR
        position read proves every locally-open app position still exists at
        the exact whole-contract quantity.

        IBKR reports positions at account/contract grain, not correlation
        grain. Multiple locally-open correlations for the same conId are
        therefore ownership-ambiguous and fail closed instead of copying the
        aggregate broker quantity onto each row. Historical closed rows remain
        available for the closed-today view, but can never become active.
        """
        if self.ledger is None:
            raise ExecutionBlocked("EXECUTION_LEDGER_UNAVAILABLE", "Execution ledger is unavailable")

        account = self.config.selected_account
        account_rows = [row for row in self.ledger.positions() if row.get("account") == account]
        broker_by_contract: dict[int, Decimal] = {}
        for position in self.transport.positions(account):
            if position.account != account:
                continue
            broker_by_contract[position.contract.con_id] = (
                broker_by_contract.get(position.contract.con_id, Decimal("0")) + position.quantity
            )

        open_by_contract: dict[int, list[dict[str, Any]]] = {}
        for row in account_rows:
            try:
                open_quantity = Decimal(str(row.get("open_quantity")))
                con_id = int(row.get("con_id"))
            except (InvalidOperation, TypeError, ValueError):
                raise ExecutionBlocked(
                    "POSITION_RECONCILIATION_INVALID",
                    "A local position row cannot be matched safely to broker position evidence",
                )
            if row.get("lifecycle_status") != "CLOSED" and open_quantity > 0:
                open_by_contract.setdefault(con_id, []).append(row)

        for con_id, broker_quantity in broker_by_contract.items():
            if broker_quantity < 0 or broker_quantity != broker_quantity.to_integral_value():
                raise ExecutionBlocked(
                    "UNSUPPORTED_BROKER_POSITION",
                    "A selected-account broker position is short or fractional and cannot be managed here",
                )
            if broker_quantity > 0 and con_id not in open_by_contract:
                raise ExecutionBlocked(
                    "UNATTRIBUTED_BROKER_POSITION",
                    "A selected-account broker position has no unique app-owned correlation",
                )

        confirmed_open_ids: set[str] = set()
        for con_id, rows in open_by_contract.items():
            if len(rows) != 1:
                raise ExecutionBlocked(
                    "POSITION_OWNERSHIP_AMBIGUOUS",
                    "Multiple app correlations share one broker contract position",
                )
            local_quantity = Decimal(str(rows[0]["open_quantity"]))
            broker_quantity = broker_by_contract.get(con_id, Decimal("0"))
            if (
                broker_quantity <= 0
                or broker_quantity != broker_quantity.to_integral_value()
                or broker_quantity != local_quantity
            ):
                raise ExecutionBlocked(
                    "POSITION_QUANTITY_DISCREPANCY",
                    "Local app position quantity does not match the selected-account IBKR position",
                )
            confirmed_open_ids.add(str(rows[0]["correlation_id"]))

        result: list[dict[str, Any]] = []
        broker_checked_at = self._now().isoformat()
        for row in account_rows:
            item = dict(row)
            item["operator_position_status"] = (
                "ACTIVE_CONFIRMED"
                if str(row["correlation_id"]) in confirmed_open_ids
                else "CLOSED_LOCAL_HISTORY"
            )
            item["broker_position_checked_at"] = (
                broker_checked_at if item["operator_position_status"] == "ACTIVE_CONFIRMED" else None
            )
            item["unrealized_pnl"] = None
            item["mark_price"] = None
            item["right"] = None
            item["strike"] = None
            item["expiry"] = None
            item["local_symbol"] = None
            entry = self.registry.lookup_entry(row["correlation_id"])
            if entry and entry.get("contract"):
                c_data = entry["contract"]
                if isinstance(c_data, dict):
                    item["right"] = c_data.get("right")
                    item["strike"] = c_data.get("strike")
                    item["expiry"] = c_data.get("expiry")
                    item["local_symbol"] = c_data.get("local_symbol")
                if item["operator_position_status"] == "ACTIVE_CONFIRMED":
                    try:
                        contract = _contract_from_json(c_data)
                        q = self.transport.quote(contract)
                        if q.bid is not None and q.ask is not None:
                            mid = (q.bid + q.ask) / Decimal("2")
                            entry_avg = Decimal(str(row["entry_avg_price"]))
                            open_qty = Decimal(str(row["open_quantity"]))
                            mult = Decimal(str(contract.multiplier or 100))
                            item["unrealized_pnl"] = str((mid - entry_avg) * open_qty * mult)
                            item["mark_price"] = str(mid)
                    except Exception:
                        pass
            result.append(item)
        if correlation_id:
            return [row for row in result if row["correlation_id"] == correlation_id]
        return result

    def execute(self, request: dict[str, Any]) -> ExecutionResult:
        # The official socket client and all account/contract reservations have
        # one owner. Serialize even if a caller bypasses the normal Node queue.
        with self._execution_lock:
            return self._execute_serialized(request)

    def preview(self, request: dict[str, Any]) -> ExecutionResult:
        """Resolve a fresh paper entry proposal without claiming or submitting it.

        A preview is advisory only.  Submission reruns every readiness, quote,
        contract, exposure, and risk check against current broker evidence.
        """
        with self._execution_lock:
            normalized = _parse_request(request)
            metadata = parse_management_contract(normalized)
            correlation_id = normalized["correlationId"]
            contract: QualifiedContract | None = None
            try:
                _validate_signal_freshness(
                    normalized["signal"]["sent_at"],
                    now=self._now(),
                    max_age_seconds=self.config.max_signal_age.total_seconds(),
                    max_future_skew_seconds=self.config.max_signal_future_skew.total_seconds(),
                )
                self._verify_readiness()
                if not normalized["signal"]["action"].startswith("OPEN_"):
                    # A side-effect-free preview of FULL_FLATTEN's multi-step
                    # cancel-then-sell sequence is out of scope for this
                    # phase (see /private/v1/close-trade instead, which
                    # performs the real, evidenced close/flatten). This is a
                    # deliberate phase-5 scope decision, not a leftover stub:
                    # the underlying evidence gate this used to describe
                    # (STRATEGY_EXECUTION_LEDGER_REQUIRED) no longer exists.
                    raise ExecutionBlocked(
                        "CLOSE_PREVIEW_NOT_SUPPORTED",
                        "Close/flatten preview is not supported; submit via /private/v1/close-trade",
                    )
                contract, order = self._prepare_open(normalized)
                body = {
                    "status": "PREVIEW_READY",
                    "correlationId": correlation_id,
                    "account": order.account,
                    "conId": contract.con_id,
                    "localSymbol": contract.local_symbol,
                    "action": order.action,
                    "quantity": order.quantity,
                    "orderType": "LMT",
                    "limitPrice": str(order.limit_price),
                    "tif": order.tif,
                    "previewOnly": True,
                    "requiresRevalidationOnSubmit": True,
                    **_management_response(metadata),
                }
                return ExecutionResult("PREVIEW_READY", body)
            except (ExecutionBlocked, SelectionError) as error:
                return self._blocked(correlation_id, error.code, str(error))
            except Exception:
                logger.exception("Unexpected error while previewing intent %s", correlation_id)
                return self._blocked(
                    correlation_id,
                    "PREVIEW_UNAVAILABLE",
                    "A local preview check failed closed",
                )

    def _execute_serialized(self, request: dict[str, Any]) -> ExecutionResult:
        normalized = _parse_request(request)
        metadata = parse_management_contract(normalized)
        registry_key = normalized["idempotencyKey"]
        correlation_id = normalized["correlationId"]
        payload_hash = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        claim = self.registry.claim(
            registry_key,
            normalized["intentId"],
            payload_hash,
            source=metadata["source"],
            ownership=metadata["ownership"],
            management_mode=metadata["managementMode"],
            management_policy=metadata["managementPolicy"],
        )
        if not claim.claimed:
            if claim.status == "CONFLICT":
                return self._blocked(
                    correlation_id, "CORRELATION_CONFLICT", "Correlation was already used for different intent data"
                )
            if claim.status in {"SUBMITTING", "SUBMISSION_UNKNOWN"}:
                return ExecutionResult(
                    "SUBMISSION_UNKNOWN",
                    {
                        "status": "SUBMISSION_UNKNOWN",
                        "code": "SUBMISSION_OUTCOME_UNRESOLVED",
                        "correlationId": correlation_id,
                    },
                    ambiguous=True,
                )
            if claim.result is None:
                return ExecutionResult(
                    "SUBMISSION_UNKNOWN",
                    {"status": "SUBMISSION_UNKNOWN", "code": "DURABLE_RESULT_MISSING", "correlationId": correlation_id},
                    ambiguous=True,
                )
            return ExecutionResult(claim.status, claim.result, ambiguous=False)

        broker_call_started = False
        action = normalized["signal"]["action"]
        entry_correlation_id: str | None = None
        contract: QualifiedContract | None = None
        close_context: dict[str, Any] = {}
        try:
            _validate_signal_freshness(
                normalized["signal"]["sent_at"],
                now=self._now(),
                max_age_seconds=self.config.max_signal_age.total_seconds(),
                max_future_skew_seconds=self.config.max_signal_future_skew.total_seconds(),
            )
            self._verify_readiness()
            if action.startswith("OPEN_"):
                contract, order = self._prepare_open(normalized)
            else:
                entry_correlation_id = _entry_reference(normalized["signal"])
                contract, order, close_context = self._prepare_close(normalized, entry_correlation_id)
            # Contract discovery and broker evidence reads can take time. Repeat
            # the age gate at the last safe boundary before the durable broker
            # call record and socket side effect.
            _validate_signal_freshness(
                normalized["signal"]["sent_at"],
                now=self._now(),
                max_age_seconds=self.config.max_signal_age.total_seconds(),
                max_future_skew_seconds=self.config.max_signal_future_skew.total_seconds(),
            )
            self.registry.record_broker_call_evidence(
                registry_key,
                account=order.account,
                action=action,
                contract=_contract_to_json(contract),
                side=order.action,
                quantity=order.quantity,
                limit_price=str(order.limit_price),
                order_ref=order.order_ref,
                entry_correlation_id=entry_correlation_id,
            )
            broker_call_started = True
            acknowledgement = self.transport.place_limit_order(order)
            result = self._submitted(correlation_id, order, acknowledgement, metadata, extra=close_context)
            self.registry.finish(
                registry_key,
                status="SUBMITTED",
                result=result.body,
                account=self.config.selected_account,
                action=action,
                contract=_contract_to_json(contract),
                entry_correlation_id=entry_correlation_id,
            )
            return result
        except (ExecutionBlocked, SelectionError) as error:
            return self._finish_blocked(
                registry_key, correlation_id, error.code, str(error), action, contract, entry_correlation_id
            )
        except BrokerDefinitiveError as error:
            body = self._blocked(correlation_id, error.code, str(error)).body
            if error.broker_code is not None:
                body["brokerErrorCode"] = error.broker_code
            self.registry.finish(
                registry_key,
                status="BLOCKED",
                result=body,
                account=self.config.selected_account,
                action=action,
                contract=_contract_to_json(contract) if contract else None,
                entry_correlation_id=entry_correlation_id,
            )
            return ExecutionResult("BLOCKED", body)
        except BrokerAmbiguousError as error:
            return self._finish_unknown(
                registry_key, correlation_id, error.code, action, contract, entry_correlation_id
            )
        except Exception:
            if broker_call_started:
                return self._finish_unknown(
                    registry_key,
                    correlation_id,
                    "IBKR_TRANSPORT_OUTCOME_UNKNOWN",
                    action,
                    contract,
                    entry_correlation_id,
                )
            return self._finish_blocked(
                registry_key,
                correlation_id,
                "PRE_SUBMIT_INTERNAL_FAILURE",
                "A local pre-submission check failed closed",
                action,
                contract,
                entry_correlation_id,
            )

    def _verify_readiness(self) -> None:
        if self.registry.has_unresolved_unknown():
            raise ExecutionBlocked(
                "UNRESOLVED_SUBMISSION",
                "A prior broker submission outcome is unknown; reconcile it before any new order",
            )
        # An unprotected-or-unknown-protected position is exactly the
        # compounding-risk scenario AGENTS.md's fail-closed rule exists to
        # prevent: a filled position whose stop/take-profit placement outcome
        # is ambiguous must block every new open exactly like an unresolved
        # entry SUBMISSION_UNKNOWN, not just reserve its own symbol/right.
        if self.protection_ledger is not None and self.protection_ledger.has_unresolved_unknown():
            raise ExecutionBlocked(
                "UNRESOLVED_PROTECTION_SUBMISSION",
                "A prior protection-order submission outcome is unknown; reconcile it before any new order",
            )
        # A management transition (e.g. MOVE_STOP_TO_BREAKEVEN) that landed in
        # FAILED_UNKNOWN -- whether from a directly-ambiguous leg modify or a
        # crash sweep of a row stuck APPLYING (transitions.py's own
        # SUBMITTING-equivalent sweep on construction) -- is exactly the same
        # class of unprotected-or-unknown-protected risk as an unresolved
        # entry or protection-order submission: global, like protection
        # ambiguity, since a silently-stuck stop-to-breakeven is invisible
        # otherwise and never automatically retried (transitions.py module
        # docstring).
        if self.transition_ledger is not None and self.transition_ledger.has_unresolved_unknown():
            raise ExecutionBlocked(
                "UNRESOLVED_TRANSITION_FAILURE",
                "A prior management-transition outcome is unknown; reconcile it before any new order",
            )
        readiness = self.transport.readiness()
        expected_environment = "LIVE" if is_live_account(self.config.selected_account) else "PAPER"
        if readiness.environment != expected_environment:
            raise ExecutionBlocked(
                "ENVIRONMENT_MISMATCH",
                f"Configured account requires a {expected_environment} IBKR session; "
                f"the connected session reports {readiness.environment}",
            )
        if not readiness.connected or not readiness.handshake_complete or not readiness.server_time_received:
            raise ExecutionBlocked("IBKR_NOT_READY", "IBKR connection handshake and server time are required")
        if not readiness.reconciled:
            raise ExecutionBlocked("RECONCILIATION_REQUIRED", "Broker position and order reconciliation is incomplete")
        if readiness.read_only is True:
            raise ExecutionBlocked("IBKR_READ_ONLY", "TWS/Gateway is configured read-only")
        if readiness.blocking_reason:
            raise ExecutionBlocked("IBKR_READINESS_BLOCKED", readiness.blocking_reason)
        account = self.config.selected_account
        is_live = expected_environment == "LIVE"
        allowlist = self.config.live_account_allowlist if is_live else self.config.paper_account_allowlist
        if account not in allowlist:
            raise ExecutionBlocked(
                "ACCOUNT_NOT_ALLOWLISTED", f"Selected {expected_environment.lower()} account is not allowlisted"
            )
        if account not in readiness.managed_accounts:
            raise ExecutionBlocked(
                "ACCOUNT_MISMATCH", "Connected IBKR accounts do not include the exact selected account"
            )

    def _prepare_open(self, request: dict[str, Any]) -> tuple[QualifiedContract, LimitOrderRequest]:
        signal = request["signal"]
        symbol = signal["ticker"]
        if symbol not in self.config.allowed_symbols:
            raise ExecutionBlocked("SYMBOL_NOT_ALLOWLISTED", "Underlying is not in the local symbol allowlist")
        right = "C" if signal["action"] == "OPEN_LONG_CALL" else "P"
        underlying = self.transport.qualify_underlying(symbol)
        _verify_underlying(underlying, symbol)

        now = self._now()
        underlying_quote = self.transport.quote(underlying)
        bid, ask = validate_quote(
            underlying_quote, now=now, max_age_seconds=self.config.max_quote_age.total_seconds()
        )
        spot = (bid + ask) / Decimal("2")
        strike_policy = signal["strike_policy"]
        if strike_policy["type"] == "EXACT_LISTED":
            requested_expiry = strike_policy["expiry"]
            expiry_date = datetime.strptime(requested_expiry, "%Y%m%d").date()
            local_date = now.astimezone(ZoneInfo(self.config.exchange_timezone)).date()
            target_dte = (expiry_date - local_date).days
        else:
            requested_expiry = None
            target_dte = signal["target_dte"]
        chain, expiry = choose_chain_and_expiry(
            tuple(self.transport.option_chains(underlying)),
            target_dte=target_dte,
            now=now,
            timezone_name=self.config.exchange_timezone,
            trading_class_allowlist=self.config.trading_class_allowlist,
            same_day_cutoff=self.config.same_day_entry_cutoff,
        )
        if requested_expiry is not None:
            if expiry != requested_expiry:
                raise ExecutionBlocked(
                    "EXACT_EXPIRY_MISMATCH", "The qualified chain did not match the requested expiry"
                )
            strike = Decimal(str(strike_policy["strike"]))
            if strike not in chain.strikes:
                raise ExecutionBlocked(
                    "EXACT_STRIKE_NOT_LISTED", "The requested strike is not listed in the qualified chain"
                )
        elif strike_policy["type"] == "ATM_OFFSET":
            strike = choose_listed_strike(
                chain,
                spot=spot,
                right=right,
                offset=strike_policy["offset"],
            )
        elif strike_policy["type"] == "TARGET_RANGE":
            strike = self._choose_target_range_strike(
                underlying=underlying, chain=chain, expiry=expiry, spot=spot, right=right,
            )
        else:
            raise ExecutionBlocked("STRIKE_POLICY_TYPE_UNSUPPORTED", "Unsupported strike_policy type")
        contract = self.transport.qualify_option(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
            exchange=chain.exchange,
            trading_class=chain.trading_class,
            multiplier=chain.multiplier,
        )
        _verify_option(contract, symbol, expiry, strike, right, chain.trading_class, chain.multiplier)
        self._block_duplicate_exposure(symbol, right)
        multiplier = _positive_decimal(contract.multiplier, "CONTRACT_MULTIPLIER_INVALID")

        option_quote = self.transport.quote(contract)
        option_bid, option_ask = validate_quote(
            option_quote, now=self._now(), max_age_seconds=self.config.max_quote_age.total_seconds()
        )
        # Sizing uses the option's own fresh mid-price, the same evidence just
        # validated above -- never a client-supplied or fabricated price. The
        # per-contract cost is mid_price * multiplier (a standard equity/ETF
        # option contract represents `multiplier` shares, typically 100), so
        # the multiplier must be folded into the sizing formula, not just the
        # separate premium-cap check below.
        mid_price = (option_bid + option_ask) / Decimal("2")
        quantity = self._resolve_quantity(signal, mid_price, multiplier)
        self._verify_option_spread(option_bid, option_ask)
        rules = self._verified_market_rule(contract)
        limit_price = marketable_limit(
            action="BUY",
            bid=option_bid,
            ask=option_ask,
            rules=rules,
            max_slippage_dollars=self.config.max_slippage_dollars,
            max_slippage_percent=self.config.max_slippage_percent,
        )
        if limit_price * multiplier > self.config.max_contract_premium_dollars:
            raise ExecutionBlocked(
                "PREMIUM_LIMIT_EXCEEDED", "The one-contract marketable limit exceeds the premium cap"
            )

        return contract, self._order(request, contract, "BUY", limit_price, quantity)

    def _resolve_quantity(self, signal: dict[str, Any], mid_price: Decimal, multiplier: Decimal) -> int:
        """Resolve the final OPEN order quantity.

        When capital_per_trade_dollars is present, it fully replaces the
        client-supplied quantity: contracts = floor(capital / (mid_price *
        multiplier)) -- one contract costs mid_price-per-share times the
        contract's share multiplier (typically 100 for equity/ETF options),
        not mid_price alone -- clamped to the deployment's
        max_contracts_per_order ceiling. Never rounds up -- an amount that
        can't cover even one contract blocks rather than silently exceeding
        the operator's stated risk budget.

        When capital_per_trade_dollars is absent, this falls back to the
        pre-existing behavior: signal.get("quantity", 1), which _parse_request
        has already constrained to exactly 1 for backward compatibility.
        """
        capital_raw = signal.get("capital_per_trade_dollars")
        if capital_raw is None:
            return min(signal.get("quantity", 1), self.config.max_contracts_per_order)
        if not mid_price.is_finite() or mid_price <= 0:
            raise ExecutionBlocked("OPTION_MID_PRICE_INVALID", "Option mid-price is invalid for capital-based sizing")
        capital = Decimal(capital_raw)
        contract_cost = mid_price * multiplier
        computed_quantity = int((capital / contract_cost).to_integral_value(rounding=ROUND_FLOOR))
        quantity = min(computed_quantity, self.config.max_contracts_per_order)
        if quantity < 1:
            raise ExecutionBlocked(
                "INSUFFICIENT_CAPITAL_FOR_ONE_CONTRACT",
                "capital_per_trade_dollars cannot cover even one contract at the current option mid-price",
            )
        return quantity

    def _prepare_close(
        self, request: dict[str, Any], entry_correlation_id: str
    ) -> tuple[QualifiedContract, LimitOrderRequest, dict[str, Any]]:
        """Shared preparation for both close actions -- ``REDUCE_ONLY_PARTIAL``
        (a bounded partial-or-full close that never touches existing
        protection orders) and ``FULL_FLATTEN`` (cancels every working
        protection leg first, then sells the entire verified remaining
        quantity). Two distinct, separately-invokable operator actions, not
        one implicit "close" with hidden behavior -- see the module-level
        ``ACTIONS`` comment.

        Un-gated (Phase 5) behind real broker-authoritative fill evidence
        (``_verify_close_eligible``) rather than the old unconditional
        ``STRATEGY_EXECUTION_LEDGER_REQUIRED`` block. Both the verified long
        quantity and the working-exit reservation are re-verified fresh here
        (never from a cache), immediately before the SELL is priced -- the
        same discipline as the fresh-quote requirement.
        """
        signal = request["signal"]
        action = signal["action"]
        is_full_flatten = action.endswith("_FULL_FLATTEN")
        entry = self.registry.lookup_entry(entry_correlation_id)
        if not entry or entry["status"] != "SUBMITTED" or not entry["contract"]:
            raise ExecutionBlocked("ENTRY_REFERENCE_NOT_FOUND", "Close requires an exact submitted entry reference")
        expected_right = "C" if action.startswith("CLOSE_LONG_CALL") else "P"
        expected_open = "OPEN_LONG_CALL" if expected_right == "C" else "OPEN_LONG_PUT"
        if entry["action"] != expected_open or entry["account"] != self.config.selected_account:
            raise ExecutionBlocked(
                "ENTRY_REFERENCE_MISMATCH", "Close action/account does not match the referenced entry"
            )
        contract = _contract_from_json(entry["contract"])
        if contract.symbol != signal["ticker"] or contract.right != expected_right:
            raise ExecutionBlocked("ENTRY_REFERENCE_MISMATCH", "Close ticker/right does not match the referenced entry")

        # Un-gating: a close is only reachable once broker-authoritative fill
        # evidence proves the referenced entry actually has open quantity.
        self._verify_close_eligible(entry_correlation_id)
        # Contract-scoped (never global) ambiguity gate -- see
        # ProtectionLedger.has_unresolved_cancel_unknown / registry.
        # has_blocking_close for the deliberate asymmetry from the global
        # blocks above.
        self._verify_close_contract_not_ambiguous(contract)

        if is_full_flatten:
            self._cancel_all_protection_legs(entry_correlation_id, contract)

        broker_quantity = self._fresh_verified_long_quantity(contract)
        working_exit_quantity = self._fresh_working_exit_quantity(contract, entry_correlation_id=entry_correlation_id)
        available = (broker_quantity - working_exit_quantity).to_integral_value(rounding=ROUND_FLOOR)

        if is_full_flatten:
            if available < 1:
                raise ExecutionBlocked(
                    "FLATTEN_NO_REMAINING_QUANTITY",
                    "No remaining quantity is available to flatten after working exits",
                )
            final_quantity = int(available)
        else:
            requested_quantity = signal["quantity"]
            if Decimal(requested_quantity) > available:
                raise ExecutionBlocked(
                    "REDUCE_ONLY_BOUND_EXCEEDED", "Close would exceed verified long quantity minus working exits"
                )
            final_quantity = requested_quantity

        order, spread_warning = self._close_sell_order(
            request, contract, final_quantity, hard_block_spread=not is_full_flatten
        )
        close_context: dict[str, Any] = {"closeAction": action}
        if spread_warning is not None:
            close_context["spreadWarning"] = spread_warning
        return contract, order, close_context

    def _verify_close_eligible(self, entry_correlation_id: str) -> None:
        """The un-gated readiness check replacing the old unconditional
        STRATEGY_EXECUTION_LEDGER_REQUIRED block: a close/flatten is only
        reachable once broker-authoritative fill evidence (position_state,
        Phase 2's ExecutionLedger, rebuilt purely from broker_executions)
        proves the referenced entry has a FILLED-or-later close-eligible
        lifecycle status and a positive open_quantity. Missing or
        not-yet-available evidence blocks -- the same fail-closed posture as
        every other missing-evidence gate in this codebase, not a special
        case for closes."""
        if self.ledger is None:
            raise ExecutionBlocked(
                "EXECUTION_LEDGER_UNAVAILABLE",
                "No broker-authoritative execution ledger is wired; closes cannot be verified",
            )
        position = self.ledger.position_state(entry_correlation_id)
        if position is None or position["lifecycle_status"] not in ("FILLED", "CLOSING"):
            raise ExecutionBlocked(
                "POSITION_FILL_EVIDENCE_UNAVAILABLE",
                "Broker-confirmed fill evidence for the referenced entry is not yet available",
            )
        open_quantity: Decimal | None = None
        if position["open_quantity"] is not None:
            try:
                open_quantity = Decimal(position["open_quantity"])
            except InvalidOperation:
                open_quantity = None
        if open_quantity is None or open_quantity <= 0:
            raise ExecutionBlocked(
                "POSITION_FILL_EVIDENCE_UNAVAILABLE",
                "Broker-confirmed open quantity for the referenced entry is unavailable or exhausted",
            )

    def _verify_close_contract_not_ambiguous(self, contract: QualifiedContract) -> None:
        """Contract-scoped (never global) ambiguity gate for closes: an
        unresolved protection-cancel or close-sell outcome for this exact
        contract blocks further close/flatten action (and further
        protection-ambiguity resolution attempts) on that contract only --
        it must never globally block an unrelated open or a close on a
        different contract the way an unresolved entry/protection-placement
        SUBMISSION_UNKNOWN already does in _verify_readiness(). That
        existing global behavior is unchanged; this is a deliberately
        separate, narrower check."""
        account = self.config.selected_account
        if self.registry.has_blocking_close(account, contract.con_id):
            raise ExecutionBlocked(
                "UNRESOLVED_CLOSE_SUBMISSION",
                "A prior close/flatten submission outcome for this contract is unresolved; "
                "reconcile it before any further close/flatten on this contract",
            )
        if self.protection_ledger is not None and self.protection_ledger.has_unresolved_cancel_unknown(
            account=account, con_id=contract.con_id
        ):
            raise ExecutionBlocked(
                "UNRESOLVED_PROTECTION_CANCEL",
                "A prior protection-leg cancel outcome for this contract is unknown; "
                "reconcile it before any further close/flatten on this contract",
            )

    def _cancel_all_protection_legs(self, entry_correlation_id: str, contract: QualifiedContract) -> None:
        """FULL_FLATTEN step 1-2: cancel every currently-working protection
        leg on this entry before any flattening SELL is even priced. Durable
        cancel-intent evidence is recorded (ProtectionLedger.
        record_cancel_intent) before each cancel_order broker call --
        idempotent: a leg already resolved (status no longer 'SUBMITTED',
        e.g. already 'CANCELLED' from a prior flatten attempt) is skipped
        entirely, never re-cancelled."""
        if self.protection_ledger is None:
            raise ExecutionBlocked(
                "PROTECTION_LEDGER_UNAVAILABLE",
                "No protection-order ledger is wired; a full flatten cannot safely verify or cancel protection legs",
            )
        for leg in self.protection_ledger.legs_for_correlation(entry_correlation_id):
            if leg["status"] != "SUBMITTED":
                continue  # already cancelled/blocked/filled/skipped -- nothing to do.
            self._cancel_protection_leg(leg, contract=contract)

    def _cancel_protection_leg(self, leg: dict[str, Any], *, contract: QualifiedContract) -> None:
        protection_id = leg["protection_id"]
        broker_order_id = leg["broker_order_id"]
        if not broker_order_id:
            raise ExecutionBlocked(
                "PROTECTION_CANCEL_ORDER_ID_MISSING",
                f"Protection leg {protection_id} has no known broker order id to cancel",
            )
        # Durable cancel-intent evidence before any broker call -- the same
        # evidence-before-broker-call discipline as every other side effect
        # in this codebase.
        self.protection_ledger.record_cancel_intent(protection_id)
        try:
            acknowledgement = self.transport.cancel_order(int(broker_order_id))
        except BrokerAmbiguousError:
            self.protection_ledger.finish_cancel_unknown(protection_id)
            # Fail closed: stop processing further legs and never attempt
            # the flattening SELL while any cancel outcome is unknown -- see
            # the module docstring / _verify_close_contract_not_ambiguous.
            raise ExecutionBlocked(
                "PROTECTION_CANCEL_UNKNOWN",
                f"Cancelling protection leg {protection_id} is ambiguous; the flatten is halted",
            )
        except Exception:
            logger.exception("Unexpected error while cancelling protection leg %s", protection_id)
            self.protection_ledger.finish_cancel_unknown(protection_id)
            raise ExecutionBlocked(
                "PROTECTION_CANCEL_UNKNOWN",
                f"Cancelling protection leg {protection_id} is ambiguous; the flatten is halted",
            )
        else:
            self.protection_ledger.finish_cancel_confirmed(protection_id, broker_raw_status=acknowledgement.raw_status)

    def _fresh_verified_long_quantity(self, contract: QualifiedContract) -> Decimal:
        """Freshly re-verified (never cached) broker-confirmed long quantity
        for this exact contract/account."""
        account = self.config.selected_account
        broker_quantity = Decimal("0")
        for position in self.transport.positions(account):
            if position.account == account and position.contract.con_id == contract.con_id:
                broker_quantity += position.quantity
        if broker_quantity < 1 or broker_quantity != broker_quantity.to_integral_value():
            raise ExecutionBlocked(
                "VERIFIED_LONG_POSITION_UNAVAILABLE", "A whole broker-confirmed long contract quantity is required"
            )
        return broker_quantity

    def _fresh_working_exit_quantity(self, contract: QualifiedContract, *, entry_correlation_id: str) -> Decimal:
        """Freshly re-verified (never cached) reservation for the
        reduce-only bound: the sum of remaining quantity across every
        working SELL order for this exact contract/account, counted exactly
        once per real reserved slice.

        This app's own protection legs (broker_protection_orders) are
        counted from that ledger directly, once per OCA group -- a
        take-profit leg and its paired stop-loss leg are two *separate*
        broker orders covering the *same* underlying shares (either can
        fill and OCA-cancels the other), so summing both rows' quantities
        would double-count that slice. Any other working sell order for
        this contract/account (a foreign/manual sell, or -- defensively --
        a protection leg not yet reflected in the ledger) is added from a
        fresh transport.working_orders() read, with protection legs' own
        known broker_order_ids excluded so they are never counted twice.
        """
        account = self.config.selected_account
        protection_reserved = Decimal("0")
        known_protection_order_ids: set[int] = set()
        if self.protection_ledger is not None:
            seen_oca_groups: set[str] = set()
            for leg in self.protection_ledger.legs_for_correlation(entry_correlation_id):
                if leg["broker_order_id"]:
                    known_protection_order_ids.add(int(leg["broker_order_id"]))
                if leg["status"] != "SUBMITTED":
                    continue
                oca_group = leg["oca_group"]
                if oca_group in seen_oca_groups:
                    continue
                seen_oca_groups.add(oca_group)
                protection_reserved += Decimal(leg["quantity"])

        other_reserved = Decimal("0")
        for order in self.transport.working_orders(account):
            if order.account != account or order.contract.con_id != contract.con_id:
                continue
            if order.action.upper() != "SELL":
                continue
            if order.order_id in known_protection_order_ids:
                continue
            if order.remaining is None:
                raise ExecutionBlocked("WORKING_EXIT_QUANTITY_UNKNOWN", "Working sell quantity is unavailable")
            if order.remaining > 0:
                other_reserved += order.remaining
        return protection_reserved + other_reserved

    def _close_sell_order(
        self, request: dict[str, Any], contract: QualifiedContract, quantity: int, *, hard_block_spread: bool
    ) -> tuple[LimitOrderRequest, dict[str, Any] | None]:
        """The exact same fresh-quote tick-valid marketable_limit(SELL, ...)
        construction for both close modes. ``hard_block_spread`` is the one
        deliberate difference: REDUCE_ONLY_PARTIAL keeps the spread check as
        a hard block (unchanged, consistent with every other order in this
        codebase); FULL_FLATTEN warns but allows -- see
        _verify_option_spread."""
        quote = self.transport.quote(contract)
        bid, ask = validate_quote(quote, now=self._now(), max_age_seconds=self.config.max_quote_age.total_seconds())
        spread_warning = self._verify_option_spread(bid, ask, hard_block=hard_block_spread)
        rules = self._verified_market_rule(contract)
        limit_price = marketable_limit(
            action="SELL",
            bid=bid,
            ask=ask,
            rules=rules,
            max_slippage_dollars=self.config.max_slippage_dollars,
            max_slippage_percent=self.config.max_slippage_percent,
        )
        return self._order(request, contract, "SELL", limit_price, quantity), spread_warning

    def _block_duplicate_exposure(self, symbol: str, right: str) -> None:
        account = self.config.selected_account
        if self.registry.has_blocking_open(account, symbol, right):
            raise ExecutionBlocked(
                "LOCAL_EXPOSURE_UNRESOLVED",
                "A prior submitted or unknown local entry still reserves this account/underlying/direction",
            )
        for position in self.transport.positions(account):
            contract = position.contract
            if (
                position.account == account
                and contract.symbol == symbol
                and contract.right == right
                and position.quantity != 0
            ):
                raise ExecutionBlocked("DUPLICATE_EXPOSURE", "A matching broker option position already exists")
        for order in self.transport.working_orders(account):
            contract = order.contract
            if order.account != account or contract.symbol != symbol or contract.right != right:
                continue
            if order.action.upper() == "BUY" and (order.remaining is None or order.remaining > 0):
                raise ExecutionBlocked("WORKING_ENTRY_EXISTS", "A matching working IBKR entry already exists")

    def _choose_target_range_strike(
        self,
        *,
        underlying: QualifiedContract,
        chain: Any,
        expiry: str,
        spot: Decimal,
        right: str,
    ) -> Decimal:
        candidates = candidate_strikes(
            chain, spot=spot, right=right, count=self.config.strike_candidate_count,
        )
        now = self._now()
        metric_by_strike: dict[Decimal, Decimal] = {}
        for candidate in candidates:
            try:
                candidate_contract = self.transport.qualify_option(
                    underlying=underlying,
                    expiry=expiry,
                    strike=candidate,
                    right=right,
                    exchange=chain.exchange,
                    trading_class=chain.trading_class,
                    multiplier=chain.multiplier,
                )
                candidate_quote = self.transport.quote(candidate_contract)
                bid, ask = validate_quote(
                    candidate_quote, now=now, max_age_seconds=self.config.max_quote_age.total_seconds()
                )
            except (SelectionError, BrokerDefinitiveError):
                continue
            if self.config.strike_target_metric == "DELTA":
                if candidate_quote.delta is None or not candidate_quote.delta.is_finite():
                    continue
                metric_by_strike[candidate] = abs(candidate_quote.delta)
            else:
                metric_by_strike[candidate] = (bid + ask) / Decimal("2")
        return choose_strike_by_target_range(
            chain,
            spot=spot,
            right=right,
            lo=self.config.strike_target_lo,
            hi=self.config.strike_target_hi,
            candidate_count=self.config.strike_candidate_count,
            metric_by_strike=metric_by_strike,
        )

    def _verified_market_rule(self, contract: QualifiedContract):
        if contract.min_tick is None or contract.min_tick <= 0:
            raise ExecutionBlocked("MIN_TICK_UNAVAILABLE", "Qualified option minimum tick is unavailable")
        rules = tuple(self.transport.market_rule(contract))
        for rule in rules:
            if rule.increment > 0 and rule.increment % contract.min_tick != 0:
                raise ExecutionBlocked(
                    "MARKET_RULE_MIN_TICK_CONFLICT", "Market rule conflicts with qualified minimum tick"
                )
        return rules

    def _verify_option_spread(
        self, bid: Decimal, ask: Decimal, *, hard_block: bool = True
    ) -> dict[str, Any] | None:
        """Every order in this codebase hard-blocks on a wide bid/ask spread
        by default (``hard_block=True``, the unchanged behavior for opens,
        REDUCE_ONLY_PARTIAL, and protection legs). The one deliberate
        exception, per explicit product decision: FULL_FLATTEN calls this
        with ``hard_block=False`` -- the operator has already cancelled
        protection and is treating this as an emergency exit, so a wide
        spread warns (returned here, never silently swallowed -- the caller
        threads it into the result payload) rather than trapping the
        operator in a now-unprotected position."""
        spread = ask - bid
        midpoint = (ask + bid) / Decimal("2")
        exceeds_dollars = spread > self.config.max_option_spread_dollars
        exceeds_percent = spread / midpoint > self.config.max_option_spread_percent
        if not (exceeds_dollars or exceeds_percent):
            return None
        if hard_block:
            raise ExecutionBlocked(
                "OPTION_SPREAD_LIMIT_EXCEEDED", "IBKR option spread exceeds the configured risk limit"
            )
        return {
            "code": "OPTION_SPREAD_LIMIT_EXCEEDED_WARNING_ONLY",
            "message": (
                "IBKR option spread exceeds the configured risk limit; proceeding as an "
                "emergency full-flatten exit rather than trapping an unprotected position"
            ),
            "bid": str(bid),
            "ask": str(ask),
            "spreadDollars": str(spread),
        }

    # ---- protection (Phase 3): stop-loss / take-profit placement ---------
    #
    # Level-triggered, not edge-triggered: called every periodic sweep (see
    # __main__.py) for every FILLED + APP_MANAGED correlation_id, and it is
    # always safe/idempotent to call again once a correlation_id is already
    # fully protected -- it simply finds nothing left to do. This is what
    # makes a fill landing in the gap between two sweeps, or across a
    # restart, self-healing rather than a missed callback.

    def ensure_protection(self, correlation_id: str) -> None:
        """Place any missing stop-loss/take-profit legs for one FILLED,
        APP_MANAGED correlation_id. Every broker side effect acquires the
        same lock guarding entry/close submission so protection placement
        and any concurrent manual operator action never race."""
        with self._execution_lock:
            self._ensure_protection_locked(correlation_id)

    def _ensure_protection_locked(self, correlation_id: str) -> None:
        if self.ledger is None or self.protection_ledger is None:
            return
        evidence = self.registry.submission_evidence(correlation_id)
        if evidence is None or evidence["management_mode"] != "APP_MANAGED":
            return  # ENTRY_ONLY rows never get protection.
        policy = evidence["management_policy"]
        contract_json = evidence["contract"]
        if not policy or not contract_json:
            return
        position = self.ledger.position_state(correlation_id)
        if position is None or position["lifecycle_status"] != "FILLED":
            # Broker-confirmed fill evidence (position_state, purely computed
            # from broker_executions) is the one and only gate -- not yet
            # FILLED simply means this correlation_id isn't a candidate this
            # sweep, never an error.
            return
        entry_price_raw = position["entry_avg_price"]
        opened_raw = position["opened_quantity"]
        if entry_price_raw is None or opened_raw is None:
            return
        try:
            entry_price = Decimal(entry_price_raw)
            filled_quantity = int(Decimal(opened_raw).to_integral_value())
        except InvalidOperation:
            return
        if not entry_price.is_finite() or entry_price <= 0 or filled_quantity <= 0:
            return
        contract = _contract_from_json(contract_json)
        levels = policy["takeProfitLevels"]
        stop_loss_percent = Decimal(policy["stopLossPercent"])
        allocations = largest_remainder_allocation(filled_quantity, levels)

        for level in levels:
            level_id = level["levelId"]
            target_quantity = allocations[level_id]
            oca_group = protection_oca_group(correlation_id, level_id)
            self._ensure_take_profit_leg(
                correlation_id=correlation_id,
                level=level,
                oca_group=oca_group,
                target_quantity=target_quantity,
                contract=contract,
                entry_price=entry_price,
            )
            self._ensure_stop_loss_leg(
                correlation_id=correlation_id,
                level_id=level_id,
                oca_group=oca_group,
                target_quantity=target_quantity,
                contract=contract,
                entry_price=entry_price,
                stop_loss_percent=stop_loss_percent,
            )

    def _ensure_take_profit_leg(
        self,
        *,
        correlation_id: str,
        level: dict[str, str],
        oca_group: str,
        target_quantity: int,
        contract: QualifiedContract,
        entry_price: Decimal,
    ) -> None:
        level_id = level["levelId"]
        trigger_percent = Decimal(level["triggerPercent"])

        def build_price(rules: tuple) -> tuple[None, Decimal]:
            raw_limit = entry_price * (Decimal("1") + trigger_percent / Decimal("100"))
            increment = applicable_increment(raw_limit, rules)
            # ROUND_CEILING per the design review: a sell limit rounded down
            # would leave money on the table versus the intended target.
            return None, round_to_tick(raw_limit, increment, upward=True)

        self._ensure_leg_family(
            correlation_id=correlation_id,
            role="TAKE_PROFIT",
            level_id=level_id,
            oca_group=oca_group,
            target_quantity=target_quantity,
            contract=contract,
            protection_id_for=lambda index: take_profit_protection_id(correlation_id, level_id, index=index),
            order_ref=protection_order_ref(correlation_id, level_id, "TAKE_PROFIT"),
            build_price=build_price,
        )

    def _ensure_stop_loss_leg(
        self,
        *,
        correlation_id: str,
        level_id: str,
        oca_group: str,
        target_quantity: int,
        contract: QualifiedContract,
        entry_price: Decimal,
        stop_loss_percent: Decimal,
    ) -> None:
        def build_price(rules: tuple) -> tuple[Decimal, Decimal]:
            raw_trigger = entry_price * (Decimal("1") - stop_loss_percent / Decimal("100"))
            trigger_increment = applicable_increment(raw_trigger, rules)
            # Round down/away from triggering early.
            trigger_price = round_to_tick(raw_trigger, trigger_increment, upward=False)
            # No live bid exists yet (the trigger hasn't happened) -- reuse
            # the same slippage-cap constants marketable_limit() uses for a
            # SELL, applied against the tick-valid trigger price itself.
            limit_price = self._stop_limit_from_trigger(trigger_price, rules)
            return trigger_price, limit_price

        self._ensure_leg_family(
            correlation_id=correlation_id,
            role="STOP_LOSS",
            # level_id is intentionally NOT stored in the STOP_LOSS row's own
            # level_id column (schema contract: nullable, TAKE_PROFIT only) --
            # it is only used here to compute a per-slice-unique
            # protection_id/order_ref and to look up this slice's family via
            # its shared oca_group with the paired TAKE_PROFIT leg.
            level_id=level_id,
            oca_group=oca_group,
            target_quantity=target_quantity,
            contract=contract,
            protection_id_for=lambda index: stop_loss_protection_id(correlation_id, level_id, index=index),
            order_ref=protection_order_ref(correlation_id, level_id, "STOP_LOSS"),
            build_price=build_price,
        )

    def _stop_limit_from_trigger(self, trigger_price: Decimal, rules: tuple) -> Decimal:
        """Shared slippage-cushion limit-price derivation for a stop-loss
        leg's trigger price. Reused for both initial placement
        (_ensure_stop_loss_leg, Phase 3) and a management transition's
        in-place modify (_ensure_one_transition, Phase 4:
        MOVE_STOP_TO_BREAKEVEN / TRAIL_FRESH_BID) -- exactly the same
        max_slippage_dollars/max_slippage_percent cushion either way."""
        slippage = min(self.config.max_slippage_dollars, trigger_price * self.config.max_slippage_percent)
        raw_limit = trigger_price - slippage
        if raw_limit <= 0:
            raise ExecutionBlocked(
                "PROTECTION_STOP_LIMIT_FLOOR_INVALID",
                "Stop slippage cushion produces a non-positive limit price",
            )
        limit_increment = applicable_increment(raw_limit, rules)
        limit_price = round_to_tick(raw_limit, limit_increment, upward=False)
        if limit_price > trigger_price:
            raise ExecutionBlocked(
                "PROTECTION_STOP_LIMIT_ABOVE_TRIGGER",
                "Stop limit price must not exceed the stop trigger price",
            )
        return limit_price

    def _ensure_leg_family(
        self,
        *,
        correlation_id: str,
        role: str,
        level_id: str,
        oca_group: str,
        target_quantity: int,
        contract: QualifiedContract,
        protection_id_for: Callable[[int], str],
        order_ref: str,
        build_price: Callable[[tuple], tuple[Decimal | None, Decimal]],
    ) -> None:
        existing = self.protection_ledger.legs(
            correlation_id,
            role=role,
            level_id=level_id if role == "TAKE_PROFIT" else None,
            oca_group=oca_group if role == "STOP_LOSS" else None,
        )
        # Fail closed: an ambiguous or (defensively) still-in-flight sibling
        # for this exact slice blocks any further action here until a future
        # reconciliation pass resolves it -- never place a second order while
        # a prior one's outcome is unknown.
        if any(row["status"] in ("SUBMISSION_UNKNOWN", "SUBMITTING") for row in existing):
            return
        resumable = [row for row in existing if row["status"] == "PENDING_FILL_CONFIRMATION"]
        if resumable:
            # A prior pass crashed after claim_leg() but before the broker
            # call. Resume the exact same durable row/quantity rather than
            # computing a fresh top-up -- there should only ever be one,
            # since claim -> evidence -> broker call all happen synchronously
            # within one _execution_lock-held pass.
            for row in resumable:
                self._submit_protection_leg(
                    protection_id=row["protection_id"],
                    correlation_id=correlation_id,
                    role=role,
                    contract=contract,
                    quantity=int(Decimal(row["quantity"])),
                    oca_group=oca_group,
                    order_ref=order_ref,
                    build_price=build_price,
                )
            return
        if target_quantity <= 0:
            if not existing:
                self.protection_ledger.mark_skipped_zero_allocation(
                    protection_id_for(1),
                    correlation_id=correlation_id,
                    role=role,
                    level_id=level_id if role == "TAKE_PROFIT" else None,
                    oca_group=oca_group,
                )
            return
        committed = sum(int(Decimal(row["quantity"])) for row in existing if row["status"] in COMMITTED_STATUSES)
        delta = target_quantity - committed
        if delta <= 0:
            return  # Already fully covered this sweep; never shrinks.
        protection_id = protection_id_for(len(existing) + 1)
        claim = self.protection_ledger.claim_leg(
            protection_id,
            correlation_id=correlation_id,
            role=role,
            level_id=level_id if role == "TAKE_PROFIT" else None,
            oca_group=oca_group,
            quantity=delta,
        )
        if not claim.claimed:
            return  # Idempotent no-op: some other pass already claimed it.
        self._submit_protection_leg(
            protection_id=protection_id,
            correlation_id=correlation_id,
            role=role,
            contract=contract,
            quantity=delta,
            oca_group=oca_group,
            order_ref=order_ref,
            build_price=build_price,
        )

    def _submit_protection_leg(
        self,
        *,
        protection_id: str,
        correlation_id: str,
        role: str,
        contract: QualifiedContract,
        quantity: int,
        oca_group: str,
        order_ref: str,
        build_price: Callable[[tuple], tuple[Decimal | None, Decimal]],
    ) -> None:
        # Price/market-rule computation happens before any durable evidence
        # commit, so a failure here leaves the row in PENDING_FILL_CONFIRMATION
        # (never SUBMITTING) -- it must be resolved via mark_blocked_pending(),
        # not finish(), which only ever transitions a SUBMITTING row.
        try:
            rules = self._verified_market_rule(contract)
            trigger_price, limit_price = build_price(rules)
        except (ExecutionBlocked, SelectionError) as error:
            self.protection_ledger.mark_blocked_pending(
                protection_id, result={"status": "BLOCKED", "code": error.code, "message": str(error)}
            )
            return
        except BrokerDefinitiveError as error:
            body = {"status": "BLOCKED", "code": error.code, "message": str(error)}
            if error.broker_code is not None:
                body["brokerErrorCode"] = error.broker_code
            self.protection_ledger.mark_blocked_pending(protection_id, result=body)
            return
        except Exception:
            logger.exception("Unexpected error while pricing protection leg %s", protection_id)
            self.protection_ledger.mark_blocked_pending(
                protection_id, result={"status": "BLOCKED", "code": "PROTECTION_PRE_SUBMIT_INTERNAL_FAILURE"}
            )
            return

        broker_call_started = False
        try:
            self.protection_ledger.record_broker_call_evidence(
                protection_id,
                trigger_price=str(trigger_price) if trigger_price is not None else None,
                limit_price=str(limit_price),
                order_ref=order_ref,
            )
            broker_call_started = True
            if role == "TAKE_PROFIT":
                request = LimitOrderRequest(
                    account=self.config.selected_account,
                    contract=contract,
                    action="SELL",
                    quantity=quantity,
                    limit_price=limit_price,
                    tif="GTC",
                    outside_rth=False,
                    order_ref=order_ref,
                    oca_group=oca_group,
                    oca_type=1,
                )
                acknowledgement = self.transport.place_limit_order(request)
            else:
                request = StopLimitOrderRequest(
                    account=self.config.selected_account,
                    contract=contract,
                    action="SELL",
                    quantity=quantity,
                    trigger_price=trigger_price,
                    limit_price=limit_price,
                    tif="GTC",
                    outside_rth=False,
                    order_ref=order_ref,
                    oca_group=oca_group,
                    oca_type=1,
                )
                acknowledgement = self.transport.place_stop_limit_order(request)
            self.protection_ledger.finish(
                protection_id,
                status="SUBMITTED",
                result={
                    "status": "SUBMITTED",
                    "correlationId": correlation_id,
                    "brokerOrderId": str(acknowledgement.order_id),
                    "permId": acknowledgement.perm_id,
                    "ocaGroup": acknowledgement.oca_group,
                    "rawBrokerStatus": acknowledgement.raw_status,
                },
                broker_order_id=str(acknowledgement.order_id),
                perm_id=acknowledgement.perm_id,
            )
        except BrokerDefinitiveError as error:
            body = {"status": "BLOCKED", "code": error.code, "message": str(error)}
            if error.broker_code is not None:
                body["brokerErrorCode"] = error.broker_code
            self.protection_ledger.finish(protection_id, status="BLOCKED", result=body)
        except BrokerAmbiguousError as error:
            self.protection_ledger.finish(
                protection_id,
                status="SUBMISSION_UNKNOWN",
                result={
                    "status": "SUBMISSION_UNKNOWN",
                    "code": "PROTECTION_SUBMISSION_UNKNOWN",
                    "underlyingCode": error.code,
                },
            )
        except Exception:
            logger.exception("Unexpected error while placing protection leg %s", protection_id)
            if broker_call_started:
                self.protection_ledger.finish(
                    protection_id,
                    status="SUBMISSION_UNKNOWN",
                    result={"status": "SUBMISSION_UNKNOWN", "code": "PROTECTION_SUBMISSION_UNKNOWN"},
                )
            else:
                self.protection_ledger.finish(
                    protection_id,
                    status="BLOCKED",
                    result={"status": "BLOCKED", "code": "PROTECTION_PRE_SUBMIT_INTERNAL_FAILURE"},
                )

    # ---- management transitions (Phase 4): MOVE_STOP_TO_BREAKEVEN /
    # TRAIL_FRESH_BID reactions to a confirmed take-profit fill ------------
    #
    # Level-triggered, exactly like ensure_protection(): called every
    # periodic sweep (see __main__.py, after ensure_protection()) for every
    # FILLED + APP_MANAGED correlation_id, and always safe/idempotent to call
    # again -- a transition already APPLIED/FAILED_UNKNOWN is a pure no-op, so
    # a fill landing in the gap between two sweeps, or across a restart, is
    # still noticed on the next pass. Never called from an ibapi callback
    # thread; always under the same _execution_lock guarding entry/close
    # submission and ensure_protection().

    def ensure_transitions(self, correlation_id: str) -> None:
        with self._execution_lock:
            self._ensure_transitions_locked(correlation_id)

    def _ensure_transitions_locked(self, correlation_id: str) -> None:
        if self.ledger is None or self.protection_ledger is None or self.transition_ledger is None:
            return
        evidence = self.registry.submission_evidence(correlation_id)
        if evidence is None or evidence["management_mode"] != "APP_MANAGED":
            return  # ENTRY_ONLY rows never get transitions.
        policy = evidence["management_policy"]
        contract_json = evidence["contract"]
        if not policy or not contract_json:
            return
        transitions = policy.get("transitions") or []
        if not transitions:
            return
        position = self.ledger.position_state(correlation_id)
        if position is None or position["lifecycle_status"] not in ("FILLED", "CLOSING"):
            return
        entry_price_raw = position["entry_avg_price"]
        if entry_price_raw is None:
            return
        try:
            entry_price = Decimal(entry_price_raw)
        except InvalidOperation:
            return
        if not entry_price.is_finite() or entry_price <= 0:
            return
        contract = _contract_from_json(contract_json)
        for spec in transitions:
            self._ensure_one_transition(
                correlation_id=correlation_id, spec=spec, entry_price=entry_price, contract=contract
            )

    def _ensure_one_transition(
        self,
        *,
        correlation_id: str,
        spec: dict[str, str],
        entry_price: Decimal,
        contract: QualifiedContract,
    ) -> None:
        after = spec["after"]
        action = spec["action"]
        tid = transition_id(correlation_id, after)
        triggering_oca_group = protection_oca_group(correlation_id, after)

        existing = self.transition_ledger.get(tid)
        if existing is not None and existing["status"] in ("APPLYING", "APPLIED", "FAILED_UNKNOWN"):
            return  # already in flight or terminal -- never re-entered/retried automatically.

        tp_family = self.protection_ledger.legs(correlation_id, role="TAKE_PROFIT", level_id=after)
        if not tp_family:
            return  # ensure_protection() has not produced this level's family yet this sweep.

        if all(row["status"] == "SKIPPED_ZERO_ALLOCATION" for row in tp_family):
            # This level never received a real broker take-profit order (its
            # allocated quantity was zero) -- there is no fill to ever
            # confirm, so this transition can never legitimately fire.
            # Resolved to APPLIED immediately (a genuinely terminal, correct
            # state) rather than left PENDING and re-evaluated forever.
            claim = self.transition_ledger.ensure_pending(
                tid, correlation_id=correlation_id, after=after, action=action
            )
            if claim.row["status"] == "PENDING":
                self.transition_ledger.mark_applied(
                    tid,
                    details={
                        "reason": "TAKE_PROFIT_LEVEL_SKIPPED_ZERO_ALLOCATION",
                        "note": (
                            f"Take-profit level {after} never received a real broker order (its allocated "
                            "quantity rounded to zero) -- there is no fill to confirm, so this transition "
                            "can never fire and is treated as permanently, correctly inert."
                        ),
                    },
                )
            return

        if not self._take_profit_level_confirmed_filled(tp_family):
            return  # Not yet triggered this sweep -- not an error, just not a candidate yet.

        claim = self.transition_ledger.ensure_pending(tid, correlation_id=correlation_id, after=after, action=action)
        row = claim.row
        if row["status"] != "PENDING":
            return  # A concurrent/previous pass already advanced this row past PENDING.

        try:
            rules = self._verified_market_rule(contract)
            if action == "MOVE_STOP_TO_BREAKEVEN":
                # Breakeven = the entry's own average fill price, tick-rounded
                # up so it can never trigger below true cost.
                new_trigger = round_to_tick(entry_price, applicable_increment(entry_price, rules), upward=True)
            else:
                fresh_bid = self._fresh_option_bid(contract)
                distance_percent = Decimal(spec["distancePercent"])
                raw_trigger = fresh_bid * (Decimal("1") - distance_percent / Decimal("100"))
                new_trigger = round_to_tick(raw_trigger, applicable_increment(raw_trigger, rules), upward=True)
            new_limit = self._stop_limit_from_trigger(new_trigger, rules)
        except (ExecutionBlocked, SelectionError) as error:
            # Pre-broker-call block (e.g. a stale/missing fresh quote for
            # TRAIL_FRESH_BID, or a market-rule failure): the row stays
            # PENDING for the next sweep and every existing stop leg is left
            # exactly as-is, untouched and still working -- the position must
            # never be left naked while waiting for evidence.
            self.transition_ledger.mark_pending_retry(tid, details={"reason": error.code, "message": str(error)})
            return

        eligible_legs = self._eligible_stop_legs_for_transition(correlation_id, triggering_oca_group)

        if not self.transition_ledger.mark_applying(tid):
            return  # Lost a race with another pass (defensive; single-threaded in production).

        applied: list[str] = []
        skipped: list[dict[str, str]] = []
        for leg in eligible_legs:
            current_trigger = Decimal(leg["trigger_price"])
            if new_trigger <= current_trigger:
                # Ratchet-only: a recomputed trigger that would not improve on
                # the currently-resting trigger is a deliberate no-op -- never
                # calls the broker, and never loosens protection.
                skipped.append({"protectionId": leg["protection_id"], "reason": "RATCHET_NO_OP"})
                continue
            outcome = self._modify_stop_leg(leg, contract, new_trigger, new_limit)
            if outcome == "AMBIGUOUS":
                # Fail closed and stop processing further legs in this same
                # transition the moment one modify becomes ambiguous -- any
                # leg modified before this point keeps its (strictly better)
                # new trigger; nothing further is attempted this pass.
                self.transition_ledger.mark_failed_unknown(
                    tid,
                    details={
                        "reason": "PROTECTION_MODIFY_UNKNOWN",
                        "protectionId": leg["protection_id"],
                        "appliedLegs": applied,
                        "skippedLegs": skipped,
                    },
                )
                return
            if outcome == "MODIFIED":
                applied.append(leg["protection_id"])
            else:
                skipped.append({"protectionId": leg["protection_id"], "reason": outcome})

        self.transition_ledger.mark_applied(
            tid,
            details={
                "appliedLegs": applied,
                "skippedLegs": skipped,
                "newTrigger": str(new_trigger),
                "newLimit": str(new_limit),
            },
        )

    def _take_profit_level_confirmed_filled(self, tp_family: list[dict[str, Any]]) -> bool:
        """A take-profit leg counts as filled only when its broker_protection_
        orders row is backed by matching broker_executions evidence for its
        exact order_ref (Phase 2's order_ref-keyed matching,
        ExecutionLedger.executions_for_order_ref) -- never from a raw order-
        status string alone (e.g. an order simply disappearing from
        working_orders(), which is equally true of a cancellation)."""
        for row in tp_family:
            if row["status"] not in COMMITTED_STATUSES:  # only a genuinely placed/filled leg can have fills.
                continue
            order_ref = row["order_ref"]
            if not order_ref:
                continue
            if self.ledger.executions_for_order_ref(order_ref):
                return True
        return False

    def _eligible_stop_legs_for_transition(
        self, correlation_id: str, triggering_oca_group: str
    ) -> list[dict[str, Any]]:
        """Every other stop-loss leg of this correlation_id that is still
        working: not the just-triggered level's own paired stop (already
        OCA-cancelled broker-side by its sibling take-profit fill -- same
        oca_group), not already flagged from a prior ambiguous modify
        (never automatically retried), and confirmed still resting at IBKR
        via a fresh working_orders() read (IBKR's own live truth is
        authoritative for "is this order still working" -- a filled or
        OCA-cancelled order is equally absent from this list, and this
        function only needs the boolean, not which)."""
        working_order_ids = {
            order.order_id for order in self.transport.working_orders(self.config.selected_account)
        }
        eligible = []
        for row in self.protection_ledger.legs_for_correlation(correlation_id):
            if row["role"] != "STOP_LOSS" or row["oca_group"] == triggering_oca_group:
                continue
            if row["status"] != "SUBMITTED":
                continue  # never truly placed (BLOCKED/SKIPPED_ZERO_ALLOCATION/etc) -- nothing to modify.
            if row["modify_status"] == "MODIFY_UNKNOWN":
                continue  # already ambiguous from a prior modify -- never retried automatically.
            broker_order_id = row["broker_order_id"]
            if not broker_order_id or int(broker_order_id) not in working_order_ids:
                continue  # filled or OCA-cancelled -- nothing to modify.
            eligible.append(row)
        return eligible

    def _fresh_option_bid(self, contract: QualifiedContract) -> Decimal:
        """Identical freshness gate to every other quote read in this engine
        (validate_quote/config.max_quote_age) -- reused verbatim, never a
        separately invented staleness threshold."""
        quote = self.transport.quote(contract)
        bid, _ask = validate_quote(quote, now=self._now(), max_age_seconds=self.config.max_quote_age.total_seconds())
        return bid

    def _modify_stop_leg(
        self, leg: dict[str, Any], contract: QualifiedContract, new_trigger: Decimal, new_limit: Decimal
    ) -> str:
        protection_id = leg["protection_id"]
        broker_order_id = leg["broker_order_id"]
        if not broker_order_id:
            return "BROKER_ORDER_ID_MISSING"
        order_id = int(broker_order_id)
        request = StopLimitOrderRequest(
            account=self.config.selected_account,
            contract=contract,
            action="SELL",
            quantity=int(Decimal(leg["quantity"])),
            trigger_price=new_trigger,
            limit_price=new_limit,
            tif="GTC",
            outside_rth=False,
            order_ref=leg["order_ref"],
            oca_group=leg["oca_group"],
            oca_type=1,
        )
        # Durable evidence of the *intended* new trigger/limit, committed
        # before any socket call -- identical discipline to every other
        # broker side effect in this codebase.
        self.protection_ledger.record_modify_evidence(
            protection_id, pending_trigger_price=str(new_trigger), pending_limit_price=str(new_limit)
        )
        try:
            self.transport.modify_stop_limit_order(order_id, request)
        except BrokerAmbiguousError:
            self.protection_ledger.finish_modify_unknown(protection_id)
            return "AMBIGUOUS"
        except BrokerDefinitiveError:
            # A definitive (non-ambiguous) rejection of the modify itself --
            # resolved, not unresolved: the leg's last-confirmed trigger/limit
            # (still accurate broker truth) is left exactly as-is.
            self.protection_ledger.abandon_modify_attempt(protection_id)
            return "MODIFY_REJECTED"
        except Exception:
            logger.exception("Unexpected error while modifying protection leg %s", protection_id)
            self.protection_ledger.finish_modify_unknown(protection_id)
            return "AMBIGUOUS"
        self.protection_ledger.finish_modify_success(protection_id)
        return "MODIFIED"

    def _order(
        self,
        request: dict[str, Any],
        contract: QualifiedContract,
        action: str,
        limit_price: Decimal,
        quantity: int = 1,
    ) -> LimitOrderRequest:
        return LimitOrderRequest(
            account=self.config.selected_account,
            contract=contract,
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            tif="DAY",
            outside_rth=False,
            order_ref=_order_ref(request["correlationId"]),
        )

    def _submitted(
        self,
        correlation_id: str,
        order: LimitOrderRequest,
        acknowledgement: BrokerAcknowledgement,
        metadata: dict[str, Any],
        *,
        extra: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        if acknowledgement.raw_status not in {"PendingSubmit", "PreSubmitted", "Submitted"}:
            raise BrokerAmbiguousError(
                "IBKR_ACKNOWLEDGEMENT_INDETERMINATE", "IBKR did not provide a submitted acknowledgement"
            )
        body = {
            "status": "SUBMITTED",
            "correlationId": correlation_id,
            "brokerOrderId": str(acknowledgement.order_id),
            "clientId": acknowledgement.client_id,
            "permId": acknowledgement.perm_id,
            "parentId": acknowledgement.parent_id,
            "ocaGroup": acknowledgement.oca_group,
            "rawBrokerStatus": acknowledgement.raw_status,
            "orderRef": order.order_ref,
            "account": order.account,
            "conId": order.contract.con_id,
            "action": order.action,
            "quantity": order.quantity,
            "orderType": "LMT",
            "limitPrice": str(order.limit_price),
            "tif": order.tif,
            "warnings": [{"code": code, "message": message} for code, message in acknowledgement.warnings],
            **_management_response(metadata),
        }
        if extra:
            body.update(extra)
        return ExecutionResult("SUBMITTED", body)

    def _blocked(self, correlation_id: str, code: str, message: str) -> ExecutionResult:
        return ExecutionResult(
            "BLOCKED", {"status": "BLOCKED", "code": code, "message": message, "correlationId": correlation_id}
        )

    def _finish_blocked(
        self,
        registry_key: str,
        correlation_id: str,
        code: str,
        message: str,
        action: str,
        contract: QualifiedContract | None,
        entry_correlation_id: str | None,
    ) -> ExecutionResult:
        result = self._blocked(correlation_id, code, message)
        self.registry.finish(
            registry_key,
            status="BLOCKED",
            result=result.body,
            account=self.config.selected_account,
            action=action,
            contract=_contract_to_json(contract) if contract else None,
            entry_correlation_id=entry_correlation_id,
        )
        return result

    def _finish_unknown(
        self,
        registry_key: str,
        correlation_id: str,
        code: str,
        action: str,
        contract: QualifiedContract | None,
        entry_correlation_id: str | None,
    ) -> ExecutionResult:
        body = {"status": "SUBMISSION_UNKNOWN", "code": code, "correlationId": correlation_id}
        self.registry.finish(
            registry_key,
            status="SUBMISSION_UNKNOWN",
            result=body,
            account=self.config.selected_account,
            action=action,
            contract=_contract_to_json(contract) if contract else None,
            entry_correlation_id=entry_correlation_id,
        )
        return ExecutionResult("SUBMISSION_UNKNOWN", body, ambiguous=True)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ExecutionBlocked("CLOCK_INVALID", "Core clock must be timezone-aware")
        return now


def _parse_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ExecutionBlocked("INVALID_REQUEST", "Request must be a JSON object")
    required = {"broker", "idempotencyKey", "intentId", "correlationId", "source", "alertId", "signal"}
    optional = {"ownership", "managementMode", "managementPolicy"}
    if not required.issubset(request) or set(request) - (required | optional):
        raise ExecutionBlocked("INVALID_REQUEST_FIELDS", "Broker request fields do not match the service contract")
    if request["broker"] != "IBKR":
        raise ExecutionBlocked("INVALID_REQUEST_SOURCE", "Only persisted IBKR intents are accepted")
    try:
        metadata = parse_management_contract(request)
    except ManagementContractError as error:
        raise ExecutionBlocked(error.code, str(error)) from None
    if isinstance(request["intentId"], bool) or not isinstance(request["intentId"], int) or request["intentId"] <= 0:
        raise ExecutionBlocked("PERSISTENCE_RECEIPT_REQUIRED", "A positive persisted Node intentId is required")
    if not isinstance(request["alertId"], str) or not IDENTIFIER.fullmatch(request["alertId"]):
        raise ExecutionBlocked("INVALID_ALERT_ID", "alertId is invalid")
    if (not isinstance(request["correlationId"], str)
            or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
                request["correlationId"],
            )):
        raise ExecutionBlocked("PERSISTENCE_RECEIPT_REQUIRED", "A persisted Node UUID correlationId is required")
    key_prefix = "tradingview" if metadata["source"] == "TRADINGVIEW" else "manual"
    expected_key = f"{key_prefix}:{request['alertId']}"
    if request["idempotencyKey"] != expected_key:
        raise ExecutionBlocked(
            "PERSISTENCE_RECEIPT_MISMATCH",
            "idempotencyKey must match the persisted source event",
        )
    signal = request["signal"]
    if not isinstance(signal, dict):
        raise ExecutionBlocked("INVALID_SIGNAL", "signal must be an object")
    common = {"schema_version", "alert_id", "sent_at", "strategy_id", "strategy_version", "action", "ticker"}
    missing = common - set(signal)
    if missing:
        raise ExecutionBlocked("INVALID_SIGNAL", "Persisted signal is missing required normalized fields")
    if signal["alert_id"] != request["alertId"] or signal["action"] not in ACTIONS:
        raise ExecutionBlocked("INVALID_SIGNAL", "Persisted signal identity/action is invalid")
    if signal["schema_version"] != "1":
        raise ExecutionBlocked("INVALID_SIGNAL", "Only normalized TradingView schema version 1 is accepted")
    if not isinstance(signal["ticker"], str) or not re.fullmatch(r"[A-Z][A-Z0-9./-]{0,14}", signal["ticker"]):
        raise ExecutionBlocked("INVALID_SIGNAL", "Ticker is invalid")
    if signal["action"].startswith("OPEN_"):
        open_signal_fields = common | {
            "target_dte",
            "strike_policy",
            "risk_hint",
            "exit_policy_id",
            "quantity",
            "capital_per_trade_dollars",
        }
        if set(signal) - open_signal_fields:
            raise ExecutionBlocked("INVALID_SIGNAL_FIELDS", "Open signal contains unsupported fields")
        if "target_dte" not in signal or "strike_policy" not in signal:
            raise ExecutionBlocked(
                "OPTION_SELECTION_REQUIRED", "Open signal requires target_dte and strike_policy"
            )
        if "exit_policy_id" in signal and (
            not isinstance(signal["exit_policy_id"], str) or not signal["exit_policy_id"]
        ):
            raise ExecutionBlocked("EXIT_POLICY_INVALID", "exit_policy_id must be a non-empty identifier when supplied")
        policy = signal["strike_policy"]
        atm_policy = (
            isinstance(policy, dict)
            and set(policy) == {"type", "offset"}
            and policy["type"] == "ATM_OFFSET"
            and not isinstance(policy["offset"], bool)
            and isinstance(policy["offset"], int)
        )
        exact_policy = (
            isinstance(policy, dict)
            and set(policy) == {"type", "expiry", "strike"}
            and policy["type"] == "EXACT_LISTED"
        )
        if exact_policy:
            try:
                datetime.strptime(policy["expiry"], "%Y%m%d")
                strike = Decimal(str(policy["strike"]))
                exact_policy = strike.is_finite() and strike > 0
            except (TypeError, ValueError, InvalidOperation):
                exact_policy = False
        target_range_policy = isinstance(policy, dict) and set(policy) == {"type"} and policy["type"] == "TARGET_RANGE"
        if not atm_policy and not exact_policy and not target_range_policy:
            raise ExecutionBlocked(
                "STRIKE_POLICY_INVALID",
                "A listed-strike ATM_OFFSET, EXACT_LISTED, or TARGET_RANGE policy is required",
            )
        # capital_per_trade_dollars is additive/optional: when present, it drives
        # dynamic capital-based sizing in _prepare_open() and the client-supplied
        # quantity below is ignored for opens. When absent, quantity keeps its
        # pre-existing, unchanged fallback behavior (must be exactly 1 if given,
        # defaulting to 1) -- this is a deliberately conservative choice so that
        # every existing TradingView/manual signal shape that predates dynamic
        # sizing keeps behaving exactly as before.
        if "quantity" in signal and (isinstance(signal["quantity"], bool) or signal["quantity"] != 1):
            raise ExecutionBlocked("QUANTITY_LIMIT_EXCEEDED", "Only one contract may be opened")
        if "capital_per_trade_dollars" in signal:
            signal["capital_per_trade_dollars"] = _validate_capital_per_trade_dollars(
                signal["capital_per_trade_dollars"]
            )
    else:
        # FULL_FLATTEN always means "everything currently open, minus any
        # other working exit" -- a client-supplied quantity for it would be
        # meaningless/confusing, so it is rejected outright rather than
        # silently ignored. REDUCE_ONLY_PARTIAL requires an explicit
        # positive integer quantity (no silent default of 1): a bounded
        # partial or full close is exactly as consequential as an open, and
        # Phase 1 makes quantity > 1 reachable.
        is_full_flatten = signal["action"].endswith("_FULL_FLATTEN")
        allowed_fields = common | {"entry_alert_id", "trade_ref"}
        if not is_full_flatten:
            allowed_fields = allowed_fields | {"quantity"}
        if set(signal) - allowed_fields:
            raise ExecutionBlocked("INVALID_SIGNAL_FIELDS", "Close signal contains unsupported fields")
        references = [
            field for field in ("entry_alert_id", "trade_ref") if isinstance(signal.get(field), str) and signal[field]
        ]
        if len(references) != 1:
            raise ExecutionBlocked(
                "ENTRY_REFERENCE_REQUIRED", "Close signal requires exactly one entry_alert_id or trade_ref"
            )
        if not is_full_flatten:
            quantity = signal.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ExecutionBlocked(
                    "CLOSE_QUANTITY_INVALID", "REDUCE_ONLY_PARTIAL requires an explicit positive integer quantity"
                )
    return json.loads(json.dumps(request))


def _management_response(metadata: dict[str, Any]) -> dict[str, Any]:
    app_managed = metadata["managementMode"] == "APP_MANAGED"
    return {
        "source": metadata["source"],
        "ownership": metadata["ownership"],
        "managementMode": metadata["managementMode"],
        "managementPolicy": metadata["managementPolicy"],
        "managementStatus": "PENDING_EXECUTION_LEDGER" if app_managed else "ENTRY_ONLY",
    }


def _validate_signal_freshness(
    sent_at: str,
    *,
    now: datetime,
    max_age_seconds: float,
    max_future_skew_seconds: float,
) -> None:
    try:
        parsed = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ExecutionBlocked("SIGNAL_TIME_INVALID", "Signal time is not valid ISO-8601") from None
    if parsed.tzinfo is None:
        raise ExecutionBlocked("SIGNAL_TIME_INVALID", "Signal time must include a timezone")
    age = (now - parsed.astimezone(UTC)).total_seconds()
    if age > max_age_seconds:
        raise ExecutionBlocked("SIGNAL_EXPIRED", "Signal expired before broker submission")
    if age < -max_future_skew_seconds:
        raise ExecutionBlocked("SIGNAL_FROM_FUTURE", "Signal time exceeds the submission clock-skew bound")


def _mask_account(account: str) -> str:
    return f"••••{account[-4:]}" if len(account) > 4 else "••••"


def _verify_underlying(contract: QualifiedContract, symbol: str) -> None:
    if contract.con_id <= 0 or contract.symbol != symbol or contract.sec_type != "STK":
        raise ExecutionBlocked("UNDERLYING_QUALIFICATION_FAILED", "IBKR did not return one exact stock/ETF underlying")
    if contract.currency != "USD" or not contract.exchange:
        raise ExecutionBlocked("UNDERLYING_QUALIFICATION_FAILED", "Underlying currency/exchange is invalid")


def _verify_option(
    contract: QualifiedContract,
    symbol: str,
    expiry: str,
    strike: Decimal,
    right: str,
    trading_class: str,
    multiplier: str,
) -> None:
    expected = (
        contract.con_id > 0
        and contract.symbol == symbol
        and contract.sec_type == "OPT"
        and contract.expiry == expiry
        and contract.strike == strike
        and contract.right == right
        and contract.trading_class == trading_class
        and contract.multiplier == multiplier
        and contract.currency == "USD"
        and bool(contract.local_symbol)
    )
    if not expected:
        raise ExecutionBlocked(
            "OPTION_QUALIFICATION_MISMATCH", "Qualified option does not exactly match selected chain attributes"
        )


def _positive_decimal(value: str, code: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ExecutionBlocked(code, "Contract multiplier is invalid") from None
    if not result.is_finite() or result <= 0:
        raise ExecutionBlocked(code, "Contract multiplier is invalid")
    return result


def _validate_capital_per_trade_dollars(value: Any) -> str:
    """Validate and canonicalize the optional open-signal capital_per_trade_dollars
    field. Returns a plain Decimal-parseable string for downstream re-parsing."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ExecutionBlocked(
            "CAPITAL_PER_TRADE_DOLLARS_INVALID", "capital_per_trade_dollars must be a positive decimal amount"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ExecutionBlocked(
            "CAPITAL_PER_TRADE_DOLLARS_INVALID", "capital_per_trade_dollars must be a positive decimal amount"
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise ExecutionBlocked(
            "CAPITAL_PER_TRADE_DOLLARS_INVALID", "capital_per_trade_dollars must be a positive decimal amount"
        )
    if parsed > MAX_CAPITAL_PER_TRADE_DOLLARS:
        raise ExecutionBlocked(
            "CAPITAL_PER_TRADE_DOLLARS_INVALID",
            f"capital_per_trade_dollars exceeds the {MAX_CAPITAL_PER_TRADE_DOLLARS} sanity ceiling",
        )
    return str(parsed)


def _order_ref(correlation_id: str) -> str:
    return "QT" + hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()[:30]


def _entry_reference(signal: dict[str, Any]) -> str:
    if "entry_alert_id" in signal:
        return f"tradingview:{signal['entry_alert_id']}"
    return signal["trade_ref"]


def _contract_to_json(contract: QualifiedContract) -> dict[str, Any]:
    value = asdict(contract)
    value["strike"] = str(contract.strike)
    value["min_tick"] = str(contract.min_tick) if contract.min_tick is not None else None
    value["valid_exchanges"] = list(contract.valid_exchanges)
    value["market_rule_ids"] = list(contract.market_rule_ids)
    return value


def _contract_from_json(value: dict[str, Any]) -> QualifiedContract:
    return QualifiedContract(
        con_id=int(value["con_id"]),
        symbol=value["symbol"],
        sec_type=value["sec_type"],
        exchange=value["exchange"],
        currency=value["currency"],
        primary_exchange=value.get("primary_exchange", ""),
        local_symbol=value.get("local_symbol", ""),
        expiry=value.get("expiry", ""),
        strike=Decimal(value.get("strike", "0")),
        right=value.get("right", ""),
        multiplier=value.get("multiplier", ""),
        trading_class=value.get("trading_class", ""),
        valid_exchanges=tuple(value.get("valid_exchanges", ())),
        market_rule_ids=tuple(value.get("market_rule_ids", ())),
        min_tick=Decimal(value["min_tick"]) if value.get("min_tick") is not None else None,
    )
