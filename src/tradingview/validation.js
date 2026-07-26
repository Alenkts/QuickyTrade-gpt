import { WebhookError } from './errors.js';

export const LIVE_SCHEMA_VERSION = '1';
export const ACTIONS = Object.freeze([
  'OPEN_LONG_CALL',
  'OPEN_LONG_PUT',
  'CLOSE_LONG_CALL',
  'CLOSE_LONG_PUT',
]);

const REQUIRED_FIELDS = Object.freeze([
  'schema_version',
  'alert_id',
  'sent_at',
  'strategy_id',
  'strategy_version',
  'action',
  'ticker',
]);
const OPTIONAL_FIELDS = Object.freeze([
  'target_dte',
  'strike_policy',
  'risk_hint',
  'exit_policy_id',
  'entry_alert_id',
  'trade_ref',
]);
const ALL_FIELDS = Object.freeze([...REQUIRED_FIELDS, ...OPTIONAL_FIELDS]);
const ALLOWED_SET = new Set(ALL_FIELDS);
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const VERSION = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;
const TICKER = /^[A-Z][A-Z0-9./-]{0,14}$/;
const ISO_WITH_ZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/;
// (source, alert_id) is the *sole* durable deduplication key. A bare timestamp
// carries no strategy, symbol, or direction, so two strategies firing in the
// same second collide: identical payloads are silently absorbed as a replay,
// and differing ones raise a spurious conflict. Neither is a correct outcome
// for two genuinely distinct signals. Require at least one non-timestamp
// component, per the documented `<strategy>-<TICKER>-<C|P>-<sent_at>` shape.
const BARE_TIMESTAMP_ALERT_ID = /^[0-9]+$|^\d{4}-\d{2}-\d{2}(?:[T:._-]\d{2})*(?:\.\d+)?Z?$/;

function fail(code, message) {
  throw new WebhookError(code, message, 400);
}

function requireExactObject(value, field, requiredKeys) {
  if (value === null || Array.isArray(value) || typeof value !== 'object') {
    fail('INVALID_FIELD', `${field} must be an object`);
  }
  const keys = Object.keys(value);
  const unknown = keys.filter((key) => !requiredKeys.includes(key));
  const missing = requiredKeys.filter((key) => !Object.hasOwn(value, key));
  if (unknown.length > 0 || missing.length > 0) {
    fail('INVALID_FIELD', `${field} must contain exactly: ${requiredKeys.join(', ')}`);
  }
}

function requireIdentifier(value, field) {
  if (typeof value !== 'string' || value !== value.trim() || !IDENTIFIER.test(value)) {
    fail('INVALID_FIELD', `${field} has an invalid format`);
  }
}

export function toBodyBuffer(rawBody, maxBodyBytes = 64 * 1024) {
  if (!(typeof rawBody === 'string' || Buffer.isBuffer(rawBody) || rawBody instanceof Uint8Array)) {
    fail('INVALID_BODY', 'Webhook body must be raw UTF-8 bytes or a string');
  }
  const body = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(rawBody);
  if (body.byteLength === 0) fail('EMPTY_BODY', 'Webhook body cannot be empty');
  if (body.byteLength > maxBodyBytes) {
    throw new WebhookError('BODY_TOO_LARGE', `Webhook body exceeds ${maxBodyBytes} bytes`, 413);
  }
  return body;
}

