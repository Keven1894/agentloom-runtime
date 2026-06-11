"""
rebuild_embeddings.py
=====================
Rebuild the ``knowledge_embeddings`` table from the current file tree.
One-way sync: files are the authoring source, the embedding table is the
execution source consumed by ``search_kg()`` at MCP runtime.

Design (Phase 2 of 2026-04-16 Envita repo optimization plan):

    1. Walk ``agents/knowledge-graphs/*.json`` for node metadata
       (domain-behaviors, domain-skills, domain-docs, builder-knowledge,
       builder-skills, builder-behaviors).
    2. For each node, build a *chunk*:
        - ``id``           stable string ``kg:<kind>:<node_id>`` (primary key).
        - ``content``      if the node points to a real ``.md`` file on disk
                           (``path`` / ``source_file`` / ``file_path`` field),
                           read that file and prefer its content for
                           embedding. Otherwise fall back to JSON metadata
                           (name + description + contains + etc).
        - ``topic``        display name.
        - ``content_hash`` sha256 of content, used to detect stale rows.
    3. Diff current DB state against the freshly-collected chunks:
        - **New**      (chunk.id not in DB)            -> embed + insert.
        - **Updated**  (chunk.id in DB, hash changed)   -> embed + replace.
        - **Unchanged** (chunk.id in DB, hash matches)  -> skip.
        - **Stale**    (row in DB, id no longer exists) -> delete.
    4. Atomic within the configured database. SQLite dev runs get a local file
       backup before writing; production MySQL must be backed up server-side.

Modes:

    --dry-run      Print the diff, don't touch the DB, don't call OpenAI.
    --commit       Actually write. Default is ``--dry-run``.
    --no-embed     Skip OpenAI calls; insert content with NULL embedding
                   (keyword fallback still works; useful when offline).
    --reset        Truncate the table first, then full re-embed.
    --only KIND    Restrict to one extractor: domain-skills, builder-knowledge,
                   builder-skills, builder-behaviors, domain-behaviors,
                   domain-docs.
    --verbose      Show each chunk id as it's processed.

Output:

    Summary printed to stdout. A JSON report of the diff is written to
    ``Scripts/kg_sync/last_run.json`` for inspection by
    ``validate_kg_sync.py``.

AgentLoom runtime KG sync
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from agentloom_runtime.db import connect, get_database_settings, is_mysql, is_sqlite
from agentloom_runtime.kg.paths import get_kg_dir, get_repo_root, get_sync_report_path
from agentloom_runtime.kg.sync.graph_sync import run_graph_sync

REPO_ROOT = get_repo_root()
KG_DIR = get_kg_dir()
REPORT = get_sync_report_path()
DB_PATH = Path()  # SQLite backup unused in MySQL-only runtime

# ─── Config ───────────────────────────────────────────────────────────────────

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM   = 1536
BATCH_SIZE  = 50
MAX_CONTENT_CHARS = 6000   # clip each chunk to ~1500 tokens before embedding
MIN_CONTENT_CHARS = 30

# Fields on JSON nodes that may point at a concrete Markdown source file.
PATH_KEYS = ("path", "source_file", "file_path", "file")

logger = logging.getLogger("agentloom-runtime.kg.rebuild")


# ─── Env / OpenAI (lazy) ──────────────────────────────────────────────────────

def _load_env() -> None:
    env_file = os.environ.get("AGENTLOOM_ENV_FILE", "")
    env_path = Path(env_file) if env_file else get_repo_root() / ".env"
    if env_path.is_file():
        values: dict[str, str] = {}
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        for key, value in values.items():
            os.environ.setdefault(key, value)
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except Exception:
        pass


def describe_db_target() -> str:
    settings = get_database_settings()
    if settings.driver == "mysql":
        return f"mysql://{settings.host}:{settings.port}/{settings.database}"
    return f"sqlite://{settings.sqlite_path}"


_openai_client = None

def _get_openai():
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    import openai
    _openai_client = openai.OpenAI(api_key=api_key)
    return _openai_client


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    id: str
    source_file: str      # JSON graph filename (for back-reference)
    node_id: str
    node_type: str        # behavior / skill / document / concept / pattern / component
    topic: str
    content: str
    content_source: str   # "markdown" | "json"
    md_path: str | None = None  # resolved MD file (if any), relative to repo

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class DiffReport:
    to_insert: list[Chunk] = field(default_factory=list)
    to_update: list[Chunk] = field(default_factory=list)
    unchanged: list[str]   = field(default_factory=list)  # chunk ids
    to_delete: list[str]   = field(default_factory=list)  # chunk ids

    def summary(self) -> dict[str, int]:
        return {
            "insert":    len(self.to_insert),
            "update":    len(self.to_update),
            "unchanged": len(self.unchanged),
            "delete":    len(self.to_delete),
        }


# ─── Markdown resolution ──────────────────────────────────────────────────────

def _strip_frontmatter(text: str) -> str:
    """Drop YAML frontmatter if present (saves tokens, same semantic content)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def _resolve_md(path_val: str | None) -> Path | None:
    """Resolve a KG node's path field to an actual readable .md file."""
    if not path_val:
        return None
    if not path_val.endswith(".md"):
        return None
    # normalize windows/unix separators
    candidate = REPO_ROOT / path_val.replace("\\", "/")
    if candidate.is_file():
        return candidate
    return None


