'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.join(__dirname, '..');
const read = (relativePath) => fs.readFileSync(path.join(repoRoot, relativePath), 'utf8');

const manifest = JSON.parse(read('extension/manifest.json'));
assert.equal(manifest.manifest_version, 3);
assert.deepEqual(manifest.permissions.slice().sort(), ['clipboardWrite', 'scripting']);
assert.deepEqual(manifest.host_permissions, ['https://diep.io/*']);
// browser-overlay-control-v0: a second, ISOLATED-world content script
// (the overlay) is now expected alongside the original MAIN-world Oracle.
assert.equal(manifest.content_scripts.length, 2);
assert.deepEqual(manifest.content_scripts[0].matches, ['https://diep.io/*']);
assert.equal(manifest.content_scripts[0].world, 'MAIN');
assert.deepEqual(manifest.content_scripts[0].js, ['src/oracle.js']);
assert.deepEqual(manifest.content_scripts[1].matches, ['https://diep.io/*']);
assert.equal(manifest.content_scripts[1].world, 'ISOLATED');
assert.deepEqual(manifest.content_scripts[1].js, ['src/overlay.js']);
assert.equal(
  JSON.stringify(manifest).includes('diepAPI'),
  false,
  'the pinned vendor must not be a manifest-loaded runtime script',
);
// browser-informed-farming-v0: a background service worker is now expected
// (the bridge that forwards Oracle snapshots to the local agent process),
// but it must be exactly the reviewed bridge file -- nothing else.
assert.equal(manifest.background.service_worker, 'background/bridge.js');
assert.equal(Object.keys(manifest.background).length, 1, 'the background block must declare only service_worker');
assert.equal(JSON.stringify(manifest).includes('<all_urls>'), false);

