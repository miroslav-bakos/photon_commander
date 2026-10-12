"""Export/import of the tile and cutout cache (``ssc.skymap.export_import``):
round-trip through a real archive, a local-wins skip, a corrupt entry
reported and skipped rather than fatal, and the whole-archive refusals
(unknown format version, zip-slip, not enough disk space) that must happen
before any write."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from ssc.skymap.cache import TileCache
from ssc.skymap.cutouts import CutoutService, ImportPiece
from ssc.skymap.export_import import EXPORT_FORMAT_VERSION, ExportImportService, ImportRefusedError
from ssc.skymap.surveys import find_survey
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate
from ssc.store.plans import PlanRepository

SURVEY_ID = "legacy-surveys-dr11"
SURVEY = find_survey(SURVEY_ID)
assert SURVEY is not None
TILE_BYTES = b"\x89PNG" + b"\x00" * 60
CUTOUT_BYTES = b"SIMPLE  " + b"\x00" * 120
DISPLAY_BYTES = b"\x89PNG" + b"\x11" * 40


class _Env:
    def __init__(
        self,
        database: Database,
        cache: TileCache,
        cutouts: CutoutService,
        service: ExportImportService,
        data_dir: Path,
    ) -> None:
        self.database = database
        self.cache = cache
        self.cutouts = cutouts
        self.service = service
        self.data_dir = data_dir
        self.published = 0

    async def note_publish(self) -> None:
        self.published += 1


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    database = await open_database(tmp_path / "ssc.sqlite3")
    await migrate(database)
    cache = TileCache(database, tmp_path / "tiles")
    plans = PlanRepository(database)
    cutouts = CutoutService(
        database,
        tmp_path / "cutouts",
        max_bytes=10 * 1024 * 1024,
        user_agent=lambda: "SSC/test (+ops@example.org)",
        plans=plans,
    )
    await cutouts.startup()
    holder = _Env(database, cache, cutouts, service=None, data_dir=tmp_path)  # type: ignore[arg-type]
    service = ExportImportService(
        database,
        cache,
        cutouts,
        data_dir=tmp_path,
        schema_version=99,
        publish=holder.note_publish,
    )
    holder.service = service
    try:
        yield holder
    finally:
        await database.aclose()


async def _seed_tiles(env: _Env) -> None:
    await env.cache.store(SURVEY, 0, 0, "png", TILE_BYTES, pinned=False)
    await env.cache.store_missing(SURVEY, 1, 3, "png", pinned=False)


async def _seed_cutout(env: _Env, *, cache_key: str = "wise:allwise:10.00:20.00:0.50") -> str:
    new_id = await env.cutouts.import_cutout(
        cache_key=cache_key,
        source_id="wise",
        ra_deg=10.0,
        dec_deg=20.0,
        size_deg=0.5,
        bands=("w1",),
        pieces=[
            ImportPiece(
                band="w1",
                format="fits",
                data=CUTOUT_BYTES,
                display_png=DISPLAY_BYTES,
                stretch="asinh",
                width=8,
                height=8,
                wcs=None,
            )
        ],
    )
    assert new_id is not None
    return new_id


async def _export_blob(env: _Env) -> bytes:
    chunks = [chunk async for chunk in env.service.export_stream()]
    return b"".join(chunks)


def _manifest_of(blob: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        manifest: dict[str, Any] = json.loads(zf.read("manifest.json"))
        return manifest


async def test_round_trip_tiles_and_cutouts_survive_export_then_import(env: _Env) -> None:
    await _seed_tiles(env)
    cutout_id = await _seed_cutout(env)

    blob = await _export_blob(env)
    manifest = _manifest_of(blob)
    assert manifest["format_version"] == EXPORT_FORMAT_VERSION
    assert manifest["counts"]["tiles"] == 2
    assert manifest["counts"]["cutouts"] == 1

    # Wipe both caches completely.
    await env.cache.clear(SURVEY)
    deleted = await env.cutouts.delete_all()
    assert deleted == 1
    assert await env.cache.lookup(SURVEY_ID, 0, 0) is None
    assert await env.cutouts.row_by_key("wise:allwise:10.00:20.00:0.50") is None

    counts = await env.service.import_archive(io.BytesIO(blob))
    assert counts.tiles_imported == 2
    assert counts.tiles_skipped == 0
    assert counts.tiles_rejected == 0
    assert counts.cutouts_imported == 1
    assert counts.cutouts_skipped == 0
    assert counts.cutouts_rejected == 0
    assert env.published == 1, "one publish for the whole import, not one per row"

    cached = await env.cache.lookup(SURVEY_ID, 0, 0)
    assert cached is not None and cached.status == "cached"
    assert await env.cache.read(SURVEY, cached) == TILE_BYTES
    missing = await env.cache.lookup(SURVEY_ID, 1, 3)
    assert missing is not None and missing.status == "missing"

    restored = await env.cutouts.row_by_key("wise:allwise:10.00:20.00:0.50")
    assert restored is not None
    assert restored.id != cutout_id, "import always assigns a fresh id"
    state, pieces = await env.cutouts.get_manifest(restored.id)
    assert state.status == "complete"
    assert len(pieces) == 1
    data, _media = await env.cutouts.get_piece_bytes(restored.id, 0, "fits")
    assert data == CUTOUT_BYTES
    png, _media2 = await env.cutouts.get_piece_bytes(restored.id, 0, "png")
    assert png == DISPLAY_BYTES


async def test_import_skips_a_key_already_cached_locally_local_wins(env: _Env) -> None:
    await _seed_tiles(env)
    await _seed_cutout(env)
    blob = await _export_blob(env)

    # Do NOT wipe: the local tile/cutout are already there when we import
    # the very same archive back on top of them.
    counts = await env.service.import_archive(io.BytesIO(blob))
    assert counts.tiles_imported == 0
    assert counts.tiles_skipped == 2
    assert counts.cutouts_imported == 0
    assert counts.cutouts_skipped == 1

    cached = await env.cache.lookup(SURVEY_ID, 0, 0)
    assert cached is not None
    assert await env.cache.read(SURVEY, cached) == TILE_BYTES, "the local copy, untouched"


async def test_a_tile_manifest_row_with_no_matching_file_is_rejected_not_fatal(env: _Env) -> None:
    await _seed_tiles(env)
    await _seed_cutout(env)
    blob = await _export_blob(env)

    # Rebuild the archive with the cached tile's file removed but its
    # manifest row intact — a corrupt entry, not present in the zip at all.
    with zipfile.ZipFile(io.BytesIO(blob)) as src:
        names = set(src.namelist())
        tile_path = next(n for n in names if n.startswith("tiles/") and n.count("/") > 1)
        members = {name: src.read(name) for name in names if name != tile_path}
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w") as dst:
        for name, data in members.items():
            dst.writestr(name, data)
    out.seek(0)

    await env.cache.clear(SURVEY)
    await env.cutouts.delete_all()

    counts = await env.service.import_archive(out)
    assert counts.tiles_imported == 1, "the still-present missing-tile row"
    assert counts.tiles_rejected == 1, "the cached tile whose file vanished from the archive"
    assert counts.cutouts_imported == 1


async def test_unknown_format_version_is_refused(env: _Env) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"format_version": 2, "schema_version": 1, "total_bytes": 0}),
        )
    out.seek(0)
    with pytest.raises(ImportRefusedError, match="format version"):
        await env.service.import_archive(out)


async def test_zip_slip_is_refused_before_any_write(env: _Env) -> None:
    """A hostile ``ext`` value spliced into a filesystem path is refused for
    the whole archive — including a second, perfectly valid row alongside
    it, proving nothing was written before the refusal."""
    manifest = {
        "format_version": EXPORT_FORMAT_VERSION,
        "schema_version": 1,
        "exported_at": "2026-09-07T00:00:00+00:00",
        "counts": {
            "tiles": 2,
            "tiles_bytes": len(TILE_BYTES),
            "cutouts": 0,
            "cutout_pieces": 0,
            "cutout_bytes": 0,
        },
        "total_bytes": len(TILE_BYTES),
    }
    rows = [
        {
            "survey": SURVEY_ID,
            "order": 0,
            "npix": 0,
            "ext": "../../../../evil",
            "status": "missing",
            "bytes": 0,
            "pinned": False,
            "fetched_at": "2026-09-07T00:00:00+00:00",
            "last_used": "2026-09-07T00:00:00+00:00",
        },
        {
            "survey": SURVEY_ID,
            "order": 2,
            "npix": 5,
            "ext": "png",
            "status": "cached",
            "bytes": len(TILE_BYTES),
            "pinned": False,
            "fetched_at": "2026-09-07T00:00:00+00:00",
            "last_used": "2026-09-07T00:00:00+00:00",
        },
    ]
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("tiles/manifest.jsonl", "\n".join(json.dumps(r) for r in rows))
        arcname = (
            "tiles/"
            + env.cache.path_for(SURVEY, 2, 5, "png").relative_to(env.cache.root).as_posix()
        )
        zf.writestr(arcname, TILE_BYTES)
        zf.writestr("cutouts/manifest.jsonl", "")
        zf.writestr("cutouts/pieces.jsonl", "")
    out.seek(0)

    with pytest.raises(ImportRefusedError, match="unsafe extension"):
        await env.service.import_archive(out)

    assert await env.cache.lookup(SURVEY_ID, 2, 5) is None, "the valid row was never written either"


async def test_not_enough_disk_space_is_refused_before_any_write(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tiles(env)
    blob = await _export_blob(env)
    await env.cache.clear(SURVEY)

    class _TinyUsage:
        total = 100
        used = 90
        free = 10

    monkeypatch.setattr(shutil, "disk_usage", lambda path: _TinyUsage())

    with pytest.raises(ImportRefusedError, match="free"):
        await env.service.import_archive(io.BytesIO(blob))
    assert await env.cache.lookup(SURVEY_ID, 0, 0) is None
