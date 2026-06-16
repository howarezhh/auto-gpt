ALTER TABLE app_settings
    ADD COLUMN IF NOT EXISTS route_health_gate_mode TEXT NOT NULL DEFAULT 'permissive';

UPDATE app_settings
SET route_health_gate_mode = 'permissive'
WHERE route_health_gate_mode IS NULL
   OR route_health_gate_mode NOT IN ('permissive', 'healthy_only', 'trusted_healthy');
