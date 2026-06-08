# ADR-001: Knowledge Graph as Engine — Graph-First Memory Pipeline

**Status:** Accepted
**Part of:** AgentLoom Runtime

---

## Context

An agent's memory architecture often models `skill`, `behavior`,
`decision_rule`, and graph edges as if they drive runtime control flow. In
practice, many deployments still read JSON files directly or treat a
`knowledge_embeddings` table as a flat vector store with no graph traversal and
no node attributes in the database. The symbolic graph becomes documentation,
not an engine.

The intended design is **agent memory management** along a single trajectory:

```text
read docs → build index → KG as index → embed for semantics → tier the index → distil into a fine-tuned agent
```

Reversed at retrieval time, every consumer — both *understanding* (context
assembly) and *policy gates* — uses **one graph-first pipeline**:

```text
index + node metadata → KG graph query → relevant nodes → node attributes/semantics → output
```

Embedding is the **fuzzy entry**; `node_id` is the **exact entry**; graph edges
expand context; node attributes feed gates and prompt assembly.

---

## Decision

### 1. The knowledge graph is the engine via a graph-first pipeline

- Sync node **attributes** (e.g. `risk_triggers`, `allowlisted_mutations`,
  `decision_rule`, source paths) and **edges** into relational tables
  (`kg_nodes`, `kg_edges`) via the kg-sync job.
- Runtime reads the **database only** — never the authoring JSON files at
  gate/retrieval time.
- `load_decision_rule()` is a **deterministic exit** of a graph lookup
  (`get_node`), not a separate parser or file read.
- **Regex remains a hard floor** for safety-critical gates. The graph *adds*
  triggers; a graph outage must **never** widen an auto-handle path beyond the
  regex floor.

### 2. Deterministic lane routing keyed on `skill_id`

- Do **not** use semantic search to *pick* which skill runs on safety-critical
  lanes.
- Keep regex / category / an ordered route table. Every lane records an explicit
  `skill_id`.

### 3. No runtime file reads for the graph

- Files are the authoring source; kg-sync is the only file → DB writer for graph
  data.
- Display surfaces may read original document text; **policy/graph reads may
  not** hit the JSON.

### 4. Temporal versioning on `kg_nodes`

- On content change: set `superseded_at` on the old row, insert a new current
  row.
- Runtime reads `WHERE superseded_at IS NULL`. Operational facts are not
  hard-deleted.

### 5. Document-store ↔ graph join

- Join key: a normalized repo-relative `source_path`.
- Cross-reference ids in each store's metadata link the two embedding islands
  without re-embedding.

---

## Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Graph as reference-only; regex as truth | Cements a deployment shortcut; contradicts the curated-knowledge design. |
| "Two paths" (semantic vs structured) | Wrong model — one pipeline, two *entry* modes (vector / node_id). |
| Dedicated graph DB (Neo4j, etc.) | A few hundred nodes + 2–3 hop SQL is sufficient; operational overhead unjustified. |
| Auto entity/relation extraction | The graph is authored; extraction adds noise for small-team curation. |
| TTL auto-forget on episodic memory | Low volume; audit value exceeds storage cost. |

---

## Consequences

**Positive:**

- Policy changes ship via a graph edit + kg-sync, not a code deploy.
- Auditable provenance: a node/edge path from a gate decision back to the source
  behavior.
- Foundation for tiering (curated / indexed / stub) and eventual fine-tune
  distillation.

**Negative / cost:**

- kg-sync must maintain `kg_nodes` + `kg_edges` alongside embeddings.
- A runtime restart is required after sync in production.
- The graph schema migration must be applied before the first graph sync.

## Related

- [`three-layer-memory-architecture.md`](three-layer-memory-architecture.md)
- [`kg-sync-and-maintenance.md`](kg-sync-and-maintenance.md)
