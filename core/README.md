# QuickyTrade TWS execution core (paper by default, live with explicit confirmation)

This directory contains a long-lived, loopback-only Python service for the
official Interactive Brokers TWS socket API. Python is infrastructure here; it
does not generate signals or run a built-in strategy. Accepted order sources
are already-persisted TradingView and manual-UI intents from the app processor.

## Safety contract

- IBKR is the authority for contracts, quotes, positions, working orders, and
  order acknowledgement.
- Non-loopback hosts are rejected during configuration. A paper account
  (`DU...`) requires the standard paper ports `7497`/`4002`. A live account
  (`U...`) requires the standard live ports `7496`/`4001` **and** an explicit
  `QT_LIVE_TRADING_CONFIRMED` opt-in (see below) — a live account is refused
  on any port without it.
- The selected account must exactly match the configured allowlist for its
  environment (paper or live) and a managed account returned by IBKR. The
  connected session's own reported environment must match the configured
  account type, or the core refuses to trade — a live-configured core will
  not silently operate against a paper session, and vice versa.
- Client ID is fixed (`71` by default) and is never incremented after an
  uncertain connection.
- An owner-only nonblocking profile-database lock prevents a second core
  process from owning the same supported TWS profile.
- The app `intentId`, UUID `correlationId`, and source-bound
  (`tradingview:<event_id>` or `manual:<event_id>`) idempotency key are required.
  A local SQLite broker-boundary claim is committed before `placeOrder`; replay
  returns the original result and restart changes an interrupted claim to
  `SUBMISSION_UNKNOWN`.
- The finalized account, action, qualified contract, side, quantity, limit, and
  `orderRef` are committed immediately before `placeOrder`, so a process crash
  retains the evidence needed for manual reconciliation.
- Opens are one long call or one long put per contract; quantity is a
  separate, independently-capped concern. When the signal supplies
  `capital_per_trade_dollars`, quantity is `floor(capital / (option fresh
  mid-price * contract multiplier))` -- one contract represents
  `multiplier` shares (typically 100 for a standard equity/ETF option), so
  the per-contract cost is the per-share mid-price times that multiplier --
  never rounded up, and never derived from a client-supplied or fabricated
  price. When `capital_per_trade_dollars` is absent, quantity
  keeps the pre-existing behavior (client-supplied `quantity`, constrained to
  exactly 1, defaulting to 1). Either way, the computed quantity is always
  clamped to the deployment-configured `max_contracts_per_order` ceiling
  (`QT_MAX_CONTRACTS_PER_ORDER`, defaults to `1`, restart-only) before an
  order is built, and an amount that cannot cover even one contract blocks
  with `INSUFFICIENT_CAPITAL_FOR_ONE_CONTRACT` rather than silently rounding
  up to one contract. Exact-DTE selection uses option-chain metadata,
  positive `ATM_OFFSET` means OTM by listed-strike index, and the selected
  option is qualified again before use.
- Entry price is a fresh-live-quote-derived, tick-valid, capped, marketable
  `DAY` limit. Market orders and fabricated quote fallbacks do not exist.
- A matching broker position or working buy blocks a new entry.
- Every intent has immutable `source`, `APP_OWNED` ownership, and a selected
  management mode. `APP_MANAGED` requires a versioned policy with strictly
  increasing take-profit triggers and allocations totaling exactly `100`;
  `ENTRY_ONLY` carries no exit policy. These fields are durable correlation
  metadata. They do not activate automated exits.
- Close alerts require `entry_alert_id` or `trade_ref`. Automated
  `REDUCE_ONLY_PARTIAL`/`FULL_FLATTEN` close submission is gated behind
  broker-authoritative fill evidence (the execution ledger's
  `position_state`) rather than unconditionally disabled -- a close is only
  reachable once the referenced entry is `FILLED`/`CLOSING` with a positive
  broker-confirmed `open_quantity`. The invariant:

  ```text
  sell quantity <= verified broker long quantity - all working sell quantity
  ```

  is re-verified fresh on every close, never cached. `server.mjs` proxies
  this as `POST /api/trades/:correlationId/close`, and the operator
  dashboard wires it to per-position partial-close and
  flatten-with-confirmation controls.

