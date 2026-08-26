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
// overlay-control-center-v0: a third content script (overlay.js,
// ISOLATED world) is now expected alongside oracle.js (MAIN world,
// unchanged, strictly read-only) and lifecycle.js (ISOLATED).
assert.equal(manifest.content_scripts.length, 3, 'exactly three content scripts: oracle.js (MAIN), lifecycle.js (ISOLATED), overlay.js (ISOLATED)');

const oracleScript = manifest.content_scripts.find((cs) => cs.js.includes('src/oracle.js'));
assert.ok(oracleScript, 'oracle.js content script entry must exist');
assert.deepEqual(oracleScript.matches, ['https://diep.io/*']);
assert.equal(oracleScript.world, 'MAIN');
assert.deepEqual(oracleScript.js, ['src/oracle.js']);
assert.equal(oracleScript.run_at, 'document_start');

const lifecycleScript = manifest.content_scripts.find((cs) => cs.js.includes('src/lifecycle.js'));
assert.ok(lifecycleScript, 'lifecycle.js content script entry must exist');
assert.deepEqual(lifecycleScript.matches, ['https://diep.io/*']);
assert.equal(lifecycleScript.world, 'ISOLATED', 'lifecycle.js must run in the isolated world, never MAIN');
assert.deepEqual(lifecycleScript.js, ['src/lifecycle.js']);
assert.equal(lifecycleScript.run_at, 'document_start');

const overlayScript = manifest.content_scripts.find((cs) => cs.js.includes('src/overlay.js'));
assert.ok(overlayScript, 'overlay.js content script entry must exist');
assert.deepEqual(overlayScript.matches, ['https://diep.io/*']);
assert.equal(overlayScript.world, 'ISOLATED', 'overlay.js must run in the isolated world, never MAIN');
assert.deepEqual(overlayScript.js, ['src/overlay.js']);

