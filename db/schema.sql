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

-- Phase 2: events inferred by DealerSight's rules. Rebuilt from raw_event on every correlation run.
CREATE TABLE IF NOT EXISTS derived_event (
    id               BIGSERIAL PRIMARY KEY,
    type             TEXT NOT NULL CHECK (type IN ('visit', 'engagement', 'probable_test_drive')),
    zone_id          INT NOT NULL REFERENCES zone(id),
    started_at       TIMESTAMPTZ NOT NULL,
    ended_at         TIMESTAMPTZ,
    confidence       TEXT NOT NULL,
    rule_version     TEXT NOT NULL,
    source_event_ids BIGINT[] NOT NULL,
    source           TEXT NOT NULL
);

-- Lot departures waiting for a return. anon_token is short-lived and cleared on match or expiry.
CREATE TABLE IF NOT EXISTS test_drive_candidate (
    id                BIGSERIAL PRIMARY KEY,
    anon_token        TEXT,
    departed_event_id BIGINT NOT NULL REFERENCES raw_event(id),
    departed_at       TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('open', 'matched', 'expired')),
    matched_event_id  BIGINT REFERENCES raw_event(id),
    rule_version      TEXT NOT NULL
);

-- Phase 3: fictional dealerships and clearly labelled simulated business records.
CREATE TABLE IF NOT EXISTS dealer (
    id      SERIAL PRIMARY KEY,
    code    TEXT UNIQUE NOT NULL,
    name    TEXT NOT NULL,
    region  TEXT NOT NULL,
    is_demo BOOLEAN NOT NULL DEFAULT false   -- the one dealership that receives live Ring events
);

ALTER TABLE device ADD COLUMN IF NOT EXISTS dealer_id INT REFERENCES dealer(id);
ALTER TABLE derived_event ADD COLUMN IF NOT EXISTS dealer_id INT REFERENCES dealer(id);
CREATE INDEX IF NOT EXISTS derived_event_dealer ON derived_event (dealer_id, type, started_at);

CREATE TABLE IF NOT EXISTS promotion (
    id        SERIAL PRIMARY KEY,
    code      TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at   TIMESTAMPTZ NOT NULL,
    source    TEXT NOT NULL DEFAULT 'simulated'
);

-- Simulated business data. Never Ring-derived: Ring cannot observe a sale or a finance agreement.
CREATE TABLE IF NOT EXISTS sale (
    id          BIGSERIAL PRIMARY KEY,
    dealer_id   INT NOT NULL REFERENCES dealer(id),
    occurred_at TIMESTAMPTZ NOT NULL,
    model_group TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'simulated' CHECK (source = 'simulated')
);
CREATE INDEX IF NOT EXISTS sale_dealer_time ON sale (dealer_id, occurred_at);

CREATE TABLE IF NOT EXISTS finance_deal (
    id           BIGSERIAL PRIMARY KEY,
    sale_id      BIGINT UNIQUE NOT NULL REFERENCES sale(id) ON DELETE CASCADE,  -- every deal belongs to a real simulated sale
    dealer_id    INT NOT NULL REFERENCES dealer(id),
    occurred_at  TIMESTAMPTZ NOT NULL,
    promotion_id INT REFERENCES promotion(id),
    source       TEXT NOT NULL DEFAULT 'simulated' CHECK (source = 'simulated')
);
CREATE INDEX IF NOT EXISTS finance_deal_dealer_time ON finance_deal (dealer_id, occurred_at);
