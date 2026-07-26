import { DatabaseSync } from 'node:sqlite';
import { randomUUID } from 'node:crypto';
import { DedupeConflictError } from './errors.js';
import { redact } from './redaction.js';

const SCHEMA = `
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE IF NOT EXISTS signal_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  correlation_id TEXT NOT NULL,
  source TEXT NOT NULL,
  alert_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  execution_eligible INTEGER NOT NULL DEFAULT 0 CHECK (execution_eligible IN (0,1)),
  management_mode TEXT NOT NULL DEFAULT 'APP_MANAGED'
    CHECK (management_mode IN ('APP_MANAGED','USER_MANAGED','TRADINGVIEW_MANAGED')),
  management_policy_id TEXT,
  management_policy_json TEXT,
  status TEXT NOT NULL CHECK (status IN ('READY','PROCESSING','SUBMITTED','BLOCKED','FAILED','SUBMISSION_UNKNOWN','EXPIRED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (source, alert_id)
);

CREATE TABLE IF NOT EXISTS signal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL REFERENCES signal_intents(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  details_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS signal_events_intent_time
  ON signal_events(intent_id, id);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL REFERENCES signal_intents(id) ON DELETE RESTRICT,
  attempt_number INTEGER NOT NULL CHECK (attempt_number = 1),
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('SUBMITTING','SUBMITTED','BLOCKED','FAILED','SUBMISSION_UNKNOWN')),
  broker TEXT NOT NULL CHECK (broker = 'IBKR'),
  broker_order_id TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (intent_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS security_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  code TEXT NOT NULL,
  details_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);
`;

function asIso(clock) {
  const value = clock();
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) throw new TypeError('clock returned an invalid date');
  return date.toISOString();
}

function parseJson(value) {
  return JSON.parse(value);
}

export class TradingViewStore {
  constructor(path = ':memory:', { clock = () => new Date(), uuid = randomUUID } = {}) {
    this.db = new DatabaseSync(path);
    this.clock = clock;
    this.uuid = uuid;
    this.db.exec(SCHEMA);
    this.#migrateCorrelationIds();
    this.#migrateExecutionEligibility();
    this.#migrateManagementOwnership();
    this.#migrateStatusVocabulary();
    this.#recoverInterruptedSubmissions();
  }

  close() {
    this.db.close();
  }

  #transaction(work) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = work();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }

