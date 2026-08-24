(() => {
  'use strict';

  if (safeRead(window, 'deepEyeOracle')) {
    return;
  }

  const VERSION = '0.6.0';

  // diep.io's official client fills each neutral shape class with a fixed
  // color (matches the vendored Cazka/diepAPI EntityColor map, and -- for
  // Square -- independently confirmed by live Canvas2D capture; see
  // README). Each class also has a fixed corner count once its render
  // contour is simplified (see collapseCollinear below): 3 for Triangle, 4
  // for Square, 5 for Pentagon. A fill is classified by BOTH signals
  // together -- see classifyFill -- never by color or corner count alone,
  // so a shape drawn in the wrong color for its corner count (or vice
  // versa) is correctly rejected rather than guessed at.
  const SHAPE_CLASSES = {
    square: { hex: '#ffe869', rgb: { r: 255, g: 232, b: 105 }, vertexCount: 4, kind: 'neutral_square' },
    triangle: { hex: '#fc7677', rgb: { r: 252, g: 118, b: 119 }, vertexCount: 3, kind: 'neutral_triangle' },
    pentagon: { hex: '#768dfc', rgb: { r: 118, g: 141, b: 252 }, vertexCount: 5, kind: 'neutral_pentagon' },
  };
  const CLASS_NAMES = Object.freeze(Object.keys(SHAPE_CLASSES));
  const VERTEX_COUNT_TO_CLASS = Object.freeze(
    Object.fromEntries(CLASS_NAMES.map((name) => [SHAPE_CLASSES[name].vertexCount, name])),
  );

  // Generous, dataset-independent tolerances for rendering/anti-aliasing
  // jitter. Not tuned against any vision holdout. Shared across all three
  // shape classes -- there is no live evidence yet that Triangle/Pentagon
  // need different tolerances than the ones established for Square.
  const SIDE_LENGTH_RATIO_TOLERANCE = 1.35;
  // Replaces the old, quad-specific "diagonal ratio" check (a quadrilateral
  // has exactly 2 diagonals; a triangle has none and a pentagon has 5,
  // neither of which generalizes). A regular N-gon's vertices are all
  // equidistant from its centroid, so "max/min vertex-to-centroid distance"
  // is the generalization that applies uniformly to N=3/4/5. For a perfect
  // square this is exactly proportional to the old diagonal check (all
  // vertices lie on one circle), so real square acceptance is unaffected.
  const RADIUS_RATIO_TOLERANCE = 1.35;
  const MIN_SIDE_LENGTH_PX = 0.5;

  // Shared "same physical location" / "same physical line" tolerance, used
  // for two distinct but equally-justified purposes below: merging a point
  // with its neighbor when they coincide (an explicit closing vertex that
  // duplicates the first point, or an accidental duplicate lineTo), and
  // testing whether a point lies on the straight line through its two
  // neighbors (an edge-subdivision point, not a real corner). Both are
  // sub-pixel Canvas rendering/floating-point noise floors, not tuned
  // against any vision holdout or dataset -- a genuine closing point or
  // edge-subdivision point round-trips through the same transform as its
  // neighbors and should match to sub-pixel precision regardless of shape
  // size, rotation, or how many subdivision points an edge carries.
  const GEOMETRY_EPSILON_PX = 0.75;

  // A subpath with a computed |signedArea| at or below this is treated as
  // inert (no meaningfully filled region), not a degenerate-but-real
  // polygon. Tied to the same MIN_SIDE_LENGTH_PX noise floor used for
  // individual side lengths elsewhere in this file (0.5px), squared: a
  // shape that would fail MIN_SIDE_LENGTH_PX on every side has an area at
  // or below this scale purely from positional noise, before any real
  // geometry.
  const MIN_MEANINGFUL_AREA_PX2 = MIN_SIDE_LENGTH_PX * MIN_SIDE_LENGTH_PX;

  // For a regular N-gon of side length s: area = (n * s^2) / (4 * tan(pi/n))
  // and perimeter = n * s. Two independent estimates of s -- one from the
  // observed area, one from the observed perimeter -- should agree for a
  // true regular polygon; this tolerance bounds how far they may disagree.
  // For n=4, tan(pi/4)=1, so this reduces exactly to the original
  // Square-only identity (sqrt(area) vs perimeter/4): real Square
  // acceptance is numerically unaffected by this generalization.
  const AREA_PERIMETER_RATIO_TOLERANCE = 1.2;

  // How many vertices of a single subpath are kept in coordinate detail.
  // Must be >= 5 to detect the manual-closing-vertex case above. Live smoke
  // evidence (see README) shows the official client's real square-fill
  // subpaths commonly carry 6-12 vertices, so this is set well above that
  // with headroom, not just above the historical 4/5-vertex assumption.
  // Anything beyond this cap is still counted (subpath.pointCount), just not
  // retained point-by-point, bounding memory for pathological/batched paths.
  const MAX_TRACKED_VERTICES_PER_SUBPATH = 16;

  // How many moveTo-started subpaths within a single beginPath()/fill() are
  // kept in detail. Live evidence shows real square-color fills use exactly
  // 2 subpaths; this is generous headroom above that, bounding memory for
  // pathological many-subpath fills. Detail for older subpaths is evicted
  // (oldest first) once this cap is exceeded. classifyFill() below selects
  // whichever tracked subpath is MEANINGFUL (real area), not necessarily
  // the most recent one; on real data (<= 2 subpaths per fill, well under
  // this cap) eviction never happens at all, so this is a documented,
  // untriggered-in-practice bound, not a live behavior.
  const MAX_TRACKED_SUBPATHS = 6;

  // There is no reliable, crash-safe frame boundary available (see README:
  // hooking requestAnimationFrame is the upstream diepAPI crash path this
  // project deliberately avoids). Instead, each detected shape is cached
  // with a timestamp and shapes()/snapshot() only return entries seen
  // within this rolling window. This is a heuristic "recent observation"
  // cache, not a synchronized frame model: it can include a shape from
  // slightly more than one frame ago, and it can briefly miss one if the
  // game skips a render. 250ms tolerates multi-frame stalls at typical
  // frame rates while still dropping shapes that stopped being drawn (e.g.
  // left the screen).
  const CACHE_WINDOW_MS = 250;

  // Generic circle observation (see the "Circle observation" section
  // below): a filled circle drawn via beginPath()/arc()/fill() has no
  // color/shape-class contract like Square/Triangle/Pentagon do, so these
  // tolerances are independent of SIDE_LENGTH_RATIO_TOLERANCE etc. above.
  //
  // How many arc() calls within one beginPath()/fill() are kept in detail
  // -- only the first is ever needed (more than one arc() call on the same
  // path is rejected as ambiguous, see classifyCircleFill), so this is
  // just a small memory bound for a pathological many-arc path, mirroring
  // MAX_TRACKED_SUBPATHS's role for polygon subpaths.
  const MAX_TRACKED_ARCS_PER_STATE = 4;
  // A rolling cache of recent circle observations is capped independently
  // of the per-fill arc() tracking above -- this bounds total memory
  // across many fills within one CACHE_WINDOW_MS window (e.g. a burst of
  // projectiles), evicting the oldest observation first.
  const MAX_TRACKED_CIRCLES = 64;
  // How close an arc()'s |endAngle - startAngle| must be to a full turn
  // (2*PI) to be treated as a solid filled circle rather than a partial
  // wedge/ring -- Canvas2D angle arguments are user-supplied radians, so a
  // small tolerance absorbs float noise (e.g. 2*Math.PI computed two
  // different ways) without accepting a genuinely partial arc.
  const CIRCLE_FULL_ARC_ANGLE_EPS = 0.01;
  // A 2D linear transform maps a circle to a circle (not an ellipse) only
  // when it is a similarity (uniform scale + rotation, no shear/skew): its
  // two transformed basis vectors must have equal length (within this
  // ratio tolerance) and be perpendicular (within this normalized dot
  // tolerance) -- see transformScaleInfo. Tighter than the polygon
  // tolerances above on purpose: an ellipse silently reported as a circle
  // would corrupt a downstream radius/position estimate, so this
  // deliberately rejects anything but a near-exact similarity transform
  // rather than being lenient about rendering noise.
  const CIRCLE_TRANSFORM_SCALE_RATIO_TOLERANCE = 1.02;
  const CIRCLE_TRANSFORM_ORTHOGONALITY_TOLERANCE = 0.02;
  // Same noise floor as MIN_SIDE_LENGTH_PX (see above) applied to a
  // transformed circle's radius.
  const MIN_CIRCLE_RADIUS_PX = MIN_SIDE_LENGTH_PX;

  const HISTOGRAM_BUCKET_CAP = 16;
  const ACCEPTED_SAMPLE_CAP = 20;
  const REJECTED_SAMPLE_CAP = 30;

  // How many distinct canvas elements are tracked for provenance (see
  // canvasProvenance below) at once. diep.io realistically has very few
  // canvases (the game canvas, maybe a minimap or other small overlay);
  // this is generous headroom, bounding memory the same way
  // MAX_TRACKED_SUBPATHS bounds per-fill subpath tracking above.
  const MAX_TRACKED_CANVASES = 8;

  const pathState = new WeakMap();
  const shapeCache = new Map();
  // circleCache: unlike shapeCache (keyed by rounded position+color, so a
  // near-stationary neutral shape occupies one slot), generic circles are
  // meant to support downstream motion tracking of moving objects
  // (projectiles) -- each accepted observation gets its own monotonically
  // increasing id and is kept until it ages out of CACHE_WINDOW_MS, so
  // consecutive frames of the same moving circle are NOT collapsed into
  // one "current position" slot.
  const circleCache = new Map();
  let nextCircleId = 0;
  const circleCounters = { acceptedTotal: 0, rejectedTotal: 0, prunedTotal: 0 };
  let hooksInstalled = false;
  let startTime = 0;

  // canvasProvenance: canvas element -> { id, acceptedCount, firstSeenAt,
  // lastAcceptedAt }, updated ONLY when a fill() is an ACCEPTED neutral
  // shape (see recordCanvasProvenance, called from onFill below) -- never
  // from an arbitrary fill() of any color/shape, which is what the
  // previous "track whatever canvas last called fill()" heuristic did and
  // is exactly what let an unrelated hidden/detached canvas (a 0x0-rect
  // helper canvas that also happens to call fill()) win the race and get
  // reported as snapshot.canvas. A canvas earns provenance only by
  // actually being where a real, accepted Square/Triangle/Pentagon was
  // drawn. See canvasInfo()/selectSingleScreenMappableCanvas() below for
  // how this is turned into (or deliberately withheld from)
  // snapshot.canvas, and diagnostics()'s canvasProvenance field for full
  // visibility into every tracked canvas, not just the selected one.
  const canvasProvenance = new Map();
  let nextCanvasTrackId = 0;

  const callCounters = {
    beginPath: 0, moveTo: 0, lineTo: 0, rect: 0, closePath: 0, fill: 0, arc: 0,
  };
  const rejectionReasonCounters = {
    noMeaningfulSubpath: 0,
    ambiguousSubpaths: 0,
    wrongVertexCount: 0,
    degenerate: 0,
    sideRatio: 0,
    radiusRatio: 0,
    areaPerimeterMismatch: 0,
    colorMismatch: 0,
    unsupportedRectPath: 0,
    transformError: 0,
    geometryError: 0,
    cacheError: 0,
    other: 0,
  };
  // vertexHistogram is keyed by the SELECTED meaningful subpath's vertex
  // count (see selectMeaningfulSubpath below) -- 0 when no subpath
  // qualified as meaningful. Sees every fill() of any color.
  const vertexHistogram = {};
  const cacheCounters = { acceptedTotal: 0, prunedTotal: 0 };
  const acceptedSamples = [];
  // Per-class diagnostics (fillsSeen/accepted/rejected counters and the
  // three histograms), scoped to fills whose fillStyle matches that class's
  // color -- exactly the semantics the original Square-only
  // squareColor/squareColor*Histogram fields had. One shared factory
  // (makeClassStats) builds all three so the bookkeeping logic in
  // recordDiagnostics is written once, not copy-pasted per class (see
  // README: "accurately generalize rather than build three copy-pasted
  // detectors").
  function makeClassStats() {
    return {
      counters: {
        fillsSeen: 0,
        accepted: 0,
        rejected: 0,
        meaningfulSinglePolygon: 0,
        noMeaningfulPolygon: 0,
        ambiguousMeaningfulPolygons: 0,
        simplifiedToExpectedCount: 0,
        simplificationFailed: 0,
      },
      vertexHistogram: {},
      subpathCountHistogram: {},
      topologyHistogram: {},
      rejectedSamples: [],
    };
  }
  const classStats = new Map(CLASS_NAMES.map((name) => [name, makeClassStats()]));

  function safeRead(object, property) {
    try {
      return object == null ? undefined : object[property];
    } catch (_error) {
      return undefined;
    }
  }

  function safeCall(callable, receiver, args = []) {
    if (typeof callable !== 'function') {
      return { succeeded: false, value: undefined };
    }
    try {
      return { succeeded: true, value: Reflect.apply(callable, receiver, args) };
    } catch (_error) {
      return { succeeded: false, value: undefined };
    }
  }

  function finiteNumber(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
  }

  function addValue(target, property, value) {
    if (value !== undefined) {
      target[property] = value;
    }
  }

  function now() {
    const performanceApi = safeRead(window, 'performance');
    const performanceNow = finiteNumber(safeCall(safeRead(performanceApi, 'now'), performanceApi).value);
    if (performanceNow !== undefined) {
      return performanceNow;
    }
    return finiteNumber(safeCall(safeRead(Date, 'now'), Date).value) ?? 0;
  }

  function wallClockNow() {
    return finiteNumber(safeCall(safeRead(Date, 'now'), Date).value);
  }

  // Applies a DOMMatrix-shaped 2D transform: x' = a*x + c*y + e, y' = b*x + d*y + f.
  function transformPoint(matrix, x, y) {
    const a = finiteNumber(safeRead(matrix, 'a'));
    const b = finiteNumber(safeRead(matrix, 'b'));
    const c = finiteNumber(safeRead(matrix, 'c'));
    const d = finiteNumber(safeRead(matrix, 'd'));
    const e = finiteNumber(safeRead(matrix, 'e'));
    const f = finiteNumber(safeRead(matrix, 'f'));
    const px = finiteNumber(x);
    const py = finiteNumber(y);
    if (
      a === undefined || b === undefined || c === undefined
      || d === undefined || e === undefined || f === undefined
      || px === undefined || py === undefined
    ) {
      return undefined;
    }
    return { x: (a * px) + (c * py) + e, y: (b * px) + (d * py) + f };
  }

  function currentTransform(ctx) {
    return safeCall(safeRead(ctx, 'getTransform'), ctx).value;
  }

  function distance(p, q) {
    return Math.hypot(q.x - p.x, q.y - p.y);
  }

  function computeBBox(vertices) {
    const xs = vertices.map((vertex) => vertex.x);
    const ys = vertices.map((vertex) => vertex.y);
    return {
      x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys),
    };
  }

  function centroid(vertices) {
    let sx = 0;
    let sy = 0;
    for (const vertex of vertices) {
      sx += vertex.x;
      sy += vertex.y;
    }
    return { x: sx / vertices.length, y: sy / vertices.length };
  }

  // Generic polygon geometry for diagnostics, valid for any vertex count
  // (unlike evaluateGeometry below, which drives acceptance). Implicitly
  // closes back to vertices[0], matching how Canvas2D's fill() renders an
  // unclosed subpath, regardless of whether closePath() was actually
  // called. Returns undefined for fewer than 2 points, where no meaningful
  // shape exists yet (e.g. a lone moveTo point).
  function polygonGeometry(vertices) {
    if (!Array.isArray(vertices) || vertices.length < 2) {
      return undefined;
    }
    const bbox = computeBBox(vertices);
    const sides = [];
    let perimeter = 0;
    let twiceSignedArea = 0;
    for (let i = 0; i < vertices.length; i += 1) {
      const a = vertices[i];
      const b = vertices[(i + 1) % vertices.length];
      const side = distance(a, b);
      sides.push(side);
      perimeter += side;
      twiceSignedArea += (a.x * b.y) - (b.x * a.y);
    }
    return {
      bbox,
      width: bbox.x1 - bbox.x0,
      height: bbox.y1 - bbox.y0,
      center: { x: (bbox.x0 + bbox.x1) / 2, y: (bbox.y0 + bbox.y1) / 2 },
      sides,
      perimeter,
      signedArea: twiceSignedArea / 2,
    };
  }

  // --- Path tracking -------------------------------------------------------
  //
  // beginPath, moveTo, and lineTo do not mutate ctx.getTransform() or
  // ctx.fillStyle, and fill() does not mutate the current path. So calling
  // the real Canvas2D method first and reading transform/path/fillStyle
  // state after it (see hookMethod below) observes exactly the same values
  // as reading them before: real rendering is unaffected either way, and
  // "call original first" is the strictly safer order (observation can
  // never prevent or alter the real draw call, even if it throws).
  //
  // A single beginPath()/fill() may contain multiple moveTo-started
  // subpaths; every one is tracked (bounded by MAX_TRACKED_SUBPATHS), not
  // just the most recent. Live evidence showed the official client's
  // #ffe869 fills are consistently two subpaths (a real, multi-vertex
  // polygon subpath plus a trailing visually-inert one-point subpath); the
  // same render structure is assumed (not yet independently live-verified)
  // for Triangle/Pentagon -- see README.

  function freshPathState() {
    return {
      subpaths: [],
      moveToCalls: 0,
      lineToCalls: 0,
      rectCalls: 0,
      closePathCalls: 0,
      // arc() tracking is independent of the polygon subpaths above -- see
      // the "Circle observation" section below. arcCallCount counts every
      // arc() call on this path (used to detect "more than one arc call",
      // which disqualifies a circle candidate); arcCalls retains detail
      // for only the first MAX_TRACKED_ARCS_PER_STATE of them (only the
      // first is ever actually used).
      arcCallCount: 0,
      arcCalls: [],
    };
  }

  function getPathState(ctx) {
    let state = pathState.get(ctx);
    if (!state) {
      state = freshPathState();
      pathState.set(ctx, state);
    }
    return state;
  }

  function resetPathState(state) {
    state.subpaths = [];
    state.moveToCalls = 0;
    state.lineToCalls = 0;
    state.rectCalls = 0;
    state.closePathCalls = 0;
    state.arcCallCount = 0;
    state.arcCalls = [];
  }

  function currentSubpath(state) {
    return state.subpaths.length > 0 ? state.subpaths[state.subpaths.length - 1] : undefined;
  }

  function onBeginPath(ctx) {
    callCounters.beginPath += 1;
    resetPathState(getPathState(ctx));
  }

  function onMoveTo(ctx, args) {
    callCounters.moveTo += 1;
    const state = getPathState(ctx);
    state.moveToCalls += 1;

    const point = transformPoint(currentTransform(ctx), args[0], args[1]);
    const subpath = {
      vertices: point === undefined ? [] : [point],
      rawVertices: point === undefined ? [] : [{ x: finiteNumber(args[0]), y: finiteNumber(args[1]) }],
      pointCount: point === undefined ? 0 : 1,
      lineToCount: 0,
      transformFailed: point === undefined,
      explicitlyClosed: false,
    };

    // Evict the oldest tracked subpath detail (not the newest) once the cap
    // is hit -- see the MAX_TRACKED_SUBPATHS note above on why this bound
    // is untriggered on real data.
    if (state.subpaths.length >= MAX_TRACKED_SUBPATHS) {
      state.subpaths.shift();
    }
    state.subpaths.push(subpath);
  }

  function onLineTo(ctx, args) {
    callCounters.lineTo += 1;
    const state = getPathState(ctx);
    state.lineToCalls += 1;
    const subpath = currentSubpath(state);
    if (!subpath) {
      // A lineTo call before any moveTo call on this path; Canvas2D treats
      // this as a no-op moveTo, but there is nothing meaningful to attach it to.
      return;
    }
    subpath.lineToCount += 1;
    if (subpath.transformFailed) {
      return;
    }
    const point = transformPoint(currentTransform(ctx), args[0], args[1]);
    if (point === undefined) {
      subpath.transformFailed = true;
      return;
    }
    subpath.pointCount += 1;
    if (subpath.vertices.length < MAX_TRACKED_VERTICES_PER_SUBPATH) {
      subpath.vertices.push(point);
      subpath.rawVertices.push({ x: finiteNumber(args[0]), y: finiteNumber(args[1]) });
    }
  }

  function onRect(ctx) {
    // Diagnostic-only: rect() does not invoke moveTo/lineTo, so it is
    // otherwise invisible to this observer. Support for classifying
    // rect()-built paths is deliberately not implemented (see README);
    // this hook only makes their presence visible instead of silently
    // mis-detecting or silently missing them.
    callCounters.rect += 1;
    getPathState(ctx).rectCalls += 1;
  }

  function onClosePath(ctx) {
    // Diagnostic-only: closePath() does not add a vertex reachable via
    // moveTo/lineTo and does not change fill() geometry (Canvas2D closes an
    // unclosed subpath implicitly for fill() regardless). It marks the
    // CURRENT subpath -- the one most recently started by a moveTo call -- as
    // explicitly closed by the source, not whichever subpath classifyFill()
    // later happens to evaluate.
    callCounters.closePath += 1;
    const state = getPathState(ctx);
    state.closePathCalls += 1;
    const subpath = currentSubpath(state);
    if (subpath) {
      subpath.explicitlyClosed = true;
    }
  }

  // arc(x, y, radius, startAngle, endAngle, anticlockwise) -- tracked
  // entirely independently of the moveTo/lineTo subpath machinery above
  // (see the "Circle observation" section below for why and how this is
  // turned into a circle candidate). Recording the CURRENT transform here,
  // at call time, matches onMoveTo/onLineTo's own approach: transform
  // state can change between this call and fill(), so it must be captured
  // now, not re-read later.
  function onArc(ctx, args) {
    callCounters.arc += 1;
    const state = getPathState(ctx);
    state.arcCallCount += 1;
    if (state.arcCalls.length < MAX_TRACKED_ARCS_PER_STATE) {
      state.arcCalls.push({
        x: finiteNumber(args[0]),
        y: finiteNumber(args[1]),
        radius: finiteNumber(args[2]),
        startAngle: finiteNumber(args[3]),
        endAngle: finiteNumber(args[4]),
        transform: currentTransform(ctx),
      });
    }
  }

  // --- Classification --------------------------------------------------
  //
  // Live evidence (see README) established that a real #ffe869 (Square)
  // fill is two subpaths: one actual polygon contour -- subdivided with
  // extra collinear points along its straight edges -- plus one visually-
  // inert single-point subpath. The pipeline below reconstructs the real
  // shape from that contour rather than inferring "shape-likeness" from
  // aggregate stats, and is shared by all three neutral shape classes:
  //
  //   all tracked subpaths
  //     -> find the unique non-degenerate ("meaningful") polygon subpath
  //     -> merge duplicate/closing points, collapse collinear edge
  //        subdivisions down to the real corners
  //     -> require exactly 3, 4, or 5 corners (the only supported classes)
  //     -> run the regular-polygon geometry check on them
  //     -> cross-check with the area/perimeter self-consistency invariant
  //     -> require the corner count's corresponding neutral-shape color
  //
  // Which subpath is "the" polygon is never hard-coded by position (e.g.
  // "subpaths[0]"): it is whichever single subpath is non-degenerate. If
  // zero or more than one subpath qualifies, the fill is rejected outright
  // rather than guessed at.

  // Perpendicular distance from `point` to the infinite line through `a`
  // and `b` (not the segment) -- exactly what "does B lie on the straight
  // line through its neighbors A and C" needs, without assuming direction.
  function perpendicularDistance(point, a, b) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const lineLength = Math.hypot(dx, dy);
    if (lineLength === 0) {
      return distance(point, a);
    }
    const cross = Math.abs(((point.x - a.x) * dy) - ((point.y - a.y) * dx));
    return cross / lineLength;
  }

  // Merges consecutive points that coincide within GEOMETRY_EPSILON_PX,
  // treating the list as a closed cycle -- this single pass handles both
  // an explicit closing vertex (last point duplicating the first) and any
  // accidental duplicate lineTo, generalized beyond a fixed vertex-count
  // special case to any number of subdivision points.
  function dedupeConsecutive(vertices) {
    const result = [];
    for (const vertex of vertices) {
      const previous = result[result.length - 1];
      if (!previous || distance(previous, vertex) > GEOMETRY_EPSILON_PX) {
        result.push(vertex);
      }
    }
    if (result.length > 1 && distance(result[0], result[result.length - 1]) <= GEOMETRY_EPSILON_PX) {
      result.pop();
    }
    return result;
  }

  // Repeatedly removes any point lying on the straight line through its
  // two neighbors (an edge-subdivision point, not a real corner), treating
  // the list as a closed polygon cycle. Removing one point can reveal a new
  // collinear triplet (e.g. two subdivision points on the same edge), so
  // this iterates until a full pass removes nothing. Never reduces below 3
  // points. Order (and therefore winding) is preserved -- points are only
  // ever removed, never reordered.
  function collapseCollinear(vertices) {
    const points = vertices.slice();
    let removedCount = 0;
    let changed = true;
    while (changed && points.length > 3) {
      changed = false;
      for (let i = 0; i < points.length; i += 1) {
        const a = points[(i - 1 + points.length) % points.length];
        const b = points[i];
        const c = points[(i + 1) % points.length];
        if (perpendicularDistance(b, a, c) <= GEOMETRY_EPSILON_PX) {
          points.splice(i, 1);
          removedCount += 1;
          changed = true;
          break;
        }
      }
    }
    return { vertices: points, removedCount };
  }

  // A subpath is "meaningful" (a real, visually-filled polygon candidate,
  // as opposed to e.g. the inert moveTo-to-center subpath) when -- after
  // merging duplicate points -- it has at least 3 distinct points AND a
  // non-negligible enclosed area. Vertex count alone is not enough: a
  // subpath could in principle carry several coincident/near-coincident
  // points and still enclose no real area.
  function prepareSubpathVertices(subpath) {
    return dedupeConsecutive(subpath.vertices);
  }

  function isMeaningfulPolygon(preparedVertices) {
    if (preparedVertices.length < 3) {
      return false;
    }
    const geometry = polygonGeometry(preparedVertices);
    return geometry !== undefined && Math.abs(geometry.signedArea) > MIN_MEANINGFUL_AREA_PX2;
  }

  // Selects the unique meaningful polygon subpath among all tracked
  // subpaths for this fill(). Never assumes a fixed position (first, last,
  // or otherwise) -- see the module-level note above.
  function selectMeaningfulSubpath(subpaths) {
    const candidates = [];
    for (let index = 0; index < subpaths.length; index += 1) {
      const prepared = prepareSubpathVertices(subpaths[index]);
      if (isMeaningfulPolygon(prepared)) {
        candidates.push({ index, prepared });
      }
    }
    return candidates;
  }

  // Regular-N-gon geometry check, generalized over vertex count: (1) no
  // side may be degenerately short: (2) side lengths must be roughly equal
  // (works for any N); (3) vertex-to-centroid distances ("radii") must be
  // roughly equal -- the generalization of the old Square-only diagonal
  // check (see RADIUS_RATIO_TOLERANCE above).
  function evaluateGeometry(vertices) {
    const geometry = polygonGeometry(vertices);
    const sides = geometry.sides;
    const center = centroid(vertices);
    const radii = vertices.map((vertex) => distance(vertex, center));
    const maxSide = Math.max(...sides);
    const minSide = Math.min(...sides);
    const maxRadius = Math.max(...radii);
    const minRadius = Math.min(...radii);

    let reason;
    if (sides.some((side) => !(side >= MIN_SIDE_LENGTH_PX))) {
      reason = 'degenerate';
    } else if (maxSide / minSide > SIDE_LENGTH_RATIO_TOLERANCE) {
      reason = 'sideRatio';
    } else if (!(minRadius > 0) || maxRadius / minRadius > RADIUS_RATIO_TOLERANCE) {
      reason = 'radiusRatio';
    }

    return {
      ok: reason === undefined,
      reason,
      sides,
      radii,
      sideRatio: minSide > 0 ? maxSide / minSide : undefined,
      radiusRatio: minRadius > 0 ? maxRadius / minRadius : undefined,
    };
  }

  // Secondary, scale-invariant sanity check on the final corners (see
  // AREA_PERIMETER_RATIO_TOLERANCE above). Never called before
  // evaluateGeometry and never used as the sole basis for acceptance.
  function evaluateAreaPerimeterConsistency(vertices, vertexCount) {
    const geometry = polygonGeometry(vertices);
    const area = Math.abs(geometry.signedArea);
    const perimeter = geometry.perimeter;
    const tanTerm = Math.tan(Math.PI / vertexCount);
    const inferredSideFromArea = Math.sqrt((4 * area * tanTerm) / vertexCount);
    const inferredSideFromPerimeter = perimeter / vertexCount;
    const maxSide = Math.max(inferredSideFromArea, inferredSideFromPerimeter);
    const minSide = Math.min(inferredSideFromArea, inferredSideFromPerimeter);
    return {
      ok: minSide > 0 && maxSide / minSide <= AREA_PERIMETER_RATIO_TOLERANCE,
      inferredSideFromArea,
      inferredSideFromPerimeter,
      ratio: minSide > 0 ? maxSide / minSide : undefined,
    };
  }

  function parseColor(value) {
    if (typeof value !== 'string') {
      return undefined;
    }
    const trimmed = value.trim();

    const hexMatch = /^#([0-9a-fA-F]{6})$/.exec(trimmed);
    if (hexMatch) {
      const hex = hexMatch[1];
      return {
        r: parseInt(hex.slice(0, 2), 16),
        g: parseInt(hex.slice(2, 4), 16),
        b: parseInt(hex.slice(4, 6), 16),
      };
    }

    const rgbMatch = /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+\s*)?\)$/.exec(trimmed);
    if (rgbMatch) {
      return { r: Number(rgbMatch[1]), g: Number(rgbMatch[2]), b: Number(rgbMatch[3]) };
    }

    return undefined;
  }

  function colorMatchesClass(fillStyle, className) {
    const parsed = parseColor(fillStyle);
    const expected = SHAPE_CLASSES[className].rgb;
    return (
      parsed !== undefined
      && parsed.r === expected.r
      && parsed.g === expected.g
      && parsed.b === expected.b
    );
  }

  // The single source of truth for "what neutral shape (if any) is this
  // fill()", used by both the production accept/reject path and
  // diagnostics(). Never call this from more than one place with divergent
  // logic, and never maintain three copy-pasted per-class variants of it.
  function classifyFill(ctx) {
    const state = pathState.get(ctx);
    const subpaths = state ? state.subpaths : [];
    const subpathCount = state ? state.moveToCalls : 0;
    const moveToCalls = state ? state.moveToCalls : 0;
    const lineToCalls = state ? state.lineToCalls : 0;
    const rectCalls = state ? state.rectCalls : 0;
    const closePathCalls = state ? state.closePathCalls : 0;
    const fillStyle = safeRead(ctx, 'fillStyle');

    if (state) {
      resetPathState(state);
    }

    const outcome = {
      accepted: false,
      reason: 'noMeaningfulSubpath',
      fillStyle,
      vertexCount: 0,
      moveToCalls,
      lineToCalls,
      rectCalls,
      closePathCalls,
      // Diagnostic-only from here down: full multi-subpath topology for
      // this fill(), independent of which subpath (if any) drives
      // acceptance above.
      subpathCount,
      subpaths,
    };

    if (rectCalls > 0) {
      outcome.reason = 'unsupportedRectPath';
      return outcome;
    }

    const candidates = selectMeaningfulSubpath(subpaths);
    outcome.meaningfulCandidateCount = candidates.length;
    if (candidates.length === 0) {
      outcome.reason = 'noMeaningfulSubpath';
      return outcome;
    }
    if (candidates.length > 1) {
      outcome.reason = 'ambiguousSubpaths';
      return outcome;
    }

    const [selected] = candidates;
    outcome.selectedSubpathIndex = selected.index;
    outcome.vertexCount = selected.prepared.length;
    outcome.preSimplificationVertexCount = selected.prepared.length;

    // A partial transform failure mid-subpath (see onLineTo) leaves earlier
    // vertices intact but the subpath incomplete; the subpath can still
    // have enough distinct points and area to look "meaningful" above, so
    // this must be checked on the selected candidate specifically, not
    // folded into isMeaningfulPolygon.
    if (subpaths[selected.index].transformFailed) {
      outcome.reason = 'transformError';
      return outcome;
    }

    const { vertices: simplified, removedCount } = collapseCollinear(selected.prepared);
    outcome.collinearPointsRemoved = removedCount;
    outcome.simplifiedVertexCount = simplified.length;

    // Which shape class does this corner count even correspond to? This is
    // the ONLY thing corner count decides; color is what decides whether
    // the fill actually IS that class (see below) -- a shape with the
    // right corner count but the wrong color is a colorMismatch, not a
    // wrongVertexCount, because "wrong count" and "wrong color for this
    // count" are different, equally real failure modes worth telling apart
    // in diagnostics.
    const expectedClass = VERTEX_COUNT_TO_CLASS[simplified.length];
    if (expectedClass === undefined) {
      outcome.reason = 'wrongVertexCount';
      return outcome;
    }
    outcome.vertices = simplified;
    outcome.candidateClass = expectedClass;

    let geometry;
    try {
      geometry = evaluateGeometry(simplified);
    } catch (_error) {
      outcome.reason = 'geometryError';
      return outcome;
    }
    outcome.geometry = geometry;
    outcome.areaPerimeter = evaluateAreaPerimeterConsistency(simplified, simplified.length);

    if (!geometry.ok) {
      outcome.reason = geometry.reason;
      return outcome;
    }

    if (!outcome.areaPerimeter.ok) {
      outcome.reason = 'areaPerimeterMismatch';
      return outcome;
    }

    if (!colorMatchesClass(fillStyle, expectedClass)) {
      outcome.reason = 'colorMismatch';
      return outcome;
    }

    outcome.accepted = true;
    outcome.shapeClass = expectedClass;
    outcome.reason = 'accepted';
    return outcome;
  }

  // --- Circle observation (generic filled-circle candidates) ---------------
  //
  // Separate from, and never interacting with, the neutral-shape
  // classification above: a "circle candidate" carries no class/color
  // contract, no ownership, and no entity identity -- only what is
  // actually observable from a beginPath()/arc()/fill() call: a
  // screen-mapped center and radius. Conservative by construction: any
  // fill() whose path was not EXACTLY one full-circle arc() call and
  // nothing else is rejected outright rather than guessed at (a wedge, a
  // ring, a path mixing arc() with lineTo/rect, or more than one arc()
  // call are all rejected, not approximated). A canvas whose transform is
  // not a similarity (uniform scale + rotation, no shear) would turn a
  // true circle into an ellipse on screen; rather than invent a radius for
  // that case, such an arc is rejected too (see transformScaleInfo).

  // Does this 2D linear transform preserve circles as circles (a
  // "similarity": uniform scale + rotation/reflection, no shear/skew)? The
  // transform's two basis vectors -- (a,b) for local +x, (c,d) for local
  // +y -- must have equal length (uniform scale in both directions) and be
  // perpendicular (no shear). Returns undefined if the matrix itself is
  // unreadable/non-finite or degenerate (zero scale in either direction).
  function transformScaleInfo(matrix) {
    const a = finiteNumber(safeRead(matrix, 'a'));
    const b = finiteNumber(safeRead(matrix, 'b'));
    const c = finiteNumber(safeRead(matrix, 'c'));
    const d = finiteNumber(safeRead(matrix, 'd'));
    if (a === undefined || b === undefined || c === undefined || d === undefined) {
      return undefined;
    }
    const scaleX = Math.hypot(a, b);
    const scaleY = Math.hypot(c, d);
    if (!(scaleX > 0) || !(scaleY > 0)) {
      return undefined;
    }
    const normalizedDot = Math.abs((a * c) + (b * d)) / (scaleX * scaleY);
    const scaleRatio = Math.max(scaleX, scaleY) / Math.min(scaleX, scaleY);
    const uniform = (
      normalizedDot <= CIRCLE_TRANSFORM_ORTHOGONALITY_TOLERANCE
      && scaleRatio <= CIRCLE_TRANSFORM_SCALE_RATIO_TOLERANCE
    );
    return { scale: (scaleX + scaleY) / 2, uniform };
  }

  // The single source of truth for "is this fill() a clean, screen-mapped
  // filled circle" -- mirrors classifyFill's role for polygons, but is
  // entirely independent of it (never shares acceptance state, never
  // affects Square/Triangle/Pentagon classification, and vice versa).
  // `nonArcOpCount` is moveTo+lineTo+rect calls seen on the SAME path --
  // any of those alongside an arc() means this was not a plain filled
  // circle (e.g. a moveTo back to center for a pie wedge), so it disqualifies
  // the candidate rather than trying to interpret the mixed path.
  function classifyCircleFill(arcCallCount, firstArc, nonArcOpCount, fillStyle) {
    const outcome = { accepted: false, reason: 'noArc', fillStyle };
    if (arcCallCount === 0) {
      return outcome;
    }
    if (nonArcOpCount > 0) {
      outcome.reason = 'mixedPathOps';
      return outcome;
    }
    if (arcCallCount > 1) {
      outcome.reason = 'multipleArcs';
      return outcome;
    }
    if (
      firstArc === undefined
      || firstArc.x === undefined || firstArc.y === undefined
      || firstArc.radius === undefined || !(firstArc.radius > 0)
      || firstArc.startAngle === undefined || firstArc.endAngle === undefined
    ) {
      outcome.reason = 'malformedArc';
      return outcome;
    }

    const span = Math.abs(firstArc.endAngle - firstArc.startAngle);
    if (span < (2 * Math.PI) - CIRCLE_FULL_ARC_ANGLE_EPS) {
      outcome.reason = 'partialArc';
      return outcome;
    }

    const center = transformPoint(firstArc.transform, firstArc.x, firstArc.y);
    if (center === undefined) {
      outcome.reason = 'transformError';
      return outcome;
    }

    const scaleInfo = transformScaleInfo(firstArc.transform);
    if (scaleInfo === undefined || !scaleInfo.uniform) {
      outcome.reason = 'nonUniformTransform';
      return outcome;
    }

    const radius = firstArc.radius * scaleInfo.scale;
    if (!(radius >= MIN_CIRCLE_RADIUS_PX) || !Number.isFinite(radius)) {
      outcome.reason = 'degenerateRadius';
      return outcome;
    }

    outcome.accepted = true;
    outcome.cx = center.x;
    outcome.cy = center.y;
    outcome.radius = radius;
    return outcome;
  }

  function buildCircleRecord(circleOutcome) {
    const record = {
      cx: circleOutcome.cx,
      cy: circleOutcome.cy,
      radius: circleOutcome.radius,
      timestamp: now(),
      source: 'canvas2d',
    };
    addValue(record, 'color', typeof circleOutcome.fillStyle === 'string' ? circleOutcome.fillStyle : undefined);
    return record;
  }

  function pruneCircleCache(currentTime) {
    for (const [key, record] of circleCache) {
      if (currentTime - record.timestamp > CACHE_WINDOW_MS) {
        circleCache.delete(key);
        circleCounters.prunedTotal += 1;
      }
    }
  }

  function recordCircle(record) {
    pruneCircleCache(record.timestamp);
    if (circleCache.size >= MAX_TRACKED_CIRCLES) {
      const oldestKey = circleCache.keys().next().value;
      circleCache.delete(oldestKey);
    }
    circleCache.set(nextCircleId, record);
    nextCircleId += 1;
  }

  // --- Canvas provenance ---------------------------------------------------
  //
  // Which canvas element is snapshot.shapes' coordinate space actually
  // relative to? A fill() call alone doesn't answer that (a page can have
  // several canvases, and nothing about fill() says which one is the real,
  // on-screen game surface) -- only "this canvas is where we just accepted
  // a real neutral shape" is real evidence. Even that is only trustworthy
  // when it is unambiguous (exactly one canvas has recently produced
  // accepted shapes) and the canvas is actually laid out on screen
  // (positive backing-store dimensions AND a positive CSS bounding rect --
  // a detached/display:none/0x0 canvas fails this even if it happens to be
  // the one fill() was called on). Contract: snapshot.canvas is emitted
  // ONLY when both hold; otherwise it is omitted entirely (never a guess),
  // and downstream (deep.eye.oh) continues to fail closed on a missing
  // canvas exactly as it already does.

  function recordCanvasProvenance(canvas, timestamp) {
    if (canvas == null) {
      return;
    }
    let entry = canvasProvenance.get(canvas);
    if (!entry) {
      if (canvasProvenance.size >= MAX_TRACKED_CANVASES) {
        // Evict the least-recently-accepted canvas (not insertion order)
        // to make room -- a canvas that stopped producing accepted shapes
        // a while ago is the least likely to still be the real game
        // canvas.
        let oldestCanvas;
        let oldestTime = Infinity;
        for (const [trackedCanvas, trackedEntry] of canvasProvenance) {
          if (trackedEntry.lastAcceptedAt < oldestTime) {
            oldestTime = trackedEntry.lastAcceptedAt;
            oldestCanvas = trackedCanvas;
          }
        }
        if (oldestCanvas !== undefined) {
          canvasProvenance.delete(oldestCanvas);
        }
      }
      entry = {
        id: nextCanvasTrackId, acceptedCount: 0, firstSeenAt: timestamp, lastAcceptedAt: timestamp,
      };
      nextCanvasTrackId += 1;
      canvasProvenance.set(canvas, entry);
    }
    entry.acceptedCount += 1;
    entry.lastAcceptedAt = timestamp;
  }

  // Reuses the same CACHE_WINDOW_MS "recent observation" heuristic
  // shapeCache itself uses (see that constant's comment) -- a canvas that
  // stopped producing accepted shapes within this window is no longer
  // trusted as "the" current shape canvas (e.g. the game switched
  // rendering targets, or that canvas was actually an unrelated
  // coincidence and simply stopped being drawn to).
  function pruneCanvasProvenance(currentTime) {
    for (const [canvas, entry] of canvasProvenance) {
      if (currentTime - entry.lastAcceptedAt > CACHE_WINDOW_MS) {
        canvasProvenance.delete(canvas);
      }
    }
  }

  // Reads a canvas element's own geometry (backing store + CSS rect) and
  // returns undefined unless every dimension involved is a positive,
  // finite number -- see the "screen-mappable" contract in this section's
  // module comment. Never treats a canvas as valid based on partial data.
  function readCanvasGeometry(canvas) {
    const width = finiteNumber(safeRead(canvas, 'width'));
    const height = finiteNumber(safeRead(canvas, 'height'));
    const rect = safeCall(safeRead(canvas, 'getBoundingClientRect'), canvas).value;
    const rectLeft = finiteNumber(safeRead(rect, 'left'));
    const rectTop = finiteNumber(safeRead(rect, 'top'));
    const rectWidth = finiteNumber(safeRead(rect, 'width'));
    const rectHeight = finiteNumber(safeRead(rect, 'height'));
    if (
      width === undefined || height === undefined
      || rectLeft === undefined || rectTop === undefined
      || rectWidth === undefined || rectHeight === undefined
      || !(width > 0) || !(height > 0) || !(rectWidth > 0) || !(rectHeight > 0)
    ) {
      return undefined;
    }
    return {
      width,
      height,
      clientWidth: finiteNumber(safeRead(canvas, 'clientWidth')),
      clientHeight: finiteNumber(safeRead(canvas, 'clientHeight')),
      rect: {
        left: rectLeft, top: rectTop, width: rectWidth, height: rectHeight,
      },
      devicePixelRatio: finiteNumber(safeRead(window, 'devicePixelRatio')),
    };
  }

  // The single source of truth for "which canvas (if any) is
  // snapshot.canvas allowed to describe right now" -- shared by
  // canvasInfo() (production) and canvasProvenanceDiagnostics() (so
  // diagnostics can mark exactly the same canvas as `selected`, never a
  // second, divergent notion of "the" canvas). Prunes as a side effect.
  // Returns undefined (fail closed, never a guess) unless exactly one
  // canvas has recently produced an accepted shape AND that canvas is
  // currently screen-mappable.
  function selectSingleScreenMappableCanvas(currentTime) {
    pruneCanvasProvenance(currentTime);
    if (canvasProvenance.size !== 1) {
      return undefined;
    }
    const [[canvas, entry]] = canvasProvenance;
    const geometry = readCanvasGeometry(canvas);
    if (geometry === undefined) {
      return undefined;
    }
    return { canvas, entry, geometry };
  }

  function canvasInfo() {
    const selected = selectSingleScreenMappableCanvas(now());
    return selected ? selected.geometry : undefined;
  }

  // JSON-safe, no-DOM-objects diagnostic view of every currently-tracked
  // canvas (not just the selected one) -- so "why is snapshot.canvas
  // missing/wrong" is answerable from diagnostics() alone: how many
  // distinct canvases are being tracked, each one's backing/CSS geometry,
  // isConnected, accepted-shape count, and which one (if any) is selected.
  function buildCanvasProvenanceEntry(canvas, entry, currentTime, selectedCanvas) {
    const width = finiteNumber(safeRead(canvas, 'width'));
    const height = finiteNumber(safeRead(canvas, 'height'));
    const rect = safeCall(safeRead(canvas, 'getBoundingClientRect'), canvas).value;
    const rectWidth = finiteNumber(safeRead(rect, 'width'));
    const rectHeight = finiteNumber(safeRead(rect, 'height'));
    const isConnected = safeRead(canvas, 'isConnected');

    const result = {
      id: entry.id,
      acceptedShapeCount: entry.acceptedCount,
      ageOfLastAcceptedMs: currentTime - entry.lastAcceptedAt,
      selected: canvas === selectedCanvas,
    };
    addValue(result, 'width', width);
    addValue(result, 'height', height);
    addValue(result, 'clientWidth', finiteNumber(safeRead(canvas, 'clientWidth')));
    addValue(result, 'clientHeight', finiteNumber(safeRead(canvas, 'clientHeight')));
    addValue(result, 'rectLeft', finiteNumber(safeRead(rect, 'left')));
    addValue(result, 'rectTop', finiteNumber(safeRead(rect, 'top')));
    addValue(result, 'rectWidth', rectWidth);
    addValue(result, 'rectHeight', rectHeight);
    if (typeof isConnected === 'boolean') {
      result.isConnected = isConnected;
    }
    result.screenMappable = (
      width !== undefined && height !== undefined && rectWidth !== undefined && rectHeight !== undefined
      && width > 0 && height > 0 && rectWidth > 0 && rectHeight > 0
    );
    return result;
  }

  function canvasProvenanceDiagnostics() {
    const currentTime = now();
    const selected = selectSingleScreenMappableCanvas(currentTime);
    const canvases = Array.from(canvasProvenance.entries()).map(
      ([canvas, entry]) => buildCanvasProvenanceEntry(canvas, entry, currentTime, selected ? selected.canvas : undefined),
    );
    return {
      distinctCanvasesTracked: canvases.length,
      distinctCanvasesEverSeen: nextCanvasTrackId,
      canvases,
    };
  }

  // --- Accepted-shape cache ---------------------------------------------

  function buildRecord(shapeClass, vertices, fillStyle, ctx) {
    const bbox = computeBBox(vertices);
    const canvas = safeRead(ctx, 'canvas');
    const center = centroid(vertices);
    const radius = vertices.reduce((sum, v) => sum + distance(v, center), 0) / vertices.length;
    const classInfo = SHAPE_CLASSES[shapeClass];

    const record = {
      kind: classInfo.kind,
      class: shapeClass,
      vertices: vertices.map((vertex) => ({ x: vertex.x, y: vertex.y })),
      cx: (bbox.x0 + bbox.x1) / 2,
      cy: (bbox.y0 + bbox.y1) / 2,
      bbox,
      halfSize: 0.5 * Math.max(bbox.x1 - bbox.x0, bbox.y1 - bbox.y0),
      radius,
      color: classInfo.hex,
      timestamp: now(),
      source: 'canvas2d',
    };
    addValue(record, 'rawFillStyle', typeof fillStyle === 'string' ? fillStyle : undefined);
    addValue(record, 'canvasWidth', finiteNumber(safeRead(canvas, 'width')));
    addValue(record, 'canvasHeight', finiteNumber(safeRead(canvas, 'height')));
    addValue(record, 'devicePixelRatio', finiteNumber(safeRead(window, 'devicePixelRatio')));
    return record;
  }

  function cacheKey(record) {
    return `${Math.round(record.cx)}:${Math.round(record.cy)}:${record.color}`;
  }

  function pruneCache(currentTime) {
    for (const [key, record] of shapeCache) {
      if (currentTime - record.timestamp > CACHE_WINDOW_MS) {
        shapeCache.delete(key);
        cacheCounters.prunedTotal += 1;
      }
    }
  }

  function recordShape(record) {
    pruneCache(record.timestamp);
    shapeCache.set(cacheKey(record), record);
  }

  // --- Diagnostics ---------------------------------------------------------

  function histogramKey(count) {
    return count > HISTOGRAM_BUCKET_CAP ? `${HISTOGRAM_BUCKET_CAP}+` : String(count);
  }

  function bumpHistogramKey(histogram, key) {
    histogram[key] = (histogram[key] || 0) + 1;
  }

  function bumpHistogram(histogram, count) {
    bumpHistogramKey(histogram, histogramKey(count));
  }

  // e.g. two subpaths with pointCount 6 and 1 -> "6,1". Bounded by
  // MAX_TRACKED_SUBPATHS entries, each drawn from the same capped
  // HISTOGRAM_BUCKET_CAP alphabet as histogramKey() -- this keeps the
  // histogram's key space bounded rather than growing per distinct fill.
  function topologySignature(subpaths) {
    if (!Array.isArray(subpaths) || subpaths.length === 0) {
      return 'none';
    }
    return subpaths.map((subpath) => histogramKey(subpath.pointCount)).join(',');
  }

  function buildSubpathSample(subpath, index) {
    const entry = {
      index,
      vertexCount: subpath.pointCount,
      lineToCount: subpath.lineToCount,
      explicitlyClosed: subpath.explicitlyClosed,
      transformFailed: subpath.transformFailed,
      rawVertices: subpath.rawVertices.map((vertex) => ({ x: vertex.x, y: vertex.y })),
      vertices: subpath.vertices.map((vertex) => ({ x: vertex.x, y: vertex.y })),
    };
    const geometry = polygonGeometry(subpath.vertices);
    if (geometry) {
      entry.geometry = geometry;
    }
    return entry;
  }

  function buildSample(outcome) {
    const sample = {
      reason: outcome.reason,
      vertexCount: outcome.vertexCount,
      moveToCalls: outcome.moveToCalls,
      lineToCalls: outcome.lineToCalls,
      rectCalls: outcome.rectCalls,
      closePathCalls: outcome.closePathCalls,
      subpathCount: outcome.subpathCount,
      timestamp: now(),
    };
    addValue(sample, 'fillStyle', typeof outcome.fillStyle === 'string' ? outcome.fillStyle : undefined);
    addValue(sample, 'meaningfulCandidateCount', outcome.meaningfulCandidateCount);
    addValue(sample, 'selectedSubpathIndex', outcome.selectedSubpathIndex);
    addValue(sample, 'preSimplificationVertexCount', outcome.preSimplificationVertexCount);
    addValue(sample, 'simplifiedVertexCount', outcome.simplifiedVertexCount);
    addValue(sample, 'collinearPointsRemoved', outcome.collinearPointsRemoved);
    addValue(sample, 'candidateClass', outcome.candidateClass);

    if (Array.isArray(outcome.vertices)) {
      sample.vertices = outcome.vertices.map((vertex) => ({ x: vertex.x, y: vertex.y }));
      sample.bbox = computeBBox(outcome.vertices);
    }

    if (outcome.geometry) {
      const geometry = {};
      addValue(geometry, 'sides', outcome.geometry.sides ? outcome.geometry.sides.slice() : undefined);
      addValue(geometry, 'radii', outcome.geometry.radii ? outcome.geometry.radii.slice() : undefined);
      addValue(geometry, 'sideRatio', finiteNumber(outcome.geometry.sideRatio));
      addValue(geometry, 'radiusRatio', finiteNumber(outcome.geometry.radiusRatio));
      sample.geometry = geometry;
    }

    if (outcome.areaPerimeter) {
      const areaPerimeter = {};
      addValue(areaPerimeter, 'inferredSideFromArea', finiteNumber(outcome.areaPerimeter.inferredSideFromArea));
      addValue(areaPerimeter, 'inferredSideFromPerimeter', finiteNumber(outcome.areaPerimeter.inferredSideFromPerimeter));
      addValue(areaPerimeter, 'ratio', finiteNumber(outcome.areaPerimeter.ratio));
      sample.areaPerimeter = areaPerimeter;
    }

    if (Array.isArray(outcome.subpaths)) {
      sample.subpaths = outcome.subpaths.map((subpath, index) => buildSubpathSample(subpath, index));
    }

    return sample;
  }

  // A JSON round-trip guarantees both detachment (no shared references back
  // into internal state) and JSON-safety in one step -- appropriate here
  // since diagnostics() is not a hot path (popup polling, at most a few
  // Hz), and the nested subpath/geometry structure is deep enough that a
  // hand-rolled deep clone would be easy to get subtly wrong.
  function cloneSample(sample) {
    return JSON.parse(JSON.stringify(sample));
  }

  // Shared per-class bookkeeping (see makeClassStats above) -- called once
  // per known class name for every fill(), scoped by whether fillStyle
  // matches THAT class's color. This is the one place the old Square-only
  // squareColor* bookkeeping logic lives now; it is not copy-pasted.
  function recordClassDiagnostics(className, outcome) {
    if (!colorMatchesClass(outcome.fillStyle, className)) {
      return;
    }
    const stats = classStats.get(className);
    stats.counters.fillsSeen += 1;
    bumpHistogram(stats.vertexHistogram, outcome.vertexCount);
    bumpHistogram(stats.subpathCountHistogram, outcome.subpathCount);
    bumpHistogramKey(stats.topologyHistogram, topologySignature(outcome.subpaths));

    if (typeof outcome.meaningfulCandidateCount === 'number') {
      if (outcome.meaningfulCandidateCount === 0) {
        stats.counters.noMeaningfulPolygon += 1;
      } else if (outcome.meaningfulCandidateCount > 1) {
        stats.counters.ambiguousMeaningfulPolygons += 1;
      } else {
        stats.counters.meaningfulSinglePolygon += 1;
        if (typeof outcome.simplifiedVertexCount === 'number') {
          if (outcome.simplifiedVertexCount === SHAPE_CLASSES[className].vertexCount) {
            stats.counters.simplifiedToExpectedCount += 1;
          } else {
            stats.counters.simplificationFailed += 1;
          }
        }
      }
    }

    if (outcome.accepted) {
      stats.counters.accepted += 1;
      return;
    }

    stats.counters.rejected += 1;
    if (stats.rejectedSamples.length < REJECTED_SAMPLE_CAP) {
      stats.rejectedSamples.push(buildSample(outcome));
    }
  }

  function recordDiagnostics(outcome) {
    bumpHistogram(vertexHistogram, outcome.vertexCount);

    for (const className of CLASS_NAMES) {
      recordClassDiagnostics(className, outcome);
    }

    if (outcome.accepted) {
      if (acceptedSamples.length < ACCEPTED_SAMPLE_CAP) {
        acceptedSamples.push(buildSample(outcome));
      }
      return;
    }

    if (Object.hasOwn(rejectionReasonCounters, outcome.reason)) {
      rejectionReasonCounters[outcome.reason] += 1;
    } else {
      rejectionReasonCounters.other += 1;
    }
  }

  function onFill(ctx) {
    callCounters.fill += 1;

    // Must be captured BEFORE classifyFill(ctx) below, which resets this
    // same per-ctx path state as part of its own bookkeeping -- circle
    // classification is independent of classifyFill, but shares the one
    // underlying path-state object.
    const stateBeforeClassify = pathState.get(ctx);
    const arcCallCount = stateBeforeClassify ? stateBeforeClassify.arcCallCount : 0;
    const firstArc = stateBeforeClassify && stateBeforeClassify.arcCalls.length > 0
      ? stateBeforeClassify.arcCalls[0]
      : undefined;
    const nonArcOpCount = stateBeforeClassify
      ? stateBeforeClassify.moveToCalls + stateBeforeClassify.lineToCalls + stateBeforeClassify.rectCalls
      : 0;

    let outcome;
    try {
      outcome = classifyFill(ctx);
    } catch (_error) {
      outcome = {
        accepted: false,
        reason: 'other',
        vertexCount: 0,
        moveToCalls: 0,
        lineToCalls: 0,
        rectCalls: 0,
        closePathCalls: 0,
        subpathCount: 0,
        subpaths: [],
        meaningfulCandidateCount: 0,
      };
    }

    if (outcome.accepted) {
      try {
        const record = buildRecord(outcome.shapeClass, outcome.vertices, outcome.fillStyle, ctx);
        recordShape(record);
        recordCanvasProvenance(safeRead(ctx, 'canvas'), record.timestamp);
        cacheCounters.acceptedTotal += 1;
      } catch (_error) {
        outcome.accepted = false;
        outcome.reason = 'cacheError';
      }
    }

    recordDiagnostics(outcome);

    // Independent of the neutral-shape path above: never establishes
    // canvas provenance (see the "Circle observation" section's module
    // comment and canvas provenance's own invariant above it), never
    // affects outcome/shape acceptance either direction.
    let circleOutcome;
    try {
      circleOutcome = classifyCircleFill(arcCallCount, firstArc, nonArcOpCount, outcome.fillStyle);
    } catch (_error) {
      circleOutcome = { accepted: false, reason: 'other' };
    }
    if (circleOutcome.accepted) {
      try {
        recordCircle(buildCircleRecord(circleOutcome));
        circleCounters.acceptedTotal += 1;
      } catch (_error) {
        circleCounters.rejectedTotal += 1;
      }
    } else if (circleOutcome.reason !== 'noArc') {
      // 'noArc' (the overwhelming majority of ordinary polygon fills) is
      // not counted as a rejection -- it is simply "not a circle fill",
      // not a circle candidate that failed.
      circleCounters.rejectedTotal += 1;
    }
  }

  function diagnostics() {
    const currentTime = now();
    pruneCache(currentTime);
    pruneCircleCache(currentTime);

    const result = {
      version: VERSION,
      ready: hooksInstalled,
      uptimeMs: currentTime - startTime,
      calls: { ...callCounters },
      rejectionReasons: { ...rejectionReasonCounters },
      vertexHistogram: { ...vertexHistogram },
      cache: {
        acceptedTotal: cacheCounters.acceptedTotal,
        currentlyCached: shapeCache.size,
        prunedTotal: cacheCounters.prunedTotal,
      },
      canvasProvenance: canvasProvenanceDiagnostics(),
      circleCache: {
        acceptedTotal: circleCounters.acceptedTotal,
        rejectedTotal: circleCounters.rejectedTotal,
        currentlyCached: circleCache.size,
        prunedTotal: circleCounters.prunedTotal,
      },
      acceptedSamples: acceptedSamples.map(cloneSample),
      // Backward-compatible top-level alias for the Square class
      // specifically (the field names the popup and existing tests read).
      rejectedSquareColorSamples: classStats.get('square').rejectedSamples.map(cloneSample),
    };

    for (const className of CLASS_NAMES) {
      const stats = classStats.get(className);
      const prefix = className === 'square' ? 'square' : className;
      result[`${prefix}Color`] = { ...stats.counters };
      // Square keeps its original field name (simplifiedToQuad) for
      // backward compatibility; the other two classes use the generic
      // name set in makeClassStats.
      if (className === 'square') {
        result.squareColor.simplifiedToQuad = stats.counters.simplifiedToExpectedCount;
        delete result.squareColor.simplifiedToExpectedCount;
      }
      result[`${prefix}ColorVertexHistogram`] = { ...stats.vertexHistogram };
      result[`${prefix}ColorSubpathCountHistogram`] = { ...stats.subpathCountHistogram };
      result[`${prefix}ColorTopologyHistogram`] = { ...stats.topologyHistogram };
    }

    return result;
  }

  // --- Hook installation ---------------------------------------------------

  function hookMethod(proto, name, observe) {
    const original = safeRead(proto, name);
    if (typeof original !== 'function') {
      return false;
    }
    Object.defineProperty(proto, name, {
      value(...args) {
        const result = Reflect.apply(original, this, args);
        try {
          observe(this, args);
        } catch (_error) {
          // Observation must never affect real rendering.
        }
        return result;
      },
      writable: true,
      configurable: true,
      enumerable: false,
    });
    return true;
  }

  function installHooks() {
    const proto = safeRead(safeRead(window, 'CanvasRenderingContext2D'), 'prototype');
    if (proto == null) {
      return false;
    }
    const installedBeginPath = hookMethod(proto, 'beginPath', onBeginPath);
    const installedMoveTo = hookMethod(proto, 'moveTo', onMoveTo);
    const installedLineTo = hookMethod(proto, 'lineTo', onLineTo);
    const installedFill = hookMethod(proto, 'fill', onFill);
    // Diagnostic-only hooks; their success/failure does not affect isReady().
    hookMethod(proto, 'rect', onRect);
    hookMethod(proto, 'closePath', onClosePath);
    // Circle observation is an additional, independent capability (see the
    // "Circle observation" section above) -- its absence must not affect
    // isReady(), which reports readiness of the core neutral-shape path.
    hookMethod(proto, 'arc', onArc);
    return installedBeginPath && installedMoveTo && installedLineTo && installedFill;
  }

  function cloneRecord(record) {
    return {
      ...record,
      vertices: record.vertices.map((vertex) => ({ x: vertex.x, y: vertex.y })),
      bbox: { ...record.bbox },
    };
  }

  function shapes() {
    const currentTime = now();
    pruneCache(currentTime);
    return Array.from(shapeCache.values()).map(cloneRecord);
  }

  // Generic recent circle candidates -- same canvas-backing coordinate
  // system as shapes(), same rolling-cache freshness contract, but no
  // class/ownership/identity fields (see the "Circle observation" section
  // above). Each object is already a plain, JSON-safe value (no shared
  // references into internal state), so unlike cloneRecord/shapes() no
  // per-entry deep-copy is needed beyond the shallow spread.
  function circles() {
    const currentTime = now();
    pruneCircleCache(currentTime);
    return Array.from(circleCache.values()).map((record) => ({ ...record }));
  }

  function snapshot() {
    const info = canvasInfo();
    const result = {
      metadata: { oracleVersion: VERSION },
      timestamps: { performanceNow: now(), wallClockMs: wallClockNow() },
      browser: { devicePixelRatio: finiteNumber(safeRead(window, 'devicePixelRatio')) },
      shapes: shapes(),
      circles: circles(),
    };
    if (info !== undefined) {
      result.canvas = info;
    }
    return result;
  }

  function isReady() {
    return hooksInstalled;
  }

  hooksInstalled = installHooks();
  startTime = now();

  const oracle = Object.freeze({
    version: VERSION,
    isReady,
    snapshot,
    shapes,
    circles,
    diagnostics,
  });

  Object.defineProperty(window, 'deepEyeOracle', {
    value: oracle,
    enumerable: true,
    configurable: false,
    writable: false,
  });
})();
