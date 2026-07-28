const state = {
  runtime: null,
  alerts: [],
  // Unbounded durable Node-ledger risks (currently SUBMISSION_UNKNOWN) are
  // loaded separately so the 500-row Activity window cannot hide them.
  attentionAlerts: [],
  nodeAttentionStatus: null,
  selectedCorrelation: null,
  toastTimer: null,
  profiles: [],
  activeProfileId: null,
  managementDefaults: null,
  tradingviewOwnership: null,
  activeTrades: [],
  // Defensive quarantine for an older/stale backend that returns CLOSED or
  // zero-quantity rows from /api/trades/active. These never receive close
  // controls. Any still-working exit orders remain visible in Attention.
  excludedNonOpenTrades: [],
  closedTrades: [],
  closedPositionsStatus: null,
  closedPositionsReason: null,
  closedPositionsCheckedAt: null,
  closedMarketDate: null,
  manualDeskExpanded: false,
  operatorApisAvailable: false,
  lastManualIntentId: null,
  // Active Positions panel: distinguishes "positions unavailable/unknown"
  // (positionsStatus !== 'OK') from "confirmed zero open positions"
  // (positionsStatus === 'OK' && activeTrades.length === 0) everywhere below
  // -- never render a failed/pending poll as an empty-looking table.
  positionsStatus: null,
  positionsReason: null,
  positionsCheckedAt: null,
  selectedTradeCorrelationId: null,
  // Per (correlationId, mode) client-generated close requestId, held until a
  // definitive response arrives so a retry of the identical action (a second
  // click, or this app's own fetch failing before any response) reuses the
  // same id -- the Node endpoint requires this for its durable dedupe, the
  // same "persist a unique intent before any broker side effect" contract
  // used everywhere else in this app.
  pendingCloseRequestIds: new Map(),
  pendingFlatten: null,
  // GET /api/reconciliation proxy result -- the "why is this blocked" panel
  // (see renderReconciliation) reads this to surface an unresolved
  // submission/protection/management-transition outcome, exactly the class
  // of ambiguity that globally blocks new opens in ExecutionEngine.
  // _verify_readiness(). null (not `{}`) while unavailable/not yet fetched --
  // never rendered as "nothing is unresolved".
  reconciliation: null,
  // UI-only acknowledgement of definitive terminal notices. Keys include the
  // status and updated timestamp so a later state/version automatically
  // resurfaces. This never mutates the durable alert, timeline, or broker.
  attentionAcknowledgements: new Set(),
  // The Active Positions table re-renders on a 3s poll (renderActiveTrades
  // rebuilds every <tr> from scratch every cycle). A brand-new qty <input>
  // element on every rebuild silently discards whatever the operator is
  // mid-typing into PARTIAL CLOSE's quantity field -- keyed here by
  // correlationId so buildActiveTradeRow can reuse the *same* input element
  // across renders (moving an element to a new parent via appendChild
  // preserves its value and focus; recreating it does not). Only the input
  // itself needs this -- the PARTIAL CLOSE/FLATTEN buttons are safely
  // recreated every render since their click handlers just need to close
  // over the current trade, not preserve any operator-typed state.
  qtyInputsByTrade: new Map(),
};
const $ = (selector) => document.querySelector(selector);

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.message || data.error || `HTTP ${response.status}`);
    error.code = data.code;
    throw error;
  }
  return data;
}

function notify(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove('show'), 3500);
}

// Failures of broker or operator actions (flatten, partial close, manual
// submit, profile save, core start/stop) are recorded here rather than only
// flashed as a toast. A toast auto-dismisses in 3.5 seconds and keeps no
// history, which is not an acceptable way to report that a FLATTEN did not go
// through. The toast still fires for immediacy; this is the durable record.
const operatorErrors = [];

function notifyFailure(action, message, context = {}) {
  operatorErrors.unshift({
    action,
    message: String(message || 'Unknown failure'),
    context,
    at: new Date().toISOString(),
  });
  if (operatorErrors.length > 50) operatorErrors.length = 50;
  renderOperatorErrors();
  notify(message);
}

function renderOperatorErrors() {
  const panel = $('#operatorErrorsPanel');
  const list = $('#operatorErrors');
  panel.hidden = operatorErrors.length === 0;
  clear(list);
  for (const failure of operatorErrors) {
    const item = document.createElement('li');
    const title = document.createElement('strong');
    title.textContent = failure.action;
    const body = document.createElement('span');
    body.textContent = failure.message;
    const meta = document.createElement('small');
    const parts = [formatMarketTime(failure.at)];
    if (failure.context.correlationId) parts.push(failure.context.correlationId);
    if (failure.context.code) parts.push(failure.context.code);
    meta.textContent = parts.filter(Boolean).join(' · ');
    item.append(title, body, meta);
    list.append(item);
  }
}

function display(value) {
  return value === null || value === undefined || value === '' ? 'Unavailable' : String(value);
}