  #event(intentId, eventType, details, occurredAt = asIso(this.clock)) {
    this.db.prepare(`
      INSERT INTO signal_events (intent_id, event_type, details_json, occurred_at)
      VALUES (?, ?, ?, ?)
    `).run(intentId, eventType, JSON.stringify(redact(details ?? {})), occurredAt);
  }

  #newCorrelationId() {
    const value = this.uuid();
    if (typeof value !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
      throw new TypeError('uuid must return a valid UUID');
    }
    return value;
  }

  #migrateCorrelationIds() {
    const columns = this.db.prepare(`PRAGMA table_info(signal_intents)`).all();
    if (!columns.some((column) => column.name === 'correlation_id')) {
      this.db.exec(`ALTER TABLE signal_intents ADD COLUMN correlation_id TEXT`);
    }

    const missing = this.db.prepare(`
      SELECT id FROM signal_intents
      WHERE correlation_id IS NULL OR correlation_id = ''
      ORDER BY id
    `).all();
    if (missing.length > 0) {
      this.#transaction(() => {
        const update = this.db.prepare(`UPDATE signal_intents SET correlation_id = ? WHERE id = ?`);
        for (const row of missing) update.run(this.#newCorrelationId(), row.id);
      });
    }
    this.db.exec(`
      CREATE UNIQUE INDEX IF NOT EXISTS signal_intents_correlation
        ON signal_intents(correlation_id);
      CREATE TRIGGER IF NOT EXISTS signal_intents_require_correlation_insert
        BEFORE INSERT ON signal_intents
        WHEN NEW.correlation_id IS NULL OR NEW.correlation_id = ''
        BEGIN
          SELECT RAISE(ABORT, 'correlation_id is required');
        END;
      CREATE TRIGGER IF NOT EXISTS signal_intents_require_correlation_update
        BEFORE UPDATE OF correlation_id ON signal_intents
        WHEN NEW.correlation_id IS NULL OR NEW.correlation_id = ''
        BEGIN
          SELECT RAISE(ABORT, 'correlation_id is required');
        END;
    `);
  }

  #migrateExecutionEligibility() {
    const columns = this.db.prepare(`PRAGMA table_info(signal_intents)`).all();
    if (!columns.some((column) => column.name === 'execution_eligible')) {
      // Existing records predate receipt-mode evidence. Quarantine them rather
      // than guessing that they were accepted for execution.
      this.db.exec(`
        ALTER TABLE signal_intents
          ADD COLUMN execution_eligible INTEGER NOT NULL DEFAULT 0
          CHECK (execution_eligible IN (0,1))
      `);
    }
  }

  #migrateManagementOwnership() {
    const columns = this.db.prepare(`PRAGMA table_info(signal_intents)`).all();
    if (!columns.some((column) => column.name === 'management_mode')) {
      // Existing rows predate an operator ownership decision. Do not rewrite
      // history by claiming that the app was selected to manage them.
      this.db.exec(`ALTER TABLE signal_intents ADD COLUMN management_mode TEXT NOT NULL DEFAULT 'LEGACY_ENTRY_ONLY'`);
      this.db.exec(`UPDATE signal_intents SET execution_eligible = 0`);
    }
    if (!columns.some((column) => column.name === 'management_policy_id')) {
      this.db.exec(`ALTER TABLE signal_intents ADD COLUMN management_policy_id TEXT`);
    }
    if (!columns.some((column) => column.name === 'management_policy_json')) {
      this.db.exec(`ALTER TABLE signal_intents ADD COLUMN management_policy_json TEXT`);
      // An earlier schema stored only a mutable policy identifier. Quarantine
      // those rows rather than resolving that identifier to today's policy.
      this.db.exec(`UPDATE signal_intents SET execution_eligible = 0 WHERE management_policy_json IS NULL`);
    }
  }

  // A database created before queue expiry existed carries a status CHECK
  // constraint without 'EXPIRED'. That is not cosmetic: findNextReady's expiry
  // UPDATE would raise ERR_SQLITE_ERROR inside the drain loop and leave the
  // stale intent sitting at 'READY', so the backlog would still flush on
  // reconnect -- the exact failure the expiry path exists to prevent. SQLite
  // cannot alter a CHECK in place, so the table is rebuilt (SQLite's documented
  // "other kinds of schema changes" procedure). Runs last, after every additive
  // column migration, so the explicit column list below is always satisfiable.
  //
  // EXPIRED is kept distinct from BLOCKED deliberately: BLOCKED means the core
  // evaluated the signal and definitively refused it, EXPIRED means no broker
  // was ever contacted. Collapsing them would make the ledger unable to
  // distinguish "refused" from "never ran".
  #migrateStatusVocabulary() {
    const table = this.db.prepare(`
      SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'signal_intents'
    `).get();
    if (!table?.sql || /'EXPIRED'/.test(table.sql)) return;

    // Legacy databases carry management_mode = 'LEGACY_ENTRY_ONLY' rows added by
    // #migrateManagementOwnership via ALTER TABLE, which bypasses the original
    // CHECK. The rebuilt constraint must admit that value or the copy fails.
    this.db.exec('PRAGMA foreign_keys = OFF');
    try {
      this.#transaction(() => {
        this.db.exec(`
          CREATE TABLE signal_intents_rebuild (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id TEXT NOT NULL,
            source TEXT NOT NULL,
            alert_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            execution_eligible INTEGER NOT NULL DEFAULT 0 CHECK (execution_eligible IN (0,1)),
            management_mode TEXT NOT NULL DEFAULT 'APP_MANAGED'
              CHECK (management_mode IN ('APP_MANAGED','USER_MANAGED','TRADINGVIEW_MANAGED','LEGACY_ENTRY_ONLY')),
            management_policy_id TEXT,
            management_policy_json TEXT,
            status TEXT NOT NULL
              CHECK (status IN ('READY','PROCESSING','SUBMITTED','BLOCKED','FAILED','SUBMISSION_UNKNOWN','EXPIRED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (source, alert_id)
          );
          INSERT INTO signal_intents_rebuild
            (id, correlation_id, source, alert_id, payload_hash, payload_json, execution_eligible,
             management_mode, management_policy_id, management_policy_json, status, created_at, updated_at)
          SELECT
            id, correlation_id, source, alert_id, payload_hash, payload_json, execution_eligible,
            management_mode, management_policy_id, management_policy_json, status, created_at, updated_at
          FROM signal_intents;
          DROP TABLE signal_intents;
          ALTER TABLE signal_intents_rebuild RENAME TO signal_intents;
        `);
      });
    } finally {
      this.db.exec('PRAGMA foreign_keys = ON');
    }
    // DROP TABLE took the correlation index and its two NOT-NULL-enforcing
    // triggers with it; this recreates them (all IF NOT EXISTS).
    this.#migrateCorrelationIds();
    const violations = this.db.prepare(`PRAGMA foreign_key_check`).all();
    if (violations.length > 0) {
      throw new Error(`signal_intents status migration left ${violations.length} dangling reference(s)`);
    }
  }

  #recoverInterruptedSubmissions() {
    const interrupted = this.db.prepare(`
      SELECT i.id AS intent_id, o.id AS order_id
      FROM signal_intents i
      JOIN orders o ON o.intent_id = i.id
      WHERE i.status = 'PROCESSING' AND o.status = 'SUBMITTING'
      ORDER BY i.id
    `).all();
    if (interrupted.length === 0) return;

    this.#transaction(() => {
      const now = asIso(this.clock);
      const updateOrder = this.db.prepare(`
        UPDATE orders
        SET status = 'SUBMISSION_UNKNOWN', error_code = 'PROCESS_INTERRUPTED', updated_at = ?
        WHERE id = ? AND status = 'SUBMITTING'
      `);
      const updateIntent = this.db.prepare(`
        UPDATE signal_intents SET status = 'SUBMISSION_UNKNOWN', updated_at = ?
        WHERE id = ? AND status = 'PROCESSING'
      `);
      for (const row of interrupted) {
        updateOrder.run(now, row.order_id);
        updateIntent.run(now, row.intent_id);
        this.#event(row.intent_id, 'SUBMISSION_UNKNOWN', {
          broker: 'IBKR',
          error_code: 'PROCESS_INTERRUPTED',
        }, now);
      }
    });
  }

  receive({
    source,
    alertId,
    payloadHash,
    payload,
    executionEligible = false,
    managementMode = 'APP_MANAGED',
    managementPolicyId = null,
    managementPolicy = null,
  }) {
    if (typeof executionEligible !== 'boolean') throw new TypeError('executionEligible must be boolean');
    if (!['APP_MANAGED', 'USER_MANAGED', 'TRADINGVIEW_MANAGED'].includes(managementMode)) {
      throw new TypeError('Unsupported management mode');
    }
    return this.#transaction(() => {
      const existing = this.db.prepare(`
        SELECT id, correlation_id, payload_hash, status FROM signal_intents
        WHERE source = ? AND alert_id = ?
      `).get(source, alertId);

      if (existing) {
        if (existing.payload_hash !== payloadHash) throw new DedupeConflictError();
        return {
          intentId: Number(existing.id),
          correlationId: existing.correlation_id,
          duplicate: true,
          status: existing.status,
        };
      }

      const now = asIso(this.clock);
      const correlationId = this.#newCorrelationId();
      const safePayload = redact(payload);
      const inserted = this.db.prepare(`
        INSERT INTO signal_intents
          (correlation_id, source, alert_id, payload_hash, payload_json, execution_eligible,
           management_mode, management_policy_id, management_policy_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?)
      `).run(correlationId, source, alertId, payloadHash, JSON.stringify(safePayload), executionEligible ? 1 : 0,
        managementMode, managementPolicyId, managementPolicy ? JSON.stringify(managementPolicy) : null, now, now);
      const intentId = Number(inserted.lastInsertRowid);
      this.#event(intentId, 'ALERT_READY', {
        duplicate: false,
        execution_eligible: executionEligible,
        receipt_mode: executionEligible ? 'paper_tws' : 'capture_only',
        management_mode: managementMode,
        management_policy_id: managementPolicyId,
      }, now);
      return { intentId, correlationId, duplicate: false, status: 'READY' };
    });
  }

  findNextReady(source, maxAgeMs = null) {
    const query = source === undefined
      ? `SELECT id, created_at FROM signal_intents WHERE status = 'READY' AND execution_eligible = 1 ORDER BY id LIMIT 1`
      : `SELECT id, created_at FROM signal_intents WHERE status = 'READY' AND execution_eligible = 1 AND source = ? ORDER BY id LIMIT 1`;
    const row = source === undefined ? this.db.prepare(query).get() : this.db.prepare(query).get(source);
    if (!row) return null;
    if (maxAgeMs !== null && maxAgeMs > 0 && row.created_at) {
      const ageMs = Date.now() - new Date(row.created_at).getTime();
      if (ageMs > maxAgeMs) {
        const now = asIso(this.clock);
        this.#transaction(() => {
          this.db.prepare(`UPDATE signal_intents SET status = 'EXPIRED', updated_at = ? WHERE id = ?`).run(now, row.id);
          this.#event(row.id, 'SIGNAL_EXPIRED', { reason: 'EXCEEDED_MAX_AGE_IN_QUEUE', age_ms: ageMs }, now);
        });
        return this.findNextReady(source, maxAgeMs);
      }
    }
    return Number(row.id);
  }

  prepareSubmission(intentId) {
    return this.#transaction(() => {
      const row = this.db.prepare(`SELECT * FROM signal_intents WHERE id = ?`).get(intentId);
      if (!row) return null;
      if (row.status !== 'READY') return { claimed: false, intent: this.#mapIntent(row) };
      if (row.execution_eligible !== 1) return { claimed: false, intent: this.#mapIntent(row) };

      const now = asIso(this.clock);
      const changed = this.db.prepare(`
        UPDATE signal_intents SET status = 'PROCESSING', updated_at = ?
        WHERE id = ? AND status = 'READY' AND execution_eligible = 1
      `).run(now, intentId);
      if (Number(changed.changes) !== 1) return { claimed: false, intent: this.getIntent(intentId) };

      const idempotencyKey = `${row.source}:${row.alert_id}`;
      const order = this.db.prepare(`
        INSERT INTO orders
          (intent_id, attempt_number, idempotency_key, status, broker, created_at, updated_at)
        VALUES (?, 1, ?, 'SUBMITTING', 'IBKR', ?, ?)
      `).run(intentId, idempotencyKey, now, now);
      this.#event(intentId, 'SUBMISSION_STARTED', { broker: 'IBKR', order_id: Number(order.lastInsertRowid) }, now);
      return {
        claimed: true,
        intent: { ...this.#mapIntent(row), status: 'PROCESSING' },
        orderId: Number(order.lastInsertRowid),
        idempotencyKey,
      };
    });
  }

  finishSubmission(intentId, orderId, { status, brokerOrderId = null, errorCode = null }) {
    const allowed = new Set(['SUBMITTED', 'BLOCKED', 'FAILED', 'SUBMISSION_UNKNOWN']);
    if (!allowed.has(status)) throw new TypeError(`Unsupported terminal order status: ${status}`);
    return this.#transaction(() => {
      const now = asIso(this.clock);
      const orderChanged = this.db.prepare(`
        UPDATE orders
        SET status = ?, broker_order_id = ?, error_code = ?, updated_at = ?
        WHERE id = ? AND intent_id = ? AND status = 'SUBMITTING'
      `).run(status, brokerOrderId, errorCode, now, orderId, intentId);
      if (Number(orderChanged.changes) !== 1) throw new Error('Order is not in SUBMITTING state');
      this.db.prepare(`
        UPDATE signal_intents SET status = ?, updated_at = ?
        WHERE id = ? AND status = 'PROCESSING'
      `).run(status, now, intentId);
      this.#event(intentId, status, { broker: 'IBKR', broker_order_id: brokerOrderId, error_code: errorCode }, now);
      return this.getIntent(intentId);
    });
  }

  #mapIntent(row) {
    return {
      id: Number(row.id),
      correlation_id: row.correlation_id,
      source: row.source,
      alertId: row.alert_id,
      payloadHash: row.payload_hash,
      payload: parseJson(row.payload_json),
      executionEligible: row.execution_eligible === 1,
      managementMode: row.management_mode || 'LEGACY_ENTRY_ONLY',
      managementPolicyId: row.management_policy_id || null,
      managementPolicy: row.management_policy_json ? parseJson(row.management_policy_json) : null,
      status: row.status,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    };
  }

  getIntent(intentId) {
    const row = this.db.prepare(`SELECT * FROM signal_intents WHERE id = ?`).get(intentId);
    return row ? this.#mapIntent(row) : null;
  }

  getAlertStatus(source, alertId) {
    const row = this.db.prepare(`SELECT * FROM signal_intents WHERE source = ? AND alert_id = ?`).get(source, alertId);
    if (!row) return null;
    const order = this.db.prepare(`
      SELECT id, attempt_number, idempotency_key, status, broker, broker_order_id, error_code, created_at, updated_at
      FROM orders WHERE intent_id = ? ORDER BY id DESC LIMIT 1
    `).get(row.id);
    return {
      ...this.#mapIntent(row),
      order: order ? {
        id: Number(order.id),
        attemptNumber: Number(order.attempt_number),
        idempotencyKey: order.idempotency_key,
        status: order.status,
        broker: order.broker,
        brokerOrderId: order.broker_order_id,
        errorCode: order.error_code,
        createdAt: order.created_at,
        updatedAt: order.updated_at,
      } : null,
    };
  }

  // Looks up an alert/intent by whatever the core reports as its correlation
  // for a position. Two distinct shapes are legitimate here and both must
  // resolve, because the core keys its own broker_submissions registry on the
  // idempotencyKey (engine.py: `registry_key = normalized["idempotencyKey"]`),
  // not on the UUID persistence receipt:
  //
  //   signal_intents.correlation_id  -> "989b6674-685f-..."      (UUID receipt)
  //   broker_submissions.correlation_id -> "tradingview:<alertId>" (idempotencyKey)
  //
  // Only those two columns are consulted. An earlier revision also matched
  // `orders.intent_id`, which meant a bare numeric string ("1") resolved to an
  // unrelated intent by rowid and mis-rendered its ticker/right -- a guess, and
  // this function must never guess. Returns null when nothing matches exactly.
  getAlertStatusByCorrelationId(correlationId) {
    if (typeof correlationId !== 'string' || !correlationId) return null;
    let row = this.db.prepare(`SELECT * FROM signal_intents WHERE correlation_id = ?`).get(correlationId);
    if (!row) {
      const orderRow = this.db.prepare(`
        SELECT intent_id FROM orders WHERE idempotency_key = ?
      `).get(correlationId);
      if (orderRow) {
        row = this.db.prepare(`SELECT * FROM signal_intents WHERE id = ?`).get(orderRow.intent_id);
      }
    }
    if (!row) return null;
    const order = this.db.prepare(`
      SELECT id, attempt_number, idempotency_key, status, broker, broker_order_id, error_code, created_at, updated_at
      FROM orders WHERE intent_id = ? ORDER BY id DESC LIMIT 1
    `).get(row.id);
    return {
      ...this.#mapIntent(row),
      order: order ? {
        id: Number(order.id),
        attemptNumber: Number(order.attempt_number),
        idempotencyKey: order.idempotency_key,
        status: order.status,
        broker: order.broker,
        brokerOrderId: order.broker_order_id,
        errorCode: order.error_code,
        createdAt: order.created_at,
        updatedAt: order.updated_at,
      } : null,
    };
  }

  getAlertTimeline(source, alertId) {
    const intent = this.db.prepare(`
      SELECT id, correlation_id FROM signal_intents WHERE source = ? AND alert_id = ?
    `).get(source, alertId);
    if (!intent) return [];
    return this.db.prepare(`
      SELECT id, event_type, details_json, occurred_at
      FROM signal_events WHERE intent_id = ? ORDER BY id
    `).all(intent.id).map((row) => ({
      id: Number(row.id),
      correlation_id: intent.correlation_id,
      type: row.event_type,
      details: parseJson(row.details_json),
      occurredAt: row.occurred_at,
    }));
  }

  listAlerts(limit = 100) {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
      throw new RangeError('limit must be an integer from 1 through 500');
    }
    return this.db.prepare(`
      SELECT i.*
      FROM signal_intents i
      ORDER BY i.id DESC
      LIMIT ?
    `).all(limit).map((row) => {
      const intent = this.#mapIntent(row);
      const order = this.db.prepare(`
        SELECT id, attempt_number, idempotency_key, status, broker, broker_order_id,
               error_code, created_at, updated_at
        FROM orders WHERE intent_id = ? ORDER BY id DESC LIMIT 1
      `).get(row.id);
      return {
        ...intent,
        order: order ? {
          id: Number(order.id),
          attemptNumber: Number(order.attempt_number),
          idempotencyKey: order.idempotency_key,
          status: order.status,
          broker: order.broker,
          brokerOrderId: order.broker_order_id,
          errorCode: order.error_code,
          createdAt: order.created_at,
          updatedAt: order.updated_at,
        } : null,
      };
    });
  }

  listSubmissionUnknownAlerts() {
    return this.db.prepare(`
      SELECT i.*
      FROM signal_intents i
      WHERE i.status = 'SUBMISSION_UNKNOWN'
      ORDER BY i.id DESC
    `).all().map((row) => {
      const intent = this.#mapIntent(row);
      const order = this.db.prepare(`
        SELECT id, attempt_number, idempotency_key, status, broker, broker_order_id,
               error_code, created_at, updated_at
        FROM orders WHERE intent_id = ? ORDER BY id DESC LIMIT 1
      `).get(row.id);
      return {
        ...intent,
        order: order ? {
          id: Number(order.id),
          attemptNumber: Number(order.attempt_number),
          idempotencyKey: order.idempotency_key,
          status: order.status,
          broker: order.broker,
          brokerOrderId: order.broker_order_id,
          errorCode: order.error_code,
          createdAt: order.created_at,
          updatedAt: order.updated_at,
        } : null,
      };
    });
  }

  recordSecurityEvent({ source, eventType = 'WEBHOOK_REJECTED', code, details = {} }) {
    const occurredAt = asIso(this.clock);
    this.db.prepare(`
      INSERT INTO security_events (source, event_type, code, details_json, occurred_at)
      VALUES (?, ?, ?, ?, ?)
    `).run(source, eventType, code, JSON.stringify(redact(details)), occurredAt);
  }

  listSecurityEvents(limit = 100) {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
      throw new RangeError('limit must be an integer from 1 through 500');
    }
    return this.db.prepare(`
      SELECT id, source, event_type, code, details_json, occurred_at
      FROM security_events ORDER BY id DESC LIMIT ?
    `).all(limit).map((row) => ({
      id: Number(row.id),
      source: row.source,
      type: row.event_type,
      code: row.code,
      details: parseJson(row.details_json),
      occurredAt: row.occurred_at,
    }));
  }
}
