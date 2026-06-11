"""Relational integrity checks for knowledge graph JSON files."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agentloom_runtime.kg.paths import get_kg_dir


@dataclass
class IntegrityReport:
    role: str
    kg_path: str
    kg_format: str | None
    total_nodes: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


class KGIntegrityValidator:
    """Validate referential integrity and structural consistency of a KG file."""

    def __init__(self, kg_path: Path, *, role: str = "custom", verbose: bool = False):
        self.kg_path = kg_path
        self.role = role
        self.verbose = verbose
        self.kg_data: dict[str, Any] | None = None
        self.kg_format: str | None = None
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def load_kg(self) -> bool:
        try:
            self.kg_data = json.loads(self.kg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.errors.append(f"Failed to load KG: {exc}")
            return False

        if "nodes" in self.kg_data:
            self.kg_format = "new"
        elif "documents" in self.kg_data:
            self.kg_format = "old"
        else:
            self.errors.append("Unknown KG format")
            return False
        return True

    def validate_all(self) -> tuple[bool, IntegrityReport]:
        if not self.load_kg():
            return False, self._report()

        self._check_duplicates()
        self._check_required_fields()
        self._check_referential_integrity()
        self._check_orphans()
        self._check_metadata()
        report = self._report()
        return report.passed, report

    def _check_duplicates(self) -> None:
        assert self.kg_data is not None and self.kg_format is not None
        if self.kg_format == "new":
            ids = [node["id"] for node in self.kg_data.get("nodes", [])]
        else:
            ids = [doc["id"] for doc in self.kg_data.get("documents", [])]
        for node_id, count in Counter(ids).items():
            if count > 1:
                self.errors.append(f"Duplicate ID '{node_id}' appears {count} times")

    def _check_required_fields(self) -> None:
        assert self.kg_data is not None and self.kg_format is not None
        if self.kg_format == "new":
            required = ["id", "type", "data", "relationships"]
            for node in self.kg_data.get("nodes", []):
                missing = [field for field in required if field not in node]
                if missing:
                    self.errors.append(f"Node {node.get('id', 'UNKNOWN')}: missing {missing}")
        else:
            required = ["id", "type"]
            for doc in self.kg_data.get("documents", []):
                missing = [field for field in required if field not in doc]
                if missing:
                    self.errors.append(f"Document {doc.get('id', 'UNKNOWN')}: missing {missing}")

    def _check_referential_integrity(self) -> None:
        assert self.kg_data is not None and self.kg_format is not None
        if self.kg_format == "new":
            all_ids = {node["id"] for node in self.kg_data.get("nodes", [])}
            for node in self.kg_data.get("nodes", []):
                node_id = node.get("id")
                parent = node.get("relationships", {}).get("parent")
                if parent and parent not in all_ids:
                    self.errors.append(f"{node_id}: parent '{parent}' not found")
                for child in node.get("relationships", {}).get("children", []):
                    if child not in all_ids:
                        self.errors.append(f"{node_id}: child '{child}' not found")
        else:
            all_ids = {doc["id"] for doc in self.kg_data.get("documents", [])}
            for doc in self.kg_data.get("documents", []):
                doc_id = doc.get("id")
                for child in doc.get("contains", []):
                    if child not in all_ids:
                        self.warnings.append(f"{doc_id}: contains '{child}' not found")

    def _check_orphans(self) -> None:
        assert self.kg_data is not None and self.kg_format is not None
        if self.kg_format == "new":
            for node in self.kg_data.get("nodes", []):
                node_id = node.get("id")
                parent = node.get("relationships", {}).get("parent")
                node_type = node.get("type")
                if node_type == "root" or (node_id and "root" in node_id):
                    continue
                if not parent:
                    self.errors.append(f"Orphan node: {node_id}")
        else:
            referenced: set[str] = set()
            for doc in self.kg_data.get("documents", []):
                referenced.update(doc.get("contains", []))
            for doc in self.kg_data.get("documents", []):
                doc_id = doc.get("id")
                doc_type = doc.get("type")
                if doc_type in {"root", "category"}:
                    continue
                if doc_id not in referenced:
                    self.warnings.append(f"Orphan document: {doc_id}")

    def _check_metadata(self) -> None:
        assert self.kg_data is not None and self.kg_format is not None
        metadata = self.kg_data.get("metadata")
        if not isinstance(metadata, dict):
            self.warnings.append("Missing metadata section")
            return
        if self.kg_format == "new":
            actual = len(self.kg_data.get("nodes", []))
            claimed = metadata.get("total_nodes", 0)
            if actual != claimed:
                self.warnings.append(f"Metadata claims {claimed} nodes, but found {actual}")
        else:
            actual = len(self.kg_data.get("documents", []))
            claimed = metadata.get("total_documents", 0)
            if actual != claimed:
                self.warnings.append(f"Metadata claims {claimed} documents, but found {actual}")
        if "last_updated" not in metadata:
            self.warnings.append("Missing 'last_updated' in metadata")

    def _report(self) -> IntegrityReport:
        total = 0
        if self.kg_data and self.kg_format == "new":
            total = len(self.kg_data.get("nodes", []))
        elif self.kg_data and self.kg_format == "old":
            total = len(self.kg_data.get("documents", []))
        return IntegrityReport(
            role=self.role,
            kg_path=str(self.kg_path),
            kg_format=self.kg_format,
            total_nodes=total,
            errors=list(self.errors),
            warnings=list(self.warnings),
        )


def default_kg_paths(kg_dir: Path | None = None) -> dict[str, Path]:
    kg_dir = kg_dir or get_kg_dir()
    return {
        "builder": kg_dir / "builder-knowledge-graph.json",
        "domain": kg_dir / "domain-docs-graph.json",
    }


def validate_default_kgs(*, kg_dir: Path | None = None) -> dict[str, IntegrityReport]:
    reports: dict[str, IntegrityReport] = {}
    for role, path in default_kg_paths(kg_dir).items():
        if not path.is_file():
            reports[role] = IntegrityReport(
                role=role,
                kg_path=str(path),
                kg_format=None,
                total_nodes=0,
                errors=[f"KG file missing: {path}"],
            )
            continue
        _passed, report = KGIntegrityValidator(path, role=role).validate_all()
        reports[role] = report
    return reports
