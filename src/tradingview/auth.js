import { createHash, createHmac, timingSafeEqual } from 'node:crypto';
import { WebhookError } from './errors.js';

function headerValue(headers, name) {
  if (typeof headers?.get === 'function') return headers.get(name);
  const target = name.toLowerCase();
  for (const [key, value] of Object.entries(headers ?? {})) {
    if (key.toLowerCase() === target) return Array.isArray(value) ? value[0] : value;
  }
  return undefined;
}

function constantTimeTextEqual(left, right) {
  const leftDigest = createHash('sha256').update(String(left)).digest();
  const rightDigest = createHash('sha256').update(String(right)).digest();
  return timingSafeEqual(leftDigest, rightDigest);
}

function validHmac(headers, rawBody, hmacSecret) {
  if (!((typeof hmacSecret === 'string' || Buffer.isBuffer(hmacSecret)) && hmacSecret.length >= 32)) return false;
  const supplied = headerValue(headers, 'x-tradingview-signature');
  if (typeof supplied !== 'string') return false;
  const match = /^sha256=([a-fA-F0-9]{64})$/.exec(supplied.trim());
  const suppliedHex = match?.[1]?.toLowerCase() ?? '0'.repeat(64);
  const expectedHex = createHmac('sha256', hmacSecret).update(rawBody).digest('hex');
  return Boolean(match) && constantTimeTextEqual(suppliedHex, expectedHex);
}

function validBearer(headers, bearerSecret) {
  if (!(typeof bearerSecret === 'string' && bearerSecret.length >= 32)) return false;
  const supplied = headerValue(headers, 'authorization');
  if (typeof supplied !== 'string') return false;
  const match = /^Bearer\s+(.+)$/i.exec(supplied.trim());
  const token = match?.[1] ?? '';
  return Boolean(match) && constantTimeTextEqual(token, bearerSecret);
}

function validBodyToken(bodyAuthToken, authTokenSecret) {
  if (!(typeof authTokenSecret === 'string' && authTokenSecret.length >= 32)) return false;
  const token = typeof bodyAuthToken === 'string' ? bodyAuthToken : '';
  return constantTimeTextEqual(token, authTokenSecret) && token.length > 0;
}

export function verifyWebhookAuth(
  { headers, rawBody, bodyAuthToken },
  { hmacSecret, bearerSecret, authTokenSecret } = {},
) {
  const hmacConfigured = typeof hmacSecret === 'string' || Buffer.isBuffer(hmacSecret);
  const bearerConfigured = typeof bearerSecret === 'string';
  const bodyTokenConfigured = typeof authTokenSecret === 'string';
  if (bodyTokenConfigured && authTokenSecret.length < 32) {
    throw new TypeError('authTokenSecret must contain at least 32 characters');
  }
  if ((!hmacConfigured || hmacSecret.length === 0)
    && (!bearerConfigured || bearerSecret.length === 0)
    && (!bodyTokenConfigured || authTokenSecret.length === 0)) {
    throw new TypeError('At least one webhook authentication secret must be configured');
  }
  if (validHmac(headers, rawBody, hmacSecret)
    || validBearer(headers, bearerSecret)
    || validBodyToken(bodyAuthToken, authTokenSecret)) return true;
  throw new WebhookError('UNAUTHORIZED', 'Webhook authentication failed', 401);
}
