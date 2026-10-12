-- Preserve quality evidence even when rejected raw files are intentionally removed.
CREATE TABLE capture_stack_outcome (
    frame_id INTEGER PRIMARY KEY,
    run_id TEXT,
    exposure_s REAL,
    outcome TEXT NOT NULL CHECK(outcome IN ('accepted', 'rejected', 'error'))
);
CREATE INDEX capture_stack_outcome_run ON capture_stack_outcome(run_id);
CREATE TABLE run_final_mount (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    polled_at TEXT,
    at_park INTEGER,
    tracking INTEGER,
    slewing INTEGER
);
