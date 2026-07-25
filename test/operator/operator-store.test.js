import assert from 'node:assert/strict';
import test from 'node:test';

import { OperatorStore } from '../../src/operator/store.js';

test('seeds distinct Paper and Live profiles without treating ports as readiness', () => {
  const store = new OperatorStore(':memory:');
  try {
    const profiles = store.listProfiles();
    assert.equal(profiles.length, 4);
    assert.equal(profiles.find((profile) => profile.id === 'paper-tws').port, 7497);
    assert.equal(profiles.find((profile) => profile.id === 'live-gateway').port, 4001);
    assert.equal(profiles.every((profile) => profile.ready === false), true);
    assert.equal(profiles.every((profile) => profile.liveUnlocked === false), true);
  } finally {
    store.close();
  }
});

test('persists separate source defaults and validates the management policy', () => {
  const store = new OperatorStore(':memory:');
  try {
    const defaults = store.managementDefaults();
    assert.equal(defaults.manualMode, 'APP_MANAGED');
    assert.equal(defaults.tradingviewMode, 'APP_MANAGED');
    assert.equal(defaults.profiles[0].targets.reduce((sum, target) => sum + target.allocationBps, 0), 10000);

    store.updateManagementDefaults({
      manualMode: 'USER_MANAGED',
      tradingviewMode: 'TRADINGVIEW_MANAGED',
      manualManagementProfileId: 'paper-balanced-v1',
      tradingviewManagementProfileId: 'paper-balanced-v1',
      manualAutosend: false,
    });
    assert.equal(store.managementDefaults().manualMode, 'USER_MANAGED');
    assert.equal(store.managementDefaults().tradingviewMode, 'TRADINGVIEW_MANAGED');
  } finally {
    store.close();
  }
});

test('manual intent snapshots ownership as a durable proposal, retrievable by id', () => {
  const store = new OperatorStore(':memory:');
  try {
    const intent = store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'EXACT',
      expiry: '20260718',
      strike: 600,
      right: 'C',
      quantity: 1,
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    });
    assert.equal(intent.source, 'MANUAL_UI');
    assert.equal(intent.managementMode, 'APP_MANAGED');
    assert.equal(intent.managementPolicy.version, 1);
    assert.equal(intent.managementPolicy.targets.reduce((sum, target) => sum + target.allocationBps, 0), 10000);
    assert.equal(intent.status, 'PROPOSAL');

    const fetched = store.getManualIntent(intent.id);
    assert.equal(fetched.id, intent.id);
    assert.equal(fetched.managementMode, 'APP_MANAGED');
    assert.equal(fetched.managementPolicy.version, 1);
    assert.equal(fetched.payload.symbol, 'QQQ');
  } finally {
    store.close();
  }
});

test('manual intents cannot delegate exits to TradingView', () => {
  const store = new OperatorStore(':memory:');
  try {
    assert.throws(() => store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'EXACT',
      expiry: '20260718',
      strike: 600,
      right: 'C',
      quantity: 1,
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'TRADINGVIEW_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    }), /Manual entries cannot use TradingView-managed exits/);
  } finally {
    store.close();
  }
});

function basePolicy(overrides = {}) {
  return {
    id: 'custom-policy-v1',
    version: 1,
    label: 'Custom',
    qualification: 'PAPER_EXPERIMENTAL',
    targets: [
      { id: 'TP1', profitBps: 2000, allocationBps: 5000 },
      { id: 'TP2', profitBps: 4000, allocationBps: 5000 },
    ],
    stop: { lossBps: 2500, coverageBps: 10000 },
    ...overrides,
  };
}

test('validates management transitions on save: accepts well-formed transitions and rejects malformed ones', () => {
  const store = new OperatorStore(':memory:');
  try {
    const wellFormed = basePolicy({
      transitions: [
        { after: 'TP1_FILLED', action: 'MOVE_STOP_TO_BREAKEVEN' },
        { after: 'TP2_FILLED', action: 'TRAIL_FRESH_BID', distanceBps: 1500 },
      ],
    });
    store.setSetting('management_profiles', [wellFormed]);
    assert.doesNotThrow(() => store.updateManagementDefaults({
      manualMode: 'APP_MANAGED',
      tradingviewMode: 'APP_MANAGED',
      manualManagementProfileId: wellFormed.id,
      tradingviewManagementProfileId: wellFormed.id,
      manualAutosend: false,
    }));

    const badReference = basePolicy({
      transitions: [{ after: 'TP9_FILLED', action: 'MOVE_STOP_TO_BREAKEVEN' }],
    });
    store.setSetting('management_profiles', [badReference]);
    assert.throws(() => store.updateManagementDefaults({
      manualMode: 'APP_MANAGED',
      tradingviewMode: 'APP_MANAGED',
      manualManagementProfileId: badReference.id,
      tradingviewManagementProfileId: badReference.id,
      manualAutosend: false,
    }), /must reference an existing target/);

    const missingDistance = basePolicy({
      transitions: [{ after: 'TP1_FILLED', action: 'TRAIL_FRESH_BID' }],
    });
    store.setSetting('management_profiles', [missingDistance]);
    assert.throws(() => store.updateManagementDefaults({
      manualMode: 'APP_MANAGED',
      tradingviewMode: 'APP_MANAGED',
      manualManagementProfileId: missingDistance.id,
      tradingviewManagementProfileId: missingDistance.id,
      manualAutosend: false,
    }), /require a positive integer distanceBps/);
  } finally {
    store.close();
  }
});

test('manual intent supports TARGET_RANGE strike selection without an expiry/strike, and rejects mixing the two', () => {
  const store = new OperatorStore(':memory:');
  try {
    const intent = store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'TARGET_RANGE',
      right: 'C',
      quantity: 1,
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    });
    assert.equal(intent.payload.strikeSelection, 'TARGET_RANGE');
    assert.equal(Object.hasOwn(intent.payload, 'expiry'), false);
    assert.equal(Object.hasOwn(intent.payload, 'strike'), false);

    assert.throws(() => store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'TARGET_RANGE',
      expiry: '20260718',
      right: 'C',
      quantity: 1,
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    }), /expiry must not be present/);

    assert.throws(() => store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'TARGET_RANGE',
      strike: 600,
      right: 'C',
      quantity: 1,
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    }), /strike must not be present/);

    assert.throws(() => store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'BOGUS',
      right: 'C',
      quantity: 1,
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    }), /strikeSelection must be EXACT or TARGET_RANGE/);
  } finally {
    store.close();
  }
});

