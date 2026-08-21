# AgentLoom Runtime

Reusable runtime patterns for production AI agents built on the
[AgentLoom](https://github.com/Keven1894/AgentLoom) framework: a layered memory
model, a graph-first knowledge pipeline, and the file-to-database sync that turns
authored knowledge into runtime behavior.

Where **AgentLoom** is the builder-side governance framework (knowledge graphs,
propose-review-accept, Tier-A validators), **AgentLoom Runtime** is the
production-side companion: how a deployed agent *remembers*, *retrieves*, and
keeps its knowledge faithful to what was authored. Extracted from production
research data lifecycle agent deployments (EnviStor); this repo ships
framework-neutral library code only.

> **Status:** v0.1.0 library release. Installable modules: `db`, `memory`, `kg`,
> `fair`, `quality`, plus CORE MySQL migrations. See [CHANGELOG.md](CHANGELOG.md).

## Quickstart

```bash
git clone https://github.com/Keven1894/agentloom-runtime.git
cd agentloom-runtime
python -m venv .venv

# Windows
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m pytest -q

# macOS/Linux
# source .venv/bin/activate && pip install -e .[dev] && pytest -q
```

Or with Make: `make install && make test`

### Database

Apply CORE migrations (MySQL 8+) before using memory/KG modules:

```bash
mysql -h "$AGENTLOOM_DB_HOST" -u "$AGENTLOOM_DB_USER" -p "$AGENTLOOM_DB_NAME" \
  < migrations/mysql/001_core_schema.sql
mysql ... < migrations/mysql/002_memory_embeddings.sql
mysql ... < migrations/mysql/003_kg_graph.sql
mysql ... < migrations/mysql/004_session_memory.sql
mysql ... < migrations/mysql/005_session_transcripts.sql
mysql ... < migrations/mysql/006_session_transcript_index.sql
mysql ... < migrations/mysql/007_session_lineage.sql
mysql ... < migrations/mysql/008_session_transcript_vectors.sql
mysql ... < migrations/mysql/009_drop_json_embeddings.sql
```

`004_session_memory.sql` is standalone: apply it on its own if you only want
Layer 0 session continuity. `005_session_transcripts.sql` adds the conversation
archive (requires 004). `006_session_transcript_index.sql` adds the archive
locator (requires 005). `007_session_lineage.sql` adds session DAG topology
(requires 004). `008_session_transcript_vectors.sql` adds compact float32
embedding storage (requires 006).

`009_drop_json_embeddings.sql` removes the superseded JSON embedding column.
**Upgrading an existing archive**: apply 008, run `agentloom-session compact`
until it reports zero conversions, then apply 009 and `OPTIMIZE TABLE
session_transcript_chunks` — `DROP COLUMN` leaves the freed pages inside the
tablespace. On a fresh install there is nothing to compact and the two can be
applied together.

Configure connection:

```bash
export AGENTLOOM_REPO_ROOT=/path/to/your/agent   # KG JSON + docs live here
export AGENTLOOM_DB_HOST=127.0.0.1
export AGENTLOOM_DB_NAME=app_db
export AGENTLOOM_DB_USER=runtime
export AGENTLOOM_DB_PASSWORD=...
# or: export DATABASE_URL=mysql://runtime:...@127.0.0.1:3306/app_db
```

Optional embedding provider (vector search):

```bash
export OPENAI_API_KEY=...
export EMBEDDING_MODEL=text-embedding-3-small
pip install -e ".[dev,embeddings]"
```

### Where configuration comes from

Every module resolves configuration through `agentloom_runtime.config`, so the
database adapter and the embedding provider can never disagree about where
settings live. Precedence:

1. The process environment. A variable already set always wins.
2. `AGENTLOOM_ENV_FILE`, if set — use this when a service or scheduled task
   runs outside the repository directory.
3. Otherwise a `.env` found by walking up from the working directory.

A `.env` only fills gaps, so a stray file cannot override injected production
configuration.

> Put `OPENAI_API_KEY` where the *runtime* will find it, not only where your
> indexing job found it. If the key resolves at index time but not at query
> time, the archive embeds fine and every search quietly falls back to
> lexical-only: no error, plausible results, worse ranking.

### Verify the install

The `agentloom-session` commands are console scripts. If they are missing, the
bootstrap instructions given to agents silently do nothing:

```bash
agentloom-session --help          # console script must resolve on PATH
python -c "import agentloom_runtime; print(agentloom_runtime.__file__)"
```

Confirm that path is the checkout you are editing. A stale `pip install -e`
pointing at a second clone imports the wrong tree, and every symptom of it
looks like a missing feature.

## Python API

### Database

```python
from agentloom_runtime.db import connect

with connect() as conn:
    row = conn.execute("SELECT 1 AS ok").fetchone()
    assert row["ok"] == 1
```

### Memory & retrieval

```python
from pathlib import Path
from agentloom_runtime.memory import (
    register_embedding_sync_marker,
    search_kg_docshare_joint,
)

register_embedding_sync_marker("kg", Path("Scripts/kg_sync/last_run.json"))
register_embedding_sync_marker("docshare", Path("Scripts/docshare/last_embeddings_run.json"))

kg_hits, doc_hits, mode = search_kg_docshare_joint("how does KG sync work?")
```

### KG search & sync

```python
from agentloom_runtime.kg import search_kg, format_for_prompt

hits = search_kg("how do I validate the knowledge graph?", top_k=5)
prompt_block = format_for_prompt(hits)
```

CLI-style sync (from repo root with `agents/knowledge-graphs/`):

```bash
export AGENTLOOM_REPO_ROOT=$PWD
python -m agentloom_runtime.kg.sync.rebuild              # dry-run (default)
python -m agentloom_runtime.kg.sync.rebuild --commit     # write to DB
python -m agentloom_runtime.kg.sync.validate
python -m agentloom_runtime.kg.sync.run_graph_sync
```

### Cross-host session memory (Layer 0)

Resume work on a repository from a different machine or a different IDE. Session
identity comes from the VCS remote, so no editor-local path or private chat
store is ever consulted.

```bash
export AGENTLOOM_AGENT_ID=my-builder

agentloom-session resume                                    # what was I doing here?
agentloom-session checkpoint --next "Apply migration to dev" --plan docs/plan/x.md

# Fork session into a new branch across machines
agentloom-session open --fork-from <session_id> --reason host_switch
agentloom-session tree                                      # view ASCII DAG tree
```

Checkpoints are deliberately terse — they load on every resume. When you need
the actual conversation behind one, the archive holds it:

```bash
agentloom-session archive --all      # capture this host's conversations (read-only)
agentloom-session index --all        # locate later: prose windows + optional embeddings
agentloom-session search "password policy"
agentloom-session replay --ref <id> --around 14
```

Capture reads the local editor's transcript store, so it has to run on each
machine that talks to an agent — no server-side process can do it for you.
Schedule `archive --all && index --all` on every such host; both are idempotent
and store by content hash, so re-running costs a scan rather than duplicate
rows.

#### Dynamic recall for agents (MCP Server)
AI coding agents (Cursor, Cline, OpenCode, Claude Desktop) can dynamically recall past conversations on demand:

```bash
agentloom-session-mcp                # or: agentloom-session mcp
```
Provides tools: `session_search`, `session_get_context`, `session_get_checkpoint`, `session_get_lineage`.

#### Session Web Viewer
Launch the zero-dependency, local visual dashboard:

```bash
agentloom-session ui                 # opens http://127.0.0.1:8766
```
Visualizes session DAG trees, interactive turn-by-turn conversation replays, collapsible tool calls, and instant search.

The locator indexes human and agent prose only (tool-call noise is dropped), at
two granularities, and ranks with hybrid lexical + vector search. It returns
pointers; `replay --around` pages the archive at that seq. Durable decisions
still belong in the knowledge graph — this finds the discussion.

Conversations are captured by per-host read-only readers, redacted for
credential-shaped content before storage, and rendered as text, Markdown, or
JSON. What this does *not* do is repaint them as native chat bubbles in an
editor's own sidebar — that would mean writing a private chat store that is
undocumented, path-keyed, and held open by the running editor.

Same thing from Python:

```python
from agentloom_runtime.session import detect_workspace_key, render_resume_pack, resume

pack = resume("my-builder", "alice", detect_workspace_key())
print(render_resume_pack(pack))
```

To make that automatic in whatever editor you use, write the bootstrap
instruction once and generate each host's rule file from it:

```bash
agentloom-hostrules sync     # one source -> AGENTS.md, editor rule files, …
agentloom-hostrules check    # fail if any drifted (CI / pre-commit)
```

A target is a path plus optional front matter, so supporting a new IDE is a
manifest entry rather than a code change.

Design, host-neutrality invariants, and the host adapter conformance checklist:
[`docs/memory/layer-0-session-memory.md`](docs/memory/layer-0-session-memory.md).

### FAIR compliance

```python
from agentloom_runtime.fair import calculate_fair_compliance

result = calculate_fair_compliance(metadata_dict)  # Dataverse-style JSON
print(result.overall_score, result.overall_status)
```

### Quality checks

```python
from agentloom_runtime.quality import run_health_check, validate_all_schemas

passed, failed, _ = validate_all_schemas()
report = run_health_check()
print(report.overall_status)
```

## Package layout

| Module | Purpose |
|--------|---------|
| `agentloom_runtime.db` | MySQL connection adapter |
| `agentloom_runtime.session` | Layer 0 working-session memory: cross-host, IDE-independent resume |
| `agentloom_runtime.hostrules` | One bootstrap instruction generated into every host's rule file |
| `agentloom_runtime.memory` | In-process embedding indexes + RRF joint retrieval |
| `agentloom_runtime.kg` | Semantic KG search + file→DB sync pipeline |
| `agentloom_runtime.fair` | FAIR metadata compliance calculator |
| `agentloom_runtime.quality` | KG JSON Schema + integrity validators |
| `migrations/mysql/` | CORE operational schema (no instance-specific tables) |

## Why this exists

A capable agent needs to answer two very different kinds of question:

1. **Knowledge questions** — "What is the accepted architecture or procedure?"
2. **Management questions** — "What is happening right now? What is the status?"

Putting both into one undifferentiated vector store is a common mistake. Stable
knowledge, live operational state, conversation history, and planning provenance
have different freshness rules, ownership, and trust levels. This project
documents a memory architecture that separates them, and a single graph-first
retrieval pipeline that serves them.

## Contents

| Doc | What it covers |
|-----|----------------|
| [`docs/memory/three-layer-memory-architecture.md`](docs/memory/three-layer-memory-architecture.md) | The layered memory model: curated knowledge, management state, and plan/provenance — with a retrieval router and freshness rules. |
| [`docs/memory/layer-0-session-memory.md`](docs/memory/layer-0-session-memory.md) | Cross-host working-session continuity: checkpoints vs. the transcript archive, why not to write the editor's chat database, the host-neutrality invariants, and the host adapter contract. |
| [`docs/memory/adr-001-kg-as-engine.md`](docs/memory/adr-001-kg-as-engine.md) | Architecture decision: the knowledge graph is the runtime *engine* via one graph-first pipeline, not a documentation-only artifact. |
| [`docs/memory/kg-sync-and-maintenance.md`](docs/memory/kg-sync-and-maintenance.md) | The one-way file → database sync contract that keeps authored knowledge and runtime behavior in agreement. |
| [`docs/agents/operational-agent-productization-playbook.md`](docs/agents/operational-agent-productization-playbook.md) | An 8-step process for turning a manual workflow into a governed, token-protected, dispatchable MCP agent. |

## Relationship to the ecosystem

Four repos, four roles — split by *what they are* and by *agent lifecycle phase*:

| Repo | Role | Lifecycle phase |
|------|------|-----------------|
| [**co-agenticOS**](https://github.com/Keven1894/co-agenticOS) | Governance **spec** — rules, coordination, memory boundaries, verification | sets the rules |
| [**AgentLoom**](https://github.com/Keven1894/AgentLoom) | Build **framework** — knowledge-graph governance, validators, propose-review-accept | **authoring time** (build & govern the agent) |
| **AgentLoom Runtime** (this repo) | Runtime **library** — layered memory, graph-first retrieval, file→DB sync | **run time** (the deployed agent remembers & retrieves) |
| [**ucgis-agentloom-2026-workshop**](https://github.com/Keven1894/ucgis-agentloom-2026-workshop) | A concrete **instance** — a worked, forkable example built on the framework | a use of all of the above |

In one line:

> **co-agenticOS** sets the rules → **AgentLoom** is the framework you build and
> govern an agent with (authoring time) → **AgentLoom Runtime** is the library the
> deployed agent uses to remember and retrieve (run time) → the **workshop** repo
> is one concrete instance that puts all of them to work.

The key boundary is **AgentLoom (authoring time) vs AgentLoom Runtime (run
time)**: AgentLoom is how you *build and govern* an agent's knowledge; this repo
is what the agent *uses while running in production*.

## Contributing

See [`CONTRIBUTORS.md`](CONTRIBUTORS.md). Report bugs or request features via [GitHub Issues](https://github.com/Keven1894/agentloom-runtime/issues/new/choose).

## License

Code: [MIT](LICENSE). Documentation and design content may be reused under the
same permissive terms.
