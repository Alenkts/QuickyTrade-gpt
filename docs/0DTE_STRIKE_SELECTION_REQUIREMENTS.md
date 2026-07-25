# 0DTE strike-selection requirements

## What happened to the 0DTE logic

It was never ported. `/Users/alensam/Projects/QuickyTrade` (package name
`odte-desk`) is an earlier, separate repository — not an ancestor commit of
this one (it has its own empty git history; there is no shared lineage to
recover). Its `selectOption()` (`server.mjs:31-46`, mirrored independently in
`app.js` and again in the native `MacDeskModel.swift` /
`ib_gateway_bridge.py` port) implements a **dynamic, market-data-driven**
strike search:

- Build a 7-strike candidate window starting at-the-money and walking in the
  profitable direction (`base + i*step` for calls, `base - i*step` for puts;
  `step` is per-symbol — 5 for TSLA, 1 for SPY/QQQ/IWM).
- Filter candidates to the listed contract whose expiry is literally today
  (0DTE).
- Pull a live snapshot for every candidate and pick whichever one's **delta**
  or **premium (mid price)** — operator-selectable metric — falls closest to
  the midpoint of a configured target range (defaults: delta 0.25–0.35,
  premium $1.00–$2.50), preferring candidates inside the range and falling
  back to nearest-overall if none qualify.

This QuickyTrade-gpt core (`core/quickytrade_core/selection.py`) instead
implements a **fixed, deterministic** model:
`choose_chain_and_expiry(target_dte=...)` finds the chain/expiry at an exact
DTE, then `choose_listed_strike(chain, spot, right, offset=N)` walks a fixed
integer number of listed strikes from ATM. There is no delta or premium
target, no live-metric scan, no candidate window — `offset` is just a number
supplied by whoever built the request.

That number currently comes from two different places depending on source,
and the two paths are **not symmetric today**:

| | TradingView alert | Manual UI |
|---|---|---|
| Strike input | `strike_policy: { type: 'ATM_OFFSET', offset }` — a fixed offset the Pine Script alert author hardcodes (`src/tradingview/validation.js:131-134`) | Operator types an exact strike + expiry into the form; the app builds `strike_policy: { type: 'EXACT_LISTED', ... }` (`server.mjs:263-267`) — **no selection algorithm runs at all** |
| Selection logic | `choose_listed_strike` (fixed offset) | None — bypassed entirely |

So today: TradingView has a primitive version of "ATM ± N", manual has no
selection logic whatsoever, and neither has the delta/premium-range search
the original app used. This doc specifies porting that search into a single
shared implementation both paths call identically.

## Requirement: one shared selection function, two trigger behaviors

