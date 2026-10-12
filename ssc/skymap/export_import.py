# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Export and import of the user's own already-fetched offline map cache —
a portability/backup feature, never a way to ship or redistribute a
pre-built pack. The archive holds exactly what ``ssc/skymap/cache.py``
(``TileCache``) and ``ssc/skymap/cutouts.py`` (``CutoutService``) already
fetched and cached on *this* machine; this module never talks to an
upstream archive and never bundles anything that was not already
downloaded. No licence question is opened by moving a copy of one
instance's own fetched data between that user's own machines; the survey
terms recorded in ``THIRD_PARTY_NOTICES.md`` still govern what may be fetched
in the first place, and this module adds nothing to that.

Archive layout (a plain zip, stdlib ``zipfile``, ``ZIP_STORED`` — tile and
cutout files are already-compressed image formats, so a second compression
pass buys nothing and only costs CPU and a full in-memory buffer per entry):

* ``manifest.json`` — ``format_version`` (this module understands ``1``
  only; anything else is refused before any write), the app's own schema
  version at export time (informational — an import never refuses on this,
  since the row data is replayed through ``TileCache``/``CutoutService``
  rather than restored as raw SQL), an export timestamp, and summary counts.
* ``tiles/manifest.jsonl`` — one row per ``skymap_tiles`` index entry,
  cached and known-missing both (a pack's negative entries matter for
  ``pack_complete_order`` too). ``tiles/<survey>/...`` mirrors
  ``TileCache``'s own on-disk pyramid layout exactly, one file per cached
  row — the path is never stored in the manifest, it is recomputed on both
  sides from ``survey + order + npix + ext`` via the same
  ``TileCache.path_for``, so there is exactly one source of truth for it.
* ``cutouts/manifest.jsonl`` / ``cutouts/pieces.jsonl`` — one row per
  ``skymap_cutouts`` (``complete`` only; nothing else has files worth
  saving) and one per ``skymap_cutout_pieces``. ``cutouts/<source>/<id>/...``
  mirrors ``CutoutService``'s own layout, likewise recomputed rather than
  stored.

Export streams (``ssc.api.routes.skymap`` wraps this in a
``StreamingResponse``): rows are read up front with plain ``await``ed
queries, no transaction held open across the stream. The zip itself is
built in a worker thread that feeds chunks to the async generator through a
small bounded ``queue.Queue`` — the whole archive, which can be gigabytes,
is never buffered in memory, only a handful of chunks in flight.

Import never overwrites a fresher local fetch: a tile or cutout whose key
is already cached locally is skipped and counted, and the local copy wins,
because a client that fetched something after a backup was taken must never
see it regressed by an older archive. Every path this module writes is
built from validated, known-safe components (a registered survey/source id,
an integer order/npix/idx, an extension checked against a plain
alphanumeric token) — never from a raw string taken out of the archive and
joined onto a filesystem path — and the whole archive is refused, before
any write, if any entry's *intended* location would not stay under
``data_dir`` (CLAUDE.md invariant 7: refuse loudly, never fill the disk or
write outside it mid-import). A manifest row referencing a file missing
from the archive, or whose size does not match, is skipped and counted —
never fatal to the rest of the import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event
from typing import IO, Any, Final

from ssc.skymap.cache import TileCache, now_utc
from ssc.skymap.cutouts import CutoutService, ImportPiece
from ssc.skymap.fits import ImageWCS
from ssc.skymap.surveys import find_cutout_source, find_survey
from ssc.store.database import Database

_log = logging.getLogger(__name__)

#: The archive container format this module writes and understands. An
#: import naming anything else is refused outright — never guess-coerced.
EXPORT_FORMAT_VERSION: Final = 1

#: A component (survey id excepted — that is checked against the registry
#: instead) spliced into a filesystem path must look like this: never
#: empty, never ``.``/``..``, never containing a path separator.
_SAFE_TOKEN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

