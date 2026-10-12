# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Wikipedia + Wikidata + Commons, called exactly the way
``THIRD_PARTY_NOTICES.md``'s "Click-through info sources — gate reading of
2026-09-08" cleared them:

* ``en.wikipedia.org/api/rest_v1/page/summary/{title}`` for the text extract
  and (when the gate found it) the ``wikibase_item`` Q-id.
* ``www.wikidata.org/w/api.php`` (``action=wbgetentities``) for the
  structured facts and the ``P18`` image claim.
* ``commons.wikimedia.org/w/api.php`` (``action=query&prop=imageinfo``) for
  *that specific file's own* licence and credit — never a blanket
  assumption, per the gate's load-bearing finding.
* ``upload.wikimedia.org`` for the actual image bytes named by the Commons
  lookup above.

Every request carries the identifying ``User-Agent`` invariant 8 and the
Wikimedia User-Agent policy both require, and every URL this module follows
is checked against the host it claims to be before it is fetched — the same
discipline ``ssc.skymap.cutouts._get`` applies to archive redirects, for the
same reason: a response must never make ``ssc-core`` reach an arbitrary host.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

WIKIPEDIA_HOST: Final = "en.wikipedia.org"
WIKIDATA_HOST: Final = "www.wikidata.org"
COMMONS_HOST: Final = "commons.wikimedia.org"
UPLOAD_HOST: Final = "upload.wikimedia.org"

#: Commons' own thumbnail width cap this module asks for — enough to show in
#: an info panel, small enough that a cache entry stays modest.
THUMBNAIL_WIDTH_PX: Final = 800

_REQUEST_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
_TAG_RE = re.compile(r"<[^>]+>")


class ObjectInfoUpstreamError(Exception):
    """Wikipedia/Wikidata/Commons could not be reached or answered oddly.

    Never turned into a cached negative result — a network hiccup is not the
    same fact as "no article exists" (CLAUDE.md invariant 7)."""


