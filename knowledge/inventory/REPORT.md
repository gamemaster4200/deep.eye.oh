# Diep.io Wiki Corpus — Structural Inventory Report

- snapshot_id: `20260821T022701Z`
- acquisition_completed_at: `2026-08-21T02:28:46Z`
- source_manifest_sha256: `3a374d91ae20e115a1d61d5657d0d883f41c33a41cfe283d0545b13d9f4150bc`
- inventory_schema_version: `1`

## Corpus

- total pages: 20432
- acquisition failures recorded: 0
- redirects: 1353 (API/wikitext redirect-status mismatches: 0)
- pages by namespace:
  - User: 6894
  - User blog comment: 3026
  - Fanon: 2544
  - Fanon talk: 1586
  - Template: 1356
  - User blog: 933
  - Project: 702
  - Project talk: 699
  - Help talk: 434
  - Template talk: 427
  - (Main): 380
  - Tale of Diep talk: 333
  - MediaWiki talk: 325
  - Category: 266
  - Category talk: 249
  - MediaWiki: 165
  - Tale of Diep: 42
  - Talk: 23
  - Help: 18
  - Module: 11
  - Module talk: 10
  - Blog: 6
  - Blog talk: 3
- pages by primary page type (heuristic, evidence-backed — see pages.jsonl for full multi-label evidence):
  - user_pages: 10853
  - unknown: 7258
  - templates: 1356
  - discussion_forum: 699
  - categories: 266

## Information structure

- categories observed: 337
- unique section headings: 14586
- most frequent section headings:
  - "Trivia": 2597
  - "Design": 2504
  - "Technical": 2116
  - "Strategy": 1387
  - "My favorite pages": 1089
  - "Gallery": 464
  - "Attacks": 399
  - "Description": 383
  - "Stats": 329
  - "Overview": 304
  - "Appearance": 294
  - "Strategies": 185
  - "Phase 2": 177
  - "Phase 1": 168
  - "History": 136
- template/infobox types observed: 1149
- most frequent templates:
  - "f": 3728 use(s), 2 distinct field(s)
  - "Fanon": 3434 use(s), 168 distinct field(s) (conflicting field-name groups: [['author-username', 'author username'], ['boss HP?', 'Boss HP?'], ['Title', 'title'], ['level', 'Level'], ['tier', 'Tier'], ['Upgrades from', 'upgrades from']])
  - "2": 3081 use(s), 3 distinct field(s)
  - "Talk": 1756 use(s), 0 distinct field(s)
  - "MW": 1753 use(s), 2 distinct field(s)
  - "d": 1596 use(s), 2 distinct field(s)
  - "Build": 920 use(s), 75 distinct field(s)
  - "Achievement": 867 use(s), 8 distinct field(s) (conflicting field-name groups: [['Color', 'color']])
  - "User": 833 use(s), 1 distinct field(s)
  - "p": 810 use(s), 2 distinct field(s)
  - "u": 774 use(s), 2 distinct field(s)
  - "NI": 739 use(s), 3 distinct field(s)
  - "TalkLog": 578 use(s), 0 distinct field(s)
  - "PA": 478 use(s), 1 distinct field(s)
  - "TalkHelp": 403 use(s), 0 distinct field(s)
- tables found: 638 (parse_quality distribution: {'complete': 242, 'partial': 396})
- link connectivity: 17651 orphan page(s) (no inbound internal link from this corpus); top hub titles: ['Staff', 'Cannons', 'Destroyer', 'Tank', 'Board:Violation Reporting']

## Domains

