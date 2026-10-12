"""Target-field cutouts against a real ``CutoutService``: cache keying, the
durable-before-acted job row, per-source fetch logic (upstream mocked with
respx), the display PNG, LRU eviction and its plan-target protection."""

from __future__ import annotations

import asyncio
import bz2
import gzip
import json
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx

from ssc.astro.mosaic import sky_tile_centers_deg
from ssc.plan import AsapTimeSpec, GotoAndStackItem, Plan, ShutdownItem
from ssc.skymap.cutouts import (
    AUTOFETCH_SIZE_DEG,
    AUTOFETCH_SOURCE_ID,
    UNWISE_TILE_SIZE_DEG,
    WISE_BAND_WAVELENGTH_M,
    CutoutNotFoundError,
    CutoutRefusedError,
    CutoutService,
    cutout_cache_key,
    release_tag,
)
from ssc.skymap.surveys import (
    UNWISE_CUTOUT,
    UNWISE_LAYER,
    CutoutSource,
    find_cutout_source,
)
from ssc.state.models import CutoutJobStatus, StateSnapshot
from ssc.state.store import StateStore
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate
from ssc.store.plans import PlanRepository
from tests.fixtures.skymap.build import build_fits_bytes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "skymap"
_WCS = {
    "CRPIX1": 4.0,
    "CRPIX2": 4.0,
    "CRVAL1": 10.6847,
    "CRVAL2": 41.269,
    "CD1_1": -0.0002777,
    "CD1_2": 0.0,
    "CD2_1": 0.0,
    "CD2_2": 0.0002777,
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
}


def _plane(value: float = 50.0) -> np.ndarray:
    return np.full((8, 8), value, dtype=np.float32)


def _cube() -> np.ndarray:
    return np.stack([_plane(10.0), _plane(20.0), _plane(30.0)])


def _votable(access_urls: list[str]) -> bytes:
    """A SIA v2 discovery response with exactly the science/image-fits rows
    ``CutoutService._wise_access_urls`` keeps — the shape it filters for
    among the weight/uncertainty FITS and auxiliary tables IRSA's real
    service also lists for the same Atlas tile (THIRD_PARTY_NOTICES.md "AllWISE
    cutouts" fix, verified live 2026-09-07)."""
    rows = "".join(
        f"<TR><TD>{url}</TD><TD>science</TD><TD>image/fits</TD></TR>" for url in access_urls
    )
    return (
        '<VOTABLE version="1.3" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">'
        "<RESOURCE><TABLE>"
        '<FIELD name="access_url" datatype="char"/>'
        '<FIELD name="dataproduct_subtype" datatype="char"/>'
        '<FIELD name="access_format" datatype="char"/>'
        f"<DATA><TABLEDATA>{rows}</TABLEDATA></DATA>"
        "</TABLE></RESOURCE></VOTABLE>"
    ).encode()


def _votable_positioned(rows: list[tuple[str, float, float]]) -> bytes:
    """Like :func:`_votable`, but with each row's own ``s_ra``/``s_dec`` —
    the standard SIA v2 columns :func:`CutoutService._wise_access_urls` sorts
    candidate Atlas tiles by, nearest to the requested centre first."""
    trs = "".join(
        f"<TR><TD>{url}</TD><TD>science</TD><TD>image/fits</TD><TD>{ra}</TD><TD>{dec}</TD></TR>"
        for url, ra, dec in rows
    )
    return (
        '<VOTABLE version="1.3" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">'
        "<RESOURCE><TABLE>"
        '<FIELD name="access_url" datatype="char"/>'
        '<FIELD name="dataproduct_subtype" datatype="char"/>'
        '<FIELD name="access_format" datatype="char"/>'
        '<FIELD name="s_ra" datatype="double"/>'
        '<FIELD name="s_dec" datatype="double"/>'
        f"<DATA><TABLEDATA>{trs}</TABLEDATA></DATA>"
        "</TABLE></RESOURCE></VOTABLE>"
    ).encode()


def _sql_json(rows: list[dict[str, Any]]) -> bytes:
    return json.dumps([{"TableName": "Table1", "Rows": rows}]).encode("utf-8")


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    database = await open_database(tmp_path / "ssc.sqlite3")
    await migrate(database)
    plans = PlanRepository(database)
    service = CutoutService(
        database,
        tmp_path / "cutouts",
        max_bytes=10 * 1024 * 1024,
        user_agent=lambda: "SSC/test (+ops@example.org)",
        plans=plans,
    )
    await service.startup()
    state = StateStore(StateSnapshot(server_started_at=datetime.now(tz=UTC)))
    tasks: list[asyncio.Task[Any]] = []

    def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    published = asyncio.Event()

    async def on_change() -> None:
        published.set()

    service.attach(on_change, spawn)
    try:
        yield {
            "service": service,
            "state": state,
            "plans": plans,
            "database": database,
            "tasks": tasks,
        }
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.aclose()
        await database.aclose()


async def _run_and_wait(env: dict[str, Any], **kwargs: Any) -> Any:
    service: CutoutService = env["service"]
    state = await service.request(**kwargs)
    task = service._jobs[state.id]
    await task
    return await service.get_state(state.id)


def test_cache_key_rounds_geometry_to_two_decimals() -> None:
    a = cutout_cache_key("wise", "allwise", 10.68471, 41.26901, 0.5001)
    b = cutout_cache_key("wise", "allwise", 10.68499, 41.26899, 0.4999)
    assert a == b


async def test_startup_removes_legacy_restricted_cache_bytes(env: dict[str, Any]) -> None:
    service: CutoutService = env["service"]
    retired = service.root / "dss2-color"
    retired.mkdir(parents=True)
    (retired / "piece.jpg").write_bytes(b"legacy")

    await service.startup()

    assert not retired.exists()


def test_unwise_release_tag_is_one_all_sky_layer_regardless_of_declination() -> None:
    """Unlike Legacy Surveys' own DR9/DR10 split, unWISE has one release —
    `legacy_layer(dec_deg)` must never decide its tag."""
    assert release_tag(UNWISE_CUTOUT, 80.0) == UNWISE_LAYER
    assert release_tag(UNWISE_CUTOUT, -80.0) == UNWISE_LAYER
    assert UNWISE_LAYER == "unwise-neo7"


