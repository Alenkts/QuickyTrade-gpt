import http from 'node:http';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, chmodSync, readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  canonicalPayload,
  DedupeConflictError,
  IbkrAlertProcessor,
  TradingViewStore,
  TradingViewWebhookIngress,
  WebhookError,
} from './src/tradingview/index.js';
import { OperatorStore } from './src/operator/store.js';

const ROOT = fileURLToPath(new URL('.', import.meta.url));

// Loads project-local `.env` (gitignored) into process.env for keys that
// aren't already set, so a real shell export always takes precedence. No
// dotenv dependency — this project's Node side is stdlib-only.
function loadEnvFile(path) {
  let content;
  try {
    content = readFileSync(path, 'utf8');
  } catch {
    return;
  }
  for (const line of content.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (value.length >= 2 && (value[0] === '"' || value[0] === "'") && value[0] === value[value.length - 1]) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  }
}
loadEnvFile(join(ROOT, '.env'));

const PORT = integerEnv('PORT', 4173, 1, 65535);
const INGRESS_PORT = integerEnv('QT_WEBHOOK_INGRESS_PORT', 4180, 1, 65535);
if (INGRESS_PORT === PORT) throw new Error('QT_WEBHOOK_INGRESS_PORT must differ from PORT');
const DESKTOP_PARENT_PID = process.env.QT_DESKTOP_PARENT_PID
  ? integerEnv('QT_DESKTOP_PARENT_PID', 0, 1, 2_147_483_647)
  : null;
const DESKTOP_INSTANCE_TOKEN = process.env.QT_DESKTOP_INSTANCE_TOKEN || null;
const MODE = process.env.QT_TRADING_MODE || 'capture_only';
// When true (current default), TradingView alerts are auto-submitted with
// zero human review once ownership is APP_MANAGED (the default -- see
// src/operator/store.js). This is not a paper-only guarantee: whether the
// resulting order is paper or live is decided entirely by the Python core's
// account config (core/quickytrade_core/config.py), not by this flag or by
// QT_TRADING_MODE.
const APP_MANAGEMENT_EXECUTION_AVAILABLE = true;
if (!['capture_only', 'paper_tws'].includes(MODE)) {
  throw new Error('QT_TRADING_MODE must be capture_only or paper_tws');
}

const DATA_DIR = resolve(process.env.QT_DATA_DIR || join(ROOT, 'data'));
mkdirSync(DATA_DIR, { recursive: true, mode: 0o700 });
try { chmodSync(DATA_DIR, 0o700); } catch {}

const DATABASE_PATH = join(DATA_DIR, 'tradingview.sqlite');
const WEBHOOK_SECRET = process.env.QT_WEBHOOK_SECRET || '';
const WEBHOOK_CONFIGURED = WEBHOOK_SECRET.length >= 32;
const CORE_URL = (process.env.QT_CORE_URL || 'http://127.0.0.1:8765').replace(/\/$/, '');
const CORE_TOKEN = process.env.QT_CORE_TOKEN || '';
const WEBHOOK_MAX_BODY = integerEnv('QT_WEBHOOK_MAX_BODY_BYTES', 32 * 1024, 1024, 256 * 1024);
const WEBHOOK_MAX_AGE_MS = integerEnv('QT_WEBHOOK_MAX_AGE_MS', 5 * 60 * 1000, 1_000, 60 * 60 * 1000);
const WEBHOOK_MAX_FUTURE_SKEW_MS = integerEnv('QT_WEBHOOK_MAX_FUTURE_SKEW_MS', 30 * 1000, 0, 5 * 60 * 1000);
const ALLOWED_TICKERS = new Set((process.env.QT_ALLOWED_TICKERS || process.env.QT_ALLOWED_SYMBOLS || 'QQQ').split(',').map((value) => value.trim()).filter(Boolean));
const ALLOWED_STRATEGIES = new Set((process.env.QT_ALLOWED_STRATEGIES || '').split(',').map((value) => value.trim()).filter(Boolean));

const safeLogger = {
  info(message, fields) { safeLoggerMessage('info', message, fields); },
  error(message, fields) { safeLoggerMessage('error', message, fields); },
};

const store = new TradingViewStore(DATABASE_PATH);
const operatorStore = new OperatorStore(DATABASE_PATH);
const ingress = WEBHOOK_CONFIGURED ? new TradingViewWebhookIngress({
  store,
  hmacSecret: WEBHOOK_SECRET,
  bearerSecret: WEBHOOK_SECRET,
  authTokenSecret: WEBHOOK_SECRET,
  maxBodyBytes: WEBHOOK_MAX_BODY,
  maxAgeMs: WEBHOOK_MAX_AGE_MS,
  maxFutureSkewMs: WEBHOOK_MAX_FUTURE_SKEW_MS,
  maxRiskHintContracts: 1,
  allowedTickers: ALLOWED_TICKERS,
  allowedStrategies: ALLOWED_STRATEGIES,
  executionEligible: MODE === 'paper_tws' && APP_MANAGEMENT_EXECUTION_AVAILABLE,
  managementResolver: () => {
    const ownership = operatorStore.tradingViewOwnership();
    return {
      ...ownership,
      managementPolicy: operatorStore.managementPolicy(ownership.managementProfileId),
    };
  },
  logger: safeLogger,
}) : null;

let coreSnapshot = {
  ready: false,
  environment: null,
  ibkrHost: null,
  ibkrPort: null,
  strikeSelection: null,
  status: MODE === 'paper_tws' ? 'CHECKING' : 'NOT_REQUIRED',
  checkedAt: null,
  reason: MODE === 'paper_tws' ? 'TWS_CORE_NOT_CHECKED' : 'CAPTURE_ONLY',
  accountMask: null,
};

// Mirrors coreSnapshot exactly (see checkCore/checkPositions below), but for
// GET /private/v1/positions. `items` is `null` whenever `status !== 'OK'` --
// never `[]` -- so a caller can never mistake "the poll failed/hasn't run
// yet" for "the broker confirmed zero open positions" (AGENTS.md: never
// render missing broker evidence as a false-empty/zero state).
let positionSnapshot = {
  status: MODE === 'paper_tws' ? 'CHECKING' : 'NOT_REQUIRED',
  items: null,
  checkedAt: null,
  reason: MODE === 'paper_tws' ? 'POSITIONS_NOT_CHECKED' : 'CAPTURE_ONLY',
};

// checkCore()'s own /healthz timeout (2s) is fine for a single lightweight
// readiness ping. GET /private/v1/positions/protection can, in the worst
// case, read across several joined sqlite tables while briefly contending
// with the same shared RLock an in-flight broker-call evidence commit holds
// -- the same reasoning that already widened twsAdapter.placeTrade/
// previewManualIntent's submit/preview timeouts (see their fetch calls
// below) away from a too-tight ~10s budget. Reusing that same widened-timeout
// philosophy here (rather than checkCore's tight 2s) avoids turning a
// merely-slow-but-healthy poll into a spurious "positions unavailable" flap.
const POSITIONS_TIMEOUT_MS = 15_000;

// Lifecycle management for the separate `python -m quickytrade_core` process.
// server.mjs only ever talks to the core over loopback HTTP (see checkCore
// below); this section optionally also owns spawning/stopping that process so
// an operator can recover from a dead core without a terminal. Only ever one
// child is tracked — if a core is already listening on CORE_URL (started
// outside this app), `start` will still spawn a second process and let it
// fail to bind, surfacing that failure in the captured log tail rather than
// silently doing nothing.
const CORE_LOG_LIMIT = 200;
let coreProcess = null;
let coreProcessState = 'stopped'; // 'stopped' | 'starting' | 'running' | 'exited'
let coreProcessStartedAt = null;
let coreProcessExitInfo = null;
let coreProcessLog = [];

function appendCoreLog(stream, text) {
  for (const line of String(text).split('\n')) {
    if (!line.trim()) continue;
    coreProcessLog.push({ stream, line, at: new Date().toISOString() });
  }
  if (coreProcessLog.length > CORE_LOG_LIMIT) {
    coreProcessLog = coreProcessLog.slice(-CORE_LOG_LIMIT);
  }
}

