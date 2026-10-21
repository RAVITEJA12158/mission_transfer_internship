
CREATE TABLE IF NOT EXISTS aircraft (
    id BIGSERIAL PRIMARY KEY,
    aircraft_name VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO aircraft (aircraft_name)
SELECT 'UAV-1' WHERE NOT EXISTS (SELECT 1 FROM aircraft);

CREATE TABLE IF NOT EXISTS missions (
    id BIGSERIAL PRIMARY KEY,
    aircraft_id BIGINT NOT NULL REFERENCES aircraft(id),
    mission_name VARCHAR(150),
    status VARCHAR(30) NOT NULL DEFAULT 'received'
        CHECK (status IN ('received','in_progress','completed','ended_incomplete')),
    mission_hash VARCHAR(64) NOT NULL,
    item_count INTEGER,
    status_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS waypoints (
    id BIGSERIAL PRIMARY KEY,
    mission_id BIGINT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL,
    command INTEGER NOT NULL,
    frame INTEGER,
    param1 DOUBLE PRECISION,
    param2 DOUBLE PRECISION,
    param3 DOUBLE PRECISION,
    param4 DOUBLE PRECISION,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude DOUBLE PRECISION,
    autocontinue BOOLEAN,
    raw_item JSONB,
    UNIQUE (mission_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS telemetry_batches (
    batch_id UUID PRIMARY KEY,
    aircraft_id BIGINT NOT NULL REFERENCES aircraft(id),
    mission_id BIGINT REFERENCES missions(id),
    flight_id UUID,
    record_count INTEGER NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS telemetry (
    id BIGSERIAL PRIMARY KEY,
    aircraft_id BIGINT NOT NULL REFERENCES aircraft(id),
    mission_id BIGINT REFERENCES missions(id),
    flight_id UUID,
    batch_id UUID REFERENCES telemetry_batches(batch_id) ON DELETE CASCADE,
    "timestamp" TIMESTAMPTZ NOT NULL,
    message_type VARCHAR(60) NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude_m DOUBLE PRECISION,
    relative_altitude_m DOUBLE PRECISION,
    speed_mps DOUBLE PRECISION,
    heading_deg DOUBLE PRECISION,
    battery_voltage DOUBLE PRECISION,
    raw_data JSONB
);

CREATE INDEX IF NOT EXISTS idx_telemetry_mission_time ON telemetry (mission_id, "timestamp");
CREATE INDEX IF NOT EXISTS idx_telemetry_aircraft_time ON telemetry (aircraft_id, "timestamp");

CREATE TABLE IF NOT EXISTS flight_logs (
    id BIGSERIAL PRIMARY KEY,
    aircraft_id BIGINT NOT NULL REFERENCES aircraft(id),
    mission_id BIGINT REFERENCES missions(id),
    flight_id UUID,
    log_number INTEGER,
    filename VARCHAR(255) NOT NULL,
    storage_path TEXT NOT NULL,
    file_size_bytes BIGINT,
    checksum_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','transferring','verified','failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_flight_logs_mission ON flight_logs (mission_id);

ALTER TABLE missions    ADD COLUMN IF NOT EXISTS item_count INTEGER;
ALTER TABLE missions    ADD COLUMN IF NOT EXISTS status_at TIMESTAMPTZ;
ALTER TABLE waypoints   ADD COLUMN IF NOT EXISTS raw_item JSONB;
ALTER TABLE telemetry   ADD COLUMN IF NOT EXISTS flight_id UUID;
ALTER TABLE telemetry   ADD COLUMN IF NOT EXISTS batch_id UUID;
ALTER TABLE flight_logs ADD COLUMN IF NOT EXISTS flight_id UUID;
ALTER TABLE flight_logs ADD COLUMN IF NOT EXISTS log_number INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS uq_missions_aircraft_hash ON missions (aircraft_id, mission_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_flight_logs_aircraft_checksum ON flight_logs (aircraft_id, checksum_sha256);
CREATE INDEX IF NOT EXISTS idx_telemetry_flight ON telemetry (flight_id);