async def test_wise_fetches_atlas_tiles_and_stores_a_fits_piece_with_display_png(
    env: dict[str, Any],
) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(
                200, content=_votable(["https://irsa.ipac.caltech.edu/tile1"])
            )
        )
        cutout_bytes = build_fits_bytes(_plane(), _WCS)
        router.get("https://irsa.ipac.caltech.edu/tile1").mock(
            return_value=httpx.Response(200, content=gzip.compress(cutout_bytes))
        )
        result = await _run_and_wait(
            env, source_id="wise", ra_deg=10.6847, dec_deg=41.269, size_deg=0.5, bands=("W1",)
        )
    assert result.status is CutoutJobStatus.COMPLETE
    assert result.bytes > 0
    _, pieces = await env["service"].get_manifest(result.id)
    assert len(pieces) == 1
    assert pieces[0].band == "W1"
    assert pieces[0].format == "fits"
    assert pieces[0].wcs is not None
    assert pieces[0].display_png_path is not None
    data, media = await env["service"].get_piece_bytes(result.id, 0, "png")
    assert media == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


async def test_wise_off_footprint_is_unavailable_not_failed(env: dict[str, Any]) -> None:
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        result = await _run_and_wait(
            env, source_id="wise", ra_deg=0.0, dec_deg=0.0, size_deg=0.5, bands=("W1",)
        )
    assert result.status is CutoutJobStatus.UNAVAILABLE
    assert result.error is not None


async def test_wise_sia_query_uses_circle_pos_and_a_wavelength_band(env: dict[str, Any]) -> None:
    """Locks in the SIA v2 query shape (THIRD_PARTY_NOTICES.md "AllWISE cutouts"
    fix, verified live 2026-09-07): a bare "ra,dec" pair with no shape
    keyword and no radius, plus BAND as the bandpass name ("W3"), both threw
    `UsageFault: BAD_REQUEST` against the real service. POS must be
    `CIRCLE <ra> <dec> <radius>` (space-separated); BAND must be a
    wavelength in metres."""
    with respx.mock() as router:
        route = router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        await _run_and_wait(
            env, source_id="wise", ra_deg=10.6847, dec_deg=41.269, size_deg=0.5, bands=("W3",)
        )
    params = dict(route.calls[0].request.url.params)
    assert params["COLLECTION"] == "wise_allwise"
    assert params["POS"] == "CIRCLE 10.6847 41.269 0.25"
    assert params["BAND"] == str(WISE_BAND_WAVELENGTH_M["W3"])


async def test_wise_keeps_only_the_science_image_fits_row(env: dict[str, Any]) -> None:
    """IRSA's SIA response lists a weight FITS, an uncertainty FITS and
    several auxiliary tables alongside the calibrated intensity image for
    the very same Atlas tile (verified live) — only the
    ``dataproduct_subtype=science``, ``access_format=image/fits`` row is a
    cutout this module can read."""
    votable = (
        b'<VOTABLE version="1.3" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">'
        b"<RESOURCE><TABLE>"
        b'<FIELD name="access_url" datatype="char"/>'
        b'<FIELD name="dataproduct_subtype" datatype="char"/>'
        b'<FIELD name="access_format" datatype="char"/>'
        b"<DATA><TABLEDATA>"
        b"<TR><TD>https://irsa.ipac.caltech.edu/aux.tbl</TD><TD>auxiliary</TD>"
        b"<TD>text/plain</TD></TR>"
        b"<TR><TD>https://irsa.ipac.caltech.edu/weight.fits.gz</TD><TD>weight</TD>"
        b"<TD>application/x-gzip</TD></TR>"
        b"<TR><TD>https://irsa.ipac.caltech.edu/tile1</TD><TD>science</TD>"
        b"<TD>image/fits</TD></TR>"
        b"</TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>"
    )
    with respx.mock(assert_all_called=True) as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=votable)
        )
        cutout_bytes = build_fits_bytes(_plane(), _WCS)
        router.get("https://irsa.ipac.caltech.edu/tile1").mock(
            return_value=httpx.Response(200, content=gzip.compress(cutout_bytes))
        )
        # respx's `assert_all_called` would otherwise pass even if the
        # weight/auxiliary URLs were (wrongly) fetched too, since they are
        # not registered as routes at all — assert directly on call count.
        result = await _run_and_wait(
            env, source_id="wise", ra_deg=10.6847, dec_deg=41.269, size_deg=0.5, bands=("W1",)
        )
    assert result.status is CutoutJobStatus.COMPLETE
    _, pieces = await env["service"].get_manifest(result.id)
    assert len(pieces) == 1


