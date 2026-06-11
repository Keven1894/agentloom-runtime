"""
validate_kg_sync.py
===================
Drift report for the internal KG sync (file tree -> knowledge_embeddings).

Runs the same extractors as ``rebuild_embeddings.py`` but **never writes**.
Prints a one-page health report showing:

    - rows in DB vs chunks in files
    - rows without embeddings (blocks vector search)
    - stale rows (in DB but no longer emitted by any extractor)
    - missing rows (file-tree chunk exists but not in DB)
    - content drift (same id but content hash differs)
    - MD path resolution failures (JSON node points at a path that no longer exists)

Exits with code 1 if any "bad" condition is found (useful for CI).

Run:

    python Scripts/kg_sync/validate_kg_sync.py
    python Scripts/kg_sync/validate_kg_sync.py --json   # machine-readable
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import datetime
from pathlib import Path

from agentloom_runtime.kg.sync.rebuild import (
    KG_DIR,
    REPO_ROOT,
    EXTRACTORS,
    PATH_KEYS,
    _load_env,
    collect_chunks,
    describe_db_target,
    load_existing,
)
from agentloom_runtime.db import connect


@contextlib.contextmanager
def _maybe_silence_stdout(silent: bool):
    """
    Redirect stdout to a buffer while ``collect_chunks()`` runs in JSON
    mode, so the JSON report that comes out afterwards is the only thing
    on stdout. Non-JSON human mode keeps the informative progress output.
    """
    if not silent:
        yield
        return
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


def _scan_path_problems() -> list[dict]:
    """Return every JSON node whose path field doesn't resolve to a real file."""
    problems: list[dict] = []
    for key, (fname, _) in EXTRACTORS.items():
        p = KG_DIR / fname
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        # Collect from any list-valued field that commonly holds nodes.
        for list_key in ("behaviors", "skills", "documents", "nodes"):
            for n in data.get(list_key, []) or []:
                if not isinstance(n, dict):
                    continue
                for pk in PATH_KEYS:
                    path_val = n.get(pk)
                    if not path_val or not isinstance(path_val, str):
                        continue
                    if path_val.startswith(("http://", "https://", "external://")):
                        continue
                    if not path_val.endswith(".md"):
                        continue
                    if n.get("archived") is True:
                        continue
                    candidate = REPO_ROOT / path_val.replace("\\", "/")
                    if not candidate.is_file():
                        problems.append({
                            "source_file": fname,
                            "node_id":     n.get("id", "?"),
                            "path_key":    pk,
                            "path_value":  path_val,
                        })
    return problems


def main() -> int:
    _load_env()

    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Output JSON report only")
    args = ap.parse_args()

    # Collect target state from files. Suppress progress output when
    # --json is set so the report is the only thing on stdout (the
    # Cursor KG-drift hook depends on this being machine-parseable).
    with _maybe_silence_stdout(args.json):
        chunks = collect_chunks()
    chunks_by_id = {c.id: c for c in chunks}

    # Load DB state through the same adapter used by runtime KG retrieval.
    conn = connect()
    existing = load_existing(conn)
    rows_total  = len(existing)
    rows_no_emb = conn.execute(
        "SELECT COUNT(*) FROM knowledge_embeddings WHERE embedding IS NULL"
    ).fetchone()[0]
    conn.close()

    stale_ids   = [eid for eid in existing if eid not in chunks_by_id]
    missing_ids = [cid for cid in chunks_by_id if cid not in existing]

    drifted: list[dict] = []
    import hashlib
    for cid, chunk in chunks_by_id.items():
        if cid not in existing:
            continue
        old_content, _, _ = existing[cid]
        old_hash = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
        if old_hash != chunk.content_hash:
            drifted.append({
                "id":       cid,
                "topic":    chunk.topic,
                "source":   chunk.source_file,
            })

    path_problems = _scan_path_problems()

    report = {
        "timestamp":    datetime.now().isoformat(),
        "db_target":    describe_db_target(),
        "db_rows":      rows_total,
        "file_chunks":  len(chunks_by_id),
        "rows_without_embedding": rows_no_emb,
        "stale_rows":   stale_ids,
        "missing_rows": missing_ids,
        "drifted_rows": drifted,
        "path_problems": path_problems,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("=== KG Sync Validation ===")
        print(f"Time:          {report['timestamp']}")
        print(f"DB target:     {report['db_target']}")
        print(f"DB rows:       {rows_total}")
        print(f"File chunks:   {len(chunks_by_id)}")
        print(f"Missing vector:{rows_no_emb}")
        print(f"Stale rows:    {len(stale_ids)}  (in DB, no source)")
        print(f"Missing rows:  {len(missing_ids)}  (in files, not in DB)")
        print(f"Content drift: {len(drifted)}  (same id, content changed)")
        print(f"Path problems: {len(path_problems)}  (JSON node -> nonexistent .md)")
        print()

        if stale_ids:
            print("--- Stale rows (run rebuild_embeddings.py --commit to clean) ---")
            for sid in stale_ids[:15]:
                print(f"  {sid}")
            if len(stale_ids) > 15:
                print(f"  ... and {len(stale_ids) - 15} more")
            print()

        if missing_ids:
            print("--- Missing rows ---")
            for mid in missing_ids[:15]:
                c = chunks_by_id[mid]
                print(f"  {mid}  ({c.source_file} / {c.topic[:50]})")
            if len(missing_ids) > 15:
                print(f"  ... and {len(missing_ids) - 15} more")
            print()

        if drifted:
            print("--- Content drift ---")
            for d in drifted[:10]:
                print(f"  {d['id']}  ({d['source']} / {d['topic'][:50]})")
            if len(drifted) > 10:
                print(f"  ... and {len(drifted) - 10} more")
            print()

        if path_problems:
            print("--- JSON -> missing .md ---")
            for p in path_problems[:15]:
                print(f"  {p['source_file']:35}  {p['node_id']:40}  {p['path_value']}")
            if len(path_problems) > 15:
                print(f"  ... and {len(path_problems) - 15} more")

    bad = bool(stale_ids or missing_ids or drifted or rows_no_emb or path_problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
