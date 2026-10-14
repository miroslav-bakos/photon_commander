-- Derived, replaceable raw-FITS quality metrics; never capture authority.
CREATE TABLE capture_quality_cache (
    path TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    quality_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
