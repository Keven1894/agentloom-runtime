"""
stub_document_nodes.py — Phase C.1
====================================
Generate stub `document` nodes in kg_nodes for DocShare corpus docs that have no
curated KG node (coverage 15% → ~100%). Deterministic; no LLM.

Stub nodes are pointers only: {tier: stub, docshare_doc_id, source_path, title}.
They are preserved across JSON kg_sync (graph=docshare-stub) and superseded when a
curated node appears at the same source_path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("agentloom-runtime.kg.stub_document_nodes")

STUB_GRAPH = "docshare-stub"
STUB_SOURCE_FILE = "phase-c-stub-generator"
MAX_NODE_ID_LEN = 191

# Layer 3 plan/provenance — indexed via plan_embeddings, not kg_nodes (see memory arch doc).
STUB_EXCLUDED_PREFIXES = (
    "docs/plan/",
)


def _scalar(row: Any) -> int:
    if not row:
        return 0
    if hasattr(row, "keys"):
        try:
            return int(row[0])
        except (KeyError, IndexError, TypeError):
            return int(list(row)[0])
    return int(row[0])


def _normalize_path(path_val: str | None) -> str | None:
    if not path_val or not isinstance(path_val, str):
        return None
    cleaned = path_val.replace("\\", "/").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned or None


def _parse_attributes(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _content_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_stub_excluded_path(source_path: str | None) -> bool:
    normalized = _normalize_path(source_path)
    if not normalized:
        return False
    return any(normalized.startswith(prefix) for prefix in STUB_EXCLUDED_PREFIXES)


def stub_node_id_for_doc_id(doc_id: str) -> str:
    """Stable stub node id; capped for VARCHAR(191)."""
    base = f"stub-{doc_id}"
    if len(base) <= MAX_NODE_ID_LEN:
        return base
    digest = hashlib.sha1(doc_id.encode("utf-8")).hexdigest()[:12]
    prefix = "stub-"
    room = MAX_NODE_ID_LEN - len(prefix) - 1 - len(digest)
    return f"{prefix}{doc_id[:room]}-{digest}"


def is_stub_node(*, graph: str | None, attributes: dict[str, Any]) -> bool:
    if graph == STUB_GRAPH:
        return True
    return str(attributes.get("tier") or "").lower() == "stub"


@dataclass
class StubDocRow:
    doc_id: str
    source_path: str
    title: str
    doc_type: str | None = None


@dataclass
class StubNode:
    node_id: str
    title: str
    source_path: str
    attributes: dict[str, Any]
    content_hash: str


@dataclass
class StubSyncReport:
    stubs_needed: int = 0
    stubs_inserted: int = 0
    stubs_superseded: int = 0
    stubs_unchanged: int = 0
    stubs_skipped_curated_path: int = 0
    orphaned_stubs_superseded: int = 0
    docshare_linked_after: int = 0
    errors: list[str] = field(default_factory=list)


def _load_path_maps(conn: Any) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return (path→node_id, path→curated_node_id, stub_node_ids)."""
    path_to_node: dict[str, str] = {}
    path_to_curated: dict[str, str] = {}
    stub_ids: set[str] = set()
    rows = conn.execute(
        "SELECT node_id, source_path, graph, attributes FROM kg_nodes "
        "WHERE superseded_at IS NULL"
    ).fetchall()
    for row in rows:
        if hasattr(row, "keys"):
            node_id = str(row["node_id"])
            source_path = _normalize_path(row["source_path"])
            graph = row["graph"]
            attrs = _parse_attributes(row["attributes"])
        else:
            node_id, source_path, graph, attrs_raw = row[0], row[1], row[2], row[3]
            source_path = _normalize_path(source_path)
            attrs = _parse_attributes(attrs_raw)
        if is_stub_node(graph=graph, attributes=attrs):
            stub_ids.add(node_id)
        if source_path:
            path_to_node[source_path] = node_id
            if not is_stub_node(graph=graph, attributes=attrs):
                path_to_curated[source_path] = node_id
    return path_to_node, path_to_curated, stub_ids


