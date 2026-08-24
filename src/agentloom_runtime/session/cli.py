"""``agentloom-session`` — the host-agnostic entry point for session memory.

Every AI coding host can run a shell command, so the CLI is the portability
floor: it works identically in Cursor, VS Code + Cline, Antigravity, a plain
terminal, or CI. Richer surfaces (Python API, MCP) are conveniences layered on
top of the same store, never a requirement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from agentloom_runtime.session import store
from agentloom_runtime.session.identity import (
    detect_host_context,
    detect_workspace_key,
    resolve_operator_id,
)
from agentloom_runtime.session.readers import discover_transcripts, get_reader
from agentloom_runtime.session.transcript import render_markdown, render_text
from agentloom_runtime.session.vcs import collect_vcs_state

DEFAULT_AGENT_ENV = "AGENTLOOM_AGENT_ID"


def _identity(args: argparse.Namespace) -> tuple[str, str, str]:
    import os

    agent_id = args.agent or os.environ.get(DEFAULT_AGENT_ENV)
    if not agent_id:
        raise SystemExit(
            "error: no agent id. Pass --agent or set AGENTLOOM_AGENT_ID "
            "(e.g. AGENTLOOM_AGENT_ID=my-agent)."
        )
    operator_id = resolve_operator_id(args.operator)
    workspace_key = args.workspace or detect_workspace_key(Path(args.path) if args.path else None)
    return agent_id, operator_id, workspace_key


def _emit(payload: Any, text: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str) if as_json else text)


def _resolve_session_id(args: argparse.Namespace) -> str:
    """Find the session to act on: explicit id, else the identity's open one."""
    if getattr(args, "session", None):
        return args.session
    agent_id, operator_id, workspace_key = _identity(args)
    pack = store.resume(agent_id, operator_id, workspace_key, turn_limit=0)
    if pack is None:
        raise SystemExit(
            "error: no open session for this identity. Run 'agentloom-session open' first."
        )
    return pack.session.session_id


def cmd_whoami(args: argparse.Namespace) -> int:
    agent_id, operator_id, workspace_key = _identity(args)
    host = detect_host_context(Path(args.path) if args.path else None)
    payload = {
        "agent_id": agent_id,
        "operator_id": operator_id,
        "workspace_key": workspace_key,
        "hints": {
            "host": host.host_hint,
            "ide": host.ide_hint,
            "path": host.workspace_path_hint,
        },
    }
    text = (
        f"agent:     {agent_id}\n"
        f"operator:  {operator_id}\n"
        f"workspace: {workspace_key}\n"
        f"hints (provenance only, never used for lookup):\n"
        f"  host: {host.host_hint}\n"
        f"  ide:  {host.ide_hint}\n"
        f"  path: {host.workspace_path_hint}"
    )
    _emit(payload, text, args.json)
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    agent_id, operator_id, workspace_key = _identity(args)
    host = detect_host_context(Path(args.path) if args.path else None)
    session, created = store.open_session(
        agent_id,
        operator_id,
        workspace_key,
        title=args.title,
        host=host,
        parent_session_id=getattr(args, "fork_from", None),
        fork_checkpoint_id=getattr(args, "checkpoint", None),
        fork_reason=getattr(args, "reason", None),
    )
    verb = "opened" if created else "reusing open"
    msg = f"{verb} session {session.session_id} for {workspace_key}"
    if session.parent_session_id:
        msg += f" (forked from {session.parent_session_id[:8]}.. reason: {session.fork_reason})"
    _emit(
        {"created": created, "session": session.to_dict()},
        msg,
        args.json,
    )
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    agent_id, operator_id, workspace_key = _identity(args)
    pack = store.resume(agent_id, operator_id, workspace_key, turn_limit=args.turns)
    _emit(
        pack.to_dict() if pack else None,
        store.render_resume_pack(pack),
        args.json,
    )
    return 0


def _archive_sources(
    sources: list,
    workspace_key: str,
    session_id: Optional[str],
    agent_id: Optional[str],
    operator_id: Optional[str],
) -> list[dict[str, Any]]:
    """Parse and store transcripts, skipping any a reader cannot handle."""
    archived = []
    for source in sources:
        reader = get_reader(source.host)
        if reader is None:
            continue
        try:
            doc = reader.read(source)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            archived.append({"ref": source.ref, "host": source.host, "error": str(exc)})
            continue
        if not doc.turns:
            continue
        transcript_id, changed = store.store_transcript(
            doc,
            workspace_key=workspace_key,
            session_id=session_id,
            agent_id=agent_id,
            operator_id=operator_id,
        )
        archived.append(
            {
                "ref": source.ref,
                "host": source.host,
                "transcript_id": transcript_id,
                "turns": doc.turn_count,
                "redactions": doc.redaction_count,
                "action": "stored" if changed else "unchanged",
            }
        )
    return archived


