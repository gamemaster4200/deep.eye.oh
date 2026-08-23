'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = path.join(__dirname, '..');
const oracleSource = fs.readFileSync(
  path.join(repoRoot, 'extension', 'src', 'oracle.js'),
  'utf8',
);

function matrix({ a = 1, b = 0, c = 0, d = 1, e = 0, f = 0 } = {}) {
  return { a, b, c, d, e, f };
}

const IDENTITY = matrix();

// A fresh constructor (with its own, untouched prototype) is required per
// installOracle() call: oracle.js wraps whatever is currently on the
// prototype, so reusing one constructor across calls would stack hooks from
// earlier oracle instances onto later ones and cross-contaminate state.
function createCanvasCtor() {
  function CanvasRenderingContext2D(options = {}) {
    this.fillStyle = Object.hasOwn(options, 'fillStyle') ? options.fillStyle : '#ffe869';
    this.canvas = Object.hasOwn(options, 'canvas') ? options.canvas : { width: 1600, height: 900 };
    this._transforms = options.transforms || [IDENTITY];
    this._transformCallCount = 0;
  }
  CanvasRenderingContext2D.prototype.beginPath = function beginPath() {};
  CanvasRenderingContext2D.prototype.moveTo = function moveTo(_x, _y) {};
  CanvasRenderingContext2D.prototype.lineTo = function lineTo(_x, _y) {};
  CanvasRenderingContext2D.prototype.fill = function fill() {};
  CanvasRenderingContext2D.prototype.rect = function rect(_x, _y, _w, _h) {};
  CanvasRenderingContext2D.prototype.closePath = function closePath() {};
  CanvasRenderingContext2D.prototype.getTransform = function getTransform() {
    const index = Math.min(this._transformCallCount, this._transforms.length - 1);
    this._transformCallCount += 1;
    return this._transforms[index];
  };
  return CanvasRenderingContext2D;
}

function installOracle({ hasCanvasApi = true, missingMethod = undefined, clock = { value: 0 } } = {}) {
  const pageWindow = {
    performance: { now: () => clock.value },
    devicePixelRatio: 2,
  };
  if (hasCanvasApi) {
    const ctxCtor = createCanvasCtor();
    if (missingMethod) {
      delete ctxCtor.prototype[missingMethod];
    }
    pageWindow.CanvasRenderingContext2D = ctxCtor;
  }
  const context = vm.createContext({ window: pageWindow });
  vm.runInContext(oracleSource, context, { filename: 'oracle.js' });
  return { oracle: pageWindow.deepEyeOracle, ctxCtor: pageWindow.CanvasRenderingContext2D };
}

function drawQuad(ctx, points, transforms) {
  ctx._transforms = transforms;
  ctx._transformCallCount = 0;
  ctx.beginPath();
  ctx.moveTo(points[0][0], points[0][1]);
  for (const [x, y] of points.slice(1)) {
    ctx.lineTo(x, y);
  }
  ctx.fill();
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

// ---------------------------------------------------------------------------
// Public API surface and readiness
// ---------------------------------------------------------------------------

{
  const { oracle } = installOracle();
  assert.deepEqual(
    Object.keys(oracle).sort(),
    ['diagnostics', 'isReady', 'shapes', 'snapshot', 'version'],
    'the public API must expose only observation operations, diagnostics, and version metadata',
  );
  assert.equal(oracle.version, '0.4.0');
  assert.equal(oracle.isReady(), true, 'hooking all four Canvas2D methods must report ready');
}

{
  const { oracle } = installOracle({ hasCanvasApi: false });
  assert.equal(oracle.isReady(), false, 'a missing CanvasRenderingContext2D must report not ready');
  assert.doesNotThrow(() => JSON.stringify(oracle.snapshot()));
  assert.deepEqual(plain(oracle.shapes()), []);
  assert.doesNotThrow(() => JSON.stringify(oracle.diagnostics()));
}

{
  const { oracle } = installOracle({ missingMethod: 'fill' });
  assert.equal(oracle.isReady(), false, 'a Canvas2D missing one hookable method must report not ready');
}

{
  // rect/closePath are diagnostic-only hooks; their absence must not affect
  // readiness (isReady() means the core square-recognition path is hooked).
  const { oracle } = installOracle({ missingMethod: 'rect' });
  assert.equal(oracle.isReady(), true, 'a missing diagnostic-only rect() must not affect readiness');
}

// ---------------------------------------------------------------------------
// A. Polygon recognition
// ---------------------------------------------------------------------------

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
  assert.equal(oracle.shapes().length, 1, 'a beginPath/moveTo/lineTo x3/fill quad must be recognized');
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(5, 10);
  ctx.fill();
  assert.equal(oracle.shapes().length, 0, 'a triangle (3 vertices) must not be accepted');
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(10, 10);
  ctx.lineTo(0, 10);
  ctx.lineTo(5, 5);
  ctx.fill();
  assert.equal(oracle.shapes().length, 0, 'a pentagon (5 vertices) must not be accepted');
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx.fill();
  assert.equal(oracle.shapes().length, 0, 'fill() with no path (0 vertices) must not be accepted');
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[5, 5], [5, 5], [5, 5], [5, 5]], [IDENTITY]);
  assert.equal(oracle.shapes().length, 0, 'a zero-area degenerate quad must not be accepted');
}