assert.equal(
  JSON.stringify(manifest).includes('diepAPI'),
  false,
  'the pinned vendor must not be a manifest-loaded runtime script',
);
// Python remains the canonical config owner (see browser_lifecycle.py) --
// no duplicate independent lifecycle config store in chrome.storage.
assert.equal(JSON.stringify(manifest).includes('storage'), false, 'no chrome.storage permission for lifecycle config');
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
  // browser-lifecycle-v0 live-smoke fix: the canvas backing-store rect
  // alone is not enough to compute a correct screen-space transform --
  // the browser's own chrome (tab strip/omnibox/infobar) must also be
  // reported, or Python computes screen points landing in the browser's
  // own UI instead of the game (see browser_game_state.py).
  'browserChromeOffsetCss',
  'browserChromeWidthCss',
  'browserChromeHeightCss',
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
// browser-lifecycle-v0: the bridge accepts back exactly one inbound
// message TYPE from Python (`lifecycle_config`) -- never a generic
// selector/JS/shell-command/URL/action payload, never eval, never
// remotely-fetched/executed code.
assert.match(bridgeSource, /buildBridgeHelloMessage/);
assert.match(bridgeSource, /parseLifecycleConfigMessage/);
assert.match(bridgeSource, /'lifecycle_config'/);
for (const forbiddenPattern of [
  /\beval\s*\(/, /\bnew Function\s*\(/, /\bimportScripts\s*\(/,
  /\bexecuteScript\s*\(\s*\{\s*[^}]*func\s*:\s*[^,}]*message/is,
]) {
  assert.doesNotMatch(bridgeSource, forbiddenPattern, `no remote-code-execution pattern allowed in bridge.js: ${forbiddenPattern}`);
}
// overlay-control-center-v0: the overlay port relay must be present, and
// still only relay the two known inbound shapes / three known outbound
// types -- see bridge.js's own doc comment.
assert.match(bridgeSource, /OVERLAY_PORT_NAME/, 'the overlay port relay must be present (overlay-control-center-v0)');
assert.match(bridgeSource, /chrome\.runtime\.onConnect/);
assert.match(bridgeSource, /buildOverlayOutboundMessage/);
assert.match(bridgeSource, /parseOverlayPushMessage/);
// A WebSocket close must tell an active overlay port about it (never
// silently leave the overlay's own focused/open state stuck true forever
// -- live-smoke-found regression, see overlay.js's 'bridge_disconnected'
// handler).
assert.match(bridgeSource, /'bridge_disconnected'/);

const lifecycleSource = read('extension/src/lifecycle.js');
assert.match(lifecycleSource, /__deepEyeLifecycleInternals/);
assert.match(lifecycleSource, /CAPTCHA_REQUIRED/);
assert.match(lifecycleSource, /challenges\.cloudflare\.com/, 'CAPTCHA detection must be grounded in the real Turnstile iframe origin');
assert.match(lifecycleSource, /never interacts with CAPTCHA controls/i);
// Gameplay input (movement/aim/shoot/upgrade) and CAPTCHA interaction of
// any kind must never appear in lifecycle.js -- it may only touch known
// pre-game/lobby/death UI (name/mode/start/respawn). Real OS-level
// gameplay input stays exclusively in Python's Controller (control.py).
for (const forbiddenPattern of [
  /\bmoveTank\s*\(/, /\baim\s*\(/, /\bshoot\s*\(/, /\bupgrade\s*\(/,
  /\bKeyboardEvent\s*\(/, /\bsendKey/i, /\bWASD\b/,
  /turnstile\.(?:execute|reset|render)/, /solveCaptcha/i, /bypassCaptcha/i,
  /\beval\s*\(/, /\bnew Function\s*\(/,
]) {
  assert.doesNotMatch(lifecycleSource, forbiddenPattern, `gameplay/CAPTCHA boundary violation in lifecycle.js: ${forbiddenPattern}`);
}
// No generic "click anything that looks like X" rule -- every applyAction
// branch must target an exact, known element id/selector, not a text-
// content or role-based blind search.
assert.doesNotMatch(lifecycleSource, /querySelectorAll\(['"]button['"]\)/);
assert.doesNotMatch(lifecycleSource, /textContent.*includes\(['"]close['"]/i);

// overlay.js (isolated-world content script) never touches
// window.deepEyeOracle/diepAPI and never opens its own WebSocket -- it
// relays exclusively through the reviewed chrome.runtime port above, and
// never contains a gameplay-control primitive (same $forbiddenPattern set
// every other runtime source is checked against).
const overlaySource = read('extension/src/overlay.js');
assert.doesNotMatch(overlaySource, /deepEyeOracle/, 'overlay.js must never touch the page-context Oracle directly');
assert.doesNotMatch(overlaySource, /new WebSocket\(/, 'overlay.js must relay through the background port, not its own WebSocket');
assert.match(overlaySource, /chrome\.runtime\.connect\(/);
assert.match(overlaySource, /Backquote/, 'the toggle must key off KeyboardEvent.code, not the layout-dependent event.key');
assert.match(overlaySource, /__deepEyeOverlayInternals/);
assert.match(overlaySource, /shell_refused/, "a leading '!' must be classified as refused, never executed");
assert.match(overlaySource, /'bridge_disconnected'/, "the overlay must reset its own focused/open state when the bridge WebSocket drops");
for (const forbiddenPattern of [
  /\bspawn\s*\(/, /\baimAt\s*\(/, /\blookAt\s*\(/, /\bshoot\s*\(/,
  /\bkeyDown\s*\(/, /\bkeyUp\s*\(/, /\bkeyPress\s*\(/, /\bmouse(?:Press)?\s*\(/,
  /\buseGamepad\s*\(/, /\bupgrade_(?:stat|tank)\s*\(/, /\bset_convar\s*\(/,
  /\binput\.execute\s*\(/, /\beval\s*\(/, /\bnew Function\s*\(/,
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
  'extension/src/lifecycle.js',
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
  'tests/lifecycle.test.js',
  'tests/overlay.test.js',
];
const mojibakePattern = /\uFFFD|\u00C3.|\u00C2.|\u00E2(?:\u20AC|\u2122)|\u0432\u0402/u;
for (const relativePath of ownedTextFiles) {
  assert.doesNotMatch(read(relativePath), mojibakePattern, `mojibake found in ${relativePath}`);
}

console.log('Repository tests passed: manifest, MAIN-world bridge, background bridge boundary, pinned refresh, process safety, and docs.');
