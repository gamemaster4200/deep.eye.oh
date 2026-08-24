Read AGENTS.md first.

Implement the first usable deep.eye.oh Browser Oracle Chrome extension.

Existing vendor:
    extension/vendor/diepAPI.user.js
    extension/vendor/diepAPI.lock.json
    third_party/diepAPI-LICENSE.txt

Do not modify the vendored diepAPI source.

GOAL

Create an unpacked Manifest V3 extension for:

    https://diep.io/*

It must run diepAPI plus our oracle in the PAGE MAIN WORLD and expose:

    window.deepEyeOracle

This milestone is observation only.

PUBLIC API

Implement at least:

    deepEyeOracle.version
    deepEyeOracle.isReady()
    deepEyeOracle.snapshot()
    deepEyeOracle.shapes()

snapshot() must return a plain JSON-safe object.

Include where genuinely available from current diepAPI:

    timestamps:
        performanceNow
        wallClockMs

    browser:
        devicePixelRatio

    canvas:
        width
        height
        clientWidth
        clientHeight
        boundingClientRect

    player:
        position
        velocity
        level
        tank
        isDead

    camera:
        position

    arena:
        size

    minimap:
        position

    entities:
        decoded entities visible to the official client

Inspect the actual vendored API.
Do not invent fields.

Serialize class/vector values explicitly into plain objects.

SHAPES

Centralize current research mapping:

    4 = Square
    5 = Triangle
    6 = Pentagon
    9 = Alpha Pentagon

shapes() must return both numeric type and readable name.

STRICT READ-ONLY BOUNDARY

Do not invoke or expose wrappers for:

    spawn
    moveTo
    aimAt
    lookAt
    shoot
    keyDown
    keyUp
    keyPress
    mouse
    mousePress
    useGamepad
    upgrade_stat
    upgrade_tank

Do not call:
    input.set_convar
    input.execute

Do not send WebSocket messages.
Do not patch WebSocket.prototype.send.
Do not mutate game entities.

EXTENSION UI

Create a tiny developer popup with:

    diep.io detected
    diepAPI ready
    Browser Oracle ready
    entity count
    shape count

Buttons:

    Copy Snapshot
    Copy Shapes
    Refresh diep.io tab
    Reload Extension

DEVELOPMENT LOOP

Create:

    scripts/update-vendor.ps1
    scripts/validate.ps1
    scripts/dev-refresh.ps1
    dev-refresh.cmd

This machine uses restrictive PowerShell ExecutionPolicy.

dev-refresh.cmd must invoke PowerShell with:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File ...

Do not require changing global ExecutionPolicy.

update-vendor.ps1 must:
- query latest Cazka/diepAPI release
- download diepAPI.user.js to a temp file
- validate non-trivial size
- SHA-256 it
- avoid rewriting unchanged vendor
- atomically replace changed vendor
- update diepAPI.lock.json
- preserve provenance/license
- report useful status/exit code

validate.ps1 must check at minimum:
- manifest parses
- manifest-referenced files exist
- no <all_urls>
- host scope limited to diep.io
- vendor is present/non-trivial
- lock JSON parses
- obvious secret/profile/HAR artifacts are not tracked

dev-refresh goal:

    one action
      -> vendor update
      -> validation
      -> reload/restart DEVELOPMENT extension/browser context
      -> reopen/refresh diep.io
      -> concise result

Never:
- kill unrelated Chrome windows
- modify normal Chrome profile
- fake clicks on chrome://extensions

A dedicated repo-local gitignored Chrome development profile is acceptable.

Investigate the safest reliable current behavior.
If Chrome restricts command-line unpacked-extension loading, document the real
limitation rather than pretending automation works.

README must document:
- architecture and separation from deep.eye.oh
- first-time installation
- exact directory to Load unpacked
- normal dev loop
- dev-refresh.cmd
- DevTools verification:

      deepEyeOracle.isReady()
      deepEyeOracle.snapshot()
      deepEyeOracle.shapes()

- copying snapshots
- updating diepAPI
- troubleshooting
- security/privacy

Prefer a small structure such as:

    extension/
        manifest.json
        vendor/
        src/
        popup/
        background/

    scripts/
        update-vendor.ps1
        validate.ps1
        dev-refresh.ps1

    dev-refresh.cmd
    README.md
    THIRD_PARTY_NOTICES.md

Avoid a bundler unless genuinely needed.

Add lightweight tests/checks for our code:
- manifest restrictions
- JSON-safe serialization
- vector conversion
- missing fields
- shape mapping/filtering
- vendor lock

Run tests/checks and:

    git diff --check

Git:
- update .gitignore
- do not commit
- do not push
- do not merge

The outer bootstrap handles Git.

At the end report:
- architecture
- files created
- unpacked extension path
- MAIN-world mechanism
- dev-refresh workflow
- dedicated dev-profile behavior
- remaining manual steps
- validation/test results
- known limitations

Stop after the read-only Browser Oracle milestone.
Do not implement protocol decoding or gameplay automation.
