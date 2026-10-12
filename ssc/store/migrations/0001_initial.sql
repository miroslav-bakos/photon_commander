-- SSC initial schema.
--
-- Scope of this migration: the things ssc-core itself owns — configuration,
-- remembered device addresses, and browser sessions. Plans, run logs and
-- forecast caches arrive with the milestones that need them (M5, M6).
--
-- Every timestamp column is an ISO-8601 UTC string with an explicit offset
-- (CLAUDE.md invariant 5). SQLite has no date type; the discipline is ours.

CREATE TABLE config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,          -- JSON document for one settings section
    updated_at TEXT NOT NULL
) STRICT;

-- Remembered device addresses. There is no seeded row and no default: the
-- address is discovered or typed, never shipped (CLAUDE.md invariant 11).
CREATE TABLE device_address (
    host          TEXT NOT NULL,
    control_port  INTEGER NOT NULL,
    label         TEXT,
    firmware      TEXT,                -- last observed, NULL means never probed
    model         TEXT,
    serial        TEXT,
    last_seen_at  TEXT,                -- NULL means never reachable
    last_good_at  TEXT,                -- NULL means never completed a handshake
    created_at    TEXT NOT NULL,
    PRIMARY KEY (host, control_port)
) STRICT;

CREATE INDEX device_address_last_good
    ON device_address (last_good_at DESC);

-- Browser sessions created by exchanging a pairing token. Only the SHA-256 of
-- the cookie value is stored: a stolen database must not be a stolen session.
CREATE TABLE auth_session (
    token_sha256 TEXT PRIMARY KEY,
    scope        TEXT NOT NULL CHECK (scope IN ('view', 'control')),
    label        TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
) STRICT;

CREATE INDEX auth_session_expiry ON auth_session (expires_at);
