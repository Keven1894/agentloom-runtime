"""Runtime health checks for KG schema and integrity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agentloom_runtime.kg.paths import get_kg_dir
from agentloom_runtime.quality.integrity import validate_default_kgs
from agentloom_runtime.quality.schema_validate import validate_all_schemas


@dataclass
class HealthReport:
    timestamp: str
    overall_status: str
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)


def run_health_check(*, kg_dir: Path | None = None) -> HealthReport:
    """Run schema + integrity checks against the configured KG directory."""
    kg_dir = kg_dir or get_kg_dir()
    report = HealthReport(timestamp=datetime.now().isoformat(), overall_status="UNKNOWN")

    passed, failed, failures = validate_all_schemas(kg_dir=kg_dir)
    schema_status = "PASS" if failed == 0 else "FAIL"
    report.checks["schema_validation"] = {
        "status": schema_status,
        "passed": passed,
        "failed": failed,
        "failures": failures,
    }

    integrity_reports = validate_default_kgs(kg_dir=kg_dir)
    integrity_failed = [role for role, item in integrity_reports.items() if not item.passed]
    integrity_status = "PASS" if not integrity_failed else "FAIL"
    report.checks["kg_integrity"] = {
        "status": integrity_status,
        "roles": {
            role: {
                "passed": item.passed,
                "errors": len(item.errors),
                "warnings": len(item.warnings),
            }
            for role, item in integrity_reports.items()
        },
    }

    statuses = [check["status"] for check in report.checks.values()]
    if "FAIL" in statuses:
        report.overall_status = "FAIL"
    elif "WARN" in statuses:
        report.overall_status = "WARN"
    else:
        report.overall_status = "PASS"
    return report
