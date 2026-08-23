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
assert.equal(manifest.content_scripts.length, 1);
assert.deepEqual(manifest.content_scripts[0].matches, ['https://diep.io/*']);
assert.equal(manifest.content_scripts[0].world, 'MAIN');
assert.deepEqual(manifest.content_scripts[0].js, ['src/oracle.js']);
assert.equal(
  JSON.stringify(manifest).includes('diepAPI'),
  false,
  'the pinned vendor must not be a manifest-loaded runtime script',
);
assert.equal(Object.hasOwn(manifest, 'background'), false);
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
  'squareColorSubpathCountHistogram',
  'squareColorTopologyHistogram',
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
]) {
  assert.ok(oracleSource.includes(requiredToken), `oracle.js is missing the subdivided-contour detector slice: ${requiredToken}`);
}
assert.equal(
  oracleSource.includes('normalizeClosedQuad'),
  false,
  'the old fixed-4/5-vertex special case must be replaced by the general collinear-collapse pipeline',
);

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
  'extension/popup/popup.css',
  'extension/popup/popup.html',
  'extension/popup/popup.js',
  'scripts/dev-refresh.ps1',
  'scripts/update-vendor.ps1',
  'scripts/validate.ps1',
  'tests/oracle.test.js',
];
const mojibakePattern = /\uFFFD|\u00C3.|\u00C2.|\u00E2(?:\u20AC|\u2122)|\u0432\u0402/u;
for (const relativePath of ownedTextFiles) {
  assert.doesNotMatch(read(relativePath), mojibakePattern, `mojibake found in ${relativePath}`);
}

console.log('Repository tests passed: manifest, MAIN-world bridge, pinned refresh, process safety, and docs.');
