import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { DatabaseSync } from 'node:sqlite';

import {
  DedupeConflictError,
  IbkrAlertProcessor,
  SubmissionUnknownError,
  TradingViewStore,
  TradingViewWebhookIngress,
  WebhookError,
  parseAndValidatePayload,
  redact,
} from '../../src/tradingview/index.js';

const NOW = new Date('2026-07-18T15:00:00.000Z');
const SECRET = 'test-webhook-secret-32-chars-long-9f4e2a';
const BODY_AUTH_SECRET = 'body-token-with-more-than-32-random-characters-9f4e2a';

function payload(overrides = {}) {
  return {
    schema_version: '1',
    alert_id: 'tv-alert-001',
    sent_at: NOW.toISOString(),
    strategy_id: 'ema-breakout',
    strategy_version: '2.1.0',
    action: 'OPEN_LONG_CALL',
    ticker: 'SPY',
    target_dte: 0,
    strike_policy: { type: 'TARGET_RANGE' },
    risk_hint: { max_contracts: 2 },
    exit_policy_id: 'standard-bracket-v1',
    ...overrides,
  };
}

function body(overrides = {}) {
  return Buffer.from(JSON.stringify(payload(overrides)));
}

function hmacHeaders(rawBody, secret = SECRET) {
  return {
    'x-tradingview-signature': `sha256=${createHmac('sha256', secret).update(rawBody).digest('hex')}`,
  };
}

function ingress(store, options = {}) {
  return new TradingViewWebhookIngress({
    store,
    bearerSecret: SECRET,
    executionEligible: true,
    clock: () => NOW,
    ...options,
  });
}

test('accepts HMAC-SHA256 and bearer auth and rejects invalid credentials', () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  try {
    const hmacBody = body({ alert_id: 'hmac-1' });
    const hmacIngress = new TradingViewWebhookIngress({
      store,
      hmacSecret: SECRET,
      clock: () => NOW,
    });
    assert.equal(hmacIngress.handle({ headers: hmacHeaders(hmacBody), rawBody: hmacBody }).statusCode, 202);

    const bearerBody = body({ alert_id: 'bearer-1' });
    assert.equal(ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: bearerBody,
    }).body.status, 'READY');

    assert.throws(
      () => ingress(store).handle({
        headers: { authorization: 'Bearer incorrect-secret' },
        rawBody: body({ alert_id: 'bad-auth-1' }),
      }),
      (error) => error instanceof WebhookError && error.code === 'UNAUTHORIZED' && error.statusCode === 401,
    );
  } finally {
    store.close();
  }
});

test('strict validation enforces schema, fields, enum, body age, and clock skew', () => {
  assert.deepEqual(parseAndValidatePayload(body(), { now: NOW }), payload());

  const invalidCases = [
    [payload({ schema_version: '2' }), 'UNSUPPORTED_SCHEMA_VERSION'],
    [payload({ action: 'BUY' }), 'INVALID_ACTION'],
    [payload({ ticker: 'spy' }), 'INVALID_TICKER'],
    [{ ...payload(), account_id: 'DU123' }, 'UNKNOWN_FIELDS'],
    [payload({ sent_at: '2026-07-18T14:54:59.999Z' }), 'ALERT_TOO_OLD'],
    [payload({ sent_at: '2026-07-18T15:00:30.001Z' }), 'ALERT_FROM_FUTURE'],
  ];

  for (const [candidate, code] of invalidCases) {
    assert.throws(
      () => parseAndValidatePayload(Buffer.from(JSON.stringify(candidate)), { now: NOW }),
      (error) => error instanceof WebhookError && error.code === code,
    );
  }
});

