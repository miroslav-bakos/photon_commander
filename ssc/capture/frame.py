# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The input contract this dispatch defines for the capture pipeline.

DESIGN.md §9.5.1/§9.5.2: SSC drives the exposure over Alpaca and pulls the
result as ``Camera.ImageArray``'s ImageBytes transfer, decoded to a numpy
array by the transport layer. This module is that boundary: a frame arrives
here as a plain array plus every Alpaca/plan property that was in force at
the moment of exposure, read once by the caller and handed over as data —
never as a live handle this module could poll again later (CLAUDE.md
invariant 7: a value is read once, at exposure time, or it is absent).

Deliberately does not import ``ssc.client``, ``ssc.alpaca`` or
``ssc.seestar`` — nothing in ``ssc.capture`` talks to a device (DESIGN.md
§9.5.5). A concurrent ``alpaca-driver`` dispatch owns the code that decodes
ImageBytes and reads these Alpaca properties; this is the shape it hands off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray


class FrameType(StrEnum):
    """What an exposure *is*, chosen by the user before capture — never
    inferred from ``light``/``dark`` alone, because a dark and a flat are
    both shutter-closed-or-not-relevant-to-ASCOM but land in different
    folders and mean different things to a stacker.

    All four are defined now, even though today's capture UI only offers
    :attr:`LIGHT` and :attr:`DARK` (owner, 2026-09-10: "so far lights and
    darks, in future flats via wizard and biases too"). Flats need a
    procedure (panel or sky, per optic) and biases a zero-length exposure —
    both are UI/workflow gaps, not gaps in what a frame's *type* can be, so
    defining the closed set up front means the catalogue schema and the
    folder layout never need a migration when flats/biases arrive.
    """

    LIGHT = "light"
    DARK = "dark"
    FLAT = "flat"
    BIAS = "bias"


@dataclass(frozen=True, slots=True)
class FrameMetadata:
    """Every FITS header field SSC has to author itself (DESIGN.md §9.5.2's
    header/source table), each read once at exposure time.

    ``None`` means the property could not be read at that moment. It is
    written into the FITS header as *absent*, never as zero and never
    back-filled from a later poll (CLAUDE.md invariant 7) — see
    ``ssc.capture.fits``.

    ``ra_deg``/``dec_deg`` are astrometric ICRS/J2000 coordinates already
    converted to decimal degrees. ASCOM's ``Telescope.RightAscension`` reports
    decimal *hours* (0-24) in the mount's declared ``EquatorialSystem``; the
    caller supplying this dataclass is responsible for both the ``* 15`` unit
    conversion and the frame conversion. This module therefore writes a
    truthful, interoperable ``RADESYS=ICRS``/``EQUINOX=2000`` declaration
    whenever the complete pair is present.
    """

    date_obs: datetime | None
    exposure_s: float | None
    gain: int | None
    ccd_temp_c: float | None
    #: ``Camera.BayerOffsetX``/``BayerOffsetY`` — the actual phase. Never
    #: derived from ``Camera.SensorType``'s enum name, which names a family
    #: (mono / Bayer-RGGB-style), not this sensor's phase. ``ssc.capture.fits``
    #: reads ``bayer_offset_x`` as a shift along ``pixels``' column axis and
    #: ``bayer_offset_y`` along its row axis. The ImageBytes transport's
    #: ``array[x, y]`` is transposed before becoming a :class:`RawFrame`, so
    #: these map naturally to FITS/Numpy column X and row Y.
    bayer_offset_x: int | None
    bayer_offset_y: int | None
    pixel_size_um: float | None
    ra_deg: float | None
    dec_deg: float | None
    site_lat_deg: float | None
    site_lon_deg: float | None
    filter_name: str | None
    #: The plan item's target. The device never knows this — SSC is the only
    #: party that does (DESIGN.md §9.5.2).
    object_name: str | None
    #: Chosen by the user at capture time (see :class:`FrameType`), never
    #: inferred from the ASCOM ``Light`` flag: a dark and a bias are both
    #: ``Light=False``, and a flat is typically ``Light=True`` despite not
    #: being a light frame at all. The mapping from this to ASCOM's ``Light``
    #: bool lives in ``ssc.api.capture_service`` (the caller that knows
    #: ASCOM), not here. Always known — this is SSC's own classification,
    #: not a device read.
    frame_type: FrameType
    focal_length_mm: float | None
    telescope_name: str | None
    #: SSC's own version. Always known — this is the one field on this
    #: dataclass that is not optional.
    ssc_version: str
    #: The device firmware SSC captured against, if the transport could read
    #: it. Recorded because a library outlives any one firmware or SSC
    #: version (DESIGN.md §9.5.2).
    device_firmware: str | None
    #: The selected profile optic for calibration frames, when known.
    optic_id: str | None = None
    #: How a flat was illuminated: a user-selected panel or twilight sky.
    flat_source: str | None = None
    #: Additional capture-time properties read from the Seestar's Alpaca
    #: devices. These mirror the diagnostic cards in Seestar-authored FITS
    #: files; each remains absent when its property cannot be read.
    date_end: datetime | None = None
    bin_x: int | None = None
    bin_y: int | None = None
    subframe_x: int | None = None
    subframe_y: int | None = None
    pixel_size_y_um: float | None = None
    sensor_name: str | None = None
    #: Measured ASCOM SensorType: 0=mono, 2=Bayer family (not its phase).
    sensor_type: int | None = None
    #: New SSC acquisitions attempted sensor/CFA reads. Distinguishes missing
    #: provenance from legacy/imported mono frames with no such metadata.
    sensor_metadata_checked: bool = False
    camera_offset: int | None = None
    max_adu: float | None = None
    readout_mode: int | None = None
    focuser_position: int | None = None
    focuser_temperature_c: float | None = None
    alignment_mode: int | None = None
    aperture_diameter_mm: float | None = None
    focal_ratio: float | None = None
    wide_camera: bool | None = None
    stack_count: int | None = None
    total_exposure_s: float | None = None
    #: True when ``date_obs``/``exposure_s`` came from SSC's own record of the
    #: exposure it commanded rather than from ``Camera.LastExposureStartTime``
    #: /``LastExposureDuration``, because the device does not implement them
    #: (the S30 Pro does not, observed 2026-09-10). Written into the header as
    #: a comment so a frame never claims device-measured timing it did not
    #: have -- provenance recorded, not the value withheld.
    timing_from_ssc: bool = False
    #: Detector-position block token; changes after successful dither or field centering.
    dither_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class RawFrame:
    """One exposure, decoded from ``Camera.ImageArray``'s ImageBytes.

    ``pixels`` carries its own rank, element type and dimensions — a single
    plane, ``(height, width)``, raw/mosaiced sensor output. ImageBytes itself
    carries no metadata at all, which is why every other field a FITS header
    needs travels separately, on :class:`FrameMetadata`.
    """

    pixels: NDArray[np.generic]
    metadata: FrameMetadata


__all__ = ["FrameMetadata", "FrameType", "RawFrame"]
