-- Captured frames: SSC's own session catalogue (DESIGN.md §9.5.4).
--
-- One row per FITS file SSC writes into the configured capture folder. The
-- row is inserted with status 'writing' *before* any FITS bytes reach disk
-- (CLAUDE.md invariant 6) and flipped to 'complete' only after the file has
-- been fsynced and renamed into place. A row a killed process left
-- 'writing' is never presented as a captured frame; ssc.capture's startup
-- reconciliation discards it (and whatever partial bytes exist at `path`)
-- rather than trying to resume a half-written image.
--
-- `object`/`filter` are the SSC-authored plan/wheel state, not device
-- fields (§9.5.2's header table: the device does not know the target).
-- Sessions are never stored here — they are derived on read, purely from
-- `object` and `date_obs` (ssc.capture.catalogue.derive_sessions), so a user
-- reorganising the capture folder on disk cannot desynchronise them.

CREATE TABLE capture_frame (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    object      TEXT,                 -- NULL: no target was known at capture time
    filter      TEXT,                 -- NULL: no filter wheel position was known
    date_obs    TEXT NOT NULL,        -- ISO-8601 UTC; SSC's own exposure-start clock
    exposure_s  REAL,
    gain        INTEGER,
    ccd_temp_c  REAL,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('writing', 'complete')),
    created_at  TEXT NOT NULL
) STRICT;

CREATE INDEX capture_frame_object_date ON capture_frame (object, date_obs);
CREATE INDEX capture_frame_status ON capture_frame (status);