function startCoreProcess() {
  if (MODE !== 'paper_tws') {
    return { started: false, reason: 'CAPTURE_ONLY_MODE' };
  }
  if (coreProcess && coreProcessState !== 'exited') {
    return { started: false, reason: 'ALREADY_RUNNING' };
  }

  const profiles = operatorStore.listProfiles();
  const selectedProfile = profiles.find((p) => p.selected);
  const coreEnv = { ...process.env, PYTHONPATH: '.' };
  if (selectedProfile) {
    coreEnv.QT_IBKR_HOST = selectedProfile.host;
    coreEnv.QT_IBKR_PORT = String(selectedProfile.port);
    coreEnv.QT_IBKR_CLIENT_ID = String(selectedProfile.clientId);
  }

  coreProcessLog = [];
  coreProcessExitInfo = null;
  const child = spawn('python3', ['-m', 'quickytrade_core'], {
    cwd: join(ROOT, 'core'),
    env: coreEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  coreProcess = child;
  coreProcessState = 'starting';
  coreProcessStartedAt = new Date().toISOString();
  child.stdout.on('data', (chunk) => appendCoreLog('stdout', chunk));
  child.stderr.on('data', (chunk) => appendCoreLog('stderr', chunk));
  child.on('spawn', () => {
    if (coreProcess === child) coreProcessState = 'running';
  });
  child.on('error', (error) => {
    appendCoreLog('stderr', `spawn error: ${error.message}`);
    if (coreProcess === child) {
      coreProcessState = 'exited';
      coreProcessExitInfo = { code: null, signal: null, error: error.message, at: new Date().toISOString() };
    }
  });
  child.on('exit', (code, signal) => {
    if (coreProcess === child) {
      coreProcessState = 'exited';
      coreProcessExitInfo = { code, signal, error: null, at: new Date().toISOString() };
    }
  });
  return { started: true };
}

function stopCoreProcess() {
  if (!coreProcess || coreProcessState === 'exited') {
    return { stopped: false, reason: 'NOT_RUNNING' };
  }
  coreProcess.kill('SIGTERM');
  return { stopped: true };
}

function coreStartFailureMessage(reason) {
  if (reason === 'CAPTURE_ONLY_MODE') return 'QT_TRADING_MODE is capture_only; the core process is not used in this mode.';
  if (reason === 'ALREADY_RUNNING') return 'A managed core process is already running or starting.';
  return 'The core process could not be started.';
}

function coreProcessSnapshot() {
  return {
    state: coreProcessState,
    pid: coreProcess && coreProcessState !== 'exited' ? coreProcess.pid : null,
    startedAt: coreProcessStartedAt,
    exit: coreProcessExitInfo,
    managed: coreProcess !== null,
    logTail: coreProcessLog.slice(-40),
  };
}
let drainRunning = false;
let webhookInFlight = 0;
const rateWindows = new Map();

function coreManagementPolicy(policyId, snapshot = null) {
  const policy = snapshot || operatorStore.managementPolicy(policyId);
  if (!policy) return null;
  const wire = {
    policyId: policy.id,
    version: policy.version,
    takeProfitLevels: policy.targets.map((target) => ({
      levelId: target.id,
      triggerPercent: String(target.profitBps / 100),
      allocationPercent: String(target.allocationBps / 100),
    })),
    stopLossPercent: String(policy.stop.lossBps / 100),
  };
  if (policy.stop && policy.stop.coverageBps !== undefined) {
    wire.stopCoveragePercent = String(policy.stop.coverageBps / 100);
  }
  if (Array.isArray(policy.transitions) && policy.transitions.length > 0) {
    // Wire-boundary transform: the store keys transitions by the
    // "<levelId>_FILLED" event convention (e.g. "TP1_FILLED"); the core's
    // takeProfitLevels are keyed by bare levelId ("TP1"), so strip the
    // "_FILLED" suffix here. Store-side distanceBps (integer basis points)
    // becomes core-side distancePercent (percent-as-decimal-string), matching
    // the stopLossPercent/triggerPercent/allocationPercent convention.
    wire.transitions = policy.transitions.map((transition) => {
      const after = typeof transition.after === 'string' && transition.after.endsWith('_FILLED')
        ? transition.after.slice(0, -'_FILLED'.length)
        : transition.after;
      const entry = { after, action: transition.action };
      if (transition.action === 'TRAIL_FRESH_BID' && transition.distanceBps !== undefined) {
        entry.distancePercent = String(transition.distanceBps / 100);
      }
      return entry;
    });
  }
  return wire;
}

const twsAdapter = {
  async placeTrade(request) {
    if (MODE !== 'paper_tws') return { status: 'BLOCKED', code: 'CAPTURE_ONLY' };
    if (!coreSnapshot.ready) return { status: 'BLOCKED', code: coreSnapshot.reason || 'TWS_CORE_NOT_READY' };
    if (!ALLOWED_TICKERS.has(request.signal?.ticker)) return { status: 'BLOCKED', code: 'SYMBOL_NOT_ALLOWED' };
    if (ALLOWED_STRATEGIES.size > 0 && !ALLOWED_STRATEGIES.has(request.signal?.strategy_id)) {
      return { status: 'BLOCKED', code: 'STRATEGY_NOT_ALLOWED' };
    }

    // The core's _parse_request rejects any field outside its declared contract
    // (broker/idempotencyKey/intentId/correlationId/source/alertId/signal plus
    // optional ownership/managementMode/managementPolicy), so this must be built
    // from an explicit allowlist rather than spreading `request` -- internal-only
    // fields like managementPolicyId (a local lookup key, never part of the wire
    // contract) would otherwise reach the core and be rejected as unrecognized.
    const signal = { ...(request.signal || {}) };
    if (!signal.capital_per_trade_dollars && (request.source === 'tradingview' || !request.source)) {
      const activeProfile = store.getSelectedProfile() || store.getProfile('paper-tws');
      const appCapital = activeProfile?.capitalPerTradeDollars || process.env.QT_CAPITAL_PER_TRADE_DOLLARS || null;
      if (appCapital) {
        signal.capital_per_trade_dollars = String(appCapital);
      }
    }
    const coreRequest = {
      broker: request.broker,
      idempotencyKey: request.idempotencyKey,
      intentId: request.intentId,
      correlationId: request.correlationId,
      source: request.source === 'manual' ? 'MANUAL_UI' : request.source,
      alertId: request.alertId,
      signal,
      ownership: 'APP_OWNED',
    };
    if (request.managementMode) coreRequest.managementMode = request.managementMode;
    if (request.managementMode === 'APP_MANAGED') {
      const policy = coreManagementPolicy(request.managementPolicyId, request.managementPolicy);
      if (!policy) return { status: 'BLOCKED', code: 'MANAGEMENT_POLICY_NOT_FOUND' };
      coreRequest.managementPolicy = policy;
    }

    let response;
    try {
      response = await fetch(`${CORE_URL}/private/v1/place-trade`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${CORE_TOKEN}`,
        },
        body: JSON.stringify(coreRequest),
        // A TARGET_RANGE submit re-scans up to strike_candidate_count listed
        // strikes (qualify + live quote per candidate, sequentially) on top of
        // the qualify/quote/market-rule/placeOrder+ack chain, so real round trips
        // can comfortably exceed 10s under normal (non-degraded) conditions. An
        // abort here only ever yields a safe SUBMISSION_UNKNOWN, never a duplicate
        // order, but firing it too early turns healthy-but-slow submits into
        // spurious ambiguous outcomes.
        signal: AbortSignal.timeout(30_000),
      });
    } catch (cause) {
      const error = new Error('Official TWS core submission outcome is unknown', { cause });
      error.code = 'TWS_CORE_TRANSPORT_UNKNOWN';
      error.ambiguous = true;
      throw error;
    }

    const result = await response.json().catch(() => ({}));
    if (result.status === 'SUBMISSION_UNKNOWN') {
      const error = new Error('Official TWS core reported an unknown submission outcome');
      error.code = machineCode(result.code, 'TWS_CORE_SUBMISSION_UNKNOWN');
      error.ambiguous = true;
      throw error;
    }
    if (response.status >= 500) {
      const error = new Error('Official TWS core failed after receiving the submission request');
      error.code = machineCode(result.code, 'TWS_CORE_SERVER_UNKNOWN');
      error.ambiguous = true;
      throw error;
    }
    if (result.status === 'BLOCKED' || response.status >= 400) {
      return { status: 'BLOCKED', code: machineCode(result.code, 'TWS_CORE_BLOCKED') };
    }
    const brokerOrderId = result.brokerOrderId ?? result.broker_order_id;
    if (result.status === 'SUBMITTED' && brokerOrderId !== undefined && brokerOrderId !== null) {
      return { status: 'SUBMITTED', brokerOrderId: String(brokerOrderId) };
    }
    const error = new Error('Official TWS core returned an indeterminate result');
    error.code = 'TWS_CORE_INDETERMINATE';
    error.ambiguous = true;
    throw error;
  },

  // Thin proxy to the core's /private/v1/close-trade (Phase 5), mirroring
  // placeTrade's exact evidence/ambiguous-outcome mapping above -- this is a
  // real reduce-only broker SELL (or, for FULL_FLATTEN, a cancel-every-leg-
  // then-SELL sequence), so it gets the identical rigor, not a shortcut.
  async closeTrade(request) {
    if (MODE !== 'paper_tws') return { status: 'BLOCKED', code: 'CAPTURE_ONLY' };
    if (!coreSnapshot.ready) return { status: 'BLOCKED', code: coreSnapshot.reason || 'TWS_CORE_NOT_READY' };

    let response;
    try {
      response = await fetch(`${CORE_URL}/private/v1/close-trade`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${CORE_TOKEN}`,
        },
        body: JSON.stringify(request),
        // Same widened-timeout rationale as placeTrade's fetch above -- a
        // FULL_FLATTEN cancels every working protection leg (N sequential
        // cancel_order round trips) before its one flattening SELL, so this
        // can legitimately take longer than a plain open. An abort here only
        // ever yields a safe SUBMISSION_UNKNOWN, never a duplicate SELL.
        signal: AbortSignal.timeout(30_000),
      });
    } catch (cause) {
      const error = new Error('Official TWS core close/flatten outcome is unknown', { cause });
      error.code = 'TWS_CORE_CLOSE_TRANSPORT_UNKNOWN';
      error.ambiguous = true;
      throw error;
    }

    const result = await response.json().catch(() => ({}));
    if (result.status === 'SUBMISSION_UNKNOWN') {
      const error = new Error('Official TWS core reported an unknown close/flatten outcome');
      error.code = machineCode(result.code, 'TWS_CORE_CLOSE_SUBMISSION_UNKNOWN');
      error.ambiguous = true;
      throw error;
    }
    if (response.status >= 500) {
      const error = new Error('Official TWS core failed after receiving the close/flatten request');
      error.code = machineCode(result.code, 'TWS_CORE_CLOSE_SERVER_UNKNOWN');
      error.ambiguous = true;
      throw error;
    }
    if (result.status === 'BLOCKED' || response.status >= 400) {
      return { status: 'BLOCKED', code: machineCode(result.code, 'TWS_CORE_CLOSE_BLOCKED') };
    }
    const brokerOrderId = result.brokerOrderId ?? result.broker_order_id;
    if (result.status === 'SUBMITTED' && brokerOrderId !== undefined && brokerOrderId !== null) {
      return { status: 'SUBMITTED', brokerOrderId: String(brokerOrderId), quantity: result.quantity ?? null };
    }
    const error = new Error('Official TWS core returned an indeterminate close/flatten result');
    error.code = 'TWS_CORE_CLOSE_INDETERMINATE';
    error.ambiguous = true;
    throw error;
  },
};

