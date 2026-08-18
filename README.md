# deep.eye.oh

Research project: an autonomous agent for diep.io that interacts with the
real game through the same channels a human uses — pixels in, keyboard and
mouse out. No DOM, process memory, network protocol, or other hidden data.

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

## Setup (Windows)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
pytest
```