#: Refuse an import whose manifest claims more bytes than
#: ``free space - this margin`` — never fill the disk to the last byte;
#: WAL growth and other processes need headroom too.
_DISK_SAFETY_MARGIN_BYTES: Final = 512 * 1024**2

#: Export streaming: how large a chunk handed to the async consumer is, and
#: how many chunks may be queued before the producer thread blocks (bounds
#: memory to a few hundred KiB in flight, never the whole archive).
_CHUNK_BYTES: Final = 256 * 1024
_QUEUE_MAXSIZE: Final = 8
#: How long the producer's queue.put waits before re-checking for
#: cancellation (a client that stopped reading mid-download).
_PUT_POLL_S: Final = 0.5

_END: Final = object()


def _safe_token(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_TOKEN.fullmatch(value)) and ".." not in value


class ImportRefusedError(ValueError):
    """The archive as a whole is refused before any write: an unknown
    format version, a manifest that will not parse, a path that would
    escape ``data_dir``, or not enough free disk space for what it claims.
    Distinct from one corrupt entry, which is skipped and counted, never
    fatal to the rest of the import."""


class _ExportCancelledError(Exception):
    """The client stopped reading mid-stream; the worker thread unwinds."""


@dataclass(frozen=True, slots=True)
class ImportCounts:
    tiles_imported: int
    tiles_skipped: int
    tiles_rejected: int
    cutouts_imported: int
    cutouts_skipped: int
    cutouts_rejected: int


def _row_int(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key!r} is not an integer")
    return value


def _row_str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key!r} is not a non-empty string")
    return value


