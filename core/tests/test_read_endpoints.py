"""Phase 6: read-only GET endpoints (`/private/v1/positions`, `/protection`,
`/executions`, `/reconciliation`) that expose the broker-authoritative
execution/protection/transition/reconciliation ledgers to the Node/operator
UI without ever placing, modifying, or cancelling an order.

Covers: correct response shape, auth-required (401, matching the existing
POST-endpoint 401 shape), a correlationId with no matching data returning a
clean not-found/empty response (never a crash), and that `last_reconciled_at`
is honestly propagated -- never fabricated as a fresh timestamp when the
ledger actually has none recorded.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from quickytrade_core.config import CoreConfig
from quickytrade_core.domain import BrokerAcknowledgement, Position, PriceIncrement, QualifiedContract
from quickytrade_core.engine import ExecutionEngine, _contract_to_json
from quickytrade_core.execution_ledger import CommissionRecord, ExecutionLedger, ExecutionRecord
from quickytrade_core.http_service import _Handler
from quickytrade_core.protection import ProtectionLedger
from quickytrade_core.registry import SubmissionRegistry
from quickytrade_core.transitions import TransitionLedger

NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


def option() -> QualifiedContract:
    return QualifiedContract(
        con_id=201, symbol="QQQ", sec_type="OPT", exchange="SMART", currency="USD",
        local_symbol="QQQ OPT C 100", expiry="20260918", strike=Decimal("100"), right="C",
        multiplier="100", trading_class="QQQ", valid_exchanges=("SMART",),
        market_rule_ids=(26,), min_tick=Decimal("0.05"),
    )


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


def http_get(config, engine, path: str, *, token: str | None = None):
    header = f"Authorization: Bearer {token}\r\n" if token is not None else ""
    request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\n{header}\r\n".encode()
    connection = _FakeHttpSocket(request)
    server = type("FakeServer", (), {"config": config, "engine": engine})()
    _Handler(connection, ("127.0.0.1", 12345), server)
    raw = bytes(connection.output)
    head, body = raw.split(b"\r\n\r\n", 1)
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(body)


class FakeTransport:
    """Only the surface ensure_protection() needs to place a real protection
    bracket for the fixture below. The read-only GET endpoints under test
    never call any of these -- they only ever read from the already-built
    ledgers -- but the fixture's own setup (seeding a genuinely protected
    position) legitimately does."""

    def __init__(self):
        self.rules = (PriceIncrement(Decimal("0"), Decimal("0.05")),)
        self._next_order_id = 800
        self.position_rows = []

    def positions(self, account):
        return tuple(row for row in self.position_rows if row.account == account)

    def market_rule(self, contract):
        return self.rules

    def place_limit_order(self, request):
        order_id = self._allocate()
        return BrokerAcknowledgement(order_id, 71, order_id + 10_000, "PreSubmitted", oca_group=request.oca_group)

    def place_stop_limit_order(self, request):
        order_id = self._allocate()
        return BrokerAcknowledgement(order_id, 71, order_id + 10_000, "PreSubmitted", oca_group=request.oca_group)

    def _allocate(self) -> int:
        order_id = self._next_order_id
        self._next_order_id += 1
        return order_id


class ReadEndpointsTests(unittest.TestCase):
    """Every ledger wired -- the "happy path" fixture."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = CoreConfig(
            ibkr_host="127.0.0.1", ibkr_port=7497, client_id=71, selected_account="DU12345",
            paper_account_allowlist=frozenset({"DU12345"}), allowed_symbols=frozenset({"QQQ"}),
            trading_class_allowlist=frozenset({"QQQ"}), state_db_path=Path(self.temp.name) / "state.sqlite3",
            service_token="x" * 32,
        )
        self.registry = SubmissionRegistry(self.config.state_db_path)
        self.ledger = ExecutionLedger(self.registry.connection, self.registry.lock)
        self.protection_ledger = ProtectionLedger(self.registry.connection, self.registry.lock)
        self.transition_ledger = TransitionLedger(self.registry.connection, self.registry.lock)
        self.transport = FakeTransport()
        self.engine = ExecutionEngine(
            config=self.config, transport=self.transport, registry=self.registry,
            ledger=self.ledger, protection_ledger=self.protection_ledger,
            transition_ledger=self.transition_ledger, clock=lambda: NOW,
        )

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    def _seed_position(
        self,
        correlation_id: str,
        *,
        quantity: int = 2,
        mark_reconciled: bool = False,
        con_id: int | None = None,
    ) -> None:
        base_contract = option()
        next_con_id = 201 + len(self.transport.position_rows)
        contract = QualifiedContract(
            **{**base_contract.__dict__, "con_id": con_id if con_id is not None else next_con_id}
        )
        self.transport.position_rows.append(
            Position(account="DU12345", contract=contract, quantity=Decimal(quantity), average_cost=Decimal("1.00"))
        )
        order_ref = "QT" + hashlib.sha256(correlation_id.encode()).hexdigest()[:30]
        self.registry.claim(
            correlation_id, 1, "hash-" + correlation_id, source="MANUAL_UI", management_mode="ENTRY_ONLY"
        )
        self.registry.record_broker_call_evidence(
            correlation_id, account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
            side="BUY", quantity=quantity, limit_price="1.00", order_ref=order_ref,
            entry_correlation_id=None,
        )
        self.registry.finish(
            correlation_id, status="SUBMITTED", result={"status": "SUBMITTED"},
            account="DU12345", action="OPEN_LONG_CALL", contract=_contract_to_json(contract),
        )
        self.ledger.record_execution(ExecutionRecord(
            exec_id=f"exec-{correlation_id}", order_ref=order_ref,
            order_id=700, perm_id=900, account="DU12345", con_id=contract.con_id, symbol=contract.symbol,
            side="BOT", shares=str(quantity), price="1.00", cum_qty=str(quantity), avg_price="1.00",
            exec_time="20260721  10:00:00",
            source="RECONCILE_SWEEP" if mark_reconciled else "LIVE_CALLBACK", raw={},
        ))

    # ---- auth ------------------------------------------------------------

    def test_every_new_route_requires_authorization(self):
        for path in (
            "/private/v1/positions",
            "/private/v1/protection?correlationId=x",
            "/private/v1/executions?correlationId=x",
            "/private/v1/reconciliation",
        ):
            with self.subTest(path=path):
                status, body = http_get(self.config, self.engine, path)
                self.assertEqual(401, status)
                self.assertEqual("BLOCKED", body["status"])
                self.assertEqual("CORE_AUTHENTICATION_FAILED", body["code"])

    def test_wrong_token_is_also_rejected(self):
        status, body = http_get(self.config, self.engine, "/private/v1/positions", token="wrong" * 8)
        self.assertEqual(401, status)
        self.assertEqual("CORE_AUTHENTICATION_FAILED", body["code"])

    def test_unknown_read_path_still_404s(self):
        status, body = http_get(self.config, self.engine, "/private/v1/unknown", token=self.config.service_token)
        self.assertEqual(404, status)
        self.assertEqual("NOT_FOUND", body["status"])

    # ---- /private/v1/positions --------------------------------------------

    def test_positions_unknown_correlation_id_is_a_clean_empty_response_not_a_crash(self):
        status, body = http_get(
            self.config, self.engine, "/private/v1/positions?correlationId=does-not-exist",
            token=self.config.service_token,
        )
        self.assertEqual(200, status)
        self.assertEqual({"status": "OK", "items": []}, body)

    def test_positions_lists_all_when_no_correlation_id_given(self):
        self._seed_position("manual:pos-a")
        self._seed_position("manual:pos-b")
        status, body = http_get(self.config, self.engine, "/private/v1/positions", token=self.config.service_token)
        self.assertEqual(200, status)
        self.assertEqual("OK", body["status"])
        ids = {row["correlation_id"] for row in body["items"]}
        self.assertEqual({"manual:pos-a", "manual:pos-b"}, ids)
        self.assertTrue(all(row["operator_position_status"] == "ACTIVE_CONFIRMED" for row in body["items"]))

    def test_positions_fail_closed_when_broker_quantity_disagrees(self):
        self._seed_position("manual:mismatch", quantity=2)
        self.transport.position_rows[0] = Position(
            account="DU12345",
            contract=self.transport.position_rows[0].contract,
            quantity=Decimal("1"),
            average_cost=Decimal("1.00"),
        )
        status, body = http_get(
            self.config, self.engine, "/private/v1/positions", token=self.config.service_token
        )
        self.assertEqual(503, status)
        self.assertEqual("POSITION_QUANTITY_DISCREPANCY", body["code"])

    def test_positions_fail_closed_when_two_correlations_share_one_contract(self):
        self._seed_position("manual:first", quantity=1, con_id=301)
        self._seed_position("manual:second", quantity=1, con_id=301)
        self.transport.position_rows = [
            Position(
                account="DU12345",
                contract=self.transport.position_rows[0].contract,
                quantity=Decimal("2"),
                average_cost=Decimal("1.00"),
            )
        ]
        status, body = http_get(
            self.config, self.engine, "/private/v1/positions", token=self.config.service_token
        )
        self.assertEqual(503, status)
        self.assertEqual("POSITION_OWNERSHIP_AMBIGUOUS", body["code"])

    def test_positions_fail_closed_for_unattributed_broker_position(self):
        self.transport.position_rows = [
            Position(
                account="DU12345",
                contract=option(),
                quantity=Decimal("1"),
                average_cost=Decimal("1.00"),
            )
        ]
        status, body = http_get(
            self.config, self.engine, "/private/v1/positions", token=self.config.service_token
        )
        self.assertEqual(503, status)
        self.assertEqual("UNATTRIBUTED_BROKER_POSITION", body["code"])

    def test_positions_filters_to_one_correlation_id(self):
        self._seed_position("manual:pos-only")
        status, body = http_get(
            self.config, self.engine, "/private/v1/positions?correlationId=manual:pos-only",
            token=self.config.service_token,
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["items"]))
        self.assertEqual("manual:pos-only", body["items"][0]["correlation_id"])
        self.assertEqual("2", body["items"][0]["open_quantity"])

    # last_reconciled_at honesty: never fabricated.

    def test_last_reconciled_at_is_null_when_never_reconciled_not_fabricated_as_recent(self):
        self._seed_position("manual:never-reconciled", mark_reconciled=False)
        status, body = http_get(
            self.config, self.engine, "/private/v1/positions?correlationId=manual:never-reconciled",
            token=self.config.service_token,
        )
        self.assertEqual(200, status)
        self.assertIsNone(body["items"][0]["last_reconciled_at"])

    def test_last_reconciled_at_reflects_the_true_stored_reconciliation_timestamp(self):
        self._seed_position("manual:reconciled", mark_reconciled=True)
        status, body = http_get(
            self.config, self.engine, "/private/v1/positions?correlationId=manual:reconciled",
            token=self.config.service_token,
        )
        self.assertEqual(200, status)
        stored = self.ledger.position_state("manual:reconciled")["last_reconciled_at"]
        self.assertIsNotNone(stored)
        self.assertEqual(stored, body["items"][0]["last_reconciled_at"])

    def test_positions_unavailable_when_ledger_not_wired(self):
        engine = ExecutionEngine(
            config=self.config, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        status, body = http_get(self.config, engine, "/private/v1/positions", token=self.config.service_token)
        self.assertEqual(503, status)
        self.assertEqual("UNAVAILABLE", body["status"])
        self.assertEqual("EXECUTION_LEDGER_UNAVAILABLE", body["code"])

    # ---- /private/v1/protection --------------------------------------------

    def test_protection_requires_correlation_id(self):
        status, body = http_get(self.config, self.engine, "/private/v1/protection", token=self.config.service_token)
        self.assertEqual(400, status)
        self.assertEqual("CORRELATION_ID_REQUIRED", body["code"])

    def test_protection_unknown_correlation_id_returns_clean_empty_lists(self):
        status, body = http_get(
            self.config, self.engine, "/private/v1/protection?correlationId=nope", token=self.config.service_token,
        )
        self.assertEqual(200, status)
        self.assertEqual({"status": "OK", "correlationId": "nope", "protectionLegs": [], "transitions": []}, body)

    def test_protection_returns_real_legs_and_transitions_for_a_protected_position(self):
        correlation_id = "manual:protected"
        self._seed_position(correlation_id, quantity=2)
        # APP_MANAGED policy required for ensure_protection(); re-seed with policy.
        self._reseed_app_managed(correlation_id)
        self.engine.ensure_protection(correlation_id)
        status, body = http_get(
            self.config, self.engine, f"/private/v1/protection?correlationId={correlation_id}",
            token=self.config.service_token,
        )
        self.assertEqual(200, status)
        self.assertEqual("OK", body["status"])
        roles = {leg["role"] for leg in body["protectionLegs"]}
        self.assertIn("TAKE_PROFIT", roles)
        self.assertIn("STOP_LOSS", roles)
        self.assertTrue(all(leg["status"] == "SUBMITTED" for leg in body["protectionLegs"]))
        self.assertIsInstance(body["transitions"], list)

    def _reseed_app_managed(self, correlation_id: str) -> None:
        policy = {
            "policyId": "paper-balanced-v1", "version": 1,
            "takeProfitLevels": [{"levelId": "TP1", "triggerPercent": "20", "allocationPercent": "100"}],
            "stopLossPercent": "25",
        }
        # Directly overwrite management_mode/policy on the already-claimed row
        # (claim() is immutable-once-set, so this test seeds it fresh instead
        # of routing through claim() a second time).
        self.registry.connection.execute(
            "UPDATE broker_submissions SET management_mode='APP_MANAGED', management_policy_json=? "
            "WHERE correlation_id=?",
            (json.dumps(policy), correlation_id),
        )

    def test_protection_unavailable_when_protection_ledger_not_wired(self):
        engine = ExecutionEngine(
            config=self.config, transport=self.transport, registry=self.registry, ledger=self.ledger, clock=lambda: NOW,
        )
        status, body = http_get(
            self.config, engine, "/private/v1/protection?correlationId=x", token=self.config.service_token,
        )
        self.assertEqual(503, status)
        self.assertEqual("PROTECTION_LEDGER_UNAVAILABLE", body["code"])

    # ---- /private/v1/executions --------------------------------------------

    def test_executions_requires_correlation_id(self):
        status, body = http_get(self.config, self.engine, "/private/v1/executions", token=self.config.service_token)
        self.assertEqual(400, status)
        self.assertEqual("CORRELATION_ID_REQUIRED", body["code"])

    def test_executions_unknown_correlation_id_returns_clean_empty_lists(self):
        status, body = http_get(
            self.config, self.engine, "/private/v1/executions?correlationId=nope", token=self.config.service_token,
        )
        self.assertEqual(200, status)
        self.assertEqual({"status": "OK", "correlationId": "nope", "executions": [], "commissions": []}, body)

    def test_executions_returns_fills_and_commissions(self):
        correlation_id = "manual:executions"
        self._seed_position(correlation_id, quantity=3)
        self.ledger.record_commission(CommissionRecord(
            exec_id=f"exec-{correlation_id}", commission="0.65", currency="USD", realized_pnl=None, raw={},
        ))
        status, body = http_get(
            self.config, self.engine, f"/private/v1/executions?correlationId={correlation_id}",
            token=self.config.service_token,
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["executions"]))
        self.assertEqual("3", body["executions"][0]["shares"])
        self.assertEqual(1, len(body["commissions"]))
        self.assertEqual("0.65", body["commissions"][0]["commission"])

    def test_executions_unavailable_when_ledger_not_wired(self):
        engine = ExecutionEngine(
            config=self.config, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        status, body = http_get(
            self.config, engine, "/private/v1/executions?correlationId=x", token=self.config.service_token,
        )
        self.assertEqual(503, status)
        self.assertEqual("EXECUTION_LEDGER_UNAVAILABLE", body["code"])

    # ---- /private/v1/reconciliation ----------------------------------------

    def test_reconciliation_returns_recent_runs_and_unresolved_flags(self):
        run_id = self.ledger.start_reconciliation_run("PERIODIC")
        self.ledger.complete_reconciliation_run(run_id, executions_ingested=3, unresolved_after=0)
        status, body = http_get(self.config, self.engine, "/private/v1/reconciliation", token=self.config.service_token)
        self.assertEqual(200, status)
        self.assertEqual("OK", body["status"])
        self.assertEqual("OK", body["recentRunsStatus"])
        self.assertTrue(any(run["run_id"] == run_id for run in body["recentRuns"]))
        self.assertFalse(body["unresolved"]["hasUnresolvedSubmission"])
        self.assertFalse(body["unresolved"]["hasUnresolvedProtection"])
        self.assertFalse(body["unresolved"]["hasUnresolvedTransition"])
        self.assertEqual(0, body["unresolved"]["closeSubmissionUnknownCount"])
        self.assertEqual(0, body["unresolved"]["protectionCancelUnknownCount"])

    def test_reconciliation_reflects_an_unresolved_open_submission(self):
        self.registry.claim("tradingview:stuck", 1, "hash-stuck")
        self.registry.finish(
            "tradingview:stuck", status="SUBMISSION_UNKNOWN", result={"status": "SUBMISSION_UNKNOWN"},
            account="DU12345", action="OPEN_LONG_CALL", contract=None,
        )
        status, body = http_get(self.config, self.engine, "/private/v1/reconciliation", token=self.config.service_token)
        self.assertEqual(200, status)
        self.assertTrue(body["unresolved"]["hasUnresolvedSubmission"])

    # Regression coverage for the transitions.py restart-crash-window fix:
    # a transition stuck FAILED_UNKNOWN (e.g. by the restart sweep) must
    # surface through the same reconciliation "why is this blocked" payload
    # the operator UI reads, exactly like an unresolved protection outcome.
    def test_reconciliation_reflects_an_unresolved_transition(self):
        self.transition_ledger.ensure_pending(
            "manual:stuck-transition:TP1", correlation_id="manual:stuck-transition",
            after="TP1", action="MOVE_STOP_TO_BREAKEVEN",
        )
        self.assertTrue(self.transition_ledger.mark_applying("manual:stuck-transition:TP1"))
        # Restart sweep: a fresh TransitionLedger over the same connection
        # resolves the stuck row to FAILED_UNKNOWN.
        restarted_transition_ledger = TransitionLedger(self.registry.connection, self.registry.lock)
        engine = ExecutionEngine(
            config=self.config, transport=self.transport, registry=self.registry,
            ledger=self.ledger, protection_ledger=self.protection_ledger,
            transition_ledger=restarted_transition_ledger, clock=lambda: NOW,
        )
        status, body = http_get(self.config, engine, "/private/v1/reconciliation", token=self.config.service_token)
        self.assertEqual(200, status)
        self.assertTrue(body["unresolved"]["hasUnresolvedTransition"])
        # Nothing else was ever touched -- this flag is independently true.
        self.assertFalse(body["unresolved"]["hasUnresolvedProtection"])

    def test_reconciliation_protection_flags_are_null_and_runs_unavailable_when_ledgers_not_wired(self):
        engine = ExecutionEngine(
            config=self.config, transport=self.transport, registry=self.registry, clock=lambda: NOW
        )
        status, body = http_get(self.config, engine, "/private/v1/reconciliation", token=self.config.service_token)
        self.assertEqual(200, status)
        self.assertEqual([], body["recentRuns"])
        self.assertEqual("UNAVAILABLE", body["recentRunsStatus"])
        self.assertIsNone(body["unresolved"]["hasUnresolvedProtection"])
        self.assertIsNone(body["unresolved"]["hasUnresolvedTransition"])
        self.assertIsNone(body["unresolved"]["protectionCancelUnknownCount"])
        # The registry-backed flag is always available (registry is never optional).
        self.assertFalse(body["unresolved"]["hasUnresolvedSubmission"])


if __name__ == "__main__":
    unittest.main()
