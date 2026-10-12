# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The survey registry: what may be drawn, from where, with which credit.

Every entry is data, and all of it is served to the client by
``GET /api/skymap/surveys`` — the credit line, the upstream host, whether the
survey may be bundled with the product — so the UI renders the registry
rather than hardcoding any of it (``THIRD_PARTY_NOTICES.md`` requirement 4).

Two rules from that document shape the entries:

* **Upstream is the originating archive**, never CDS/alasky (§9): IRSA/IPAC
  for WISE, NASA LAMBDA for H-alpha, SDSS's own servers, legacysurvey.org for
  Legacy Surveys and unWISE.
* **Nothing is bundled unless the licence explicitly permits redistribution.**
  ``bundle_allowed`` is ``False`` until a compliance report says otherwise;
  a survey the user prefetches into *their own* cache from the archive is
  caching, not redistribution, and is what the offline pack does.

A survey that is not drawn as tiles is still listed **here, and in the
``GET /api/skymap/surveys`` response** — with ``enabled=False`` and the
reason in ``pending_clearance`` — never omitted from the registry (CLAUDE.md
invariant 7): the API stays the honest, complete source of truth regardless
of what any one client does with it. That is a narrower promise than it used
to be: until 2026-09-08 the UI rendered a disabled row for every such entry
too, but the product owner has since decided the AllWISE/SDSS "cannot be a
background, only a target-field cutout" rows and the separate per-target
cutout-source rows are menu clutter now that a background survey's own
cache can be deepened for a target directly (`SkymapService`'s Mercator cone
prefetch, ``docs/DESIGN.md``-adjacent "6° detail" feature) — see
`web/src/skymap/LayersPanel.tsx`. The registry entries and the API response
are unchanged; only those UI rows are gone. The gate reading that fixed each
entry below is ``THIRD_PARTY_NOTICES.md`` "Sky-survey layers — gate reading of
2026-09-07".
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ssc.skymap import hips, mercator, toast


class SurveyKind(StrEnum):
    OPTICAL = "optical"
    INFRARED = "infrared"
    HALPHA = "halpha"
    #: [OIII] 500.7nm and [SII] 671.7/673.0nm narrowband — added 2026-09-09
    #: alongside the Northern Sky Narrowband Survey entries below, the first
    #: OIII/SII layers this registry has ever carried. The frontend's
    #: ``SkySurvey["kind"]`` union (``web/src/skymap/types.ts``) was widened
    #: to match on the same day, so the two are in step; a new kind here
    #: still needs adding there.
    OIII = "oiii"
    SII = "sii"


class TileScheme(StrEnum):
    """How the pyramid is addressed. See ``hips.py``, ``toast.py`` and ``mercator.py``."""

    HIPS = "hips"
    #: TOAST pyramid addressing (``toast.py``). Kept even though no registry
    #: entry below uses it: Pan-STARRS DR1 was the one TOAST survey and was
    #: removed 2026-09-08 ("obsolete, not usable for our purpose" — the
    #: product owner's own words). Not dead code by accident; the scheme is a
    #: real option for a future TOAST-addressed survey.
    #:
    #: Note the *frontend* can no longer draw it: the WWT frame that consumed
    #: TOAST was deleted 2026-09-09 (`cd169b9`) and the vector frame
    #: implements Mercator and HiPS only. Adding a TOAST survey now means
    #: implementing it in `web/src/skymap/frames/vector/` first. The addressing
    #: here and its tests remain correct and are worth keeping for that day.
    TOAST = "toast"
    #: Standard spherical Web Mercator (XYZ), e.g. Legacy Surveys' own tile server
    #: (``mercator.py``) — not HiPS, not WWT's TOAST.
    MERCATOR = "mercator"


class SurveyFrame(StrEnum):
    """Coordinate frame used by a HiPS pyramid's HEALPix indices."""

    EQUATORIAL = "equatorial"
    GALACTIC = "galactic"


#: HTTP media type per tile extension. Anything else is refused, not guessed.
TILE_MEDIA_TYPES: Final[dict[str, str]] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "fits": "application/fits",
}


