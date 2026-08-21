# Changelog

## Unreleased

### Added

- **`agentloom_runtime.session`** — Layer 0 working-session memory. An agent and
  operator resume work on a repository from a different machine or a different
  IDE, because session identity comes from the VCS remote rather than from a
  filesystem path or an editor-local store.
  - `migrations/mysql/004_session_memory.sql` — `agent_sessions`,
    `session_checkpoints`, `session_turns`. Standalone: appliable without the
    CORE schema. A generated `open_key` enforces at most one open session per
    identity in the database rather than in application code.
  - `agentloom-session` console script — the portability floor, since every AI
    coding host can run a shell command. No editor extension is required for any
    operation.
  - Checkpoint working-tree summaries redact sensitive paths (`.env`, key
    material, `secrets/`): the summary records that such a file was dirty, never
    its name.
  - Host-neutrality invariants (identity never derives from host/path/IDE;
    provenance hints are never lookup predicates) are enforced by tests.

- **`agentloom_runtime.hostrules`** — generate every AI coding host's rule file
  from one canonical bootstrap instruction, so Layer 0 engages automatically
  wherever the agent runs.
  - `agentloom-hostrules sync` / `check` (the latter suits CI or a pre-commit
    hook, failing when a generated file drifts from its source).
  - A target is a path plus optional front matter. The emitter knows about no
    specific editor, so supporting an IDE that does not exist yet is a manifest
    entry rather than a code change — enforced by a test that fails if any
    editor name appears in the module.

- **Layer 0 conversation archive** — the conversation itself now travels between
  machines, not only the agent's working state. Checkpoints stay the cheap index
  loaded on every resume; the archive is the detail paged in on demand.
  - `migrations/mysql/005_session_transcripts.sql` — `session_transcripts`,
    holding redacted conversations as compressed JSON keyed by
    `(source_host, source_ref)`, so re-archiving a conversation that is still
    growing updates one row. Requires 004.
  - `agentloom-session archive` / `transcripts` / `replay`, rendering to text,
    Markdown, or JSON. `checkpoint` now archives and cites the current
    conversation by default (`--no-archive` opts out), so a terse checkpoint can
    always be expanded into the exchange it came from.
  - `agentloom_runtime.session.readers` — an explicit read-only plugin seam for
    host-specific capture, with a Cursor reader. Supporting another host is one
    module; nothing outside the package changes. A test fails if any reader
    gains a write, delete, or truncate call, and the host-neutrality tests now
    cover the subpackage.
  - Transcripts are redacted **before** storage: provider tokens, `KEY=value`
    assignments, bearer tokens, connection-string passwords, and private-key
    blocks become `[redacted:…]` markers. Redaction is idempotent, so re-running
    it over stored data and expecting zero hits is a valid completeness audit.
  - Tool arguments are truncated per field rather than whole: a path survives
    intact, a file body does not. Those bodies are already in version control,
    and they are both the bulk and the largest exposure surface.

  Restoring a conversation as native chat bubbles inside an editor's own sidebar
  remains out of scope: it requires writing an undocumented, path-keyed store
  that the running editor holds open.

- **Layer 0 archive locator** — find a discussion without loading the whole
  conversation. `migrations/mysql/006_session_transcript_index.sql` stores
  session-level nodes plus overlapping prose windows (human/agent text only).
  `agentloom-session index` / `search` rank with hybrid lexical + vector RRF and
  return `(source_ref, seq)` pointers; `replay --around` pages the archive.
  Embeddings are optional (`--no-embed` is a lexical-only index). Time is a
  filter column (`--since`), not a cosine side-effect.

- **Layer 0 session DAG & lineage** — explicit directed acyclic graph (DAG) topology
  across sessions and hosts.
  - `migrations/mysql/007_session_lineage.sql` — adds `parent_session_id`,
    `fork_checkpoint_id`, and `fork_reason` to `agent_sessions` with `ON DELETE SET NULL`.
  - `agentloom-session open --fork-from <id> --reason <reason>` branches sessions
    across machines or subtasks; `agentloom-session tree` / `lineage` renders the DAG.
  - `store.get_session_lineage()` and `store.get_workspace_session_tree()`.

- **Layer 0 MCP server (`agentloom-session-mcp`)** — active on-demand memory retrieval
  for AI coding agents in any IDE.
  - Stdio JSON-RPC 2.0 server with tools: `session_search`, `session_get_context`,
    `session_get_checkpoint`, `session_get_lineage`.
  - Agents dynamically query past decisions and page conversation turns without
    token-dumping entire chat histories into context upfront.