- `SUBMITTED` is returned only after `openOrder` or `orderStatus` reports
  `PendingSubmit`, `PreSubmitted`, or `Submitted`. Socket loss or acknowledgement
  timeout returns `SUBMISSION_UNKNOWN`, and must not be retried.
- Definitive validation or broker rejection returns HTTP `200` with `BLOCKED`
  and a stable code. Keeping `BLOCKED` at HTTP 200 lets the Node adapter
  distinguish a definitive no-order result from an ambiguous transport error.

## Install and run

Use Python 3.11 or newer. Obtain and install the official TWS Python API from
Interactive Brokers after reviewing its license; this project deliberately does
not bundle or replace it with `ib_insync`.

Required environment:

```text
QT_IBKR_PAPER_ACCOUNT=DU...
QT_IBKR_PAPER_ACCOUNT_ALLOWLIST=DU...
QT_ALLOWED_SYMBOLS=QQQ
QT_TRADING_CLASS_ALLOWLIST=QQQ
QT_CORE_TOKEN=<at-least-32-random-bytes>
```

The selected account and every paper allowlist entry must be an IBKR `DU...`
paper-account identifier.

To use a real IBKR account instead, set these in place of the paper variables
above:

```text
QT_IBKR_LIVE_ACCOUNT=U...
QT_IBKR_LIVE_ACCOUNT_ALLOWLIST=U...
QT_LIVE_TRADING_CONFIRMED=I_ACCEPT_LIVE_TRADING_RISK
QT_IBKR_PORT=7496   # or 4001 for Gateway
```

`QT_LIVE_TRADING_CONFIRMED` must match that exact phrase — there is no other
way to enable live trading, so it can never happen by copying a paper config
and changing one value. Every other invariant in this document (quote
freshness, one-contract cap, spread limits, duplicate-exposure blocking,
`SUBMISSION_UNKNOWN` handling) applies identically to a live-configured core.

**Also set a distinct `QT_CORE_STATE_DB` for a live core** (e.g.
`~/.quickytrade/core-submissions-live.sqlite3`) if paper and live ever run on
the same machine. The submission registry is not scoped by account — sharing
the default path means a paper `SUBMISSION_UNKNOWN` row can block new live
orders too (safe, but confusing), and vice versa.

Optional environment:

```text
QT_IBKR_HOST=127.0.0.1
QT_IBKR_PORT=7497
QT_IBKR_CLIENT_ID=71
QT_CORE_HTTP_HOST=127.0.0.1
QT_CORE_HTTP_PORT=8765
QT_CORE_STATE_DB=~/.quickytrade/core-submissions.sqlite3
QT_MAX_CONTRACTS_PER_ORDER=1
QT_RECONCILE_INTERVAL_SECONDS=45
```

`QT_MAX_CONTRACTS_PER_ORDER` is the deployment-level safety ceiling for
capital-based dynamic sizing (see "Opens are one long call or one long put"
above). It defaults to `1`, matching this core's prior hard-locked behavior,
so upgrading without touching this variable changes nothing. Raising it is a
deliberate operator action that requires a core restart; it is never
inferred or auto-raised by app code.

`QT_RECONCILE_INTERVAL_SECONDS` controls the periodic background
reconciliation sweep (see "Execution/commission capture and reconciliation"
below). Defaults to `45` seconds.

From this directory:

```bash
PYTHONPATH=. python3 -m quickytrade_core
```

The service connects once, completes the IBKR handshake, resolves managed
accounts and server time, reconciles positions and open orders, runs one
startup reconciliation sweep (see below), then listens on loopback. Public
`GET /health` returns only liveness. Authenticated
`GET /healthz` returns `{ "ready": true|false, "environment": "PAPER"|"LIVE", "code"?: "..." }`
without account data — `environment` truthfully reflects the configured
account type, not a hardcoded value.

## Execution/commission capture and reconciliation

