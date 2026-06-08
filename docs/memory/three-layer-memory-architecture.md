# Three-Layer Memory Architecture

**Status:** Reference architecture
**Part of:** AgentLoom Runtime

## Purpose

A production agent needs a memory system that can answer two different kinds of
question:

1. **Knowledge questions** — "What is the accepted architecture or procedure?"
2. **Management questions** — "What is this person or agent doing now, what is
   the task status, and what happened recently?"

These should not share one undifferentiated vector store. Stable knowledge, live
operational facts, conversation history, and planning provenance have different
freshness rules, ownership, and retrieval needs.

This document defines a deployable memory architecture: what belongs in a
**curated knowledge graph**, what belongs in a **runtime database**, what belongs
in **conversation/message semantic memory**, and how **planning records** should
be treated as manager-level provenance.

## External patterns this builds on

Current agent-memory practice generally separates memory by role rather than
putting everything into a single retrieval index:

- LangGraph's memory guidance distinguishes short-term thread state from
  long-term memory, and classifies long-term memory as **semantic** facts,
  **episodic** experiences, and **procedural** instructions — writable either on
  the hot path or asynchronously in the background.
- Retrieval guidance from major model providers treats vector stores as
  semantic-search indexes with ranking, score thresholds, hybrid search,
  chunking, and metadata/attribute filtering.
- MemGPT frames long-context agents as systems with tiered memory and explicit
  movement between prompt context and external storage; its recall/archival
  split is a useful analogy for separating message history from durable
  knowledge.

The contribution here is a clear **product boundary** between layers and a
**retrieval router** that picks the right layer by intent.

## Product boundary

A deployed agent uses three memory layers:

1. **Curated knowledge layer** — stable institutional knowledge.
2. **Management layer** — live operational state.
3. **Plan / provenance layer** — manager-level records and historical design
   provenance.

These layers are exposed through tools and internal services that run in
production, independent of any IDE-local developer configuration. Editor-native
rules and skills (e.g. files an author keeps in their IDE to work on the agent)
are developer ergonomics and **must not** be required for the deployed agent's
runtime behavior.

---

## Layer 1: Curated knowledge

**Purpose:** stable institutional knowledge.

This layer answers:

- "What is the architecture?"
- "What is the accepted procedure?"
- "What standard or reusable design applies here?"
- "Which canonical document should I read?"

### Implementation shape

The curated knowledge graph is stored as a set of JSON graphs plus the markdown
files they reference. A sync job reads those graph nodes and writes semantic
chunks into a `knowledge_embeddings` table:

```text
knowledge-graphs/*.json
        +
referenced markdown files
        |
        v
   kg-sync (file → DB)
        |
        v
   knowledge_embeddings (DB)
        |
        v
   search_kg() semantic retrieval
```

### What belongs here

- Canonical architecture documents.
- Procedures that should be followed repeatedly.
- Standards and policies.
- Durable design decisions.
- Reusable skills and behaviors that are intentionally curated.

### What does not belong here

- Every markdown file in the repository.
- Raw task progress.
- Conversation history.
- Personal / editor-local configuration.
- Temporary plans not yet promoted into canonical knowledge.

### Exposure

Expose a `kg/search` tool backed by `search_kg()` so external agents and
developer sessions can query curated knowledge without filesystem search.

---

## Layer 2: Management memory

**Purpose:** human-in-the-loop agentic workflow management.

This layer answers:

- "What task is this worker agent working on now?"
- "What is the progress on this task?"
- "Who owns this project?"
- "What blockers were reported recently?"
- "Why did this task change direction?"

Management memory has two complementary parts: structured facts and semantic
message memory.

### 2.1 Structured runtime facts

Structured facts live in relational tables such as `projects`, `tasks`, `users`,
`teams`, `messages`, `message_replies`, and agent ownership/status tables.

These are the **source of truth for current state**. If a user asks "what is the
current status?", the agent should query the database first, not infer state from
an old plan or an embedding hit.

Structured retrieval should use typed tools or dashboard APIs: project lookup,
task lookup, agent/user lookup, open-message triage, and recent activity by
project, task, or agent.

### 2.2 Message semantic memory

