from __future__ import annotations

import math
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from quickytrade_core.domain import Position, QualifiedContract
from quickytrade_core.execution_ledger import CommissionRecord, ExecutionLedger, ExecutionRecord
from quickytrade_core.ibapi_transport import OfficialIbapiTransport, _sanitize_realized_pnl
from quickytrade_core.registry import SubmissionRegistry


def _bare_transport(ledger: ExecutionLedger, positions=()) -> OfficialIbapiTransport:
    """A transport instance with no real ibapi socket, exercising only the
    pure reconciliation-logic helpers (matches this suite's existing
    OfficialIbapiTransport.__new__ pattern for callback-level unit tests --
    see test_execution.py)."""
    transport = OfficialIbapiTransport.__new__(OfficialIbapiTransport)
    transport.ledger = ledger
    transport._positions = list(positions)
    return transport


class ExecutionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = SubmissionRegistry(Path(self.temp.name) / "state.sqlite3")
        self.ledger = ExecutionLedger(self.registry.connection, self.registry.lock)

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    # ---- fixtures ---------------------------------------------------------

    def _claim_and_evidence(
        self, correlation_id: str, *, order_ref: str, quantity: int = 5,
        account: str = "DU12345", con_id: int = 501, symbol: str = "QQQ",
    ) -> None:
        self.registry.claim(correlation_id, 1, "hash-" + correlation_id)
        self.registry.record_broker_call_evidence(
            correlation_id,
            account=account,
            action="OPEN_LONG_CALL",
            contract={"con_id": con_id, "symbol": symbol, "right": "C"},
            side="BUY",
            quantity=quantity,
            limit_price="1.05",
            order_ref=order_ref,
            entry_correlation_id=None,
        )

    def _mark_unknown(self, correlation_id: str) -> None:
        self.registry.finish(
            correlation_id, status="SUBMISSION_UNKNOWN", result={"status": "SUBMISSION_UNKNOWN"}
        )

    def _execution(
        self, exec_id: str, *, order_ref: str = "QTref1", shares: str = "5", price: str = "1.05",
        source: str = "LIVE_CALLBACK", con_id: int = 501, account: str = "DU12345", symbol: str = "QQQ",
    ) -> ExecutionRecord:
        return ExecutionRecord(
            exec_id=exec_id,
            order_ref=order_ref,
            order_id=700,
            perm_id=900,
            account=account,
            con_id=con_id,
            symbol=symbol,
            side="BOT",
            shares=shares,
            price=price,
            cum_qty=shares,
            avg_price=price,
            exec_time="20260720  10:00:00",
            source=source,
            raw={"execId": exec_id},
        )

    # ---- idempotency (REC / broker-truth capture) --------------------------

    def test_duplicate_exec_id_delivery_is_a_no_op_not_an_error(self):
        first = self.ledger.record_execution(self._execution("e1"))
        second = self.ledger.record_execution(self._execution("e1"))
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(1, len(self.ledger.executions_for_order_ref("QTref1")))

    def test_duplicate_commission_delivery_is_a_no_op_not_an_error(self):
        self.ledger.record_execution(self._execution("e1"))
        first = self.ledger.record_commission(CommissionRecord("e1", "1.00", "USD", None, {}))
        second = self.ledger.record_commission(CommissionRecord("e1", "1.00", "USD", None, {}))
        self.assertTrue(first)
        self.assertFalse(second)

    def test_record_execution_rejects_unsupported_source(self):
        with self.assertRaises(ValueError):
            self.ledger.record_execution(self._execution("e-bad-source", source="SOMETHING_ELSE"))

    # ---- commission/execution arrival ordering -----------------------------

    def test_commission_arriving_before_its_execution_is_picked_up_once_the_execution_arrives(self):
        self._claim_and_evidence("tradingview:c-before-e", order_ref="QTcbeforee")
        # Commission lands first; nothing to attribute it to yet.
        self.ledger.record_commission(CommissionRecord("e-early", "0.65", "USD", "12.34", {}))
        self.assertIsNone(self.ledger.position_state("tradingview:c-before-e"))
        self.ledger.record_execution(self._execution("e-early", order_ref="QTcbeforee"))
        state = self.ledger.position_state("tradingview:c-before-e")
        self.assertEqual("0.65", state["total_commission"])
        self.assertEqual("12.34", state["realized_pnl"])

    def test_execution_arriving_before_its_commission_still_totals_correctly_once_commission_lands(self):
        self._claim_and_evidence("tradingview:e-before-c", order_ref="QTebeforec")
        self.ledger.record_execution(self._execution("e-late", order_ref="QTebeforec"))
        state = self.ledger.position_state("tradingview:e-before-c")
        self.assertIsNone(state["total_commission"])
        self.ledger.record_commission(CommissionRecord("e-late", "0.65", "USD", None, {}))
        state = self.ledger.position_state("tradingview:e-before-c")
        self.assertEqual("0.65", state["total_commission"])
        self.assertIsNone(state["realized_pnl"])

    # ---- unmatched order_ref / lazy backfill -------------------------------

    def test_execution_with_no_matching_order_ref_yet_stays_unattributed_without_crashing(self):
        ok = self.ledger.record_execution(self._execution("e-orphan", order_ref="QTneverclaimed"))
        self.assertTrue(ok)
        row = self.ledger.get_execution("e-orphan")
        self.assertIsNone(row["correlation_id"])
        self.assertIsNone(self.ledger.position_state("tradingview:late-claim"))

    def test_backfill_resolves_correlation_id_once_the_entry_is_claimed(self):
        self.ledger.record_execution(self._execution("e-orphan2", order_ref="QTneverclaimed2"))
        self._claim_and_evidence("tradingview:late-claim2", order_ref="QTneverclaimed2")
        updated = self.ledger.backfill_missing_correlation_ids()
        self.assertEqual(1, updated)
        row = self.ledger.get_execution("e-orphan2")
        self.assertEqual("tradingview:late-claim2", row["correlation_id"])
        self.assertIsNotNone(self.ledger.position_state("tradingview:late-claim2"))

    # ---- has_unresolved_unknown / has_blocking_open ------------------------

    def test_has_unresolved_unknown_respects_reconciliation_outcome(self):
        self._claim_and_evidence("tradingview:unk1", order_ref="QTunk1", quantity=1)
        self._mark_unknown("tradingview:unk1")
        self.assertTrue(self.registry.has_unresolved_unknown())
        self.assertTrue(self.ledger.mark_reconciliation_outcome("tradingview:unk1", "CONFIRMED_NO_FILL"))
        self.assertFalse(self.registry.has_unresolved_unknown())

    def test_mark_reconciliation_outcome_is_a_no_op_once_already_resolved(self):
        self._claim_and_evidence("tradingview:unk2", order_ref="QTunk2", quantity=1)
        self._mark_unknown("tradingview:unk2")
        self.assertTrue(self.ledger.mark_reconciliation_outcome("tradingview:unk2", "CONFIRMED_FILLED"))
        self.assertFalse(self.ledger.mark_reconciliation_outcome("tradingview:unk2", "CONFIRMED_NO_FILL"))
        evidence = self.registry.submission_evidence("tradingview:unk2")
        self.assertEqual("CONFIRMED_FILLED", evidence["reconciliation_outcome"])

    def test_mark_reconciliation_outcome_rejects_unsupported_value(self):
        self._claim_and_evidence("tradingview:unk3", order_ref="QTunk3", quantity=1)
        self._mark_unknown("tradingview:unk3")
        with self.assertRaises(ValueError):
            self.ledger.mark_reconciliation_outcome("tradingview:unk3", "MAYBE")

    def test_has_blocking_open_confirmed_no_fill_stops_blocking(self):
        self._claim_and_evidence("tradingview:blk1", order_ref="QTblk1", quantity=1, con_id=501)
        self._mark_unknown("tradingview:blk1")
        self.assertTrue(self.registry.has_blocking_open("DU12345", "QQQ", "C"))
        self.ledger.mark_reconciliation_outcome("tradingview:blk1", "CONFIRMED_NO_FILL")
        self.assertFalse(self.registry.has_blocking_open("DU12345", "QQQ", "C"))

    def test_has_blocking_open_confirmed_filled_keeps_blocking(self):
        self._claim_and_evidence("tradingview:blk2", order_ref="QTblk2", quantity=1, con_id=502)
        self._mark_unknown("tradingview:blk2")
        self.ledger.mark_reconciliation_outcome("tradingview:blk2", "CONFIRMED_FILLED")
        self.assertTrue(self.registry.has_blocking_open("DU12345", "QQQ", "C"))

    # ---- position_state rebuild --------------------------------------------

    def test_position_state_is_correctly_rebuilt_from_raw_rows_after_deletion(self):
        self._claim_and_evidence("tradingview:rebuild1", order_ref="QTrebuild1", quantity=3)
        self.ledger.record_execution(self._execution("e-r1", order_ref="QTrebuild1", shares="2", price="1.00"))
        self.ledger.record_execution(self._execution("e-r2", order_ref="QTrebuild1", shares="1", price="1.30"))
        self.ledger.record_commission(CommissionRecord("e-r1", "0.65", "USD", None, {}))
        self.ledger.record_commission(CommissionRecord("e-r2", "0.65", "USD", None, {}))

        before = self.ledger.position_state("tradingview:rebuild1")
        self.assertEqual("3", before["opened_quantity"])
        self.assertEqual("0", before["closed_quantity"])
        self.assertEqual("3", before["open_quantity"])
        self.assertEqual("FILLED", before["lifecycle_status"])
        self.assertEqual(str(Decimal("3.30") / Decimal("3")), before["entry_avg_price"])
        self.assertEqual("1.30", before["total_commission"])

        with self.registry.lock:
            self.registry.connection.execute(
                "DELETE FROM position_state WHERE correlation_id=?", ("tradingview:rebuild1",)
            )
        self.assertIsNone(self.ledger.position_state("tradingview:rebuild1"))

        self.ledger.rebuild_position_state("tradingview:rebuild1")
        after = self.ledger.position_state("tradingview:rebuild1")
        self.assertEqual(before["opened_quantity"], after["opened_quantity"])
        self.assertEqual(before["entry_avg_price"], after["entry_avg_price"])
        self.assertEqual(before["total_commission"], after["total_commission"])
        self.assertEqual(before["lifecycle_status"], after["lifecycle_status"])

    def test_position_state_partial_fill_stays_partially_filled_below_requested_quantity(self):
        self._claim_and_evidence("tradingview:partial1", order_ref="QTpartial1", quantity=5)
        self.ledger.record_execution(self._execution("e-p1", order_ref="QTpartial1", shares="2", price="1.00"))
        state = self.ledger.position_state("tradingview:partial1")
        self.assertEqual("PARTIALLY_FILLED", state["lifecycle_status"])
        self.assertEqual("2", state["open_quantity"])

    def test_closed_position_uses_latest_broker_sell_execution_time_not_cache_update_time(self):
        self._claim_and_evidence("tradingview:closed-time", order_ref="QTclosedtime", quantity=1)
        self.ledger.record_execution(self._execution("e-open", order_ref="QTclosedtime", shares="1", price="1.00"))
        self.ledger.record_execution(ExecutionRecord(
            exec_id="e-close", order_ref="QTclosedtime", order_id=701, perm_id=901,
            account="DU12345", con_id=501, symbol="QQQ", side="SLD", shares="1", price="1.20",
            cum_qty="1", avg_price="1.20", exec_time="2026-07-20T15:31:00-04:00", source="LIVE_CALLBACK", raw={},
        ))
        state = self.ledger.position_state("tradingview:closed-time")
        self.assertEqual("CLOSED", state["lifecycle_status"])
        self.assertEqual("2026-07-20T19:31:00Z", state["closed_at"])

    def test_rebuild_position_state_marks_last_reconciled_at_only_when_sweep_sourced(self):
        self._claim_and_evidence("tradingview:mark1", order_ref="QTmark1", quantity=1)
        self.ledger.record_execution(self._execution("e-live", order_ref="QTmark1", source="LIVE_CALLBACK"))
        live_state = self.ledger.position_state("tradingview:mark1")
        self.assertIsNone(live_state["last_reconciled_at"])

        self.ledger.rebuild_position_state("tradingview:mark1", mark_reconciled=True)
        reconciled_state = self.ledger.position_state("tradingview:mark1")
        self.assertIsNotNone(reconciled_state["last_reconciled_at"])

        # A later plain live-callback rebuild must not erase the stamp.
        self.ledger.record_execution(
            self._execution("e-live-2", order_ref="QTmark1", source="LIVE_CALLBACK")
        )
        still_reconciled = self.ledger.position_state("tradingview:mark1")
        self.assertEqual(reconciled_state["last_reconciled_at"], still_reconciled["last_reconciled_at"])

    # ---- reconciliation_runs audit trail ------------------------------------

    def test_reconciliation_run_start_and_complete_round_trips(self):
        run_id = self.ledger.start_reconciliation_run("STARTUP")
        run = self.ledger.get_reconciliation_run(run_id)
        self.assertEqual("STARTUP", run["trigger"])
        self.assertIsNone(run["completed_at"])
        self.ledger.complete_reconciliation_run(run_id, executions_ingested=3, unresolved_after=1, notes="n")
        run = self.ledger.get_reconciliation_run(run_id)
        self.assertIsNotNone(run["completed_at"])
        self.assertEqual(3, run["executions_ingested"])
        self.assertEqual(1, run["unresolved_after"])
        self.assertEqual("n", run["notes"])

    def test_reconciliation_run_rejects_unsupported_trigger(self):
        with self.assertRaises(ValueError):
            self.ledger.start_reconciliation_run("MANUAL")

    # ---- cross-day fallback: unattributed broker position -------------------

    def test_cross_day_fallback_flags_but_does_not_resolve_an_unattributed_position(self):
        self._claim_and_evidence("tradingview:unattributed1", order_ref="QTunattr1", con_id=777)
        self._mark_unknown("tradingview:unattributed1")
        contract = QualifiedContract(con_id=777, symbol="QQQ", sec_type="OPT", exchange="SMART", currency="USD")
        transport = _bare_transport(
            self.ledger, positions=[Position(account="DU12345", contract=contract, quantity=Decimal("1"))]
        )
        flagged = transport._flag_unattributed_positions()
        self.assertEqual(1, len(flagged))
        self.assertEqual("tradingview:unattributed1", flagged[0]["correlation_id"])
        self.assertEqual(777, flagged[0]["con_id"])
        # Never auto-resolved either way.
        self.assertTrue(self.registry.has_unresolved_unknown())
        evidence = self.registry.submission_evidence("tradingview:unattributed1")
        self.assertIsNone(evidence["reconciliation_outcome"])

    def test_cross_day_fallback_does_not_flag_when_execution_evidence_exists_for_the_con_id(self):
        self._claim_and_evidence("tradingview:attributed1", order_ref="QTattr1", con_id=778)
        self._mark_unknown("tradingview:attributed1")
        self.ledger.record_execution(self._execution("e-attr", order_ref="QTattr1", con_id=778))
        contract = QualifiedContract(con_id=778, symbol="QQQ", sec_type="OPT", exchange="SMART", currency="USD")
        transport = _bare_transport(
            self.ledger, positions=[Position(account="DU12345", contract=contract, quantity=Decimal("1"))]
        )
        self.assertEqual([], transport._flag_unattributed_positions())

    def test_cross_day_fallback_does_not_flag_when_the_broker_position_is_flat(self):
        self._claim_and_evidence("tradingview:flat1", order_ref="QTflat1", con_id=779)
        self._mark_unknown("tradingview:flat1")
        contract = QualifiedContract(con_id=779, symbol="QQQ", sec_type="OPT", exchange="SMART", currency="USD")
        transport = _bare_transport(
            self.ledger, positions=[Position(account="DU12345", contract=contract, quantity=Decimal("0"))]
        )
        self.assertEqual([], transport._flag_unattributed_positions())

    # ---- order-ref-evidence-based auto-resolution ---------------------------

    def test_resolve_unknown_submissions_marks_confirmed_filled_when_execution_evidence_exists(self):
        self._claim_and_evidence("tradingview:resolve-filled", order_ref="QTresolvefilled")
        self._mark_unknown("tradingview:resolve-filled")
        self.ledger.record_execution(self._execution("e-resolve-filled", order_ref="QTresolvefilled"))
        transport = _bare_transport(self.ledger)
        transport._resolve_unknown_submissions({})
        evidence = self.registry.submission_evidence("tradingview:resolve-filled")
        self.assertEqual("CONFIRMED_FILLED", evidence["reconciliation_outcome"])

    def test_resolve_unknown_submissions_marks_confirmed_no_fill_from_cancelled_completed_order(self):
        self._claim_and_evidence("tradingview:resolve-no-fill", order_ref="QTresolvenofill")
        self._mark_unknown("tradingview:resolve-no-fill")
        transport = _bare_transport(self.ledger)
        transport._resolve_unknown_submissions({"QTresolvenofill": {"status": "Cancelled", "con_id": 501}})
        evidence = self.registry.submission_evidence("tradingview:resolve-no-fill")
        self.assertEqual("CONFIRMED_NO_FILL", evidence["reconciliation_outcome"])

    def test_resolve_unknown_submissions_leaves_unresolved_without_evidence_either_way(self):
        self._claim_and_evidence("tradingview:resolve-neither", order_ref="QTresolveneither")
        self._mark_unknown("tradingview:resolve-neither")
        transport = _bare_transport(self.ledger)
        transport._resolve_unknown_submissions({})
        evidence = self.registry.submission_evidence("tradingview:resolve-neither")
        self.assertIsNone(evidence["reconciliation_outcome"])

    def test_resolve_unknown_submissions_does_not_resolve_no_fill_from_an_ambiguous_inactive_status(self):
        # 'Inactive' can follow a partial fill; it must never be treated as
        # definitive "no fill" evidence without actual execution rows.
        self._claim_and_evidence("tradingview:resolve-inactive", order_ref="QTresolveinactive")
        self._mark_unknown("tradingview:resolve-inactive")
        transport = _bare_transport(self.ledger)
        transport._resolve_unknown_submissions({"QTresolveinactive": {"status": "Inactive", "con_id": 501}})
        evidence = self.registry.submission_evidence("tradingview:resolve-inactive")
        self.assertIsNone(evidence["reconciliation_outcome"])

    # ---- realized PnL sentinel sanitization ---------------------------------

    def test_sanitize_realized_pnl_converts_sentinel_and_nan_to_none(self):
        self.assertIsNone(_sanitize_realized_pnl(sys.float_info.max))
        self.assertIsNone(_sanitize_realized_pnl(float("nan")))
        self.assertIsNone(_sanitize_realized_pnl(math.inf))
        self.assertIsNone(_sanitize_realized_pnl(None))
        self.assertEqual("12.34", _sanitize_realized_pnl(12.34))


if __name__ == "__main__":
    unittest.main()
