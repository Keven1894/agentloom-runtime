-- Distinguish a title someone typed from one derived from the opening prompt.
--
-- 010 stored a title so the listing page need not decompress every body. That
-- title was recomputed on every archive write, which is correct for a derived
-- value and wrong the moment a human renames a conversation: the next
-- checkpoint would silently put the first sentence back.
--
-- `title_source = 'user'` is the lock. `store_transcript` leaves that row's
-- title alone. Clearing the title in the viewer sets the source back to
-- `derived` and restores the opening-prompt value.
--
-- Requires 010_session_transcript_titles.sql.

SET NAMES utf8mb4;

ALTER TABLE `session_transcripts`
  ADD COLUMN `title_source` ENUM('derived','user') NOT NULL DEFAULT 'derived'
    AFTER `title`;
