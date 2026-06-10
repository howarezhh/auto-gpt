ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS content_guard_json_probe_enabled BOOLEAN NOT NULL DEFAULT FALSE;
