-- Sky-survey tile cache (ssc/skymap): the *index* only. Tile bytes live on
-- disk under ``data_dir/tiles/<survey>/...`` as plain files, laid out exactly
-- like the upstream pyramid; this table is what makes LRU eviction, pack
-- coverage and size accounting a query rather than a directory walk.
--
-- ``status``: 'cached' (the file exists) or 'missing' (upstream answered 404 —
-- the tile is outside the survey's footprint; a negative entry with no file,
-- so a pack with a hole can still be *complete* without ever pretending the
-- hole is imagery).
-- ``pinned``: 1 when fetched by an offline-pack job. Pinned tiles, and every
-- tile of an order the survey's pack has completed, are never evicted; only
-- ``DELETE /api/skymap/cache/{survey}`` removes them.
-- ``tile_order`` is -1 for the survey's HiPS ``properties`` file (npix 0).
--
-- Every timestamp column is an ISO-8601 UTC string with an explicit offset
-- (CLAUDE.md invariant 5), as in 0001_initial.sql.

CREATE TABLE skymap_tiles (
    survey     TEXT    NOT NULL,
    tile_order INTEGER NOT NULL,
    npix       INTEGER NOT NULL,
    ext        TEXT    NOT NULL,
    status     TEXT    NOT NULL,            -- 'cached' | 'missing'
    bytes      INTEGER NOT NULL DEFAULT 0,
    pinned     INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT    NOT NULL,
    last_used  TEXT    NOT NULL,
    PRIMARY KEY (survey, tile_order, npix)
) STRICT;

CREATE INDEX skymap_tiles_lru ON skymap_tiles (survey, pinned, last_used);

-- Offline-pack downloads. A job row is written *before* the first tile is
-- requested and updated as it goes, so a restart finds it and marks it
-- 'interrupted' (never silently resumed — the user restarts it).
CREATE TABLE skymap_prefetch_jobs (
    id          TEXT    PRIMARY KEY,
    survey      TEXT    NOT NULL,
    mode        TEXT    NOT NULL,           -- 'sky' | 'cone'
    max_order   INTEGER NOT NULL,
    ra_deg      REAL,
    dec_deg     REAL,
    radius_deg  REAL,
    status      TEXT    NOT NULL,           -- queued|running|complete|failed|cancelled|interrupted
    done        INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    missing     INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    finished_at TEXT
) STRICT;

CREATE INDEX skymap_prefetch_jobs_created ON skymap_prefetch_jobs (created_at DESC);
