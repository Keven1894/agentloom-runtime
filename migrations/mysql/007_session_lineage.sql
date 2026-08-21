-- Session lineage and DAG topology for Layer 0.
--
-- Adds parent session, fork checkpoint, and fork reason to agent_sessions.
-- Lets sessions form an explicit directed acyclic graph (DAG) across hosts.
--
-- Requires 004_session_memory.sql.

SET NAMES utf8mb4;

ALTER TABLE `agent_sessions`
  ADD COLUMN `parent_session_id` CHAR(36) NULL AFTER `workspace_key`,
  ADD COLUMN `fork_checkpoint_id` CHAR(36) NULL AFTER `parent_session_id`,
  ADD COLUMN `fork_reason` VARCHAR(64) NULL AFTER `fork_checkpoint_id`,
  ADD KEY `idx_agent_sessions_parent` (`parent_session_id`),
  ADD CONSTRAINT `fk_agent_sessions_parent`
    FOREIGN KEY (`parent_session_id`) REFERENCES `agent_sessions` (`session_id`)
    ON DELETE SET NULL,
  ADD CONSTRAINT `fk_agent_sessions_fork_checkpoint`
    FOREIGN KEY (`fork_checkpoint_id`) REFERENCES `session_checkpoints` (`checkpoint_id`)
    ON DELETE SET NULL;
