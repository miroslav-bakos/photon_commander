# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Merge policy: primary plus ranked fallbacks, provenance preserved per row.

DESIGN.md §6: "Met.no primary, plus any user-keyed providers as ranked
fallbacks, per-row provenance visible." Merging happens at **row**
granularity, never at field granularity — a timestamp is filled entirely from
one provider's point, never stitched together from two. Splicing a cloud
figure from one provider onto a humidity figure from another would erase the
`source` provenance the UI depends on and manufacture a reading nobody
actually produced.
"""

from __future__ import annotations

from datetime import datetime

from ssc.weather.base import ForecastPoint, ForecastSeries


def merge_series(*series_by_rank: ForecastSeries) -> ForecastSeries:
    """Combine series in rank order: ``series_by_rank[0]`` is primary.

    For each timestamp present in any input, the highest-ranked series that
    has a point at that timestamp wins the whole row. Timestamps absent from
    the primary but present in a fallback are still included — a fallback
    covering a gap in the primary's series is exactly what "ranked fallback"
    is for.

    ``fetched_at`` on the result is the earliest among the inputs (the
    merged series is only as fresh as its stalest contributor).
    ``stale_since`` is taken from the **primary** series only: DESIGN.md
    names Met.no as the one authoritative default, so its own staleness is
    what drives the UI's staleness badge, even if a lower-ranked fallback
    happens to be fresh.
    """
    if not series_by_rank:
        raise ValueError("merge_series requires at least one series")

    by_time: dict[datetime, ForecastPoint] = {}
    for series in reversed(series_by_rank):  # lowest rank first, so higher ranks overwrite
        for point in series.points:
            by_time[point.t] = point
    points = tuple(sorted(by_time.values(), key=lambda p: p.t))

    primary = series_by_rank[0]
    fetched_at = min(s.fetched_at for s in series_by_rank)
    return ForecastSeries(points=points, fetched_at=fetched_at, stale_since=primary.stale_since)


__all__ = ["merge_series"]
