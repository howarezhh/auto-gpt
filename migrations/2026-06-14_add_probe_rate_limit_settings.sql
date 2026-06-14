ALTER TABLE app_settings
    ADD COLUMN IF NOT EXISTS probe_rate_limit_per_minute INTEGER NOT NULL DEFAULT 4,
    ADD COLUMN IF NOT EXISTS probe_type_rate_limit_per_minute INTEGER NOT NULL DEFAULT 4;
