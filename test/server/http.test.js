// End-to-end HTTP tests for server.mjs's Phase 6 additions: the
// positions/close wiring and the public ingress route allowlist. server.mjs
// starts its HTTP listeners as an unconditional side effect of being
// imported (see the bottom of that file), so -- unlike the other test files
// in this repo, which import pure modules directly -- these tests spawn it
// as a real child process (pointed at a small in-test fake core over HTTP)
// and exercise it exactly the way TradingView/the operator UI would: real
// requests over loopback HTTP, nothing mocked at the module level.

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { randomUUID, createHmac } from 'node:crypto';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const CORE_TOKEN = 'x'.repeat(40);

async function freePort() {
  return await new Promise((resolvePort, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolvePort(port));
    });
  });
}

// A minimal, fully test-controlled stand-in for the Python core's private
// HTTP surface -- only the routes this test flow actually needs
// (healthz/preview-trade/place-trade/close-trade/positions/protection).
// `state` is mutated between assertions to switch its close-trade behavior
// between a definitive SUBMITTED, a definitive BLOCKED, and the two distinct
// shapes an ambiguous/unknown outcome can take on the wire (a 200 body with
// status:SUBMISSION_UNKNOWN, or a >=500 with no usable body) -- both must
// map to the same SUBMISSION_UNKNOWN outcome Node-side (see
// twsAdapter.closeTrade).
function createFakeCore() {
  const state = {
    closeBehavior: 'SUBMITTED',
    closeCallCount: 0,
    healthzReady: true,
    healthzCode: null,
    positionsItems: [],
    positionsBehavior: 'OK',
    reconciliationUnresolved: {
      hasUnresolvedSubmission: false,
      hasUnresolvedProtection: false,
      hasUnresolvedTransition: false,
      closeSubmissionUnknownCount: 0,
      protectionCancelUnknownCount: 0,
    },
  };
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => {
      let body = null;
      try {
        body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : null;
      } catch {
        body = null;
      }
      const url = new URL(req.url, 'http://localhost');
      const send = (status, payload) => {
        const text = JSON.stringify(payload);
        res.writeHead(status, { 'content-type': 'application/json' });
        res.end(text);
      };

      if (url.pathname === '/healthz' && req.method === 'GET') {
        return send(200, {
          ready: state.healthzReady, environment: 'PAPER', accountMask: '•••1234',
          ibkrHost: '127.0.0.1', ibkrPort: 7497, strikeSelection: null,
          ...(state.healthzReady ? {} : { code: state.healthzCode }),
        });
      }
      if (url.pathname === '/private/v1/preview-trade' && req.method === 'POST') {
        return send(200, {
          status: 'PREVIEW_READY', correlationId: body.correlationId, account: 'DU12345', conId: 999,
          localSymbol: `${body.signal.ticker} OPT`, action: 'BUY', quantity: 1, orderType: 'LMT',
          limitPrice: '1.05', tif: 'DAY', previewOnly: true, requiresRevalidationOnSubmit: true,
          source: body.source, ownership: 'APP_OWNED', managementMode: body.managementMode || 'ENTRY_ONLY',
          managementPolicy: body.managementPolicy || null, managementStatus: 'ENTRY_ONLY',
        });
      }
      if (url.pathname === '/private/v1/place-trade' && req.method === 'POST') {
        return send(200, {
          status: 'SUBMITTED', correlationId: body.correlationId, brokerOrderId: '9001', clientId: 71,
          permId: 9001, parentId: 0, ocaGroup: null, rawBrokerStatus: 'PreSubmitted',
          orderRef: 'QTtest', account: 'DU12345', conId: 999, action: 'BUY', quantity: 1,
          orderType: 'LMT', limitPrice: '1.05', tif: 'DAY', warnings: [],
          source: body.source, ownership: 'APP_OWNED', managementMode: body.managementMode || 'ENTRY_ONLY',
          managementPolicy: body.managementPolicy || null, managementStatus: 'ENTRY_ONLY',
        });
      }
      if (url.pathname === '/private/v1/close-trade' && req.method === 'POST') {
        state.closeCallCount += 1;
        if (state.closeBehavior === 'SUBMITTED') {
          return send(200, {
            status: 'SUBMITTED', correlationId: body.correlationId, brokerOrderId: '9100',
            quantity: body.signal.quantity || 4, action: 'SELL',
          });
        }
        if (state.closeBehavior === 'BLOCKED') {
          return send(400, { status: 'BLOCKED', code: 'REDUCE_ONLY_BOUND_EXCEEDED', correlationId: body.correlationId });
        }
        if (state.closeBehavior === 'AMBIGUOUS_200') {
          return send(200, { status: 'SUBMISSION_UNKNOWN', code: 'SUBMISSION_OUTCOME_UNRESOLVED', correlationId: body.correlationId });
        }
        if (state.closeBehavior === 'SERVER_ERROR') {
          return send(503, { status: 'SUBMISSION_UNKNOWN', code: 'CORE_OUTCOME_UNAVAILABLE' });
        }
        return send(500, {});
      }
      if (url.pathname === '/private/v1/positions' && req.method === 'GET') {
        if (state.positionsBehavior === 'MALFORMED') return send(200, { status: 'OK' }); // items missing.
        if (state.positionsBehavior === 'SOCKET_RESET') { req.socket.destroy(); return undefined; }
        return send(200, { status: 'OK', items: state.positionsItems });
      }
      if (url.pathname === '/private/v1/protection' && req.method === 'GET') {
        return send(200, {
          status: 'OK', correlationId: url.searchParams.get('correlationId'), protectionLegs: [], transitions: [],
        });
      }
      if (url.pathname === '/private/v1/reconciliation' && req.method === 'GET') {
        return send(200, {
          status: 'OK', recentRunsStatus: 'OK', recentRuns: [{ run_id: 1, run_type: 'PERIODIC' }],
          unresolved: state.reconciliationUnresolved,
        });
      }
      return send(404, { status: 'NOT_FOUND' });
    });
  });
  return { server, state };
}

