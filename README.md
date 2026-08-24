# deep.eye.oh

Research project: an autonomous agent for diep.io that interacts with the
real game through the same channels a human uses — pixels in, keyboard and
mouse out. No DOM, process memory, network protocol, or other hidden data.

## Install (Windows 10/11 x64)

No Python, Git, or Chrome install required beforehand — per-user, no
Administrator privileges.

```powershell
irm https://github.com/gamemaster4200/deep.eye.oh/releases/latest/download/install.ps1 | iex
```

Then, from a **new** terminal, in any directory:

```powershell
deep-eye-oh browser-farm
```

This downloads and caches a pinned Chrome for Testing build on first run,
launches it with a dedicated profile and the bundled Browser Oracle
extension already loaded, opens diep.io, waits for the extension/bridge to
connect, and only then starts farming. The dedicated panic key (default:
Pause) stops input immediately at any time.

Other useful commands:

```powershell
deep-eye-oh --version   # identifies the installed bot+extension release
deep-eye-oh doctor      # checks runtime/browser/extension/bridge prerequisites
```

## Architecture

```
Environment
    ↓
Observation
    ↓
StateEstimator
    ↓
GameState
    ↓
Policy
    ↓
Action
    ↓
Environment
```

See [CLAUDE.md](CLAUDE.md) for the full architectural baseline and
constraints.

## Status

Bootstrap only. No capture, vision, control, simulator, or policy code
exists yet. Next step: `capture + replay v0`.

## Development

For working on deep.eye.oh itself (not just running it):

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
pytest
```

`[dev]` is enough for the core test suite. `[wiki]` and `[knowledge]` are
only needed for the diep.io wiki-inventory tooling under `tools/wiki/`.
`websockets` (needed by the bridge/`browser-farm`) is a base dependency, not
an extra, so a plain editable install can already run `browser-farm`.

The Browser Oracle extension's canonical source lives at
`deep_eye_oh_ext/extension/` (imported via `git subtree` from the former
`deep.eye.oh.ext` repo, kept as one repo since bot and extension are a
single version-coupled release). It is copied into the installed package as
`deep_eye_oh/_extension/` automatically at build time (see `setup.py`) — do
not hand-edit or duplicate it elsewhere. `deep_eye_oh_ext/scripts/validate.ps1`
runs its own manifest/boundary/JS checks and hand-rolled Node tests; CI runs
it alongside pytest.
