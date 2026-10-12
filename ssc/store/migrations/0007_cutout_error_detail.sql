-- `skymap_cutouts.error` becomes the human-readable sentence a viewer reads
-- ("Legacy Surveys could not produce this image (server error) — try again
-- later"); this column holds the raw upstream detail behind it (e.g. "host
-- answered 500") for a title/tooltip, never shown as the primary line
-- (ssc.skymap.cutouts._humanize_upstream_error). NULL whenever `error` is,
-- and for every `_OffFootprintError` (its message already is the primary
-- line — there is no separate raw detail worth keeping).

ALTER TABLE skymap_cutouts ADD COLUMN error_detail TEXT;