async function waitForReady(url, { timeoutMs = 10_000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(500) });
      if (response.ok) return;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`Timed out waiting for ${url} to become ready: ${lastError?.message}`);
}

let child;
let dataDir;
let fakeCore;
let PORT;
let INGRESS_PORT;
let CORE_PORT;
let base;
let ingressBase;

before(async () => {
  fakeCore = createFakeCore();
  await new Promise((resolveListen) => fakeCore.server.listen(0, '127.0.0.1', resolveListen));
  CORE_PORT = fakeCore.server.address().port;

  PORT = await freePort();
  INGRESS_PORT = await freePort();
  base = `http://127.0.0.1:${PORT}`;
  ingressBase = `http://127.0.0.1:${INGRESS_PORT}`;

  dataDir = mkdtempSync(join(tmpdir(), 'quickytrade-http-test-'));

  child = spawn(process.execPath, ['server.mjs'], {
    cwd: ROOT,
    env: {
      ...process.env,
      PORT: String(PORT),
      QT_WEBHOOK_INGRESS_PORT: String(INGRESS_PORT),
      QT_TRADING_MODE: 'paper_tws',
      QT_CORE_URL: `http://127.0.0.1:${CORE_PORT}`,
      QT_CORE_TOKEN: CORE_TOKEN,
      QT_DATA_DIR: dataDir,
      QT_WEBHOOK_SECRET: 'a'.repeat(40),
      QT_ALLOWED_TICKERS: 'QQQ',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', () => {});
  child.stderr.on('data', () => {});

  await waitForReady(`${base}/healthz`);
  // Force an immediate core readiness check rather than waiting out the
  // background ~2s drainTimer tick, so tests aren't flaky/slow.
  await fetch(`${base}/api/tradingview/runtime?refresh=1`);
});

after(async () => {
  await new Promise((resolveClose) => {
    if (!child || child.killed) return resolveClose();
    child.once('exit', resolveClose);
    child.kill('SIGTERM');
  });
  await new Promise((resolveClose) => fakeCore.server.close(resolveClose));
  rmSync(dataDir, { recursive: true, force: true });
});

// ---- public ingress route allowlist regression -----------------------

test('public ingress listener still exposes only /healthz and /webhooks/tradingview', async () => {
  const healthz = await fetch(`${ingressBase}/healthz`);
  assert.equal(healthz.status, 200);

  const webhook = await fetch(`${ingressBase}/webhooks/tradingview`, { method: 'POST' });
  assert.notEqual(webhook.status, 404); // recognized, even though this bare request is itself rejected.
  const webhookBody = await webhook.json();
  assert.notEqual(webhookBody.code, 'PUBLIC_ROUTE_NOT_FOUND');

  for (const [path, method] of [
    ['/api/trades/active', 'GET'],
    [`/api/trades/${encodeURIComponent('manual:x')}/close`, 'POST'],
    ['/private/v1/positions', 'GET'],
    ['/private/v1/protection?correlationId=x', 'GET'],
    ['/private/v1/executions?correlationId=x', 'GET'],
    ['/private/v1/reconciliation', 'GET'],
    ['/api/reconciliation', 'GET'],
    ['/api/tradingview/runtime', 'GET'],
    ['/api/connection-profiles', 'GET'],
    ['/api/manual-action-blocks', 'GET'],
  ]) {
    const response = await fetch(`${ingressBase}${path}`, { method });
    assert.equal(response.status, 404, `expected ${method} ${path} to 404 on the public ingress listener`);
    const responseBody = await response.json();
    assert.equal(responseBody.code, 'PUBLIC_ROUTE_NOT_FOUND');
  }
});

// ---- checkPositions() fail-closed behavior (via GET /api/trades/active) --

test('positions read fails closed to an explicit unavailable state on a core error, never an empty list', async () => {
  fakeCore.state.positionsBehavior = 'SOCKET_RESET'; // exercises the exact same catch{} branch a real AbortSignal timeout would.
  const response = await fetch(`${base}/api/trades/active?refresh=1`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.notEqual(body.positionsStatus, 'OK');
  assert.equal(body.items, null); // never [] -- "unavailable" must never look like "confirmed empty".
  assert.ok(body.reason);
  fakeCore.state.positionsBehavior = 'OK';
});

test('positions read fails closed to an explicit unavailable state on a malformed core response', async () => {
  fakeCore.state.positionsBehavior = 'MALFORMED';
  const response = await fetch(`${base}/api/trades/active?refresh=1`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.notEqual(body.positionsStatus, 'OK');
  assert.equal(body.items, null);
  fakeCore.state.positionsBehavior = 'OK';
});

test('positions read reports confirmed-empty distinctly once the core is healthy again', async () => {
  fakeCore.state.positionsItems = [];
  const response = await fetch(`${base}/api/trades/active?refresh=1`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.positionsStatus, 'OK');
  assert.deepEqual(body.items, []); // confirmed empty, not unavailable.
});

test('closed positions today is read-only, New-York-day scoped, and remains distinct from active positions', async () => {
  const now = new Date();
  const old = new Date(now.getTime() - (48 * 60 * 60 * 1000));
  fakeCore.state.positionsItems = [
    {
      correlation_id: randomUUID(), account: 'DU12345', con_id: 701, symbol: 'SPY',
      opened_quantity: '2', closed_quantity: '2', open_quantity: '0', entry_avg_price: '1.05',
      realized_pnl: '35.50', total_commission: '1.20', lifecycle_status: 'CLOSED',
      last_reconciled_at: now.toISOString(), closed_at: now.toISOString(), updated_at: now.toISOString(),
    },
    {
      correlation_id: randomUUID(), account: 'DU12345', con_id: 702, symbol: 'QQQ',
      opened_quantity: '1', closed_quantity: '1', open_quantity: '0', entry_avg_price: '1.10',
      realized_pnl: '10.00', total_commission: '0.60', lifecycle_status: 'CLOSED',
      last_reconciled_at: old.toISOString(), closed_at: old.toISOString(), updated_at: old.toISOString(),
    },
  ];
  const [activeResponse, closedResponse] = await Promise.all([
    fetch(`${base}/api/trades/active?refresh=1`),
    fetch(`${base}/api/trades/closed-today?refresh=1`),
  ]);
  const active = await activeResponse.json();
  const closed = await closedResponse.json();
  assert.equal(active.positionsStatus, 'OK');
  assert.deepEqual(active.items, []);
  assert.equal(closed.positionsStatus, 'OK');
  assert.equal(closed.items.length, 1);
  assert.equal(closed.items[0].symbol, 'SPY');
  assert.equal(closed.items[0].quantityClosed, '2');
  assert.equal(closed.items[0].pnlFormatted, '+$35.50');
  assert.equal(closed.timeZone, 'America/New_York');
});

test('closed positions today fails closed rather than claiming no closes when broker data is unavailable', async () => {
  fakeCore.state.positionsBehavior = 'SOCKET_RESET';
  const response = await fetch(`${base}/api/trades/closed-today?refresh=1`);
  const body = await response.json();
  assert.notEqual(body.positionsStatus, 'OK');
  assert.equal(body.items, null);
  fakeCore.state.positionsBehavior = 'OK';
});

// ---- POST /api/trades/:correlationId/close request-shape validation ------

test('close endpoint rejects an invalid mode', async () => {
  const response = await fetch(`${base}/api/trades/${encodeURIComponent('manual:x')}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'SOMETHING_ELSE', requestId: randomUUID() }),
  });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.code, 'CLOSE_MODE_INVALID');
});

test('close endpoint requires a stable client-supplied UUID requestId', async () => {
  const response = await fetch(`${base}/api/trades/${encodeURIComponent('manual:x')}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'FULL_FLATTEN' }),
  });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.code, 'CLOSE_REQUEST_ID_REQUIRED');
});

test('close endpoint requires a positive integer quantity for REDUCE_ONLY_PARTIAL', async () => {
  const response = await fetch(`${base}/api/trades/${encodeURIComponent('manual:x')}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'REDUCE_ONLY_PARTIAL', requestId: randomUUID() }),
  });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.code, 'CLOSE_QUANTITY_INVALID');
});

test('close endpoint rejects a quantity supplied alongside FULL_FLATTEN', async () => {
  const response = await fetch(`${base}/api/trades/${encodeURIComponent('manual:x')}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'FULL_FLATTEN', quantity: 2, requestId: randomUUID() }),
  });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.code, 'CLOSE_QUANTITY_NOT_ALLOWED');
});