const popupSource = read('extension/popup/popup.js');
assert.match(popupSource, /chrome\.scripting\.executeScript\(\{/);
assert.match(popupSource, /world:\s*'MAIN'/);
assert.match(popupSource, /location\.origin === 'https:\/\/diep\.io'/);
assert.match(popupSource, /oracle\?\.diagnostics\(\)/);
assert.match(popupSource, /copy-diagnostics/);
assert.match(popupSource, /diagnostics:\s*diag/);
assert.match(popupSource, /squareColorTopologyHistogram/);

const popupHtml = read('extension/popup/popup.html');
assert.match(popupHtml, /id="copy-diagnostics"/);
assert.match(popupHtml, /id="diag-fills-seen"/);
assert.match(popupHtml, /id="diag-accepted"/);
assert.match(popupHtml, /id="diag-rejected"/);
assert.match(popupHtml, /id="diag-cached"/);
assert.match(popupHtml, /id="diag-top-reasons"/);
assert.match(popupHtml, /id="diag-top-topology"/);

const oracleSource = read('extension/src/oracle.js');
for (const requiredToken of [
  'MAX_TRACKED_SUBPATHS',
  'topologySignature',
  'polygonGeometry',
  'selectMeaningfulSubpath',
  'isMeaningfulPolygon',
  'collapseCollinear',
  'perpendicularDistance',
  'evaluateAreaPerimeterConsistency',
  'GEOMETRY_EPSILON_PX',
  'AREA_PERIMETER_RATIO_TOLERANCE',
  'noMeaningfulSubpath',
  'ambiguousSubpaths',
  'areaPerimeterMismatch',
  // browser-informed-farming-v0: Triangle/Pentagon generalization -- one
  // shared classification pipeline keyed by color+corner-count, not three
  // copy-pasted per-shape detectors.
  'SHAPE_CLASSES',
  'VERTEX_COUNT_TO_CLASS',
  'colorMatchesClass',
  'recordClassDiagnostics',
  'RADIUS_RATIO_TOLERANCE',
]) {
  assert.ok(oracleSource.includes(requiredToken), `oracle.js is missing the subdivided-contour detector slice: ${requiredToken}`);
}
assert.equal(
  oracleSource.includes('normalizeClosedQuad'),
  false,
  'the old fixed-4/5-vertex special case must be replaced by the general collinear-collapse pipeline',
);
// squareColorSubpathCountHistogram/squareColorTopologyHistogram are no
// longer literal source tokens (built generically per class -- see
// SHAPE_CLASSES/recordClassDiagnostics above), but must still exist at
// runtime under those exact names for popup.js's backward-compatible
// reads; tests/oracle.test.js's diagnostics tests are the source of truth
// for that runtime behavior.

const bridgeSource = read('extension/background/bridge.js');
assert.match(bridgeSource, /new WebSocket\(/, 'the bridge must use a plain WebSocket client, not a hand-rolled protocol');
assert.match(bridgeSource, /chrome\.scripting\.executeScript\(\{/);
assert.match(bridgeSource, /world:\s*'MAIN'/);
assert.match(bridgeSource, /deepEyeOracle\.snapshot\(\)/);
assert.match(bridgeSource, /__deepEyeBridgeInternals/);
// The bridge must never send an inbound command INTO the page or the game
// -- it only forwards oracle.js's own read-only snapshot() output outward.
// Same read-only boundary as oracle.js/popup.js (see scripts/validate.ps1),
// just without the blanket WebSocket ban, since this file's whole job is a
// legitimate, reviewed, one-directional WebSocket telemetry export.
for (const forbiddenPattern of [
  /\bspawn\s*\(/, /\baimAt\s*\(/, /\blookAt\s*\(/, /\bshoot\s*\(/,
  /\bkeyDown\s*\(/, /\bkeyUp\s*\(/, /\bkeyPress\s*\(/, /\bmouse(?:Press)?\s*\(/,
  /\buseGamepad\s*\(/, /\bupgrade_(?:stat|tank)\s*\(/, /\bset_convar\s*\(/,
  /\binput\.execute\s*\(/, /\.send\(['"]/, /WebSocket\.prototype/,
]) {
  assert.doesNotMatch(bridgeSource, forbiddenPattern, `read-only boundary violation in bridge.js: ${forbiddenPattern}`);
}
assert.match(bridgeSource, /OVERLAY_PORT_NAME/, 'the overlay port relay must be present (browser-overlay-control-v0)');
assert.match(bridgeSource, /chrome\.runtime\.onConnect/);

// overlay.js (isolated-world content script) never touches
// window.deepEyeOracle/diepAPI and never opens its own WebSocket -- it
// relays exclusively through the reviewed chrome.runtime port above.
const overlaySource = read('extension/src/overlay.js');
assert.doesNotMatch(overlaySource, /deepEyeOracle/, 'overlay.js must never touch the page-context Oracle directly');
assert.doesNotMatch(overlaySource, /new WebSocket\(/, 'overlay.js must relay through the background port, not its own WebSocket');
assert.match(overlaySource, /chrome\.runtime\.connect\(/);
assert.match(overlaySource, /Backquote/, 'the toggle must key off KeyboardEvent.code, not the layout-dependent event.key');
for (const forbiddenPattern of [
  /\bspawn\s*\(/, /\baimAt\s*\(/, /\blookAt\s*\(/, /\bshoot\s*\(/,
  /\bkeyDown\s*\(/, /\bkeyUp\s*\(/, /\bkeyPress\s*\(/, /\bmouse(?:Press)?\s*\(/,
  /\buseGamepad\s*\(/, /\bupgrade_(?:stat|tank)\s*\(/, /\bset_convar\s*\(/,
  /\binput\.execute\s*\(/,
]) {
  assert.doesNotMatch(overlaySource, forbiddenPattern, `read-only boundary violation in overlay.js: ${forbiddenPattern}`);
}

const refreshSource = read('scripts/dev-refresh.ps1');
assert.match(refreshSource, /\[switch\]\$UpdateVendor/);
assert.match(refreshSource, /if \(\$UpdateVendor\) \{/);
assert.doesNotMatch(refreshSource, /SkipVendorUpdate/);
assert.doesNotMatch(refreshSource, /Stop-Process/i);
assert.doesNotMatch(refreshSource, /taskkill/i);
assert.doesNotMatch(refreshSource, /Start-Process/i);
assert.doesNotMatch(refreshSource, /CloseMainWindow/);
assert.doesNotMatch(refreshSource, /Win32_Process/);
assert.doesNotMatch(refreshSource, /ChromePath/);
assert.doesNotMatch(refreshSource, /chrome-dev-profile/);
assert.match(refreshSource, /not launch, close, or otherwise manage any Chrome process or profile/);
assert.match(refreshSource, /chrome:\/\/extensions/);

const readme = read('README.md');
for (const requiredStep of [
  'Open `https://diep.io/`',
  'Join a match and keep at least one neutral Square visible on screen',
  'Click **Copy Diagnostics**',
  'deepEyeOracle.diagnostics()',
  'kind: "neutral_square"',
  'memory access out of bounds',
  'Cazka/diepAPI#80',
  'unsupportedRectPath',
  'Closing-vertex normalization',
]) {
  assert.ok(readme.includes(requiredStep), `README smoke gate is missing: ${requiredStep}`);
}
assert.match(readme, /dev-refresh\.cmd -UpdateVendor/);
assert.match(readme, /without a network query or download/);
assert.match(readme, /not launch, close, or otherwise manage any Chrome process or profile/);
assert.match(readme, /has not been run yet for this slice/);
assert.match(readme, /rect: 8560/, 'the live-probe evidence backing the rect() hypothesis must be documented');

const ownedTextFiles = [
  'README.md',
  'THIRD_PARTY_NOTICES.md',
  'dev-refresh.cmd',
  'extension/manifest.json',
  'extension/src/oracle.js',
  'extension/src/overlay.js',
  'extension/background/bridge.js',
  'extension/popup/popup.css',
  'extension/popup/popup.html',
  'extension/popup/popup.js',
  'scripts/dev-refresh.ps1',
  'scripts/update-vendor.ps1',
  'scripts/validate.ps1',
  'tests/oracle.test.js',
  'tests/bridge.test.js',
  'tests/overlay.test.js',
];
const mojibakePattern = /\uFFFD|\u00C3.|\u00C2.|\u00E2(?:\u20AC|\u2122)|\u0432\u0402/u;
for (const relativePath of ownedTextFiles) {
  assert.doesNotMatch(read(relativePath), mojibakePattern, `mojibake found in ${relativePath}`);
}

console.log('Repository tests passed: manifest, MAIN-world bridge, background bridge boundary, pinned refresh, process safety, and docs.');
