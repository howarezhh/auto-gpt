ALTER TABLE api_client_keys DROP COLUMN IF EXISTS token_limit_total;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS request_limit_daily;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS token_limit_daily;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS cost_limit_daily;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS tpm_limit;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS cost_limit_total;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS balance_amount;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS total_recharge_amount;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS route_mode;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS default_provider_id;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS manual_allow_fallback;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS route_exhausted_retry_infinite_enabled;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS max_candidate_count;
ALTER TABLE api_client_keys DROP COLUMN IF EXISTS cost_bias;

ALTER TABLE api_key_policy_templates DROP COLUMN IF EXISTS route_mode;
ALTER TABLE api_key_policy_templates DROP COLUMN IF EXISTS default_provider_id;
ALTER TABLE api_key_policy_templates DROP COLUMN IF EXISTS manual_allow_fallback;
ALTER TABLE api_key_policy_templates DROP COLUMN IF EXISTS token_limit_total;
ALTER TABLE api_key_policy_templates DROP COLUMN IF EXISTS cost_limit_total;

ALTER TABLE request_logs DROP COLUMN IF EXISTS api_client_remaining_tokens;
ALTER TABLE request_logs DROP COLUMN IF EXISTS api_client_remaining_requests_daily;
ALTER TABLE request_logs DROP COLUMN IF EXISTS api_client_remaining_cost_daily;