class Survey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    label: str
    kind: SurveyKind
    scheme: TileScheme
    frame: SurveyFrame = SurveyFrame.EQUATORIAL
    #: The pyramid root at the originating archive. HiPS: the directory holding
    #: ``properties`` and ``Norder*/``. TOAST: the directory holding ``{level}/``.
    #: Empty for a bundled survey (``local_dir`` set): nothing is ever fetched.
    upstream_base_url: str
    #: A bundled pyramid shipped with the product (only when the licence
    #: permits redistribution — ``bundle_allowed`` must be ``True``), as a
    #: path *relative to the bundled HiPS root* (``AppConfig.bundled_hips_dir``,
    #: by default ``data/hips/``). Served with no network at all; never
    #: proxied, never cached, never prefetched.
    local_dir: Path | None = None
    tile_format: str
    #: The deepest order/level the upstream pyramid has. Requests beyond it are
    #: refused locally rather than forwarded to 404.
    max_order: int = Field(ge=0)
    credit_label: str
    credit_url: str
    #: Whether the licence explicitly permits shipping the tiles inside the
    #: product. ``False`` means "the user's own instance caches from upstream".
    bundle_allowed: bool = False
    #: A pack the licence lets us redistribute (mirror), when one exists.
    redistributable_pack_url: str | None = None
    #: An overlay (H-alpha, …) that is blended over a background, never used as one.
    blend_only: bool = False
    enabled: bool = True
    #: Why the survey is not available, when it is not.
    pending_clearance: str | None = None
    #: Experimental data may be exercised on a POC branch, but the API and
    #: UI must make it structurally impossible to mistake it for a source
    #: cleared for a commercial build.
    production_cleared: bool = True
    rights_notice: str | None = None
    #: The archive serves this pyramid as calibrated float32 FITS, not a
    #: browser-decodable image (the Northern Sky Narrowband Survey's
    #: individual H-alpha/[OIII]/[SII] bands: linear intensity in Rayleighs, full
    #: dynamic range — verified live 2026-09-09, ``file`` on a fetched tile
    #: read "FITS image data, 32-bit, floating point ... 512 x 512"). WWT's
    #: web engine decodes an imageset tile as an ordinary bitmap
    #: (`web/src/skymap/frames/wwt/imagesets.ts`); handed raw FITS bytes it
    #: draws nothing, silently — the exact failure mode this agent split
    #: exists to catch. When this is set, ``SkymapService`` fetches the
    #: tile as ``.fits`` from upstream and converts it to the ``.png``
    #: ``tile_format`` promises with ``ssc.skymap.png.asinh_stretch_reference``
    #: (**not** ``asinh_stretch``, and not any more since 2026-09-09 — see
    #: ``fits_stretch_black``/``fits_stretch_white`` below for why) before
    #: caching and serving it — never a bundled conversion (WHAM's is done
    #: once at build time; this licence forbids bundling anything, so it
    #: happens on every fetch-through, same as any other tile).
    #: ``tile_format`` must be ``"png"`` when this is set, and it is
    #: meaningless for a bundled survey — both enforced below.
    fits_stretch: bool = False
    #: The fixed black/white point (this survey's native units — NSNS's are
    #: Rayleighs) that every tile of a ``fits_stretch`` survey is stretched
    #: against, required exactly when ``fits_stretch`` is set (enforced
    #: below). ``asinh_stretch`` (deriving black/white from each tile's own
    #: percentiles) was the original conversion, and it was wrong for a tile
    #: *pyramid*: found live 2026-09-09 that ``nsns-halpha`` order-6 npix
    #: 21439 (the Orion Nebula core) stretched to a mean display value of
    #: 11.5 while its actual-fainter neighbour npix 21480 stretched to 190 —
    #: each tile normalised to *its own* dynamic range, so the brighter tile
    #: came out darker, and the seam between them read as a hard-edged
    #: mosaic rather than continuous sky (same pattern reproduced at order 2
    #: and in ``nsns-ohs8``'s already-composited tiles, though that survey
    #: has no ``fits_stretch`` of its own to fix — its stretch, if any, is
    #: baked in by the archive, out of this registry's control). A survey
    #: constant fixes it: the same Rayleigh value now maps to the same grey
    #: everywhere, so what varies on screen is the real sky, not which tile
    #: it happened to land in.
    #:
    #: A *computed-and-cached* reference (derive once from whatever tiles
    #: happen to be fetched first, memoise) was considered and rejected: it
    #: would drift with which corner of the sky a user happened to view
    #: first, and two installations could show the same survey differently
    #: depending on browsing order — a correctness bug of exactly this kind,
    #: just quieter. A registry constant is reproducible and identical for
    #: everyone.
    #:
    #: Derived 2026-09-09 by fetching real order-6 FITS tiles for 16 fields
    #: per band from the live archive (not by inference from one field): the
    #: 7 known bright H II regions NGC 7000/North America, M42/Orion,
    #: Rosette, California (NGC 1499), Heart (IC 1805), the Cygnus Loop/Veil
    #: and IC 1396; 6 faint, high-galactic-latitude control fields (RA/Dec
    #: 180/40, 225/30, 15/20, 150/15, 200/60, 100/20); and 2 near-pole fields
    #: (0/88, 180/87). ``black_point = 0`` for every band: calibrated,
    #: continuum-subtracted intensity, and the sampled faint fields cluster
    #: at or slightly below 0 with no real signal (e.g. H-alpha faint-field
    #: medians of 0.1-0.8 R), so 0 is the true "nothing here" floor, not a
    #: guess. ``white_point`` is chosen near the 99th percentile of the
    #: *bright, on-nebula* fields (deliberately excluding M42's own
    #: Trapezium-core outlier, which reaches ~140000 R in H-alpha alone and
    #: would crush everything else to black): H-alpha bright-field p99 was
    #: ~1100-1600 R (Cygnus Loop/Veil lower, ~187 R, being genuinely fainter
    #: diffuse emission) -> 1000; [OIII] bright-field p99 was ~50-440 R -> 300;
    #: [SII] bright-field p99 was ~115-333 R -> 250. Verified against the
    #: Orion-core tile and its 8 order-6 neighbours (npix 21436-22848): mean
    #: display values went from an 11.5-vs-190 split (``asinh_stretch``) to a
    #: continuous 82-155 spread (``asinh_stretch_reference`` at these
    #: constants) in H-alpha, and the same neighbours no longer invert
    #: brightness (the bright core tile no longer renders darker than its
    #: fainter neighbours) in [OIII]/[SII].
    fits_stretch_black: float | None = None
    fits_stretch_white: float | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Survey:
        if not self.production_cleared:
            if not self.rights_notice:
                raise ValueError(f"survey {self.id}: uncleared POC source needs rights_notice")
            if self.bundle_allowed:
                raise ValueError(f"survey {self.id}: uncleared POC source cannot be bundled")
        if self.local_dir is not None:
            if self.upstream_base_url:
                raise ValueError(f"survey {self.id}: a bundled survey has no upstream URL")
            if not self.bundle_allowed:
                raise ValueError(f"survey {self.id}: bundled but bundle_allowed is False")
            if self.scheme is not TileScheme.HIPS:
                raise ValueError(f"survey {self.id}: only HiPS directories can be bundled")
            if self.local_dir.is_absolute():
                raise ValueError(f"survey {self.id}: local_dir is relative to the bundled root")
            if self.fits_stretch:
                raise ValueError(f"survey {self.id}: fits_stretch is only for fetch-through tiles")
        elif not self.upstream_base_url:
            raise ValueError(f"survey {self.id}: neither an upstream URL nor a local_dir")
        elif (
            "worldwidetelescope.org" in self.upstream_host or "wwtassets.org" in self.upstream_host
        ):
            # Their terms are personal/non-commercial (THIRD_PARTY_NOTICES.md).
            raise ValueError(f"survey {self.id}: WWT's servers are never an upstream")
        elif (
            "alasky" in self.upstream_host or "unistra.fr" in self.upstream_host
        ) and self.production_cleared:
            raise ValueError(f"survey {self.id}: CDS is never an upstream (COMPLIANCE.md §9)")
        if self.fits_stretch and self.tile_format != "png":
            raise ValueError(
                f"survey {self.id}: fits_stretch converts to png, not {self.tile_format}"
            )
        if self.fits_stretch:
            if self.fits_stretch_black is None or self.fits_stretch_white is None:
                raise ValueError(
                    f"survey {self.id}: fits_stretch needs fits_stretch_black/white "
                    "(a fixed reference — see the field docstring for why)"
                )
            if self.fits_stretch_white <= self.fits_stretch_black:
                raise ValueError(
                    f"survey {self.id}: fits_stretch_white ({self.fits_stretch_white}) must "
                    f"exceed fits_stretch_black ({self.fits_stretch_black})"
                )
        elif self.fits_stretch_black is not None or self.fits_stretch_white is not None:
            raise ValueError(
                f"survey {self.id}: fits_stretch_black/white are meaningless without fits_stretch"
            )
        return self

    @property
    def bundled(self) -> bool:
        return self.local_dir is not None

    @property
    def upstream_host(self) -> str:
        if not self.upstream_base_url:
            return "(bundled)"
        return self.upstream_base_url.split("://", 1)[-1].split("/", 1)[0]


