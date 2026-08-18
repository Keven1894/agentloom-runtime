-- Archive locator for Layer 0 transcripts.
--
-- The conversation body stays in session_transcripts. This table holds the
-- search index over it: session-level nodes plus overlapping prose windows,
-- human/agent text only. Embeddings are optional — lexical search still works
-- when they are NULL.
--
-- Requires 005_session_transcripts.sql.

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `session_transcript_chunks` (
  `chunk_id`        CHAR(36)     NOT NULL,
  `transcript_id`   CHAR(36)     NOT NULL,
  `workspace_key`   VARCHAR(512) NOT NULL,
  `source_host`     VARCHAR(64)  NOT NULL,
  `source_ref`      VARCHAR(255) NOT NULL,
  `granularity`     VARCHAR(16)  NOT NULL,
  `seq_start`       INT          NOT NULL,
  `seq_end`         INT          NOT NULL,
  `captured_at`     DATETIME(3)  NULL,
  `content`         LONGTEXT     NOT NULL,
  `content_sha256`  CHAR(64)     NOT NULL,
  `embedding`       JSON         NULL,
  `embedding_model` VARCHAR(191) NOT NULL,
  `created_at`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                 ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`chunk_id`),
  UNIQUE KEY `uq_session_transcript_chunks` (
    `transcript_id`, `granularity`, `seq_start`, `seq_end`, `embedding_model`
  ),
  KEY `idx_session_transcript_chunks_workspace` (`workspace_key`(191), `captured_at`),
  KEY `idx_session_transcript_chunks_ref` (`source_ref`),
  KEY `idx_session_transcript_chunks_model` (`embedding_model`),
  CONSTRAINT `fk_session_transcript_chunks_transcript`
    FOREIGN KEY (`transcript_id`) REFERENCES `session_transcripts` (`transcript_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
