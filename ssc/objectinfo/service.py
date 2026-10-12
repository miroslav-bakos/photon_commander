# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Click-through info (M11): Wikipedia + Wikidata + Commons, fetch-through,
cached to SQLite so a repeat look works with zero network calls.

**Disabled means disabled.** :meth:`ObjectInfoService.get_info` checks the
feature switch before touching the database *or* the network — the honest
"no source configured" state the router serves is never reached by way of a
half-started fetch (CLAUDE.md invariant 7). No BYO endpoint: the compliance
gate (`THIRD_PARTY_NOTICES.md` "Click-through info sources") cleared exactly one
pairing (Wikipedia + Wikidata + Commons, all keyless), and letting an
operator retarget these calls at an arbitrary host would only move whose
name is on the request — the exact trap `docs/IMPLEMENTATION.md`'s "trap in
BYO" section names. The feature is a single on/off toggle
(:class:`ssc.settings.models.ObjectInfoSettings`), not a settings form.

A transient upstream failure (:class:`~ssc.objectinfo.client.ObjectInfoUpstreamError`)
is never written to the cache as a negative result — only a source that
*answered* and said "nothing here" (no article, no such Commons file) counts
as a real, cacheable ``found=False`` (module docstring of ``client.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ssc.objectinfo.client import (
    ObjectInfoUpstreamError,
    fetch_commons_image_info,
    fetch_image_bytes,
    fetch_wikidata_facts,
    fetch_wikipedia_summary,
)
from ssc.objectinfo.models import ObjectInfoImage, ObjectInfoResult
from ssc.store.database import Database

_log = logging.getLogger(__name__)

_REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _cache_key(designation: str) -> str:
    return designation.strip().casefold()


class ObjectInfoService:
    """One lookup path: cache, else Wikipedia -> Wikidata -> Commons, cached
    on the way out. No background job, no state-snapshot section — unlike
    ``ssc.skymap.cutouts.CutoutService``, every upstream call here is a
    single small JSON or thumbnail fetch, not a multi-minute FITS pull, so a
    plain awaited round trip in the request handler is the whole feature.
    """

    def __init__(
        self,
        database: Database,
        *,
        enabled: Callable[[], bool],
        user_agent: Callable[[], str],
        http: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] = now_utc,
    ) -> None:
        self._db = database
        self._enabled = enabled
        self._user_agent = user_agent
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
        self._now = now

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def enabled(self) -> bool:
        return self._enabled()

    async def get_info(self, designation: str, common_name: str | None) -> ObjectInfoResult | None:
        """``None`` means the feature is off — checked first, before any
        cache read or network call, so a disabled feature is provably inert.
        """
        if not self._enabled():
            return None
        key = _cache_key(designation)
        row = await self._db.fetch_one("SELECT * FROM object_info WHERE cache_key = ?", (key,))
        if row is not None:
            return _result_of_row(row)
        result = await self._resolve(designation, common_name)
        await self._store(key, result)
        return result

    async def get_image_bytes(self, designation: str) -> tuple[bytes, str] | None:
        """The cached image's bytes and media type, or ``None`` when this
        object has no cached image (never fetched live from this method —
        an image is only ever fetched once, as part of :meth:`get_info`)."""
        key = _cache_key(designation)
        row = await self._db.fetch_one(
            "SELECT image_bytes, image_media_type FROM object_info WHERE cache_key = ?", (key,)
        )
        if row is None or row["image_bytes"] is None:
            return None
        media_type = row["image_media_type"]
        return bytes(row["image_bytes"]), str(
            media_type
        ) if media_type else "application/octet-stream"

    # -- resolving ------------------------------------------------------------ #

    async def _resolve(self, designation: str, common_name: str | None) -> ObjectInfoResult:
        candidates = list(dict.fromkeys([c for c in (common_name, designation) if c]))
        summary = None
        for candidate in candidates:
            summary = await fetch_wikipedia_summary(self._http, self._user_agent, candidate)
            if summary is not None:
                break
        if summary is None:
            return ObjectInfoResult(
                designation=designation,
                common_name=common_name,
                found=False,
                fetched_at=self._now(),
                cached=False,
            )

        instance_of: tuple[str, ...] = ()
        discovery_date: str | None = None
        image: ObjectInfoImage | None = None
        if summary.wikidata_qid is not None:
            facts = await fetch_wikidata_facts(self._http, self._user_agent, summary.wikidata_qid)
            instance_of = facts.instance_of
            discovery_date = facts.discovery_date
            if facts.image_commons_title is not None:
                image = await self._resolve_image(facts.image_commons_title)

        return ObjectInfoResult(
            designation=designation,
            common_name=common_name,
            found=True,
            fetched_at=self._now(),
            cached=False,
            title=summary.title,
            article_url=summary.article_url,
            extract=summary.extract,
            wikidata_qid=summary.wikidata_qid,
            instance_of=instance_of,
            discovery_date=discovery_date,
            image=image,
        )

    async def _resolve_image(self, commons_title: str) -> ObjectInfoImage | None:
        """The named file's own licence, then its bytes. Any upstream
        hiccup here loses only the image, never the text lookup already in
        hand — logged, not silently dropped (CLAUDE.md: no bare except)."""
        try:
            info = await fetch_commons_image_info(self._http, self._user_agent, commons_title)
            if info is None:
                return None
            data, media_type = await fetch_image_bytes(self._http, self._user_agent, info.thumb_url)
        except ObjectInfoUpstreamError as exc:
            _log.warning("object-info image %s could not be fetched: %s", commons_title, exc)
            return None
        return ObjectInfoImage(
            commons_title=commons_title,
            source_url=info.source_url,
            thumb_url=info.thumb_url,
            width=info.width,
            height=info.height,
            media_type=media_type,
            license_short_name=info.license_short_name,
            artist=info.artist,
            credit=info.credit,
            data=data,
        )

    # -- storage --------------------------------------------------------------- #

    async def _store(self, key: str, result: ObjectInfoResult) -> None:
        image = result.image
        await self._db.execute(
            """
            INSERT INTO object_info (
                cache_key, designation, common_name, found, title, article_url, extract,
                wikidata_qid, instance_of, discovery_date, image_commons_title,
                image_source_url, image_thumb_url, image_width, image_height,
                image_media_type, image_license_short_name, image_artist, image_credit,
                image_bytes, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (cache_key) DO UPDATE SET
                designation = excluded.designation,
                common_name = excluded.common_name,
                found = excluded.found,
                title = excluded.title,
                article_url = excluded.article_url,
                extract = excluded.extract,
                wikidata_qid = excluded.wikidata_qid,
                instance_of = excluded.instance_of,
                discovery_date = excluded.discovery_date,
                image_commons_title = excluded.image_commons_title,
                image_source_url = excluded.image_source_url,
                image_thumb_url = excluded.image_thumb_url,
                image_width = excluded.image_width,
                image_height = excluded.image_height,
                image_media_type = excluded.image_media_type,
                image_license_short_name = excluded.image_license_short_name,
                image_artist = excluded.image_artist,
                image_credit = excluded.image_credit,
                image_bytes = excluded.image_bytes,
                fetched_at = excluded.fetched_at
            """,
            (
                key,
                result.designation,
                result.common_name,
                1 if result.found else 0,
                result.title,
                result.article_url,
                result.extract,
                result.wikidata_qid,
                json.dumps(list(result.instance_of)) if result.instance_of else None,
                result.discovery_date,
                None if image is None else image.commons_title,
                None if image is None else image.source_url,
                None if image is None else image.thumb_url,
                None if image is None else image.width,
                None if image is None else image.height,
                None if image is None else image.media_type,
                None if image is None else image.license_short_name,
                None if image is None else image.artist,
                None if image is None else image.credit,
                None if image is None else image.data,
                result.fetched_at.isoformat(),
            ),
        )