export function parseAndValidatePayload(rawBody, {
  now = new Date(),
  maxAgeMs = 5 * 60 * 1000,
  maxFutureSkewMs = 30 * 1000,
  maxRiskHintContracts = 100,
  supportedSchemaVersion = LIVE_SCHEMA_VERSION,
  // ATM_OFFSET is pure listed-strike geometry: no quote, no delta, no premium,
  // and critically no band check (selection.choose_listed_strike). Accepting it
  // from the wire hands contract selection to whoever wrote the Pine script and
  // bypasses the operator's configured strike band entirely -- that is how 5 of
  // 6 trades in the 07-24 session pinned to the same strike all day. The
  // untrusted ingress passes false; the manual operator path, where a human
  // picked the strike deliberately, leaves it true.
  allowAtmOffsetStrikePolicy = true,
} = {}) {
  if (!Number.isSafeInteger(maxRiskHintContracts) || maxRiskHintContracts < 1) {
    throw new TypeError('maxRiskHintContracts must be a positive integer');
  }
  let value;
  if (rawBody !== null && typeof rawBody === 'object' && !Buffer.isBuffer(rawBody) && !(rawBody instanceof Uint8Array)) {
    value = rawBody;
  } else {
    try {
      value = JSON.parse(Buffer.from(rawBody).toString('utf8'));
    } catch {
      fail('INVALID_JSON', 'Webhook body must be valid JSON');
    }
  }

  if (value === null || Array.isArray(value) || typeof value !== 'object') {
    fail('INVALID_PAYLOAD', 'Webhook payload must be a JSON object');
  }

  const keys = Object.keys(value);
  const unknown = keys.filter((key) => !ALLOWED_SET.has(key));
  const missing = REQUIRED_FIELDS.filter((key) => !Object.hasOwn(value, key));
  if (unknown.length > 0) fail('UNKNOWN_FIELDS', `Unknown payload fields: ${unknown.sort().join(', ')}`);
  if (missing.length > 0) fail('MISSING_FIELDS', `Missing payload fields: ${missing.join(', ')}`);

  for (const field of REQUIRED_FIELDS) {
    if (typeof value[field] !== 'string' || value[field].length === 0) {
      fail('INVALID_FIELD', `${field} must be a non-empty string`);
    }
    if (value[field] !== value[field].trim()) {
      fail('INVALID_FIELD', `${field} cannot have leading or trailing whitespace`);
    }
  }

  if (value.schema_version !== supportedSchemaVersion) {
    fail('UNSUPPORTED_SCHEMA_VERSION', `schema_version must be ${supportedSchemaVersion}`);
  }
  if (!IDENTIFIER.test(value.alert_id)) fail('INVALID_ALERT_ID', 'alert_id has an invalid format');
  if (BARE_TIMESTAMP_ALERT_ID.test(value.alert_id)) {
    fail('INVALID_ALERT_ID', 'alert_id must identify the signal, not just its time (e.g. "mystrat-IWM-P-2026-07-24T13:40:00Z")');
  }
  if (!IDENTIFIER.test(value.strategy_id)) fail('INVALID_STRATEGY_ID', 'strategy_id has an invalid format');
  if (!VERSION.test(value.strategy_version)) fail('INVALID_STRATEGY_VERSION', 'strategy_version has an invalid format');
  if (!ACTIONS.includes(value.action)) fail('INVALID_ACTION', 'action is not supported');
  if (!TICKER.test(value.ticker)) fail('INVALID_TICKER', 'ticker must be an uppercase IBKR-compatible symbol');
  if (!ISO_WITH_ZONE.test(value.sent_at)) fail('INVALID_SENT_AT', 'sent_at must be an ISO-8601 timestamp with a timezone');

  const sentAtMs = Date.parse(value.sent_at);
  if (!Number.isFinite(sentAtMs)) fail('INVALID_SENT_AT', 'sent_at is not a valid timestamp');
  const nowMs = now instanceof Date ? now.getTime() : Number(now);
  if (!Number.isFinite(nowMs)) throw new TypeError('now must be a valid Date or epoch milliseconds');
  if (nowMs - sentAtMs > maxAgeMs) fail('ALERT_TOO_OLD', 'Alert timestamp is outside the accepted age window');
  if (sentAtMs - nowMs > maxFutureSkewMs) fail('ALERT_FROM_FUTURE', 'Alert timestamp exceeds the accepted clock skew');

  if (Object.hasOwn(value, 'target_dte')) {
    if (!Number.isSafeInteger(value.target_dte) || value.target_dte < 0 || value.target_dte > 3650) {
      fail('INVALID_TARGET_DTE', 'target_dte must be an integer from 0 through 3650');
    }
  }
  if (Object.hasOwn(value, 'strike_policy')) {
    const policyType = value.strike_policy && typeof value.strike_policy === 'object'
      ? value.strike_policy.type
      : undefined;
    if (policyType === 'ATM_OFFSET') {
      if (!allowAtmOffsetStrikePolicy) {
        fail('STRIKE_POLICY_NOT_ALLOWED',
          'strike_policy ATM_OFFSET is not accepted from TradingView; omit strike_policy and let the app apply its configured strike band');
      }
      requireExactObject(value.strike_policy, 'strike_policy', ['type', 'offset']);
      if (!Number.isSafeInteger(value.strike_policy.offset)) {
        fail('INVALID_STRIKE_POLICY', 'strike_policy must use ATM_OFFSET with an integer offset');
      }
    } else if (policyType === 'TARGET_RANGE') {
      requireExactObject(value.strike_policy, 'strike_policy', ['type']);
    } else {
      fail('INVALID_STRIKE_POLICY', 'strike_policy must use ATM_OFFSET or TARGET_RANGE');
    }
  }
  if (Object.hasOwn(value, 'risk_hint')) {
    requireExactObject(value.risk_hint, 'risk_hint', ['max_contracts']);
    if (!Number.isSafeInteger(value.risk_hint.max_contracts)
      || value.risk_hint.max_contracts < 1
      || value.risk_hint.max_contracts > maxRiskHintContracts) {
      fail('INVALID_RISK_HINT', `risk_hint.max_contracts must be from 1 through ${maxRiskHintContracts}`);
    }
  }
  for (const field of ['exit_policy_id', 'entry_alert_id', 'trade_ref']) {
    if (Object.hasOwn(value, field)) requireIdentifier(value[field], field);
  }

  const isOpen = value.action === 'OPEN_LONG_CALL' || value.action === 'OPEN_LONG_PUT';
  if (isOpen) {
    if (Object.hasOwn(value, 'entry_alert_id') || Object.hasOwn(value, 'trade_ref')) {
      fail('INVALID_ACTION_FIELDS', 'Open alerts cannot contain close-reference fields');
    }
    if (!Object.hasOwn(value, 'target_dte')) {
      value.target_dte = 0;
    }
    if (!Object.hasOwn(value, 'strike_policy')) {
      value.strike_policy = { type: 'TARGET_RANGE' };
    }
  } else {
    if (['target_dte', 'strike_policy', 'risk_hint', 'exit_policy_id'].some((field) => Object.hasOwn(value, field))) {
      fail('INVALID_ACTION_FIELDS', 'Close alerts cannot contain open-selection fields');
    }
    const references = ['entry_alert_id', 'trade_ref'].filter((field) => Object.hasOwn(value, field));
    if (references.length !== 1) {
      fail('MISSING_CLOSE_REFERENCE', 'Close alerts require exactly one entry_alert_id or trade_ref');
    }
  }

  const normalized = {};
  for (const field of ALL_FIELDS) {
    if (!Object.hasOwn(value, field)) continue;
    if (field === 'strike_policy' || field === 'risk_hint') {
      normalized[field] = Object.freeze({ ...value[field] });
    } else {
      normalized[field] = value[field];
    }
  }
  return Object.freeze(normalized);
}
