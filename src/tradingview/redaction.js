const SENSITIVE_KEY = /(?:authorization|cookie|secret|signature|token|password|passphrase|api[_-]?key|account(?:[_-]?id|[_-]?number)?)/i;

function redactString(value) {
  return value
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]')
    .replace(/\b(?:secret|token|password|api[_-]?key|account(?:[_-]?id|[_-]?number)?)\s*[=:]\s*[^\s,;]+/gi, (match) => {
      const separator = match.includes('=') ? '=' : ':';
      return `${match.split(/[=:]/, 1)[0]}${separator}[REDACTED]`;
    });
}

export function redact(value, seen = new WeakSet()) {
  if (typeof value === 'string') return redactString(value);
  if (value === null || typeof value !== 'object') return value;
  if (seen.has(value)) return '[CIRCULAR]';
  seen.add(value);

  if (Array.isArray(value)) return value.map((item) => redact(item, seen));

  const result = {};
  for (const [key, item] of Object.entries(value)) {
    result[key] = SENSITIVE_KEY.test(key) ? '[REDACTED]' : redact(item, seen);
  }
  return result;
}

export function safeErrorCode(error, fallback = 'IBKR_ADAPTER_ERROR') {
  const candidate = typeof error?.code === 'string' ? error.code : fallback;
  return /^[A-Z0-9_]{1,64}$/.test(candidate) ? candidate : fallback;
}
