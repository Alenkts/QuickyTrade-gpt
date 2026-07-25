# Requirement traceability — TradingView slice

| Requirement | Current evidence | Status |
|---|---|---|
| SIG-002, WEB-003 | Strict versioned signal schema and action-aware validation in `src/tradingview/validation.js` | Implemented |
| SIG-003, WEB-005 | SQLite unique source/alert ID, canonical hash conflict, reopen tests | Implemented |
| WEB-001, WEB-002 | Loopback service; documented TLS edge; constant-time HMAC, bearer, or stripped body token | Implemented for local service; TLS edge operator-owned |
| WEB-004, WEB-007 | Auth, body bound, timestamp/skew, strict fields, symbol/strategy allowlists before actionable persistence, durable redacted rejection events, per-socket rate limit, and concurrency bound | Partial: local controls implemented; JSON-depth/global limits and external TLS-edge controls remain |
| WEB-006 | Commit then return `202`; broker adapter not called from ingress | Implemented |
| WEB-009 | Public webhook plus minimal health only; private dashboard APIs stay loopback | Implemented by route boundary |
| ORD-001 | Durable intent, order attempt, and finalized broker-call evidence before adapter/`placeOrder` | Implemented |
| ORD-002 | Durable correlation/idempotency plus account/action/contract/side/quantity/limit/orderRef before `placeOrder`; submitted response captures broker/client/permanent/parent/OCA identifiers when IBKR supplies them | Implemented at the submission boundary; full callback projection remains deferred |
| ORD-003, NFR-REL-001 | Ambiguous/interrupted submission becomes `SUBMISSION_UNKNOWN`; no retry; any unresolved unknown blocks all further orders | Implemented with restart and follow-on block tests; automatic resolution remains deferred |
| ORD-004–006 | Paper TWS core qualifies the underlying and exact option, verifies a fresh live quote and market rule, then constructs a tick-valid capped marketable `DAY` limit | Implemented with a fake official-API transport; real TWS compatibility/soak remains required |
| ORD-008–022 | Fill and commission capture via `execDetails`/`commissionAndFeesReport` (`core/quickytrade_core/ibapi_transport.py`, `execution_ledger.py`); rebuildable `position_state` cache; `cancel_order` (broker-confirmed vs. ambiguous, mirroring the placement-ack pattern) used by `FULL_FLATTEN` to cancel protection legs before flattening | Partial: broker-authoritative fill/commission capture and reduce-only close/flatten cancellation implemented; full lifecycle/UI projections remain deferred |
| REC-001–010 | Startup + periodic reconciliation sweep (`OfficialIbapiTransport.reconcile`) resolves `SUBMISSION_UNKNOWN` via `reqExecutions`/`reqCompletedOrders` evidence; cross-day unattributed-position discrepancies are flagged, never auto-resolved | Partial: automatic resolution of ambiguous submissions and broker-truth execution/commission capture implemented; account-value/daily-risk reconciliation and protection-order reconciliation remain deferred |
| RSK-001–015 | Exact DU paper account, allowlists, pre-submit signal-age recheck, serialized quantity/premium/spread gates (quantity is either the pre-existing default of one contract or, when `capital_per_trade_dollars` is supplied, `floor(capital / (fresh option mid-price * contract multiplier))` — either way clamped to the deployment-configured `max_contracts_per_order` ceiling), durable duplicate-exposure reservation, and reduce-only `REDUCE_ONLY_PARTIAL`/`FULL_FLATTEN` (wired end to end through `POST /api/trades/:correlationId/close` and the operator dashboard) bounded by a freshly re-verified broker long quantity minus every working sell reservation | Partial: account-value/daily-risk ledger and broader reconciliation remain deferred |
| UI-001–009 | Persistent paper/capture status and alert/order correlation timeline | Implemented for signal/order-attempt slice; fill/protection views deferred |
| UI-001–003, SET-001–004 | Tauri/macOS shell, named Paper/Live profile preferences, persistent environment/host/port/client display, and truthful live/paper status | Partial: desktop shell, single Node profile authority, and truthful LIVE/PAPER environment surfacing (`overlayProfile` in `server.mjs`, `renderProfiles`/`renderManualDesk` in `app.js`) implemented; full profile-to-core reconnect/readiness orchestration remains deferred |
| SIG-001, ORD-001 | Manual and TradingView sources carry immutable ownership and management-policy snapshots into the shared core contract | Implemented: both manual submission (`MANUAL_EXECUTION_AVAILABLE`, human Review-then-Submit checkpoint) and TradingView app-managed auto-submission (`APP_MANAGEMENT_EXECUTION_AVAILABLE`, zero human review) are live in `server.mjs` |
| RSK-008, ORD-016 | Source-specific default management choice, versioned policy, increasing targets, and allocation totaling 100% | Contract/UI implemented; broker-native targets, stops, partial-fill resizing, and outcome analytics remain deferred |
| AUD-001–003 | Append-only signal/security events and structured secret/account redaction | Implemented for ingress/rejection/order-attempt events; full broker lifecycle audit remains deferred |
| CON-001, SET-002, ACC-001 | One profile-database owner lock; exact configured paper (DU) or, with explicit `QT_LIVE_TRADING_CONFIRMED` opt-in, live (U) account and matching allowlist; no first-account selection; mismatched account/port/session-environment combinations refused | Implemented by the core; legacy direct order routes disabled |
| LIVE-001 | Live trading opt-in gated on `QT_LIVE_TRADING_CONFIRMED`, a separate live account allowlist, standard live ports, and a two-sided session/config environment cross-check (`ExecutionEngine._verify_readiness`) — see `docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md`'s "Live trading" section | Implemented and tested (`core/tests/test_execution.py`); real TWS live-session compatibility remains unverified against a live session, same caveat as the rest of the ibapi transport |
| SEL-001–006 | Shared delta/premium target-range strike selection for both TradingView and manual entry, ported from the retired `odte-desk` app's dynamic strike search | Proposed only — see [0DTE strike-selection requirements](0DTE_STRIKE_SELECTION_REQUIREMENTS.md); not implemented |

