'use strict';

// Unit tests for the pure logic in extension/src/overlay.js (command-text
// routing, status-line formatting). The imperative glue (Shadow DOM,
// chrome.runtime.connect, the physical keydown listener) is deliberately
// NOT unit tested here -- same rationale as tests/bridge.test.js's own
// doc comment -- and is instead exercised by the live smoke procedure.
// This mirrors bridge.test.js's/oracle.test.js's approach of loading real
// source into a sandboxed vm context rather than re-implementing the
// logic in the test.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = path.join(__dirname, '..');
const overlaySource = fs.readFileSync(
  path.join(repoRoot, 'extension', 'src', 'overlay.js'),
  'utf8',
);

function loadOverlayInternals() {
  // No `chrome`/`document` global in this sandbox -- overlay.js must stop
  // at its `typeof chrome === 'undefined'` guard (see the module's own
  // doc comment) before touching any DOM/chrome.runtime API, so this
  // needs no Shadow DOM/port mocking at all for the pure-function tests
  // below. `window` only needs to support the module's own re-install
  // guard (`window.__deepEyeOverlayInstalled`).
  const sandbox = { console, window: {} };
  vm.createContext(sandbox);
  vm.runInContext(overlaySource, sandbox, { filename: 'overlay.js' });
  return sandbox.__deepEyeOverlayInternals;
}

const internals = loadOverlayInternals();

// classifyInput()'s return value is constructed inside the vm sandbox
// realm; round-tripping through JSON (as tests/oracle.test.js's/
// bridge.test.js's own plain() does) gives a same-realm plain object so
// deepEqual compares structure only, not cross-realm prototype identity.
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

assert.equal(typeof internals, 'object', 'overlay.js must expose __deepEyeOverlayInternals for testing');
assert.deepEqual(Object.keys(internals).sort(), ['classifyInput', 'formatStatusLine']);

// ---------------------------------------------------------------------------
// classifyInput: ordinary text -> bot command; /... -> local; !... -> refused
// ---------------------------------------------------------------------------

{
  assert.deepEqual(plain(internals.classifyInput('pause')), { kind: 'bot_command', text: 'pause' });
  assert.deepEqual(plain(internals.classifyInput('mode farm')), { kind: 'bot_command', text: 'mode farm' });
}

{
  // Local overlay-only commands are stripped of their leading '/' and
  // trimmed, but the ordinary bot-command case above preserves the raw
  // text verbatim (the backend does its own trimming).
  assert.deepEqual(plain(internals.classifyInput('/clear')), { kind: 'local', text: 'clear' });
  assert.deepEqual(plain(internals.classifyInput('/help')), { kind: 'local', text: 'help' });
  assert.deepEqual(plain(internals.classifyInput('/  close  ')), { kind: 'local', text: 'close' });
}

{
  // A leading '!' must NEVER be sent to the bot or executed locally --
  // reserved/refused shell, unconditionally.
  assert.deepEqual(plain(internals.classifyInput('!rm -rf /')), { kind: 'shell_refused', text: '!rm -rf /' });
  assert.deepEqual(plain(internals.classifyInput('!')), { kind: 'shell_refused', text: '!' });
}

{
  assert.deepEqual(plain(internals.classifyInput('')), { kind: 'empty' });
  assert.deepEqual(plain(internals.classifyInput('   ')), { kind: 'empty' });
  assert.deepEqual(plain(internals.classifyInput('\t\n')), { kind: 'empty' });
}

{
  // Leading/trailing whitespace around ordinary text must not change
  // which channel it's routed to.
  assert.equal(internals.classifyInput('  pause  ').kind, 'bot_command');
}

// ---------------------------------------------------------------------------
// formatStatusLine: only renders fields the given status object actually
// carries -- never an invented field (see overlay.js's module doc
// comment: this file has no knowledge of any particular backend's
// game-specific fields).
// ---------------------------------------------------------------------------

{
  assert.equal(internals.formatStatusLine(null), 'bot: no status yet');
  assert.equal(internals.formatStatusLine(undefined), 'bot: no status yet');
}

{
  // A minimal/generic backend (e.g. overlay_dev_backend.py) sends only
  // connected/pausedByCommand/tickCount -- richer fields must simply be
  // absent from the rendered line, not fabricated.
  const line = internals.formatStatusLine({
    type: 'bot_status',
    connected: true,
    pausedByCommand: false,
    tickCount: 42,
  });
  assert.match(line, /connected/);
  assert.match(line, /running/);
  assert.match(line, /tick=42/);
  assert.doesNotMatch(line, /target=/);
  assert.doesNotMatch(line, /bullet=/);
}

{
  const line = internals.formatStatusLine({
    type: 'bot_status',
    connected: true,
    pausedByCommand: false,
    snapshotAgeS: 0.123,
    target: 'square@(900,450)',
    held: { moving: true, shooting: false },
    bulletSpeedPxS: 713.4,
    bulletSpeedConfidence: 0.91,
  });
  assert.match(line, /connected/);
  assert.match(line, /running/);
  assert.match(line, /target=square@\(900,450\)/);
  assert.match(line, /moving=true/);
  assert.match(line, /shooting=false/);
  assert.match(line, /age=0\.12s/);
  assert.match(line, /bullet=713px\/s \(0\.91\)/);
}

{
  const line = internals.formatStatusLine({
    type: 'bot_status',
    connected: false,
    pausedByCommand: true,
    snapshotAgeS: null,
    target: 'none',
    held: { moving: false, shooting: false },
    bulletSpeedPxS: null,
    bulletSpeedConfidence: null,
  });
  assert.match(line, /disconnected/);
  assert.match(line, /paused/);
  assert.doesNotMatch(line, /age=/, 'a null snapshotAgeS must not be rendered as a fabricated age');
  assert.doesNotMatch(line, /bullet=/, 'a null bulletSpeedPxS must not be rendered as a fabricated speed');
}

console.log('Overlay tests passed: command-text routing (bot/local/refused) and throttled status-line formatting.');