{
  // A path manually closed with an explicit lineTo back to the start point
  // (5 raw vertices, the 5th equal to the 1st) is a standard way to close a
  // polygon and must still be recognized as the same 4-vertex quad.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], [IDENTITY]);
  assert.equal(oracle.shapes().length, 1, 'a manually-closed square path must be accepted');
  const shape = plain(oracle.shapes()[0]);
  assert.equal(shape.vertices.length, 4, 'the duplicate closing vertex must be dropped');
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }]);
}

{
  // A genuine 5th vertex that is NOT close to the start point, and not
  // collinear with its neighbors, is a different (unsupported) shape, not
  // a closed square, and must still be rejected rather than silently
  // normalized away.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10], [-10, 20]], [IDENTITY]);
  assert.equal(oracle.shapes().length, 0, 'a 5th vertex far from the start must not be treated as closing');
  assert.equal(oracle.diagnostics().rejectionReasons.wrongVertexCount, 1);
}

// ---------------------------------------------------------------------------
// B. Transform application
// ---------------------------------------------------------------------------

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }]);
  assert.deepEqual(shape.bbox, { x0: 0, y0: 0, x1: 10, y1: 10 });
  assert.equal(shape.cx, 5);
  assert.equal(shape.cy, 5);
  assert.equal(shape.halfSize, 5);
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const translated = matrix({ e: 100, f: 200 });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [translated]);
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [
    { x: 100, y: 200 }, { x: 110, y: 200 }, { x: 110, y: 210 }, { x: 100, y: 210 },
  ]);
  assert.equal(shape.cx, 105);
  assert.equal(shape.cy, 205);
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const scaled = matrix({ a: 3, d: 3 });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [scaled]);
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [
    { x: 0, y: 0 }, { x: 30, y: 0 }, { x: 30, y: 30 }, { x: 0, y: 30 },
  ]);
  assert.equal(shape.halfSize, 15);
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const scaledAndTranslated = matrix({ a: 2, d: 2, e: 50, f: 60 });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [scaledAndTranslated]);
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [
    { x: 50, y: 60 }, { x: 70, y: 60 }, { x: 70, y: 80 }, { x: 50, y: 80 },
  ]);
}

{
  // A 90-degree rotation matrix (a=0, b=1, c=-1, d=0): x' = -y, y' = x.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const rotated = matrix({ a: 0, b: 1, c: -1, d: 0 });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [rotated]);
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [
    { x: 0, y: 0 }, { x: 0, y: 10 }, { x: -10, y: 10 }, { x: -10, y: 0 },
  ]);
  assert.deepEqual(shape.bbox, { x0: -10, y0: 0, x1: 0, y1: 10 });
  assert.equal(shape.cx, -5);
  assert.equal(shape.cy, 5);
}

{
  // The transform must be re-read on every moveTo/lineTo call, not cached
  // once. The same local point (1, 1) is fed to all four calls; only a
  // distinct translation per call maps it onto the four corners of a valid
  // 10x10 square. A "capture once" bug would collapse every vertex onto a
  // single point instead.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const perCallTransforms = [
    matrix({ e: -1, f: -1 }),
    matrix({ e: 9, f: -1 }),
    matrix({ e: 9, f: 9 }),
    matrix({ e: -1, f: 9 }),
  ];
  drawQuad(ctx, [[1, 1], [1, 1], [1, 1], [1, 1]], perCallTransforms);
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [
    { x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 },
  ], 'each vertex must use the transform in effect at the moment of its own call');
}

// ---------------------------------------------------------------------------
// C. Square geometry (bbox / cx / cy / halfSize semantics)
// ---------------------------------------------------------------------------

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 12], [0, 12]], [IDENTITY]);
  const [shape] = oracle.shapes();
  assert.equal(shape.cx, 5);
  assert.equal(shape.cy, 6);
  assert.equal(shape.halfSize, 6, 'halfSize must be 0.5 * max(width, height), not the average');
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const stretched = matrix({ a: 3, d: 1 });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [stretched]);
  assert.equal(oracle.shapes().length, 0, 'a quad far outside the side-length tolerance must be rejected');
}