async def test_wise_sorts_atlas_tiles_nearest_first_and_caps_the_count(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IRSA's own SIA response is not distance-ordered (verified live,
    2026-09-09: an unsorted 4-row slice for a real M31 request picked four
    Atlas tiles 2-3.7 deg from the requested centre and none covering it —
    a cutout of the wrong patch of sky, not the target). `s_ra`/`s_dec` must
    be read and used to sort nearest-first before truncating to
    ``WISE_MAX_ATLAS_TILES``, so a capped result is always the tiles that
    actually cover the target. The production cap (25, comfortably above a 6
    deg box's real ~21-tile need, 2026-09-09) is too large to exercise with a
    handful of fixture rows, so this pins a small cap instead of asserting
    against the real constant."""
    monkeypatch.setattr("ssc.skymap.cutouts.WISE_MAX_ATLAS_TILES", 4)
    target_ra, target_dec = 10.6847, 41.269
    near2 = "https://irsa.ipac.caltech.edu/near2"
    near1 = "https://irsa.ipac.caltech.edu/near1"
    mid2 = "https://irsa.ipac.caltech.edu/mid2"
    mid1 = "https://irsa.ipac.caltech.edu/mid1"
    far1 = "https://irsa.ipac.caltech.edu/far1"
    far2 = "https://irsa.ipac.caltech.edu/far2"
    candidates = [
        (far1, 5.0, target_dec),
        (near1, 10.6, 41.2),
        (mid1, 9.0, target_dec),
        (near2, 10.75, 41.3),
        (mid2, 9.5, 41.0),
        (far2, 4.0, target_dec),
    ]
    with respx.mock(assert_all_called=False) as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable_positioned(candidates))
        )
        cutout_bytes = build_fits_bytes(_plane(), _WCS)
        for url, _, _ in candidates:
            router.get(url).mock(
                return_value=httpx.Response(200, content=gzip.compress(cutout_bytes))
            )
        result = await _run_and_wait(
            env,
            source_id="wise",
            ra_deg=target_ra,
            dec_deg=target_dec,
            size_deg=0.5,
            bands=("W1",),
        )
        # Read while the mock router is still installed — its call log is
        # not guaranteed to survive past the `with` block.
        called = {str(call.request.url.copy_with(query=None, params=None)) for call in router.calls}
    assert result.status is CutoutJobStatus.COMPLETE
    called.discard("https://irsa.ipac.caltech.edu/SIA")
    # The four nearest, never the two genuinely distant ones.
    assert called == {near1, near2, mid1, mid2}
    assert far1 not in called
    assert far2 not in called


async def test_sdss_sas_frames_are_fetched_after_a_field_lookup(env: dict[str, Any]) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://skyserver.sdss.org/dr17/SkyServerWS/SearchTools/SqlSearch").mock(
            return_value=httpx.Response(
                200, content=_sql_json([{"run": 1, "rerun": 301, "camcol": 2, "field": 55}])
            )
        )
        frame_bytes = build_fits_bytes(_plane(), _WCS)
        router.get(
            "https://data.sdss.org/sas/dr17/eboss/photoObj/frames/301/1/2/"
            "frame-g-000001-2-0055.fits.bz2"
        ).mock(return_value=httpx.Response(200, content=bz2.compress(frame_bytes)))
        router.get(
            "https://data.sdss.org/sas/dr17/eboss/photoObj/frames/301/1/2/"
            "frame-r-000001-2-0055.fits.bz2"
        ).mock(return_value=httpx.Response(404))
        router.get(
            "https://data.sdss.org/sas/dr17/eboss/photoObj/frames/301/1/2/"
            "frame-i-000001-2-0055.fits.bz2"
        ).mock(return_value=httpx.Response(404))
        result = await _run_and_wait(
            env, source_id="sdss", ra_deg=10.6847, dec_deg=41.269, size_deg=0.3, bands=None
        )
    assert result.status is CutoutJobStatus.COMPLETE
    _, pieces = await env["service"].get_manifest(result.id)
    assert [p.band for p in pieces] == ["g"]


async def test_sdss_falls_back_to_getjpeg_when_no_field_is_found(env: dict[str, Any]) -> None:
    with respx.mock() as router:
        router.get("https://skyserver.sdss.org/dr17/SkyServerWS/SearchTools/SqlSearch").mock(
            return_value=httpx.Response(200, content=_sql_json([]))
        )
        router.get("https://skyserver.sdss.org/dr17/SkyServerWS/ImgCutout/getjpeg").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff" + b"\x00" * 32)
        )
        result = await _run_and_wait(
            env, source_id="sdss", ra_deg=200.0, dec_deg=-10.0, size_deg=0.2, bands=None
        )
    assert result.status is CutoutJobStatus.COMPLETE
    _, pieces = await env["service"].get_manifest(result.id)
    assert pieces[0].format == "jpg"
    assert pieces[0].display_png_path is None, "the jpg quick-look is already a display image"
    assert pieces[0].stretch is None


async def test_sdss_getjpeg_404_is_off_footprint(env: dict[str, Any]) -> None:
    with respx.mock() as router:
        router.get("https://skyserver.sdss.org/dr17/SkyServerWS/SearchTools/SqlSearch").mock(
            return_value=httpx.Response(200, content=_sql_json([]))
        )
        router.get("https://skyserver.sdss.org/dr17/SkyServerWS/ImgCutout/getjpeg").mock(
            return_value=httpx.Response(404)
        )
        result = await _run_and_wait(
            env, source_id="sdss", ra_deg=200.0, dec_deg=-10.0, size_deg=0.2, bands=None
        )
    assert result.status is CutoutJobStatus.UNAVAILABLE


async def test_legacy_surveys_stores_a_grz_cube_as_one_piece_with_rgb_display(
    env: dict[str, Any],
) -> None:
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/fits-cutout").mock(
            return_value=httpx.Response(200, content=build_fits_bytes(_cube(), _WCS))
        )
        result = await _run_and_wait(
            env, source_id="legacy-surveys", ra_deg=10.6847, dec_deg=10.0, size_deg=0.1, bands=None
        )
    assert result.status is CutoutJobStatus.COMPLETE
    _, pieces = await env["service"].get_manifest(result.id)
    assert len(pieces) == 1
    assert pieces[0].format == "fits"
    assert pieces[0].display_png_path is not None


async def test_legacy_surveys_uses_dr9_north_of_the_declination_line(env: dict[str, Any]) -> None:
    with respx.mock() as router:
        route = router.get("https://www.legacysurvey.org/viewer/fits-cutout").mock(
            return_value=httpx.Response(200, content=build_fits_bytes(_cube(), _WCS))
        )
        await _run_and_wait(
            env, source_id="legacy-surveys", ra_deg=10.0, dec_deg=50.0, size_deg=0.1, bands=None
        )
    assert "layer=ls-dr9" in str(route.calls[0].request.url)


async def test_legacy_surveys_falls_back_to_jpeg_cutout_on_a_5xx_fits_cutout(
    env: dict[str, Any],
) -> None:
    """`fits-cutout` (the grz cube) 500s reproducibly for some real fields
    while `jpeg-cutout` succeeds at the identical coordinates — confirmed
    live against two distinct targets, both DR9 and DR10 (2026-09-07). The
    fallback completes the request with a JPEG piece and a synthetic TAN
    WCS built from the request geometry, the same technique SDSS's own
    getjpeg fallback already uses."""
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/fits-cutout").mock(
            return_value=httpx.Response(500)
        )
        route = router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        result = await _run_and_wait(
            env, source_id="legacy-surveys", ra_deg=10.6847, dec_deg=10.0, size_deg=0.1, bands=None
        )
    assert result.status is CutoutJobStatus.COMPLETE
    assert route.called
    _, pieces = await env["service"].get_manifest(result.id)
    assert len(pieces) == 1
    assert pieces[0].format == "jpg"
    # A JPEG piece has no FitsImage to derive width/height from server-side,
    # so it carries a wcs but no server-rendered display PNG.
    assert pieces[0].display_png_path is None


async def test_legacy_surveys_jpeg_fallback_off_footprint_is_not_an_error(
    env: dict[str, Any],
) -> None:
    """If the FITS path 500s and the JPEG fallback then genuinely finds no
    coverage (404), that is still `unavailable`, never `failed` — an
    optional layer's off-footprint result is not an error at either step."""
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/fits-cutout").mock(
            return_value=httpx.Response(500)
        )
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(404)
        )
        result = await _run_and_wait(
            env, source_id="legacy-surveys", ra_deg=10.6847, dec_deg=10.0, size_deg=0.1, bands=None
        )
    assert result.status is CutoutJobStatus.UNAVAILABLE


