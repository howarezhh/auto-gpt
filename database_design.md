# 数据库设计

项目默认使用 SQLite，数据库文件路径为 `data/app.db`。

## `providers`

- `id` INTEGER PRIMARY KEY
- `name` TEXT UNIQUE NOT NULL
- `base_url` TEXT NOT NULL
- `api_key` TEXT NOT NULL
- `provider_type` TEXT NOT NULL DEFAULT `openai_compatible`
- `enabled` BOOLEAN NOT NULL DEFAULT `1`
- `priority` INTEGER NOT NULL DEFAULT `100`
- `weight` INTEGER NOT NULL DEFAULT `100`
- `timeout_ms` INTEGER NOT NULL DEFAULT `30000`
- `max_retries` INTEGER NOT NULL DEFAULT `1`
- `models_json` TEXT NOT NULL DEFAULT `[]`
- `health_status` TEXT NOT NULL DEFAULT `unknown`
- `last_check_at` DATETIME NULL
- `last_latency_ms` INTEGER NULL
- `failure_count` INTEGER NOT NULL DEFAULT `0`
- `success_count` INTEGER NOT NULL DEFAULT `0`
- `circuit_state` TEXT NOT NULL DEFAULT `closed`
- `remark` TEXT NULL
- `created_at` DATETIME NOT NULL
- `updated_at` DATETIME NOT NULL

## `app_settings`

- `id` INTEGER PRIMARY KEY，固定为 `1`
- `route_mode` TEXT NOT NULL DEFAULT `failover`
- `default_provider_id` INTEGER NULL，关联 `providers.id`
- `manual_allow_fallback` BOOLEAN NOT NULL DEFAULT `1`
- `global_timeout_ms` INTEGER NOT NULL DEFAULT `30000`
- `global_max_retries` INTEGER NOT NULL DEFAULT `2`
- `circuit_breaker_threshold` INTEGER NOT NULL DEFAULT `3`
- `auto_health_check` BOOLEAN NOT NULL DEFAULT `1`
- `health_check_interval_sec` INTEGER NOT NULL DEFAULT `60`
- `recovery_probe_interval_sec` INTEGER NOT NULL DEFAULT `30`
- `created_at` DATETIME NOT NULL
- `updated_at` DATETIME NOT NULL

## `request_logs`

- `id` INTEGER PRIMARY KEY
- `log_type` TEXT NOT NULL
- `provider_id` INTEGER NULL，关联 `providers.id`
- `provider_name` TEXT NULL
- `model_name` TEXT NULL
- `request_path` TEXT NULL
- `success` BOOLEAN NOT NULL
- `status_code` INTEGER NULL
- `latency_ms` INTEGER NULL
- `message` TEXT NULL
- `trace_json` TEXT NULL
- `created_at` DATETIME NOT NULL
