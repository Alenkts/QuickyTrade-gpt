# TradingView alert setup

> **Scope check before you start:** a webhook alert can be authenticated,
> validated, deduplicated, and durably recorded — but it can **never place an
> IBKR order today**, in any `QT_TRADING_MODE`, including `paper_tws`. Only
> manually created, app-managed trade intents from the QuickyTrade dashboard
> can currently reach IBKR (and even those are gated behind Review → Submit).
> This is a hardcoded scope limit (`APP_MANAGEMENT_EXECUTION_AVAILABLE =
> false` in `server.mjs`), not a config flag — there is nothing to turn on to
> change it. Set this up to validate ingestion, dedup, and the alert timeline;
> don't expect a submitted order at the end of it.

## 1. Start the local service

Generate a high-entropy secret and keep it outside source control:

```bash
export QT_WEBHOOK_SECRET='replace-with-at-least-32-random-characters'
export QT_TRADING_MODE='capture_only'
npm start
```

The service binds two loopback listeners. The dashboard/private API uses
`127.0.0.1:4173`; the restricted TradingView listener uses
`127.0.0.1:4180`. Point an operator-managed HTTPS reverse proxy or ngrok tunnel
only at port `4180`. That listener rejects `/api` and dashboard routes. Never
publish port `4173`, the IBKR socket, or the private core.

To expose the ingress for TradingView's cloud alert servers to actually reach
(TradingView cannot call `127.0.0.1` on your machine), tunnel just that port:

```bash
  ngrok http 4180
```

Use the `https://` forwarding URL ngrok prints, with the path appended:
`https://<your-ngrok-subdomain>.ngrok-free.app/webhooks/tradingview`. That
full URL is what goes in the TradingView alert's "Webhook URL" field. Prefer
ngrok's authenticated/rate-limited options (or an equivalent operator-managed
edge) over a bare free tunnel if this will stay up unattended.

### Allowlists

Two more env vars gate what an accepted alert can contain — set these before
testing, or a correctly authenticated alert can still be rejected:

- `QT_ALLOWED_TICKERS` (or `QT_ALLOWED_SYMBOLS`) — comma-separated tickers the
  webhook will accept in the `ticker` field. Defaults to `QQQ` only. A
  rejected ticker returns `SYMBOL_NOT_ALLOWED`. Example:
  `export QT_ALLOWED_TICKERS='QQQ,SPY,IWM,TSLA'`. Note this is the *webhook's*
  allowlist on the Node side — the Python core has its own, separate
  `QT_ALLOWED_SYMBOLS` (in `core/.env`) that gates what it will actually
  submit to IBKR for manual trades. Keep both in sync if you want the same
  ticker usable end-to-end.
- `QT_ALLOWED_STRATEGIES` — comma-separated `strategy_id` values. **Left
  unset (the default), every strategy_id is allowed** — this is "empty means
  unrestricted," not "empty means reject all." Only set it if you want to
  restrict which alert templates are accepted, e.g.
  `export QT_ALLOWED_STRATEGIES='qqq-alerts'`. A rejected strategy returns
  `STRATEGY_NOT_ALLOWED`.

## 2. Alert JSON

TradingView cannot attach arbitrary HMAC headers in its standard webhook UI, so
the native alert template may carry `auth_token` in the JSON body. QuickyTrade
compares it in constant time and removes it before hashing, persistence, logs,
and the app timeline.

Open example:

```json
{
  "auth_token": "YOUR_SECRET",
  "schema_version": "1",
  "alert_id": "qqq-call-{{timenow}}",
  "sent_at": "{{timenow}}",
  "strategy_id": "qqq-alerts",
  "strategy_version": "2026.07.18",
  "action": "OPEN_LONG_CALL",
  "ticker": "QQQ",
  "target_dte": 0,
  "strike_policy": {
    "type": "ATM_OFFSET",
    "offset": 1
  },
  "risk_hint": {
    "max_contracts": 1
  }
}
```

An optional `exit_policy_id` may be included for future policy correlation, but
the current controlled paper adapter does not create bracket protection from
that value. Verify and manage protection directly in TWS.

Close example:

```json
{
  "auth_token": "YOUR_SECRET",
  "schema_version": "1",
  "alert_id": "qqq-call-close-{{timenow}}",
  "sent_at": "{{timenow}}",
  "strategy_id": "qqq-alerts",
  "strategy_version": "2026.07.18",
  "action": "CLOSE_LONG_CALL",
  "ticker": "QQQ",
  "entry_alert_id": "qqq-call-ENTRY-ID"
}
```

The alert ID must be stable across TradingView retries. Do not use a template
that creates a new ID on every retry of the same signal.

`sent_at` must be ISO-8601 with a zone/offset (what `{{timenow}}` produces).
The service rejects it outside a moving window around "now":

- `QT_WEBHOOK_MAX_AGE_MS` (default 5 minutes) — older than this returns
  `ALERT_TOO_OLD`. TradingView's own alert delivery can lag during high load;
  widen this if you see spurious rejections.
- `QT_WEBHOOK_MAX_FUTURE_SKEW_MS` (default 30 seconds) — timestamped further
  in the future than this (clock drift) returns `ALERT_FROM_FUTURE`.
- `QT_WEBHOOK_MAX_BODY_BYTES` (default 32KB) caps the request body size.

## 3. Authentication alternatives

Capable relays may omit `auth_token` and send one of:

- `Authorization: Bearer <secret>`
- `X-TradingView-Signature: sha256=<HMAC-SHA256 of the raw body>`

Never place the secret in a URL or query parameter.

## 4. Send a test alert

Before wiring up TradingView itself, confirm the local pipeline works with a
direct request (`QT_WEBHOOK_SECRET` must already be exported in the shell
running `npm start`):

```bash
curl -i http://127.0.0.1:4180/webhooks/tradingview \
  -H 'Content-Type: application/json' \
  -d '{
    "auth_token": "'"$QT_WEBHOOK_SECRET"'",
    "schema_version": "1",
    "alert_id": "test-'"$(date +%s)"'",
    "sent_at": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
    "strategy_id": "qqq-alerts",
    "strategy_version": "2026.07.18",
    "action": "OPEN_LONG_CALL",
    "ticker": "QQQ",
    "target_dte": 0,
    "strike_policy": {"type": "ATM_OFFSET", "offset": 1}
  }'
```

A `202` with `"accepted": true` means it worked. Open the dashboard at
`http://127.0.0.1:4173` — the new alert should appear in the TradingView
Alert Activity table with a correlation ID matching the response. That
confirms authentication, validation, and durable capture; it does not mean
(and never will mean, per the scope note above) that an order reached IBKR.

## 5. Response meaning

An accepted alert returns `202`:

```json
{
  "accepted": true,
  "correlation_id": "...",
  "status": "READY",
  "duplicate": false
}
```

This means “durably captured.” Follow the correlation in the QuickyTrade app.
It does not mean the signal passed risk, an order was submitted, or a fill
occurred.

Stable rejection codes include `UNAUTHORIZED`, `ALERT_TOO_OLD`,
`ALERT_FROM_FUTURE`, `INVALID_ACTION`, `UNKNOWN_FIELDS`,
`SYMBOL_NOT_ALLOWED`, `STRATEGY_NOT_ALLOWED`, and `ALERT_ID_CONFLICT`.

## 6. Rotation and incident response

Stop new alerts before rotating the secret. Restart with the new secret, update
the TradingView alert template, then send a unique capture-only test. If a
secret may have leaked, disable the public route/tunnel immediately and review
the local alert timeline; do not delete the SQLite evidence.