def cmd_checkpoint(args: argparse.Namespace) -> int:
    agent_id, operator_id, workspace_key = _identity(args)
    host = detect_host_context(Path(args.path) if args.path else None)
    workspace_path = Path(args.path) if args.path else Path.cwd()

    if args.session:
        session_id = args.session
    else:
        session, _ = store.open_session(
            agent_id, operator_id, workspace_key, title=args.title, host=host
        )
        session_id = session.session_id

    # Archive the conversation this checkpoint belongs to and cite it, so the
    # checkpoint's "what" can always be expanded into the underlying "why".
    citations = list(args.cite or [])
    archived: list[dict[str, Any]] = []
    if not args.no_archive:
        sources = discover_transcripts(workspace_path)[:1]
        archived = _archive_sources(sources, workspace_key, session_id, agent_id, operator_id)
        citations += [
            f"{a['host']}:{a['ref']}" for a in archived if a.get("transcript_id")
        ]

    vcs = collect_vcs_state(workspace_path) if not args.no_vcs else None
    checkpoint_id = store.checkpoint(
        session_id,
        next_action=args.next,
        open_plan_path=args.plan,
        vcs_head=vcs.head if vcs else None,
        vcs_branch=vcs.branch if vcs else None,
        vcs_status_summary=vcs.status_summary if vcs else None,
        decisions=args.decision or None,
        transcript_citations=citations or None,
        host=host,
    )
    lines = [f"checkpoint {checkpoint_id} saved for session {session_id}"]
    lines += [
        f"  transcript {a['ref']} ({a.get('turns', '?')} turns, "
        f"{a.get('redactions', 0)} redacted): {a.get('action', a.get('error'))}"
        for a in archived
    ]
    _emit(
        {"checkpoint_id": checkpoint_id, "session_id": session_id, "transcripts": archived},
        "\n".join(lines),
        args.json,
    )
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    agent_id, operator_id, workspace_key = _identity(args)
    workspace_path = Path(args.path) if args.path else Path.cwd()

    sources = discover_transcripts(workspace_path, host=args.host)
    if not args.all:
        sources = sources[: args.limit]
    if not sources:
        _emit([], "no host transcripts found for this workspace", args.json)
        return 0

    archived = _archive_sources(sources, workspace_key, None, agent_id, operator_id)
    text = "\n".join(
        f"  {a.get('action', 'error'):<9} {a['host']}:{a['ref']}  "
        f"{a.get('turns', '?')} turns, {a.get('redactions', 0)} redacted"
        for a in archived
    ) or "nothing archived"
    _emit(archived, f"{len(archived)} transcript(s) from {workspace_key}\n{text}", args.json)
    return 0


def cmd_transcripts(args: argparse.Namespace) -> int:
    _, _, workspace_key = _identity(args)
    records = store.list_transcripts(workspace_key=workspace_key, limit=args.limit)
    text = "\n".join(
        f"{r.captured_at}  {r.turn_count:>4} turns  {r.body_bytes:>8}B  "
        f"{r.source_host}:{r.source_ref}"
        for r in records
    ) or "no archived transcripts for this workspace"
    _emit([r.to_dict() for r in records], text, args.json)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    _, _, workspace_key = _identity(args)
    doc = store.load_transcript(
        transcript_id=getattr(args, "id", None),
        source_ref=args.ref,
        workspace_key=None if (args.ref or getattr(args, "id", None)) else workspace_key,
    )
    if doc is None:
        print("no archived transcript found. Run 'agentloom-session archive' first.")
        return 0

    if args.around:
        doc = doc.around(args.around, radius=args.radius)
    elif args.last:
        doc = doc.tail(args.last)
    if args.format == "json":
        print(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False))
    elif args.format == "markdown":
        print(render_markdown(doc))
    else:
        print(render_text(doc))
    return 0


