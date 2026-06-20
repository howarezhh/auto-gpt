-- Add live request status and detailed retry timeline fields for external request observability.

ALTER TABLE request_logs
    ADD COLUMN IF NOT EXISTS request_status TEXT;

UPDATE request_logs
SET request_status = '已完成'
WHERE request_status IS NULL;

ALTER TABLE request_route_decision_events
    ADD COLUMN IF NOT EXISTS hard_filter_final_candidate_count INTEGER,
    ADD COLUMN IF NOT EXISTS candidate_count_after_failed_exclusion INTEGER,
    ADD COLUMN IF NOT EXISTS base_candidate_count INTEGER,
    ADD COLUMN IF NOT EXISTS candidate_expand_count INTEGER,
    ADD COLUMN IF NOT EXISTS candidate_window_count INTEGER,
    ADD COLUMN IF NOT EXISTS selected_reason TEXT,
    ADD COLUMN IF NOT EXISTS failed_candidate_keys_json TEXT,
    ADD COLUMN IF NOT EXISTS failed_candidate_count INTEGER,
    ADD COLUMN IF NOT EXISTS hard_filter_reason_counts_json TEXT,
    ADD COLUMN IF NOT EXISTS top_candidates_json TEXT,
    ADD COLUMN IF NOT EXISTS stage_traces_json TEXT,
    ADD COLUMN IF NOT EXISTS retry_wait_plan_json TEXT,
    ADD COLUMN IF NOT EXISTS selection_guard TEXT;

ALTER TABLE request_provider_attempt_events
    ADD COLUMN IF NOT EXISTS route_round INTEGER,
    ADD COLUMN IF NOT EXISTS retry_index INTEGER,
    ADD COLUMN IF NOT EXISTS error_message TEXT,
    ADD COLUMN IF NOT EXISTS retry_wait_seconds DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS retry_wait_plan_json TEXT,
    ADD COLUMN IF NOT EXISTS detail_json TEXT;

CREATE INDEX IF NOT EXISTS ix_request_logs_request_status_created_at
ON request_logs (request_status, created_at DESC)
WHERE request_status IS NOT NULL;
