ALTER TABLE app_settings
    ADD COLUMN IF NOT EXISTS content_guard_high_risk_strategy TEXT NOT NULL DEFAULT 'switch_provider',
    ADD COLUMN IF NOT EXISTS content_guard_max_detection_delay_ms INTEGER NOT NULL DEFAULT 300,
    ADD COLUMN IF NOT EXISTS content_guard_stream_mode TEXT NOT NULL DEFAULT 'buffer_300ms',
    ADD COLUMN IF NOT EXISTS content_guard_url_check_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS content_guard_url_allowlist_json TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS content_guard_async_review_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS content_guard_high_risk_confidence_threshold INTEGER NOT NULL DEFAULT 85;

ALTER TABLE request_logs
    ADD COLUMN IF NOT EXISTS content_guard_latency_ms INTEGER,
    ADD COLUMN IF NOT EXISTS content_guard_buffer_wait_ms INTEGER,
    ADD COLUMN IF NOT EXISTS content_guard_retry_provider_count INTEGER,
    ADD COLUMN IF NOT EXISTS content_guard_final_strategy TEXT;

CREATE INDEX IF NOT EXISTS ix_request_logs_content_guard_latency
    ON request_logs (created_at, content_guard_latency_ms)
    WHERE content_guard_latency_ms IS NOT NULL;