def _embed_fn(batch_size: int = 64):
    from agentloom_runtime.memory.embedding_provider import embed_texts

    def _run(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            out.extend(embed_texts(texts[i : i + batch_size]))
        return out

    return _run


def cmd_index(args: argparse.Namespace) -> int:
    _, _, workspace_key = _identity(args)
    from agentloom_runtime.memory.embedding_provider import get_embedding_model

    model = args.model or get_embedding_model()
    embed_fn = None if args.no_embed else _embed_fn()
    try:
        stats = store.index_workspace(
            workspace_key,
            model=model,
            embed_fn=embed_fn,
            limit=0 if args.all else args.limit,
        )
    except RuntimeError as exc:
        print(
            f"error: {exc}\n"
            "Embeddings need OPENAI_API_KEY. Re-run with --no-embed for a lexical-only index."
        )
        return 1
    _emit(
        stats,
        f"indexed {stats['transcripts']} transcript(s) for {workspace_key}: "
        f"{stats['chunks']} chunks, {stats['embedded']} embedded, "
        f"{stats['unchanged']} unchanged",
        args.json,
    )
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    _, _, workspace_key = _identity(args)
    stats = store.compact_embeddings(
        workspace_key=None if args.all_workspaces else workspace_key
    )
    _emit(
        stats,
        f"converted {stats['converted']} embedding(s) to float32, "
        f"{stats['unreadable']} unreadable, {stats['scanned']} scanned",
        args.json,
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    _, _, workspace_key = _identity(args)
    from agentloom_runtime.memory.embedding_provider import embed_query, get_embedding_model

    model = get_embedding_model()
    query_vec = None if args.lexical else embed_query(args.query, model=model)
    hits = store.search_archive(
        args.query,
        workspace_key=workspace_key,
        since=args.since,
        limit=args.limit,
        query_vec=query_vec,
        model=model,
    )
    lines = []
    for hit in hits:
        lines.append(
            f"{hit.score:.3f}  {hit.granularity:<7}  {hit.source_host}:{hit.source_ref}  "
            f"seq {hit.seq_start}–{hit.seq_end}  {hit.captured_at or ''}"
        )
        if hit.snippet:
            lines.append(f"      {hit.snippet}")
        lines.append(
            f"      replay: agentloom-session replay --ref {hit.source_ref} "
            f"--around {hit.seq}"
        )
    _emit(
        [h.to_dict() for h in hits],
        "\n".join(lines) or "no matching conversations",
        args.json,
    )
    return 0


def cmd_turn(args: argparse.Namespace) -> int:
    session_id = _resolve_session_id(args)
    turn_id = store.add_turn(session_id, args.role, args.summary)
    _emit({"turn_id": turn_id, "session_id": session_id}, f"turn {turn_id} added", args.json)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    import os

    sessions = store.search_sessions(
        agent_id=args.agent or os.environ.get(DEFAULT_AGENT_ENV),
        operator_id=args.operator,
        workspace_key=args.workspace,
        status=args.status,
        limit=args.limit,
    )
    text = "\n".join(
        f"{s.updated_at}  {s.status:<7} {s.session_id}  {s.agent_id}  {s.workspace_key}"
        for s in sessions
    ) or "no sessions found"
    _emit([s.to_dict() for s in sessions], text, args.json)
    return 0


def cmd_park(args: argparse.Namespace) -> int:
    session_id = _resolve_session_id(args)
    ok = store.park_session(session_id)
    _emit({"session_id": session_id, "parked": ok}, f"parked {session_id}", args.json)
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    session_id = _resolve_session_id(args)
    ok = store.close_session(session_id)
    _emit({"session_id": session_id, "closed": ok}, f"closed {session_id}", args.json)
    return 0


def _render_tree_node(node: dict[str, Any], prefix: str = "", is_last: bool = True) -> list[str]:
    lines = []
    connector = "└── " if is_last else "├── "
    s = node
    status_tag = f"[{s.get('status', 'open')}]"
    title_part = f" - {s['title']}" if s.get("title") else ""
    reason_part = f" (forked: {s['fork_reason']})" if s.get("fork_reason") else ""
    line = f"{prefix}{connector}{status_tag} {s['session_id'][:8]}.. ({s.get('operator_id')} @ {s.get('agent_id')}){title_part}{reason_part}"
    lines.append(line)

    children = node.get("children", [])
    child_prefix = prefix + ("    " if is_last else "│   ")
    for i, child in enumerate(children):
        lines.extend(_render_tree_node(child, child_prefix, is_last=(i == len(children) - 1)))
    return lines


def cmd_tree(args: argparse.Namespace) -> int:
    _, _, workspace_key = _identity(args)
    roots = store.get_workspace_session_tree(workspace_key)
    if args.json:
        _emit(roots, "", True)
        return 0

    if not roots:
        print(f"No sessions found for workspace: {workspace_key}")
        return 0

    lines = [f"Session DAG for workspace: {workspace_key}"]
    for i, root in enumerate(roots):
        lines.extend(_render_tree_node(root, "", is_last=(i == len(roots) - 1)))
    print("\n".join(lines))
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    session_id = _resolve_session_id(args)
    lineage = store.get_session_lineage(session_id)
    if args.json:
        _emit(lineage, "", True)
        return 0

    cur = lineage["session"]
    lines = [
        f"Session: {cur['session_id']} [{cur['status']}]",
        f"  Agent:     {cur['agent_id']}",
        f"  Operator:  {cur['operator_id']}",
        f"  Workspace: {cur['workspace_key']}",
    ]
    if cur.get("fork_reason"):
        lines.append(f"  Fork Reason:     {cur['fork_reason']}")
    if cur.get("fork_checkpoint_id"):
        lines.append(f"  Fork Checkpoint: {cur['fork_checkpoint_id']}")

    ancestors = lineage.get("ancestors", [])
    if ancestors:
        lines.append("\nAncestors (oldest to newest parent):")
        for anc in reversed(ancestors):
            lines.append(f"  ▲ {anc['session_id'][:8]}.. [{anc['status']}] ({anc['agent_id']}) {anc.get('title') or ''}")
    else:
        lines.append("\nAncestors: (root session, no parent)")

    children = lineage.get("children", [])
    if children:
        lines.append("\nDirect Child Forks:")
        for ch in children:
            lines.append(f"  ▼ {ch['session_id'][:8]}.. [{ch['status']}] ({ch['agent_id']}) [reason: {ch.get('fork_reason') or 'none'}] {ch.get('title') or ''}")
    else:
        lines.append("\nDirect Child Forks: (none)")

    print("\n".join(lines))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from agentloom_runtime.session.mcp import run_stdio_server

    return run_stdio_server()


def cmd_ui(args: argparse.Namespace) -> int:
    from agentloom_runtime.session.ui import run_server

    return run_server(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


def cmd_init(args: argparse.Namespace) -> int:
    from agentloom_runtime.db import migrate

    group = None if args.group == "all" else args.group

    if args.status:
        states = migrate.status(group)
        print(f"migrations: {migrate.migrations_dir()}")
        print(migrate.describe(states))
        return 0

    actions = migrate.apply_migrations(group, baseline=args.baseline)
    applied = [name for name, action in actions if action != "skipped"]
    skipped = [name for name, action in actions if action == "skipped"]

    verb = "recorded (not executed)" if args.baseline else "applied"
    if applied:
        print(f"{verb}: {len(applied)}")
        for name in applied:
            print(f"  + {name}")
    if skipped:
        print(f"already applied: {len(skipped)}")
    if not applied:
        print("schema is up to date.")
    return 0


def _doctor_checks(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """Run every self-check. Returns ``(name, status, detail)`` triples.

    Ordered so the first failure is the most upstream cause: an unreachable
    database explains a migration check that could not run, and there is no
    point reporting identity if nothing can be stored.
    """
    import os

    checks: list[tuple[str, str, str]] = []

    import agentloom_runtime

    checks.append(("package", "ok", str(Path(agentloom_runtime.__file__).parent)))

    from agentloom_runtime.config import find_env_file

    env_file = find_env_file()
    checks.append(("config", "ok" if env_file else "warn",
                   str(env_file) if env_file else "no .env found; relying on the process environment"))

    settings = None
    try:
        from agentloom_runtime.db import get_database_settings

        settings = get_database_settings()
        secret = "set" if getattr(settings, "password", None) else "MISSING"
        checks.append((
            "db config", "ok",
            f"{settings.user}@{settings.host}:{settings.port}/{settings.database} (password: {secret})",
        ))
    except Exception as exc:  # noqa: BLE001 - a config error is a reportable result
        checks.append(("db config", "fail", str(exc)))

    conn = None
    if settings is not None:
        try:
            from agentloom_runtime.db import connect

            conn = connect()
            conn.execute("SELECT 1").fetchone()
            checks.append(("db connect", "ok", "reachable"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("db connect", "fail", str(exc)))
            conn = None

    if conn is not None:
        try:
            from agentloom_runtime.db import migrate

            group = None if args.group == "all" else args.group
            states = migrate.status(group, conn=conn)
            blocked = [s for s in states if s.state in ("pending", "changed", "missing")]
            summary = ", ".join(
                f"{state}={sum(1 for s in states if s.state == state)}"
                for state in ("applied", "pending", "changed", "missing")
                if any(s.state == state for s in states)
            )
            checks.append(("migrations", "fail" if blocked else "ok", summary))
            for item in blocked:
                checks.append((
                    f"  {item.migration.filename}", "fail",
                    {
                        "pending": "not applied - run 'agentloom-session init'",
                        "changed": "file differs from what was applied",
                        "missing": "recorded/expected but the SQL file is absent",
                    }[item.state],
                ))
        except Exception as exc:  # noqa: BLE001
            checks.append(("migrations", "fail", str(exc)))
        finally:
            conn.close()

    agent_id = args.agent or os.environ.get(DEFAULT_AGENT_ENV)
    checks.append((
        "agent id", "ok" if agent_id else "fail",
        agent_id or f"unset - export {DEFAULT_AGENT_ENV}=<your-agent>",
    ))
    checks.append(("operator id", "ok", resolve_operator_id(args.operator)))

    try:
        workspace_key = args.workspace or detect_workspace_key(
            Path(args.path) if args.path else None
        )
        local = workspace_key.startswith("local:")
        checks.append((
            "workspace key", "warn" if local else "ok",
            workspace_key + (" (no VCS remote; will not match another machine)" if local else ""),
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(("workspace key", "fail", str(exc)))

    # The documented silent failure: an archive that embeds fine at index time
    # while every query quietly falls back to lexical-only ranking.
    try:
        import contextlib
        import io

        from agentloom_runtime.memory.embedding_provider import embed_query, get_embedding_model

        model = get_embedding_model()
        # The provider narrates its own failure on the console. Swallow it:
        # this check reports the outcome itself, and a stray line both muddles
        # the report and risks making --json unparseable downstream.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            vector = embed_query("healthcheck", model=model)
        if vector:
            checks.append(("embeddings", "ok", f"{model} ({len(vector)} dims)"))
        else:
            checks.append(("embeddings", "warn", "no query vector; search is lexical-only"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("embeddings", "warn", f"unavailable; search is lexical-only ({exc})"))

    from agentloom_runtime.session.readers import READERS

    root = Path(args.path) if args.path else Path.cwd()
    for reader in READERS:
        try:
            found = reader.discover(root)
        except Exception as exc:  # noqa: BLE001
            checks.append((f"reader:{reader.host}", "warn", f"discovery failed ({exc})"))
            continue
        checks.append((
            f"reader:{reader.host}", "ok" if found else "warn",
            f"{len(found)} transcript(s) for this checkout" if found else "no transcripts here",
        ))

    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = _doctor_checks(args)

    if args.json:
        payload = [{"check": name, "status": state, "detail": detail} for name, state, detail in checks]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for name, state, detail in checks:
            mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}.get(state, state)
            print(f"[{mark}] {name:<22} {detail}")

    failures = sum(1 for _, state, _ in checks if state == "fail")
    if failures and not args.json:
        print(f"\n{failures} check(s) failed.")
    return 1 if failures else 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", help=f"agent id (default: ${DEFAULT_AGENT_ENV})")
    parser.add_argument("--operator", help="operator id (default: $AGENTLOOM_OPERATOR_ID or OS user)")
    parser.add_argument("--workspace", help="workspace key override (default: normalized VCS remote)")
    parser.add_argument("--path", help="repository path (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentloom-session",
        description="Host-agnostic working-session memory for AgentLoom agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("whoami", help="show the resolved session identity")
    _add_common(p)
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("open", help="open (or reuse) a session for this identity")
    _add_common(p)
    p.add_argument("--title", help="short session title")
    p.add_argument("--fork-from", dest="fork_from", help="parent session id to fork from")
    p.add_argument("--checkpoint", help="checkpoint id to fork from (default: latest of parent)")
    p.add_argument("--reason", help="fork reason (e.g. host_switch, subtask_branch, continuation)")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("tree", help="show session DAG hierarchy for this workspace")
    _add_common(p)
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("lineage", help="show ancestry and child forks for a session")
    _add_common(p)
    p.add_argument("--session", help="explicit session id (default: this identity's open session)")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("resume", help="print the resume pack for this identity")
    _add_common(p)
    p.add_argument("--turns", type=int, default=10, help="recent turn summaries to include")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("checkpoint", help="record a resume point")
    _add_common(p)
    p.add_argument("--session", help="explicit session id (default: this identity's open session)")
    p.add_argument("--title", help="session title if one must be opened")
    p.add_argument("--next", help="the next action for whoever resumes")
    p.add_argument("--plan", help="path to the plan or document being worked on")
    p.add_argument("--decision", action="append", help="a decision made (repeatable)")
    p.add_argument("--cite", action="append", help="external transcript reference (repeatable)")
    p.add_argument("--no-vcs", action="store_true", help="skip working-tree capture")
    p.add_argument(
        "--no-archive",
        action="store_true",
        help="do not archive and cite this host's current transcript",
    )
    p.set_defaults(func=cmd_checkpoint)

    p = sub.add_parser("archive", help="capture this host's conversation transcripts")
    _add_common(p)
    p.add_argument("--host", help="only read one host's transcripts")
    p.add_argument("--all", action="store_true", help="archive every transcript found")
    p.add_argument("--limit", type=int, default=1, help="how many newest to archive")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("transcripts", help="list archived transcripts for this workspace")
    _add_common(p)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_transcripts)

    p = sub.add_parser("replay", help="print an archived conversation")
    _add_common(p)
    p.add_argument("--ref", help="source reference (default: newest for this workspace)")
    p.add_argument("--id", help="transcript id")
    p.add_argument("--last", type=int, default=0, help="only the last N turns (0 = all)")
    p.add_argument("--around", type=int, help="centre seq from a search hit")
    p.add_argument("--radius", type=int, default=10, help="turns either side of --around")
    p.add_argument(
        "--format", choices=["text", "markdown", "json"], default="text"
    )
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("index", help="build the search index over archived conversations")
    _add_common(p)
    p.add_argument("--all", action="store_true", help="index every archived transcript")
    p.add_argument("--limit", type=int, default=50, help="newest N to index (ignored with --all)")
    p.add_argument("--no-embed", action="store_true", help="lexical index only; skip embeddings")
    p.add_argument("--model", help="embedding model name (default: EMBEDDING_MODEL)")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser(
        "compact", help="re-encode legacy JSON embeddings as compact float32"
    )
    _add_common(p)
    p.add_argument(
        "--all-workspaces",
        action="store_true",
        help="convert every workspace, not only the current one",
    )
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("search", help="find archived conversations by what was said")
    _add_common(p)
    p.add_argument("query", help="natural-language or keyword query")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--since", help="only conversations captured at or after this timestamp")
    p.add_argument("--lexical", action="store_true", help="skip vector search")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("turn", help="append a short turn summary")
    _add_common(p)
    p.add_argument("--session", help="explicit session id")
    p.add_argument("--role", required=True, choices=["human", "agent", "system"])
    p.add_argument("--summary", required=True)
    p.set_defaults(func=cmd_turn)

    p = sub.add_parser("list", help="list sessions")
    _add_common(p)
    p.add_argument("--status", choices=["open", "parked", "closed"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("park", help="pause a session")
    _add_common(p)
    p.add_argument("--session", help="explicit session id")
    p.set_defaults(func=cmd_park)

    p = sub.add_parser("close", help="close a session")
    _add_common(p)
    p.add_argument("--session", help="explicit session id")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("init", help="create or update the database schema")
    p.add_argument(
        "--group",
        choices=["session", "core", "all"],
        default="session",
        help="which schema to install (default: session — Layer 0 memory only)",
    )
    p.add_argument("--status", action="store_true", help="show migration state without changing anything")
    p.add_argument(
        "--baseline",
        action="store_true",
        help="record migrations as applied without executing them (adopt a hand-built database)",
    )
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="check configuration, schema, identity, and hosts")
    _add_common(p)
    p.add_argument(
        "--group",
        choices=["session", "core", "all"],
        default="session",
        help="which schema to verify (default: session)",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("mcp", help="run the Model Context Protocol (MCP) server over stdio")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ui", help="launch the Layer 0 Session Viewer web UI")
    p.add_argument("--host", default="127.0.0.1", help="server host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8766, help="server port (default: 8766)")
    p.add_argument("--no-browser", action="store_true", help="do not open browser automatically")
    p.set_defaults(func=cmd_ui)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
