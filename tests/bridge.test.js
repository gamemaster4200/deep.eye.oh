'use strict';

// Unit tests for the pure logic in extension/background/bridge.js. The
// imperative glue (real chrome.tabs/chrome.scripting/WebSocket) is
// deliberately NOT unit tested here -- see createBridge's doc comment --
// and is instead exercised by the live smoke procedure. This mirrors
// tests/oracle.test.js's approach of loading real source into a sandboxed
// vm context rather than re-implementing the logic in the test.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = path.join(__dirname, '..');
const bridgeSource = fs.readFileSync(
  path.join(repoRoot, 'extension', 'background', 'bridge.js'),
  'utf8',
);

function loadBridgeInternals() {
  // No `chrome` global in this sandbox -- createBridge()/start() must not
  // be auto-invoked as a side effect of merely loading the source (see
  // bridge.js's guard), so this needs no WebSocket/setTimeout mocking at
  // all for the pure-function tests below.
  const sandbox = { console };
  vm.createContext(sandbox);
  vm.runInContext(bridgeSource, sandbox, { filename: 'bridge.js' });
  return sandbox.__deepEyeBridgeInternals;
}

const internals = loadBridgeInternals();

// buildOutboundMessage() constructs its return value inside the vm sandbox
// realm; round-tripping through JSON (as tests/oracle.test.js's plain()
// does) gives a same-realm plain object so deepEqual compares structure
// only, not cross-realm prototype identity.
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

assert.equal(typeof internals, 'object', 'bridge.js must expose __deepEyeBridgeInternals for testing');
assert.deepEqual(
  Object.keys(internals).sort(),
  [
    'DEFAULT_BRIDGE_PORT', 'POLL_INTERVAL_MS', 'bridgeUrl', 'buildOutboundMessage',
    'createBridge', 'nextReconnectDelayMs', 'pickTargetTab',
  ],
);

// ---------------------------------------------------------------------------
// buildOutboundMessage
// ---------------------------------------------------------------------------

{
  const snapshot = { shapes: [{ class: 'square', cx: 1, cy: 2 }] };
  const message = plain(internals.buildOutboundMessage(snapshot, 42, 12345));
  assert.deepEqual(message, {
    type: 'oracle_snapshot', tabId: 42, polledAtMs: 12345, snapshot,
  });
  assert.doesNotThrow(() => JSON.stringify(message), 'the outbound message must be JSON-safe');
}

// ---------------------------------------------------------------------------
// nextReconnectDelayMs: exponential backoff, floored and capped
// ---------------------------------------------------------------------------

{
  assert.equal(internals.nextReconnectDelayMs(0), 1000);
  assert.equal(internals.nextReconnectDelayMs(1), 2000);
  assert.equal(internals.nextReconnectDelayMs(2), 4000);
  assert.equal(internals.nextReconnectDelayMs(3), 8000, 'must be capped at MAX_RECONNECT_DELAY_MS');
  assert.equal(internals.nextReconnectDelayMs(10), 8000, 'must stay capped for large attempt counts');
  assert.equal(internals.nextReconnectDelayMs(-1), 1000, 'a negative attempt count must not underflow the floor');
}

// ---------------------------------------------------------------------------
// pickTargetTab
// ---------------------------------------------------------------------------

{
  assert.equal(internals.pickTargetTab([]), null);
  assert.equal(internals.pickTargetTab(undefined), null);
}

{
  // The active tab (the one the operator is looking at, and the one
  // Controller will actually be arming against) must be preferred over
  // any other matching tab.
  const tabs = [
    { id: 1, active: false, url: 'https://diep.io/' },
    { id: 2, active: true, url: 'https://diep.io/' },
    { id: 3, active: false, url: 'https://diep.io/' },
  ];
  assert.deepEqual(internals.pickTargetTab(tabs), tabs[1]);
}

{
  // With no active tab, fall back deterministically (lowest id) rather
  // than whatever order chrome.tabs.query happened to return.
  const tabs = [
    { id: 30, active: false, url: 'https://diep.io/' },
    { id: 5, active: false, url: 'https://diep.io/' },
    { id: 17, active: false, url: 'https://diep.io/' },
  ];
  assert.deepEqual(internals.pickTargetTab(tabs), tabs[1]);
}

// ---------------------------------------------------------------------------
// bridgeUrl / defaults
// ---------------------------------------------------------------------------

{
  assert.equal(internals.bridgeUrl(8765), 'ws://127.0.0.1:8765/');
  assert.equal(internals.bridgeUrl(internals.DEFAULT_BRIDGE_PORT), 'ws://127.0.0.1:8765/');
  assert.equal(internals.DEFAULT_BRIDGE_PORT, 8765);
  assert.ok(internals.POLL_INTERVAL_MS > 0 && internals.POLL_INTERVAL_MS <= 250, 'poll interval must be fast enough for a live farming loop');
}

// ---------------------------------------------------------------------------
// createBridge: must not touch chrome.*/WebSocket merely by being
// constructed (only start() may) -- constructing it is side-effect-free.
// ---------------------------------------------------------------------------

{
  const bridge = internals.createBridge(9999);
  assert.equal(typeof bridge.start, 'function');
  assert.equal(typeof bridge.stop, 'function');
  // stop() before start() must be a safe no-op, not throw.
  assert.doesNotThrow(() => bridge.stop());
}

console.log('Bridge tests passed: outbound message shape, reconnect backoff, tab selection, and URL construction.');