#: Legacy Surveys DR11 as a full-sky **background** layer — genuinely a second thing
#: from the existing ``LEGACY_SURVEYS_CUTOUT`` per-target cutout source below: this is
#: the same archive's own Web Mercator tile pyramid (``mercator.py``), proxied and
#: cached exactly like a survey's own tile pyramid, not a cutout. (Pan-STARRS DR1 used
#: to be the comparison point here — WWT's "PanSTARRS1 3pi" TOAST imageset, STScI/MAST
#: — but it was removed 2026-09-08: the product owner's own call, "not usable for our
#: purpose", and its STScI redistribution question was never answered either way; see
#: ``THIRD_PARTY_NOTICES.md`` "Sky-survey layers" for the closed-out history.)
#:
#: **Single unified layer, no hemisphere split** — verified live 2026-09-07 against
#: three id/generation candidates:
#:
#: * ``ls-dr9`` and ``ls-dr10`` (the ids the existing cutout-path ``legacy_layer()``
#:   splits on at Dec +32.375°) both answered ``500`` for tiles whose row touches the
#:   pole-side half of the sky at low zoom (``z=1, y=0``) — a real server-side error, not
#:   a coverage gap (``curl`` returned an HTML "Server Error (500)" body with no reason).
#: * ``ls-dr11`` answered ``200`` with a real JPEG at every zoom/row/column combination
#:   tried (z=0 through z=4 including every corner, plus z=16 near a target field), for
#:   *both* halves of the sky that made ``ls-dr9``/``ls-dr10`` fail.
#: * The live viewer's own inline config (``legacysurvey.org/viewer/``, read 2026-09-07)
#:   confirms why: ``layer_initial = 'ls-dr11'`` and its ``tile_layers['ls-dr11']`` entry
#:   (``"Legacy Surveys DR11 images"``, zoom range ``[0, 16]``) is listed *separately*
#:   from ``ls-dr11-north``/``ls-dr11-south`` — i.e. DR11 already ships the combined,
#:   already-stitched mosaic as one servable id. DR11's own description page
#:   (``legacysurvey.org/dr11/description/``) confirms the north region
#:   (``region=<north>``, Dec > 32.375°) is DR9's existing reductions carried forward
#:   unchanged, while the new DR11 imaging extends the *southern* footprint — DR11 is the
#:   release where the historical DR9/DR10 hemisphere split stopped mattering client-side.
#:
#: Tile scheme confirmed live against the server's own published source
#: (``github.com/legacysurvey/decals-web``, ``map/utils.py``): standard spherical Web
#: Mercator, 256px tiles, ``2^zoom`` grid — exactly what WWT's own ``ProjectionType.mercator``
#: draws, with the same tile-row (``y``) direction (worked out from the engine's own
#: ``MercatorTile`` arithmetic — see ``mercator.py``'s module docstring). No reprojection:
#: this is a direct proxy and cache, the same shape every other fetch-through survey in
#: this registry uses.
#:
#: ``ver=1`` and ``maxZoom=16`` are both read from the live viewer's own config
#: (2026-09-07); a JPEG fetch through this exact base URL at ``z=1..16`` succeeded.
#: Credit and licence: CC BY 4.0, "Legacy Surveys / D. Lang (Perimeter Institute)" —
#: the same grant and credit string already cleared for ``LEGACY_SURVEYS_CUTOUT`` below
#: and quoted in ``THIRD_PARTY_NOTICES.md`` ("Sky-survey layers", "unWISE resolved"); this
#: entry extends that clearance from the cutout endpoint to the background-tile endpoint
#: on the same host, not a new clearance.
#:
#: ``bundle_allowed=True`` since the commercial-licensing audit of 2026-09-08, which
#: corrected this entry: it previously read "STScI-style silence on redistribution is
#: the same posture as Pan-STARRS ... never bundled", which was simply wrong. There is
#: no silence to interpret here — ``legacysurvey.org/acknowledgment/`` grants CC BY 4.0
#: explicitly, "Images are licensed under a Creative Commons Attribution 4.0
#: International License, and hence may on a non-exclusive basis be reproduced without
#: fee provided the credit is clear and visible" (re-fetched 2026-09-08). CC BY 4.0
#: permits commercial redistribution outright, so the module rule above ("nothing is
#: bundled unless the licence explicitly permits redistribution") is *satisfied*, not
#: waived. Pan-STARRS is the genuinely unresolved one; this never was.
#:
#: The flag is a statement about the licence, not a promise that a pack ships: shipping
#: one additionally needs ``local_dir``, which the validator restricts to HiPS, and this
#: is a Mercator pyramid. Until that exists the offline route is unchanged (the user's
#: own prefetch into their own cache), and the UI keys off ``bundled``, not this flag.
LEGACY_SURVEYS: Final = Survey(
    id="legacy-surveys-dr11",
    label="Legacy Surveys DR11 (grz)",
    kind=SurveyKind.OPTICAL,
    scheme=TileScheme.MERCATOR,
    upstream_base_url="https://www.legacysurvey.org/viewer/ls-dr11/1",
    tile_format="jpg",
    max_order=mercator.MAX_ZOOM,
    credit_label="Legacy Surveys / D. Lang (Perimeter Institute)",
    credit_url="https://www.legacysurvey.org/",
    bundle_allowed=True,
    redistributable_pack_url=None,
)

