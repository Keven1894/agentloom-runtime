-- Let one workspace answer to more than one VCS remote.
--
-- Deriving identity from the VCS remote is what makes a session resumable from
-- any checkout on any machine, and nothing here weakens that. But it quietly
-- assumes the remote is immortal, and remotes move: this deployment migrated
-- from Bitbucket to a self-hosted Gitea, and a machine whose checkout still
-- pointed at the old remote derived a different key and opened its own session
-- instead of resuming the shared one. The provenance was never wrong; the two
-- halves of one work stream simply could not see each other.
--
-- An alias resolves an old key to the current one at lookup time, so the
-- derivation stays pure and the remap is data rather than a special case in
-- the identity code.
--
-- Resolution is deliberately ONE HOP. `canonical_key` may not itself be an
-- alias, which is enforced by the writer rather than by the schema. That makes
-- a cycle impossible to create instead of something the reader has to detect,
-- and keeps resolution a single indexed lookup on the hot path where every
-- command resolves its identity.

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `workspace_aliases` (
  -- The key as derived from some checkout's remote. Primary key: one alias
  -- cannot point at two canonical workspaces.
  `alias_key`     VARCHAR(512) NOT NULL,
  `canonical_key` VARCHAR(512) NOT NULL,
  -- Why this alias exists. The remote that moved, the date, whoever decided.
  `note`          VARCHAR(512) NULL,
  `created_at`    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`alias_key`),
  KEY `idx_workspace_aliases_canonical` (`canonical_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
