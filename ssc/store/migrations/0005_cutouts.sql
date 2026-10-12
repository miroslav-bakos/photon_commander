-- Target-field cutouts (ssc/skymap/cutouts.py): deep imagery for one field,
-- fetched from WISE (IRSA), SDSS or Legacy Surveys, kept beside the target
-- with its WCS rather than as tiles (THIRD_PARTY_NOTICES.md "Sky-survey layers",
-- consequence 1). File bytes live on disk under
-- ``data_dir/cutouts/<source>/<key>/``; this migration is the index.
--
-- ``cache_key`` is ``source + release tag + geometry rounded to 0.01 deg``
-- (``ssc.skymap.cutouts.cache_key``) — a second request for the same key is
-- a cache hit, no fetch. It is the row's natural key; ``id`` is a separate
-- short public id (like ``skymap_prefetch_jobs.id``) so a client's URL does
-- not have to carry ra/dec/size.
--
-- ``status``: queued|running|complete|unavailable|failed|cancelled|interrupted,
-- mirroring ``skymap_prefetch_jobs.status`` — 'unavailable' is the archive
-- confirmed there is nothing here (off-footprint, or unreachable and there
-- is no fallback route); 'failed' is an unexpected error.
--
-- ``skymap_cutout_pieces`` rows are the manifest ``GET
-- /api/skymap/cutouts/{id}`` serves: one WCS (or none, for a quick-look JPEG
-- with no header WCS) per piece, as plain numbers, plus the path to its
-- bytes and — for a FITS piece — the path to the server-rendered display PNG
-- (``ssc.skymap.png``) so the browser never parses FITS.
--
-- Every timestamp column is an ISO-8601 UTC string with an explicit offset
-- (CLAUDE.md invariant 5), as in 0001_initial.sql.

CREATE TABLE skymap_cutouts (
    id         TEXT    PRIMARY KEY,
    cache_key  TEXT    NOT NULL,
    source     TEXT    NOT NULL,            -- 'wise' | 'sdss' | 'legacy-surveys'
    ra_deg     REAL    NOT NULL,
    dec_deg    REAL    NOT NULL,
    size_deg   REAL    NOT NULL,
    bands      TEXT,                        -- JSON array of band names, or NULL
    status     TEXT    NOT NULL,
    bytes      INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    fetched_at TEXT,
    last_used  TEXT    NOT NULL
) STRICT;

CREATE UNIQUE INDEX skymap_cutouts_cache_key ON skymap_cutouts (cache_key);
CREATE INDEX skymap_cutouts_lru ON skymap_cutouts (last_used);
-- Offline lookup by position (GET /api/skymap/cutouts?ra&dec&radius): a
-- coarse pre-filter box, refined in Python with a real angular separation.
CREATE INDEX skymap_cutouts_pos ON skymap_cutouts (ra_deg, dec_deg);

CREATE TABLE skymap_cutout_pieces (
    cutout_id  TEXT    NOT NULL REFERENCES skymap_cutouts (id),
    idx        INTEGER NOT NULL,
    band       TEXT,
    format     TEXT    NOT NULL,            -- 'fits' | 'png' | 'jpg'
    path       TEXT    NOT NULL,            -- relative to data_dir/cutouts/
    bytes      INTEGER NOT NULL DEFAULT 0,
    width      INTEGER,
    height     INTEGER,
    crpix1     REAL,
    crpix2     REAL,
    crval1     REAL,
    crval2     REAL,
    cd1_1      REAL,
    cd1_2      REAL,
    cd2_1      REAL,
    cd2_2      REAL,
    ctype1     TEXT,
    ctype2     TEXT,
    -- The stretch description (ssc.skymap.png.describe_stretch) and the path
    -- to the server-rendered display PNG; both NULL for a piece that is
    -- already a display image itself (SDSS's getjpeg quick-look).
    stretch          TEXT,
    display_png_path TEXT,
    PRIMARY KEY (cutout_id, idx)
) STRICT;