test('close endpoint fails closed with ENTRY_REFERENCE_NOT_FOUND for an unrecognized correlationId, never calling the core', async () => {
  const before_ = fakeCore.state.closeCallCount;
  const response = await fetch(`${base}/api/trades/${encodeURIComponent('manual:does-not-exist')}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'FULL_FLATTEN', requestId: randomUUID() }),
  });
  assert.equal(response.status, 202);
  const body = await response.json();
  assert.equal(body.submission.status, 'BLOCKED');
  assert.equal(body.submission.code, 'ENTRY_REFERENCE_NOT_FOUND');
  assert.equal(fakeCore.state.closeCallCount, before_);
});

// ---- durable audit trail for blocks that predate store.receive() ---------

test('a close/flatten block that happens before store.receive() is durably recorded and readable back', async () => {
  const requestId = randomUUID();
  const response = await fetch(`${base}/api/trades/${encodeURIComponent('manual:does-not-exist-2')}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'FULL_FLATTEN', requestId }),
  });
  assert.equal(response.status, 202);
  const body = await response.json();
  assert.equal(body.submission.code, 'ENTRY_REFERENCE_NOT_FOUND');

  const blocksResponse = await fetch(`${base}/api/manual-action-blocks`);
  assert.equal(blocksResponse.status, 200);
  const blocksBody = await blocksResponse.json();
  const recorded = blocksBody.items.find((item) => item.requestId === requestId);
  assert.ok(recorded, 'expected the block to be durably recorded and retrievable');
  assert.equal(recorded.action, 'FULL_FLATTEN');
  assert.equal(recorded.code, 'ENTRY_REFERENCE_NOT_FOUND');
  assert.equal(recorded.entryCorrelationId, 'manual:does-not-exist-2');
});