- **Layer 0 Session Web Viewer** — zero-dependency local graphical interface.
  - `agentloom-session ui [--host 127.0.0.1] [--port 8766]` launches a standalone
    threaded HTTP server with embedded reactive single-page viewer.
  - Visualizes session DAG trees, interactive conversation transcripts with collapsible
    tool cards, instant hybrid search, and one-click "Continue in IDE" fork commands.

- **Compact vector storage for the archive index** — embeddings move from JSON
  text to little-endian float32 blobs.
  - `migrations/mysql/008_session_transcript_vectors.sql` adds `embedding_f32`
    (MEDIUMBLOB) and `embedding_dim`, alongside the JSON column so readers can
    be upgraded independently. `009_drop_json_embeddings.sql` retires the JSON
    column once every row carries a compact vector.
  - `agentloom-session compact` re-encodes existing rows. It costs no embedding
    API calls — the vectors are identical — and is a no-op after 009, so it is
    safe to leave in a scheduled job.
  - Byte order is pinned explicitly because these rows are written and read
    across architectures; the same archive is served to x86-64 and aarch64.

- **`agentloom_runtime.config`** — one environment loader for the whole package.
  A `.env` file fills gaps only; a variable already in the process environment
  always wins, so injected configuration beats a file on disk. Discovery walks
  up from the working directory, and `AGENTLOOM_ENV_FILE` pins it for services
  whose working directory is not the repository.

### Fixed

- Vector search reached the database but never the query. The adapter and the
  embedding provider each discovered configuration by their own rules, so a
  deployment could hold a fully embedded archive while every search silently
  degraded to lexical-only — plausible-looking results, worse ranking, no
  error. Both now share `agentloom_runtime.config`.
- `session_search` (MCP) passed `lexical_only` to `store.search_archive`, which
  takes a precomputed `query_vec`. Every call failed at runtime. The tool now
  embeds the query the way the CLI does, and honours `lexical_only` by skipping
  embedding rather than discarding it.
- `session_get_lineage` (MCP) required a `session_id` that a resuming agent has
  no way to know. It now falls back to the caller's own session.
- The session web viewer ignored `limit` on `/api/transcripts` and `/api/search`
  and never computed a query vector, so the human-facing surface was capped at
  hardcoded page sizes and searched lexically against an embedded archive.
- `search_archive` selected the embedding column on every query, including
  lexical-only ones that never looked at a vector. On a 10,121-chunk archive
  that moved 299 MB to rank against 27 MB of content: 16.4 s for a lexical
  search whose ranking took 115 ms. Vectors are now fetched only when something
  will rank with them, and re-indexing tests for a vector's presence instead of
  selecting it to check for NULL. Lexical search 16.4 s → 0.6 s; hybrid search
  12 s → 1.7 s excluding the embedding API call.
- `HybridRow` iterated column *names* rather than values, so the most ordinary
  line in database code — `for schema, table in cursor.fetchall():` — bound the
  wrong data with no exception raised. It now matches `sqlite3.Row` and the
  DB-API. Key-based membership (`"col" in row`) is unchanged.

### Changed

- The MCP server no longer defaults `agent_id`/`operator_id` to any particular
  deployment's names. An unset `AGENTLOOM_AGENT_ID` is reported as
  configuration to supply; guessing would quietly read another identity's
  session and answer confidently from it. The host-neutrality test now fails on
  agent and operator names, not only hostnames and IP ranges.
- MCP and viewer tests bind `autospec=True`. A bare `MagicMock` accepts any
  argument, which is how a tool calling the store with a parameter the store
  does not have passed its tests and failed on first real use.

### Documentation

- `docs/memory/layer-0-session-memory.md` — separates the four problems behind
  "sync my sessions" (capture, move, reconstruct, repaint in a vendor UI) and
  marks only the last unsupported; adds the checkpoint-vs-archive split, the
  redaction contract, and the amended H5 invariant.

## 0.1.0 — 2026-06-11

First installable library release. Extracted from the Envita production runtime and
neutralized for open-source use.

### Added

- **`agentloom_runtime.db`** — MySQL adapter (`AGENTLOOM_DB_*` / `DATABASE_URL`)
- **`agentloom_runtime.memory`** — embedding indexes (KG, DocShare, message, plan),
  RRF joint retrieval, configurable sync markers
- **`agentloom_runtime.kg`** — `search_kg()` + file→DB kg_sync pipeline
- **`agentloom_runtime.fair`** — FAIR compliance calculator (stdlib only)
- **`agentloom_runtime.quality`** — JSON Schema validation, KG integrity checks,
  bundled KG schemas, runtime health report
- **`migrations/mysql/`** — CORE schema (`001`–`003`) without instance-specific tables
- 23 pytest tests; OSS pre-release scanner gate at 0 BLOCK

### Documentation

- Memory architecture, KG-as-engine ADR, kg-sync contract, productization playbook
  (docs-first, unchanged from initial release)
