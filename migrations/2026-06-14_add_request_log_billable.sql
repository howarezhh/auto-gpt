ALTER TABLE request_logs
    ADD COLUMN IF NOT EXISTS billable BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS billable_reason TEXT;

UPDATE request_logs
SET
    billable = TRUE,
    billable_reason = COALESCE(billable_reason, 'completed')
WHERE
    api_client_key_id IS NOT NULL
    AND success = TRUE
    AND log_type IN ('chat', 'responses', 'embeddings')
    AND request_path IS NOT NULL
    AND request_path <> '/v1/models'
    AND request_path NOT LIKE '/v1/models/%';

UPDATE request_logs
SET
    billable = TRUE,
    billable_reason = COALESCE(billable_reason, 'blocked_after_output')
WHERE
    api_client_key_id IS NOT NULL
    AND success = FALSE
    AND error_code = 'content_integrity_violation'
    AND log_type IN ('chat', 'responses', 'embeddings')
    AND request_path IS NOT NULL
    AND request_path <> '/v1/models'
    AND request_path NOT LIKE '/v1/models/%'
    AND (
        COALESCE(total_tokens, 0) > 0
        OR COALESCE(prompt_tokens, 0) > 0
        OR COALESCE(completion_tokens, 0) > 0
        OR NULLIF(response_text, '') IS NOT NULL
        OR NULLIF(response_body_json, '') IS NOT NULL
    );

DROP INDEX IF EXISTS ix_request_logs_token_finalize_pending;

CREATE INDEX IF NOT EXISTS ix_request_logs_token_finalize_pending
ON request_logs (created_at DESC, id DESC)
WHERE
    api_client_key_id IS NOT NULL
    AND billable = TRUE
    AND log_type IN ('chat','responses','embeddings')
    AND request_path IS NOT NULL
    AND request_path <> '/v1/models'
    AND request_path NOT LIKE '/v1/models/%'
    AND (billing_finalized_at IS NULL OR billing_status = 'pending_tokens')
    AND (token_finalize_attempt_count IS NULL OR token_finalize_attempt_count < 3);