`ExecutionLedger` (`quickytrade_core/execution_ledger.py`) extends the same
SQLite file/connection `SubmissionRegistry` owns with four additional
tables: append-only `broker_executions` (keyed by IBKR's own `execId`, so a
redelivered or corrected execution is a no-op insert, never a mutation),
`broker_commissions` (a separate table, since a commission report frequently
arrives independently of — and out of order relative to — its execution),
`position_state` (a rebuildable per-`correlation_id` cache computed only from
the raw execution/commission rows — never a second source of truth), and
`reconciliation_runs` (an audit trail of every sweep).

`OfficialIbapiTransport` gained `execDetails`/`commissionAndFeesReport`
EWrapper callbacks (fast, lock-protected, non-blocking ledger writes only —
they never place, modify, or cancel an order) that key off `Execution.orderRef`
to attribute a fill back to the durable `correlation_id`/`order_ref` recorded
before submission. A fill that arrives before its `order_ref` is resolvable
(or one outside this app's own tracking) is stored with `correlation_id =
NULL` and re-attempted on the next reconciliation sweep.

A reconciliation sweep (`OfficialIbapiTransport.reconcile`) runs once at
startup and then periodically (`QT_RECONCILE_INTERVAL_SECONDS`, background
daemon thread, data capture/resolution only — no order placement):

1. `reqExecutions` backfills same-day fills. **This is scoped to roughly the
   current trading day for the account** (IBKR's own documented behavior) and
   cannot resolve a `SUBMISSION_UNKNOWN` left over from a prior calendar day —
   a real, permanent limitation, not something this phase works around.
2. `reqCompletedOrders(apiOnly=True)` backfills order-level completion status
   (present in the installed `ibapi` 10.37.2; guarded by a runtime
   availability check so an install that lacks it degrades to a documented
   skip rather than a guessed workaround).
3. Any `SUBMISSION_UNKNOWN` row with a matching `broker_executions` row is
   resolved to `reconciliation_outcome = CONFIRMED_FILLED`; a row with no
   execution evidence but a definitive `Cancelled`/`ApiCancelled` completed-order
   status is resolved to `CONFIRMED_NO_FILL`. Either outcome stops that row
   from blocking all new entries (`has_unresolved_unknown`); only
   `CONFIRMED_FILLED` continues to reserve the exact symbol/right
   (`has_blocking_open`) — `CONFIRMED_NO_FILL` releases it, since nothing
   filled.
4. **Cross-day fallback**: for anything still unresolved, IBKR's own live
   positions are compared against the ledger. A live position with no
   matching execution evidence anywhere in `broker_executions` is an
   *unattributed-position discrepancy* — logged and recorded in the
   `reconciliation_runs.notes` audit trail, but never auto-resolved either
   way, and it stays a permanently blocking `SUBMISSION_UNKNOWN`. Per the
   safety rules above, this app does not automate the interpretation or
   resolution of a manual/external position.

Fill/commission capture, position-state maintenance, and this reconciliation
sweep are pure data capture and resolution of already-ambiguous submissions.
Protection-order placement (stop-loss/take-profit) and reacting to a
take-profit fill with a management-policy transition are covered next;
reduce-only close submission remains a later phase's concern and is not
implemented here.

## Protection-order placement and management-policy transitions

Once a `position_state` row reads `FILLED` for an `APP_MANAGED` correlation
id, `ExecutionEngine.ensure_protection` (periodic sweep, level-triggered)
places a stop-loss/take-profit `OCA` pair per take-profit level, sized by
largest-remainder allocation of the filled quantity (`ProtectionLedger`,
`broker_protection_orders`). Each take-profit slice shares its own OCA group
with its own quantity-matched stop-loss slice, so one level filling never
cancels another level's protection.

`ExecutionEngine.ensure_transitions` (same sweep, runs after
`ensure_protection`) reacts to a take-profit leg's *broker-execution-evidence*
confirmed fill (never a raw order-status string alone) by applying the
entry's `managementPolicy.transitions[]`:

- `MOVE_STOP_TO_BREAKEVEN` modifies every other still-working stop-loss leg
  **in place** (same IBKR order id — `placeOrder` resent with updated
  trigger/limit, never cancel+replace) to the entry's own average fill price.
- `TRAIL_FRESH_BID` modifies every other still-working stop-loss leg to a
  fresh-quote-derived trigger, **ratchet-only**: a recomputed trigger that
  would not improve on the leg's current resting trigger is a deliberate
  no-op, never a broker call.

Durable evidence (`management_transitions`, PK `transition_id =
"<correlation_id>:<after>"`) is committed before every broker call, exactly
like every other broker side effect in this codebase; an ambiguous modify ack
is `MODIFY_UNKNOWN` (tracked on the affected `broker_protection_orders` row)
and, like an ambiguous protection placement, globally blocks new opens until
reconciled. See `quickytrade_core/transitions.py`'s module docstring for the
full state machine and the terminal-state/retry policy.

## App-to-core request

Send `POST /private/v1/place-trade`, `Content-Type: application/json`, and
`Authorization: Bearer <QT_CORE_TOKEN>`. A TradingView compatibility request
without the new management fields remains `ENTRY_ONLY`:

```json
{
  "broker": "IBKR",
  "idempotencyKey": "tradingview:tv-QQQ-001",
  "intentId": 42,
  "correlationId": "7d468820-e55a-4d4d-831f-10c8e7c83a12",
  "source": "tradingview",
  "alertId": "tv-QQQ-001",
  "signal": {
    "schema_version": "1",
    "alert_id": "tv-QQQ-001",
    "sent_at": "2026-07-20T14:35:00Z",
    "strategy_id": "tv-options",
    "strategy_version": "1",
    "action": "OPEN_LONG_CALL",
    "ticker": "QQQ",
    "target_dte": 0,
    "strike_policy": { "type": "ATM_OFFSET", "offset": 1 },
    "risk_hint": { "max_contracts": 1 }
  }
}
```

An open signal may additionally include `"capital_per_trade_dollars": "500"`
(a positive decimal string/number, sanity-capped at $1,000,000 -- a
last-resort fat-finger check, not the real safety mechanism). When present,
it fully replaces `quantity` for sizing: the core computes
`floor(capital_per_trade_dollars / (option_fresh_mid_price *
contract_multiplier))` -- the per-contract cost is the fresh per-share
mid-price times the qualified contract's share multiplier (typically 100),
never the mid-price alone -- then clamps the result to
`QT_MAX_CONTRACTS_PER_ORDER`. When absent, sizing keeps its
pre-existing, unchanged behavior (`quantity` defaults to `1` and, if
supplied, must equal `1`) -- this field is additive, not a breaking change to
any existing TradingView or manual signal shape.

New TradingView intents should use canonical source `TRADINGVIEW`. A manual
intent uses source `MANUAL_UI`, `manual:<event_id>` as its idempotency key, and
must make ownership and management selection explicit. For example, add these
top-level fields to the normalized intent:

```json
{
  "source": "MANUAL_UI",
  "ownership": "APP_OWNED",
  "managementMode": "APP_MANAGED",
  "managementPolicy": {
    "policyId": "paper-balanced-v1",
    "version": 1,
    "takeProfitLevels": [
      { "levelId": "TP1", "triggerPercent": 20, "allocationPercent": 50 },
      { "levelId": "TP2", "triggerPercent": 40, "allocationPercent": 25 },
      { "levelId": "TP3", "triggerPercent": 60, "allocationPercent": 25 }
    ],
    "stopLossPercent": 25
  }
}
```

Manual preview may replace `ATM_OFFSET` with an exact listed-contract policy:

```json
{ "type": "EXACT_LISTED", "expiry": "20260720", "strike": 600 }
```

The requested expiry must resolve to one allowlisted chain and the strike must
be present in that chain before IBKR contract qualification. It is not a bypass
for quote, spread, cutoff, duplicate-exposure, or account checks.

`managementMode` is immutable for an idempotency key. Replaying that key with a
different mode or policy returns `CORRELATION_CONFLICT` and cannot place a
second order. `ENTRY_ONLY` must omit `managementPolicy`. The app-level
`USER_MANAGED` and `TRADINGVIEW_MANAGED` modes stay quarantined outside the
broker boundary; if either reaches this service it is rejected rather than
silently reinterpreted as executable management.

Authenticated `POST /private/v1/preview-trade` accepts the same persisted
request. It qualifies the contract, obtains fresh quotes, and returns a
`PREVIEW_READY` marketable-limit proposal without claiming the idempotency key
or calling `placeOrder`. Preview is advisory: submission repeats every broker
readiness, quote, contract, exposure, and risk check.

`exit_policy_id` is accepted as optional untrusted signal correlation metadata.
It does not select or override the operator-snapshotted `managementPolicy`, and
it does not create bracket children in this controlled paper-submission slice.

A close replaces the option-selection fields with the exact entry reference:

```json
{
  "action": "CLOSE_LONG_CALL",
  "ticker": "QQQ",
  "entry_alert_id": "tv-QQQ-001"
}
```

The close snippet shows only the action-specific signal fields; the common
schema, alert, timestamp, and strategy fields remain required. A close may use
`trade_ref` instead when it contains the exact broker-registry key. Exactly one
close-reference field is accepted.

## Response contract

Acknowledged paper submission:

```json
{
  "status": "SUBMITTED",
  "brokerOrderId": "700",
  "clientId": 71,
  "permId": 900,
  "rawBrokerStatus": "PreSubmitted",
  "orderRef": "QT...",
  "orderType": "LMT",
  "tif": "DAY",
  "source": "MANUAL_UI",
  "ownership": "APP_OWNED",
  "managementMode": "APP_MANAGED",
  "managementStatus": "PENDING_EXECUTION_LEDGER"
}
```

Definitive failure:

```json
{ "status": "BLOCKED", "code": "QUOTE_STALE", "correlationId": "7d468820-e55a-4d4d-831f-10c8e7c83a12" }
```

Ambiguous outcome:

```json
{ "status": "SUBMISSION_UNKNOWN", "code": "IBKR_ACK_TIMEOUT", "correlationId": "7d468820-e55a-4d4d-831f-10c8e7c83a12" }
```

The endpoint intentionally returns HTTP 200 for the explicit
`SUBMISSION_UNKNOWN` body because the Node adapter maps non-2xx responses to a
definitive block. Node then treats the unknown status as ambiguous. It does the
same for an unreadable response or connection timeout and must never retry.

## Verification and requirement mapping

Run:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

The fake-transport tests map to `SET-002`, `ACC-001`, `CTR-004`, `CTR-005`,
`CTR-008`, `MKT-001`, `MKT-002`, `ORD-003`, `ORD-004`, `ORD-005`, `ORD-008`,
`ORD-015`, `ORD-022`, `RSK-009`, `SIG-003`, and `NFR-REL-001`.

## Deliberate limits and readiness blockers

This adapter does not claim full production readiness for either paper or
live. Live trading requires the explicit opt-in described above; it is not
automated or inferred from configuration alone, and the connected session's
own reported environment is always cross-checked against the configured
account before any order is placed. Before enabling live (or paper) at scale,
the official IBKR package/API and TWS versions must be pinned and
compatibility-tested, market data and option permissions must be verified, and
the profile must pass the prescribed protocol and restart drills.

Unknown submissions are durably quarantined but are not automatically resolved
in this slice; the broader reconciliation ledger must match them by orderRef,
client/order/perm ID, account, conId, side, size, and time. Execution,
commission, partial-fill, cancellation, bracket protection, account-value risk,
and UI projection remain responsibilities of the broader broker ledger and are
not weakened or represented as complete here — this is true for live exactly
as much as for paper. An `APP_MANAGED` acknowledgement therefore reports
`PENDING_EXECUTION_LEDGER`; it is not a claim that the position is currently
protected. Until those gates exist, this service is suitable for controlled
adapter development and validation, not unattended operation.
