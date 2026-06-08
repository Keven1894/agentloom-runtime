# Knowledge Graph Sync & Maintenance

**Status:** Reference architecture
**Part of:** AgentLoom Runtime

**Scope:** how the **authoring side** (the repository, edited in an IDE) produces
the knowledge graph, and how the **runtime side** (the agent process + its
database) consumes it. The focus is the **file ↔ DB sync boundary** — the one
place where author-time knowledge becomes run-time behavior.

**Out of scope:** cross-agent knowledge federation between multiple agents.

---

## 1. Two roles, one brain

A deployed agent is typically split across two physical loci that must agree on
what it "knows":

| Side | Role |
|---|---|
| **Authoring** (IDE / repo) | Reads + writes the repo; regenerates knowledge artifacts; does long-horizon thinking. |
| **Runtime** (agent process) | Handles inbound work, plans, dispatches tools; does short-horizon execution. |

Both halves are the same agent, but they cannot share an in-process object
graph. Their only shared state is:

- **The file tree** (version-controlled, human-editable) — the **authoring
  source**.
- **A `knowledge_embeddings` table** — the **execution source** that semantic
  retrieval queries at runtime to inject context into prompts.

Reading raw markdown on every model call is too slow, so the table must exist —
but it must also stay **faithfully in sync with the files**, or the two halves
start to disagree and autonomous behavior degrades silently.

**The sync is strictly one-way: files → DB.** The layers don't merge; they
agree.

---

## 2. The authoring layer: JSON graphs

Knowledge is authored as a set of JSON graphs plus the markdown files they
reference. A common split is by **track**:

- **Domain** knowledge — what the runtime agent needs to execute correctly
  (skills it invokes, documents it quotes back, behaviors it must obey).
- **Builder** knowledge — what an authoring session needs to edit the repo
  correctly (how to maintain the graph, structure plans, follow conventions).

Both tracks embed into the same table and are searchable by the same function;
the split just helps authors reason about "is this for runtime, or for me while
editing?"

Each node typically carries an id, a display name, a `path`/`source_file` field,
a description, a category, and a status. The path field is the hook that lets the
sync step pick the richer markdown body over the thin JSON metadata when building
an embedding chunk.

---

## 3. The execution layer: `knowledge_embeddings`

A representative schema:

```sql
CREATE TABLE knowledge_embeddings (
    id          TEXT PRIMARY KEY,   -- 'kg:<kind>:<node_id>'
    source_file TEXT NOT NULL,      -- which JSON graph this row came from
    node_id     TEXT,               -- node id inside that graph
    node_type   TEXT,               -- skill | document | behavior | concept | pattern | component
    topic       TEXT,               -- display name
    content     TEXT NOT NULL,      -- clipped body (≈1500 tokens)
    embedding   TEXT,               -- serialized embedding vector
    created_at  TEXT,
    updated_at  TEXT
);
```

### How the runtime reads it

Two retrieval modes, selected automatically:

1. **Vector mode** (when `embedding` is populated): embed the query with the
   same model used at write time, rank candidate rows by cosine similarity.
2. **Keyword fallback** (when embeddings are missing, e.g. offline dev): a SQL
   `LIKE` against `topic` + `content`.

Top-k hits are formatted and spliced into the planner or handler system prompt.
Because every inbound item flows through this channel, **the knowledge graph is
a hard dependency of autonomous execution**, not an optional augmentation.

### Public retrieval contract

Expose curated retrieval as tools:

- `kg/search` — semantic search over `knowledge_embeddings`.
- a summary tool to browse high-level graph structure.
- a node-fetch tool to retrieve a known node by id.

Use `kg/search` for **stable** knowledge (architecture, procedures, standards,
durable decisions, reusable skills/behaviors). Do **not** use it as the source of
truth for current operational state — query structured runtime tools first and
use message semantic memory only for narrative context.

---

## 4. The sync bridge

A minimal sync surface is three responsibilities:

| Responsibility | Role |
|---|---|
| Rebuild | Diff-based rebuild. Dry-run by default; an explicit `--commit` writes. |
| Validate | Read-only drift report. Non-zero exit if anything is off. |
| Last-run record | Machine-readable report of the most recent rebuild (inserted / updated / deleted). |

### How the rebuild works

1. **Collect chunks.** Walk the JSON graphs. For each non-archived node:
   - Compute a stable id: `kg:<kind>:<node_id>`.
   - If the node points at a real `.md` file, read that body as the embedding
     content; otherwise derive a fallback from the JSON fields.
   - Clip to ≈1500 tokens.
   - Hash the content (SHA-256) — this is the **drift key**.

2. **Diff against DB state.** For every chunk id:
   - id not in DB → **insert** (embed + write).
   - id in DB, hash changed → **update** (re-embed, replace).
   - id in DB, hash matches → **skip** (no embedding call, no write).
   - row in DB, id no longer collected → **delete** (node was removed).

3. **Back up + write atomically.** On `--commit`, back up the database first,
   then apply all inserts/updates/deletes inside one transaction.

### Validation

A separate read-only validator reports: row coverage vs collected chunks,
rows missing embeddings, stale rows, and content drift (hash mismatch between a
DB row and its current source file). It should exit non-zero on any discrepancy
so it can gate CI.

---

## 5. Why faithfulness matters

The most common silent failure is **content drift**: a source file changes but
the embedding row is not updated (a missed write path), so retrieval returns
stale content while the file on disk looks correct. Complete row *coverage* is
not the same as *faithful content*. Always validate the drift key, not just the
row count, before trusting the layer.

## Related

- [`three-layer-memory-architecture.md`](three-layer-memory-architecture.md)
- [`adr-001-kg-as-engine.md`](adr-001-kg-as-engine.md)
