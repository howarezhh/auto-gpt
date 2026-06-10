ALTER TABLE app_settings DROP COLUMN IF EXISTS route_mode;
ALTER TABLE app_settings DROP COLUMN IF EXISTS default_provider_id;
ALTER TABLE app_settings DROP COLUMN IF EXISTS manual_allow_fallback;

ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS route_candidate_expand_count INTEGER NOT NULL DEFAULT 5;

ALTER TABLE providers DROP COLUMN IF EXISTS max_error_rate;

DROP INDEX IF EXISTS ix_request_logs_route_metrics;
CREATE INDEX IF NOT EXISTS ix_request_logs_route_metrics
    ON request_logs (log_type, created_at, provider_id, model_name, success);