const processor = new IbkrAlertProcessor({ store, ibkrAdapter: twsAdapter, logger: safeLogger });

function integerEnv(name, fallback, minimum, maximum) {
  const raw = process.env[name];
  const value = raw === undefined ? fallback : Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer from ${minimum} through ${maximum}`);
  }
  return value;
}

function machineCode(value, fallback) {
  return typeof value === 'string' && /^[A-Z0-9_]{1,64}$/.test(value) ? value : fallback;
}

function safeLoggerMessage(level, message, fields = {}) {
  const safe = Object.fromEntries(Object.entries(fields).filter(([key]) => !/secret|token|authorization|account/i.test(key)));
  console[level](`${message} ${JSON.stringify(safe)}`);
}

function json(res, status, data, headers = {}) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
    ...headers,
  });
  res.end(JSON.stringify(data));
}

async function rawBody(req, limit) {
  const chunks = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > limit) throw new WebhookError('BODY_TOO_LARGE', `Webhook body exceeds ${limit} bytes`, 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, length);
}

function assertWebhookRate(req) {
  if (webhookInFlight >= 8) throw new WebhookError('TOO_MANY_CONCURRENT_REQUESTS', 'Too many concurrent webhook requests', 429);
  const key = req.socket.remoteAddress || 'unknown';
  const now = Date.now();
  const current = rateWindows.get(key);
  if (!current || now - current.startedAt >= 1_000) {
    rateWindows.set(key, { startedAt: now, count: 1 });
    return;
  }
  current.count += 1;
  if (current.count > 10) throw new WebhookError('RATE_LIMITED', 'Webhook request rate exceeded', 429);
}

function mapAlert(alert) {
  return {
    id: alert.id,
    correlationId: alert.correlation_id,
    source: alert.source,
    alertId: alert.alertId,
    payload: alert.payload,
    executionEligible: alert.executionEligible,
    managementMode: alert.managementMode,
    managementPolicyId: alert.managementPolicyId,
    managementPolicy: alert.managementPolicy,
    status: alert.status,
    createdAt: alert.createdAt,
    updatedAt: alert.updatedAt,
    order: alert.order,
  };
}

async function jsonBody(req, limit = 64 * 1024) {
  if (!String(req.headers['content-type'] || '').toLowerCase().startsWith('application/json')) {
    throw new WebhookError('CONTENT_TYPE_REQUIRED', 'Content-Type must be application/json', 415);
  }
  const raw = await rawBody(req, limit);
  try {
    const value = JSON.parse(raw.toString('utf8'));
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error();
    return value;
  } catch {
    throw new WebhookError('INVALID_JSON', 'Request body must be a JSON object', 400);
  }
}

function operatorError(res, error) {
  if (error instanceof TypeError) {
    return json(res, 400, { code: 'INVALID_OPERATOR_REQUEST', message: error.message });
  }
  throw error;
}

// Shared by preview and submit so both surfaces resolve the identical
// contract/policy from the identical intent. Only APP_MANAGED manual intents
// are submittable today (mirrors the core's own management.py contract:
// USER_MANAGED/TRADINGVIEW_MANAGED must never reach the core as-is).
function buildManualCoreRequest(intent) {
  if (intent.managementMode !== 'APP_MANAGED') return null;
  const policy = coreManagementPolicy(intent.managementProfileId, intent.managementPolicy);
  if (!policy) return { error: { status: 'BLOCKED', code: 'MANAGEMENT_POLICY_NOT_FOUND' } };
  // The connection profile the intent was created against is this manual
  // entry's only source of capital_per_trade_dollars. It is operator-editable
  // (Settings, no core restart) and additive: when unset the core falls back
  // to its own pre-existing default-quantity-of-1 behavior driven by
  // intent.payload.quantity below.
  const profile = operatorStore.getProfile(intent.profileId);
  const signal = {
    schema_version: '1',
    alert_id: intent.id,
    sent_at: intent.createdAt,
    strategy_id: 'manual-ui',
    strategy_version: '1',
    action: intent.payload.right === 'C' ? 'OPEN_LONG_CALL' : 'OPEN_LONG_PUT',
    ticker: intent.payload.symbol,
    target_dte: 0,
    strike_policy: intent.payload.strikeSelection === 'TARGET_RANGE'
      ? { type: 'TARGET_RANGE' }
      : { type: 'EXACT_LISTED', expiry: intent.payload.expiry, strike: intent.payload.strike },
    quantity: intent.payload.quantity,
    exit_policy_id: policy.policyId,
  };
  if (profile?.capitalPerTradeDollars) {
    signal.capital_per_trade_dollars = profile.capitalPerTradeDollars;
  }
  return {
    policy,
    signal,
    request: {
      broker: 'IBKR',
      idempotencyKey: `manual:${intent.id}`,
      intentId: intent.intentId,
      correlationId: intent.correlationId,
      source: 'MANUAL_UI',
      alertId: intent.id,
      ownership: 'APP_OWNED',
      managementMode: 'APP_MANAGED',
      managementPolicy: policy,
      signal,
    },
  };
}

async function previewManualIntent(intent) {
  if (MODE !== 'paper_tws' || !coreSnapshot.ready || CORE_TOKEN.length < 32) return null;
  const built = buildManualCoreRequest(intent);
  if (!built) return null;
  if (built.error) return built.error;
  try {
    const response = await fetch(`${CORE_URL}/private/v1/preview-trade`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${CORE_TOKEN}` },
      body: JSON.stringify(built.request),
      // See the matching comment on twsAdapter.placeTrade's fetch above:
      // TARGET_RANGE preview does the same multi-candidate strike scan, so 10s
      // is too tight for a healthy-but-slow round trip.
      signal: AbortSignal.timeout(20_000),
    });
    const result = await response.json().catch(() => ({}));
    return response.ok ? result : {
      status: 'BLOCKED',
      code: machineCode(result.code, 'CORE_PREVIEW_BLOCKED'),
    };
  } catch {
    return { status: 'BLOCKED', code: 'CORE_PREVIEW_UNAVAILABLE' };
  }
}

// Manual trades keep an explicit human checkpoint (Review Trade -> inspect
// the resolved contract -> Submit, optionally automated by the operator's own
// Autosend setting) that TradingView's fully-automated queue does not. This is
// therefore a deliberately separate flag from APP_MANAGEMENT_EXECUTION_AVAILABLE
// -- enabling manual submission must never silently also unblock TradingView's
// zero-human-review queue.
const MANUAL_EXECUTION_AVAILABLE = true;

