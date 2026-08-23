# knowledge-normalization-v0 — Normalization Report

- snapshot_id: `20260821T022701Z`
- normalization_schema_version: `v0`

## Candidate pool

- Main-namespace (ns=0), non-redirect candidate pages: 156
- normalized tank_v0 entities: 58
- normalized shape_v0 entities: 11
- unknown_entity (entity-like, unsupported structure): 15
- ambiguous pages (more than one candidate Infobox on one page): 2
- skipped, no recognized entity-defining template (Infobox/Polygon/Boss): 72

## Representative unsupported reasons

- boss entity page, out of scope for tank_v0/shape_v0 in this slice: 12 page(s), e.g. ['Fallen Booster', 'Summoner', 'Fallen Overlord', 'Defender', 'Guardian']
- expected exactly one Infobox template, found 3: 1 page(s), e.g. ['Dominator']
- Infobox has no id field: 1 page(s), e.g. ['Draft:Arras:Arms Race/Leviathan']
- expected exactly one Infobox template, found 4: 1 page(s), e.g. ['Beta.diep.io']

## Relations

- upgrades_to edges: 67 (active 62, removed 5, unknown 0)
- variant_of edges: 4

## Extraction quality

- validation failures: 0 (fail-fast: a schema-invalid record aborts the run before this report is written -- see normalize.py's validate-on-construction step)
- unresolved upgrade link targets: 0
- unresolved variant link targets: 0
- previous/next cross-check disagreements (a tank's `previous` link has no matching `upgrades_to` edge from that parent): 0

