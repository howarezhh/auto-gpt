-- 迁移说明：新增提供商内容完整性防护、信任等级、日志留证和 API Key 策略字段。
-- 执行前请按生产规范完成数据库备份。

ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS trust_level TEXT NOT NULL DEFAULT 'standard',
    ADD COLUMN IF NOT EXISTS content_integrity_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS content_integrity_score INTEGER NOT NULL DEFAULT 80,
    ADD COLUMN IF NOT EXISTS content_violation_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_content_violation_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS content_guard_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS low_trust_route_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS buffer_stream_for_guard BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE provider_models
    ADD COLUMN IF NOT EXISTS content_integrity_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS content_probe_last_passed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS content_probe_last_failed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS content_probe_failure_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS content_probe_results_json TEXT;

ALTER TABLE request_logs
    ADD COLUMN IF NOT EXISTS content_guard_result TEXT,
    ADD COLUMN IF NOT EXISTS content_guard_risk_level TEXT,
    ADD COLUMN IF NOT EXISTS content_guard_categories_json TEXT,
    ADD COLUMN IF NOT EXISTS content_guard_reason TEXT,
    ADD COLUMN IF NOT EXISTS content_guard_action TEXT,
    ADD COLUMN IF NOT EXISTS content_guard_excerpt TEXT;

ALTER TABLE app_settings
    ADD COLUMN IF NOT EXISTS content_guard_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS content_guard_block_on_high_risk BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS content_guard_probe_interval_sec INTEGER NOT NULL DEFAULT 3600,
    ADD COLUMN IF NOT EXISTS content_guard_max_scan_bytes INTEGER NOT NULL DEFAULT 16384,
    ADD COLUMN IF NOT EXISTS content_guard_stream_buffer_max_bytes INTEGER NOT NULL DEFAULT 16384,
    ADD COLUMN IF NOT EXISTS content_guard_low_trust_requires_buffer BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS content_guard_rules_json TEXT NOT NULL DEFAULT '';

ALTER TABLE api_client_keys
    ADD COLUMN IF NOT EXISTS trusted_providers_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS allow_low_trust_providers BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS content_guard_required BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE providers
SET trust_level = 'standard'
WHERE trust_level IS NULL OR trust_level NOT IN ('official', 'trusted', 'standard', 'low', 'blocked');

UPDATE providers
SET content_integrity_status = 'unknown'
WHERE content_integrity_status IS NULL OR content_integrity_status NOT IN ('unknown', 'passed', 'degraded', 'blocked');

UPDATE provider_models
SET content_integrity_status = 'unknown'
WHERE content_integrity_status IS NULL OR content_integrity_status NOT IN ('unknown', 'passed', 'degraded', 'blocked');

CREATE INDEX IF NOT EXISTS ix_providers_content_integrity_status ON providers (content_integrity_status);
CREATE INDEX IF NOT EXISTS ix_providers_trust_level ON providers (trust_level);
CREATE INDEX IF NOT EXISTS ix_request_logs_content_guard_result ON request_logs (content_guard_result);
