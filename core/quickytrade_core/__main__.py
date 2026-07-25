"""Run the long-lived loopback TWS core service."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from .config import CoreConfig
from .engine import ExecutionEngine
from .execution_ledger import ExecutionLedger
from .http_service import CoreHttpServer
from .ibapi_transport import OfficialIbapiTransport
from .protection import ProtectionLedger
from .registry import SubmissionRegistry
from .transitions import TransitionLedger

logger = logging.getLogger(__name__)


def _load_env_file(path: Path) -> None:
    """Load project-local `.env` (gitignored) into os.environ for keys that
    aren't already set, so a real shell export always takes precedence. No
    python-dotenv dependency — this package deliberately stays stdlib-only.
    """
    try:
        content = path.read_text()
    except OSError:
        return
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[0] == value[-1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _run_periodic_reconciliation(
    transport: OfficialIbapiTransport,
    engine: ExecutionEngine,
    ledger: ExecutionLedger,
    stopping: threading.Event,
    interval_seconds: float,
) -> None:
    """Background reconciliation + protection + transition sweep, on the same
    interval and the same thread (Phase 3/4 extend Phase 2's thread rather
    than adding new ones). Reconciliation is data capture + resolution only,
    as before. Protection placement and transition application are both
    level-triggered: every FILLED + APP_MANAGED correlation_id is
    re-evaluated every sweep (see ExecutionEngine.ensure_protection /
    ensure_transitions), so a fill landing between two sweeps, or across a
    restart, is still noticed and acted on the next pass -- never dependent
    on catching a one-shot fill callback. ensure_transitions() always runs
    after ensure_protection() for the same correlation_id within the same
    pass, so any top-up leg ensure_protection() just placed is immediately
    visible to that correlation_id's transition evaluation. Uses
    `stopping.wait` (not `time.sleep`) so shutdown is prompt rather than
    waiting out a full interval."""
    while not stopping.wait(interval_seconds):
        try:
            transport.reconcile("PERIODIC")
        except Exception:
            logger.exception("Periodic reconciliation sweep failed; will retry next interval")
        try:
            candidates = ledger.filled_app_managed_correlation_ids()
        except Exception:
            logger.exception("Listing protection/transition candidates failed; will retry next interval")
            candidates = []
        for correlation_id in candidates:
            try:
                engine.ensure_protection(correlation_id)
            except Exception:
                logger.exception(
                    "Protection sweep failed for correlation_id %s; will retry next interval", correlation_id
                )
            try:
                engine.ensure_transitions(correlation_id)
            except Exception:
                logger.exception(
                    "Transition sweep failed for correlation_id %s; will retry next interval", correlation_id
                )


def main() -> None:
    _load_env_file(Path(__file__).resolve().parent.parent / ".env")
    config = CoreConfig.from_environment()
    registry = SubmissionRegistry(config.state_db_path)
    ledger = ExecutionLedger(registry.connection, registry.lock)
    # Sweeps any broker_protection_orders row stuck SUBMITTING (or, Phase 4,
    # stuck mid-modify) from a prior crash to SUBMISSION_UNKNOWN/
    # MODIFY_UNKNOWN on construction -- identical restart-recovery pattern to
    # SubmissionRegistry/ExecutionLedger above.
    protection_ledger = ProtectionLedger(registry.connection, registry.lock)
    # Sweeps any management_transitions row stuck APPLYING from a prior crash
    # to FAILED_UNKNOWN on construction -- same restart-recovery pattern.
    transition_ledger = TransitionLedger(registry.connection, registry.lock)
    transport = OfficialIbapiTransport(config, ledger)
    server: CoreHttpServer | None = None
    stopping = threading.Event()
    reconciliation_thread: threading.Thread | None = None
    try:
        transport.start()
        engine = ExecutionEngine(
            config=config,
            transport=transport,
            registry=registry,
            ledger=ledger,
            protection_ledger=protection_ledger,
            transition_ledger=transition_ledger,
        )
        server = CoreHttpServer(config, engine)

        def stop(signum, frame) -> None:  # noqa: ARG001
            stopping.set()
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        reconciliation_thread = threading.Thread(
            target=_run_periodic_reconciliation,
            args=(transport, engine, ledger, stopping, config.reconciliation_interval_seconds),
            name="quickytrade-reconciliation",
            daemon=True,
        )
        reconciliation_thread.start()

        server.serve_forever(poll_interval=0.25)
    finally:
        stopping.set()
        if reconciliation_thread is not None:
            reconciliation_thread.join(timeout=5)
        if server is not None:
            server.server_close()
        transport.stop()
        registry.close()


if __name__ == "__main__":
    main()
