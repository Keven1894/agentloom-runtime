-- Fold the lane into session identity, so two lanes can be open at once.
--
-- 016 added the column without touching `open_key`, which still hashes
-- (agent_id, operator_id, workspace_key) and carries the unique index that
-- enforces one open session per identity. This migration widens that key to
-- include the lane. Sessions in different lanes then hash differently and can
-- both be open; two sessions in the *same* lane still collide, which is the
-- protection worth keeping.
--
-- `open_key` is a STORED generated column and the unique index depends on it,
-- so MySQL cannot redefine the expression in place. Index and column are
-- dropped and rebuilt. No data moves: every existing row carries lane
-- 'default', so each currently-open session keeps its slot.
--
-- Ordering requirement: apply this only once every host runs lane-aware code.
-- Older code's open-session lookup does not filter on lane, and its query ends
-- in LIMIT 1 with no ORDER BY, so once a second lane exists it would pick one
-- of them arbitrarily -- including for the implicit open that `checkpoint`
-- performs, which would file that host's checkpoint under another lane.

SET NAMES utf8mb4;

ALTER TABLE `agent_sessions` DROP INDEX `uq_agent_sessions_open`;

ALTER TABLE `agent_sessions` DROP COLUMN `open_key`;

ALTER TABLE `agent_sessions`
  ADD COLUMN `open_key` CHAR(64) GENERATED ALWAYS AS (
    IF(`status` = 'open',
       SHA2(CONCAT(`agent_id`, CHAR(10), `operator_id`, CHAR(10),
                   `workspace_key`, CHAR(10), `lane`), 256),
       NULL)
  ) STORED,
  ADD UNIQUE KEY `uq_agent_sessions_open` (`open_key`);
