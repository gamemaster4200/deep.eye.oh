# deep.eye.oh.ext

Separate research companion to deep.eye.oh.

Architecture:

deep.eye.oh:
    screen -> vision -> GameState -> Policy

deep.eye.oh.ext:
    official diep.io client -> decoded runtime state -> Browser Oracle

Purposes:
1. Read-only Browser Oracle.
2. Ground truth for the separate vision project.
3. Later, oracle-assisted protocol research.

The canonical screen-only agent (deep.eye.oh's vision -> GameState ->
Policy pipeline) must not silently consume Browser Oracle state. A
separate, explicit consumer is a different matter: deep.eye.oh's
browser-informed-farming-v0 slice deliberately reads Oracle snapshots
(via this repo's background bridge) and drives the game through its own,
ordinary Controller (keyboard/mouse) -- a sanctioned, explicit
exception-consumer, not the canonical agent silently reaching into this
extension.

`oracle.js` remains strictly read-only; browser-lifecycle-v0 introduces
one narrow exception: an isolated-world lifecycle script
(`extension/src/lifecycle.js`) may interact with known pre-game, lobby,
and death/respawn DOM UI for name/mode/start/respawn only.

It may not:
- move the tank
- aim
- shoot
- upgrade
- synthesize gameplay keyboard/mouse input
- patch game networking
- execute arbitrary commands from Python
- interact with CAPTCHA controls

That is the ONLY exception to this repo's read-only milestone -- scoped
to `lifecycle.js` and nothing else. `oracle.js`, `popup.js`, and
`background/bridge.js` remain exactly as read-only as before this slice:
the Oracle only observes the Canvas and reports snapshots outward, the
bridge only forwards those snapshots (plus lifecycle.js's own read-only
DOM observations) outward and accepts back exactly one validated
player-name/game-mode config message from Python (see
deep_eye_oh's browser_lifecycle.py) -- never a generic selector,
JavaScript, shell command, URL, or action payload.

Do not implement or invoke, anywhere in this repo:
- automatic movement
- aiming
- shooting
- spawning
- upgrades
- gameplay keyboard/mouse control
- WebSocket packet injection
- WebSocket.send patching
- CAPTCHA solving, bypassing, or any other CAPTCHA-control interaction

deep.eye.oh (the separate, sibling project) is not bound by this rule --
it may aim/shoot/move via its own Controller, informed by the snapshots
this repo provides. If that ever changes, update this file explicitly
rather than leaving the next agent to guess from a stale-sounding blanket
statement.

Vendored Cazka/diepAPI:
- MIT licensed
- preserve provenance/license
- never directly edit extension/vendor/diepAPI.user.js

Engineering:
- Manifest V3
- https://diep.io/* only
- minimal permissions
- no <all_urls>
- no remote executable code
- no eval
- JSON-safe snapshots
- Windows-friendly tooling
- small focused implementation
- tests/checks for our own code
- do not merge branches
- do not rewrite history

Never commit:
- HAR files
- WebSocket tickets
- cookies
- tokens
- browser profiles
- auth material
- private captures
