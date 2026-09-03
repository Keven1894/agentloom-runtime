-- Concurrent work streams in one repository, and the per-host activity record
-- that tells them apart.
--
-- 004 keyed an open session on (agent_id, operator_id, workspace_key) and
-- enforced "at most one open session per identity" in the database. That is
-- right for sequential handoff -- one machine stops, another picks the thread
-- up -- which is the only way this layer had been used. It leaves no room for
-- two machines working *different* streams at the same time: the second one
-- cannot get a slot, and forking to make one parks the first machine's live
-- session out from under it.
--
-- The missing dimension is the work stream, not the machine. Keying on the
-- host would buy concurrency by destroying the reason this layer exists, since
-- a session must stay resumable from anywhere. A lane names *what* is being
-- worked on, so any host can still resume any lane.
--
-- This migration is deliberately additive. It adds the column and the activity
-- table but leaves `open_key` alone, so a host still running pre-lane code
-- keeps seeing exactly one open session per identity and cannot be surprised by
-- a second one. 017 folds the lane into `open_key` once every host is upgraded.

SET NAMES utf8mb4;

ALTER TABLE `agent_sessions`
  ADD COLUMN `lane` VARCHAR(64) NOT NULL DEFAULT 'default' AFTER `workspace_key`,
  ADD KEY `idx_agent_sessions_lane`
    (`agent_id`, `operator_id`, `workspace_key`(191), `lane`, `status`);

-- Which machines are actually working in a session, and when each was last
-- seen there.
--
-- `host` and `ide` are this row's identity, not provenance: the question the
-- table answers is "which machine", so selecting on it is the whole point.
-- That does not weaken 004's host-neutrality invariant, which governs
-- *session lookup* -- that still keys only on
-- (agent_id, operator_id, workspace_key, lane) and never on a machine name.
-- Liveness read from here only ever advises a human.
CREATE TABLE IF NOT EXISTS `session_hosts` (
  `session_id`    CHAR(36)     NOT NULL,
  `host`          VARCHAR(256) NOT NULL,
  `ide`           VARCHAR(64)  NULL,
  `first_seen_at` DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `last_seen_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`session_id`, `host`),
  KEY `idx_session_hosts_seen` (`session_id`, `last_seen_at`),
  CONSTRAINT `fk_session_hosts_session`
    FOREIGN KEY (`session_id`) REFERENCES `agent_sessions` (`session_id`)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
