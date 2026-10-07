-- CJK runs of each chunk, indexed with the n-gram parser.
--
-- The default parser on ft_session_transcript_chunks_content keeps English,
-- Spanish, and identifiers but does not split Chinese. Keeping CJK in its own
-- column lets the n-gram parser index it without re-tokenizing English.
-- Writers fill content_cjk from content; old rows are backfilled by
-- ``agentloom-session backfill-cjk``. An empty column matches nothing, which
-- is the behavior before this migration.

SET NAMES utf8mb4;

ALTER TABLE `session_transcript_chunks`
  ADD COLUMN `content_cjk` LONGTEXT NULL;

ALTER TABLE `session_transcript_chunks`
  ADD FULLTEXT INDEX `ft_session_transcript_chunks_cjk` (`content_cjk`) WITH PARSER ngram;
