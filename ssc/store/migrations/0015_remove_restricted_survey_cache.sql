-- Purge metadata for a survey source that is not licensed for commercial use.
-- Its legacy files are removed by CutoutService.startup because SQL cannot
-- delete files.
DELETE FROM skymap_cutout_pieces
WHERE cutout_id IN (SELECT id FROM skymap_cutouts WHERE source = 'dss2-color');

DELETE FROM skymap_cutouts WHERE source = 'dss2-color';

DELETE FROM skymap_bulk_cutout_jobs
WHERE EXISTS (
    SELECT 1 FROM json_each(skymap_bulk_cutout_jobs.sources)
    WHERE json_each.value = 'dss2-color'
);
