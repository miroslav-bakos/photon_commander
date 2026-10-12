# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The capture folder's own templated layout, beneath the configured root
(``ssc.settings.capture_folder``).

The root is validated once (exists, writable, has room); everything below it
is SSC's own — created on demand from a user-configurable template, default
``%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%``. Every placeholder this module knows
about is generated from :class:`ssc.capture.frame.FrameMetadata`'s own
fields, plus derived tags such as ``%DATE%``, ``%DATETIME%`` and
``%SEQUENCE%`` (see :data:`PATH_TAGS`). This is deliberately the *one* source:
``tests/capture/test_pathtemplate.py`` asserts every :class:`~ssc.capture.frame.FrameMetadata`
field is covered by exactly one tag, so a header field added without a
matching tag fails a test rather than silently leaving the Settings UI
stale.

**Unknown is never a value.** Every tag renders the literal ``unknown``
for a field that could not be read, never an empty path segment and never
``"None"`` — ``~/Pictures/SSC//2026-12-28/`` is a different directory from
the one intended.

**Numeric tags get one documented rendering each**, because a raw
``str(float)`` makes a poor directory name: ``%EXPOSURE%`` is always
fixed-notation with an ``s`` suffix (never ``3e-05``), ``%CCDTEMP%`` is 3 d.p., and the
angle/coordinate tags are 4 d.p. with an explicit sign on anything that can
be negative (declination, site longitude/latitude) so ``+`` and ``-`` never
collide in a sorted directory listing.

Validation happens once, at set time (:func:`validate_capture_folder_template`,
called from ``ssc.settings.store.SettingsStore.set_capture_folder_template``):
an unknown placeholder, a template that escapes the root via ``..``, or one
that renders to an absolute path is refused with a message naming the
problem, checked by rendering the template against a synthetic example frame
— discovering a bad template when the first real frame fails at 2 a.m. is
the outcome this is designed against.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from ssc.capture.frame import FrameMetadata, FrameType

#: The default a fresh install starts with (``ssc.settings.models.CaptureSettings``).
DEFAULT_CAPTURE_FOLDER_TEMPLATE: Final = "%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%"
PREVIOUS_DEFAULT_CAPTURE_FILENAME_TEMPLATE: Final = (
    "%TARGET%_%FILTER%_%DATETIME%_%SEQUENCE%"
)
INTERIM_DEFAULT_CAPTURE_FILENAME_TEMPLATE: Final = (
    "%IMAGETYPE%_%TARGET%_%EXPOSURE%_%FILTER%_%DATETIME%_%SEQUENCE%"
)
LEGACY_DEFAULT_CAPTURE_FILENAME_TEMPLATES: Final = frozenset(
    {
        PREVIOUS_DEFAULT_CAPTURE_FILENAME_TEMPLATE,
        INTERIM_DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    }
)
DEFAULT_CAPTURE_FILENAME_TEMPLATE: Final = (
    "%IMAGETYPE%_%TARGET%_%EXPOSURE%_%CCDTEMP%_%FILTER%_%DATETIME%_%SEQUENCE%"
)

_PLACEHOLDER_RE: Final = re.compile(r"%([A-Z0-9_]+)%")

#: ``%IMAGETYPE%`` renders plural, matching the owner's own wording
#: ("lights and darks, in future flats ... and biases too").
_IMAGETYPE_PLURAL: Final[dict[FrameType, str]] = {
    FrameType.LIGHT: "lights",
    FrameType.DARK: "darks",
    FrameType.FLAT: "flats",
    FrameType.BIAS: "biases",
}

#: The local wall-clock time a capture night is anchored to when deriving
#: ``%DATEOFCAPTURE%``. Mirrors ``ssc.scheduler.compiler``'s own
#: ``_NIGHT_ANCHOR_LOCAL_TIME`` and ``ssc.api.plans_service.current_night_date``
#: — the same "which calendar date is 'tonight'" question, answered the same
#: way. A midnight boundary would split one night's frames across two
#: folders at exactly the hour a session is usually in full swing; noon does
#: not, and it sits clear of both DST transition windows (architecture policy
#: invariant 5: no wall-clock rule may depend on luck around a transition).
_NIGHT_ANCHOR_LOCAL_TIME: Final = time(12, 0)