Add a new `strike_policy.type` — `TARGET_RANGE` — implemented **once**, in
`core/quickytrade_core/selection.py`, and called by both the TradingView
processor and the manual-entry path through the same core contract
(`ExecutionEngine._prepare_open`, `engine.py:290`). Do not reimplement the
metric-scan in Node/JS or duplicate it per surface — that duplication (three
independent copies: Node, browser, Swift) is exactly what let the algorithm
drift/get lost across the old app's surfaces, and this app's core already
exists specifically so IBKR-facing logic has one owner (`AGENTS.md`: "only
the long-lived `core/` service may own the supported TWS connection").

**The only intentional difference between the two entry paths is the
submission trigger, and the current architecture already provides it
correctly — no new work is needed there:**

- TradingView: `IbkrAlertProcessor.processIntent` (`src/tradingview/processor.js:29`)
  calls `ibkrAdapter.placeTrade` automatically once the alert is durably
  persisted and claimed — no human in the loop.
- Manual: the operator must click **Review Trade**, inspect the resolved
  contract, then click **Submit** (`server.mjs`'s `/api/trade-intents/manual`
  → preview → explicit submit step). This already exists in the UI shown in
  the operator dashboard.

Both paths funnel into the same `ExecutionEngine.execute`/`preview`, so once
`TARGET_RANGE` exists as a `strike_policy` type, both surfaces get identical
strike selection for free by constructing that policy shape — the manual
form's Symbol/Expiry/Strike/Right/Quantity fields would change to
Symbol/Right/Quantity/Metric(Δ or $)/Range(lo–hi), with the resolved contract
shown for review before the existing Submit click, mirroring the old app's
review-dialog pattern.

## Proposed `selection.py` addition

```python
def choose_strike_by_target_range(
    chain: OptionChain,
    *,
    right: str,
    metric: str,          # "DELTA" | "PREMIUM"
    lo: Decimal,
    hi: Decimal,
    spot: Decimal,
    strike_step: Decimal,
    candidate_count: int,
    quotes_by_strike: dict[Decimal, Quote],
    now: datetime,
    max_age_seconds: float,
) -> Decimal:
    ...
```

Behavioral requirements (ported from `odte-desk/server.mjs:31-46`, adjusted
to this core's stricter evidence rules):

- **SEL-001**: Candidate strikes are the `candidate_count` listed strikes
  starting at-the-money and walking in the profitable direction (higher for
  calls, lower for puts), at `strike_step` increments — `strike_step` and
  `candidate_count` are config, not hardcoded per-symbol like the old app's
  `TSLA ? 5 : 1`.
- **SEL-002**: Every candidate's quote passes the *existing* `validate_quote`
  (`selection.py:18`) — live market data, bounded staleness, finite
  bid/ask/mid, non-crossed — before it is eligible. The old app's mid-price
  fallback to a possibly-stale last-trade tick (`m['7635'] ?? last`) must
  **not** carry over; this violates AGENTS.md's "missing or stale ... quote
  ... evidence blocks new entries."
- **SEL-003**: `metric = DELTA` uses `abs(quote.delta)`; `metric = PREMIUM`
  uses the validated quote's mid. A missing/non-finite metric on a candidate
  excludes that candidate, it does not fail the whole selection.
- **SEL-004**: Prefer candidates with `lo <= metric <= hi`; if none qualify,
  fall back to the full eligible candidate set. From the chosen pool, select
  the single strike whose metric is closest to `(lo + hi) / 2`. Ties break
  toward the closer-to-ATM candidate (deterministic; the old app's `Array#sort`
  tie-break was insertion-order, which is not deterministic enough for an
  auditable trading decision).
- **SEL-005**: If the eligible candidate set is empty (all candidates failed
  quote validation), raise `SelectionError("TARGET_RANGE_NO_ELIGIBLE_STRIKE", ...)`
  — fail closed, same pattern as every other `SelectionError` in this module.
- **SEL-006**: `lo`/`hi`/`strike_step`/`candidate_count` are validated the
  same way `offset` is today (finite, correctly-typed, in-range) before use.

## What should explicitly NOT be ported

The old app also had per-position **sizing** (risk-cap/buying-power-based,
up to 50 contracts, `app.js:9`) and **scale-out/stop management** (three
tranches at +25/+50/+100%, stop at −35%, `server.mjs:53`). Do not port these
alongside strike selection:

- `core/quickytrade_core/config.py:69-70` hard-validates
  `max_contracts_per_order == 1` — "This release hard-caps option orders at
  exactly one contract." The old app's sizing model directly conflicts with
  this invariant.
- Automated scale-out/stop management requires the execution/fill/protection
  ledger, which `docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md`'s "Deliberate
  deferrals" section already lists as not yet built. Wiring old-app TP/stop
  logic on top of that gap would silently violate "automated closes ... are
  not implemented or unlockable" (`README.md`).

If multi-contract sizing or automated scale-out is wanted later, it needs its
own requirements doc and an explicit decision to relax the 1-contract cap —
treat it as out of scope here.

## Open decisions (need a decision before implementation)

1. Does `TARGET_RANGE` **replace** `ATM_OFFSET`/`EXACT_LISTED` for both
   surfaces, or is it additive (operator/alert author picks a mode)? This doc
   assumes additive — `ATM_OFFSET` and `EXACT_LISTED` keep working for
   existing alerts/tests.
2. Are default ranges (delta 0.25–0.35, premium $1.00–$2.50) and per-symbol
   `strike_step`/`candidate_count` global config (env vars, like the rest of
   `CoreConfig`) or per-request fields the caller can override? The old
   app exposed live UI steppers; this app's manual-UI equivalent would need
   the same if per-request overrides are wanted.
3. Manual UI: replace the Expiry/Strike text fields with the Metric/Range
   picker outright, or offer both `EXACT_LISTED` and `TARGET_RANGE` as
   selectable modes?

## Test coverage to add

Mirroring the existing pattern (`core/tests/test_execution.py` already
covers listed-strike selection and marketable-limit construction):

- Candidate in range selected; no candidate in range falls back to
  nearest-overall; all candidates fail quote validation raises
  `TARGET_RANGE_NO_ELIGIBLE_STRIKE`.
- Delta metric vs. premium metric produce different picks on the same chain.
- Per-symbol `strike_step`/`candidate_count` boundaries (e.g. TSLA-style
  step=5 vs. step=1).
- A stale/missing quote on the otherwise-best candidate excludes it rather
  than selecting it.
- TradingView alert and manual-UI request with identical `TARGET_RANGE`
  policy produce identical selected contracts (proves single shared code
  path, not per-surface drift).