test('a flatten blocked by core unreadiness forwards the real reason, not a generic catch-all, and is durably recorded', async () => {
  fakeCore.state.healthzReady = false;
  fakeCore.state.healthzCode = 'RECONCILIATION_REQUIRED';
  await fetch(`${base}/api/tradingview/runtime?refresh=1`); // force coreSnapshot to re-poll immediately.

  const requestId = randomUUID();
  const response = await fetch(`${base}/api/trades/${encodeURIComponent('manual:whatever')}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'FULL_FLATTEN', requestId }),
  });
  assert.equal(response.status, 202);
  const body = await response.json();
  assert.equal(body.submission.status, 'BLOCKED');
  assert.equal(body.submission.code, 'RECONCILIATION_REQUIRED');
  assert.notEqual(body.submission.code, 'MANUAL_EXECUTION_UNAVAILABLE');

  const blocksResponse = await fetch(`${base}/api/manual-action-blocks`);
  const blocksBody = await blocksResponse.json();
  const recorded = blocksBody.items.find((item) => item.requestId === requestId);
  assert.ok(recorded, 'expected the readiness block to be durably recorded and retrievable');
  assert.equal(recorded.code, 'RECONCILIATION_REQUIRED');

  fakeCore.state.healthzReady = true;
  fakeCore.state.healthzCode = null;
  await fetch(`${base}/api/tradingview/runtime?refresh=1`); // restore for later tests.
});

// ---- BLOCKED/ambiguous mapping and idempotency, against a real seeded entry ---

async function seedOpenEntry() {
  const created = await fetch(`${base}/api/trade-intents/manual`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      profileId: 'paper-tws', symbol: 'QQQ', strikeSelection: 'EXACT', expiry: '20261231', strike: 500,
      right: 'C', entryPolicy: 'MARKETABLE_LIMIT', managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    }),
  });
  assert.equal(created.status, 202);
  const { intent } = await created.json();
  const submitted = await fetch(`${base}/api/trade-intents/manual/${encodeURIComponent(intent.id)}/submit`, { method: 'POST' });
  assert.equal(submitted.status, 202);
  const submission = (await submitted.json()).submission;
  assert.equal(submission.status, 'SUBMITTED');
  // Use the real UUID correlationId the submit call actually returned --
  // never the "manual:<id>" idempotencyKey shape, which is a distinct
  // identifier server.mjs's lookupOriginatingAlert must not confuse with the
  // core's correlation_id (see server.mjs's lookupOriginatingAlert comment).
  //
  // Verification gate 2. The core keys its own broker_submissions registry on
  // the *idempotencyKey* ("manual:<id>" / "tradingview:<alertId>"), not on the
  // UUID persistence receipt -- see engine.py's
  // `registry_key = normalized["idempotencyKey"]`. This fixture previously
  // seeded the UUID, which is a shape the real core never reports, so the whole
  // close-path suite passed against an impossible contract while
  // POST /api/trades/:id/close returned ENTRY_REFERENCE_NOT_FOUND for every
  // real position. Seed the registry shape the core actually produces.
  const entryCorrelationId = `manual:${intent.id}`;
  assert.notEqual(entryCorrelationId, submission.correlationId);
  fakeCore.state.positionsItems = [{
    correlation_id: entryCorrelationId, account: 'DU12345', con_id: 999, symbol: 'QQQ',
    opened_quantity: '4', closed_quantity: '0', open_quantity: '4', entry_avg_price: '1.05',
    realized_pnl: null, total_commission: null, lifecycle_status: 'FILLED', last_reconciled_at: null,
    operator_position_status: 'ACTIVE_CONFIRMED',
    updated_at: new Date().toISOString(),
  }];
  return entryCorrelationId;
}

test('active positions combine broker-confirmed position data with the originating alert', async () => {
  const entryCorrelationId = await seedOpenEntry();
  const response = await fetch(`${base}/api/trades/active?refresh=1`);
  const body = await response.json();
  assert.equal(body.positionsStatus, 'OK');
  const item = body.items.find((candidate) => candidate.correlationId === entryCorrelationId);
  assert.ok(item, 'expected the seeded position to be present');
  assert.equal(item.symbol, 'QQQ');
  assert.equal(item.right, 'C');
  assert.equal(item.quantity.open, '4');
});

test('close endpoint maps a core BLOCKED close-trade response through as BLOCKED', async () => {
  const entryCorrelationId = await seedOpenEntry();
  fakeCore.state.closeBehavior = 'BLOCKED';
  const response = await fetch(`${base}/api/trades/${encodeURIComponent(entryCorrelationId)}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'REDUCE_ONLY_PARTIAL', quantity: 1, requestId: randomUUID() }),
  });
  assert.equal(response.status, 202);
  const body = await response.json();
  assert.equal(body.submission.status, 'BLOCKED');
  assert.equal(body.submission.code, 'REDUCE_ONLY_BOUND_EXCEEDED');
});