async def test_unwise_fetches_the_jpeg_composite_directly(
    env: dict[str, Any],
) -> None:
    """unWISE reaches the map through Legacy Surveys' own `unwise-neo7`
    layer (THIRD_PARTY_NOTICES.md "unWISE resolved, 2026-09-07"), via
    `jpeg-cutout` directly rather than `fits-cutout` -- confirmed live
    (2026-09-07) that the FITS path's W1+W2 float cube is ~25x the bytes
    and ~6x the wall time of the JPEG composite for the same field, for a
    display that only ever used one of the cube's two planes anyway
    (`_fetch_unwise`'s own doc comment)."""
    with respx.mock() as router:
        route = router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        result = await _run_and_wait(
            env, source_id="unwise", ra_deg=10.6847, dec_deg=-60.0, size_deg=0.1, bands=None
        )
    assert "layer=unwise-neo7" in str(route.calls[0].request.url)
    # No `bands` value is meaningful for this layer (verified live: the
    # endpoint ignores it and always returns its own fixed composite) — it
    # is never sent, unlike Legacy Surveys' own `bands=grz`.
    assert "bands=" not in str(route.calls[0].request.url)
    assert result.status is CutoutJobStatus.COMPLETE
    _, pieces = await env["service"].get_manifest(result.id)
    assert len(pieces) == 1
    assert pieces[0].format == "jpg"
    assert pieces[0].display_png_path is None, "the jpg composite is already a display image"
    assert pieces[0].width is not None
    assert pieces[0].height is not None