// ---------------------------------------------------------------------------
// D. Color filtering
// ---------------------------------------------------------------------------

{
  const cases = [
    ['#ffe869', true],
    ['#FFE869', true],
    ['rgb(255, 232, 105)', true],
    ['rgba(255, 232, 105, 1)', true],
    ['rgba(255,232,105,0.5)', true],
    ['#00b2e1', false],
    ['rgb(0, 178, 225)', false],
    [undefined, false],
    [{ toString: () => '#ffe869' }, false],
  ];
  for (const [fillStyle, expectAccepted] of cases) {
    const { oracle, ctxCtor } = installOracle();
    const ctx = new ctxCtor({ fillStyle });
    drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
    assert.equal(
      oracle.shapes().length,
      expectAccepted ? 1 : 0,
      `fillStyle ${JSON.stringify(fillStyle)} accepted-ness must be ${expectAccepted}`,
    );
  }
}

// ---------------------------------------------------------------------------
// E. JSON safety
// ---------------------------------------------------------------------------

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
  const snapshot = oracle.snapshot();
  const shapes = oracle.shapes();
  assert.doesNotThrow(() => JSON.stringify(snapshot));
  assert.doesNotThrow(() => JSON.stringify(shapes));
  assert.equal(snapshot.shapes.length, 1);
  assert.equal(JSON.stringify(plain(snapshot)), JSON.stringify(snapshot));
  assert.equal(JSON.stringify(plain(shapes)), JSON.stringify(shapes));
}

{
  const { oracle, ctxCtor } = installOracle();
  const hostileCanvas = {};
  Object.defineProperty(hostileCanvas, 'width', { get() { throw new Error('width unavailable'); } });
  const ctx = new ctxCtor({ canvas: hostileCanvas });
  Object.defineProperty(ctx, 'fillStyle', { get() { throw new Error('fillStyle unavailable'); } });
  assert.doesNotThrow(() => drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]));
  assert.doesNotThrow(() => JSON.stringify(oracle.snapshot()));
  assert.doesNotThrow(() => JSON.stringify(oracle.shapes()));
  assert.equal(oracle.shapes().length, 0, 'a throwing fillStyle must not be classified as the neutral square color');
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx.getTransform = () => { throw new Error('getTransform unavailable'); };
  assert.doesNotThrow(() => drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]));
  assert.doesNotThrow(() => JSON.stringify(oracle.shapes()));
  assert.equal(oracle.shapes().length, 0);
}

// ---------------------------------------------------------------------------
// F. Detachment
// ---------------------------------------------------------------------------

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);

  const first = oracle.shapes();
  first[0].cx = -999;
  first[0].vertices[0].x = -999;
  first[0].bbox.x0 = -999;

  const second = oracle.shapes();
  assert.equal(second[0].cx, 5, 'mutating a returned shape must not affect internal state');
  assert.equal(second[0].vertices[0].x, 0);
  assert.equal(second[0].bbox.x0, 0);
  assert.notEqual(first[0], second[0], 'each call must allocate fresh records');
  assert.notEqual(first[0].vertices, second[0].vertices);
}

// ---------------------------------------------------------------------------
// G. Frame/lifetime cache semantics (documented heuristic, not a frame API)
// ---------------------------------------------------------------------------

{
  const clock = { value: 0 };
  const { oracle, ctxCtor } = installOracle({ clock });
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });

  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
  assert.equal(oracle.shapes().length, 1, 'a freshly drawn square must be present');

  clock.value = 100;
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
  clock.value = 150;
  assert.equal(oracle.shapes().length, 1, 'redrawing the same square must not duplicate its record');

  let diag = oracle.diagnostics();
  assert.equal(diag.cache.acceptedTotal, 2, 'acceptedTotal counts every accepted classification, including upserts');
  assert.equal(diag.cache.currentlyCached, 1);
  assert.equal(diag.cache.prunedTotal, 0);

  clock.value = 150 + 300;
  assert.equal(
    oracle.shapes().length,
    0,
    'a square not redrawn within the cache window must age out',
  );

  diag = oracle.diagnostics();
  assert.equal(diag.cache.acceptedTotal, 2, 'acceptedTotal must not decrease on prune');
  assert.equal(diag.cache.currentlyCached, 0, 'if acceptedTotal > 0 but currentlyCached = 0, the detector worked and lifetime aged it out');
  assert.equal(diag.cache.prunedTotal, 1, 'the single cached entry was pruned exactly once');
}

