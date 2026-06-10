ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS global_qps_limit INTEGER DEFAULT 20;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS global_rpm_limit INTEGER DEFAULT 20;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS account_qps_limit INTEGER DEFAULT 20;
ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS account_rpm_limit INTEGER DEFAULT 20;

UPDATE app_settings
SET
    global_qps_limit = COALESCE(global_qps_limit, 20),
    global_rpm_limit = COALESCE(global_rpm_limit, 20),
    account_qps_limit = COALESCE(account_qps_limit, 20),
    account_rpm_limit = COALESCE(account_rpm_limit, 20);
