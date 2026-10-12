# SPDX-License-Identifier: GPL-3.0-only
"""Move authored wall-clock times together to a new observing night."""

from datetime import date, timedelta

from ssc.plan.models import AbsoluteTimeSpec, Plan


def apply_night(document: Plan, night_date: date) -> Plan:
    """Preserve local clock readings and their offsets from the first night.

    Infer the source from authored absolute starts rather than calendar
    metadata, which may already have changed in the date picker. Before
    noon belongs to the previous observing night. Event/relative starts and
    DST folds remain untouched; the compiler validates the selected zone.
    """
    starts = [item.start.local_datetime for item in document.items
              if isinstance(item.start, AbsoluteTimeSpec)]
    if not starts:
        return document
    first = min(starts)
    source_night = first.date() - timedelta(days=int(first.hour < 12))
    delta = night_date - source_night
    return document.model_copy(update={"items": tuple(
        item.model_copy(update={"start": item.start.model_copy(update={
            "local_datetime": item.start.local_datetime + delta,
        })}) if isinstance(item.start, AbsoluteTimeSpec) else item
        for item in document.items
    )})
