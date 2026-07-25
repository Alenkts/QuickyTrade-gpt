# IBKR supervised paper runbook

## Preconditions

- Use current TWS or IB Gateway and the current official TWS Python API.
- Log in manually to an IBKR paper account. QuickyTrade never collects IBKR
  credentials or bypasses 2FA.
- Bind the API socket to loopback and enable socket clients.
- Configure the exact paper account ID. Never select the first managed account
  automatically.
- Confirm US options permission and the required underlying and option market
  data entitlements.
- Keep TWS visible for supervised paper testing and manual recovery.

## Daily startup

1. Start and authenticate TWS/IB Gateway in paper mode.
2. Start the official-TWS QuickyTrade core.
3. Confirm the service reports paper-adapter checks passed and the expected
   masked `DU...` account. Independently verify deterministic client ID,
   API/server compatibility, and current TWS state; the present UI does not yet
   project all those details.
4. Confirm market data is live/allowed and fresh for the allowlisted ticker and
   its option contracts.
5. Start the Node dashboard/webhook service with
   `QT_TRADING_MODE=paper_tws`.
6. Send one unique TradingView alert. Verify the app timeline shows durable,
   execution-ineligible receipt with the selected management-policy snapshot.
   The current application gate does not submit the alert because protective
   fill management is not yet qualified.

If any item is unavailable, stale, ambiguous, or mismatched, remain in
`capture_only`.

The `paper_tws` mode is for controlled adapter validation only. Until the
fill/commission ledger, automatic reconciliation, partial-fill/cancel handling,
and protection workflow are implemented and soaked, do not use it unattended.

## Unknown submission

When an intent is `SUBMISSION_UNKNOWN`:

1. Do not resend the TradingView alert and do not manually retry it in the app.
2. Inspect TWS open/completed orders and executions using the correlation
   `orderRef`.
3. Reconcile account, conId, side, quantity, client/order/permanent IDs, and
   execution IDs.
4. Keep new entries blocked for the affected account/contract until the outcome
   is resolved and audited.

## Disconnect

- Stop new entries immediately.
- Treat last-known market/account/order data as stale.
- Do not assume broker-native working orders were cancelled.
- After reconnect, process IBKR connectivity code behavior, refresh required
  subscriptions, and reconcile orders, executions, and positions before
  resuming.

## Manual recovery

If the app cannot verify a position or safe reducing quantity, use TWS for the
manual operator decision. Record the QuickyTrade correlation and broker IDs.
QuickyTrade must not automatically close or cancel manual/external orders.
Automated close alerts are deliberately blocked in this slice; manage verified
paper positions directly in TWS until execution attribution is implemented.

## End of session

Stop new entries, verify all strategy-owned orders and positions, export the
timeline/audit evidence, and leave unresolved unknown or reconciliation states
visible. Paper operation does not authorize live trading.
