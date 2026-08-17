# Layer 0: Working-Session Memory

**Status:** Reference architecture + shipped module (`agentloom_runtime.session`)
**Part of:** AgentLoom Runtime

## Purpose

The three-layer memory model answers what the organization *knows*, what the
team is *doing*, and what was *planned*. None of those answer a question a
developer asks every morning:

> Where did this agent and I leave off in this repository?

Today that state lives inside the AI coding editor — a local SQLite database or
a local transcript directory on the machine running the editor's UI. Switch
laptops, re-clone to a different directory, or switch editors, and the thread is
gone even though the code, the knowledge graph, and the task database all moved
over fine.

Layer 0 puts that state in the same shared database as the rest of the agent's
memory, so continuity survives the host.

## Non-goal: syncing the editor

There is a tempting shortcut: reverse-engineer the editor's local database and
copy it between machines. Do not build on that.

Editor chat stores are private implementation details. They are keyed by a hash
of the absolute workspace path, they change shape between releases, they are
cached in memory and not re-read while the editor runs, and concurrent writers
corrupt them. Anything built on them breaks on the next update.

Layer 0 stores **the agent's working state**, not the editor's conversation.
Resuming means the agent knows what it was doing, not that old chat bubbles
reappear in a sidebar. That is a deliberate product decision, and it is what
makes the capability portable.

## Host neutrality invariants

These are the properties that make Layer 0 work in any host. They are enforced
by tests in `tests/test_session.py`, not just by convention.

| # | Invariant |
|---|---|
| **H1** | Session identity is `(agent_id, operator_id, workspace_key)`. `workspace_key` derives from the VCS remote, never from a filesystem path, machine name, or editor. |
| **H2** | `host_hint`, `ide_hint`, and `workspace_path_hint` are write-only provenance. They must never appear in a lookup predicate or a lookup index. |
| **H3** | Every operation is reachable from a plain shell command. No editor extension, plugin, or SDK is required. |
| **H4** | Resume output is plain text (or JSON), readable by any agent without parsing a proprietary format. |
| **H5** | No code path reads or writes editor-local storage. |

H2 is the one that fails silently if you get it wrong: filter on a hint and
everything looks fine on the machine that wrote it, then returns nothing
anywhere else.

### Why the VCS remote

Every transport for the same repository collapses to one key:

```text
git@github.com:Acme/widget.git          ─┐
https://github.com/Acme/widget           ├─►  github.com/acme/widget
ssh://git@github.com:22/Acme/widget.git ─┘
```

So `C:\projects\widget` on Windows and `/home/dev/widget` on Linux are the same
workspace. A directory without a remote falls back to `local:<directory-name>`,
which still matches across machines when the folder name matches — usable, but
prefer a real remote.

## Invocation surfaces

Three surfaces over one store, ordered by how universally they work. Pick the
highest one your host supports; the lower ones remain available.

| Surface | Requires | Use when |
|---|---|---|
| **CLI** (`agentloom-session`) | a shell | always available — this is the portability floor |
| **Python API** (`agentloom_runtime.session`) | Python in-process | building a service or a richer integration |
| **MCP** (planned) | an MCP-capable host | the host speaks MCP and you want tool-call ergonomics |

Every AI coding host can run a shell command. That is why the CLI is the floor
and why no feature may be CLI-inaccessible.

```bash
export AGENTLOOM_AGENT_ID=my-builder
export AGENTLOOM_DB_HOST=… AGENTLOOM_DB_NAME=… AGENTLOOM_DB_USER=… AGENTLOOM_DB_PASSWORD=…

agentloom-session whoami       # show resolved identity (debug host neutrality)
agentloom-session resume       # print the resume pack
agentloom-session checkpoint --next "Apply the migration to dev" --plan docs/plan/x.md
agentloom-session park         # pause; frees the identity's open slot
```

## Host adapter contract

A host adapter is a bootstrap instruction, not software. Adding a new editor
should take minutes.

1. Resolve the workspace from the VCS remote (the CLI does this).
2. Run `agentloom-session resume` at session start; treat the output as context.
3. Do the work using the agent's normal memory layers.
4. Run `agentloom-session checkpoint --next "…"` before stopping.

The only per-host difference is *where that instruction is written*: a rules
file, a system prompt, or a documented habit. Keep the instruction text in one
canonical place and emit it to each host's format rather than maintaining
divergent copies.

### Conformance checklist

A host is supported when all of these pass:

- [ ] `agentloom-session whoami` reports the same `workspace_key` as every other host for the same repository.
- [ ] `resume` returns a checkpoint written by a *different* host.
- [ ] Nothing in the flow reads editor-local storage.
- [ ] The flow works with the editor's own chat history cleared.
- [ ] No host-specific code was added to `agentloom_runtime.session`.

The fourth item is the honest test. If clearing the editor's history breaks
resume, the state was never really in Layer 0.

## Data model

Three tables (`migrations/mysql/004_session_memory.sql`):

| Table | Holds |
|---|---|
| `agent_sessions` | one row per working session; a generated `open_key` enforces at most one open session per identity |
| `session_checkpoints` | resume points: next action, open plan, VCS state, decisions, optional transcript citations |
| `session_turns` | optional short turn summaries |

### What is stored

Structured resume state and short summaries.

### What is not stored

Editor conversation blobs, full transcripts, and secrets. Checkpoints capture a
working-tree summary with sensitive paths (`.env`, key material, `secrets/`)
withheld — the summary records that such a file was dirty, never its name.

If full transcripts are archived elsewhere for research, cite them by
identifier on the checkpoint and leave the bytes where they are.

## Retrieval routing

Add one row to the router:

| Question | Layer |
|---|---|
| "Where did we leave off in this repository?" | **Layer 0 session memory** |
| "What is the accepted design?" | Layer 1 curated knowledge |
| "What is the team doing now?" | Layer 2 management |
| "Did we plan this before?" | Layer 3 plan / provenance |

Do not answer the first question from editor transcripts, and do not answer it
from the team message log — those are a different kind of record with different
freshness and ownership.

## Anti-patterns

- Reading or writing the editor's chat database to move sessions between machines.
- Symlinking editor application-data directories across machines or into cloud storage.
- Keying a session on the absolute checkout path.
- Filtering a resume lookup on a machine name or editor label.
- Storing whole transcripts in the session tables.
- Requiring an editor extension for any operation.

## Related

- [`three-layer-memory-architecture.md`](three-layer-memory-architecture.md) — layers 1–3 and the retrieval router.
- [`kg-sync-and-maintenance.md`](kg-sync-and-maintenance.md) — the file → database sync contract.