class PathTemplateError(ValueError):
    """A capture folder template is malformed. The message names the exact
    problem — unknown placeholder, escaping ``..``, or an absolute path —
    per this module's own docstring."""


@dataclass(frozen=True, slots=True)
class _Context:
    metadata: FrameMetadata
    date_obs: datetime
    sequence: int
    tz: ZoneInfo


@dataclass(frozen=True, slots=True)
class PathTag:
    """One ``%PLACEHOLDER%`` a capture folder template may use."""

    name: str
    description: str
    #: The originating ``ssc.capture.frame.FrameMetadata`` field name, or
    #: ``None`` for a tag that is not one of that dataclass's own fields
    #: (``%SEQUENCE%`` is the one case — it comes from the capture loop's
    #: own counter, never from the device or from SSC's own header authoring).
    field: str | None
    render: Callable[[_Context], str]


def _slugify(value: str) -> str:
    """A filesystem-safe path segment, preserving non-ASCII letters (a
    target or telescope name is not necessarily English) — only characters
    that are neither alphanumeric nor a common separator are replaced.
    Mirrors ``ssc.capture.pipeline``'s own ``_slug`` for filenames; kept as
    its own small copy here rather than imported, since a path *segment* and
    a *filename* are different enough concerns (a segment never needs an
    extension) to not be worth coupling.
    """
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value.strip())
    # Collapse runs of the replacement character and any dot-only artefact,
    # then trim leading/trailing separators so e.g. "  NGC 7000  " becomes
    # "NGC-7000" rather than "-NGC-7000-" or literal "..".
    collapsed = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    normalised = unicodedata.normalize("NFC", collapsed)
    return normalised


def _str_or_unknown(value: str | None) -> str:
    if value is None:
        return "unknown"
    slug = _slugify(value)
    return slug or "unknown"


