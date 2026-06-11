"""
kg_graph_sync.py
================
Sync KG node attributes + edges from agents/knowledge-graphs/*.json into kg_nodes / kg_edges.

Called from rebuild_embeddings.py on --commit (same authoring → DB contract).
Runtime must read kg_graph.py (DB only), never these JSON files.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("agentloom-runtime.kg.graph_sync")

from agentloom_runtime.kg.paths import get_kg_dir

KG_DIR = get_kg_dir()

PATH_KEYS = ("path", "source_file", "file_path", "file", "content_path")
STRUCTURED_ATTR_KEYS = (
    "risk_triggers",
    "allowlisted_mutations",
    "prod_live_verification",
    "decision_rule",
    "type",
    "category",
    "priority",
    "status",
    "tier",
)
EDGE_LIST_KEYS = {
    "governed_by": "governed_by",
    "guided_by": "guided_by",
    "applies_to": "applies_to",
    "constrains": "constrains",
    "related_to": "related_to",
    "links": "links",
}


def _normalize_path(path_val: str | None) -> str | None:
    if not path_val or not isinstance(path_val, str):
        return None
    cleaned = path_val.replace("\\", "/").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned or None


def _content_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pick_path(node: dict[str, Any]) -> str | None:
    for key in PATH_KEYS:
        val = node.get(key)
        if isinstance(val, str) and val:
            return _normalize_path(val)
    return None


def _node_title(node: dict[str, Any], fallback: str) -> str:
    return (
        node.get("name")
        or node.get("label")
        or (node.get("data") or {}).get("label")
        or (node.get("data") or {}).get("name")
        or fallback
    )


def _node_type(node: dict[str, Any], default: str = "node") -> str:
    return (
        node.get("type")
        or (node.get("data") or {}).get("type")
        or default
    )


def _extract_attributes(node: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for key in STRUCTURED_ATTR_KEYS:
        if key in node and node[key] is not None:
            attrs[key] = node[key]
    source_path = _pick_path(node)
    if source_path:
        attrs["source_path"] = source_path
    return attrs


def _is_archived(node: dict[str, Any]) -> bool:
    if node.get("archived") is True:
        return True
    status = (node.get("status") or "").lower()
    return status in {"archived", "deprecated", "deleted"}


@dataclass
class GraphNode:
    node_id: str
    node_type: str
    graph: str
    title: str
    attributes: dict[str, Any]
    source_file: str
    source_path: str | None
    content_hash: str


@dataclass
class GraphEdge:
    src_id: str
    dst_id: str
    edge_type: str
    graph: str


@dataclass
class GraphSyncReport:
    nodes_inserted: int = 0
    nodes_superseded: int = 0
    nodes_unchanged: int = 0
    edges_written: int = 0
    docshare_linked: int = 0
    docshare_unlinked: int = 0
    errors: list[str] = field(default_factory=list)


def _emit_edges(node_id: str, node: dict[str, Any], graph: str) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    for field_name, edge_type in EDGE_LIST_KEYS.items():
        targets = node.get(field_name) or []
        if not isinstance(targets, list):
            continue
        for target in targets:
            if isinstance(target, str) and target:
                edges.append(GraphEdge(node_id, target, edge_type, graph))
    contains = node.get("contains") or []
    if isinstance(contains, list):
        for target in contains:
            if isinstance(target, str) and target:
                edges.append(GraphEdge(node_id, target, "contains", graph))
    return edges


def _make_node(
    node: dict[str, Any],
    *,
    graph: str,
    source_file: str,
    default_type: str,
) -> GraphNode | None:
    node_id = node.get("id", "")
    if not node_id or _is_archived(node):
        return None
    attributes = _extract_attributes(node)
    source_path = attributes.get("source_path") or _pick_path(node)
    title = _node_title(node, node_id)
    node_type = _node_type(node, default_type)
    payload = {
        "node_id": node_id,
        "node_type": node_type,
        "graph": graph,
        "title": title,
        "attributes": attributes,
        "source_file": source_file,
        "source_path": source_path,
    }
    return GraphNode(
        node_id=node_id,
        node_type=node_type,
        graph=graph,
        title=title,
        attributes=attributes,
        source_file=source_file,
        source_path=source_path,
        content_hash=_content_hash(payload),
    )


def collect_graph_from_json() -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_ids: set[str] = set()

    def add_node(node: GraphNode | None) -> None:
        if node is None or node.node_id in seen_ids:
            return
        seen_ids.add(node.node_id)
        nodes.append(node)

    specs: list[tuple[str, str, str, str]] = [
        ("domain-behaviors.json", "behaviors", "domain", "behavior"),
        ("domain-skills-graph.json", "skills", "domain", "skill"),
        ("domain-docs-graph.json", "documents", "domain", "document"),
        ("builder-knowledge-graph.json", "nodes", "builder", "concept"),
        ("builder-skills-graph.json", "skills", "builder", "skill"),
        ("builder-behaviors-graph.json", "behaviors", "builder", "behavior"),
    ]

    for fname, array_key, graph_name, default_type in specs:
        path = KG_DIR / fname
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get(array_key) or []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            if array_key == "nodes" and "data" in raw:
                merged = {**raw, **(raw.get("data") or {})}
                merged["id"] = raw.get("id") or merged.get("id")
                node = _make_node(merged, graph=graph_name, source_file=fname, default_type=default_type)
            else:
                node = _make_node(raw, graph=graph_name, source_file=fname, default_type=default_type)
            if node:
                add_node(node)
                edges.extend(_emit_edges(node.node_id, raw, graph_name))

        for edge in data.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            src = edge.get("from") or edge.get("src_id") or edge.get("source")
            dst = edge.get("to") or edge.get("dst_id") or edge.get("target")
            etype = edge.get("type") or edge.get("edge_type") or "related"
            if src and dst:
                edges.append(GraphEdge(str(src), str(dst), str(etype), graph_name))

    # Deduplicate edges
    edge_keys = set()
    unique_edges: list[GraphEdge] = []
    for edge in edges:
        key = (edge.src_id, edge.dst_id, edge.edge_type, edge.graph)
        if key in edge_keys:
            continue
        edge_keys.add(key)
        unique_edges.append(edge)
    return nodes, unique_edges


def ensure_kg_graph_tables(conn: Any, *, is_mysql: bool = True) -> None:
    del is_mysql  # MySQL-only runtime (2026-06-07)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kg_nodes (
            row_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
            node_id       VARCHAR(191) NOT NULL,
            node_type     VARCHAR(64),
            graph         VARCHAR(64),
            title         LONGTEXT,
            attributes    JSON,
            source_file   VARCHAR(191),
            source_path   VARCHAR(512),
            content_hash  CHAR(64),
            valid_from    DATETIME,
            superseded_at DATETIME NULL,
            current_key   VARCHAR(191) GENERATED ALWAYS AS (
                IF(superseded_at IS NULL, node_id, NULL)
            ) STORED,
            updated_at    DATETIME NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kg_edges (
            src_id    VARCHAR(191) NOT NULL,
            dst_id    VARCHAR(191) NOT NULL,
            edge_type VARCHAR(64) NOT NULL,
            graph     VARCHAR(64),
            PRIMARY KEY (src_id, dst_id, edge_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """)
    for sql in (
        "CREATE UNIQUE INDEX uq_kg_nodes_current ON kg_nodes (current_key)",
        "CREATE INDEX idx_kg_nodes_node_id ON kg_nodes (node_id)",
        "CREATE INDEX idx_kg_nodes_source_path ON kg_nodes (source_path(191))",
        "CREATE INDEX idx_kg_edges_dst ON kg_edges (dst_id)",
    ):
        try:
            conn.execute(sql)
        except Exception as exc:
            if "Duplicate key name" not in str(exc) and "already exists" not in str(exc):
                raise


