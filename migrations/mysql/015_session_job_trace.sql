-- Durable trace for long-running jobs over the Layer 0 archive.
--
-- A batch job that translates, re-embeds, or re-summarizes transcripts needs
-- three things to survive a machine change: where it got to, what it decided,
-- and why. Keeping any of that in a file next to the checkout makes it the
-- property of one laptop, which is the exact failure mode Layer 0 exists to
-- avoid.
--
-- Progress alone would not justify a table: which turns are translated is
-- already derivable from `session_transcripts.presentation_json`, and a
-- partial overlay is a legal state because missing sequences fall back to the
-- original. What is genuinely unreconstructable is the judgement — a reviewer
-- model's score, its diagnosis, and the patch it proposed. Re-deriving that
-- costs another paid audit and may not even reproduce.
--
-- Tables are named for jobs rather than for translation so that re-embedding
-- and re-summarizing runs land in the same audit surface.
--
-- Requires 005_session_transcripts.sql.

SET NAMES utf8mb4;

-- One invocation of a batch job.
CREATE TABLE IF NOT EXISTS `session_job_runs` (
  `run_id`        CHAR(36)     NOT NULL,
  `job_kind`      VARCHAR(64)  NOT NULL,
  `host`          VARCHAR(64)  NULL,
  `operator_id`   VARCHAR(128) NULL,
  `workspace_key` VARCHAR(512) NULL,
  -- Invocation arguments: models, thresholds, selection filters. Enough to
  -- answer "what settings produced this verdict" without reading a shell log.
  `args_json`     JSON         NULL,
  `status`        VARCHAR(16)  NOT NULL DEFAULT 'running',
  `started_at`    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `finished_at`   DATETIME(3)  NULL,
  `items_total`   INT          NOT NULL DEFAULT 0,
  `items_done`    INT          NOT NULL DEFAULT 0,
  `items_failed`  INT          NOT NULL DEFAULT 0,
  PRIMARY KEY (`run_id`),
  KEY `idx_session_job_runs_kind` (`job_kind`, `started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Durable per-transcript state. This is the resume ledger and the idempotency
-- key: keyed by work kind and transcript, not by run, so a restart on another
-- machine sees the same completion set.
--
-- `body_sha256` fingerprints the archive body the verdict was made against, so
-- an edited transcript re-enters the queue instead of inheriting a stale pass.
CREATE TABLE IF NOT EXISTS `session_job_items` (
  `job_kind`         VARCHAR(64)   NOT NULL,
  `transcript_id`    CHAR(36)      NOT NULL,
  `last_run_id`      CHAR(36)      NULL,
  `status`           VARCHAR(24)   NOT NULL DEFAULT 'pending',
  `body_sha256`      CHAR(64)      NULL,
  `attempt`          INT           NOT NULL DEFAULT 0,
  `turns_total`      INT           NOT NULL DEFAULT 0,
  `qc_model`         VARCHAR(64)   NULL,
  `qc_score`         DECIMAL(4,3)  NULL,
  `qc_passed`        TINYINT(1)    NULL,
  -- The reviewer's full verdict: metrics, diagnosis, suggested patches, and
  -- the critique handed back to the local model.
  `qc_report_json`   JSON          NULL,
  `patches_applied`  INT           NOT NULL DEFAULT 0,
  `error_text`       TEXT          NULL,
  `started_at`       DATETIME(3)   NULL,
  `updated_at`       DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                                   ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`job_kind`, `transcript_id`),
  KEY `idx_session_job_items_status` (`job_kind`, `status`, `updated_at`),
  KEY `idx_session_job_items_run` (`last_run_id`),
  CONSTRAINT `fk_session_job_items_transcript`
    FOREIGN KEY (`transcript_id`) REFERENCES `session_transcripts` (`transcript_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Append-only typed event log. `seq` is monotonic within a run so the stream
-- replays in the order it happened regardless of timestamp granularity.
CREATE TABLE IF NOT EXISTS `session_job_events` (
  `event_id`      BIGINT       NOT NULL AUTO_INCREMENT,
  `run_id`        CHAR(36)     NOT NULL,
  `seq`           INT          NOT NULL,
  `ts`            DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `event_type`    VARCHAR(48)  NOT NULL,
  `transcript_id` CHAR(36)     NULL,
  `payload_json`  JSON         NULL,
  PRIMARY KEY (`event_id`),
  UNIQUE KEY `uq_session_job_events_seq` (`run_id`, `seq`),
  KEY `idx_session_job_events_type` (`run_id`, `event_type`),
  KEY `idx_session_job_events_transcript` (`transcript_id`, `ts`),
  CONSTRAINT `fk_session_job_events_run`
    FOREIGN KEY (`run_id`) REFERENCES `session_job_runs` (`run_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
