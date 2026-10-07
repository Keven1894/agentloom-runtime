-- Exact cache for query embeddings.
--
-- A warm embedding call is about 370 ms and a cold one about 4.5 s. Once
-- search itself is under 200 ms, a repeated question spends most of its time
-- re-embedding the same string. Key: sha256 of model, dimensions, and the
-- whitespace-collapsed query. Old code never reads this table.

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `query_embedding_cache` (
  `cache_key` CHAR(64) NOT NULL,
  `embedding_model` VARCHAR(191) NOT NULL,
  `dimensions` INT NOT NULL,
  `query_norm` VARCHAR(512) NOT NULL,
  `embedding` JSON NOT NULL,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`cache_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
