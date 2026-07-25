"""Phase 5: REDUCE_ONLY_PARTIAL and FULL_FLATTEN close actions.

Two distinct, separately-invokable operator actions, not one implicit
"close" with hidden behavior:

- REDUCE_ONLY_PARTIAL: a bounded partial-or-full close, bound by fresh
  verified broker long quantity minus every working sell reservation
  (this app's own still-working protection legs plus any foreign/manual
  working sell). Never touches existing protection orders.
- FULL_FLATTEN: cancels every working protection leg first (with durable
  cancel-intent evidence before each cancel_order call, and a hard fail-
  closed halt the moment any cancel outcome is ambiguous), then re-queries
  fresh verified long quantity and submits exactly one reduce-only SELL for
  everything that remains. Warns but does not hard-block on a wide spread,
  unlike every other order in this codebase -- an explicit, documented
  product decision for this one emergency-exit path.

Also covers the un-gating of _prepare_close behind real execution-ledger
fill evidence, the contract-scoped-vs-global ambiguity-blocking asymmetry
(has_blocking_close / has_unresolved_cancel_unknown vs. the pre-existing
global has_unresolved_unknown checks), and idempotent cancel-then-flatten.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from quickytrade_core.config import CoreConfig
from quickytrade_core.domain import (
    BrokerAcknowledgement,
    BrokerAmbiguousError,
    CancelAcknowledgement,
    OptionChain,
    Position,
    PriceIncrement,
    QualifiedContract,
    Quote,
    Readiness,
    WorkingOrder,
)
from quickytrade_core.engine import ExecutionEngine, _contract_to_json
from quickytrade_core.execution_ledger import ExecutionLedger, ExecutionRecord
from quickytrade_core.http_service import _Handler
from quickytrade_core.protection import ProtectionLedger
from quickytrade_core.registry import SubmissionRegistry

NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


def option() -> QualifiedContract:
    return QualifiedContract(
        con_id=201, symbol="QQQ", sec_type="OPT", exchange="SMART", currency="USD",
        local_symbol="QQQ OPT C 100", expiry="20260918", strike=Decimal("100"), right="C",
        multiplier="100", trading_class="QQQ", valid_exchanges=("SMART",),
        market_rule_ids=(26,), min_tick=Decimal("0.05"),
    )


def underlying() -> QualifiedContract:
    return QualifiedContract(
        con_id=100, symbol="QQQ", sec_type="STK", exchange="SMART",
        primary_exchange="NASDAQ", currency="USD", local_symbol="QQQ",
    )


def management_policy(allocations=(50, 50), stop_loss_percent="25"):
    triggers = (20, 40, 60, 80, 100, 120, 140, 160)
    levels = [
        {"levelId": f"TP{index + 1}", "triggerPercent": str(triggers[index]), "allocationPercent": str(allocation)}
        for index, allocation in enumerate(allocations)
    ]
    return {
        "policyId": "paper-balanced-v1",
        "version": 1,
        "takeProfitLevels": levels,
        "stopLossPercent": stop_loss_percent,
    }


def open_request(alert="tv-open", *, action="OPEN_LONG_PUT"):
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
            "sent_at": "2026-07-21T14:00:00Z",
            "strategy_id": "tv-options",
            "strategy_version": "1",
            "action": action,
            "ticker": "QQQ",
            "target_dte": 59,  # matches the FakeTransport chain's sole 20260918 expiry from NOW (2026-07-21).
            "strike_policy": {"type": "ATM_OFFSET", "offset": 1},
        },
    }


def close_request(entry_alert_id, alert, *, action="CLOSE_LONG_CALL_REDUCE_ONLY_PARTIAL", quantity=None):
    signal = {
        "schema_version": "1",
        "alert_id": alert,
        "sent_at": "2026-07-21T14:00:00Z",
        "strategy_id": "tv-options",
        "strategy_version": "1",
        "action": action,
        "ticker": "QQQ",
        "entry_alert_id": entry_alert_id,
    }
    if not action.endswith("_FULL_FLATTEN"):
        signal["quantity"] = quantity if quantity is not None else 1
    return {
        "broker": "IBKR",
        "idempotencyKey": f"tradingview:{alert}",
        "intentId": int.from_bytes(alert.encode("utf-8"), "little") % 1_000_000 + 1,
        "correlationId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"quickytrade:{alert}")),
        "source": "tradingview",
        "alertId": alert,
        "signal": signal,
    }


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


class FakeTransport:
    """Extends the Phase 3/4 fakes with a fresh, directly test-controlled
    broker position (position_rows), a foreign/manual working-order list
    distinct from this app's own tracked orders (order_rows vs. _working),
    and cancel_order support for FULL_FLATTEN."""

    def __init__(self):
        self.managed_accounts = ("DU12345",)
        self.environment = "PAPER"
        self.reconciled = True
        self.underlying = underlying()
        self.chain = OptionChain(
            exchange="SMART", underlying_con_id=100, trading_class="QQQ", multiplier="100",
            expirations=("20260918",), strikes=(Decimal("98"), Decimal("99"), Decimal("100"), Decimal("101")),
        )
        self.underlying_quote = Quote(Decimal("100.20"), Decimal("100.30"), NOW, "LIVE")
        self.option_quote = Quote(Decimal("1.00"), Decimal("1.03"), NOW, "LIVE")
        self.rules = (PriceIncrement(Decimal("0"), Decimal("0.05")),)
        self.position_rows: list[Position] = []
        self.order_rows: list[WorkingOrder] = []  # foreign/manual working orders.
        self.placed: list = []  # DAY tif entry/close SELL orders.
        self.placed_limit: list = []
        self.placed_stop: list = []
        self.place_result: object = BrokerAcknowledgement(700, 71, 900, "PreSubmitted")
        self.cancelled: list[int] = []
        self.cancel_results: dict[int, object] = {}  # order_id -> Exception instance, else confirmed success.
        self.on_cancel = None  # optional callback(order_id) invoked at the moment cancel_order() is called.
        self._next_order_id = 800
        self._working: dict[int, WorkingOrder] = {}  # this app's own placed (protection) orders.

    def start(self): pass
    def stop(self): pass

    def readiness(self):
        return Readiness(True, True, True, self.reconciled, self.managed_accounts, self.environment, False)

    def qualify_underlying(self, symbol): return self.underlying
    def option_chains(self, underlying_contract): return (self.chain,)

    def qualify_option(self, *, underlying, expiry, strike, right, exchange, trading_class, multiplier):
        return QualifiedContract(
            con_id=301 if right == "C" else 302, symbol="QQQ", sec_type="OPT", exchange="SMART",
            currency="USD", local_symbol=f"QQQ OPT {right} {strike}", expiry=expiry, strike=strike,
            right=right, multiplier="100", trading_class="QQQ", valid_exchanges=("SMART",),
            market_rule_ids=(26,), min_tick=Decimal("0.05"),
        )

    def quote(self, contract):
        return self.underlying_quote if contract.sec_type == "STK" else self.option_quote

    def market_rule(self, contract):
        return self.rules

    def positions(self, account):
        return tuple(self.position_rows)

    def working_orders(self, account):
        return tuple(self.order_rows) + tuple(self._working.values())

    def place_limit_order(self, request):
        order_id = self._allocate()
        if request.tif == "GTC":
            self.placed_limit.append(request)
        else:
            self.placed.append(request)
            if isinstance(self.place_result, Exception):
                raise self.place_result
            return self.place_result
        self._working[order_id] = WorkingOrder(
            account=request.account, contract=request.contract, action=request.action,
            remaining=Decimal(request.quantity), order_id=order_id, client_id=71,
            perm_id=order_id + 10_000, order_ref=request.order_ref, raw_status="Submitted",
        )
        return BrokerAcknowledgement(order_id, 71, order_id + 10_000, "PreSubmitted", oca_group=request.oca_group)

    def place_stop_limit_order(self, request):
        order_id = self._allocate()
        self.placed_stop.append(request)
        self._working[order_id] = WorkingOrder(
            account=request.account, contract=request.contract, action=request.action,
            remaining=Decimal(request.quantity), order_id=order_id, client_id=71,
            perm_id=order_id + 10_000, order_ref=request.order_ref, raw_status="Submitted",
        )
        return BrokerAcknowledgement(order_id, 71, order_id + 10_000, "PreSubmitted", oca_group=request.oca_group)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        if self.on_cancel is not None:
            self.on_cancel(order_id)
        result = self.cancel_results.get(order_id)
        if isinstance(result, Exception):
            raise result
        self._working.pop(order_id, None)
        return CancelAcknowledgement(order_id=order_id, raw_status="Cancelled")

    def _allocate(self) -> int:
        order_id = self._next_order_id
        self._next_order_id += 1
        return order_id


class ReduceOnlyCloseTests(unittest.TestCase):
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
        self.registry = SubmissionRegistry(self.config.state_db_path)
        self.ledger = ExecutionLedger(self.registry.connection, self.registry.lock)
        self.protection_ledger = ProtectionLedger(self.registry.connection, self.registry.lock)
        self.transport = FakeTransport()
        self.engine = ExecutionEngine(
            config=self.config,
            transport=self.transport,
            registry=self.registry,
            ledger=self.ledger,
            protection_ledger=self.protection_ledger,
            clock=lambda: NOW,
        )

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    # ---- fixtures -----------------------------------------------------

    def _seed_filled_position(
        self, correlation_id: str, *, quantity: int, fill_price: str = "1.00", policy: dict | None = None
    ) -> QualifiedContract:
        contract = option()
        order_ref = "QT" + hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()[:30]
        self.registry.claim(
            correlation_id, 1, "hash-" + correlation_id,
            source="MANUAL_UI",
            management_mode="APP_MANAGED" if policy else "ENTRY_ONLY",
            management_policy=policy,
        )
        self.registry.record_broker_call_evidence(
            correlation_id, account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
            side="BUY", quantity=quantity, limit_price=fill_price, order_ref=order_ref, entry_correlation_id=None,
        )
        self.registry.finish(
            correlation_id, status="SUBMITTED", result={"status": "SUBMITTED"},
            account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
        )
        self.ledger.record_execution(ExecutionRecord(
            exec_id=f"exec-{correlation_id}-entry", order_ref=order_ref, order_id=700, perm_id=900,
            account="DU12345", con_id=contract.con_id, symbol=contract.symbol, side="BOT",
            shares=str(quantity), price=fill_price, cum_qty=str(quantity), avg_price=fill_price,
            exec_time="20260721  10:00:00", source="LIVE_CALLBACK", raw={},
        ))
        return contract

    def _protected(self, correlation_id: str, *, quantity: int, allocations=(50, 50), broker_quantity=None):
        """Seed a FILLED, APP_MANAGED entry, place its full protection
        bracket via the real ensure_protection() path (so every leg is a
        genuine tracked FakeTransport order with a real broker_order_id),
        and set the broker's reported live position (defaults to the same
        quantity actually filled/protected)."""
        contract = self._seed_filled_position(
            correlation_id, quantity=quantity, policy=management_policy(allocations=allocations)
        )
        self.engine.ensure_protection(correlation_id)
        self.transport.position_rows = [
            Position("DU12345", contract, Decimal(str(broker_quantity if broker_quantity is not None else quantity)))
        ]
        return contract

    def _leg(self, correlation_id: str, key: str) -> dict:
        row = self.protection_ledger.get(f"{correlation_id}:{key}")
        self.assertIsNotNone(row, f"expected a protection row for {correlation_id}:{key}")
        return row

    # ---- REDUCE_ONLY_PARTIAL: bound accounting -------------------------

    def test_reduce_only_partial_excess_blocked_by_protection_reservation(self):
        correlation_id = "tradingview:rop-excess"
        self._protected(correlation_id, quantity=4)  # protection reserves all 4.

        result = self.engine.execute(
            close_request("rop-excess", "rop-excess-close", quantity=1)
        )
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("REDUCE_ONLY_BOUND_EXCEEDED", result.body["code"])
        self.assertEqual([], self.transport.placed)
        # Never touched: every leg is still exactly as it was.
        for key in ("TP:TP1", "TP:TP2", "STOP:TP1", "STOP:TP2"):
            self.assertEqual("SUBMITTED", self._leg(correlation_id, key)["status"])
        self.assertEqual([], self.transport.cancelled)

    def test_reduce_only_partial_succeeds_within_bound_accounting_for_protection_and_foreign_orders(self):
        correlation_id = "tradingview:rop-room"
        # Protection covers 2 of the entry's own tracked quantity, but the
        # broker additionally reports 5 live contracts (e.g. drift versus
        # this entry's own tracked fill) and a foreign/manual working sell
        # reserves 1 more -- available = 5 - 2 (protection) - 1 (foreign) = 2.
        contract = self._protected(correlation_id, quantity=2, broker_quantity=5)
        self.transport.order_rows = [
            WorkingOrder(
                account="DU12345", contract=contract, action="SELL", remaining=Decimal("1"),
                order_id=999, client_id=99, perm_id=999, order_ref="foreign-manual-sell", raw_status="Submitted",
            )
        ]

        blocked = self.engine.execute(close_request("rop-room", "rop-room-close-excess", quantity=3))
        self.assertEqual("BLOCKED", blocked.status)
        self.assertEqual("REDUCE_ONLY_BOUND_EXCEEDED", blocked.body["code"])
        self.assertEqual([], self.transport.placed)

        allowed = self.engine.execute(close_request("rop-room", "rop-room-close-ok", quantity=2))
        self.assertEqual("SUBMITTED", allowed.status)
        self.assertEqual(1, len(self.transport.placed))
        self.assertEqual(2, self.transport.placed[0].quantity)
        self.assertEqual("SELL", self.transport.placed[0].action)
        # Never touches existing protection: still present/working after the close.
        for key in ("TP:TP1", "TP:TP2", "STOP:TP1", "STOP:TP2"):
            self.assertEqual("SUBMITTED", self._leg(correlation_id, key)["status"])
        self.assertEqual([], self.transport.cancelled)

    def test_reduce_only_partial_uses_the_same_tick_valid_fresh_quote_sell_construction(self):
        correlation_id = "tradingview:rop-pricing"
        self._protected(correlation_id, quantity=2, broker_quantity=4)  # available = 2.

        result = self.engine.execute(close_request("rop-pricing", "rop-pricing-close", quantity=2))
        self.assertEqual("SUBMITTED", result.status)
        # bid=1.00/ask=1.03, 0.05 increment -> SELL marketable limit is bid
        # rounded down to the nearest tick: 1.00.
        self.assertEqual(Decimal("1.00"), self.transport.placed[0].limit_price)
        self.assertEqual("1.00", result.body["limitPrice"])
        self.assertEqual("LMT", result.body["orderType"])

    def test_reduce_only_partial_hard_blocks_on_wide_spread(self):
        correlation_id = "tradingview:rop-spread"
        self._protected(correlation_id, quantity=2, broker_quantity=4)
        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("2.00"), NOW, "LIVE")

        result = self.engine.execute(close_request("rop-spread", "rop-spread-close", quantity=2))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("OPTION_SPREAD_LIMIT_EXCEEDED", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # ---- FULL_FLATTEN: cancel-all-legs-first, then one full SELL -------

    def test_full_flatten_cancels_every_leg_first_with_durable_cancel_intent_evidence(self):
        correlation_id = "tradingview:flatten-happy"
        self._protected(correlation_id, quantity=4)

        seen_cancel_status_at_call_time: list[str | None] = []

        def on_cancel(order_id):
            for leg in self.protection_ledger.legs_for_correlation(correlation_id):
                if leg["broker_order_id"] and int(leg["broker_order_id"]) == order_id:
                    seen_cancel_status_at_call_time.append(leg["cancel_status"])

        self.transport.on_cancel = on_cancel

        result = self.engine.execute(
            close_request("flatten-happy", "flatten-happy-close", action="CLOSE_LONG_CALL_FULL_FLATTEN")
        )

        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(4, len(self.transport.cancelled))
        # Cancel-intent evidence ('CANCELLING') was durably committed before
        # every single cancel_order() broker call fired.
        self.assertEqual(["CANCELLING"] * 4, seen_cancel_status_at_call_time)
        for key in ("TP:TP1", "TP:TP2", "STOP:TP1", "STOP:TP2"):
            row = self._leg(correlation_id, key)
            self.assertEqual("CANCELLED", row["status"])
            self.assertEqual("CANCEL_CONFIRMED", row["cancel_status"])
        self.assertEqual(1, len(self.transport.placed))
        self.assertEqual(4, self.transport.placed[0].quantity)
        self.assertEqual("SELL", self.transport.placed[0].action)
        self.assertEqual("CLOSE_LONG_CALL_FULL_FLATTEN", result.body["closeAction"])
        self.assertNotIn("spreadWarning", result.body)

    def test_full_flatten_halts_before_the_flattening_sell_if_any_cancel_is_ambiguous(self):
        correlation_id = "tradingview:flatten-ambiguous"
        self._protected(correlation_id, quantity=4)
        # legs_for_correlation() is ordered by protection_id; "STOP:..." sorts
        # before "TP:..." alphabetically, so STOP1/STOP2/TP1 cancel first and
        # TP2 (the last one processed) is made ambiguous -- proving partial
        # progress is preserved and the SELL is never reached.
        tp2_order_id = int(self._leg(correlation_id, "TP:TP2")["broker_order_id"])
        self.transport.cancel_results[tp2_order_id] = BrokerAmbiguousError("IBKR_CANCEL_ACK_TIMEOUT", "timeout")

        result = self.engine.execute(
            close_request("flatten-ambiguous", "flatten-ambiguous-close", action="CLOSE_LONG_CALL_FULL_FLATTEN")
        )

        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("PROTECTION_CANCEL_UNKNOWN", result.body["code"])
        self.assertEqual([], self.transport.placed)  # zero SELL ever placed.
        for key in ("STOP:TP1", "STOP:TP2", "TP:TP1"):
            row = self._leg(correlation_id, key)
            self.assertEqual("CANCELLED", row["status"])
            self.assertEqual("CANCEL_CONFIRMED", row["cancel_status"])
        tp2_row = self._leg(correlation_id, "TP:TP2")
        # Left exactly as-is (broker truth is unknown either way) -- never
        # claimed cancelled, never silently assumed still-working either.
        self.assertEqual("SUBMITTED", tp2_row["status"])
        self.assertEqual("CANCEL_UNKNOWN", tp2_row["cancel_status"])

    def test_full_flatten_once_all_cancels_confirmed_requeries_fresh_quantity_and_submits_one_sell(self):
        correlation_id = "tradingview:flatten-fresh-qty"
        contract = self._protected(correlation_id, quantity=4)
        # Simulate the live broker quantity having drifted since the entry's
        # own tracked fill (e.g. reconciliation catching up) -- the flatten
        # must use this freshly-read number, never a stale/cached one.
        self.transport.position_rows = [Position("DU12345", contract, Decimal("6"))]

        result = self.engine.execute(
            close_request("flatten-fresh-qty", "flatten-fresh-qty-close", action="CLOSE_LONG_CALL_FULL_FLATTEN")
        )

        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(1, len(self.transport.placed))
        self.assertEqual(6, self.transport.placed[0].quantity)
        self.assertEqual(6, result.body["quantity"])

    def test_full_flatten_warns_but_allows_on_wide_spread(self):
        correlation_id = "tradingview:flatten-spread-warn"
        self._protected(correlation_id, quantity=4)
        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("2.00"), NOW, "LIVE")

        result = self.engine.execute(
            close_request("flatten-spread-warn", "flatten-spread-warn-close", action="CLOSE_LONG_CALL_FULL_FLATTEN")
        )

        self.assertEqual("SUBMITTED", result.status)
        self.assertEqual(1, len(self.transport.placed))
        self.assertIn("spreadWarning", result.body)
        self.assertEqual("OPTION_SPREAD_LIMIT_EXCEEDED_WARNING_ONLY", result.body["spreadWarning"]["code"])

    def test_idempotent_cancel_then_flatten_does_not_recancel_already_cancelled_legs(self):
        correlation_id = "tradingview:flatten-idempotent"
        contract = self._protected(correlation_id, quantity=4)

        first = self.engine.execute(
            close_request("flatten-idempotent", "flatten-idempotent-close-1", action="CLOSE_LONG_CALL_FULL_FLATTEN")
        )
        self.assertEqual("SUBMITTED", first.status)
        self.assertEqual(4, len(self.transport.cancelled))

        # The broker still shows one live contract (e.g. a slow-to-settle
        # sell), but it is now fully reserved by a foreign/manual working
        # sell -- available == 0, a distinct block from "no position at
        # all" (VERIFIED_LONG_POSITION_UNAVAILABLE), specifically exercising
        # the "nothing left to flatten" path.
        self.transport.position_rows = [Position("DU12345", contract, Decimal("1"))]
        self.transport.order_rows = [
            WorkingOrder(
                account="DU12345", contract=contract, action="SELL", remaining=Decimal("1"),
                order_id=777, client_id=99, perm_id=777, order_ref="foreign-remaining-sell", raw_status="Submitted",
            )
        ]

        second = self.engine.execute(
            close_request("flatten-idempotent", "flatten-idempotent-close-2", action="CLOSE_LONG_CALL_FULL_FLATTEN")
        )
        # No remaining quantity to flatten -- but critically, no leg (already
        # CANCELLED from the first attempt) was ever re-cancelled.
        self.assertEqual("BLOCKED", second.status)
        self.assertEqual("FLATTEN_NO_REMAINING_QUANTITY", second.body["code"])
        self.assertEqual(4, len(self.transport.cancelled))  # unchanged.
        self.assertEqual(1, len(self.transport.placed))  # only the first flatten's SELL.

    # ---- un-gating: missing/stale evidence blocks ----------------------

    def test_missing_verified_long_quantity_blocks_and_never_defaults_to_zero(self):
        correlation_id = "tradingview:rop-no-position"
        self._seed_filled_position(correlation_id, quantity=2, policy=management_policy())
        self.transport.position_rows = []  # broker reports nothing for this contract.

        result = self.engine.execute(close_request("rop-no-position", "rop-no-position-close", quantity=1))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("VERIFIED_LONG_POSITION_UNAVAILABLE", result.body["code"])
        self.assertEqual([], self.transport.placed)

    def test_unknown_working_exit_quantity_blocks_rather_than_assuming_zero(self):
        correlation_id = "tradingview:rop-unknown-exit"
        contract = self._seed_filled_position(correlation_id, quantity=2, policy=None)
        self.transport.position_rows = [Position("DU12345", contract, Decimal("2"))]
        self.transport.order_rows = [
            WorkingOrder(
                account="DU12345", contract=contract, action="SELL", remaining=None,
                order_id=555, client_id=99, perm_id=555, order_ref="foreign-unknown-qty", raw_status="Submitted",
            )
        ]

        result = self.engine.execute(close_request("rop-unknown-exit", "rop-unknown-exit-close", quantity=1))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("WORKING_EXIT_QUANTITY_UNKNOWN", result.body["code"])
        self.assertEqual([], self.transport.placed)

    def test_close_without_fill_evidence_blocks(self):
        # SUBMITTED at the registry level but no broker_executions evidence
        # yet -- position_state has no row at all.
        correlation_id = "tradingview:rop-no-evidence"
        contract = option()
        self.registry.claim(correlation_id, 1, "hash-no-evidence", source="MANUAL_UI", management_mode="ENTRY_ONLY")
        self.registry.record_broker_call_evidence(
            correlation_id, account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
            side="BUY", quantity=1, limit_price="1.00", order_ref="QTnoevidence", entry_correlation_id=None,
        )
        self.registry.finish(
            correlation_id, status="SUBMITTED", result={"status": "SUBMITTED"},
            account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
        )
        self.transport.position_rows = [Position("DU12345", contract, Decimal("1"))]

        result = self.engine.execute(close_request("rop-no-evidence", "rop-no-evidence-close", quantity=1))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("POSITION_FILL_EVIDENCE_UNAVAILABLE", result.body["code"])
        self.assertEqual([], self.transport.placed)

    # ---- idempotency / concurrency guard: has_blocking_close -----------

    def test_second_close_on_same_contract_is_blocked_while_the_first_is_unresolved(self):
        correlation_id = "tradingview:rop-double-click"
        contract = self._protected(correlation_id, quantity=2, broker_quantity=4)
        self.transport.place_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")

        first = self.engine.execute(close_request("rop-double-click", "rop-double-click-close-1", quantity=1))
        self.assertEqual("SUBMISSION_UNKNOWN", first.status)
        self.assertTrue(self.registry.has_blocking_close("DU12345", contract.con_id))
        # The ambiguous IBKR ack still happens *after* place_limit_order is
        # called (the ambiguity is in the acknowledgement, not the call
        # itself) -- exactly one broker call so far.
        placed_after_first_attempt = len(self.transport.placed)
        self.assertEqual(1, placed_after_first_attempt)

        self.transport.place_result = BrokerAcknowledgement(900, 71, 950, "PreSubmitted")
        second = self.engine.execute(close_request("rop-double-click", "rop-double-click-close-2", quantity=1))
        self.assertEqual("BLOCKED", second.status)
        self.assertEqual("UNRESOLVED_CLOSE_SUBMISSION", second.body["code"])
        # No second SELL broker call was made for this contract as a result
        # of the retry -- the count is exactly where the first attempt left it.
        self.assertEqual(placed_after_first_attempt, len(self.transport.placed))

    # ---- contract-scoped vs. global ambiguity blocking ------------------

    def test_unresolved_close_sell_ambiguity_blocks_only_that_contract_not_unrelated_opens(self):
        correlation_id = "tradingview:rop-scoped-sell"
        self._protected(correlation_id, quantity=2, broker_quantity=4)
        self.transport.place_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")

        ambiguous = self.engine.execute(close_request("rop-scoped-sell", "rop-scoped-sell-close", quantity=1))
        self.assertEqual("SUBMISSION_UNKNOWN", ambiguous.status)

        # The existing GLOBAL block (registry.has_unresolved_unknown, used by
        # _verify_readiness for every new open) must be unaffected -- a
        # close's own ambiguity is contract-scoped only.
        self.assertFalse(self.registry.has_unresolved_unknown())

        # A brand-new, unrelated open (different right on the same
        # underlying, so it is not itself a duplicate-exposure conflict)
        # still succeeds.
        self.transport.place_result = BrokerAcknowledgement(901, 71, 951, "PreSubmitted")
        unrelated_open = self.engine.execute(open_request("rop-scoped-sell-unrelated-open", action="OPEN_LONG_PUT"))
        self.assertEqual("SUBMITTED", unrelated_open.status)

    def test_unresolved_protection_cancel_ambiguity_blocks_only_that_contract_not_unrelated_opens(self):
        correlation_id = "tradingview:flatten-scoped-cancel"
        contract = self._protected(correlation_id, quantity=4)
        tp2_order_id = int(self._leg(correlation_id, "TP:TP2")["broker_order_id"])
        self.transport.cancel_results[tp2_order_id] = BrokerAmbiguousError("IBKR_CANCEL_ACK_TIMEOUT", "timeout")

        flatten = self.engine.execute(
            close_request("flatten-scoped-cancel", "flatten-scoped-cancel-close", action="CLOSE_LONG_CALL_FULL_FLATTEN")
        )
        self.assertEqual("BLOCKED", flatten.status)
        self.assertEqual("PROTECTION_CANCEL_UNKNOWN", flatten.body["code"])

        self.assertTrue(
            self.protection_ledger.has_unresolved_cancel_unknown(account="DU12345", con_id=contract.con_id)
        )
        # Deliberate asymmetry: neither of the pre-existing GLOBAL blocks
        # (open-submission ambiguity, protection *placement*/*modify*
        # ambiguity) are tripped by a cancel ambiguity.
        self.assertFalse(self.registry.has_unresolved_unknown())
        self.assertFalse(self.protection_ledger.has_unresolved_unknown())

        # A brand-new, unrelated open still succeeds.
        unrelated_open = self.engine.execute(
            open_request("flatten-scoped-cancel-unrelated-open", action="OPEN_LONG_PUT")
        )
        self.assertEqual("SUBMITTED", unrelated_open.status)

        # But ANY further close/flatten action on the *same* contract is
        # blocked -- including a REDUCE_ONLY_PARTIAL, not just a repeat of
        # the exact same FULL_FLATTEN mode.
        another = self.engine.execute(
            close_request("flatten-scoped-cancel", "flatten-scoped-cancel-close-retry", quantity=1)
        )
        self.assertEqual("BLOCKED", another.status)
        self.assertEqual("UNRESOLVED_PROTECTION_CANCEL", another.body["code"])

    # ---- restart recovery: a stuck CANCELLING row becomes CANCEL_UNKNOWN --

    def test_restart_sweep_resolves_a_stuck_cancelling_protection_row(self):
        correlation_id = "tradingview:flatten-restart"
        self._protected(correlation_id, quantity=2, allocations=(100,))
        protection_id = self._leg(correlation_id, "TP:TP1")["protection_id"]
        self.protection_ledger.record_cancel_intent(protection_id)
        self.assertEqual("CANCELLING", self.protection_ledger.get(protection_id)["cancel_status"])

        restarted = ProtectionLedger(self.registry.connection, self.registry.lock)
        row = restarted.get(protection_id)
        self.assertEqual("CANCEL_UNKNOWN", row["cancel_status"])
        # Still SUBMITTED -- broker truth about whether it actually cancelled
        # is unknown either way, never claimed resolved either direction.
        self.assertEqual("SUBMITTED", row["status"])
        contract = option()
        self.assertTrue(
            restarted.has_unresolved_cancel_unknown(account="DU12345", con_id=contract.con_id)
        )
        # And, per the deliberate asymmetry, this still does not feed the
        # global has_unresolved_unknown() check used by _verify_readiness.
        self.assertFalse(restarted.has_unresolved_unknown())

    # ---- /private/v1/close-trade HTTP endpoint --------------------------

    def _http_post(self, path: str, request: dict):
        payload = json.dumps(request).encode("utf-8")
        return http_exchange(
            self.config,
            self.engine,
            (f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
             f"Authorization: Bearer {self.config.service_token}\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload,
        )

    def test_close_trade_endpoint_submits_a_reduce_only_partial(self):
        correlation_id = "tradingview:http-close-rop"
        self._protected(correlation_id, quantity=2, broker_quantity=4)
        status, body = self._http_post(
            "/private/v1/close-trade", close_request("http-close-rop", "http-close-rop-close", quantity=2)
        )
        self.assertEqual(200, status)
        self.assertEqual("SUBMITTED", body["status"])
        self.assertEqual(2, body["quantity"])
        self.assertEqual("SELL", body["action"])

    def test_close_trade_endpoint_submits_a_full_flatten(self):
        correlation_id = "tradingview:http-close-flatten"
        self._protected(correlation_id, quantity=4)
        status, body = self._http_post(
            "/private/v1/close-trade",
            close_request("http-close-flatten", "http-close-flatten-close", action="CLOSE_LONG_CALL_FULL_FLATTEN"),
        )
        self.assertEqual(200, status)
        self.assertEqual("SUBMITTED", body["status"])
        self.assertEqual(4, body["quantity"])
        self.assertEqual(4, len(self.transport.cancelled))

    def test_close_trade_endpoint_rejects_an_open_action(self):
        status, body = self._http_post("/private/v1/close-trade", open_request("http-close-wrong-action"))
        self.assertEqual(400, status)
        self.assertEqual("CLOSE_TRADE_REQUIRES_CLOSE_ACTION", body["code"])
        self.assertEqual([], self.transport.placed)

    def test_place_trade_endpoint_rejects_a_close_action(self):
        correlation_id = "tradingview:http-place-wrong-action"
        self._protected(correlation_id, quantity=2, broker_quantity=4)
        status, body = self._http_post(
            "/private/v1/place-trade", close_request("http-place-wrong-action", "http-place-wrong-action-close")
        )
        self.assertEqual(400, status)
        self.assertEqual("PLACE_TRADE_REQUIRES_OPEN_ACTION", body["code"])
        self.assertEqual([], self.transport.placed)


if __name__ == "__main__":
    unittest.main()