## Automated test evidence

`test/tradingview/foundation.test.js` covers authentication, schema/timestamps,
body bounds, allowlists, security-event redaction, durable replay,
capture-only execution quarantine across restart and direct-processing attempts,
changed-payload conflict,
async separation, successful submission mapping, definitive block, ambiguous
unknown outcome, restart recovery, and secret/account redaction.
`core/tests/test_execution.py` covers DU paper-account enforcement, account
mismatch, broker-result mapping, listed-strike selection, marketable-limit
construction, stale quotes, pre-submit signal expiry, duplicate
exposure/idempotency, concurrent submission serialization, single-core profile
ownership, global unknown-state blocking, crash-surviving broker-call evidence,
automated-close blocking, immutable manual/TradingView management metadata,
management allocation validation, preview-without-side-effect, owner-only database mode, the Node/core HTTP contract,
quote callback completion, current official-API error callback compatibility, target-range strike selection
(premium/delta metrics, range fallback, no-eligible-strike blocking, manual/TradingView selection parity),
live-trading opt-in (confirmation-phrase gating, live account/port/allowlist validation, and the two-sided
session/config environment mismatch check in both directions), and capital-based dynamic sizing (floor-not-round
arithmetic including a non-evenly-divisible amount, clamping to both the default and an operator-raised
`max_contracts_per_order` ceiling, `INSUFFICIENT_CAPITAL_FOR_ONE_CONTRACT` blocking with zero broker calls,
backward-compatible fallback to the pre-existing default-quantity-of-one behavior when `capital_per_trade_dollars`
is absent, rejection of negative/zero/fat-fingered amounts, and the relaxed `max_contracts_per_order` config
validation), and the reconciled-vs-unresolved distinction end to end through `_verify_readiness()`
(`CONFIRMED_NO_FILL` unblocks both global readiness and symbol/right reservation, `CONFIRMED_FILLED` unblocks
global readiness only, and a still-unresolved row keeps blocking exactly as before).
`test/operator/operator-store.test.js` covers the connection-profile `capitalPerTradeDollars` field
(persistence, validation bounds, leaving an unrelated update untouched, explicit clearing) and the now-optional
manual-intent `quantity` field. `core/tests/test_execution_ledger.py` covers idempotent execution/commission
ingestion (duplicate `execId`/`exec_id` redelivery as a no-op), both commission/execution arrival orderings,
an execution with no matching `order_ref` yet (stays unattributed, then backfills), `has_unresolved_unknown`/
`has_blocking_open` respecting `reconciliation_outcome`, `position_state` rebuild fidelity after deletion
(including partial-fill lifecycle and `last_reconciled_at` only being stamped by a reconciliation-sourced
rebuild), the reconciliation-runs audit trail, order-ref-evidence-based auto-resolution (including that an
ambiguous `Inactive` completed-order status is never treated as definitive no-fill), the cross-day
unattributed-position fallback (flagged but never auto-resolved), and `realizedPNL` sentinel sanitization.
`core/tests/test_reduce_only_close.py` covers `REDUCE_ONLY_PARTIAL` (the fresh
verified-long-minus-working-exit bound, including protection legs and a
foreign/manual working sell in the reservation count; leaving existing
protection legs untouched; the same tick-valid fresh-quote SELL construction;
the hard spread block) and `FULL_FLATTEN` (cancelling every protection leg
first with durable cancel-intent evidence before each `cancel_order` call;
halting before the flattening sell with zero orders placed if any cancel is
ambiguous; re-querying fresh quantity once every cancel is confirmed;
warning-but-allowing on a wide spread; idempotent cancel-then-flatten), the
close-side `has_blocking_close` idempotency/crash-restart guard, the
un-gated-behind-fill-evidence readiness check, missing/stale verified-long or
working-exit evidence blocking rather than defaulting, and the
contract-scoped-vs-global ambiguity-blocking asymmetry (an unresolved
close-sell or protection-cancel ambiguity blocks further action on that exact
contract but not an unrelated new open) end to end, plus the
`/private/v1/close-trade` HTTP endpoint and its place-trade/close-trade
action-family guard.

The implementation is a safe TradingView capture foundation, a controlled
paper-submission adapter, and (as of the execution-ledger/reconciliation
addition) broker-authoritative fill/commission capture with automatic
resolution of already-ambiguous submissions, plus (as of the reduce-only
close/flatten addition) a core-side, evidence-gated reduce-only close and
protection-cancelling flatten wired end to end through
`POST /api/trades/:correlationId/close` and the operator dashboard's
per-position partial-close/flatten controls, plus (as of the
management-transitions addition) durable, once-per-fill
`MOVE_STOP_TO_BREAKEVEN`/`TRAIL_FRESH_BID` application with a global
readiness block (`UNRESOLVED_TRANSITION_FAILURE`) on any unresolved
transition outcome, including one stuck mid-application across a restart. It
is not evidence for unattended paper operation, live readiness, UI
fill/commission projection, or release Gates B/C — those remain deferred to
later phases.