def _read_md(md: Path, max_chars: int = MAX_CONTENT_CHARS) -> str:
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Failed to read %s: %s", md, e)
        return ""
    text = _strip_frontmatter(text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n...(truncated)"
    return text


# ─── JSON extraction helpers ──────────────────────────────────────────────────

def _text_from_contains(items: list) -> str:
    parts: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            name  = item.get("name", "")
            value = item.get("value") or item.get("description") or ""
            if name and value:
                parts.append(f"{name}: {value}")
            elif value:
                parts.append(str(value))
            elif name:
                parts.append(name)
    return "\n".join(parts)


def _pick_path(node: dict) -> str | None:
    for k in PATH_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _node_is_archived(node: dict) -> bool:
    if node.get("archived") is True:
        return True
    # Some schemas use status fields.
    status = (node.get("status") or "").lower()
    return status in {"archived", "deprecated", "deleted"}


def _build_content(
    *,
    title: str,
    json_body: str,
    md_path: Path | None,
) -> tuple[str, str, str | None]:
    """
    Returns (content, content_source, md_relpath).
    Prefers markdown content when available; always prepends a one-line title.
    """
    md_rel: str | None = None
    if md_path is not None:
        md_body = _read_md(md_path)
        if len(md_body) >= MIN_CONTENT_CHARS:
            md_rel = str(md_path.relative_to(REPO_ROOT)).replace("\\", "/")
            return (f"{title}\n\n{md_body}", "markdown", md_rel)
    # Fallback to JSON-derived body.
    return (f"{title}\n\n{json_body}".strip(), "json", None)


# ─── Extractors (one per JSON graph) ──────────────────────────────────────────

def extract_domain_behaviors(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Chunk] = []
    for b in data.get("behaviors", []):
        bid = b.get("id", "")
        if not bid or _node_is_archived(b):
            continue
        name = b.get("name") or bid

        desc     = b.get("description") or b.get("rule") or b.get("summary") or ""
        rules    = b.get("rules") or ([b["rule"]] if b.get("rule") else [])
        rule_txt = "\n".join(f"- {r}" for r in rules)
        rationale= b.get("rationale", "")
        example  = b.get("example", "")
        category = b.get("category", "")
        applies  = ", ".join(b.get("applies_to", []))

        impl = b.get("implementation_details", {})
        impl_txt = "\n".join(f"{k}: {v}" for k, v in impl.items() if v) if isinstance(impl, dict) else ""

        body_parts: list[str] = []
        if category:   body_parts.append(f"Category: {category}")
        if desc:       body_parts.append(f"Description: {desc}")
        if rule_txt:   body_parts.append(f"Rules:\n{rule_txt}")
        if rationale:  body_parts.append(f"Rationale: {rationale}")
        if example:    body_parts.append(f"Example: {example}")
        if impl_txt:   body_parts.append(f"Implementation:\n{impl_txt}")
        if applies:    body_parts.append(f"Applies to: {applies}")
        json_body = "\n\n".join(body_parts)

        md = _resolve_md(_pick_path(b))
        content, source, md_rel = _build_content(
            title=f"Behavior: {name}", json_body=json_body, md_path=md
        )
        if len(content) < MIN_CONTENT_CHARS:
            continue

        out.append(Chunk(
            id=f"kg:behavior:{bid}",
            source_file=path.name,
            node_id=bid,
            node_type="behavior",
            topic=name,
            content=content,
            content_source=source,
            md_path=md_rel,
        ))
    return out


def extract_domain_skills(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Chunk] = []
    for s in data.get("skills", []):
        sid = s.get("id", "")
        if not sid or _node_is_archived(s):
            continue
        name = s.get("name") or sid

        desc = s.get("description", "")
        body = desc or _text_from_contains(s.get("contains", []))

        inbound_parts: list[str] = []
        for h in s.get("inbound_handling", []) or []:
            intent   = h.get("intent", "")
            triggers = "\n".join(f"- {t}" for t in h.get("triggers", []))
            steps    = "\n".join(h.get("steps", []))
            escalate = "\n".join(f"- {e}" for e in h.get("escalate_if", []))
            piece = f"Intent: {intent}\nTriggers:\n{triggers}\nSteps:\n{steps}"
            if escalate:
                piece += f"\nEscalate if:\n{escalate}"
            inbound_parts.append(piece)
        inbound_txt = "\n\n".join(inbound_parts)

        json_body = body
        if inbound_txt:
            json_body = f"{body}\n\nInbound handling:\n{inbound_txt}".strip()

        md = _resolve_md(_pick_path(s))
        content, source, md_rel = _build_content(
            title=f"Skill: {name}", json_body=json_body, md_path=md
        )
        if len(content) < MIN_CONTENT_CHARS:
            continue

        out.append(Chunk(
            id=f"kg:skill:{sid}",
            source_file=path.name,
            node_id=sid,
            node_type="skill",
            topic=name,
            content=content,
            content_source=source,
            md_path=md_rel,
        ))
    return out


def extract_domain_docs(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Chunk] = []
    for d in data.get("documents", []):
        did = d.get("id", "")
        if not did or did.endswith("-root") or _node_is_archived(d):
            continue
        name = d.get("name") or did
        json_body = _text_from_contains(d.get("contains", []))

        md = _resolve_md(_pick_path(d))
        content, source, md_rel = _build_content(
            title=f"Documentation: {name}", json_body=json_body, md_path=md
        )
        if len(content) < MIN_CONTENT_CHARS:
            continue

        out.append(Chunk(
            id=f"kg:doc:{did}",
            source_file=path.name,
            node_id=did,
            node_type="document",
            topic=name,
            content=content,
            content_source=source,
            md_path=md_rel,
        ))
    return out


def extract_builder_knowledge(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Chunk] = []
    for n in data.get("nodes", []):
        nid   = n.get("id", "")
        ntype = n.get("type") or "concept"
        node_data = n.get("data", {}) or {}
        if not nid or _node_is_archived(node_data) or _node_is_archived(n):
            continue
        name    = node_data.get("label") or node_data.get("name") or nid
        desc    = node_data.get("description") or node_data.get("summary") or ""
        details = node_data.get("details")     or node_data.get("content") or ""
        json_body = "\n\n".join(filter(None, [desc, details]))

        md = _resolve_md(_pick_path(node_data) or _pick_path(n))
        content, source, md_rel = _build_content(
            title=f"{ntype.title()}: {name}", json_body=json_body, md_path=md
        )
        if len(content) < MIN_CONTENT_CHARS:
            continue

        out.append(Chunk(
            id=f"kg:builder:{nid}",
            source_file=path.name,
            node_id=nid,
            node_type=ntype,
            topic=name,
            content=content,
            content_source=source,
            md_path=md_rel,
        ))
    return out


def extract_builder_skills(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Chunk] = []
    skills = data.get("skills") or data.get("nodes") or []
    for s in skills:
        sid = s.get("id", "")
        if not sid or _node_is_archived(s):
            continue
        name = s.get("name") or s.get("label") or sid
        desc = s.get("description") or s.get("summary") or ""
        contains = _text_from_contains(s.get("contains", []))
        json_body = "\n\n".join(filter(None, [desc, contains]))

        md = _resolve_md(_pick_path(s))
        content, source, md_rel = _build_content(
            title=f"Skill: {name}", json_body=json_body, md_path=md
        )
        if len(content) < MIN_CONTENT_CHARS:
            continue

        out.append(Chunk(
            id=f"kg:skill:builder:{sid}",
            source_file=path.name,
            node_id=sid,
            node_type="skill",
            topic=name,
            content=content,
            content_source=source,
            md_path=md_rel,
        ))
    return out


def extract_builder_behaviors(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Chunk] = []
    behaviors = data.get("behaviors") or data.get("nodes") or []
    for b in behaviors:
        bid = b.get("id", "")
        if not bid or _node_is_archived(b):
            continue
        name = b.get("name") or b.get("label") or bid
        desc = b.get("description") or b.get("rule") or b.get("summary") or ""
        rules    = b.get("rules") or ([b["rule"]] if b.get("rule") else [])
        rule_txt = "\n".join(f"- {r}" for r in rules)
        rationale = b.get("rationale", "")
        json_body = "\n\n".join(filter(None, [
            f"Description: {desc}" if desc else "",
            f"Rules:\n{rule_txt}" if rule_txt else "",
            f"Rationale: {rationale}" if rationale else "",
        ]))

        md = _resolve_md(_pick_path(b))
        content, source, md_rel = _build_content(
            title=f"Behavior: {name}", json_body=json_body, md_path=md
        )
        if len(content) < MIN_CONTENT_CHARS:
            continue

        out.append(Chunk(
            id=f"kg:behavior:builder:{bid}",
            source_file=path.name,
            node_id=bid,
            node_type="behavior",
            topic=name,
            content=content,
            content_source=source,
            md_path=md_rel,
        ))
    return out


EXTRACTORS: dict[str, tuple[str, Callable[[Path], list[Chunk]]]] = {
    "domain-behaviors":  ("domain-behaviors.json",        extract_domain_behaviors),
    "domain-skills":     ("domain-skills-graph.json",     extract_domain_skills),
    "domain-docs":       ("domain-docs-graph.json",       extract_domain_docs),
    "builder-knowledge": ("builder-knowledge-graph.json", extract_builder_knowledge),
    "builder-skills":    ("builder-skills-graph.json",    extract_builder_skills),
    "builder-behaviors": ("builder-behaviors-graph.json", extract_builder_behaviors),
}


def collect_chunks(only: str | None = None, verbose: bool = False) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for key, (fname, fn) in EXTRACTORS.items():
        if only and key != only:
            continue
        p = KG_DIR / fname
        if not p.exists():
            print(f"  SKIP (not found): {fname}")
            continue
        try:
            chunks = fn(p)
            md_count = sum(1 for c in chunks if c.content_source == "markdown")
            print(f"  {fname:35}  {len(chunks):4} chunks  ({md_count} from .md, {len(chunks) - md_count} from JSON)")
            if verbose:
                for c in chunks[:3]:
                    print(f"      -> {c.id}  [{c.content_source}]  {c.topic[:60]}")
            all_chunks.extend(chunks)
        except Exception as e:
            print(f"  ERROR {fname}: {e}")
            raise

    # Guard: unique primary keys
    seen: dict[str, Chunk] = {}
    for c in all_chunks:
        if c.id in seen:
            print(f"  WARN duplicate chunk id: {c.id} from {c.source_file}")
        seen[c.id] = c
    return list(seen.values())


# ─── DB ───────────────────────────────────────────────────────────────────────

def ensure_table(conn: Any) -> None:
    if is_mysql():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_embeddings (
                id          VARCHAR(191) PRIMARY KEY,
                source_file VARCHAR(191) NOT NULL,
                node_id     VARCHAR(191),
                node_type   VARCHAR(191),
                topic       LONGTEXT,
                content     LONGTEXT NOT NULL,
                embedding   JSON,
                created_at  DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """)
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_embeddings (
                id          TEXT PRIMARY KEY,
                source_file TEXT NOT NULL,
                node_id     TEXT,
                node_type   TEXT,
                topic       TEXT,
                content     TEXT NOT NULL,
                embedding   TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
    if is_mysql():
        for sql in (
            "CREATE INDEX idx_ke_source ON knowledge_embeddings(source_file)",
            "CREATE INDEX idx_ke_topic ON knowledge_embeddings(topic(191))",
        ):
            try:
                conn.execute(sql)
            except Exception as e:
                if "Duplicate key name" not in str(e):
                    raise
    else:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ke_source ON knowledge_embeddings(source_file)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ke_topic  ON knowledge_embeddings(topic)")
    conn.commit()


def load_existing(conn: Any) -> dict[str, tuple[str, str | None, bool]]:
    """Returns {id: (content, updated_at, has_embedding)} for every existing row."""
    existing: dict[str, tuple[str, str | None, bool]] = {}
    for row in conn.execute(
        "SELECT id, content, updated_at, embedding FROM knowledge_embeddings"
    ).fetchall():
        emb = row[3]
        has_embedding = emb is not None and not (
            isinstance(emb, (bytes, bytearray, memoryview)) and len(emb) == 0
        )
        existing[row[0]] = (row[1] or "", row[2], bool(has_embedding))
    return existing


def load_existing_or_empty(conn: Any) -> dict[str, tuple[str, str | None, bool]]:
    try:
        return load_existing(conn)
    except Exception as e:
        if is_sqlite() and "no such table" in str(e).lower():
            return {}
        raise


def backup_db() -> Path | None:
    if not is_sqlite():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = DB_PATH.with_suffix(f".db.bak_kgsync_{ts}")
    shutil.copy2(DB_PATH, dst)
    return dst


# ─── Diff ─────────────────────────────────────────────────────────────────────

def compute_diff(chunks: list[Chunk], existing: dict[str, tuple[str, str | None, bool]]) -> DiffReport:
    diff = DiffReport()
    new_ids = set()
    for c in chunks:
        new_ids.add(c.id)
        if c.id not in existing:
            diff.to_insert.append(c)
            continue
        old_content, _, has_embedding = existing[c.id]
        hash_mismatch = hashlib.sha256(old_content.encode("utf-8")).hexdigest() != c.content_hash
        if hash_mismatch or not has_embedding:
            diff.to_update.append(c)
        else:
            diff.unchanged.append(c.id)
    # Stale rows still in DB but no longer emitted by any extractor.
    for old_id in existing.keys():
        if old_id not in new_ids:
            diff.to_delete.append(old_id)
    return diff


# ─── Embed ────────────────────────────────────────────────────────────────────

def embed_texts(texts: list[str]) -> list[list[float]]:
    client = _get_openai()
    if client is None:
        raise RuntimeError("OPENAI_API_KEY missing (or openai package unavailable).")
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [item.embedding for item in resp.data]


def embed_chunks(chunks: list[Chunk], no_embed: bool = False) -> dict[str, str | None]:
    """Return {chunk_id: embedding_json_or_None}."""
    out: dict[str, str | None] = {}
    if not chunks:
        return out
    if no_embed:
        for c in chunks:
            out[c.id] = None
        return out
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c.content for c in batch]
        try:
            vectors = embed_texts(texts)
            for c, v in zip(batch, vectors):
                out[c.id] = json.dumps(v)
            print(f"  embedded {i + len(batch)}/{len(chunks)}")
            if i + BATCH_SIZE < len(chunks):
                time.sleep(0.3)
        except Exception as e:
            print(f"  ERROR batch starting at {i}: {e}")
            for c in batch:
                out[c.id] = None
    return out


def log_embedding_cost(conn: Any, n_chunks: int) -> None:
    # text-embedding-3-small: $0.02 / 1M tokens, ~150 tokens/chunk average.
    if n_chunks <= 0:
        return
    est_tokens = n_chunks * 150
    est_cost   = (est_tokens / 1_000_000) * 0.02
    try:
        conn.execute(
            """
            INSERT INTO llm_usage_log
              (timestamp, module, model, prompt_tokens, completion_tokens,
               total_tokens, estimated_cost_usd, success)
            VALUES (?, 'kg_sync.rebuild_embeddings', ?, ?, 0, ?, ?, 1)
            """,
            (datetime.now().isoformat(), EMBED_MODEL, est_tokens, est_tokens, round(est_cost, 6)),
        )
        conn.commit()
    except Exception as e:
        # llm_usage_log may have a different schema in some environments.
        print(f"  (cost log skipped: {e})")
    print(f"  Estimated embedding cost: ${est_cost:.4f} ({est_tokens:,} tokens across {n_chunks} chunks)")


# ─── Apply ────────────────────────────────────────────────────────────────────

def apply_diff(
    conn: Any,
    diff: DiffReport,
    embeddings: dict[str, str | None],
    chunks_by_id: dict[str, Chunk],
) -> None:
    now = datetime.now().isoformat()

    try:
        # Delete stale rows.
        if diff.to_delete:
            placeholders = ",".join("?" * len(diff.to_delete))
            conn.execute(f"DELETE FROM knowledge_embeddings WHERE id IN ({placeholders})", diff.to_delete)

        for c in [*diff.to_insert, *diff.to_update]:
            if is_mysql():
                conn.execute(
                    """
                    INSERT INTO knowledge_embeddings
                      (id, source_file, node_id, node_type, topic, content, embedding, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON DUPLICATE KEY UPDATE
                      source_file = VALUES(source_file),
                      node_id = VALUES(node_id),
                      node_type = VALUES(node_type),
                      topic = VALUES(topic),
                      content = VALUES(content),
                      embedding = VALUES(embedding),
                      updated_at = VALUES(updated_at)
                    """,
                    (c.id, c.source_file, c.node_id, c.node_type, c.topic,
                     c.content, embeddings.get(c.id), now, now),
                )
            else:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO knowledge_embeddings
                      (id, source_file, node_id, node_type, topic, content, embedding, created_at, updated_at)
                    VALUES (
                      ?, ?, ?, ?, ?, ?, ?,
                      COALESCE((SELECT created_at FROM knowledge_embeddings WHERE id = ?), ?),
                      ?
                    )
                    """,
                    (c.id, c.source_file, c.node_id, c.node_type, c.topic,
                     c.content, embeddings.get(c.id), c.id, now, now),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ─── Reporting ────────────────────────────────────────────────────────────────

def write_report(diff: DiffReport, chunks_by_id: dict[str, Chunk], committed: bool) -> None:
    def _chunk_row(c: Chunk) -> dict:
        d = asdict(c)
        d.pop("content", None)  # keep report small
        d["content_hash"] = c.content_hash
        d["content_len"]  = len(c.content)
        return d

    payload = {
        "timestamp":  datetime.now().isoformat(),
        "committed":  committed,
        "summary":    diff.summary(),
        "to_insert":  [_chunk_row(c) for c in diff.to_insert],
        "to_update":  [_chunk_row(c) for c in diff.to_update],
        "to_delete":  diff.to_delete,
        "unchanged_count": len(diff.unchanged),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        print(f"  report: {REPORT.relative_to(REPO_ROOT)}")
    except ValueError:
        print(f"  report: {REPORT}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    _load_env()

    ap = argparse.ArgumentParser(description="Rebuild knowledge_embeddings from the file tree.")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Default. Print diff, don't touch DB or call OpenAI.")
    ap.add_argument("--commit",  action="store_true",
                    help="Actually write to the DB (overrides --dry-run).")
    ap.add_argument("--reset",   action="store_true",
                    help="Truncate knowledge_embeddings before rebuilding.")
    ap.add_argument("--no-embed", action="store_true",
                    help="Insert content without calling OpenAI (embedding=NULL).")
    ap.add_argument("--only", choices=sorted(EXTRACTORS.keys()),
                    help="Restrict to one extractor.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    dry_run = not args.commit
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    print("=== KG Sync: file tree -> knowledge_embeddings ===")
    print(f"Repo:  {REPO_ROOT}")
    print(f"KG:    {KG_DIR}")
    print(f"DB:    {describe_db_target()}")
    print(f"Mode:  {'DRY-RUN' if dry_run else 'COMMIT'}"
          + ("  +reset" if args.reset else "")
          + ("  +no-embed" if args.no_embed else "")
          + (f"  only={args.only}" if args.only else ""))
    print()

    # 1. Collect target chunks.
    print("1) Collecting chunks from file tree...")
    chunks = collect_chunks(only=args.only, verbose=args.verbose)
    print(f"   total: {len(chunks)} chunks")
    chunks_by_id = {c.id: c for c in chunks}

    # 2. Load current DB state. Dry-run must not create tables or indexes.
    conn = connect()
    if not dry_run:
        ensure_table(conn)

    if args.reset and not dry_run:
        print("\n2) --reset: truncating knowledge_embeddings")
        conn.execute("DELETE FROM knowledge_embeddings")
        conn.commit()
        existing: dict[str, tuple[str, str | None]] = {}
    elif args.reset and dry_run:
        print("\n2) --reset requested but in DRY-RUN, skipping truncate")
        existing = load_existing_or_empty(conn)
    else:
        existing = load_existing_or_empty(conn)
    print(f"   existing rows in DB: {len(existing)}")

    # 3. Diff.
    print("\n3) Computing diff...")
    diff = compute_diff(chunks, existing)
    s = diff.summary()
    print(f"   insert: {s['insert']}   update: {s['update']}   unchanged: {s['unchanged']}   delete: {s['delete']}")

    if args.verbose:
        for c in diff.to_insert[:10]:
            print(f"     + {c.id}  [{c.content_source}]  {c.topic[:60]}")
        for c in diff.to_update[:10]:
            print(f"     ~ {c.id}  [{c.content_source}]  {c.topic[:60]}")
        for i in diff.to_delete[:10]:
            print(f"     - {i}")

    # 4. Commit?
    if dry_run:
        print("\n4) DRY-RUN — no changes written. Re-run with --commit to apply.")
        write_report(diff, chunks_by_id, committed=False)
        conn.close()
        return 0

    nothing_to_do = not (diff.to_insert or diff.to_update or diff.to_delete)
    if nothing_to_do:
        print("\n4) Nothing to do. DB already matches file tree.")
        write_report(diff, chunks_by_id, committed=True)
        conn.close()
        return 0

    print("\n4) Backing up DB...")
    backup = backup_db()
    if backup:
        print(f"   backup: {backup.relative_to(REPO_ROOT)}")
    else:
        print("   MySQL target: no local SQLite file backup created.")
        print("   Ensure server-side backup/snapshot policy is in place before production rebuilds.")

    # 5. Embed new/changed chunks.
    to_embed = diff.to_insert + diff.to_update
    print(f"\n5) Embedding {len(to_embed)} chunks...")
    if not to_embed:
        embeddings: dict[str, str | None] = {}
    elif args.no_embed:
        embeddings = {c.id: None for c in to_embed}
        print("   --no-embed: skipping OpenAI calls")
    else:
        if _get_openai() is None:
            print("   ERROR: OPENAI_API_KEY not set.  Pass --no-embed to insert without vectors.")
            conn.close()
            return 2
        embeddings = embed_chunks(to_embed)

    # 6. Apply.
    print("\n6) Applying diff to DB...")
    try:
        apply_diff(conn, diff, embeddings, chunks_by_id)
    except Exception as e:
        print(f"   ERROR: {e}")
        if backup:
            print(f"   DB backup preserved at {backup}")
        conn.close()
        return 3

    if not args.no_embed and to_embed:
        log_embedding_cost(conn, len(to_embed))

    # 6b. Sync kg_nodes / kg_edges + DocShare backlink.
    print("\n6b) Syncing kg_nodes / kg_edges + DocShare backlink...")
    try:
        graph_summary = run_graph_sync(conn, is_mysql=is_mysql(), dry_run=False)
        conn.commit()
        print(
            f"   nodes: +{graph_summary['nodes_inserted']} "
            f"superseded={graph_summary['nodes_superseded']} "
            f"unchanged={graph_summary['nodes_unchanged']} "
            f"edges={graph_summary['edges_written']}"
        )
        print(
            f"   docshare: linked={graph_summary['docshare_linked']} "
            f"unlinked={graph_summary['docshare_unlinked']}"
        )
    except Exception as e:
        print(f"   WARNING: kg_graph sync failed: {e}")

    # 7. Verify.
    final = conn.execute("SELECT COUNT(*) FROM knowledge_embeddings").fetchone()[0]
    with_emb = conn.execute(
        "SELECT COUNT(*) FROM knowledge_embeddings WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    print(f"\n7) DONE.  rows: {final}  (with embedding: {with_emb})")
    write_report(diff, chunks_by_id, committed=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
