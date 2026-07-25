// REPL driver for the QuickyTrade desktop app. Runs on macOS (this repo's
// dev environment has a real WindowServer session — no xvfb needed).
// Designed for agents: wrap in tmux, send-keys commands, capture-pane output.
//
// The desktop shell is Tauri v2 (`src-tauri/`), which renders in a native
// macOS WKWebView. Playwright has no CDP-style attach for WKWebView (unlike
// Electron's Chromium), so this driver launches the real Tauri dev shell as
// a child process (proving the actual desktop launch path boots the local
// service correctly) and then drives the same loopback URL the shell loads
// (http://127.0.0.1:4173/) via a plain Playwright Chromium tab. This exercises
// identical DOM/app.js behavior to the native window; it just can't screenshot
// the native window chrome or trigger OS-level dialogs.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import * as readline from 'node:readline';
import * as fs from 'node:fs';
import * as path from 'node:path';

const APP_DIR = path.resolve(import.meta.dirname, '../../..');
const SHOT_DIR = process.env.SCREENSHOT_DIR || '/tmp/qt-shots';
fs.mkdirSync(SHOT_DIR, { recursive: true });

const PORT = process.env.QT_DESKTOP_PORT || '4173';
const TARGET_URL = `http://127.0.0.1:${PORT}/`;

let tauriChild = null;
let browser = null;
let page = null;

