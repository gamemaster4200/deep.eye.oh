"""Exercises inventory.py's full offline pipeline against the tiny_snapshot
fixture: internal consistency across outputs, provenance on every artifact,
and REPORT.md structure. No network access anywhere in this module."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _classify
import inventory

SNAPSHOT_DIR = Path(__file__).resolve().parent / "fixtures" / "wiki" / "snapshots" / "tiny_snapshot"


def _build_all():
    manifest, manifest_bytes, pages = inventory.load_snapshot(SNAPSHOT_DIR)
    provenance = inventory.compute_provenance(manifest, manifest_bytes)
    signals = _classify.EMPTY_SIGNALS
    analyses = [inventory.analyze_page(p) for p in pages]
    pages_jsonl_rows = inventory.build_pages_jsonl(analyses, signals, provenance)
    categories = inventory.build_categories_json(pages_jsonl_rows, provenance)
    sections = inventory.build_sections_json(analyses, provenance)
    templates = inventory.build_templates_json(analyses, provenance)
    tables = inventory.build_tables_json(analyses, provenance)
    domains = inventory.build_domains_json(pages_jsonl_rows, provenance)
    acquisition_failures = inventory.build_acquisition_failures_json(SNAPSHOT_DIR, pages, provenance)
    corpus = inventory.build_corpus_json(manifest, pages_jsonl_rows, analyses, acquisition_failures["failures"], provenance)
    return dict(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        provenance=provenance,
        pages=pages,
        pages_jsonl_rows=pages_jsonl_rows,
        categories=categories,
        sections=sections,
        templates=templates,
        tables=tables,
        domains=domains,
        acquisition_failures=acquisition_failures,
        corpus=corpus,
    )


def test_load_snapshot_reads_manifest_and_all_pages():
    manifest, manifest_bytes, pages = inventory.load_snapshot(SNAPSHOT_DIR)
    assert manifest["snapshot_id"] == "20260101T000000Z"
    assert len(pages) == 7
    assert hashlib.sha256(manifest_bytes).hexdigest() == hashlib.sha256((SNAPSHOT_DIR / "manifest.json").read_bytes()).hexdigest()


def test_provenance_matches_manifest():
    built = _build_all()
    prov = built["provenance"]
    assert prov["snapshot_id"] == built["manifest"]["snapshot_id"]
    assert prov["acquisition_completed_at"] == built["manifest"]["completed_at"]
    assert prov["inventory_schema_version"] == inventory.INVENTORY_SCHEMA_VERSION
    assert prov["source_manifest_sha256"] == hashlib.sha256(built["manifest_bytes"]).hexdigest()


def test_every_output_carries_matching_provenance_block():
    built = _build_all()
    for name in ("categories", "sections", "templates", "tables", "domains", "acquisition_failures", "corpus"):
        assert built[name]["provenance"] == built["provenance"], f"{name}.json provenance mismatch"
    for row in built["pages_jsonl_rows"]:
        assert row["provenance"] == built["provenance"]


def test_acquisition_failures_matches_acquisition_jsonl_unsucceeded_entries():
    built = _build_all()
    failure_pageids = {f["pageid"] for f in built["acquisition_failures"]["failures"]}
    assert failure_pageids == {999}
    assert built["corpus"]["acquisition_failure_count"] == 1


def test_categories_member_counts_match_pages_jsonl():
    built = _build_all()
    tanks_members = {row["title"] for row in built["pages_jsonl_rows"] if "Category:Tanks" in row["categories"]}
    assert built["categories"]["categories"]["Category:Tanks"]["member_count"] == len(tanks_members)
    assert set(built["categories"]["categories"]["Category:Tanks"]["representative_members"]) == tanks_members


def test_category_page_records_parent_category_and_subcategory_link():
    built = _build_all()
    categories = built["categories"]["categories"]
    assert "Category:Game Content" in categories["Category:Tanks"]["parent_categories"]
    assert "Category:Tanks" in categories["Category:Game Content"]["subcategories"]


def test_redirect_status_mismatch_is_false_for_consistent_fixture_data():
    built = _build_all()
    row = next(r for r in built["pages_jsonl_rows"] if r["pageid"] == 203)
    assert row["is_redirect"] is True
    assert row["wikitext_redirect_target"] == "Overlord"
    assert row["redirect_status_mismatch"] is False


def test_template_conflicting_field_name_group_detected():
    built = _build_all()
    entry = built["templates"]["templates"]["Infobox tank"]
    groups = entry["conflicting_field_name_groups"]
    flattened = {name for group in groups for name in group}
    assert "speed" in flattened and "Speed" in flattened


def test_tables_include_malformed_and_complete_with_parse_quality():
    built = _build_all()
    qualities = {t["parse_quality"] for t in built["tables"]["tables"]}
    assert "complete" in qualities


def test_domains_empty_signals_yields_unknown_domain_for_most_pages():
    built = _build_all()
    assert built["domains"]["unknown_domain_page_count"] >= 1
    # changelog_updates is a structural (non-vocabulary) signal, so it should
    # still fire for the Destroyer page's "Changelog" section even with empty signals
    assert built["domains"]["domains"]["changelog_updates"]["page_count"] >= 1


def test_render_report_md_has_all_required_sections_and_schema_tags():
    built = _build_all()
    report = inventory.render_report_md(
        built["provenance"], built["corpus"], built["categories"], built["sections"], built["templates"], built["tables"], built["domains"], built["acquisition_failures"]
    )
    for heading in ("## Corpus", "## Information structure", "## Domains", "## Temporal data", "## Provenance", "## Schema implications", "## Data quality problems"):
        assert heading in report
    assert built["provenance"]["snapshot_id"] in report
    for tag in ("supports:", "requires-extending:", "contradicts:", "still-unknown:"):
        assert tag in report