def _load_current_stub_hashes(conn: Any) -> dict[str, str]:
    rows = conn.execute(
        "SELECT node_id, content_hash FROM kg_nodes "
        "WHERE superseded_at IS NULL AND graph = ?",
        (STUB_GRAPH,),
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        node_id = row[0] if not hasattr(row, "keys") else row["node_id"]
        content_hash = row[1] if not hasattr(row, "keys") else row["content_hash"]
        if node_id:
            out[str(node_id)] = str(content_hash or "")
    return out


def collect_unlinked_docshare_rows(conn: Any) -> list[StubDocRow]:
    if not _table_exists(conn, "docshare_embeddings"):
        return []
    rows = conn.execute(
        """
        SELECT e.doc_id, e.source_path, COALESCE(d.title, e.title) AS title,
               COALESCE(d.doc_type, e.doc_type) AS doc_type
        FROM docshare_embeddings e
        LEFT JOIN docshare_documents d ON d.doc_id = e.doc_id
        """
    ).fetchall()
    _, path_to_curated, _ = _load_path_maps(conn)
    unlinked: list[StubDocRow] = []
    for row in rows:
        if hasattr(row, "keys"):
            doc_id = str(row["doc_id"] or "")
            source_path = _normalize_path(row["source_path"]) or ""
            title = str(row["title"] or doc_id)
            doc_type = row["doc_type"] if "doc_type" in row.keys() else None
        else:
            doc_id, source_path, title, doc_type = row[0], row[1], row[2], row[3]
            source_path = _normalize_path(source_path) or ""
            title = str(title or doc_id)
        if not doc_id or not source_path:
            continue
        if is_stub_excluded_path(source_path):
            continue
        if source_path in path_to_curated:
            continue
        unlinked.append(
            StubDocRow(
                doc_id=doc_id,
                source_path=source_path,
                title=title,
                doc_type=str(doc_type) if doc_type else None,
            )
        )
    return unlinked


def build_stub_node(doc: StubDocRow) -> StubNode:
    node_id = stub_node_id_for_doc_id(doc.doc_id)
    attributes = {
        "tier": "stub",
        "provenance": "phase-c-stub-generator",
        "docshare_doc_id": doc.doc_id,
        "source_path": doc.source_path,
    }
    if doc.doc_type:
        attributes["doc_type"] = doc.doc_type
    payload = {
        "node_id": node_id,
        "node_type": "document",
        "graph": STUB_GRAPH,
        "title": doc.title,
        "attributes": attributes,
        "source_file": STUB_SOURCE_FILE,
        "source_path": doc.source_path,
    }
    return StubNode(
        node_id=node_id,
        title=doc.title,
        source_path=doc.source_path,
        attributes=attributes,
        content_hash=_content_hash(payload),
    )


def load_preserved_stub_node_ids(conn: Any) -> set[str]:
    """Stub node_ids that JSON kg_sync must not orphan-supersede."""
    _, _, stub_ids = _load_path_maps(conn)
    return stub_ids


def _table_exists(conn: Any, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


def _supersede_node(conn: Any, node_id: str, now: str) -> None:
    conn.execute(
        "UPDATE kg_nodes SET superseded_at = ?, updated_at = ? "
        "WHERE node_id = ? AND superseded_at IS NULL",
        (now, now, node_id),
    )


def _insert_stub_node(conn: Any, node: StubNode, now: str) -> None:
    attrs_json = json.dumps(node.attributes, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO kg_nodes
          (node_id, node_type, graph, title, attributes, source_file,
           source_path, content_hash, valid_from, superseded_at, updated_at)
        VALUES (?, 'document', ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            node.node_id,
            STUB_GRAPH,
            node.title,
            attrs_json,
            STUB_SOURCE_FILE,
            node.source_path,
            node.content_hash,
            now,
            now,
        ),
    )


def supersede_stubs_at_curated_paths(conn: Any, *, dry_run: bool = False) -> int:
    """Remove stub nodes when a curated node now owns the same source_path."""
    path_to_node, path_to_curated, stub_ids = _load_path_maps(conn)
    if not stub_ids:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    count = 0
    for source_path, curated_id in path_to_curated.items():
        stub_id = path_to_node.get(source_path)
        if not stub_id or stub_id not in stub_ids or stub_id == curated_id:
            continue
        if not dry_run:
            _supersede_node(conn, stub_id, now)
        count += 1
    return count


def sync_stub_document_nodes(
    conn: Any,
    *,
    dry_run: bool = False,
) -> StubSyncReport:
    report = StubSyncReport()
    unlinked = collect_unlinked_docshare_rows(conn)
    report.stubs_needed = len(unlinked)

    report.orphaned_stubs_superseded = supersede_stubs_at_curated_paths(
        conn, dry_run=dry_run,
    )

    existing_hashes = _load_current_stub_hashes(conn)
    now = datetime.now().isoformat(timespec="seconds")
    target_stub_ids: set[str] = set()

    for doc in unlinked:
        node = build_stub_node(doc)
        target_stub_ids.add(node.node_id)
        prev_hash = existing_hashes.get(node.node_id)
        if prev_hash == node.content_hash:
            report.stubs_unchanged += 1
            continue
        if dry_run:
            if prev_hash is not None:
                report.stubs_superseded += 1
            report.stubs_inserted += 1
            continue
        if prev_hash is not None:
            _supersede_node(conn, node.node_id, now)
            report.stubs_superseded += 1
        _insert_stub_node(conn, node, now)
        report.stubs_inserted += 1

    # Supersede stubs whose docshare row disappeared
    for stub_id in existing_hashes:
        if stub_id not in target_stub_ids:
            if not dry_run:
                _supersede_node(conn, stub_id, now)
            report.orphaned_stubs_superseded += 1

    return report


@dataclass
class RetractPlanStubsReport:
    stubs_superseded: int = 0
    plan_stubs_remaining: int = 0


def retract_plan_stubs(conn: Any, *, dry_run: bool = False) -> RetractPlanStubsReport:
    """Supersede docshare-stub kg_nodes under docs/plan/ (Layer 3, not KG index)."""
    report = RetractPlanStubsReport()
    now = datetime.now().isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT node_id, source_path FROM kg_nodes "
        "WHERE superseded_at IS NULL AND graph = ?",
        (STUB_GRAPH,),
    ).fetchall()
    for row in rows:
        node_id = row[0] if not hasattr(row, "keys") else row["node_id"]
        source_path = _normalize_path(
            row[1] if not hasattr(row, "keys") else row["source_path"]
        )
        if not is_stub_excluded_path(source_path):
            continue
        if not dry_run:
            _supersede_node(conn, str(node_id), now)
        report.stubs_superseded += 1

    if not dry_run:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM kg_nodes "
            "WHERE superseded_at IS NULL AND graph = ? AND source_path LIKE ?",
            (STUB_GRAPH, "docs/plan/%"),
        ).fetchone()
        report.plan_stubs_remaining = _scalar(remaining)
    else:
        before = conn.execute(
            "SELECT COUNT(*) FROM kg_nodes "
            "WHERE superseded_at IS NULL AND graph = ? AND source_path LIKE ?",
            (STUB_GRAPH, "docs/plan/%"),
        ).fetchone()
        report.plan_stubs_remaining = max(0, _scalar(before) - report.stubs_superseded)
    return report