test('extended payload validation enforces open, close, nested, and configured risk contracts', () => {
  const open = parseAndValidatePayload(body(), { now: NOW, maxRiskHintContracts: 2 });
  assert.deepEqual(open.strike_policy, { type: 'TARGET_RANGE' });
  assert.deepEqual(open.risk_hint, { max_contracts: 2 });

  const requirementExample = payload({ alert_id: 'requirements-open' });
  delete requirementExample.exit_policy_id;
  assert.equal(
    parseAndValidatePayload(Buffer.from(JSON.stringify(requirementExample)), { now: NOW }).alert_id,
    'requirements-open',
  );

  const close = payload({
    alert_id: 'close-1',
    action: 'CLOSE_LONG_CALL',
    entry_alert_id: 'entry-1',
  });
  delete close.target_dte;
  delete close.strike_policy;
  delete close.risk_hint;
  delete close.exit_policy_id;
  assert.equal(
    parseAndValidatePayload(Buffer.from(JSON.stringify(close)), { now: NOW }).entry_alert_id,
    'entry-1',
  );

  const minimalOpen = payload({ alert_id: 'minimal-open' });
  delete minimalOpen.target_dte;
  delete minimalOpen.strike_policy;
  const parsedMinimal = parseAndValidatePayload(Buffer.from(JSON.stringify(minimalOpen)), { now: NOW });
  assert.equal(parsedMinimal.target_dte, 0);
  assert.deepEqual(parsedMinimal.strike_policy, { type: 'TARGET_RANGE' });

  const missingCloseReference = { ...close, alert_id: 'missing-close' };
  delete missingCloseReference.entry_alert_id;
  assert.throws(
    () => parseAndValidatePayload(Buffer.from(JSON.stringify(missingCloseReference)), { now: NOW }),
    (error) => error.code === 'MISSING_CLOSE_REFERENCE',
  );

  assert.throws(
    () => parseAndValidatePayload(body({ strike_policy: { type: 'ATM_OFFSET', offset: 1, symbol: 'SPY' } }), { now: NOW }),
    (error) => error.code === 'INVALID_FIELD',
  );
  assert.throws(
    () => parseAndValidatePayload(body({ risk_hint: { max_contracts: 3 } }), {
      now: NOW,
      maxRiskHintContracts: 2,
    }),
    (error) => error.code === 'INVALID_RISK_HINT',
  );

  assert.throws(
    () => parseAndValidatePayload(body({ entry_alert_id: 'entry-1' }), { now: NOW }),
    (error) => error.code === 'INVALID_ACTION_FIELDS',
  );

  const bothCloseReferences = { ...close, trade_ref: 'tradingview:entry-1' };
  assert.throws(
    () => parseAndValidatePayload(Buffer.from(JSON.stringify(bothCloseReferences)), { now: NOW }),
    (error) => error.code === 'MISSING_CLOSE_REFERENCE',
  );
});

test('strike_policy accepts TARGET_RANGE as a minimal alternative to ATM_OFFSET', () => {
  const open = parseAndValidatePayload(body({ strike_policy: { type: 'TARGET_RANGE' } }), { now: NOW });
  assert.deepEqual(open.strike_policy, { type: 'TARGET_RANGE' });

  assert.throws(
    () => parseAndValidatePayload(body({ strike_policy: { type: 'TARGET_RANGE', offset: 1 } }), { now: NOW }),
    (error) => error instanceof WebhookError && error.code === 'INVALID_FIELD',
  );

  assert.throws(
    () => parseAndValidatePayload(body({ strike_policy: { type: 'BOGUS' } }), { now: NOW }),
    (error) => error instanceof WebhookError && error.code === 'INVALID_STRIKE_POLICY',
  );

  const stillAtmOffset = parseAndValidatePayload(body({ strike_policy: { type: 'ATM_OFFSET', offset: -2 } }), { now: NOW });
  assert.deepEqual(stillAtmOffset.strike_policy, { type: 'ATM_OFFSET', offset: -2 });
});

test('JSON auth_token is accepted and stripped before hashing, persistence, and logging', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quickytrade-body-auth-'));
  const databasePath = join(directory, 'alerts.sqlite');
  const logEntries = [];
  const store = new TradingViewStore(databasePath, { clock: () => NOW });
  try {
    const receiver = new TradingViewWebhookIngress({
      store,
      authTokenSecret: BODY_AUTH_SECRET,
      clock: () => NOW,
      logger: { info: (...args) => logEntries.push(args) },
    });
    const authenticatedBody = Buffer.from(JSON.stringify({
      ...payload({ alert_id: 'body-auth-1' }),
      auth_token: BODY_AUTH_SECRET,
    }));
    const acknowledgement = receiver.handle({ rawBody: authenticatedBody });
    assert.equal(acknowledgement.statusCode, 202);
    assert.equal(store.getAlertStatus('tradingview', 'body-auth-1').payload.auth_token, undefined);
    assert.equal(JSON.stringify(logEntries).includes(BODY_AUTH_SECRET), false);
    assert.throws(
      () => receiver.handle({
        rawBody: Buffer.from(JSON.stringify({
          ...payload({ alert_id: 'body-auth-invalid' }),
          auth_token: `${BODY_AUTH_SECRET}-wrong`,
        })),
      }),
      (error) => error.code === 'UNAUTHORIZED',
    );
    store.close();
    const bytes = await readFile(databasePath);
    assert.equal(bytes.includes(Buffer.from(BODY_AUTH_SECRET)), false);
  } finally {
    try { store.close(); } catch {}
    await rm(directory, { recursive: true, force: true });
  }
});

