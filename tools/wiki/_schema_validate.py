"""Loads the knowledge-schema-v0 JSON Schema contracts (knowledge/schema/v0/)
into a jsonschema Draft202012Validator registry, shared between normalize.py
(which must fail visibly on any schema-invalid emitted record) and the test
suite (which validates every generated fixture record against the same
schemas).

Development tooling only — not part of the production pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

_SCHEMA_FILES = (
    "entity.schema.json",
    "provenance.schema.json",
    "tank.schema.json",
    "shape.schema.json",
    "relation.schema.json",
)


def _load(schema_dir: Path, name: str) -> dict:
    return json.loads((schema_dir / name).read_text(encoding="utf-8"))


def build_validators(schema_dir: Path) -> dict[str, Draft202012Validator]:
    """Returns {"entity": Draft202012Validator, "relation": Draft202012Validator}
    for the entity/relation schemas, both able to resolve $ref against the
    other schemas in schema_dir via a shared registry."""
    schemas = {name: _load(schema_dir, name) for name in _SCHEMA_FILES}
    resources = [Resource.from_contents(schema) for schema in schemas.values()]
    registry = Registry().with_resources((r.id(), r) for r in resources)
    return {
        "entity": Draft202012Validator(schemas["entity.schema.json"], registry=registry),
        "relation": Draft202012Validator(schemas["relation.schema.json"], registry=registry),
    }
