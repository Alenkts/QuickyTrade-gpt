# QuickyTrade

QuickyTrade is an IBKR-only, paper-first desktop trading control center. Its
current feature slice packages the control center for Tauri/macOS, durably
captures authenticated TradingView alerts, stores manual trade proposals,
supports named Paper/Live connection preferences, snapshots source-specific
trade-management ownership, and tracks correlation state in the app.

Schwab, Robinhood, and built-in/Python signal-generation strategies are outside
this product scope. Python is used only where needed for the official IBKR TWS
API transport.

## Safety status

The default mode is `capture_only`. It records and displays TradingView alerts
but cannot place an order, and those records remain permanently ineligible for
later execution. Controlled paper entry submission is available only through
the separately started official-TWS core and an exact configured `DU...` paper
account. New app-managed entries are currently captured/proposed but not sent:
the execution/fill/protection ledger is not complete, so the app cannot yet
honor the selected management policy. Automated closes are not implemented.

Live execution is possible, but only through explicit operator configuration
of the core — never automatically. It requires a real IBKR account (`U...`),
`QT_LIVE_TRADING_CONFIRMED` set to an exact confirmation phrase, a separate
live account allowlist, and the standard IBKR live ports; the core refuses to
trade at all unless the connected session's own reported environment matches
the configured account type. Every other safety invariant (quote freshness,
capital-based quantity clamped to a deployment-restart-only contract ceiling,
spread limits, duplicate-exposure blocking, `SUBMISSION_UNKNOWN` handling)
applies identically to live and paper. See `core/README.md` for the exact
live setup and `docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md`'s "Live trading"
section.

The legacy Client Portal order route remains disabled. Manual entry now uses
`POST /api/trade-intents/manual`, which durably creates a non-executable Paper
proposal. A successful webhook response
means only that the alert was durably received; it never means an IBKR order was
submitted or filled.

The older Swift/Python native bridge is also retired and fail-closed. It cannot
open a second IBKR session or bypass the durable TradingView/core path.

## Requirements

- Node.js 22.5 or newer (Node 24 recommended for the built-in SQLite API)
- For paper execution: current official IBKR TWS Python API, plus a logged-in
  paper TWS or IB Gateway session bound to loopback
- An operator-managed HTTPS reverse proxy or tunnel for a TradingView-facing URL

## macOS desktop

The application ships a lightweight Tauri v2 shell (requires Rust).

To launch it:

```bash
npm run desktop:dev
```

Connection profiles suggest TWS Paper/Live ports `7497`/`7496` and Gateway
Paper/Live ports `4002`/`4001`. A selected port is never by itself treated as
environment or live authorization — the core independently verifies its own
configured account/port against the connected session before treating a
profile as ready (see "Live trading" below).

## Start in capture-only mode

```bash
export QT_WEBHOOK_SECRET='replace-with-a-long-random-secret'
export QT_TRADING_MODE='capture_only'
npm start
```

Open `http://127.0.0.1:4173`.

The local webhook endpoint is:

```text
POST http://127.0.0.1:4180/webhooks/tradingview
```

Port `4173` is the private dashboard/API listener. Port `4180` is a separate
public-edge listener that exposes only webhook ingress and minimal health.
Point ngrok or another authenticated, rate-limited TLS edge at port `4180`;
never expose port `4173`.

## Start the controlled paper adapter

First start a manually authenticated paper TWS or IB Gateway session. Then, in
one terminal:

```bash
export QT_IBKR_PAPER_ACCOUNT='DU...'
export QT_IBKR_PAPER_ACCOUNT_ALLOWLIST='DU...'
export QT_ALLOWED_SYMBOLS='QQQ'
export QT_TRADING_CLASS_ALLOWLIST='QQQ'
export QT_CORE_TOKEN='replace-with-a-different-long-random-secret'
PYTHONPATH=core python3 -m quickytrade_core
```

In a second terminal, use the same core token:

```bash
export QT_WEBHOOK_SECRET='replace-with-a-long-random-secret'
export QT_CORE_TOKEN='replace-with-a-different-long-random-secret'
export QT_ALLOWED_TICKERS='QQQ'
export QT_TRADING_MODE='paper_tws'
npm start
```

This mode currently verifies the controlled paper adapter and readiness only.
New app-managed alerts remain execution-ineligible until fills, commissions,
partial-fill/cancel handling, bracket protection, and reconciliation are
projected into the app. Keep TWS visible and follow the paper runbook.

## Verify

```bash
npm test
npm run check
```

See:

- [Implementation and scope](docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md)
- [Tauri v2 macOS desktop](docs/TAURI_MACOS_DESKTOP.md)
- [TradingView alert setup](docs/TRADINGVIEW_ALERT_SETUP.md)
- [IBKR paper runbook](docs/IBKR_PAPER_RUNBOOK.md)
- [Requirement traceability](docs/REQUIREMENT_TRACEABILITY.md)
- [0DTE strike-selection requirements](docs/0DTE_STRIKE_SELECTION_REQUIREMENTS.md) (proposed, not implemented)
