"""Validates the knowledge-schema-v0 JSON Schema contracts (knowledge/schema/v0/)
against small hand-authored example entities/relations grounded in the wikitext
sampled from real diep.io tank/shape pages during the knowledge-schema-v0 design
review. These tests check schema *shape*, not real corpus population -- no
classification_signals.json curation or entity normalization happens here.
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "schema" / "v0"


def _load(name):
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _registry():
    resources = [
        Resource.from_contents(_load(name))
        for name in (
            "entity.schema.json",
            "provenance.schema.json",
            "tank.schema.json",
            "shape.schema.json",
            "relation.schema.json",
        )
    ]
    return Registry().with_resources((r.id(), r) for r in resources)


REGISTRY = _registry()
ENTITY_VALIDATOR = Draft202012Validator(_load("entity.schema.json"), registry=REGISTRY)
RELATION_VALIDATOR = Draft202012Validator(_load("relation.schema.json"), registry=REGISTRY)
PROVENANCE_VALIDATOR = Draft202012Validator(_load("provenance.schema.json"), registry=REGISTRY)


def make_provenance(**overrides):
    prov = {
        "snapshot_id": "20260821T022701Z",
        "pageid": 858,
        "content_sha256": "a" * 64,
    }
    prov.update(overrides)
    return prov


def qual(raw_value, normalized_rating=None):
    return {"raw_value": raw_value, "normalized_rating": normalized_rating}


def make_tank_entity(**overrides):
    entity = {
        "schema_version": "v0",
        "id": "tank:destroyer",
        "type": "tank_v0",
        "name": "Destroyer",
        "aliases": [],
        "entity_status": "active",
        "source_domain": "canonical_candidate",
        "provenance": make_provenance(),
        "payload": {
            "tier": 3,
            "source_tank_id": 10,
            "barrel_info": "[[Cannons|Cannon]](1)",
            "stat_blocks": [
                {
                    "source_class": None,
                    "stats": {
                        "bullet_speed": qual("▼▼▼", -3),
                        "bullet_penetration": qual("▲▲", 2),
                        "bullet_damage": qual("▲▲▲", 3),
                        "reload": qual("▼▼▼", -3),
                    },
                    "hidden_stats": [
                        {"label": "Recoil", "value": qual("▲▲▲", 3)},
                        {"label": "Projectile Size", "value": qual("▲▲", 2)},
                    ],
                }
            ],
            "temporal": {
                "valid_from": None,
                "valid_to": None,
                "added_in": None,
                "removed_in": None,
                "reworked_in": None,
            },
            "raw_infobox_evidence": {},
        },
    }
    entity.update(overrides)
    return entity


def make_shape_entity(**overrides):
    entity = {
        "schema_version": "v0",
        "id": "shape:pentagon",
        "type": "shape_v0",
        "name": "Pentagon",
        "aliases": [],
        "entity_status": "active",
        "source_domain": "canonical_candidate",
        "provenance": make_provenance(pageid=164430),
        "payload": {
            "rarity": "Uncommon<br/>Common in the [[Pentagon Nest]]",
            "hp": 100,
            "xp": 130,
            "attacks": None,
            "raw_infobox_evidence": {},
        },
    }
    entity.update(overrides)
    return entity


def test_valid_tank_entity_accepted():
    ENTITY_VALIDATOR.validate(make_tank_entity())


def test_valid_shape_entity_accepted():
    ENTITY_VALIDATOR.validate(make_shape_entity())


def test_multiple_tank_stat_blocks_accepted():
    entity = make_tank_entity(id="tank:gunner-trapper", name="Gunner Trapper")
    entity["payload"]["stat_blocks"] = [
        {
            "source_class": "Gunner’s Bullets",
            "stats": {"bullet_penetration": qual("+", None)},
            "hidden_stats": [],
        },
        {
            "source_class": "Trapper’s Trap Launcher",
            "stats": {"bullet_penetration": qual("▲", 1), "reload": qual("▼▼", -2)},
            "hidden_stats": [],
        },
    ]
    ENTITY_VALIDATOR.validate(entity)


def test_qualitative_base_stats_accepted():
    entity = make_tank_entity()
    entity["payload"]["stat_blocks"][0]["stats"]["reload"] = qual("N/A", None)
    ENTITY_VALIDATOR.validate(entity)


def test_numeric_base_stats_are_not_accidentally_required():
    """The tank schema must not silently demand a numeric stat value --
    raw_value is a glyph/text string and normalized_rating is nullable."""
    entity = make_tank_entity()
    entity["payload"]["stat_blocks"][0]["stats"] = {
        "health_regen": qual("—"),  # no normalized_rating key at all
    }
    ENTITY_VALIDATOR.validate(entity)

    # A numeric raw_value must be REJECTED -- raw_value models the literal
    # wikitext glyph/text, not a parsed number.
    entity_bad = make_tank_entity()
    entity_bad["payload"]["stat_blocks"][0]["stats"]["reload"] = {
        "raw_value": 3,
        "normalized_rating": 3,
    }
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(entity_bad)


def test_flexible_hidden_stat_list_accepted():
    entity = make_tank_entity()
    entity["payload"]["stat_blocks"][0]["hidden_stats"] = [
        {"label": "Drone Count", "value": qual("▲▲", 2)},
        {"label": "Damage Reduction", "value": qual("▼▼▼", -3)},
        {"label": "Invisibility", "value": qual("+", None)},
    ]
    ENTITY_VALIDATOR.validate(entity)


def test_empty_stat_blocks_rejected():
    entity = make_tank_entity()
    entity["payload"]["stat_blocks"] = []
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(entity)


def test_valid_upgrades_to_relation_accepted():
    relation = {
        "from_id": "tank:machine-gun",
        "to_id": "tank:destroyer",
        "relation_type": "upgrades_to",
        "status": "active",
        "provenance": make_provenance(pageid=706),
    }
    RELATION_VALIDATOR.validate(relation)


def test_valid_variant_of_relation_accepted():
    relation = {
        "from_id": "shape:pentagon",
        "to_id": "shape:alpha-pentagon",
        "relation_type": "variant_of",
        "status": "active",
        "provenance": make_provenance(pageid=164430),
    }
    RELATION_VALIDATOR.validate(relation)


def test_removed_relation_accepted():
    """The sampled Twin/Smasher pages both document upgrade paths explicitly
    marked as removed by the wiki -- the relation schema must represent that."""
    relation = {
        "from_id": "tank:twin",
        "to_id": "tank:triplet",
        "relation_type": "upgrades_to",
        "status": "removed",
        "provenance": make_provenance(pageid=834),
    }
    RELATION_VALIDATOR.validate(relation)


def test_no_upgrades_from_relation_type():
    """Design correction: upgrade relations are canonicalized to upgrades_to
    only -- upgrades_from must not be an accepted relation_type."""
    relation = {
        "from_id": "tank:destroyer",
        "to_id": "tank:machine-gun",
        "relation_type": "upgrades_from",
        "status": "active",
        "provenance": make_provenance(pageid=706),
    }
    with pytest.raises(jsonschema.ValidationError):
        RELATION_VALIDATOR.validate(relation)


def test_malformed_provenance_rejected():
    bad_provenance_cases = [
        make_provenance(content_sha256="not-a-valid-sha"),
        {k: v for k, v in make_provenance().items() if k != "pageid"},  # missing required field
        make_provenance(pageid="858"),  # wrong type
    ]
    for bad in bad_provenance_cases:
        with pytest.raises(jsonschema.ValidationError):
            PROVENANCE_VALIDATOR.validate(bad)

    entity = make_tank_entity(provenance=make_provenance(content_sha256="not-a-valid-sha"))
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(entity)


def test_provenance_does_not_require_character_offsets_or_cell_coordinates():
    """Minimum provenance is snapshot_id/pageid/content_sha256 only; optional
    fields are template_occurrence_index/table_index/section -- no offset or
    cell-coordinate fields exist or are required."""
    PROVENANCE_VALIDATOR.validate(make_provenance())
    PROVENANCE_VALIDATOR.validate(
        make_provenance(template_occurrence_index=0, table_index=2, section="Technical")
    )


def test_dangling_relation_ids_are_not_a_schema_concern():
    """JSON Schema validates document shape, not cross-document referential
    integrity. A relation pointing at entity ids that don't exist anywhere
    must still validate as a well-formed relation -- graph consistency is
    explicitly out of scope for this schema slice (see relation.schema.json's
    description) and must not be falsely implemented as a schema constraint."""
    relation = {
        "from_id": "tank:does-not-exist-1",
        "to_id": "tank:does-not-exist-2",
        "relation_type": "upgrades_to",
        "status": "unknown",
        "provenance": make_provenance(pageid=999999),
    }
    RELATION_VALIDATOR.validate(relation)


def test_dominator_like_nonstandard_source_uses_unknown_entity():
    """The sampled Dominator page carries three separate {{Infobox}} blocks
    (Destroyer/Gunner/Trapper Dominator variants) on one page, a non-numeric
    tier ('Supposedly 6'), and a previous field that is prose rather than an
    upgrade-source link -- it should be represented as unknown_entity, not
    forced into tank_v0."""
    entity = {
        "schema_version": "v0",
        "id": "unknown:dominator",
        "type": "unknown_entity",
        "name": "Dominator",
        "aliases": [],
        "entity_status": "active",
        "source_domain": "canonical_candidate",
        "provenance": make_provenance(pageid=2261),
        "payload": {
            "raw_evidence": {
                "infobox_1_title": "Destroyer Dominator",
                "infobox_1_tier": "Supposedly 6",
                "infobox_2_title": "Gunner Dominator",
                "infobox_3_title": "Trapper Dominator",
            },
            "reason": (
                "Multiple infobox blocks per page (boss/capture entity); "
                "tier is non-numeric/hedged; does not fit tank_v0's single-tank assumptions."
            ),
        },
    }
    ENTITY_VALIDATOR.validate(entity)

    # Forcing the same source data into tank_v0 with a non-numeric tier must fail.
    forced = make_tank_entity(id="tank:dominator", name="Dominator")
    forced["payload"]["tier"] = "Supposedly 6"
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(forced)


def test_tank_payload_rejected_on_shape_entity_and_vice_versa():
    """The discriminated payload must actually discriminate: a shape_v0
    entity carrying a tank-shaped payload (or vice versa) must be rejected."""
    entity = make_shape_entity()
    entity["payload"] = make_tank_entity()["payload"]
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(entity)

    entity2 = make_tank_entity()
    entity2["payload"] = make_shape_entity()["payload"]
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(entity2)


def test_source_domain_canonical_candidate_is_not_the_only_valid_value():
    """source_domain must support at least canonical_candidate/fanon/community/
    unknown -- canonical_candidate must never be treated as the sole/implicit
    'canonical truth' value."""
    for domain in ("canonical_candidate", "fanon", "community", "unknown"):
        entity = make_shape_entity(source_domain=domain)
        ENTITY_VALIDATOR.validate(entity)

    bad = make_shape_entity(source_domain="canonical")  # not an accepted value
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(bad)


def test_entity_envelope_rejects_unknown_top_level_fields():
    entity = make_shape_entity()
    entity["properties_bag"] = {"anything": "goes"}
    with pytest.raises(jsonschema.ValidationError):
        ENTITY_VALIDATOR.validate(entity)


def test_deep_copy_of_valid_examples_still_round_trips_through_json():
    """Sanity check that the hand-authored examples are plain JSON-compatible
    data (as real inventory-derived entities would be), not Python-only objects."""
    for entity in (make_tank_entity(), make_shape_entity()):
        rehydrated = json.loads(json.dumps(copy.deepcopy(entity)))
        ENTITY_VALIDATOR.validate(rehydrated)
