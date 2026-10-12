-- The upstream URL one piece was fetched from, when the source fetches many
-- discrete files for one cutout (AllWISE's Atlas tiles — `_fetch_wise_incremental`,
-- ssc.skymap.cutouts) rather than one file per band. NULL for every other
-- source and for an imported piece (`CutoutService.import_cutout`) — nothing
-- to resume there.
--
-- Read back on a retry to skip a tile already on disk: a 6 deg AllWISE box
-- can need up to ~21 Atlas tiles per band at ~45 MB/~100 s each (measured
-- live, 2026-09-09), so an interruption must resume from wherever it left
-- off rather than re-downloading tiles IRSA already served once — CLAUDE.md
-- invariant 6 and the "good citizen of the upstream" rule both apply to a
-- fetch this size the same way they apply to the tile prefetch jobs.

ALTER TABLE skymap_cutout_pieces ADD COLUMN source_url TEXT;
