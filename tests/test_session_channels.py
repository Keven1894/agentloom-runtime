"""Layer 0 channel search: neither channel filters the other."""

from __future__ import annotations

from unittest.mock import patch

import numpy

from agentloom_runtime.session import store
from agentloom_runtime.session.index import cjk_runs, encode_vector, fuse_channels
from agentloom_runtime.session.sidecar import VectorSidecar


def _item(cid, tid, gran="window", locale="original"):
    return {
        "id": cid,
        "transcript_id": tid,
        "workspace_key": "w",
        "source_host": "cursor",
        "source_ref": tid,
        "granularity": gran,
        "locale": locale,
        "seq_start": 1,
        "seq_end": 2,
        "captured_at": "2026-09-01T00:00:00",
        "content": f"content {cid}",
    }


def test_dense_only_overlay_hit_survives_fusion():
    items = [_item("en-1", "t-en"), _item("es-1", "t-es", locale="es")]
    ranked = fuse_channels(items, ["en-1"], ["es-1"], limit=5)
    assert {row["id"] for row in ranked} == {"en-1", "es-1"}


def test_one_pointer_per_transcript_prefers_the_window():
    items = [_item("s", "t", gran="session"), _item("w", "t")]
    ranked = fuse_channels(items, ["s"], ["w"], limit=5)
    assert [row["id"] for row in ranked] == ["w"]


def test_cjk_runs_keep_only_cjk():
    assert cjk_runs("MySQL 会话存档 search 检索") == "会话存档 检索"
    assert cjk_runs("no cjk here") == ""


class _SidecarConn:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []
        self._last = None

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "BIT_XOR" in sql:
            import zlib

            checksum = 0
            for row in self.rows:
                checksum ^= zlib.crc32(f"{row['chunk_id']}:{row['content_sha256']}".encode())
            self._last = [{"n": len(self.rows), "checksum": checksum}]
        elif "embedding_f32, embedding_dim" in sql:
            wanted = set(params)
            self._last = [
                {"chunk_id": r["chunk_id"], "embedding_f32": r["blob"], "embedding_dim": 3}
                for r in self.rows
                if r["chunk_id"] in wanted
            ]
        else:
            self._last = [
                {"chunk_id": r["chunk_id"], "content_sha256": r["content_sha256"], "captured_at": r["captured_at"]}
                for r in self.rows
            ]
        return self

    def fetchone(self):
        return self._last[0]

    def fetchall(self):
        return self._last

    def close(self):
        pass


def _vec_row(cid, vec, sha="h", captured="2026-09-01T00:00:00"):
    return {"chunk_id": cid, "content_sha256": sha, "captured_at": captured, "blob": encode_vector(vec)}


def test_sidecar_skips_the_server_check_inside_the_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOM_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTLOOM_SIDECAR_CHECK_SECONDS", "600")
    rows = [_vec_row("a", [1.0, 0.0, 0.0])]
    conn = _SidecarConn(rows)
    VectorSidecar("w", "m", connect=lambda: conn).ensure_synced()
    before = len(conn.sql)
    rows.append(_vec_row("b", [0.0, 1.0, 0.0]))
    status = VectorSidecar("w", "m", connect=lambda: conn).ensure_synced()
    assert len(conn.sql) == before
    assert status.rows == 1


def test_sidecar_syncs_then_only_fetches_new_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOM_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTLOOM_SIDECAR_CHECK_SECONDS", "0")
    rows = [_vec_row("a", [1.0, 0.0, 0.0]), _vec_row("b", [0.0, 1.0, 0.0])]
    conn = _SidecarConn(rows)
    sidecar = VectorSidecar("w", "m", connect=lambda: conn)
    first = sidecar.ensure_synced()
    assert first.fetched == 2
    assert sidecar.search([0.9, 0.1, 0.0], limit=1)[0][0] == "a"

    rows.append(_vec_row("c", [0.0, 0.0, 1.0]))
    reopened = VectorSidecar("w", "m", connect=lambda: conn)
    second = reopened.ensure_synced()
    assert second.fetched == 1
    assert second.rows == 3
    assert reopened.search([0.0, 0.0, 1.0], limit=1)[0][0] == "c"

    third = VectorSidecar("w", "m", connect=lambda: conn).ensure_synced()
    assert third.fetched == 0


def test_sidecar_since_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOM_INDEX_DIR", str(tmp_path))
    rows = [
        _vec_row("old", [1.0, 0.0, 0.0], captured="2026-01-01T00:00:00"),
        _vec_row("new", [0.9, 0.1, 0.0], captured="2026-09-01T00:00:00"),
    ]
    conn = _SidecarConn(rows)
    sidecar = VectorSidecar("w", "m", connect=lambda: conn)
    sidecar.ensure_synced()
    hits = sidecar.search([1.0, 0.0, 0.0], limit=5, since="2026-06-01")
    assert [cid for cid, _ in hits] == ["new"]


def test_channel_mode_is_opt_in(monkeypatch):
    monkeypatch.delenv("AGENTLOOM_SEARCH_MODE", raising=False)
    assert store._search_mode() == "scan"
    monkeypatch.setenv("AGENTLOOM_SEARCH_MODE", "channels")
    assert store._search_mode() == "channels"


def test_channel_path_runs_lexical_without_filtering_dense(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTLOOM_SEARCH_MODE", "channels")
    monkeypatch.setenv("AGENTLOOM_INDEX_DIR", str(tmp_path))
    store._has_cjk_column.cache_clear()

    class _Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None):
            self.sql.append(sql)
            self._sql = sql
            self._params = params
            return self

        def fetchall(self):
            if "MATCH (content)" in self._sql:
                return [{"chunk_id": "lex-1", "ft_score": 2.0}]
            if "WHERE chunk_id IN" in self._sql:
                return [
                    {
                        "chunk_id": cid,
                        "transcript_id": f"t-{cid}",
                        "workspace_key": "w",
                        "source_host": "cursor",
                        "source_ref": f"r-{cid}",
                        "granularity": "window",
                        "locale": "es" if cid == "vec-1" else "original",
                        "seq_start": 1,
                        "seq_end": 2,
                        "captured_at": None,
                        "content": "texto" if cid == "vec-1" else "session memory",
                    }
                    for cid in self._params
                ]
            return []

        def fetchone(self):
            return {"n": 0}

        def close(self):
            pass

    conn = _Conn()

    def fake_search(self, query_vec, *, limit=100, since=None):
        return [("vec-1", 0.9)]

    with patch("agentloom_runtime.session.store.connect", return_value=conn), \
         patch.object(VectorSidecar, "ensure_synced", lambda self: None), \
         patch.object(VectorSidecar, "search", fake_search):
        hits = store.search_archive(
            "session memory",
            workspace_key="w",
            query_vec=[0.1, 0.2, 0.3],
            model="m",
        )
    assert {h.chunk_id for h in hits} == {"lex-1", "vec-1"}
    match_sql = [s for s in conn.sql if "MATCH (content)" in s]
    assert match_sql and "LIMIT ?" in match_sql[0]
    assert not any("embedding_f32" in s for s in conn.sql if "MATCH" in s)
