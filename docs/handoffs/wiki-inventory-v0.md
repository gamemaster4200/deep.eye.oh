# Handoff: wiki-inventory-v0

## Status

- Branch: `feat/wiki-inventory-v0`
- Base `main` commit: `3137fb7`
- Milestone: `wiki-inventory-v0`
- Status: implementation complete, real inventory acquired, **not merged**
- Full tests: `172 passed` (80 wiki-specific + 92 pre-existing)
- No changes to `src/deep_eye_oh/**` — this slice never touches the live bot pipeline (capture/control/perception/policy/simulator)

## What this slice built

A reproducible, offline-testable pipeline to acquire and structurally inventory the Diep.io Fandom wiki corpus, as prior knowledge for later (not-yet-designed) schema work:

```
tools/wiki/
    discover.py     # API-backend page/namespace discovery -> pages_index.json
    fetch.py         # acquisition orchestrator: prefers the official dump backend,
                      # falls back to the page-by-page API backend
    inventory.py     # offline: raw snapshot -> knowledge/inventory/* + REPORT.md
    _http.py         # urllib transport: rate limiting, retry/backoff, RFC 9309 robots.txt
    _mediawiki.py    # MediaWiki action=query request builders
    _dump.py         # official Fandom database-dump backend (see below)
    _wikitext.py     # mwparserfromhell-based section/template/table/link extraction
    _classify.py     # page-type + domain classification, driven by external signals config
    config/classification_signals.json   # intentionally empty (see below)
```

Tests live in `tests/test_wiki_*.py` + `tests/fixtures/wiki/` (synthetic fixtures + a hand-built `tiny_snapshot` + a small sample MediaWiki XML export) — fully offline, no network access in the test suite.

## Real acquisition result

**Source:** official Fandom database dump (preferred automatically over the page-by-page API):

```
https://s3.amazonaws.com/wikia_xml_dumps/d/di/diepio_pages_current.xml.7z
Last-Modified: 2026-06-12
```

**Snapshot directory:**

```
knowledge/raw/fandom/20260821T022701Z/
```

**This directory is gitignored (`knowledge/raw/`) and will NOT exist after cloning the repo on another machine.** Only the derived `knowledge/inventory/` outputs are tracked. To inspect raw source data or regenerate the inventory from scratch, reacquire it first.

### Reproducing the acquisition

```bash
# one-time setup (mwparserfromhell has an optional C tokenizer that needs
# MSVC build tools; WITH_EXTENSION=0 forces the pure-Python fallback, which
# is what was actually used to produce this snapshot)
WITH_EXTENSION=0 pip install -e ".[dev,wiki]"

# acquisition: --backend auto (default) prefers the official dump, falling
# back to the API backend only if no sufficiently fresh dump is found (in
# which case --pages-index from discover.py is also required)
python tools/wiki/fetch.py --new-snapshot \
  --user-agent "deep-eye-oh-wiki-inventory/0.1 (+research tool; see project repo)"

# offline inventory generation from the resulting snapshot (never touches
# the network):
python tools/wiki/inventory.py \
  --snapshot-dir knowledge/raw/fandom/<snapshot-id printed above> \
  --out-dir knowledge/inventory
```

Both commands print the snapshot id / output location on completion. `fetch.py --new-snapshot` always creates a fresh, separate snapshot directory — it never mutates a prior one. See each tool's `--help` / module docstring for the full flag set (rate limiting, `--backend {auto,dump,api}`, `--dump-max-age-days`, `--skip-namespaces`, resume semantics).

## Inventory findings (from this real snapshot)

```
pages acquired: 20,432
namespaces represented: 23
redirects: 1,353
categories: 337
unique section headings: 14,586
templates: 1,149
tables: 638
table parse quality:
    complete 242
    partial 396
    failed 0

orphan pages: 17,651

top hubs:
    Staff 719
    Cannons 603
    Destroyer 571
    Tank 406

structural temporal signals:
    changelog_updates 152
    historical_removed_content 188

unknown_domain: 20,188

page types:
    user_pages 10,853
    unknown 7,258
    templates 1,356
    discussion_forum 699
    categories 266

canonical_candidate: 0
fanon/community_strategy/other_games: 0
```

