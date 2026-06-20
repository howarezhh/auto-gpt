ALTER TABLE app_settings
    ADD COLUMN IF NOT EXISTS route_strategy TEXT NOT NULL DEFAULT 'availability_first';

UPDATE app_settings
SET route_strategy = 'availability_first'
WHERE route_strategy IS NULL
   OR route_strategy NOT IN ('availability_first', 'latency_first', 'capacity_avoidance');
