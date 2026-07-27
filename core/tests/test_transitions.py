"""Phase 4: reacting to a confirmed take-profit fill by applying the entry's
management-policy transitions[] (MOVE_STOP_TO_BREAKEVEN / TRAIL_FRESH_BID) to
the position's other still-working stop-loss legs.

Covers: broker-execution-evidence-gated fill confirmation (never a raw
order-status string alone), in-place modify (never cancel+replace) of only
the *other* stop leg(s), ratchet-only trailing, stale-quote blocking,
ambiguous-modify global blocking (end to end), restart recovery, idempotency,
the SKIPPED_ZERO_ALLOCATION terminal policy, and transition_id PK uniqueness.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quickytrade_core.config import CoreConfig
from quickytrade_core.domain import (
    BrokerAcknowledgement,
    BrokerAmbiguousError,
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
from quickytrade_core.protection import ProtectionLedger
from quickytrade_core.registry import SubmissionRegistry
from quickytrade_core.transitions import TransitionLedger, transition_id

NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


def underlying() -> QualifiedContract:
    return QualifiedContract(
        con_id=100, symbol="QQQ", sec_type="STK", exchange="SMART",
        primary_exchange="NASDAQ", currency="USD", local_symbol="QQQ",
    )


def option() -> QualifiedContract:
    return QualifiedContract(
        con_id=201, symbol="QQQ", sec_type="OPT", exchange="SMART", currency="USD",
        local_symbol="QQQ OPT C 100", expiry="20260918", strike=Decimal("100"), right="C",
        multiplier="100", trading_class="QQQ", valid_exchanges=("SMART",),
        market_rule_ids=(26,), min_tick=Decimal("0.05"),
    )


def management_policy(
    allocations=(50, 50), stop_loss_percent="25", transitions=None
):
    triggers = (20, 40, 60, 80, 100, 120, 140, 160)
    levels = [
        {"levelId": f"TP{index + 1}", "triggerPercent": str(triggers[index]), "allocationPercent": str(allocation)}
        for index, allocation in enumerate(allocations)
    ]
    policy = {
        "policyId": "paper-transitions-v1",
        "version": 1,
        "takeProfitLevels": levels,
        "stopLossPercent": stop_loss_percent,
    }
    if transitions is not None:
        policy["transitions"] = transitions
    return policy


def open_request(alert="tv-transitions-open"):
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
            "action": "OPEN_LONG_PUT",
            "ticker": "QQQ",
            "target_dte": 0,
            "strike_policy": {"type": "ATM_OFFSET", "offset": 1},
        },
    }


class FakeTransport:
    """Extends the Phase 3 fake with a distinct broker order id per placed
    order (needed since eligibility/modify tests must tell legs apart), a
    working-orders map a test can mutate to simulate a fill/OCA-cancellation,
    and modify_stop_limit_order support."""

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
        self.order_rows: list[WorkingOrder] = []  # unused by these tests; kept for interface parity.
        self.placed: list = []
        self.placed_limit: list = []
        self.placed_stop: list = []
        self.modified_stop: list = []  # (order_id, request) pairs.
        self.place_result: object = BrokerAcknowledgement(700, 71, 900, "PreSubmitted")
        self.modify_result: object = None  # None -> synthesize a fresh ack; else raised/returned as-is.
        self._next_order_id = 800
        self._working = {}  # order_id -> WorkingOrder

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

    def positions(self, account): return tuple(self.position_rows)

    def working_orders(self, account):
        return tuple(self._working.values())

    def drop_working_order(self, order_id: int) -> None:
        """Simulate IBKR removing a terminal (filled or OCA-cancelled) order
        from the open-orders set."""
        self._working.pop(order_id, None)

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

    def modify_stop_limit_order(self, order_id, request):
        self.modified_stop.append((order_id, request))
        if isinstance(self.modify_result, Exception):
            raise self.modify_result
        if self.modify_result is not None:
            return self.modify_result
        return BrokerAcknowledgement(order_id, 71, order_id + 10_000, "PreSubmitted", oca_group=request.oca_group)

    def _allocate(self) -> int:
        order_id = self._next_order_id
        self._next_order_id += 1
        return order_id


class TransitionsTests(unittest.TestCase):
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
        self.transition_ledger = TransitionLedger(self.registry.connection, self.registry.lock)
        self.transport = FakeTransport()
        self.engine = ExecutionEngine(
            config=self.config,
            transport=self.transport,
            registry=self.registry,
            ledger=self.ledger,
            protection_ledger=self.protection_ledger,
            transition_ledger=self.transition_ledger,
            clock=lambda: NOW,
        )

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    # ---- fixtures -----------------------------------------------------

    def _seed_filled_position(
        self, correlation_id: str, *, quantity: int, fill_price: str, policy: dict
    ) -> QualifiedContract:
        contract = option()
        order_ref = "QT" + hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()[:30]
        self.registry.claim(
            correlation_id, 1, "hash-" + correlation_id,
            source="MANUAL_UI", management_mode="APP_MANAGED", management_policy=policy,
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

    def _tp_id(self, correlation_id, level_id):
        return f"{correlation_id}:TP:{level_id}"

    def _stop_id(self, correlation_id, level_id):
        return f"{correlation_id}:STOP:{level_id}"

    def _fill_tp_leg(self, correlation_id: str, contract: QualifiedContract, level_id: str, *, quantity: int) -> None:
        """Records broker_executions evidence for a TP leg's order_ref (a
        genuine SLD fill) and drops its own paired stop from working_orders
        (IBKR's own OCA auto-cancellation of the sibling)."""
        tp_row = self.protection_ledger.get(self._tp_id(correlation_id, level_id))
        stop_row = self.protection_ledger.get(self._stop_id(correlation_id, level_id))
        self.ledger.record_execution(ExecutionRecord(
            exec_id=f"exec-{correlation_id}-{level_id}-tp", order_ref=tp_row["order_ref"],
            order_id=int(tp_row["broker_order_id"]), perm_id=900, account="DU12345",
            con_id=contract.con_id, symbol=contract.symbol, side="SLD", shares=str(quantity),
            price=tp_row["limit_price"], cum_qty=str(quantity), avg_price=tp_row["limit_price"],
            exec_time="20260721  11:00:00", source="LIVE_CALLBACK", raw={},
        ))
        self.transport.drop_working_order(int(tp_row["broker_order_id"]))
        self.transport.drop_working_order(int(stop_row["broker_order_id"]))

    # ---- TP1 fill -> MOVE_STOP_TO_BREAKEVEN on the other leg(s) only ------

    def test_tp1_confirmed_fill_moves_only_the_other_stop_leg_to_breakeven(self):
        correlation_id = "manual:transitions-breakeven"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            transitions=[{"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN"}],
        )
        contract = self._seed_filled_position(correlation_id, quantity=4, fill_price="1.03", policy=policy)
        self.engine.ensure_protection(correlation_id)

        stop1_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        stop2_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))
        self.assertEqual("SUBMITTED", stop1_before["status"])
        self.assertEqual("SUBMITTED", stop2_before["status"])

        self._fill_tp_leg(correlation_id, contract, "TP1", quantity=2)

        self.engine.ensure_transitions(correlation_id)

        tid = transition_id(correlation_id, "TP1")
        row = self.transition_ledger.get(tid)
        self.assertEqual("APPLIED", row["status"])

        # Only STOP2 (the *other* level's leg) was modified.
        self.assertEqual(1, len(self.transport.modified_stop))
        modified_order_id, modified_request = self.transport.modified_stop[0]
        self.assertEqual(int(stop2_before["broker_order_id"]), modified_order_id)
        self.assertEqual(stop2_before["order_ref"], modified_request.order_ref)

        # entry_fill_price=1.03, tick 0.05, rounded UP -> 1.05.
        self.assertEqual(Decimal("1.05"), modified_request.trigger_price)
        self.assertLessEqual(modified_request.limit_price, modified_request.trigger_price)

        stop2_after = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))
        self.assertEqual(Decimal("1.05"), Decimal(stop2_after["trigger_price"]))
        self.assertIsNone(stop2_after["modify_status"])

        # STOP1 (TP1's own now-cancelled paired stop) was never touched.
        stop1_after = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        self.assertEqual(stop1_before["trigger_price"], stop1_after["trigger_price"])
        self.assertEqual(stop1_before["limit_price"], stop1_after["limit_price"])

    # ---- raw "Filled"-looking absence with no execution evidence ----------

    def test_order_disappearing_from_working_orders_without_execution_evidence_does_not_trigger(self):
        correlation_id = "manual:transitions-no-evidence"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            transitions=[{"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN"}],
        )
        self._seed_filled_position(correlation_id, quantity=4, fill_price="1.00", policy=policy)
        self.engine.ensure_protection(correlation_id)

        tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1"))
        stop1 = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        stop2_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))
        # Simulate the order-status callback reporting the TP1 leg as
        # terminal/gone (as a real "Filled" status would) -- but WITHOUT any
        # corresponding broker_executions row. This must not be sufficient.
        self.transport.drop_working_order(int(tp1["broker_order_id"]))
        self.transport.drop_working_order(int(stop1["broker_order_id"]))
        self.assertEqual([], self.ledger.executions_for_order_ref(tp1["order_ref"]))

        self.engine.ensure_transitions(correlation_id)

        self.assertIsNone(self.transition_ledger.get(transition_id(correlation_id, "TP1")))
        self.assertEqual([], self.transport.modified_stop)
        stop2_after = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))
        self.assertEqual(stop2_before["trigger_price"], stop2_after["trigger_price"])

    # ---- TP2 fill -> TRAIL_FRESH_BID: fresh-quote-derived, tick-valid -----

    def test_tp2_confirmed_fill_trails_the_other_stop_leg_from_a_fresh_quote(self):
        correlation_id = "manual:transitions-trail"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            transitions=[{"after": "TP2", "action": "TRAIL_FRESH_BID", "distancePercent": "15"}],
        )
        contract = self._seed_filled_position(correlation_id, quantity=4, fill_price="1.00", policy=policy)
        self.engine.ensure_protection(correlation_id)
        stop1_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        self.assertEqual(Decimal("0.75"), Decimal(stop1_before["trigger_price"]))  # 1.00 * 0.75, tick floor.

        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("1.05"), NOW, "LIVE")
        self._fill_tp_leg(correlation_id, contract, "TP2", quantity=2)

        self.engine.ensure_transitions(correlation_id)

        tid = transition_id(correlation_id, "TP2")
        self.assertEqual("APPLIED", self.transition_ledger.get(tid)["status"])
        self.assertEqual(1, len(self.transport.modified_stop))
        _order_id, request = self.transport.modified_stop[0]
        self.assertEqual(stop1_before["order_ref"], request.order_ref)
        # fresh bid 1.00 * (1 - 15/100) = 0.85 -> already tick-valid at 0.05.
        self.assertEqual(Decimal("0.85"), request.trigger_price)
        self.assertEqual(Decimal("0"), request.trigger_price % Decimal("0.05"))
        self.assertLessEqual(request.limit_price, request.trigger_price)
        self.assertEqual(Decimal("0"), request.limit_price % Decimal("0.05"))

        stop1_after = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        self.assertEqual(Decimal("0.85"), Decimal(stop1_after["trigger_price"]))

    # ---- stale/missing quote blocks the trail transition -------------------

    def test_stale_quote_blocks_trail_transition_leaving_existing_stop_untouched_then_retries(self):
        correlation_id = "manual:transitions-stale-quote"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            transitions=[{"after": "TP2", "action": "TRAIL_FRESH_BID", "distancePercent": "15"}],
        )
        contract = self._seed_filled_position(correlation_id, quantity=4, fill_price="1.00", policy=policy)
        self.engine.ensure_protection(correlation_id)
        stop1_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))

        # Stale quote (older than config.max_quote_age) at transition time.
        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("1.05"), NOW - timedelta(minutes=5), "LIVE")
        self._fill_tp_leg(correlation_id, contract, "TP2", quantity=2)

        self.engine.ensure_transitions(correlation_id)

        tid = transition_id(correlation_id, "TP2")
        row = self.transition_ledger.get(tid)
        self.assertEqual("PENDING", row["status"])
        self.assertEqual([], self.transport.modified_stop)
        stop1_after = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        self.assertEqual(stop1_before["trigger_price"], stop1_after["trigger_price"])
        self.assertIsNone(stop1_after["modify_status"])

        # A fresh quote on the next sweep resolves it.
        self.transport.option_quote = Quote(Decimal("1.00"), Decimal("1.05"), NOW, "LIVE")
        self.engine.ensure_transitions(correlation_id)

        self.assertEqual("APPLIED", self.transition_ledger.get(tid)["status"])
        self.assertEqual(1, len(self.transport.modified_stop))

    # ---- ratchet-only: a worse recomputed trigger is a true no-op ---------

    def test_ratchet_only_worse_recomputed_trigger_makes_no_broker_call(self):
        correlation_id = "manual:transitions-ratchet"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            transitions=[{"after": "TP2", "action": "TRAIL_FRESH_BID", "distancePercent": "15"}],
        )
        contract = self._seed_filled_position(correlation_id, quantity=4, fill_price="1.00", policy=policy)
        self.engine.ensure_protection(correlation_id)
        stop1_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        self.assertEqual(Decimal("0.75"), Decimal(stop1_before["trigger_price"]))

        # fresh bid 0.80 * 0.85 = 0.68 -> tick-ceiling 0.70, which is WORSE
        # than the currently-resting 0.75 trigger.
        self.transport.option_quote = Quote(Decimal("0.80"), Decimal("0.83"), NOW, "LIVE")
        self._fill_tp_leg(correlation_id, contract, "TP2", quantity=2)

        self.engine.ensure_transitions(correlation_id)

        tid = transition_id(correlation_id, "TP2")
        self.assertEqual("APPLIED", self.transition_ledger.get(tid)["status"])
        self.assertEqual([], self.transport.modified_stop)  # no broker call at all.
        stop1_after = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        self.assertEqual(Decimal("0.75"), Decimal(stop1_after["trigger_price"]))

    # ---- ambiguous modify ack: PROTECTION_MODIFY_UNKNOWN-equivalent -------

    def test_ambiguous_modify_ack_blocks_a_brand_new_unrelated_entry_end_to_end(self):
        correlation_id = "manual:transitions-ambiguous"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            transitions=[{"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN"}],
        )
        contract = self._seed_filled_position(correlation_id, quantity=4, fill_price="1.03", policy=policy)
        self.engine.ensure_protection(correlation_id)
        stop2_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))

        self.transport.modify_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")
        self._fill_tp_leg(correlation_id, contract, "TP1", quantity=2)

        self.engine.ensure_transitions(correlation_id)

        tid = transition_id(correlation_id, "TP1")
        self.assertEqual("FAILED_UNKNOWN", self.transition_ledger.get(tid)["status"])
        stop2_after = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))
        self.assertEqual("MODIFY_UNKNOWN", stop2_after["modify_status"])
        # Last-confirmed trigger/limit are left untouched (broker truth about
        # whether the modify landed is unknown either way).
        self.assertEqual(stop2_before["trigger_price"], stop2_after["trigger_price"])
        self.assertTrue(self.protection_ledger.has_unresolved_unknown())

        before = len(self.transport.placed)
        result = self.engine.execute(open_request("tv-blocked-by-transition-modify"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("UNRESOLVED_PROTECTION_SUBMISSION", result.body["code"])
        self.assertEqual(before, len(self.transport.placed))

    # ---- restart sweep: a stuck APPLYING row becomes FAILED_UNKNOWN -------

    def test_restart_sweep_resolves_a_stuck_applying_transition_row(self):
        tid = "manual:transitions-restart:TP1"
        self.transition_ledger.ensure_pending(
            tid, correlation_id="manual:transitions-restart", after="TP1", action="MOVE_STOP_TO_BREAKEVEN"
        )
        self.assertTrue(self.transition_ledger.mark_applying(tid))
        self.assertEqual("APPLYING", self.transition_ledger.get(tid)["status"])

        restarted = TransitionLedger(self.registry.connection, self.registry.lock)
        self.assertEqual("FAILED_UNKNOWN", restarted.get(tid)["status"])

    # Regression test for the HIGH finding: a crash exactly between
    # transition_ledger.mark_applying(tid) committing and the stop-leg
    # modify's own evidence being recorded (i.e. before protection_ledger's
    # unresolved-tracking has anything to catch) must not silently strand the
    # position on its original, less-protective stop while new unrelated
    # opens keep proceeding. Nothing at the broker was touched in this
    # window -- no modify_stop_limit_order call was ever made -- so
    # protection_ledger.has_unresolved_unknown() alone (correctly) sees
    # nothing wrong; only the restart-swept FAILED_UNKNOWN transition row
    # itself carries evidence that something didn't finish.
    def test_restart_after_mark_applying_with_no_leg_touched_blocks_a_new_unrelated_open(self):
        tid = "manual:transitions-crash-window:TP1"
        self.transition_ledger.ensure_pending(
            tid, correlation_id="manual:transitions-crash-window", after="TP1", action="MOVE_STOP_TO_BREAKEVEN"
        )
        self.assertTrue(self.transition_ledger.mark_applying(tid))
        self.assertEqual("APPLYING", self.transition_ledger.get(tid)["status"])
        # Crash: nothing else about this transition was ever recorded -- no
        # broker modify call, no protection-ledger evidence at all.
        self.assertEqual([], self.transport.modified_stop)

        # "Process restart": a fresh TransitionLedger over the same durable
        # connection sweeps the stuck row, and a fresh ExecutionEngine is
        # constructed against it exactly as main() would build one on boot.
        restarted_transition_ledger = TransitionLedger(self.registry.connection, self.registry.lock)
        self.assertEqual("FAILED_UNKNOWN", restarted_transition_ledger.get(tid)["status"])
        self.assertTrue(restarted_transition_ledger.has_unresolved_unknown())
        self.assertFalse(self.protection_ledger.has_unresolved_unknown())  # nothing else was ever touched.

        restarted_engine = ExecutionEngine(
            config=self.config,
            transport=self.transport,
            registry=self.registry,
            ledger=self.ledger,
            protection_ledger=self.protection_ledger,
            transition_ledger=restarted_transition_ledger,
            clock=lambda: NOW,
        )

        before = len(self.transport.placed)
        result = restarted_engine.execute(open_request("tv-blocked-by-stuck-transition"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("UNRESOLVED_TRANSITION_FAILURE", result.body["code"])
        self.assertEqual(before, len(self.transport.placed))  # never reached the broker.

    # ---- idempotency: re-running after APPLIED does nothing ---------------

    def test_ensure_transitions_is_idempotent_after_applied(self):
        correlation_id = "manual:transitions-idempotent"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            transitions=[{"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN"}],
        )
        contract = self._seed_filled_position(correlation_id, quantity=4, fill_price="1.00", policy=policy)
        self.engine.ensure_protection(correlation_id)
        self._fill_tp_leg(correlation_id, contract, "TP1", quantity=2)

        self.engine.ensure_transitions(correlation_id)
        self.assertEqual(1, len(self.transport.modified_stop))
        first_row = self.transition_ledger.get(transition_id(correlation_id, "TP1"))
        self.assertEqual("APPLIED", first_row["status"])

        self.engine.ensure_transitions(correlation_id)

        self.assertEqual(1, len(self.transport.modified_stop))  # no duplicate modify call.
        second_row = self.transition_ledger.get(transition_id(correlation_id, "TP1"))
        self.assertEqual(first_row["applied_at"], second_row["applied_at"])

    # ---- SKIPPED_ZERO_ALLOCATION level: resolved APPLIED, permanently inert

    # H5. A transition whose level never received a real broker order resolves
    # to INERT, not APPLIED. "APPLIED" answered "did my trailing stop engage?"
    # with yes, for a stop that was never placed and never modified.
    def test_skipped_zero_allocation_level_transition_is_inert_never_reported_as_applied(self):
        correlation_id = "manual:transitions-skipped"
        policy = management_policy(
            allocations=(50, 50), stop_loss_percent="25",
            # Q=1 with an even 50/50 split allocates TP1:1, TP2:0 (largest-
            # remainder ties favor the earlier level) -- TP2 never receives a
            # real broker order, so it can never produce fill evidence.
            transitions=[{"after": "TP2", "action": "TRAIL_FRESH_BID", "distancePercent": "15"}],
        )
        self._seed_filled_position(correlation_id, quantity=1, fill_price="1.00", policy=policy)
        self.engine.ensure_protection(correlation_id)
        tp2 = self.protection_ledger.get(self._tp_id(correlation_id, "TP2"))
        self.assertEqual("SKIPPED_ZERO_ALLOCATION", tp2["status"])

        self.engine.ensure_transitions(correlation_id)

        tid = transition_id(correlation_id, "TP2")
        row = self.transition_ledger.get(tid)
        self.assertEqual("INERT", row["status"])
        self.assertNotEqual("APPLIED", row["status"])
        self.assertIsNone(row["applied_at"], "nothing was applied, so there is no applied_at")
        self.assertIn("SKIPPED_ZERO_ALLOCATION", row["details_json"])
        self.assertEqual([], self.transport.modified_stop)

        # Re-running is a pure no-op -- never re-evaluated forever.
        self.engine.ensure_transitions(correlation_id)
        self.assertEqual(row["applied_at"], self.transition_ledger.get(tid)["applied_at"])

    # ---- transition_id PK uniqueness: a double claim never double-applies -

    def test_transition_id_primary_key_prevents_a_double_claim_race(self):
        tid = "manual:transitions-pk-race:TP1"
        first = self.transition_ledger.ensure_pending(
            tid, correlation_id="manual:transitions-pk-race", after="TP1", action="MOVE_STOP_TO_BREAKEVEN"
        )
        second = self.transition_ledger.ensure_pending(
            tid, correlation_id="manual:transitions-pk-race", after="TP1", action="MOVE_STOP_TO_BREAKEVEN"
        )
        self.assertTrue(first.claimed)
        self.assertFalse(second.claimed)
        self.assertEqual(first.row["transition_id"], second.row["transition_id"])

        self.assertTrue(self.transition_ledger.mark_applying(tid))
        # A second concurrent "sweep pass" attempting to re-enter APPLYING
        # from a race is a structural no-op, never a crash or a second
        # broker-facing state change.
        self.assertFalse(self.transition_ledger.mark_applying(tid))
        self.assertEqual("APPLYING", self.transition_ledger.get(tid)["status"])

    # ---- real periodic-sweep wiring: a CLOSING (not just FILLED) position -
    # ---- must still be handed to ensure_transitions() ---------------------

    # Regression test for a production incident: a 3-contract, 3-level
    # (50/25/25) position with both MOVE_STOP_TO_BREAKEVEN-after-TP1 and
    # TRAIL_FRESH_BID-after-TP2 configured never received either transition.
    # Every other test in this file calls self.engine.ensure_transitions()
    # directly, which cannot catch this class of bug: production only ever
    # reaches ensure_transitions() for correlation_ids returned by
    # ExecutionLedger.sweep_candidate_correlation_ids() (see __main__.py's
    # periodic sweep). That query used to require lifecycle_status=='FILLED'
    # -- but a TP1 fill on a multi-contract position moves lifecycle_status
    # straight to CLOSING (some quantity closed, some still open), so the
    # correlation_id silently and permanently fell out of the sweep the
    # instant its transitions became eligible to run. This test drives the
    # same two-step gate production does (list candidates, then only act on
    # what's returned) rather than calling ensure_transitions() directly.
    def test_periodic_sweep_gate_still_reaches_transitions_while_a_position_is_closing(self):
        correlation_id = "manual:transitions-sweep-gate"
        policy = management_policy(
            allocations=(50, 25, 25), stop_loss_percent="25",
            transitions=[
                {"after": "TP1", "action": "MOVE_STOP_TO_BREAKEVEN"},
                {"after": "TP2", "action": "TRAIL_FRESH_BID", "distancePercent": "15"},
            ],
        )
        contract = self._seed_filled_position(correlation_id, quantity=3, fill_price="1.25", policy=policy)

        def run_one_sweep_pass() -> None:
            for candidate_id in self.ledger.sweep_candidate_correlation_ids():
                self.engine.ensure_protection(candidate_id)
                self.engine.ensure_transitions(candidate_id)

        # Initial sweep: position is FILLED, not yet CLOSING -- places the
        # bracket, no transitions are eligible yet.
        run_one_sweep_pass()
        stop2_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))
        stop3_before = self.protection_ledger.get(self._stop_id(correlation_id, "TP3"))
        self.assertEqual("SUBMITTED", stop2_before["status"])
        self.assertEqual("SUBMITTED", stop3_before["status"])

        # TP1 fills (1 of 3 contracts) -> lifecycle_status becomes CLOSING,
        # not FILLED.
        self._fill_tp_leg(correlation_id, contract, "TP1", quantity=1)
        self.assertEqual("CLOSING", self.ledger.position_state(correlation_id)["lifecycle_status"])
        self.assertIn(correlation_id, self.ledger.sweep_candidate_correlation_ids())

        run_one_sweep_pass()

        breakeven_tid = transition_id(correlation_id, "TP1")
        self.assertEqual("APPLIED", self.transition_ledger.get(breakeven_tid)["status"])
        # entry_fill_price=1.25, tick 0.05, rounded UP -> 1.25 exactly.
        stop2_after_be = self.protection_ledger.get(self._stop_id(correlation_id, "TP2"))
        stop3_after_be = self.protection_ledger.get(self._stop_id(correlation_id, "TP3"))
        self.assertEqual(Decimal("1.25"), Decimal(stop2_after_be["trigger_price"]))
        self.assertEqual(Decimal("1.25"), Decimal(stop3_after_be["trigger_price"]))

        # TP2 fills next -> still CLOSING (TP3's contract remains open) ->
        # still a sweep candidate -> TRAIL_FRESH_BID now fires for STOP:TP3.
        self.transport.option_quote = Quote(Decimal("2.00"), Decimal("2.03"), NOW, "LIVE")
        self._fill_tp_leg(correlation_id, contract, "TP2", quantity=1)
        self.assertEqual("CLOSING", self.ledger.position_state(correlation_id)["lifecycle_status"])
        self.assertIn(correlation_id, self.ledger.sweep_candidate_correlation_ids())

        run_one_sweep_pass()

        trail_tid = transition_id(correlation_id, "TP2")
        self.assertEqual("APPLIED", self.transition_ledger.get(trail_tid)["status"])
        # fresh bid 2.00 * (1 - 15/100) = 1.70, better than the 1.25 breakeven
        # trigger already resting on STOP:TP3 -> ratchets up again.
        stop3_after_trail = self.protection_ledger.get(self._stop_id(correlation_id, "TP3"))
        self.assertEqual(Decimal("1.70"), Decimal(stop3_after_trail["trigger_price"]))

        # TP3 fills last -> fully CLOSED -> correctly drops out of the sweep.
        self._fill_tp_leg(correlation_id, contract, "TP3", quantity=1)
        self.assertEqual("CLOSED", self.ledger.position_state(correlation_id)["lifecycle_status"])
        self.assertNotIn(correlation_id, self.ledger.sweep_candidate_correlation_ids())


if __name__ == "__main__":
    unittest.main()
