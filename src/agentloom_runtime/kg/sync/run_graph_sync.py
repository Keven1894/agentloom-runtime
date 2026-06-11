#!/usr/bin/env python3
"""Run kg graph sync pipeline (JSON nodes + stubs + structural edges + backlink)."""

from __future__ import annotations

import json
import os
import sys

from agentloom_runtime.db import connect, get_database_settings, is_mysql
from agentloom_runtime.kg.sync.graph_sync import run_graph_sync


def main() -> int:
    if not is_mysql():
        print("ERROR: MySQL required")
        return 2
    settings = get_database_settings()
    print(f"target: mysql://{settings.host}:{settings.port}/{settings.database}")
    conn = connect()
    try:
        summary = run_graph_sync(conn, is_mysql=True, dry_run=False)
        conn.commit()
    finally:
        conn.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