async def test_unwise_tiles_a_wide_request_into_a_2x2_grid_of_3_deg_tiles(
    env: dict[str, Any],
) -> None:
    """A `size_deg=6` unWISE request -- above `UNWISE_TILE_SIZE_DEG` -- is
    four 3 deg `jpeg-cutout` requests, one per tile of `sky_tile_centers_deg`'s
    own 2x2 grid, each getting the same `UNWISE_CUTOUT.max_pixels` budget a
    single 3 deg request always got, rather than one 6 deg request sharing
    that budget across four times the footprint (reported live 2026-09-08)."""
    ra_deg, dec_deg = 10.6847, -60.0
    assert UNWISE_TILE_SIZE_DEG == 3.0
    expected_centers = [
        (float(ra), float(dec)) for ra, dec in sky_tile_centers_deg(ra_deg, dec_deg, 2, 3.0)
    ]
    seen_centers: list[tuple[float, float]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        seen_centers.append((float(params["ra"]), float(params["dec"])))
        marker = f"tile-{len(seen_centers)}".encode()
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0" + marker)

    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(side_effect=upstream)
        result = await _run_and_wait(
            env, source_id="unwise", ra_deg=ra_deg, dec_deg=dec_deg, size_deg=6.0, bands=None
        )
    assert result.status is CutoutJobStatus.COMPLETE
    # Exactly the four tile centres `sky_tile_centers_deg` itself computes --
    # this locks in that `_fetch_unwise` calls the shared astro helper rather
    # than re-deriving the grid.
    assert seen_centers == expected_centers
    _, pieces = await env["service"].get_manifest(result.id)
    assert len(pieces) == 4
    assert all(p.format == "jpg" for p in pieces)
    # Edge to edge, no gap or overlap: two distinct declinations exactly one
    # tile width (3 deg) apart; RA convergence is exercised at the
    # `ssc.astro.mosaic` unit-test level, not re-verified here.
    decs = sorted({round(p.wcs.crval2, 6) for p in pieces if p.wcs is not None})
    assert decs == [pytest.approx(dec_deg - 1.5), pytest.approx(dec_deg + 1.5)]
    # Four real, distinct pieces of imagery -- not the same tile served
    # four times over.
    contents = set()
    for idx in range(4):
        data, media = await env["service"].get_piece_bytes(result.id, idx, "jpg")
        assert media == "image/jpeg"
        contents.add(data)
    assert len(contents) == 4


async def test_unwise_small_request_stays_a_single_untiled_fetch(env: dict[str, Any]) -> None:
    """`size_deg` at or below `UNWISE_TILE_SIZE_DEG` is `n=1`: one request,
    unchanged from before tiling existed."""
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        result = await _run_and_wait(
            env, source_id="unwise", ra_deg=10.0, dec_deg=20.0, size_deg=3.0, bands=None
        )
    assert result.status is CutoutJobStatus.COMPLETE
    assert len(route.calls) == 1
    _, pieces = await env["service"].get_manifest(result.id)
    assert len(pieces) == 1


async def test_unwise_one_failed_tile_fails_the_whole_job(env: dict[str, Any]) -> None:
    """A genuine upstream error on one of four tiles must not be swallowed
    into a silent 3-piece result (CLAUDE.md invariant 7) -- the whole job
    fails, with a reason a viewer can read, and the tiles after the failing
    one are never fetched at all (a plain sequential loop that lets the
    first exception propagate, not a partial-result cleanup step)."""
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")  # pragma: no cover

    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(side_effect=upstream)
        result = await _run_and_wait(
            env, source_id="unwise", ra_deg=10.6847, dec_deg=-60.0, size_deg=6.0, bands=None
        )
    assert result.status is CutoutJobStatus.FAILED
    assert result.error is not None
    assert calls == 1
    _, pieces = await env["service"].get_manifest(result.id)
    assert pieces == []


async def test_unwise_uses_the_same_layer_regardless_of_declination(env: dict[str, Any]) -> None:
    """unlike Legacy Surveys' own optical layers, unWISE has no DR9/DR10
    hemisphere split — one all-sky release (THIRD_PARTY_NOTICES.md "unWISE
    resolved, 2026-09-07"); `legacy_layer(dec_deg)` must never apply to it."""
    with respx.mock() as router:
        route = router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        await _run_and_wait(
            env, source_id="unwise", ra_deg=10.0, dec_deg=50.0, size_deg=0.1, bands=None
        )
    assert "layer=unwise-neo7" in str(route.calls[0].request.url)


# -- autofetch on resolve ------------------------------------------------------ #


async def _wait_for_autofetch(
    env: dict[str, Any],
    ra_deg: float,
    dec_deg: float,
    source: CutoutSource = UNWISE_CUTOUT,
) -> Any | None:
    """Waits out the debounce and, if a request actually landed for this
    exact target, waits for that job to reach a terminal status too.
    Returns ``None`` if this target was never fetched (debounced away)."""
    service: CutoutService = env["service"]
    task = service._autofetch_task
    assert task is not None
    await task
    key = cutout_cache_key(
        source.id.value,
        release_tag(source, dec_deg),
        ra_deg,
        dec_deg,
        AUTOFETCH_SIZE_DEG,
    )
    row = await service.row_by_key(key)
    if row is None:
        return None
    await service._jobs[row.id]
    return await service.get_state(row.id)


def test_autofetch_default_source_is_unwise_at_a_single_untiled_size() -> None:
    assert AUTOFETCH_SOURCE_ID == "unwise"
    assert AUTOFETCH_SIZE_DEG == 3.0


def test_autofetch_eligible_sources_are_a_narrow_allowlist() -> None:
    """unWISE is the commercially cleared, genuinely all-sky,
    single-untiled-request source. AllWISE is not: its own cutout logic tiles a footprint into
    many requests (`_fetch_wise_incremental`), which is exactly the "tile
    sweep" the guard exists to keep out of an automatic, no-click fetch."""
    assert UNWISE_CUTOUT.autofetch_allowed is True
    assert find_cutout_source("wise") is not None
    assert find_cutout_source("wise").autofetch_allowed is False  # type: ignore[union-attr]


async def test_schedule_autofetch_refuses_a_source_not_eligible(env: dict[str, Any]) -> None:
    """The teeth: naming AllWISE raises immediately, synchronously, before
    the debounce even starts -- not a silently-dropped background task."""
    service: CutoutService = env["service"]
    with pytest.raises(CutoutRefusedError, match="wise"):
        service.schedule_autofetch(10.0, 20.0, source_id="wise")
    assert service._autofetch_task is None


async def test_schedule_autofetch_refuses_an_unknown_source(env: dict[str, Any]) -> None:
    service: CutoutService = env["service"]
    with pytest.raises(CutoutRefusedError, match="nonesuch"):
        service.schedule_autofetch(10.0, 20.0, source_id="nonesuch")


async def test_schedule_autofetch_fetches_unwise_by_default_after_the_debounce_window(
    env: dict[str, Any],
) -> None:
    service: CutoutService = env["service"]
    service._autofetch_debounce_s = 0.02
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        service.schedule_autofetch(10.0, 20.0)
        result = await _wait_for_autofetch(env, 10.0, 20.0)
    assert result is not None
    assert result.status is CutoutJobStatus.COMPLETE
    assert result.source == "unwise"


async def test_schedule_autofetch_accepts_an_eligible_non_default_source(
    env: dict[str, Any],
) -> None:
    """The source really is threaded through end to end, not just accepted
    and then ignored in favour of the hardcoded default."""
    service: CutoutService = env["service"]
    service._autofetch_debounce_s = 0.02
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        service.schedule_autofetch(10.0, 20.0, source_id="unwise")
        result = await _wait_for_autofetch(env, 10.0, 20.0, source=UNWISE_CUTOUT)
    assert result is not None
    assert result.status is CutoutJobStatus.COMPLETE
    assert result.source == "unwise"


async def test_schedule_autofetch_debounces_a_fast_sequence_of_resolves(
    env: dict[str, Any],
) -> None:
    """A user typing through targets re-resolving on every keystroke must
    not queue a fetch per keystroke -- only the last target in a fast
    sequence, once nothing newer arrives within the debounce window, is
    ever actually requested. Exercised against the default source to prove
    the debounce still holds once the source is a
    parameter, not just for the previous hardcoded unWISE path."""
    service: CutoutService = env["service"]
    service._autofetch_debounce_s = 0.05
    with respx.mock() as router:
        route = router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        # Four "keystrokes", each one superseding the last, well inside the
        # debounce window (no `await asyncio.sleep` between them).
        service.schedule_autofetch(1.0, 10.0)
        service.schedule_autofetch(2.0, 10.0)
        service.schedule_autofetch(3.0, 10.0)
        service.schedule_autofetch(4.0, 10.0)
        first_three = await asyncio.gather(
            *(_wait_for_autofetch(env, ra, 10.0) for ra in (1.0, 2.0, 3.0))
        )
        last = await _wait_for_autofetch(env, 4.0, 10.0)
        assert route.call_count == 1, "one request, not one per superseded resolve"
    assert first_three == [None, None, None]
    assert last is not None and last.status is CutoutJobStatus.COMPLETE


async def test_cancelling_a_superseded_autofetch_after_it_reaches_request_does_not_wedge_a_row(
    env: dict[str, Any],
) -> None:
    """The debounce is meant to cancel a call that has not started yet --
    not one that already reached `request()`. `request()` writes its row
    durably (invariant 6) *before* spawning the task that will ever move it
    to a terminal state; a cancellation landing in between used to abandon
    a `queued` row nothing would ever finish -- exactly the wedge the
    owner's instance hit (`snapshot.skymap.cutouts` showing a `running`,
    0-byte row with no toggle in the UI, live 2026-09-10). Reproduced by
    gating the row's own INSERT so the supersession lands right after it
    commits but before `request()` spawns `_run`."""
    service: CutoutService = env["service"]
    database: Database = env["database"]
    service._autofetch_debounce_s = 0.0

    reached_insert = asyncio.Event()
    release_insert = asyncio.Event()
    real_execute = database.execute

    async def gated_execute(sql: str, params: Any = ()) -> None:
        await real_execute(sql, params)
        if sql.startswith("INSERT INTO skymap_cutouts"):
            reached_insert.set()
            await release_insert.wait()

    database.execute = gated_execute  # type: ignore[method-assign]
    try:
        with respx.mock() as router:
            router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
                return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
            )
            service.schedule_autofetch(10.0, 20.0, UNWISE_CUTOUT.id.value)
            await asyncio.wait_for(reached_insert.wait(), timeout=2.0)

            # A newer resolve, superseding the one currently suspended just
            # past its own durable write -- "cancelled mid-request", not
            # mid-debounce.
            service.schedule_autofetch(30.0, 40.0, UNWISE_CUTOUT.id.value)
            release_insert.set()
            await asyncio.sleep(0.2)
    finally:
        database.execute = real_execute  # type: ignore[method-assign]

    key = cutout_cache_key(
        UNWISE_CUTOUT.id.value,
        release_tag(UNWISE_CUTOUT, 20.0),
        10.0,
        20.0,
        AUTOFETCH_SIZE_DEG,
    )
    row = await service.row_by_key(key)
    assert row is not None
    assert row.status not in (CutoutJobStatus.QUEUED, CutoutJobStatus.RUNNING), (
        "a superseded autofetch that already reached request() must not leave a "
        f"non-terminal row behind: got {row.status!r}"
    )
    # And it must actually have been allowed to finish, not merely marked
    # cancelled/interrupted by fiat -- the whole point is that this fetch
    # was nearly free to let complete.
    assert row.status is CutoutJobStatus.COMPLETE


