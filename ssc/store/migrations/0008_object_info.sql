-- Click-through info (M11): one row per catalogue designation ever looked
-- up while the feature was enabled, so a repeat lookup works with zero
-- network calls (docs/IMPLEMENTATION.md M11 "Click-through info").
--
-- `found = 0` is a real, cacheable answer ("Wikipedia has no article for
-- this"), not a failure -- a transient upstream error is never written here
-- (ssc/objectinfo/service.py). The image, when present, is stored as bytes
-- directly (a single small thumbnail, unlike the multi-piece FITS cutouts
-- in skymap_cutouts) alongside *that file's own* licence fields, never a
-- blanket assumption (THIRD_PARTY_NOTICES.md's load-bearing finding).

CREATE TABLE object_info (
    cache_key                TEXT    PRIMARY KEY, -- normalised designation
    designation               TEXT    NOT NULL,    -- as looked up, verbatim
    common_name               TEXT,
    found                     INTEGER NOT NULL,    -- 0 | 1
    title                     TEXT,                -- resolved Wikipedia article title
    article_url               TEXT,
    extract                   TEXT,
    wikidata_qid              TEXT,
    instance_of               TEXT,                -- JSON array of English labels
    discovery_date            TEXT,
    image_commons_title       TEXT,                -- "File:...", Wikidata's P18 value
    image_source_url          TEXT,                -- Commons file description page
    image_thumb_url           TEXT,                -- what was actually fetched
    image_width               INTEGER,
    image_height              INTEGER,
    image_media_type          TEXT,
    image_license_short_name  TEXT,                -- Commons' own extmetadata, per file
    image_artist              TEXT,
    image_credit              TEXT,
    image_bytes               BLOB,
    fetched_at                TEXT    NOT NULL
) STRICT;