test('close endpoint maps a 200-with-SUBMISSION_UNKNOWN core response as ambiguous', async () => {
  const entryCorrelationId = await seedOpenEntry();
  fakeCore.state.closeBehavior = 'AMBIGUOUS_200';
  const response = await fetch(`${base}/api/trades/${encodeURIComponent(entryCorrelationId)}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'FULL_FLATTEN', requestId: randomUUID() }),
  });
  assert.equal(response.status, 202);
  const body = await response.json();
  assert.equal(body.submission.status, 'SUBMISSION_UNKNOWN');
});

test('close endpoint maps a >=500 core response as ambiguous, identically to the 200-ambiguous shape', async () => {
  const entryCorrelationId = await seedOpenEntry();
  fakeCore.state.closeBehavior = 'SERVER_ERROR';
  const response = await fetch(`${base}/api/trades/${encodeURIComponent(entryCorrelationId)}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'FULL_FLATTEN', requestId: randomUUID() }),
  });
  assert.equal(response.status, 202);
  const body = await response.json();
  assert.equal(body.submission.status, 'SUBMISSION_UNKNOWN');
});

test('close endpoint never submits a second order for a retried (same) requestId', async () => {
  const entryCorrelationId = await seedOpenEntry();
  fakeCore.state.closeBehavior = 'SUBMITTED';
  const requestId = randomUUID();
  const callCountBefore = fakeCore.state.closeCallCount;

  const first = await fetch(`${base}/api/trades/${encodeURIComponent(entryCorrelationId)}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'REDUCE_ONLY_PARTIAL', quantity: 1, requestId }),
  });
  const firstBody = await first.json();
  assert.equal(firstBody.submission.status, 'SUBMITTED');

  const second = await fetch(`${base}/api/trades/${encodeURIComponent(entryCorrelationId)}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode: 'REDUCE_ONLY_PARTIAL', quantity: 1, requestId }),
  });
  const secondBody = await second.json();
  assert.equal(secondBody.submission.status, 'SUBMITTED');
  assert.equal(secondBody.submission.brokerOrderId, firstBody.submission.brokerOrderId);
  assert.equal(fakeCore.state.closeCallCount, callCountBefore + 1); // exactly one real broker call, not two.
});

