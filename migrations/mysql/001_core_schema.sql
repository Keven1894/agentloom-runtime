-- AgentLoom Runtime CORE schema (MySQL)
-- Operational tables for projects, tasks, messaging, and KG vector index.
-- Instance-specific extensions (datasets, object storage tokens, publications, etc.) are excluded.

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS=0;

CREATE TABLE IF NOT EXISTS `schema_version` (
  `version` BIGINT AUTO_INCREMENT NOT NULL,
  `description` LONGTEXT NOT NULL,
  `applied_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `users` (
  `id` VARCHAR(191),
  `username` VARCHAR(191) NOT NULL,
  `email` VARCHAR(512) NOT NULL,
  `password_hash` LONGTEXT NOT NULL,
  `full_name` VARCHAR(512) NOT NULL,
  `status` VARCHAR(191) NOT NULL DEFAULT 'active',
  `last_login` LONGTEXT,
  `created_at` DATETIME NULL,
  `updated_at` DATETIME NULL,
  `department` LONGTEXT,
  `title` VARCHAR(512),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX `idx_users_username` ON `users` (`username`);
CREATE INDEX `idx_users_email` ON `users` (`email`);
CREATE INDEX `idx_users_status` ON `users` (`status`);

CREATE TABLE IF NOT EXISTS `roles` (
  `id` VARCHAR(191),
  `name` VARCHAR(191) NOT NULL,
  `description` LONGTEXT,
  `level` BIGINT NOT NULL DEFAULT 0,
  `permissions` LONGTEXT NOT NULL,
  `created_at` DATETIME NULL,
  `updated_at` DATETIME NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `user_roles` (
  `id` BIGINT AUTO_INCREMENT NOT NULL,
  `user_id` VARCHAR(191) NOT NULL,
  `role_id` VARCHAR(191) NOT NULL,
  `scope` LONGTEXT NOT NULL,
  `granted_by` LONGTEXT,
  `granted_at` DATETIME NULL,
  `expires_at` DATETIME NULL,
  `metadata` JSON,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX `idx_user_roles_user` ON `user_roles` (`user_id`);
CREATE INDEX `idx_user_roles_role` ON `user_roles` (`role_id`);

CREATE TABLE IF NOT EXISTS `teams` (
  `id` VARCHAR(191),
  `name` VARCHAR(191) NOT NULL,
  `description` LONGTEXT,
  `team_lead_id` VARCHAR(191),
  `parent_team_id` VARCHAR(191),
  `status` VARCHAR(191) NOT NULL DEFAULT 'active',
  `created_at` DATETIME NULL,
  `updated_at` DATETIME NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `team_members` (
  `id` BIGINT AUTO_INCREMENT NOT NULL,
  `team_id` VARCHAR(191) NOT NULL,
  `user_id` VARCHAR(191) NOT NULL,
  `role` VARCHAR(191) NOT NULL DEFAULT 'member',
  `joined_at` DATETIME NULL,
  `left_at` DATETIME NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX `idx_team_members_team` ON `team_members` (`team_id`);
CREATE INDEX `idx_team_members_user` ON `team_members` (`user_id`);

CREATE TABLE IF NOT EXISTS `projects` (
  `id` VARCHAR(191),
  `name` VARCHAR(191) NOT NULL,
  `research_group` LONGTEXT,
  `project_lead` LONGTEXT,
  `status` VARCHAR(191) NOT NULL DEFAULT 'planned',
  `priority` VARCHAR(191) DEFAULT 'medium',
  `start_date` DATETIME NULL,
  `due_date` DATETIME NULL,
  `completed_date` DATETIME NULL,
  `description` LONGTEXT,
  `objectives` LONGTEXT,
  `deliverables` LONGTEXT,
  `technology_stack` LONGTEXT,
  `collaborators` LONGTEXT,
  `file_path` VARCHAR(512),
  `metadata` JSON,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `team_id` VARCHAR(191),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX `idx_projects_status` ON `projects` (`status`);
CREATE INDEX `idx_projects_team` ON `projects` (`team_id`);

CREATE TABLE IF NOT EXISTS `tasks` (
  `id` VARCHAR(191),
  `name` VARCHAR(191) NOT NULL,
  `data_source_type` VARCHAR(191) NOT NULL,
  `description` LONGTEXT,
  `status` VARCHAR(191) NOT NULL DEFAULT 'pending',
  `assigned_to_user_id` VARCHAR(191),
  `priority` VARCHAR(191) DEFAULT 'medium',
  `duration_hours` DOUBLE,
  `datasets_processed` BIGINT,
  `success_rate` DOUBLE,
  `generated_at` DATETIME NULL,
  `metadata` JSON,
  `start_date` DATETIME NULL,
  `due_date` DATETIME NULL,
  `completed_date` DATETIME NULL,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `project_id` VARCHAR(191),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `agents` (
  `id` VARCHAR(191),
  `name` VARCHAR(191) NOT NULL,
  `type` VARCHAR(191) NOT NULL,
  `current_manager` LONGTEXT,
  `status` VARCHAR(191) NOT NULL DEFAULT 'active',
  `descriptions` LONGTEXT,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `git_url` LONGTEXT,
  `current_host_ip` VARCHAR(512),
  `mcp_port` BIGINT,
  `mcp_entry_point` VARCHAR(512),
  `os_type` LONGTEXT,
  `api_token` LONGTEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `message_categories` (
  `id` VARCHAR(191),
  `label` LONGTEXT NOT NULL,
  `handling` LONGTEXT NOT NULL,
  `auto_reply` BIGINT NOT NULL DEFAULT 1,
  `resolve_after_reply` BIGINT NOT NULL DEFAULT 0,
  `template_key` LONGTEXT,
  `description` LONGTEXT,
  `deprecated_aliases` JSON,
  `created_at` DATETIME NULL,
  `updated_at` DATETIME NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `messages` (
  `id` VARCHAR(191),
  `reporter` VARCHAR(191) NOT NULL,
  `title` VARCHAR(512) NOT NULL,
  `description` LONGTEXT NOT NULL,
  `category` VARCHAR(191) NOT NULL,
  `severity` VARCHAR(191) NOT NULL DEFAULT 'medium',
  `status` VARCHAR(191) NOT NULL DEFAULT 'open',
  `context` JSON,
  `resolution` LONGTEXT,
  `resolved_by` LONGTEXT,
  `resolved_at` DATETIME NULL,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `task_id` VARCHAR(191),
  `reporter_host` LONGTEXT,
  `reporter_port` BIGINT,
  `reporter_endpoint` LONGTEXT,
  `recipient` VARCHAR(191),
  `recipient_host` LONGTEXT,
  `recipient_port` BIGINT,
  `project_id` VARCHAR(191),
  `package_id` VARCHAR(191),
  `parent_message_id` VARCHAR(191),
  `correlation_id` VARCHAR(191),
  `dispatched` TINYINT(1) NOT NULL DEFAULT 0,
  `dispatched_at` DATETIME NULL,
  `dispatch_endpoint` LONGTEXT,
  `escalated` TINYINT(1) DEFAULT 0,
  `escalation_reason` LONGTEXT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `message_replies` (
  `id` BIGINT AUTO_INCREMENT NOT NULL,
  `message_id` VARCHAR(191) NOT NULL,
  `author` VARCHAR(191) NOT NULL,
  `note` LONGTEXT NOT NULL,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `auto_generated` TINYINT(1) DEFAULT 0,
  `dispatched` TINYINT(1) NOT NULL DEFAULT 0,
  `dispatched_at` DATETIME NULL,
  `dispatch_endpoint` LONGTEXT,
  `draft` TINYINT(1) DEFAULT 0,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX `idx_message_replies_message` ON `message_replies` (`message_id`);

CREATE TABLE IF NOT EXISTS `knowledge_embeddings` (
  `id` VARCHAR(191),
  `source_file` VARCHAR(191) NOT NULL,
  `node_id` VARCHAR(191),
  `node_type` VARCHAR(191),
  `topic` LONGTEXT,
  `content` LONGTEXT NOT NULL,
  `embedding` JSON,
  `created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
CREATE INDEX `idx_ke_source` ON `knowledge_embeddings` (`source_file`);
CREATE INDEX `idx_ke_topic` ON `knowledge_embeddings` (`topic`(191));
CREATE INDEX `idx_knowledge_embeddings_node_type` ON `knowledge_embeddings` (`node_type`);

SET FOREIGN_KEY_CHECKS=1;
