-- Runtime KG graph store: node attributes + edges (authoring remains JSON files).

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS kg_nodes (
  row_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  node_id       VARCHAR(191) NOT NULL,
  node_type     VARCHAR(64),
  graph         VARCHAR(64),
  title         LONGTEXT,
  attributes    JSON,
  source_file   VARCHAR(191),
  source_path   VARCHAR(512),
  content_hash  CHAR(64),
  valid_from    DATETIME,
  superseded_at DATETIME NULL,
  current_key   VARCHAR(191) GENERATED ALWAYS AS (
    IF(superseded_at IS NULL, node_id, NULL)
  ) STORED,
  updated_at    DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE UNIQUE INDEX uq_kg_nodes_current ON kg_nodes (current_key);
CREATE INDEX idx_kg_nodes_node_id ON kg_nodes (node_id);
CREATE INDEX idx_kg_nodes_type ON kg_nodes (node_type);
CREATE INDEX idx_kg_nodes_source_path ON kg_nodes (source_path(191));

CREATE TABLE IF NOT EXISTS kg_edges (
  src_id    VARCHAR(191) NOT NULL,
  dst_id    VARCHAR(191) NOT NULL,
  edge_type VARCHAR(64) NOT NULL,
  graph     VARCHAR(64),
  PRIMARY KEY (src_id, dst_id, edge_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE INDEX idx_kg_edges_dst ON kg_edges (dst_id);
CREATE INDEX idx_kg_edges_type ON kg_edges (edge_type);
