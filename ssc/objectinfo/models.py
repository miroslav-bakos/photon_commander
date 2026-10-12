# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The shape a resolved lookup returns, independent of SQLite row layout or
the API's own response model — :mod:`ssc.objectinfo.service` builds these,
:mod:`ssc.api.routes.astro` renders them.

``None`` means unknown, never a guessed or blanked-out value (CLAUDE.md: "None
means unknown; it never means zero.") — the same discipline
``ssc.astro.catalog.CatalogObject`` already follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ObjectInfoImage:
    """One Commons file, with *that file's own* licence — never a blanket
    assumption (THIRD_PARTY_NOTICES.md "Click-through info sources", the
    load-bearing finding: licence varies per file, resolved from Commons'
    own ``imageinfo``/``extmetadata``, not guessed from Wikidata's ``P18``
    alone)."""

    commons_title: str  # "File:...", Wikidata's P18 value verbatim
    source_url: str  # the Commons file description page, for the credit link
    thumb_url: str  # what was actually fetched and cached
    width: int | None
    height: int | None
    media_type: str  # "image/jpeg", etc.
    license_short_name: str  # Commons' own extmetadata.LicenseShortName
    artist: str | None  # extmetadata.Artist, HTML stripped
    credit: str | None  # extmetadata.Credit, HTML stripped
    data: bytes


@dataclass(frozen=True, slots=True)
class ObjectInfoResult:
    """A resolved (or deliberately negative) lookup for one catalogue object.

    ``found=False`` means every source was asked and none had an article —
    a real, cacheable answer, not a failure. Distinct from the feature being
    disabled, which :mod:`ssc.objectinfo.service` never turns into a
    :class:`ObjectInfoResult` at all (CLAUDE.md invariant 7: the two must
    never be conflated at the API boundary).
    """

    designation: str
    common_name: str | None
    found: bool
    #: UTC, always (architecture policy invariant 5) — when this row was fetched, or,
    #: for a cache hit, when the fetch that produced it was.
    fetched_at: datetime
    cached: bool
    title: str | None = None
    article_url: str | None = None
    extract: str | None = None
    wikidata_qid: str | None = None
    instance_of: tuple[str, ...] = ()
    discovery_date: str | None = None
    image: ObjectInfoImage | None = None


__all__ = ["ObjectInfoImage", "ObjectInfoResult"]
