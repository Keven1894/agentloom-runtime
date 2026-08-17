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
