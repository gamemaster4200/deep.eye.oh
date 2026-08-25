'use strict';

// Unit tests for the pure logic in extension/src/lifecycle.js. The
// imperative glue (real document/chrome.runtime) is deliberately NOT unit
// tested here -- see startLifecyclePolling's doc comment -- and is instead
// exercised by the live smoke procedure. Mirrors tests/bridge.test.js's/
// tests/oracle.test.js's approach of loading real source into a sandboxed
// vm context and exercising exported pure functions against small fake
// DOM-like objects, rather than pulling in jsdom for a whole content
// script.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = path.join(__dirname, '..');
const lifecycleSource = fs.readFileSync(
  path.join(repoRoot, 'extension', 'src', 'lifecycle.js'),
  'utf8',
);

function loadLifecycleInternals() {
  // No `chrome`/`document`/`window` globals in this sandbox -- the auto-
  // start guard at the bottom of lifecycle.js must not fire merely from
  // loading the source (mirrors bridge.js's createBridge()/start() guard).
  const sandbox = { console };
  vm.createContext(sandbox);
  vm.runInContext(lifecycleSource, sandbox, { filename: 'lifecycle.js' });
  return sandbox.__deepEyeLifecycleInternals;
}

const internals = loadLifecycleInternals();

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

assert.equal(typeof internals, 'object', 'lifecycle.js must expose __deepEyeLifecycleInternals for testing');
assert.deepEqual(
  Object.keys(internals).sort(),
  [
    'ACTION_COOLDOWN_MS', 'CAPTCHA_IFRAME_SELECTOR', 'LIFECYCLE_STATES',
    'buildLifecycleSnapshotMessage', 'classifyLifecycle', 'classifyState',
    'collectSignals', 'hasClass', 'isVisible', 'planAction',
  ],
);

const S = internals.LIFECYCLE_STATES;

// --- Fake DOM helpers -------------------------------------------------------

function fakeEl({ className = '', disabled = false, readOnly = false, value = null, rect = { width: 100, height: 20 }, attrs = {} } = {}) {
  return {
    className,
    disabled,
    readOnly,
    value,
    getBoundingClientRect: () => rect,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
  };
}

// Builds a fake `document` from a plain map of id -> fakeEl, plus a
// queryMap for the handful of querySelector() calls collectSignals makes.
function fakeDoc({ byId = {}, query = {} } = {}) {
  return {
    getElementById: (id) => byId[id] ?? null,
    querySelector: (sel) => query[sel] ?? null,
  };
}

// ---------------------------------------------------------------------------
// hasClass / isVisible
// ---------------------------------------------------------------------------

{
  assert.equal(internals.hasClass(fakeEl({ className: 'screen active' }), 'active'), true);
  assert.equal(internals.hasClass(fakeEl({ className: 'screen' }), 'active'), false);
  assert.equal(internals.hasClass(null, 'active'), false);
  assert.equal(internals.hasClass(fakeEl({ className: 'screen active loading-error' }), 'loading-error'), true);
}

{
  assert.equal(internals.isVisible(fakeEl({ rect: { width: 100, height: 40 } })), true);
  assert.equal(internals.isVisible(fakeEl({ rect: { width: 0, height: 0 } })), false);
  assert.equal(internals.isVisible(null), false);
}

// ---------------------------------------------------------------------------
// classifyState: precedence (CAPTCHA_REQUIRED > DEAD > LOBBY >
// ENTERING_GAME > PLAYING > LOADING > UNKNOWN) and "never PLAYING merely
// because no known menu is visible"
// ---------------------------------------------------------------------------

function baseSignals(overrides = {}) {
  return {
    screenHolderPresent: true,
    anyScreenActive: false,
    homeActive: false,
    homeLoadingError: false,
    gameOverActive: false,
    inGameActive: false,
    loadingActive: false,
    spawnButtonDisabled: true,
    captchaVisible: false,
    ...overrides,
  };
}

{
  // CAPTCHA takes precedence over every other concurrently-true signal.
  const signals = baseSignals({ captchaVisible: true, homeActive: true, gameOverActive: true });
  assert.equal(internals.classifyState(signals).state, S.CAPTCHA_REQUIRED);
}

{
  const signals = baseSignals({ gameOverActive: true, anyScreenActive: true });
  assert.equal(internals.classifyState(signals).state, S.DEAD);
}

{
  const signals = baseSignals({ homeActive: true, anyScreenActive: true, spawnButtonDisabled: false });
  const result = internals.classifyState(signals);
  assert.equal(result.state, S.LOBBY);
  assert.equal(result.reason, 'home_screen_ready');
}

{
  // Not-yet-actionable lobby (initial "Connecting..." OR the live-observed
  // "Kicked due to inactivity" home-screen.loading-error case) must still
  // classify as LOBBY, distinguished only by `reason` -- never a separate
  // top-level state, and never treated as ready-to-click.
  const notReady = internals.classifyState(baseSignals({ homeActive: true, spawnButtonDisabled: true }));
  assert.equal(notReady.state, S.LOBBY);
  assert.equal(notReady.reason, 'home_screen_not_ready');

  const connectionError = internals.classifyState(
    baseSignals({ homeActive: true, spawnButtonDisabled: true, homeLoadingError: true }),
  );
  assert.equal(connectionError.state, S.LOBBY);
  assert.equal(connectionError.reason, 'home_screen_connection_error');
}