// ---- GET /api/tradingview/runtime capability flags -----------------------
// Regression test: these must reflect what the running server can actually
// do, not a stale hardcoded false -- manual paper entry, app-managed
// protection/transitions, and TradingView auto-submit are all real, working
// capabilities in paper_tws mode with a ready, non-live core (this test
// harness's fixture); only live entry stays false, since this fixture's fake
// core reports PAPER.

// ---- GET /api/reconciliation proxy ----------------------------------------

test('reconciliation proxy surfaces the core\'s recentRuns and unresolved flags, including why a global block would exist', async () => {
  const response = await fetch(`${base}/api/reconciliation`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.status, 'OK');
  assert.equal(body.recentRunsStatus, 'OK');
  assert.ok(Array.isArray(body.recentRuns));
  assert.deepEqual(body.unresolved, {
    hasUnresolvedSubmission: false,
    hasUnresolvedProtection: false,
    hasUnresolvedTransition: false,
    closeSubmissionUnknownCount: 0,
    protectionCancelUnknownCount: 0,
  });
});

test('reconciliation proxy surfaces an unresolved transition exactly as the core reports it', async () => {
  fakeCore.state.reconciliationUnresolved = {
    hasUnresolvedSubmission: false,
    hasUnresolvedProtection: false,
    hasUnresolvedTransition: true,
    closeSubmissionUnknownCount: 0,
    protectionCancelUnknownCount: 0,
  };
  const response = await fetch(`${base}/api/reconciliation`);
  const body = await response.json();
  assert.equal(body.unresolved.hasUnresolvedTransition, true);
  fakeCore.state.reconciliationUnresolved = {
    hasUnresolvedSubmission: false,
    hasUnresolvedProtection: false,
    hasUnresolvedTransition: false,
    closeSubmissionUnknownCount: 0,
    protectionCancelUnknownCount: 0,
  };
});

