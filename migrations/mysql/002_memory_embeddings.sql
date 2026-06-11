-- Layered memory embedding tables for runtime retrieval.

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `message_embeddings` (
  `id` VARCHAR(191) PRIMARY KEY,
  `message_id` VARCHAR(191) NOT NULL,
  `reply_id` VARCHAR(191),
  `chunk_type` VARCHAR(64) NOT NULL,
  `content` LONGTEXT NOT NULL,
  `embedding` JSON,
  `embedding_model` VARCHAR(191) NOT NULL,
  `content_hash` CHAR(64) NOT NULL,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE INDEX IF NOT EXISTS `idx_msg_emb_message` ON `message_embeddings` (`message_id`);
CREATE INDEX IF NOT EXISTS `idx_msg_emb_reply` ON `message_embeddings` (`reply_id`);
CREATE INDEX IF NOT EXISTS `idx_msg_emb_model` ON `message_embeddings` (`embedding_model`);

CREATE TABLE IF NOT EXISTS `docshare_embeddings` (
  `id` VARCHAR(191) NOT NULL,
  `doc_id` VARCHAR(191) NOT NULL,
  `source_path` VARCHAR(1024) NOT NULL,
  `title` VARCHAR(512),
  `doc_type` VARCHAR(64),
  `lifecycle` VARCHAR(64),
  `content` LONGTEXT NOT NULL,
  `embedding` JSON,
  `embedding_model` VARCHAR(191) NOT NULL,
  `content_hash` CHAR(64) NOT NULL,
  `version_hash` VARCHAR(128) NOT NULL,
  `metadata_json` JSON,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE INDEX IF NOT EXISTS `idx_docshare_emb_doc` ON `docshare_embeddings` (`doc_id`);
CREATE INDEX IF NOT EXISTS `idx_docshare_emb_source` ON `docshare_embeddings` (`source_path`(191));
CREATE INDEX IF NOT EXISTS `idx_docshare_emb_model` ON `docshare_embeddings` (`embedding_model`);

CREATE TABLE IF NOT EXISTS `plan_embeddings` (
  `id` VARCHAR(191) PRIMARY KEY,
  `path` VARCHAR(512) NOT NULL,
  `title` VARCHAR(512),
  `lifecycle` VARCHAR(64),
  `status` VARCHAR(128),
  `owner` VARCHAR(191),
  `project_id` VARCHAR(191),
  `task_id` VARCHAR(191),
  `message_id` VARCHAR(191),
  `tags` JSON,
  `content` LONGTEXT NOT NULL,
  `embedding` JSON,
  `embedding_model` VARCHAR(191) NOT NULL,
  `content_hash` CHAR(64) NOT NULL,
  `metadata_json` JSON,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE INDEX IF NOT EXISTS `idx_plan_emb_path` ON `plan_embeddings` (`path`);
CREATE INDEX IF NOT EXISTS `idx_plan_emb_lifecycle` ON `plan_embeddings` (`lifecycle`);
CREATE INDEX IF NOT EXISTS `idx_plan_emb_project` ON `plan_embeddings` (`project_id`);
CREATE INDEX IF NOT EXISTS `idx_plan_emb_model` ON `plan_embeddings` (`embedding_model`);
