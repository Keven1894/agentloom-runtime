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
| **H1** | Session identity is `(agent_id, operator_id, workspace_key, lane)`. `workspace_key` derives from the VCS remote and `lane` names a work stream, never a filesystem path, machine name, or editor. |
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

### Why the lane

At most one session may be open per identity, which is exactly right for a
handoff and fatal for two machines doing unrelated work: the second cannot get a
slot, and forking to make one parks the first machine's live session.

The tempting fix is to key on the host. That buys concurrency by destroying the
reason this layer exists, since a session keyed to a machine is no longer
resumable from anywhere. The missing dimension is the **work stream**. A lane
names what is being worked on, so two lanes can be open at once while either
host can still resume either lane. Two sessions in the same lane still collide,
which is the protection worth keeping.

Lane defaults to `default`, so a host that never passes one resumes exactly what
it resumed before. `AGENTLOOM_SESSION_LANE` pins it per checkout; an explicit
`--lane` outranks the environment.

Liveness (`session_hosts`) is read only to **refuse** a destructive fork, never
to select a session. Lookup still keys on
`(agent_id, operator_id, workspace_key, lane)` and never on a machine name, so
H2 is untouched.

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

Configuration comes from the process environment or the repository's `.env`,
whichever is present; the real environment always wins. Both entry points load
it before resolving identity, so `AGENTLOOM_AGENT_ID` in a checkout's `.env` is
enough and no command needs `--agent`.

