# deep.eye.oh Browser Oracle

This repository is a separate observation companion to `deep.eye.oh`.

```text
deep.eye.oh:      screen -> vision -> GameState -> Policy
deep.eye.oh.ext:  official diep.io client -> Canvas2D render observation -> Browser Oracle
```

It supplies developer-visible ground truth for research. The canonical screen-only agent must not consume oracle state silently. The extension implements no movement, aiming, shooting, spawning, upgrades, gameplay keyboard/mouse control, WebSocket injection, packet sending, or `WebSocket.send` patching.

## Scope of this slice: neutral Square ground truth only

This slice answers one question: can we get accurate ground truth for visible neutral Squares directly from the official client's render stream, in the same pixel space vision observes? It deliberately does not attempt a generic entity framework, other shape types, arena/camera coordinate reconstruction, WebSocket/protocol decoding, or gameplay automation.

## Why diepAPI is not used at runtime

An earlier version of this extension vendored [Cazka/diepAPI](https://github.com/Cazka/diepAPI) and loaded it as a MAIN-world runtime dependency. Live Chrome smoke testing found two disqualifying problems with the pinned v3.3.1 build:

1. Its public `diepAPI.apis` surface (`arena`, `camera`, `game`, `input`, `minimap`, `player`, `playerMovement`, `scaling`) does not include an `entityManager`, so no entity/shape stream is actually reachable through the documented API.
2. Running it against the official client leads to `Uncaught RuntimeError: memory access out of bounds` in `diep.wasm`, with the stack passing through the vendor's `requestAnimationFrame` hook. This matches the open upstream issue [Cazka/diepAPI#80](https://github.com/Cazka/diepAPI/issues/80): the userscript runs for a while, then the WASM heap access faults and the game freezes or blanks.

Because of (2), `diepAPI.user.js` is no longer manifest-loaded at all — it is not safe to run against the real client. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for what is preserved and why.

## Architecture and boundary

The unpacked Manifest V3 extension is restricted to `https://diep.io/*`. Chrome loads one packaged content script, `extension/src/oracle.js`, at `document_start` in the page's `MAIN` JavaScript world. This is manifest-declared MAIN-world execution (`content_scripts[].world = "MAIN"`), not DOM script injection. The popup crosses the extension/page isolation boundary with `chrome.scripting.executeScript({ world: "MAIN" })`; only detached results are returned. Runtime code is packaged locally. There is no remote executable code, `eval`, dynamic `Function`, or page message bridge. Since browser-informed-farming-v0, there IS exactly one background service worker, `extension/background/bridge.js` -- see "Bridge" below for precisely what it is scoped to do (forward Oracle snapshots outward over one local WebSocket; nothing else, and nothing comes back in).

The public API is:

```js
deepEyeOracle.version
deepEyeOracle.isReady()
deepEyeOracle.snapshot()
deepEyeOracle.shapes()
deepEyeOracle.diagnostics()
```

`isReady()` means the Canvas2D observer installed successfully and is able to observe the square render path — not that a square is currently on screen. In a frame with no visible neutral Square, `shapes()` is legitimately empty while `isReady()` is `true`. Use `diagnostics()` (below) to tell "empty because nothing yellow is on screen" apart from "empty because detection is failing."

Every `snapshot()`, `shapes()`, and `diagnostics()` result is newly allocated plain data safe for `JSON.stringify`; mutating a previously returned result never affects oracle state.

### How the Canvas2D observer works

`oracle.js` hooks six `CanvasRenderingContext2D.prototype` methods: `beginPath`, `moveTo`, `lineTo`, `fill`, `rect`, `closePath`. Each wraps the original so it is always invoked with the same `this`/arguments and the same observable behavior; observation runs after and can never throw into the page. `isReady()` only depends on `beginPath`/`moveTo`/`lineTo`/`fill` hooking successfully — `rect`/`closePath` are diagnostic-only and best-effort (see below).

- `beginPath` resets the tracked path state for that context (all subpaths and the per-path call counters described under Diagnostics).
- `moveTo` starts a new subpath; every subpath in the path is tracked (bounded by `MAX_TRACKED_SUBPATHS`), not just the most recent — see "Reconstructing the real square" below for why.
- `lineTo` reads `ctx.getTransform()` **at the time of that call** and applies it to the given point (`x' = a*x + c*y + e`, `y' = b*x + d*y + f`), so a transform change between path calls is handled correctly rather than assumed away.
- `fill` calls a single classification function, `classifyFill()`, shared by both the production accept/reject path and `diagnostics()` — there is no separate, divergent detector implementation. It selects the unique meaningful polygon subpath, collapses collinear edge-subdivision points down to the real corners, requires exactly 4 of them, runs the geometry sanity check (roughly equal side lengths and roughly equal diagonals, with a generous fixed tolerance — not tuned against any vision holdout), cross-checks the area/perimeter self-consistency invariant, and checks `ctx.fillStyle` against the neutral Square color (`#ffe869`, accepting the browser's `rgb()`/`rgba()`-normalized equivalent). A path built via `rect()` is rejected outright (`unsupportedRectPath`, see below), even if the tracked `moveTo`/`lineTo` vertices happen to look valid, since `rect()` content is not observed and the actual filled geometry may not match what was tracked.

**Closing-vertex normalization.** A path manually closed with an explicit `lineTo` back to its start point (rather than relying on `fill()`'s implicit closure) produces a tracked vertex numerically equal to the first. This is a standard Canvas2D idiom, not a different shape. It is one instance of the general point-merging step described below — a duplicate point is merged with its neighbor regardless of whether it happens to be the closing point or an accidental repeated `lineTo`.

**Original-first hook order.** Each hook always calls the real Canvas2D method first, then observes. This is safe here specifically because none of `beginPath`/`moveTo`/`lineTo`/`fill` mutate `ctx.getTransform()`, the current path, or `ctx.fillStyle` as a side effect of their own execution — reading those either before or after the real call observes the same values, and "original first" is strictly safer (observation can never block or alter the real draw call, even if it throws).

### Reconstructing the real square from a subdivided render contour

A live diagnostic capture (`squareColorTopologyHistogram`, `squareColorSubpathCountHistogram`; see Diagnostics below) established the real render structure for every observed `#ffe869` fill (232,469/232,469 samples in the capture that motivated this): **exactly 2 subpaths** — one real polygon contour (convex, with an invariant signed area and perimeter across samples, `sqrt(area) ≈ perimeter/4`, and an AABB width that varies by up to `sqrt(2)` exactly as expected for a fixed-size randomly-rotated square) plus one visually-inert single-point subpath (a lone `moveTo` to that polygon's own bbox center, with no `lineTo` calls — no fillable area). The "extra" vertices beyond 4 on the real contour lie on/near its four straight edges: they are edge subdivisions from the renderer, not additional semantic corners.

The detector reconstructs the actual quad from this rather than inferring "square-likeness" from aggregate area/perimeter alone:

1. **Select the unique meaningful subpath.** For every tracked subpath, merge duplicate/closing points, then require at least 3 distinct points and a non-negligible enclosed area (`MIN_MEANINGFUL_AREA_PX2`, tied to the same sub-pixel noise floor as `MIN_SIDE_LENGTH_PX`). This is never a hard-coded position (`subpaths[0]`, or "the last one") — it is whichever subpath is the only one with real area. Zero qualifying subpaths → `noMeaningfulSubpath`. More than one → `ambiguousSubpaths`, rejected rather than guessed at.
2. **Collapse collinear edge subdivisions.** For each point, test whether it lies within `GEOMETRY_EPSILON_PX` (0.75px — sub-pixel Canvas rendering/floating-point noise, not tuned against any vision holdout) of the straight line through its two neighbors; if so it is a subdivision point, not a corner, and is removed. This repeats (removing one point can reveal a new collinear triplet next to it) until a full pass removes nothing, never below 3 points. Point order — and therefore winding — is only ever thinned, never reordered.
3. **Require exactly 4 corners.** If simplification does not converge on exactly 4 points, reject as `wrongVertexCount` — this is not weakened into accepting arbitrary polygons (a genuine pentagon, or a point genuinely off the tolerance, is retained and correctly fails this step).
4. **Run the existing side/diagonal geometry check** on the 4 recovered corners (unchanged constants — reviewed, not blindly kept: see below).
5. **Cross-check area/perimeter self-consistency.** For an ideal square, `sqrt(area)` and `perimeter / 4` are the same quantity regardless of size or rotation. `AREA_PERIMETER_RATIO_TOLERANCE` (1.2) bounds how far the two independently-computed values may disagree — a secondary, scale-invariant sanity signal, never the sole basis for acceptance, and not calibrated against any specific observed pixel measurement (which varies with zoom/canvas size across sessions).
6. **Check the neutral Square color**, exactly as before.

`SIDE_LENGTH_RATIO_TOLERANCE`/`DIAGONAL_RATIO_TOLERANCE` (1.35) were reviewed for this slice, not automatically kept: no live evidence yet establishes a tighter reconstructed-corner-level noise bound, so they remain the same generous, dataset-independent value as before; the area/perimeter cross-check above adds an independent, more mathematically precise signal on top rather than replacing them.

### Lifetime semantics (heuristic cache, not a frame boundary)

There is no crash-safe way to hook a frame boundary in this project (see above — that is the vendor's crash path). Instead, each detected square is upserted into a cache keyed by its rounded center and color, stamped with `performance.now()`. `shapes()`/`snapshot()` prune and return only entries seen within the last 250ms. This is a **heuristic recent-observation window**, not a synchronized single-frame guarantee: it tolerates a missed/slow frame without dropping a still-visible square, and a square that stops being drawn ages out within that window. A square filled multiple times in one visual cycle collapses to one record instead of duplicating.

### Multi-subpath paths: now handled, not a scope gap

An earlier slice of this project noted (and deferred) that only the most recent `moveTo`-started subpath was ever evaluated. That gap is closed: every subpath is tracked (bounded by `MAX_TRACKED_SUBPATHS`) and the meaningful-subpath-selection step above considers all of them. The remaining, narrower gap is specifically **two or more subpaths that are each independently meaningful** (real area, 3+ points) in the same `fill()` — that case is rejected as `ambiguousSubpaths` rather than guessed at, since nothing in the currently-understood render structure requires resolving it (the real structure is one meaningful polygon plus one inert point, never two meaningful polygons).

### Why `rect()` is not (yet) supported

`ctx.rect(x, y, w, h)` builds a subpath without calling the exposed `moveTo`/`lineTo` methods, so it is invisible to this observer entirely. A `rect` hook is installed, but **only to count calls** (`diagnostics().calls.rect`, and `rectCalls` on each path/sample) — it never contributes vertices, and any path that used `rect()` is unconditionally classified as `unsupportedRectPath` rather than risk mis-detecting or silently missing its actual filled geometry.

An earlier slice hypothesized `rect()` (e.g. via `ctx.translate()` + `ctx.rotate()` + an axis-aligned `ctx.rect()`) as the leading explanation for the render structure, based on an unfiltered-by-color capture that recorded `rect: 8560` calls alongside a large 0-tracked-vertex `fill()` bucket. **That hypothesis has since been ruled out for the neutral Square specifically** by the `#ffe869`-filtered live capture described above: the real square subpath is built from ordinary `moveTo`/`lineTo` calls (with edge-subdivision points), not `rect()`. `rect()` may still be used elsewhere in the renderer (UI, minimap, other entities) — the hook remains in place, diagnostic-only, for exactly that visibility, but it is not why neutral Squares were undetected.

## Diagnostics: why isn't a Square being detected?

`deepEyeOracle.diagnostics()` is a JSON-safe, detached snapshot of the detector's own bookkeeping, so a person does not have to hand-write a DevTools probe to find out why `shapes()` is empty:

```js
{
  version, ready, uptimeMs,
  calls: { beginPath, moveTo, lineTo, rect, closePath, fill },
  squareColor: {                                         // scoped to fillStyle === #ffe869 (or its rgb() equivalent)
    fillsSeen, accepted, rejected,
    meaningfulSinglePolygon, noMeaningfulPolygon, ambiguousMeaningfulPolygons, // subpath-selection stage
    simplifiedToQuad, simplificationFailed,                                    // collinear-collapse stage
  },
  rejectionReasons: {                                    // global, across every fill() of any color
    noMeaningfulSubpath, ambiguousSubpaths,
    wrongVertexCount, degenerate, sideRatio, diagonalRatio, areaPerimeterMismatch,
    colorMismatch, unsupportedRectPath, transformError,
    geometryError, cacheError, other,
  },
  vertexHistogram: { "0": n, "4": n, "5": n, ... },       // global, keyed by the selected subpath's vertex count ("16+" bucket caps it)
  squareColorVertexHistogram: { ... },                    // same keying, fillStyle === #ffe869 only
  squareColorSubpathCountHistogram: { "1": n, "2": n, ... },  // #ffe869-only, subpaths per fill()
  squareColorTopologyHistogram: { "6,1": n, "4": n, ... },    // #ffe869-only, per-subpath vertex-count signature (see live evidence above)
  cache: { acceptedTotal, currentlyCached, prunedTotal }, // acceptedTotal > 0 with currentlyCached = 0 means detection works but the cache aged it out
  acceptedSamples: [ { reason: "accepted", meaningfulCandidateCount, selectedSubpathIndex, preSimplificationVertexCount, simplifiedVertexCount, collinearPointsRemoved, vertices, geometry, areaPerimeter, subpaths, ... } ],           // first 20
  rejectedSquareColorSamples: [ { reason, vertexCount, meaningfulCandidateCount, selectedSubpathIndex, preSimplificationVertexCount, simplifiedVertexCount, collinearPointsRemoved, vertices, geometry, areaPerimeter, subpaths, moveToCalls, lineToCalls, rectCalls, closePathCalls, fillStyle, ... } ], // first 30
}
```

`rejectionReasons` and the two vertex histograms are populated by every observed `fill()` call, not just `#ffe869` ones — this is what lets `colorMismatch` (valid quad, wrong color) be told apart from `wrongVertexCount`/`sideRatio`/etc. (never a valid quad at all), confirming the geometry and color logic are working independently of each other. `squareColor` and the `squareColor*` histograms are scoped specifically to `#ffe869` fills, since that is the question that matters here; `subpaths` on every sample carries the full raw multi-subpath topology (every subpath's vertices/geometry) regardless of which one (if any) drove the classification, so a rejection can always be audited against what was actually drawn. Sample buffers are bounded (first 20 accepted, first 30 rejected `#ffe869` samples) — counters keep counting past the cap, only the detailed samples stop accumulating.

## Neutral Square record shape

```js
{
  kind: "neutral_square",
  class: "square",                              // "square" | "triangle" | "pentagon"
  vertices: [{x, y}, {x, y}, {x, y}, {x, y}], // canvas pixel space, in transform order
  cx, cy,                                      // (min+max)/2 of the axis-aligned bbox
  bbox: { x0, y0, x1, y1 },
  halfSize,                                    // 0.5 * max(x1-x0, y1-y0)
  radius,                                       // mean vertex-to-centroid distance
  color,                                        // "#ffe869"
  rawFillStyle,                                 // as read from ctx.fillStyle, if a string
  timestamp,                                    // performance.now() at detection
  canvasWidth, canvasHeight, devicePixelRatio,  // diagnostics, when readable
  source: "canvas2d",
}
```

Coordinates are absolute canvas pixel coordinates, not viewport-center-relative `dx`/`dy`. This layer intentionally does not attempt to match `GameState` semantics beyond the shared `cx`/`cy`/`halfSize` bbox convention already used by shape-perception-v0.

## Triangle and Pentagon (browser-informed-farming-v0)

`shapes()`/`snapshot()` also return neutral Triangles (`kind: "neutral_triangle"`, `class: "triangle"`, 3 `vertices`, color `#fc7677`) and neutral Pentagons (`kind: "neutral_pentagon"`, `class: "pentagon"`, 5 `vertices`, color `#768dfc`) -- the same colors the vendored `Cazka/diepAPI`'s `EntityColor` map records, with Square's independently confirmed live. All three classes are produced by the SAME classification pipeline in `classifyFill()` (color+corner-count together, generalized side/radius-ratio geometry, generalized area/perimeter consistency), not three copy-pasted detectors, and share the same `cx`/`cy` coordinate space, so downstream code can treat `oracle.shapes()` as one flat, mixed-class list. `radius` (mean vertex-to-centroid distance) is provided uniformly across all three classes as a size measure that does not assume Square's bbox-symmetric geometry.

## Bridge: forwarding Oracle snapshots to the agent process

`extension/background/bridge.js` is a Manifest V3 background service worker (the only file in this extension permitted to touch the network or hold a background lifecycle). At a fixed ~10Hz interval it pulls `window.deepEyeOracle.snapshot()` out of the active diep.io tab's MAIN world via `chrome.scripting.executeScript` (bypassing page CSP, since a MAIN-world *page* script speaking to the network would be subject to diep.io's own Content-Security-Policy) and forwards it as one small JSON message, `{type: "oracle_snapshot", tabId, polledAtMs, snapshot}`, over a plain `WebSocket` to `ws://127.0.0.1:8765/` -- a local `deep.eye.oh` agent process (see that repository's `browser_bridge` module) is expected to be listening there. There is no inbound command channel: the bridge never reads anything back off that socket, so nothing on the other end can make the extension (or the game) do anything. This is a thin, purpose-specific export, not a general message broker -- see `tests/bridge.test.js` for what is unit tested (message shape, reconnect backoff, tab selection) versus the live smoke procedure below (the actual `chrome.tabs`/`chrome.scripting`/`WebSocket` glue).

## Manifest permissions

- `host_permissions: ["https://diep.io/*"]` permits only the declared diep.io content script, popup inspection, and the background bridge's tab lookup on that origin.
- `scripting` lets the popup and the background bridge execute their small read-only functions in the active diep.io tab's MAIN world, where `window.deepEyeOracle` exists.
- `clipboardWrite` lets a user-initiated popup button write the selected JSON result.

No `tabs` permission is needed for active-tab lookup/reload (host permission on `https://diep.io/*` is sufficient for `chrome.tabs.query` to see matching tabs' `url`/`id`), and there is no `<all_urls>` scope or remote script. `background.service_worker` (`background/bridge.js`) is the one addition in this milestone -- see "Bridge" above for exactly what it does and does not do.

## First-time installation

1. Validate the checkout:

   ```bat
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\validate.ps1
   ```

2. Open `chrome://extensions` in your normal Chrome.
3. Enable **Developer mode** and choose **Load unpacked**.
4. Select exactly:

   ```text
   D:\toxic\devel\private\deep.eye.oh.ext\extension
   ```

5. Open or fully reload `https://diep.io/`.

A dedicated Chrome profile/process was tried and dropped: it added ownership/process-management complexity for no real benefit, and a separate profile even hit an "Access denied" loading the unpacked extension. Normal Chrome loads it without issue.

## Development loop

The ordinary loop is:

```bat
dev-refresh.cmd
```

It invokes PowerShell with `-NoProfile -ExecutionPolicy Bypass`, then:

1. uses the current pinned vendor files without a network query or download;
2. validates the manifest, vendor lock/SHA (provenance only), runtime boundary, JavaScript, and tests;
3. prints the manual reload steps.

`dev-refresh.cmd`/`dev-refresh.ps1` do not launch, close, or otherwise manage any Chrome process or profile. After it prints "Validation passed", finish the reload yourself:

1. `chrome://extensions`
2. Click **Reload** on "deep.eye.oh Browser Oracle"
3. Reload the `https://diep.io/` tab so the `document_start` script runs again

The popup's **Reload Extension** button is also available for step 2.

## Explicit vendor update

An upgrade of the preserved (non-executing) vendor provenance is deliberately separate from ordinary refresh:

```bat
dev-refresh.cmd -UpdateVendor
```

This runs `scripts/update-vendor.ps1`, then validation. The updater queries the latest Cazka/diepAPI release, downloads `diepAPI.user.js` to a temporary file, checks identity and non-trivial size, computes SHA-256, avoids rewriting identical vendor bytes, atomically replaces changed files, and updates provenance in the lock.

The updater remains directly available:

```bat
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\update-vendor.ps1
```

Preserve the upstream source and MIT license; never hand-edit or reformat the vendor bundle. Updating it does not change what Browser Oracle executes — the vendor file is not loaded by the manifest.

## Verification and copying

The popup is the intended day-to-day workflow — it displays readiness, the current neutral-square count, and a **Diagnostics** section (`#ffe869` fills seen, accepted, rejected, currently cached, and the top rejection reasons), all read through the same `chrome.scripting.executeScript({ world: "MAIN" })` bridge used for the rest of the popup. **Copy Snapshot**, **Copy Shapes**, and **Copy Diagnostics** each write formatted JSON to the clipboard after a user click; no manual DevTools probe should be necessary to explain why `shapes()` is empty. Data remains in the page/popup unless deliberately copied; the extension does not store or transmit snapshots, cookies, profiles, authentication material, or WebSocket data.

Manual DevTools access remains available in the page context when deeper inspection is useful:

```js
deepEyeOracle.isReady()
deepEyeOracle.snapshot()
deepEyeOracle.shapes()
deepEyeOracle.diagnostics()
JSON.stringify(deepEyeOracle.diagnostics())
```

## PRE-MERGE manual Chrome smoke gate

This gate is required and is not claimed by automated validation. It has not been run yet for this slice — do not treat it as passed until a person confirms every step below in a normal Chrome window:

1. Load the unpacked extension in your normal Chrome (see First-time installation).
2. Open `https://diep.io/`.
3. Join a match and keep at least one neutral Square visible on screen for a few seconds.
4. Confirm the game does **not** crash with `memory access out of bounds` (the failure mode the pinned diepAPI runtime caused).
5. Open the extension popup. Confirm **Browser Oracle ready** is Yes.
6. Click **Copy Diagnostics** and send the copied JSON. Expected with the subdivided-contour detector: `squareColor.accepted > 0`, `cache.acceptedTotal > 0`, and `cache.currentlyCached > 0` while squares remain on screen. If any of those are still 0, `rejectionReasons` and `rejectedSquareColorSamples` (now carrying `meaningfulCandidateCount`/`simplifiedVertexCount`/`collinearPointsRemoved`) explain exactly why — no manual DevTools probing needed.
7. If `neutral squares` in the popup is `> 0`, run `deepEyeOracle.shapes()` in page DevTools and spot-check a few records: centers should correspond visually to rendered squares, AABB dimensions should vary with each square's rotation, and (via `diagnostics().acceptedSamples[].areaPerimeter`) the inferred true side (`inferredSideFromArea`/`inferredSideFromPerimeter`) should stay approximately constant across samples even as `halfSize` (AABB-derived) varies with rotation.

## Checks and troubleshooting

Run:

```bat
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\validate.ps1
git diff --check -- . ":(exclude)extension/vendor/**" ":(exclude)third_party/**"
```

- **Oracle is undefined:** confirm the loaded directory ends in `\extension`, reload the extension, then reload the diep.io tab.
- **`isReady()` is false:** `CanvasRenderingContext2D` was not hookable at `document_start`; check page-console errors. It does not depend on a match being joined.
- **`shapes()` is empty:** open the popup's Diagnostics section (or `deepEyeOracle.diagnostics()`) rather than guessing. `squareColor.fillsSeen === 0` means no `#ffe869` fill was even observed (wrong color assumption, or the shape isn't drawn via `fill()` at all); `fillsSeen > 0` with `accepted === 0` means fills are seen but rejected — check `rejectionReasons` and `rejectedSquareColorSamples` for the exact cause: `noMeaningfulSubpath`/`ambiguousSubpaths` mean subpath selection itself failed (check `subpaths` on the sample against the live-evidence 2-subpath pattern), `wrongVertexCount` after a nonzero `collinearPointsRemoved` means simplification did not converge on 4 corners, `areaPerimeterMismatch` means the recovered quad passed side/diagonal checks but failed the area/perimeter cross-check; `cache.acceptedTotal > 0` with `cache.currentlyCached === 0` means detection works and the ~250ms cache window aged the result out between draws.
- **Vendor update fails:** check GitHub connectivity. Existing vendor bytes are not replaced until the download passes validation.
- **Popup cannot read/copy:** activate a diep.io tab and reopen the popup.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for provenance.
