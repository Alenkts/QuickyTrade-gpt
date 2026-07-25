# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## What this is

QuickyTrade is an **IBKR-only, paper-first** desktop trading control center — not a signal/strategy generator. It ingests TradingView webhook alerts and manual proposals, durably records them, and (behind heavy gating) can submit **paper-only** IBKR orders. Live trading and automated position management are not implemented. Schwab, Robinhood, and built-in/Python strategy execution are out of scope.

## Stack & structure

- Node.js backend (`server.mjs`, ES modules, `"type": "module"`, stdlib only — no runtime npm deps) exposes two listeners: port `4173` (private dashboard/API — never expose) and `4180`/`QT_WEBHOOK_INGRESS_PORT` (public webhook-only edge for TradingView/ngrok).
- `app.js` + `index.html` — vanilla JS operator dashboard, no framework.
- `src-tauri/` — Tauri v2 macOS shell (Rust), spawns `server.mjs` as a child process, loads only loopback origins in an isolated `WKWebView`, navigation restricted to loopback.
- `src/tradingview/` — webhook ingress pipeline (auth, validation, processing, SQLite-backed store).
- `src/operator/store.js` — SQLite store for connection profiles and management-policy defaults.
- `core/` — separate Python package `quickytrade_core` (Python ≥3.11), a loopback-only service wrapping the **official IBKR TWS socket API** (not `ib_insync`). This is the only place that talks to IBKR's socket API; `server.mjs` calls it over an authenticated loopback HTTP boundary (`QT_CORE_TOKEN`). Requires the official TWS Python API installed separately (not pip-installable from a public index).
- `NativeApps/` — legacy, retired SwiftUI iOS/Mac companion apps. Fail-closed, not an active execution path.
- `docs/` — safety/requirements specs, notably `docs/IBKR_TRADINGVIEW_IMPLEMENTATION.md` (requirement IDs referenced by tests) and `docs/REQUIREMENT_TRACEABILITY.md`.

## Commands

```bash
npm start                # node server.mjs
npm run desktop:dev      # tauri dev (macOS desktop shell)
npm test                 # node --test (JS) && test:core (Python unittest)
npm run test:core        # PYTHONPATH=core python3 -m unittest discover -s core/tests -v
npm run check            # node --check on every entry file + python3 -m compileall + npm test
npm run desktop:package:mac  # tauri build
npm run lint                 # eslint . && npm run lint:core
npm run lint:core            # python3 -m ruff check core/quickytrade_core core/tests
```

ESLint (flat config in `eslint.config.js`) covers the JS side; ruff (`[tool.ruff]` in `core/pyproject.toml`, line-length 120) covers the Python core — install with `pip3 install --user ruff` if missing. No formatter is configured. JS tests use Node's built-in `node:test` + `node:assert/strict` (no Jest/Vitest/Mocha).

## Env vars

No `.env.example` exists; required vars are documented in `README.md` and `core/README.md`. Node side: `QT_WEBHOOK_SECRET`, `QT_TRADING_MODE` (`capture_only`|`paper_tws`), `QT_CORE_TOKEN`, `QT_CORE_URL`, `QT_ALLOWED_TICKERS`/`QT_ALLOWED_SYMBOLS`, `QT_ALLOWED_STRATEGIES`, `QT_DATA_DIR`, `QT_WEBHOOK_INGRESS_PORT`. Python core: `QT_IBKR_PAPER_ACCOUNT`(+`_ALLOWLIST`, must be `DU...` paper accounts — a live `U...` account is hard-rejected), `QT_ALLOWED_SYMBOLS`, `QT_TRADING_CLASS_ALLOWLIST`, `QT_CORE_TOKEN`, optional `QT_IBKR_HOST/PORT/CLIENT_ID`.

## Gotchas

- Ports `4173` (private) and `4180` (public webhook ingress) must never be the same — the server throws if they collide. Never expose `4173` externally.
- HTTP `200` on the core's response contract can mean a **definitive `BLOCKED`** result — it's used to distinguish "definitive no" (200) from "ambiguous/unknown" (non-2xx/timeout). Don't assume 200 = order succeeded.
- `src-tauri/target/` contains built `.dmg`/`.zip`/`.app` binaries (under `target/release/bundle/`) and is gitignored — don't `git add -f` build output into commits.
- `.codex/agents/*.toml` defines specialist routing (architect/trade-analyst/end-user/solution-designer/programmer/evaluator) for OpenAI Codex CLI — the delegation guidance in `AGENTS.md` (imported above) applies regardless of which CLI is driving.
