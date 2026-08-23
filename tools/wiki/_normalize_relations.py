"""Extracts upgrades_to (tank progression) and variant_of (shape variant)
relation edges from the raw infobox wikitext fields observed on real
diep.io tank/shape pages.

Tank progression evidence: the `{{Infobox ... next=... previous=...}}`
fields use `[[File:icon.png|50px|link=Target]]` icon-link syntax rather
than plain `[[Target]]` wikilinks; a removed upgrade option is marked by an
immediately-following `<ref>...removed...</ref>` footnote (observed on the
sampled Twin page, whose `next` field links Triplet with such a footnote).
`previous` is parsed the same way and used only as an independent
cross-check against the `next`-derived edges (see normalize.py), not as a
second source of edges -- upgrades_to is canonicalized to one direction
only (see relation.schema.json); this module never emits an
`upgrades_from`-shaped edge.

Shape variant evidence: the `{{Polygon ... variants=...}}` field contains
plain `[[Target]]` wikilinks (observed on the sampled Pentagon/Square/
Triangle pages, e.g. Pentagon's `variants` links Alpha Pentagon and Green
Pentagon).

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

import re

_ICON_LINK_RE = re.compile(
    r"\[\[File:[^\]]*?link=([^\|\]]+)\]\]"
    r"(?:\s*<ref[^>]*>(?P<ref_body>.*?)</ref>)?",
    re.DOTALL,
)
_SIMPLE_LINK_RE = re.compile(r"\[\[([^\]\|#]+)")


def extract_icon_links(field_text: str) -> list[dict]:
    """Parses a `next`/`previous` infobox field into
    [{"target": str, "removed": bool, "ref_body": str | None}, ...],
    in source order. `removed` is True only when the immediately-following
    <ref> footnote's body contains the word "removed" (case-insensitive);
    a <ref> present but not matching that word still leaves `removed`
    False but is returned in `ref_body` so callers can treat it as
    ambiguous rather than confidently active if they choose to."""
    if not field_text or not field_text.strip():
        return []
    results = []
    for match in _ICON_LINK_RE.finditer(field_text):
        target = match.group(1).strip()
        ref_body = match.group("ref_body")
        removed = bool(ref_body) and "removed" in ref_body.lower()
        results.append({"target": target, "removed": removed, "ref_body": ref_body})
    return results


def extract_simple_links(field_text: str) -> list[str]:
    """Parses a `variants`-style infobox field (plain `[[Target]]` wikilinks,
    e.g. separated by `<br/>`) into an ordered list of link target titles."""
    if not field_text or not field_text.strip():
        return []
    return [m.group(1).strip() for m in _SIMPLE_LINK_RE.finditer(field_text)]
