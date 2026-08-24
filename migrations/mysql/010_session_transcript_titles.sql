-- Human-readable conversation titles for the Layer 0 archive.
--
-- A transcript's natural key is a UUID, which is useless to a human scanning a
-- list. The title is derived from the conversation's own opening prompts and
-- stored here so that listing does not have to decompress every body.
--
-- Derived, not authoritative: it is recomputed from the body on every archive
-- write, so a changed derivation rule takes effect on the next capture. Older
-- rows can be refreshed by re-deriving from `body_zlib`.
--
-- Requires 005_session_transcripts.sql.
--
-- Additive: the column is nullable and every reader treats a NULL title as
-- "fall back to the source ref", so this may be applied before the code that
-- populates it.

SET NAMES utf8mb4;

ALTER TABLE `session_transcripts`
  ADD COLUMN `title` VARCHAR(255) NULL AFTER `operator_id`;