test('runtime capabilities reflect real manual/app-managed availability, not a stale hardcoded false', async () => {
  const response = await fetch(`${base}/api/tradingview/runtime?refresh=1`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.capabilities.manualProposal, true);
  assert.equal(body.capabilities.manualPaperEntry, true);
  assert.equal(body.capabilities.appTradeManagement, true);
  // TradingView auto-submit is enabled for paper trading.
  assert.equal(body.capabilities.tradingviewPaperEntry, true);
  // This fixture's fake core reports PAPER, so live entry stays false too.
  assert.equal(body.capabilities.liveEntry, false);
});

// ---- verification gates from docs/REVIEW_2026-07-25.md --------------------

const WEBHOOK_SECRET = 'a'.repeat(40);

function signedWebhook(payload) {
  const raw = Buffer.from(JSON.stringify(payload));
  const signature = createHmac('sha256', WEBHOOK_SECRET).update(raw).digest('hex');
  return {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-tradingview-signature': `sha256=${signature}` },
    body: raw,
  };
}

function tradingViewAlert(overrides = {}) {
  return {
    schema_version: '1',
    alert_id: `gate-QQQ-C-${randomUUID()}`,
    sent_at: new Date().toISOString(),
    strategy_id: 'gate-strategy',
    strategy_version: '1.0',
    action: 'OPEN_LONG_CALL',
    ticker: 'QQQ',
    ...overrides,
  };
}

async function alertStatus(alertId) {
  const response = await fetch(`${base}/api/tradingview/alerts?limit=100`);
  const body = await response.json();
  return (body.items || []).find((item) => item.alertId === alertId) || null;
}

// Gate 1. This is the regression test for C3: server.mjs called
// operatorStore.getSelectedProfile() on every TradingView-sourced submission,
// and the method did not exist. The resulting TypeError was classified as
// ambiguous, so every alert was durably recorded as SUBMISSION_UNKNOWN --
// "an order may exist at IBKR" -- with provably zero broker contact. No test
// exercised placeTrade with source:'tradingview' at all; this one does.
test('a signed TradingView webhook reaches the core and ends SUBMITTED, never a fabricated SUBMISSION_UNKNOWN', async () => {
  const alert = tradingViewAlert();
  const accepted = await fetch(`${ingressBase}/webhooks/tradingview`, signedWebhook(alert));
  assert.equal(accepted.status, 202);
  const acceptedBody = await accepted.json();
  assert.equal(acceptedBody.accepted, true);

  let status = null;
  const deadline = Date.now() + 8_000;
  while (Date.now() < deadline) {
    status = await alertStatus(alert.alert_id);
    if (status && status.status !== 'READY' && status.status !== 'PROCESSING') break;
    await new Promise((r) => setTimeout(r, 100));
  }
  assert.ok(status, 'expected the alert to be durably recorded');
  assert.equal(status.status, 'SUBMITTED');
  assert.equal(status.order.brokerOrderId, '9001');
});

// Gate 6 (H8). An alert accepted while the core was unavailable must not be
// submitted once the core returns: three stale 0DTE entries drained within 8ms
// of each other after sitting READY for up to 3.5 hours. `sent_at` is inside
// the ingress window here, and the intent is aged past the drain budget by
// rewriting created_at directly -- exactly what a multi-hour outage produces.
test('an alert that goes stale in the queue expires instead of flushing on reconnect', async () => {
  const { TradingViewStore } = await import('../../src/tradingview/store.js');
  const alert = tradingViewAlert({ action: 'OPEN_LONG_PUT' });
  const accepted = await fetch(`${ingressBase}/webhooks/tradingview`, signedWebhook(alert));
  assert.equal(accepted.status, 202);

  // Age the queued row past QT_WEBHOOK_MAX_AGE_MS without waiting it out.
  const store = new TradingViewStore(join(dataDir, 'tradingview.sqlite'));
  try {
    const stale = new Date(Date.now() - (60 * 60 * 1000)).toISOString();
    store.db.prepare('UPDATE signal_intents SET created_at = ? WHERE alert_id = ?').run(stale, alert.alert_id);
  } finally {
    store.close();
  }

  // findNextReady must expire it rather than throw on the status CHECK
  // constraint and leave it READY (which is what shipped before this fix).
  await fetch(`${base}/api/tradingview/runtime?refresh=1`);
  await new Promise((r) => setTimeout(r, 500));

  const status = await alertStatus(alert.alert_id);
  assert.ok(status, 'expected the alert to still be recorded');
  assert.equal(status.status, 'EXPIRED');
  assert.equal(status.order, null, 'an expired alert must never have produced an order');
});