test('seeded connection profiles have no capital-per-trade configured by default', () => {
  const store = new OperatorStore(':memory:');
  try {
    const profiles = store.listProfiles();
    assert.equal(profiles.every((profile) => profile.capitalPerTradeDollars === null), true);
  } finally {
    store.close();
  }
});

test('updateProfile persists a valid capital-per-trade amount as a canonical decimal string', () => {
  const store = new OperatorStore(':memory:');
  try {
    const updated = store.updateProfile('paper-tws', {
      host: '127.0.0.1', port: 7497, clientId: 17, selected: true, capitalPerTradeDollars: 500,
    });
    assert.equal(updated.capitalPerTradeDollars, '500');
    assert.equal(store.getProfile('paper-tws').capitalPerTradeDollars, '500');
  } finally {
    store.close();
  }
});

test('updateProfile rejects a negative, zero, non-numeric, or absurdly large capital-per-trade amount', () => {
  const store = new OperatorStore(':memory:');
  try {
    for (const bad of [-100, 0, 'not-a-number', 100_000_000, NaN, Infinity]) {
      assert.throws(() => store.updateProfile('paper-tws', {
        host: '127.0.0.1', port: 7497, clientId: 17, selected: false, capitalPerTradeDollars: bad,
      }), /capitalPerTradeDollars must be a positive number/, `expected ${bad} to be rejected`);
    }
    // None of the rejected attempts partially applied.
    assert.equal(store.getProfile('paper-tws').capitalPerTradeDollars, null);
  } finally {
    store.close();
  }
});

test('updateProfile without capitalPerTradeDollars leaves a previously configured value untouched', () => {
  const store = new OperatorStore(':memory:');
  try {
    store.updateProfile('paper-tws', { host: '127.0.0.1', port: 7497, clientId: 17, selected: true, capitalPerTradeDollars: 750 });
    // Switching the selected profile (the app's activateProfile flow) omits
    // the key entirely -- must not reset the value set above.
    const reselected = store.updateProfile('paper-tws', { host: '127.0.0.1', port: 7497, clientId: 17, selected: true });
    assert.equal(reselected.capitalPerTradeDollars, '750');
  } finally {
    store.close();
  }
});

test('updateProfile with capitalPerTradeDollars set to null explicitly clears a previously configured value', () => {
  const store = new OperatorStore(':memory:');
  try {
    store.updateProfile('paper-tws', { host: '127.0.0.1', port: 7497, clientId: 17, selected: true, capitalPerTradeDollars: 750 });
    const cleared = store.updateProfile('paper-tws', { host: '127.0.0.1', port: 7497, clientId: 17, selected: true, capitalPerTradeDollars: null });
    assert.equal(cleared.capitalPerTradeDollars, null);
  } finally {
    store.close();
  }
});

test('manual intent quantity is optional now that the core computes it from capital and live price', () => {
  const store = new OperatorStore(':memory:');
  try {
    const intent = store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'EXACT',
      expiry: '20260718',
      strike: 600,
      right: 'C',
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    });
    assert.equal(Object.hasOwn(intent.payload, 'quantity'), false);

    assert.throws(() => store.createManualIntent({
      profileId: 'paper-tws',
      symbol: 'QQQ',
      strikeSelection: 'EXACT',
      expiry: '20260718',
      strike: 600,
      right: 'C',
      quantity: 0,
      entryPolicy: 'MARKETABLE_LIMIT',
      managementMode: 'APP_MANAGED',
      managementProfileId: 'paper-balanced-v1',
    }), /Invalid quantity/);
  } finally {
    store.close();
  }
});
