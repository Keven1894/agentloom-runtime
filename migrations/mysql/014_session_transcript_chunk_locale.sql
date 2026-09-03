-- Search locale for translated listing copy and turn overlays.
--
-- Original chunks stay locale='original'. English/Spanish presentation
-- overlays get their own rows so a query in that language can hit them.
-- The unique key gains locale; existing rows keep the default.
--
-- Requires 006_session_transcript_index.sql and 013_session_transcript_presentation.sql.

SET NAMES utf8mb4;

ALTER TABLE `session_transcript_chunks`
  ADD COLUMN `locale` VARCHAR(16) NOT NULL DEFAULT 'original' AFTER `granularity`;

ALTER TABLE `session_transcript_chunks`
  DROP INDEX `uq_session_transcript_chunks`,
  ADD UNIQUE KEY `uq_session_transcript_chunks` (
    `transcript_id`, `locale`, `granularity`, `seq_start`, `seq_end`, `embedding_model`
  );
