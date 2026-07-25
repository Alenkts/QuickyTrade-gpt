# Tauri v2 macOS desktop shell

QuickyTrade's macOS desktop application runs via a lightweight Tauri v2 shell around the local Node service (~5–10 MB binary, ~40–60 MB RAM). It is the sole desktop shell — the earlier Electron shell has been retired.

## Security boundary

The shell loads only loopback origins (`http://127.0.0.1:4173`). The renderer runs in macOS `WKWebView` with isolated context and strict CSP rules. It cannot access arbitrary IPC, node APIs, or the local filesystem directly. External navigation, webviews, and permission requests are blocked.

The Tauri shell never opens an IBKR socket connection itself. The local Node service (`server.mjs`) and Python official core (`core/quickytrade_core`) remain the sole authorities for intent persistence, contract verification, and order placement.

## Prerequisites

- Node.js 22.5 or newer
- Rust toolchain (`rustc` and `cargo` 1.77+). Install via [rustup](https://rustup.rs):
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  source "$HOME/.cargo/env"
  ```
  *(Note: If rustup was just installed, open terminal sessions need `source "$HOME/.cargo/env"` or a shell restart to add `cargo` to their active PATH).*

## Development and Build

Start the Tauri development shell:

```bash
npm run desktop:tauri:dev
```

By default it spawns the local service on `127.0.0.1:4173` and stores service data below the root `data/` directory.

To build packaged native macOS app artifacts (.app / .dmg):

```bash
npm run desktop:tauri:build
```

The output artifacts will be created under `src-tauri/target/release/bundle/`.

## TradingView Tunnels

If TradingView uses ngrok or another TLS tunnel, point it at the dedicated loopback ingress port `4180`. That listener exposes only `/webhooks/tradingview` and minimal health. Never expose the Tauri UI on `4173`, the connection-profile API, `/api/` routes, or the private IBKR core.