async def test_three_rapid_resolves_in_a_row_leave_no_cutout_row_stuck(
    env: dict[str, Any],
) -> None:
    """Real usage from a live instance (2026-09-10): resolving M31, then
    M45, then IC1805 in quick succession, each with a pause for UI
    interaction in between. Gates each row's own INSERT so every
    supersession deterministically lands right after `request()` has
    already committed a durable row for the *previous* target -- the
    owner's session, forced instead of hoped for. Every row it creates
    must reach a terminal state, and the last target's imagery must
    actually be the one that arrives."""
    service: CutoutService = env["service"]
    service._autofetch_debounce_s = 0.0
    database: Database = env["database"]
    targets = [(10.6847, 41.269), (56.75, 24.117), (38.173, 61.457)]  # M31, M45, IC1805

    reached_insert = asyncio.Event()
    release_insert = asyncio.Event()
    real_execute = database.execute

    async def gated_execute(sql: str, params: Any = ()) -> None:
        await real_execute(sql, params)
        if sql.startswith("INSERT INTO skymap_cutouts"):
            reached_insert.set()
            await release_insert.wait()

    database.execute = gated_execute  # type: ignore[method-assign]
    try:
        with respx.mock() as router:
            router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
                return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
            )
            for ra_deg, dec_deg in targets[:-1]:
                service.schedule_autofetch(ra_deg, dec_deg)
                await asyncio.wait_for(reached_insert.wait(), timeout=2.0)
                reached_insert.clear()
                # Left suspended right past its own durable write; the next
                # resolve below supersedes it exactly there.
            service.schedule_autofetch(*targets[-1])
            release_insert.set()
            await asyncio.sleep(0.2)
    finally:
        database.execute = real_execute  # type: ignore[method-assign]

    rows = await database.fetch_all("SELECT * FROM skymap_cutouts ORDER BY created_at, id")
    assert len(rows) == 3, [dict(row) for row in rows]
    assert all(
        row["status"] not in (CutoutJobStatus.QUEUED.value, CutoutJobStatus.RUNNING.value)
        for row in rows
    ), [dict(row) for row in rows]
    assert rows[-1]["status"] == CutoutJobStatus.COMPLETE.value
    assert rows[-1]["ra_deg"] == pytest.approx(targets[-1][0])


async def test_cancelling_an_in_flight_fetch_frees_the_host_lock_for_the_next_one(
    env: dict[str, Any],
) -> None:
    """The more serious half of the same incident: if the per-host lock in
    `_get` stayed held by a cancelled fetch, every later request to that
    host would hang forever -- matching "nothing loaded" for every resolve
    after the wedge. Cancel a fetch while it holds the lock (via `delete()`,
    the one supported way to cancel a live `_run`) and confirm a second,
    different target to the *same* host completes rather than blocking on
    it."""
    service: CutoutService = env["service"]
    asked = asyncio.Event()
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        asked.set()
        await gate.wait()
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")

    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            side_effect=slow
        )
        first = await service.request(
            source_id="unwise", ra_deg=10.0, dec_deg=20.0, size_deg=0.1, bands=None
        )
        await asyncio.wait_for(asked.wait(), timeout=2.0)
        assert (await service.get_state(first.id)).status is CutoutJobStatus.RUNNING

        await service.delete(first.id)  # cancels the still-running fetch, holding the lock
        gate.set()  # let the (now moot) mock stop blocking

        second = await service.request(
            source_id="unwise", ra_deg=30.0, dec_deg=40.0, size_deg=0.1, bands=None
        )
        task = service._jobs[second.id]
        await asyncio.wait_for(task, timeout=2.0)
        final = await service.get_state(second.id)
    assert final.status is CutoutJobStatus.COMPLETE


async def test_a_5xx_upstream_failure_is_a_human_sentence_with_the_raw_detail_kept(
    env: dict[str, Any],
) -> None:
    """CLAUDE.md invariant 7 ("no silent degradation... surfaced in state and
    UI, never papered over"): a viewer reads a sentence, not "host answered
    500" — but the raw detail is still there, for diagnosis, in a separate
    field (`THIRD_PARTY_NOTICES.md`-adjacent deliverable, 2026-09-07: "Legacy
    Surveys shows a raw upstream error as if it were a normal message").
    Both the FITS path and its `jpeg-cutout` fallback (added the same day,
    for the reproducible `fits-cutout` 500 the fallback exists to route
    around) have to fail for this to reach FAILED at all."""
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/fits-cutout").mock(
            return_value=httpx.Response(500)
        )
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(500)
        )
        result = await _run_and_wait(
            env, source_id="legacy-surveys", ra_deg=10.6847, dec_deg=10.0, size_deg=0.1, bands=None
        )
    assert result.status is CutoutJobStatus.FAILED
    assert result.error == (
        "Legacy Surveys could not produce this image (server error) — try again later."
    )
    assert result.error_detail is not None
    assert "500" in result.error_detail
    assert "www.legacysurvey.org" in result.error_detail


