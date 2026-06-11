"""
structural_edges.py — Phase C.2
=================================
Deterministic kg_edges from document source_path (knowledge domain only).
No LLM; targets must already exist in kg_nodes. Excludes docs/plan/ (Layer 3).

Edges use graph=structural and are preserved across JSON kg_sync.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agentloom-runtime.kg.structural_edges")

STRUCTURAL_GRAPH = "structural"

from agentloom_runtime.kg.sync.stub_document_nodes import _normalize_path, is_stub_excluded_path


@dataclass(frozen=True)
class StructuralEdge:
    src_id: str
    dst_id: str
    edge_type: str
    provenance: str


@dataclass
class StructuralEdgeReport:
    documents_scanned: int = 0
    edges_derived: int = 0
    edges_written: int = 0
    edges_skipped_missing_target: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)


def load_current_node_ids(conn: Any) -> set[str]:
    rows = conn.execute(
        "SELECT node_id FROM kg_nodes WHERE superseded_at IS NULL"
    ).fetchall()
    return {
        str(row[0] if not hasattr(row, "keys") else row["node_id"])
        for row in rows
        if row
    }


def collect_document_nodes(conn: Any) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT node_id, source_path, node_type, graph
        FROM kg_nodes
        WHERE superseded_at IS NULL
          AND source_path IS NOT NULL
          AND source_path != ''
        """
    ).fetchall()
    docs: list[dict[str, str]] = []
    for row in rows:
        node_id = str(row[0] if not hasattr(row, "keys") else row["node_id"])
        source_path = _normalize_path(
            row[1] if not hasattr(row, "keys") else row["source_path"]
        ) or ""
        node_type = str(row[2] if not hasattr(row, "keys") else row["node_type"] or "")
        graph = str(row[3] if not hasattr(row, "keys") else row["graph"] or "")
        if is_stub_excluded_path(source_path):
            continue
        docs.append({
            "node_id": node_id,
            "source_path": source_path,
            "node_type": node_type,
            "graph": graph,
        })
    return docs


def _add_edge(
    edges: list[StructuralEdge],
    *,
    src_id: str,
    dst_id: str,
    edge_type: str,
    provenance: str,
) -> None:
    edges.append(
        StructuralEdge(
            src_id=src_id,
            dst_id=dst_id,
            edge_type=edge_type,
            provenance=provenance,
        )
    )


def derive_structural_edges(
    node_id: str,
    source_path: str,
    node_ids: set[str],
) -> list[StructuralEdge]:
    """Return candidate structural edges for one document node."""
    path = _normalize_path(source_path) or ""
    if not path or is_stub_excluded_path(path):
        return []

    parts = path.split("/")
    edges: list[StructuralEdge] = []

    # docs/projects/active/{slug}/...
    if len(parts) >= 4 and parts[:3] == ["docs", "projects", "active"]:
        slug = parts[3]
        if slug and not slug.endswith(".md"):
            index_id = f"doc:domain:projects:active:{slug}:index"
            if index_id in node_ids:
                _add_edge(
                    edges,
                    src_id=node_id,
                    dst_id=index_id,
                    edge_type="belongs_to",
                    provenance="path:projects/active",
                )
                _add_edge(
                    edges,
                    src_id=index_id,
                    dst_id=node_id,
                    edge_type="contains",
                    provenance="path:projects/active",
                )

    # docs/architecture/...
    elif len(parts) >= 2 and parts[:2] == ["docs", "architecture"]:
        if "cat-architecture" in node_ids:
            _add_edge(
                edges,
                src_id=node_id,
                dst_id="cat-architecture",
                edge_type="in_domain",
                provenance="path:architecture",
            )

    # docs/research/workshop-ucgis-2026/...
    elif (
        len(parts) >= 3
        and parts[:3] == ["docs", "research", "workshop-ucgis-2026"]
        and "cat-research-workshop-ucgis" in node_ids
    ):
        _add_edge(
            edges,
            src_id=node_id,
            dst_id="cat-research-workshop-ucgis",
            edge_type="in_domain",
            provenance="path:research/workshop-ucgis",
        )

    # agents/skills/...
    elif len(parts) >= 2 and parts[:2] == ["agents", "skills"]:
        if "skills-root" in node_ids:
            _add_edge(
                edges,
                src_id=node_id,
                dst_id="skills-root",
                edge_type="in_domain",
                provenance="path:agents/skills",
            )

    # agents/behaviors/...
    elif len(parts) >= 2 and parts[:2] == ["agents", "behaviors"]:
        if "behavior-root" in node_ids:
            _add_edge(
                edges,
                src_id=node_id,
                dst_id="behavior-root",
                edge_type="in_domain",
                provenance="path:agents/behaviors",
            )

    return edges


def sync_structural_edges(conn: Any, *, dry_run: bool = False) -> StructuralEdgeReport:
    report = StructuralEdgeReport()
    node_ids = load_current_node_ids(conn)
    docs = collect_document_nodes(conn)
    report.documents_scanned = len(docs)

    derived: list[StructuralEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for doc in docs:
        for edge in derive_structural_edges(
            doc["node_id"], doc["source_path"], node_ids
        ):
            key = (edge.src_id, edge.dst_id, edge.edge_type)
            if key in seen:
                continue
            if edge.src_id not in node_ids or edge.dst_id not in node_ids:
                report.edges_skipped_missing_target += 1
                continue
            seen.add(key)
            derived.append(edge)
            report.by_rule[edge.provenance] = report.by_rule.get(edge.provenance, 0) + 1

    report.edges_derived = len(derived)
    if dry_run:
        report.edges_written = len(derived)
        return report

    conn.execute("DELETE FROM kg_edges WHERE graph = ?", (STRUCTURAL_GRAPH,))
    for edge in derived:
        conn.execute(
            """
            INSERT INTO kg_edges (src_id, dst_id, edge_type, graph)
            VALUES (?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE graph = VALUES(graph)
            """,
            (edge.src_id, edge.dst_id, edge.edge_type, STRUCTURAL_GRAPH),
        )
        report.edges_written += 1
    return report