{
  // The observed transient gap: screen-holder exists, but the exclusive
  // `.screen.active` flag is on nothing right now.
  const signals = baseSignals({ anyScreenActive: false });
  assert.equal(internals.classifyState(signals).state, S.ENTERING_GAME);
}

{
  const signals = baseSignals({ inGameActive: true, anyScreenActive: true });
  assert.equal(internals.classifyState(signals).state, S.PLAYING);
}

{
  const signals = baseSignals({ loadingActive: true, anyScreenActive: true });
  assert.equal(internals.classifyState(signals).state, S.LOADING);
}

{
  // No screen-holder at all (e.g. document_start, before diep.io's own DOM
  // has been built yet) -- must be UNKNOWN, never guessed as anything else.
  const signals = baseSignals({ screenHolderPresent: false, anyScreenActive: false });
  assert.equal(internals.classifyState(signals).state, S.UNKNOWN);
}

// ---------------------------------------------------------------------------
// collectSignals / classifyLifecycle: end-to-end against a fake DOM
// ---------------------------------------------------------------------------

{
  const doc = fakeDoc({
    byId: {
      'screen-holder': fakeEl(),
      'home-screen': fakeEl({ className: 'screen active' }),
      'game-over-screen': fakeEl({ className: 'screen' }),
      'in-game-screen': fakeEl({ className: 'screen' }),
      'loading-screen': fakeEl({ className: 'screen' }),
      'spawn-button': fakeEl({ disabled: false }),
      'spawn-nickname': fakeEl({ value: 'deep.eye.oh', readOnly: false }),
    },
    query: {
      '#gamemode-selector .selected': fakeEl({ attrs: { 'data-value': 'ffa' } }),
    },
  });
  const result = internals.classifyLifecycle(doc);
  assert.equal(result.state, S.LOBBY);
  assert.equal(result.reason, 'home_screen_ready');
  assert.equal(result.selectedMode, 'ffa');
}

{
  // A visible Turnstile iframe anywhere in the DOM is authoritative,
  // regardless of which .screen happens to be active underneath it.
  const doc = fakeDoc({
    byId: {
      'screen-holder': fakeEl(),
      'home-screen': fakeEl({ className: 'screen active' }),
      'spawn-button': fakeEl({ disabled: true }),
    },
    query: {
      [internals.CAPTCHA_IFRAME_SELECTOR]: fakeEl({ rect: { width: 300, height: 65 } }),
    },
  });
  assert.equal(internals.classifyLifecycle(doc).state, S.CAPTCHA_REQUIRED);
}

{
  // An iframe present in the DOM but not visible (0x0, or Turnstile's
  // detached/pre-render state) must NOT trigger CAPTCHA_REQUIRED --
  // detection is on the rendered, visible widget, not mere DOM presence.
  const doc = fakeDoc({
    byId: {
      'screen-holder': fakeEl(),
      'home-screen': fakeEl({ className: 'screen active' }),
      'spawn-button': fakeEl({ disabled: false }),
    },
    query: {
      [internals.CAPTCHA_IFRAME_SELECTOR]: fakeEl({ rect: { width: 0, height: 0 } }),
      '#gamemode-selector .selected': fakeEl({ attrs: { 'data-value': 'ffa' } }),
    },
  });
  assert.equal(internals.classifyLifecycle(doc).state, S.LOBBY);
}

// ---------------------------------------------------------------------------
// planAction: at most one permitted action, CAPTCHA/UNKNOWN -> no action
// ---------------------------------------------------------------------------

function classification(state, signals) {
  return { state, reason: 'test', selectedMode: signals.selectedMode ?? null, signals };
}

const CONFIG = { playerName: 'deep.eye.oh', gameMode: 'ffa' };

{
  // CAPTCHA_REQUIRED -> never act, regardless of config/signals.
  const c = classification(S.CAPTCHA_REQUIRED, { nicknameValue: 'wrong', selectedMode: 'teams' });
  assert.equal(internals.planAction(c, CONFIG, 1000, null), null);
}

{
  // UNKNOWN -> never act.
  const c = classification(S.UNKNOWN, {});
  assert.equal(internals.planAction(c, CONFIG, 1000, null), null);
}

{
  // No config yet from Python -- must never guess a name/mode of its own.
  const c = classification(S.LOBBY, { nicknameValue: '', selectedMode: null, spawnButtonDisabled: false });
  assert.equal(internals.planAction(c, null, 1000, null), null);
}