def _result_of_row(row: Any) -> ObjectInfoResult:
    image: ObjectInfoImage | None = None
    if row["image_commons_title"] is not None:
        image = ObjectInfoImage(
            commons_title=str(row["image_commons_title"]),
            source_url=str(row["image_source_url"]),
            thumb_url=str(row["image_thumb_url"]),
            width=None if row["image_width"] is None else int(row["image_width"]),
            height=None if row["image_height"] is None else int(row["image_height"]),
            media_type=str(row["image_media_type"]),
            license_short_name=str(row["image_license_short_name"]),
            artist=None if row["image_artist"] is None else str(row["image_artist"]),
            credit=None if row["image_credit"] is None else str(row["image_credit"]),
            data=bytes(row["image_bytes"]) if row["image_bytes"] is not None else b"",
        )
    instance_of_json = row["instance_of"]
    instance_of = tuple(json.loads(instance_of_json)) if instance_of_json else ()
    return ObjectInfoResult(
        designation=str(row["designation"]),
        common_name=None if row["common_name"] is None else str(row["common_name"]),
        found=bool(row["found"]),
        fetched_at=datetime.fromisoformat(str(row["fetched_at"])),
        cached=True,
        title=None if row["title"] is None else str(row["title"]),
        article_url=None if row["article_url"] is None else str(row["article_url"]),
        extract=None if row["extract"] is None else str(row["extract"]),
        wikidata_qid=None if row["wikidata_qid"] is None else str(row["wikidata_qid"]),
        instance_of=instance_of,
        discovery_date=None if row["discovery_date"] is None else str(row["discovery_date"]),
        image=image,
    )


__all__ = ["ObjectInfoService", "now_utc"]
