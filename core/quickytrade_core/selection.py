"""Deterministic option selection and tick-safe marketable limits."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from zoneinfo import ZoneInfo

from .domain import OptionChain, PriceIncrement, Quote


class SelectionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_quote(quote: Quote, *, now: datetime, max_age_seconds: float) -> tuple[Decimal, Decimal]:
    if quote.market_data_type.upper() not in {"LIVE", "UNKNOWN"}:
        raise SelectionError("MARKET_DATA_NOT_LIVE", "A live IBKR market-data quote is required")
    if quote.received_at.tzinfo is None or now.tzinfo is None:
        raise SelectionError("QUOTE_TIMESTAMP_INVALID", "Quote and current timestamps must be timezone-aware")
    age = (now - quote.received_at).total_seconds()
    if age < -1:
        raise SelectionError("QUOTE_TIMESTAMP_INVALID", "Quote receipt time is in the future")
    if age > max_age_seconds:
        raise SelectionError("QUOTE_STALE", "The IBKR quote is older than the configured maximum age")
    if quote.bid is None or quote.ask is None:
        raise SelectionError("QUOTE_MISSING", "Both IBKR bid and ask are required")
    bid, ask = quote.bid, quote.ask
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0:
        raise SelectionError("QUOTE_INVALID", "IBKR bid and ask must be finite positive values")
    if bid > ask:
        raise SelectionError("QUOTE_CROSSED", "IBKR bid exceeds ask")
    return bid, ask


def choose_chain_and_expiry(
    chains: tuple[OptionChain, ...],
    *,
    target_dte: int,
    now: datetime,
    timezone_name: str,
    trading_class_allowlist: frozenset[str],
    same_day_cutoff,
) -> tuple[OptionChain, str]:
    if isinstance(target_dte, bool) or not isinstance(target_dte, int) or target_dte < 0:
        raise SelectionError("TARGET_DTE_INVALID", "target_dte must be a non-negative integer")
    local_now = now.astimezone(ZoneInfo(timezone_name))
    if target_dte == 0 and local_now.time().replace(tzinfo=None) >= same_day_cutoff:
        raise SelectionError("SAME_DAY_ENTRY_CUTOFF", "Same-day option entry cutoff has passed")

    candidates: list[tuple[OptionChain, str]] = []
    for chain in chains:
        if chain.trading_class not in trading_class_allowlist:
            continue
        for expiry in chain.expirations:
            try:
                expiry_date = datetime.strptime(expiry, "%Y%m%d").date()
            except ValueError:
                continue
            if (expiry_date - local_now.date()).days == target_dte:
                candidates.append((chain, expiry))

    # IBKR's reqSecDefOptParams returns one row per exchange the option class is
    # listed on (SMART plus each real exchange, e.g. CBOE/BOX/PHLX) -- for a
    # multi-listed underlying like SPY/QQQ that's normal and does not describe
    # distinct products, so exchange is excluded from the uniqueness key here.
    # When a SMART-routed row is present among the duplicates it wins, since
    # that's the venue IBKR will actually use to route the order.
    unique: dict[tuple[str, str, str], tuple[OptionChain, str]] = {}
    for chain, expiry in candidates:
        key = (chain.trading_class, chain.multiplier, expiry)
        existing = unique.get(key)
        if existing is None or (existing[0].exchange != "SMART" and chain.exchange == "SMART"):
            unique[key] = (chain, expiry)
    if not unique:
        raise SelectionError("TARGET_EXPIRY_UNAVAILABLE", "No allowlisted option chain has the exact target DTE")
    if len(unique) != 1:
        raise SelectionError("OPTION_CHAIN_AMBIGUOUS", "More than one allowlisted option chain matches")
    return next(iter(unique.values()))


def choose_listed_strike(
    chain: OptionChain,
    *,
    spot: Decimal,
    right: str,
    offset: int,
) -> Decimal:
    if not spot.is_finite() or spot <= 0:
        raise SelectionError("UNDERLYING_PRICE_INVALID", "Underlying quote midpoint must be positive")
    if right not in {"C", "P"}:
        raise SelectionError("OPTION_RIGHT_INVALID", "Option right must be C or P")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise SelectionError("STRIKE_OFFSET_INVALID", "ATM_OFFSET offset must be an integer")
    strikes = sorted({strike for strike in chain.strikes if strike.is_finite() and strike > 0})
    if not strikes:
        raise SelectionError("STRIKES_UNAVAILABLE", "The selected chain has no valid listed strikes")
    atm_index = min(range(len(strikes)), key=lambda index: (abs(strikes[index] - spot), strikes[index]))
    # Positive means farther OTM: higher strike for calls, lower for puts.
    selected_index = atm_index + offset if right == "C" else atm_index - offset
    if selected_index < 0 or selected_index >= len(strikes):
        raise SelectionError("STRIKE_OFFSET_OUT_OF_RANGE", "The listed-strike offset is outside the chain")
    return strikes[selected_index]


def candidate_strikes(
    chain: OptionChain,
    *,
    spot: Decimal,
    right: str,
    count: int,
) -> tuple[Decimal, ...]:
    if not spot.is_finite() or spot <= 0:
        raise SelectionError("UNDERLYING_PRICE_INVALID", "Underlying quote midpoint must be positive")
    if right not in {"C", "P"}:
        raise SelectionError("OPTION_RIGHT_INVALID", "Option right must be C or P")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise SelectionError("STRIKE_CANDIDATE_COUNT_INVALID", "candidate count must be a positive integer")
    strikes = sorted({strike for strike in chain.strikes if strike.is_finite() and strike > 0})
    if not strikes:
        raise SelectionError("STRIKES_UNAVAILABLE", "The selected chain has no valid listed strikes")
    atm_index = min(range(len(strikes)), key=lambda index: (abs(strikes[index] - spot), strikes[index]))
    step = 1 if right == "C" else -1
    indices = (atm_index + step * offset for offset in range(count))
    return tuple(strikes[index] for index in indices if 0 <= index < len(strikes))


def choose_strike_by_target_range(
    chain: OptionChain,
    *,
    spot: Decimal,
    right: str,
    lo: Decimal,
    hi: Decimal,
    candidate_count: int,
    metric_by_strike: dict[Decimal, Decimal],
) -> Decimal:
    if not lo.is_finite() or not hi.is_finite() or lo <= 0 or hi <= 0 or lo > hi:
        raise SelectionError("TARGET_RANGE_BOUNDS_INVALID", "lo/hi must be finite, positive, and ordered")
    candidates = candidate_strikes(chain, spot=spot, right=right, count=candidate_count)
    # Preserves ATM-first ordering from candidate_strikes(); min() below returns the
    # first minimal element on a tie, which makes ties resolve toward the ATM side
    # deterministically without a separate tie-break key.
    eligible = [strike for strike in candidates if strike in metric_by_strike]
    if not eligible:
        raise SelectionError("TARGET_RANGE_NO_ELIGIBLE_STRIKE", "No candidate strike had a usable quote")
    target = (lo + hi) / 2
    in_range = [strike for strike in eligible if lo <= metric_by_strike[strike] <= hi]
    pool = in_range if in_range else eligible
    return min(pool, key=lambda strike: abs(metric_by_strike[strike] - target))


def applicable_increment(price: Decimal, rules: tuple[PriceIncrement, ...]) -> Decimal:
    valid = sorted(
        (rule for rule in rules if rule.low_edge.is_finite() and rule.increment.is_finite()
         and rule.low_edge >= 0 and rule.increment > 0),
        key=lambda rule: rule.low_edge,
    )
    if not valid or valid[0].low_edge != 0:
        raise SelectionError("MARKET_RULE_INVALID", "IBKR market rule must begin at a zero low edge")
    applicable = [rule.increment for rule in valid if rule.low_edge <= price]
    if not applicable:
        raise SelectionError("MARKET_RULE_INVALID", "No market-rule increment applies to the price")
    return applicable[-1]


def round_to_tick(price: Decimal, increment: Decimal, *, upward: bool) -> Decimal:
    if not price.is_finite() or price <= 0 or not increment.is_finite() or increment <= 0:
        raise SelectionError("PRICE_INCREMENT_INVALID", "Price and increment must be finite and positive")
    mode = ROUND_CEILING if upward else ROUND_FLOOR
    return (price / increment).to_integral_value(rounding=mode) * increment


def marketable_limit(
    *,
    action: str,
    bid: Decimal,
    ask: Decimal,
    rules: tuple[PriceIncrement, ...],
    max_slippage_dollars: Decimal,
    max_slippage_percent: Decimal,
) -> Decimal:
    if action == "BUY":
        slippage = min(max_slippage_dollars, ask * max_slippage_percent)
        cap = ask + slippage
        increment = applicable_increment(cap, rules)
        limit_price = round_to_tick(cap, increment, upward=False)
        marketable_floor = round_to_tick(ask, applicable_increment(ask, rules), upward=True)
        if limit_price < marketable_floor or limit_price > cap:
            raise SelectionError("TICK_VALID_MARKETABLE_LIMIT_UNAVAILABLE", "No tick-valid buy limit fits the slippage cap")
        return limit_price
    if action == "SELL":
        slippage = min(max_slippage_dollars, bid * max_slippage_percent)
        floor = bid - slippage
        if floor <= 0:
            raise SelectionError("SELL_PRICE_FLOOR_INVALID", "Sell slippage cap produces a non-positive price")
        increment = applicable_increment(bid, rules)
        limit_price = round_to_tick(bid, increment, upward=False)
        if limit_price < floor or limit_price > bid:
            raise SelectionError("TICK_VALID_MARKETABLE_LIMIT_UNAVAILABLE", "No tick-valid sell limit fits the slippage cap")
        return limit_price
    raise SelectionError("ORDER_ACTION_INVALID", "Only BUY and SELL limit actions are supported")

