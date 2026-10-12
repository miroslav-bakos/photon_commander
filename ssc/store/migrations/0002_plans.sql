-- Plans (M5): the user's portable observing intent, stored verbatim.
--
-- ``document`` is the whole schema-versioned Plan JSON (``ssc.plan.models.Plan``)
-- — never normalised into columns. A plan is authored by hand, by the
-- Planning tab's "Send to Scheduling", or by a template, and it has to
-- round-trip exactly, including fields a future schema version adds that this
-- build does not otherwise care about.
--
-- ``name`` and ``revision`` are pulled out purely so the plan list and the
-- optimistic-concurrency check (``PUT /api/plans/{id}``) are a query, not a
-- full JSON parse of every row.
--
-- Every timestamp column is an ISO-8601 UTC string with an explicit offset
-- (CLAUDE.md invariant 5), matching 0001_initial.sql's convention. These are
-- the store's own bookkeeping — when this row was created/last written —
-- distinct from the ``created_at``/``updated_at`` the Plan document itself
-- carries inside ``document``.

CREATE TABLE plans (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    document   TEXT NOT NULL,              -- the schema-versioned Plan JSON, verbatim
    revision   INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX plans_updated_at ON plans (updated_at DESC);