def repair_stub_docshare_doc_ids(conn: Any, *, dry_run: bool = False) -> int:
    """Fix stub nodes whose docshare_doc_id was set to embedding id instead of doc_id."""
    rows = conn.execute(
        "SELECT node_id, attributes FROM kg_nodes "
        "WHERE superseded_at IS NULL AND graph = ?",
        (STUB_GRAPH,),
    ).fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    fixed = 0
    for row in rows:
        node_id = row[0] if not hasattr(row, "keys") else row["node_id"]
        attrs = _parse_attributes(row[1] if not hasattr(row, "keys") else row["attributes"])
        if not str(node_id).startswith("stub-"):
            continue
        correct = str(node_id)[len("stub-") :]
        current = str(attrs.get("docshare_doc_id") or "")
        if not correct or current == correct:
            continue
        attrs["docshare_doc_id"] = correct
        if dry_run:
            fixed += 1
            continue
        conn.execute(
            "UPDATE kg_nodes SET attributes = ?, updated_at = ? "
            "WHERE node_id = ? AND superseded_at IS NULL",
            (json.dumps(attrs, ensure_ascii=False), now, node_id),
        )
        fixed += 1
    return fixed


def coverage_summary(conn: Any) -> dict[str, int]:
    total = 0
    linked = 0
    if _table_exists(conn, "docshare_embeddings"):
        total_row = conn.execute("SELECT COUNT(*) FROM docshare_embeddings").fetchone()
        total = int(total_row[0] if not hasattr(total_row, "keys") else list(total_row.values())[0])
        linked_row = conn.execute(
            """
            SELECT COUNT(*) FROM docshare_embeddings
            WHERE metadata_json LIKE '%"kg_indexed": true%'
               OR metadata_json LIKE '%"kg_indexed":true%'
            """
        ).fetchone()
        linked = int(linked_row[0] if not hasattr(linked_row, "keys") else list(linked_row.values())[0])
    stubs_row = conn.execute(
        "SELECT COUNT(*) FROM kg_nodes WHERE superseded_at IS NULL AND graph = ?",
        (STUB_GRAPH,),
    ).fetchone()
    stubs = int(stubs_row[0] if not hasattr(stubs_row, "keys") else list(stubs_row.values())[0])
    return {
        "docshare_total": total,
        "docshare_kg_indexed": linked,
        "docshare_unlinked": max(0, total - linked),
        "stub_nodes_current": stubs,
    }