def _int_or_unknown(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _fixed(value: float | None, *, dp: int, signed: bool = False) -> str:
    if value is None:
        return "unknown"
    template = f"{{:+.{dp}f}}" if signed else f"{{:.{dp}f}}"
    return template.format(value)


def _exposure(value: float | None) -> str:
    if value is None:
        return "unknown"
    # Fixed notation only. ``repr(3e-05)`` — this device's own measured
    # minimum exposure (``ssc.api.capture_service``'s docstring) — is
    # "3e-05", which is a poor directory name; 6 d.p. renders it as
    # "0.000030" instead, and never collapses a sub-second exposure to "0".
    seconds = f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{seconds}s"


def dateofcapture(date_obs: datetime, tz: ZoneInfo) -> date:
    """The local calendar date of the preceding local noon — see
    ``_NIGHT_ANCHOR_LOCAL_TIME``'s own docstring for why noon rather than
    midnight."""
    local = date_obs.astimezone(tz)
    if local.time() < _NIGHT_ANCHOR_LOCAL_TIME:
        return (local - timedelta(days=1)).date()
    return local.date()


PATH_TAGS: Final[tuple[PathTag, ...]] = (
    PathTag(
        name="TARGET",
        description="The plan item's target (FrameMetadata.object_name), sanitised for "
        "a path segment. 'unknown' for a frame with no target — e.g. a dark.",
        field="object_name",
        render=lambda ctx: _str_or_unknown(ctx.metadata.object_name),
    ),
    PathTag(
        name="DATEOFCAPTURE",
        description="The local calendar date of the preceding local noon, derived from "
        "DATE-OBS and the site's timezone (format YYYY-MM-DD). Never the midnight-to-"
        "midnight date — see the module docstring.",
        field="date_obs",
        render=lambda ctx: dateofcapture(ctx.date_obs, ctx.tz).isoformat(),
    ),
    PathTag(
        name="DATE",
        description="Exposure start date in UTC, derived from DATE-OBS "
        "(format YYYYMMDD, without time). Unlike DATEOFCAPTURE, this is the "
        "frame's UTC calendar date, not the local observing-night date.",
        field=None,
        render=lambda ctx: ctx.date_obs.astimezone(UTC).strftime("%Y%m%d"),
    ),
    PathTag(
        name="DATETIME",
        description="Exposure start time in UTC, derived from DATE-OBS "
        "(format YYYYMMDD-HHMMSS).",
        field=None,
        render=lambda ctx: ctx.date_obs.astimezone(UTC).strftime("%Y%m%d-%H%M%S"),
    ),
    PathTag(
        name="IMAGETYPE",
        description="The frame's type (FrameMetadata.frame_type), rendered plural: "
        "lights, darks, flats, biases.",
        field="frame_type",
        render=lambda ctx: _IMAGETYPE_PLURAL[ctx.metadata.frame_type],
    ),
    PathTag(
        name="SEQUENCE",
        description="This capture run's own 1-based exposure counter, zero-padded to 4 "
        "digits. Not a FrameMetadata field — SSC's own capture-loop counter.",
        field=None,
        render=lambda ctx: f"{ctx.sequence:04d}",
    ),
    PathTag(
        name="FILTER",
        description="The filter wheel position in force at exposure time "
        "(FrameMetadata.filter_name), sanitised. 'unknown' if it could not be read.",
        field="filter_name",
        render=lambda ctx: _str_or_unknown(ctx.metadata.filter_name),
    ),
    PathTag(
        name="EXPOSURE",
        description="Exposure duration in seconds with an 's' suffix, fixed-notation "
        "with insignificant trailing zeroes removed (for example 30s or 0.5s; "
        "FrameMetadata.exposure_s).",
        field="exposure_s",
        render=lambda ctx: _exposure(ctx.metadata.exposure_s),
    ),
    PathTag(
        name="GAIN",
        description="Camera.Gain in force at exposure time (FrameMetadata.gain).",
        field="gain",
        render=lambda ctx: _int_or_unknown(ctx.metadata.gain),
    ),
    PathTag(
        name="CCDTEMP",
        description="Sensor temperature in Celsius, 3 d.p., signed (FrameMetadata.ccd_temp_c).",
        field="ccd_temp_c",
        render=lambda ctx: _fixed(ctx.metadata.ccd_temp_c, dp=3, signed=True),
    ),
    PathTag(
        name="BAYEROFFSETX",
        description="Camera.BayerOffsetX (FrameMetadata.bayer_offset_x).",
        field="bayer_offset_x",
        render=lambda ctx: _int_or_unknown(ctx.metadata.bayer_offset_x),
    ),
    PathTag(
        name="BAYEROFFSETY",
        description="Camera.BayerOffsetY (FrameMetadata.bayer_offset_y).",
        field="bayer_offset_y",
        render=lambda ctx: _int_or_unknown(ctx.metadata.bayer_offset_y),
    ),
    PathTag(
        name="PIXELSIZE",
        description="Camera.PixelSizeX in microns, 3 d.p. (FrameMetadata.pixel_size_um).",
        field="pixel_size_um",
        render=lambda ctx: _fixed(ctx.metadata.pixel_size_um, dp=3),
    ),
    PathTag(
        name="RA",
        description="Right ascension in decimal degrees, 4 d.p., unsigned (FrameMetadata.ra_deg).",
        field="ra_deg",
        render=lambda ctx: _fixed(ctx.metadata.ra_deg, dp=4),
    ),
    PathTag(
        name="DEC",
        description="Declination in decimal degrees, 4 d.p., signed (FrameMetadata.dec_deg).",
        field="dec_deg",
        render=lambda ctx: _fixed(ctx.metadata.dec_deg, dp=4, signed=True),
    ),
    PathTag(
        name="SITELAT",
        description="Telescope.SiteLatitude in decimal degrees, 4 d.p., signed "
        "(FrameMetadata.site_lat_deg).",
        field="site_lat_deg",
        render=lambda ctx: _fixed(ctx.metadata.site_lat_deg, dp=4, signed=True),
    ),
    PathTag(
        name="SITELON",
        description="Telescope.SiteLongitude in decimal degrees, 4 d.p., signed "
        "(FrameMetadata.site_lon_deg).",
        field="site_lon_deg",
        render=lambda ctx: _fixed(ctx.metadata.site_lon_deg, dp=4, signed=True),
    ),
    PathTag(
        name="FOCALLENGTH",
        description="The optic's focal length in mm, 1 d.p. (FrameMetadata.focal_length_mm).",
        field="focal_length_mm",
        render=lambda ctx: _fixed(ctx.metadata.focal_length_mm, dp=1),
    ),
    PathTag(
        name="TELESCOPE",
        description="The configured telescope/device label, sanitised "
        "(FrameMetadata.telescope_name).",
        field="telescope_name",
        render=lambda ctx: _str_or_unknown(ctx.metadata.telescope_name),
    ),
    PathTag(
        name="SSCVERSION",
        description="SSC's own version. Always known (FrameMetadata.ssc_version).",
        field="ssc_version",
        render=lambda ctx: _str_or_unknown(ctx.metadata.ssc_version),
    ),
    PathTag(
        name="FIRMWARE",
        description="The device firmware this exposure was captured against, sanitised "
        "(FrameMetadata.device_firmware).",
        field="device_firmware",
        render=lambda ctx: _str_or_unknown(ctx.metadata.device_firmware),
    ),
)

_TAGS_BY_NAME: Final[dict[str, PathTag]] = {tag.name: tag for tag in PATH_TAGS}

#: A synthetic example frame, used only to render an example per tag
#: (``path_tags()``, the ``GET /api/settings/capture/path-tags`` vocabulary)
#: and to validate a template at set time (``validate_capture_folder_template``)
#: without ever touching a device.
_EXAMPLE_DATE_OBS: Final = datetime(2026, 12, 28, 22, 15, 0, tzinfo=UTC)
_EXAMPLE_TZ: Final = ZoneInfo("UTC")
_EXAMPLE_SEQUENCE: Final = 1
_EXAMPLE_METADATA: Final = FrameMetadata(
    date_obs=_EXAMPLE_DATE_OBS,
    exposure_s=30.0,
    gain=100,
    ccd_temp_c=-10.123,
    bayer_offset_x=1,
    bayer_offset_y=0,
    pixel_size_um=2.9,
    ra_deg=83.8221,
    dec_deg=-5.3911,
    site_lat_deg=48.2082,
    site_lon_deg=16.3738,
    filter_name="LP",
    object_name="NGC 7000",
    focal_length_mm=250.0,
    telescope_name="Seestar S30 Pro",
    ssc_version="0.1.0",
    device_firmware="9.16",
    frame_type=FrameType.LIGHT,
)


def _check_placeholders(template: str, *, label: str) -> None:
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name not in _TAGS_BY_NAME:
            raise PathTemplateError(
                f"capture {label} template {template!r} uses unknown placeholder "
                f"%{name}%; known placeholders are {', '.join(sorted(_TAGS_BY_NAME))}"
            )


def render_capture_path(
    template: str,
    *,
    metadata: FrameMetadata,
    date_obs: datetime,
    sequence: int,
    tz: ZoneInfo,
) -> Path:
    """Render ``template`` into a path *relative to the capture root* for
    one frame. Raises :class:`PathTemplateError` for the same three problems
    :func:`validate_capture_folder_template` checks for — this is the single
    function both that validation and every real capture go through, so a
    template accepted at set time can never fail differently at capture time.

    ``date_obs`` is SSC's own, always-known record of when the exposure
    started (mirrors ``ssc.capture.pipeline.frame_filename``'s own reasoning
    for taking it as a parameter rather than reading ``metadata.date_obs``,
    which may be ``None``).
    """
    if template.startswith("/"):
        raise PathTemplateError(f"capture folder template {template!r} must be a relative path")
    _check_placeholders(template, label="folder")
    raw_segments = template.split("/")
    if any(segment == ".." for segment in raw_segments):
        raise PathTemplateError(
            f"capture folder template {template!r} must not contain a '..' path segment"
        )
    context = _Context(metadata=metadata, date_obs=date_obs, sequence=sequence, tz=tz)
    rendered_segments = [
        _PLACEHOLDER_RE.sub(lambda m: _TAGS_BY_NAME[m.group(1)].render(context), segment)
        for segment in raw_segments
        if segment != ""
    ]
    if not rendered_segments:
        raise PathTemplateError(f"capture folder template {template!r} renders to an empty path")
    if any(segment in ("", "..", ".") for segment in rendered_segments):
        raise PathTemplateError(
            f"capture folder template {template!r} renders to an escaping or empty segment"
        )
    return Path(*rendered_segments)


def validate_capture_folder_template(template: str) -> None:
    """Refuse a template that cannot possibly render safely, at the moment
    it is saved (``ssc.settings.store.SettingsStore.set_capture_folder_template``).

    Rendered against a synthetic example frame rather than merely pattern-
    matched, so a placeholder that is spelled correctly but combines badly
    (e.g. producing ``..`` once sanitised) is still caught here rather than
    at the first real capture.
    """
    if not template.strip():
        raise PathTemplateError("capture folder template must not be blank")
    rendered = render_capture_path(
        template,
        metadata=_EXAMPLE_METADATA,
        date_obs=_EXAMPLE_DATE_OBS,
        sequence=_EXAMPLE_SEQUENCE,
        tz=_EXAMPLE_TZ,
    )
    if rendered.is_absolute():
        raise PathTemplateError(f"capture folder template {template!r} must be a relative path")


def render_capture_filename(
    template: str,
    *,
    metadata: FrameMetadata,
    date_obs: datetime,
    sequence: int,
    tz: ZoneInfo,
) -> str:
    """Render a safe filename stem; the caller appends the FITS extension."""
    _check_placeholders(template, label="filename")
    if "/" in template or "\\" in template:
        raise PathTemplateError("capture filename template must not contain path separators")
    context = _Context(metadata=metadata, date_obs=date_obs, sequence=sequence, tz=tz)
    rendered = _PLACEHOLDER_RE.sub(
        lambda match: _TAGS_BY_NAME[match.group(1)].render(context), template
    ).strip()
    if not rendered or rendered in {".", ".."}:
        raise PathTemplateError("capture filename template renders to an empty or unsafe name")
    if any(ord(char) < 32 for char in rendered):
        raise PathTemplateError("capture filename template must not contain control characters")
    if rendered.casefold().endswith((".fit", ".fits")):
        raise PathTemplateError(
            "capture filename template must omit the extension; SSC appends .fits"
        )
    if len(rendered.encode("utf-8")) > 240:
        raise PathTemplateError("capture filename template renders to more than 240 bytes")
    return rendered


def capture_filename_template_without_target(template: str) -> str:
    """Remove ``%TARGET%`` and one adjacent separator for calibration files.

    Calibration frames deliberately have no observing target. Removing the
    semantic component before rendering avoids both an invented ``unknown``
    target and doubled separators while retaining the user's remaining
    filename vocabulary.
    """
    marker = "%TARGET%"
    result = template
    while marker in result:
        index = result.index(marker)
        after = index + len(marker)
        if after < len(result) and result[after] in "_-. ":
            result = result[:index] + result[after + 1 :]
        elif index > 0 and result[index - 1] in "_-. ":
            result = result[: index - 1] + result[after:]
        else:
            result = result[:index] + result[after:]
    validate_capture_filename_template(result)
    return result


def validate_capture_filename_template(template: str) -> None:
    """Refuse a filename template that cannot render safely before saving it."""
    if not template.strip():
        raise PathTemplateError("capture filename template must not be blank")
    render_capture_filename(
        template,
        metadata=_EXAMPLE_METADATA,
        date_obs=_EXAMPLE_DATE_OBS,
        sequence=_EXAMPLE_SEQUENCE,
        tz=_EXAMPLE_TZ,
    )


def path_tags() -> list[tuple[PathTag, str]]:
    """The full vocabulary, each with an example rendering against the same
    synthetic frame :func:`validate_capture_folder_template` uses — what
    ``GET /api/settings/capture/path-tags`` serves."""
    context = _Context(
        metadata=_EXAMPLE_METADATA,
        date_obs=_EXAMPLE_DATE_OBS,
        sequence=_EXAMPLE_SEQUENCE,
        tz=_EXAMPLE_TZ,
    )
    return [(tag, tag.render(context)) for tag in PATH_TAGS]


#: ``FrameMetadata`` fields deliberately absent from the tag vocabulary.
#: The guard below exists so a new field cannot be added without a decision
#: being made about it; this set records the decisions that were "no tag",
#: with the reason, rather than letting them be made by omission.
_FIELDS_WITHOUT_A_TAG: Final = frozenset({
    # Provenance of the timing cards, not a property of the frame's subject.
    # A boolean cannot name a folder, and nobody sorts a library by whether
    # the camera reported its own exposure length.
    "timing_from_ssc",
    # Detector-position grouping is scientific provenance, not a folder partition.
    "dither_group_id",
    # Flat provenance belongs in FITS headers; the established folder layout
    # remains lights/darks/flats/biases and does not split by optic/source.
    "optic_id",
    "flat_source",
    # Diagnostic FITS provenance. These values are valuable when inspecting
    # a frame, but are not stable library-partition or filename components.
    "date_end",
    "bin_x",
    "bin_y",
    "subframe_x",
    "subframe_y",
    "pixel_size_y_um",
    "sensor_name",
    "camera_offset",
    "max_adu",
    "readout_mode",
    "focuser_position",
    "focuser_temperature_c",
    "alignment_mode",
    "aperture_diameter_mm",
    "focal_ratio",
    "wide_camera",
    "stack_count",
    "total_exposure_s",
})


def _assert_covers_every_frame_metadata_field() -> None:  # pragma: no cover - import-time guard
    covered = {tag.field for tag in PATH_TAGS if tag.field is not None}
    declared = {f.name for f in fields(FrameMetadata)}
    missing = declared - covered - _FIELDS_WITHOUT_A_TAG
    if missing:
        raise AssertionError(
            f"ssc.capture.frame.FrameMetadata field(s) {sorted(missing)} have no "
            "matching ssc.capture.pathtemplate.PathTag; add one so the capture "
            "folder template vocabulary stays in sync (CLAUDE.md invariant 7)"
        )


_assert_covers_every_frame_metadata_field()


__all__ = [
    "DEFAULT_CAPTURE_FILENAME_TEMPLATE",
    "DEFAULT_CAPTURE_FOLDER_TEMPLATE",
    "INTERIM_DEFAULT_CAPTURE_FILENAME_TEMPLATE",
    "LEGACY_DEFAULT_CAPTURE_FILENAME_TEMPLATES",
    "PATH_TAGS",
    "PREVIOUS_DEFAULT_CAPTURE_FILENAME_TEMPLATE",
    "PathTag",
    "PathTemplateError",
    "capture_filename_template_without_target",
    "dateofcapture",
    "path_tags",
    "render_capture_filename",
    "render_capture_path",
    "validate_capture_filename_template",
    "validate_capture_folder_template",
]
