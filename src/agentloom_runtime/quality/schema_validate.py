"""JSON Schema validation for AgentLoom knowledge graph files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from agentloom_runtime.kg.paths import get_kg_dir

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"


@dataclass
class SchemaTarget:
    name: str
    kg_file: Path
    schema_file: Path


def default_schema_targets(kg_dir: Path | None = None) -> list[SchemaTarget]:
    kg_dir = kg_dir or get_kg_dir()
    return [
        SchemaTarget("builder-skills", kg_dir / "builder-skills-graph.json", SCHEMAS_DIR / "skills-graph.schema.json"),
        SchemaTarget("builder-knowledge", kg_dir / "builder-knowledge-graph.json", SCHEMAS_DIR / "knowledge-graph.schema.json"),
        SchemaTarget("builder-behaviors", kg_dir / "builder-behaviors-graph.json", SCHEMAS_DIR / "behaviors-graph.schema.json"),
        SchemaTarget("domain-skills", kg_dir / "domain-skills-graph.json", SCHEMAS_DIR / "skills-graph.schema.json"),
        SchemaTarget("domain-behaviors", kg_dir / "domain-behaviors.json", SCHEMAS_DIR / "behaviors-graph.schema.json"),
        SchemaTarget("domain-docs", kg_dir / "domain-docs-graph.json", SCHEMAS_DIR / "knowledge-graph.schema.json"),
        SchemaTarget("master", kg_dir / "master-graph.json", SCHEMAS_DIR / "master-graph.schema.json"),
    ]


def validate_kg_schema(target: SchemaTarget) -> tuple[bool, list[str]]:
    if not target.kg_file.is_file():
        return False, [f"KG file missing: {target.kg_file}"]
    if not target.schema_file.is_file():
        return False, [f"Schema file missing: {target.schema_file}"]
    try:
        kg = json.loads(target.kg_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, [f"JSON parse error: {exc}"]
    try:
        schema = json.loads(target.schema_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, [f"Schema parse error: {exc}"]

    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(kg), key=lambda item: item.path)
    if not errors:
        return True, []
    messages: list[str] = []
    for err in errors:
        path = "/".join(str(part) for part in err.path) or "(root)"
        messages.append(f"  at {path}: {err.message}")
    return False, messages


def validate_all_schemas(
    *,
    only: list[str] | None = None,
    kg_dir: Path | None = None,
) -> tuple[int, int, dict[str, list[str]]]:
    """Return (passed_count, failed_count, failures_by_target)."""
    targets = default_schema_targets(kg_dir)
    if only:
        allowed = set(only)
        targets = [target for target in targets if target.name in allowed]

    failures: dict[str, list[str]] = {}
    passed = 0
    for target in targets:
        ok, messages = validate_kg_schema(target)
        if ok:
            passed += 1
        else:
            failures[target.name] = messages
    return passed, len(failures), failures
