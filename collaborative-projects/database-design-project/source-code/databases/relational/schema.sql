-- ============================================================
--  TransitFlow PostgreSQL Schema
--  Seed data is loaded separately by: python skeleton/seed_postgres.py
--
--  TWO ROLES:
--    1. Relational  → dual-network transit data you design below
--    2. Vector      → policy documents for RAG (provided — do not modify)
-- ============================================================

-- ============================================================
--  STUDENT TASK — Design and create your relational tables here
--
--  Start from the mock data in train-mock-data/:
--    metro_stations.json, national_rail_stations.json
--    metro_schedules.json, national_rail_schedules.json
--    national_rail_seat_layouts.json
--    registered_users.json
--    bookings.json, metro_travel_history.json
--    payments.json, feedback.json
--
--  Think about:
--    - What tables do you need?
--    - What columns and data types?
--    - Which fields are primary keys? Which are foreign keys?
--    - What constraints make sense?
--
--  Apply your schema with:
--    docker-compose down -v && docker-compose up -d
-- ============================================================

-- ============================================================
-- DESIGN DECISIONS
-- ============================================================
--
-- PK Design (Surrogate Key Pattern):
--   We use surrogate PKs (UUID or SERIAL) alongside preserved business IDs.
--   The business ID (e.g. "RU01", "MS01") is kept as a UNIQUE NOT NULL
--   column so that existing application code, the agent, and seed scripts
--   continue to work unchanged.
--
--   UUID is chosen for tables whose IDs may be exposed externally
--   (users, bookings, payments) — UUIDs are globally unique and
--   non-sequential, preventing enumeration attacks (guessing IDs).
--
--   SERIAL is chosen for internal structural tables (stations, schedules,
--   seat layouts, coaches, seats) where IDs stay within the database
--   and auto-increment is sufficient.  SERIALs are smaller (4-byte INT)
--   than UUIDs, making joins and indexes on these high-join tables faster.
--
--   FK columns continue to reference the UNIQUE business-ID column rather
--   than the surrogate PK.  PostgreSQL permits FKs on any UNIQUE NOT NULL
--   column — this keeps all queries and seed logic stable while the schema
--   gains proper surrogate keys.
--
-- Delete Strategy: SOFT DELETE for users (is_active BOOLEAN flag) so that
--   historical bookings, payments, and feedback remain intact even when a
--   user deactivates their account. HARD DELETE (ON DELETE CASCADE) is used
--   for structural child rows (schedule stops, coaches, seats) that have no
--   meaning without their parent. Transactional tables (bookings, payments,
--   feedback) use ON DELETE RESTRICT to prevent accidental data loss.
-- ============================================================

-- Required for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- Users Domain
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    -- UUID PK: user IDs may appear in booking references or external APIs;
    -- UUID prevents sequential enumeration of user accounts.
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(50) UNIQUE NOT NULL,   -- business ID e.g. "RU01"
    full_name       VARCHAR(100) NOT NULL,
    email           VARCHAR(255) UNIQUE NOT NULL,
    phone           VARCHAR(50),
    date_of_birth   DATE,
    secret_question VARCHAR(255),
    registered_at   TIMESTAMPTZ DEFAULT NOW(),
    is_active       BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS user_passwords (
    -- 1-to-1 with users; uses the business user_id as PK/FK so that the
    -- join is a simple equality on a UNIQUE indexed column.
    user_id             VARCHAR(50) PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    password_hash       VARCHAR(255) NOT NULL,
    secret_answer_hash  VARCHAR(255)
);

-- ============================================================
-- Stations
-- ============================================================

