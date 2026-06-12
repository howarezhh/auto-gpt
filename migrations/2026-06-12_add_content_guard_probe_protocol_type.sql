ALTER TABLE app_settings
    ADD COLUMN IF NOT EXISTS content_guard_probe_protocol_type TEXT NOT NULL DEFAULT 'chat_completions';
