# IBKR TradingView implementation

## Delivered scope

This slice covers IBKR only and treats TradingView as an untrusted signal
source. It excludes Schwab and any built-in or Python signal-generation
strategy.

The Tauri/macOS operator surface also accepts durable manual proposals.
Manual and TradingView intents snapshot an operator-selected management mode.
`APP_MANAGED` is the default; `USER_MANAGED` and `TRADINGVIEW_MANAGED` are
distinct app-level choices. The latter two are quarantined from the broker
boundary. Protection management (stop-loss/take-profit legs and lifecycle
transitions) is implemented and runs unconditionally on app-managed positions,
so app-managed submission -- including TradingView-sourced alerts -- is live
and auto-submits with zero human review once the trading mode and core are
ready (see `APP_MANAGEMENT_EXECUTION_AVAILABLE` in `server.mjs`). This is
gated only by config-level checks (freshness, spread, allowlists, quantity,
paper/live account validation), not by a human-in-the-loop review step.

The implemented ingress pipeline is:

```text
TradingView -> TLS edge -> authenticated loopback webhook
            -> schema and timestamp validation
            -> SQLite durable dedupe and correlation
            -> HTTP 202 RECEIVED
            -> asynchronous processor
            -> official IBKR TWS paper core or capture-only state
            -> app alert and order timeline
```

IBKR remains authoritative for qualified contracts, quotes, orders,
executions, commissions, positions, and account values. Local SQLite is a
durable intent and audit ledger, not a substitute for broker reconciliation.

## Safety invariants

1. The signal is committed before the webhook returns and before any broker
   call.
2. `(source, alert_id)` is unique. An identical replay returns the original
   correlation. The same ID with a changed semantic payload returns
   `ALERT_ID_CONFLICT`.
3. The HTTP handler never places an order. Processing occurs after the `202`
   response is ready.
4. Exactly one durable order attempt is allowed per intent.
5. An interrupted or ambiguous submission becomes `SUBMISSION_UNKNOWN` and is
   never automatically retried.
   Final account, action, contract, side, quantity, limit, and `orderRef`
   evidence is committed immediately before the socket call.
6. Paper execution requires an exact account allowlist and the official TWS
   core. Live execution is additionally possible only for an explicitly
   confirmed account (`QT_LIVE_TRADING_CONFIRMED` set to the exact
   confirmation phrase, a live `U...` account in a separate live allowlist, on
   the standard IBKR live ports) — the core refuses to trade at all if the
   connected session's own reported environment doesn't match the configured
   account type.
7. Market orders, fabricated prices, automatic account selection, short
   options, spreads, and modification of manual/external orders are disabled.
