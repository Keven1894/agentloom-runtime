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

### Documentation

- `docs/memory/layer-0-session-memory.md` — why not to sync the editor's own
  database, the host-neutrality invariants, and the host adapter conformance
  checklist.

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
