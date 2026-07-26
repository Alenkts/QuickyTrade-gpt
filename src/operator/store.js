import { randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

export const MANAGEMENT_MODES = Object.freeze([
  'APP_MANAGED',
  'USER_MANAGED',
  'TRADINGVIEW_MANAGED',
]);

const PROFILE_SEEDS = Object.freeze([
  ['paper-tws', 'Paper - TWS', 'PAPER', 'TWS', 7497],
  ['paper-gateway', 'Paper - Gateway', 'PAPER', 'GATEWAY', 4002],
  ['live-tws', 'Live - TWS', 'LIVE', 'TWS', 7496],
  ['live-gateway', 'Live - Gateway', 'LIVE', 'GATEWAY', 4001],
]);

const PAPER_POLICY = Object.freeze({
  id: 'paper-balanced-v1',
  version: 1,
  label: 'Paper balanced 20/40/60',
  qualification: 'PAPER_EXPERIMENTAL',
  targets: [
    { id: 'TP1', profitBps: 2000, allocationBps: 5000 },
    { id: 'TP2', profitBps: 4000, allocationBps: 2500 },
    { id: 'TP3', profitBps: 6000, allocationBps: 2500 },
  ],
  stop: { lossBps: 2500, coverageBps: 10000 },
  transitions: [
    { after: 'TP1_FILLED', action: 'MOVE_STOP_TO_BREAKEVEN' },
    { after: 'TP2_FILLED', action: 'TRAIL_FRESH_BID', distanceBps: 1500 },
  ],
});

const SCHEMA = `
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE IF NOT EXISTS connection_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment IN ('PAPER','LIVE')),
  gateway_type TEXT NOT NULL CHECK(gateway_type IN ('TWS','GATEWAY')),
  host TEXT NOT NULL,
  port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
  client_id INTEGER NOT NULL CHECK(client_id BETWEEN 0 AND 2147483647),
  account_mask TEXT,
  selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1)),
  -- Operator-editable, no core restart required. NULL means "not configured":
  -- entries fall back to the pre-existing default-quantity-of-1 behavior. When
  -- set, it drives capital-based dynamic sizing in the core (contracts =
  -- floor(capital / option mid-price)), which the core additionally clamps to
  -- its own deployment-level max_contracts_per_order safety ceiling.
  capital_per_trade_dollars TEXT,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_selected_connection_profile
  ON connection_profiles(selected) WHERE selected = 1;

CREATE TABLE IF NOT EXISTS operator_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_trade_intents (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL CHECK(source = 'MANUAL_UI'),
  profile_id TEXT NOT NULL REFERENCES connection_profiles(id),
  payload_json TEXT NOT NULL,
  management_mode TEXT NOT NULL CHECK(management_mode IN ('APP_MANAGED','USER_MANAGED','TRADINGVIEW_MANAGED')),
  management_policy_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('PROPOSAL','BLOCKED','READY')),
  status_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
`;

function nowIso() {
  return new Date().toISOString();
}

function setting(value) {
  return JSON.stringify(value);
}

function isLoopbackHost(value) {
  return value === '127.0.0.1' || value === 'localhost' || value === '::1';
}

function assertMode(value) {
  if (!MANAGEMENT_MODES.includes(value)) throw new TypeError('Unsupported management mode');
}

// Last-resort sanity ceiling only -- mirrors the identical bound enforced
// independently by the core (quickytrade_core.engine.MAX_CAPITAL_PER_TRADE_DOLLARS).
// The real safety mechanism is the core's own deployment-level
// max_contracts_per_order ceiling; this just rejects an obviously
// fat-fingered value (an extra zero or three) before it is even sent.
const MAX_CAPITAL_PER_TRADE_DOLLARS = 1_000_000;

function normalizeCapitalPerTradeDollars(value) {
  if (value === null || value === undefined || value === '') return null;
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0 || numeric > MAX_CAPITAL_PER_TRADE_DOLLARS) {
    throw new TypeError(`capitalPerTradeDollars must be a positive number up to ${MAX_CAPITAL_PER_TRADE_DOLLARS}`);
  }
  return String(numeric);
}

const TRANSITION_ACTIONS = Object.freeze(['MOVE_STOP_TO_BREAKEVEN', 'TRAIL_FRESH_BID']);

function validatePolicy(policy) {
  if (!policy || !Array.isArray(policy.targets) || policy.targets.length === 0) {
    throw new TypeError('Management policy requires at least one target');
  }
  const allocation = policy.targets.reduce((sum, target) => sum + target.allocationBps, 0);
  if (allocation !== 10000) throw new TypeError('Management target allocations must total 10000 basis points');
  if (policy.stop?.coverageBps !== 10000) throw new TypeError('Management stop must cover 10000 basis points');
  if (policy.transitions !== undefined) validateTransitions(policy.transitions, policy.targets);
}

