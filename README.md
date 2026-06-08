# AgentLoom Runtime

Reusable runtime patterns for production AI agents built on the
[AgentLoom](https://github.com/Keven1894/AgentLoom) framework: a layered memory
model, a graph-first knowledge pipeline, and the file-to-database sync that turns
authored knowledge into runtime behavior.

Where **AgentLoom** is the builder-side governance framework (knowledge graphs,
propose-review-accept, Tier-A validators), **AgentLoom Runtime** is the
production-side companion: how a deployed agent *remembers*, *retrieves*, and
keeps its knowledge faithful to what was authored.

> **Status:** documentation-first release. The architecture and design
> decisions are published here as reusable patterns. Runtime code modules
> (KG sync, retrieval, RRF fusion) are being extracted and scrubbed in
> subsequent releases.

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

- **[AgentLoom](https://github.com/Keven1894/AgentLoom)** — builder-side
  framework: knowledge-graph governance, validators, propose-review-accept.
- **[co-agenticOS](https://github.com/Keven1894/co-agenticOS)** — governance /
  safety layer: rules, coordination, memory boundaries, verification.
- **AgentLoom Runtime** (this repo) — production-side memory + retrieval
  patterns that an AgentLoom-built agent uses at runtime.

## License

Code: [MIT](LICENSE). Documentation and design content may be reused under the
same permissive terms.
