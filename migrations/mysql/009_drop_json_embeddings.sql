-- Drop the superseded JSON embedding column.
--
-- 008 added `embedding_f32` alongside `embedding` so readers could be upgraded
-- independently of the schema. Once every row carries a compact vector, the
-- JSON column is dead weight that still costs bytes on every table scan and
-- every backup.
--
-- Verified before applying, on the 10,121-chunk production archive:
--   - 0 rows with `embedding` and no `embedding_f32`
--   - max element difference between the two encodings: 4.3e-19
--   - min cosine(original, round-trip): 1.000000000000
-- The round trip is exact because the provider already returns float32
-- precision; the JSON text was storing decimal expansions of the same values.
--
-- Do not apply this until `agentloom-session compact --all-workspaces` reports
-- zero remaining rows. Run OPTIMIZE TABLE afterwards to return the freed pages
-- to the filesystem — DROP COLUMN alone leaves them inside the tablespace.
--
-- Requires 008_session_transcript_vectors.sql.

SET NAMES utf8mb4;

ALTER TABLE `session_transcript_chunks`
  DROP COLUMN `embedding`;
