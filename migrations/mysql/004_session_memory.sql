-- Layer 0 working-session memory: cross-host, host-agnostic agent session continuity.
--
-- Purpose: let the same agent + operator resume work on a repository from a
-- different machine or a different IDE, without reading any editor-local store.
--
-- Host neutrality invariant: session identity is (agent_id, operator_id,
-- workspace_key) where workspace_key is a normalized VCS remote URL. The
-- *_hint columns are write-only provenance; they must never appear in a
-- resume lookup predicate.
--
-- Indexes are declared inline so this file is idempotent under
-- CREATE TABLE IF NOT EXISTS (MySQL has no CREATE INDEX IF NOT EXISTS).

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `agent_sessions` (
  `session_id`          CHAR(36)     NOT NULL,
  `agent_id`            VARCHAR(128) NOT NULL,
  `operator_id`         VARCHAR(128) NOT NULL,
  `workspace_key`       VARCHAR(512) NOT NULL,
  `status`              ENUM('open','parked','closed') NOT NULL DEFAULT 'open',
  `title`               VARCHAR(512) NULL,
  -- Write-only provenance. Never used to look up a session.
  `workspace_path_hint` VARCHAR(1024) NULL,
  `host_hint`           VARCHAR(256) NULL,
  `ide_hint`            VARCHAR(64)  NULL,
  `created_at`          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                     ON UPDATE CURRENT_TIMESTAMP(3),
  `last_checkpoint_at`  DATETIME(3)  NULL,
  -- Enforces "at most one open session per identity" in the database rather
  -- than in application code. NULL for parked/closed rows, so history is kept.
  `open_key` CHAR(64) GENERATED ALWAYS AS (
    IF(`status` = 'open',
       SHA2(CONCAT(`agent_id`, CHAR(10), `operator_id`, CHAR(10), `workspace_key`), 256),
       NULL)
  ) STORED,
  PRIMARY KEY (`session_id`),
  UNIQUE KEY `uq_agent_sessions_open` (`open_key`),
  KEY `idx_agent_sessions_identity` (`agent_id`, `operator_id`, `workspace_key`(191), `status`),
  KEY `idx_agent_sessions_workspace` (`workspace_key`(191)),
  KEY `idx_agent_sessions_updated` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `session_checkpoints` (
  `checkpoint_id`             CHAR(36)     NOT NULL,
  `session_id`                CHAR(36)     NOT NULL,
  `schema_version`            SMALLINT     NOT NULL DEFAULT 1,
  `created_at`                DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `host_hint`                 VARCHAR(256) NULL,
  `ide_hint`                  VARCHAR(64)  NULL,
  `vcs_head`                  VARCHAR(64)  NULL,
  `vcs_branch`                VARCHAR(256) NULL,
  `vcs_status_summary`        TEXT         NULL,
  `open_plan_path`            VARCHAR(512) NULL,
  `next_action`               TEXT         NULL,
  `decisions_json`            JSON         NULL,
  -- References to external transcript archives (UUIDs/paths only, never bodies).
  `transcript_citations_json` JSON         NULL,
  `payload_json`              JSON         NULL,
  PRIMARY KEY (`checkpoint_id`),
  KEY `idx_session_checkpoints_session` (`session_id`, `created_at`),
  CONSTRAINT `fk_session_checkpoints_session`
    FOREIGN KEY (`session_id`) REFERENCES `agent_sessions` (`session_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Optional narrative layer. Stores short summaries, never full host transcripts.
CREATE TABLE IF NOT EXISTS `session_turns` (
  `turn_id`    CHAR(36)     NOT NULL,
  `session_id` CHAR(36)     NOT NULL,
  `seq`        INT          NOT NULL,
  `role`       ENUM('human','agent','system') NOT NULL,
  `summary`    TEXT         NOT NULL,
  `created_at` DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`turn_id`),
  UNIQUE KEY `uq_session_turns_seq` (`session_id`, `seq`),
  CONSTRAINT `fk_session_turns_session`
    FOREIGN KEY (`session_id`) REFERENCES `agent_sessions` (`session_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
