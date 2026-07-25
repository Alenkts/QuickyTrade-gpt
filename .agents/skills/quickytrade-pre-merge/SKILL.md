---
name: quickytrade-pre-merge
description: Run QuickyTrade's test/check suite and verify a diff against the AGENTS.md non-negotiable safety invariants before merging. Use whenever the user asks "is this safe to merge?", "run pre-merge checks", or before any PR/commit that touches src/tradingview/, src/operator/, core/, or src-tauri/.
---

Run this before merging any change that touches `src/tradingview/`, `src/operator/`, `core/`, or `src-tauri/`.

## 1. Run the suite

```bash
npm test
npm run check
```

Both must pass cleanly. `npm run check` also syntax-checks every JS entry point and compiles the Python core.

## 2. Check the diff against the non-negotiable safety rules (from AGENTS.md)

For each rule, read the actual diff (`git diff` against the merge base) and confirm it isn't violated — don't just assume:

- **Persist before broker side effect**: any new order/trade path must durably record the signal and trade intent *before* calling the broker (or the core).
- **Duplicate-alert dedup**: a duplicate alert must return the original correlation ID, not submit a second order — including across a restart.
- **`SUBMISSION_UNKNOWN` on timeout/disconnect**: a submission timeout or disconnect must be recorded as `SUBMISSION_UNKNOWN`, never retried until broker reconciliation resolves it.
- **No fabricated quotes / no silent market-order fallback**: entries must use fresh-quote-derived, tick-valid marketable limits.
- **Opens are long calls/puts only; closes are reduce-only**: closes may not exceed verified long broker quantity minus working strategy exits.
- **Fail closed on missing/stale evidence**: missing or stale connection, account, contract, quote, position, risk, storage, or reconciliation data must block new entries, not proceed with defaults.
- **No live-trading automation**: no automating live unlock, account selection, risk-limit changes, or modification of manual/external IBKR positions/orders.
- **Never render missing broker values as `0`**: UI/API must show `Unavailable` or `Stale` with source and age instead.

## 3. Report

List each rule as pass / not-applicable (diff doesn't touch this concern) / **violation found** (quote the offending code). If anything is a violation, do not consider the change safe to merge until it's fixed and re-checked.

If the diff touches trading-critical logic (order construction, sizing, risk, reconciliation) and the contract seems unsettled, suggest the user route it through the `ibkr-trade-analyst` / `ibkr-architect` Codex specialists defined in `.codex/agents/` before implementation, per `AGENTS.md`'s delegation guidance.