test('ingress applies a body-size bound before authentication', () => {
  const store = new TradingViewStore(':memory:');
  try {
    const receiver = ingress(store, { maxBodyBytes: 8 });
    assert.throws(
      () => receiver.handle({ headers: { authorization: `Bearer ${SECRET}` }, rawBody: Buffer.alloc(9) }),
      (error) => error.code === 'BODY_TOO_LARGE' && error.statusCode === 413,
    );
  } finally {
    store.close();
  }
});

test('ingress rejects non-allowlisted symbols and strategies before persistence', () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  const receiver = new TradingViewWebhookIngress({
    store,
    bearerSecret: SECRET,
    allowedTickers: new Set(['QQQ']),
    allowedStrategies: new Set(['approved-strategy']),
    clock: () => NOW,
  });
  try {
    assert.throws(
      () => receiver.handle({
        headers: { authorization: `Bearer ${SECRET}` },
        rawBody: body({ alert_id: 'blocked-symbol', ticker: 'SPY', strategy_id: 'approved-strategy' }),
      }),
      (error) => error.code === 'SYMBOL_NOT_ALLOWED',
    );
    assert.throws(
      () => receiver.handle({
        headers: { authorization: `Bearer ${SECRET}` },
        rawBody: body({ alert_id: 'blocked-strategy', ticker: 'QQQ', strategy_id: 'unapproved-strategy' }),
      }),
      (error) => error.code === 'STRATEGY_NOT_ALLOWED',
    );
    assert.equal(store.listAlerts().length, 0);
  } finally {
    store.close();
  }
});

test('security rejection events are durable and redact sensitive details', () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  try {
    store.recordSecurityEvent({
      source: 'tradingview',
      code: 'UNAUTHORIZED',
      details: { http_status: 401, auth_token: BODY_AUTH_SECRET },
    });
    const [event] = store.listSecurityEvents();
    assert.equal(event.code, 'UNAUTHORIZED');
    assert.equal(event.details.http_status, 401);
    assert.equal(event.details.auth_token, '[REDACTED]');
    assert.equal(JSON.stringify(event).includes(BODY_AUTH_SECRET), false);
  } finally {
    store.close();
  }
});

