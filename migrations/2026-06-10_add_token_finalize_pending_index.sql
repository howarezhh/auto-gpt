CREATE INDEX IF NOT EXISTS ix_request_logs_token_finalize_pending
    ON request_logs (created_at DESC, id DESC)
    WHERE api_client_key_id IS NOT NULL
      AND success = true
      AND log_type IN ('chat', 'responses', 'embeddings')
      AND request_path IS NOT NULL
      AND request_path <> '/v1/models'
      AND request_path NOT LIKE '/v1/models/%'
      AND (billing_finalized_at IS NULL OR billing_status = 'pending_tokens')
      AND (token_finalize_attempt_count IS NULL OR token_finalize_attempt_count < 3);