def _wcs_from_row(row: dict[str, Any]) -> ImageWCS | None:
    if row.get("crpix1") is None:
        return None
    try:
        return ImageWCS(
            crpix1=float(row["crpix1"]),
            crpix2=float(row["crpix2"]),
            crval1=float(row["crval1"]),
            crval2=float(row["crval2"]),
            cd1_1=float(row["cd1_1"]),
            cd1_2=float(row["cd1_2"]),
            cd2_1=float(row["cd2_1"]),
            cd2_2=float(row["cd2_2"]),
            ctype1=str(row["ctype1"]),
            ctype2=str(row["ctype2"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


class _QueueSink:
    """A minimal binary "file" ``zipfile.ZipFile`` writes to: no ``tell``/
    ``seek`` at all, which is cpython zipfile's own documented recipe for
    streaming to a non-seekable destination (it catches the resulting
    ``AttributeError`` and switches to writing data descriptors instead of
    seeking back to patch local headers). ``write`` subdivides into
    ``_CHUNK_BYTES`` pieces and blocks on the bounded queue for backpressure,
    polling ``cancel`` so a dropped client connection unwinds the producer
    promptly instead of leaking a blocked thread forever."""

    def __init__(self, sink: Queue[object], cancel: Event) -> None:
        self._sink = sink
        self._cancel = cancel

    def write(self, data: bytes) -> int:
        view = bytes(data)
        for start in range(0, len(view), _CHUNK_BYTES):
            self._put(view[start : start + _CHUNK_BYTES])
        return len(view)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def _put(self, chunk: bytes) -> None:
        while True:
            if self._cancel.is_set():
                raise _ExportCancelledError
            try:
                self._sink.put(chunk, timeout=_PUT_POLL_S)
                return
            except Full:
                continue


class ExportImportService:
    """Streams the tile/cutout cache out as one archive, and replays one
    back in — the only two places that read ``TileCache``'s and
    ``CutoutService``'s tables directly rather than through their own
    methods (for export; import always writes back through those classes'
    public methods, never raw SQL)."""

    def __init__(
        self,
        database: Database,
        cache: TileCache,
        cutouts: CutoutService,
        *,
        data_dir: Path,
        schema_version: int,
        publish: Callable[[], Awaitable[None]],
        now: Callable[[], datetime] = now_utc,
    ) -> None:
        self._db = database
        self._cache = cache
        self._cutouts = cutouts
        self._data_dir = data_dir
        self._schema_version = schema_version
        self._publish = publish
        self._now = now

    # -- export ---------------------------------------------------------- #

    async def export_stream(self) -> AsyncIterator[bytes]:
        tile_rows = await self._export_tile_rows()
        cutout_rows, pieces_by_cutout = await self._export_cutout_rows()
        manifest = self._manifest_bytes(tile_rows, cutout_rows, pieces_by_cutout)

        sink: Queue[object] = Queue(maxsize=_QUEUE_MAXSIZE)
        cancel = Event()
        future = asyncio.get_running_loop().run_in_executor(
            None, self._write_zip, sink, cancel, manifest, tile_rows, cutout_rows, pieces_by_cutout
        )
        try:
            while True:
                item = await asyncio.to_thread(sink.get)
                if item is _END:
                    break
                if not isinstance(item, (bytes, bytearray)):
                    raise TypeError(f"export producer yielded an unexpected item: {item!r}")
                yield bytes(item)
        finally:
            cancel.set()
            # Drain so the producer (blocked on a full queue, if the
            # consumer stopped early) sees room, notices `cancel` and exits,
            # rather than being left running forever in the executor.
            while not future.done():
                try:
                    sink.get_nowait()
                except Empty:
                    await asyncio.sleep(_PUT_POLL_S)
            await future

    async def _export_tile_rows(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT survey, tile_order, npix, ext, status, bytes, pinned, fetched_at, last_used "
            "FROM skymap_tiles ORDER BY survey, tile_order, npix"
        )
        return [
            {
                "survey": str(row["survey"]),
                "order": int(row["tile_order"]),
                "npix": int(row["npix"]),
                "ext": str(row["ext"]),
                "status": str(row["status"]),
                "bytes": int(row["bytes"]),
                "pinned": bool(row["pinned"]),
                "fetched_at": str(row["fetched_at"]),
                "last_used": str(row["last_used"]),
            }
            for row in rows
        ]

    async def _export_cutout_rows(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        cutout_rows = await self._db.fetch_all(
            "SELECT id, cache_key, source, ra_deg, dec_deg, size_deg, bands, bytes, "
            "created_at, updated_at, fetched_at FROM skymap_cutouts WHERE status = 'complete' "
            "ORDER BY id"
        )
        cutouts = [
            {
                "id": str(row["id"]),
                "cache_key": str(row["cache_key"]),
                "source": str(row["source"]),
                "ra_deg": float(row["ra_deg"]),
                "dec_deg": float(row["dec_deg"]),
                "size_deg": float(row["size_deg"]),
                "bands": json.loads(str(row["bands"])) if row["bands"] is not None else None,
                "bytes": int(row["bytes"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "fetched_at": str(row["fetched_at"]),
            }
            for row in cutout_rows
        ]
        ids = {c["id"] for c in cutouts}
        piece_rows = await self._db.fetch_all(
            "SELECT * FROM skymap_cutout_pieces ORDER BY cutout_id, idx"
        )
        by_cutout: dict[str, list[dict[str, Any]]] = {}
        for row in piece_rows:
            cutout_id = str(row["cutout_id"])
            if cutout_id not in ids:
                continue  # a piece of a job not (yet) complete; nothing to export
            by_cutout.setdefault(cutout_id, []).append(
                {
                    "cutout_id": cutout_id,
                    "idx": int(row["idx"]),
                    "band": None if row["band"] is None else str(row["band"]),
                    "format": str(row["format"]),
                    "bytes": int(row["bytes"]),
                    "has_display_png": row["display_png_path"] is not None,
                    "width": None if row["width"] is None else int(row["width"]),
                    "height": None if row["height"] is None else int(row["height"]),
                    "crpix1": row["crpix1"],
                    "crpix2": row["crpix2"],
                    "crval1": row["crval1"],
                    "crval2": row["crval2"],
                    "cd1_1": row["cd1_1"],
                    "cd1_2": row["cd1_2"],
                    "cd2_1": row["cd2_1"],
                    "cd2_2": row["cd2_2"],
                    "ctype1": row["ctype1"],
                    "ctype2": row["ctype2"],
                    "stretch": None if row["stretch"] is None else str(row["stretch"]),
                }
            )
        return cutouts, by_cutout

    def _manifest_bytes(
        self,
        tile_rows: list[dict[str, Any]],
        cutout_rows: list[dict[str, Any]],
        pieces_by_cutout: dict[str, list[dict[str, Any]]],
    ) -> bytes:
        tiles_bytes = sum(int(row["bytes"]) for row in tile_rows)
        cutout_bytes = sum(int(row["bytes"]) for row in cutout_rows)
        piece_count = sum(len(pieces) for pieces in pieces_by_cutout.values())
        manifest = {
            "format_version": EXPORT_FORMAT_VERSION,
            "schema_version": self._schema_version,
            "exported_at": self._now().isoformat(),
            "counts": {
                "tiles": len(tile_rows),
                "tiles_bytes": tiles_bytes,
                "cutouts": len(cutout_rows),
                "cutout_pieces": piece_count,
                "cutout_bytes": cutout_bytes,
            },
            "total_bytes": tiles_bytes + cutout_bytes,
        }
        return json.dumps(manifest, sort_keys=True).encode("utf-8")

    def _write_zip(
        self,
        sink: Queue[object],
        cancel: Event,
        manifest: bytes,
        tile_rows: list[dict[str, Any]],
        cutout_rows: list[dict[str, Any]],
        pieces_by_cutout: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Runs entirely in a worker thread — every call here is a plain
        synchronous file read plus a queue put, never a coroutine, so this
        method itself never touches the event loop."""
        try:
            with zipfile.ZipFile(
                _QueueSink(sink, cancel), mode="w", compression=zipfile.ZIP_STORED
            ) as zf:
                zf.writestr("manifest.json", manifest)
                zf.writestr("tiles/manifest.jsonl", _jsonl(tile_rows))
                for row in tile_rows:
                    if row["status"] != "cached":
                        continue
                    survey = find_survey(row["survey"])
                    if survey is None:
                        _log.warning(
                            "export: survey %r is no longer registered, skipping its cached tile",
                            row["survey"],
                        )
                        continue
                    path = self._cache.path_for(survey, row["order"], row["npix"], row["ext"])
                    if not path.is_file():
                        _log.warning(
                            "export: tile index row with no file on disk, skipping: %s", path
                        )
                        continue
                    arcname = "tiles/" + path.relative_to(self._cache.root).as_posix()
                    zf.write(path, arcname=arcname)
                zf.writestr("cutouts/manifest.jsonl", _jsonl(cutout_rows))
                all_pieces = [p for row in cutout_rows for p in pieces_by_cutout.get(row["id"], [])]
                zf.writestr("cutouts/pieces.jsonl", _jsonl(all_pieces))
                for piece in all_pieces:
                    source_id = next(
                        (row["source"] for row in cutout_rows if row["id"] == piece["cutout_id"]),
                        None,
                    )
                    if source_id is None:
                        continue  # pragma: no cover - piece always belongs to an exported cutout
                    directory = f"{source_id}/{piece['cutout_id']}"
                    raw_path = (
                        self._cutouts.root / directory / f"piece{piece['idx']}.{piece['format']}"
                    )
                    if not raw_path.is_file():
                        _log.warning(
                            "export: cutout piece index row with no file on disk, skipping: %s",
                            raw_path,
                        )
                        continue
                    zf.write(
                        raw_path,
                        arcname=f"cutouts/{directory}/piece{piece['idx']}.{piece['format']}",
                    )
                    if piece["has_display_png"]:
                        display_path = self._cutouts.root / directory / f"piece{piece['idx']}.png"
                        if display_path.is_file():
                            zf.write(
                                display_path, arcname=f"cutouts/{directory}/piece{piece['idx']}.png"
                            )
        except _ExportCancelledError:
            _log.info("export: client stopped reading mid-stream, archive aborted")
        except Exception:
            _log.exception("export: failed while streaming the archive")
        finally:
            # Blocking, never `put_nowait` + drop-on-`Full`: the consumer
            # must always see this sentinel or it blocks on `sink.get()`
            # forever. Safe to block here — the consumer keeps draining
            # until it sees `_END`, whether that is the normal read loop or
            # the cancellation drain in `export_stream`'s own `finally`.
            sink.put(_END)

    # -- import ------------------------------------------------------------ #

    async def import_archive(self, stream: IO[bytes]) -> ImportCounts:
        zf = await asyncio.to_thread(zipfile.ZipFile, stream)
        try:
            return await self._import_from_zip(zf)
        finally:
            await asyncio.to_thread(zf.close)

    async def _import_from_zip(self, zf: zipfile.ZipFile) -> ImportCounts:
        manifest = await self._read_manifest(zf)
        format_version = manifest.get("format_version")
        if format_version != EXPORT_FORMAT_VERSION:
            raise ImportRefusedError(
                f"unknown export format version {format_version!r}; this build understands "
                f"version {EXPORT_FORMAT_VERSION} only — export it again with a matching build"
            )

        tile_rows = await self._read_jsonl(zf, "tiles/manifest.jsonl")
        cutout_rows = await self._read_jsonl(zf, "cutouts/manifest.jsonl")
        piece_rows = await self._read_jsonl(zf, "cutouts/pieces.jsonl")
        pieces_by_cutout: dict[str, list[dict[str, Any]]] = {}
        for row in piece_rows:
            cutout_id = row.get("cutout_id")
            if isinstance(cutout_id, str):
                pieces_by_cutout.setdefault(cutout_id, []).append(row)

        # Zip-slip guard, over every entry this import would write, before
        # any write happens.
        self._check_no_escape(tile_rows, cutout_rows, pieces_by_cutout)

        claimed_total = manifest.get("total_bytes")
        if not isinstance(claimed_total, int) or claimed_total < 0:
            raise ImportRefusedError("manifest.json's total_bytes is missing or not a number")
        usage = await asyncio.to_thread(shutil.disk_usage, self._data_dir)
        if claimed_total + _DISK_SAFETY_MARGIN_BYTES > usage.free:
            raise ImportRefusedError(
                f"archive claims {claimed_total:,} bytes of content; only {usage.free:,} bytes "
                f"are free under {self._data_dir} (refusing within a "
                f"{_DISK_SAFETY_MARGIN_BYTES:,}-byte safety margin) — free up space and retry"
            )

        names = frozenset(zf.namelist())
        tiles_imported = tiles_skipped = tiles_rejected = 0
        for row in tile_rows:
            outcome = await self._import_tile(zf, names, row)
            if outcome == "imported":
                tiles_imported += 1
            elif outcome == "skipped":
                tiles_skipped += 1
            else:
                tiles_rejected += 1

        cutouts_imported = cutouts_skipped = cutouts_rejected = 0
        for row in cutout_rows:
            cutout_id = row.get("id")
            pieces = pieces_by_cutout.get(cutout_id, []) if isinstance(cutout_id, str) else []
            outcome = await self._import_cutout_row(zf, names, row, pieces)
            if outcome == "imported":
                cutouts_imported += 1
            elif outcome == "skipped":
                cutouts_skipped += 1
            else:
                cutouts_rejected += 1

        if tiles_imported or cutouts_imported:
            await self._publish()

        return ImportCounts(
            tiles_imported=tiles_imported,
            tiles_skipped=tiles_skipped,
            tiles_rejected=tiles_rejected,
            cutouts_imported=cutouts_imported,
            cutouts_skipped=cutouts_skipped,
            cutouts_rejected=cutouts_rejected,
        )

    async def _read_manifest(self, zf: zipfile.ZipFile) -> dict[str, Any]:
        try:
            raw = await asyncio.to_thread(zf.read, "manifest.json")
        except KeyError as exc:
            raise ImportRefusedError("archive has no manifest.json") from exc
        try:
            manifest = json.loads(raw)
        except ValueError as exc:
            raise ImportRefusedError(f"manifest.json is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ImportRefusedError("manifest.json is not a JSON object")
        return manifest

    async def _read_jsonl(self, zf: zipfile.ZipFile, name: str) -> list[dict[str, Any]]:
        if name not in zf.namelist():
            return []
        raw = await asyncio.to_thread(zf.read, name)
        rows: list[dict[str, Any]] = []
        try:
            for line in raw.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    rows.append(parsed)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ImportRefusedError(f"{name} is corrupt: {exc}") from exc
        return rows

    def _check_no_escape(
        self,
        tile_rows: list[dict[str, Any]],
        cutout_rows: list[dict[str, Any]],
        pieces_by_cutout: dict[str, list[dict[str, Any]]],
    ) -> None:
        tiles_root = self._cache.root.resolve()
        cutouts_root = self._cutouts.root.resolve()
        for row in tile_rows:
            survey_id, order, npix, ext = (
                row.get("survey"),
                row.get("order"),
                row.get("npix"),
                row.get("ext"),
            )
            if (
                not isinstance(survey_id, str)
                or not isinstance(order, int)
                or not isinstance(npix, int)
            ):
                continue  # malformed row: rejected per-entry later, not a slip
            survey = find_survey(survey_id)
            if survey is None:
                continue  # no longer registered: rejected per-entry later, not a slip
            if not isinstance(ext, str) or not _safe_token(ext):
                raise ImportRefusedError(
                    f"a tile entry has an unsafe extension {ext!r}; refusing the whole archive"
                )
            path = self._cache.path_for(survey, order, npix, ext).resolve()
            if not path.is_relative_to(tiles_root):
                raise ImportRefusedError(
                    "a tile entry's path would escape the tile cache directory; "
                    "refusing the whole archive"
                )
        for row in cutout_rows:
            source_id, cutout_id = row.get("source"), row.get("id")
            if not isinstance(source_id, str) or find_cutout_source(source_id) is None:
                continue
            if not isinstance(cutout_id, str) or not _safe_token(cutout_id):
                raise ImportRefusedError(
                    f"a cutout entry has an unsafe id {cutout_id!r}; refusing the whole archive"
                )
            for piece in pieces_by_cutout.get(cutout_id, []):
                fmt, idx = piece.get("format"), piece.get("idx")
                if not isinstance(idx, int):
                    continue
                if not isinstance(fmt, str) or not _safe_token(fmt):
                    raise ImportRefusedError(
                        f"a cutout piece has an unsafe format {fmt!r}; refusing the whole archive"
                    )
                path = (cutouts_root / source_id / cutout_id / f"piece{idx}.{fmt}").resolve()
                if not path.is_relative_to(cutouts_root):
                    raise ImportRefusedError(
                        "a cutout piece's path would escape the cutouts cache directory; "
                        "refusing the whole archive"
                    )

    async def _import_tile(
        self, zf: zipfile.ZipFile, names: frozenset[str], row: dict[str, Any]
    ) -> str:
        try:
            survey_id = _row_str(row, "survey")
            order = _row_int(row, "order")
            npix = _row_int(row, "npix")
            ext = _row_str(row, "ext")
            status = _row_str(row, "status")
            pinned = bool(row.get("pinned", False))
            size = _row_int(row, "bytes")
        except ValueError:
            return "rejected"
        survey = find_survey(survey_id)
        if survey is None:
            return "rejected"
        existing = await self._cache.lookup(survey_id, order, npix)
        if existing is not None:
            return "skipped"  # already cached locally: imported data never overwrites it
        if status == "missing":
            await self._cache.store_missing(survey, order, npix, ext, pinned=pinned)
            return "imported"
        arcname = (
            "tiles/"
            + self._cache.path_for(survey, order, npix, ext)
            .relative_to(self._cache.root)
            .as_posix()
        )
        if arcname not in names:
            return "rejected"
        info = zf.getinfo(arcname)
        if info.file_size != size:
            return "rejected"
        data = await asyncio.to_thread(zf.read, arcname)
        if len(data) != size:
            return "rejected"
        await self._cache.store(survey, order, npix, ext, data, pinned=pinned)
        return "imported"

    async def _import_cutout_row(
        self,
        zf: zipfile.ZipFile,
        names: frozenset[str],
        row: dict[str, Any],
        piece_rows: list[dict[str, Any]],
    ) -> str:
        try:
            cache_key = _row_str(row, "cache_key")
            source_id = _row_str(row, "source")
            ra_deg = float(row["ra_deg"])
            dec_deg = float(row["dec_deg"])
            size_deg = float(row["size_deg"])
            original_id = _row_str(row, "id")
        except (ValueError, KeyError, TypeError):
            return "rejected"
        if find_cutout_source(source_id) is None:
            return "rejected"
        existing = await self._cutouts.row_by_key(cache_key)
        if existing is not None:
            return "skipped"  # already cached locally: imported data never overwrites it
        if not piece_rows:
            return "rejected"
        bands_raw = row.get("bands")
        bands = tuple(bands_raw) if isinstance(bands_raw, list) else ()

        pieces: list[ImportPiece] = []
        for piece_row in sorted(piece_rows, key=lambda r: r.get("idx", 0)):
            piece = await self._read_import_piece(zf, names, source_id, original_id, piece_row)
            if piece is None:
                return "rejected"
            pieces.append(piece)

        new_id = await self._cutouts.import_cutout(
            cache_key=cache_key,
            source_id=source_id,
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            size_deg=size_deg,
            bands=bands,
            pieces=pieces,
            publish=False,
        )
        return "imported" if new_id is not None else "skipped"

    async def _read_import_piece(
        self,
        zf: zipfile.ZipFile,
        names: frozenset[str],
        source_id: str,
        cutout_id: str,
        piece_row: dict[str, Any],
    ) -> ImportPiece | None:
        try:
            idx = _row_int(piece_row, "idx")
            fmt = _row_str(piece_row, "format")
            expected_bytes = _row_int(piece_row, "bytes")
        except ValueError:
            return None
        arcname = f"cutouts/{source_id}/{cutout_id}/piece{idx}.{fmt}"
        if arcname not in names:
            return None
        info = zf.getinfo(arcname)
        if info.file_size != expected_bytes:
            return None
        data = await asyncio.to_thread(zf.read, arcname)
        if len(data) != expected_bytes:
            return None
        display_png: bytes | None = None
        if piece_row.get("has_display_png"):
            display_arcname = f"cutouts/{source_id}/{cutout_id}/piece{idx}.png"
            if display_arcname not in names:
                return None
            display_png = await asyncio.to_thread(zf.read, display_arcname)
        return ImportPiece(
            band=piece_row.get("band") if isinstance(piece_row.get("band"), str) else None,
            format=fmt,
            data=data,
            display_png=display_png,
            stretch=piece_row.get("stretch") if isinstance(piece_row.get("stretch"), str) else None,
            width=piece_row.get("width") if isinstance(piece_row.get("width"), int) else None,
            height=piece_row.get("height") if isinstance(piece_row.get("height"), int) else None,
            wcs=_wcs_from_row(piece_row),
        )


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode("utf-8")


__all__ = [
    "EXPORT_FORMAT_VERSION",
    "ExportImportService",
    "ImportCounts",
    "ImportRefusedError",
]