8. Close alerts are captured and durably recorded. Automated close/flatten
   submission (`REDUCE_ONLY_PARTIAL`/`FULL_FLATTEN`, `core/quickytrade_core/
   engine.py`) is now gated behind broker-authoritative fill evidence (the
   execution ledger's `position_state`, Phase 2) rather than unconditionally
   disabled: a close is only reachable once the referenced entry is
   `FILLED`/`CLOSING` with a positive broker-confirmed `open_quantity`. The
   bound is the freshly re-verified broker long quantity minus every working
   sell reservation (this app's own working protection legs plus any
   foreign/manual working sell) — never a cached number. `FULL_FLATTEN`
   additionally cancels every working protection leg first (with durable
   cancel-intent evidence before each `cancel_order` call) and halts before
   the flattening sell if any cancel outcome is ambiguous; an unresolved
   close-sell or protection-cancel ambiguity blocks further close/flatten
   action on that exact contract only, not globally. `server.mjs` proxies
   this as `POST /api/trades/:correlationId/close` (looking up the
   originating alert by the core's UUID `correlation_id`, a distinct
   identifier from the Node-side `source:alertId` idempotencyKey — see
   `lookupOriginatingAlert`/`getAlertStatusByCorrelationId`) and the operator
   dashboard (`app.js`/`index.html`) wires it to per-position partial-close
   and flatten-with-confirmation controls, using a client-generated stable
   `requestId` so a retried click cannot submit a second order.
9. The legacy native Python bridge is retired; only the long-lived `core/`
   service may own the supported TWS connection and submit an order. A profile
   database lock rejects a second core process.
10. Management-policy transitions (`MOVE_STOP_TO_BREAKEVEN`/`TRAIL_FRESH_BID`,
    `core/quickytrade_core/transitions.py`) apply once per take-profit fill
    (deterministic `transition_id` primary key) and are, like protection-order
    submission and entry submission, never automatically retried once
    ambiguous. `ExecutionEngine._verify_readiness()` globally blocks every new
    open while any transition is `FAILED_UNKNOWN` (`transitions.py`'s
    `TransitionLedger.has_unresolved_unknown()`) — including a transition that
    only ever reached `mark_applying()` before a crash, with no broker call
    yet attempted, which the restart sweep also resolves to `FAILED_UNKNOWN`
    rather than leaving it silently stuck forever. This mirrors the existing
    `SubmissionRegistry`/`ProtectionLedger` unresolved-unknown global blocks.

## Supported signal actions

- `OPEN_LONG_CALL`
- `OPEN_LONG_PUT`
- `CLOSE_LONG_CALL_REDUCE_ONLY_PARTIAL` / `CLOSE_LONG_PUT_REDUCE_ONLY_PARTIAL` —
  a bounded partial-or-full close (an explicit positive integer `quantity`
  is required) that never touches existing protection orders.
- `CLOSE_LONG_CALL_FULL_FLATTEN` / `CLOSE_LONG_PUT_FULL_FLATTEN` — cancels
  every working protection leg on the entry first, then sells the entire
  freshly re-verified remaining quantity. A deliberately separate, more
  consequential action from `REDUCE_ONLY_PARTIAL`; `quantity` is not
  accepted (flatten always means everything remaining). Unlike every other
  order in this codebase, a wide bid/ask spread on the flattening sell warns
  rather than hard-blocks — an explicit product decision so the operator is
  never trapped in a now-unprotected position after deliberately cancelling
  protection.

An open signal provides a contract-selection policy and optional upper sizing
hint; local broker/risk evidence chooses the final contract, price, and size.
Quantity itself is broker/risk evidence, not client-supplied: when an open
signal (or the manual entry's connection profile) supplies
`capital_per_trade_dollars`, the core computes
`floor(capital_per_trade_dollars / (option_fresh_mid_price *
contract_multiplier))` — one contract represents `contract_multiplier` shares
(typically 100 for a standard equity/ETF option), so the per-contract cost is
the per-share mid-price times that multiplier, not the mid-price alone —
always rounding down and never fabricating a price; a client-supplied
`quantity` field is ignored in that case. Absent `capital_per_trade_dollars`,
quantity keeps the pre-existing default-of-one behavior for backward
compatibility. Either way, the computed quantity is clamped to the
deployment-configured, restart-only
`max_contracts_per_order` ceiling, and an amount too small to cover even one
contract blocks with `INSUFFICIENT_CAPITAL_FOR_ONE_CONTRACT` rather than
silently rounding up. A close signal must reference `entry_alert_id` or
`trade_ref`, but this slice records and blocks it rather than risking an
arbitrary/manual position.

## State model

Ingress states:

```text
READY -> PROCESSING -> SUBMITTED
                    -> BLOCKED
                    -> FAILED
                    -> SUBMISSION_UNKNOWN
```

The app labels `READY` as received/queued. `SUBMITTED` means the paper core
observed an IBKR acknowledgement; it does not mean filled. Fill, commission,
position, and reconciliation projections remain required before any future live
claim.

## Runtime modes

| Mode | Broker side effects | Purpose |
|---|---|---|
| `capture_only` | None | Validate TradingView delivery, authentication, replay behavior, and UI tracking. |
| `paper_tws` | Real broker side effects (paper or live, per the connected core's account config) | Readiness, qualification development, durable intent capture, and live managed entry submission (manual and app-managed/TradingView). |

`capture_only` is the default. `paper_tws` mode does not by itself guarantee
paper-only execution — despite its name, it means "trading enabled." It
delegates entirely to the Python core's own account configuration — the core
decides paper vs. live based on the exact account identifier it was started
with (see the "Live trading" section below); no UI action or Node-side setting can flip this on
by itself. Capture-only rows are durably marked execution-ineligible and
cannot drain after a later restart in `paper_tws` mode.

## Live trading

Live execution is available, but only through explicit, non-automatable
operator configuration of the Python core process — never through a UI
toggle or a code path that could silently flip an account from paper to
live. To run against a real IBKR account: set `selected_account`/
`QT_IBKR_LIVE_ACCOUNT` to the live account identifier (`U...`), set
`QT_LIVE_TRADING_CONFIRMED` to the exact confirmation phrase
(`I_ACCEPT_LIVE_TRADING_RISK`), populate a separate
`QT_IBKR_LIVE_ACCOUNT_ALLOWLIST`, and connect on the standard IBKR live ports
(7496 TWS / 4001 Gateway). The core independently cross-checks the connected
session's own reported environment (`PAPER`/`LIVE`) against the configured
account type before it will trade at all — a live-configured core refuses a
paper session, and a paper-configured core refuses a live session. Every
other invariant in this document (quote freshness, one-contract cap, spread
limits, duplicate-exposure blocking, `SUBMISSION_UNKNOWN` handling, tick-valid
marketable limits) applies identically to live and paper — live is not a
separate, less-guarded code path.

## Deliberate deferrals

- Bracket/partial-fill protection claims until callback reconciliation is
  complete
- Multi-account/FA, spreads, short premium, assignment/exercise workflows
- Hosted ingress infrastructure
- Adoption of a manual/external (non-app-placed) order or position; a
  `FULL_FLATTEN` only ever cancels this app's own tracked protection legs
- iPhone order submission
- Built-in signal generation or replay
- Signed/notarized packaging and release Gate B/C evidence

These are not hidden behind feature flags. They require separate implementation
and validation against the source requirements.
