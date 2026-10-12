# SPDX-License-Identifier: GPL-3.0-only

"""Match calibration frames without hiding incompatible metadata.

The matcher is deliberately independent of FITS I/O and the capture store. A
caller reads the authoritative FITS/catalogue metadata once, then passes these
small immutable records here. Gain and exposure are hard requirements; when an
exact CCD temperature is unavailable, the nearest compatible dark is returned
with its temperature delta so the caller can surface that mismatch in the
stack result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CalibrationFrame:
    """Metadata needed to match one dark against a light frame."""

    id: int
    path: Path
    gain: int | None
    exposure_s: float | None
    ccd_temp_c: float | None


@dataclass(frozen=True, slots=True)
class CalibrationMatch:
    """The selected dark and the provenance of its temperature match."""

    dark: CalibrationFrame
    temperature_delta_c: float | None

    @property
    def exact_temperature(self) -> bool:
        return self.temperature_delta_c == 0.0


class CalibrationMatchError(ValueError):
    """No dark satisfies the light frame's gain and exposure requirements."""


def match_dark(
    light: CalibrationFrame,
    darks: tuple[CalibrationFrame, ...] | list[CalibrationFrame],
) -> CalibrationMatch:
    """Choose a gain/exposure-compatible dark nearest in CCD temperature.

    Missing gain, exposure or temperature is never treated as a wildcard.
    Temperature is used as the tie-breaker only after exact gain and exposure
    equality; a returned non-zero delta is an explicit calibration warning for
    the caller to display or record beside the stack.
    """

    compatible = [
        dark
        for dark in darks
        if dark.gain is not None
        and dark.exposure_s is not None
        and light.gain is not None
        and light.exposure_s is not None
        and dark.gain == light.gain
        and dark.exposure_s == light.exposure_s
    ]
    if not compatible:
        raise CalibrationMatchError(
            "no dark matches the light frame's gain and exposure; refusing to apply a wrong calibration"
        )

    light_temp = light.ccd_temp_c
    if light_temp is None:
        raise CalibrationMatchError(
            "light frame has no CCD-TEMP; refusing to choose a temperature-mismatched dark"
        )
    with_temperature = [dark for dark in compatible if dark.ccd_temp_c is not None]
    if not with_temperature:
        raise CalibrationMatchError(
            "compatible darks have no CCD-TEMP; refusing to choose an unknown-temperature dark"
        )
    selected = min(
        with_temperature,
        key=lambda dark: (abs(dark.ccd_temp_c - light_temp), dark.id),  # type: ignore[operator]
    )
    assert selected.ccd_temp_c is not None
    return CalibrationMatch(
        dark=selected,
        temperature_delta_c=selected.ccd_temp_c - light_temp,
    )


__all__ = ["CalibrationFrame", "CalibrationMatch", "CalibrationMatchError", "match_dark"]
