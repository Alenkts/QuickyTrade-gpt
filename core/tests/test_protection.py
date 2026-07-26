"""Phase 3: stop-loss / take-profit protection placement.

Covers the largest-remainder allocation math in isolation, then
ExecutionEngine.ensure_protection() end to end against a fake transport --
per-slice OCA pairing, idempotency, the FILLED+APP_MANAGED gate, top-up on a
second fill, ambiguous-ack global blocking, the restart sweep, tick-valid
pricing, and a missing-market-rule clean block.
"""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from quickytrade_core.config import CoreConfig
from quickytrade_core.domain import (
    BrokerAcknowledgement,
    BrokerAmbiguousError,
    BrokerDefinitiveError,
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
from quickytrade_core.protection import ProtectionLedger, largest_remainder_allocation
from quickytrade_core.registry import SubmissionRegistry

NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


def underlying() -> QualifiedContract:
    return QualifiedContract(
        con_id=100, symbol="QQQ", sec_type="STK", exchange="SMART",
        primary_exchange="NASDAQ", currency="USD", local_symbol="QQQ",
    )


def option(
    *, con_id: int = 201, right: str = "C", strike: str = "100", min_tick: str | None = "0.05"
) -> QualifiedContract:
    return QualifiedContract(
        con_id=con_id,
        symbol="QQQ",
        sec_type="OPT",
        exchange="SMART",
        currency="USD",
        local_symbol=f"QQQ OPT {right} {strike}",
        expiry="20260918",
        strike=Decimal(strike),
        right=right,
        multiplier="100",
        trading_class="QQQ",
        valid_exchanges=("SMART",),
        market_rule_ids=(26,),
        min_tick=Decimal(min_tick) if min_tick is not None else None,
    )


def management_policy(allocations=(50, 25, 25), stop_loss_percent="25"):
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


def open_request(alert="tv-protection-open"):
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
    """Supports both the full open-order flow (for the one end-to-end
    ambiguous-protection-blocks-a-new-entry test) and protection's own
    market_rule/place_limit_order/place_stop_limit_order surface."""

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
        self.market_rule_result: object = None
        self.market_rule_calls = 0
        self.position_rows: list[Position] = []
        self.order_rows: list[WorkingOrder] = []
        self.placed: list = []  # entry/close LMT orders via the standard open flow
        self.placed_limit: list = []  # protection TAKE_PROFIT SELL LMT orders
        self.placed_stop: list = []  # protection STOP_LOSS STP LMT orders
        self.place_result: object = BrokerAcknowledgement(700, 71, 900, "PreSubmitted")
        self.limit_result: object = BrokerAcknowledgement(800, 71, 950, "PreSubmitted")
        self.stop_result: object = BrokerAcknowledgement(801, 71, 951, "PreSubmitted")

    def start(self): pass
    def stop(self): pass

    def readiness(self):
        return Readiness(True, True, True, self.reconciled, self.managed_accounts, self.environment, False)

    def qualify_underlying(self, symbol): return self.underlying
    def option_chains(self, underlying_contract): return (self.chain,)

    def qualify_option(self, *, underlying, expiry, strike, right, exchange, trading_class, multiplier):
        return option(con_id=301 if right == "C" else 302, right=right, strike=str(strike))

    def quote(self, contract):
        return self.underlying_quote if contract.sec_type == "STK" else self.option_quote

    def market_rule(self, contract):
        self.market_rule_calls += 1
        if isinstance(self.market_rule_result, Exception):
            raise self.market_rule_result
        return self.market_rule_result if self.market_rule_result is not None else self.rules

    def positions(self, account): return tuple(self.position_rows)
    def working_orders(self, account): return tuple(self.order_rows)

    def place_limit_order(self, request):
        # Entry/close requests use tif="DAY"; protection TAKE_PROFIT legs use
        # tif="GTC" -- routed to separate lists so assertions can tell them
        # apart without relying on caller bookkeeping.
        if request.tif == "GTC":
            self.placed_limit.append(request)
            result = self.limit_result
        else:
            self.placed.append(request)
            result = self.place_result
        if isinstance(result, Exception):
            raise result
        return result

    def place_stop_limit_order(self, request):
        self.placed_stop.append(request)
        if isinstance(self.stop_result, Exception):
            raise self.stop_result
        return self.stop_result


class LargestRemainderAllocationTests(unittest.TestCase):
    def test_q1_5050_2525_yields_tp1_only(self):
        levels = management_policy(allocations=(50, 25, 25))["takeProfitLevels"]
        allocation = largest_remainder_allocation(1, levels)
        self.assertEqual({"TP1": 1, "TP2": 0, "TP3": 0}, allocation)

    def test_q4_5050_2525_yields_2_1_1(self):
        levels = management_policy(allocations=(50, 25, 25))["takeProfitLevels"]
        allocation = largest_remainder_allocation(4, levels)
        self.assertEqual({"TP1": 2, "TP2": 1, "TP3": 1}, allocation)

    def test_zero_quantity_yields_all_zero(self):
        levels = management_policy(allocations=(50, 25, 25))["takeProfitLevels"]
        allocation = largest_remainder_allocation(0, levels)
        self.assertEqual({"TP1": 0, "TP2": 0, "TP3": 0}, allocation)

    def test_ties_break_toward_the_earlier_lower_trigger_level(self):
        # Q=2 with an even 3-way split: raw floors are all 0, remainder 2/3
        # each (a genuine 3-way tie) -- the two leftover units must land on
        # TP1 then TP2 (ascending-trigger order), never TP3.
        levels = management_policy(allocations=("33.34", "33.33", "33.33"))["takeProfitLevels"]
        allocation = largest_remainder_allocation(2, levels)
        self.assertEqual(2, sum(allocation.values()))
        self.assertEqual(1, allocation["TP1"])

    def test_property_sum_equals_quantity_across_q_and_split_combinations(self):
        splits = [
            (50, 25, 25),
            (34, 33, 33),
            (100,),
            (10, 10, 10, 10, 10, 10, 10, 30),
            (1, 1, 1, 1, 1, 1, 1, 93),
            (60, 40),
            ("33.34", "33.33", "33.33"),
        ]
        rng = random.Random(20260721)
        for split in splits:
            levels = management_policy(allocations=split)["takeProfitLevels"]
            for _ in range(6):
                quantity = rng.randint(0, 500)
                with self.subTest(split=split, quantity=quantity):
                    allocation = largest_remainder_allocation(quantity, levels)
                    self.assertEqual(quantity, sum(allocation.values()))
                    self.assertTrue(all(value >= 0 for value in allocation.values()))

    def test_rejects_negative_quantity(self):
        levels = management_policy()["takeProfitLevels"]
        with self.assertRaises(ValueError):
            largest_remainder_allocation(-1, levels)


class ProtectionPlacementTests(unittest.TestCase):
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

    def _seed(
        self,
        correlation_id: str,
        *,
        quantity: int,
        fill_price: str = "1.00",
        management_mode: str = "APP_MANAGED",
        policy=None,
        contract: QualifiedContract | None = None,
        account: str = "DU12345",
        exec_suffix: str = "1",
    ) -> QualifiedContract:
        contract = contract or option()
        if management_mode == "APP_MANAGED" and policy is None:
            policy = management_policy()
        order_ref = "QT" + hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()[:30]
        # First seed for this correlation_id claims/finishes the entry; a
        # second call (simulating a later top-up fill) only adds an execution.
        if self.registry.submission_evidence(correlation_id) is None:
            self.registry.claim(
                correlation_id, 1, "hash-" + correlation_id,
                source="MANUAL_UI", management_mode=management_mode, management_policy=policy,
            )
            self.registry.record_broker_call_evidence(
                correlation_id, account=account, action="OPEN_LONG_CALL",
                contract=_contract_to_json(contract), side="BUY", quantity=quantity,
                limit_price=fill_price, order_ref=order_ref, entry_correlation_id=None,
            )
            self.registry.finish(
                correlation_id, status="SUBMITTED", result={"status": "SUBMITTED"},
                account=account, action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
            )
        self.ledger.record_execution(ExecutionRecord(
            exec_id=f"exec-{correlation_id}-{exec_suffix}", order_ref=order_ref, order_id=700, perm_id=900,
            account=account, con_id=contract.con_id, symbol=contract.symbol, side="BOT",
            shares=str(quantity), price=fill_price, cum_qty=str(quantity), avg_price=fill_price,
            exec_time="20260721  10:00:00", source="LIVE_CALLBACK", raw={},
        ))
        return contract

    def _tp_id(self, correlation_id, level_id, index=1):
        base = f"{correlation_id}:TP:{level_id}"
        return base if index == 1 else f"{base}:{index}"

    def _stop_id(self, correlation_id, level_id, index=1):
        base = f"{correlation_id}:STOP:{level_id}"
        return base if index == 1 else f"{base}:{index}"

    # ---- confirmed fill -> exactly the right legs ----------------------

    def test_one_contract_fill_places_one_oca_pair_and_skips_remaining_levels(self):
        correlation_id = "manual:protect-one-contract"
        self._seed(correlation_id, quantity=1)

        self.engine.ensure_protection(correlation_id)

        rows = {row["protection_id"]: row for row in self.protection_ledger.legs_for_correlation(correlation_id)}
        self.assertEqual(6, len(rows))
        tp1 = rows[self._tp_id(correlation_id, "TP1")]
        stop1 = rows[self._stop_id(correlation_id, "TP1")]
        self.assertEqual("SUBMITTED", tp1["status"])
        self.assertEqual("SUBMITTED", stop1["status"])
        self.assertEqual("1", tp1["quantity"])
        self.assertEqual("1", stop1["quantity"])
        self.assertEqual(tp1["oca_group"], stop1["oca_group"])
        for level_id in ("TP2", "TP3"):
            tp = rows[self._tp_id(correlation_id, level_id)]
            stop = rows[self._stop_id(correlation_id, level_id)]
            self.assertEqual("SKIPPED_ZERO_ALLOCATION", tp["status"])
            self.assertEqual("SKIPPED_ZERO_ALLOCATION", stop["status"])
            self.assertEqual("0", tp["quantity"])
            self.assertEqual("0", stop["quantity"])

        self.assertEqual(1, len(self.transport.placed_limit))
        self.assertEqual(1, len(self.transport.placed_stop))
        self.assertEqual(tp1["oca_group"], self.transport.placed_limit[0].oca_group)
        self.assertEqual(stop1["oca_group"], self.transport.placed_stop[0].oca_group)

    def test_q4_fill_yields_2_1_1_three_independently_oca_linked_pairs(self):
        correlation_id = "manual:protect-q4"
        self._seed(correlation_id, quantity=4)

        self.engine.ensure_protection(correlation_id)

        rows = {row["protection_id"]: row for row in self.protection_ledger.legs_for_correlation(correlation_id)}
        expected = {"TP1": 2, "TP2": 1, "TP3": 1}
        oca_groups = set()
        for level_id, expected_qty in expected.items():
            tp = rows[self._tp_id(correlation_id, level_id)]
            stop = rows[self._stop_id(correlation_id, level_id)]
            self.assertEqual("SUBMITTED", tp["status"])
            self.assertEqual("SUBMITTED", stop["status"])
            self.assertEqual(str(expected_qty), tp["quantity"])
            self.assertEqual(str(expected_qty), stop["quantity"])
            self.assertEqual(tp["oca_group"], stop["oca_group"])
            oca_groups.add(tp["oca_group"])
        # Sigma qty == Q for both roles.
        self.assertEqual(4, sum(int(rows[self._tp_id(correlation_id, level)]["quantity"]) for level in expected))
        self.assertEqual(4, sum(int(rows[self._stop_id(correlation_id, level)]["quantity"]) for level in expected))
        # Each pair is independently OCA-linked: three distinct groups, and
        # TP1's group contains neither TP2's nor TP3's orders.
        self.assertEqual(3, len(oca_groups))
        tp1_group = rows[self._tp_id(correlation_id, "TP1")]["oca_group"]
        for level_id in ("TP2", "TP3"):
            self.assertNotEqual(tp1_group, rows[self._tp_id(correlation_id, level_id)]["oca_group"])
            self.assertNotEqual(tp1_group, rows[self._stop_id(correlation_id, level_id)]["oca_group"])

        self.assertEqual(3, len(self.transport.placed_limit))
        self.assertEqual(3, len(self.transport.placed_stop))
        placed_oca_groups = {request.oca_group for request in self.transport.placed_limit}
        self.assertEqual(oca_groups, placed_oca_groups)

    # ---- idempotency ----------------------------------------------------

    def test_ensure_protection_is_idempotent_running_twice_places_nothing_new(self):
        correlation_id = "manual:protect-idempotent"
        self._seed(correlation_id, quantity=1)

        self.engine.ensure_protection(correlation_id)
        first_rows = self.protection_ledger.legs_for_correlation(correlation_id)
        self.engine.ensure_protection(correlation_id)
        second_rows = self.protection_ledger.legs_for_correlation(correlation_id)

        self.assertEqual(len(first_rows), len(second_rows))
        self.assertEqual(1, len(self.transport.placed_limit))
        self.assertEqual(1, len(self.transport.placed_stop))

    # ---- gating -----------------------------------------------------------

    def test_not_yet_filled_is_not_a_candidate_zero_orders_placed(self):
        correlation_id = "manual:protect-partial"
        contract = option()
        policy = management_policy()
        order_ref = "QT" + hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()[:30]
        self.registry.claim(
            correlation_id, 1, "hash-" + correlation_id,
            source="MANUAL_UI", management_mode="APP_MANAGED", management_policy=policy,
        )
        # quantity target 5, only 2 filled so far -> PARTIALLY_FILLED, not FILLED.
        self.registry.record_broker_call_evidence(
            correlation_id, account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
            side="BUY", quantity=5, limit_price="1.00", order_ref=order_ref, entry_correlation_id=None,
        )
        self.registry.finish(
            correlation_id, status="SUBMITTED", result={"status": "SUBMITTED"},
            account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
        )
        self.ledger.record_execution(ExecutionRecord(
            exec_id="exec-partial-1", order_ref=order_ref, order_id=700, perm_id=900, account="DU12345",
            con_id=contract.con_id, symbol=contract.symbol, side="BOT", shares="2", price="1.00",
            cum_qty="2", avg_price="1.00", exec_time="20260721  10:00:00", source="LIVE_CALLBACK", raw={},
        ))
        self.assertEqual(
            "PARTIALLY_FILLED", self.ledger.position_state(correlation_id)["lifecycle_status"]
        )

        self.engine.ensure_protection(correlation_id)

        self.assertEqual([], self.protection_ledger.legs_for_correlation(correlation_id))
        self.assertEqual([], self.transport.placed_limit)
        self.assertEqual([], self.transport.placed_stop)

    def test_entry_only_never_gets_protection_regardless_of_fill_status(self):
        correlation_id = "manual:protect-entry-only"
        self._seed(correlation_id, quantity=1, management_mode="ENTRY_ONLY", policy=None)

        self.engine.ensure_protection(correlation_id)

        self.assertEqual([], self.protection_ledger.legs_for_correlation(correlation_id))
        self.assertEqual([], self.transport.placed_limit)
        self.assertEqual([], self.transport.placed_stop)

    # ---- top-up on a second fill --------------------------------------

    def test_second_fill_tops_up_existing_legs_without_shrinking_them(self):
        correlation_id = "manual:protect-topup"
        self._seed(correlation_id, quantity=1)
        self.engine.ensure_protection(correlation_id)
        first_tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1"))
        self.assertEqual("1", first_tp1["quantity"])
        self.assertEqual("SUBMITTED", first_tp1["status"])

        # A second fill event lands (synthetic multi-fill scenario): total
        # opened quantity is now 4 -- allocations become {TP1:2, TP2:1, TP3:1}.
        self._seed(correlation_id, quantity=3, exec_suffix="2")
        self.assertEqual(4, int(self.ledger.position_state(correlation_id)["opened_quantity"]))

        self.engine.ensure_protection(correlation_id)

        # The original TP1 row is never mutated/shrunk -- a NEW row covers
        # the additional delta.
        unchanged_first_tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1"))
        self.assertEqual("1", unchanged_first_tp1["quantity"])
        topup_tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1", index=2))
        self.assertIsNotNone(topup_tp1)
        self.assertEqual("1", topup_tp1["quantity"])
        self.assertEqual("SUBMITTED", topup_tp1["status"])
        # Total TP1 coverage now sums to the new target (2).
        tp1_family = self.protection_ledger.legs(correlation_id, role="TAKE_PROFIT", level_id="TP1")
        self.assertEqual(2, sum(int(row["quantity"]) for row in tp1_family if row["status"] == "SUBMITTED"))

        # TP2 (previously SKIPPED_ZERO_ALLOCATION at Q=1) now gets covered.
        original_tp2 = self.protection_ledger.get(self._tp_id(correlation_id, "TP2"))
        self.assertEqual("SKIPPED_ZERO_ALLOCATION", original_tp2["status"])
        topup_tp2 = self.protection_ledger.get(self._tp_id(correlation_id, "TP2", index=2))
        self.assertIsNotNone(topup_tp2)
        self.assertEqual("SUBMITTED", topup_tp2["status"])
        self.assertEqual("1", topup_tp2["quantity"])

        # Stop-loss slices top up in lockstep with their paired TP slices.
        stop1_family = self.protection_ledger.legs(
            correlation_id, role="STOP_LOSS", oca_group=unchanged_first_tp1["oca_group"]
        )
        self.assertEqual(2, sum(int(row["quantity"]) for row in stop1_family if row["status"] == "SUBMITTED"))

    # ---- ambiguous ack blocks a brand-new unrelated entry ---------------

    def test_ambiguous_protection_ack_blocks_a_new_unrelated_entry_end_to_end(self):
        correlation_id = "manual:protect-ambiguous"
        self._seed(correlation_id, quantity=1)
        self.transport.limit_result = BrokerAmbiguousError("IBKR_ACK_TIMEOUT", "timeout")

        self.engine.ensure_protection(correlation_id)

        tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1"))
        self.assertEqual("SUBMISSION_UNKNOWN", tp1["status"])
        self.assertTrue(self.protection_ledger.has_unresolved_unknown())

        before = len(self.transport.placed)
        result = self.engine.execute(open_request("tv-blocked-by-protection"))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("UNRESOLVED_PROTECTION_SUBMISSION", result.body["code"])
        self.assertEqual(before, len(self.transport.placed))

    # ---- restart sweep ----------------------------------------------------

    def test_restart_sweep_resolves_a_stuck_submitting_protection_row(self):
        protection_id = "manual:restart-stub:TP:TP1"
        self.protection_ledger.claim_leg(
            protection_id, correlation_id="manual:restart-stub", role="TAKE_PROFIT",
            level_id="TP1", oca_group="QTOCAstub", quantity=1,
        )
        self.protection_ledger.record_broker_call_evidence(
            protection_id, trigger_price=None, limit_price="1.25", order_ref="QTstub",
        )
        self.assertEqual("SUBMITTING", self.protection_ledger.get(protection_id)["status"])

        # Simulate a restart: a fresh ProtectionLedger over the same
        # connection/lock (mirrors how __main__.py reconstructs it).
        restarted = ProtectionLedger(self.registry.connection, self.registry.lock)
        self.assertEqual("SUBMISSION_UNKNOWN", restarted.get(protection_id)["status"])

    # ---- tick-valid pricing -----------------------------------------------

    def test_stop_limit_is_at_or_below_trigger_and_take_profit_rounds_ceiling(self):
        correlation_id = "manual:protect-pricing"
        self._seed(correlation_id, quantity=1, fill_price="1.03")

        self.engine.ensure_protection(correlation_id)

        tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1"))
        stop1 = self.protection_ledger.get(self._stop_id(correlation_id, "TP1"))
        tick = Decimal("0.05")

        # entry_price * 1.20 = 1.236 -> ROUND_CEILING to the nearest nickel = 1.25.
        self.assertEqual(Decimal("1.25"), Decimal(tp1["limit_price"]))
        self.assertEqual(Decimal("0"), Decimal(tp1["limit_price"]) % tick)

        trigger = Decimal(stop1["trigger_price"])
        limit = Decimal(stop1["limit_price"])
        self.assertLessEqual(limit, trigger)
        self.assertEqual(Decimal("0"), trigger % tick)
        self.assertEqual(Decimal("0"), limit % tick)
        # entry_price * 0.75 = 0.7725 -> ROUND_FLOOR to the nearest nickel = 0.75.
        self.assertEqual(Decimal("0.75"), trigger)

    # ---- missing/invalid market rule blocks cleanly ------------------------

    def test_missing_min_tick_blocks_protection_placement_cleanly(self):
        correlation_id = "manual:protect-no-min-tick"
        contract = option(min_tick=None)
        self._seed(correlation_id, quantity=1, contract=contract)

        self.engine.ensure_protection(correlation_id)

        tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1"))
        self.assertEqual("BLOCKED", tp1["status"])
        self.assertEqual(0, self.transport.market_rule_calls)
        self.assertEqual([], self.transport.placed_limit)
        self.assertEqual([], self.transport.placed_stop)

    def test_invalid_market_rule_blocks_protection_placement_cleanly(self):
        correlation_id = "manual:protect-bad-market-rule"
        self._seed(correlation_id, quantity=1)
        self.transport.market_rule_result = BrokerDefinitiveError("MARKET_RULE_UNAVAILABLE", "no rule")

        self.engine.ensure_protection(correlation_id)

        tp1 = self.protection_ledger.get(self._tp_id(correlation_id, "TP1"))
        self.assertEqual("BLOCKED", tp1["status"])
        self.assertEqual("MARKET_RULE_UNAVAILABLE", json.loads(tp1["result_json"])["code"])
        self.assertEqual([], self.transport.placed_limit)
        self.assertEqual([], self.transport.placed_stop)


if __name__ == "__main__":
    unittest.main()


class ProtectionLegReconciliationTests(ProtectionPlacementTests):
    """H7: 18 SUBMITTED protection legs against 9 CLOSED positions.

    Nothing ever moved a leg off SUBMITTED, so the ledger could not answer
    "is this position actually still protected?".
    """

    def test_a_leg_with_broker_execution_evidence_resolves_to_filled(self):
        correlation_id = "manual:leg-recon-filled"
        self._seed(correlation_id, quantity=2, fill_price="1.00")
        self.engine.ensure_protection(correlation_id)
        legs = self.protection_ledger.legs_for_correlation(correlation_id)
        working = [leg for leg in legs if leg["status"] == "SUBMITTED"]
        self.assertTrue(working, "expected the fixture to place working legs")
        target = working[0]

        self.ledger.record_execution(_leg_execution("x-leg-1", target["order_ref"]))
        resolved = self.engine.reconcile_protection_legs()

        self.assertEqual(1, resolved["filled"])
        self.assertEqual("FILLED", self.protection_ledger.get(target["protection_id"])["status"])

    def test_the_oca_sibling_of_a_filled_leg_resolves_to_cancelled(self):
        correlation_id = "manual:leg-recon-oca"
        self._seed(correlation_id, quantity=2, fill_price="1.00")
        self.engine.ensure_protection(correlation_id)
        legs = [leg for leg in self.protection_ledger.legs_for_correlation(correlation_id)
                if leg["status"] == "SUBMITTED" and leg["oca_group"]]
        groups = {}
        for leg in legs:
            groups.setdefault(leg["oca_group"], []).append(leg)
        pair = next((members for members in groups.values() if len(members) >= 2), None)
        self.assertIsNotNone(pair, "expected an OCA take-profit/stop pair")

        self.ledger.record_execution(_leg_execution("x-leg-2", pair[0]["order_ref"]))
        resolved = self.engine.reconcile_protection_legs()

        self.assertEqual("FILLED", self.protection_ledger.get(pair[0]["protection_id"])["status"])
        self.assertEqual("CANCELLED", self.protection_ledger.get(pair[1]["protection_id"])["status"])
        self.assertEqual(1, resolved["cancelled"])

    def test_a_leg_with_no_execution_evidence_is_left_alone_never_guessed(self):
        correlation_id = "manual:leg-recon-untouched"
        self._seed(correlation_id, quantity=2, fill_price="1.00")
        self.engine.ensure_protection(correlation_id)
        before = [leg["status"] for leg in self.protection_ledger.legs_for_correlation(correlation_id)]

        resolved = self.engine.reconcile_protection_legs()

        after = [leg["status"] for leg in self.protection_ledger.legs_for_correlation(correlation_id)]
        self.assertEqual(before, after, "absence of an order is not evidence of a fill or a cancel")
        self.assertEqual({"filled": 0, "cancelled": 0}, resolved)


def _leg_execution(exec_id: str, order_ref: str) -> ExecutionRecord:
    return ExecutionRecord(
        exec_id=exec_id, order_ref=order_ref, order_id=800, perm_id=901, account="DU12345",
        con_id=201, symbol="QQQ", side="SLD", shares="1", price="1.20", cum_qty="1", avg_price="1.20",
        exec_time="20260720 10:05:00 US/Eastern", source="LIVE_CALLBACK", raw={"execId": exec_id},
    )