// Store-side transitions key `after` by the "<levelId>_FILLED" event
// convention (e.g. "TP1_FILLED"); this is stripped to the bare levelId at the
// wire boundary to the core (see server.mjs's coreManagementPolicy).
function validateTransitions(transitions, targets) {
  if (!Array.isArray(transitions)) throw new TypeError('Management transitions must be an array');
  const targetIds = new Set(targets.map((target) => target.id));
  for (const transition of transitions) {
    if (!transition || typeof transition !== 'object') {
      throw new TypeError('Each management transition must be an object');
    }
    const after = transition.after;
    if (
      typeof after !== 'string'
      || !after.endsWith('_FILLED')
      || !targetIds.has(after.slice(0, -'_FILLED'.length))
    ) {
      throw new TypeError('Management transition "after" must reference an existing target as "<levelId>_FILLED"');
    }
    if (!TRANSITION_ACTIONS.includes(transition.action)) {
      throw new TypeError('Management transition action must be MOVE_STOP_TO_BREAKEVEN or TRAIL_FRESH_BID');
    }
    if (transition.action === 'TRAIL_FRESH_BID') {
      if (!Number.isSafeInteger(transition.distanceBps) || transition.distanceBps <= 0) {
        throw new TypeError('TRAIL_FRESH_BID transitions require a positive integer distanceBps');
      }
    } else if (Object.hasOwn(transition, 'distanceBps')) {
      throw new TypeError('MOVE_STOP_TO_BREAKEVEN transitions must not include distanceBps');
    }
  }
}

export class OperatorStore {
  constructor(path = ':memory:') {
    this.db = new DatabaseSync(path);
    this.db.exec(SCHEMA);
    this.#migrateColumns();
    this.#seed();
  }

  close() {
    this.db.close();
  }

