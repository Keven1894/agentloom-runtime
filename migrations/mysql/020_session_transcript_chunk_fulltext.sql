-- Lexical candidate index for the archive locator.
--
-- search_archive used to read every chunk in the workspace and rank in
-- Python. Two production audits, thirty days apart, showed that cost is linear
-- in the archive: about 45 ms per 1,000 chunks. At 33k chunks hybrid search
-- had already crossed its 4 s gate.
--
-- This index lets MATCH (content) AGAINST (? IN NATURAL LANGUAGE MODE) return
-- a bounded candidate set. Vector comparison then runs on those rows, not on
-- the whole workspace. The default InnoDB parser is intentional: it keeps
-- English, Spanish, and identifiers (an underscore stays inside a word). An
-- n-gram parser would also re-tokenize English, and the probes this change is
-- gated on are English.
--
-- Old code never mentions the index, so applying this before every host has
-- pulled is safe. New code treats a missing index or an empty MATCH as a
-- reason to scan, which is the behavior this migration replaces rather than
-- a new failure.

SET NAMES utf8mb4;

ALTER TABLE `session_transcript_chunks`
  ADD FULLTEXT INDEX `ft_session_transcript_chunks_content` (`content`);
