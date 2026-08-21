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

## What is portable, and the one thing that is not

"Move my sessions between machines" is four separable problems, and only the
last one is blocked. It is worth being precise about which is which, because
conflating them leads to abandoning three achievable things to avoid one
impossible one.

| | Problem | Status |
|---|---|---|
| 1 | **Capture** the conversation a host recorded | Solved per host by a read-only reader. Plain files, no host API needed. |
| 2 | **Move** it between machines | Solved. Compressed JSON in the shared database; a long session is a few hundred kilobytes. |
| 3 | **Reconstruct** it — render it for a human, or feed it back to an agent | Solved. `replay` in text, Markdown, or JSON. |
| 4 | **Restore it as native chat bubbles in the target host's own sidebar** | Not supported, by decision. |

Only (4) requires writing a host's private chat store, and that is the part
worth refusing. Those stores are keyed by a hash of the absolute workspace
path, they change shape between releases, the running editor caches them in
memory rather than re-reading them, and a concurrent external writer corrupts
them. It is not literally impossible — you can edit the file with the editor
closed — but a memory system built on it breaks on the next update of a
product you do not control.

So the trade-off is narrower than "no chat history". The conversation moves;
what does not move is its rendering inside a *specific vendor's UI widget*. You
read it with `replay` instead of by scrolling a sidebar.

Layer 0 therefore has two halves:

- **Checkpoints** are the index — a few hundred bytes, loaded on every session
  start, answering *what was I doing*.
- **Transcripts** are the archive — a few hundred kilobytes, paged in on
  demand, answering *what exactly was said, and why did we decide that*.

They are complementary. A transcript is far too large to load on every resume,
which is why the checkpoint exists; a checkpoint is far too terse to settle an
argument about a past decision, which is why the archive exists. A checkpoint
cites the transcript it came from, so you can always expand one into the other.

## Host neutrality invariants

These are the properties that make Layer 0 work in any host. They are enforced
by tests in `tests/test_session.py`, not just by convention.

| # | Invariant |
|---|---|
| **H1** | Session identity is `(agent_id, operator_id, workspace_key)`. `workspace_key` derives from the VCS remote, never from a filesystem path, machine name, or editor. |
| **H2** | `host_hint`, `ide_hint`, and `workspace_path_hint` are write-only provenance. They must never appear in a lookup predicate or a lookup index. |
| **H3** | Every operation is reachable from a plain shell command. No editor extension, plugin, or SDK is required. |
| **H4** | Resume output is plain text (or JSON), readable by any agent without parsing a proprietary format. |
| **H5** | No code path opens a host's private chat store (`state.vscdb`, `workspaceStorage`, `composerData`, and equivalents), and no code path writes to host-local storage at all. Reading a plain transcript file the host itself wrote is permitted, read-only, and confined to `session/readers/`. |

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
| **MCP** (`agentloom-session-mcp`) | an MCP-capable host | coding agents dynamically querying past context and lineage |
| **Web UI** (`agentloom-session ui`) | browser / localhost | humans inspecting session DAGs, transcripts, and checkpoints |
| **Python API** (`agentloom_runtime.session`) | Python in-process | building a service or a richer integration |

Every AI coding host can run a shell command. That is why the CLI is the floor
and why no feature may be CLI-inaccessible.

```bash
export AGENTLOOM_AGENT_ID=my-builder
export AGENTLOOM_DB_HOST=… AGENTLOOM_DB_NAME=… AGENTLOOM_DB_USER=… AGENTLOOM_DB_PASSWORD=…

agentloom-session whoami       # show resolved identity (debug host neutrality)
agentloom-session resume       # print the resume pack
agentloom-session checkpoint --next "Apply the migration to dev" --plan docs/plan/x.md
agentloom-session park         # pause; frees the identity's open slot

agentloom-session open --fork-from <id> --reason host_switch   # branch session into DAG
agentloom-session tree         # render ASCII DAG session hierarchy
agentloom-session lineage      # inspect session ancestry and child branches

agentloom-session archive --all      # capture this host's conversations
agentloom-session transcripts        # list what is archived for this workspace
agentloom-session replay --last 20   # read the most recent conversation back
agentloom-session index --all        # build the archive locator (prose chunks + embeddings)
agentloom-session search "password policy"   # pointers into the archive
# then: agentloom-session replay --ref <id> --around <seq>

agentloom-session mcp          # run stdio JSON-RPC MCP server
agentloom-session ui           # launch local web dashboard on port 8766
```

## Host adapter contract

A host adapter is a bootstrap instruction, not software. Adding a new editor
should take minutes.

1. Resolve the workspace from the VCS remote (the CLI does this).
2. Run `agentloom-session resume` at session start; treat the output as context.
3. Do the work using the agent's normal memory layers.
4. Run `agentloom-session checkpoint --next "…"` before stopping.

