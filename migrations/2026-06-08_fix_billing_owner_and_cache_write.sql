-- Fix billing ownership, cache-write pricing, and billing record audit fields.

ALTER TABLE provider_models
    ADD COLUMN IF NOT EXISTS cache_write_price_per_1k NUMERIC(18, 8);

ALTER TABLE api_client_billing_records
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS unit_cache_read_price_per_1k NUMERIC(18, 8),
    ADD COLUMN IF NOT EXISTS unit_cache_write_price_per_1k NUMERIC(18, 8);

ALTER TABLE user_account_billing_records
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS unit_cache_read_price_per_1k NUMERIC(18, 8),
    ADD COLUMN IF NOT EXISTS unit_cache_write_price_per_1k NUMERIC(18, 8);

UPDATE api_client_billing_records record
SET
    cache_read_tokens = log.cache_read_tokens,
    cache_write_tokens = log.cache_write_tokens,
    unit_cache_read_price_per_1k = log.channel_price_cache_per_1k,
    unit_cache_write_price_per_1k = log.channel_price_cache_write_per_1k
FROM request_logs log
WHERE record.request_log_id = log.id
  AND (
      record.cache_read_tokens IS NULL
      OR record.cache_write_tokens IS NULL
      OR record.unit_cache_read_price_per_1k IS NULL
      OR record.unit_cache_write_price_per_1k IS NULL
  );

UPDATE user_account_billing_records record
SET
    cache_read_tokens = log.cache_read_tokens,
    cache_write_tokens = log.cache_write_tokens,
    unit_cache_read_price_per_1k = log.channel_price_cache_per_1k,
    unit_cache_write_price_per_1k = log.channel_price_cache_write_per_1k
FROM request_logs log
WHERE record.request_log_id = log.id
  AND (
      record.cache_read_tokens IS NULL
      OR record.cache_write_tokens IS NULL
      OR record.unit_cache_read_price_per_1k IS NULL
      OR record.unit_cache_write_price_per_1k IS NULL
  );

WITH default_owner AS (
    SELECT id
    FROM user_accounts
    WHERE enabled = TRUE
    ORDER BY id ASC
    LIMIT 1
)
UPDATE api_client_keys
SET owner_user_id = (SELECT id FROM default_owner)
WHERE owner_user_id IS NULL
  AND EXISTS (SELECT 1 FROM default_owner);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM api_client_keys WHERE owner_user_id IS NULL) THEN
        RAISE EXCEPTION 'api_client_keys.owner_user_id still contains NULL; create an enabled user and rerun backfill before enforcing owner requirement';
    END IF;
END $$;

INSERT INTO user_account_billing_records (
    user_account_id,
    api_client_key_id,
    request_log_id,
    record_type,
    amount,
    balance_after,
    provider_id,
    provider_name,
    model_name,
    prompt_tokens,
    completion_tokens,
    total_tokens,
    cache_read_tokens,
    cache_write_tokens,
    unit_input_price_per_1k,
    unit_output_price_per_1k,
    unit_cache_read_price_per_1k,
    unit_cache_write_price_per_1k,
    remark,
    created_at
)
SELECT
    api_key.owner_user_id,
    record.api_client_key_id,
    record.request_log_id,
    record.record_type,
    record.amount,
    record.balance_after,
    record.provider_id,
    record.provider_name,
    record.model_name,
    record.prompt_tokens,
    record.completion_tokens,
    record.total_tokens,
    record.cache_read_tokens,
    record.cache_write_tokens,
    record.unit_input_price_per_1k,
    record.unit_output_price_per_1k,
    record.unit_cache_read_price_per_1k,
    record.unit_cache_write_price_per_1k,
    record.remark,
    record.created_at
FROM api_client_billing_records record
JOIN api_client_keys api_key ON api_key.id = record.api_client_key_id
WHERE record.record_type = 'request_charge'
  AND record.request_log_id IS NOT NULL
  AND api_key.owner_user_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM user_account_billing_records existing
      WHERE existing.request_log_id = record.request_log_id
  );

ALTER TABLE api_client_keys
    ALTER COLUMN owner_user_id SET NOT NULL;
