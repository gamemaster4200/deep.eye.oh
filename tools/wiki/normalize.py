"""Offline normalization: reads the Main-namespace (ns=0), non-redirect page
pool from the tracked knowledge/inventory/pages.jsonl (the structural
canonical-candidate filter -- NOT a claim of canonical truth, see
entity.schema.json), fetches each candidate's full wikitext from a raw
acquisition snapshot (knowledge/raw/fandom/<snapshot-id>/, not tracked; see
--snapshot-dir), and produces real tank_v0/shape_v0/unknown_entity entities
plus upgrades_to/variant_of relation edges under knowledge/entities/ and
knowledge/relations/, every one validated against knowledge/schema/v0/
before being written.

Never touches the network. Every emitted entity/relation carries a
provenance block tracing it back to the exact source page/snapshot.

Development tooling only — not part of the production pipeline.

    python tools/wiki/normalize.py --snapshot-dir knowledge/raw/fandom/20260821T022701Z
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _normalize_relations
import _normalize_shape
import _normalize_tank
import _schema_validate
import _wikitext

NORMALIZATION_SCHEMA_VERSION = "v0"


# --- Loading -----------------------------------------------------------

def load_pages_index(inventory_dir: Path) -> dict[str, dict]:
    """title -> {pageid, namespace_id, is_redirect, redirect_target_title}
    for every page in the tracked inventory (all namespaces -- used for
    redirect resolution of relation link targets, not just candidates)."""
    index = {}
    with (inventory_dir / "pages.jsonl").open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            index[row["title"]] = {
                "pageid": row["pageid"],
                "namespace_id": row["namespace_id"],
                "is_redirect": row["is_redirect"],
                "redirect_target_title": row.get("redirect_target_title"),
            }
    return index


def build_alias_index(pages_index: dict[str, dict]) -> dict[int, list[str]]:
    """target pageid -> sorted list of Main-namespace redirect page titles
    pointing to it (e.g. "Basic" -> "Tank"), for populating entity.aliases."""
    aliases: dict[int, list[str]] = {}
    for title, row in pages_index.items():
        if row["namespace_id"] == 0 and row["is_redirect"] and row["redirect_target_title"]:
            target = pages_index.get(row["redirect_target_title"])
            if target is not None:
                aliases.setdefault(target["pageid"], []).append(title)
    for pageid in aliases:
        aliases[pageid].sort()
    return aliases


def main_namespace_candidates(pages_index: dict[str, dict]) -> list[tuple[int, str]]:
    candidates = [
        (row["pageid"], title)
        for title, row in pages_index.items()
        if row["namespace_id"] == 0 and not row["is_redirect"]
    ]
    candidates.sort(key=lambda item: item[0])
    return candidates


def load_manifest(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))


def load_raw_page(snapshot_dir: Path, pageid: int) -> dict | None:
    path = snapshot_dir / "pages" / f"{pageid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_wiki_title(title: str) -> str:
    """MediaWiki title normalization for link targets extracted from
    wikitext: strip a `#section` fragment, treat `_`/` ` as interchangeable,
    and auto-capitalize the first character (observed on the real corpus:
    Destroyer's `next` field links "firework" for the page actually titled
    "Firework"; Gunner's links "Auto_5#History" for "Auto 5")."""
    text = title.split("#", 1)[0].strip().replace("_", " ")
    return text[:1].upper() + text[1:] if text else text


def resolve_title(title: str, pages_index: dict[str, dict]) -> str:
    """MediaWiki title normalization, then single-hop redirect resolution
    (matches wiki-inventory-v0's own non-chasing redirect model -- see
    docs/handoffs/wiki-inventory-v0.md)."""
    normalized = normalize_wiki_title(title)
    row = pages_index.get(normalized)
    if row and row["is_redirect"] and row["redirect_target_title"]:
        return row["redirect_target_title"]
    return normalized


# --- Provenance / envelope ------------------------------------------------

def build_provenance(snapshot_id: str, pageid: int, content_sha256: str, template_occurrence_index: int | None = None) -> dict:
    prov = {"snapshot_id": snapshot_id, "pageid": pageid, "content_sha256": content_sha256}
    if template_occurrence_index is not None:
        prov["template_occurrence_index"] = template_occurrence_index
    return prov


def build_entity(entity_id: str, entity_type: str, title: str, aliases: list[str], entity_status: str, payload: dict, provenance: dict) -> dict:
    return {
        "schema_version": NORMALIZATION_SCHEMA_VERSION,
        "id": entity_id,
        "type": entity_type,
        "name": title,
        "aliases": aliases,
        "entity_status": entity_status,
        "source_domain": "canonical_candidate",
        "provenance": provenance,
        "payload": payload,
    }


# --- Classification / normalization pass ----------------------------------

class Diagnostics:
    def __init__(self):
        self.candidate_count = 0
        self.tank_count = 0
        self.shape_count = 0
        self.unknown_count = 0
        self.ambiguous_count = 0
        self.skipped_non_entity_count = 0
        self.unknown_reasons: dict[str, list[str]] = {}
        self.upgrade_active = 0
        self.upgrade_removed = 0
        self.upgrade_unknown = 0
        self.variant_edges = 0
        self.unresolved_upgrade_targets: list[str] = []
        self.unresolved_variant_targets: list[str] = []
        self.previous_next_disagreements: list[str] = []
        self.raw_page_missing: list[int] = []

    def record_unknown(self, title: str, reason: str) -> None:
        self.unknown_reasons.setdefault(reason, []).append(title)


def classify_and_normalize_page(
    pageid: int,
    title: str,
    templates: list[dict],
    content_sha256: str,
    snapshot_id: str,
    pages_index: dict[str, dict],
    aliases_by_pageid: dict[int, list[str]],
    diagnostics: Diagnostics,
) -> tuple[str, dict | None, dict | None]:
    """Returns (kind, entity_or_None, tank_working_record_or_None).
    kind is one of "tank", "shape", "unknown", "skip"."""
    aliases = aliases_by_pageid.get(pageid, [])
    infobox = [(i, t) for i, t in enumerate(templates) if t["name"] == "Infobox"]
    polygon = [(i, t) for i, t in enumerate(templates) if t["name"] == "Polygon"]
    boss = [t for t in templates if t["name"] == "Boss"]

    if infobox:
        fields, reason = _normalize_tank.classify_tank(templates)
        if fields is not None:
            template_index = infobox[0][0]
            provenance = build_provenance(snapshot_id, pageid, content_sha256, template_index)
            basestats = [t for t in templates if t["name"] == "BaseStats"]
            payload = _normalize_tank.build_tank_payload(fields, basestats)
            entity_id = f"tank_v0:{pageid}"
            entity = build_entity(entity_id, "tank_v0", title, aliases, "active", payload, provenance)
            diagnostics.tank_count += 1
            working = {
                "pageid": pageid,
                "title": title,
                "entity_id": entity_id,
                "content_sha256": content_sha256,
                "template_index": template_index,
                "next_field": fields.get("next", ""),
                "previous_field": fields.get("previous", ""),
            }
            return "tank", entity, working
        diagnostics.record_unknown(title, reason)
        if len(infobox) > 1:
            diagnostics.ambiguous_count += 1
        provenance = build_provenance(snapshot_id, pageid, content_sha256, infobox[0][0])
        entity_id = f"unknown_entity:{pageid}"
        entity = build_entity(entity_id, "unknown_entity", title, aliases, "unknown", {"reason": reason, "raw_evidence": {}}, provenance)
        diagnostics.unknown_count += 1
        return "unknown", entity, None

    if polygon:
        fields, reason = _normalize_shape.classify_shape(templates)
        if fields is not None:
            template_index = polygon[0][0]
            provenance = build_provenance(snapshot_id, pageid, content_sha256, template_index)
            payload = _normalize_shape.build_shape_payload(fields)
            entity_id = f"shape_v0:{pageid}"
            entity = build_entity(entity_id, "shape_v0", title, aliases, "active", payload, provenance)
            diagnostics.shape_count += 1
            working = {
                "pageid": pageid,
                "title": title,
                "entity_id": entity_id,
                "content_sha256": content_sha256,
                "template_index": template_index,
                "variants_field": fields.get("variants", ""),
            }
            return "shape", entity, working
        diagnostics.record_unknown(title, reason)
        provenance = build_provenance(snapshot_id, pageid, content_sha256, polygon[0][0])
        entity_id = f"unknown_entity:{pageid}"
        entity = build_entity(entity_id, "unknown_entity", title, aliases, "unknown", {"reason": reason, "raw_evidence": {}}, provenance)
        diagnostics.unknown_count += 1
        return "unknown", entity, None

    if boss:
        reason = "boss entity page, out of scope for tank_v0/shape_v0 in this slice"
        diagnostics.record_unknown(title, reason)
        provenance = build_provenance(snapshot_id, pageid, content_sha256)
        entity_id = f"unknown_entity:{pageid}"
        entity = build_entity(entity_id, "unknown_entity", title, aliases, "unknown", {"reason": reason, "raw_evidence": {}}, provenance)
        diagnostics.unknown_count += 1
        return "unknown", entity, None

    diagnostics.skipped_non_entity_count += 1
    return "skip", None, None


# --- Relation extraction ---------------------------------------------------

def extract_upgrade_edges(
    tank_working_records: list[dict],
    tank_entity_ids: set[str],
    pages_index: dict[str, dict],
    title_to_entity_id: dict[str, str],
    snapshot_id: str,
    diagnostics: Diagnostics,
) -> list[dict]:
    edges = []
    emitted_pairs = set()
    for tank in tank_working_records:
        links = _normalize_relations.extract_icon_links(tank["next_field"])
        for link in links:
            resolved_title = resolve_title(link["target"], pages_index)
            target_id = title_to_entity_id.get(resolved_title)
            if target_id is None or target_id not in tank_entity_ids:
                diagnostics.unresolved_upgrade_targets.append(f"{tank['title']} -> {link['target']}")
                continue
            status = "removed" if link["removed"] else ("unknown" if link["ref_body"] else "active")
            if status == "removed":
                diagnostics.upgrade_removed += 1
            elif status == "unknown":
                diagnostics.upgrade_unknown += 1
            else:
                diagnostics.upgrade_active += 1
            provenance = build_provenance(snapshot_id, tank["pageid"], tank["content_sha256"], tank["template_index"])
            edges.append(
                {
                    "from_id": tank["entity_id"],
                    "to_id": target_id,
                    "relation_type": "upgrades_to",
                    "status": status,
                    "provenance": provenance,
                }
            )
            emitted_pairs.add((tank["entity_id"], target_id))

    # Cross-check: previous is an independently-authored field on a
    # different page than the corresponding next -- agreement is a
    # diagnostic-only confidence signal, not a second edge source.
    for tank in tank_working_records:
        for link in _normalize_relations.extract_icon_links(tank["previous_field"]):
            resolved_title = resolve_title(link["target"], pages_index)
            parent_id = title_to_entity_id.get(resolved_title)
            if parent_id is None or parent_id not in tank_entity_ids:
                continue
            if (parent_id, tank["entity_id"]) not in emitted_pairs:
                diagnostics.previous_next_disagreements.append(f"{tank['title']}.previous={link['target']} has no matching next-edge")

    edges.sort(key=lambda e: (e["from_id"], e["to_id"], e["relation_type"]))
    return edges


def extract_variant_edges(
    shape_working_records: list[dict],
    shape_entity_ids: set[str],
    pages_index: dict[str, dict],
    title_to_entity_id: dict[str, str],
    snapshot_id: str,
    diagnostics: Diagnostics,
) -> list[dict]:
    edges = []
    for shape in shape_working_records:
        targets = _normalize_relations.extract_simple_links(shape["variants_field"])
        for target_title in targets:
            resolved_title = resolve_title(target_title, pages_index)
            target_id = title_to_entity_id.get(resolved_title)
            if target_id is None or target_id not in shape_entity_ids:
                diagnostics.unresolved_variant_targets.append(f"{shape['title']} -> {target_title}")
                continue
            provenance = build_provenance(snapshot_id, shape["pageid"], shape["content_sha256"], shape["template_index"])
            edges.append(
                {
                    "from_id": shape["entity_id"],
                    "to_id": target_id,
                    "relation_type": "variant_of",
                    "status": "active",
                    "provenance": provenance,
                }
            )
            diagnostics.variant_edges += 1
    edges.sort(key=lambda e: (e["from_id"], e["to_id"]))
    return edges


# --- Output -----------------------------------------------------------

def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def render_report_md(diagnostics: Diagnostics, snapshot_id: str) -> str:
    lines = []
    lines.append("# knowledge-normalization-v0 — Normalization Report")
    lines.append("")
    lines.append(f"- snapshot_id: `{snapshot_id}`")
    lines.append(f"- normalization_schema_version: `{NORMALIZATION_SCHEMA_VERSION}`")
    lines.append("")
    lines.append("## Candidate pool")
    lines.append("")
    lines.append(f"- Main-namespace (ns=0), non-redirect candidate pages: {diagnostics.candidate_count}")
    lines.append(f"- normalized tank_v0 entities: {diagnostics.tank_count}")
    lines.append(f"- normalized shape_v0 entities: {diagnostics.shape_count}")
    lines.append(f"- unknown_entity (entity-like, unsupported structure): {diagnostics.unknown_count}")
    lines.append(f"- ambiguous pages (more than one candidate Infobox on one page): {diagnostics.ambiguous_count}")
    lines.append(f"- skipped, no recognized entity-defining template (Infobox/Polygon/Boss): {diagnostics.skipped_non_entity_count}")
    if diagnostics.raw_page_missing:
        lines.append(f"- candidates missing from the raw snapshot (skipped): {len(diagnostics.raw_page_missing)} {diagnostics.raw_page_missing[:10]}")
    lines.append("")

    lines.append("## Representative unsupported reasons")
    lines.append("")
    for reason, titles in sorted(diagnostics.unknown_reasons.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {reason}: {len(titles)} page(s), e.g. {titles[:5]}")
    lines.append("")

    lines.append("## Relations")
    lines.append("")
    lines.append(
        f"- upgrades_to edges: {diagnostics.upgrade_active + diagnostics.upgrade_removed + diagnostics.upgrade_unknown} "
        f"(active {diagnostics.upgrade_active}, removed {diagnostics.upgrade_removed}, unknown {diagnostics.upgrade_unknown})"
    )
    lines.append(f"- variant_of edges: {diagnostics.variant_edges}")
    lines.append("")

    lines.append("## Extraction quality")
    lines.append("")
    lines.append(
        "- validation failures: 0 (fail-fast: a schema-invalid record aborts the run before this "
        "report is written -- see normalize.py's validate-on-construction step)"
    )
    lines.append(f"- unresolved upgrade link targets: {len(diagnostics.unresolved_upgrade_targets)}")
    for item in diagnostics.unresolved_upgrade_targets[:15]:
        lines.append(f"  - {item}")
    lines.append(f"- unresolved variant link targets: {len(diagnostics.unresolved_variant_targets)}")
    for item in diagnostics.unresolved_variant_targets[:15]:
        lines.append(f"  - {item}")
    lines.append(
        f"- previous/next cross-check disagreements (a tank's `previous` link has no matching "
        f"`upgrades_to` edge from that parent): {len(diagnostics.previous_next_disagreements)}"
    )
    for item in diagnostics.previous_next_disagreements[:15]:
        lines.append(f"  - {item}")
    lines.append("")
    return "\n".join(lines) + "\n"


# --- Orchestration -----------------------------------------------------

def run(snapshot_dir: Path, inventory_dir: Path, entities_out_dir: Path, relations_out_dir: Path, schema_dir: Path) -> Diagnostics:
    validators = _schema_validate.build_validators(schema_dir)
    manifest = load_manifest(snapshot_dir)
    snapshot_id = manifest.get("snapshot_id")

    pages_index = load_pages_index(inventory_dir)
    aliases_by_pageid = build_alias_index(pages_index)
    candidates = main_namespace_candidates(pages_index)

    diagnostics = Diagnostics()
    diagnostics.candidate_count = len(candidates)

    tanks: list[dict] = []
    shapes: list[dict] = []
    unknowns: list[dict] = []
    tank_working: list[dict] = []
    shape_working: list[dict] = []
    title_to_entity_id: dict[str, str] = {}

    for pageid, title in candidates:
        page = load_raw_page(snapshot_dir, pageid)
        if page is None:
            diagnostics.raw_page_missing.append(pageid)
            continue
        templates = _wikitext.extract_templates(page["wikitext"])
        kind, entity, working = classify_and_normalize_page(
            pageid, title, templates, page["content_sha256"], snapshot_id, pages_index, aliases_by_pageid, diagnostics,
        )
        if kind == "skip":
            continue

        errors = list(validators["entity"].iter_errors(entity))
        if errors:
            raise SystemExit(
                f"schema-invalid entity emitted for pageid={pageid} title={title!r}: "
                f"{errors[0].message} (path: {list(errors[0].path)})"
            )

        title_to_entity_id[title] = entity["id"]
        if kind == "tank":
            tanks.append(entity)
            tank_working.append(working)
        elif kind == "shape":
            shapes.append(entity)
            shape_working.append(working)
        else:
            unknowns.append(entity)

    tank_entity_ids = {t["id"] for t in tanks}
    shape_entity_ids = {s["id"] for s in shapes}

    upgrade_edges = extract_upgrade_edges(tank_working, tank_entity_ids, pages_index, title_to_entity_id, snapshot_id, diagnostics)
    variant_edges = extract_variant_edges(shape_working, shape_entity_ids, pages_index, title_to_entity_id, snapshot_id, diagnostics)
    relations = upgrade_edges + variant_edges

    for relation in relations:
        errors = list(validators["relation"].iter_errors(relation))
        if errors:
            raise SystemExit(
                f"schema-invalid relation emitted: {relation['from_id']} -> {relation['to_id']}: "
                f"{errors[0].message} (path: {list(errors[0].path)})"
            )

    write_jsonl(entities_out_dir / "tanks.jsonl", tanks)
    write_jsonl(entities_out_dir / "shapes.jsonl", shapes)
    if unknowns:
        write_jsonl(entities_out_dir / "unknown.jsonl", unknowns)
    write_jsonl(relations_out_dir / "relations.jsonl", relations)
    (entities_out_dir / "REPORT.md").write_text(render_report_md(diagnostics, snapshot_id), encoding="utf-8")

    return diagnostics


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-dir", required=True, help="path to a knowledge/raw/fandom/<snapshot-id> directory")
    parser.add_argument("--inventory-dir", default="knowledge/inventory", help="path to the tracked wiki-inventory-v0 output")
    parser.add_argument("--entities-out-dir", default="knowledge/entities", help="output directory for entity JSONL + REPORT.md")
    parser.add_argument("--relations-out-dir", default="knowledge/relations", help="output directory for relations.jsonl")
    parser.add_argument("--schema-dir", default="knowledge/schema/v0", help="path to knowledge/schema/v0")
    args = parser.parse_args(argv)

    diagnostics = run(
        Path(args.snapshot_dir),
        Path(args.inventory_dir),
        Path(args.entities_out_dir),
        Path(args.relations_out_dir),
        Path(args.schema_dir),
    )
    print(
        f"normalized {diagnostics.tank_count} tank(s), {diagnostics.shape_count} shape(s), "
        f"{diagnostics.unknown_count} unknown_entity(ies) from {diagnostics.candidate_count} candidate(s); "
        f"{diagnostics.upgrade_active + diagnostics.upgrade_removed + diagnostics.upgrade_unknown} upgrades_to edge(s), "
        f"{diagnostics.variant_edges} variant_of edge(s) -> {args.entities_out_dir}"
    )


if __name__ == "__main__":
    main()
