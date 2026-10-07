"""Per-host copy of archive vectors for the dense channel.

MySQL holds the vectors. Shipping every vector to Python on every search
is what made hybrid search linear in the archive, so each host keeps a
float32 matrix on disk and checks it against the server with one
aggregate query before searching. A mismatch re-syncs by diff: only new
or changed rows are fetched.

The sidecar is derived data. Deleting the directory is always safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agentloom_runtime.session.index import VECTOR_DTYPE

ConnectFn = Callable[[], Any]


def index_root() -> Path:
    override = os.environ.get("AGENTLOOM_INDEX_DIR")
    if override:
        return Path(override)
    return Path.home() / ".agentloom" / "index"


def sidecar_dir(workspace_key: str, model: str) -> Path:
    digest = hashlib.sha256(f"{workspace_key}\n{model}".encode("utf-8")).hexdigest()[:16]
    return index_root() / digest


@dataclass
class SidecarStatus:
    path: str
    rows: int
    server_rows: int
    dim: int
    in_sync: bool
    synced_at: Optional[str]
    sync_ms: float = 0.0
    fetched: int = 0


class VectorSidecar:
    """Dense channel for one (workspace, model)."""

    def __init__(self, workspace_key: str, model: str, *, connect: ConnectFn):
        self.workspace_key = workspace_key
        self.model = model
        self._connect = connect
        self.path = sidecar_dir(workspace_key, model)
        self._matrix = None
        self._ids: list[str] = []
        self._shas: list[str] = []
        self._captured: list[str] = []
        self._meta: dict[str, Any] = {}

    def ensure_synced(self) -> SidecarStatus:
        """Compare with the server at most once per check interval.

        The archive is written by a nightly job, and the aggregate check reads
        every row's id and hash, so checking on every search would spend more
        time than the search itself.
        """
        started = time.perf_counter()
        self._load()
        fetched = 0
        if self._meta and self._checked_recently():
            server = {"rows": self._meta.get("rows"), "checksum": self._meta.get("checksum")}
        else:
            server = self._server_signature()
            if server["rows"] != self._meta.get("rows") or server["checksum"] != self._meta.get("checksum"):
                fetched = self._sync(server)
            else:
                self._touch_checked()
        return SidecarStatus(
            path=str(self.path),
            rows=len(self._ids),
            server_rows=int(server["rows"]),
            dim=int(self._meta.get("dim") or 0),
            in_sync=True,
            synced_at=self._meta.get("synced_at"),
            sync_ms=(time.perf_counter() - started) * 1000,
            fetched=fetched,
        )

    def status(self) -> SidecarStatus:
        self._load()
        server = self._server_signature()
        return SidecarStatus(
            path=str(self.path),
            rows=len(self._ids),
            server_rows=int(server["rows"]),
            dim=int(self._meta.get("dim") or 0),
            in_sync=(
                server["rows"] == self._meta.get("rows")
                and server["checksum"] == self._meta.get("checksum")
            ),
            synced_at=self._meta.get("synced_at"),
        )

    def search(
        self,
        query_vec: list[float],
        *,
        limit: int = 100,
        since: Optional[str] = None,
    ) -> list[tuple[str, float]]:
        import numpy

        if self._matrix is None or not self._ids or not query_vec:
            return []
        query = numpy.asarray(query_vec, dtype=VECTOR_DTYPE)
        if query.shape[0] != self._matrix.shape[1]:
            return []
        norm = float(numpy.linalg.norm(query))
        if norm == 0.0:
            return []
        scores = self._matrix @ (query / norm)
        if since:
            mask = numpy.asarray([c >= since for c in self._captured], dtype=bool)
            scores = numpy.where(mask, scores, -numpy.inf)
        count = min(limit, len(self._ids))
        top = numpy.argpartition(-scores, count - 1)[:count]
        top = top[numpy.argsort(-scores[top])]
        return [
            (self._ids[i], float(scores[i]))
            for i in top
            if numpy.isfinite(scores[i])
        ]

    def _checked_recently(self) -> bool:
        raw = os.environ.get("AGENTLOOM_SIDECAR_CHECK_SECONDS", "600")
        try:
            interval = float(raw)
        except ValueError:
            interval = 600.0
        checked = float(self._meta.get("checked_epoch") or 0)
        return interval > 0 and (time.time() - checked) < interval

    def _touch_checked(self) -> None:
        self._meta["checked_epoch"] = time.time()
        tmp_meta = self.path / "meta.json.tmp"
        tmp_meta.write_text(json.dumps(self._meta), encoding="utf-8")
        os.replace(tmp_meta, self.path / "meta.json")

    def _server_signature(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, "
                "       COALESCE(BIT_XOR(CRC32(CONCAT(chunk_id, ':', content_sha256))), 0) AS checksum "
                "FROM session_transcript_chunks "
                "WHERE workspace_key = ? AND embedding_model = ? AND embedding_f32 IS NOT NULL",
                [self.workspace_key, self.model],
            ).fetchone()
        finally:
            conn.close()
        return {"rows": int(row["n"] or 0), "checksum": int(row["checksum"] or 0)}

    def _sync(self, server: dict[str, Any]) -> int:
        import numpy

        conn = self._connect()
        try:
            listing = conn.execute(
                "SELECT chunk_id, content_sha256, captured_at "
                "FROM session_transcript_chunks "
                "WHERE workspace_key = ? AND embedding_model = ? AND embedding_f32 IS NOT NULL",
                [self.workspace_key, self.model],
            ).fetchall()
            local = {cid: (sha, i) for i, (cid, sha) in enumerate(zip(self._ids, self._shas))}
            keep_rows: list[int] = []
            need: list[str] = []
            ids: list[str] = []
            shas: list[str] = []
            captured: list[str] = []
            for row in listing:
                cid = row["chunk_id"]
                sha = row["content_sha256"]
                ids.append(cid)
                shas.append(sha)
                captured.append(_iso(row["captured_at"]))
                prev = local.get(cid)
                if prev and prev[0] == sha:
                    keep_rows.append(prev[1])
                else:
                    keep_rows.append(-1)
                    need.append(cid)
            fetched: dict[str, Any] = {}
            dim = int(self._meta.get("dim") or 0)
            for start in range(0, len(need), 500):
                batch = need[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                for row in conn.execute(
                    "SELECT chunk_id, embedding_f32, embedding_dim FROM session_transcript_chunks "
                    f"WHERE chunk_id IN ({placeholders})",
                    batch,
                ).fetchall():
                    blob = row["embedding_f32"]
                    if not blob:
                        continue
                    vec = numpy.frombuffer(blob, dtype=VECTOR_DTYPE)
                    if dim and vec.shape[0] != dim:
                        continue
                    dim = dim or int(vec.shape[0])
                    fetched[row["chunk_id"]] = vec
        finally:
            conn.close()

        if not ids or not dim:
            self._write(numpy.zeros((0, 0), dtype=VECTOR_DTYPE), [], [], [], server, dim)
            return len(fetched)

        matrix = numpy.zeros((len(ids), dim), dtype=VECTOR_DTYPE)
        keep_ids: list[str] = []
        keep_shas: list[str] = []
        keep_captured: list[str] = []
        out = 0
        for position, cid in enumerate(ids):
            old_row = keep_rows[position]
            if old_row >= 0 and self._matrix is not None:
                vec = self._matrix[old_row]
            else:
                raw = fetched.get(cid)
                if raw is None:
                    continue
                norm = float(numpy.linalg.norm(raw))
                vec = raw / norm if norm else raw
            matrix[out] = vec
            keep_ids.append(cid)
            keep_shas.append(shas[position])
            keep_captured.append(captured[position])
            out += 1
        self._write(matrix[:out], keep_ids, keep_shas, keep_captured, server, dim)
        return len(fetched)

    def _write(self, matrix, ids, shas, captured, server, dim) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        tmp_vec = self.path / "vectors.f32.tmp"
        matrix.astype(VECTOR_DTYPE).tofile(tmp_vec)
        os.replace(tmp_vec, self.path / "vectors.f32")
        meta = {
            "workspace_key": self.workspace_key,
            "model": self.model,
            "dim": int(dim),
            "rows": int(server["rows"]),
            "checksum": int(server["checksum"]),
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "checked_epoch": time.time(),
        }
        rows = {"ids": ids, "shas": shas, "captured": captured}
        tmp_rows = self.path / "rows.json.tmp"
        tmp_rows.write_text(json.dumps(rows), encoding="utf-8")
        os.replace(tmp_rows, self.path / "rows.json")
        tmp_meta = self.path / "meta.json.tmp"
        tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp_meta, self.path / "meta.json")
        self._matrix = matrix
        self._ids, self._shas, self._captured, self._meta = ids, shas, captured, meta

    def _load(self) -> None:
        import numpy

        if self._matrix is not None:
            return
        meta_path = self.path / "meta.json"
        rows_path = self.path / "rows.json"
        vec_path = self.path / "vectors.f32"
        if not (meta_path.is_file() and rows_path.is_file() and vec_path.is_file()):
            self._meta = {}
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            rows = json.loads(rows_path.read_text(encoding="utf-8"))
            dim = int(meta.get("dim") or 0)
            ids = list(rows.get("ids") or [])
            flat = numpy.fromfile(vec_path, dtype=VECTOR_DTYPE)
            if dim == 0 or flat.shape[0] != dim * len(ids):
                self._meta = {}
                return
            self._matrix = flat.reshape(len(ids), dim)
            self._ids = ids
            self._shas = list(rows.get("shas") or [])
            self._captured = list(rows.get("captured") or [])
            self._meta = meta
        except (OSError, ValueError):
            self._meta = {}


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