  // Additive-only migration for an already-populated pre-capital-per-trade
  // database (mirrors TradingViewStore's own #migrate* methods): the
  // `CREATE TABLE IF NOT EXISTS` above only benefits a brand-new file, so an
  // existing connection_profiles table predating this column is otherwise
  // left without it -- #seed()'s INSERT OR IGNORE below unconditionally
  // references capital_per_trade_dollars and would fail closed (a hard
  // startup crash, not a silent skip) against such a database.
  #migrateColumns() {
    const columns = this.db.prepare(`PRAGMA table_info(connection_profiles)`).all();
    if (!columns.some((column) => column.name === 'capital_per_trade_dollars')) {
      this.db.exec(`ALTER TABLE connection_profiles ADD COLUMN capital_per_trade_dollars TEXT`);
    }
  }

  #seed() {
    const timestamp = nowIso();
    const insert = this.db.prepare(`
      INSERT OR IGNORE INTO connection_profiles
        (id,name,environment,gateway_type,host,port,client_id,selected,capital_per_trade_dollars,updated_at)
      VALUES (?,?,?,?,?,?,?,0,NULL,?)
    `);
    for (const [id, name, environment, type, port] of PROFILE_SEEDS) {
      insert.run(id, name, environment, type, '127.0.0.1', port, 17, timestamp);
    }
    const selected = this.db.prepare('SELECT id FROM connection_profiles WHERE selected=1').get();
    if (!selected) this.db.prepare("UPDATE connection_profiles SET selected=1 WHERE id='paper-tws'").run();
    this.#insertSetting('management_defaults', {
      manualMode: 'APP_MANAGED',
      tradingviewMode: 'APP_MANAGED',
      manualManagementProfileId: PAPER_POLICY.id,
      tradingviewManagementProfileId: PAPER_POLICY.id,
      manualAutosend: false,
    });
    this.#insertSetting('tradingview_ownership', {
      mode: 'APP_MANAGED',
      managementProfileId: PAPER_POLICY.id,
    });
    this.#insertSetting('management_profiles', [PAPER_POLICY]);
  }

  #insertSetting(key, value) {
    this.db.prepare(`
      INSERT OR IGNORE INTO operator_settings (key,value_json,updated_at) VALUES (?,?,?)
    `).run(key, setting(value), nowIso());
  }

  getSetting(key) {
    const row = this.db.prepare('SELECT value_json FROM operator_settings WHERE key=?').get(key);
    return row ? JSON.parse(row.value_json) : null;
  }

  setSetting(key, value) {
    this.db.prepare(`
      INSERT INTO operator_settings (key,value_json,updated_at) VALUES (?,?,?)
      ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at
    `).run(key, setting(value), nowIso());
    return value;
  }

  listProfiles() {
    return this.db.prepare('SELECT * FROM connection_profiles ORDER BY environment DESC,gateway_type DESC').all()
      .map((row) => this.#profile(row));
  }

  getProfile(id) {
    const row = this.db.prepare('SELECT * FROM connection_profiles WHERE id=?').get(id);
    return row ? this.#profile(row) : null;
  }

  // The operator's currently selected connection profile, or null. The schema
  // guarantees at most one (`one_selected_connection_profile`), and #ensureSeed
  // selects `paper-tws` when a fresh database has none -- but this must still
  // tolerate null rather than assume, because callers use it to derive
  // capital-per-trade for a real order and a wrong default is worse than none.
  getSelectedProfile() {
    const row = this.db.prepare('SELECT * FROM connection_profiles WHERE selected=1').get();
    return row ? this.#profile(row) : null;
  }

  #profile(row) {
    return {
      id: row.id,
      name: row.name,
      environment: row.environment,
      gatewayType: row.gateway_type,
      host: row.host,
      port: Number(row.port),
      clientId: Number(row.client_id),
      accountMask: row.account_mask,
      accountStatus: row.account_mask ? 'CONFIGURED_UNVERIFIED' : 'NOT_CONFIGURED',
      selected: row.selected === 1,
      // A canonical decimal string, or null when the operator has not
      // configured capital-based sizing for this profile (falls back to the
      // pre-existing default-quantity-of-1 behavior in the core).
      capitalPerTradeDollars: row.capital_per_trade_dollars ?? null,
      liveUnlocked: false,
      ready: false,
      readinessReasons: ['PROFILE_NOT_CONNECTED', 'BROKER_RECONCILIATION_REQUIRED'],
      updatedAt: row.updated_at,
    };
  }

  updateProfile(id, options) {
    const { host, port, clientId, selected = false } = options;
    const existing = this.getProfile(id);
    if (!existing) return null;
    if (!isLoopbackHost(host)) throw new TypeError('IBKR host must be loopback');
    if (!Number.isSafeInteger(port) || port < 1 || port > 65535) throw new TypeError('Invalid IBKR port');
    if (!Number.isSafeInteger(clientId) || clientId < 0 || clientId > 2147483647) throw new TypeError('Invalid IBKR client ID');
    // capitalPerTradeDollars is only touched when the caller explicitly
    // supplies the key (even as null, to intentionally clear it) -- an
    // unrelated profile update (e.g. just switching `selected`) must never
    // silently reset a previously configured capital-per-trade value.
    const capitalPerTradeDollars = Object.hasOwn(options, 'capitalPerTradeDollars')
      ? normalizeCapitalPerTradeDollars(options.capitalPerTradeDollars)
      : existing.capitalPerTradeDollars;
    this.db.exec('BEGIN IMMEDIATE');
    try {
      if (selected) this.db.prepare('UPDATE connection_profiles SET selected=0 WHERE selected=1').run();
      this.db.prepare(`
        UPDATE connection_profiles SET host=?,port=?,client_id=?,selected=?,capital_per_trade_dollars=?,updated_at=? WHERE id=?
      `).run(host, port, clientId, selected ? 1 : existing.selected ? 1 : 0, capitalPerTradeDollars, nowIso(), id);
      this.db.exec('COMMIT');
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
    return this.getProfile(id);
  }

  managementDefaults() {
    const defaults = this.getSetting('management_defaults');
    return { ...defaults, profiles: this.getSetting('management_profiles') };
  }

  managementPolicy(id) {
    const policy = this.getSetting('management_profiles').find((candidate) => candidate.id === id);
    return policy ? JSON.parse(JSON.stringify(policy)) : null;
  }

  updateManagementDefaults(value) {
    assertMode(value.manualMode);
    assertMode(value.tradingviewMode);
    if (typeof value.manualAutosend !== 'boolean') throw new TypeError('manualAutosend must be boolean');
    const profiles = this.getSetting('management_profiles');
    for (const policy of profiles) validatePolicy(policy);
    const ids = new Set(profiles.map((policy) => policy.id));
    if (!ids.has(value.manualManagementProfileId) || !ids.has(value.tradingviewManagementProfileId)) {
      throw new TypeError('Unknown management profile');
    }
    this.setSetting('management_defaults', {
      manualMode: value.manualMode,
      tradingviewMode: value.tradingviewMode,
      manualManagementProfileId: value.manualManagementProfileId,
      tradingviewManagementProfileId: value.tradingviewManagementProfileId,
      manualAutosend: value.manualAutosend,
    });
    this.setSetting('tradingview_ownership', {
      mode: value.tradingviewMode,
      managementProfileId: value.tradingviewManagementProfileId,
    });
    return this.managementDefaults();
  }

  tradingViewOwnership() {
    return this.getSetting('tradingview_ownership');
  }

  updateTradingViewOwnership(value) {
    assertMode(value.mode);
    const policies = new Set(this.getSetting('management_profiles').map((policy) => policy.id));
    if (value.managementProfileId && !policies.has(value.managementProfileId)) {
      throw new TypeError('Unknown management profile');
    }
    const ownership = this.setSetting('tradingview_ownership', {
      mode: value.mode,
      managementProfileId: value.managementProfileId || null,
    });
    const defaults = this.getSetting('management_defaults');
    this.setSetting('management_defaults', {
      ...defaults,
      tradingviewMode: ownership.mode,
      tradingviewManagementProfileId: ownership.managementProfileId,
    });
    return ownership;
  }

  createManualIntent(payload) {
    const profile = this.getProfile(payload.profileId);
    if (!profile) throw new TypeError('Unknown connection profile');
    assertMode(payload.managementMode);
    if (payload.managementMode === 'TRADINGVIEW_MANAGED') {
      throw new TypeError('Manual entries cannot use TradingView-managed exits');
    }
    if (!/^[A-Z]{1,8}$/.test(payload.symbol || '')) throw new TypeError('Invalid symbol');
    if (!['C', 'P'].includes(payload.right)) throw new TypeError('right must be C or P');
    if (!['EXACT', 'TARGET_RANGE'].includes(payload.strikeSelection)) {
      throw new TypeError('strikeSelection must be EXACT or TARGET_RANGE');
    }
    if (payload.strikeSelection === 'EXACT') {
      if (!/^\d{8}$/.test(payload.expiry || '')) throw new TypeError('expiry must be YYYYMMDD');
      if (typeof payload.strike !== 'number' || !Number.isFinite(payload.strike) || payload.strike <= 0) throw new TypeError('Invalid strike');
    } else {
      if (Object.hasOwn(payload, 'expiry')) throw new TypeError('expiry must not be present when strikeSelection is TARGET_RANGE');
      if (Object.hasOwn(payload, 'strike')) throw new TypeError('strike must not be present when strikeSelection is TARGET_RANGE');
    }
    // quantity is now optional: the core computes the actual contract count
    // from the connection profile's capitalPerTradeDollars and the option's
    // fresh mid-price at preview/submit time (Manual Trade Desk no longer
    // collects it as operator input). A caller-supplied value is still
    // validated as a sane positive integer for backward compatibility.
    if (payload.quantity !== undefined && (!Number.isSafeInteger(payload.quantity) || payload.quantity < 1)) {
      throw new TypeError('Invalid quantity');
    }
    if (payload.entryPolicy !== 'MARKETABLE_LIMIT') throw new TypeError('Only fresh-quote marketable limits are currently supported');
    const policyIds = new Set(this.getSetting('management_profiles').map((policy) => policy.id));
    if (payload.managementMode === 'APP_MANAGED' && !policyIds.has(payload.managementProfileId)) {
      throw new TypeError('App-managed trades require a known management profile');
    }
    const timestamp = nowIso();
    const id = randomUUID();
    const managementPolicy = payload.managementMode === 'APP_MANAGED'
      ? this.managementPolicy(payload.managementProfileId)
      : null;
    const snapshot = Object.freeze({ ...payload, managementPolicy, source: 'MANUAL_UI' });
    const inserted = this.db.prepare(`
      INSERT INTO manual_trade_intents
        (id,source,profile_id,payload_json,management_mode,management_policy_id,status,status_reason,created_at,updated_at)
      VALUES (?,'MANUAL_UI',?,?,?,?, 'PROPOSAL',?,?,?)
    `).run(id, profile.id, JSON.stringify(snapshot), payload.managementMode,
      payload.managementProfileId || null, null, timestamp, timestamp);
    return {
      id,
      intentId: Number(inserted.lastInsertRowid),
      correlationId: id,
      source: 'MANUAL_UI',
      profileId: profile.id,
      payload: snapshot,
      managementMode: payload.managementMode,
      managementProfileId: payload.managementProfileId || null,
      managementPolicy,
      status: 'PROPOSAL',
      createdAt: timestamp,
    };
  }

  getManualIntent(id) {
    const row = this.db.prepare(`SELECT rowid AS row_id, * FROM manual_trade_intents WHERE id = ?`).get(id);
    if (!row) return null;
    const payload = JSON.parse(row.payload_json);
    return {
      id: row.id,
      intentId: Number(row.row_id),
      correlationId: row.id,
      source: 'MANUAL_UI',
      profileId: row.profile_id,
      payload,
      managementMode: row.management_mode,
      managementProfileId: row.management_policy_id,
      managementPolicy: payload.managementPolicy,
      status: row.status,
      createdAt: row.created_at,
    };
  }
}