#: The user-facing sentence for a survey that reaches the map only as a
#: target-field cutout, never as a full-sky background layer — no
#: implementation detail (`/api/skymap/cutouts`, `THIRD_PARTY_NOTICES.md`
#: section names) in it; that belongs in a code comment, not the UI
#: (deliverable 3, 2026-09-07). The reason *why* is still exactly
#: `THIRD_PARTY_NOTICES.md` "Sky-survey layers": the only ready full-sky HiPS is
#: CDS-built and unclonable, so WISE/SDSS imagery is only ever available
#: from their own archives as a per-target cutout.
_CUTOUTS_NOT_TILES: Final = "Available as a target-field image below, not a full-sky background."

#: A real background tile layer, not a cutout-only placeholder — amended
#: 2026-09-07, the same day ``LEGACY_SURVEYS`` above was added: verified
#: live that ``legacysurvey.org/viewer``'s ``unwise-neo7`` id answers the
#: same Web Mercator XYZ endpoint DR11 does
#: (``https://c.legacysurvey.org/viewer/unwise-neo7/1/{z}/{x}/{y}.jpg``,
#: confirmed a real 256x256 JPEG at zoom 1). The per-target cutout source
#: (``UNWISE_CUTOUT`` below, ``CUTOUT_REGISTRY``) stays — framing a target
#: still wants one high-resolution, padded-for-context fetch, not
#: whatever a pannable background happens to have cached — but there is no
#: reason panning the map should be limited to Pan-STARRS/Legacy Surveys
#: DR11 alone when this same host already serves unWISE the identical way.
#: Same licence already cleared for the cutout source (CC BY 4.0, credit
#: below) extends to background use, the same reasoning ``LEGACY_SURVEYS``
#: already relies on for its own cutout-to-background extension — including
#: ``bundle_allowed``: the acknowledgement page's CC BY 4.0 grant names
#: unWISE explicitly and alongside the Legacy Surveys layers, so the
#: redistribution reasoning recorded on that entry applies here verbatim
#: (commercial-licensing audit, 2026-09-08).
#: Removed from ``REGISTRY`` 2026-09-09 when NSNS composite became the main
#: survey, then deliberately restored on the isolated full-sky survey POC
#: branch (): Legacy Surveys omits the Galactic plane, while this
#: existing CC-BY layer supplies all-sky infrared/dust context. It remains a
#: selectable alternative, not the default, and the matching cutout source
#: below remains independently useful for a resolved target field.
UNWISE: Final = Survey(
    id="unwise",
    label="unWISE (infrared)",
    kind=SurveyKind.INFRARED,
    scheme=TileScheme.MERCATOR,
    upstream_base_url="https://www.legacysurvey.org/viewer/unwise-neo7/1",
    tile_format="jpg",
    max_order=mercator.MAX_ZOOM,
    credit_label="unWISE / NASA/JPL-Caltech / D. Lang (Perimeter Institute)",
    credit_url="https://www.legacysurvey.org/acknowledgment/",
    bundle_allowed=True,
)

#: Remote personal-use layer. Its pixels are never bundled and it must not be
#: presented as commercially cleared. The UI requires an acknowledgement
#: before first use; only ordinary view-cache requests initiated by the user's
#: own installation remain now that every batch-prefetch API is removed.
DSS_COLOR_REMOTE_POC: Final = Survey(
    id="dss2-color-hips-poc",
    label="DSS2 colour (personal use)",
    kind=SurveyKind.OPTICAL,
    scheme=TileScheme.HIPS,
    upstream_base_url="https://alasky.cds.unistra.fr/DSS/DSSColor",
    tile_format="jpg",
    max_order=9,
    credit_label="Digitized Sky Survey — STScI/NASA; coloured and HEALPixed by CDS",
    credit_url="https://archive.stsci.edu/publishing/data-use",
    bundle_allowed=False,
    production_cleared=False,
    rights_notice=(
        "Personal, non-commercial use under the DSS data-use policy. Commercial-use "
        "permission remains unresolved. No DSS pixels are bundled."
    ),
)

#: Not a tile layer. WISE imagery reaches the map as per-target field cutouts
#: from IRSA's own services; this entry exists so the survey list says so
#: rather than omitting the layer (architecture policy invariant 7). No tile is ever
#: requested from ``upstream_base_url`` while ``enabled=False``.
WISE: Final = Survey(
    id="wise",
    label="AllWISE (infrared)",
    kind=SurveyKind.INFRARED,
    scheme=TileScheme.HIPS,
    upstream_base_url="https://irsa.ipac.caltech.edu",
    tile_format="png",
    max_order=0,
    credit_label="AllWISE: NASA/JPL-Caltech/UCLA — NASA/IPAC Infrared Science Archive",
    credit_url="https://irsa.ipac.caltech.edu/",
    bundle_allowed=False,
    enabled=False,
    pending_clearance=_CUTOUTS_NOT_TILES,
)

#: Not a tile layer either — per-target cutouts from SDSS's own SkyServer.
SDSS: Final = Survey(
    id="sdss",
    label="SDSS (color)",
    kind=SurveyKind.OPTICAL,
    scheme=TileScheme.HIPS,
    upstream_base_url="https://skyserver.sdss.org",
    tile_format="jpg",
    max_order=0,
    credit_label="Sloan Digital Sky Survey",
    credit_url="https://www.sdss.org",
    bundle_allowed=False,
    enabled=False,
    pending_clearance=_CUTOUTS_NOT_TILES,
)

