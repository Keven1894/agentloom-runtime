# AgentLoom Runtime

Reusable runtime patterns for production AI agents built on the
[AgentLoom](https://github.com/Keven1894/AgentLoom) framework: a layered memory
model, a graph-first knowledge pipeline, and the file-to-database sync that turns
authored knowledge into runtime behavior.

Where **AgentLoom** is the builder-side governance framework (knowledge graphs,
propose-review-accept, Tier-A validators), **AgentLoom Runtime** is the
production-side companion: how a deployed agent *remembers*, *retrieves*, and
keeps its knowledge faithful to what was authored.

> **Status:** early library release (v0.1.0). The `agentloom_runtime.db` module
> is installable today; memory and KG sync modules are landing next.

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

Configure MySQL via environment variables:

```bash
export AGENTLOOM_DB_HOST=127.0.0.1
export AGENTLOOM_DB_NAME=app_db
export AGENTLOOM_DB_USER=runtime
export AGENTLOOM_DB_PASSWORD=...
# or: export DATABASE_URL=mysql://runtime:...@127.0.0.1:3306/app_db
```

```python
from agentloom_runtime.db import connect

with connect() as conn:
    row = conn.execute("SELECT 1 AS ok").fetchone()
    assert row["ok"] == 1
```

Or with Make: `make install && make test`

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
