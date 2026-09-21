-- DealerSight schema (Phase 1). All timestamps are UTC (TIMESTAMPTZ).
-- Applied by `python -m backend.db init`; safe to re-run.

-- Three dealership zones (project rules §4).
CREATE TABLE IF NOT EXISTS zone (
    id   SERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL
);

-- Four camera positions: Zone 3 has a departure lane and a return lane.
CREATE TABLE IF NOT EXISTS camera_position (
    id      SERIAL PRIMARY KEY,
    code    TEXT UNIQUE NOT NULL,
    name    TEXT NOT NULL,
    zone_id INT NOT NULL REFERENCES zone(id)
);

-- Ring devices discovered through GET /v1/devices.
CREATE TABLE IF NOT EXISTS device (
    id             SERIAL PRIMARY KEY,
    ring_device_id TEXT UNIQUE NOT NULL,
    display_name   TEXT NOT NULL,
    mode           TEXT NOT NULL CHECK (mode IN ('demo', 'production')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Where a device is placed over time. In demo mode each row is an "arm" with an expiry.
CREATE TABLE IF NOT EXISTS device_assignment (
    id                 SERIAL PRIMARY KEY,
    device_id          INT NOT NULL REFERENCES device(id),
    camera_position_id INT NOT NULL REFERENCES camera_position(id),
    valid_from         TIMESTAMPTZ NOT NULL,
    valid_to           TIMESTAMPTZ,
    zone_source        TEXT NOT NULL CHECK (zone_source IN ('demo_assignment', 'device_configuration'))
);
CREATE INDEX IF NOT EXISTS device_assignment_lookup ON device_assignment (device_id, valid_from);

-- A demo session sets the watermark: events that started earlier are never counted.
CREATE TABLE IF NOT EXISTS demo_session (
    id             SERIAL PRIMARY KEY,
    watermark      TIMESTAMPTZ NOT NULL,
    timing_profile TEXT NOT NULL DEFAULT 'demo',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every Ring event we see is stored once. Rejected events are kept as evidence but never counted.
CREATE TABLE IF NOT EXISTS raw_event (
    id                 BIGSERIAL PRIMARY KEY,
    ring_event_id      TEXT UNIQUE NOT NULL,
    device_id          INT NOT NULL REFERENCES device(id),
    ring_event_type    TEXT NOT NULL,
    started_at         TIMESTAMPTZ NOT NULL,
    ended_at           TIMESTAMPTZ,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    camera_position_id INT REFERENCES camera_position(id),
    zone_source        TEXT,
    accepted           BOOLEAN NOT NULL,
    reject_reason      TEXT,
    source             TEXT NOT NULL CHECK (source IN ('ring_live', 'ring_replay', 'simulated_baseline')),
    payload            JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_event_started ON raw_event (started_at DESC);

INSERT INTO zone (code, name) VALUES
    ('entrance', 'Dealership Entrance'),
    ('display_area', 'Vehicle Display Area'),
    ('lot', 'Lot Entrance & Exit')
ON CONFLICT (code) DO NOTHING;

INSERT INTO camera_position (code, name, zone_id) VALUES
    ('entrance', 'Entrance', (SELECT id FROM zone WHERE code = 'entrance')),
    ('display_area', 'Display Area', (SELECT id FROM zone WHERE code = 'display_area')),
    ('lot_departure', 'Lot: Departure lane', (SELECT id FROM zone WHERE code = 'lot')),
    ('lot_return', 'Lot: Return lane', (SELECT id FROM zone WHERE code = 'lot'))
ON CONFLICT (code) DO NOTHING;
