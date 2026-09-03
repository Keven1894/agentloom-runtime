-- captured_at is when the host last wrote the conversation, not when we
-- ingested it into the archive.
--
-- 005 declared ON UPDATE CURRENT_TIMESTAMP. Every archive pass, title rename,
-- or incidental UPDATE then stamped the row with "now", so a listing of a
-- hundred conversations all showed the night they were first imported.
-- The application writes this column from the host file's mtime; MySQL must
-- not override that.
--
-- Requires 005_session_transcripts.sql.

SET NAMES utf8mb4;

ALTER TABLE `session_transcripts`
  MODIFY COLUMN `captured_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3);
