# AgentLoom Runtime

Reusable runtime patterns for production AI agents built on the
[AgentLoom](https://github.com/Keven1894/AgentLoom) framework: a layered memory
model, a graph-first knowledge pipeline, and the file-to-database sync that turns
authored knowledge into runtime behavior.

Where **AgentLoom** is the builder-side governance framework (knowledge graphs,
propose-review-accept, Tier-A validators), **AgentLoom Runtime** is the
production-side companion: how a deployed agent *remembers*, *retrieves*, and
keeps its knowledge faithful to what was authored.

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
```

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

## License

Code: [MIT](LICENSE). Documentation and design content may be reused under the
same permissive terms.