// ---------------------------------------------------------------------------
// H. diagnostics()
// ---------------------------------------------------------------------------

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);

  const diag = oracle.diagnostics();
  assert.equal(diag.squareColor.fillsSeen, 1);
  assert.equal(diag.squareColor.accepted, 1);
  assert.equal(diag.squareColor.rejected, 0);
  assert.equal(diag.calls.beginPath, 1);
  assert.equal(diag.calls.moveTo, 1);
  assert.equal(diag.calls.lineTo, 3);
  assert.equal(diag.calls.fill, 1);
  assert.equal(diag.calls.rect, 0);
  assert.equal(diag.acceptedSamples.length, 1);
  assert.equal(diag.acceptedSamples[0].reason, 'accepted');
}

{
  // A rejected candidate must increment the exact rejection reason, and a
  // sample with enough detail to diagnose it must be captured.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(5, 10);
  ctx.fill();

  const diag = oracle.diagnostics();
  assert.equal(diag.rejectionReasons.wrongVertexCount, 1);
  assert.equal(diag.squareColor.rejected, 1);
  assert.equal(diag.squareColor.fillsSeen, 1);
  assert.equal(diag.rejectedSquareColorSamples.length, 1);
  assert.equal(diag.rejectedSquareColorSamples[0].reason, 'wrongVertexCount');
  assert.equal(diag.rejectedSquareColorSamples[0].vertexCount, 3);
  assert.equal(diag.rejectedSquareColorSamples[0].moveToCalls, 1);
  assert.equal(diag.rejectedSquareColorSamples[0].lineToCalls, 2);
  assert.equal(diag.rejectedSquareColorSamples[0].fillStyle, '#ffe869');
}

{
  // squareColor stats must be specific to the neutral square color: a
  // valid quad in a different color must not count toward them, and must
  // instead show up as a global colorMismatch rejection.
  const { oracle, ctxCtor } = installOracle();
  const yellow = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(yellow, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
  const blue = new ctxCtor({ fillStyle: '#00b2e1' });
  drawQuad(blue, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);

  const diag = oracle.diagnostics();
  assert.equal(diag.squareColor.fillsSeen, 1, 'only the #ffe869 fill counts toward squareColor stats');
  assert.equal(diag.squareColor.accepted, 1);
  assert.equal(diag.rejectionReasons.colorMismatch, 1, 'the valid blue quad must be tallied as a color mismatch');
}

{
  const { oracle, ctxCtor } = installOracle();
  const yellow = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(yellow, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);
  const black = new ctxCtor({ fillStyle: '#000000' });
  black._transforms = [IDENTITY];
  black.beginPath();
  black.moveTo(0, 0);
  black.lineTo(1, 0);
  black.lineTo(0, 1);
  black.fill();

  const diag = oracle.diagnostics();
  assert.equal(diag.vertexHistogram['4'], 1);
  assert.equal(diag.vertexHistogram['3'], 1);
  assert.equal(diag.squareColorVertexHistogram['4'], 1);
  assert.equal(diag.squareColorVertexHistogram['3'], undefined, 'the black fill must not appear in the color-scoped histogram');
}

{
  // Bounded sample buffer: only the first REJECTED_SAMPLE_CAP (30) rejected
  // #ffe869 samples are retained in detail, even though the counter itself
  // keeps counting every rejection.
  const { oracle, ctxCtor } = installOracle();
  for (let i = 0; i < 40; i += 1) {
    const ctx = new ctxCtor({ fillStyle: '#ffe869' });
    ctx._transforms = [IDENTITY];
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(1, 0);
    ctx.fill();
  }
  const diag = oracle.diagnostics();
  assert.equal(diag.squareColor.rejected, 40, 'the counter must not be capped');
  assert.equal(diag.rejectedSquareColorSamples.length, 30, 'the detailed sample buffer must be capped');
}

{
  // rect() is not hooked into vertex tracking (see README), but its
  // presence in a rejected path must be visible, not silent.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.rect(0, 0, 10, 10);
  ctx.fill();

  const diag = oracle.diagnostics();
  assert.equal(diag.calls.rect, 1);
  assert.equal(diag.rejectionReasons.unsupportedRectPath, 1);
  assert.equal(diag.rejectedSquareColorSamples[0].reason, 'unsupportedRectPath');
  assert.equal(diag.rejectedSquareColorSamples[0].rectCalls, 1);
  assert.equal(oracle.shapes().length, 0, 'rect()-built paths are not (yet) accepted as squares');
}

{
  // A candidate rejected by the real geometry rule (side ratio) must be
  // classified with that exact reason and carry the numbers that explain it.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const stretched = matrix({ a: 3, d: 1 });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [stretched]);

  const diag = oracle.diagnostics();
  assert.equal(diag.rejectionReasons.sideRatio, 1);
  assert.equal(diag.rejectedSquareColorSamples[0].reason, 'sideRatio');
  assert.ok(diag.rejectedSquareColorSamples[0].geometry.sideRatio > 1.35);
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);

  const diag = oracle.diagnostics();
  assert.doesNotThrow(() => JSON.stringify(diag));
  assert.equal(JSON.stringify(plain(diag)), JSON.stringify(diag), 'diagnostics() must round-trip losslessly through JSON');
}

