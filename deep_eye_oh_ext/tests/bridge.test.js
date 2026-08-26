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
    'BRIDGE_PROTOCOL_VERSION', 'CAPABILITIES', 'DEFAULT_BRIDGE_PORT', 'OVERLAY_PORT_NAME', 'POLL_INTERVAL_MS',
    'bridgeUrl', 'buildBridgeHelloMessage', 'buildLifecycleSnapshotMessage', 'buildOutboundMessage',
    'buildOverlayOutboundMessage', 'createBridge', 'nextReconnectDelayMs', 'parseLifecycleConfigMessage',
    'parseOverlayPushMessage', 'pickTargetTab',
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
// buildBridgeHelloMessage / buildLifecycleSnapshotMessage (browser-lifecycle-v0)
// ---------------------------------------------------------------------------

{
  const hello = plain(internals.buildBridgeHelloMessage());
  assert.deepEqual(hello, {
    type: 'bridge_hello',
    protocolVersion: 1,
    capabilities: ['oracle_snapshot', 'lifecycle_v0'],
  });
  assert.ok(hello.capabilities.includes('oracle_snapshot'));
  assert.ok(hello.capabilities.includes('lifecycle_v0'));
  assert.doesNotThrow(() => JSON.stringify(hello), 'bridge_hello must be JSON-safe');
}

{
  const snapshot = { state: 'LOBBY', reason: 'home_screen_ready', selectedMode: 'ffa' };
  const message = plain(internals.buildLifecycleSnapshotMessage(7, 12345, snapshot));
  assert.deepEqual(message, {
    type: 'lifecycle_snapshot', tabId: 7, observedAtMs: 12345, snapshot,
  });
  assert.doesNotThrow(() => JSON.stringify(message), 'lifecycle_snapshot must be JSON-safe');
  // Must never carry full DOM fragments/HTML -- see module doc comment.
  assert.equal(JSON.stringify(message).includes('<'), false);
}

// ---------------------------------------------------------------------------
// parseLifecycleConfigMessage: the ONLY inbound message type ever accepted
// ---------------------------------------------------------------------------

{
  const valid = plain(internals.parseLifecycleConfigMessage({
    type: 'lifecycle_config', playerName: 'deep.eye.oh', gameMode: 'ffa',
  }));
  assert.deepEqual(valid, { playerName: 'deep.eye.oh', gameMode: 'ffa' });
}

{
  // Wrong/unknown type -- e.g. an attempted arbitrary command payload --
  // must be rejected, not partially accepted.
  for (const bad of [
    null, undefined, 'not an object', 42,
    { type: 'oracle_snapshot', playerName: 'x', gameMode: 'ffa' },
    { type: 'run_command', command: 'rm -rf /' },
    { type: 'lifecycle_config' }, // missing fields
    { type: 'lifecycle_config', playerName: 123, gameMode: 'ffa' },
    { type: 'lifecycle_config', playerName: 'x', gameMode: 456 },
    { type: 'lifecycle_config', playerName: '', gameMode: 'ffa' },
    { type: 'lifecycle_config', playerName: 'x', gameMode: '' },
  ]) {
    assert.equal(internals.parseLifecycleConfigMessage(bad), null, `must reject: ${JSON.stringify(bad)}`);
  }
}

// ---------------------------------------------------------------------------
// buildOverlayOutboundMessage / parseOverlayPushMessage (overlay-control-center-v0)
// ---------------------------------------------------------------------------

{
  const message = plain(internals.buildOverlayOutboundMessage({ type: 'overlay_command', text: 'pause' }, 7, 12345));
  assert.deepEqual(message, { type: 'overlay_command', tabId: 7, sentAtMs: 12345, text: 'pause' });
}

{
  const message = plain(internals.buildOverlayOutboundMessage({ type: 'overlay_focus', focused: true }, 7, 12345));
  assert.deepEqual(message, { type: 'overlay_focus', tabId: 7, sentAtMs: 12345, focused: true });
}

{
  // Anything that isn't exactly one of the two known port-message shapes
  // must never be forwarded onto the WebSocket -- this is the entire
  // point of the "narrow, explicit, reviewed exception", not a general
  // message bus.
  const cases = [
    null,
    undefined,
    {},
    { type: 'overlay_command' }, // missing text
    { type: 'overlay_command', text: 42 }, // wrong type
    { type: 'overlay_focus' }, // missing focused
    { type: 'overlay_focus', focused: 'yes' }, // wrong type
    { type: 'oracle_snapshot', snapshot: {} }, // wrong channel entirely
    { type: 'shell_command', text: 'rm -rf /' },
  ];
  for (const raw of cases) {
    assert.equal(internals.buildOverlayOutboundMessage(raw, 1, 0), null, `must reject ${JSON.stringify(raw)}`);
  }
}

{
  assert.deepEqual(
    internals.parseOverlayPushMessage({ type: 'bot_status', connected: true }),
    { type: 'bot_status', connected: true },
  );
  assert.deepEqual(
    internals.parseOverlayPushMessage({ type: 'overlay_command_result', text: 'pause', status: 'ok', message: 'bot paused' }),
    { type: 'overlay_command_result', text: 'pause', status: 'ok', message: 'bot paused' },
  );
  assert.deepEqual(
    internals.parseOverlayPushMessage({ type: 'overlay_key_event', kind: 'char', value: 'w' }),
    { type: 'overlay_key_event', kind: 'char', value: 'w' },
  );
}

{
  // Anything other than the three known overlay-outbound shapes
  // (including the pre-existing lifecycle_config/oracle_snapshot
  // channels) must never be forwarded onto the overlay port.
  for (const bad of [
    null, undefined, 'not an object', 42,
    { type: 'lifecycle_config', playerName: 'x', gameMode: 'ffa' },
    { type: 'oracle_snapshot' },
    { type: 'unknown_type' },
  ]) {
    assert.equal(internals.parseOverlayPushMessage(bad), null, `must reject ${JSON.stringify(bad)}`);
  }
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
  assert.equal(typeof bridge.acceptOverlayPort, 'function');
  // stop() before start() must be a safe no-op, not throw.
  assert.doesNotThrow(() => bridge.stop());
}

{
  assert.equal(internals.OVERLAY_PORT_NAME, 'deepEyeOverlay');
}

console.log('Bridge tests passed: outbound message shape, overlay port relay, reconnect backoff, tab selection, and URL construction.');