async function submitManualIntent(intent) {
  if (MODE !== 'paper_tws' || !coreSnapshot.ready || CORE_TOKEN.length < 32 || !MANUAL_EXECUTION_AVAILABLE) {
    return { status: 'BLOCKED', code: 'MANUAL_EXECUTION_UNAVAILABLE' };
  }
  const built = buildManualCoreRequest(intent);
  if (!built) return { status: 'BLOCKED', code: 'MANAGEMENT_MODE_NOT_SUBMITTABLE' };
  if (built.error) return built.error;

  const payloadHash = createHash('sha256').update(canonicalPayload(built.signal)).digest('hex');
  let received;
  try {
    // Durably persists the signal/trade intent before any broker call, and
    // (source, alertId) uniqueness makes a repeated submit for the same
    // manual proposal id idempotent -- it returns the original correlation
    // rather than risking a second order, exactly like a TradingView replay.
    received = store.receive({
      // Local dedup/idempotencyKey namespace (matches TradingView's lowercase
      // 'tradingview'), distinct from the core's wire-level source vocabulary --
      // twsAdapter.placeTrade() translates this to 'MANUAL_UI' before the core
      // sees it. The core's own idempotencyKey-prefix check expects lowercase
      // "manual:<alertId>" even though the wire `source` field must be uppercase.
      source: 'manual',
      alertId: intent.id,
      payloadHash,
      payload: built.signal,
      executionEligible: true,
      managementMode: intent.managementMode,
      managementPolicyId: intent.managementProfileId,
      // Raw operatorStore shape, matching the TradingView ingress path (server.mjs
      // ~line 93) -- twsAdapter.placeTrade() applies coreManagementPolicy() itself
      // to build the wire shape. built.policy is already that wire shape (used to
      // call the core's preview endpoint directly above); persisting it here would
      // make placeTrade() re-transform an already-transformed object and crash on
      // the missing .targets field.
      managementPolicy: operatorStore.managementPolicy(intent.managementProfileId),
    });
  } catch (error) {
    if (error instanceof DedupeConflictError) {
      return { status: 'BLOCKED', code: 'MANUAL_SUBMISSION_PAYLOAD_CONFLICT' };
    }
    throw error;
  }

  await processor.processIntent(received.intentId);
  const alertStatus = store.getAlertStatus('manual', intent.id);
  return {
    status: alertStatus?.status || 'SUBMISSION_UNKNOWN',
    correlationId: received.correlationId,
    brokerOrderId: alertStatus?.order?.brokerOrderId || null,
    code: alertStatus?.order?.errorCode || null,
  };
}

async function checkCore() {
  if (MODE !== 'paper_tws') return coreSnapshot;
  if (CORE_TOKEN.length < 32) {
    coreSnapshot = {
      ready: false, environment: null, ibkrHost: null, ibkrPort: null, strikeSelection: null,
      status: 'BLOCKED', checkedAt: new Date().toISOString(), reason: 'TWS_CORE_TOKEN_NOT_CONFIGURED',
    };
    return coreSnapshot;
  }
  try {
    const response = await fetch(`${CORE_URL}/healthz`, {
      headers: { authorization: `Bearer ${CORE_TOKEN}` },
      signal: AbortSignal.timeout(2_000),
    });
    const result = await response.json().catch(() => ({}));
    const environment = result.environment === 'LIVE' ? 'LIVE' : 'PAPER';
    const ready = response.ok && result.ready === true;
    const strikeSelection = result.strikeSelection && typeof result.strikeSelection === 'object'
      ? {
        metric: typeof result.strikeSelection.metric === 'string' ? result.strikeSelection.metric : null,
        lo: typeof result.strikeSelection.lo === 'string' ? result.strikeSelection.lo : null,
        hi: typeof result.strikeSelection.hi === 'string' ? result.strikeSelection.hi : null,
        candidateCount: Number.isSafeInteger(result.strikeSelection.candidateCount) ? result.strikeSelection.candidateCount : null,
      }
      : null;
    coreSnapshot = {
      ready,
      environment,
      // The exact host/port the core actually connected IBKR through — used to
      // tell which connection profile (if any) this running core corresponds
      // to, so a same-environment profile the core is NOT using is never
      // shown as ready/unlocked too.
      ibkrHost: typeof result.ibkrHost === 'string' ? result.ibkrHost : null,
      ibkrPort: Number.isSafeInteger(result.ibkrPort) ? result.ibkrPort : null,
      strikeSelection,
      status: ready ? 'READY' : 'BLOCKED',
      checkedAt: new Date().toISOString(),
      reason: ready ? null : machineCode(result.code, 'TWS_CORE_NOT_READY'),
      accountMask: typeof result.accountMask === 'string' ? result.accountMask : null,
    };
  } catch {
    coreSnapshot = {
      ready: false,
      environment: null,
      ibkrHost: null,
      ibkrPort: null,
      strikeSelection: null,
      status: 'UNAVAILABLE',
      checkedAt: new Date().toISOString(),
      reason: 'TWS_CORE_UNAVAILABLE',
      accountMask: null,
    };
  }
  return coreSnapshot;
}

