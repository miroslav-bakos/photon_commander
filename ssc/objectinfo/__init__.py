# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Click-through info (M11): a short description and, when available, an
image for a DSO glyph clicked on the sky map.

Proxied server-side only (CLAUDE.md invariant 8) — the browser never calls
Wikipedia, Wikidata or Commons directly. See :mod:`ssc.objectinfo.service`'s
module docstring for why this is a single on/off toggle rather than a BYO
endpoint, and ``THIRD_PARTY_NOTICES.md``'s "Click-through info sources" section
for the licence reading this module implements.
"""

from __future__ import annotations

from ssc.objectinfo.models import ObjectInfoImage, ObjectInfoResult
from ssc.objectinfo.service import ObjectInfoService

__all__ = ["ObjectInfoImage", "ObjectInfoResult", "ObjectInfoService"]