{
  // LOBBY, name not yet set -> set_name first (name before mode before play).
  const c = classification(S.LOBBY, {
    nicknameValue: '', nicknameReadonly: false, selectedMode: 'ffa', spawnButtonDisabled: false,
  });
  const action = plain(internals.planAction(c, CONFIG, 1000, null));
  assert.deepEqual(action, { type: 'set_name', value: 'deep.eye.oh' });
}

{
  // Readonly nickname (e.g. still connecting) -- must not attempt to set it.
  const c = classification(S.LOBBY, {
    nicknameValue: '', nicknameReadonly: true, selectedMode: 'ffa', spawnButtonDisabled: true,
  });
  assert.equal(internals.planAction(c, CONFIG, 1000, null), null);
}

{
  // Name already correct, mode wrong, dropdown closed -> open it first.
  const c = classification(S.LOBBY, {
    nicknameValue: 'deep.eye.oh', nicknameReadonly: false, selectedMode: 'teams',
    modeDropdownOpen: false, spawnButtonDisabled: false,
  });
  assert.deepEqual(plain(internals.planAction(c, CONFIG, 1000, null)), { type: 'open_mode_dropdown' });
}

{
  // Name correct, mode wrong, dropdown open -> select the configured mode.
  const c = classification(S.LOBBY, {
    nicknameValue: 'deep.eye.oh', nicknameReadonly: false, selectedMode: 'teams',
    modeDropdownOpen: true, spawnButtonDisabled: false,
  });
  assert.deepEqual(plain(internals.planAction(c, CONFIG, 1000, null)), { type: 'select_mode', value: 'ffa' });
}

{
  // Name + mode correct, button enabled -> click_play.
  const c = classification(S.LOBBY, {
    nicknameValue: 'deep.eye.oh', nicknameReadonly: false, selectedMode: 'ffa', spawnButtonDisabled: false,
  });
  assert.deepEqual(plain(internals.planAction(c, CONFIG, 1000, null)), { type: 'click_play' });
}

{
  // Name + mode correct, button still disabled (still connecting) -> wait, no action.
  const c = classification(S.LOBBY, {
    nicknameValue: 'deep.eye.oh', nicknameReadonly: false, selectedMode: 'ffa', spawnButtonDisabled: true,
  });
  assert.equal(internals.planAction(c, CONFIG, 1000, null), null);
}

{
  // DEAD -> click_respawn, unconditionally on state alone.
  const c = classification(S.DEAD, {});
  assert.deepEqual(plain(internals.planAction(c, CONFIG, 1000, null)), { type: 'click_respawn' });
}

{
  // LOADING/ENTERING_GAME/PLAYING -> never act.
  for (const state of [S.LOADING, S.ENTERING_GAME, S.PLAYING]) {
    const c = classification(state, { nicknameValue: 'wrong', spawnButtonDisabled: false });
    assert.equal(internals.planAction(c, CONFIG, 1000, null), null, `must not act during ${state}`);
  }
}

// ---------------------------------------------------------------------------
// planAction: debounce/cooldown and idempotence
// ---------------------------------------------------------------------------

{
  const c = classification(S.DEAD, {});
  // First call (no prior action) -> acts.
  assert.deepEqual(plain(internals.planAction(c, CONFIG, 1000, null)), { type: 'click_respawn' });
  // Immediately again, within the cooldown window -> no repeated action.
  assert.equal(internals.planAction(c, CONFIG, 1000 + 10, 1000), null);
  // Same signals, but well past the cooldown -> still idempotent: DEAD's
  // plan is deterministic (always click_respawn while DEAD), so this is
  // expected to act again -- lifecycle.js's own reported state (not
  // planAction) is what changes once the click actually takes effect.
  assert.deepEqual(
    plain(internals.planAction(c, CONFIG, 1000 + internals.ACTION_COOLDOWN_MS + 1, 1000)),
    { type: 'click_respawn' },
  );
}

{
  // Signals that already match config -> no repeated set_name/select_mode
  // even with no cooldown in effect (idempotence on already-satisfied state).
  const c = classification(S.LOBBY, {
    nicknameValue: 'deep.eye.oh', nicknameReadonly: false, selectedMode: 'ffa', spawnButtonDisabled: true,
  });
  assert.equal(internals.planAction(c, CONFIG, 5000, null), null);
}

// ---------------------------------------------------------------------------
// buildLifecycleSnapshotMessage: JSON-safe, no DOM fragments/HTML
// ---------------------------------------------------------------------------

{
  const c = classification(S.LOBBY, { selectedMode: 'ffa' });
  const message = plain(internals.buildLifecycleSnapshotMessage(c, null, 555));
  assert.deepEqual(message, {
    type: 'lifecycle_snapshot',
    observedAtMs: 555,
    snapshot: { state: 'LOBBY', reason: 'test', selectedMode: 'ffa' },
  });
  assert.doesNotThrow(() => JSON.stringify(message));
  assert.equal(JSON.stringify(message).includes('<'), false, 'must never carry HTML/DOM fragments');
}

console.log('Lifecycle tests passed: state classification/precedence, CAPTCHA/UNKNOWN/LOBBY/DEAD action planning, debounce/idempotence, and snapshot message shape.');