async def test_a_retry_after_a_failure_restarts_the_same_row_not_a_stale_cache_hit(
    env: dict[str, Any],
) -> None:
    """The bug a plain cache-key lookup would have: `request()` returning the
    same `FAILED` row forever for the identical (source, ra, dec, size) a
    Retry button re-POSTs — the LayersPanel "Retry" action (deliverable 2,
    2026-09-07) would otherwise be a silent no-op. Both `fits-cutout` and
    its `jpeg-cutout` fallback fail here, the same as the sibling 5xx test
    above, to actually reach FAILED."""
    service: CutoutService = env["service"]
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/fits-cutout").mock(
            return_value=httpx.Response(500)
        )
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(500)
        )
        failed = await _run_and_wait(
            env, source_id="legacy-surveys", ra_deg=10.6847, dec_deg=10.0, size_deg=0.1, bands=None
        )
    assert failed.status is CutoutJobStatus.FAILED

    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/fits-cutout").mock(
            return_value=httpx.Response(200, content=build_fits_bytes(_cube(), _WCS))
        )
        retried = await service.request(
            source_id="legacy-surveys", ra_deg=10.6847, dec_deg=10.0, size_deg=0.1, bands=None
        )
        # Same row, not a second one under the same cache key (which the
        # unique index on cache_key would refuse anyway).
        assert retried.id == failed.id
        task = service._jobs[retried.id]
        await task
        final = await service.get_state(retried.id)
    assert final.id == failed.id
    assert final.status is CutoutJobStatus.COMPLETE
    assert final.error is None
    assert final.error_detail is None


async def test_a_repeat_request_for_the_same_key_is_a_cache_hit_no_second_fetch(
    env: dict[str, Any],
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        first = await _run_and_wait(
            env, source_id="wise", ra_deg=1.0, dec_deg=1.0, size_deg=0.5, bands=("W1",)
        )
        second = await env["service"].request(
            source_id="wise", ra_deg=1.0, dec_deg=1.0, size_deg=0.5, bands=("W1",)
        )
    assert first.id == second.id
    assert route.call_count == 1


async def test_an_unknown_band_is_refused(env: dict[str, Any]) -> None:
    with pytest.raises(CutoutRefusedError):
        await env["service"].request(
            source_id="wise", ra_deg=1.0, dec_deg=1.0, size_deg=0.5, bands=("Q9",)
        )


async def test_an_unknown_source_is_refused(env: dict[str, Any]) -> None:
    with pytest.raises(CutoutRefusedError):
        await env["service"].request(
            source_id="not-a-source", ra_deg=1.0, dec_deg=1.0, size_deg=0.5, bands=None
        )


async def test_eviction_drops_lru_but_protects_a_saved_plan_targets_cutout(
    env: dict[str, Any],
) -> None:
    service: CutoutService = env["service"]
    plans: PlanRepository = env["plans"]
    now = datetime.now(tz=UTC)
    # A saved plan targets exactly the first cutout's field (RA in hours).
    plan = Plan(
        id="p1",
        name="n",
        items=(
            GotoAndStackItem(
                id="g", start=AsapTimeSpec(), ra_j2000_h=10.6847 / 15.0, dec_j2000_deg=41.269
            ),
            ShutdownItem(id="s", start=AsapTimeSpec()),
        ),
        created_at=now,
        updated_at=now,
    )
    await plans.create(plan)

    big = np.full((64, 64), 50.0, dtype=np.float32)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable(["https://irsa.ipac.caltech.edu/t"]))
        )
        router.get("https://irsa.ipac.caltech.edu/t").mock(
            return_value=httpx.Response(200, content=build_fits_bytes(big, _WCS))
        )
        protected = await _run_and_wait(
            env, source_id="wise", ra_deg=10.6847, dec_deg=41.269, size_deg=0.5, bands=("W1",)
        )
        evictable = await _run_and_wait(
            env, source_id="wise", ra_deg=150.0, dec_deg=-20.0, size_deg=0.5, bands=("W1",)
        )
    # Force the budget below what both cutouts together need.
    service._max_bytes = protected.bytes + 10
    await service._enforce_budget()

    with pytest.raises(CutoutNotFoundError):
        await service.get_state(evictable.id)
    still_there = await service.get_state(protected.id)
    assert still_there.status is CutoutJobStatus.COMPLETE


# -- the startup reaper, and the exact way a row got stuck (2026-09-09) ------ #