{
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);

  const first = oracle.diagnostics();
  first.calls.fill = -999;
  first.squareColor.accepted = -999;
  first.acceptedSamples[0].vertices[0].x = -999;
  first.acceptedSamples.push({ hostile: true });

  const second = oracle.diagnostics();
  assert.equal(second.calls.fill, 1, 'mutating a returned diagnostics object must not affect internal state');
  assert.equal(second.squareColor.accepted, 1);
  assert.equal(second.acceptedSamples.length, 1);
  assert.equal(second.acceptedSamples[0].vertices[0].x, 0);
}

// ---------------------------------------------------------------------------
// I. Multi-subpath diagnostic topology (knowledge-schema-v0 evidence slice)
//
// Live smoke evidence: real #ffe869 fills consistently use 2 moveTo-started
// subpaths per fill() -- a real, multi-vertex first subpath followed by a
// trailing one-point second subpath -- which the prior single-subpath model
// silently discarded down to that trailing point every time, explaining the
// prior 0% acceptance rate. These tests cover the new tracking that exposes
// every subpath's real topology WITHOUT changing acceptance semantics (see
// classifyFill()'s doc comment in oracle.js).
// ---------------------------------------------------------------------------

{
  // A second moveTo() must not discard the first subpath's tracked
  // vertices -- diagnostics must show both subpaths with their real
  // coordinates. Acceptance is now driven by the unique MEANINGFUL subpath
  // (the 3-point triangle-shaped subpath0, which has real area), not by
  // subpath position -- subpath1 (a lone point) has no area and is not a
  // candidate at all. subpath0 is correctly selected but rejected for
  // having 3 (not 4) corners after simplification.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(10, 10);
  ctx.moveTo(5, 5);
  ctx.fill();

  assert.equal(oracle.shapes().length, 0, 'a 3-corner meaningful subpath must still be rejected as wrongVertexCount');
  const diag = plain(oracle.diagnostics());
  const sample = diag.rejectedSquareColorSamples[diag.rejectedSquareColorSamples.length - 1];
  assert.equal(sample.subpathCount, 2, 'two moveTo calls must be reported as two subpaths');
  assert.equal(sample.subpaths.length, 2);
  assert.equal(sample.subpaths[0].vertexCount, 3, 'the first subpath (moveTo + 2 lineTo) must not be lost');
  assert.deepEqual(sample.subpaths[0].vertices, [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }]);
  assert.equal(sample.subpaths[0].lineToCount, 2);
  assert.equal(sample.subpaths[1].vertexCount, 1);
  assert.deepEqual(sample.subpaths[1].vertices, [{ x: 5, y: 5 }]);
  assert.equal(sample.subpaths[1].lineToCount, 0);
  assert.equal(sample.reason, 'wrongVertexCount');
  assert.equal(sample.meaningfulCandidateCount, 1, 'the lone-point subpath must not count as a meaningful candidate');
  assert.equal(sample.selectedSubpathIndex, 0, 'the meaningful (area-bearing) subpath0 must be selected, not subpath1');
  assert.equal(sample.vertexCount, 3);
}

{
  // C: each subpath's vertices must reflect the transform in effect at the
  // moment of ITS OWN moveTo/lineTo calls, not a transform captured once for
  // the whole fill() or leaked from a different subpath.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const translated = matrix({ e: 100, f: 200 });
  ctx._transforms = [IDENTITY, IDENTITY, IDENTITY, translated];
  ctx.beginPath();
  ctx.moveTo(0, 0); // IDENTITY
  ctx.lineTo(1, 0); // IDENTITY
  ctx.lineTo(1, 1); // IDENTITY
  ctx.moveTo(9, 9); // translated
  ctx.fill();

  const diag = plain(oracle.diagnostics());
  const sample = diag.rejectedSquareColorSamples[diag.rejectedSquareColorSamples.length - 1];
  assert.deepEqual(sample.subpaths[0].vertices, [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }]);
  assert.deepEqual(
    sample.subpaths[1].vertices,
    [{ x: 109, y: 209 }],
    "the second subpath must use its own in-effect transform, not the first subpath's",
  );
}

