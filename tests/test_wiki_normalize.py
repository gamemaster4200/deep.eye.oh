"""Exercises normalize.py's real tank_v0/shape_v0/unknown_entity/relation
extraction, offline, against small hand-built template lists (mirroring
_wikitext.extract_templates()'s output shape) grounded in the real diep.io
wikitext sampled during the knowledge-schema-v0/knowledge-normalization-v0
review (Twin/Tank/Battleship/Dominator/Pentagon -- see docs/handoffs),
plus one small synthetic on-disk snapshot for full run() pipeline coverage
(redirect resolution, multi-parent upgrades, deterministic output).

Every fixture entity/relation this module builds is validated against the
real, merged knowledge/schema/v0/ contracts -- not a copy of them.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "wiki"))

import _normalize_relations
import _normalize_shape
import _normalize_tank
import _schema_validate
import normalize

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "schema" / "v0"
VALIDATORS = _schema_validate.build_validators(SCHEMA_DIR)


def assert_valid_entity(entity):
    errors = list(VALIDATORS["entity"].iter_errors(entity))
    assert errors == [], f"schema-invalid entity: {[e.message for e in errors]}"


def assert_valid_relation(relation):
    errors = list(VALIDATORS["relation"].iter_errors(relation))
    assert errors == [], f"schema-invalid relation: {[e.message for e in errors]}"


def tpl(name, **fields):
    return {"name": name, "fields": fields}


def make_provenance(pageid=1):
    return {"snapshot_id": "test-snapshot", "pageid": pageid, "content_sha256": "a" * 64}


def wrap_entity(entity_id, entity_type, name, payload, entity_status="active"):
    entity = normalize.build_entity(entity_id, entity_type, name, [], entity_status, payload, make_provenance())
    assert_valid_entity(entity)
    return entity


# ---------------------------------------------------------------------------
# Tank recognition and normalization
# ---------------------------------------------------------------------------

def test_normal_tank_extraction():
    # Grounded in the real Twin page: {{Infobox tier=2 id=1 barrel=...}} +
    # one {{BaseStats}} block with a hidden stat.
    templates = [
        tpl("Infobox", tier="2", id="1", barrel="[[Cannons]] (2)", next="", previous="[[File:x.png|link=Tank]]"),
        tpl(
            "BaseStats",
            HealthRegen="—", MaxHealth="—", BodyDamage="—",
            BulletSpeed="—", BulletPenetration="▼", BulletDamage="▼",
            Reload="—", MovementSpeed="—",
            HiddenStat1="Recoil", HiddenStat1Val="▼",
            HiddenStat2="", HiddenStat2Val="",
        ),
    ]
    fields, reason = _normalize_tank.classify_tank(templates)
    assert reason is None
    payload = _normalize_tank.build_tank_payload(fields, [templates[1]])
    entity = wrap_entity("tank_v0:1", "tank_v0", "Twin", payload)

    assert entity["payload"]["tier"] == 2
    assert entity["payload"]["source_tank_id"] == 1
    assert entity["payload"]["barrel_info"] == "[[Cannons]] (2)"
    assert len(entity["payload"]["stat_blocks"]) == 1
    stats = entity["payload"]["stat_blocks"][0]["stats"]
    assert stats["bullet_penetration"] == {"raw_value": "▼", "normalized_rating": -1}
    assert stats["health_regen"] == {"raw_value": "—", "normalized_rating": 0}
    assert entity["payload"]["stat_blocks"][0]["hidden_stats"] == [
        {"label": "Recoil", "value": {"raw_value": "▼", "normalized_rating": -1}}
    ]


def test_root_tank_has_no_previous_field():
    # Grounded in the real Tank (root) page: id=0, no `previous` field at all.
    templates = [tpl("Infobox", tier="1", id="0", barrel="[[Cannons]] (1)", next="")]
    fields, reason = _normalize_tank.classify_tank(templates)
    assert reason is None
    payload = _normalize_tank.build_tank_payload(fields, [])
    entity = wrap_entity("tank_v0:2", "tank_v0", "Tank", payload)
    assert entity["payload"]["source_tank_id"] == 0
    # No BaseStats template at all still needs >=1 stat_blocks entry.
    assert entity["payload"]["stat_blocks"] == [{"source_class": None, "stats": {}, "hidden_stats": []}]


def test_multiple_basestats_blocks_with_source_class():
    # Grounded in the real Battleship page: two BaseStats blocks, PrevClass
    # "Overseer" and "Twin Flank" respectively.
    templates = [
        tpl("Infobox", tier="4", id="48", barrel="[[Spawners]] (4)"),
        tpl("BaseStats", PrevClass="Overseer", BulletDamage="▼▼"),
        tpl("BaseStats", PrevClass="Twin Flank", BulletDamage="▼▼▼"),
    ]
    fields, reason = _normalize_tank.classify_tank(templates)
    basestats = [t for t in templates if t["name"] == "BaseStats"]
    payload = _normalize_tank.build_tank_payload(fields, basestats)
    entity = wrap_entity("tank_v0:3", "tank_v0", "Battleship", payload)
    blocks = entity["payload"]["stat_blocks"]
    assert len(blocks) == 2
    assert [b["source_class"] for b in blocks] == ["Overseer", "Twin Flank"]
    assert blocks[0]["stats"]["bullet_damage"]["raw_value"] == "▼▼"
    assert blocks[1]["stats"]["bullet_damage"]["raw_value"] == "▼▼▼"


def test_hidden_stats_flexible_labels():
    templates = [tpl("Infobox", tier="3", id="10")]
    fields, _ = _normalize_tank.classify_tank(templates)
    basestats = [tpl("BaseStats", HiddenStat1="Drone Count", HiddenStat1Val="▲▲", HiddenStat3="Invisibility", HiddenStat3Val="—")]
    payload = _normalize_tank.build_tank_payload(fields, basestats)
    entity = wrap_entity("tank_v0:4", "tank_v0", "SomeDrone", payload)
    hidden = entity["payload"]["stat_blocks"][0]["hidden_stats"]
    assert {h["label"] for h in hidden} == {"Drone Count", "Invisibility"}


def test_qualitative_basestats_are_not_numeric():
    assert _normalize_tank.normalize_rating("N/A") is None
    assert _normalize_tank.normalize_rating("▲▲▲") == 3
    assert _normalize_tank.normalize_rating("▼") == -1
    assert _normalize_tank.normalize_rating("—") == 0
    # A raw_value that never parses to a number must still be representable
    # (raw_value is required; normalized_rating is nullable, never required).
    templates = [tpl("Infobox", tier="1", id="1")]
    fields, _ = _normalize_tank.classify_tank(templates)
    basestats = [tpl("BaseStats", BulletDamage="N/A")]
    payload = _normalize_tank.build_tank_payload(fields, basestats)
    entity = wrap_entity("tank_v0:5", "tank_v0", "Weird", payload)
    assert entity["payload"]["stat_blocks"][0]["stats"]["bullet_damage"] == {
        "raw_value": "N/A", "normalized_rating": None,
    }


def test_dominator_like_multi_infobox_is_not_tank_v0():
    # Grounded in the real Dominator page: 3 {{Infobox}} templates (one per
    # variant) plus a hedged non-numeric tier ("Supposedly 6").
    templates = [
        tpl("Infobox", title="Destroyer Dominator", tier="Supposedly 6", id="45"),
        tpl("Infobox", title="Gunner Dominator", tier="Supposedly 6", id="45"),
        tpl("Infobox", title="Overlord Dominator", tier="Supposedly 6", id="45"),
    ]
    fields, reason = _normalize_tank.classify_tank(templates)
    assert fields is None
    assert "3" in reason
    unknown = wrap_entity(
        "unknown_entity:99", "unknown_entity", "Dominator",
        {"reason": reason, "raw_evidence": {}},
        entity_status="unknown",
    )
    assert unknown["type"] == "unknown_entity"


def test_malformed_infobox_missing_id_is_ambiguous_not_tank_v0():
    templates = [tpl("Infobox", tier="2")]
    fields, reason = _normalize_tank.classify_tank(templates)
    assert fields is None
    assert "id" in reason


# ---------------------------------------------------------------------------
# Shape recognition and normalization
# ---------------------------------------------------------------------------

def test_normal_shape_extraction():
    # Grounded in the real Pentagon page.
    templates = [tpl("Polygon", rarity="Uncommon<br/>Common in the [[Pentagon Nest]]", hp="100", xp="130", variants="[[Alpha Pentagon]]<br/>[[Green Pentagon]]")]
    fields, reason = _normalize_shape.classify_shape(templates)
    assert reason is None
    payload = _normalize_shape.build_shape_payload(fields)
    entity = wrap_entity("shape_v0:6", "shape_v0", "Pentagon", payload)
    assert entity["payload"]["hp"] == 100
    assert entity["payload"]["xp"] == 130
    assert "Uncommon" in entity["payload"]["rarity"]


def test_shape_hp_with_thousands_separator():
    # Grounded in the real Alpha Pentagon page: hp=3,000.
    templates = [tpl("Polygon", hp="3,000", xp="3,000")]
    fields, _ = _normalize_shape.classify_shape(templates)
    payload = _normalize_shape.build_shape_payload(fields)
    entity = wrap_entity("shape_v0:7", "shape_v0", "Alpha Pentagon", payload)
    assert entity["payload"]["hp"] == 3000
    assert entity["payload"]["xp"] == 3000


def test_shape_missing_fields_are_not_invented():
    templates = [tpl("Polygon", hp="10")]
    fields, _ = _normalize_shape.classify_shape(templates)
    payload = _normalize_shape.build_shape_payload(fields)
    entity = wrap_entity("shape_v0:8", "shape_v0", "Square", payload)
    assert "xp" not in entity["payload"]
    assert "rarity" not in entity["payload"]
    assert "attacks" not in entity["payload"]


def test_ambiguous_polygon_source_is_not_shape_v0():
    templates = [tpl("Polygon", hp="1"), tpl("Polygon", hp="2")]
    fields, reason = _normalize_shape.classify_shape(templates)
    assert fields is None
    assert "2" in reason


# ---------------------------------------------------------------------------
# Relation extraction
# ---------------------------------------------------------------------------

def test_variant_relation_links():
    links = _normalize_relations.extract_simple_links("[[Alpha Pentagon]]<br/>[[Green Pentagon]]")
    assert links == ["Alpha Pentagon", "Green Pentagon"]


def test_icon_link_removed_upgrade():
    # Grounded in the real Twin page's `next` field.
    field = (
        "[[File:Triple shot icon new.png|50px|link=Triple Shot]]"
        "[[File:TripletNAV0.png|50px|link=Triplet]]"
        '<ref name="twin1">Removed Upgrade Option. Removed tanks and removed upgrade options are shown by a gray icon background.</ref>'
    )
    links = _normalize_relations.extract_icon_links(field)
    assert [link["target"] for link in links] == ["Triple Shot", "Triplet"]
    assert links[0]["removed"] is False
    assert links[1]["removed"] is True


def test_multi_parent_upgrade_edges():
    tank_working = [
        {"pageid": 1, "title": "Basic", "entity_id": "tank_v0:1", "content_sha256": "a" * 64, "template_index": 0,
         "next_field": "[[File:x.png|link=Sniper]]", "previous_field": ""},
        {"pageid": 2, "title": "Flank Guard", "entity_id": "tank_v0:2", "content_sha256": "b" * 64, "template_index": 0,
         "next_field": "[[File:x.png|link=Sniper]]", "previous_field": ""},
        {"pageid": 3, "title": "Sniper", "entity_id": "tank_v0:3", "content_sha256": "c" * 64, "template_index": 0,
         "next_field": "", "previous_field": ""},
    ]
    tank_ids = {"tank_v0:1", "tank_v0:2", "tank_v0:3"}
    pages_index = {
        "Basic": {"pageid": 1, "namespace_id": 0, "is_redirect": False, "redirect_target_title": None},
        "Flank Guard": {"pageid": 2, "namespace_id": 0, "is_redirect": False, "redirect_target_title": None},
        "Sniper": {"pageid": 3, "namespace_id": 0, "is_redirect": False, "redirect_target_title": None},
    }
    title_to_id = {"Basic": "tank_v0:1", "Flank Guard": "tank_v0:2", "Sniper": "tank_v0:3"}
    diagnostics = normalize.Diagnostics()
    edges = normalize.extract_upgrade_edges(tank_working, tank_ids, pages_index, title_to_id, "snap", diagnostics)
    for edge in edges:
        assert_valid_relation(edge)
    parents = sorted(e["from_id"] for e in edges if e["to_id"] == "tank_v0:3")
    assert parents == ["tank_v0:1", "tank_v0:2"]
    assert diagnostics.upgrade_active == 2


def test_removed_relation_status():
    tank_working = [
        {"pageid": 1, "title": "Twin", "entity_id": "tank_v0:1", "content_sha256": "a" * 64, "template_index": 0,
         "next_field": '[[File:x.png|link=Triplet]]<ref>Removed Upgrade Option.</ref>', "previous_field": ""},
        {"pageid": 2, "title": "Triplet", "entity_id": "tank_v0:2", "content_sha256": "b" * 64, "template_index": 0,
         "next_field": "", "previous_field": ""},
    ]
    tank_ids = {"tank_v0:1", "tank_v0:2"}
    pages_index = {
        "Twin": {"pageid": 1, "namespace_id": 0, "is_redirect": False, "redirect_target_title": None},
        "Triplet": {"pageid": 2, "namespace_id": 0, "is_redirect": False, "redirect_target_title": None},
    }
    title_to_id = {"Twin": "tank_v0:1", "Triplet": "tank_v0:2"}
    diagnostics = normalize.Diagnostics()
    edges = normalize.extract_upgrade_edges(tank_working, tank_ids, pages_index, title_to_id, "snap", diagnostics)
    assert len(edges) == 1
    assert edges[0]["status"] == "removed"
    assert edges[0]["relation_type"] == "upgrades_to"
    assert_valid_relation(edges[0])
    # No reverse upgrades_from edge type is ever emitted.
    assert all(e["relation_type"] != "upgrades_from" for e in edges)


def test_relation_title_normalization_underscore_fragment_and_case():
    # Grounded in the real Gunner/Destroyer pages: "Auto_5#History" and
    # lowercase "firework" for the page actually titled "Firework".
    assert normalize.normalize_wiki_title("Auto_5#History") == "Auto 5"
    assert normalize.normalize_wiki_title("firework") == "Firework"


def test_redirect_resolution_in_upgrade_target():
    pages_index = {
        "Basic Tank": {"pageid": 9, "namespace_id": 0, "is_redirect": True, "redirect_target_title": "Basic"},
        "Basic": {"pageid": 1, "namespace_id": 0, "is_redirect": False, "redirect_target_title": None},
    }
    assert normalize.resolve_title("Basic Tank", pages_index) == "Basic"
    assert normalize.resolve_title("Basic", pages_index) == "Basic"


# ---------------------------------------------------------------------------
# Full pipeline: redirects, multi-parent, determinism (synthetic on-disk snapshot)
# ---------------------------------------------------------------------------

def _page(pageid, title, wikitext, is_redirect=False, redirect_target_title=None, redirect_target_pageid=None, content_sha256=None):
    return {
        "format_version": 1, "pageid": pageid, "title": title, "namespace_id": 0, "namespace_name": "(Main)",
        "canonical_url": f"https://example.fandom.com/wiki/{title}", "is_redirect": is_redirect,
        "redirect_target_title": redirect_target_title, "redirect_target_pageid": redirect_target_pageid,
        "categories": [],
        "revision": {"revid": 1, "parentid": None, "timestamp": "2026-01-01T00:00:00Z", "contentmodel": "wikitext", "contentformat": "text/x-wiki"},
        "wikitext": wikitext,
        "content_sha256": content_sha256 or f"{pageid:04x}" + "0" * 60,
        "retrieved_at": "2026-01-01T00:00:00Z",
        "source_query": {"endpoint": "https://example.fandom.com/api.php", "params_digest": "fixture"},
    }


def _build_synthetic_snapshot(root: Path) -> tuple[Path, Path]:
    snapshot_dir = root / "raw" / "test-snapshot"
    inventory_dir = root / "inventory"
    (snapshot_dir / "pages").mkdir(parents=True)
    inventory_dir.mkdir(parents=True)

    pages = [
        _page(1, "Basic", "{{Infobox\n|tier=1\n|id=0\n|barrel=[[Cannons]] (1)\n|next=[[File:x.png|50px|link=Twin]][[File:y.png|50px|link=Sniper]]\n}}"),
        _page(
            2, "Twin",
            "{{Infobox\n|tier=2\n|id=1\n|barrel=[[Cannons]] (2)\n"
            "|previous=[[File:x.png|50px|link=Basic Tank]]\n"
            "|next=[[File:x.png|50px|link=Twin Flank]]<ref>Removed Upgrade Option.</ref>[[File:y.png|50px|link=Machine Gun]]\n}}\n"
            "{{BaseStats\n|HiddenStat1=Recoil\n|HiddenStat1Val=▼\n}}",
        ),
        _page(3, "Basic Tank", "#REDIRECT [[Basic]]", is_redirect=True, redirect_target_title="Basic", redirect_target_pageid=1),
        _page(4, "Twin Flank", "{{Infobox\n|tier=3\n|id=2\n}}"),
        _page(5, "Machine Gun", "{{Infobox\n|tier=3\n|id=3\n}}"),
        _page(6, "Sniper", "{{Infobox\n|tier=2\n|id=4\n|previous=[[File:x.png|50px|link=Basic]][[File:y.png|50px|link=Flank Guard]]\n}}"),
        _page(7, "Flank Guard", "{{Infobox\n|tier=2\n|id=5\n|next=[[File:x.png|50px|link=Sniper]]\n}}"),
        _page(
            8, "Weird Tank",
            "{{Infobox\n|title=Variant A\n|tier=6\n|id=50\n}}\n{{Infobox\n|title=Variant B\n|tier=6\n|id=50\n}}",
        ),
        _page(9, "TestPentagon", "{{Polygon\n|rarity=Common\n|hp=100\n|xp=130\n|variants=[[TestAlphaPentagon]]\n}}"),
        _page(10, "TestAlphaPentagon", "{{Polygon\n|hp=3000\n|xp=3000\n}}"),
        _page(11, "Levels", "Some prose about leveling up. No infobox here."),
        _page(12, "TestBoss", "{{Boss\n|hp=100000\n}}"),
    ]
    for page in pages:
        (snapshot_dir / "pages" / f"{page['pageid']}.json").write_text(json.dumps(page), encoding="utf-8")
    (snapshot_dir / "manifest.json").write_text(json.dumps({"snapshot_id": "test-snapshot"}), encoding="utf-8")

    with (inventory_dir / "pages.jsonl").open("w", encoding="utf-8") as f:
        for page in pages:
            f.write(json.dumps({
                "title": page["title"], "pageid": page["pageid"], "namespace_id": page["namespace_id"],
                "is_redirect": page["is_redirect"], "redirect_target_title": page["redirect_target_title"],
            }) + "\n")

    return snapshot_dir, inventory_dir


def test_full_pipeline_redirect_multiparent_and_counts(tmp_path):
    snapshot_dir, inventory_dir = _build_synthetic_snapshot(tmp_path)
    entities_out = tmp_path / "entities"
    relations_out = tmp_path / "relations"

    diagnostics = normalize.run(snapshot_dir, inventory_dir, entities_out, relations_out, SCHEMA_DIR)

    # Basic Tank (redirect) is excluded from the candidate pool.
    assert diagnostics.candidate_count == 11
    assert diagnostics.tank_count == 6  # Basic, Twin, Twin Flank, Machine Gun, Sniper, Flank Guard
    assert diagnostics.shape_count == 2
    assert diagnostics.unknown_count == 2  # Weird Tank, TestBoss
    assert diagnostics.ambiguous_count == 1  # Weird Tank
    assert diagnostics.skipped_non_entity_count == 1  # Levels

    tanks = [json.loads(line) for line in (entities_out / "tanks.jsonl").read_text(encoding="utf-8").splitlines()]
    twin = next(t for t in tanks if t["name"] == "Twin")
    assert twin["aliases"] == []
    basic = next(t for t in tanks if t["name"] == "Basic")
    assert basic["aliases"] == ["Basic Tank"], "the redirect must become an alias, not its own entity"

    relations = [json.loads(line) for line in (relations_out / "relations.jsonl").read_text(encoding="utf-8").splitlines()]
    sniper_id = next(t for t in tanks if t["name"] == "Sniper")["id"]
    parents = sorted(r["from_id"] for r in relations if r["to_id"] == sniper_id)
    assert len(parents) == 2, "Sniper must have two upgrades_to parents (multi-parent upgrade)"

    twin_flank_id = next(t for t in tanks if t["name"] == "Twin Flank")["id"]
    twin_id = twin["id"]
    removed = [r for r in relations if r["from_id"] == twin_id and r["to_id"] == twin_flank_id]
    assert len(removed) == 1 and removed[0]["status"] == "removed"

    variant_edges = [r for r in relations if r["relation_type"] == "variant_of"]
    assert len(variant_edges) == 1

    for entity in tanks:
        assert_valid_entity(entity)
    for relation in relations:
        assert_valid_relation(relation)


def test_deterministic_output(tmp_path):
    snapshot_dir, inventory_dir = _build_synthetic_snapshot(tmp_path / "run1src")
    out1 = tmp_path / "run1"
    normalize.run(snapshot_dir, inventory_dir, out1 / "entities", out1 / "relations", SCHEMA_DIR)

    snapshot_dir2, inventory_dir2 = _build_synthetic_snapshot(tmp_path / "run2src")
    out2 = tmp_path / "run2"
    normalize.run(snapshot_dir2, inventory_dir2, out2 / "entities", out2 / "relations", SCHEMA_DIR)

    for name in ("tanks.jsonl", "shapes.jsonl", "unknown.jsonl"):
        assert (out1 / "entities" / name).read_text(encoding="utf-8") == (out2 / "entities" / name).read_text(encoding="utf-8")
    assert (out1 / "relations" / "relations.jsonl").read_text(encoding="utf-8") == (out2 / "relations" / "relations.jsonl").read_text(encoding="utf-8")


def test_schema_invalid_record_aborts_the_run(tmp_path, monkeypatch):
    snapshot_dir, inventory_dir = _build_synthetic_snapshot(tmp_path)

    def poison(fields, base_stats_templates):
        payload = original_build(fields, base_stats_templates)
        payload["tier"] = "not-an-integer"  # violates tank.schema.json's tier: [integer, null]
        return payload

    original_build = _normalize_tank.build_tank_payload
    monkeypatch.setattr(_normalize_tank, "build_tank_payload", poison)
    with pytest.raises(SystemExit):
        normalize.run(snapshot_dir, inventory_dir, tmp_path / "entities", tmp_path / "relations", SCHEMA_DIR)
