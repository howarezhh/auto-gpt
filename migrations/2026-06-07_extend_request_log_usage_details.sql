ALTER TABLE request_logs
    ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS prompt_audio_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS completion_audio_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS accepted_prediction_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS rejected_prediction_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS token_source TEXT,
    ADD COLUMN IF NOT EXISTS upstream_usage_missing BOOLEAN,
    ADD COLUMN IF NOT EXISTS usage_details_json TEXT;

CREATE INDEX IF NOT EXISTS ix_request_logs_token_source_created_at
    ON request_logs (token_source, created_at);
