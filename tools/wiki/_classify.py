"""Page-type and content-domain classification for the wiki inventory.

Every classification is a (label, evidence) pair, never a bare label.
Classification vocabulary specific to this wiki (category names, template
names) is external data consumed from a `signals` dict — never hardcoded as
Python literals here — normally loaded from
tools/wiki/config/classification_signals.json. Populating that vocabulary is
a deliberate, separate curation step; running discover.py/fetch.py/
inventory.py never writes to it, and it is expected/acceptable for pages to
classify as "unknown"/"unknown_domain" until that curation happens.

`canonical_candidate` (not `canonical`) is used deliberately throughout:
inventory-time classification is heuristic and evidence-backed, never an
authoritative claim of ground truth.

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

CANDIDATE_DOMAINS = [
    "tanks",
    "tank_tiers_classes",
    "upgrade_relationships",
    "shapes_polygons",
    "bosses",
    "weapons",
    "ammunition",
    "stats",
    "mechanics",
    "levels",
    "game_modes",
    "maps_map_features",
    "changelog_updates",
    "builds_strategies",
    "historical_removed_content",
    "event_content",
]

DISCUSSION_NAMESPACE_NAMES = {
    "Talk",
    "User talk",
    "Category talk",
    "Template talk",
    "Forum",
    "Message Wall",
    "Board",
    "Thread",
}

EMPTY_SIGNALS: dict = {
    "config_version": 1,
    "description": (
        "Human-curated classification vocabulary consumed by _classify.py. "
        "Never auto-generated or auto-populated from a discover.py/fetch.py/"
        "inventory.py run -- editing this file is a separate, deliberate "
        "curation step with its own review, not a side effect of running the "
        "tools."
    ),
    "canonical_candidate_categories": [],
    "fanon_categories": [],
    "community_strategy_categories": [],
    "other_games_categories": [],
    "fanon_template_names": [],
    "domain_category_signals": {domain: [] for domain in CANDIDATE_DOMAINS},
    "domain_template_signals": {domain: [] for domain in CANDIDATE_DOMAINS},
    "evidence": [],
}


def load_signals(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _lower_set(values: list[str]) -> set[str]:
    return {v.strip().lower() for v in values}


def classify_page_type(page: dict, signals: dict) -> list[tuple[str, dict]]:
    """`page`: {"namespace_name": str, "categories": list[str],
    "templates": list[str] (template names present on the page)}.

    Structural (namespace-driven) buckets are objective MediaWiki/Fandom
    facts and always take priority; curated-vocabulary buckets
    (fanon/other_games/community_strategy/canonical_candidate) are only
    considered when no structural signal fired, since a Category/Template/
    File/discussion/user page is never also main-namespace content. Always
    returns at least one (label, evidence) pair — "unknown" if nothing else
    matches, never defaulting to "canonical_candidate"."""
    labels: list[tuple[str, dict]] = []
    namespace_name = page.get("namespace_name", "")
    categories_lower = _lower_set(page.get("categories", []))
    template_names_lower = _lower_set(page.get("templates", []))

    if namespace_name == "File":
        labels.append(("files_media", {"signal": "namespace_name", "value": "File"}))
    elif namespace_name == "Template":
        labels.append(("templates", {"signal": "namespace_name", "value": "Template"}))
    elif namespace_name == "Category":
        labels.append(("categories", {"signal": "namespace_name", "value": "Category"}))
    elif namespace_name in DISCUSSION_NAMESPACE_NAMES:
        labels.append(("discussion_forum", {"signal": "namespace_name", "value": namespace_name}))
    elif namespace_name == "User" or namespace_name.startswith("User "):
        labels.append(("user_pages", {"signal": "namespace_name", "value": namespace_name}))

    if not labels:
        for category in signals.get("fanon_categories", []):
            if category.strip().lower() in categories_lower:
                labels.append(("fanon", {"signal": "category_membership", "category": category}))
        for template_name in signals.get("fanon_template_names", []):
            if template_name.strip().lower() in template_names_lower:
                labels.append(("fanon", {"signal": "template", "template": template_name}))
        for category in signals.get("other_games_categories", []):
            if category.strip().lower() in categories_lower:
                labels.append(("other_games", {"signal": "category_membership", "category": category}))
        for category in signals.get("community_strategy_categories", []):
            if category.strip().lower() in categories_lower:
                labels.append(("community_strategy", {"signal": "category_membership", "category": category}))
        for category in signals.get("canonical_candidate_categories", []):
            if category.strip().lower() in categories_lower:
                labels.append(("canonical_candidate", {"signal": "category_membership", "category": category}))

    if not labels:
        labels.append(("unknown", {"signal": "none", "note": "no structural or curated-vocabulary signal matched"}))

    return labels


def classify_domains(page: dict, sections: list[dict], templates: list[dict], signals: dict) -> list[tuple[str, dict]]:
    """Multi-label domain classification. Returns [] when nothing matches —
    callers (inventory.py) are responsible for recording that as
    `unknown_domain: true` rather than forcing a label."""
    labels: list[tuple[str, dict]] = []
    categories_lower = _lower_set(page.get("categories", []))
    template_names_lower = {t["name"].strip().lower() for t in templates}

    domain_category_signals = signals.get("domain_category_signals", {})
    domain_template_signals = signals.get("domain_template_signals", {})

    for domain in CANDIDATE_DOMAINS:
        for category in domain_category_signals.get(domain, []):
            if category.strip().lower() in categories_lower:
                labels.append((domain, {"signal": "category_membership", "category": category}))
        for template_name in domain_template_signals.get(domain, []):
            if template_name.strip().lower() in template_names_lower:
                labels.append((domain, {"signal": "template", "template": template_name}))

    # A small set of corpus-independent structural signals: certain section
    # headings are strong domain hints regardless of curated vocabulary.
    for section in sections:
        heading_lower = section["heading"].strip().lower()
        if "changelog" in heading_lower or "update" in heading_lower:
            labels.append(("changelog_updates", {"signal": "section_heading", "heading": section["heading"]}))
        if "history" in heading_lower or "removed" in heading_lower or "former" in heading_lower:
            labels.append(("historical_removed_content", {"signal": "section_heading", "heading": section["heading"]}))

    return labels
