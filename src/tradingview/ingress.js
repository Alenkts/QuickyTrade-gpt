import { createHash } from 'node:crypto';
import { verifyWebhookAuth } from './auth.js';
import { WebhookError } from './errors.js';
import { redact } from './redaction.js';
import { parseAndValidatePayload, toBodyBuffer } from './validation.js';

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]));
  }
  return value;
}

export function canonicalPayload(payload) {
  return JSON.stringify(canonicalize(payload));
}

function extractBodyAuth(rawBody) {
  try {
    const parsed = JSON.parse(rawBody.toString('utf8'));
    if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
      return { bodyAuthToken: undefined, strippedPayload: parsed };
    }
    const bodyAuthToken = parsed.auth_token;
    const strippedPayload = Object.fromEntries(
      Object.entries(parsed).filter(([key]) => key !== 'auth_token'),
    );
    return { bodyAuthToken, strippedPayload };
  } catch {
    return { bodyAuthToken: undefined, strippedPayload: undefined };
  }
}

export class TradingViewWebhookIngress {
  constructor({
    store,
    hmacSecret,
    bearerSecret,
    authTokenSecret,
    source = 'tradingview',
    maxBodyBytes = 64 * 1024,
    maxAgeMs = 5 * 60 * 1000,
    maxFutureSkewMs = 30 * 1000,
    maxRiskHintContracts = 100,
    allowedTickers = undefined,
    allowedStrategies = undefined,
    executionEligible = false,
    managementResolver = () => ({ mode: 'APP_MANAGED', managementProfileId: null }),
    clock = () => new Date(),
    logger = undefined,
  }) {
    if (!store) throw new TypeError('store is required');
    if (!Number.isSafeInteger(maxRiskHintContracts) || maxRiskHintContracts < 1) {
      throw new TypeError('maxRiskHintContracts must be a positive integer');
    }
    this.store = store;
    this.auth = { hmacSecret, bearerSecret, authTokenSecret };
    this.source = source;
    this.maxBodyBytes = maxBodyBytes;
    this.maxAgeMs = maxAgeMs;
    this.maxFutureSkewMs = maxFutureSkewMs;
    this.maxRiskHintContracts = maxRiskHintContracts;
    this.allowedTickers = allowedTickers === undefined ? null : new Set(allowedTickers);
    this.allowedStrategies = allowedStrategies === undefined ? null : new Set(allowedStrategies);
    if (typeof executionEligible !== 'boolean') throw new TypeError('executionEligible must be boolean');
    this.executionEligible = executionEligible;
    if (typeof managementResolver !== 'function') throw new TypeError('managementResolver must be a function');
    this.managementResolver = managementResolver;
    this.clock = clock;
    this.logger = logger;
  }

  handle({ headers = {}, rawBody }) {
    const body = toBodyBuffer(rawBody, this.maxBodyBytes);
    const { bodyAuthToken, strippedPayload } = extractBodyAuth(body);
    verifyWebhookAuth({ headers, rawBody: body, bodyAuthToken }, this.auth);
    const payload = parseAndValidatePayload(strippedPayload === undefined ? body : strippedPayload, {
      now: this.clock(),
      maxAgeMs: this.maxAgeMs,
      maxFutureSkewMs: this.maxFutureSkewMs,
      maxRiskHintContracts: this.maxRiskHintContracts,
      // TradingView is an untrusted signal source. Contract geometry stays an
      // app-side policy decision -- see allowAtmOffsetStrikePolicy.
      allowAtmOffsetStrikePolicy: false,
    });
    if (this.allowedTickers && !this.allowedTickers.has(payload.ticker)) {
      throw new WebhookError('SYMBOL_NOT_ALLOWED', 'ticker is not in the local TradingView allowlist', 400);
    }
    if (this.allowedStrategies && this.allowedStrategies.size > 0
      && !this.allowedStrategies.has(payload.strategy_id)) {
      throw new WebhookError('STRATEGY_NOT_ALLOWED', 'strategy_id is not in the local TradingView allowlist', 400);
    }
    const payloadHash = createHash('sha256').update(canonicalPayload(payload)).digest('hex');
    const ownership = this.managementResolver();
    // Until the execution ledger can prove and protect app-owned quantity,
    // non-app management choices are captured for review but never auto-sent.
    const executionEligible = this.executionEligible && ownership.mode === 'APP_MANAGED';
    const received = this.store.receive({
      source: this.source,
      alertId: payload.alert_id,
      payloadHash,
      payload,
      executionEligible,
      managementMode: ownership.mode,
      managementPolicyId: ownership.managementProfileId || null,
      managementPolicy: ownership.managementPolicy || null,
    });

    this.logger?.info?.('TradingView alert accepted', redact({
      source: this.source,
      alert_id: payload.alert_id,
      correlation_id: received.correlationId,
      intent_id: received.intentId,
      duplicate: received.duplicate,
    }));

    return Object.freeze({
      statusCode: 202,
      headers: { 'content-type': 'application/json' },
      body: Object.freeze({
        accepted: true,
        status: received.status,
        alert_id: payload.alert_id,
        correlation_id: received.correlationId,
        intent_id: received.intentId,
        duplicate: received.duplicate,
      }),
    });
  }
}
