"""``agentloom-hostrules`` — generate each host's rule file from one source."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from agentloom_runtime.hostrules.emit import DEFAULT_MANIFEST, load_manifest, status, sync


def _resolve(args: argparse.Namespace):
    root = Path(args.root or Path.cwd()).resolve()
    manifest_path = Path(args.manifest) if args.manifest else root / DEFAULT_MANIFEST
    if not manifest_path.is_file():
        raise SystemExit(f"error: manifest not found: {manifest_path}")
    return load_manifest(manifest_path, root=root)


def cmd_sync(args: argparse.Namespace) -> int:
    manifest = _resolve(args)
    print(f"source: {manifest.source}")
    for target, action in sync(manifest):
        print(f"  {action:<9} {target.path}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Fail when any host file is missing or stale. Suitable for CI."""
    manifest = _resolve(args)
    entries = status(manifest)
    for entry in entries:
        print(f"  {entry.state:<7} {entry.target.path}")
    drifted = [e for e in entries if not e.current]
    if drifted:
        print(
            f"\n{len(drifted)} host rule file(s) out of date with {manifest.source}. "
            "Run 'agentloom-hostrules sync'.",
            file=sys.stderr,
        )
        return 1
    print(f"\nall {len(entries)} host rule file(s) match {manifest.source}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    manifest = _resolve(args)
    print(f"source: {manifest.source}")
    for target in manifest.targets:
        note = f"  # {target.note}" if target.note else ""
        print(f"  {target.path}{note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentloom-hostrules",
        description="Generate every AI coding host's rule file from one canonical source.",
    )
    parser.add_argument("--manifest", help=f"manifest path (default: {DEFAULT_MANIFEST})")
    parser.add_argument("--root", help="repository root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="write out-of-date host rule files").set_defaults(func=cmd_sync)
    sub.add_parser("check", help="fail if any host rule file is stale").set_defaults(
        func=cmd_check
    )
    sub.add_parser("list", help="list configured targets").set_defaults(func=cmd_list)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