// Mirrors checkCore() exactly: same Bearer CORE_TOKEN header, same
// AbortSignal.timeout pattern, same fail-closed-to-an-explicit-unavailable-
// state catch block. Never renders a poll failure (timeout, network error,
// non-OK/malformed response) as an empty items list -- that would silently
// read as "confirmed zero positions" to every caller, which is exactly the
// dishonest state AGENTS.md forbids. "positions unknown" (`items: null`)
// stays visibly distinct from "confirmed empty" (`items: []`, status 'OK')
// all the way through to the UI.
async function checkPositions() {
  if (MODE !== 'paper_tws') return positionSnapshot;
  if (CORE_TOKEN.length < 32) {
    positionSnapshot = {
      status: 'BLOCKED', items: null, checkedAt: new Date().toISOString(), reason: 'TWS_CORE_TOKEN_NOT_CONFIGURED',
    };
    return positionSnapshot;
  }
  if (!coreSnapshot.ready) {
    // The core itself is not ready/reachable -- fail closed rather than
    // claiming any knowledge of positions one way or the other.
    positionSnapshot = {
      status: 'UNAVAILABLE', items: null, checkedAt: new Date().toISOString(),
      reason: coreSnapshot.reason || 'TWS_CORE_NOT_READY',
    };
    return positionSnapshot;
  }
  try {
    const response = await fetch(`${CORE_URL}/private/v1/positions`, {
      headers: { authorization: `Bearer ${CORE_TOKEN}` },
      signal: AbortSignal.timeout(POSITIONS_TIMEOUT_MS),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.status !== 'OK' || !Array.isArray(result.items)) {
      positionSnapshot = {
        status: 'UNAVAILABLE', items: null, checkedAt: new Date().toISOString(),
        reason: machineCode(result.code, 'TWS_CORE_POSITIONS_UNAVAILABLE'),
      };
      return positionSnapshot;
    }
    positionSnapshot = {
      status: 'OK', items: result.items, checkedAt: new Date().toISOString(), reason: null,
    };
  } catch {
    positionSnapshot = {
      status: 'UNAVAILABLE', items: null, checkedAt: new Date().toISOString(),
      reason: 'TWS_CORE_POSITIONS_TRANSPORT_UNKNOWN',
    };
  }
  return positionSnapshot;
}

// Per-position protection/transition detail read, same fail-closed pattern.
// Called once per confirmed open position when building the combined
// /api/trades/active response -- the number of concurrently open app-owned
// positions in this paper-first app is small, so an N-call fan-out (never
// TradingView-alert volume) keeps the UI simple without a second endpoint.
async function fetchCorePositionDetail(entryCorrelationId) {
  if (MODE !== 'paper_tws' || !coreSnapshot.ready || CORE_TOKEN.length < 32) {
    return { status: 'UNAVAILABLE', protectionLegs: null, transitions: null };
  }
  try {
    const response = await fetch(`${CORE_URL}/private/v1/protection?correlationId=${encodeURIComponent(entryCorrelationId)}`, {
      headers: { authorization: `Bearer ${CORE_TOKEN}` },
      signal: AbortSignal.timeout(POSITIONS_TIMEOUT_MS),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.status !== 'OK') {
      return { status: 'UNAVAILABLE', protectionLegs: null, transitions: null };
    }
    return { status: 'OK', protectionLegs: result.protectionLegs || [], transitions: result.transitions || [] };
  } catch {
    return { status: 'UNAVAILABLE', protectionLegs: null, transitions: null };
  }
}

// Thin proxy for the core's /private/v1/reconciliation read -- mirrors
// checkPositions()/fetchCorePositionDetail()'s exact fail-closed pattern
// (never fabricates a false/empty result when the core or a ledger is
// unavailable; UNAVAILABLE stays visibly distinct from a genuine "nothing
// unresolved"). This is deliberately the one endpoint that lets an operator
// see *why* _verify_readiness() is globally blocking new opens (unresolved
// submission/protection/transition ambiguity) -- see
// GET /api/reconciliation below and its dashboard "why is this blocked"
// surfacing in app.js.
async function fetchCoreReconciliation() {
  if (MODE !== 'paper_tws' || CORE_TOKEN.length < 32) {
    return { status: 'UNAVAILABLE', reason: 'TWS_CORE_TOKEN_NOT_CONFIGURED', recentRuns: null, unresolved: null };
  }
  if (!coreSnapshot.ready) {
    return { status: 'UNAVAILABLE', reason: coreSnapshot.reason || 'TWS_CORE_NOT_READY', recentRuns: null, unresolved: null };
  }
  try {
    const response = await fetch(`${CORE_URL}/private/v1/reconciliation`, {
      headers: { authorization: `Bearer ${CORE_TOKEN}` },
      signal: AbortSignal.timeout(POSITIONS_TIMEOUT_MS),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.status !== 'OK') {
      return {
        status: 'UNAVAILABLE',
        reason: machineCode(result.code, 'TWS_CORE_RECONCILIATION_UNAVAILABLE'),
        recentRuns: null,
        unresolved: null,
      };
    }
    return {
      status: 'OK',
      reason: null,
      recentRunsStatus: result.recentRunsStatus || 'UNAVAILABLE',
      recentRuns: Array.isArray(result.recentRuns) ? result.recentRuns : null,
      unresolved: result.unresolved && typeof result.unresolved === 'object' ? result.unresolved : null,
    };
  } catch {
    return { status: 'UNAVAILABLE', reason: 'TWS_CORE_RECONCILIATION_TRANSPORT_UNKNOWN', recentRuns: null, unresolved: null };
  }
}

function mapProtectionLeg(leg) {
  return {
    protectionId: leg.protection_id,
    role: leg.role,
    levelId: leg.level_id,
    ocaGroup: leg.oca_group,
    status: leg.status,
    quantity: leg.quantity,
    triggerPrice: leg.trigger_price,
    limitPrice: leg.limit_price,
    brokerOrderId: leg.broker_order_id,
    modifyStatus: leg.modify_status,
    cancelStatus: leg.cancel_status,
    updatedAt: leg.updated_at,
  };
}

function mapTransition(transition) {
  return {
    transitionId: transition.transition_id,
    after: transition.after,
    action: transition.action,
    status: transition.status,
    appliedAt: transition.applied_at,
    updatedAt: transition.updated_at,
  };
}

// position.correlation_id (the core's own broker_submissions primary key) is
// the plain UUID this app's own TradingViewStore generated (#newCorrelationId
// in store.js) and handed to the core as the persistence receipt when the
// intent was created (see receive()/prepareSubmission()) -- the core's own
// _parse_request hard-rejects anything that isn't UUID-shaped
// (PERSISTENCE_RECEIPT_REQUIRED), so it can never have a "source:alertId"
// shape. That colon-prefixed shape ("tradingview:<alertId>" /
// "manual:<alertId>") is the Node-side idempotencyKey (a distinct
// identifier -- see prepareSubmission()), not the correlationId. So the
// originating signal (ticker, OPEN_LONG_CALL/OPEN_LONG_PUT) is recoverable
// straight from this app's own durable TradingViewStore by the UUID
// correlation_id column directly, with no new core endpoint required.
// Returns null (never guesses) for anything that doesn't match a stored
// intent.
function lookupOriginatingAlert(coreCorrelationId) {
  return store.getAlertStatusByCorrelationId(coreCorrelationId);
}

function rightFromOpenAction(alert) {
  const action = alert?.payload?.action;
  if (action === 'OPEN_LONG_CALL') return 'C';
  if (action === 'OPEN_LONG_PUT') return 'P';
  return null;
}

// The operator works the US options session.  Date-only filters must use the
// market day, never the server's UTC date (which changes during the NY
// afternoon/evening and would put the same close in two different "today"
// views depending on where the service runs).
function newYorkDate(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date);
  const part = (type) => parts.find((item) => item.type === type)?.value;
  const year = part('year');
  const month = part('month');
  const day = part('day');
  return year && month && day ? `${year}-${month}-${day}` : null;
}

function isClosedToday(position, today = newYorkDate(new Date())) {
  const closeTime = position?.closed_at || position?.updated_at;
  return position?.lifecycle_status === 'CLOSED'
    && Boolean(today)
    && newYorkDate(closeTime) === today;
}

function computeDailyPnl(items) {
  if (!Array.isArray(items)) return { realizedPnlToday: null, totalCommissionToday: null, tradesClosedToday: null };
  const todayStr = newYorkDate(new Date());
  let totalPnl = 0;
  let totalComm = 0;
  let closedCount = 0;
  let missingPnl = false;
  let missingCommission = false;
  for (const pos of items) {
    if (!pos.updated_at) continue;
    if (isClosedToday(pos, todayStr)) {
      closedCount += 1;
      if (pos.realized_pnl !== null && pos.realized_pnl !== undefined) {
        totalPnl += Number(pos.realized_pnl);
      } else missingPnl = true;
      if (pos.total_commission !== null && pos.total_commission !== undefined) {
        totalComm += Number(pos.total_commission);
      } else missingCommission = true;
    }
  }
  return {
    // A missing broker P&L/commission is not zero.  The UI renders this as
    // Unavailable rather than inventing a reassuring number.
    realizedPnlToday: closedCount > 0 && !missingPnl ? totalPnl.toFixed(2) : null,
    totalCommissionToday: closedCount > 0 && !missingCommission ? totalComm.toFixed(2) : null,
    tradesClosedToday: closedCount,
  };
}

async function mapActiveTradeItem(position) {
  const alert = lookupOriginatingAlert(position.correlation_id);
  const detail = await fetchCorePositionDetail(position.correlation_id);
  const pnlNum = position.realized_pnl !== null && position.realized_pnl !== undefined ? Number(position.realized_pnl) : null;
  const pnlFormatted = pnlNum !== null
    ? `${pnlNum >= 0 ? '+' : ''}$${pnlNum.toFixed(2)}`
    : 'Unavailable';
  const unPnlNum = position.unrealized_pnl !== null && position.unrealized_pnl !== undefined ? Number(position.unrealized_pnl) : null;
  const unrealizedPnlFormatted = unPnlNum !== null && Number.isFinite(unPnlNum)
    ? `${unPnlNum >= 0 ? '+' : ''}$${unPnlNum.toFixed(2)}`
    : 'Unavailable';
  const right = position.right || rightFromOpenAction(alert);
  return {
    correlationId: position.correlation_id,
    account: position.account,
    conId: position.con_id,
    symbol: position.symbol,
    right,
    strike: position.strike || null,
    expiry: position.expiry || null,
    localSymbol: position.local_symbol || null,
    source: alert?.source || null,
    managementMode: alert?.managementMode || null,
    quantity: {
      opened: position.opened_quantity,
      closed: position.closed_quantity,
      open: position.open_quantity,
    },
    entryAvgPrice: position.entry_avg_price,
    markPrice: position.mark_price || null,
    realizedPnl: position.realized_pnl,
    pnlFormatted,
    unrealizedPnl: position.unrealized_pnl || null,
    unrealizedPnlFormatted,
    totalCommission: position.total_commission,
    lifecycleStatus: position.lifecycle_status,
    lastReconciledAt: position.broker_position_checked_at || position.last_reconciled_at,
    updatedAt: position.updated_at,
    protection: {
      status: detail.status,
      legs: (detail.protectionLegs || []).map(mapProtectionLeg),
      transitions: (detail.transitions || []).map(mapTransition),
    },
  };
}

function mapClosedTradeItem(position) {
  const alert = lookupOriginatingAlert(position.correlation_id);
  const pnlNum = position.realized_pnl !== null && position.realized_pnl !== undefined ? Number(position.realized_pnl) : null;
  const right = position.right || rightFromOpenAction(alert);
  return {
    correlationId: position.correlation_id,
    account: position.account,
    symbol: position.symbol,
    right,
    strike: position.strike || null,
    expiry: position.expiry || null,
    localSymbol: position.local_symbol || null,
    quantityClosed: position.closed_quantity,
    realizedPnl: position.realized_pnl,
    pnlFormatted: pnlNum !== null && Number.isFinite(pnlNum) ? `${pnlNum >= 0 ? '+' : ''}$${pnlNum.toFixed(2)}` : 'Unavailable',
    totalCommission: position.total_commission,
    // This is a normalized broker SLD execution timestamp from the core,
    // never the mutable projection/reconciliation update timestamp.
    closedAt: position.closed_at || position.updated_at,
    source: alert?.source || null,
    outcome: alert?.status || position.lifecycle_status,
  };
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

// Thin proxy to /private/v1/close-trade, applying the exact same durable-
// evidence-before-broker-call discipline submitManualIntent already uses for
// entries: the close intent is persisted via the identical TradingViewStore
// receive()/prepareSubmission() pipeline (source 'manual', matching the same
// 'manual:<id>' idempotencyKey prefix the core expects) BEFORE twsAdapter.
// closeTrade is ever called, so a retried requestId (the operator's own
// action, or a network retry that reuses the same UI-held id) returns the
// original durable outcome and never submits a second SELL -- including
// across a Node restart, exactly like every other broker side effect in this
// app. requestId MUST be supplied by the caller (never generated here) so a
// retry can actually reuse it; see POST /api/trades/:correlationId/close.
async function submitCloseIntent({ entryCorrelationId, mode, quantity, requestId }) {
  if (MODE !== 'paper_tws' || !coreSnapshot.ready || CORE_TOKEN.length < 32) {
    return { status: 'BLOCKED', code: 'MANUAL_EXECUTION_UNAVAILABLE' };
  }

  // A retried requestId (the operator's own action, or a network retry that
  // reuses the same UI-held id) must short-circuit to its already-durable
  // outcome BEFORE anything below is (re)computed -- in particular, before a
  // fresh, time-varying `sent_at` is generated and hashed, which would make
  // an otherwise-identical retry look like a *different* payload (a false
  // CLOSE_REQUEST_PAYLOAD_CONFLICT) instead of the safe no-op replay this is
  // supposed to be. This mirrors submitManualIntent's own durable-replay
  // guarantee, just checked one step earlier since this function (unlike
  // submitManualIntent) builds its own signal payload from scratch each call.
  const existingAlert = store.getAlertStatus('manual', requestId);
  if (existingAlert) {
    return {
      status: existingAlert.status,
      correlationId: existingAlert.correlation_id,
      brokerOrderId: existingAlert.order?.brokerOrderId || null,
      code: existingAlert.order?.errorCode || null,
    };
  }

  const alert = lookupOriginatingAlert(entryCorrelationId);
  const right = rightFromOpenAction(alert);
  if (!alert || !right || typeof alert.payload?.ticker !== 'string') {
    return { status: 'BLOCKED', code: 'ENTRY_REFERENCE_NOT_FOUND' };
  }

  const signal = {
    schema_version: '1',
    alert_id: requestId,
    sent_at: new Date().toISOString(),
    strategy_id: 'manual-close-ui',
    strategy_version: '1',
    action: mode === 'FULL_FLATTEN'
      ? (right === 'C' ? 'CLOSE_LONG_CALL_FULL_FLATTEN' : 'CLOSE_LONG_PUT_FULL_FLATTEN')
      : (right === 'C' ? 'CLOSE_LONG_CALL_REDUCE_ONLY_PARTIAL' : 'CLOSE_LONG_PUT_REDUCE_ONLY_PARTIAL'),
    ticker: alert.payload.ticker,
    // trade_ref must equal the referenced entry's own core-side registry key
    // exactly (position.correlation_id / broker_submissions.correlation_id),
    // never Node's own distinct UUID correlationId -- see
    // ExecutionEngine._entry_reference / _prepare_close.
    trade_ref: entryCorrelationId,
  };
  if (mode === 'REDUCE_ONLY_PARTIAL') signal.quantity = quantity;

  const payloadHash = createHash('sha256').update(canonicalPayload(signal)).digest('hex');
  let received;
  try {
    received = store.receive({
      source: 'manual',
      alertId: requestId,
      payloadHash,
      payload: signal,
      executionEligible: true,
      managementMode: 'USER_MANAGED',
    });
  } catch (error) {
    if (error instanceof DedupeConflictError) {
      return { status: 'BLOCKED', code: 'CLOSE_REQUEST_PAYLOAD_CONFLICT' };
    }
    throw error;
  }

  const prepared = store.prepareSubmission(received.intentId);
  if (!prepared || !prepared.claimed) {
    // Not claimable: either already terminal (a genuine duplicate submit of
    // this exact requestId -- return the durable prior outcome, never call
    // the core again) or a crash-recovered PROCESSING/SUBMISSION_UNKNOWN
    // row -- either way, this must never attempt a second broker call.
    const alertStatus = store.getAlertStatus('manual', requestId);
    return {
      status: alertStatus?.status || 'SUBMISSION_UNKNOWN',
      correlationId: received.correlationId,
      brokerOrderId: alertStatus?.order?.brokerOrderId || null,
      code: alertStatus?.order?.errorCode || null,
    };
  }

  const coreRequest = {
    broker: 'IBKR',
    idempotencyKey: `manual:${requestId}`,
    intentId: prepared.orderId,
    correlationId: received.correlationId,
    source: 'MANUAL_UI',
    alertId: requestId,
    ownership: 'APP_OWNED',
    signal,
  };

  try {
    const result = await twsAdapter.closeTrade(coreRequest);
    if (result.status === 'BLOCKED') {
      store.finishSubmission(received.intentId, prepared.orderId, { status: 'BLOCKED', errorCode: result.code });
      return { status: 'BLOCKED', code: result.code, correlationId: received.correlationId };
    }
    store.finishSubmission(received.intentId, prepared.orderId, {
      status: 'SUBMITTED', brokerOrderId: result.brokerOrderId,
    });
    return { status: 'SUBMITTED', correlationId: received.correlationId, brokerOrderId: result.brokerOrderId };
  } catch (error) {
    const errorCode = machineCode(error.code, 'IBKR_CLOSE_SUBMISSION_UNKNOWN');
    store.finishSubmission(received.intentId, prepared.orderId, { status: 'SUBMISSION_UNKNOWN', errorCode });
    return { status: 'SUBMISSION_UNKNOWN', code: errorCode, correlationId: received.correlationId };
  }
}

function overlayProfile(profile) {
  // A profile matches the running core only if BOTH its environment category
  // AND its exact host/port equal what the core actually connected through —
  // otherwise two same-environment profiles (e.g. live-tws and live-gateway)
  // could both render as ready/unlocked when only one is truly connected.
  const matchesRunningCore = profile.environment === coreSnapshot.environment
    && coreSnapshot.ibkrHost !== null
    && coreSnapshot.ibkrPort !== null
    && profile.host === coreSnapshot.ibkrHost
    && profile.port === coreSnapshot.ibkrPort;
  return {
    ...profile,
    ready: matchesRunningCore && coreSnapshot.ready === true,
    liveUnlocked: profile.environment === 'LIVE' && matchesRunningCore && coreSnapshot.ready === true,
  };
}

async function drainQueue() {
  if (MODE !== 'paper_tws' || drainRunning) return;
  drainRunning = true;
  try {
    await checkCore();
    // Same background cadence as checkCore() (this function already runs
    // every drainTimer tick, see below) -- keeps positionSnapshot from ever
    // going more than ~2s stale even with no pending TradingView queue work.
    await checkPositions();
    if (!coreSnapshot.ready) return;
    while (store.findNextReady('tradingview') !== null) {
      await processor.processNext({ source: 'tradingview' });
    }
  } finally {
    drainRunning = false;
  }
}

async function handleWebhook(req, res) {
  let counted = false;
  try {
    if (!ingress) {
      throw new WebhookError('WEBHOOK_NOT_CONFIGURED', 'TradingView webhook authentication is not configured', 503);
    }
    if (!String(req.headers['content-type'] || '').toLowerCase().startsWith('application/json')) {
      throw new WebhookError('CONTENT_TYPE_REQUIRED', 'Content-Type must be application/json', 415);
    }
    assertWebhookRate(req);
    webhookInFlight += 1;
    counted = true;
    const body = await rawBody(req, WEBHOOK_MAX_BODY);
    const accepted = ingress.handle({ headers: req.headers, rawBody: body });
    json(res, accepted.statusCode, accepted.body, accepted.headers);
    setImmediate(() => drainQueue().catch((error) => safeLogger.error('TradingView queue drain failed', { code: machineCode(error.code, 'QUEUE_DRAIN_FAILED') })));
  } catch (error) {
    if (error instanceof WebhookError) {
      store.recordSecurityEvent({
        source: 'tradingview',
        code: machineCode(error.code, 'WEBHOOK_REJECTED'),
        details: { http_status: error.statusCode },
      });
    }
    throw error;
  } finally {
    if (counted) webhookInFlight -= 1;
  }
}

async function route(req, res) {
  const url = new URL(req.url, 'http://localhost');

  if (url.pathname === '/healthz') {
    const suppliedToken = url.searchParams.get('instanceToken');
    return json(res, 200, {
      status: 'ok',
      listenersReady: mainListenerReady && ingressListenerReady,
      instanceToken: DESKTOP_INSTANCE_TOKEN && suppliedToken === DESKTOP_INSTANCE_TOKEN
        ? DESKTOP_INSTANCE_TOKEN
        : null,
    });
  }

  if (url.pathname === '/api/tradingview/runtime' && req.method === 'GET') {
    const forceRefresh = url.searchParams.get('refresh') === '1';
    if (MODE === 'paper_tws' && (forceRefresh || !coreSnapshot.checkedAt || Date.now() - new Date(coreSnapshot.checkedAt).getTime() > 5_000)) {
      await checkCore();
    }
    const profiles = operatorStore.listProfiles();
    const activeProfile = profiles.find((profile) => profile.selected) || null;
    return json(res, 200, {
      environment: coreSnapshot.environment || activeProfile?.environment || 'PAPER',
      activeProfileId: activeProfile?.id || null,
      mode: MODE,
      webhook: { configured: WEBHOOK_CONFIGURED, endpoint: `http://127.0.0.1:${INGRESS_PORT}/webhooks/tradingview` },
      ledger: { ready: true },
      core: coreSnapshot,
      capabilities: {
        // TradingView-sourced alerts ARE auto-submitted with zero human
        // review once ownership is APP_MANAGED (the default -- see
        // src/operator/store.js) and this evaluates true. Protection and
        // transition management run unconditionally alongside it (see
        // __main__.py). Do not reintroduce "still gated" language here or
        // in docs/ without also actually gating the code.
        tradingviewPaperEntry: MODE === 'paper_tws' && APP_MANAGEMENT_EXECUTION_AVAILABLE,
        manualProposal: true,
        // Manual paper entry (POST /api/trade-intents/manual + its /submit)
        // is real and working as of this build -- gated by the same
        // MODE/MANUAL_EXECUTION_AVAILABLE condition submitManualIntent()
        // itself checks, not a hardcoded false.
        manualPaperEntry: MODE === 'paper_tws' && MANUAL_EXECUTION_AVAILABLE,
        // App-managed protection/transitions (protection.py/transitions.py's
        // periodic sweep, driven by an APP_MANAGED entry) are real and
        // working as of this build, and reachable through both submission
        // paths that can create an APP_MANAGED entry: manual submission
        // (buildManualCoreRequest with managementMode === 'APP_MANAGED') and
        // TradingView auto-submit (APP_MANAGEMENT_EXECUTION_AVAILABLE);
        // true whenever either is available.
        appTradeManagement: MODE === 'paper_tws' && (MANUAL_EXECUTION_AVAILABLE || APP_MANAGEMENT_EXECUTION_AVAILABLE),
        // Live entry itself is never Node-flipped -- the core independently
        // refuses to trade unless its own live account/confirmation-phrase
        // config is satisfied (AGENTS.md) -- but whichever submission path is
        // available (manual and/or TradingView) does carry a live order
        // through once the core self-reports a genuinely live-configured,
        // ready session, so this must track the same availability as the
        // paper capabilities above, not just the TradingView-only flag.
        liveEntry: MODE === 'paper_tws' && coreSnapshot.environment === 'LIVE'
          && (MANUAL_EXECUTION_AVAILABLE || APP_MANAGEMENT_EXECUTION_AVAILABLE),
      },
      updatedAt: new Date().toISOString(),
    });
  }

  if (url.pathname === '/api/core/process' && req.method === 'GET') {
    return json(res, 200, coreProcessSnapshot());
  }

  if (url.pathname === '/api/core/process/start' && req.method === 'POST') {
    const result = startCoreProcess();
    await new Promise((resolve) => setTimeout(resolve, 500));
    await checkCore();
    return json(res, result.started ? 200 : 409, {
      ...result,
      message: result.started ? 'Core process started.' : coreStartFailureMessage(result.reason),
      process: coreProcessSnapshot(),
      core: coreSnapshot,
    });
  }

  if (url.pathname === '/api/core/process/stop' && req.method === 'POST') {
    const result = stopCoreProcess();
    return json(res, result.stopped ? 200 : 409, {
      ...result,
      message: result.stopped ? 'Core process stop signal sent.' : 'No managed core process is running.',
      process: coreProcessSnapshot(),
    });
  }

  if (url.pathname === '/api/core/process/restart' && req.method === 'POST') {
    stopCoreProcess();
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    const result = startCoreProcess();
    await new Promise((resolve) => setTimeout(resolve, 500));
    await checkCore();
    return json(res, result.started ? 200 : 409, {
      ...result,
      message: result.started ? 'Core process restarted.' : coreStartFailureMessage(result.reason),
      process: coreProcessSnapshot(),
      core: coreSnapshot,
    });
  }

  if (url.pathname === '/api/connection-profiles' && req.method === 'GET') {
    const items = operatorStore.listProfiles().map(overlayProfile);
    return json(res, 200, {
      items,
      activeProfileId: items.find((profile) => profile.selected)?.id || null,
    });
  }

  if (url.pathname.startsWith('/api/connection-profiles/') && req.method === 'PUT') {
    try {
      const id = decodeURIComponent(url.pathname.slice('/api/connection-profiles/'.length));
      const body = await jsonBody(req);
      const updates = {
        host: body.host,
        port: body.port,
        clientId: body.clientId,
        selected: body.selected === true,
      };
      // Only touched when the request body explicitly includes the key (even
      // as null, to intentionally clear it) -- e.g. switching the selected
      // profile must never silently reset a previously configured value.
      if (Object.hasOwn(body, 'capitalPerTradeDollars')) {
        updates.capitalPerTradeDollars = body.capitalPerTradeDollars;
      }
      const profile = operatorStore.updateProfile(id, updates);
      if (!profile) return json(res, 404, { code: 'PROFILE_NOT_FOUND', message: 'Connection profile was not found' });
      return json(res, 200, { profile: overlayProfile(profile) });
    } catch (error) {
      return operatorError(res, error);
    }
  }

  if (url.pathname === '/api/management/defaults' && req.method === 'GET') {
    return json(res, 200, operatorStore.managementDefaults());
  }

  if (url.pathname === '/api/management/defaults' && req.method === 'PUT') {
    try {
      return json(res, 200, await Promise.resolve(operatorStore.updateManagementDefaults(await jsonBody(req))));
    } catch (error) {
      return operatorError(res, error);
    }
  }

  if (url.pathname === '/api/tradingview/ownership' && req.method === 'GET') {
    return json(res, 200, operatorStore.tradingViewOwnership());
  }

  if (url.pathname === '/api/tradingview/ownership' && req.method === 'PUT') {
    try {
      return json(res, 200, operatorStore.updateTradingViewOwnership(await jsonBody(req)));
    } catch (error) {
      return operatorError(res, error);
    }
  }

  if (url.pathname === '/api/trade-intents/manual' && req.method === 'POST') {
    try {
      const intent = operatorStore.createManualIntent(await jsonBody(req));
      const preview = await previewManualIntent(intent);
      // Autosend collapses review->submit into the single round trip a 0DTE
      // entry needs; it only ever fires on a successful, freshly-resolved
      // preview, never on a blocked/stale one.
      const submission = preview?.status === 'PREVIEW_READY' && operatorStore.managementDefaults().manualAutosend
        ? await submitManualIntent(intent)
        : null;
      return json(res, 202, { intent, preview, submission });
    } catch (error) {
      return operatorError(res, error);
    }
  }

  if (url.pathname.startsWith('/api/trade-intents/manual/') && url.pathname.endsWith('/submit') && req.method === 'POST') {
    const id = decodeURIComponent(url.pathname.slice('/api/trade-intents/manual/'.length, -'/submit'.length));
    const intent = operatorStore.getManualIntent(id);
    if (!intent) return json(res, 404, { code: 'MANUAL_INTENT_NOT_FOUND', message: 'Manual proposal was not found' });
    return json(res, 202, { submission: await submitManualIntent(intent) });
  }

  if (url.pathname === '/api/trades/active' && req.method === 'GET') {
    const forceRefresh = url.searchParams.get('refresh') === '1';
    if (MODE === 'paper_tws' && (forceRefresh || !positionSnapshot.checkedAt || Date.now() - new Date(positionSnapshot.checkedAt).getTime() > 5_000)) {
      await checkPositions();
    }
    if (MODE !== 'paper_tws') {
      return json(res, 200, {
        items: [],
        positionsStatus: 'NOT_REQUIRED',
        checkedAt: positionSnapshot.checkedAt,
        reason: 'CAPTURE_ONLY',
        message: 'Active position tracking requires paper_tws execution mode.',
      });
    }
    if (positionSnapshot.status !== 'OK') {
      // Fail closed: `items` stays null, never `[]` -- a poll failure or a
      // not-yet-checked state must never render as "confirmed zero
      // positions" (see AGENTS.md and checkPositions() above).
      return json(res, 200, {
        items: null,
        positionsStatus: positionSnapshot.status,
        checkedAt: positionSnapshot.checkedAt,
        reason: positionSnapshot.reason,
        message: 'Broker-confirmed position data is currently unavailable; this is not the same as zero open positions.',
      });
    }
    const activePositions = positionSnapshot.items.filter(
      (pos) => pos.operator_position_status === 'ACTIVE_CONFIRMED'
        && pos.lifecycle_status !== 'CLOSED'
        && pos.open_quantity !== '0'
        && Number(pos.open_quantity || 0) > 0
    );
    const items = await Promise.all(activePositions.map(mapActiveTradeItem));
    const dailyPnl = computeDailyPnl(positionSnapshot.items);
    return json(res, 200, {
      items,
      dailyPnl,
      positionsStatus: 'OK',
      checkedAt: positionSnapshot.checkedAt,
      reason: null,
      message: null,
    });
  }

  if (url.pathname === '/api/trades/closed-today' && req.method === 'GET') {
    const forceRefresh = url.searchParams.get('refresh') === '1';
    if (MODE === 'paper_tws' && (forceRefresh || !positionSnapshot.checkedAt || Date.now() - new Date(positionSnapshot.checkedAt).getTime() > 5_000)) {
      await checkPositions();
    }
    if (MODE !== 'paper_tws') {
      return json(res, 200, {
        items: [], positionsStatus: 'NOT_REQUIRED', checkedAt: positionSnapshot.checkedAt,
        reason: 'CAPTURE_ONLY', message: 'Closed-position tracking requires paper_tws execution mode.',
        marketDate: newYorkDate(new Date()), timeZone: 'America/New_York',
      });
    }
    if (positionSnapshot.status !== 'OK') {
      return json(res, 200, {
        items: null, positionsStatus: positionSnapshot.status, checkedAt: positionSnapshot.checkedAt,
        reason: positionSnapshot.reason,
        message: 'Broker-confirmed closed-position data is currently unavailable; this is not the same as no closed positions.',
        marketDate: newYorkDate(new Date()), timeZone: 'America/New_York',
      });
    }
    const today = newYorkDate(new Date());
    const closedWithoutExecutionTime = positionSnapshot.items.some((position) => position.lifecycle_status === 'CLOSED' && !newYorkDate(position.closed_at || position.updated_at));
    if (closedWithoutExecutionTime) {
      return json(res, 200, {
        items: null, positionsStatus: 'UNAVAILABLE', checkedAt: positionSnapshot.checkedAt,
        reason: 'CLOSE_EXECUTION_TIME_UNAVAILABLE',
        message: 'A closed position lacks a normalized broker close-execution time; today cannot be confirmed.',
        marketDate: today, timeZone: 'America/New_York',
      });
    }
    const items = positionSnapshot.items.filter((position) => isClosedToday(position, today)).map(mapClosedTradeItem);
    return json(res, 200, {
      items, positionsStatus: 'OK', checkedAt: positionSnapshot.checkedAt, reason: null, message: null,
      marketDate: today, timeZone: 'America/New_York',
    });
  }

  // Read-only proxy for the core's /private/v1/reconciliation -- lets the
  // operator see *why* new opens are globally blocked (an unresolved
  // submission/protection/transition outcome) without inventing a second
  // source of truth. Always live-fetched (no snapshot/cache like
  // positions/runtime) since it's only ever read on demand from a small
  // "why is this blocked" panel, never polled at TradingView-alert volume.
  if (url.pathname === '/api/reconciliation' && req.method === 'GET') {
    if (MODE !== 'paper_tws') {
      return json(res, 200, {
        status: 'NOT_REQUIRED', reason: 'CAPTURE_ONLY', recentRuns: null, unresolved: null,
        message: 'Reconciliation requires paper_tws execution mode.',
      });
    }
    const reconciliation = await fetchCoreReconciliation();
    return json(res, 200, reconciliation);
  }

  if (url.pathname.startsWith('/api/trades/') && url.pathname.endsWith('/close') && req.method === 'POST') {
    const entryCorrelationId = decodeURIComponent(url.pathname.slice('/api/trades/'.length, -'/close'.length));
    if (!entryCorrelationId) {
      return json(res, 400, { code: 'ENTRY_REFERENCE_REQUIRED', message: 'An entry correlationId path segment is required' });
    }
    const body = await jsonBody(req);
    const mode = body.mode;
    if (mode !== 'REDUCE_ONLY_PARTIAL' && mode !== 'FULL_FLATTEN') {
      return json(res, 400, { code: 'CLOSE_MODE_INVALID', message: 'mode must be REDUCE_ONLY_PARTIAL or FULL_FLATTEN' });
    }
    if (typeof body.requestId !== 'string' || !UUID_PATTERN.test(body.requestId)) {
      return json(res, 400, {
        code: 'CLOSE_REQUEST_ID_REQUIRED',
        message: 'requestId must be a stable, client-generated UUID (reused on retry) so a retry cannot submit a second order',
      });
    }
    let quantity;
    if (mode === 'REDUCE_ONLY_PARTIAL') {
      quantity = body.quantity;
      if (!Number.isSafeInteger(quantity) || quantity < 1) {
        return json(res, 400, { code: 'CLOSE_QUANTITY_INVALID', message: 'quantity must be a positive integer for REDUCE_ONLY_PARTIAL' });
      }
    } else if (Object.hasOwn(body, 'quantity')) {
      return json(res, 400, { code: 'CLOSE_QUANTITY_NOT_ALLOWED', message: 'FULL_FLATTEN must not include a quantity' });
    }
    const submission = await submitCloseIntent({ entryCorrelationId, mode, quantity, requestId: body.requestId });
    return json(res, 202, { submission });
  }

  if (url.pathname === '/api/tradingview/alerts' && req.method === 'GET') {
    const limit = Number(url.searchParams.get('limit') || 100);
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
      return json(res, 400, { code: 'INVALID_LIMIT', message: 'limit must be an integer from 1 through 500' });
    }
    return json(res, 200, { items: store.listAlerts(limit).map(mapAlert), asOf: new Date().toISOString() });
  }

  if (url.pathname === '/api/tradingview/attention-risks' && req.method === 'GET') {
    return json(res, 200, {
      items: store.listSubmissionUnknownAlerts().map(mapAlert),
      asOf: new Date().toISOString(),
    });
  }

  if (url.pathname.startsWith('/api/tradingview/alerts/') && req.method === 'GET') {
    const correlationId = decodeURIComponent(url.pathname.slice('/api/tradingview/alerts/'.length));
    const listed = store.listAlerts(500).find((item) => item.correlation_id === correlationId);
    if (!listed) return json(res, 404, { code: 'ALERT_NOT_FOUND', message: 'Alert correlation was not found' });
    const alert = store.getAlertStatus(listed.source, listed.alertId);
    const timeline = store.getAlertTimeline(listed.source, listed.alertId);
    return json(res, 200, { alert: mapAlert(alert), timeline });
  }

  if (url.pathname === '/api/orders' && req.method === 'POST') {
    return json(res, 410, {
      code: 'LEGACY_ORDER_PATH_DISABLED',
      message: 'Direct manual order submission is disabled; use the durable TradingView paper pipeline',
    });
  }
  if (url.pathname.startsWith('/api/')) return json(res, 404, { code: 'API_ROUTE_NOT_FOUND', message: 'API route not found' });

  const pathname = url.pathname === '/' ? 'index.html' : normalize(url.pathname).replace(/^\.{2}(\/|\\|$)/, '');
  const file = join(ROOT, pathname);
  if (!file.startsWith(ROOT)) return json(res, 403, { code: 'FORBIDDEN', message: 'Forbidden' });
  const types = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
  };
  try {
    const data = await readFile(file);
    res.writeHead(200, {
      'content-type': types[extname(file)] || 'application/octet-stream',
      'x-content-type-options': 'nosniff',
      'referrer-policy': 'no-referrer',
      'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    });
    res.end(data);
  } catch {
    json(res, 404, { code: 'NOT_FOUND', message: 'Not found' });
  }
}

const server = http.createServer((req, res) => route(req, res).catch((error) => {
  const status = error instanceof WebhookError ? error.statusCode : 500;
  const code = machineCode(error.code, status === 500 ? 'INTERNAL_ERROR' : 'REQUEST_REJECTED');
  if (status === 500) safeLogger.error('Request failed', { code });
  json(res, status, { code, message: status === 500 ? 'Internal service error' : error.message });
}));

async function ingressRoute(req, res) {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/healthz' && req.method === 'GET') {
    return json(res, 200, { status: 'ok', webhookConfigured: WEBHOOK_CONFIGURED });
  }
  if (url.pathname === '/webhooks/tradingview' && req.method === 'POST') return handleWebhook(req, res);
  return json(res, 404, { code: 'PUBLIC_ROUTE_NOT_FOUND', message: 'Public ingress exposes only the TradingView webhook' });
}

const ingressServer = http.createServer((req, res) => ingressRoute(req, res).catch((error) => {
  const status = error instanceof WebhookError ? error.statusCode : 500;
  const code = machineCode(error.code, status === 500 ? 'INTERNAL_ERROR' : 'REQUEST_REJECTED');
  if (status === 500) safeLogger.error('Public ingress failed', { code });
  json(res, status, { code, message: status === 500 ? 'Internal service error' : error.message });
}));

let mainListenerReady = false;
let ingressListenerReady = false;
server.listen(PORT, '127.0.0.1', () => {
  mainListenerReady = true;
  console.log(`QuickyTrade running at http://127.0.0.1:${PORT}`);
  console.log(`TradingView mode: ${MODE}; webhook authentication: ${WEBHOOK_CONFIGURED ? 'configured' : 'not configured'}`);
});
ingressServer.listen(INGRESS_PORT, '127.0.0.1', () => {
  ingressListenerReady = true;
  console.log(`TradingView-only ingress at http://127.0.0.1:${INGRESS_PORT}/webhooks/tradingview`);
});

const drainTimer = setInterval(() => drainQueue().catch(() => {}), 2_000);
drainTimer.unref();

let shutdownStarted = false;
const desktopParentWatch = DESKTOP_PARENT_PID
  ? setInterval(() => {
    // When the Tauri process is replaced or killed, macOS reparents this Node
    // child. Exit instead of leaving a stale loopback service on the app port.
    if (process.ppid !== DESKTOP_PARENT_PID) shutdown();
  }, 1_000)
  : null;
desktopParentWatch?.unref();
if (DESKTOP_PARENT_PID) {
  // Rust owns the write end of this pipe. It closes even when the Tauri
  // process is killed before any application cleanup hook can run.
  process.stdin.resume();
  process.stdin.once('end', shutdown);
  process.stdin.once('error', shutdown);
}

function shutdown() {
  if (shutdownStarted) return;
  shutdownStarted = true;
  clearInterval(drainTimer);
  if (desktopParentWatch) clearInterval(desktopParentWatch);
  stopCoreProcess();
  let remaining = 2;
  const closed = () => {
    remaining -= 1;
    if (remaining > 0) return;
    store.close();
    operatorStore.close();
    process.exit(0);
  };
  server.close(closed);
  ingressServer.close(closed);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
