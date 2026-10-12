CREATE TABLE polar_alignment_night (
    night_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (night_date, timezone)
);
