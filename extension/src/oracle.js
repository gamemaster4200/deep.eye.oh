(() => {
  'use strict';

  if (safeRead(window, 'deepEyeOracle')) {
    return;
  }

  const VERSION = '0.4.0';

  // diep.io's official client fills neutral Squares with this color. Chrome
  // may read the fillStyle setter back as either the hex form or its
  // computed rgb() equivalent, so both are accepted.
  const NEUTRAL_SQUARE_HEX = '#ffe869';
  const NEUTRAL_SQUARE_RGB = Object.freeze({ r: 255, g: 232, b: 105 });

  // Generous, dataset-independent tolerances for rendering/anti-aliasing
  // jitter. Not tuned against any vision holdout.
  const SIDE_LENGTH_RATIO_TOLERANCE = 1.35;
  const DIAGONAL_RATIO_TOLERANCE = 1.35;
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
  // inert (no meaningfully filled region), not a degenerate-but-real quad.
  // Tied to the same MIN_SIDE_LENGTH_PX noise floor used for individual
  // side lengths elsewhere in this file (0.5px), squared: a shape that
  // would fail MIN_SIDE_LENGTH_PX on every side has an area at or below
  // this scale purely from positional noise, before any real geometry.
  const MIN_MEANINGFUL_AREA_PX2 = MIN_SIDE_LENGTH_PX * MIN_SIDE_LENGTH_PX;

  // For an ideal square, sqrt(area) and perimeter/4 are the same quantity
  // exactly, regardless of absolute size or rotation. This is a secondary,
  // scale-invariant self-consistency check on top of the side/diagonal
  // checks below -- not a replacement for them, and not calibrated against
  // any specific observed pixel measurement (which would vary with zoom/
  // canvas size across sessions); only against how far the two independent
  // ways of inferring "the side length" of a true square are allowed to
  // disagree before something other than corner-recovery noise is suspected.
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
  // (oldest first) once this cap is exceeded, but the most recent subpath
  // -- the one classifyFill() below evaluates -- is never evicted by this,
  // since eviction only ever removes from the front of the list.
  const MAX_TRACKED_SUBPATHS = 6;

  // There is no reliable, crash-safe frame boundary available (see README:
  // hooking requestAnimationFrame is the upstream diepAPI crash path this
  // project deliberately avoids). Instead, each detected square is cached
  // with a timestamp and shapes()/snapshot() only return entries seen within
  // this rolling window. This is a heuristic "recent observation" cache, not
  // a synchronized frame model: it can include a square from slightly more
  // than one frame ago, and it can briefly miss one if the game skips a
  // render. 250ms tolerates multi-frame stalls at typical frame rates while
  // still dropping squares that stopped being drawn (e.g. left the screen).
  const CACHE_WINDOW_MS = 250;

  const HISTOGRAM_BUCKET_CAP = 16;
  const ACCEPTED_SAMPLE_CAP = 20;
  const REJECTED_SAMPLE_CAP = 30;

  const pathState = new WeakMap();
  const squareCache = new Map();
  let hooksInstalled = false;
  let startTime = 0;

  const callCounters = {
    beginPath: 0, moveTo: 0, lineTo: 0, rect: 0, closePath: 0, fill: 0,
  };
  const squareColorCounters = {
    fillsSeen: 0,
    accepted: 0,
    rejected: 0,
    // Subpath-selection stage (see selectMeaningfulSubpath below).
    meaningfulSinglePolygon: 0,
    noMeaningfulPolygon: 0,
    ambiguousMeaningfulPolygons: 0,
    // Collinear-simplification stage (see collapseCollinear below).
    simplifiedToQuad: 0,
    simplificationFailed: 0,
  };
  const rejectionReasonCounters = {
    noMeaningfulSubpath: 0,
    ambiguousSubpaths: 0,
    wrongVertexCount: 0,
    degenerate: 0,
    sideRatio: 0,
    diagonalRatio: 0,
    areaPerimeterMismatch: 0,
    colorMismatch: 0,
    unsupportedRectPath: 0,
    transformError: 0,
    geometryError: 0,
    cacheError: 0,
    other: 0,
  };
  const vertexHistogram = {};
  const squareColorVertexHistogram = {};
  // Diagnostic-only, added alongside the single-subpath vertexHistogram
  // above (which still reflects only the LAST subpath, matching
  // classifyFill()'s acceptance-relevant view -- see the note on
  // classifyFill below). These two see every subpath of every #ffe869 fill,
  // to answer "what does the real renderer draw in one fill() call" without
  // guessing from aggregate counts.
  const squareColorSubpathCountHistogram = {};
  const squareColorTopologyHistogram = {};
  const cacheCounters = { acceptedTotal: 0, prunedTotal: 0 };
  const acceptedSamples = [];
  const rejectedSquareColorSamples = [];

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

  // Generic polygon geometry for diagnostics, valid for any vertex count
  // (unlike evaluateGeometry below, which is quad-specific and drives
  // acceptance). Implicitly closes back to vertices[0], matching how
  // Canvas2D's fill() renders an unclosed subpath, regardless of whether
  // closePath() was actually called. Returns undefined for fewer than 2
  // points, where no meaningful shape exists yet (e.g. a lone moveTo point).
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
  // subpaths; every one is now tracked (bounded by MAX_TRACKED_SUBPATHS),
  // not just the most recent. Live evidence showed the official client's
  // #ffe869 fills are consistently two subpaths (a real, multi-vertex first
  // subpath followed by a trailing one-point second subpath), which the
  // prior single-subpath model silently discarded down to that trailing
  // point on every occurrence. classifyFill() below still bases ACCEPTANCE
  // only on the last subpath -- identical to the prior model's behavior --
  // this slice only adds visibility into every subpath's real topology, it
  // does not change what gets accepted as a Square.

  function freshPathState() {
    return {
      subpaths: [],
      moveToCalls: 0,
      lineToCalls: 0,
      rectCalls: 0,
      closePathCalls: 0,
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
    // is hit, so the most recently started subpath -- what classifyFill()
    // below actually evaluates -- is always present.
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
    // rect()-built paths as squares is deliberately not implemented yet
    // (see README); this hook only makes their presence visible instead of
    // silently mis-detecting or silently missing them.
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

  // --- Classification --------------------------------------------------
  //
  // Live evidence (see README) established that a real #ffe869 fill is two
  // subpaths: one actual polygon contour -- a square, subdivided with extra
  // collinear points along its four edges -- plus one visually-inert
  // single-point subpath (a moveTo call to the center with no lineTo calls, filling no
  // area). The pipeline below reconstructs the real square from that
  // contour rather than inferring "square-likeness" from aggregate stats:
  //
  //   all tracked subpaths
  //     -> find the unique non-degenerate ("meaningful") polygon subpath
  //     -> merge duplicate/closing points, collapse collinear edge
  //        subdivisions down to the real corners
  //     -> require exactly 4 corners
  //     -> run existing side/diagonal square geometry checks on them
  //     -> cross-check with the area/perimeter self-consistency invariant
  //     -> require the neutral-square color
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
  // accidental duplicate lineTo, generalized beyond the old fixed 4-vs-5
  // vertex special case to any number of subdivision points.
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

  function evaluateGeometry(vertices) {
    const [p0, p1, p2, p3] = vertices;
    const sides = [distance(p0, p1), distance(p1, p2), distance(p2, p3), distance(p3, p0)];
    const diagonals = [distance(p0, p2), distance(p1, p3)];
    const maxSide = Math.max(...sides);
    const minSide = Math.min(...sides);
    const maxDiagonal = Math.max(...diagonals);
    const minDiagonal = Math.min(...diagonals);

    let reason;
    if (sides.some((side) => !(side >= MIN_SIDE_LENGTH_PX))) {
      reason = 'degenerate';
    } else if (maxSide / minSide > SIDE_LENGTH_RATIO_TOLERANCE) {
      reason = 'sideRatio';
    } else if (!(minDiagonal > 0) || maxDiagonal / minDiagonal > DIAGONAL_RATIO_TOLERANCE) {
      reason = 'diagonalRatio';
    }

    return {
      ok: reason === undefined,
      reason,
      sides,
      diagonals,
      sideRatio: minSide > 0 ? maxSide / minSide : undefined,
      diagonalRatio: minDiagonal > 0 ? maxDiagonal / minDiagonal : undefined,
    };
  }

  // Secondary, scale-invariant sanity check on the final 4 corners (see
  // AREA_PERIMETER_RATIO_TOLERANCE above). Never called before evaluateGeometry
  // and never used as the sole basis for acceptance.
  function evaluateAreaPerimeterConsistency(vertices) {
    const geometry = polygonGeometry(vertices);
    const area = Math.abs(geometry.signedArea);
    const perimeter = geometry.perimeter;
    const inferredSideFromArea = Math.sqrt(area);
    const inferredSideFromPerimeter = perimeter / 4;
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

  function isNeutralSquareColor(fillStyle) {
    const parsed = parseColor(fillStyle);
    return (
      parsed !== undefined
      && parsed.r === NEUTRAL_SQUARE_RGB.r
      && parsed.g === NEUTRAL_SQUARE_RGB.g
      && parsed.b === NEUTRAL_SQUARE_RGB.b
    );
  }

  // The single source of truth for "is this fill() a neutral Square", used
  // by both the production accept/reject path and diagnostics(). Never call
  // this from more than one place with divergent logic.
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

    if (simplified.length !== 4) {
      outcome.reason = 'wrongVertexCount';
      return outcome;
    }
    outcome.vertices = simplified;

    let geometry;
    try {
      geometry = evaluateGeometry(simplified);
    } catch (_error) {
      outcome.reason = 'geometryError';
      return outcome;
    }
    outcome.geometry = geometry;
    outcome.areaPerimeter = evaluateAreaPerimeterConsistency(simplified);

    if (!geometry.ok) {
      outcome.reason = geometry.reason;
      return outcome;
    }

    if (!outcome.areaPerimeter.ok) {
      outcome.reason = 'areaPerimeterMismatch';
      return outcome;
    }

    if (!isNeutralSquareColor(fillStyle)) {
      outcome.reason = 'colorMismatch';
      return outcome;
    }

    outcome.accepted = true;
    outcome.reason = 'accepted';
    return outcome;
  }

  // --- Accepted-square cache ---------------------------------------------

  function buildRecord(vertices, fillStyle, ctx) {
    const bbox = computeBBox(vertices);
    const canvas = safeRead(ctx, 'canvas');

    const record = {
      kind: 'neutral_square',
      vertices: vertices.map((vertex) => ({ x: vertex.x, y: vertex.y })),
      cx: (bbox.x0 + bbox.x1) / 2,
      cy: (bbox.y0 + bbox.y1) / 2,
      bbox,
      halfSize: 0.5 * Math.max(bbox.x1 - bbox.x0, bbox.y1 - bbox.y0),
      color: NEUTRAL_SQUARE_HEX,
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
    for (const [key, record] of squareCache) {
      if (currentTime - record.timestamp > CACHE_WINDOW_MS) {
        squareCache.delete(key);
        cacheCounters.prunedTotal += 1;
      }
    }
  }

  function recordSquare(record) {
    pruneCache(record.timestamp);
    squareCache.set(cacheKey(record), record);
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

    if (Array.isArray(outcome.vertices)) {
      sample.vertices = outcome.vertices.map((vertex) => ({ x: vertex.x, y: vertex.y }));
      sample.bbox = computeBBox(outcome.vertices);
    }

    if (outcome.geometry) {
      const geometry = {};
      addValue(geometry, 'sides', outcome.geometry.sides ? outcome.geometry.sides.slice() : undefined);
      addValue(geometry, 'diagonals', outcome.geometry.diagonals ? outcome.geometry.diagonals.slice() : undefined);
      addValue(geometry, 'sideRatio', finiteNumber(outcome.geometry.sideRatio));
      addValue(geometry, 'diagonalRatio', finiteNumber(outcome.geometry.diagonalRatio));
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

  function recordDiagnostics(outcome) {
    bumpHistogram(vertexHistogram, outcome.vertexCount);

    const isSquareColor = isNeutralSquareColor(outcome.fillStyle);
    if (isSquareColor) {
      squareColorCounters.fillsSeen += 1;
      bumpHistogram(squareColorVertexHistogram, outcome.vertexCount);
      bumpHistogram(squareColorSubpathCountHistogram, outcome.subpathCount);
      bumpHistogramKey(squareColorTopologyHistogram, topologySignature(outcome.subpaths));

      if (typeof outcome.meaningfulCandidateCount === 'number') {
        if (outcome.meaningfulCandidateCount === 0) {
          squareColorCounters.noMeaningfulPolygon += 1;
        } else if (outcome.meaningfulCandidateCount > 1) {
          squareColorCounters.ambiguousMeaningfulPolygons += 1;
        } else {
          squareColorCounters.meaningfulSinglePolygon += 1;
          if (typeof outcome.simplifiedVertexCount === 'number') {
            if (outcome.simplifiedVertexCount === 4) {
              squareColorCounters.simplifiedToQuad += 1;
            } else {
              squareColorCounters.simplificationFailed += 1;
            }
          }
        }
      }
    }

    if (outcome.accepted) {
      if (isSquareColor) {
        squareColorCounters.accepted += 1;
      }
      if (acceptedSamples.length < ACCEPTED_SAMPLE_CAP) {
        acceptedSamples.push(buildSample(outcome));
      }
      return;
    }

    if (isSquareColor) {
      squareColorCounters.rejected += 1;
      if (rejectedSquareColorSamples.length < REJECTED_SAMPLE_CAP) {
        rejectedSquareColorSamples.push(buildSample(outcome));
      }
    }

    if (Object.hasOwn(rejectionReasonCounters, outcome.reason)) {
      rejectionReasonCounters[outcome.reason] += 1;
    } else {
      rejectionReasonCounters.other += 1;
    }
  }

  function onFill(ctx) {
    callCounters.fill += 1;

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
        recordSquare(buildRecord(outcome.vertices, outcome.fillStyle, ctx));
        cacheCounters.acceptedTotal += 1;
      } catch (_error) {
        outcome.accepted = false;
        outcome.reason = 'cacheError';
      }
    }

    recordDiagnostics(outcome);
  }

  function diagnostics() {
    const currentTime = now();
    pruneCache(currentTime);
    return {
      version: VERSION,
      ready: hooksInstalled,
      uptimeMs: currentTime - startTime,
      calls: { ...callCounters },
      squareColor: { ...squareColorCounters },
      rejectionReasons: { ...rejectionReasonCounters },
      vertexHistogram: { ...vertexHistogram },
      squareColorVertexHistogram: { ...squareColorVertexHistogram },
      squareColorSubpathCountHistogram: { ...squareColorSubpathCountHistogram },
      squareColorTopologyHistogram: { ...squareColorTopologyHistogram },
      cache: {
        acceptedTotal: cacheCounters.acceptedTotal,
        currentlyCached: squareCache.size,
        prunedTotal: cacheCounters.prunedTotal,
      },
      acceptedSamples: acceptedSamples.map(cloneSample),
      rejectedSquareColorSamples: rejectedSquareColorSamples.map(cloneSample),
    };
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
    return Array.from(squareCache.values()).map(cloneRecord);
  }

  function snapshot() {
    return {
      metadata: { oracleVersion: VERSION },
      timestamps: { performanceNow: now(), wallClockMs: wallClockNow() },
      browser: { devicePixelRatio: finiteNumber(safeRead(window, 'devicePixelRatio')) },
      shapes: shapes(),
    };
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
    diagnostics,
  });

  Object.defineProperty(window, 'deepEyeOracle', {
    value: oracle,
    enumerable: true,
    configurable: false,
    writable: false,
  });
})();
