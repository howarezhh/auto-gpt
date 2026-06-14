ALTER TABLE provider_models
    ADD COLUMN IF NOT EXISTS upstream_model_name TEXT;