function short(value, length = 12) {
  const text = display(value);
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

// Deliberately removed: an unlabelled browser-local formatter. It rendered the
// same timestamp in a different zone from the adjacent NY-labelled column with
// nothing to say so, which is a misreading hazard for an operator outside ET.
// Every timestamp in this app now goes through formatMarketTime.

function formatMarketTime(value) {
  if (!value) return 'Unavailable';
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString(undefined, { timeZone: 'America/New_York', timeZoneName: 'short' })
    : 'Unavailable';
}

function marketDate(value) {
  if (!value) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  const year = get('year'); const month = get('month'); const day = get('day');
  return year && month && day ? `${year}-${month}-${day}` : null;
}

const ATTENTION_ACK_STORAGE_KEY = 'quickytrade.attentionAcknowledgements.v1';
const CLEARABLE_ATTENTION_STATES = new Set(['BLOCKED', 'FAILED']);
// Reason codes that must be surfaced as an ACTIVE RISK even though they arrive
// on a BLOCKED alert. An expired signal means a real entry was dropped: the
// operator needs to see it against the chart, not dismiss it as history.
const NON_CLEARABLE_REASON_CODES = new Set(['SIGNAL_EXPIRED', 'EXCEEDED_MAX_AGE_IN_QUEUE']);

// A notice may only be acknowledged away when it is a definitive, already-
// handled refusal. An EXPIRED intent, or a BLOCKED one whose reason is an
// expiry, means a real entry was silently dropped -- it stays an ACTIVE RISK.
function isClearableAttention(alert) {
  if (!CLEARABLE_ATTENTION_STATES.has(alert.status)) return false;
  return !NON_CLEARABLE_REASON_CODES.has(alert.order?.errorCode);
}

function attentionAcknowledgementKey(alert) {
  return `${alert.correlationId}:${alert.status}:${alert.updatedAt || alert.createdAt || 'unknown'}`;
}

function loadAttentionAcknowledgements() {
  try {
    const stored = JSON.parse(localStorage.getItem(ATTENTION_ACK_STORAGE_KEY) || '[]');
    state.attentionAcknowledgements = new Set(Array.isArray(stored) ? stored.filter((item) => typeof item === 'string') : []);
  } catch {
    state.attentionAcknowledgements = new Set();
  }
}

function saveAttentionAcknowledgements() {
  try {
    localStorage.setItem(ATTENTION_ACK_STORAGE_KEY, JSON.stringify([...state.attentionAcknowledgements]));
    return true;
  } catch {
    return false;
  }
}

function ensureTodayAlertDate() {
  const control = $('#alertDateFilter');
  if (control) control.value = marketDate(new Date());
}

function age(value) {
  if (!value) return 'Unavailable';
  const elapsed = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(elapsed)) return 'Unavailable';
  if (elapsed < 1_000) return 'now';
  if (elapsed < 60_000) return `${Math.floor(elapsed / 1_000)}s ago`;
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m ago`;
  return `${Math.floor(elapsed / 3_600_000)}h ago`;
}

function statusLabel(status) {
  return status === 'READY' ? 'RECEIVED' : display(status);
}

function statusClass(status) {
  if (['FILLED', 'PROTECTED'].includes(status)) return 'ok';
  if (['SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'PROCESSING'].includes(status)) return 'working';
  if (status === 'BLOCKED') return 'blocked';
  if (status === 'INERT') return 'blocked';
  if (['FAILED', 'EXPIRED', 'UNPROTECTED', 'SUBMISSION_UNKNOWN'].includes(status)) return 'critical';
  return 'queued';
}

function attentionReason(alert) {
  const symbol = alert.payload?.ticker || 'This';
  const code = alert.order?.errorCode;
  if (alert.status === 'SUBMISSION_UNKNOWN') {
    return 'Broker submission outcome is unknown. Do not resend. Verify the order reference in TWS, then reconcile.';
  }
  if (code === 'LOCAL_EXPOSURE_UNRESOLVED') {
    return `${symbol} entry was blocked because earlier local exposure was unresolved. No new order was sent. Inspect this lifecycle before resending.`;
  }
  if (code === 'SAME_DAY_ENTRY_CUTOFF') {
    return 'Entry arrived after the same-day cutoff. No order was sent and no action is required.';
  }
  if (alert.status === 'EXPIRED' || NON_CLEARABLE_REASON_CODES.has(code)) {
    return `${symbol} signal expired before it could be submitted, so no order was sent and this entry was silently skipped. Repeated expiries mean alerts are arriving faster than the core can accept them -- check ingress latency and core readiness against the chart.`;
  }
  if (alert.status === 'BLOCKED') {
    return `A safety check blocked this signal${code ? ` (${code})` : ''}. No order was sent.`;
  }
  if (alert.status === 'FAILED') {
    return `Processing ended before a confirmed broker submission${code ? ` (${code})` : ''}. Inspect the lifecycle for details.`;
  }
  if (alert.status === 'REJECTED') {
    return `The broker or execution core rejected this request${code ? ` (${code})` : ''}. Inspect the lifecycle before taking action.`;
  }
  if (alert.status === 'STALE') {
    return 'The signal is stale. Confirm that no broker order exists before taking any manual action.';
  }
  return `${code || 'Safety state requires operator review'}. Inspect the lifecycle before taking action.`;
}

function appendAttentionHeading(list, label, count, className) {
  const heading = element('div', undefined, `attention-section-heading ${className}`);
  heading.append(element('strong', label), element('span', String(count)));
  list.append(heading);
}

function setText(selector, value) {
  $(selector).textContent = display(value);
}

function setValue(selector, value) {
  const control = $(selector);
  control.value = value === null || value === undefined ? '' : String(value);
}

// The dashboard re-renders on a 3s poll. Writing to an input the operator is
// currently editing silently reverts their typing -- they then press SAVE
// believing they changed a host/port/capital value and write back the old one.
// A control is left alone while it has focus or has been edited since the last
// successful save.
function setValuePreservingEdits(selector, value) {
  const control = $(selector);
  if (control === document.activeElement || control.dataset.dirty === '1') return;
  control.value = value === null || value === undefined ? '' : String(value);
}

function markDirtyOnInput(selector) {
  const control = $(selector);
  control.addEventListener('input', () => { control.dataset.dirty = '1'; });
}

function clearDirty(selectors) {
  for (const selector of selectors) delete $(selector).dataset.dirty;
}

function clear(element) {
  element.replaceChildren();
}

function capability(action) {
  const capabilities = state.runtime?.capabilities;
  if (!capabilities) return false;
  if (capabilities[action] === true) return true;
  const aliases = {
    connection_profiles_read: 'manualProposal',
    connection_profiles_write: 'manualProposal',
    management_defaults_write: 'manualProposal',
    manual_trade_intents_write: 'manualProposal',
  };
  if (aliases[action] && capabilities[aliases[action]] === true) return true;
  return capabilities.actions?.includes?.(action) === true;
}

function selectedProfile() {
  return state.profiles.find((profile) => profile.id === state.activeProfileId) || null;
}

function setControlDisabled(selector, disabled) {
  const control = $(selector);
  if (control) control.disabled = disabled;
}

function profileIsUsable(profile) {
  if (!profile || profile.ready !== true) return false;
  if (profile.environment === 'LIVE') return profile.liveUnlocked === true;
  return true;
}

function profileIsReady(profile) {
  return profileIsUsable(profile);
}

function profileCanCreateProposal(profile) {
  return profileIsUsable(profile);
}

function profileIsEditable(profile) {
  return Boolean(profile) && capability('connection_profiles_write');
}

function appendOption(select, value, label, selected = false) {
  const option = element('option', label);
  option.value = value;
  option.selected = selected;
  select.append(option);
}

function renderProfiles() {
  const select = $('#profileSelect');
  const profile = selectedProfile();
  clear(select);
  if (!state.profiles.length) {
    appendOption(select, '', 'Profiles unavailable');
    select.disabled = true;
  } else {
    for (const item of state.profiles) {
      appendOption(select, item.id, `${item.name || item.id} · ${item.environment || 'Unavailable'}`, item.id === state.activeProfileId);
    }
    select.disabled = !capability('connection_profiles_read');
  }

  const environment = profile?.environment || 'Unavailable';
  const isLive = environment === 'LIVE';
  const isReady = Boolean(profile?.ready);
  const isUsable = profileIsUsable(profile);
  const profilePill = $('#profilePill');
  profilePill.textContent = isLive
    ? (isUsable ? 'LIVE SESSION VERIFIED' : 'LIVE LOCKED')
    : (isReady ? 'PAPER READY' : 'NOT READY');
  profilePill.className = `status-pill ${isUsable ? 'ok' : 'blocked'}`;
  setText('#profileEnvironment', environment);
  setText('#profileLockStatus', isLive
    ? (isUsable ? 'LIVE VERIFIED' : 'LIVE LOCKED')
    : (isReady ? 'PAPER VERIFIED' : 'NOT VERIFIED'));
  $('#profileEnvironment').className = isLive ? 'live-environment' : 'paper-environment';
  setValuePreservingEdits('#profileHost', profile?.host);
  setValuePreservingEdits('#profilePort', profile?.port);
  setValuePreservingEdits('#profileClientId', profile?.clientId);
  setValue('#profileAccount', profile?.accountMask || profile?.account || 'Unavailable');
  setValuePreservingEdits('#profileCapitalPerTrade', profile?.capitalPerTradeDollars ?? '');
  const editable = profileIsEditable(profile);
  for (const selector of ['#profileHost', '#profilePort', '#profileClientId', '#profileCapitalPerTrade']) setControlDisabled(selector, !editable);
  setControlDisabled('#saveProfile', !editable);
  setText('#profileNote', isLive
    ? (isUsable
      ? 'Live profile is verified for its exact account. Saving settings requires a fresh readiness check before any entry. Live trading places real capital at risk.'
      : 'Live profile is locked until the connected core reports a verified LIVE-ready session for this exact account. A port cannot unlock live trading.')
    : isReady
      ? 'Paper profile is verified for its exact account. Saving settings requires a fresh readiness check before any entry.'
      : (profile?.readinessReasons?.join?.(' · ') || 'Select a verified profile before creating a trade intent.'));
  renderManualDesk();
}

function renderManualDesk() {
  const profile = selectedProfile();
  const ready = profileCanCreateProposal(profile);
  const manualApi = capability('manual_trade_intents_write');
  const enabled = ready && manualApi;
  const isLive = profile?.environment === 'LIVE';
  const status = $('#manualDeskStatus');
  status.textContent = enabled ? (isLive ? 'LIVE PROPOSAL' : 'PAPER PROPOSAL') : isLive ? 'LIVE LOCKED' : 'NOT READY';
  status.className = `status-pill ${enabled ? 'ok' : 'blocked'}`;
  const controls = [
    '#manualSymbol', '#manualStrikeSelection', '#manualRight',
    '#manualManagementProfile', '#reviewManualTrade',
  ];
  for (const selector of controls) setControlDisabled(selector, !enabled);
  document.querySelectorAll('input[name="managementMode"]').forEach((input) => { input.disabled = !enabled; });
  setText('#manualDeskNote', enabled
    ? `Review creates one durable ${isLive ? 'Live' : 'Paper'} proposal and resolves its contract. Submit (or Autosend) sends it to IBKR; ongoing fill/protection tracking in Active Trades remains blocked until the execution ledger is qualified.`
    : `New entries are blocked until a verified ${isLive ? 'Live' : 'Paper'} profile and the manual-intent service are available.`);
  setText('#manualDeskSummary', enabled
    ? `${isLive ? 'Live' : 'Paper'} ${profile?.accountMask || profile?.account || 'account'} is eligible. ${state.managementDefaults?.manualAutosend ? 'Autosend is ON: a successful preview can submit immediately.' : 'Autosend is OFF: submission requires the final Submit action.'} Open a proposal to resolve and review a contract before any submission.`
    : `New entries are blocked: ${profile?.readinessReasons?.join?.(' · ') || 'a verified profile and manual-intent service are required.'}`);
  $('#manualTradeForm').hidden = !state.manualDeskExpanded;
  const toggle = $('#toggleManualTrade');
  toggle.textContent = state.manualDeskExpanded ? 'HIDE MANUAL PROPOSAL' : 'NEW MANUAL PROPOSAL';
  toggle.setAttribute('aria-expanded', String(state.manualDeskExpanded));

  updateStrikeSelectionFields(enabled);

  const profileSelect = $('#manualManagementProfile');
  const previous = profileSelect.value;
  clear(profileSelect);
  const profiles = state.managementDefaults?.profiles || [];
  if (!profiles.length) {
    appendOption(profileSelect, '', 'No management profiles available');
  } else {
    for (const item of profiles) appendOption(profileSelect, item.id, item.name || item.id, item.id === previous);
  }
}

function updateStrikeSelectionFields(enabled) {
  const isExact = $('#manualStrikeSelection').value !== 'TARGET_RANGE';
  setControlDisabled('#manualExpiry', !enabled || !isExact);
  setControlDisabled('#manualStrike', !enabled || !isExact);
  $('#manualExpiryField').hidden = !isExact;
  $('#manualStrikeField').hidden = !isExact;
}

function renderManagementDefaults() {
  const defaults = state.managementDefaults;
  const enabled = Boolean(defaults) && capability('management_defaults_write');
  const manual = $('#manualDefaultMode');
  const tradingview = $('#tradingviewDefaultMode');
  if (defaults) {
    manual.value = defaults.manualMode || 'MANAGED';
    tradingview.value = defaults.tradingviewMode || 'MANAGED';
    $('#manualAutosend').checked = Boolean(defaults.manualAutosend);
  }
  for (const selector of ['#manualDefaultMode', '#tradingviewDefaultMode', '#manualAutosend', '#saveManagementDefaults']) {
    setControlDisabled(selector, !enabled);
  }
  setText('#managementSettingsNote', defaults
    ? 'Changing a default affects future app-owned intents only. Existing trade instructions are immutable without a reconciled management action. Autosend submits a manual entry to IBKR immediately after a successful preview, with no further click.'
    : 'Management defaults are unavailable. The app will not infer an exit-management policy.');
  renderManualDesk();
}

function managementLabel(trade) {
  const mode = trade.managementMode;
  if (mode === 'APP_MANAGED') return 'APP MANAGED';
  if (mode === 'USER_MANAGED') return 'TRACK ONLY';
  if (mode === 'TRADINGVIEW_MANAGED') return 'TV MANAGED';
  return 'Unavailable';
}

// Summarizes a position's protection.legs into one status-pill label.
// protection.status !== 'OK' means the per-position protection/transitions
// read itself failed or was never wired -- distinct from a position that is
// genuinely unprotected (protection.status === 'OK' and legs is empty).
function summarizeProtection(protection) {
  if (!protection || protection.status !== 'OK') return { label: 'UNAVAILABLE', cls: 'blocked' };
  const legs = protection.legs || [];
  if (!legs.length) return { label: 'UNPROTECTED', cls: 'critical' };
  const allocatedLegs = legs.filter((leg) => Number(leg.quantity || 0) > 0);
  if (!allocatedLegs.length) return { label: 'UNPROTECTED', cls: 'critical' };
  const unresolved = allocatedLegs.some((leg) => (
    leg.status === 'SUBMISSION_UNKNOWN'
    || leg.modifyStatus === 'MODIFY_UNKNOWN'
    || leg.cancelStatus === 'CANCEL_UNKNOWN'
  ));
  if (unresolved) return { label: 'UNRESOLVED', cls: 'critical' };
  const working = allocatedLegs.filter((leg) => leg.status === 'SUBMITTED').length;
  const filled = allocatedLegs.filter((leg) => leg.status === 'FILLED').length;
  // A single working sibling must not mask a blocked/pending/cancelled leg
  // (for example, a take-profit working while its stop failed).
  if (working === allocatedLegs.length) return { label: `${working} WORKING`, cls: 'ok' };
  if (filled > 0) return { label: `${filled} FILLED`, cls: 'working' };
  return { label: 'PENDING', cls: 'queued' };
}

function reconciledAgeLabel(trade) {
  if (!trade.lastReconciledAt) return 'Stale · never reconciled';
  return `as of ${age(trade.lastReconciledAt)}`;
}

// Repurposes the allocation-grid area (below the positions table) as the
// selected position's real protection-leg / management-transition detail --
// broker-confirmed status, not just the pre-fill configured policy.
function renderPositionDetail(trade) {
  const grid = $('#allocationGrid');
  clear(grid);
  if (!trade) {
    grid.append(element('p', 'Select an active position to see its protection legs and management transitions.', 'empty-note'));
    return;
  }
  const header = element('div', undefined, 'allocation-heading');
  const rightLabel = trade.right === 'C' ? 'CALL' : trade.right === 'P' ? 'PUT' : (trade.right || '');
  const optionTitle = `${display(trade.symbol)}${trade.strike ? ` $${trade.strike}` : ''} ${rightLabel}`.trim();
  const contractInfo = trade.localSymbol || (trade.conId ? `ConID ${trade.conId}` : 'Contract details unavailable');
  const expiryInfo = trade.expiry ? ` · Exp ${trade.expiry}` : '';
  header.append(
    element('strong', `${optionTitle} (${contractInfo}${expiryInfo}) · ${managementLabel(trade)}`),
    element('span', `Realized P&L: ${trade.pnlFormatted || 'Unavailable'} · Unrealized P&L: ${trade.unrealizedPnlFormatted || 'Unavailable'} · Commission: ${trade.totalCommission ?? 'Unavailable'}`),
  );
  grid.append(header);
  const ladder = trade.protection?.ladder;
  if (ladder?.underfunded) {
    const warning = element('div', undefined, 'allocation-heading ladder-underfunded');
    warning.append(
      element('strong', `LADDER UNDERFUNDED · ${ladder.fundedLevels}/${ladder.configuredLevels} TIERS`),
      element('span', ladder.message),
    );
    grid.append(warning);
  }
  const legs = trade.protection?.legs || [];
  if (!legs.length) {
    grid.append(element('p', trade.protection?.status === 'OK'
      ? 'No protection legs are recorded for this position.'
      : 'Protection legs are unavailable for this position (the last read failed).', 'empty-note'));
  } else {
    for (const leg of legs) {
      const item = element('div', undefined, 'allocation-item');
      item.append(
        element('small', `${leg.role}${leg.levelId ? ` · ${leg.levelId}` : ''}`),
        element('strong', display(leg.status)),
        element('span', leg.triggerPrice ? `Trigger ${leg.triggerPrice}` : 'No trigger'),
        element('span', leg.limitPrice ? `Limit ${leg.limitPrice}` : 'No limit'),
        element('span', `Qty ${display(leg.quantity)}`),
        element('span', leg.realizedDriftPercent
          ? `Actual ${leg.realizedDriftPercent} vs entry`
          : 'Actual % unavailable'),
      );
      grid.append(item);
    }
  }
  const transitions = trade.protection?.transitions || [];
  if (transitions.length) {
    const transitionsHeader = element('div', undefined, 'allocation-heading');
    transitionsHeader.append(element('strong', 'MANAGEMENT TRANSITIONS'));
    grid.append(transitionsHeader);
    for (const transition of transitions) {
      const item = element('div', undefined, 'allocation-item');
      item.append(
        element('small', transition.after),
        element('strong', transition.action),
        element('span', transition.status === 'INERT' ? 'INERT · NEVER FIRED' : display(transition.status)),
        element('span', transition.status === 'INERT'
          ? (transition.reason || 'This transition could never fire; no stop was moved.')
          : transition.appliedAt
            ? `Applied ${formatMarketTime(transition.appliedAt)}`
            : 'Not yet applied'),
      );
      grid.append(item);
    }
  }
}

function buildActiveTradeRow(trade) {
  const row = element('tr');
  row.dataset.tradeId = trade.correlationId || '';
  row.tabIndex = 0;
  // Deliberately NOT role="button". That role removes the row from the table's
  // accessibility tree entirely -- the <td>s stop being exposed as cells, so a
  // screen-reader user loses the SYMBOL/QTY/PROTECTION/LIFECYCLE column-header
  // association -- and it nests three interactive controls (qty input, PARTIAL
  // CLOSE, FLATTEN) inside a button, which is invalid. The row keeps native
  // row/cell semantics; it is still focusable and Enter/Space still selects it
  // (see the #activeTradeRows keydown handler).
  row.setAttribute('aria-label', `Show protection detail for ${trade.symbol || 'position'}`);
  const rightLabel = trade.right === 'C' ? 'CALL' : trade.right === 'P' ? 'PUT' : (trade.right || '');
  const strikeLabel = trade.strike ? `$${trade.strike}` : '';
  const symbolDisplay = `${display(trade.symbol)}${strikeLabel ? ` ${strikeLabel}` : ''}${rightLabel ? ` ${rightLabel}` : ''}`;
  const protectionSummary = summarizeProtection(trade.protection);
  const protectionCell = element('td');
  protectionCell.append(element('span', protectionSummary.label, `status-pill ${protectionSummary.cls}`));
  const lifecycleCell = element('td');
  lifecycleCell.append(element('span', display(trade.lifecycleStatus), `status-pill ${statusClass(trade.lifecycleStatus)}`));
  const staleCell = element('td', reconciledAgeLabel(trade));
  if (!trade.lastReconciledAt) staleCell.classList.add('stale-cell');

  const pnlNum = trade.realizedPnl !== null && trade.realizedPnl !== undefined ? Number(trade.realizedPnl) : null;
  const pnlCell = element('td');
  if (pnlNum !== null) {
    const pnlClass = pnlNum > 0 ? 'ok' : pnlNum < 0 ? 'critical' : 'queued';
    pnlCell.append(element('span', trade.pnlFormatted || `${pnlNum >= 0 ? '+' : ''}$${pnlNum.toFixed(2)}`, `status-pill ${pnlClass}`));
  } else {
    pnlCell.append(element('span', '—', 'muted'));
  }

  const unPnlNum = trade.unrealizedPnl !== null && trade.unrealizedPnl !== undefined ? Number(trade.unrealizedPnl) : null;
  const unrealizedPnlCell = element('td');
  if (unPnlNum !== null && Number.isFinite(unPnlNum)) {
    const unPnlClass = unPnlNum > 0 ? 'ok' : unPnlNum < 0 ? 'critical' : 'queued';
    unrealizedPnlCell.append(element('span', trade.unrealizedPnlFormatted || `${unPnlNum >= 0 ? '+' : ''}$${unPnlNum.toFixed(2)}`, `status-pill ${unPnlClass}`));
  } else {
    unrealizedPnlCell.append(element('span', '—', 'muted'));
  }

  const actionsCell = element('td');
  const actionsWrap = element('div', undefined, 'row-actions');
  // Reuse the same qty <input> element across renders (see
  // state.qtyInputsByTrade) so an in-progress PARTIAL CLOSE quantity never
  // vanishes on the next 3s poll -- only ever set up once per trade; its
  // value is never touched again here, since there is no "server value" to
  // reconcile against (unlike setValuePreservingEdits' settings inputs).
  const correlationId = trade.correlationId || '';
  let qtyInput = state.qtyInputsByTrade.get(correlationId);
  if (!qtyInput) {
    qtyInput = element('input');
    qtyInput.type = 'number';
    qtyInput.min = '1';
    qtyInput.step = '1';
    qtyInput.className = 'control qty-input';
    qtyInput.placeholder = 'Qty';
    qtyInput.dataset.correlationId = correlationId;
    state.qtyInputsByTrade.set(correlationId, qtyInput);
  }
  if (trade.quantity?.open) qtyInput.max = trade.quantity.open;
  const partialButton = element('button', 'PARTIAL CLOSE', 'secondary');
  partialButton.type = 'button';
  partialButton.title = 'Reduce-only: never exceeds verified long quantity minus working exits.';
  partialButton.addEventListener('click', () => {
    const qty = Number(qtyInput.value);
    const openQty = Number(trade.quantity?.open);
    if (!Number.isInteger(qty) || qty < 1 || (Number.isInteger(openQty) && qty > openQty)) {
      const bound = Number.isInteger(openQty) && openQty > 0 ? ` between 1 and ${openQty}` : '';
      notifyFailure('PARTIAL CLOSE', `Enter a whole number of contracts${bound}.`, {
        correlationId: trade.correlationId,
        code: 'CLOSE_QUANTITY_INVALID',
      });
      qtyInput.focus();
      return;
    }
    openPartialCloseConfirm(trade, qty);
  });
  const flattenButton = element('button', 'FLATTEN', 'secondary flatten-button');
  flattenButton.type = 'button';
  flattenButton.title = 'Cancels every working protection leg, then sells the entire remaining quantity.';
  flattenButton.addEventListener('click', () => openFlattenConfirm(trade));
  actionsWrap.append(qtyInput, partialButton, flattenButton);
  actionsCell.append(actionsWrap);

  row.append(
    element('td', symbolDisplay),
    element('td', display(trade.account)),
    element('td', trade.quantity?.open ?? 'Unavailable'),
    element('td', trade.entryAvgPrice ? `$${trade.entryAvgPrice}` : 'Unavailable'),
    pnlCell,
    unrealizedPnlCell,
    protectionCell,
    lifecycleCell,
    staleCell,
    actionsCell,
  );
  return row;
}

function positionsStatusLabel(status) {
  if (status === 'OK') return 'CONFIRMED';
  if (status === 'NOT_REQUIRED') return 'CAPTURE ONLY';
  if (status === 'CHECKING') return 'CHECKING';
  return 'UNAVAILABLE';
}

function isConfirmedOpenTrade(trade) {
  const openQuantity = Number(trade?.quantity?.open);
  return trade?.lifecycleStatus !== 'CLOSED'
    && Number.isFinite(openQuantity)
    && openQuantity > 0;
}

function isConfirmedNonOpenTrade(trade) {
  const openQuantity = Number(trade?.quantity?.open);
  return trade?.lifecycleStatus === 'CLOSED'
    || (Number.isFinite(openQuantity) && openQuantity === 0);
}

function hasWorkingProtection(trade) {
  return trade?.protection?.status === 'OK'
    && (trade.protection.legs || []).some((leg) => ['SUBMITTED', 'WORKING', 'PRESUBMITTED'].includes(String(leg.status).toUpperCase()));
}

function renderActiveTrades() {
  const rows = $('#activeTradeRows');
  // clear(rows) below fully detaches every row (and the qty input inside
  // it) from the document -- a detached element cannot hold focus, so even
  // though buildActiveTradeRow reuses the same input object, the browser
  // still blurs it mid-render. Capture what the operator was doing before
  // that happens so it can be restored once the (possibly reused) input is
  // back in the document -- see the restore below.
  const focusedQtyInput = document.activeElement?.classList?.contains('qty-input') ? document.activeElement : null;
  const focusedCorrelationId = focusedQtyInput?.dataset.correlationId ?? null;
  const focusedSelectionStart = focusedQtyInput?.selectionStart ?? null;
  const focusedSelectionEnd = focusedQtyInput?.selectionEnd ?? null;
  clear(rows);
  // Drop any reusable qty-input entries for trades that are no longer active
  // (closed, or positions currently unavailable) -- otherwise
  // state.qtyInputsByTrade grows unboundedly across a long session.
  const activeCorrelationIds = new Set(state.activeTrades.map((trade) => trade.correlationId || ''));
  for (const correlationId of state.qtyInputsByTrade.keys()) {
    if (!activeCorrelationIds.has(correlationId)) state.qtyInputsByTrade.delete(correlationId);
  }
  const status = state.positionsStatus || 'CHECKING';
  const confirmed = status === 'OK';
  const statusPill = $('#positionsStatusPill');
  statusPill.textContent = positionsStatusLabel(status);
  statusPill.className = `status-pill ${confirmed ? 'ok' : status === 'NOT_REQUIRED' ? 'queued' : 'critical'}`;
  setText('#positionsStatusNote', confirmed
    ? `Broker-confirmed ${state.positionsCheckedAt ? age(state.positionsCheckedAt) : ''}.`
    : status === 'NOT_REQUIRED'
      ? 'Position tracking requires paper_tws execution mode.'
      : `Broker-confirmed position data is unavailable (${display(state.positionsReason)}) — this is not the same as zero open positions.`);

  const dailyPnlObj = state.dailyPnl;
  const dailyPnlElem = $('#dailyPnlStatus');
  if (dailyPnlElem) {
    if (dailyPnlObj && dailyPnlObj.realizedPnlToday !== null) {
      const val = Number(dailyPnlObj.realizedPnlToday);
      dailyPnlElem.textContent = `${val >= 0 ? '+' : ''}$${val.toFixed(2)}`;
      dailyPnlElem.style.color = val > 0 ? 'var(--green)' : val < 0 ? 'var(--red)' : 'var(--text)';
    } else {
      dailyPnlElem.textContent = 'Unavailable';
      dailyPnlElem.style.color = 'var(--dim)';
    }
  }

  if (!confirmed) {
    const row = element('tr');
    const cell = element('td', 'Positions are unavailable; broker truth could not be confirmed. This is not the same as zero open positions.', 'empty-cell critical-cell');
    cell.colSpan = 10;
    row.append(cell);
    rows.append(row);
    renderPositionDetail(null);
    return;
  }
  if (!state.activeTrades.length) {
    const row = element('tr');
    const cell = element('td', 'Confirmed: no open app-owned positions.', 'empty-cell');
    cell.colSpan = 10;
    row.append(cell);
    rows.append(row);
    renderPositionDetail(null);
    return;
  }
  for (const trade of state.activeTrades) {
    rows.append(buildActiveTradeRow(trade));
  }
  if (focusedCorrelationId) {
    const restored = state.qtyInputsByTrade.get(focusedCorrelationId);
    if (restored) {
      restored.focus();
      if (focusedSelectionStart !== null) {
        // type="number" inputs reject setSelectionRange in some browsers --
        // losing cursor position is harmless, losing focus/value is not.
        try {
          restored.setSelectionRange(focusedSelectionStart, focusedSelectionEnd);
        } catch {
          // ignored -- see comment above.
        }
      }
    }
  }
  const selected = (state.selectedTradeCorrelationId
    && state.activeTrades.find((item) => item.correlationId === state.selectedTradeCorrelationId))
    || state.activeTrades[0];
  renderPositionDetail(selected);
}

function renderClosedTrades() {
  const rows = $('#closedTradeRows');
  clear(rows);
  const status = state.closedPositionsStatus || 'CHECKING';
  const confirmed = status === 'OK';
  const pill = $('#closedPositionsStatusPill');
  pill.textContent = positionsStatusLabel(status);
  pill.className = `status-pill ${confirmed ? 'ok' : status === 'NOT_REQUIRED' ? 'queued' : 'critical'}`;
  setText('#closedPositionsStatusNote', confirmed
    ? `Broker-confirmed for ${state.closedMarketDate || 'the current New York market day'} · ${state.closedPositionsCheckedAt ? age(state.closedPositionsCheckedAt) : 'age unavailable'}.`
    : status === 'NOT_REQUIRED'
      ? 'Closed-position tracking requires paper_tws execution mode.'
      : `Broker-confirmed closed-position data is unavailable (${display(state.closedPositionsReason)}) — this is not the same as no closed positions.`);
  if (!confirmed) {
    const row = element('tr');
    const cell = element('td', 'Closed positions are unavailable; broker truth could not be confirmed.', 'empty-cell critical-cell');
    cell.colSpan = 9; row.append(cell); rows.append(row); return;
  }
  if (!state.closedTrades.length) {
    const row = element('tr');
    const cell = element('td', 'Confirmed: no app-owned positions closed during the current New York market day.', 'empty-cell');
    cell.colSpan = 9; row.append(cell); rows.append(row); return;
  }
  for (const trade of state.closedTrades) {
    const row = element('tr');
    const pnl = element('td');
    const pnlNum = trade.realizedPnl !== null && trade.realizedPnl !== undefined ? Number(trade.realizedPnl) : null;
    if (pnlNum !== null && Number.isFinite(pnlNum)) pnl.append(element('span', trade.pnlFormatted, `status-pill ${pnlNum > 0 ? 'ok' : pnlNum < 0 ? 'critical' : 'queued'}`));
    else pnl.textContent = 'Unavailable';
    const outcome = element('td');
    const rightLabel = trade.right === 'C' ? 'CALL' : trade.right === 'P' ? 'PUT' : (trade.right || '');
    const strikeLabel = trade.strike ? `$${trade.strike}` : '';
    const symbolDisplay = `${display(trade.symbol)}${strikeLabel ? ` ${strikeLabel}` : ''}${rightLabel ? ` ${rightLabel}` : ''}`;
    row.append(
      element('td', symbolDisplay), element('td', display(trade.account)),
      element('td', display(trade.quantityClosed)), pnl,
      element('td', trade.totalCommission ?? 'Unavailable'),
      element('td', formatMarketTime(trade.closedAt)), element('td', display(trade.source)),
      outcome, element('td', short(trade.correlationId)),
    );
    rows.append(row);
  }
}

async function loadOperatorState() {
  const results = await Promise.allSettled([
    api('/api/connection-profiles'),
    api('/api/management/defaults'),
    api('/api/tradingview/ownership'),
    api('/api/trades/active'),
    api('/api/trades/closed-today'),
  ]);
  const [profiles, defaults, ownership, trades, closedTrades] = results;
  state.operatorApisAvailable = results.some((result) => result.status === 'fulfilled');
  if (profiles.status === 'fulfilled') {
    state.profiles = profiles.value.items || [];
    state.activeProfileId = profiles.value.activeProfileId || state.profiles.find((profile) => profile.selected)?.id || state.profiles[0]?.id || null;
  } else {
    state.profiles = [];
    state.activeProfileId = null;
  }
  state.managementDefaults = defaults.status === 'fulfilled' ? defaults.value : null;
  state.tradingviewOwnership = ownership.status === 'fulfilled' ? ownership.value : null;
  if (trades.status === 'fulfilled') {
    // positionsStatus (not the presence of `items`) is the single source of
    // truth for "confirmed empty" vs. "unavailable" -- see renderActiveTrades.
    const returnedTrades = trades.value.items || [];
    state.excludedNonOpenTrades = returnedTrades.filter(isConfirmedNonOpenTrade);
    state.activeTrades = returnedTrades.filter(isConfirmedOpenTrade);
    state.dailyPnl = trades.value.dailyPnl || null;
    state.positionsStatus = trades.value.positionsStatus || null;
    state.positionsReason = trades.value.reason || null;
    state.positionsCheckedAt = trades.value.checkedAt || null;
    if (state.positionsStatus === 'OK'
      && returnedTrades.some((trade) => !isConfirmedOpenTrade(trade) && !isConfirmedNonOpenTrade(trade))) {
      state.positionsStatus = 'UNAVAILABLE';
      state.positionsReason = 'ACTIVE_POSITION_ROW_INVALID';
      state.activeTrades = [];
    }
  } else {
    state.activeTrades = [];
    state.excludedNonOpenTrades = [];
    state.dailyPnl = null;
    state.positionsStatus = 'UNAVAILABLE';
    state.positionsReason = 'ACTIVE_TRADES_API_UNAVAILABLE';
    state.positionsCheckedAt = null;
  }
  if (closedTrades.status === 'fulfilled') {
    state.closedTrades = closedTrades.value.items || [];
    state.closedPositionsStatus = closedTrades.value.positionsStatus || null;
    state.closedPositionsReason = closedTrades.value.reason || null;
    state.closedPositionsCheckedAt = closedTrades.value.checkedAt || null;
    state.closedMarketDate = closedTrades.value.marketDate || null;
  } else {
    state.closedTrades = [];
    state.closedPositionsStatus = 'UNAVAILABLE';
    state.closedPositionsReason = 'CLOSED_TRADES_API_UNAVAILABLE';
    state.closedPositionsCheckedAt = null;
    state.closedMarketDate = null;
  }
  renderRuntime();
  renderProfiles();
  renderManagementDefaults();
  renderActiveTrades();
  renderClosedTrades();
  // Position and protection reads finish after the alert fetch. Recompute
  // Attention now so it never shows the previous poll's risk state.
  renderAlerts();
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text === null ? 'Unavailable' : String(text);
  if (className) node.className = className;
  return node;
}

// Single source of truth for how the connected session's environment is named
// anywhere in the UI. Never defaults to PAPER -- see renderRuntime().
function setDialogEnvironment(selector) {
  const label = environmentLabelForDisplay();
  const node = $(selector);
  node.textContent = `ENVIRONMENT: ${label}`;
  node.className = `field-note dialog-environment ${label.toLowerCase()}`;
}

function environmentLabelForDisplay() {
  const reported = state.runtime?.core?.environment;
  return reported === 'LIVE' || reported === 'PAPER' ? reported : 'UNVERIFIED';
}

function renderStrikeSelection(strikeSelection) {
  const pill = $('#strikeSelectionPill');
  if (!strikeSelection || !strikeSelection.metric) {
    pill.textContent = 'UNAVAILABLE';
    pill.className = 'status-pill blocked';
    setText('#strikeMetric', 'Unavailable');
    setText('#strikeRange', 'Unavailable');
    setText('#strikeCandidateCount', 'Unavailable');
    return;
  }
  pill.textContent = 'ACTIVE';
  pill.className = 'status-pill ok';
  const unit = strikeSelection.metric === 'DELTA' ? '' : '$';
  setText('#strikeMetric', strikeSelection.metric);
  setText('#strikeRange', `${unit}${strikeSelection.lo} – ${unit}${strikeSelection.hi}`);
  setText('#strikeCandidateCount', strikeSelection.candidateCount);
}

function renderRuntime() {
  const runtime = state.runtime || {};
  const configured = runtime.webhook?.configured === true;
  const mode = runtime.mode || 'capture_only';
  const coreReady = runtime.core?.ready === true;
  const ledgerReady = runtime.ledger?.ready === true;
  const paperExecution = mode === 'paper_tws';
  const profile = selectedProfile();
  // The environment is a property of the *connected session*, reported by the
  // core. The operator's profile dropdown is an intent, not evidence, so it is
  // deliberately not a fallback here -- a selected "paper-tws" profile must
  // never make an unverified live session render as PAPER. Anything other than
  // an affirmative LIVE or PAPER is UNVERIFIED, and UNVERIFIED is presented as
  // a risk state, not as the safe one.
  const reported = runtime.core?.environment;
  const environment = reported === 'LIVE' || reported === 'PAPER' ? reported : 'UNVERIFIED';
  const isLive = environment === 'LIVE';
  const isVerifiedEnvironment = environment !== 'UNVERIFIED';
  const usable = profileIsReady(profile);

  setText('#webhookStatus', configured ? 'READY' : 'NOT CONFIGURED');
  setText('#executionStatus', paperExecution
    ? `${isLive ? 'LIVE SESSION' : (environment === 'UNVERIFIED' ? 'UNVERIFIED SESSION' : 'PAPER TWS')} · ${usable ? 'VERIFIED' : 'LOCKED'}`
    : 'CAPTURE ONLY');
  setText('#coreStatus', coreReady ? 'ADAPTER READY' : 'UNAVAILABLE');
  setText('#accountStatus', runtime.core?.accountMask || 'Unavailable');
  setText('#lastUpdate', runtime.updatedAt ? age(runtime.updatedAt) : 'Unavailable');
  setText('#webhookEndpoint', runtime.webhook?.endpoint || `${location.origin}/webhooks/tradingview`);
  setText('#authStatus', configured ? 'Configured · secret redacted' : 'Missing QT_WEBHOOK_SECRET');
  setText('#ledgerStatus', ledgerReady ? 'SQLite WAL ready' : 'Unavailable');
  const environmentLabel = isLive ? 'LIVE' : isVerifiedEnvironment ? 'PAPER' : 'UNVERIFIED';
  setText('#footerMode', `${isLive ? 'LIVE SESSION' : environmentLabel} · ${usable ? 'VERIFIED' : 'LOCKED'} · ${paperExecution ? 'OFFICIAL TWS' : 'CAPTURE ONLY'}`);
  setText('#environmentBadge', isVerifiedEnvironment
    ? `${environmentLabel} · ${isLive ? (usable ? 'SESSION VERIFIED' : 'LOCKED') : (usable ? 'READY' : 'LOCKED')}`
    : 'UNVERIFIED · ENVIRONMENT UNKNOWN');
  $('#environmentBadge').className = `mode-badge ${isLive ? 'live' : ''}${isVerifiedEnvironment ? '' : ' unverified'}`;

  const ingressPill = $('#ingressPill');
  ingressPill.textContent = configured && ledgerReady ? 'READY' : 'BLOCKED';
  ingressPill.className = `status-pill ${configured && ledgerReady ? 'ok' : 'blocked'}`;

  renderStrikeSelection(runtime.core?.strikeSelection || null);

  const banner = $('#safetyBanner');
  clear(banner);
  const title = element('strong', paperExecution ? `CONTROLLED ${isLive ? 'LIVE SESSION' : environmentLabel} ADAPTER` : 'CAPTURE-ONLY SAFETY MODE');
  const message = element('span', paperExecution
    ? (!isVerifiedEnvironment
      ? 'The connected core has not reported whether this session is PAPER or LIVE. Treat this as potentially live: do not assume orders are simulated.'
      : coreReady
      ? `The ${isLive ? 'live' : 'paper'} core is connected. Manual entries may be reviewed and submitted (or autosent, per Settings); TradingView-sourced alerts remain execution-blocked until fill and protection management is qualified.`
      : `${isLive ? 'Live' : 'Paper'} execution is configured but the official TWS core is not ready; new entries are blocked.`)
    : 'Alerts can be authenticated and recorded; no broker order can be placed in this mode.');
  banner.append(title, message);
  banner.className = `safety-banner ${paperExecution && (!coreReady || !isVerifiedEnvironment) ? 'danger' : ''}`;

  const checks = [
    ['Webhook secret configured', configured],
    ['Durable SQLite ledger ready', ledgerReady],
    [isLive ? 'Exact live (U) account verified' : 'Exact DU paper account verified', coreReady && Boolean(runtime.core?.accountMask)],
    ['Basic core readiness (execution ledger incomplete)', coreReady],
  ];
  const list = $('#readinessList');
  clear(list);
  for (const [label, passed] of checks) {
    const item = element('li');
    item.className = passed ? 'pass' : 'fail';
    item.append(element('span', passed ? 'PASS' : 'BLOCK'), element('strong', label));
    list.append(item);
  }
  setText('#readinessCount', `${checks.filter(([, passed]) => passed).length} / ${checks.length}`);
}

// "Why is this blocked" panel: surfaces GET /api/reconciliation's unresolved
// submission/protection/management-transition ambiguity flags -- exactly the
// broker-evidence gaps ExecutionEngine._verify_readiness() globally blocks
// new opens on (UNRESOLVED_SUBMISSION / UNRESOLVED_PROTECTION_SUBMISSION /
// UNRESOLVED_TRANSITION_FAILURE), otherwise invisible to the operator.
function renderReconciliation() {
  const pill = $('#reconciliationPill');
  const list = $('#reconciliationList');
  if (!pill || !list) return; // defensive: panel may not exist in an older cached index.html.
  clear(list);
  const data = state.reconciliation;
  if (!data || data.status !== 'OK' || !data.unresolved) {
    pill.textContent = 'UNAVAILABLE';
    pill.className = 'status-pill blocked';
    list.append(element('p', 'Reconciliation status is currently unavailable; this is not the same as "nothing unresolved".', 'empty-note'));
    return;
  }
  const u = data.unresolved;
  const reasons = [
    [u.hasUnresolvedSubmission === true, 'An entry/close submission outcome is unknown (SUBMISSION_UNKNOWN) and unreconciled.'],
    [u.hasUnresolvedProtection === true, 'A protection-order placement or modify outcome is unknown and unreconciled.'],
    [u.hasUnresolvedTransition === true, 'A management transition (e.g. move-stop-to-breakeven) outcome is unknown and unreconciled.'],
  ].filter(([active]) => active);
  if (!reasons.length) {
    pill.textContent = 'CLEAR';
    pill.className = 'status-pill ok';
    list.append(element('p', 'No unresolved broker outcomes -- new opens are not blocked by reconciliation ambiguity.', 'empty-note'));
    return;
  }
  pill.textContent = 'BLOCKED';
  pill.className = 'status-pill blocked';
  for (const [, label] of reasons) {
    const item = element('li');
    item.className = 'fail';
    item.append(element('span', 'BLOCK'), element('strong', label));
    list.append(item);
  }
}

function renderAlerts() {
  ensureTodayAlertDate();
  const rows = $('#alertRows');
  clear(rows);
  const stateFilter = $('#alertStateFilter')?.value || 'ALL';
  const symbolFilter = ($('#alertSymbolFilter')?.value || '').trim().toUpperCase();
  const dateFilter = $('#alertDateFilter')?.value || '';
  const attentionStates = ['BLOCKED', 'FAILED', 'EXPIRED', 'SUBMISSION_UNKNOWN'];
  const visibleAlerts = state.alerts.filter((alert) => {
    if (stateFilter === 'ATTENTION' ? !attentionStates.includes(alert.status) : stateFilter !== 'ALL' && alert.status !== stateFilter) return false;
    if (symbolFilter && alert.payload?.ticker?.toUpperCase() !== symbolFilter) return false;
    return !dateFilter || marketDate(alert.createdAt) === dateFilter;
  });
  setText('#alertResultCount', `Showing ${visibleAlerts.length} of latest ${state.alerts.length} alerts · NY time`);
  if (!visibleAlerts.length) {
    const row = element('tr');
    const cell = element('td', state.alerts.length ? 'No alerts match the current filters.' : 'No TradingView alerts captured yet.', 'empty-cell');
    cell.colSpan = 7;
    row.append(cell);
    rows.append(row);
  }

  for (const alert of visibleAlerts) {
    const row = element('tr');
    if (alert.correlationId === state.selectedCorrelation) row.classList.add('selected');
    row.append(
      element('td', formatMarketTime(alert.createdAt)),
      element('td', alert.alertId),
      element('td', `${alert.payload?.ticker || '—'} · ${alert.payload?.action || '—'}`),
      element('td', `${alert.payload?.strategy_id || '—'} @ ${alert.payload?.strategy_version || '—'}`),
    );
    const statusCell = element('td');
    statusCell.append(element('span', statusLabel(alert.status), `status-pill ${statusClass(alert.status)}`));
    row.append(statusCell, element('td', short(alert.correlationId)));
    const actionCell = element('td');
    const button = element('button', 'VIEW', 'text-button');
    button.type = 'button';
    button.dataset.correlation = alert.correlationId;
    actionCell.append(button);
    row.append(actionCell);
    rows.append(row);
  }

  const mergedAttentionAlerts = [...state.alerts];
  const knownAttentionCorrelations = new Set(mergedAttentionAlerts.map((alert) => alert.correlationId));
  for (const alert of state.attentionAlerts) {
    if (!knownAttentionCorrelations.has(alert.correlationId)) mergedAttentionAlerts.push(alert);
  }
  const attention = mergedAttentionAlerts.filter((alert) => attentionStates.includes(alert.status)
    && !(isClearableAttention(alert)
      && state.attentionAcknowledgements.has(attentionAcknowledgementKey(alert))));
  // Only a definitive working protection status is safe. Unavailable,
  // unresolved, pending, filled-only, and unprotected states all require
  // operator action for a currently open position.
  const unsafeProtectionTrades = state.positionsStatus === 'OK'
    ? state.activeTrades.filter((trade) => !summarizeProtection(trade.protection).label.endsWith(' WORKING'))
    : [];
  const closedWithWorkingProtection = state.excludedNonOpenTrades.filter(hasWorkingProtection);
  const historicalNotices = attention.filter(isClearableAttention);
  const activeAlertRisks = attention.filter((alert) => !isClearableAttention(alert));
  const positionEvidenceUnavailable = state.positionsStatus !== 'OK';
  const reconciliationEvidenceUnavailable = !state.reconciliation || state.reconciliation.status !== 'OK';
  const nodeAttentionEvidenceUnavailable = state.nodeAttentionStatus !== 'OK';
  const unresolvedReconciliation = state.reconciliation?.status === 'OK'
    && state.reconciliation.unresolved
    && (
      state.reconciliation.unresolved.hasUnresolvedSubmission === true
      || state.reconciliation.unresolved.hasUnresolvedProtection === true
      || state.reconciliation.unresolved.hasUnresolvedTransition === true
    );
  const activeRiskCount = activeAlertRisks.length
    + unsafeProtectionTrades.length
    + closedWithWorkingProtection.length
    + (positionEvidenceUnavailable ? 1 : 0)
    + (reconciliationEvidenceUnavailable || unresolvedReconciliation ? 1 : 0)
    + (nodeAttentionEvidenceUnavailable ? 1 : 0);
  const clearButton = $('#clearResolvedAttention');
  clearButton.textContent = `ACKNOWLEDGE NOTICES${historicalNotices.length ? ` (${historicalNotices.length})` : ''}`;
  clearButton.disabled = historicalNotices.length === 0;
  const riskCount = $('#attentionRiskCount');
  riskCount.textContent = `${activeRiskCount} ACTIVE RISK${activeRiskCount === 1 ? '' : 'S'}`;
  riskCount.className = `attention-risk-count ${activeRiskCount ? 'critical' : 'ok'}`;
  setText('#attentionNoticeCount', `${historicalNotices.length} NOTICE${historicalNotices.length === 1 ? '' : 'S'}`);
  const attentionList = $('#attentionList');
  clear(attentionList);
  appendAttentionHeading(attentionList, 'ACTIVE RISKS', activeRiskCount, activeRiskCount ? 'critical' : 'ok');
  if (!activeRiskCount) {
    attentionList.append(element('p', 'No active broker or position risk is currently detected.', 'attention-empty-state'));
  } else {
    if (positionEvidenceUnavailable) {
      const item = element('div', undefined, 'attention-item critical');
      item.append(
        element('strong', 'POSITION EVIDENCE UNAVAILABLE'),
        element('span', 'Open-position risk cannot be confirmed. New entries remain unsafe until the selected account is refreshed from IBKR.'),
        element('small', `${display(state.positionsReason)} · checked ${age(state.positionsCheckedAt)}`, 'attention-item-meta'),
      );
      attentionList.append(item);
    }
    if (reconciliationEvidenceUnavailable || unresolvedReconciliation) {
      const item = element('div', undefined, 'attention-item critical');
      item.append(
        element('strong', reconciliationEvidenceUnavailable ? 'RECONCILIATION EVIDENCE UNAVAILABLE' : 'BROKER OUTCOME REQUIRES RECONCILIATION'),
        element(
          'span',
          reconciliationEvidenceUnavailable
            ? 'Unknown submissions, protection changes, and management transitions cannot be ruled out. Do not resend or open until reconciliation is available.'
            : 'At least one broker submission or management outcome is unresolved. Review the reconciliation panel and TWS before taking action.',
        ),
        element('small', `Checked ${age(state.reconciliation?.checkedAt)}`, 'attention-item-meta'),
      );
      attentionList.append(item);
    }
    if (nodeAttentionEvidenceUnavailable) {
      const item = element('div', undefined, 'attention-item critical');
      item.append(
        element('strong', 'ALERT-RISK HISTORY UNAVAILABLE'),
        element('span', 'Older Node-side unknown submissions cannot be ruled out. Do not resend a prior signal until alert-risk history is available.'),
      );
      attentionList.append(item);
    }
    for (const trade of closedWithWorkingProtection) {
      const item = element('button', undefined, 'attention-item critical');
      item.type = 'button';
      item.dataset.correlation = trade.correlationId || '';
      item.append(
        element('strong', `CLOSED · WORKING EXIT · ${trade.symbol || '—'}`),
        element('span', 'A zero-quantity position still has working protection evidence. Verify/cancel the orders in TWS and reconcile.'),
        element('small', `Lifecycle ${display(trade.lifecycleStatus)} · ${short(trade.correlationId)}`, 'attention-item-meta'),
      );
      attentionList.append(item);
    }
    for (const trade of unsafeProtectionTrades) {
      const protection = summarizeProtection(trade.protection);
      const item = element('button', undefined, 'attention-item critical');
      item.type = 'button';
      item.dataset.tradeId = trade.correlationId || '';
      item.append(
        element('strong', `${protection.label} PROTECTION · ${trade.symbol || '—'}`),
        element('span', 'A confirmed open position does not have definitively working protective exits. Review the position and orders in TWS now.'),
        element('small', `${trade.source || 'Source unavailable'} · ${short(trade.correlationId)}`, 'attention-item-meta'),
      );
      attentionList.append(item);
    }
    for (const alert of activeAlertRisks) {
      const item = element('button', undefined, `attention-item ${statusClass(alert.status)}`);
      item.type = 'button';
      item.dataset.correlation = alert.correlationId;
      item.append(
        element('strong', `${statusLabel(alert.status)} · ${alert.payload?.ticker || '—'}`),
        element('span', attentionReason(alert)),
        element('small', `${formatMarketTime(alert.createdAt)} · ${age(alert.createdAt)} · ${short(alert.correlationId)}`, 'attention-item-meta'),
      );
      attentionList.append(item);
    }
  }

  appendAttentionHeading(attentionList, 'HISTORICAL SIGNAL NOTICES', historicalNotices.length, 'notice');
  if (!historicalNotices.length) {
    attentionList.append(element('p', 'No unacknowledged blocked or failed signal notices.', 'attention-empty-state'));
  } else {
    for (const alert of historicalNotices) {
      const item = element('button', undefined, 'attention-item notice');
      item.type = 'button';
      item.dataset.correlation = alert.correlationId;
      item.append(
        element('strong', `${statusLabel(alert.status)} · ${alert.payload?.ticker || '—'} · ${alert.status === 'BLOCKED' ? 'NO ORDER SENT' : 'TERMINAL NOTICE'}`),
        element('span', attentionReason(alert)),
        element('small', `${formatMarketTime(alert.createdAt)} · ${age(alert.createdAt)} · ${short(alert.correlationId)}`, 'attention-item-meta'),
      );
      attentionList.append(item);
    }
  }
}

function summaryItem(label, value) {
  const item = element('div');
  item.append(element('small', label), element('strong', value));
  return item;
}

async function selectAlert(correlationId) {
  state.selectedCorrelation = correlationId;
  renderAlerts();
  try {
    const detail = await api(`/api/tradingview/alerts/${encodeURIComponent(correlationId)}`);
    const alert = detail.alert;
    setText('#selectedCorrelation', alert.correlationId);
    const selectedStatus = $('#selectedStatus');
    selectedStatus.textContent = statusLabel(alert.status);
    selectedStatus.className = `status-pill ${statusClass(alert.status)}`;
    $('#detailEmpty').hidden = true;
    $('#detailContent').hidden = false;
    const summary = $('#alertSummary');
    clear(summary);
    summary.append(
      summaryItem('Alert ID', alert.alertId),
      summaryItem('Signal', `${alert.payload?.ticker || '—'} · ${alert.payload?.action || '—'}`),
      summaryItem('Strategy', `${alert.payload?.strategy_id || '—'} @ ${alert.payload?.strategy_version || '—'}`),
      summaryItem('Received', `${formatMarketTime(alert.createdAt)} · ${age(alert.createdAt)}`),
      // Never the literal "Paper adapter". That string was rendered on orders
      // executed against a live U-account, because it described the *feature*
      // rather than the session. Report the environment the connected core
      // actually reports, and say UNVERIFIED when it reports nothing.
      summaryItem('Execution eligibility', alert.executionEligible
        ? `Submittable · ${environmentLabelForDisplay()} session`
        : 'Capture only · permanent'),
      summaryItem('Management', `${alert.managementMode || 'Unavailable'}${alert.managementMode === 'APP_MANAGED' ? ' · protection active' : ''}`),
      summaryItem('Management policy', alert.managementPolicyId || 'Unavailable'),
      summaryItem('IBKR order', alert.order?.brokerOrderId || 'Not acknowledged'),
      summaryItem('Reason code', alert.order?.errorCode || 'None'),
    );
    const timeline = $('#alertTimeline');
    clear(timeline);
    for (const event of detail.timeline || []) {
      const item = element('li');
      item.append(
        element('span', '', `timeline-dot ${statusClass(event.type)}`),
        element('strong', statusLabel(event.type)),
        element('small', formatMarketTime(event.occurredAt)),
        element('p', event.details?.error_code || event.details?.broker_order_id || 'Durably recorded'),
      );
      timeline.append(item);
    }
    $('#unknownWarning').hidden = alert.status !== 'SUBMISSION_UNKNOWN';
  } catch (error) {
    notify(`${error.code ? `${error.code}: ` : ''}${error.message}`);
  }
}

// Loaded and rendered independently of the main runtime/alerts fetch above --
// a reconciliation-proxy failure must never block the rest of the dashboard
// from refreshing, and vice versa.
async function loadReconciliation() {
  try {
    state.reconciliation = await api('/api/reconciliation');
  } catch {
    state.reconciliation = null;
  }
  renderReconciliation();
  // Reconciliation is the unbounded source for unknown broker outcomes,
  // including ones older than the recent Alert Activity window.
  renderAlerts();
}

async function refresh(options) {
  const forceCoreCheck = options?.force === true;
  try {
    const [runtime, result, attentionResult] = await Promise.all([
      api(forceCoreCheck ? '/api/tradingview/runtime?refresh=1' : '/api/tradingview/runtime'),
      api('/api/tradingview/alerts?limit=500'),
      api('/api/tradingview/attention-risks'),
    ]);
    state.runtime = runtime;
    state.alerts = result.items || [];
    state.attentionAlerts = attentionResult.items || [];
    state.nodeAttentionStatus = 'OK';
    renderRuntime();
    renderAlerts();
    await loadOperatorState();
    await loadReconciliation();
    if (state.selectedCorrelation && state.alerts.some((item) => item.correlationId === state.selectedCorrelation)) {
      await selectAlert(state.selectedCorrelation);
    }
  } catch (error) {
    // A failed dashboard fetch tells us nothing about the backend. Reporting
    // mode 'capture_only' here made the banner assert "no broker order can be
    // placed in this mode" -- a fabricated safety claim, while the backend may
    // well still be in paper_tws auto-submitting TradingView alerts. Keep the
    // previously observed mode and drop the environment to unknown.
    state.runtime = {
      ...(state.runtime || {}),
      core: null,
      transportStatus: 'UNAVAILABLE',
      updatedAt: state.runtime?.updatedAt || null,
    };
    state.nodeAttentionStatus = 'UNAVAILABLE';
    renderRuntime();
    await loadOperatorState();
    await loadReconciliation();
    notifyFailure('MANUAL SUBMIT', `${error.code ? `${error.code}: ` : ''}${error.message}`, { code: error.code });
  }
}

async function saveProfile() {
  const profile = selectedProfile();
  if (!profileIsEditable(profile)) return notify('Connection settings are locked until the Paper profile service confirms this action.');
  const capitalPerTrade = $('#profileCapitalPerTrade').value.trim();
  try {
    await api(`/api/connection-profiles/${encodeURIComponent(profile.id)}`, {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        host: $('#profileHost').value.trim(),
        port: Number($('#profilePort').value),
        clientId: Number($('#profileClientId').value),
        selected: true,
        // Empty clears any previously configured capital-per-trade amount,
        // which is a legitimate operator action (falls back to the
        // pre-existing default of one contract, never a wider risk budget).
        capitalPerTradeDollars: capitalPerTrade === '' ? null : Number(capitalPerTrade),
      }),
    });
    clearDirty(['#profileHost', '#profilePort', '#profileClientId', '#profileCapitalPerTrade']);
    notify(`${profile.environment === 'LIVE' ? 'Live' : 'Paper'} profile saved. The running core keeps its current host and port until it is restarted.`);
    await loadOperatorState();
  } catch (error) {
    notifyFailure('SAVE PROFILE', `${error.code ? `${error.code}: ` : ''}${error.message}`, { code: error.code });
  }
}

async function activateProfile(id) {
  const profile = state.profiles.find((item) => item.id === id);
  if (!profile || !capability('connection_profiles_write')) return;
  try {
    await api(`/api/connection-profiles/${encodeURIComponent(profile.id)}`, {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ host: profile.host, port: profile.port, clientId: profile.clientId, selected: true }),
    });
    await loadOperatorState();
  } catch (error) {
    notify(`${error.code ? `${error.code}: ` : ''}${error.message}`);
    await loadOperatorState();
  }
}

function manualTradePayload() {
  const selectedMode = document.querySelector('input[name="managementMode"]:checked')?.value || 'DEFAULT';
  const defaults = state.managementDefaults || {};
  const mode = selectedMode === 'DEFAULT' ? (defaults.manualMode || 'USER_MANAGED') : selectedMode;
  const strikeSelection = $('#manualStrikeSelection').value;
  // No quantity field: the core computes the actual contract count from this
  // profile's capital-per-trade amount and the option's fresh mid-price at
  // preview/submit time (see #previewQuantity in renderTradePreview()).
  const payload = {
    profileId: state.activeProfileId,
    source: 'MANUAL_UI',
    symbol: $('#manualSymbol').value.trim().toUpperCase(),
    strikeSelection,
    right: $('#manualRight').value === 'CALL' ? 'C' : 'P',
    entryPolicy: 'MARKETABLE_LIMIT',
    managementMode: mode,
  };
  if (strikeSelection === 'EXACT') {
    payload.expiry = $('#manualExpiry').value.replaceAll('-', '');
    payload.strike = Number($('#manualStrike').value);
  }
  if (mode === 'APP_MANAGED') payload.managementProfileId = selectedMode === 'DEFAULT'
    ? defaults.manualManagementProfileId
    : ($('#manualManagementProfile').value || undefined);
  return payload;
}

function renderTradePreview(preview) {
  const panel = $('#manualTradePreview');
  const submitButton = $('#submitManualTrade');
  if (!preview || preview.status !== 'PREVIEW_READY') {
    panel.hidden = true;
    submitButton.hidden = true;
    state.lastManualIntentId = null;
    return;
  }
  setText('#previewLocalSymbol', preview.localSymbol || `conId ${preview.conId}`);
  setText('#previewAccount', preview.account || 'Unavailable');
  setText('#previewAction', preview.action || 'Unavailable');
  setText('#previewQuantity', preview.quantity ?? 'Unavailable');
  setText('#previewOrder', preview.orderType && preview.limitPrice ? `${preview.orderType} ${preview.limitPrice}` : 'Unavailable');
  setText('#previewSubmission', 'Not submitted');
  panel.hidden = false;
}

function renderSubmissionResult(submission) {
  const submitButton = $('#submitManualTrade');
  if (!submission) {
    setText('#previewSubmission', 'Not submitted');
    return;
  }
  setText('#previewSubmission', submission.brokerOrderId
    ? `${submission.status} · broker order ${submission.brokerOrderId}`
    : submission.code
      ? `${submission.status} · ${submission.code}`
      : submission.status);
  submitButton.hidden = true;
}

async function reviewManualTrade(event) {
  event.preventDefault();
  const profile = selectedProfile();
  if (!profileCanCreateProposal(profile) || !capability('manual_trade_intents_write')) {
    notify('Manual proposal is blocked: select a verified, connected Paper or Live profile and wait for the manual-intent API.');
    return;
  }
  const form = $('#manualTradeForm');
  if (!form.reportValidity()) return;
  const envLabel = profile.environment === 'LIVE' ? 'Live' : 'Paper';
  const payload = manualTradePayload();
  try {
    const result = await api('/api/trade-intents/manual', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
    });
    const preview = result.preview;
    renderTradePreview(preview);
    state.lastManualIntentId = result.intent?.id || null;
    const previewText = preview?.status === 'PREVIEW_READY'
      ? ` Resolved ${preview.localSymbol || `conId ${preview.conId}`} at ${preview.orderType} ${preview.limitPrice}.`
      : preview?.code
        ? ` Preview blocked: ${preview.code}.`
        : '';
    const submitButton = $('#submitManualTrade');
    let outcomeText;
    if (result.submission) {
      renderSubmissionResult(result.submission);
      outcomeText = result.submission.status === 'SUBMITTED'
        ? ` Autosend submitted to IBKR — broker order ${result.submission.brokerOrderId}.`
        : ` Autosend result: ${result.submission.status}${result.submission.code ? ` (${result.submission.code})` : ''}.`;
    } else if (preview?.status === 'PREVIEW_READY') {
      submitButton.hidden = false;
      submitButton.disabled = false;
      outcomeText = ' No IBKR order was sent — click SUBMIT to send it.';
    } else {
      submitButton.hidden = true;
      outcomeText = ' No IBKR order was sent.';
    }
    notify(`${envLabel} proposal ${short(result.intent?.correlationId || result.intent?.id || 'created')} was stored.${previewText}${outcomeText}`);
    await refresh();
  } catch (error) {
    renderTradePreview(null);
    notify(`${error.code ? `${error.code}: ` : ''}${error.message}`);
  }
}

async function submitReviewedTrade() {
  const id = state.lastManualIntentId;
  const button = $('#submitManualTrade');
  if (!id) return;
  button.disabled = true;
  try {
    const result = await api(`/api/trade-intents/manual/${encodeURIComponent(id)}/submit`, { method: 'POST' });
    renderSubmissionResult(result.submission);
    const submission = result.submission;
    notify(submission?.status === 'SUBMITTED'
      ? `Submitted to IBKR — broker order ${submission.brokerOrderId}.`
      : `Submission result: ${submission?.status || 'UNKNOWN'}${submission?.code ? ` (${submission.code})` : ''}.`);
    await refresh();
  } catch (error) {
    button.disabled = false;
    notify(`${error.code ? `${error.code}: ` : ''}${error.message}`);
  }
}

// A retried click for the identical (position, close mode) reuses the same
// requestId until a definitive response arrives -- the Node close endpoint
// requires this so a retry never submits a second broker order (see
// server.mjs's submitCloseIntent). Cleared only once the outcome is known.
function pendingCloseRequestId(correlationId, mode) {
  const key = `${correlationId}:${mode}`;
  if (!state.pendingCloseRequestIds.has(key)) {
    state.pendingCloseRequestIds.set(key, crypto.randomUUID());
  }
  return state.pendingCloseRequestIds.get(key);
}

function clearPendingCloseRequest(correlationId, mode) {
  state.pendingCloseRequestIds.delete(`${correlationId}:${mode}`);
}

function closeSubmissionIsDefinitive(submission) {
  // A timeout/disconnect is intentionally not a retryable result. Retain the
  // exact client request id so another operator click cannot mint a second
  // close intent before broker reconciliation proves what happened.
  return ['SUBMITTED', 'BLOCKED', 'FAILED', 'REJECTED'].includes(submission?.status);
}

async function submitPartialClose(trade, quantity) {
  if (!Number.isFinite(quantity) || quantity < 1 || !Number.isInteger(quantity)) {
    notifyFailure('PARTIAL CLOSE', 'Enter a positive whole number of contracts to partially close.', {
      correlationId: trade.correlationId, code: 'CLOSE_QUANTITY_INVALID',
    });
    return;
  }
  const requestId = pendingCloseRequestId(trade.correlationId, 'REDUCE_ONLY_PARTIAL');
  try {
    const result = await api(`/api/trades/${encodeURIComponent(trade.correlationId)}/close`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ mode: 'REDUCE_ONLY_PARTIAL', quantity, requestId }),
    });
    const submission = result.submission;
    if (closeSubmissionIsDefinitive(submission)) clearPendingCloseRequest(trade.correlationId, 'REDUCE_ONLY_PARTIAL');
    if (submission.status === 'SUBMITTED') {
      notify(`Partial close submitted — broker order ${submission.brokerOrderId}.`);
    } else {
      notifyFailure('PARTIAL CLOSE', `Partial close result: ${submission.status}.`, {
        correlationId: trade.correlationId, code: submission.code,
      });
    }
    await loadOperatorState();
  } catch (error) {
    notifyFailure('PARTIAL CLOSE', `${error.code ? `${error.code}: ` : ''}${error.message}`, {
      correlationId: trade.correlationId, code: error.code,
    });
  }
}

function openPartialCloseConfirm(trade, quantity) {
  state.pendingPartialClose = { trade, quantity };
  const dialog = $('#partialCloseConfirmDialog');
  if (dialog) {
    setDialogEnvironment('#partialCloseEnvironment');
    setText('#partialCloseSummary', `${display(trade.symbol)} · ${quantity} contract(s) · account ${display(trade.account)}`);
    dialog.showModal();
  } else {
    const confirmed = window.confirm(`Confirm reduce-only partial close of ${quantity} contract(s) for ${display(trade.symbol)}?`);
    if (confirmed) {
      submitPartialClose(trade, quantity);
    }
  }
}

// FLATTEN is the most consequential position action in this app (cancels
// every protection leg, then sells the entire remaining quantity) -- it gets
// its own explicitly-confirmed dialog rather than a plain button, the same
// weight of friction this app already gives the live-trading confirmation
// phrase and the Autosend opt-in, not a single unguarded click.
function openFlattenConfirm(trade) {
  state.pendingFlatten = trade;
  setDialogEnvironment('#flattenEnvironment');
  setText('#flattenSummary', `${display(trade.symbol)} · qty ${trade.quantity?.open ?? 'Unavailable'} · account ${display(trade.account)}`);
  $('#flattenConfirmInput').value = '';
  $('#confirmFlatten').disabled = true;
  $('#flattenConfirmDialog').showModal();
}

async function submitFlatten() {
  const trade = state.pendingFlatten;
  if (!trade) return;
  const button = $('#confirmFlatten');
  button.disabled = true;
  const requestId = pendingCloseRequestId(trade.correlationId, 'FULL_FLATTEN');
  try {
    const result = await api(`/api/trades/${encodeURIComponent(trade.correlationId)}/close`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ mode: 'FULL_FLATTEN', requestId }),
    });
    const submission = result.submission;
    if (closeSubmissionIsDefinitive(submission)) clearPendingCloseRequest(trade.correlationId, 'FULL_FLATTEN');
    if (submission.status === 'SUBMITTED') {
      notify(`Flatten submitted — broker order ${submission.brokerOrderId}.`);
    } else {
      notifyFailure('FLATTEN', `Flatten result: ${submission.status}.`, {
        correlationId: trade.correlationId, code: submission.code,
      });
    }
    $('#flattenConfirmDialog').close();
    state.pendingFlatten = null;
    await loadOperatorState();
  } catch (error) {
    notifyFailure('FLATTEN', `${error.code ? `${error.code}: ` : ''}${error.message}`, {
      correlationId: trade.correlationId, code: error.code,
    });
  } finally {
    button.disabled = false;
  }
}

function renderCoreProcess(snapshot) {
  if (!snapshot) return;
  const pillLabel = { stopped: 'STOPPED', starting: 'STARTING', running: 'RUNNING', exited: 'EXITED' }[snapshot.state] || 'UNKNOWN';
  setText('#coreProcessPill', pillLabel);
  setText('#coreProcessState', pillLabel);
  setText('#coreProcessPid', snapshot.pid);
  setText('#coreProcessStarted', snapshot.startedAt);
  const log = (snapshot.logTail || []).map((entry) => `[${entry.stream}] ${entry.line}`).join('\n');
  $('#coreProcessLog').textContent = log || (snapshot.managed ? '(no output yet)' : '(not managed by this app)');
  $('#coreProcessLog').scrollTop = $('#coreProcessLog').scrollHeight;
}

async function loadCoreProcess() {
  try {
    const snapshot = await api('/api/core/process');
    renderCoreProcess(snapshot);
  } catch (error) {
    notify(`${error.code ? `${error.code}: ` : ''}${error.message}`);
  }
}

async function coreProcessAction(action, button) {
  button.disabled = true;
  try {
    const result = await api(`/api/core/process/${action}`, { method: 'POST' });
    notify(result.message || 'Done.');
    renderCoreProcess(result.process);
    if (result.core) {
      state.runtime = { ...state.runtime, core: result.core };
      renderRuntime();
    }
  } catch (error) {
    notifyFailure(`CORE ${action.toUpperCase()}`, `${error.code ? `${error.code}: ` : ''}${error.message}`, { code: error.code });
  } finally {
    button.disabled = false;
  }
}

async function saveManagementDefaults() {
  if (!state.managementDefaults || !capability('management_defaults_write')) {
    notify('Trade-management defaults are unavailable. No policy was changed.');
    return;
  }
  const payload = {
    manualMode: $('#manualDefaultMode').value,
    tradingviewMode: $('#tradingviewDefaultMode').value,
    tradingviewOwnershipMode: $('#tradingviewDefaultMode').value,
    manualManagementProfileId: state.managementDefaults.manualManagementProfileId,
    tradingviewManagementProfileId: state.managementDefaults.tradingviewManagementProfileId,
    manualAutosend: $('#manualAutosend').checked,
  };
  try {
    await api('/api/management/defaults', { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) });
    await api('/api/tradingview/ownership', { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mode: payload.tradingviewOwnershipMode, managementProfileId: payload.tradingviewManagementProfileId }) });
    notify('Defaults saved for future intents only. Existing trade-management instructions were not changed.');
    await loadOperatorState();
  } catch (error) {
    notify(`${error.code ? `${error.code}: ` : ''}${error.message}`);
  }
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.correlation) selectAlert(button.dataset.correlation);
});

$('#openSettings').addEventListener('click', () => {
  $('#settingsDialog').showModal();
  loadCoreProcess();
});
$('#refreshDashboard').addEventListener('click', async (event) => {
  event.target.disabled = true;
  try {
    await refresh({ force: true });
    notify('Dashboard and TWS core status refreshed.');
  } finally {
    event.target.disabled = false;
  }
});
for (const selector of ['#profileHost', '#profilePort', '#profileClientId', '#profileCapitalPerTrade']) {
  markDirtyOnInput(selector);
}
$('#clearOperatorErrors').addEventListener('click', () => {
  operatorErrors.length = 0;
  renderOperatorErrors();
});
$('#startCoreProcess').addEventListener('click', (event) => coreProcessAction('start', event.target));
$('#restartCoreProcess').addEventListener('click', (event) => coreProcessAction('restart', event.target));
$('#stopCoreProcess').addEventListener('click', (event) => coreProcessAction('stop', event.target));
$('#refreshCoreProcess').addEventListener('click', loadCoreProcess);
$('#closeSettings').addEventListener('click', () => {
  $('#settingsDialog').close();
});
$('#settingsDialog').addEventListener('click', (event) => {
  if (event.target === $('#settingsDialog')) $('#settingsDialog').close();
});

$('#flattenConfirmInput').addEventListener('input', (event) => {
  // Exact match, case-sensitive and untrimmed. The label says "Type FLATTEN";
  // accepting "flatten" or "  fLaTtEn  " made the friction decorative, and this
  // is the last gate before an irreversible market-hours sale of the whole
  // position.
  $('#confirmFlatten').disabled = event.target.value !== 'FLATTEN';
});
$('#cancelFlatten').addEventListener('click', () => {
  state.pendingFlatten = null;
  $('#flattenConfirmDialog').close();
});
$('#closeFlattenDialog').addEventListener('click', () => {
  state.pendingFlatten = null;
  $('#flattenConfirmDialog').close();
});
$('#flattenConfirmDialog').addEventListener('click', (event) => {
  if (event.target === $('#flattenConfirmDialog')) {
    state.pendingFlatten = null;
    $('#flattenConfirmDialog').close();
  }
});
$('#confirmFlatten').addEventListener('click', submitFlatten);

const closePartialModal = () => {
  state.pendingPartialClose = null;
  const dialog = $('#partialCloseConfirmDialog');
  if (dialog && typeof dialog.close === 'function') dialog.close();
};

const cancelPartialBtn = $('#cancelPartialClose');
if (cancelPartialBtn) cancelPartialBtn.addEventListener('click', closePartialModal);
const closePartialBtn = $('#closePartialDialog');
if (closePartialBtn) closePartialBtn.addEventListener('click', closePartialModal);
const partialModal = $('#partialCloseConfirmDialog');
if (partialModal) {
  partialModal.addEventListener('click', (event) => {
    if (event.target === partialModal) closePartialModal();
  });
}
const confirmPartialBtn = $('#confirmPartialClose');
if (confirmPartialBtn) {
  confirmPartialBtn.addEventListener('click', async () => {
    const pending = state.pendingPartialClose;
    closePartialModal();
    if (pending?.trade && pending?.quantity) {
      await submitPartialClose(pending.trade, pending.quantity);
    }
  });
}

$('#copyEndpoint').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('#webhookEndpoint').textContent);
    notify('Webhook endpoint copied. Expose only this path through a TLS edge.');
  } catch {
    notify('Clipboard access is unavailable. Copy the endpoint manually.');
  }
});
$('#refreshAlerts').addEventListener('click', refresh);
$('#refreshActiveTrades').addEventListener('click', loadOperatorState);
$('#profileSelect').addEventListener('change', async (event) => {
  state.activeProfileId = event.target.value || null;
  renderRuntime();
  renderProfiles();
  await activateProfile(state.activeProfileId);
});
$('#saveProfile').addEventListener('click', saveProfile);
$('#manualStrikeSelection').addEventListener('change', renderManualDesk);
$('#manualTradeForm').addEventListener('submit', reviewManualTrade);
$('#manualTradeForm').addEventListener('input', () => renderTradePreview(null));
$('#submitManualTrade').addEventListener('click', submitReviewedTrade);
$('#saveManagementDefaults').addEventListener('click', saveManagementDefaults);
$('#toggleManualTrade').addEventListener('click', () => {
  state.manualDeskExpanded = !state.manualDeskExpanded;
  renderManualDesk();
  if (state.manualDeskExpanded) $('#manualSymbol').focus();
});
$('#refreshClosedTrades').addEventListener('click', loadOperatorState);
$('#clearResolvedAttention').addEventListener('click', async () => {
  const clearable = state.alerts.filter((alert) => isClearableAttention(alert)
    && !state.attentionAcknowledgements.has(attentionAcknowledgementKey(alert)));
  const addedKeys = clearable.map(attentionAcknowledgementKey);
  for (const key of addedKeys) state.attentionAcknowledgements.add(key);
  if (!saveAttentionAcknowledgements()) {
    for (const key of addedKeys) state.attentionAcknowledgements.delete(key);
    renderAlerts();
    notify('Notices could not be acknowledged because local storage is unavailable. No notice or trading record was changed.');
    return;
  }
  renderAlerts();
  notify(`Acknowledged ${clearable.length} historical notice${clearable.length === 1 ? '' : 's'} on this device. Alert history was not deleted; active risks remain visible.`);
});
$('#clearAlertFilters').addEventListener('click', () => {
  $('#alertStateFilter').value = 'ALL';
  $('#alertSymbolFilter').value = '';
  ensureTodayAlertDate();
  renderAlerts();
});
for (const selector of ['#alertStateFilter', '#alertSymbolFilter', '#alertDateFilter']) {
  $(selector).addEventListener(selector === '#alertSymbolFilter' ? 'input' : 'change', renderAlerts);
}

function selectActiveTrade(correlationId) {
  const trade = state.activeTrades.find((item) => item.correlationId === correlationId);
  if (!trade) return;
  state.selectedTradeCorrelationId = correlationId;
  renderActiveTrades();
  document.querySelector('.active-trades-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  document.querySelector(`#activeTradeRows tr[data-trade-id="${CSS.escape(correlationId)}"]`)?.focus();
}
$('#activeTradeRows').addEventListener('click', (event) => {
  const row = event.target.closest('tr[data-trade-id]');
  if (!row) return;
  selectActiveTrade(row.dataset.tradeId || '');
});
$('#activeTradeRows').addEventListener('keydown', (event) => {
  if (!['Enter', ' '].includes(event.key)) return;
  // Only the row itself. Previously this matched the ancestor row even when
  // focus was on FLATTEN or PARTIAL CLOSE, and the preventDefault() below
  // suppressed the button's own Enter/Space activation -- leaving the two
  // destructive position controls reachable by mouse only (WCAG 2.1.1).
  if (event.target !== event.currentTarget && !event.target.matches('tr[data-trade-id]')) return;
  const row = event.target.closest('tr[data-trade-id]');
  if (!row) return;
  event.preventDefault();
  selectActiveTrade(row.dataset.tradeId || '');
});
document.addEventListener('click', (event) => {
  const attentionTrade = event.target.closest('button[data-trade-id]');
  if (attentionTrade?.dataset.tradeId) selectActiveTrade(attentionTrade.dataset.tradeId);
});

loadAttentionAcknowledgements();
ensureTodayAlertDate();
renderRuntime();
renderAlerts();
refresh();
setInterval(refresh, 3_000);
