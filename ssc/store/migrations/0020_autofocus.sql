-- One latest measured run, retained for presentation only.
CREATE TABLE autofocus_latest (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