{
  // D: closePath() must mark the CURRENT subpath at the time it is called,
  // not whichever subpath is current later when fill() runs.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(10, 10);
  ctx.closePath();
  ctx.moveTo(5, 5);
  ctx.fill();

  const diag = oracle.diagnostics();
  const sample = diag.rejectedSquareColorSamples[diag.rejectedSquareColorSamples.length - 1];
  assert.equal(sample.subpaths[0].explicitlyClosed, true, 'closePath() must mark the subpath that was current when it was called');
  assert.equal(sample.subpaths[1].explicitlyClosed, false, 'a later subpath must not inherit an earlier closePath() call');
}

{
  // F: diagnostics() with subpaths must still be JSON-safe and detached.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(10, 10);
  ctx.moveTo(5, 5);
  ctx.fill();

  const diag = oracle.diagnostics();
  assert.doesNotThrow(() => JSON.stringify(diag));
  assert.equal(JSON.stringify(plain(diag)), JSON.stringify(diag), 'diagnostics() with subpaths must round-trip losslessly through JSON');

  const first = oracle.diagnostics();
  const firstSample = first.rejectedSquareColorSamples[first.rejectedSquareColorSamples.length - 1];
  firstSample.subpaths[0].vertices[0].x = -999;
  firstSample.subpaths.push({ hostile: true });

  const second = oracle.diagnostics();
  const secondSample = second.rejectedSquareColorSamples[second.rejectedSquareColorSamples.length - 1];
  assert.equal(secondSample.subpaths[0].vertices[0].x, 0, 'mutating a returned subpath must not affect internal state');
  assert.equal(secondSample.subpaths.length, 2);
}

{
  // H: squareColorSubpathCountHistogram / squareColorTopologyHistogram must
  // aggregate real per-fill topology, keyed by subpath vertex-count
  // signatures like "6,1", not just overall vertex/rejection counts.
  const { oracle, ctxCtor } = installOracle();

  // Fill 1: single subpath, 4 vertices (a plain, valid quad -- accepted).
  const ctxA = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctxA, [[0, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);

  // Fill 2 and 3: two subpaths each (6-vertex real subpath, 1-vertex
  // trailing subpath), matching the live-evidence pattern.
  for (let i = 0; i < 2; i += 1) {
    const ctx = new ctxCtor({ fillStyle: '#ffe869' });
    ctx._transforms = [IDENTITY];
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(20, 0);
    ctx.lineTo(20, 20);
    ctx.lineTo(0, 20);
    ctx.lineTo(0, 0);
    ctx.lineTo(1, 1);
    ctx.moveTo(50, 50);
    ctx.fill();
  }

  const diag = oracle.diagnostics();
  assert.equal(diag.squareColorSubpathCountHistogram['1'], 1, 'the single-subpath accepted fill must be counted');
  assert.equal(diag.squareColorSubpathCountHistogram['2'], 2, 'both two-subpath fills must be counted');
  assert.equal(diag.squareColorTopologyHistogram['4'], 1, 'a lone 4-vertex subpath must produce topology signature "4"');
  assert.equal(diag.squareColorTopologyHistogram['6,1'], 2, 'both repeated 6-vertex-then-1-vertex fills must share one topology signature');
}

{
  // Inert subpath FIRST rather than second: semantic meaningful-subpath
  // selection must still find the unique real quad regardless of its
  // position among subpaths. This prevents accidentally hard-coding
  // "subpath[0]" (the real quad happens to be first in the live-evidence
  // topology) as much as it prevents hard-coding "the last subpath" (the
  // old, now-removed model).
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(50, 50); // unrelated leading subpath, a single point -- no area, not a candidate
  ctx.moveTo(0, 0); // the real, valid quad
  ctx.lineTo(10, 0);
  ctx.lineTo(10, 10);
  ctx.lineTo(0, 10);
  ctx.fill();

  assert.equal(oracle.shapes().length, 1, 'the unique meaningful subpath must be accepted regardless of its position');
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }]);

  const diag = plain(oracle.diagnostics());
  const lastAccepted = diag.acceptedSamples[diag.acceptedSamples.length - 1];
  assert.equal(lastAccepted.subpathCount, 2);
  assert.equal(lastAccepted.subpaths[0].vertexCount, 1, 'the earlier unrelated subpath must still be visible in diagnostics even though it did not drive acceptance');
  assert.equal(lastAccepted.selectedSubpathIndex, 1, 'the meaningful subpath must be selected by content, not by being first');
}

