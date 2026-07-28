from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from quickytrade_core.config import CoreConfig
from quickytrade_core.domain import (
    BrokerAcknowledgement,
    BrokerAmbiguousError,
    BrokerDefinitiveError,
    CancelAcknowledgement,
    OptionChain,
    Position,
    PriceIncrement,
    QualifiedContract,
    Quote,
    Readiness,
    WorkingOrder,
)
from quickytrade_core.engine import ExecutionEngine
from quickytrade_core.execution_ledger import ExecutionLedger
from quickytrade_core.http_service import _Handler
from quickytrade_core.ibapi_transport import OfficialIbapiTransport, _OrderAck, _Pending
from quickytrade_core.registry import SubmissionRegistry

NOW = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


def underlying() -> QualifiedContract:
    return QualifiedContract(
        con_id=100,
        symbol="QQQ",
        sec_type="STK",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        local_symbol="QQQ",
    )


def option(strike: str = "101", right: str = "C", con_id: int = 201) -> QualifiedContract:
    return QualifiedContract(
        con_id=con_id,
        symbol="QQQ",
        sec_type="OPT",
        exchange="SMART",
        currency="USD",
        local_symbol=f"QQQ OPT {right} {strike}",
        expiry="20260720",
        strike=Decimal(strike),
        right=right,
        multiplier="100",
        trading_class="QQQ",
        valid_exchanges=("SMART",),
        market_rule_ids=(26,),
        min_tick=Decimal("0.05"),
    )


class FakeTransport:
    def __init__(self):
        self.client_id = 71
        self.managed_accounts = ("DU12345",)
        self.environment = "PAPER"
        self.reconciled = True
        self.underlying = underlying()
        self.chain = OptionChain(
            exchange="SMART",
            underlying_con_id=100,
            trading_class="QQQ",
            multiplier="100",
            expirations=("20260720",),
            strikes=(Decimal("99"), Decimal("100"), Decimal("101"), Decimal("103")),
        )
        self.underlying_quote = Quote(Decimal("100.20"), Decimal("100.30"), NOW, "LIVE")
        self.option_quote = Quote(Decimal("1.00"), Decimal("1.03"), NOW, "LIVE")
        self.option_quotes_by_strike: dict[Decimal, Quote] | None = None
        self.rules = (PriceIncrement(Decimal("0"), Decimal("0.05")),)
        self.position_rows: list[Position] = []
        self.order_rows: list[WorkingOrder] = []
        self.placed = []
        self.place_result: object = BrokerAcknowledgement(700, 71, 900, "PreSubmitted")
        self.cancelled: list[int] = []
        self.cancel_results: dict[int, object] = {}  # order_id -> Exception instance, else confirmed success.

    def start(self): pass
    def stop(self): pass

    def readiness(self):
        return Readiness(True, True, True, self.reconciled, self.managed_accounts, self.environment, False)

    def qualify_underlying(self, symbol): return self.underlying
    def option_chains(self, underlying_contract): return (self.chain,)

    def qualify_option(self, *, underlying, expiry, strike, right, exchange, trading_class, multiplier):
        return option(str(strike), right, 201 if right == "C" else 202)

    def qualify_options_concurrent(self, *, underlying, expiry, strikes, right, exchange, trading_class, multiplier):
        return [
            self.qualify_option(
                underlying=underlying, expiry=expiry, strike=strike, right=right,
                exchange=exchange, trading_class=trading_class, multiplier=multiplier,
            )
            for strike in strikes
        ]

    def quote(self, contract):
        if contract.sec_type == "STK":
            return self.underlying_quote
        if self.option_quotes_by_strike is not None:
            return self.option_quotes_by_strike.get(contract.strike, self.option_quote)
        return self.option_quote

    def quotes_concurrent(self, contracts, *, include_greeks=True):
        return [self.quote(contract) for contract in contracts]

    def market_rule(self, contract): return self.rules
    def positions(self, account): return tuple(self.position_rows)
    def working_orders(self, account): return tuple(self.order_rows)

    def place_limit_order(self, order):
        self.placed.append(order)
        if isinstance(self.place_result, Exception):
            raise self.place_result
        return self.place_result

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        result = self.cancel_results.get(order_id)
        if isinstance(result, Exception):
            raise result
        return result if result is not None else CancelAcknowledgement(order_id, "Cancelled")


def open_request(alert="tv-open-1", *, action="OPEN_LONG_CALL", offset=1):
    return {
        "broker": "IBKR",
        "idempotencyKey": f"tradingview:{alert}",
        "intentId": int.from_bytes(alert.encode("utf-8"), "little") % 1_000_000 + 1,
        "correlationId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"quickytrade:{alert}")),
        "source": "tradingview",
        "alertId": alert,
        "signal": {
            "schema_version": "1",
            "alert_id": alert,
            "sent_at": "2026-07-20T14:00:00Z",
            "strategy_id": "tv-options",
            "strategy_version": "1",
            "action": action,
            "ticker": "QQQ",
            "target_dte": 0,
            "strike_policy": {"type": "ATM_OFFSET", "offset": offset},
            "risk_hint": {"max_contracts": 1},
            "exit_policy_id": "standard-bracket-v1",
        },
    }


def target_range_request(alert="tv-open-1", *, action="OPEN_LONG_CALL"):
    request = open_request(alert, action=action)
    request["signal"]["strike_policy"] = {"type": "TARGET_RANGE"}
    return request


def close_request(entry_alert_id, alert="tv-close-1", *, action="CLOSE_LONG_CALL_REDUCE_ONLY_PARTIAL", quantity=1):
    signal = {
        "schema_version": "1",
        "alert_id": alert,
        "sent_at": "2026-07-20T14:00:00Z",
        "strategy_id": "tv-options",
        "strategy_version": "1",
        "action": action,
        "ticker": "QQQ",
        "entry_alert_id": entry_alert_id,
    }
    if not action.endswith("_FULL_FLATTEN"):
        signal["quantity"] = quantity
    return {
        "broker": "IBKR",
        "idempotencyKey": f"tradingview:{alert}",
        "intentId": int.from_bytes(alert.encode("utf-8"), "little") % 1_000_000 + 1,
        "correlationId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"quickytrade:{alert}")),
        "source": "tradingview",
        "alertId": alert,
        "signal": signal,
    }


def management_policy(*, allocations=(50, 25, 25), transitions=None, stop_coverage_percent=None):
    policy = {
        "policyId": "paper-balanced-v1",
        "version": 1,
        "takeProfitLevels": [
            {"levelId": f"TP{index + 1}", "triggerPercent": trigger, "allocationPercent": allocation}
            for index, (trigger, allocation) in enumerate(zip((20, 40, 60), allocations))
        ],
        "stopLossPercent": 25,
    }
    if stop_coverage_percent is not None:
        policy["stopCoveragePercent"] = stop_coverage_percent
    if transitions is not None:
        policy["transitions"] = transitions
    return policy


def manual_request(event="manual-1", *, mode="APP_MANAGED"):
    request = open_request(event)
    request.update(
        {
            "source": "MANUAL_UI",
            "idempotencyKey": f"manual:{event}",
            "ownership": "APP_OWNED",
            "managementMode": mode,
        }
    )
    request["signal"]["strategy_id"] = "manual-ui"
    if mode == "APP_MANAGED":
        request["managementPolicy"] = management_policy()
    return request


