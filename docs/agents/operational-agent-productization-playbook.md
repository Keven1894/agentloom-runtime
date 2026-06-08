# Operational Agent Productization Playbook

**Status:** Reference process
**Part of:** AgentLoom Runtime

## Purpose

A reusable process for turning a mostly manual operational workflow into a
governed, discoverable, token-protected MCP agent — for example DevOps, storage,
publication, compute, or orchestration agents.

The goal is not just "deploy another MCP server." The goal is to productize a
manual workflow into an agent that is **bounded, self-describing,
token-protected, auditable, dispatchable** from a coordinating agent, and
**promotable** into a curated knowledge layer.

## Core principle

Do not start from "what tools should the agent have?" Start from:

1. what manual workflow already exists,
2. what part of it is stable,
3. what part is read-only vs write,
4. what evidence and approvals are required,
5. what knowledge should become durable and reusable across agents.

Productization is therefore a pipeline:

```text
manual procedure
-> stable library / script surface
-> read-only MCP surface
-> auth + audit
-> dispatch integration
-> canonical architecture / knowledge description
-> write tools only after approvals and evidence are ready
```

## The reusable 8-step flow

### 1. Define the bounded capability

Write down what the agent is and is not responsible for, what inputs it accepts,
what outputs it returns, what is read-only vs write-capable, and what external
systems it touches.

### 2. Extract a stable library or CLI first

Do not let the first implementation live inside an MCP endpoint. First create
shared library functions, idempotent scripts, stable CLI commands, and
structured JSON output. The MCP layer should *wrap* these, not become the
primary location of business logic.

### 3. Build read-only MCP first

The recommended first surface is `initialize`, `resources/list`,
`resources/read`, `tools/list`, and **read-only** `tools/call`. Typical
read-only operational tools: inventory/list, semantic search over inventory,
preflight/plan assembly, status/health/capability discovery. This creates
discoverability and safe integration before any mutation exists.

### 4. Add a self-describing resource layer

Every operational agent should have a `welcome` resource, a resource index, a
tool catalog, governance notes, JSON-RPC examples, and explicit
phase/current-limit statements — for both human onboarding and FAIR-style
cross-agent discoverability.

### 5. Add token protection before opening the surface

Knowing the MCP URL must never be sufficient for access.

- `/health`: unauthenticated, minimal, no operational disclosure.
- `/mcp`: fully token-protected, token via `Authorization: Bearer <token>`.

Tokens must **not** appear in git, docs, knowledge-graph nodes, MCP resources,
prompts, or normal logs.

### 6. Add a database-backed token lifecycle and audit

A static `.env` token is not enough. Establish a token registry table, an MCP
call audit log, a future action/evidence log, token admin scripts, and an audit
report. This enables tracking who called what, revoking/rotating access, proving
approval/evidence chains, and investigating misuse.

### 7. Wire the agent into the coordinator's dispatch

An MCP endpoint is not truly part of the system until the coordinating agent can
call it through the standard outbound path: `ping`, `discover`,
`read_resource`, `call_tool` — with the token kept server-side. This usually
requires a token-aware MCP client, token lookup by agent name in the dispatcher,
and a verified agent registration record.

### 8. Promote the stable description into curated knowledge

A deployed runtime is not enough; the system also needs durable semantic
knowledge about the agent. Create a canonical architecture/operations document
that answers what the agent is, where it runs, how it is called, what it
exposes, what boundaries it obeys, and what its approval/audit model is — then
index it into the curated knowledge layer if it represents stable knowledge.

## Reusable vs agent-specific

| Reusable across many agents | Agent-specific |
|---|---|
| MCP server skeleton | Tool handlers and schemas |
| `initialize` / `resources/*` / `tools/*` handler pattern | External systems touched |
| Welcome / resource-index pattern | Risk tiers and approvals |
| Token auth boundary | Evidence schema |
| Token registry + audit logging tables | Smoke tests |
| Token admin scripts | Rollback logic |
| Coordinator dispatch token support | |
| Canonical architecture doc pattern | |

## Example productization sequences

**Deployment agent**

```text
manual deploy/update/remove workflow
-> stable deploy helper library
-> read-only MCP: list/search/preflight
-> token auth -> token registry + audit log
-> dispatch integration -> architecture/knowledge description
-> future write tools with approval + evidence
```

**Storage agent**

```text
existing proxied storage tools
-> lift and own data-plane logic in the new agent
-> read-only inspection endpoints first
-> token auth + audit -> dispatch integration
-> deprecate the proxy path after stabilization
```

**Compute / staging agent**

```text
manual namespace/job/staging procedure
-> stable submit/monitor/stage library
-> read-only inspect/preflight tools
-> token auth + audit -> action log for submit/cancel/retry
-> dispatch + knowledge integration
```

**Publication agent**

```text
manual publish/report-back workflow
-> stable publication primitives
-> read-only inventory/status/search
-> token auth + audit
-> write/publish tools with evidence + result logging
-> dispatch + knowledge integration
```

## Recommended standard deliverables

Every new operational agent should ideally produce: a repo with a stable
library/CLI layer; an MCP server (read-only first); a `welcome` resource; token
auth; a token registry + audit logging; token admin scripts; direct MCP smoke
tests; dispatch smoke tests; a canonical architecture doc; and a
knowledge-sync/update plan.

## Which memory layer gets what

These artifacts do not all belong in the same memory layer (see
[`../memory/three-layer-memory-architecture.md`](../memory/three-layer-memory-architecture.md)):

- **Curated knowledge:** this playbook, stable per-agent architecture docs,
  reusable skills/behaviors distilled from repeated patterns.
- **Plan / provenance (not curated by default):** one-off migration todos,
  rollout-specific checklists, temporary blockers, local sequencing notes.

## Why this matters

Without a playbook, every new operational agent risks being rebuilt from scratch
as an ad hoc MCP service. With it, agent creation becomes a governed pipeline
with reusable scaffolding, common security/audit expectations, and a clear
promotion path into the long-term knowledge layer.
