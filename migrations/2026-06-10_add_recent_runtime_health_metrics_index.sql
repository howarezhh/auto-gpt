CREATE INDEX IF NOT EXISTS ix_request_logs_recent_provider_model_health
    ON request_logs (created_at, provider_id, resolved_provider_model_id, log_type, success);