#: H-alpha from WHAM alone (the Finkbeiner composite includes SHASSA, which
#: forbids commercial use — FAIL). WHAM's terms permit redistribution, so this
#: is the one survey shipped *with* the product: a HiPS directory generated
#: at build time under ``data/hips/wham/``. No network is ever involved; the
#: tile endpoint reads the directory. Enabled only while that directory
#: exists (``SkymapService.startup`` checks; the reason says so otherwise).
WHAM_HALPHA: Final = Survey(
    id="wham-halpha",
    label="H-alpha (WHAM)",
    kind=SurveyKind.HALPHA,
    scheme=TileScheme.HIPS,
    upstream_base_url="",
    local_dir=Path("wham"),
    tile_format="png",
    #: Orders 0..1: the source is 0.25 deg/px and the order-1 tile pixel (3.4')
    #: is already under a quarter of that (scripts/build-wham-hips.py, "Depth").
    max_order=1,
    credit_label="WHAM — Wisconsin H-Alpha Mapper (NSF)",
    credit_url="https://www.astro.wisc.edu/research/research-areas/galactic-astronomy/wham/",
    bundle_allowed=True,
    blend_only=True,
)

#: The reason a bundled survey reports when its directory is absent.
BUNDLED_PACK_MISSING: Final = "bundled H-alpha pack missing"

#: Shared credit line for every Northern Sky Narrowband Survey (NSNS) entry
#: below — added 2026-09-09 when SSC briefly used a GPL distribution and CC
#: BY-NC-SA data became usable (`THIRD_PARTY_NOTICES.md` requirement 5;
#: gate reading by `compliance-auditor`, 2026-09-08). CC BY-NC-SA 4.0
#: requires identifying the licence itself, not just the author, so both are
#: named here rather than folded into a generic "credit" the way CC BY
#: entries above are; the WHAM acknowledgement is included because NSNS's
#: own ``obs_ack`` names it explicitly for background-correction/calibration
#: of the H-alpha plane the whole survey (H-alpha alone, and the ``ohs8`` composite
#: that includes it) is built from. Quoted from the live HiPS ``properties``
#: files, ``http://www.simg.de/nebulae3/dr0_2/{halpha,oiii,sii,ohs8}/properties``
#: (verified reachable 2026-09-09 — see ``NSNS_*`` entries below):
#: ``obs_copyright = "Northern Sky Narrowband Survey by Stefan Ziegenbalg.
#: The material can be freely used and distributed under Creative Commons
#: Attribution-Noncommercial-Share Alike 4.0 license (CC-BY-NC-SA)."``
#: DOI from the task brief: 10.3847/2515-5172/adfec7.
_NSNS_CREDIT_LABEL: Final = (
    "Northern Sky Narrowband Survey / Stefan Ziegenbalg — "
    "CC BY-NC-SA 4.0 (non-commercial; H-alpha background-corrected and "
    "calibrated using WHAM, Wisconsin H-Alpha Mapper, NSF); "
    "doi:10.3847/2515-5172/adfec7"
)
#: The user-facing credit link, https (unlike ``_NSNS_BASE`` below, which is
#: deliberately the archive's own plain-http address — this is the one a
#: person clicks, and it is the same host's own redirect target).
_NSNS_CREDIT_URL: Final = "https://www.simg.de/nebulae3/dr0_2"
#: The archive's own directory root for one DR0.2 layer, plain HTTP as
#: `THIRD_PARTY_NOTICES.md` requires (§9: the originating archive, never a CDS
#: mirror) — the host 301-redirects every request to HTTPS, which
#: ``SkymapService``'s fetch client follows (``follow_redirects=True``), so
#: this is the address actually reached, not merely the address on file.
_NSNS_BASE: Final = "http://www.simg.de/nebulae3/dr0_2"
#: Every DR0.2 layer publishes ``hips_order = 6`` in its own ``properties``
#: file (all four checked live 2026-09-09) — a survey-wide constant, not
#: per-band coincidence, since all four were generated from the same 5"
#: intermediate mosaic (``prov_progenitor``).
_NSNS_MAX_ORDER: Final = 6
#: The published grant is CC BY-NC-SA 4.0. Keep the implementation available
#: for a future licensed build, but never enable it in a commercial release
#: until the rights holder's written commercial permission is recorded.
_NSNS_COMMERCIAL_CLEARANCE_REQUIRED: Final = (
    "Unavailable in this commercial build: written commercial-use permission "
    "from the Northern Sky Narrowband Survey rights holder is required."
)

#: Fixed black/white points (Rayleighs) for ``asinh_stretch_reference``, one
#: pair per band — see ``Survey.fits_stretch_black``'s docstring for how
#: these were derived (16 real order-6 tiles per band, sampled live
#: 2026-09-09 from bright, faint and near-pole fields) and why they are a
#: registry constant rather than computed at runtime.
_NSNS_HALPHA_BLACK: Final = 0.0
_NSNS_HALPHA_WHITE: Final = 1000.0
_NSNS_OIII_BLACK: Final = 0.0
_NSNS_OIII_WHITE: Final = 300.0
_NSNS_SII_BLACK: Final = 0.0
_NSNS_SII_WHITE: Final = 250.0

