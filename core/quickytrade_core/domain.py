"""Broker-neutral execution contracts for the QuickyTrade TWS core.

The transport deliberately exposes IBKR identifiers and raw statuses.  The
execution engine may normalize a response, but it must never hide the broker
evidence needed for reconciliation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class QualifiedContract:
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    primary_exchange: str = ""
    local_symbol: str = ""
    expiry: str = ""
    strike: Decimal = Decimal("0")
    right: str = ""
    multiplier: str = ""
    trading_class: str = ""
    valid_exchanges: tuple[str, ...] = ()
    market_rule_ids: tuple[int | None, ...] = ()
    min_tick: Decimal | None = None


@dataclass(frozen=True)
class OptionChain:
    exchange: str
    underlying_con_id: int
    trading_class: str
    multiplier: str
    expirations: tuple[str, ...]
    strikes: tuple[Decimal, ...]


@dataclass(frozen=True)
class Quote:
    bid: Decimal | None
    ask: Decimal | None
    received_at: datetime
    market_data_type: str
    source: str = "IBKR_TWS"
    exchange_time: datetime | None = None
    delta: Decimal | None = None


@dataclass(frozen=True)
class PriceIncrement:
    low_edge: Decimal
    increment: Decimal


@dataclass(frozen=True)
class Position:
    account: str
    contract: QualifiedContract
    quantity: Decimal
    average_cost: Decimal | None = None


@dataclass(frozen=True)
class WorkingOrder:
    account: str
    contract: QualifiedContract
    action: str
    remaining: Decimal | None
    order_id: int
    client_id: int
    perm_id: int | None
    order_ref: str
    raw_status: str


@dataclass(frozen=True)
class Readiness:
    connected: bool
    handshake_complete: bool
    server_time_received: bool
    reconciled: bool
    managed_accounts: tuple[str, ...]
    environment: str
    read_only: bool | None
    blocking_reason: str | None = None


@dataclass(frozen=True)
class LimitOrderRequest:
    account: str
    contract: QualifiedContract
    action: str
    quantity: int
    limit_price: Decimal
    tif: str
    outside_rth: bool
    order_ref: str
    # Additive/optional: empty string means "not part of any OCA group",
    # preserving exact prior behavior for every existing entry/close caller.
    # Used by protection (Phase 3) to pair a take-profit SELL LMT leg with its
    # quantity-matched stop-loss slice so either fill cancels the other.
    oca_group: str = ""
    oca_type: int = 0


@dataclass(frozen=True)
class StopLimitOrderRequest:
    """A protective STP LMT order (never a plain market-triggered STP -- this
    codebase never places market-priced orders). Used only for the
    stop-loss leg of app-managed protection (Phase 3); entries and closes
    continue to use LimitOrderRequest exclusively."""

    account: str
    contract: QualifiedContract
    action: str
    quantity: int
    trigger_price: Decimal
    limit_price: Decimal
    tif: str
    outside_rth: bool
    order_ref: str
    oca_group: str = ""
    oca_type: int = 0


@dataclass(frozen=True)
class BrokerAcknowledgement:
    order_id: int
    client_id: int
    perm_id: int | None
    raw_status: str
    parent_id: int | None = None
    oca_group: str = ""
    warnings: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class CancelAcknowledgement:
    """A *confirmed* broker cancellation (never returned for a timed-out or
    otherwise ambiguous outcome -- see ``BrokerTransport.cancel_order``).
    ``raw_status`` carries IBKR's own terminal status string (e.g.
    ``"Cancelled"``/``"ApiCancelled"``) purely as audit evidence, never
    normalized away."""

    order_id: int
    raw_status: str


class BrokerDefinitiveError(Exception):
    """The broker definitively refused a request before accepting the order."""

    def __init__(self, code: str, message: str, *, broker_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.broker_code = broker_code


class BrokerAmbiguousError(Exception):
    """The socket outcome is unknown and must be reconciled, never retried."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrokerTransport(Protocol):
    """High-level surface implemented by the official TWS API adapter."""

    @property
    def client_id(self) -> int: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def readiness(self) -> Readiness: ...

    def qualify_underlying(self, symbol: str) -> QualifiedContract: ...

    def option_chains(self, underlying: QualifiedContract) -> Sequence[OptionChain]: ...

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
    ) -> QualifiedContract: ...

    def quote(self, contract: QualifiedContract) -> Quote: ...

    def market_rule(self, contract: QualifiedContract) -> Sequence[PriceIncrement]: ...

    def positions(self, account: str) -> Sequence[Position]: ...

    def working_orders(self, account: str) -> Sequence[WorkingOrder]: ...

    def place_limit_order(self, order: LimitOrderRequest) -> BrokerAcknowledgement: ...

    def place_stop_limit_order(self, order: StopLimitOrderRequest) -> BrokerAcknowledgement: ...

    def modify_stop_limit_order(self, order_id: int, order: StopLimitOrderRequest) -> BrokerAcknowledgement: ...

    def cancel_order(self, order_id: int) -> CancelAcknowledgement:
        """Cancel an existing working order and wait for a *definitive*
        broker confirmation. Must raise ``BrokerAmbiguousError`` (never
        return a value, never raise anything else) for any outcome that is
        not a proven cancellation -- a timeout, disconnect, or an order
        observed in some other terminal state (e.g. filled) during the
        cancel race. Callers that must never oversell (a full flatten)
        depend on this being conservative: only a genuine confirmed
        cancellation may return normally."""
        ...