async def test_an_unexpected_error_while_storing_fails_the_row_instead_of_leaving_it_running(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism behind the 2026-09-09 M31 incident: `_run` used to wrap
    only the network fetch in its except clauses, so an unexpected failure
    in `_store_pieces` (a disk write, a rendering bug, a DB constraint
    violation) propagated straight out of the spawned task -- `TaskRegistry`
    logs that and moves on, but nothing ever touches the row again. It is
    left at `running` forever, `updated_at` frozen at the earlier
    `_set_status(RUNNING)` write and no error ever recorded — invisible to
    `startup()`'s reaper, which only runs at process start, not while a live
    task is silently dying. `_run` must catch this too and mark the row
    `failed`, exactly as an upstream error already does."""
    service: CutoutService = env["service"]

    async def _boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("disk is full")

    monkeypatch.setattr(service, "_store_pieces", _boom)

    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        result = await _run_and_wait(
            env, source_id="unwise", ra_deg=10.0, dec_deg=20.0, size_deg=0.1, bands=None
        )
    assert result.status is CutoutJobStatus.FAILED
    assert result.error is not None
    assert result.error_detail == "disk is full"


async def test_startup_reaps_a_stuck_running_row_across_a_simulated_restart(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """The exact row shape the 2026-09-09 incident left behind: a cutout at
    `running`, `updated_at` equal to `created_at` (nothing ever wrote to it
    past the initial `_set_status(RUNNING)`), inserted directly rather than
    through `CutoutService.request()` -- this reproduces "some task, in some
    process, died leaving the row here" without needing to know which bug
    did it. A brand new `CutoutService` on top of a genuinely separate
    `Database` connection to the same on-disk file -- what an actual process
    restart does, not just a second call on the same instance -- must reap
    it, exactly as ``ssc.main``'s lifespan calls ``startup()`` unconditionally
    before serving anything."""
    database: Database = env["database"]
    cutout_id = "stuck012345"
    stamp = "2026-09-09T06:08:44.000000+00:00"
    await database.execute(
        "INSERT INTO skymap_cutouts (id, cache_key, source, ra_deg, dec_deg, size_deg, "
        "bands, status, bytes, error, created_at, updated_at, fetched_at, last_used) "
        "VALUES (?, 'stuck-key', 'unwise', 10.68, 41.27, 6.0, NULL, 'running', 0, NULL, "
        "?, ?, NULL, ?)",
        (cutout_id, stamp, stamp, stamp),
    )

    restarted_db = await open_database(database.path)
    try:
        restarted = CutoutService(
            restarted_db,
            tmp_path / "cutouts",
            max_bytes=10 * 1024 * 1024,
            user_agent=lambda: "SSC/test (+ops@example.org)",
            plans=PlanRepository(restarted_db),
        )
        await restarted.startup()
        state = await restarted.get_state(cutout_id)
    finally:
        await restarted_db.aclose()

    assert state.status is CutoutJobStatus.INTERRUPTED
    assert state.error == "ssc-core restarted while this cutout was fetching; request it again"
    assert state.updated_at > state.created_at


async def test_deleting_a_still_running_cutout_cancels_its_fetch_first(
    env: dict[str, Any],
) -> None:
    """The supported recovery path for a poisoned row (2026-09-09: the
    owner's only alternative was restarting ssc-core, which is never an
    agent's or an operator's call to make against their own instance). If
    `delete()` only removed the DB row and files, the still-live `_run` task
    would keep going and eventually try to write a piece row for a
    `cutout_id` that no longer exists in `skymap_cutouts` — a foreign key
    violation, not a silent no-op (`foreign_keys=ON`). Deleting a running
    cutout must cancel it first."""
    service: CutoutService = env["service"]
    tasks: list[asyncio.Task[Any]] = env["tasks"]
    asked = asyncio.Event()
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        asked.set()
        await gate.wait()
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")

    with respx.mock(assert_all_called=False) as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(side_effect=slow)
        state = await service.request(
            source_id="unwise", ra_deg=10.0, dec_deg=20.0, size_deg=0.1, bands=None
        )
        await asyncio.wait_for(asked.wait(), timeout=2.0)
        assert (await service.get_state(state.id)).status is CutoutJobStatus.RUNNING

        await service.delete(state.id)

        # The task `delete()` cancelled is the very one this test is
        # tracking (the `env` fixture's own teardown also cancels/awaits
        # everything, so this just confirms it happened here, not there).
        job = next(t for t in tasks if t.get_name() == f"cutout-{state.id}")
        assert job.cancelled()
        gate.set()

    with pytest.raises(CutoutNotFoundError):
        await service.get_state(state.id)


async def test_request_treats_a_running_row_with_no_live_task_as_dead(
    env: dict[str, Any],
) -> None:
    """Defense in depth for the exact failure mode `startup()`'s reaper
    cannot reach: a row left `running` by a task that has already died in
    *this* live process (no restart involved at all). `request()` must not
    hand that state back forever just because it is not one of
    `_RETRYABLE_STATUSES` -- with no task backing it in `self._jobs`, it can
    never finish on its own, so a fresh request for the same target must
    restart it rather than parrot the poisoned row."""
    service: CutoutService = env["service"]
    database: Database = env["database"]
    key = cutout_cache_key("unwise", "unwise-neo7", 10.0, 20.0, 0.1)
    stamp = "2026-09-09T06:08:44.000000+00:00"
    await database.execute(
        "INSERT INTO skymap_cutouts (id, cache_key, source, ra_deg, dec_deg, size_deg, "
        "bands, status, bytes, error, created_at, updated_at, fetched_at, last_used) "
        "VALUES ('orphan01234', ?, 'unwise', 10.0, 20.0, 0.1, NULL, 'running', 0, NULL, "
        "?, ?, NULL, ?)",
        (key, stamp, stamp, stamp),
    )
    # No entry in `service._jobs` for this id at all -- exactly what a fresh
    # process (or a task that already finished dying) looks like.

    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        state = await service.request(
            source_id="unwise", ra_deg=10.0, dec_deg=20.0, size_deg=0.1, bands=None
        )
        task = service._jobs[state.id]
        await task
    result = await service.get_state(state.id)
    assert state.id == "orphan01234"
    assert result.status is CutoutJobStatus.COMPLETE


async def test_a_retry_after_a_partial_batch_failure_does_not_collide_on_old_pieces(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch source (everything but AllWISE) has no way to skip a piece
    already on disk -- unlike `_fetch_wise_incremental`, a retry always
    re-fetches and re-stores every piece from index 0. If storing partway
    through a multi-piece result fails (piece 0 written, piece 1 raises) the
    row still has piece 0's row in `skymap_cutout_pieces` when it is marked
    `failed` by `_run`'s catch-all handler; a naive retry re-inserting piece
    0 would collide with that table's own (cutout_id, idx) primary key.
    `request()`'s retry path must clear a batch source's stale pieces first."""
    ra_deg, dec_deg = 10.6847, -60.0  # a size_deg=6 request: 4 unWISE tiles
    real_store_piece = CutoutService._store_piece
    calls = 0

    async def flaky_store(
        self: CutoutService, cutout_id: str, source_id: str, idx: int, piece: Any, **kwargs: Any
    ) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("disk is full")
        return await real_store_piece(self, cutout_id, source_id, idx, piece, **kwargs)

    monkeypatch.setattr(CutoutService, "_store_piece", flaky_store)
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        first = await _run_and_wait(
            env, source_id="unwise", ra_deg=ra_deg, dec_deg=dec_deg, size_deg=6.0, bands=None
        )
    assert first.status is CutoutJobStatus.FAILED
    _, pieces = await env["service"].get_manifest(first.id)
    assert len(pieces) == 1, "the one piece stored before the failure is still there"

    # A real retry: no more failures this time.
    monkeypatch.setattr(CutoutService, "_store_piece", real_store_piece)
    with respx.mock() as router:
        router.get("https://www.legacysurvey.org/viewer/jpeg-cutout").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg")
        )
        retried = await _run_and_wait(
            env, source_id="unwise", ra_deg=ra_deg, dec_deg=dec_deg, size_deg=6.0, bands=None
        )
    assert retried.id == first.id
    assert retried.status is CutoutJobStatus.COMPLETE
    _, pieces = await env["service"].get_manifest(retried.id)
    assert len(pieces) == 4
