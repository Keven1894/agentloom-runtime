-- Compact vector storage for the Layer 0 archive index.
--
-- Embeddings were stored as JSON text. Measured on a 10,121-chunk archive:
-- 299 MB of embeddings against 27 MB of the content they index — 92% of the
-- row data was float serialization overhead. Because `search_archive` selected
-- the column unconditionally, every query paid 10.9 s to transfer it and 1.9 s
-- to parse it, including lexical-only queries that never looked at a vector.
-- Ranking itself took 115 ms.
--
-- The same vectors as little-endian float32 are ~59 MB and decode with a single
-- buffer read. Byte order is fixed explicitly because these rows are written and
-- read across architectures (x86-64 and aarch64 both run this agent).
--
-- The JSON column stays for now so a reader that has not been upgraded still
-- works; 009 drops it once the backfill is verified.
--
-- Requires 006_session_transcript_index.sql.

SET NAMES utf8mb4;

ALTER TABLE `session_transcript_chunks`
  ADD COLUMN `embedding_f32` MEDIUMBLOB NULL COMMENT
    'Little-endian float32 vector; embedding_dim floats. Supersedes embedding JSON.'
    AFTER `embedding`,
  ADD COLUMN `embedding_dim` SMALLINT UNSIGNED NULL COMMENT
    'Dimensionality of embedding_f32, for length validation on read.'
    AFTER `embedding_f32`;