def _strip_html(value: str | None) -> str | None:
    """Commons' ``extmetadata`` fields (``Artist``, ``Credit``) are HTML
    fragments; this API returns plain text, so tags are stripped rather than
    passed through for a caller to render unescaped."""
    if value is None:
        return None
    text = _TAG_RE.sub("", value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class WikipediaSummary:
    title: str
    article_url: str
    extract: str
    wikidata_qid: str | None


@dataclass(frozen=True, slots=True)
class WikidataFacts:
    instance_of: tuple[str, ...]
    discovery_date: str | None
    image_commons_title: str | None  # P18 value, "File:..." not yet resolved


@dataclass(frozen=True, slots=True)
class CommonsImageInfo:
    source_url: str
    thumb_url: str
    width: int | None
    height: int | None
    license_short_name: str
    artist: str | None
    credit: str | None


async def _get_json(
    http: httpx.AsyncClient,
    url: str,
    *,
    host: str,
    user_agent: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """``None`` on a 404 (Wikipedia's own "no such article" answer); raises
    :class:`ObjectInfoUpstreamError` for anything else that is not a clean
    200, so a transient failure is never mistaken for "no article exists"."""
    actual_host = urlsplit(url).hostname
    if actual_host != host:
        raise ObjectInfoUpstreamError(
            f"refusing to fetch from {actual_host!r}; {host!r} is the only host cleared here"
        )
    try:
        response = await http.get(
            url, params=params, headers={"User-Agent": user_agent}, timeout=_REQUEST_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise ObjectInfoUpstreamError(f"{host}: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ObjectInfoUpstreamError(f"{host} answered {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ObjectInfoUpstreamError(f"{host} did not return JSON: {exc}") from exc
    return data if isinstance(data, dict) else None


async def fetch_wikipedia_summary(
    http: httpx.AsyncClient, user_agent: Callable[[], str], title: str
) -> WikipediaSummary | None:
    """``page/summary/{title}``: ``None`` for a missing article, a
    disambiguation page, or one with no usable extract — every one of those
    is a legitimate "nothing here", not an error."""
    encoded = title.strip().replace(" ", "_")
    url = f"https://{WIKIPEDIA_HOST}/api/rest_v1/page/summary/{encoded}"
    data = await _get_json(http, url, host=WIKIPEDIA_HOST, user_agent=user_agent())
    if data is None:
        return None
    if data.get("type") == "disambiguation":
        return None
    extract = data.get("extract")
    if not isinstance(extract, str) or not extract.strip():
        return None
    content_urls = data.get("content_urls", {})
    desktop = content_urls.get("desktop", {}) if isinstance(content_urls, dict) else {}
    article_url = desktop.get("page") if isinstance(desktop, dict) else None
    resolved_title = data.get("title")
    if not isinstance(article_url, str) or not isinstance(resolved_title, str):
        return None
    qid = data.get("wikibase_item")
    return WikipediaSummary(
        title=resolved_title,
        article_url=article_url,
        extract=extract.strip(),
        wikidata_qid=qid if isinstance(qid, str) else None,
    )


def _wikidata_time_label(snak: dict[str, Any]) -> str | None:
    """A Wikidata ``time`` datavalue (``+1923-01-01T00:00:00Z``, a
    ``precision`` code), reduced to the coarsest label its own precision
    claims — usually just a year for a discovery date. Never invents a month
    or day precision the source did not assert."""
    datavalue = snak.get("datavalue")
    if not isinstance(datavalue, dict) or datavalue.get("type") != "time":
        return None
    value = datavalue.get("value")
    if not isinstance(value, dict):
        return None
    raw = value.get("time")
    precision = value.get("precision")
    if not isinstance(raw, str):
        return None
    # "+1923-01-01T00:00:00Z" -> "1923-01-01"; the leading sign marks CE/BCE,
    # dropped for the CE case this app's astronomical catalogue always is.
    body = raw.lstrip("+").split("T", 1)[0]
    if precision == 9:  # year only
        return body.split("-", 1)[0]
    if precision == 10:  # year-month
        year, month, _ = body.split("-")
        return f"{year}-{month}"
    return body


async def fetch_wikidata_facts(
    http: httpx.AsyncClient, user_agent: Callable[[], str], qid: str
) -> WikidataFacts:
    """``wbgetentities`` for one entity's claims, plus a second batched call
    to resolve any ``P31`` (instance of) target Q-ids to English labels — at
    most two requests total, kept modest per the parent brief: no attempt at
    distance (``P2583``, unit conversion this feature does not need)."""
    url = f"https://{WIKIDATA_HOST}/w/api.php"
    data = await _get_json(
        http,
        url,
        host=WIKIDATA_HOST,
        user_agent=user_agent(),
        params={
            "action": "wbgetentities",
            "ids": qid,
            "props": "claims",
            "format": "json",
        },
    )
    entities = (data or {}).get("entities", {}) if data else {}
    entity = entities.get(qid, {}) if isinstance(entities, dict) else {}
    claims = entity.get("claims", {}) if isinstance(entity, dict) else {}
    if not isinstance(claims, dict):
        return WikidataFacts(instance_of=(), discovery_date=None, image_commons_title=None)

    image_title: str | None = None
    for claim in claims.get("P18", []):
        snak = claim.get("mainsnak", {}) if isinstance(claim, dict) else {}
        datavalue = snak.get("datavalue", {}) if isinstance(snak, dict) else {}
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if isinstance(value, str) and value:
            image_title = f"File:{value}"
            break

    discovery_date: str | None = None
    for claim in claims.get("P575", []):
        snak = claim.get("mainsnak", {}) if isinstance(claim, dict) else {}
        if isinstance(snak, dict):
            label = _wikidata_time_label(snak)
            if label is not None:
                discovery_date = label
                break

    instance_of_qids: list[str] = []
    for claim in claims.get("P31", []):
        snak = claim.get("mainsnak", {}) if isinstance(claim, dict) else {}
        datavalue = snak.get("datavalue", {}) if isinstance(snak, dict) else {}
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        target_id = value.get("id") if isinstance(value, dict) else None
        if isinstance(target_id, str):
            instance_of_qids.append(target_id)

    instance_of = await _labels_for(http, user_agent, instance_of_qids) if instance_of_qids else ()
    return WikidataFacts(
        instance_of=instance_of, discovery_date=discovery_date, image_commons_title=image_title
    )


async def _labels_for(
    http: httpx.AsyncClient, user_agent: Callable[[], str], qids: list[str]
) -> tuple[str, ...]:
    url = f"https://{WIKIDATA_HOST}/w/api.php"
    data = await _get_json(
        http,
        url,
        host=WIKIDATA_HOST,
        user_agent=user_agent(),
        params={
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "labels",
            "languages": "en",
            "format": "json",
        },
    )
    entities = (data or {}).get("entities", {}) if data else {}
    labels: list[str] = []
    if isinstance(entities, dict):
        for entity_id in qids:
            entity = entities.get(entity_id, {})
            english = entity.get("labels", {}).get("en", {}) if isinstance(entity, dict) else {}
            value = english.get("value") if isinstance(english, dict) else None
            if isinstance(value, str):
                labels.append(value)
    return tuple(labels)


async def fetch_commons_image_info(
    http: httpx.AsyncClient, user_agent: Callable[[], str], commons_title: str
) -> CommonsImageInfo | None:
    """The named file's *own* licence and credit — the gate's load-bearing
    requirement. ``None`` when Commons has no such file (a stale ``P18``
    pointing at a deleted or renamed file, which does happen)."""
    url = f"https://{COMMONS_HOST}/w/api.php"
    data = await _get_json(
        http,
        url,
        host=COMMONS_HOST,
        user_agent=user_agent(),
        params={
            "action": "query",
            "titles": commons_title,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": str(THUMBNAIL_WIDTH_PX),
            "format": "json",
        },
    )
    if data is None:
        return None
    pages = data.get("query", {}).get("pages", {})
    if not isinstance(pages, dict) or not pages:
        return None
    page = next(iter(pages.values()))
    if not isinstance(page, dict) or "missing" in page:
        return None
    infos = page.get("imageinfo")
    if not isinstance(infos, list) or not infos:
        return None
    info = infos[0]
    if not isinstance(info, dict):
        return None
    thumb_url = info.get("thumburl") or info.get("url")
    source_url = info.get("descriptionurl")
    if not isinstance(thumb_url, str) or not isinstance(source_url, str):
        return None
    extmetadata = info.get("extmetadata", {})
    if not isinstance(extmetadata, dict):
        extmetadata = {}

    def field_value(name: str) -> str | None:
        entry = extmetadata.get(name)
        value = entry.get("value") if isinstance(entry, dict) else None
        return value if isinstance(value, str) else None

    license_short_name = field_value("LicenseShortName") or "licence unknown"
    width = info.get("thumbwidth") if isinstance(info.get("thumbwidth"), int) else None
    height = info.get("thumbheight") if isinstance(info.get("thumbheight"), int) else None
    return CommonsImageInfo(
        source_url=source_url,
        thumb_url=thumb_url,
        width=width,
        height=height,
        license_short_name=license_short_name,
        artist=_strip_html(field_value("Artist")),
        credit=_strip_html(field_value("Credit")),
    )


async def fetch_image_bytes(
    http: httpx.AsyncClient, user_agent: Callable[[], str], url: str
) -> tuple[bytes, str]:
    """The actual bytes named by :class:`CommonsImageInfo.thumb_url`, plus
    the response's own content type."""
    actual_host = urlsplit(url).hostname
    if actual_host != UPLOAD_HOST:
        raise ObjectInfoUpstreamError(
            f"refusing to fetch image bytes from {actual_host!r}; only {UPLOAD_HOST!r} is cleared"
        )
    try:
        response = await http.get(
            url, headers={"User-Agent": user_agent()}, timeout=_REQUEST_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise ObjectInfoUpstreamError(f"{UPLOAD_HOST}: {exc}") from exc
    if response.status_code != 200:
        raise ObjectInfoUpstreamError(f"{UPLOAD_HOST} answered {response.status_code}")
    media_type = (
        response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
    )
    return response.content, media_type


__all__ = [
    "COMMONS_HOST",
    "THUMBNAIL_WIDTH_PX",
    "UPLOAD_HOST",
    "WIKIDATA_HOST",
    "WIKIPEDIA_HOST",
    "CommonsImageInfo",
    "ObjectInfoUpstreamError",
    "WikidataFacts",
    "WikipediaSummary",
    "fetch_commons_image_info",
    "fetch_image_bytes",
    "fetch_wikidata_facts",
    "fetch_wikipedia_summary",
]
