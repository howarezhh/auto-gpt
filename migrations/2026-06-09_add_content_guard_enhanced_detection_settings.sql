ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_detection_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_illegal_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_ad_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_custom_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_obfuscation_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_threshold INTEGER NOT NULL DEFAULT 70;
ALTER TABLE app_settings ADD COLUMN content_guard_enhanced_context_window_chars INTEGER NOT NULL DEFAULT 96;
