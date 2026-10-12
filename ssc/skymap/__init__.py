# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Sky-survey tiles: registry, addressing, cache and prefetch policy.

A top-level package, on purpose. It is not ``api`` plumbing (routers stay
thin) and it is not ``store`` schema (the store owns *how* rows are kept, not
which tiles are worth keeping). What lives here is policy: which surveys may
be drawn and from which originating archive (``surveys``), how a HiPS or
TOAST pyramid is addressed and which tiles cover a cone (``hips``,
``toast``), what is on disk and what may be evicted (``cache``), and how an
offline pack is fetched, persisted and reported (``service``).

It imports ``ssc.store`` and ``ssc.state`` and nothing from ``ssc.seestar`` or
``ssc.alpaca``; the ``.importlinter`` layering contract says so.
"""

from ssc.skymap.bulk_prefetch import (
    BulkCutoutEstimate,
    BulkCutoutRefusedError,
    BulkCutoutRequest,
    BulkCutoutService,
    BulkJobNotFoundError,
)
from ssc.skymap.cutouts import (
    CutoutNotFoundError,
    CutoutRefusedError,
    CutoutService,
)
from ssc.skymap.export_import import (
    EXPORT_FORMAT_VERSION,
    ExportImportService,
    ImportCounts,
    ImportRefusedError,
)
from ssc.skymap.service import (
    PrefetchEstimate,
    PrefetchRefusedError,
    PrefetchRequest,
    SkymapService,
    TileResult,
    TileUnavailableError,
    build_user_agent,
)
from ssc.skymap.surveys import (
    CUTOUT_REGISTRY,
    REGISTRY,
    CutoutSource,
    Survey,
    SurveyKind,
    TileScheme,
    find_cutout_source,
    find_survey,
)

__all__ = [
    "CUTOUT_REGISTRY",
    "EXPORT_FORMAT_VERSION",
    "REGISTRY",
    "BulkCutoutEstimate",
    "BulkCutoutRefusedError",
    "BulkCutoutRequest",
    "BulkCutoutService",
    "BulkJobNotFoundError",
    "CutoutNotFoundError",
    "CutoutRefusedError",
    "CutoutService",
    "CutoutSource",
    "ExportImportService",
    "ImportCounts",
    "ImportRefusedError",
    "PrefetchEstimate",
    "PrefetchRefusedError",
    "PrefetchRequest",
    "SkymapService",
    "Survey",
    "SurveyKind",
    "TileResult",
    "TileScheme",
    "TileUnavailableError",
    "build_user_agent",
    "find_cutout_source",
    "find_survey",
]