test('durable dedupe survives database reopen and returns the original intent', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quickytrade-dedupe-'));
  const databasePath = join(directory, 'alerts.sqlite');
  const rawBody = body({ alert_id: 'durable-1' });
  try {
    let store = new TradingViewStore(databasePath, { clock: () => NOW });
    const first = ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody,
    });
    assert.equal(first.body.duplicate, false);
    store.close();

    store = new TradingViewStore(databasePath, { clock: () => NOW });
    const second = ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody,
    });
    assert.equal(second.statusCode, 202);
    assert.equal(second.body.duplicate, true);
    assert.equal(second.body.intent_id, first.body.intent_id);
    assert.match(first.body.correlation_id, /^[0-9a-f-]{36}$/i);
    assert.equal(second.body.correlation_id, first.body.correlation_id);
    assert.equal(
      store.getAlertStatus('tradingview', 'durable-1').correlation_id,
      first.body.correlation_id,
    );
    const timeline = store.getAlertTimeline('tradingview', 'durable-1');
    assert.equal(timeline.length, 1);
    assert.equal(timeline[0].correlation_id, first.body.correlation_id);
    store.close();
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('capture-only receipt remains permanently ineligible after restart and mode change', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quickytrade-capture-quarantine-'));
  const databasePath = join(directory, 'alerts.sqlite');
  const rawBody = body({ alert_id: 'capture-only-1' });
  let brokerCalls = 0;
  try {
    let store = new TradingViewStore(databasePath, { clock: () => NOW });
    const captureIngress = new TradingViewWebhookIngress({
      store,
      bearerSecret: SECRET,
      executionEligible: false,
      clock: () => NOW,
    });
    const accepted = captureIngress.handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody,
    });
    assert.equal(accepted.statusCode, 202);
    assert.equal(store.getAlertStatus('tradingview', 'capture-only-1').executionEligible, false);
    store.close();

    store = new TradingViewStore(databasePath, { clock: () => NOW });
    const processor = new IbkrAlertProcessor({
      store,
      ibkrAdapter: { async placeTrade() { brokerCalls += 1; return { status: 'SUBMITTED', brokerOrderId: '1' }; } },
    });
    assert.equal(store.findNextReady('tradingview'), null);
    assert.equal(await processor.processNext(), null);
    assert.equal(brokerCalls, 0);
    store.close();
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('direct processing cannot bypass capture-only execution quarantine', async () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  let brokerCalls = 0;
  try {
    const receiver = new TradingViewWebhookIngress({
      store,
      bearerSecret: SECRET,
      executionEligible: false,
      clock: () => NOW,
    });
    const accepted = receiver.handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'direct-capture-bypass' }),
    });
    const processor = new IbkrAlertProcessor({
      store,
      ibkrAdapter: { async placeTrade() { brokerCalls += 1; return { status: 'SUBMITTED', brokerOrderId: '1' }; } },
    });
    const result = await processor.processIntent(accepted.body.intent_id);
    assert.equal(result.status, 'READY');
    assert.equal(result.executionEligible, false);
    assert.equal(brokerCalls, 0);
  } finally {
    store.close();
  }
});

test('listAlerts returns newest dashboard records with durable unique correlations', () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  try {
    const receiver = ingress(store);
    const first = receiver.handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'list-1' }),
    });
    const second = receiver.handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'list-2' }),
    });
    const listed = store.listAlerts(1);
    assert.equal(listed.length, 1);
    assert.equal(listed[0].alertId, 'list-2');
    assert.equal(listed[0].correlation_id, second.body.correlation_id);
    assert.notEqual(first.body.correlation_id, second.body.correlation_id);
    assert.throws(() => store.listAlerts(0), RangeError);
    assert.throws(() => store.listAlerts(501), RangeError);
  } finally {
    store.close();
  }
});

test('TradingView intent snapshots management ownership and quarantines non-app management', () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  try {
    const receiver = new TradingViewWebhookIngress({
      store,
      bearerSecret: SECRET,
      executionEligible: true,
      managementResolver: () => ({
        mode: 'TRADINGVIEW_MANAGED',
        managementProfileId: 'paper-balanced-v1',
        managementPolicy: { id: 'paper-balanced-v1', version: 1 },
      }),
      clock: () => NOW,
    });
    receiver.handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'ownership-snapshot-1' }),
    });
    const intent = store.getAlertStatus('tradingview', 'ownership-snapshot-1');
    assert.equal(intent.managementMode, 'TRADINGVIEW_MANAGED');
    assert.equal(intent.managementPolicyId, 'paper-balanced-v1');
    assert.deepEqual(intent.managementPolicy, { id: 'paper-balanced-v1', version: 1 });
    assert.equal(intent.executionEligible, false);
    assert.equal(store.findNextReady('tradingview'), null);
  } finally {
    store.close();
  }
});

