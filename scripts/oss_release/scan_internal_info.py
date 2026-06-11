#!/usr/bin/env python3
"""Open-source pre-release internal-information scanner.

Scans a directory tree for internal / sensitive markers that must not leak into
a public release: private IPs, institutional identifiers (FIU/EnviStor),
personal names + emails, local filesystem paths, internal DB/host names, and
credential-shaped strings.

This is the automated half of the OSS review gate. A human still signs off on
the residual findings, but every release candidate must pass this first.

Usage:
    python Scripts/oss_release/scan_internal_info.py <path> [options]

Options:
    --json OUT        Write findings as JSON to OUT.
    --allowlist FILE  Path to an allowlist file (one substring per line; lines
                      starting with '#' are comments). Matches whose full line
                      contains an allowlisted substring are downgraded to
                      'allowlisted' and do not fail the gate.
    --config FILE     JSON file overriding/extending the default rule set.
    --warn-only       Always exit 0 (report only; do not fail the gate).

Exit code:
    0  no BLOCK-severity findings (or --warn-only)
    1  at least one BLOCK-severity finding remains after allowlisting
    2  bad invocation / path not found

The rule set is intentionally conservative: it prefers false positives (which a
human dismisses via the allowlist) over false negatives (which leak).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BLOCK = "block"
WARN = "warn"

TEXT_EXTENSIONS = {
    ".py", ".md", ".json", ".jsonl", ".yaml", ".yml", ".sql", ".html", ".htm",
    ".js", ".ts", ".tsx", ".css", ".txt", ".toml", ".cfg", ".ini", ".sh",
    ".env.example", ".cff",
}

# Extensionless files that are still plain text and worth scanning.
TEXT_FILENAMES = {
    "LICENSE", "LICENSE-DOCS", "Makefile", "Dockerfile", "CITATION",
    ".gitignore", ".env.example",
}

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "dist", "build", ".idea", ".vscode", "logs",
}

# Each rule: (id, severity, compiled regex, human description)
DEFAULT_RULES: list[tuple[str, str, str, str]] = [
    # --- Network / hosts -----------------------------------------------------
    ("private-ip-10", BLOCK, r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
     "Private 10.0.0.0/8 IP address"),
    ("private-ip-192", BLOCK, r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
     "Private 192.168.0.0/16 IP address"),
    ("private-ip-172", BLOCK, r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
     "Private 172.16.0.0/12 IP address"),
    ("internal-host", BLOCK, r"\b[\w.-]*\.fiu\.edu\b",
     "fiu.edu internal hostname"),
    # --- Institutional identity ---------------------------------------------
    ("fiu", WARN, r"\bFIU\b", "FIU institutional reference"),
    ("gis-center", WARN, r"\bGIS Center\b", "GIS Center reference"),
    ("envistor", WARN, r"\bEnvi[Ss]tor\b", "EnviStor project name"),
    ("dataverse-fiu", BLOCK, r"dataverse\.fiu\.edu",
     "FIU Dataverse instance URL"),
    # --- People (emails + named individuals) --------------------------------
    ("email", BLOCK, r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "Email address"),
    ("person-name", WARN,
     r"\b(Keven|Boyuan|bguan|Hong|Wencong|Taylor|Jennifer Fu|Jason Liu|"
     r"Leonardo Bobadilla|Sonia Santana|Rebecca Bakke|Mike Kirgan)\b",
     "Named individual"),
    # --- Local filesystem paths ---------------------------------------------
    ("win-drive-path", BLOCK, r"[A-Za-z]:\\\\?(?:projects|Users|__gann__)",
     "Windows local filesystem path"),
    ("repo-abs-path", WARN, r"[Cc]:[\\/]projects[\\/]",
     "Absolute developer repo path"),
    # --- Internal data stores -----------------------------------------------
    ("db-prod", WARN, r"\benvita_prod\b", "Production database name"),
    ("db-sqlite", WARN, r"\benvita\.db\b", "SQLite runtime DB filename"),
    ("s3-bucket", WARN, r"\benvistor-osdf\b", "Internal S3 bucket name"),
    # --- Credential-shaped strings ------------------------------------------
    ("assigned-secret", BLOCK,
     r"(?i)\b(secret|password|passwd|access[_-]?key|api[_-]?key|token)\b\s*"
     r"[:=]\s*['\"][^'\"]{6,}['\"]",
     "Hard-coded credential assignment"),
    ("openai-key", BLOCK, r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI API key literal"),
    ("aws-key", BLOCK, r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
]


@dataclass
class Finding:
    path: str
    line: int
    rule_id: str
    severity: str
    description: str
    excerpt: str
    allowlisted: bool = False


@dataclass
class Ruleset:
    rules: list[tuple[str, str, re.Pattern[str], str]] = field(default_factory=list)

    @classmethod
    def build(cls, config_path: Path | None) -> "Ruleset":
        raw = list(DEFAULT_RULES)
        if config_path:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            for entry in data.get("disable", []):
                raw = [r for r in raw if r[0] != entry]
            for entry in data.get("add", []):
                raw.append((entry["id"], entry.get("severity", WARN),
                            entry["pattern"], entry.get("description", "")))
        compiled = [(rid, sev, re.compile(pat), desc) for rid, sev, pat, desc in raw]
        return cls(compiled)


def load_allowlist(path: Path | None) -> list[str]:
    if not path:
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        suffix = "".join(p.suffixes[-2:]) if len(p.suffixes) > 1 else p.suffix
        if (p.suffix in TEXT_EXTENSIONS or suffix in TEXT_EXTENSIONS
                or p.name in TEXT_FILENAMES):
            yield p


def scan_file(path: Path, rules: Ruleset, allowlist: list[str],
              root: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = str(path.relative_to(root)) if path != root else path.name
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rid, sev, pattern, desc in rules.rules:
            if pattern.search(line):
                allowed = any(token in line for token in allowlist)
                findings.append(Finding(
                    path=rel, line=lineno, rule_id=rid, severity=sev,
                    description=desc, excerpt=line.strip()[:200],
                    allowlisted=allowed,
                ))
    return findings


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="OSS pre-release internal-info scanner")
    ap.add_argument("path")
    ap.add_argument("--json")
    ap.add_argument("--allowlist")
    ap.add_argument("--config")
    ap.add_argument("--warn-only", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"error: path not found: {root}", file=sys.stderr)
        return 2

    rules = Ruleset.build(Path(args.config).resolve() if args.config else None)
    allowlist = load_allowlist(Path(args.allowlist).resolve() if args.allowlist else None)

    all_findings: list[Finding] = []
    for f in iter_files(root):
        all_findings.extend(scan_file(f, rules, allowlist, root))

    active = [f for f in all_findings if not f.allowlisted]
    blocks = [f for f in active if f.severity == BLOCK]
    warns = [f for f in active if f.severity == WARN]
    allowed = [f for f in all_findings if f.allowlisted]

    # Group by rule for the human summary
    by_rule: dict[str, int] = {}
    for f in active:
        by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1

    print(f"Scanned root : {root}")
    print(f"BLOCK findings: {len(blocks)}   WARN findings: {len(warns)}   "
          f"allowlisted: {len(allowed)}")
    print("-" * 70)
    for rid in sorted(by_rule, key=lambda r: -by_rule[r]):
        sample = next(f for f in active if f.rule_id == rid)
        flag = "BLOCK" if sample.severity == BLOCK else "warn "
        print(f"[{flag}] {rid:<18} {by_rule[rid]:>4}  {sample.description}")
    print("-" * 70)
    # Show the first chunk of BLOCK findings with file:line for triage
    for f in blocks[:50]:
        print(f"  {f.path}:{f.line}: [{f.rule_id}] {f.excerpt}")
    if len(blocks) > 50:
        print(f"  ... and {len(blocks) - 50} more BLOCK findings")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "root": str(root),
            "summary": {"block": len(blocks), "warn": len(warns),
                        "allowlisted": len(allowed)},
            "by_rule": by_rule,
            "findings": [f.__dict__ for f in all_findings],
        }, indent=2), encoding="utf-8")
        print(f"\nJSON written to {args.json}")

    if args.warn_only:
        return 0
    return 1 if blocks else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