```bash
export AGENTLOOM_AGENT_ID=my-builder
export AGENTLOOM_DB_HOST=… AGENTLOOM_DB_NAME=… AGENTLOOM_DB_USER=… AGENTLOOM_DB_PASSWORD=…

agentloom-session whoami       # show resolved identity (debug host neutrality)
agentloom-session resume       # print the resume pack, and claim this lane
agentloom-session resume --peek  # the same, without recording this host as active
agentloom-session checkpoint --next "Apply the migration to dev" --plan docs/plan/x.md
agentloom-session checkpoint --auto --if-stale 4   # automation: refresh state, never author it
agentloom-session list         # every session for this identity, with lane and status
agentloom-session park         # pause; frees this lane's open slot
agentloom-session title "…"    # correct a session title that no longer describes the work
agentloom-session alias --add <old-key> --to <current-key> --migrate   # a remote moved

agentloom-session decisions --lineage   # decisions under the newest checkpoint, across the fork chain
agentloom-session checkpoints  # the checkpoint history, not just the latest

agentloom-session open --lane medialoom --title "…"            # a second concurrent work stream
agentloom-session open --fork-from <id> --reason host_switch   # branch session into DAG
agentloom-session tree         # render ASCII DAG session hierarchy
agentloom-session lineage      # inspect session ancestry and child branches

agentloom-session archive --all      # capture this host's conversations
agentloom-session transcripts        # list what is archived for this workspace
agentloom-session replay --last 20   # read the most recent conversation back
agentloom-session index --all        # build the archive locator (prose chunks + embeddings)
agentloom-session search "password policy"   # pointers into the archive
# then: agentloom-session replay --ref <id> --around <seq>
agentloom-session present --ref <id>         # trilingual overlay via local chat model

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

Five tables plus a locator (`migrations/mysql/004_session_memory.sql`,
`005_session_transcripts.sql`, `006_session_transcript_index.sql`), then
additive migrations for listing copy, locale, batch-job traces, and lanes:

| Table | Holds |
|---|---|
| `agent_sessions` | one row per working session; a generated `open_key` enforces at most one open session per identity **and lane** |
| `session_hosts` | which machines have worked in a session and when each was last seen; read only to refuse a destructive fork, never to resolve a session |
| `session_checkpoints` | resume points: next action, open plan, VCS state, decisions, transcript citations |
| `session_transcripts` | archived conversations, redacted and compressed, keyed by `(source_host, source_ref)` |
| `session_transcript_chunks` | search index over the archive: session-level nodes + overlapping prose windows (human/agent text only). Embeddings optional; lexical search works without them. `locale` (`original` / `en` / `es`) is part of the unique key so translated overlays do not collide with the original. |
| `session_job_runs` | one invocation of a long-running job over the archive (host, models, filters) |
| `session_job_items` | per-transcript job state, keyed `(job_kind, transcript_id)`, fingerprinted against the archive body |
| `session_job_events` | append-only typed events, monotonic `seq` within a run |

`session_transcripts.presentation_json` (migration 013) is an optional overlay:
trilingual title and description, plus per-turn English/Spanish text. The
archive body is never rewritten to store a translation. Missing overlay
sequences fall back to the original, so a partial translation is a legal state.

Batch work that produces overlays — translation, re-embedding, review — writes
progress and judgement to `session_job_*` (migration 015), not to a file beside
the checkout. Overlay presence is derivable from `presentation_json`; the
reviewer's verdict is not, which is why it has its own column
(`qc_report_json`). The runtime module is `agentloom_runtime.session.jobs`.

Lanes ship as expand/contract across two migrations because they rewrite a
unique index both machines depend on. **016** is additive: it adds `lane` and
`session_hosts` but leaves `open_key` alone, so a host on pre-lane code still
sees one open session per identity. **017** rebuilds `open_key` to include the
lane. The order is mandatory — pre-lane code's open lookup does not filter on
lane and ends in `LIMIT 1` with no `ORDER BY`, so once a second lane exists it
would pick one arbitrarily, including for the implicit open that `checkpoint`
performs. `agentloom-session init --through` lets a fleet sit at 016 until every
host is upgraded.

**019** adds `workspace_aliases`. Deriving identity from the VCS remote is what
makes a session resumable from any checkout, and nothing about an alias weakens
that — but the derivation quietly assumes the remote is immortal, and remotes
move. A machine whose checkout still points at the old one derives a different
key and opens its own session instead of resuming the shared one. Resolution
happens once on the identity path and is **one hop**: a canonical key may not
itself be an alias, enforced by the writer, so a cycle cannot be created rather
than having to be detected. It fails open, so `whoami` still answers offline.
`--migrate` re-files rows already stored under the old key across all four
workspace-keyed tables, pinning the `ON UPDATE CURRENT_TIMESTAMP` columns —
otherwise every recovered row is dated to the day of the remap, and `resume`,
which falls back to the most recently updated parked session in a lane, would
prefer a long-dead session over the one somebody paused yesterday.

**018** drops `session_turns`, a table created in 004 to hold short per-turn
summaries. Nothing ever wrote one: the only path to it required a hand-typed
summary at the exact moment somebody is trying to stop working, and the archive
already answers that question at full fidelity. Two records of one fact is a
maintenance cost with no reader, so the unpopulated one went. Same ordering
requirement as 017 in the other direction — apply it only once every host runs
code that no longer reads the table, since a host on older code fails its whole
resume rather than just the turn lookup.

### Who writes a checkpoint

A checkpoint is a person — or an agent acting for one — saying where things
stand. `--auto` exists so automation can keep the *surrounding facts* current
without speaking for them: it refreshes the working tree and the transcript
citation, carries the last `--next` and `--plan` forward unchanged, and refuses
to run alongside `--next` or `--decision` rather than quietly preferring one.

Two consequences are load-bearing:

- **Decisions are never inherited.** Next action and plan are current-state
  fields where the newest row wins; a decision is an append-only event, and
  copying it forward would repeat it once per automatic run in
  `agentloom-session decisions`.
- **The row is marked**, via `checkpoint_kind: auto` in `payload_json`, and
  `resume` labels it where the next action is read. A reader who cannot tell
  the difference would treat a carried-forward instruction as one a person left
  them *after* the work that followed it.

`--if-stale HOURS` makes the common invocation a no-op, and answers the
staleness question server-side so a checkpoint another machine wrote minutes
ago counts.

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
| "Find that discussion in another language" | **Layer 0 locator** rows with `locale=en` / `es` |
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
encode. Tool-call noise is not embedded. Translated overlays are additional
rows with `locale=en` or `locale=es`; they do not replace the original.

### How search chooses rows

Migration 020 adds a full-text index on `session_transcript_chunks.content`.
Search does not use it unless `AGENTLOOM_SEARCH_FULLTEXT=1`. With the flag,
`MATCH … AGAINST` in natural-language mode returns at most
`AGENTLOOM_SEARCH_CANDIDATES` rows (default 400) and vector comparison runs on
that set. An empty match, or a database that has not applied 020, scans the
workspace instead.

The flag stays off because the 2026-09-22 gates split. Latency passed at both
cap 400 (lexical median 76 ms, hybrid 148 ms) and cap 800 (96 ms and 175 ms),
against baselines of 1,458 ms and 6,033 ms. Recall did not: fewer than 3 of
each probe's previous hybrid top-5 chunk ids appeared in the new top 20.
A capped index that drops those hits is a different search, not a faster one.

`AGENTLOOM_SEARCH_MODE=channels` (2026-09-26) runs the two halves
independently. The lexical channel is `MATCH` on `content`, plus `MATCH` on the
n-gram-indexed `content_cjk` (migration 021) when the query has CJK text, each
capped at 100 rows. The dense channel is exact cosine over a per-host sidecar
(`~/.agentloom/index/<hash>/`, float32 matrix plus ids and a watermark) at
depth 100, across every locale. The two id lists are fused with RRF and
collapsed to one pointer per transcript. The sidecar re-syncs by
`(chunk_id, content_sha256)` diff and checks the server at most every
`AGENTLOOM_SIDECAR_CHECK_SECONDS` (600). Measured on 33,126 chunks: lexical
83–93 ms, hybrid 169–197 ms warm, and a Spanish-only query returns `es` rows.
Search stays on the scan by default until the labeled quality gate in the
unified retrieval plan passes. The old 2026-09-22 gate compared against the
previous ranker's top 5, which is overlap, not relevance, and is no longer used.

## Anti-patterns

- Opening the editor's private chat database, in either direction, to move sessions between machines.
- Symlinking editor application-data directories across machines or into cloud storage.
- Keying a session on the absolute checkout path.
- Filtering a resume lookup on a machine name or editor label.
- Loading a full transcript on every resume instead of the checkpoint that cites it.
- Archiving a transcript without the redaction pass.
- Requiring an editor extension for any operation.
- Rewriting `body_zlib` to store a translation.
- Keeping job resume state or review verdicts in a checkout-local file.
- Keying per-transcript job state on the run rather than `(job_kind, transcript_id)`.
- Keying a lane on a machine instead of on the work stream.
- Reading `session_hosts` to decide *which* session to resume rather than only to refuse a fork.
- Forking on `ANOTHER MACHINE IS WORKING HERE`, which parks a live session; take a lane instead.
- Letting automation author a `--next`, or render an automatic checkpoint as though a person wrote it.
- Deciding staleness by subtracting a server timestamp from the local clock.
- Chaining workspace aliases, or resolving them anywhere but the identity path.
- Re-filing rows under a new workspace key without pinning `ON UPDATE` columns.
- Adding a full-text index and then reading every matching row. The cap is what makes it an index. A cap that fails the recall gate is not turned on by default.
- Letting one channel filter the other. Scoring vectors only on full-text hits drops every overlay that shares no word with the question.
- Gating a new ranker on overlap with the old ranker's results. Gate on labeled relevance.

## Related

- [`memory-reconstruction.md`](memory-reconstruction.md) — the mechanism behind this contract, in diagrams: reconstruction vs. migration, identity derivation, the cross-machine lifecycle, and the retrieval cost ladder.
- Envita companion: `docs/architecture/memory/multi-host-concurrency-and-the-execution-boundary.md` in the deployment repository — lanes and liveness as deployed, the two banners, and the master/follower execution boundary.
- Envita companion: `docs/architecture/memory/layer-0-archive-presentation-and-job-trace.md` in the deployment repository — overlay shape, locale index, job-trace tables, translator vs. independent reviewer.
- [`three-layer-memory-architecture.md`](three-layer-memory-architecture.md) — layers 1–3 and the retrieval router.
- [`kg-sync-and-maintenance.md`](kg-sync-and-maintenance.md) — the file → database sync contract.