{
  // Per-subpath geometry (bbox/width/height/center/sides/perimeter/
  // signedArea) must be computed for arbitrary vertex counts, not just
  // quads -- this is what makes real (non-4-vertex) first-subpath topology
  // inspectable.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(4, 0);
  ctx.lineTo(0, 3);
  ctx.moveTo(99, 99);
  ctx.fill();

  const diag = plain(oracle.diagnostics());
  const sample = diag.rejectedSquareColorSamples[diag.rejectedSquareColorSamples.length - 1];
  const geometry = sample.subpaths[0].geometry;
  assert.deepEqual(geometry.bbox, { x0: 0, y0: 0, x1: 4, y1: 3 });
  assert.equal(geometry.width, 4);
  assert.equal(geometry.height, 3);
  assert.deepEqual(geometry.center, { x: 2, y: 1.5 });
  assert.equal(geometry.perimeter, 12, 'a 3-4-5 right triangle, implicitly closed, has perimeter 12');
  assert.equal(geometry.signedArea, 6);
  assert.deepEqual(geometry.sides, [4, 5, 3]);
  assert.equal(sample.subpaths[1].geometry, undefined, 'a lone one-point subpath has no meaningful geometry');
}

// ---------------------------------------------------------------------------
// J. Detector redesign: reconstructing the real square from a subdivided
// contour (see README: live evidence established the real render is one
// polygon subpath with collinear edge-subdivision points, plus one inert
// moveTo-to-center subpath).
// ---------------------------------------------------------------------------

{
  // A plain 4-corner square (no subdivisions at all) must still be accepted
  // -- the simplification pipeline must be a no-op when there is nothing
  // to simplify.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [20, 0], [20, 20], [0, 20]], [IDENTITY]);
  assert.equal(oracle.shapes().length, 1);
  const diag = oracle.diagnostics();
  assert.equal(diag.acceptedSamples[0].collinearPointsRemoved, 0);
  assert.ok(diag.acceptedSamples[0].areaPerimeter.ratio < 1.01, 'area/perimeter ratio must be ~1 for an exact square');
}

{
  // One subdivided edge: A, p (on segment AB), B, C, D -- p must be
  // removed and the real 4 corners recovered and accepted.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [20, 0], [20, 20], [0, 20]], [IDENTITY]);

  assert.equal(oracle.shapes().length, 1, 'a single edge-subdivision point must not block acceptance');
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 20 }, { x: 0, y: 20 }]);

  const diag = oracle.diagnostics();
  assert.equal(diag.acceptedSamples[0].preSimplificationVertexCount, 5);
  assert.equal(diag.acceptedSamples[0].collinearPointsRemoved, 1);
  assert.equal(diag.acceptedSamples[0].simplifiedVertexCount, 4);
}

{
  // Multiple subdivisions on all four edges (8 rendered vertices: 4 real
  // corners + 1 midpoint subdivision per edge) must reduce to exactly the
  // 4 real corners and be accepted -- matching the live-evidence range of
  // "6-12 vertices" for a real subdivided square subpath.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [
    [0, 0], [10, 0], [20, 0], [20, 10], [20, 20], [10, 20], [0, 20], [0, 10],
  ], [IDENTITY]);

  assert.equal(oracle.shapes().length, 1, 'subdivisions on every edge must still reduce to a valid square');
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 20 }, { x: 0, y: 20 }]);

  const diag = oracle.diagnostics();
  assert.equal(diag.acceptedSamples[0].preSimplificationVertexCount, 8);
  assert.equal(diag.acceptedSamples[0].collinearPointsRemoved, 4);
  assert.equal(diag.squareColor.simplifiedToQuad, 1);
}

{
  // A rotated, subdivided square must still be accepted, with correct
  // bbox/center/halfSize computed from the recovered corners.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  const rotated = matrix({ a: 0, b: 1, c: -1, d: 0 }); // x' = -y, y' = x
  drawQuad(ctx, [[0, 0], [5, 0], [10, 0], [10, 10], [0, 10]], [rotated]);

  assert.equal(oracle.shapes().length, 1);
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 0, y: 10 }, { x: -10, y: 10 }, { x: -10, y: 0 }]);
  assert.deepEqual(shape.bbox, { x0: -10, y0: 0, x1: 0, y1: 10 });
  assert.equal(shape.cx, -5);
  assert.equal(shape.cy, 5);
  assert.equal(shape.halfSize, 5);
}

{
  // Reverse winding (counter-clockwise instead of clockwise), with a
  // subdivision point, must still be accepted, and vertex order must be
  // preserved (points are only ever removed, never reordered).
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [0, 5], [0, 10], [10, 10], [10, 0]], [IDENTITY]);

  assert.equal(oracle.shapes().length, 1, 'reversed winding must not affect acceptance');
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 0, y: 10 }, { x: 10, y: 10 }, { x: 10, y: 0 }], 'source path order must be preserved');
}