CREATE TABLE IF NOT EXISTS metro_stations (
    -- SERIAL PK: stations are a small, stable, internal reference set.
    -- SERIAL (4-byte integer) is lighter than UUID for a table that is
    -- joined on every schedule and booking query.
    id          SERIAL PRIMARY KEY,
    station_id  VARCHAR(50) UNIQUE NOT NULL,  -- business ID e.g. "MS01"
    name        VARCHAR(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS national_rail_stations (
    id          SERIAL PRIMARY KEY,
    station_id  VARCHAR(50) UNIQUE NOT NULL,  -- business ID e.g. "NR01"
    name        VARCHAR(100) NOT NULL
);

-- ============================================================
-- Metro Schedules
-- ============================================================

CREATE TABLE IF NOT EXISTS metro_schedules (
    -- SERIAL PK: schedules are managed internally and never exposed
    -- as opaque tokens; SERIAL keeps the index compact.
    id                      SERIAL PRIMARY KEY,
    schedule_id             VARCHAR(50) UNIQUE NOT NULL,  -- e.g. "MS_SCH01"
    line                    VARCHAR(50) NOT NULL,
    direction               VARCHAR(50) NOT NULL,
    origin_station_id       VARCHAR(50) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    destination_station_id  VARCHAR(50) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    first_train_time        TIME,
    last_train_time         TIME,
    base_fare_usd           NUMERIC(10, 2) NOT NULL,
    per_stop_rate_usd       NUMERIC(10, 2) NOT NULL,
    frequency_min           INT NOT NULL
);

CREATE TABLE IF NOT EXISTS metro_schedule_stops (
    schedule_id                 VARCHAR(50) REFERENCES metro_schedules(schedule_id) ON DELETE CASCADE,
    station_id                  VARCHAR(50) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    stop_order                  INT NOT NULL,
    travel_time_from_origin_min INT NOT NULL,
    PRIMARY KEY (schedule_id, station_id)
);

CREATE TABLE IF NOT EXISTS metro_schedule_days (
    schedule_id  VARCHAR(50) REFERENCES metro_schedules(schedule_id) ON DELETE CASCADE,
    day_of_week  VARCHAR(3) NOT NULL,
    PRIMARY KEY (schedule_id, day_of_week)
);

-- ============================================================
-- National Rail Schedules
-- ============================================================

CREATE TABLE IF NOT EXISTS national_rail_schedules (
    id                      SERIAL PRIMARY KEY,
    schedule_id             VARCHAR(50) UNIQUE NOT NULL,  -- e.g. "NR_SCH01"
    line                    VARCHAR(50) NOT NULL,
    service_type            VARCHAR(50) NOT NULL,
    direction               VARCHAR(50) NOT NULL,
    origin_station_id       VARCHAR(50) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    destination_station_id  VARCHAR(50) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    first_train_time        TIME,
    last_train_time         TIME,
    frequency_min           INT NOT NULL
);

CREATE TABLE IF NOT EXISTS national_rail_schedule_stops (
    schedule_id                 VARCHAR(50) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    station_id                  VARCHAR(50) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    stop_order                  INT NOT NULL,
    travel_time_from_origin_min INT NOT NULL,
    is_passed_through           BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (schedule_id, station_id)
);

CREATE TABLE IF NOT EXISTS national_rail_schedule_fares (
    schedule_id        VARCHAR(50) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    fare_class         VARCHAR(50) NOT NULL,
    base_fare_usd      NUMERIC(10, 2) NOT NULL,
    per_stop_rate_usd  NUMERIC(10, 2) NOT NULL,
    PRIMARY KEY (schedule_id, fare_class)
);

CREATE TABLE IF NOT EXISTS national_rail_schedule_days (
    schedule_id  VARCHAR(50) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    day_of_week  VARCHAR(3) NOT NULL,
    PRIMARY KEY (schedule_id, day_of_week)
);

-- ============================================================
-- Seat Layouts
-- ============================================================

CREATE TABLE IF NOT EXISTS national_rail_seat_layouts (
    -- SERIAL PK: purely internal seat-map data; SERIAL is sufficient.
    id           SERIAL PRIMARY KEY,
    layout_id    VARCHAR(50) UNIQUE NOT NULL,  -- e.g. "NR_LAYOUT01"
    schedule_id  VARCHAR(50) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS national_rail_coaches (
    id          SERIAL PRIMARY KEY,
    coach_id    VARCHAR(50) UNIQUE NOT NULL,  -- e.g. "NR_LAYOUT01_A"
    layout_id   VARCHAR(50) REFERENCES national_rail_seat_layouts(layout_id) ON DELETE CASCADE,
    coach_name  VARCHAR(50) NOT NULL,
    fare_class  VARCHAR(50) NOT NULL
);

CREATE TABLE IF NOT EXISTS national_rail_seats (
    -- Composite PK on business keys: a seat is uniquely identified by its
    -- coach and seat label; no surrogate key needed for this leaf table.
    seat_id      VARCHAR(50) NOT NULL,
    coach_id     VARCHAR(50) REFERENCES national_rail_coaches(coach_id) ON DELETE CASCADE,
    row          INT NOT NULL,
    seat_column  VARCHAR(5) NOT NULL,
    PRIMARY KEY (coach_id, seat_id)
);

-- ============================================================
-- Transactions
-- ============================================================

CREATE TABLE IF NOT EXISTS national_rail_bookings (
    -- UUID PK: booking references are shown to passengers on tickets and
    -- in confirmation emails; UUID prevents sequential ID guessing.
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id              VARCHAR(50) UNIQUE NOT NULL,  -- e.g. "BK001" or "BK-XXXXXX"
    user_id                 VARCHAR(50) REFERENCES users(user_id) ON DELETE RESTRICT,
    schedule_id             VARCHAR(50) REFERENCES national_rail_schedules(schedule_id) ON DELETE RESTRICT,
    origin_station_id       VARCHAR(50) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    destination_station_id  VARCHAR(50) REFERENCES national_rail_stations(station_id) ON DELETE RESTRICT,
    travel_date             DATE NOT NULL,
    departure_time          TIME NOT NULL,
    ticket_type             VARCHAR(50) NOT NULL,
    fare_class              VARCHAR(50) NOT NULL,
    coach_id                VARCHAR(50),
    seat_id                 VARCHAR(50),
    FOREIGN KEY (coach_id, seat_id) REFERENCES national_rail_seats(coach_id, seat_id) ON DELETE SET NULL,
    CONSTRAINT national_rail_bookings_seat_pair_chk CHECK (
        (coach_id IS NULL AND seat_id IS NULL) OR
        (coach_id IS NOT NULL AND seat_id IS NOT NULL)
    ),
    stops_travelled         INT NOT NULL,
    amount_usd              NUMERIC(10, 2) NOT NULL,
    status                  VARCHAR(50) DEFAULT 'confirmed',
    booked_at               TIMESTAMPTZ DEFAULT NOW(),
    travelled_at            TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS metro_travel_history (
    -- UUID PK: trip records may be referenced in receipts or exports.
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id                 VARCHAR(50) UNIQUE NOT NULL,  -- e.g. "MT001"
    user_id                 VARCHAR(50) REFERENCES users(user_id) ON DELETE RESTRICT,
    schedule_id             VARCHAR(50) REFERENCES metro_schedules(schedule_id) ON DELETE RESTRICT,
    origin_station_id       VARCHAR(50) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    destination_station_id  VARCHAR(50) REFERENCES metro_stations(station_id) ON DELETE RESTRICT,
    travel_date             DATE NOT NULL,
    ticket_type             VARCHAR(50) NOT NULL,
    day_pass_ref            VARCHAR(50),
    stops_travelled         INT,
    amount_usd              NUMERIC(10, 2) NOT NULL,
    status                  VARCHAR(50) DEFAULT 'completed',
    purchased_at            TIMESTAMPTZ DEFAULT NOW(),
    travelled_at            TIMESTAMPTZ
);

-- ============================================================
-- Payments
-- ============================================================

CREATE TABLE IF NOT EXISTS metro_payments (
    -- UUID PK: payment IDs are referenced in refund records and
    -- external payment gateway callbacks; UUID prevents enumeration.
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id  VARCHAR(50) UNIQUE NOT NULL,
    trip_id     VARCHAR(50) NOT NULL REFERENCES metro_travel_history(trip_id) ON DELETE CASCADE,
    amount_usd  NUMERIC(10, 2) NOT NULL,
    method      VARCHAR(50) NOT NULL,
    status      VARCHAR(50) NOT NULL,
    paid_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS national_rail_payments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id  VARCHAR(50) UNIQUE NOT NULL,
    booking_id  VARCHAR(50) NOT NULL REFERENCES national_rail_bookings(booking_id) ON DELETE CASCADE,
    amount_usd  NUMERIC(10, 2) NOT NULL,
    method      VARCHAR(50) NOT NULL,
    status      VARCHAR(50) NOT NULL,
    paid_at     TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Feedbacks
-- ============================================================

CREATE TABLE IF NOT EXISTS metro_feedbacks (
    -- SERIAL PK: feedback records are internal analytics data only;
    -- they are never exposed to passengers as tokens, so SERIAL suffices.
    id            SERIAL PRIMARY KEY,
    feedback_id   VARCHAR(50) UNIQUE NOT NULL,
    trip_id       VARCHAR(50) NOT NULL REFERENCES metro_travel_history(trip_id) ON DELETE CASCADE,
    user_id       VARCHAR(50) REFERENCES users(user_id) ON DELETE SET NULL,
    rating        INT CHECK (rating BETWEEN 1 AND 5),
    comment       TEXT,
    submitted_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS national_rail_feedbacks (
    id            SERIAL PRIMARY KEY,
    feedback_id   VARCHAR(50) UNIQUE NOT NULL,
    booking_id    VARCHAR(50) NOT NULL REFERENCES national_rail_bookings(booking_id) ON DELETE CASCADE,
    user_id       VARCHAR(50) REFERENCES users(user_id) ON DELETE SET NULL,
    rating        INT CHECK (rating BETWEEN 1 AND 5),
    comment       TEXT,
    submitted_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- TASK 6 EXTENSION: bonus-feature schema objects below —
--   tables platform_assignments, delay_records, loyalty_transactions,
--   and the users.loyalty_points column. See TASK6.md.
-- ============================================================

-- ============================================================
-- Platform Assignments
-- ============================================================

CREATE TABLE IF NOT EXISTS platform_assignments (
    -- Maps each national rail service (schedule) to a platform at each station it stops at.
    -- One row per (schedule, station) pair — a service always departs from the same platform.
    schedule_id     VARCHAR(50) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    station_id      VARCHAR(50) REFERENCES national_rail_stations(station_id)   ON DELETE CASCADE,
    platform_number VARCHAR(10) NOT NULL,
    PRIMARY KEY (schedule_id, station_id)
);

-- ============================================================
-- Service Delay Records
-- ============================================================

CREATE TABLE IF NOT EXISTS delay_records (
    -- Historical log of operator-reported delays per national rail service and date.
    -- Complements the graph-based delay ripple analysis with real recorded data.
    id            SERIAL PRIMARY KEY,
    delay_id      VARCHAR(50) UNIQUE NOT NULL,
    schedule_id   VARCHAR(50) REFERENCES national_rail_schedules(schedule_id) ON DELETE CASCADE,
    travel_date   DATE NOT NULL,
    delay_minutes INT NOT NULL CHECK (delay_minutes > 0),
    reason        VARCHAR(255),
    reported_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS delay_records_schedule_date_idx ON delay_records (schedule_id, travel_date);

-- ============================================================
-- Loyalty Points
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS loyalty_points INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS loyalty_transactions (
    -- Audit trail of every loyalty point earn/redeem event.
    -- positive points = earned (booking), negative = redeemed (future use).
    id          SERIAL PRIMARY KEY,
    user_id     VARCHAR(50) REFERENCES users(user_id) ON DELETE CASCADE,
    trip_ref    VARCHAR(50),          -- booking_id that triggered this transaction
    points      INT NOT NULL,
    reason      VARCHAR(100) NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS loyalty_transactions_user_idx ON loyalty_transactions (user_id);

-- ============================================================
--  VECTOR SCHEMA  (RAG / Help Desk) — do not modify
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_documents (
    id          SERIAL       PRIMARY KEY,
    title       VARCHAR(200) NOT NULL,
    category    VARCHAR(50)  NOT NULL,  -- 'refund', 'booking', 'conduct'
    content     TEXT         NOT NULL,
    -- 768-dim  → Ollama nomic-embed-text (default)
    -- 3072-dim → Gemini gemini-embedding-001
    -- If you switch LLM_PROVIDER to gemini, change to vector(3072) and reset the database.
    embedding   vector(768),
    source_file VARCHAR(200),
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

-- Index for fast cosine similarity search
CREATE INDEX IF NOT EXISTS policy_documents_embedding_idx ON policy_documents USING hnsw (embedding vector_cosine_ops);
