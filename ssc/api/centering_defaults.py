# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Shared pointing-accuracy defaults for every closed-loop centering entry point."""

from typing import Final

DEFAULT_CENTERING_TOLERANCE_ARCSEC: Final = 30.0
DEFAULT_CENTERING_TOLERANCE_ARCMIN: Final = DEFAULT_CENTERING_TOLERANCE_ARCSEC / 60.0
# Scheduled mosaic transitions favor reliable unattended acquisition over the
# stricter interactive default.  One arcminute is still negligible against a
# Seestar field while avoiding correction slews below the mount's motion floor.
DEFAULT_SCHEDULED_CENTERING_TOLERANCE_ARCSEC: Final = 60.0
DEFAULT_SCHEDULED_CENTERING_TOLERANCE_ARCMIN: Final = (
    DEFAULT_SCHEDULED_CENTERING_TOLERANCE_ARCSEC / 60.0
)
__all__ = [
    "DEFAULT_CENTERING_TOLERANCE_ARCMIN",
    "DEFAULT_CENTERING_TOLERANCE_ARCSEC",
    "DEFAULT_SCHEDULED_CENTERING_TOLERANCE_ARCMIN",
    "DEFAULT_SCHEDULED_CENTERING_TOLERANCE_ARCSEC",
]
