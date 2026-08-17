-- Conversation archive for Layer 0.
--
-- Checkpoints are the index: a few hundred bytes, loaded on every session
-- start. Transcripts are the archive: a few hundred kilobytes, paged in on
-- demand when someone needs to know what was actually said.
--
-- Bodies are stored compressed as normalized JSON, already redacted at capture
-- time. A transcript may outlive its session, so the link is nullable and
-- clears rather than cascades.
--
-- Requires 004_session_memory.sql (foreign key to agent_sessions).

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `session_transcripts` (
  `transcript_id`   CHAR(36)     NOT NULL,
  `session_id`      CHAR(36)     NULL,
  `schema_version`  SMALLINT     NOT NULL DEFAULT 1,
  -- Which host recorded it, and that host's own identifier for it. Together
  -- these are the natural key: re-archiving a growing conversation updates the
  -- same row instead of accumulating near-duplicates.
  `source_host`     VARCHAR(64)  NOT NULL,
  `source_ref`      VARCHAR(255) NOT NULL,
  `workspace_key`   VARCHAR(512) NOT NULL,
  `agent_id`        VARCHAR(128) NULL,
  `operator_id`     VARCHAR(128) NULL,
  `captured_at`     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                 ON UPDATE CURRENT_TIMESTAMP(3),
  `turn_count`      INT          NOT NULL DEFAULT 0,
  `redaction_count` INT          NOT NULL DEFAULT 0,
  `body_bytes`      INT          NOT NULL DEFAULT 0,
  `content_sha256`  CHAR(64)     NOT NULL,
  `body_zlib`       MEDIUMBLOB   NULL,
  PRIMARY KEY (`transcript_id`),
  UNIQUE KEY `uq_session_transcripts_source` (`source_host`, `source_ref`),
  KEY `idx_session_transcripts_session` (`session_id`, `captured_at`),
  KEY `idx_session_transcripts_workspace` (`workspace_key`(191), `captured_at`),
  CONSTRAINT `fk_session_transcripts_session`
    FOREIGN KEY (`session_id`) REFERENCES `agent_sessions` (`session_id`)
    ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