{
  // The real observed topology end-to-end: subpath0 is a subdivided square
  // contour, subpath1 is a lone moveTo to its center with no lineTo calls
  // (visually inert). subpath1 must be discarded as not meaningful, and
  // the square must be recovered and accepted from subpath0 alone.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(20, 0);
  ctx.lineTo(20, 20);
  ctx.lineTo(0, 20);
  ctx.moveTo(10, 10); // inert: bbox center of subpath0, no lineTo calls
  ctx.fill();

  assert.equal(oracle.shapes().length, 1, 'the inert center subpath must be discarded, not block acceptance');
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 20 }, { x: 0, y: 20 }]);

  const diag = oracle.diagnostics();
  assert.equal(diag.acceptedSamples[0].subpathCount, 2);
  assert.equal(diag.acceptedSamples[0].selectedSubpathIndex, 0);
  assert.equal(diag.acceptedSamples[0].meaningfulCandidateCount, 1, 'the inert one-point subpath must not count as meaningful');
}

{
  // Two independently meaningful polygon subpaths in one fill() must be
  // rejected as ambiguous rather than guessed at (e.g. by always picking
  // the first, or the largest).
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx._transforms = [IDENTITY];
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(10, 0);
  ctx.lineTo(10, 10);
  ctx.lineTo(0, 10);
  ctx.moveTo(50, 50);
  ctx.lineTo(60, 50);
  ctx.lineTo(60, 60);
  ctx.lineTo(50, 60);
  ctx.fill();

  assert.equal(oracle.shapes().length, 0, 'two meaningful polygons must not be guessed between');
  const diag = oracle.diagnostics();
  assert.equal(diag.rejectionReasons.ambiguousSubpaths, 1);
  assert.equal(diag.squareColor.ambiguousMeaningfulPolygons, 1);
  assert.equal(diag.rejectedSquareColorSamples[0].meaningfulCandidateCount, 2);
}

{
  // A genuine (non-collinear) pentagon must not be simplified into a
  // square merely because the tolerance is loose.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [13, 8], [5, 13], [-3, 8]], [IDENTITY]);

  assert.equal(oracle.shapes().length, 0, 'a genuine pentagon must not reduce to a quad');
  const diag = oracle.diagnostics();
  assert.equal(diag.rejectedSquareColorSamples[0].reason, 'wrongVertexCount');
  assert.equal(diag.rejectedSquareColorSamples[0].simplifiedVertexCount, 5, 'no pentagon vertex may be dropped as if collinear');
}

{
  // A point 2px off the true edge line (well beyond GEOMETRY_EPSILON_PX =
  // 0.75px) is a genuine bend, not subdivision noise, and must be retained
  // -- so the fill is correctly rejected for having 5 (not 4) corners.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 2], [20, 0], [20, 20], [0, 20]], [IDENTITY]);

  assert.equal(oracle.shapes().length, 0, 'a point outside the collinearity tolerance must not be collapsed away');
  const diag = oracle.diagnostics();
  assert.equal(diag.rejectedSquareColorSamples[0].collinearPointsRemoved, 0);
  assert.equal(diag.rejectedSquareColorSamples[0].simplifiedVertexCount, 5);
}

{
  // A duplicate consecutive point (the same corner listed twice in a row)
  // must be merged safely rather than treated as a 5th real corner.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  drawQuad(ctx, [[0, 0], [10, 0], [10, 0], [10, 10], [0, 10]], [IDENTITY]);

  assert.equal(oracle.shapes().length, 1, 'a duplicate consecutive point must be merged, not treated as a real corner');
  const shape = plain(oracle.shapes()[0]);
  assert.deepEqual(shape.vertices, [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }]);
}

{
  // fill() with no path at all: zero subpaths -> explicitly
  // "noMeaningfulSubpath", not a guess, and correctly tallied.
  const { oracle, ctxCtor } = installOracle();
  const ctx = new ctxCtor({ fillStyle: '#ffe869' });
  ctx.fill();

  assert.equal(oracle.shapes().length, 0);
  const diag = oracle.diagnostics();
  assert.equal(diag.rejectionReasons.noMeaningfulSubpath, 1);
  assert.equal(diag.squareColor.noMeaningfulPolygon, 1);
  assert.equal(diag.rejectedSquareColorSamples[0].meaningfulCandidateCount, 0);
}

console.log(
  'Oracle tests passed: polygon recognition, closing-vertex normalization, per-call transform, geometry, color filtering, JSON safety, detachment, cache lifetime, diagnostics, multi-subpath topology, and subdivided-contour square reconstruction.',
);
