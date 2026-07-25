"""Authenticated loopback HTTP boundary used by the Node intent processor."""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import CoreConfig
from .engine import ExecutionBlocked, ExecutionEngine

MAX_BODY_BYTES = 64 * 1024

# Read-only GET routes (Phase 6): handlers never place, modify, or cancel an
# order. The position route does perform a fresh selected-account IBKR read so
# cached execution-ledger rows are never mislabeled as current broker truth.
# They exist solely so the Node/operator UI can render broker-authoritative
# position, protection, execution, and reconciliation state without inventing
# a second source of truth. Optional ledgers (protection_ledger/
# transition_ledger/ledger) mirror ExecutionEngine's own optionality: a
# caller that never wires one (see ExecutionEngine.__init__) makes the
# corresponding read honestly UNAVAILABLE rather than a fabricated empty
# result -- "unavailable" and "confirmed empty" must never be conflated here,
# exactly like the position/protection UI itself must never conflate them.
READ_ROUTES = frozenset(
    {
        "/private/v1/positions",
        "/private/v1/protection",
        "/private/v1/executions",
        "/private/v1/reconciliation",
    }
)


class CoreHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: CoreConfig, engine: ExecutionEngine):
        config.validate()
        self.config = config
        self.engine = engine
        super().__init__((config.http_host, config.http_port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: CoreHttpServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            self._json(200, {"status": "UP"})
            return
        if path == "/healthz":
            if not self._authorized():
                self._json(401, {"ready": False, "code": "CORE_AUTHENTICATION_FAILED"})
                return
            health = self.server.engine.health()
            self._json(200, health)
            return
        if path in READ_ROUTES:
            if not self._authorized():
                self._json(401, {"status": "BLOCKED", "code": "CORE_AUTHENTICATION_FAILED"})
                return
            query = parse_qs(parsed.query)
            correlation_id = (query.get("correlationId") or [None])[0] or None
            if path == "/private/v1/positions":
                self._get_positions(correlation_id)
            elif path == "/private/v1/protection":
                self._get_protection(correlation_id)
            elif path == "/private/v1/executions":
                self._get_executions(correlation_id)
            else:
                self._get_reconciliation()
            return
        self._json(404, {"status": "NOT_FOUND"})
        return

    # ---- read-only GET handlers (Phase 6) --------------------------------

    def _get_positions(self, correlation_id: str | None) -> None:
        engine = self.server.engine
        if engine.ledger is None:
            self._json(503, {"status": "UNAVAILABLE", "code": "EXECUTION_LEDGER_UNAVAILABLE"})
            return
        try:
            items = engine.operator_positions(correlation_id)
        except ExecutionBlocked as error:
            self._json(503, {"status": "UNAVAILABLE", "code": error.code})
            return
        except Exception:
            self._json(503, {"status": "UNAVAILABLE", "code": "BROKER_POSITION_READ_UNAVAILABLE"})
            return
        self._json(200, {"status": "OK", "items": items})

    def _get_protection(self, correlation_id: str | None) -> None:
        if not correlation_id:
            self._json(400, {"status": "BLOCKED", "code": "CORRELATION_ID_REQUIRED"})
            return
        engine = self.server.engine
        if engine.protection_ledger is None or engine.transition_ledger is None:
            self._json(503, {"status": "UNAVAILABLE", "code": "PROTECTION_LEDGER_UNAVAILABLE"})
            return
        legs = engine.protection_ledger.legs_for_correlation(correlation_id)
        transitions = engine.transition_ledger.for_correlation(correlation_id)
        self._json(
            200,
            {
                "status": "OK",
                "correlationId": correlation_id,
                "protectionLegs": legs,
                "transitions": transitions,
            },
        )

    def _get_executions(self, correlation_id: str | None) -> None:
        if not correlation_id:
            self._json(400, {"status": "BLOCKED", "code": "CORRELATION_ID_REQUIRED"})
            return
        engine = self.server.engine
        if engine.ledger is None:
            self._json(503, {"status": "UNAVAILABLE", "code": "EXECUTION_LEDGER_UNAVAILABLE"})
            return
        self._json(
            200,
            {
                "status": "OK",
                "correlationId": correlation_id,
                "executions": engine.ledger.executions_for_correlation(correlation_id),
                "commissions": engine.ledger.commissions_for_correlation(correlation_id),
            },
        )

    def _get_reconciliation(self) -> None:
        engine = self.server.engine
        ledger_available = engine.ledger is not None
        recent_runs = engine.ledger.recent_reconciliation_runs() if ledger_available else []
        protection_available = engine.protection_ledger is not None
        transition_available = engine.transition_ledger is not None
        self._json(
            200,
            {
                "status": "OK",
                "recentRuns": recent_runs,
                "recentRunsStatus": "OK" if ledger_available else "UNAVAILABLE",
                "unresolved": {
                    "hasUnresolvedSubmission": engine.registry.has_unresolved_unknown(),
                    "hasUnresolvedProtection": (
                        engine.protection_ledger.has_unresolved_unknown() if protection_available else None
                    ),
                    # Mirrors hasUnresolvedProtection: null (never a fabricated
                    # false) when transition_ledger isn't wired -- otherwise
                    # true when a management transition (e.g.
                    # MOVE_STOP_TO_BREAKEVEN) landed FAILED_UNKNOWN, including
                    # one that only ever reached mark_applying() before a
                    # crash -- see ExecutionEngine._verify_readiness's
                    # UNRESOLVED_TRANSITION_FAILURE block.
                    "hasUnresolvedTransition": (
                        engine.transition_ledger.has_unresolved_unknown() if transition_available else None
                    ),
                    "closeSubmissionUnknownCount": len(engine.registry.unresolved_close_submissions()),
                    "protectionCancelUnknownCount": (
                        len(engine.protection_ledger.unresolved_cancel_unknown_legs())
                        if protection_available
                        else None
                    ),
                },
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {
            "/private/v1/preview-trade",
            "/private/v1/place-trade",
            "/private/v1/close-trade",
        }:
            self._json(404, {"status": "NOT_FOUND"})
            return
        if not self._authorized():
            self._json(401, {"status": "BLOCKED", "code": "CORE_AUTHENTICATION_FAILED"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"status": "BLOCKED", "code": "CONTENT_TYPE_REQUIRED"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"status": "BLOCKED", "code": "BODY_SIZE_INVALID"})
            return
        raw = self.rfile.read(length)
        try:
            request: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"status": "BLOCKED", "code": "INVALID_JSON"})
            return
        # Defense in depth (the engine's own _parse_request validates the
        # full signal shape regardless): a mis-routed request never reaches
        # the wrong broker side effect just because it landed on the wrong
        # path -- place-trade only ever opens, close-trade only ever
        # closes/flattens. Two distinct, separately-invokable operator
        # actions stay distinct at the transport boundary too.
        action = _signal_action(request)
        if self.path == "/private/v1/close-trade" and not (isinstance(action, str) and action.startswith("CLOSE_")):
            self._json(400, {"status": "BLOCKED", "code": "CLOSE_TRADE_REQUIRES_CLOSE_ACTION"})
            return
        if self.path == "/private/v1/place-trade" and not (isinstance(action, str) and action.startswith("OPEN_")):
            self._json(400, {"status": "BLOCKED", "code": "PLACE_TRADE_REQUIRES_OPEN_ACTION"})
            return
        try:
            result = (
                self.server.engine.preview(request)
                if self.path == "/private/v1/preview-trade"
                else self.server.engine.execute(request)
            )
        except ExecutionBlocked as error:
            self._json(400, {"status": "BLOCKED", "code": error.code, "message": str(error)})
            return
        except Exception:
            # This is pre-contract failure.  The caller still treats an
            # unclassified service failure conservatively and never retries.
            self._json(503, {"status": "SUBMISSION_UNKNOWN", "code": "CORE_OUTCOME_UNAVAILABLE"})
            return
        # Keep an explicit unknown outcome on 200 so the caller consumes the
        # durable ambiguity code rather than mistaking it for an HTTP failure.
        self._json(200, result.body, close=result.ambiguous)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.config.service_token}"
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _json(self, status: int, body: dict[str, Any], *, close: bool = False) -> None:
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Avoid accidentally logging authorization or trading payload data.
        return


def _signal_action(request: Any) -> Any:
    """Best-effort, non-raising extraction of request["signal"]["action"] for
    the place-trade/close-trade action-family guard above. Any malformed
    shape here is deliberately left for the engine's own full _parse_request
    validation to reject with a precise code -- this helper only ever needs
    to know "is it unambiguously OPEN_/CLOSE_", never validate the rest."""
    if not isinstance(request, dict):
        return None
    signal = request.get("signal")
    if not isinstance(signal, dict):
        return None
    return signal.get("action")