#: Raw calibrated H-alpha (without continuum), linear intensity in Rayleighs —
#: fetched as FITS, converted to a display PNG at request time
#: (``Survey.fits_stretch``; see its docstring for why the browser cannot
#: draw the archive's own bytes directly). ``blend_only``: like
#: ``WHAM_HALPHA`` this is a single emission line meant to be laid over a
#: broadband background for framing/planning a narrowband filter, not
#: chosen as the background itself (the task brief: "the individual bands
#: are what make it useful for planning a filter").
#:
#: Tile addressing verified against the live archive, not by
#: self-consistency (the two same-day bugs this note exists to not repeat,
#: `f1bda71`/`c9ee492`): North America Nebula, NGC 7000 (20h58m47s
#: +44°31') resolves via this repo's own ``hips.ang2pix`` at order 6 to
#: npix 15363, path ``Norder6/Dir10000/Npix15363.fits`` — fetched live
#: 2026-09-09 from ``http://www.simg.de/nebulae3/dr0_2/halpha/...`` (301 to
#: the same path under ``https://``) and confirmed by ``file``: "FITS image
#: data, 32-bit, floating point, single precision, 2 axes, 512 x 512" — the
#: right shape and BITPIX for a ``hips_tile_width=512``, ``BITPIX=-32``
#: layer, at the tile the target's own coordinates address.
NSNS_HALPHA: Final = Survey(
    id="nsns-halpha",
    label="NSNS H-alpha (narrowband)",
    kind=SurveyKind.HALPHA,
    scheme=TileScheme.HIPS,
    upstream_base_url=f"{_NSNS_BASE}/halpha",
    tile_format="png",
    fits_stretch=True,
    fits_stretch_black=_NSNS_HALPHA_BLACK,
    fits_stretch_white=_NSNS_HALPHA_WHITE,
    max_order=_NSNS_MAX_ORDER,
    credit_label=_NSNS_CREDIT_LABEL,
    credit_url=_NSNS_CREDIT_URL,
    bundle_allowed=False,
    blend_only=True,
    enabled=False,
    pending_clearance=_NSNS_COMMERCIAL_CLEARANCE_REQUIRED,
)

#: [OIII] at 500.7nm, same DR0.2 pipeline and the same fetch-time FITS ->
#: PNG conversion as ``NSNS_HALPHA`` above. This is the survey the task
#: exists for: "NSNS is the only OIII survey that exists" (no other cleared
#: source in this registry carries an oxygen line at all). Address verified
#: live the same way as H-alpha, same target field, same order-6 npix 15363
#: (the three bands share one pointing/tiling — ``moc_sky_fraction`` is
#: identical across all four ``properties`` files): ``file`` on the fetched
#: tile again read "FITS image data, 32-bit, floating point ... 512 x 512".
NSNS_OIII: Final = Survey(
    id="nsns-oiii",
    label="NSNS [OIII] (narrowband)",
    kind=SurveyKind.OIII,
    scheme=TileScheme.HIPS,
    upstream_base_url=f"{_NSNS_BASE}/oiii",
    tile_format="png",
    fits_stretch=True,
    fits_stretch_black=_NSNS_OIII_BLACK,
    fits_stretch_white=_NSNS_OIII_WHITE,
    max_order=_NSNS_MAX_ORDER,
    credit_label=_NSNS_CREDIT_LABEL,
    credit_url=_NSNS_CREDIT_URL,
    bundle_allowed=False,
    blend_only=True,
    enabled=False,
    pending_clearance=_NSNS_COMMERCIAL_CLEARANCE_REQUIRED,
)

#: [SII] at 671.7/673.0nm — same pipeline, same conversion, same live
#: verification as the two entries above (npix 15363 at order 6 for the
#: North America Nebula field, confirmed a real 512x512 float32 FITS tile).
NSNS_SII: Final = Survey(
    id="nsns-sii",
    label="NSNS [SII] (narrowband)",
    kind=SurveyKind.SII,
    scheme=TileScheme.HIPS,
    upstream_base_url=f"{_NSNS_BASE}/sii",
    tile_format="png",
    fits_stretch=True,
    fits_stretch_black=_NSNS_SII_BLACK,
    fits_stretch_white=_NSNS_SII_WHITE,
    max_order=_NSNS_MAX_ORDER,
    credit_label=_NSNS_CREDIT_LABEL,
    credit_url=_NSNS_CREDIT_URL,
    bundle_allowed=False,
    blend_only=True,
    enabled=False,
    pending_clearance=_NSNS_COMMERCIAL_CLEARANCE_REQUIRED,
)

#: [OIII]->red, H-alpha->green, [SII]->blue, already composited and encoded as an
#: 8-bit RGBA PNG by the archive itself (``hips_tile_format = png`` in its
#: own ``properties`` — unlike the three single-band layers above, nothing
#: here needs ``fits_stretch``: the tile fetched live 2026-09-09 at order 6
#: npix 15363 was read by ``file`` as "PNG image data, 512 x 512, 8-bit/color
#: RGBA, non-interlaced", the same address the FITS bands resolve to for the
#: same field). This is the layer that directly answers the task brief's
#: quoted requirement ("shows dust, hydrogen and oxygen as well") in one
#: image, so — unlike the three bands above — it is a real selectable
#: **background**, not ``blend_only``: ``kind=OPTICAL`` so it falls inside
#: `LayersPanel`'s existing background filter (`kind === "optical" ||
#: kind === "infrared"`) with no frontend change needed to pick it. Still
#: never the *default* (invariant 10): coverage is Dec > -16° only, not
#: all-sky, so unWISE stays first.
NSNS_OHS8: Final = Survey(
    id="nsns-ohs8",
    label="NSNS OIII/H-alpha/SII (composite)",
    kind=SurveyKind.OPTICAL,
    scheme=TileScheme.HIPS,
    upstream_base_url=f"{_NSNS_BASE}/ohs8",
    tile_format="png",
    max_order=_NSNS_MAX_ORDER,
    credit_label=_NSNS_CREDIT_LABEL,
    credit_url=_NSNS_CREDIT_URL,
    bundle_allowed=False,
    enabled=False,
    pending_clearance=_NSNS_COMMERCIAL_CLEARANCE_REQUIRED,
)


# -- target-field cutouts (ssc/skymap/cutouts.py) --------------------------------- #
#
# A cutout source is not a tile pyramid: there is no ``scheme``, no
# ``max_order``, nothing to prefetch as a pack. It is registered here anyway,
# alongside the tile ``Survey`` registry, because it is exactly the same kind
# of fact — what may be drawn, from where, with which credit — and the
# ``THIRD_PARTY_NOTICES.md`` "Endpoint gate, 2026-09-07" subsection is what fixed
# both. Credit strings below are quoted from that subsection verbatim, which
# for AllWISE differs slightly in punctuation from the disabled tile-registry
# entry above (that one predates the endpoint gate reading).


