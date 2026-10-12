-- Bulk target-field cutout prefetch (ssc/skymap/bulk_prefetch.py): a queue of
-- individual per-target cutout requests (skymap_cutouts, 0005_cutouts.sql),
-- scoped to a catalogue subset a user would plausibly image — never "the
-- whole catalogue" (THIRD_PARTY_NOTICES.md, CLAUDE.md invariant 7's "no silent
-- degradation" applied the other way: no silent over-fetch either).
--
-- One row per job, mirroring skymap_prefetch_jobs' shape: durable before
-- acted on (CLAUDE.md invariant 6), a restart marks anything still
-- queued/running as interrupted rather than resuming it. Per-object status
-- is never stored here — only the aggregate counters the snapshot carries;
-- a full per-object journal would be thousands of rows for one job and the
-- snapshot must stay small (see the module docstring).
--
-- ``scope``: 'messier' | 'magnitude'. ``magnitude_lte`` is set iff scope is
-- 'magnitude'. ``sources`` is a JSON array of cutout source ids
-- (ssc.skymap.surveys.CutoutSourceId).
--
-- Every timestamp column is an ISO-8601 UTC string with an explicit offset
-- (CLAUDE.md invariant 5), as in 0001_initial.sql.

CREATE TABLE skymap_bulk_cutout_jobs (
    id            TEXT    PRIMARY KEY,
    scope         TEXT    NOT NULL,            -- 'messier' | 'magnitude'
    magnitude_lte REAL,
    sources       TEXT    NOT NULL,            -- JSON array of source ids
    size_deg      REAL    NOT NULL,
    status        TEXT    NOT NULL,
    total         INTEGER NOT NULL DEFAULT 0,  -- object/source pairs in scope
    done          INTEGER NOT NULL DEFAULT 0,  -- pairs fetched this run
    skipped       INTEGER NOT NULL DEFAULT 0,  -- pairs already cached
    failed        INTEGER NOT NULL DEFAULT 0,  -- pairs that ended failed/unavailable
    bytes         INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    finished_at   TEXT
) STRICT;
