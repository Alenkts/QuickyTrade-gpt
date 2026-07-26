"""Validated configuration for an IBKR core process.

Paper accounts (``DU...``) work with no extra opt-in. A live account
(``U...``) additionally requires ``QT_LIVE_TRADING_CONFIRMED`` to be set to
the exact confirmation phrase and an explicit live account allowlist, so live
trading can never be enabled by copying a paper config and changing one value.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

PAPER_PORTS = frozenset({7497, 4002})
LIVE_PORTS = frozenset({7496, 4001})
PAPER_ACCOUNT_PATTERN = re.compile(r"^DU[A-Z0-9]{3,30}$")
LIVE_ACCOUNT_PATTERN = re.compile(r"^U[A-Z0-9]{3,30}$")
LIVE_TRADING_CONFIRMATION_PHRASE = "I_ACCEPT_LIVE_TRADING_RISK"


@dataclass(frozen=True)
class CoreConfig:
    ibkr_host: str
    ibkr_port: int
    client_id: int
    selected_account: str
    paper_account_allowlist: frozenset[str]
    allowed_symbols: frozenset[str]
    trading_class_allowlist: frozenset[str]
    state_db_path: Path
    service_token: str
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    request_timeout_seconds: float = 8.0
    acknowledgement_timeout_seconds: float = 5.0
    max_quote_age: timedelta = timedelta(seconds=3)
    max_slippage_dollars: Decimal = Decimal("0.05")
    max_slippage_percent: Decimal = Decimal("0.05")
    max_option_spread_dollars: Decimal = Decimal("0.25")
    max_option_spread_percent: Decimal = Decimal("0.20")
    max_contract_premium_dollars: Decimal = Decimal("500")
    max_contracts_per_order: int = 1
    # Minimum acceptable option premium (per share, i.e. the quoted mid) for a
    # NEW entry. Sub-dollar 0DTE premium makes IBKR's per-contract commission a
    # dominant share of gross P&L (observed: $1.90 commission against $3.00
    # gross on a 0.11 entry -- 63%), and one tick is a double-digit percentage
    # of the position, so the configured take-profit/stop percentages cannot be
    # expressed accurately at that price. Blocks with PREMIUM_BELOW_MINIMUM
    # rather than silently accepting economics the policy cannot express.
    # Entries only -- never applied to a close, protection leg, or trail.
    min_entry_premium: Decimal = Decimal("1.00")
    # Declaration-only: the per-trade capital budget the operator intends to
    # inject with each open (from the Node connection profile's
    # capital_per_trade_dollars). The core never sizes from this value -- it
    # exists so a budget that could never bind, because
    # max_contracts_per_order is still at its conservative default of 1, is
    # rejected at startup instead of silently producing one-contract orders.
    expected_capital_per_trade_dollars: Decimal | None = None
    max_signal_age: timedelta = timedelta(minutes=5)
    max_signal_future_skew: timedelta = timedelta(seconds=30)
    exchange_timezone: str = "America/New_York"
    same_day_entry_cutoff: time = time(15, 30)
    strike_target_metric: str = "PREMIUM"
    strike_target_lo: Decimal = Decimal("1.00")
    strike_target_hi: Decimal = Decimal("2.50")
    strike_candidate_count: int = 7
    live_account_allowlist: frozenset[str] = frozenset()
    live_trading_confirmed: bool = False
    # How often the background reconciliation sweep re-runs (see __main__.py).
    # 45s: frequent enough that a SUBMISSION_UNKNOWN left over from a
    # disconnect/timeout gets a real chance to resolve (and stop globally
    # blocking new entries) within roughly a minute of the fill/cancel
    # actually landing at IBKR, while staying well clear of hammering the
    # socket with reqExecutions/reqCompletedOrders/reqPositions/
    # reqAllOpenOrders every cycle (each with its own request_timeout_seconds
    # wait) on a long-lived connection.
    reconciliation_interval_seconds: float = 45.0

    def validate(self) -> None:
        _require_loopback(self.ibkr_host, "IBKR host")
        _require_loopback(self.http_host, "HTTP host")
        if not 1 <= self.client_id <= 2_147_483_647:
            raise ValueError("client_id must be a fixed positive integer")
        if not self.selected_account:
            raise ValueError("A selected account is required")
        if is_paper_account(self.selected_account):
            if self.ibkr_port in LIVE_PORTS or self.ibkr_port not in PAPER_PORTS:
                raise ValueError("Only standard IBKR paper ports 7497 and 4002 are allowed for a paper account")
            if self.selected_account not in self.paper_account_allowlist:
                raise ValueError("The selected account must be in the exact paper allowlist")
            if any(
                account != account.strip() or not is_paper_account(account) for account in self.paper_account_allowlist
            ):
                raise ValueError("Paper account allowlist entries must be exact IBKR DU paper-account identifiers")
        elif is_live_account(self.selected_account):
            if not self.live_trading_confirmed:
                raise ValueError(
                    "Live account selected but live trading is not confirmed; set "
                    f"QT_LIVE_TRADING_CONFIRMED={LIVE_TRADING_CONFIRMATION_PHRASE} to enable it"
                )
            if self.ibkr_port not in LIVE_PORTS:
                raise ValueError("Only standard IBKR live ports 7496 and 4001 are allowed for a live account")
            if not self.live_account_allowlist:
                raise ValueError("A live account allowlist is required when live trading is confirmed")
            if self.selected_account not in self.live_account_allowlist:
                raise ValueError("The selected account must be in the exact live allowlist")
            if any(
                account != account.strip() or not is_live_account(account) for account in self.live_account_allowlist
            ):
                raise ValueError("Live account allowlist entries must be exact IBKR live account identifiers")
        else:
            raise ValueError(
                "The selected account must be a valid IBKR paper (DU...) or live (U...) account identifier"
            )
        if not self.allowed_symbols:
            raise ValueError("At least one underlying symbol must be allowlisted")
        if not self.trading_class_allowlist:
            raise ValueError("At least one option trading class must be allowlisted")
        if len(self.service_token.encode("utf-8")) < 32:
            raise ValueError("The loopback service token must contain at least 32 bytes")
        # max_contracts_per_order is a deployment-level safety ceiling (set via
        # QT_MAX_CONTRACTS_PER_ORDER, requires a core restart to change). It no
        # longer hard-locks to exactly one contract -- the execution engine now
        # derives an order's actual quantity from capital_per_trade_dollars and
        # the option's fresh mid-price, then clamps that computed quantity to
        # this ceiling. This is still a hard cap regardless of what the
        # capital/price math implies; it never expands automatically.
        if (
            isinstance(self.max_contracts_per_order, bool)
            or not isinstance(self.max_contracts_per_order, int)
            or self.max_contracts_per_order < 1
        ):
            raise ValueError("max_contracts_per_order must be a positive integer safety ceiling")
        if self.max_quote_age.total_seconds() <= 0:
            raise ValueError("max_quote_age must be positive")
        if self.max_slippage_dollars < 0 or self.max_slippage_percent < 0:
            raise ValueError("Slippage caps cannot be negative")
        if self.max_option_spread_dollars <= 0 or self.max_option_spread_percent <= 0:
            raise ValueError("Option spread limits must be positive")
        if self.max_contract_premium_dollars <= 0:
            raise ValueError("Contract premium cap must be positive")
        if self.max_signal_age.total_seconds() <= 0 or self.max_signal_future_skew.total_seconds() < 0:
            raise ValueError("Signal age and future-skew bounds are invalid")
        if not 1 <= self.http_port <= 65535:
            raise ValueError("HTTP port is invalid")
        if self.strike_target_metric not in {"DELTA", "PREMIUM"}:
            raise ValueError("strike_target_metric must be DELTA or PREMIUM")
        if not self.strike_target_lo.is_finite() or not self.strike_target_hi.is_finite():
            raise ValueError("strike_target_lo/hi must be finite")
        if self.strike_target_lo <= 0 or self.strike_target_hi <= 0:
            raise ValueError("strike_target_lo/hi must be positive")
        if self.strike_target_lo > self.strike_target_hi:
            raise ValueError("strike_target_lo must not exceed strike_target_hi")
        if self.strike_target_metric == "DELTA" and self.strike_target_hi > 1:
            raise ValueError("strike_target_hi must be at most 1 when strike_target_metric is DELTA")
        if isinstance(self.strike_candidate_count, bool) or not isinstance(self.strike_candidate_count, int):
            raise ValueError("strike_candidate_count must be an integer")
        if not 1 <= self.strike_candidate_count <= 50:
            raise ValueError("strike_candidate_count must be from 1 through 50")
        if self.reconciliation_interval_seconds <= 0:
            raise ValueError("reconciliation_interval_seconds must be positive")

    @classmethod
    def from_environment(cls) -> CoreConfig:
        account = os.environ.get("QT_IBKR_PAPER_ACCOUNT") or os.environ.get("QT_IBKR_LIVE_ACCOUNT", "")
        allowlist = _csv("QT_IBKR_PAPER_ACCOUNT_ALLOWLIST")
        live_allowlist = _csv("QT_IBKR_LIVE_ACCOUNT_ALLOWLIST")
        config = cls(
            ibkr_host=os.environ.get("QT_IBKR_HOST", "127.0.0.1"),
            ibkr_port=int(os.environ.get("QT_IBKR_PORT", "7497")),
            client_id=int(os.environ.get("QT_IBKR_CLIENT_ID", "71")),
            selected_account=account,
            paper_account_allowlist=frozenset(allowlist),
            live_account_allowlist=frozenset(live_allowlist),
            live_trading_confirmed=os.environ.get("QT_LIVE_TRADING_CONFIRMED", "") == LIVE_TRADING_CONFIRMATION_PHRASE,
            allowed_symbols=frozenset(_csv("QT_ALLOWED_SYMBOLS")),
            trading_class_allowlist=frozenset(_csv("QT_TRADING_CLASS_ALLOWLIST")),
            state_db_path=Path(os.environ.get(
                "QT_CORE_STATE_DB",
                str(Path.home() / ".quickytrade" / "core-submissions.sqlite3"),
            )).expanduser(),
            service_token=os.environ.get("QT_CORE_TOKEN", ""),
            http_host=os.environ.get("QT_CORE_HTTP_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("QT_CORE_HTTP_PORT", "8765")),
            strike_target_metric=os.environ.get("QT_STRIKE_TARGET_METRIC", "PREMIUM"),
            strike_target_lo=Decimal(os.environ.get("QT_STRIKE_TARGET_LO", "1.00")),
            strike_target_hi=Decimal(os.environ.get("QT_STRIKE_TARGET_HI", "2.50")),
            strike_candidate_count=int(os.environ.get("QT_STRIKE_CANDIDATE_COUNT", "7")),
            # Conservative default: unless an operator explicitly raises this via
            # QT_MAX_CONTRACTS_PER_ORDER, behavior is unchanged from before this
            # release (one contract per order).
            max_contracts_per_order=int(os.environ.get("QT_MAX_CONTRACTS_PER_ORDER", "1")),
            reconciliation_interval_seconds=float(os.environ.get("QT_RECONCILE_INTERVAL_SECONDS", "45")),
        )
        config.validate()
        return config


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in os.environ.get(name, "").split(",") if part.strip())


def _require_loopback(value: str, label: str) -> None:
    if value.lower() == "localhost":
        return
    try:
        if ipaddress.ip_address(value).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError(f"{label} must be a numeric loopback address or localhost")


def is_paper_account(value: str) -> bool:
    return isinstance(value, str) and PAPER_ACCOUNT_PATTERN.fullmatch(value) is not None


def is_live_account(value: str) -> bool:
    return isinstance(value, str) and LIVE_ACCOUNT_PATTERN.fullmatch(value) is not None
