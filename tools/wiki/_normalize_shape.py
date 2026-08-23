"""Recognizes and normalizes real Polygon pages (a single top-level
`{{Polygon ... rarity=... hp=... xp=... }}` template, per the sampled
Pentagon/Alpha Pentagon/Square/Triangle pages) into shape_v0 entity
payloads matching knowledge/schema/v0/shape.schema.json.

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

# hp/xp were observed with thousands separators on the real corpus (Alpha
# Pentagon: hp=3,000). Values that still don't parse after stripping commas
# are left null and preserved verbatim in raw_infobox_evidence -- never
# guessed.
_CONSUMED_INFOBOX_FIELDS = {"rarity", "hp", "xp", "attacks", "variants"}


def classify_shape(templates: list[dict]) -> tuple[dict | None, str | None]:
    polygons = [t for t in templates if t["name"] == "Polygon"]
    if len(polygons) != 1:
        return None, f"expected exactly one Polygon template, found {len(polygons)}"
    return polygons[0]["fields"], None


def _parse_number(raw_value: str) -> float | None:
    cleaned = raw_value.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def build_shape_payload(infobox_fields: dict[str, str]) -> dict:
    rarity = infobox_fields.get("rarity", "").strip() or None
    hp_text = infobox_fields.get("hp", "").strip()
    xp_text = infobox_fields.get("xp", "").strip()
    attacks = infobox_fields.get("attacks", "").strip() or None
    hp = _parse_number(hp_text)
    xp = _parse_number(xp_text)

    raw_infobox_evidence = {}
    for field_name, value in infobox_fields.items():
        if field_name in _CONSUMED_INFOBOX_FIELDS:
            continue
        if value.strip():
            raw_infobox_evidence[field_name] = value
    if hp_text and hp is None:
        raw_infobox_evidence["hp"] = hp_text
    if xp_text and xp is None:
        raw_infobox_evidence["xp"] = xp_text

    payload = {}
    if rarity is not None:
        payload["rarity"] = rarity
    if hp is not None:
        payload["hp"] = hp
    if xp is not None:
        payload["xp"] = xp
    if attacks is not None:
        payload["attacks"] = attacks
    if raw_infobox_evidence:
        payload["raw_infobox_evidence"] = raw_infobox_evidence
    return payload