**The zero semantic-domain/canonical counts are intentional, not a bug.** `tools/wiki/config/classification_signals.json` is still empty — this slice deliberately did not populate curated category/template vocabulary by inspecting the very corpus it was measuring (that would be circular/overfit, not honest measurement). Structural, corpus-independent signals (namespace names, a few section-heading keywords) still fired normally, which is why `changelog_updates`/`historical_removed_content`/`user_pages`/`templates`/`discussion_forum`/`categories` are non-zero while every curated-vocabulary domain and `canonical_candidate`/`fanon`/`community_strategy`/`other_games` are exactly 0. Lots of `unknown`/`unknown_domain` is the expected, correct outcome for this run.

Full detail, including per-namespace breakdowns of the "unknown" bucket (diagnostic only, not a classification change) and the "Schema implications" analysis (supports/requires-extending/contradicts/still-unknown tagged), is in `knowledge/inventory/REPORT.md`.

## Important architectural decisions

- Acquisition and inventory are separate stages: `source acquisition → immutable raw snapshot → offline inventory`. `inventory.py` never touches the network.
- The official Fandom database dump is preferred over page-by-page MediaWiki API acquisition (`fetch.py --backend auto`); the API backend remains as an explicit fallback with polite rate limiting.
- Raw snapshots (`knowledge/raw/`) are immutable (write-once per page) and gitignored — regenerable from the source, not meant to live in git history.
- Derived inventory (`knowledge/inventory/`) is small enough to track in git and always carries a `provenance` block tying it back to the exact raw snapshot (id, manifest hash, schema version).
- Classification signals (category/template vocabulary) are explicit, versioned, curated data in `classification_signals.json` — never hardcoded Python literals, never auto-populated from a live run.
- `canonical_candidate` (not `canonical`) is the label used throughout — it is heuristic and evidence-backed, never treated as authoritative ground truth.
- Table parsing is best-effort; every table carries a `parse_quality` (`complete`/`partial`/`failed`) and its raw source text is preserved regardless of parse quality.
- robots.txt handling follows RFC 9309: 2xx obeys parsed rules, 4xx (including 403) is "unavailable" and acquisition MAY proceed with no restrictions, 5xx/network-unreachable fails closed. The outcome is recorded in acquisition provenance for every host checked (the wiki host and, for the dump backend, the S3 host).
- No final knowledge schema has been designed yet. No SQLite has been introduced. No runtime bot/simulator/perception/policy integration has started.

## Review gate / next task

**The next task is NOT more scraping.** The next session should:

1. Read `knowledge/inventory/REPORT.md` in full.
2. Inspect representative data in `corpus.json`, `pages.jsonl`, `categories.json`, `sections.json`, `templates.json`, `tables.json`, `domains.json`.
3. Evaluate what the real corpus implies for the structured knowledge model (this is what `REPORT.md`'s "Schema implications" section is for).
4. Only after that review, design `knowledge-schema-v0` as its own slice.

Do not automatically populate `classification_signals.json` before that schema/inventory review happens — populating it is a deliberate, separate curation step with its own review, informed by what the review in step 3 finds, not a mechanical follow-on to this slice.

Do not begin simulator/perception/policy integration.

## Raw-data caveat

Because `knowledge/raw/` is intentionally not committed, a fresh clone on another machine can inspect the committed derived inventory (`knowledge/inventory/`) immediately, but must reacquire the dump (see "Reproducing the acquisition" above) if raw-source inspection or inventory regeneration is required. The dump backend is fast (a few minutes end to end against the real wiki, based on this run: ~1m45s acquisition + ~2m10s inventory generation for the same 20,432-page corpus).