(candidate domains, heuristic multi-label — a page may appear in multiple domains or none)
- historical_removed_content: 188 page(s), e.g. pageids [102055, 103834, 10486, 105223, 1101]
- changelog_updates: 152 page(s), e.g. pageids [105223, 110285, 111119, 112112, 124682]
- tanks: 0 page(s), e.g. pageids []
- tank_tiers_classes: 0 page(s), e.g. pageids []
- upgrade_relationships: 0 page(s), e.g. pageids []
- shapes_polygons: 0 page(s), e.g. pageids []
- bosses: 0 page(s), e.g. pageids []
- weapons: 0 page(s), e.g. pageids []
- ammunition: 0 page(s), e.g. pageids []
- stats: 0 page(s), e.g. pageids []
- mechanics: 0 page(s), e.g. pageids []
- levels: 0 page(s), e.g. pageids []
- game_modes: 0 page(s), e.g. pageids []
- maps_map_features: 0 page(s), e.g. pageids []
- builds_strategies: 0 page(s), e.g. pageids []
- event_content: 0 page(s), e.g. pageids []
- unknown_domain (no signal matched): 20188 page(s)

## Temporal data

- tables flagged changelog_like: 5
- section headings suggesting versioned/historical content: ['History of the Diep.io Wiki Project', 'History', 'History Project', 'Main Page Updates', 'Banners Script Death — The Future of Weekly Updates', 'News Team Dissolvement (Weekly Updates will continue as normal)', 'Sandboxes & Weekly Updates', 'Weekly Updates Renamed', 'Linking directly to the Changelog page', 'Discontiuned - No more updates', 'Changelog', 'Update 2', 'Update 1', 'Update 3', 'Edits History']
- pages classified historical_removed_content: 188
- pages classified changelog_updates: 152
- still-unknown: whether the corpus provides enough machine-readable structure (vs. free prose) to reliably derive valid_from/valid_to/status/version fields requires reviewing the representative table/section examples above; this run only measures their existence and frequency, not their semantic content.

## Provenance

- fields retained per raw page: pageid, title, namespace, canonical_url, revision id/parentid/timestamp/contentmodel/contentformat, retrieved_at, content_sha256, categories, redirect status/target.
- fields deliberately NOT retained (data minimization): revision author, revision edit-comment.
- source coordinates NOT currently retained for sub-page facts: exact character offset of a given section/template/table within a page's wikitext (only page-level plus nearest-heading context is recorded — see tables.json's `heading` field and templates.json's per-template aggregation).

## Schema implications

- requires-extending: 6 template(s) show conflicting/duplicate field-name variants (e.g. differing casing/underscore/spacing for what is likely the same concept) — a future schema needs an explicit field-name normalization/alias layer, not a 1:1 raw-field mapping.
- still-unknown: 20188 page(s) have no matched domain signal — this is expected given the classification-signal config ships empty in this slice; populating it is a separate, deliberate curation step before domain counts here can be treated as representative.
- supports: API-reported redirect status and wikitext #REDIRECT markers agreed on every acquired page in this snapshot — a future schema can treat either source as reliable for redirect detection.
- requires-extending: 396/638 table(s) parsed only partially or failed outright — a future schema must not assume every wiki table is machine-parseable; some stat/upgrade data may need per-template (infobox) extraction instead of table extraction, or manual curation.
- still-unknown: whether categories alone provide enough of an implicit ontology for the eventual schema, or whether infobox template fields will need to carry most of the semantic weight, cannot be judged until classification_signals.json is populated with real curated vocabulary and re-run.
- contradicts: nothing in this snapshot outright contradicts the schema ideas reviewed so far; recorded here because every run should explicitly state a finding for this category, not omit it.

## Data quality problems

- acquisition failures: 0 (see acquisition_failures.json)
- redirect status mismatches: 0
- table parse_quality != complete: 396
- templates with likely conflicting/duplicate field-name variants: 6
- pages with unknown page-type classification: 7258
  - by namespace (diagnostic only -- classification_signals.json stays empty; this is visibility into which namespaces the current structural rules don't cover, not a proposal to add corpus-specific vocabulary this run):
    - Fanon: 2544
    - Fanon talk: 1586
    - Project: 702
    - Project talk: 699
    - Help talk: 434
    - (Main): 380
    - Tale of Diep talk: 333
    - MediaWiki talk: 325
    - MediaWiki: 165
    - Tale of Diep: 42
    - Help: 18
    - Module: 11
    - Module talk: 10
    - Blog: 6
    - Blog talk: 3
- pages with unknown_domain: 20188