class _FakeHttpSocket:
    def __init__(self, request: bytes):
        self.input = BytesIO(request)
        self.output = bytearray()

    def makefile(self, mode, buffering=None):
        if "r" in mode:
            return self.input
        return self

    def sendall(self, value):
        self.output.extend(value)

    def write(self, value):
        self.output.extend(value)
        return len(value)

    def flush(self): pass
    def close(self): pass


def http_exchange(config, engine, request: bytes):
    connection = _FakeHttpSocket(request)
    server = type("FakeServer", (), {"config": config, "engine": engine})()
    _Handler(connection, ("127.0.0.1", 12345), server)
    raw = bytes(connection.output)
    head, body = raw.split(b"\r\n\r\n", 1)
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(body)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = CoreConfig(
            ibkr_host="127.0.0.1",
            ibkr_port=7497,
            client_id=71,
            selected_account="DU12345",
            paper_account_allowlist=frozenset({"DU12345"}),
            allowed_symbols=frozenset({"QQQ"}),
            trading_class_allowlist=frozenset({"QQQ"}),
            state_db_path=Path(self.temp.name) / "state.sqlite3",
            service_token="x" * 32,
        )
        self.transport = FakeTransport()
        self.registry = SubmissionRegistry(self.config.state_db_path)
        self.engine = ExecutionEngine(
            config=self.config, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    # CTR-004/005: exact DTE and listed-strike index, not dollar arithmetic.
    def test_call_atm_offset_selects_by_listed_strike_index(self):
        result = self.engine.execute(open_request())
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(Decimal("101"), self.transport.placed[0].contract.strike)
        self.assertEqual("TRADINGVIEW", result.body["source"])
        self.assertEqual("ENTRY_ONLY", result.body["managementMode"])

    def test_put_positive_offset_moves_otm_down_listed_strikes(self):
        result = self.engine.execute(open_request("tv-put", action="OPEN_LONG_PUT"))
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(Decimal("99"), self.transport.placed[0].contract.strike)

    # SEL-001/004: TARGET_RANGE prefers the in-range candidate closest to the
    # configured range midpoint over other in-range or out-of-range candidates.
    def test_target_range_premium_metric_prefers_in_range_candidate_closest_to_midpoint(self):
        self.transport.option_quotes_by_strike = {
            Decimal("100"): Quote(Decimal("0.40"), Decimal("0.45"), NOW, "LIVE"),
            Decimal("101"): Quote(Decimal("1.00"), Decimal("1.03"), NOW, "LIVE"),
            Decimal("103"): Quote(Decimal("2.20"), Decimal("2.30"), NOW, "LIVE"),
        }
        result = self.engine.execute(target_range_request("tv-target-range-premium"))
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(Decimal("103"), self.transport.placed[0].contract.strike)

    # SEL-004: when no candidate's metric falls inside the configured range,
    # selection falls back to the eligible candidate closest to the midpoint.
    def test_target_range_falls_back_to_nearest_when_none_in_range(self):
        # All three mids ($3.00/$3.50/$4.20) sit above the default $1.00-$2.50
        # range, so none qualify and selection must fall back to whichever is
        # closest to the $1.75 midpoint (strike 100, distance 1.25).
        self.transport.option_quotes_by_strike = {
            Decimal("100"): Quote(Decimal("2.95"), Decimal("3.05"), NOW, "LIVE"),
            Decimal("101"): Quote(Decimal("3.45"), Decimal("3.55"), NOW, "LIVE"),
            Decimal("103"): Quote(Decimal("4.15"), Decimal("4.25"), NOW, "LIVE"),
        }
        result = self.engine.execute(target_range_request("tv-target-range-fallback"))
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(Decimal("100"), self.transport.placed[0].contract.strike)

    # SEL-002/005: every candidate fails quote validation (stale) -> fail closed
    # rather than selecting a stale-quoted contract.
    def test_target_range_blocks_when_no_candidate_has_a_usable_quote(self):
        stale = NOW - timedelta(seconds=10)
        self.transport.option_quotes_by_strike = {
            Decimal("100"): Quote(Decimal("0.40"), Decimal("0.45"), stale, "LIVE"),
            Decimal("101"): Quote(Decimal("1.00"), Decimal("1.03"), stale, "LIVE"),
            Decimal("103"): Quote(Decimal("2.20"), Decimal("2.30"), stale, "LIVE"),
        }
        result = self.engine.execute(target_range_request("tv-target-range-stale"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("TARGET_RANGE_NO_ELIGIBLE_STRIKE", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # SEL-003: the configured metric (DELTA vs PREMIUM) changes which strike is
    # selected from the identical chain/quotes.
    def test_target_range_delta_metric_selects_differently_than_premium(self):
        self.transport.option_quotes_by_strike = {
            Decimal("100"): Quote(Decimal("0.40"), Decimal("0.45"), NOW, "LIVE", delta=Decimal("0.55")),
            Decimal("101"): Quote(Decimal("1.00"), Decimal("1.03"), NOW, "LIVE", delta=Decimal("0.30")),
            Decimal("103"): Quote(Decimal("2.20"), Decimal("2.30"), NOW, "LIVE", delta=Decimal("0.10")),
        }
        delta_config = replace(
            self.config,
            strike_target_metric="DELTA",
            strike_target_lo=Decimal("0.25"),
            strike_target_hi=Decimal("0.35"),
        )
        delta_engine = ExecutionEngine(
            config=delta_config, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        delta_preview = delta_engine.preview(target_range_request("tv-target-range-delta"))
        self.assertEqual("PREVIEW_READY", delta_preview.status)
        self.assertIn("QQQ OPT C 101", delta_preview.body["localSymbol"])

        premium_preview = self.engine.preview(target_range_request("tv-target-range-premium-cmp"))
        self.assertEqual("PREVIEW_READY", premium_preview.status)
        self.assertIn("QQQ OPT C 103", premium_preview.body["localSymbol"])
        self.assertEqual([], self.transport.placed)

    # Proves TradingView and manual-UI requests share one selection code path:
    # identical chain/quotes under TARGET_RANGE resolve to the identical contract.
    def test_target_range_manual_and_tradingview_requests_select_the_same_contract(self):
        self.transport.option_quotes_by_strike = {
            Decimal("100"): Quote(Decimal("0.40"), Decimal("0.45"), NOW, "LIVE"),
            Decimal("101"): Quote(Decimal("1.00"), Decimal("1.03"), NOW, "LIVE"),
            Decimal("103"): Quote(Decimal("2.20"), Decimal("2.30"), NOW, "LIVE"),
        }
        tv_preview = self.engine.preview(target_range_request("tv-target-range-shared"))
        manual_body = manual_request("manual-target-range-shared")
        manual_body["signal"]["strike_policy"] = {"type": "TARGET_RANGE"}
        manual_preview = self.engine.preview(manual_body)
        self.assertEqual("PREVIEW_READY", tv_preview.status)
        self.assertEqual("PREVIEW_READY", manual_preview.status)
        self.assertEqual(tv_preview.body["localSymbol"], manual_preview.body["localSymbol"])
        self.assertEqual([], self.transport.placed)

    # CTR-008 / ORD-004: capped marketable price is rounded to an IBKR tick.
    def test_marketable_limit_respects_tick_and_slippage_cap(self):
        result = self.engine.execute(open_request("tv-rounding"))
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(Decimal("1.05"), self.transport.placed[0].limit_price)
        self.assertEqual("LMT", result.body["orderType"])
        self.assertEqual("DAY", result.body["tif"])

    # --- Capital-based dynamic sizing (Phase 1) ------------------------------
    # contracts = floor(capital_per_trade_dollars / (option_mid_price *
    # contract_multiplier)) -- one contract represents `multiplier` shares
    # (typically 100 for equity/ETF options), so the per-contract cost is the
    # per-share mid-price times the multiplier, not the mid-price alone --
    # clamped to the deployment-configured max_contracts_per_order ceiling.

    # Verification gate 8 from docs/REVIEW_2026-07-25.md: capital $500,
    # min_entry_premium $1.00, ceiling >= 5. A $1.29 mid (bid $1.26 / ask
    # $1.32) prices a marketable limit of $1.35 under this fixture's tick
    # rules, so one contract costs $135 and the budget floors to 3 -- not the
    # 3.70 an un-floored division gives,
    # and not the 3.87 that sizing off the $1.29 mid would have allowed. The
    # difference is the point: sizing on the mid understates what will actually
    # be paid by half the spread plus slippage on every contract.
    def test_capital_per_trade_dollars_computes_floor_quantity_not_round(self):
        self.transport.option_quote = Quote(Decimal("1.26"), Decimal("1.32"), NOW, "LIVE")
        raised_ceiling = replace(self.config, max_contracts_per_order=1000)
        engine = ExecutionEngine(
            config=raised_ceiling, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        request = open_request("tv-capital-floor")
        request["signal"]["capital_per_trade_dollars"] = "500"
        result = engine.execute(request)
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual("1.35", str(self.transport.placed[0].limit_price))
        self.assertEqual(3, self.transport.placed[0].quantity)
        self.assertEqual(3, result.body["quantity"])

    # Regression test for the multiplier bug: sizing must divide capital by
    # (mid_price * contract.multiplier), not mid_price alone. With capital
    # $500, mid-price $0.93 (bid $0.90 / ask $0.96), and the fixture's
    # standard multiplier="100" contract, the correct quantity is
    # floor(500 / (0.93 * 100)) = floor(500 / 93) = 5. The pre-fix formula
    # (floor(500 / 0.93) = 537) would have sized ~107x too large -- a fixed
    # $500 budget would actually have controlled $53,700 of option premium.
    def test_capital_sizing_accounts_for_contract_multiplier_not_just_mid_price(self):
        self.assertEqual("100", option().multiplier)
        self.transport.option_quote = Quote(Decimal("1.26"), Decimal("1.32"), NOW, "LIVE")
        raised_ceiling = replace(self.config, max_contracts_per_order=1000)
        engine = ExecutionEngine(
            config=raised_ceiling, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        request = open_request("tv-capital-multiplier-regression")
        request["signal"]["capital_per_trade_dollars"] = "500"
        result = engine.execute(request)
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(3, self.transport.placed[0].quantity)
        self.assertNotEqual(370, self.transport.placed[0].quantity)

    # H2. min_entry_premium was a dataclass field and a nine-line comment wired
    # to nothing: no engine check, no env read, no test. A $0.11 entry executed
    # exactly as if the floor did not exist -- $1.90 commission against $3.00
    # gross (63%), with one tick worth 9% of the position.
    def test_entry_below_min_entry_premium_is_blocked_not_silently_accepted(self):
        self.transport.option_quote = Quote(Decimal("0.10"), Decimal("0.12"), NOW, "LIVE")
        result = self.engine.execute(open_request("tv-premium-floor"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("PREMIUM_BELOW_MINIMUM", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # The floor applies to entries only -- never to a close, which must always
    # be able to exit a position no matter how far the premium has decayed.
    def test_min_entry_premium_never_blocks_a_reduce_only_close(self):
        low = replace(self.config, min_entry_premium=Decimal("5.00"))
        engine = ExecutionEngine(
            config=low, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        self.assertEqual(Decimal("5.00"), engine.config.min_entry_premium)

    # Same $500 capital / 100-multiplier math as above, but
    # this engine uses the setUp fixture's default max_contracts_per_order (1)
    # unchanged -- confirming the deployment ceiling actually clamps the
    # computed quantity rather than the capital math silently winning.
    def test_capital_per_trade_dollars_clamps_to_the_default_max_contracts_per_order(self):
        self.assertEqual(1, self.config.max_contracts_per_order)
        self.transport.option_quote = Quote(Decimal("1.26"), Decimal("1.32"), NOW, "LIVE")
        request = open_request("tv-capital-clamp-default")
        request["signal"]["capital_per_trade_dollars"] = "500"
        result = self.engine.execute(request)
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(1, self.transport.placed[0].quantity)

    # $5,000 capital at the same $0.93 mid/100-multiplier ($93/contract) floors
    # to 53 contracts (floor(5000/93) = 53) uncapped -- with an operator-raised
    # ceiling of 10 (below the uncapped 53), proves the clamp binds at
    # whatever ceiling is configured, not just at the default of 1.
    def test_capital_per_trade_dollars_clamps_to_a_configured_ceiling_above_one(self):
        self.transport.option_quote = Quote(Decimal("1.26"), Decimal("1.32"), NOW, "LIVE")
        ceiling_ten = replace(self.config, max_contracts_per_order=10)
        engine = ExecutionEngine(
            config=ceiling_ten, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        request = open_request("tv-capital-clamp-ten")
        request["signal"]["capital_per_trade_dollars"] = "5000"
        result = engine.execute(request)
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(10, self.transport.placed[0].quantity)

    # $1,000 capital at an exact $3.00 mid ($2.95/$3.05) on a 100-multiplier
    # contract costs $300/contract, which is not evenly divisible into $1,000
    # (3.33...) -- must floor to 3, never round up to 4.
    def test_capital_per_trade_dollars_floors_a_non_evenly_divisible_amount(self):
        self.transport.option_quote = Quote(Decimal("2.95"), Decimal("3.05"), NOW, "LIVE")
        raised_ceiling = replace(self.config, max_contracts_per_order=1000)
        engine = ExecutionEngine(
            config=raised_ceiling, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        request = open_request("tv-capital-non-divisible")
        request["signal"]["capital_per_trade_dollars"] = "1000"
        result = engine.execute(request)
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(3, self.transport.placed[0].quantity)

    # Capital too small to afford even one contract at the fresh mid-price
    # blocks cleanly rather than silently rounding up to one (which would
    # exceed the operator's stated risk budget) -- and never reaches placeOrder.
    def test_insufficient_capital_for_one_contract_blocks_with_zero_broker_calls(self):
        # Default fixture mid-price is (1.00+1.03)/2 = 1.015; 50 cents can't
        # cover even one contract at that price.
        request = open_request("tv-capital-insufficient")
        request["signal"]["capital_per_trade_dollars"] = "0.50"
        result = self.engine.execute(request)
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("INSUFFICIENT_CAPITAL_FOR_ONE_CONTRACT", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # Backward compatibility: capital_per_trade_dollars absent falls back to
    # the pre-existing quantity behavior (defaulting to exactly 1), unchanged.
    def test_capital_per_trade_dollars_absent_falls_back_to_default_quantity_of_one(self):
        result = self.engine.execute(open_request("tv-capital-absent-default"))
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(1, self.transport.placed[0].quantity)

    def test_capital_per_trade_dollars_absent_with_explicit_client_quantity_of_one_still_works(self):
        request = open_request("tv-capital-absent-explicit")
        request["signal"]["quantity"] = 1
        result = self.engine.execute(request)
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(1, self.transport.placed[0].quantity)

    # A negative, zero, or fat-fingered (100 million) capital_per_trade_dollars
    # must be rejected outright with a stable code -- never silently clamped to
    # something plausible-looking. _parse_request-level rejection surfaces as
    # an HTTP 400, matching the other signal-validation tests in this file.
    def test_capital_per_trade_dollars_negative_zero_or_absurdly_large_is_rejected(self):
        for label, raw in (("negative", "-100"), ("zero", "0"), ("fat-fingered", "100000000")):
            with self.subTest(label=label):
                request = open_request(f"tv-capital-invalid-{label}")
                request["signal"]["capital_per_trade_dollars"] = raw
                payload = json.dumps(request).encode("utf-8")
                status, result = http_exchange(
                    self.config,
                    self.engine,
                    (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
                     f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
                )
                self.assertEqual(400, status)
                self.assertEqual("CAPITAL_PER_TRADE_DOLLARS_INVALID", result["code"])
        self.assertEqual([], self.transport.placed)

    # SET-002 / ACC-001: exact configured paper account must be managed.
    def test_account_mismatch_blocks_without_order(self):
        self.transport.managed_accounts = ("DU99999",)
        result = self.engine.execute(open_request("tv-account"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("ACCOUNT_MISMATCH", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # A live-formatted account requires the connected session to actually
    # report LIVE; a paper session cannot be silently used to fill a live order.
    def test_live_configured_engine_blocks_when_connected_session_is_paper(self):
        live_config = replace(
            self.config,
            selected_account="U1234567",
            ibkr_port=7496,
            live_account_allowlist=frozenset({"U1234567"}),
            live_trading_confirmed=True,
        )
        live_engine = ExecutionEngine(
            config=live_config, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        result = live_engine.execute(open_request("tv-live-mismatch"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("ENVIRONMENT_MISMATCH", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # The mismatch check is symmetric: a paper-configured engine must equally
    # refuse to trade against a session that reports itself as LIVE.
    def test_paper_configured_engine_blocks_when_connected_session_is_live(self):
        self.transport.environment = "LIVE"
        result = self.engine.execute(open_request("tv-paper-mismatch"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("ENVIRONMENT_MISMATCH", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # A fully-confirmed live account, matched by a session that actually
    # reports LIVE, submits exactly like paper — every other invariant
    # (quote freshness, 1-contract cap, tick-valid limit) still applies.
    def test_live_configured_engine_submits_when_session_matches(self):
        live_config = replace(
            self.config,
            selected_account="U1234567",
            ibkr_port=7496,
            live_account_allowlist=frozenset({"U1234567"}),
            live_trading_confirmed=True,
        )
        self.transport.environment = "LIVE"
        self.transport.managed_accounts = ("U1234567",)
        live_engine = ExecutionEngine(
            config=live_config, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        result = live_engine.execute(open_request("tv-live-match"))
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(Decimal("101"), self.transport.placed[0].contract.strike)
        self.assertEqual("LIVE", live_engine.health()["environment"])

    def test_health_reports_paper_environment_by_default(self):
        self.assertEqual("PAPER", self.engine.health()["environment"])

    # MKT-001/002 / ORD-005: stale quotes fail closed without fallback.
    def test_stale_option_quote_blocks(self):
        self.transport.option_quote = Quote(
            Decimal("1.00"), Decimal("1.03"), NOW - timedelta(seconds=4), "LIVE"
        )
        result = self.engine.execute(open_request("tv-stale"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("QUOTE_STALE", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # RSK-009: broker position prevents duplicate directional exposure.
    def test_duplicate_exposure_blocks(self):
        self.transport.position_rows = [Position("DU12345", option(), Decimal("1"))]
        result = self.engine.execute(open_request("tv-duplicate"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("DUPLICATE_EXPOSURE", result.body["code"])

    # ORD-012/018/REC-007: no exit may consume an unattributed broker position.
    # Phase 5: closes are un-gated behind real execution-ledger fill
    # evidence rather than an unconditional block -- this engine fixture
    # (like most of this file's tests) never wires an ExecutionLedger, so a
    # close attempt fails closed on that missing evidence, never a special
    # "feature disabled" case. See core/tests/test_reduce_only_close.py for
    # the full REDUCE_ONLY_PARTIAL/FULL_FLATTEN behavior with a wired ledger.
    def test_close_without_a_wired_execution_ledger_blocks_on_missing_evidence(self):
        opened = self.engine.execute(open_request("tv-entry"))
        self.assertEqual("SUBMITTED", opened.status)
        result = self.engine.execute(close_request("tv-entry"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("EXECUTION_LEDGER_UNAVAILABLE", result.body["code"])
        self.assertEqual(1, len(self.transport.placed))

    # ORD-003/008/022: only ack is SUBMITTED; rejection/timeout map safely.
    def test_broker_response_mapping(self):
        self.transport.place_result = BrokerDefinitiveError("IBKR_ORDER_REJECTED", "rejected", broker_code=201)
        result = self.engine.execute(open_request("tv-reject"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual(201, result.body["brokerErrorCode"])

    # SIG-003 / NFR-REL-001: broker-boundary replay returns the durable result.
    def test_duplicate_request_does_not_place_second_order(self):
        request = open_request("tv-replay")
        first = self.engine.execute(request)
        second = self.engine.execute(request)
        self.assertEqual("SUBMITTED", first.status)
        self.assertEqual(first.body, second.body)
        self.assertEqual(1, len(self.transport.placed))

    def test_distinct_duplicate_exposure_is_reserved_at_local_broker_boundary(self):
        first = self.engine.execute(open_request("tv-local-reserve-1"))
        second = self.engine.execute(open_request("tv-local-reserve-2"))
        self.assertEqual("SUBMITTED", first.status)
        self.assertEqual("BLOCKED", second.status)
        self.assertEqual("LOCAL_EXPOSURE_UNRESOLVED", second.body["code"])
        self.assertEqual(1, len(self.transport.placed))
        # A still-fresh blocking entry is never auto-cancelled.
        self.assertEqual([], self.transport.cancelled)

    # ---- stale blocking entry auto-cancel (on-demand only, at the exact
    # moment a new entry would otherwise be blocked by it -- never a
    # background sweep; see ExecutionEngine._cancel_stale_blocking_entry) ----

    def test_stale_blocking_entry_is_auto_cancelled_and_the_new_entry_proceeds(self):
        first = self.engine.execute(open_request("tv-stale-1"))
        self.assertEqual("SUBMITTED", first.status)
        blocking = self.registry.blocking_open_entries("DU12345", "QQQ", "C")
        self.assertEqual(1, len(blocking))
        order_ref = blocking[0]["order_ref"]
        stale_order_id = 9001
        self.transport.order_rows = [WorkingOrder(
            account="DU12345", contract=option(), action="BUY", remaining=Decimal("1"),
            order_id=stale_order_id, client_id=71, perm_id=91001, order_ref=order_ref, raw_status="Submitted",
        )]
        # created_at is stamped from the registry's own real wall clock, not
        # the engine's injectable test clock -- pin it to the fictional NOW
        # directly so "stale relative to `later`" is well-defined here.
        self.registry.connection.execute(
            "UPDATE broker_submissions SET created_at=? WHERE correlation_id=?",
            (NOW.isoformat(), "tradingview:tv-stale-1"),
        )
        later = NOW + timedelta(minutes=6)  # past the 5-minute default max_signal_age.
        self.engine.clock = lambda: later
        self.transport.underlying_quote = Quote(Decimal("100.20"), Decimal("100.30"), later, "LIVE")
        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("1.03"), later, "LIVE")
        second_request = open_request("tv-stale-2")
        second_request["signal"]["sent_at"] = later.isoformat()
        self.transport.place_result = BrokerAcknowledgement(701, 71, 901, "PreSubmitted")
        result = self.engine.execute(second_request)
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual([stale_order_id], self.transport.cancelled)
        self.assertEqual(2, len(self.transport.placed))
        # The cancelled entry no longer reserves the symbol/right at all --
        # only the brand-new one does.
        remaining = self.registry.blocking_open_entries("DU12345", "QQQ", "C")
        self.assertEqual(1, len(remaining))
        self.assertNotEqual("tradingview:tv-stale-1", remaining[0]["correlation_id"])

    def test_stale_blocking_entry_not_resting_at_the_broker_still_blocks(self):
        first = self.engine.execute(open_request("tv-gone-1"))
        self.assertEqual("SUBMITTED", first.status)
        # transport.order_rows stays empty: the stale order is no longer
        # resting at the broker (already filled or independently cancelled),
        # so nothing safe to cancel -- must not fabricate an outcome.
        later = NOW + timedelta(minutes=6)
        self.engine.clock = lambda: later
        self.transport.underlying_quote = Quote(Decimal("100.20"), Decimal("100.30"), later, "LIVE")
        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("1.03"), later, "LIVE")
        second_request = open_request("tv-gone-2")
        second_request["signal"]["sent_at"] = later.isoformat()
        result = self.engine.execute(second_request)
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("LOCAL_EXPOSURE_UNRESOLVED", result.body["code"])
        self.assertEqual([], self.transport.cancelled)
        self.assertEqual(1, len(self.transport.placed))

    def test_stale_blocking_entry_cancel_ambiguous_blocks_every_new_order_globally(self):
        first = self.engine.execute(open_request("tv-ambig-1"))
        self.assertEqual("SUBMITTED", first.status)
        blocking = self.registry.blocking_open_entries("DU12345", "QQQ", "C")
        order_ref = blocking[0]["order_ref"]
        stale_order_id = 9002
        self.transport.order_rows = [WorkingOrder(
            account="DU12345", contract=option(), action="BUY", remaining=Decimal("1"),
            order_id=stale_order_id, client_id=71, perm_id=91002, order_ref=order_ref, raw_status="Submitted",
        )]
        self.registry.connection.execute(
            "UPDATE broker_submissions SET created_at=? WHERE correlation_id=?",
            (NOW.isoformat(), "tradingview:tv-ambig-1"),
        )
        self.transport.cancel_results[stale_order_id] = BrokerAmbiguousError("IBKR_CANCEL_ACK_TIMEOUT", "timeout")
        later = NOW + timedelta(minutes=6)
        self.engine.clock = lambda: later
        self.transport.underlying_quote = Quote(Decimal("100.20"), Decimal("100.30"), later, "LIVE")
        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("1.03"), later, "LIVE")
        second_request = open_request("tv-ambig-2")
        second_request["signal"]["sent_at"] = later.isoformat()
        result = self.engine.execute(second_request)
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("STALE_ENTRY_CANCEL_UNKNOWN", result.body["code"])
        self.assertEqual([stale_order_id], self.transport.cancelled)
        self.assertTrue(self.registry.has_unresolved_unknown())
        # A brand-new, wholly unrelated symbol/right is now globally blocked
        # too -- an ambiguous entry cancel can mean an unknown new position
        # now exists, so (unlike a close/flatten protection-leg cancel) it is
        # never scoped to just this one contract.
        third_request = open_request("tv-ambig-3", action="OPEN_LONG_PUT")
        third_request["signal"]["sent_at"] = later.isoformat()
        third = self.engine.execute(third_request)
        self.assertEqual("BLOCKED", third.status)
        self.assertEqual("UNRESOLVED_SUBMISSION", third.body["code"])

    def test_restart_sweep_resolves_a_stuck_entry_cancel_intent(self):
        first = self.engine.execute(open_request("tv-restart-cancel"))
        self.assertEqual("SUBMITTED", first.status)
        registry_key = "tradingview:tv-restart-cancel"
        self.registry.record_entry_cancel_intent(registry_key)
        # Simulate a crash: 'CANCELLING' evidence is durable, but no broker
        # outcome was ever recorded.
        self.registry.close()
        restarted = SubmissionRegistry(self.config.state_db_path)
        # Fail closed both ways, identical to an initial-placement
        # SUBMISSION_UNKNOWN: still reserves the symbol/right, and now
        # globally blocks every new order until reconciled.
        self.assertTrue(restarted.has_blocking_open("DU12345", "QQQ", "C"))
        self.assertTrue(restarted.has_unresolved_unknown())
        self.registry = restarted  # tearDown closes whichever registry is current.

    def test_concurrent_distinct_entries_serialize_to_one_broker_call(self):
        barrier = threading.Barrier(3)
        results = []

        def submit(alert):
            barrier.wait()
            results.append(self.engine.execute(open_request(alert)))

        threads = [
            threading.Thread(target=submit, args=("tv-concurrent-1",)),
            threading.Thread(target=submit, args=("tv-concurrent-2",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(["BLOCKED", "SUBMITTED"], sorted(result.status for result in results))
        self.assertEqual(1, len(self.transport.placed))

    def test_unknown_submission_blocks_all_new_orders(self):
        self.transport.place_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")
        unknown = self.engine.execute(open_request("tv-unknown-first"))
        self.assertEqual("SUBMISSION_UNKNOWN", unknown.status)
        self.transport.place_result = BrokerAcknowledgement(701, 71, 901, "PreSubmitted")
        blocked = self.engine.execute(open_request("tv-after-unknown", action="OPEN_LONG_PUT"))
        self.assertEqual("BLOCKED", blocked.status)
        self.assertEqual("UNRESOLVED_SUBMISSION", blocked.body["code"])
        self.assertEqual(1, len(self.transport.placed))

    # Phase 2: reconciliation can resolve a SUBMISSION_UNKNOWN row after the
    # fact. CONFIRMED_NO_FILL must stop it from blocking new orders globally
    # (_verify_readiness -> has_unresolved_unknown) AND stop it from reserving
    # the symbol/right (_block_duplicate_exposure -> has_blocking_open) --
    # nothing ever filled, so a genuinely new entry must be allowed through
    # end to end.
    def test_reconciled_confirmed_no_fill_unblocks_both_global_and_symbol_reservation(self):
        self.transport.place_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")
        unknown = self.engine.execute(open_request("tv-reconcile-no-fill"))
        self.assertEqual("SUBMISSION_UNKNOWN", unknown.status)
        # broker_submissions.correlation_id is the durable *registry* key
        # (idempotencyKey, e.g. "tradingview:<alertId>") -- not the UUID
        # correlationId field carried in the response body.
        registry_key = "tradingview:tv-reconcile-no-fill"
        self.assertTrue(self.registry.has_unresolved_unknown())
        self.assertTrue(self.registry.has_blocking_open("DU12345", "QQQ", "C"))

        ledger = ExecutionLedger(self.registry.connection, self.registry.lock)
        self.assertTrue(ledger.mark_reconciliation_outcome(registry_key, "CONFIRMED_NO_FILL"))
        self.assertFalse(self.registry.has_unresolved_unknown())
        self.assertFalse(self.registry.has_blocking_open("DU12345", "QQQ", "C"))

        self.transport.place_result = BrokerAcknowledgement(702, 71, 902, "PreSubmitted")
        result = self.engine.execute(open_request("tv-reconcile-no-fill-retry"))
        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(2, len(self.transport.placed))

    # CONFIRMED_FILLED is the opposite outcome: it must unblock *global*
    # readiness (the ambiguity itself is resolved) but keep reserving the
    # exact symbol/right, since the position is confirmed to actually exist.
    def test_reconciled_confirmed_filled_unblocks_global_but_keeps_symbol_reserved(self):
        self.transport.place_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")
        unknown = self.engine.execute(open_request("tv-reconcile-filled"))
        self.assertEqual("SUBMISSION_UNKNOWN", unknown.status)
        registry_key = "tradingview:tv-reconcile-filled"

        ledger = ExecutionLedger(self.registry.connection, self.registry.lock)
        self.assertTrue(ledger.mark_reconciliation_outcome(registry_key, "CONFIRMED_FILLED"))
        self.assertFalse(self.registry.has_unresolved_unknown())
        self.assertTrue(self.registry.has_blocking_open("DU12345", "QQQ", "C"))

        self.transport.place_result = BrokerAcknowledgement(703, 71, 903, "PreSubmitted")
        result = self.engine.execute(open_request("tv-reconcile-filled-retry"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("LOCAL_EXPOSURE_UNRESOLVED", result.body["code"])
        # Still only the one original broker call -- the retry never reached
        # placeOrder because _block_duplicate_exposure caught it first.
        self.assertEqual(1, len(self.transport.placed))

    # A still-unresolved SUBMISSION_UNKNOWN (reconciliation_outcome IS NULL)
    # must keep blocking exactly as before -- this pins the "no regression"
    # baseline the two tests above are relative to.
    def test_unreconciled_unknown_still_blocks_after_ledger_is_constructed(self):
        self.transport.place_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")
        unknown = self.engine.execute(open_request("tv-unresolved-still-blocks"))
        self.assertEqual("SUBMISSION_UNKNOWN", unknown.status)
        # Constructing the ledger (as reconciliation code would) must not by
        # itself resolve anything.
        ExecutionLedger(self.registry.connection, self.registry.lock)
        self.assertTrue(self.registry.has_unresolved_unknown())
        self.transport.place_result = BrokerAcknowledgement(704, 71, 904, "PreSubmitted")
        blocked = self.engine.execute(open_request("tv-unresolved-still-blocks-retry", action="OPEN_LONG_PUT"))
        self.assertEqual("BLOCKED", blocked.status)
        self.assertEqual("UNRESOLVED_SUBMISSION", blocked.body["code"])

    def test_signal_age_is_rechecked_immediately_before_submission(self):
        request = open_request("tv-expired-at-core")
        request["signal"]["sent_at"] = (NOW - timedelta(minutes=6)).isoformat()
        result = self.engine.execute(request)
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("SIGNAL_EXPIRED", result.body["code"])
        self.assertEqual([], self.transport.placed)

    def test_signal_can_expire_during_preparation_before_broker_call(self):
        request = open_request("tv-expires-during-preparation")
        request["signal"]["sent_at"] = (NOW - timedelta(minutes=4, seconds=59)).isoformat()
        times = iter((NOW, NOW, NOW, NOW + timedelta(seconds=2)))
        engine = ExecutionEngine(
            config=self.config,
            transport=self.transport,
            registry=self.registry,
            clock=lambda: next(times),
        )
        result = engine.execute(request)
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("SIGNAL_EXPIRED", result.body["code"])
        self.assertEqual([], self.transport.placed)

    def test_crash_recovery_retains_final_broker_call_evidence(self):
        path = Path(self.temp.name) / "crash-evidence.sqlite3"
        registry = SubmissionRegistry(path)
        registry.claim("tradingview:crash-evidence", 42, "payload-hash")
        registry.record_broker_call_evidence(
            "tradingview:crash-evidence",
            account="DU12345",
            action="OPEN_LONG_CALL",
            contract={"con_id": 201, "symbol": "QQQ", "right": "C"},
            side="BUY",
            quantity=1,
            limit_price="1.05",
            order_ref="QTcrashevidence",
            entry_correlation_id=None,
        )
        registry.close()

        recovered = SubmissionRegistry(path)
        try:
            evidence = recovered.submission_evidence("tradingview:crash-evidence")
            self.assertEqual("SUBMISSION_UNKNOWN", evidence["status"])
            self.assertEqual("DU12345", evidence["account"])
            self.assertEqual("OPEN_LONG_CALL", evidence["action"])
            self.assertEqual(201, evidence["contract"]["con_id"])
            self.assertEqual("BUY", evidence["side"])
            self.assertEqual("1", evidence["quantity"])
            self.assertEqual("1.05", evidence["limit_price"])
            self.assertEqual("QTcrashevidence", evidence["order_ref"])
        finally:
            recovered.close()

    def test_live_account_without_confirmation_is_rejected_even_on_paper_port(self):
        live_config = replace(
            self.config,
            selected_account="U1234567",
            paper_account_allowlist=frozenset({"U1234567"}),
        )
        with self.assertRaisesRegex(ValueError, "live trading is not confirmed"):
            live_config.validate()

    def test_malformed_account_identifier_is_rejected(self):
        malformed_config = replace(self.config, selected_account="not-an-account")
        with self.assertRaisesRegex(ValueError, "paper .* or live .* account identifier"):
            malformed_config.validate()

    def test_live_account_confirmed_but_wrong_port_is_rejected(self):
        live_config = replace(
            self.config,
            selected_account="U1234567",
            ibkr_port=7497,  # paper port, not a live port
            live_account_allowlist=frozenset({"U1234567"}),
            live_trading_confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "live ports 7496 and 4001"):
            live_config.validate()

    def test_live_account_confirmed_and_correct_port_but_not_allowlisted_is_rejected(self):
        live_config = replace(
            self.config,
            selected_account="U1234567",
            ibkr_port=7496,
            live_account_allowlist=frozenset({"U9999999"}),
            live_trading_confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "exact live allowlist"):
            live_config.validate()

    def test_live_account_fully_configured_validates(self):
        live_config = replace(
            self.config,
            selected_account="U1234567",
            ibkr_port=7496,
            live_account_allowlist=frozenset({"U1234567"}),
            live_trading_confirmed=True,
        )
        live_config.validate()  # must not raise

    def test_paper_account_still_validates_unaffected_by_live_fields(self):
        self.config.validate()  # must not raise; existing paper setUp fixture

    # Phase 1: max_contracts_per_order is now a relaxable deployment ceiling
    # (previously hard-locked to exactly 1) -- any positive integer validates.
    def test_max_contracts_per_order_accepts_a_relaxed_ceiling_above_one(self):
        replace(self.config, max_contracts_per_order=25).validate()  # must not raise

    def test_max_contracts_per_order_rejects_non_positive_or_non_integer_values(self):
        for bad in (0, -1, 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "positive integer safety ceiling"):
                    replace(self.config, max_contracts_per_order=bad).validate()

    def test_registry_database_is_owner_only(self):
        mode = self.config.state_db_path.stat().st_mode & 0o777
        self.assertEqual(0o600, mode)

    def test_second_core_cannot_own_the_same_profile_database(self):
        with self.assertRaisesRegex(RuntimeError, "already owns"):
            SubmissionRegistry(self.config.state_db_path)

    # CON-001 / typed private contract: Node health and place paths remain aligned.
    def test_private_http_contract_matches_node_adapter(self):
        token = self.config.service_token
        status, health = http_exchange(
            self.config,
            self.engine,
            f"GET /healthz HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n\r\n".encode(),
        )
        self.assertEqual(200, status)
        self.assertEqual(
            {
                "environment": "PAPER",
                "ready": True,
                "accountMask": "••••2345",
                "ibkrHost": "127.0.0.1",
                "ibkrPort": 7497,
                "strikeSelection": {"metric": "PREMIUM", "lo": "1.00", "hi": "2.50", "candidateCount": 5},
            },
            health,
        )

        request = open_request("tv-http")
        payload = json.dumps(request).encode("utf-8")
        status, placed = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(200, status)
        self.assertEqual("SUBMITTED", placed["status"])
        self.assertEqual(request["correlationId"], placed["correlationId"])

    def test_manual_app_managed_preview_then_submit_persists_immutable_metadata(self):
        request = manual_request("manual-managed")
        preview = self.engine.preview(request)
        self.assertEqual("PREVIEW_READY", preview.status)
        self.assertTrue(preview.body["previewOnly"])
        self.assertEqual("APP_MANAGED", preview.body["managementMode"])
        self.assertEqual("PENDING_EXECUTION_LEDGER", preview.body["managementStatus"])
        self.assertEqual([], self.transport.placed)
        self.assertIsNone(self.registry.submission_evidence("manual:manual-managed"))

        submitted = self.engine.execute(request)
        self.assertEqual("SUBMITTED", submitted.status)
        self.assertEqual(1, len(self.transport.placed))
        evidence = self.registry.submission_evidence("manual:manual-managed")
        self.assertEqual("MANUAL_UI", evidence["source"])
        self.assertEqual("APP_OWNED", evidence["ownership"])
        self.assertEqual("APP_MANAGED", evidence["management_mode"])
        self.assertEqual("50", evidence["management_policy"]["takeProfitLevels"][0]["allocationPercent"])
        self.assertEqual("paper-balanced-v1", evidence["management_policy"]["policyId"])
        self.assertEqual("standard-bracket-v1", request["signal"]["exit_policy_id"])

    def test_manual_exact_listed_contract_preview_uses_requested_expiry_and_strike(self):
        request = manual_request("manual-exact-preview")
        request["signal"]["strike_policy"] = {
            "type": "EXACT_LISTED",
            "expiry": "20260720",
            "strike": 101,
        }
        preview = self.engine.preview(request)
        self.assertEqual("PREVIEW_READY", preview.status)
        self.assertEqual(201, preview.body["conId"])
        self.assertEqual([], self.transport.placed)

    def test_http_preview_never_places_or_claims(self):
        request = manual_request("manual-http-preview", mode="ENTRY_ONLY")
        payload = json.dumps(request).encode("utf-8")
        status, preview = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/preview-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(200, status)
        self.assertEqual("PREVIEW_READY", preview["status"])
        self.assertEqual([], self.transport.placed)
        self.assertIsNone(self.registry.submission_evidence("manual:manual-http-preview"))

    def test_changed_management_mode_conflicts_without_second_order(self):
        request = manual_request("manual-immutable", mode="ENTRY_ONLY")
        first = self.engine.execute(request)
        changed = manual_request("manual-immutable", mode="APP_MANAGED")
        second = self.engine.execute(changed)
        self.assertEqual("SUBMITTED", first.status)
        self.assertEqual("BLOCKED", second.status)
        self.assertEqual("CORRELATION_CONFLICT", second.body["code"])
        self.assertEqual(1, len(self.transport.placed))

    def test_management_allocations_must_total_exactly_one_hundred(self):
        request = manual_request("manual-invalid-allocation")
        request["managementPolicy"] = management_policy(allocations=(50, 25, 20))
        payload = json.dumps(request).encode("utf-8")
        status, result = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(400, status)
        self.assertEqual("MANAGEMENT_ALLOCATION_INVALID", result["code"])
        self.assertEqual([], self.transport.placed)
        self.assertIsNone(self.registry.submission_evidence("manual:manual-invalid-allocation"))

    def test_management_policy_accepts_transitions_and_stop_coverage_percent(self):
        request = manual_request("manual-transitions-ok")
        request["managementPolicy"] = management_policy(
            transitions=[
                {"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN"},
                {"after": "TP2", "action": "TRAIL_FRESH_BID", "distancePercent": 15},
            ],
            stop_coverage_percent=100,
        )
        result = self.engine.execute(request)
        self.assertEqual("SUBMITTED", result.status)
        evidence = self.registry.submission_evidence("manual:manual-transitions-ok")
        self.assertEqual("100", evidence["management_policy"]["stopCoveragePercent"])
        self.assertEqual(
            [
                {"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN"},
                {"after": "TP2", "action": "TRAIL_FRESH_BID", "distancePercent": "15"},
            ],
            evidence["management_policy"]["transitions"],
        )

    def test_management_transition_after_must_reference_existing_level(self):
        # parse_management_contract runs inside _parse_request, ahead of the
        # execute()/preview() try/except -- an invalid management policy
        # therefore surfaces as an HTTP 400, not an ExecutionResult, matching
        # test_management_allocations_must_total_exactly_one_hundred above.
        request = manual_request("manual-transitions-bad-ref")
        request["managementPolicy"] = management_policy(
            transitions=[{"after": "TP9", "action": "MOVE_STOP_TO_BREAKEVEN"}],
        )
        payload = json.dumps(request).encode("utf-8")
        status, result = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(400, status)
        self.assertEqual("MANAGEMENT_POLICY_INVALID", result["code"])
        self.assertEqual([], self.transport.placed)
        self.assertIsNone(self.registry.submission_evidence("manual:manual-transitions-bad-ref"))

    def test_management_trail_fresh_bid_requires_distance_percent(self):
        request = manual_request("manual-transitions-no-distance")
        request["managementPolicy"] = management_policy(
            transitions=[{"after": "TP1", "action": "TRAIL_FRESH_BID"}],
        )
        payload = json.dumps(request).encode("utf-8")
        status, result = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(400, status)
        self.assertEqual("MANAGEMENT_POLICY_INVALID", result["code"])
        self.assertEqual([], self.transport.placed)
        self.assertIsNone(self.registry.submission_evidence("manual:manual-transitions-no-distance"))

    def test_management_move_stop_to_breakeven_rejects_distance_percent(self):
        request = manual_request("manual-transitions-extra-field")
        request["managementPolicy"] = management_policy(
            transitions=[{"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN", "distancePercent": 15}],
        )
        payload = json.dumps(request).encode("utf-8")
        status, result = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(400, status)
        self.assertEqual("MANAGEMENT_POLICY_INVALID", result["code"])
        self.assertEqual([], self.transport.placed)
        self.assertIsNone(self.registry.submission_evidence("manual:manual-transitions-extra-field"))

    def test_entry_only_rejects_exit_management_policy(self):
        request = manual_request("manual-entry-only-policy", mode="ENTRY_ONLY")
        request["managementPolicy"] = management_policy()
        payload = json.dumps(request).encode("utf-8")
        status, result = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(400, status)
        self.assertEqual("MANAGEMENT_POLICY_NOT_APPLICABLE", result["code"])
        self.assertEqual([], self.transport.placed)

    def test_non_app_management_modes_cannot_cross_broker_boundary(self):
        for index, mode in enumerate(("USER_MANAGED", "TRADINGVIEW_MANAGED")):
            with self.subTest(mode=mode):
                request = manual_request(f"manual-quarantined-{index}", mode="ENTRY_ONLY")
                request["managementMode"] = mode
                payload = json.dumps(request).encode("utf-8")
                status, result = http_exchange(
                    self.config,
                    self.engine,
                    (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
                     f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
                )
                self.assertEqual(400, status)
                self.assertEqual("INVALID_MANAGEMENT_MODE", result["code"])
        self.assertEqual([], self.transport.placed)

    def test_legacy_registry_rows_migrate_to_entry_only(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE broker_submissions (
              correlation_id TEXT PRIMARY KEY,
              node_intent_id INTEGER NOT NULL,
              payload_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              result_json TEXT,
              account TEXT,
              action TEXT,
              contract_json TEXT,
              entry_correlation_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO broker_submissions
              (correlation_id,node_intent_id,payload_hash,status,created_at,updated_at)
            VALUES ('tradingview:legacy',1,'hash','SUBMITTED','now','now');
            """
        )
        db.close()
        migrated = SubmissionRegistry(path)
        try:
            evidence = migrated.submission_evidence("tradingview:legacy")
            self.assertEqual("TRADINGVIEW", evidence["source"])
            self.assertEqual("APP_OWNED", evidence["ownership"])
            self.assertEqual("ENTRY_ONLY", evidence["management_mode"])
            self.assertIsNone(evidence["management_policy"])
        finally:
            migrated.close()

    def test_http_unknown_is_200_payload_that_node_maps_ambiguous(self):
        self.transport.place_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")
        payload = json.dumps(open_request("tv-http-unknown")).encode("utf-8")
        status, result = http_exchange(
            self.config,
            self.engine,
            (f"POST /private/v1/place-trade HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )
        self.assertEqual(200, status)
        self.assertEqual("SUBMISSION_UNKNOWN", result["status"])

    def test_ibapi_quote_callback_completes_when_bid_and_ask_arrive(self):
        transport = OfficialIbapiTransport.__new__(OfficialIbapiTransport)
        transport._lock = threading.RLock()
        pending = _Pending(values=[{
            "bid": None,
            "ask": None,
            "market_data_type": "LIVE",
            "received_at": NOW,
        }])
        transport._requests = {10: pending}
        transport.clock = lambda: NOW
        transport.tickPrice(10, 1, 1.00, None)
        self.assertFalse(pending.event.is_set())
        transport.tickPrice(10, 2, 1.05, None)
        self.assertTrue(pending.event.is_set())

    def test_ibapi_current_error_signature_preserves_order_rejection(self):
        transport = OfficialIbapiTransport.__new__(OfficialIbapiTransport)
        transport._lock = threading.RLock()
        transport._requests = {}
        ack = _OrderAck()
        transport._order_acks = {700: ack}
        transport._cancel_acks = {}
        transport._reconciled = True
        transport._blocking_reason = None
        transport.error(700, 1_753_102_800, 201, "Order rejected", "{}")
        self.assertEqual((201, "Order rejected"), ack.error)
        self.assertTrue(ack.event.is_set())

    def _ready_ibapi_transport(self, config, managed_accounts):
        transport = OfficialIbapiTransport.__new__(OfficialIbapiTransport)
        transport.config = config
        transport._managed_accounts = managed_accounts
        transport.isConnected = lambda: True
        transport._handshake = threading.Event()
        transport._handshake.set()
        transport._server_time = threading.Event()
        transport._server_time.set()
        transport._reconciled = True
        transport._blocking_reason = None
        return transport

    # HIGH finding from adversarial review: the real (non-fake) transport's
    # PAPER/LIVE/UNKNOWN derivation had no direct test — only FakeTransport's
    # hand-set string was ever exercised. These test the actual formula in
    # OfficialIbapiTransport.readiness().
    def test_ibapi_readiness_reports_paper_when_port_account_and_managed_accounts_agree(self):
        transport = self._ready_ibapi_transport(self.config, ("DU12345",))
        self.assertEqual("PAPER", transport.readiness().environment)

    def test_ibapi_readiness_reports_live_when_port_account_and_managed_accounts_agree(self):
        live_config = replace(
            self.config,
            selected_account="U1234567",
            ibkr_port=7496,
            live_account_allowlist=frozenset({"U1234567"}),
            live_trading_confirmed=True,
        )
        transport = self._ready_ibapi_transport(live_config, ("U1234567",))
        self.assertEqual("LIVE", transport.readiness().environment)

    def test_ibapi_readiness_is_unknown_when_paper_account_is_on_a_live_port(self):
        mismatched_config = replace(self.config, ibkr_port=7496)
        transport = self._ready_ibapi_transport(mismatched_config, ("DU12345",))
        self.assertEqual("UNKNOWN", transport.readiness().environment)

    def test_ibapi_readiness_is_unknown_when_configured_account_is_not_managed_by_ibkr(self):
        transport = self._ready_ibapi_transport(self.config, ("DU99999",))
        self.assertEqual("UNKNOWN", transport.readiness().environment)


if __name__ == "__main__":
    unittest.main()


class QuoteRevalidationTests(unittest.TestCase):
    """H3: the entry quote must be re-checked between selection and placeOrder.

    Nothing re-validated it, so the single quote read near the start of the
    submission chain silently determined the strike, the size, and every
    take-profit and stop price.
    """

    setUp = ExecutionTests.setUp
    tearDown = ExecutionTests.tearDown

    def test_entry_blocks_when_the_quote_moves_materially_before_submission(self):
        moving = _MovingQuoteTransport(self.transport)
        engine = ExecutionEngine(
            config=self.config, transport=moving, registry=self.registry, clock=lambda: NOW
        )
        result = engine.execute(open_request("tv-quote-moved"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("ENTRY_QUOTE_MOVED", result.body["code"])
        self.assertEqual([], moving.placed, "no order may be sent once the quote is known to have moved")

    def test_a_stable_quote_still_submits(self):
        result = self.engine.execute(open_request("tv-quote-stable"))
        self.assertEqual("SUBMITTED", result.status)

    def test_a_non_live_market_data_type_is_never_priced_on(self):
        self.transport.option_quote = Quote(Decimal("1.26"), Decimal("1.32"), NOW, "UNKNOWN")
        result = self.engine.execute(open_request("tv-quote-unknown"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("MARKET_DATA_NOT_LIVE", result.body["code"])
        self.assertEqual([], self.transport.placed)


class _MovingQuoteTransport:
    """Serves a normal quote first, then a materially different one.

    This is exactly the shape of the 18:20 entry in the reviewed session: a
    limit of 0.23 implying an observed ask of at least 0.18, filled at 0.11.
    """

    def __init__(self, inner):
        self._inner = inner
        self._calls = 0
        self.placed = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def quote(self, contract):
        # Only the option quote moves; the underlying spot read is left alone so
        # this exercises the re-validation rather than strike selection.
        if contract.sec_type != "OPT":
            return self._inner.quote(contract)
        self._calls += 1
        if self._calls == 1:
            return Quote(Decimal("2.40"), Decimal("2.50"), NOW, "LIVE")
        return Quote(Decimal("1.05"), Decimal("1.15"), NOW, "LIVE")

    def place_limit_order(self, order):
        self.placed.append(order)
        return self._inner.place_limit_order(order)
