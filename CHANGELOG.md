# Changelog

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
