"""Recognizes and normalizes real player-tank pages (a single top-level
`{{Infobox ... tier=... id=...}}` template, per the sampled Twin/Tank/
Battleship/Spike pages) into tank_v0 entity payloads matching
knowledge/schema/v0/tank.schema.json.

Recognition is deliberately narrow: a page qualifies only when it has
exactly one `Infobox` template. Dominator (3 `Infobox` templates, one per
tank variant, plus a hedged non-numeric tier "Supposedly 6") is the
concrete real-corpus example this excludes -- see classify_tank() below
and normalize.py's unknown_entity fallback. Real numeric gameplay stats
(the Technical section's prose, e.g. "Health: 50 (+20 for each point...")
are out of scope for this schema slice and are never parsed here --
BaseStats fields are the wiki's own qualitative glyph ratings only.

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

# CamelCase BaseStats field name -> tank.schema.json BaseStatsBlock.stats key.
_STAT_FIELD_MAP = {
    "HealthRegen": "health_regen",
    "MaxHealth": "max_health",
    "BodyDamage": "body_damage",
    "BulletSpeed": "bullet_speed",
    "BulletPenetration": "bullet_penetration",
    "BulletDamage": "bullet_damage",
    "Reload": "reload",
    "MovementSpeed": "movement_speed",
}
_GLYPH_UP = "▲"  # ▲
_GLYPH_DOWN = "▼"  # ▼
_GLYPH_NEUTRAL = "—"  # —

# Infobox fields already surfaced as first-class tank_v0 fields; everything
# else observed on a real infobox (image, next, previous, title, categories,
# key, releasedate -- see the sampled Tank page) goes to raw_infobox_evidence.
_CONSUMED_INFOBOX_FIELDS = {"tier", "id", "barrel"}


def normalize_rating(raw_value: str) -> int | None:
    """Glyph count -> signed magnitude (▲▲ -> 2, ▼ -> -1, — -> 0); None when
    raw_value is not a pure run of one glyph (e.g. "N/A", free text, or an
    unrecognized glyph) -- never guessed."""
    text = raw_value.strip()
    if text == _GLYPH_NEUTRAL:
        return 0
    if text and all(ch == _GLYPH_UP for ch in text):
        return len(text)
    if text and all(ch == _GLYPH_DOWN for ch in text):
        return -len(text)
    return None


def _qualitative_stat(raw_value: str) -> dict:
    return {"raw_value": raw_value, "normalized_rating": normalize_rating(raw_value)}


def _hidden_stats(fields: dict[str, str]) -> list[dict]:
    hidden = []
    for i in range(1, 5):
        label = fields.get(f"HiddenStat{i}", "").strip()
        value = fields.get(f"HiddenStat{i}Val", "").strip()
        if label and value:
            hidden.append({"label": label, "value": _qualitative_stat(value)})
    return hidden


def _base_stats_block(fields: dict[str, str]) -> dict:
    stats = {}
    for wiki_field, schema_key in _STAT_FIELD_MAP.items():
        value = fields.get(wiki_field, "").strip()
        if value:
            stats[schema_key] = _qualitative_stat(value)
    source_class = fields.get("PrevClass", "").strip() or None
    return {"source_class": source_class, "stats": stats, "hidden_stats": _hidden_stats(fields)}


def classify_tank(templates: list[dict]) -> tuple[dict | None, str | None]:
    """Returns (infobox_fields, disqualify_reason). infobox_fields is None
    (and disqualify_reason is set) when the page does not have exactly one
    Infobox template, or the Infobox has no `id` field at all -- both real,
    empirically-observed disqualifiers (see module docstring)."""
    infoboxes = [t for t in templates if t["name"] == "Infobox"]
    if len(infoboxes) != 1:
        return None, f"expected exactly one Infobox template, found {len(infoboxes)}"
    fields = infoboxes[0]["fields"]
    if not fields.get("id", "").strip():
        return None, "Infobox has no id field"
    return fields, None


def build_tank_payload(infobox_fields: dict[str, str], base_stats_templates: list[dict]) -> dict:
    tier_text = infobox_fields.get("tier", "").strip()
    tier = int(tier_text) if tier_text.isdigit() else None

    id_text = infobox_fields.get("id", "").strip()
    source_tank_id = int(id_text) if id_text.lstrip("-").isdigit() else None

    barrel_info = infobox_fields.get("barrel", "").strip() or None

    stat_blocks = [_base_stats_block(t["fields"]) for t in base_stats_templates]
    if not stat_blocks:
        # tank.schema.json requires >= 1 stat_blocks entry; a tank page with
        # no BaseStats template at all still needs a (empty) placeholder
        # block rather than being silently unrepresentable.
        stat_blocks = [{"source_class": None, "stats": {}, "hidden_stats": []}]

    raw_infobox_evidence = {}
    for field_name, value in infobox_fields.items():
        if field_name in _CONSUMED_INFOBOX_FIELDS:
            continue
        if value.strip():
            raw_infobox_evidence[field_name] = value
    if tier_text and tier is None:
        raw_infobox_evidence["tier"] = tier_text
    if id_text and source_tank_id is None:
        raw_infobox_evidence["id"] = id_text

    payload = {"stat_blocks": stat_blocks}
    if tier is not None:
        payload["tier"] = tier
    if source_tank_id is not None:
        payload["source_tank_id"] = source_tank_id
    if barrel_info is not None:
        payload["barrel_info"] = barrel_info
    if raw_infobox_evidence:
        payload["raw_infobox_evidence"] = raw_infobox_evidence
    return payload
