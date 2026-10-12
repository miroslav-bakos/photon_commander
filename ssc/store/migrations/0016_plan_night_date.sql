-- A plan can be assigned to one observing night without baking that date into
-- its portable observing instructions. Existing date-named plans are migrated
-- so the calendar is useful immediately after upgrade.

ALTER TABLE plans ADD COLUMN night_date TEXT;

UPDATE plans
SET night_date = substr(name, 10, 10)
WHERE (name LIKE 'Plan for ____-__-__%' OR name LIKE 'Night of ____-__-__%')
  AND date(substr(name, 10, 10)) IS NOT NULL;

CREATE INDEX plans_night_date ON plans (night_date, updated_at DESC);
