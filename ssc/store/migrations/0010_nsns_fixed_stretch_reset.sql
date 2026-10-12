-- Invalidates cached NSNS narrowband tiles converted by the OLD per-tile
-- ``ssc.skymap.png.asinh_stretch`` (each tile normalised to its own pixel
-- percentiles), now replaced by ``asinh_stretch_reference`` against a fixed
-- survey-wide black/white point (``Survey.fits_stretch_black``/``_white``,
-- ssc/skymap/surveys.py). The old conversion made a bright tile and its
-- fainter neighbour disagree about what a given intensity should look like
-- (nsns-halpha order-6 npix 21439 stretched to a mean display value of
-- 11.5, while its actual-fainter neighbour npix 21480 stretched to 190 —
-- the reverse of what the sky actually looks like there) — a tile already
-- cached under that scheme does not match a tile freshly fetched under the
-- new one, so every one of them must be treated as stale, not just the
-- specific tiles that happened to be reported.
--
-- This deletes only the index rows, not the files on disk: ``TileCache``
-- always checks this table before ever reading a file (``get_tile`` in
-- ssc/skymap/service.py), so an orphaned old PNG is simply never looked at
-- again — the next request for that tile misses the cache, refetches from
-- the archive, and overwrites the old file at the same path with the new
-- conversion. Nothing here touches ``nsns-ohs8`` (composited RGB, served
-- unmodified — never went through either stretch) or these surveys'
-- ``properties`` files (``tile_order = -1``, plain text, unaffected by
-- either stretch).
--
-- A user with a warm cache (the product owner's own instance, mid-prefetch
-- when this bug was found) experiences this as: every previously-fetched
-- nsns-halpha/-oiii/-sii tile falls out of the cache and is refetched from
-- www.simg.de on next view — a one-time re-download of whatever of these
-- three surveys had already been viewed or prefetched, nothing more.

DELETE FROM skymap_tiles
WHERE survey IN ('nsns-halpha', 'nsns-oiii', 'nsns-sii')
  AND tile_order >= 0;