// C6. ATM_OFFSET is pure listed-strike geometry with no band check; accepting
// it from the wire hands contract selection to the Pine script author.
test('TradingView may not select contract geometry with ATM_OFFSET', async () => {
  const alert = tradingViewAlert({ strike_policy: { type: 'ATM_OFFSET', offset: 1 } });
  const response = await fetch(`${ingressBase}/webhooks/tradingview`, signedWebhook(alert));
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.code, 'STRIKE_POLICY_NOT_ALLOWED');
});

// M12. (source, alert_id) is the sole durable dedup key.
test('a bare-timestamp alert_id is rejected as an insufficient deduplication key', async () => {
  const response = await fetch(`${ingressBase}/webhooks/tradingview`,
    signedWebhook(tradingViewAlert({ alert_id: '2026-07-24T18:32:00Z' })));
  assert.equal(response.status, 400);
  assert.equal((await response.json()).code, 'INVALID_ALERT_ID');
});

// H11. The real attack is bodyless: a cross-site fetch with mode:'no-cors'
// sends no Content-Type, so a media-type allowlist alone never sees it.
// Stopping the core mid-position also stops the sweep that maintains stops.
test('a bodyless cross-site POST cannot stop the core', async () => {
  for (const headers of [
    { origin: 'https://evil.example' },
    { 'sec-fetch-site': 'cross-site' },
  ]) {
    const response = await fetch(`${base}/api/core/process/stop`, { method: 'POST', headers });
    assert.equal(response.status, 403, `expected 403 for ${JSON.stringify(headers)}`);
    assert.equal((await response.json()).code, 'CROSS_ORIGIN_FORBIDDEN');
  }
});

// C1. An absent Host header previously skipped validation entirely, which is a
// bypass sitting next to the control that exists to stop DNS rebinding.
test('Host validation rejects a non-loopback host and an absent host alike', async () => {
  // `Host` is a forbidden header name for fetch(), so these have to go out over
  // a raw socket -- which is also exactly how a non-browser attacker would send
  // them.
  const rawRequest = (requestLines) => new Promise((resolvePromise, reject) => {
    const socket = net.connect(PORT, '127.0.0.1', () => socket.write(requestLines));
    let data = '';
    socket.on('data', (chunk) => { data += chunk.toString('utf8'); });
    socket.on('end', () => resolvePromise(data));
    socket.on('error', reject);
  });

  const rebind = await rawRequest('GET /api/tradingview/runtime HTTP/1.1\r\nHost: evil.example\r\nConnection: close\r\n\r\n');
  assert.match(rebind.split('\r\n')[0], /^HTTP\/1\.[01] 403 /, 'a non-loopback Host must be refused');

  const noHost = await rawRequest('GET /api/tradingview/runtime HTTP/1.0\r\n\r\n');
  assert.match(noHost.split('\r\n')[0], /^HTTP\/1\.[01] 403 /, 'an absent Host must not skip validation');

  const loopback = await rawRequest(`GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:${PORT}\r\nConnection: close\r\n\r\n`);
  assert.match(loopback.split('\r\n')[0], /^HTTP\/1\.[01] 200 /, 'loopback must still be served');
});

// M10. The core's preview body carries the full account number and is rendered
// straight into the operator's review panel.
test('the manual preview never returns an unmasked account number', async () => {
  const created = await fetch(`${base}/api/trade-intents/manual`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      profileId: 'paper-tws', symbol: 'QQQ', strikeSelection: 'EXACT', expiry: '20261231', strike: 500,
      right: 'C', entryPolicy: 'MARKETABLE_LIMIT', managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    }),
  });
  assert.equal(created.status, 202);
  const { preview } = await created.json();
  assert.ok(preview, 'expected a core preview');
  assert.notEqual(preview.account, 'DU12345');
  assert.match(preview.account, /^DU\*+345$/);
});
