"""Exercises _wikitext.py's section/template/table/link extraction and
redirect detection against the committed wikitext_samples fixtures."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _wikitext

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "wiki" / "wikitext_samples"


def _read(name):
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_extract_sections_basic():
    sections = _wikitext.extract_sections(_read("sections_basic.wikitext"))
    headings = [s["heading"] for s in sections]
    assert headings == ["Design", "Technical", "Trivia"]
    assert sections[0]["level"] == 2
    assert sections[1]["level"] == 3


def test_extract_templates_infobox_fields():
    templates = _wikitext.extract_templates(_read("tank_infobox.wikitext"))
    assert len(templates) == 1
    assert templates[0]["name"] == "Infobox tank"
    assert templates[0]["fields"] == {"name": "Basic", "hp": "100", "speed": "1"}


def test_extract_templates_conflicting_field_names_preserved_raw():
    templates = _wikitext.extract_templates(_read("shape_infobox_conflicting_fields.wikitext"))
    assert len(templates) == 2
    field_names = {name for t in templates for name in t["fields"]}
    assert field_names == {"health", "Health"}  # raw names preserved, not normalized here


def test_extract_internal_links_excludes_file_and_category():
    links = _wikitext.extract_internal_links(_read("links_sample.wikitext"))
    assert links == ["Basic", "Twin", "Sniper"]


def test_detect_redirect_target():
    assert _wikitext.detect_redirect_target(_read("redirect_page.wikitext")) == "Overlord"
    assert _wikitext.detect_redirect_target(_read("tank_infobox.wikitext")) is None


def test_extract_tables_complete_stats_table():
    tables = _wikitext.extract_tables(_read("table_stats.wikitext"))
    assert len(tables) == 1
    table = tables[0]
    assert table["heading"] == "Stats"
    assert table["columns"] == ["Level", "HP", "Speed"]
    assert table["row_count"] == 2
    assert table["parse_quality"] == "complete"
    assert table["flags"]["levels_like"] is True
    assert table["flags"]["stats_like"] is True
    assert "level" in table["flag_evidence"]["levels_like"]


def test_extract_tables_changelog_table_flagged():
    tables = _wikitext.extract_tables(_read("table_changelog.wikitext"))
    assert len(tables) == 1
    assert tables[0]["flags"]["changelog_like"] is True


def test_extract_tables_malformed_degrades_gracefully_never_raises():
    tables = _wikitext.extract_tables(_read("table_malformed.wikitext"))
    assert len(tables) == 1
    table = tables[0]
    assert table["parse_quality"] in ("partial", "failed")
    # raw source is preserved regardless of parse quality
    assert "{|" in table["raw_wikitext"]
    assert "|}" in table["raw_wikitext"]


def test_extract_tables_on_completely_garbage_input_never_raises():
    garbage = "{|\n|-\n|-\n|-\n" * 50 + "not closed"
    tables = _wikitext.extract_tables(garbage)  # must not raise
    assert isinstance(tables, list)