async function waitForServer() {
  const deadline = Date.now() + 120_000; // first Rust build can be slow; target/ is usually already built
  let lastError;
  while (Date.now() < deadline) {
    if (tauriChild && tauriChild.exitCode !== null) {
      throw new Error(`tauri process exited before the server became ready (code=${tauriChild.exitCode})`);
    }
    try {
      const response = await fetch(`${TARGET_URL}healthz`, { signal: AbortSignal.timeout(500) });
      if (response.ok) return;
      lastError = new Error(`Local service returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error('QuickyTrade local service did not become ready', { cause: lastError });
}

const COMMANDS = {
  async launch() {
    if (page) return console.log('already launched');
    console.log('starting tauri dev shell (npm run desktop:dev)...');
    tauriChild = spawnTauri();
    try {
      await waitForServer();
    } catch (error) {
      console.log('ERROR:', error.message);
      await COMMANDS.quit();
      return;
    }
    browser = await chromium.launch();
    page = await browser.newPage();
    await page.goto(TARGET_URL, { waitUntil: 'load', timeout: 15_000 });
    console.log('launched. driving', TARGET_URL, 'via a plain browser tab (the native Tauri window is separate and cannot be automated by Playwright).');
  },

  async ss(name) {
    if (!page) return console.log('ERROR: launch first');
    const f = path.join(SHOT_DIR, (name || `ss-${Date.now()}`) + '.png');
    await page.screenshot({ path: f });
    console.log('screenshot:', f);
  },

  // DOM click, not locator.click() — avoids coordinate math entirely.
  async click(sel) {
    if (!page) return console.log('ERROR: launch first');
    const r = await page.evaluate((s) => {
      const el = document.querySelector(s);
      if (!el) return 'NOT_FOUND';
      el.click();
      return 'OK';
    }, sel);
    console.log('click', sel, '→', r);
  },

  async 'click-text'(text) {
    if (!page) return console.log('ERROR: launch first');
    const r = await page.evaluate((t) => {
      const els = [...document.querySelectorAll('button, a, [role="button"], option')];
      const el = els.find((e) => e.textContent?.trim() === t) ?? els.find((e) => e.textContent?.includes(t));
      if (!el) return 'NOT_FOUND';
      el.click();
      return 'OK: ' + el.tagName;
    }, text);
    console.log('click-text', JSON.stringify(text), '→', r);
  },

  // Select a <select>'s option by value and fire a change event — the
  // manual-desk dropdowns (#manualStrikeSelection, #manualRight, etc.) are
  // wired via native `change` listeners, so setting .value alone is not enough.
  async select(args) {
    if (!page) return console.log('ERROR: launch first');
    const [sel, value] = args.split(/\s+/);
    const r = await page.evaluate(([s, v]) => {
      const el = document.querySelector(s);
      if (!el) return 'NOT_FOUND';
      el.value = v;
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return 'OK: ' + el.value;
    }, [sel, value]);
    console.log('select', sel, value, '→', r);
  },

  async type(text) { if (page) await page.keyboard.type(text, { delay: 30 }); },
  async press(key) { if (page) await page.keyboard.press(key); },

  async fill(args) {
    if (!page) return console.log('ERROR: launch first');
    const [sel, ...rest] = args.split(/\s+/);
    const value = rest.join(' ');
    const r = await page.evaluate(([s, v]) => {
      const el = document.querySelector(s);
      if (!el) return 'NOT_FOUND';
      el.value = v;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return 'OK';
    }, [sel, value]);
    console.log('fill', sel, '→', r);
  },

  async wait(sel) {
    if (!page) return console.log('ERROR: launch first');
    try {
      await page.waitForSelector(sel, { timeout: 10_000 });
      console.log('found:', sel);
    } catch {
      console.log('TIMEOUT:', sel);
    }
  },

  async eval(expr) {
    if (!page) return console.log('ERROR: launch first');
    try {
      console.log(JSON.stringify(await page.evaluate(expr)));
    } catch (e) {
      console.log('ERROR:', e.message);
    }
  },

  async text(sel) {
    if (!page) return console.log('ERROR: launch first');
    console.log(await page.evaluate(
      (s) => (s ? document.querySelector(s) : document.body)?.innerText ?? '(null)',
      sel || null,
    ));
  },

  // List every <select>'s id and its <option> values/labels — fast way to
  // enumerate "every dropdown" without hand-writing a selector per field.
  async dropdowns() {
    if (!page) return console.log('ERROR: launch first');
    const data = await page.evaluate(() => [...document.querySelectorAll('select')].map((sel) => ({
      id: sel.id || '(no id)',
      disabled: sel.disabled,
      value: sel.value,
      options: [...sel.options].map((o) => ({ value: o.value, label: o.textContent.trim() })),
    })));
    console.log(JSON.stringify(data, null, 2));
  },

  async windows() {
    if (!page) return console.log('ERROR: launch first');
    console.log(' ', page.url(), '(single browser tab — the native Tauri window is separate and not enumerable here)');
  },

  async quit() {
    if (browser) await browser.close().catch(() => {});
    browser = null;
    page = null;
    if (tauriChild && tauriChild.exitCode === null) {
      try { process.kill(-tauriChild.pid, 'SIGTERM'); } catch { /* already gone */ }
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, 3000);
        tauriChild.once('exit', () => { clearTimeout(timer); resolve(); });
      });
      if (tauriChild.exitCode === null) {
        try { process.kill(-tauriChild.pid, 'SIGKILL'); } catch { /* already gone */ }
      }
    }
    tauriChild = null;
  },
  help() { console.log('commands:', Object.keys(COMMANDS).join(', ')); },
};

function spawnTauri() {
  // detached so `-pid` targets the whole process group (tauri dev → cargo → the
  // compiled binary → node server.mjs), matching src-tauri's own ChildGuard
  // teardown pattern for the node child it owns.
  const child = spawn('npm', ['run', 'desktop:dev'], {
    cwd: APP_DIR,
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (d) => process.stdout.write(`[tauri] ${d}`));
  child.stderr.on('data', (d) => process.stderr.write(`[tauri] ${d}`));
  return child;
}

const stdin = fs.createReadStream(null, { fd: fs.openSync('/dev/stdin', 'r') });
const rl = readline.createInterface({ input: stdin, output: process.stdout, prompt: 'driver> ' });

rl.on('line', async (line) => {
  const [cmd, ...rest] = line.trim().split(/\s+/);
  if (!cmd) return rl.prompt();
  const fn = COMMANDS[cmd];
  if (!fn) { console.log('unknown:', cmd, '— try: help'); return rl.prompt(); }
  try { await fn(rest.join(' ')); } catch (e) { console.log('ERROR:', e.message); }
  if (cmd === 'quit') { rl.close(); process.exit(0); }
  rl.prompt();
});
rl.on('close', async () => { await COMMANDS.quit(); process.exit(0); });

console.log('QuickyTrade desktop driver — "help" for commands, "launch" to start');
rl.prompt();