The only per-host difference is *where that instruction is written*, because
each host auto-loads a different file. Write the instruction once and generate
the rest with `agentloom-hostrules`:

```json
{
  "source": "agents/AGENT_BOOTSTRAP.md",
  "targets": [
    {"path": "AGENTS.md"},
    {"path": ".some-editor/rules/agentloom-session.md",
     "front_matter": {"alwaysApply": true}}
  ]
}
```

```bash
agentloom-hostrules sync    # write every host's rule file
agentloom-hostrules check   # fail if any drifted (CI / pre-commit)
```

A target is a path plus optional front matter — the path is the entire host
binding, and the emitter knows about no specific editor. Supporting an IDE that
does not exist yet is a manifest entry, not a code change and not a release.
That property is itself a test: if an editor's name appears in the emitter, some
host has become privileged and the next one will need special handling.

### Conformance checklist

A host is supported when all of these pass:

- [ ] `agentloom-session whoami` reports the same `workspace_key` as every other host for the same repository.
- [ ] `resume` returns a checkpoint written by a *different* host.
- [ ] Nothing in the flow opens the editor's private chat store.
- [ ] The flow works with the editor's own chat history cleared.
- [ ] No host-specific code was added outside `session/readers/`.

The fourth item is the honest test. If clearing the editor's history breaks
resume, the state was never really in Layer 0.

## Data model

Four tables plus a locator (`migrations/mysql/004_session_memory.sql`,
`005_session_transcripts.sql`, `006_session_transcript_index.sql`):

| Table | Holds |
|---|---|
| `agent_sessions` | one row per working session; a generated `open_key` enforces at most one open session per identity |
| `session_checkpoints` | resume points: next action, open plan, VCS state, decisions, transcript citations |
| `session_turns` | optional short turn summaries |
| `session_transcripts` | archived conversations, redacted and compressed, keyed by `(source_host, source_ref)` |
| `session_transcript_chunks` | search index over the archive: session-level nodes + overlapping prose windows (human/agent text only). Embeddings optional; lexical search works without them. |

### What is stored

Structured resume state, short summaries, and redacted conversation archives.

### What is not stored

Secrets, and a host's own chat database.

Two redaction passes protect the archive, because a transcript is precisely
where a credential that was echoed once would live forever:

- **Checkpoints** summarize the working tree with sensitive paths (`.env`, key
  material, `secrets/`) withheld — the summary records that such a file was
  dirty, never its name.
- **Transcripts** are redacted at capture, before anything is written. Anything
  credential-shaped — provider tokens, `KEY=value` assignments, bearer tokens,
  passwords inside connection strings, private-key blocks — is replaced with a
  `[redacted:…]` marker. Tool arguments are additionally truncated per field, so
  a path survives intact while a file body does not: those bodies are already in
  version control and reproducing them here would add bulk and risk without
  adding recall.

Redaction is idempotent, which makes a useful audit possible: re-run it over
everything already stored and expect zero hits. A non-zero result means a
pattern is missing. Run it with
`Scripts/db_migration/verify_agentloom_transcript_archive.py` in the deployment
repository.

Redaction is a safety net for accidental echoes, not a licence to paste
credentials into a conversation.

## Retrieval routing

Add one row to the router:

| Question | Layer |
|---|---|
| "Where did we leave off in this repository?" | **Layer 0 checkpoints** |
| "What exactly did we say about it?" | **Layer 0 transcript archive** (`replay`) |
| "When did we decide X?" | **Layer 0 archive locator** (`search` → `replay --around`) |
| "What is the accepted design?" | Layer 1 curated knowledge |
| "What is the team doing now?" | Layer 2 management |
| "Did we plan this before?" | Layer 3 plan / provenance |

The first three differ by cost, not by subject. Resume always answers the first
from a checkpoint. `search` returns pointers into the archive — never load the
whole conversation to answer a locator question. Durable decisions still belong
in the curated KG or a plan; the locator finds the discussion, it does not
become the authority.

The locator indexes **prose only** (human + agent text), at two granularities
(one session node + overlapping turn windows), and ranks with hybrid lexical +
vector RRF. Time is a filter (`--since`), not something cosine is asked to
encode. Tool-call noise is not embedded.

## Anti-patterns

- Opening the editor's private chat database, in either direction, to move sessions between machines.
- Symlinking editor application-data directories across machines or into cloud storage.
- Keying a session on the absolute checkout path.
- Filtering a resume lookup on a machine name or editor label.
- Loading a full transcript on every resume instead of the checkpoint that cites it.
- Archiving a transcript without the redaction pass.
- Requiring an editor extension for any operation.

## Related

- [`three-layer-memory-architecture.md`](three-layer-memory-architecture.md) — layers 1–3 and the retrieval router.
- [`kg-sync-and-maintenance.md`](kg-sync-and-maintenance.md) — the file → database sync contract.
