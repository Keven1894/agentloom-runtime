# Mounting Layer 0 on your own agent

Layer 0 gives an agent working-session continuity across machines and IDEs: it
answers *where did we leave off in this repository*, from any checkout, in any
host. This guide is for someone who wants that for their own agent and has no
interest in the rest of this runtime.

You do not need the CORE schema, an Envita deployment, or any particular editor.
The reference deployment itself runs Layer 0 with no CORE tables at all.

## What you are installing

Three things, and nothing else:

| Piece | What it is |
|---|---|
| Seven tables | Sessions, checkpoints, turns, and the conversation archive |
| `agentloom-session` | A console script — the portability floor, since every host can run a shell command |
| A bootstrap instruction | One line in your agent's rule file telling it to resume first |

Session identity is `(agent_id, operator_id, workspace_key)`, where
`workspace_key` is the normalized VCS remote URL. That is the entire reason this
survives switching machines: nothing in the lookup path is a filesystem path, a
hostname, or an editor-local store.

## 1. Install

```bash
pip install -e /path/to/agentloom-runtime
```

## 2. Point it at a database

MySQL 8 or later. Put this in a `.env` at your repository root, or export it:

```bash
AGENTLOOM_DB_HOST=127.0.0.1
AGENTLOOM_DB_NAME=my_agent_memory
AGENTLOOM_DB_USER=runtime
AGENTLOOM_DB_PASSWORD=...
AGENTLOOM_AGENT_ID=my-agent
```

Set `AGENTLOOM_AGENT_ID` here rather than passing `--agent` on every command.
It is the one identity component that cannot be detected from the environment.

Optional, for semantic search over past sessions:

```bash
OPENAI_API_KEY=...
EMBEDDING_MODEL=text-embedding-3-small
```

Put the key where the *runtime* will find it, not only where your indexing job
found it. If it resolves at index time but not at query time, the archive embeds
fine and every search quietly degrades to lexical-only ranking.

## 3. Create the schema

```bash
agentloom-session init
```

That is the whole step. It applies the `session` group in dependency order and
records each migration in `agentloom_schema_history`, so re-running it is safe
and upgrading later only applies what is new.

If you already created these tables by hand, adopt them instead of re-running
the DDL:

```bash
agentloom-session init --baseline
```

## 4. Check it

```bash
agentloom-session doctor
```

Everything should be `ok`. Two warnings are worth understanding rather than
ignoring:

- **`workspace key: local:...`** — this checkout has no VCS remote, so the key
  falls back to a local path and will not match the same repository on another
  machine. Add a remote; cross-machine continuity depends on it.
- **`embeddings: unavailable`** — search still works, but ranks lexically only.

## 5. Use it

```bash
agentloom-session open --title "add retry backoff"
agentloom-session checkpoint --next "apply the migration to dev"
agentloom-session resume          # from any machine, any IDE
```

`resume` is the one that matters. It returns the last checkpoint's next action,
the open plan, and the working-tree state at that point.

## 6. Make your agent do it automatically

An agent that has to be reminded to resume will not resume. Put the instruction
in the rule file of every host you use — and generate those files from one
source so they cannot drift:

```bash
agentloom-hostrules sync
```

The canonical instruction is a single step: run `agentloom-session resume`
before anything else, and treat the output as the start of the session.

## Archiving conversations (optional)

Layer 0 can also archive the conversations your host recorded, making them
searchable across machines:

```bash
agentloom-session archive     # import this checkout's transcripts
agentloom-session search "why did we drop the JSON column"
agentloom-session ui          # browse them
```

## Supporting your host

Capture is the one part that cannot be host-neutral: session identity can be,
and rule-file emission can be, but every host writes its own format in its own
place. So it is an explicit plugin seam.

Supported today:

| Host | Location |
|---|---|
| Cursor | `~/.cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl` |
| Claude Code | `~/.claude/projects/<cwd-with-separators-as-dashes>/<uuid>.jsonl` |

Adding one means writing a single module in
`agentloom_runtime/session/readers/` and listing it in `READERS`. Nothing
outside that package changes. Two contracts apply:

1. **Readers never write.** They open files a host already wrote. Writing to,
   locking, or migrating a host's own storage is how you corrupt someone's
   editor. A test enforces this by scanning the package for mutating calls.
2. **Every step is optional.** These layouts are not published APIs. An
   unfamiliar line or a renamed field must yield fewer turns, never an
   exception.

A reader may also surface things the host knows and a generic rule cannot
guess — Claude Code names its own conversations, and that name is used verbatim
rather than re-deriving one from the opening prompts.

Everything else — identity, storage, search, the viewer, the rule files — is
already host-neutral and needs no changes.

## Where this sits

Layer 0 is deliberately narrow. It is not a replacement for the other memory
layers, and durable knowledge does not belong in a checkpoint, which the next
checkpoint overwrites.

| Question | Where it belongs |
|---|---|
| Where did we leave off here? | Layer 0 session memory |
| What is the accepted design? | Curated knowledge graph |
| What is the team doing now? | Your management database |
| Did we plan this before? | Your plan documents and their embeddings |

See [`layer-0-session-memory.md`](layer-0-session-memory.md) for the design and
its invariants, and [`memory-reconstruction.md`](memory-reconstruction.md) for
why the archive reconstructs a conversation rather than restoring it into the
host's own UI.
