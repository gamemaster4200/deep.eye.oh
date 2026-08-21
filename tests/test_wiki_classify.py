"""Exercises _classify.py's page-type and domain classification against a
populated test-only signals fixture, and confirms the real (empty) shipped
signals config degrades honestly to unknown/unknown_domain."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _classify

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "wiki"
REAL_CONFIG = Path(__file__).resolve().parents[1] / "tools" / "wiki" / "config" / "classification_signals.json"


def _signals():
    return json.loads((FIXTURES / "classification_signals_sample.json").read_text(encoding="utf-8"))


def test_structural_buckets_from_namespace_name():
    signals = _signals()
    cases = {
        "File": "files_media",
        "Template": "templates",
        "Category": "categories",
        "Talk": "discussion_forum",
        "User talk": "discussion_forum",
        "Message Wall": "discussion_forum",
        "User": "user_pages",
    }
    for namespace_name, expected_label in cases.items():
        labels = _classify.classify_page_type({"namespace_name": namespace_name, "categories": [], "templates": []}, signals)
        assert labels[0][0] == expected_label
        assert labels[0][1]["signal"] == "namespace_name"


def test_canonical_candidate_never_named_canonical():
    signals = _signals()
    labels = _classify.classify_page_type({"namespace_name": "(Main)", "categories": ["Category:Tanks"], "templates": []}, signals)
    label_names = [l for l, _ in labels]
    assert "canonical_candidate" in label_names
    assert "canonical" not in label_names
    evidence = dict(labels)["canonical_candidate"]
    assert evidence == {"signal": "category_membership", "category": "Category:Tanks"}


def test_fanon_from_category_and_template():
    signals = _signals()
    by_category = _classify.classify_page_type({"namespace_name": "(Main)", "categories": ["Category:Fan Ideas"], "templates": []}, signals)
    assert by_category[0][0] == "fanon"

    by_template = _classify.classify_page_type({"namespace_name": "(Main)", "categories": [], "templates": ["Fanon"]}, signals)
    assert by_template[0][0] == "fanon"


def test_other_games_and_community_strategy_from_category():
    signals = _signals()
    other_games = _classify.classify_page_type({"namespace_name": "(Main)", "categories": ["Category:Other Games"], "templates": []}, signals)
    assert other_games[0][0] == "other_games"

    community = _classify.classify_page_type({"namespace_name": "(Main)", "categories": ["Category:Strategy"], "templates": []}, signals)
    assert community[0][0] == "community_strategy"


def test_unknown_when_no_signal_matches():
    signals = _signals()
    labels = _classify.classify_page_type({"namespace_name": "(Main)", "categories": ["Category:Unrelated Stuff"], "templates": []}, signals)
    assert labels == [("unknown", {"signal": "none", "note": "no structural or curated-vocabulary signal matched"})]


def test_empty_signals_config_yields_unknown_not_default_canonical():
    empty = _classify.EMPTY_SIGNALS
    labels = _classify.classify_page_type({"namespace_name": "(Main)", "categories": ["Category:Tanks"], "templates": []}, empty)
    assert labels == [("unknown", {"signal": "none", "note": "no structural or curated-vocabulary signal matched"})]


def test_real_shipped_config_is_empty_and_produces_unknown():
    real_signals = _classify.load_signals(REAL_CONFIG)
    assert real_signals["canonical_candidate_categories"] == []
    assert real_signals["fanon_categories"] == []
    assert all(v == [] for v in real_signals["domain_category_signals"].values())
    labels = _classify.classify_page_type({"namespace_name": "(Main)", "categories": ["Category:Tanks"], "templates": []}, real_signals)
    assert labels[0][0] == "unknown"


def test_domain_multi_label_from_category_and_template():
    signals = _signals()
    page = {"categories": ["Category:Tanks", "Category:Bosses"]}
    templates = [{"name": "Infobox tank", "fields": {}}]
    labels = _classify.classify_domains(page, sections=[], templates=templates, signals=signals)
    label_names = {l for l, _ in labels}
    assert "tanks" in label_names
    assert "bosses" in label_names


def test_domain_zero_signal_is_empty_list_not_forced_label():
    signals = _signals()
    page = {"categories": []}
    labels = _classify.classify_domains(page, sections=[], templates=[], signals=signals)
    assert labels == []


def test_domain_structural_section_heading_signal_independent_of_vocabulary():
    labels = _classify.classify_domains(
        {"categories": []}, sections=[{"heading": "Changelog", "level": 2, "index": 0}], templates=[], signals=_classify.EMPTY_SIGNALS
    )
    label_names = {l for l, _ in labels}
    assert "changelog_updates" in label_names
