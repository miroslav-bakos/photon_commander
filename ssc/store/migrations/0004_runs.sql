-- Runs (M5): one row per execution of a compiled plan, plus its append-only
-- journal — the durable half of ``ssc/scheduler/executor.py``.
--
-- ``plan_document`` and ``schedule`` are stored verbatim (the Plan JSON and
-- the ``CompiledSchedule`` JSON as they were the moment the run started). The
-- executor joins the two on ``item_id`` while it runs *and* while it resumes,
-- so a plan edited after the run began must not change what the resumed run
-- does — the row carries its own copy for that reason, not a foreign key.
--
-- ``status`` is a summary for the run list and the interrupted-run check at
-- startup (running | paused | stopped | completed | interrupted | failed).
-- It is derived from the journal and re-written as the run progresses; the
-- journal, never this column, is the source of truth for *where* a run is.
--
-- ``run_journal`` is append-only: rows are inserted, never updated or
-- deleted (CLAUDE.md invariant 6 — durable before acted on). ``seq`` is
-- per-run, dense from 1, and ``record`` is the ``JournalRecord`` JSON.
--
-- Every timestamp column is an ISO-8601 UTC string with an explicit offset
-- (CLAUDE.md invariant 5), as in 0001_initial.sql.

CREATE TABLE runs (
    id            TEXT    PRIMARY KEY,
    plan_id       TEXT    NOT NULL,
    plan_name     TEXT    NOT NULL,
    plan_revision INTEGER NOT NULL,
    plan_document TEXT    NOT NULL,        -- the Plan JSON, as compiled
    schedule      TEXT    NOT NULL,        -- the CompiledSchedule JSON
    status        TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    ended_at      TEXT,
    ended_reason  TEXT
) STRICT;

CREATE INDEX runs_started_at ON runs (started_at DESC);

CREATE TABLE run_journal (
    run_id TEXT    NOT NULL REFERENCES runs (id),
    seq    INTEGER NOT NULL,
    at     TEXT    NOT NULL,
    record TEXT    NOT NULL,               -- the JournalRecord JSON
    PRIMARY KEY (run_id, seq)
) STRICT;