class CutoutSourceId(StrEnum):
    UNWISE = "unwise"
    WISE = "wise"
    SDSS = "sdss"
    LEGACY_SURVEYS = "legacy-surveys"


class CutoutSource(BaseModel):
    """One archive a target-field cutout may come from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CutoutSourceId
    label: str
    #: The archive host(s) whose requests share one per-host semaphore of 1
    #: (THIRD_PARTY_NOTICES.md endpoint-gate cross-cutting rule). Listed, not
    #: derived, because a source may reach more than one host (SDSS's SQL
    #: lookup and its image servers are different hosts).
    upstream_hosts: tuple[str, ...]
    credit_label: str
    credit_url: str
    #: Bands this source offers, when it has more than one; ``()`` for a
    #: source with no band concept (SDSS's colour quick-look).
    bands: tuple[str, ...] = ()
    #: The bands fetched when a request does not name any.
    default_bands: tuple[str, ...] = ()
    #: An optional layer: on only when the field is confirmed in its
    #: footprint, and never an error when it is not (Legacy Surveys).
    optional: bool = False
    #: A hard pixel cap this source's cutout endpoint enforces server-side;
    #: SSC must clamp to it rather than let upstream reject the request.
    max_pixels: int = 3000
    #: Whether ``ssc.skymap.bulk_prefetch`` may queue this source across many
    #: catalogue objects at once. This is separate from a single interactive
    #: field request because upstream services can impose different bulk-use
    #: and rate-limit terms.
    #: This is a real guard, not a comment: ``bulk_prefetch._sources``
    #: refuses a request naming a source with this set to ``False``.
    bulk_prefetch_allowed: bool = True
    #: Whether ``CutoutService.schedule_autofetch`` may fetch this source
    #: automatically on target resolve, with no click. The bar is narrower
    #: than ``bulk_prefetch_allowed`` and orthogonal to it: a single
    #: bounded per-target request that satisfies this source's own terms —
    #: never a source whose cutout logic tiles a footprint into many
    #: requests to cover one field (AllWISE's own ``_fetch_wise_incremental``
    #: can span "tens of tiles wide" per its own module docstring — a sweep,
    #: not a single fetch, however small the requested field), and never a
    #: source that is bundled or default-only. ``False`` by default —
    #: opt-in per source, mirroring ``bulk_prefetch_allowed``'s own default
    #: direction reversed: most sources are fine to bulk-queue but only a
    #: few are safe to fetch with no human in the loop at all. This is a
    #: real guard, not a comment: ``CutoutService.schedule_autofetch``
    #: refuses a ``source_id`` naming a source with this set to ``False``.
    autofetch_allowed: bool = False


#: CC BY 4.0 via Legacy Surveys' own `unwise-neo7` layer, not `unwise.me`
#: (still CC BY-NC-SA, never used) -- `legacysurvey.org/acknowledgment/`
#: names unWISE explicitly under the same grant as Legacy Surveys' own
#: layers, with its own required credit, distinct from "Legacy Surveys / D.
#: Lang (Perimeter Institute)" (THIRD_PARTY_NOTICES.md "unWISE resolved,
#: 2026-09-07"). Genuinely all-sky -- verified live at five widely spread
#: coordinates including the deep southern sky -- so it is listed first:
#: the one cutout source that reliably has something everywhere.
UNWISE_CUTOUT: Final = CutoutSource(
    id=CutoutSourceId.UNWISE,
    label="unWISE (infrared)",
    upstream_hosts=("www.legacysurvey.org",),
    credit_label="unWISE / NASA/JPL-Caltech / D. Lang (Perimeter Institute)",
    credit_url="https://www.legacysurvey.org/acknowledgment/",
    #: No band concept: the endpoint ignores `bands` for this layer and
    #: always returns its own fixed 3-plane composite (verified live).
    optional=False,
    max_pixels=3000,
    #: `_fetch_unwise` tiles only above `UNWISE_TILE_SIZE_DEG` (3 deg); at
    #: `AUTOFETCH_SIZE_DEG` (<= that) it is always the single untiled
    #: request `autofetch_allowed` requires — the previous (2026-09-09)
    #: autofetch default, kept eligible as the CC BY, genuinely all-sky
    #: commercially cleared all-sky fallback.
    autofetch_allowed=True,
)

WISE_CUTOUT: Final = CutoutSource(
    id=CutoutSourceId.WISE,
    label="AllWISE (infrared)",
    upstream_hosts=("irsa.ipac.caltech.edu",),
    credit_label="AllWISE: NASA/JPL-Caltech/UCLA · NASA/IPAC IRSA",
    credit_url="https://irsa.ipac.caltech.edu/",
    bands=("W1", "W2", "W3", "W4"),
    #: W3 (12 micron) shows nebulae best; W1 alongside it by default (M11 spec).
    default_bands=("W3", "W1"),
)

SDSS_CUTOUT: Final = CutoutSource(
    id=CutoutSourceId.SDSS,
    label="SDSS (color)",
    upstream_hosts=("skyserver.sdss.org", "data.sdss.org"),
    credit_label="Sloan Digital Sky Survey",
    credit_url="https://www.sdss.org",
    #: SAS DR17 frames, when the field lookup succeeds; the getjpeg
    #: quick-look is fetched independently of ``bands`` and is never the only
    #: attempt when a field is found (THIRD_PARTY_NOTICES.md endpoint gate).
    bands=("g", "r", "i"),
    default_bands=("g", "r", "i"),
    max_pixels=2048,
)

LEGACY_SURVEYS_CUTOUT: Final = CutoutSource(
    id=CutoutSourceId.LEGACY_SURVEYS,
    label="Legacy Surveys",
    upstream_hosts=("www.legacysurvey.org",),
    credit_label="Legacy Surveys / D. Lang (Perimeter Institute)",
    credit_url="https://www.legacysurvey.org/",
    bands=("g", "r", "z"),
    default_bands=("g", "r", "z"),
    optional=True,
    max_pixels=3000,
)

#: unWISE leads: the one cutout source verified genuinely all-sky, so it is
#: the one presented/suggested first.
CUTOUT_REGISTRY: Final[tuple[CutoutSource, ...]] = (
    UNWISE_CUTOUT,
    WISE_CUTOUT,
    SDSS_CUTOUT,
    LEGACY_SURVEYS_CUTOUT,
)

_CUTOUTS_BY_ID: Final[dict[str, CutoutSource]] = {source.id: source for source in CUTOUT_REGISTRY}


def find_cutout_source(source_id: str) -> CutoutSource | None:
    return _CUTOUTS_BY_ID.get(source_id)


#: Dec above which Legacy Surveys serves ``ls-dr9`` rather than ``ls-dr10``
#: (THIRD_PARTY_NOTICES.md endpoint gate, Legacy Surveys row).
LEGACY_DR9_NORTH_OF_DEC: Final = 32.375

#: unWISE has no such hemisphere split — one release, all-sky (unlike
#: Legacy Surveys' own DR9/DR10 optical layers), which is exactly why it is
#: cleared as the default: it always has something to show.
UNWISE_LAYER: Final = "unwise-neo7"


def legacy_layer(dec_deg: float) -> str:
    return "ls-dr9" if dec_deg > LEGACY_DR9_NORTH_OF_DEC else "ls-dr10"


REGISTRY: Final[tuple[Survey, ...]] = (
    DSS_COLOR_REMOTE_POC,
    LEGACY_SURVEYS,
    UNWISE,
    WHAM_HALPHA,
    NSNS_OHS8,
    NSNS_HALPHA,
    NSNS_OIII,
    NSNS_SII,
    WISE,
    SDSS,
)

_BY_ID: Final[dict[str, Survey]] = {survey.id: survey for survey in REGISTRY}


def find_survey(survey_id: str) -> Survey | None:
    return _BY_ID.get(survey_id)


def tile_count(survey: Survey, order: int) -> int:
    """Tiles at exactly ``order`` for this survey's scheme."""
    if survey.scheme is TileScheme.HIPS:
        return hips.tiles_in_order(order)
    if survey.scheme is TileScheme.MERCATOR:
        return mercator.tiles_in_zoom(order)
    return toast.tiles_in_level(order)


