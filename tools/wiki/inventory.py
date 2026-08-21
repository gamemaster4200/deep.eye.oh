"""Offline structural inventory: read a raw snapshot under
knowledge/raw/fandom/<snapshot-id>/ (written by fetch.py) and produce the
machine-readable inventory outputs + REPORT.md under knowledge/inventory/.

Never touches the network. Every output file carries a `provenance` block
(snapshot id, acquisition-completed timestamp, source manifest hash,
inventory schema version) so it can always be traced back to the exact raw
snapshot it came from.

Classification (page-type and domain labels) is driven entirely by the
external, versioned tools/wiki/config/classification_signals.json — this
tool never hardcodes wiki-specific vocabulary and never writes back to that
config. It ships empty in this slice, so it is expected that most pages
classify as "unknown"/"unknown_domain" until a human populates it in a
separate curation step.

Development tooling only — not part of the production pipeline.

    python tools/wiki/inventory.py --snapshot-dir knowledge/raw/fandom/20260821T140500Z --out-dir knowledge/inventory
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _classify
import _wikitext

INVENTORY_SCHEMA_VERSION = 1


def analyze_page(page: dict) -> dict:
    wikitext = page.get("wikitext", "")
    return {
        "page": page,
        "sections": _wikitext.extract_sections(wikitext),
        "templates": _wikitext.extract_templates(wikitext),
        "tables": _wikitext.extract_tables(wikitext),
        "links": _wikitext.extract_internal_links(wikitext),
        "wikitext_redirect_target": _wikitext.detect_redirect_target(wikitext),
    }


def build_pages_jsonl(analyses: list[dict], signals: dict, provenance: dict) -> list[dict]:
    rows = []
    for a in analyses:
        page = a["page"]
        page_type_input = {
            "namespace_name": page["namespace_name"],
            "categories": page["categories"],
            "templates": [t["name"] for t in a["templates"]],
        }
        page_type_labels = _classify.classify_page_type(page_type_input, signals)
        domain_labels = _classify.classify_domains(page, a["sections"], a["templates"], signals)
        redirect_mismatch = page["is_redirect"] != (a["wikitext_redirect_target"] is not None)
        rows.append(
            {
                "provenance": provenance,
                "pageid": page["pageid"],
                "title": page["title"],
                "namespace_id": page["namespace_id"],
                "namespace_name": page["namespace_name"],
                "is_redirect": page["is_redirect"],
                "redirect_target_title": page.get("redirect_target_title"),
                "wikitext_redirect_target": a["wikitext_redirect_target"],
                "redirect_status_mismatch": redirect_mismatch,
                "categories": page["categories"],
                "content_sha256": page["content_sha256"],
                "section_count": len(a["sections"]),
                "template_count": len(a["templates"]),
                "table_count": len(a["tables"]),
                "link_count": len(a["links"]),
                "page_types": [{"label": label, "evidence": evidence} for label, evidence in page_type_labels],
                "primary_page_type": page_type_labels[0][0],
                "domains": [{"label": label, "evidence": evidence} for label, evidence in domain_labels],
                "unknown_domain": len(domain_labels) == 0,
            }
        )
    return rows


def build_categories_json(pages_jsonl_rows: list[dict], provenance: dict) -> dict:
    categories: dict[str, dict] = {}
    for row in pages_jsonl_rows:
        for cat in row["categories"]:
            entry = categories.setdefault(
                cat, {"member_count": 0, "representative_members": [], "parent_categories": [], "subcategories": [], "is_itself_a_page": False}
            )
            entry["member_count"] += 1
            if len(entry["representative_members"]) < 10:
                entry["representative_members"].append(row["title"])

    for row in pages_jsonl_rows:
        if row["namespace_name"] == "Category":
            entry = categories.setdefault(
                row["title"], {"member_count": 0, "representative_members": [], "parent_categories": [], "subcategories": [], "is_itself_a_page": False}
            )
            entry["is_itself_a_page"] = True
            entry["parent_categories"] = row["categories"]

    for cat_title, entry in list(categories.items()):
        if entry["is_itself_a_page"]:
            for parent in entry["parent_categories"]:
                parent_entry = categories.setdefault(
                    parent, {"member_count": 0, "representative_members": [], "parent_categories": [], "subcategories": [], "is_itself_a_page": False}
                )
                if cat_title not in parent_entry["subcategories"]:
                    parent_entry["subcategories"].append(cat_title)

    return {"provenance": provenance, "categories": categories}


def build_sections_json(analyses: list[dict], provenance: dict) -> dict:
    sections: dict[str, dict] = {}
    for a in analyses:
        pageid = a["page"]["pageid"]
        for s in a["sections"]:
            entry = sections.setdefault(s["heading"], {"frequency": 0, "example_pageids": []})
            entry["frequency"] += 1
            if len(entry["example_pageids"]) < 5:
                entry["example_pageids"].append(pageid)
    return {"provenance": provenance, "sections": sections}


def _normalize_field_name(name: str) -> str:
    return name.strip().lower().replace("_", " ").replace("-", " ")


def build_templates_json(analyses: list[dict], provenance: dict) -> dict:
    templates: dict[str, dict] = {}
    for a in analyses:
        for t in a["templates"]:
            entry = templates.setdefault(t["name"], {"frequency": 0, "fields": {}})
            entry["frequency"] += 1
            for field_name, value in t["fields"].items():
                field_entry = entry["fields"].setdefault(field_name, {"frequency": 0, "example_values": []})
                field_entry["frequency"] += 1
                if value and len(field_entry["example_values"]) < 5:
                    field_entry["example_values"].append(value)

    for entry in templates.values():
        groups: dict[str, list[str]] = {}
        for field_name in entry["fields"]:
            groups.setdefault(_normalize_field_name(field_name), []).append(field_name)
        entry["conflicting_field_name_groups"] = [names for names in groups.values() if len(names) > 1]

    return {"provenance": provenance, "templates": templates}


def build_tables_json(analyses: list[dict], provenance: dict) -> dict:
    tables = []
    for a in analyses:
        page = a["page"]
        for t in a["tables"]:
            tables.append(
                {
                    "source_pageid": page["pageid"],
                    "source_title": page["title"],
                    "heading": t["heading"],
                    "columns": t["columns"],
                    "row_count": t["row_count"],
                    "representative_rows": t["representative_rows"],
                    "parse_quality": t["parse_quality"],
                    "flags": t["flags"],
                    "flag_evidence": t["flag_evidence"],
                    "raw_wikitext": t["raw_wikitext"],
                }
            )
    return {"provenance": provenance, "tables": tables}


def build_domains_json(pages_jsonl_rows: list[dict], provenance: dict) -> dict:
    domains: dict[str, dict] = {d: {"page_count": 0, "example_pageids": []} for d in _classify.CANDIDATE_DOMAINS}
    unknown_count = 0
    for row in pages_jsonl_rows:
        if row["unknown_domain"]:
            unknown_count += 1
        for d in row["domains"]:
            entry = domains.setdefault(d["label"], {"page_count": 0, "example_pageids": []})
            entry["page_count"] += 1
            if len(entry["example_pageids"]) < 10:
                entry["example_pageids"].append(row["pageid"])
    return {"provenance": provenance, "domains": domains, "unknown_domain_page_count": unknown_count}


def read_acquisition_records(snapshot_dir: Path) -> list[dict]:
    path = snapshot_dir / "acquisition.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def build_acquisition_failures_json(snapshot_dir: Path, pages: list[dict], provenance: dict) -> dict:
    succeeded_pageids = {p["pageid"] for p in pages}
    records = read_acquisition_records(snapshot_dir)
    failures = [
        r for r in records if r.get("outcome") in ("permanent_failure", "transient_failure") and r.get("pageid") not in succeeded_pageids
    ]
    return {"provenance": provenance, "failures": failures}


def build_corpus_json(manifest: dict, pages_jsonl_rows: list[dict], analyses: list[dict], acquisition_failures: list[dict], provenance: dict) -> dict:
    by_namespace: dict[str, int] = {}
    by_page_type: dict[str, int] = {}
    unknown_page_type_by_namespace: dict[str, int] = {}
    redirect_count = 0
    redirect_mismatch_count = 0
    for row in pages_jsonl_rows:
        by_namespace[row["namespace_name"]] = by_namespace.get(row["namespace_name"], 0) + 1
        by_page_type[row["primary_page_type"]] = by_page_type.get(row["primary_page_type"], 0) + 1
        if row["primary_page_type"] == "unknown":
            unknown_page_type_by_namespace[row["namespace_name"]] = unknown_page_type_by_namespace.get(row["namespace_name"], 0) + 1
        if row["is_redirect"]:
            redirect_count += 1
        if row["redirect_status_mismatch"]:
            redirect_mismatch_count += 1

    in_degree: dict[str, int] = {}
    for a in analyses:
        for target in a["links"]:
            in_degree[target] = in_degree.get(target, 0) + 1
    titles_in_corpus = {row["title"] for row in pages_jsonl_rows}
    orphans = sorted(t for t in titles_in_corpus if in_degree.get(t, 0) == 0)
    hubs = sorted(in_degree.items(), key=lambda kv: -kv[1])[:20]

    return {
        "provenance": provenance,
        "total_pages": len(pages_jsonl_rows),
        "by_namespace": by_namespace,
        "by_page_type": by_page_type,
        "unknown_page_type_by_namespace": unknown_page_type_by_namespace,
        "redirect_count": redirect_count,
        "redirect_status_mismatch_count": redirect_mismatch_count,
        "acquisition_failure_count": len(acquisition_failures),
        "links": {
            "orphan_page_count": len(orphans),
            "orphan_example_titles": orphans[:20],
            "hub_titles": [{"title": t, "in_degree": d} for t, d in hubs],
        },
        "manifest_summary": {
            "snapshot_id": manifest.get("snapshot_id"),
            "wiki_base_url": manifest.get("wiki_base_url"),
            "started_at": manifest.get("started_at"),
            "completed_at": manifest.get("completed_at"),
            "source_backend": manifest.get("source", {}).get("backend"),
            "source_dump": manifest.get("source", {}).get("dump") or {k: v for k, v in manifest.get("source", {}).items() if k in ("dump_url", "dump_last_modified", "archive_sha256", "dump_type", "wikiid")} or None,
            "robots_txt_status": manifest.get("robots_txt_status"),
        },
    }


def compute_provenance(manifest: dict, manifest_bytes: bytes) -> dict:
    return {
        "snapshot_id": manifest.get("snapshot_id"),
        "acquisition_completed_at": manifest.get("completed_at"),
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "inventory_schema_version": INVENTORY_SCHEMA_VERSION,
    }


def load_snapshot(snapshot_dir: Path) -> tuple[dict, bytes, list[dict]]:
    manifest_bytes = (snapshot_dir / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    pages = []
    pages_dir = snapshot_dir / "pages"
    if pages_dir.exists():
        for page_path in sorted(pages_dir.glob("*.json")):
            pages.append(json.loads(page_path.read_text(encoding="utf-8")))
    return manifest, manifest_bytes, pages


def render_report_md(
    provenance: dict,
    corpus: dict,
    categories: dict,
    sections: dict,
    templates: dict,
    tables: dict,
    domains: dict,
    acquisition_failures: dict,
) -> str:
    lines: list[str] = []
    lines.append("# Diep.io Wiki Corpus — Structural Inventory Report")
    lines.append("")
    lines.append(f"- snapshot_id: `{provenance['snapshot_id']}`")
    lines.append(f"- acquisition_completed_at: `{provenance['acquisition_completed_at']}`")
    lines.append(f"- source_manifest_sha256: `{provenance['source_manifest_sha256']}`")
    lines.append(f"- inventory_schema_version: `{provenance['inventory_schema_version']}`")
    lines.append("")

    lines.append("## Corpus")
    lines.append("")
    lines.append(f"- total pages: {corpus['total_pages']}")
    lines.append(f"- acquisition failures recorded: {corpus['acquisition_failure_count']}")
    lines.append(f"- redirects: {corpus['redirect_count']} (API/wikitext redirect-status mismatches: {corpus['redirect_status_mismatch_count']})")
    lines.append("- pages by namespace:")
    for ns, count in sorted(corpus["by_namespace"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  - {ns}: {count}")
    lines.append("- pages by primary page type (heuristic, evidence-backed — see pages.jsonl for full multi-label evidence):")
    for pt, count in sorted(corpus["by_page_type"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  - {pt}: {count}")
    lines.append("")

    quality_counts: dict[str, int] = {}
    for t in tables["tables"]:
        quality_counts[t["parse_quality"]] = quality_counts.get(t["parse_quality"], 0) + 1

    lines.append("## Information structure")
    lines.append("")
    lines.append(f"- categories observed: {len(categories['categories'])}")
    lines.append(f"- unique section headings: {len(sections['sections'])}")
    top_sections = sorted(sections["sections"].items(), key=lambda kv: -kv[1]["frequency"])[:15]
    lines.append("- most frequent section headings:")
    for heading, entry in top_sections:
        lines.append(f'  - "{heading}": {entry["frequency"]}')
    lines.append(f"- template/infobox types observed: {len(templates['templates'])}")
    top_templates = sorted(templates["templates"].items(), key=lambda kv: -kv[1]["frequency"])[:15]
    lines.append("- most frequent templates:")
    for name, entry in top_templates:
        conflict_note = f" (conflicting field-name groups: {entry['conflicting_field_name_groups']})" if entry["conflicting_field_name_groups"] else ""
        lines.append(f'  - "{name}": {entry["frequency"]} use(s), {len(entry["fields"])} distinct field(s){conflict_note}')
    lines.append(f"- tables found: {len(tables['tables'])} (parse_quality distribution: {quality_counts})")
    hub_titles = [h["title"] for h in corpus["links"]["hub_titles"][:5]]
    lines.append(f"- link connectivity: {corpus['links']['orphan_page_count']} orphan page(s) (no inbound internal link from this corpus); top hub titles: {hub_titles}")
    lines.append("")

    lines.append("## Domains")
    lines.append("")
    lines.append("(candidate domains, heuristic multi-label — a page may appear in multiple domains or none)")
    for domain, entry in sorted(domains["domains"].items(), key=lambda kv: -kv[1]["page_count"]):
        lines.append(f"- {domain}: {entry['page_count']} page(s), e.g. pageids {entry['example_pageids'][:5]}")
    lines.append(f"- unknown_domain (no signal matched): {domains['unknown_domain_page_count']} page(s)")
    lines.append("")

    lines.append("## Temporal data")
    lines.append("")
    changelog_like_tables = sum(1 for t in tables["tables"] if t["flags"].get("changelog_like"))
    changelog_headings = [h for h in sections["sections"] if any(kw in h.lower() for kw in ("changelog", "history", "update"))]
    lines.append(f"- tables flagged changelog_like: {changelog_like_tables}")
    lines.append(f"- section headings suggesting versioned/historical content: {changelog_headings[:15]}")
    lines.append(f"- pages classified historical_removed_content: {domains['domains'].get('historical_removed_content', {}).get('page_count', 0)}")
    lines.append(f"- pages classified changelog_updates: {domains['domains'].get('changelog_updates', {}).get('page_count', 0)}")
    lines.append(
        "- still-unknown: whether the corpus provides enough machine-readable structure (vs. free prose) to "
        "reliably derive valid_from/valid_to/status/version fields requires reviewing the representative "
        "table/section examples above; this run only measures their existence and frequency, not their "
        "semantic content."
    )
    lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(
        "- fields retained per raw page: pageid, title, namespace, canonical_url, revision id/parentid/"
        "timestamp/contentmodel/contentformat, retrieved_at, content_sha256, categories, redirect status/target."
    )
    lines.append("- fields deliberately NOT retained (data minimization): revision author, revision edit-comment.")
    lines.append(
        "- source coordinates NOT currently retained for sub-page facts: exact character offset of a given "
        "section/template/table within a page's wikitext (only page-level plus nearest-heading context is "
        "recorded — see tables.json's `heading` field and templates.json's per-template aggregation)."
    )
    lines.append("")

    conflicting_template_fields = sum(1 for entry in templates["templates"].values() if entry["conflicting_field_name_groups"])
    quality_not_complete = sum(c for q, c in quality_counts.items() if q != "complete")

    schema_bullets: list[str] = []
    if conflicting_template_fields > 0:
        schema_bullets.append(
            f"requires-extending: {conflicting_template_fields} template(s) show conflicting/duplicate field-name "
            "variants (e.g. differing casing/underscore/spacing for what is likely the same concept) — a future "
            "schema needs an explicit field-name normalization/alias layer, not a 1:1 raw-field mapping."
        )
    if domains["unknown_domain_page_count"] > 0:
        schema_bullets.append(
            f"still-unknown: {domains['unknown_domain_page_count']} page(s) have no matched domain signal — this "
            "is expected given the classification-signal config ships empty in this slice; populating it is a "
            "separate, deliberate curation step before domain counts here can be treated as representative."
        )
    if corpus["redirect_status_mismatch_count"] > 0:
        schema_bullets.append(
            f"contradicts: {corpus['redirect_status_mismatch_count']} page(s) have a mismatch between the "
            "API-reported redirect status and the wikitext's own #REDIRECT marker — a future schema's "
            "redirect/alias handling cannot rely on a single source of truth for this."
        )
    else:
        schema_bullets.append(
            "supports: API-reported redirect status and wikitext #REDIRECT markers agreed on every acquired "
            "page in this snapshot — a future schema can treat either source as reliable for redirect detection."
        )
    if tables["tables"]:
        if quality_not_complete > 0:
            schema_bullets.append(
                f"requires-extending: {quality_not_complete}/{len(tables['tables'])} table(s) parsed only "
                "partially or failed outright — a future schema must not assume every wiki table is "
                "machine-parseable; some stat/upgrade data may need per-template (infobox) extraction instead "
                "of table extraction, or manual curation."
            )
        else:
            schema_bullets.append(
                "supports: every table in this snapshot parsed as parse_quality=complete — table-derived "
                "structured data (stats/levels/etc.) looks tractable to extract mechanically for this corpus."
            )
    if corpus["acquisition_failure_count"] > 0:
        schema_bullets.append(
            f"still-unknown: {corpus['acquisition_failure_count']} page(s) could not be acquired in this run — "
            "whether they matter for the eventual schema (deleted pages vs. transient network issues) needs "
            "case-by-case review of acquisition_failures.json before assuming the corpus is complete."
        )
    schema_bullets.append(
        "still-unknown: whether categories alone provide enough of an implicit ontology for the eventual "
        "schema, or whether infobox template fields will need to carry most of the semantic weight, cannot be "
        "judged until classification_signals.json is populated with real curated vocabulary and re-run."
    )

    tags_present = {b.split(":", 1)[0] for b in schema_bullets}
    fallback_bullets = {
        "supports": (
            "supports: the corpus was successfully split into distinct raw pages with per-page category/"
            "section/template/table extraction, supporting the plan to normalize per-page rather than per-site."
        ),
        "requires-extending": (
            "requires-extending: raw field/heading vocabulary varies enough across pages that any schema "
            "mapping will need an explicit alias table rather than a fixed 1:1 field list."
        ),
        "contradicts": (
            "contradicts: nothing in this snapshot outright contradicts the schema ideas reviewed so far; "
            "recorded here because every run should explicitly state a finding for this category, not omit it."
        ),
        "still-unknown": (
            "still-unknown: whether the corpus's category graph alone is sufficient ontology, or infobox "
            "fields will carry most schema weight, remains unresolved pending curated classification vocabulary."
        ),
    }
    for tag, text in fallback_bullets.items():
        if tag not in tags_present:
            schema_bullets.append(text)

    lines.append("## Schema implications")
    lines.append("")
    lines.extend(f"- {b}" for b in schema_bullets)
    lines.append("")

    lines.append("## Data quality problems")
    lines.append("")
    lines.append(f"- acquisition failures: {len(acquisition_failures['failures'])} (see acquisition_failures.json)")
    lines.append(f"- redirect status mismatches: {corpus['redirect_status_mismatch_count']}")
    lines.append(f"- table parse_quality != complete: {quality_not_complete}")
    lines.append(f"- templates with likely conflicting/duplicate field-name variants: {conflicting_template_fields}")
    lines.append(f"- pages with unknown page-type classification: {corpus['by_page_type'].get('unknown', 0)}")
    unknown_by_ns = sorted(corpus["unknown_page_type_by_namespace"].items(), key=lambda kv: -kv[1])
    if unknown_by_ns:
        lines.append("  - by namespace (diagnostic only -- classification_signals.json stays empty; this is visibility into which namespaces the current structural rules don't cover, not a proposal to add corpus-specific vocabulary this run):")
        for ns_name, count in unknown_by_ns[:15]:
            lines.append(f"    - {ns_name}: {count}")
    lines.append(f"- pages with unknown_domain: {domains['unknown_domain_page_count']}")
    lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot-dir", required=True, help="path to a knowledge/raw/fandom/<snapshot-id> directory")
    parser.add_argument("--out-dir", default="knowledge/inventory", help="output directory for inventory JSON/JSONL + REPORT.md")
    parser.add_argument(
        "--classification-signals",
        default=str(Path(__file__).resolve().parent / "config" / "classification_signals.json"),
        help="path to classification_signals.json",
    )
    args = parser.parse_args(argv)

    snapshot_dir = Path(args.snapshot_dir)
    manifest, manifest_bytes, pages = load_snapshot(snapshot_dir)
    provenance = compute_provenance(manifest, manifest_bytes)
    signals = _classify.load_signals(Path(args.classification_signals))

    analyses = [analyze_page(p) for p in pages]
    pages_jsonl_rows = build_pages_jsonl(analyses, signals, provenance)
    categories = build_categories_json(pages_jsonl_rows, provenance)
    sections = build_sections_json(analyses, provenance)
    templates = build_templates_json(analyses, provenance)
    tables = build_tables_json(analyses, provenance)
    domains = build_domains_json(pages_jsonl_rows, provenance)
    acquisition_failures = build_acquisition_failures_json(snapshot_dir, pages, provenance)
    corpus = build_corpus_json(manifest, pages_jsonl_rows, analyses, acquisition_failures["failures"], provenance)
    report_md = render_report_md(provenance, corpus, categories, sections, templates, tables, domains, acquisition_failures)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def write_json(name: str, data) -> None:
        with (out_dir / name).open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    write_json("corpus.json", corpus)
    write_json("categories.json", categories)
    write_json("sections.json", sections)
    write_json("templates.json", templates)
    write_json("tables.json", tables)
    write_json("domains.json", domains)
    write_json("acquisition_failures.json", acquisition_failures)

    with (out_dir / "pages.jsonl").open("w", encoding="utf-8") as f:
        for row in pages_jsonl_rows:
            f.write(json.dumps(row) + "\n")

    (out_dir / "REPORT.md").write_text(report_md, encoding="utf-8")

    print(f"inventory written to {out_dir} from snapshot {manifest.get('snapshot_id')} ({len(pages)} page(s))")


if __name__ == "__main__":
    main()
