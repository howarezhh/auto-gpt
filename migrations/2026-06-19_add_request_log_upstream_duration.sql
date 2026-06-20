ALTER TABLE request_logs
    ADD COLUMN IF NOT EXISTS upstream_duration_ms INTEGER;

CREATE INDEX IF NOT EXISTS ix_request_logs_upstream_duration_ms
    ON request_logs (created_at, upstream_duration_ms)
    WHERE upstream_duration_ms IS NOT NULL;