def upstream_tile_url(survey: Survey, order: int, npix: int, ext: str) -> str:
    """Where the originating archive serves one tile. Not for bundled surveys."""
    if survey.local_dir is not None:
        raise ValueError(f"survey {survey.id} is bundled; it has no upstream")
    if survey.scheme is TileScheme.HIPS:
        return f"{survey.upstream_base_url}/{hips.tile_path(order, npix, ext)}"
    if survey.scheme is TileScheme.MERCATOR:
        x, y = mercator.unpack(order, npix)
        return f"{survey.upstream_base_url}/{mercator.tile_path(order, x, y, ext)}"
    x, y = toast.unpack(order, npix)
    return f"{survey.upstream_base_url}/{toast.tile_path(order, x, y, ext)}"


def local_tile_path(survey: Survey, order: int, npix: int, ext: str) -> str:
    """The tile's path under ``data_dir/tiles/<survey>/``; mirrors upstream so a
    cache directory is itself a valid pyramid root."""
    if survey.scheme is TileScheme.HIPS:
        return hips.tile_path(order, npix, ext)
    if survey.scheme is TileScheme.MERCATOR:
        x, y = mercator.unpack(order, npix)
        return f"mercator/{mercator.tile_path(order, x, y, ext)}"
    x, y = toast.unpack(order, npix)
    return f"toast/{toast.tile_path(order, x, y, ext)}"


def client_tile_template(survey: Survey) -> str:
    """The same-origin URL a map frame requests tiles from, with the
    placeholders of the survey's scheme left for the engine to fill.

    HiPS uses the standard ``Norder{order}/Dir{dir}/Npix{npix}`` layout under
    ``/api/skymap/tiles/<id>/`` so that root *is* a HiPS root — the same
    directory the ``properties`` file sits in, which is how a HiPS client
    (WWT's ``HipsProperties`` included) finds it from a tile URL.
    """
    root = f"/api/skymap/tiles/{survey.id}"
    if survey.scheme is TileScheme.HIPS:
        return f"{root}/Norder{{order}}/Dir{{dir}}/Npix{{npix}}.{survey.tile_format}"
    if survey.scheme is TileScheme.MERCATOR:
        return f"{root}/mercator/{{level}}/{{x}}/{{y}}.{survey.tile_format}"
    return f"{root}/toast/{{level}}/{{x}}/{{y}}.{survey.tile_format}"


__all__ = [
    "BUNDLED_PACK_MISSING",
    "CUTOUT_REGISTRY",
    "DSS_COLOR_REMOTE_POC",
    "LEGACY_DR9_NORTH_OF_DEC",
    "LEGACY_SURVEYS",
    "LEGACY_SURVEYS_CUTOUT",
    "NSNS_HALPHA",
    "NSNS_OHS8",
    "NSNS_OIII",
    "NSNS_SII",
    "REGISTRY",
    "SDSS",
    "SDSS_CUTOUT",
    "TILE_MEDIA_TYPES",
    "UNWISE",
    "UNWISE_CUTOUT",
    "UNWISE_LAYER",
    "WHAM_HALPHA",
    "WISE",
    "WISE_CUTOUT",
    "CutoutSource",
    "CutoutSourceId",
    "Survey",
    "SurveyFrame",
    "SurveyKind",
    "TileScheme",
    "client_tile_template",
    "find_cutout_source",
    "find_survey",
    "legacy_layer",
    "local_tile_path",
    "tile_count",
    "upstream_tile_url",
]
