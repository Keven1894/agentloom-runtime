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
            "(e.g. AGENTLOOM_AGENT_ID=envita-builder)."
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
        agent_id, operator_id, workspace_key, title=args.title, host=host
    )
    verb = "opened" if created else "reusing open"
    _emit(
        {"created": created, "session": session.to_dict()},
        f"{verb} session {session.session_id} for {workspace_key}",
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
        source_ref=args.ref,
        workspace_key=None if args.ref else workspace_key,
    )
    if doc is None:
        print("no archived transcript found. Run 'agentloom-session archive' first.")
        return 0

    doc = doc.tail(args.last)
    if args.format == "json":
        print(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False))
    elif args.format == "markdown":
        print(render_markdown(doc))
    else:
        print(render_text(doc))
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
    p.set_defaults(func=cmd_open)

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
    p.add_argument("--last", type=int, default=0, help="only the last N turns (0 = all)")
    p.add_argument(
        "--format", choices=["text", "markdown", "json"], default="text"
    )
    p.set_defaults(func=cmd_replay)

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

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