Message semantic memory captures the narrative and reasoning *around* structured
facts, stored in a `message_embeddings` table refreshed from messages and
replies.

It answers questions like:

- "What did someone say about a given topic last week?"
- "Which messages mention a particular policy or decision?"
- "What did a worker report as blocked?"
- "What is the discussion history behind this task?"

Expose this through a `messages/search_semantic` tool with filters for agent,
project, task, category, status, date range, and `top_k`. Responses should
include message and reply ids, scores, metadata, excerpts, and links back to the
detail view.

**Freshness note:** message embeddings must be validated and drift-repaired.
Complete row coverage is not enough — chunk content can drift from the source if
an update path is missed. Repair drift before promoting this layer as a
first-class retrieval source.

---

## Layer 3: Plan / provenance memory

**Purpose:** manager-level operational record and historical design provenance.

A `plans/` directory is **not** canonical knowledge by default. It contains
todos, completion reports, checkpoints, learning records, and execution notes.
It is valuable, but it should be searched as provenance, not mixed into curated
knowledge.

This layer answers:

- "Did we ever plan something like this before?"
- "Where is the completion report for that migration?"
- "What old decision led to this implementation?"
- "Which plan produced this canonical architecture?"

### Plan registry

Maintain a lightweight registry for planning documents with fields such as
`path`, `title`, `date`, `status`, `owner`, `project_id`, `task_id`,
`lifecycle` (`todo` / `completed` / `checkpoint` / `learning` / `archive`),
`tags`, `durable_knowledge` (bool), and `promoted_to` (canonical doc path, if
any).

### Plan embeddings

Use a separate `plan_embeddings` index. **Do not** write plan chunks into
`knowledge_embeddings` by default. Plan search should support both metadata/tag
filtering (for manager workflows) and semantic search (for fuzzy historical
recall).

### Promotion policy

If a plan produces durable knowledge, extract that knowledge into a canonical
document under the appropriate long-term location, index it in the curated
knowledge layer, and leave the plan as provenance/backlink.

---

## Retrieval router

Use an explicit router rather than one global search function:

| User question | First source | Follow-up source |
|---|---|---|
| "What is the canonical design?" | `kg/search` | canonical doc read |
| "What task is X doing?" | structured DB task/agent query | message semantic memory |
| "What did they report recently?" | message list / dashboard API | message semantic memory |
| "Why did this project change direction?" | message semantic memory | plan/provenance search |
| "Did we plan this before?" | plan/provenance search | curated KG if promoted |
| "How should the agent behave in deployment?" | curated KG / runtime config | not editor-local rules |

The router should **preserve source identity** in every answer. The agent should
say whether a claim came from current DB state, a message, a plan, or canonical
knowledge.

## Write paths and freshness rules

| Layer | Written by | Trust | Frequency |
|---|---|---|---|
| Curated knowledge | manual doc/KG maintenance, rebuilt via kg-sync | high | low |
| Management DB | runtime tools, dashboards, approved admin ops | source of truth for current state | high |
| Message semantic | automatically from messages/replies, async refresh | medium (narrative, not authoritative) | high |
| Plan / provenance | humans/sessions creating plans + completion reports | medium (valuable history, may be stale) | medium |

## Anti-patterns

- Do not put all repository docs into `knowledge_embeddings`.
- Do not treat the `plans/` directory as canonical architecture.
- Do not answer "current status" from semantic search alone.
- Do not depend on editor-local rules for deployed runtime behavior.
- Do not use embeddings where a structured DB query is the authoritative path.
- Do not hide source provenance in synthesized answers.

## Implementation roadmap

1. Productize `kg/search` for curated knowledge.
2. Repair message-embedding drift and expose `messages/search_semantic`.
3. Design structured management queries for "who is doing what" workflows.
4. Build a plan registry and `plan_embeddings`.
5. Add a promotion workflow from plan/provenance into canonical docs.
6. Implement a retrieval router that queries the right layer by intent.
7. Add source-aware answer formatting so users can see the origin of each claim.

## Related

- [`adr-001-kg-as-engine.md`](adr-001-kg-as-engine.md) — the graph-first pipeline decision.
- [`kg-sync-and-maintenance.md`](kg-sync-and-maintenance.md) — the file → DB sync contract.
