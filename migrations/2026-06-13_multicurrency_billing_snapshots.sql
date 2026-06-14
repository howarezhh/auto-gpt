-- Preserve original pricing currency and deterministic exchange snapshots for production billing.

ALTER TABLE user_accounts
    ADD COLUMN IF NOT EXISTS currency_code TEXT NOT NULL DEFAULT 'USD';

ALTER TABLE model_catalogs
    ADD COLUMN IF NOT EXISTS cache_write_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_currency TEXT NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS billing_currency TEXT NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS source_input_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_output_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_cache_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_cache_write_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_to_billing_currency NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_source TEXT,
    ADD COLUMN IF NOT EXISTS exchange_rate_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS exchange_rate_version TEXT,
    ADD COLUMN IF NOT EXISTS rounding_strategy TEXT NOT NULL DEFAULT 'ROUND_HALF_UP';

ALTER TABLE provider_models
    ADD COLUMN IF NOT EXISTS source_currency TEXT NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS billing_currency TEXT NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS source_input_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_output_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_cache_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_cache_write_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_to_billing_currency NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_source TEXT,
    ADD COLUMN IF NOT EXISTS exchange_rate_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS exchange_rate_version TEXT,
    ADD COLUMN IF NOT EXISTS rounding_strategy TEXT NOT NULL DEFAULT 'ROUND_HALF_UP';

ALTER TABLE request_logs
    ADD COLUMN IF NOT EXISTS source_currency TEXT,
    ADD COLUMN IF NOT EXISTS billing_currency TEXT,
    ADD COLUMN IF NOT EXISTS source_price_input_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_price_output_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_price_cache_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_price_cache_write_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS source_prompt_cost NUMERIC(24, 9),
    ADD COLUMN IF NOT EXISTS source_completion_cost NUMERIC(24, 9),
    ADD COLUMN IF NOT EXISTS source_total_cost NUMERIC(24, 9),
    ADD COLUMN IF NOT EXISTS exchange_rate_to_billing_currency NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_source TEXT,
    ADD COLUMN IF NOT EXISTS exchange_rate_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS exchange_rate_version TEXT,
    ADD COLUMN IF NOT EXISTS rounding_strategy TEXT;

ALTER TABLE api_client_billing_records
    ADD COLUMN IF NOT EXISTS source_currency TEXT,
    ADD COLUMN IF NOT EXISTS billing_currency TEXT,
    ADD COLUMN IF NOT EXISTS source_amount NUMERIC(24, 9),
    ADD COLUMN IF NOT EXISTS unit_source_input_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS unit_source_output_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS unit_source_cache_read_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS unit_source_cache_write_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_to_billing_currency NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_source TEXT,
    ADD COLUMN IF NOT EXISTS exchange_rate_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS exchange_rate_version TEXT,
    ADD COLUMN IF NOT EXISTS rounding_strategy TEXT;

ALTER TABLE user_account_billing_records
    ADD COLUMN IF NOT EXISTS source_currency TEXT,
    ADD COLUMN IF NOT EXISTS billing_currency TEXT,
    ADD COLUMN IF NOT EXISTS source_amount NUMERIC(24, 9),
    ADD COLUMN IF NOT EXISTS unit_source_input_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS unit_source_output_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS unit_source_cache_read_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS unit_source_cache_write_price_per_1k NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_to_billing_currency NUMERIC(24, 12),
    ADD COLUMN IF NOT EXISTS exchange_rate_source TEXT,
    ADD COLUMN IF NOT EXISTS exchange_rate_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS exchange_rate_version TEXT,
    ADD COLUMN IF NOT EXISTS rounding_strategy TEXT;

UPDATE model_catalogs
SET source_currency = COALESCE(source_currency, 'USD'),
    billing_currency = COALESCE(billing_currency, 'USD'),
    source_input_price_per_1k = COALESCE(source_input_price_per_1k, input_price_per_1k),
    source_output_price_per_1k = COALESCE(source_output_price_per_1k, output_price_per_1k),
    source_cache_price_per_1k = COALESCE(source_cache_price_per_1k, cache_price_per_1k),
    source_cache_write_price_per_1k = COALESCE(source_cache_write_price_per_1k, cache_write_price_per_1k),
    exchange_rate_to_billing_currency = COALESCE(exchange_rate_to_billing_currency, 1),
    exchange_rate_source = COALESCE(exchange_rate_source, 'legacy_usd_assumption'),
    exchange_rate_version = COALESCE(exchange_rate_version, 'legacy_usd_assumption'),
    rounding_strategy = COALESCE(rounding_strategy, 'ROUND_HALF_UP');

UPDATE provider_models
SET source_currency = COALESCE(source_currency, 'USD'),
    billing_currency = COALESCE(billing_currency, 'USD'),
    source_input_price_per_1k = COALESCE(source_input_price_per_1k, input_price_per_1k),
    source_output_price_per_1k = COALESCE(source_output_price_per_1k, output_price_per_1k),
    source_cache_price_per_1k = COALESCE(source_cache_price_per_1k, cache_price_per_1k),
    source_cache_write_price_per_1k = COALESCE(source_cache_write_price_per_1k, cache_write_price_per_1k),
    exchange_rate_to_billing_currency = COALESCE(exchange_rate_to_billing_currency, 1),
    exchange_rate_source = COALESCE(exchange_rate_source, 'legacy_usd_assumption'),
    exchange_rate_version = COALESCE(exchange_rate_version, 'legacy_usd_assumption'),
    rounding_strategy = COALESCE(rounding_strategy, 'ROUND_HALF_UP');

ALTER TABLE request_auth_events
    ALTER COLUMN remaining_cost_daily TYPE NUMERIC(24, 9) USING remaining_cost_daily::numeric;

ALTER TABLE request_billing_events
    ALTER COLUMN prompt_cost TYPE NUMERIC(24, 9) USING prompt_cost::numeric,
    ALTER COLUMN completion_cost TYPE NUMERIC(24, 9) USING completion_cost::numeric,
    ALTER COLUMN total_cost TYPE NUMERIC(24, 9) USING total_cost::numeric,
    ALTER COLUMN balance_before TYPE NUMERIC(24, 9) USING balance_before::numeric,
    ALTER COLUMN balance_after TYPE NUMERIC(24, 9) USING balance_after::numeric;

ALTER TABLE billing_process_events
    ALTER COLUMN balance_delta TYPE NUMERIC(24, 9) USING balance_delta::numeric,
    ALTER COLUMN balance_after TYPE NUMERIC(24, 9) USING balance_after::numeric;
