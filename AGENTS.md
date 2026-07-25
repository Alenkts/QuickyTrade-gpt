# QuickyTrade agent routing

QuickyTrade is an IBKR-only, paper-first application. Treat TradingView as an
untrusted signal source and IBKR as the authority for contracts, orders,
executions, commissions, positions, and account data.

Exclude Schwab and built-in/Python strategy execution unless the user explicitly
starts a separate scope. Never infer live readiness from a successful API call.

## Delegation

Use the smallest applicable specialist set:

- `ibkr-architect`: component boundaries, state machines, persistence,
  reconciliation, recovery, or major cross-cutting changes.
- `ibkr-trade-analyst`: alert semantics, option selection, order behavior,
  sizing, risk, protection, and reduce-only invariants.
- `ibkr-end-user`: operator journeys, readiness, status hierarchy,
  accessibility, failure presentation, and supervised-trading usability.
- `ibkr-solution-designer`: concrete file/module/API/UI integration design and
  phased delivery.
- `ibkr-programmer`: implementation, migrations, automated tests, and scoped
  documentation updates after contracts are clear.
- `ibkr-evaluator`: independent final review against requirements, tests,
  safety invariants, and deliberate deferrals.

Delegate independent read-heavy reviews in parallel. Keep overlapping writes
serial. For a trading-critical implementation, use architect/trade analyst
before the programmer when the contract is unsettled, then run the evaluator
after implementation.

## Non-negotiable safety rules

- Persist a unique signal and trade intent before any broker side effect.
- A duplicate alert must return its original correlation and must not submit a
  second order, including after restart.
- A submission timeout or disconnect becomes `SUBMISSION_UNKNOWN`; do not
  retry until broker reconciliation proves the outcome.
- Default entries to fresh-quote-derived, tick-valid marketable limits. Do not
  fabricate quotes or silently fall back to market orders.
- Opens are long calls or long puts only. Closes are reduce-only and may not
  exceed verified long broker quantity minus working strategy exits.
- Missing or stale connection, account, contract, quote, position, risk,
  storage, or reconciliation evidence blocks new entries.
- Do not automate live unlock, account selection, risk-limit changes, or
  modification of manual/external IBKR positions and orders. Live trading is
  supported only when the operator explicitly confirms it (`selected_account`
  is a live `U...` identifier, `QT_LIVE_TRADING_CONFIRMED` is set to the exact
  confirmation phrase, and the account is in a separate live allowlist) — the
  code must never flip this on by itself, and the connected session's
  self-reported environment (`PAPER`/`LIVE`) must match the configured account
  type or the core refuses to trade.
- Never render missing broker values as numeric zero. Show `Unavailable` or
  `Stale` with source and age.

## Verification

Map implementation and tests to the requirement IDs in
`docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md`. At minimum, test authentication,
schema/timestamp checks, durable deduplication, changed-payload conflicts,
async acknowledgement, broker rejection, unknown submission, restart, and UI
timeline rendering. Paper validation is required before any live enablement.