def _load_current_nodes(conn: Any) -> dict[str, str]:
    rows = conn.execute(
        "SELECT node_id, content_hash FROM kg_nodes WHERE superseded_at IS NULL"
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        node_id = row[0] if not hasattr(row, "keys") else row["node_id"]
        content_hash = row[1] if not hasattr(row, "keys") else row["content_hash"]
        if node_id:
            out[str(node_id)] = str(content_hash or "")
    return out


def _supersede_node(conn: Any, node_id: str, now: str) -> None:
    conn.execute(
        "UPDATE kg_nodes SET superseded_at = ?, updated_at = ? "
        "WHERE node_id = ? AND superseded_at IS NULL",
        (now, now, node_id),
    )


def _insert_node(conn: Any, node: GraphNode, now: str, *, is_mysql: bool = True) -> None:
    del is_mysql
    attrs_json = json.dumps(node.attributes, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO kg_nodes
          (node_id, node_type, graph, title, attributes, source_file,
           source_path, content_hash, valid_from, superseded_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            node.node_id,
            node.node_type,
            node.graph,
            node.title,
            attrs_json,
            node.source_file,
            node.source_path,
            node.content_hash,
            now,
            now,
        ),
    )


def sync_kg_graph(conn: Any, *, is_mysql: bool, dry_run: bool = False) -> GraphSyncReport:
    report = GraphSyncReport()
    nodes, edges = collect_graph_from_json()
    if dry_run:
        report.nodes_inserted = len(nodes)
        report.edges_written = len(edges)
        return report

    ensure_kg_graph_tables(conn, is_mysql=is_mysql)
    existing = _load_current_nodes(conn)
    now = datetime.now().isoformat(timespec="seconds")

    try:
        from agentloom_runtime.kg.sync.stub_document_nodes import load_preserved_stub_node_ids
    except ImportError:
        load_preserved_stub_node_ids = lambda _conn: set()  # type: ignore

    preserved_stub_ids = load_preserved_stub_node_ids(conn)
    target_ids = {n.node_id for n in nodes} | preserved_stub_ids
    for node in nodes:
        prev_hash = existing.get(node.node_id)
        if prev_hash == node.content_hash:
            report.nodes_unchanged += 1
            continue
        if prev_hash is not None:
            _supersede_node(conn, node.node_id, now)
            report.nodes_superseded += 1
        _insert_node(conn, node, now, is_mysql=is_mysql)
        report.nodes_inserted += 1

    for old_id in existing:
        if old_id not in target_ids:
            _supersede_node(conn, old_id, now)
            report.nodes_superseded += 1

    conn.execute("DELETE FROM kg_edges WHERE graph != ?", ("structural",))
    for edge in edges:
        conn.execute(
            "INSERT INTO kg_edges (src_id, dst_id, edge_type, graph) VALUES (?, ?, ?, ?)",
            (edge.src_id, edge.dst_id, edge.edge_type, edge.graph),
        )
    report.edges_written = len(edges)
    return report


def _table_exists(conn: Any, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


def backlink_docshare(conn: Any, *, is_mysql: bool, dry_run: bool = False) -> GraphSyncReport:
    """Phase A: join docshare_embeddings ↔ kg_nodes via source_path."""
    report = GraphSyncReport()
    if not _table_exists(conn, "docshare_embeddings"):
        return report

    path_to_node: dict[str, str] = {}
    rows = conn.execute(
        "SELECT node_id, source_path, attributes FROM kg_nodes "
        "WHERE superseded_at IS NULL AND source_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        if hasattr(row, "keys"):
            node_id = row["node_id"]
            source_path = _normalize_path(row["source_path"])
            attrs_raw = row["attributes"]
        else:
            node_id, source_path, attrs_raw = row[0], row[1], row[2]
            source_path = _normalize_path(source_path)
        if source_path:
            path_to_node[source_path] = node_id

    doc_rows = conn.execute(
        "SELECT id, doc_id, source_path, metadata_json FROM docshare_embeddings"
    ).fetchall()
    for row in doc_rows:
        if hasattr(row, "keys"):
            emb_id = row["id"]
            doc_id = row["doc_id"]
            source_path = _normalize_path(row["source_path"])
            meta_raw = row["metadata_json"]
        else:
            emb_id, doc_id, source_path, meta_raw = row[0], row[1], row[2], row[3]
            source_path = _normalize_path(source_path)
        meta: dict[str, Any]
        if isinstance(meta_raw, dict):
            meta = dict(meta_raw)
        elif isinstance(meta_raw, str) and meta_raw:
            try:
                meta = json.loads(meta_raw)
            except json.JSONDecodeError:
                meta = {}
        else:
            meta = {}

        kg_node_id = path_to_node.get(source_path or "")
        if kg_node_id:
            meta["kg_node_id"] = kg_node_id
            meta["kg_indexed"] = True
            report.docshare_linked += 1
        else:
            meta["kg_node_id"] = None
            meta["kg_indexed"] = False
            report.docshare_unlinked += 1

        if dry_run:
            continue
        conn.execute(
            "UPDATE docshare_embeddings SET metadata_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), emb_id),
        )

        if kg_node_id:
            node_row = conn.execute(
                "SELECT attributes FROM kg_nodes WHERE node_id = ? AND superseded_at IS NULL",
                (kg_node_id,),
            ).fetchone()
            if node_row:
                attrs_raw = node_row[0] if not hasattr(node_row, "keys") else node_row["attributes"]
                if isinstance(attrs_raw, str):
                    attrs = json.loads(attrs_raw or "{}")
                elif isinstance(attrs_raw, dict):
                    attrs = dict(attrs_raw)
                else:
                    attrs = {}
                canonical_doc_id = str(doc_id or "").strip()
                if canonical_doc_id and attrs.get("docshare_doc_id") != canonical_doc_id:
                    attrs["docshare_doc_id"] = canonical_doc_id
                    conn.execute(
                        "UPDATE kg_nodes SET attributes = ?, updated_at = ? "
                        "WHERE node_id = ? AND superseded_at IS NULL",
                        (json.dumps(attrs, ensure_ascii=False), datetime.now().isoformat(), kg_node_id),
                    )

    return report


def run_graph_sync(conn: Any, *, is_mysql: bool, dry_run: bool = False) -> dict[str, Any]:
    from agentloom_runtime.kg.sync.stub_document_nodes import (
        coverage_summary,
        supersede_stubs_at_curated_paths,
        sync_stub_document_nodes,
    )
    from agentloom_runtime.kg.sync.structural_edges import sync_structural_edges

    graph_report = sync_kg_graph(conn, is_mysql=is_mysql, dry_run=dry_run)
    stub_report = sync_stub_document_nodes(conn, dry_run=dry_run)
    orphaned_stubs = supersede_stubs_at_curated_paths(conn, dry_run=dry_run)
    structural_report = sync_structural_edges(conn, dry_run=dry_run)
    link_report = backlink_docshare(conn, is_mysql=is_mysql, dry_run=dry_run)
    cov = coverage_summary(conn)
    return {
        "nodes_inserted": graph_report.nodes_inserted,
        "nodes_superseded": graph_report.nodes_superseded,
        "nodes_unchanged": graph_report.nodes_unchanged,
        "edges_written": graph_report.edges_written,
        "stub_stubs_needed": stub_report.stubs_needed,
        "stub_stubs_inserted": stub_report.stubs_inserted,
        "stub_stubs_superseded": stub_report.stubs_superseded,
        "stub_stubs_unchanged": stub_report.stubs_unchanged,
        "stub_orphaned_superseded": orphaned_stubs,
        "structural_edges_derived": structural_report.edges_derived,
        "structural_edges_written": structural_report.edges_written,
        "structural_by_rule": structural_report.by_rule,
        "docshare_linked": link_report.docshare_linked,
        "docshare_unlinked": link_report.docshare_unlinked,
        "docshare_total": cov["docshare_total"],
        "docshare_kg_indexed": cov["docshare_kg_indexed"],
        "stub_nodes_current": cov["stub_nodes_current"],
    }