test('existing intent databases are migrated to durable correlation UUIDs', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quickytrade-correlation-migration-'));
  const databasePath = join(directory, 'alerts.sqlite');
  try {
    const legacy = new DatabaseSync(databasePath);
    legacy.exec(`
      CREATE TABLE signal_intents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        alert_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source, alert_id)
      );
      INSERT INTO signal_intents
        (source, alert_id, payload_hash, payload_json, status, created_at, updated_at)
      VALUES ('tradingview', 'legacy-1', 'hash', '{}', 'READY',
              '2026-07-18T15:00:00.000Z', '2026-07-18T15:00:00.000Z');
    `);
    legacy.close();

    let store = new TradingViewStore(databasePath, { clock: () => NOW });
    const migratedCorrelation = store.getAlertStatus('tradingview', 'legacy-1').correlation_id;
    assert.match(migratedCorrelation, /^[0-9a-f-]{36}$/i);
    assert.equal(store.getAlertStatus('tradingview', 'legacy-1').executionEligible, false);
    assert.equal(store.getAlertStatus('tradingview', 'legacy-1').managementMode, 'LEGACY_ENTRY_ONLY');
    store.close();
    store = new TradingViewStore(databasePath, { clock: () => NOW });
    assert.equal(
      store.getAlertStatus('tradingview', 'legacy-1').correlation_id,
      migratedCorrelation,
    );
    store.close();
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('same source and alert_id with a different payload is a conflict', () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  try {
    const receiver = ingress(store);
    receiver.handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'conflict-1' }),
    });
    assert.throws(
      () => receiver.handle({
        headers: { authorization: `Bearer ${SECRET}` },
        rawBody: body({ alert_id: 'conflict-1', action: 'OPEN_LONG_PUT' }),
      }),
      (error) => error instanceof DedupeConflictError && error.statusCode === 409,
    );
  } finally {
    store.close();
  }
});

test('intermediate management schema is quarantined when immutable policy snapshot is absent', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quickytrade-policy-migration-'));
  const databasePath = join(directory, 'alerts.sqlite');
  try {
    const legacy = new DatabaseSync(databasePath);
    legacy.exec(`
      CREATE TABLE signal_intents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT, source TEXT NOT NULL,
        alert_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        execution_eligible INTEGER NOT NULL, management_mode TEXT NOT NULL,
        management_policy_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(source,alert_id)
      );
      INSERT INTO signal_intents VALUES
        (1,'a31e2b1f-e17b-4e5e-96ee-5f276c769e51','tradingview','mid-1','hash','{}',1,
         'APP_MANAGED','paper-balanced-v1','READY','2026-07-18T15:00:00Z','2026-07-18T15:00:00Z');
    `);
    legacy.close();
    const store = new TradingViewStore(databasePath, { clock: () => NOW });
    assert.equal(store.getAlertStatus('tradingview', 'mid-1').executionEligible, false);
    assert.equal(store.getAlertStatus('tradingview', 'mid-1').managementPolicy, null);
    assert.equal(store.findNextReady('tradingview'), null);
    store.close();
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('ingress only acknowledges READY; adapter invocation is asynchronous and pre-persisted', async () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  let adapterCalls = 0;
  let release;
  const adapter = {
    placeTrade: () => {
      adapterCalls += 1;
      return new Promise((resolve) => { release = resolve; });
    },
  };
  try {
    const acknowledgement = ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'async-1' }),
    });
    assert.deepEqual(
      { statusCode: acknowledgement.statusCode, status: acknowledgement.body.status, calls: adapterCalls },
      { statusCode: 202, status: 'READY', calls: 0 },
    );

    const processor = new IbkrAlertProcessor({ store, ibkrAdapter: adapter });
    const processing = processor.processNext();
    assert.equal(adapterCalls, 1);
    const inFlight = store.getAlertStatus('tradingview', 'async-1');
    assert.equal(inFlight.status, 'PROCESSING');
    assert.equal(inFlight.order.status, 'SUBMITTING');
    release({ status: 'SUBMITTED', brokerOrderId: 'IBKR-1001' });
    await processing;
    assert.equal(store.getAlertStatus('tradingview', 'async-1').status, 'SUBMITTED');
  } finally {
    store.close();
  }
});

test('successful IBKR result is tracked in status and timeline', async () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  try {
    ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'success-1' }),
    });
    const processor = new IbkrAlertProcessor({
      store,
      ibkrAdapter: { placeTrade: async () => ({ status: 'SUBMITTED', brokerOrderId: 'IBKR-42' }) },
    });
    await processor.processNext();
    const status = store.getAlertStatus('tradingview', 'success-1');
    assert.equal(status.status, 'SUBMITTED');
    assert.equal(status.order.broker, 'IBKR');
    assert.equal(status.order.brokerOrderId, 'IBKR-42');
    assert.deepEqual(
      store.getAlertTimeline('tradingview', 'success-1').map((event) => event.type),
      ['ALERT_READY', 'SUBMISSION_STARTED', 'SUBMITTED'],
    );
  } finally {
    store.close();
  }
});

