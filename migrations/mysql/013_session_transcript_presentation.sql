-- Optional trilingual listing pack: title + short description.
--
-- The derived `title` column stays the fallback. This JSON is filled by a
-- later summarizer (or a one-off experiment) and is never required to read
-- the archive. Re-archiving must not wipe it.
--
-- Shape:
--   {"title": {"original": "...", "en": "...", "es": "..."},
--    "description": {"original": "...", "en": "...", "es": "..."}}
--
-- Requires 010_session_transcript_titles.sql.

SET NAMES utf8mb4;

ALTER TABLE `session_transcripts`
  ADD COLUMN `presentation_json` JSON NULL AFTER `title_source`;
