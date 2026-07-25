---
name: run-desktop
description: Build, run, and drive the QuickyTrade Tauri desktop app. Use when asked to start the desktop app, take a screenshot of it, validate its UI/dropdowns, or interact with it end-to-end (e.g. an end-user functionality review).
---

QuickyTrade's desktop shell is a Tauri v2 app (`src-tauri/`) that renders in a native macOS `WKWebView`. This repo's dev environment is a real macOS session with WindowServer access — no `xvfb`/`--no-sandbox` workarounds are needed, unlike a headless Linux container.

Playwright cannot attach to a native `WKWebView` the way it attaches to Electron's Chromium (no CDP). So the driver REPL at `.claude/skills/run-desktop/driver.mjs` launches the real `tauri dev` shell as a child process — proving the actual desktop launch path boots `server.mjs` and passes its readiness probe — and then drives the same loopback URL the shell loads (`http://127.0.0.1:4173/`) via a plain Playwright Chromium tab. This exercises identical DOM/`app.js` behavior; it just can't screenshot the native window chrome or trigger OS-level dialogs.

All paths are relative to the repo root.

## Prerequisites

```bash
npm install --no-save playwright-core           # not a project dependency; installed on demand
node node_modules/playwright-core/cli.js install chromium   # browser binary, matched to the installed playwright-core version
```

Also requires the Rust toolchain (`rustc`/`cargo`) on `PATH` (or under `~/.cargo/bin`) — see `docs/TAURI_MACOS_DESKTOP.md` for install steps. The first `tauri dev` after a clean checkout compiles the Rust binary and can take a couple of minutes; subsequent runs are fast.

## Run (agent path)

```bash
node .claude/skills/run-desktop/driver.mjs
```

Wrap in tmux for interactive use:

```bash
tmux new-session -d -s qt -x 200 -y 50
tmux send-keys -t qt 'cd /Users/alensam/Projects/QuickyTrade-gpt && node .claude/skills/run-desktop/driver.mjs' Enter
timeout 20 bash -c 'until tmux capture-pane -t qt -p | grep -q "driver>"; do sleep 0.2; done'
tmux send-keys -t qt 'launch' Enter
timeout 150 bash -c 'until tmux capture-pane -t qt -p | grep -q "launched\|ERROR"; do sleep 0.2; done'
tmux send-keys -t qt 'ss landing' Enter
tmux capture-pane -t qt -p
```

Screenshots land in `/tmp/qt-shots/` (override: `SCREENSHOT_DIR`).

### Commands

| command | what it does |
|---|---|
| `launch` | spawn `npm run desktop:dev` (tauri dev), wait for `/healthz`, open a browser tab at the app's loopback URL |
| `ss [name]` | screenshot → `/tmp/qt-shots/<name>.png` |
| `click <css-sel>` | click element via DOM (not coordinates) |
| `click-text <text>` | click button/link/option containing text |
| `select <css-sel> <value>` | set a `<select>`'s value and fire `change` — use this for dropdowns, not `click` |
| `fill <css-sel> <text>` | set an input's value and fire `input` |
| `type <text>` / `press <key>` | keyboard input |
| `wait <css-sel>` | wait for element, 10s timeout |
| `eval <js>` | evaluate in the page, print JSON |
| `text [css-sel]` | print innerText |
| `dropdowns` | list every `<select>` on the page with its id, disabled state, and options — fast way to enumerate all dropdowns for a review |
| `windows` | print the driven tab's URL (there's exactly one — the native Tauri window is separate and not enumerable here) |
| `quit` | close the browser tab and terminate the tauri dev process group |

## Run (human path)

```bash
npm run desktop:dev   # opens a real native window
```

## Gotchas

- Manual-desk dropdowns are wired to native `change` listeners (`app.js`), so `click` on an `<option>` alone won't trigger the app's state update — use `select <sel> <value>` instead.
- The driven tab is a normal Chromium page hitting the same loopback URL the native window loads — it does **not** exercise the Tauri shell's own navigation/origin-restriction code path (`src-tauri/src/main.rs`'s `validated_loopback_url`), only `server.mjs` and `app.js`. If you need to verify the shell's own loopback enforcement, that's covered by `src-tauri`'s Rust unit tests, not this driver.
- `launch` waits up to 120s for `/healthz` — a clean-checkout Rust compile can eat most of that. If it still isn't ready, check the `[tauri]`-prefixed output the driver prints for a build error before assuming the service itself is broken.

## Troubleshooting

- **`launch` reports an error and exits:** read the `[tauri]`-prefixed lines printed above it — usually a missing Rust toolchain (`rustup` not installed / not on `PATH`) or a stale `src-tauri/target/` needing a clean rebuild.
- **Port already in use:** something else is bound to `4173` (e.g. a leftover `server.mjs` from a previous run) — `lsof -iTCP:4173 -sTCP:LISTEN -n -P` and kill it, then retry.
- **Blank screenshot:** confirm the tab actually reached `http://127.0.0.1:4173/` with `eval "location.href"`; if the tauri process is still mid-build, `launch` should still be waiting rather than returning early.
- **`browserType.launch: Executable doesn't exist at .../chromium_headless_shell-<rev>`:** the installed browser revision doesn't match this repo's `playwright-core` version. Run `node node_modules/playwright-core/cli.js install chromium` (not bare `npx playwright install`, which can resolve a different `playwright` package version and fetch the wrong revision).