test('blocked IBKR result is terminal and is never auto-retried', async () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  let calls = 0;
  try {
    ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'blocked-1' }),
    });
    const processor = new IbkrAlertProcessor({
      store,
      ibkrAdapter: {
        placeTrade: async () => {
          calls += 1;
          return { status: 'BLOCKED', code: 'RISK_LIMIT' };
        },
      },
    });
    await processor.processNext();
    assert.equal(store.getAlertStatus('tradingview', 'blocked-1').status, 'BLOCKED');
    assert.equal(await processor.processNext(), null);
    assert.equal(calls, 1);
  } finally {
    store.close();
  }
});

test('ambiguous adapter failure becomes SUBMISSION_UNKNOWN and is never auto-retried', async () => {
  const store = new TradingViewStore(':memory:', { clock: () => NOW });
  let calls = 0;
  try {
    ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'unknown-1' }),
    });
    const processor = new IbkrAlertProcessor({
      store,
      ibkrAdapter: {
        placeTrade: async () => {
          calls += 1;
          throw new SubmissionUnknownError('timeout account=DU123 token=supersecret');
        },
      },
    });
    await processor.processNext();
    const status = store.getAlertStatus('tradingview', 'unknown-1');
    assert.equal(status.status, 'SUBMISSION_UNKNOWN');
    assert.equal(status.order.status, 'SUBMISSION_UNKNOWN');
    assert.equal(status.order.errorCode, 'SUBMISSION_UNKNOWN');
    const durableRisks = store.listSubmissionUnknownAlerts();
    assert.equal(durableRisks.length, 1);
    assert.equal(durableRisks[0].alertId, 'unknown-1');
    assert.equal(await processor.processNext(), null);
    assert.equal(calls, 1);
  } finally {
    store.close();
  }
});

test('restart converts an interrupted persisted submission to SUBMISSION_UNKNOWN without retry', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'quickytrade-restart-'));
  const databasePath = join(directory, 'alerts.sqlite');
  try {
    let store = new TradingViewStore(databasePath, { clock: () => NOW });
    const acknowledgement = ingress(store).handle({
      headers: { authorization: `Bearer ${SECRET}` },
      rawBody: body({ alert_id: 'restart-1' }),
    });
    const prepared = store.prepareSubmission(acknowledgement.body.intent_id);
    assert.equal(prepared.claimed, true);
    assert.equal(store.getAlertStatus('tradingview', 'restart-1').order.status, 'SUBMITTING');
    store.close();

    store = new TradingViewStore(databasePath, { clock: () => NOW });
    const recovered = store.getAlertStatus('tradingview', 'restart-1');
    assert.equal(recovered.status, 'SUBMISSION_UNKNOWN');
    assert.equal(recovered.order.status, 'SUBMISSION_UNKNOWN');
    assert.equal(recovered.order.errorCode, 'PROCESS_INTERRUPTED');
    assert.equal(store.findNextReady('tradingview'), null);
    assert.deepEqual(
      store.getAlertTimeline('tradingview', 'restart-1').map((event) => event.type),
      ['ALERT_READY', 'SUBMISSION_STARTED', 'SUBMISSION_UNKNOWN'],
    );
    store.close();
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('persisted payloads and structured values redact secret and account fields', async () => {
  assert.deepEqual(redact({
    authorization: 'Bearer abc',
    nested: { account_id: 'DU123', note: 'token=hunter2' },
  }), {
    authorization: '[REDACTED]',
    nested: { account_id: '[REDACTED]', note: 'token=[REDACTED]' },
  });

  const directory = await mkdtemp(join(tmpdir(), 'quickytrade-redact-'));
  const databasePath = join(directory, 'alerts.sqlite');
  try {
    const store = new TradingViewStore(databasePath, { clock: () => NOW });
    store.receive({
      source: 'test',
      alertId: 'redact-1',
      payloadHash: 'hash',
      payload: { ticker: 'SPY', secret: 'hidden', account: 'DU123' },
    });
    store.close();
    const bytes = await readFile(databasePath);
    assert.equal(bytes.includes(Buffer.from('hidden')), false);
    assert.equal(bytes.includes(Buffer.from('DU123')), false);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
