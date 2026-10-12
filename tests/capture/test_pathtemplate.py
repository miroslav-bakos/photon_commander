# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc.capture.pathtemplate``: the templated capture folder layout."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ssc.capture.frame import FrameMetadata, FrameType
from ssc.capture.pathtemplate import (
    _FIELDS_WITHOUT_A_TAG,
    DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    DEFAULT_CAPTURE_FOLDER_TEMPLATE,
    PATH_TAGS,
    PathTemplateError,
    capture_filename_template_without_target,
    dateofcapture,
    path_tags,
    render_capture_filename,
    render_capture_path,
    validate_capture_filename_template,
    validate_capture_folder_template,
)

VIENNA = ZoneInfo("Europe/Vienna")


def _metadata(*, object_name: str | None, frame_type: FrameType = FrameType.LIGHT) -> FrameMetadata:
    return FrameMetadata(
        date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
        exposure_s=30.0,
        gain=100,
        ccd_temp_c=-10.123,
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=2.9,
        ra_deg=83.8,
        dec_deg=-5.4,
        site_lat_deg=48.2,
        site_lon_deg=16.4,
        filter_name="LP",
        object_name=object_name,
        frame_type=frame_type,
        focal_length_mm=250.0,
        telescope_name="Seestar S30 Pro",
        ssc_version="0.1.0",
        device_firmware="9.16",
    )


# -- the noon boundary (dispatch verification bullet 2) ---------------------- #


def test_evening_and_small_hours_share_a_dateofcapture() -> None:
    evening = datetime(2026, 9, 9, 19, 0, 0, tzinfo=UTC)  # 21:00 CEST, 2026-09-09
    small_hours = datetime(2026, 9, 9, 23, 0, 0, tzinfo=UTC)  # 01:00 CEST, 2026-09-10
    assert dateofcapture(evening, VIENNA) == dateofcapture(small_hours, VIENNA)
    assert dateofcapture(evening, VIENNA).isoformat() == "2026-09-09"
    assert dateofcapture(small_hours, VIENNA).isoformat() == "2026-09-09"


def test_afternoon_lands_in_the_next_dateofcapture() -> None:
    afternoon = datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC)  # 13:00 CEST, 2026-09-10
    assert dateofcapture(afternoon, VIENNA).isoformat() == "2026-09-10"


def test_noon_itself_belongs_to_the_new_day() -> None:
    noon = datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)  # 12:00 CEST exactly
    assert dateofcapture(noon, VIENNA).isoformat() == "2026-09-10"


# -- rendering ----------------------------------------------------------------- #


def test_default_template_renders_target_date_and_plural_type() -> None:
    metadata = _metadata(object_name="M42", frame_type=FrameType.LIGHT)
    path = render_capture_path(
        DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        metadata=metadata,
        date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
        sequence=3,
        tz=UTC,
    )
    assert path == Path("M42") / "2026-09-10" / "lights"


def test_default_filename_template_renders_a_sortable_stem() -> None:
    stem = render_capture_filename(
        DEFAULT_CAPTURE_FILENAME_TEMPLATE,
        metadata=_metadata(object_name="M42"),
        date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
        sequence=3,
        tz=UTC,
    )
    assert stem == "lights_M42_30s_-10.123_LP_20260910-220000_0003"


def test_date_tag_is_utc_date_only_for_folders_and_filenames() -> None:
    date_obs = datetime(2026, 9, 10, 1, 23, 45, tzinfo=UTC)
    metadata = _metadata(object_name="M42", frame_type=FrameType.LIGHT)
    folder = render_capture_path(
        "%DATEOFCAPTURE%/%DATE%",
        metadata=metadata,
        date_obs=date_obs,
        sequence=3,
        tz=VIENNA,
    )
    filename = render_capture_filename(
        "%IMAGETYPE%_%DATE%_%DATETIME%_%SEQUENCE%",
        metadata=metadata,
        date_obs=date_obs,
        sequence=3,
        tz=VIENNA,
    )

    assert folder == Path("2026-09-09") / "20260910"
    assert filename == "lights_20260910_20260910-012345_0003"


def test_date_tag_works_for_calibration_filenames() -> None:
    template = capture_filename_template_without_target("%TARGET%_%DATE%_%IMAGETYPE%")
    stem = render_capture_filename(
        template,
        metadata=_metadata(object_name=None, frame_type=FrameType.DARK),
        date_obs=datetime(2026, 9, 10, 1, 23, 45, tzinfo=UTC),
        sequence=1,
        tz=VIENNA,
    )
    assert stem == "20260910_darks"


def test_calibration_filename_template_omits_target_and_adjacent_separator() -> None:
    template = capture_filename_template_without_target(DEFAULT_CAPTURE_FILENAME_TEMPLATE)
    stem = render_capture_filename(
        template,
        metadata=_metadata(object_name=None, frame_type=FrameType.FLAT),
        date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
        sequence=3,
        tz=UTC,
    )
    assert stem == "flats_30s_-10.123_LP_20260910-220000_0003"


@pytest.mark.parametrize(
    "exposure_s,expected",
    [(30.0, "30s"), (60.0, "60s"), (120.0, "120s"), (0.5, "0.5s"), (0.00003, "0.00003s")],
)
def test_exposure_tag_is_a_compact_duration(exposure_s: float, expected: str) -> None:
    metadata = replace(_metadata(object_name="M42"), exposure_s=exposure_s)
    stem = render_capture_filename(
        "%EXPOSURE%",
        metadata=metadata,
        date_obs=metadata.date_obs,
        sequence=1,
        tz=UTC,
    )
    assert stem == expected


def test_a_dark_with_no_target_renders_unknown_not_none_or_empty() -> None:
    metadata = _metadata(object_name=None, frame_type=FrameType.DARK)
    path = render_capture_path(
        DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        metadata=metadata,
        date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
        sequence=1,
        tz=UTC,
    )
    assert path == Path("unknown") / "2026-09-10" / "darks"
    assert "None" not in str(path)


def test_a_target_needing_sanitising_survives_spaces_and_non_ascii() -> None:
    metadata = _metadata(object_name="NGC 7000 Cassiopée")
    path = render_capture_path(
        DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        metadata=metadata,
        date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
        sequence=1,
        tz=UTC,
    )
    target_segment = path.parts[0]
    assert " " not in target_segment
    assert "Cassiopée" in target_segment or "Cassiopee" in target_segment
    assert "NGC" in target_segment


@pytest.mark.parametrize(
    "frame_type,expected",
    [
        (FrameType.LIGHT, "lights"),
        (FrameType.DARK, "darks"),
        (FrameType.FLAT, "flats"),
        (FrameType.BIAS, "biases"),
    ],
)
def test_imagetype_renders_plural_for_every_frame_type(
    frame_type: FrameType, expected: str
) -> None:
    metadata = _metadata(object_name="M42", frame_type=frame_type)
    path = render_capture_path(
        "%IMAGETYPE%",
        metadata=metadata,
        date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
        sequence=1,
        tz=UTC,
    )
    assert path == Path(expected)


# -- validation at set time (dispatch verification bullet 3) ------------------ #


def test_unknown_placeholder_is_refused_with_its_own_name() -> None:
    with pytest.raises(PathTemplateError, match="BOGUS"):
        validate_capture_folder_template("%TARGET%/%BOGUS%")


def test_a_literal_dotdot_segment_is_refused() -> None:
    with pytest.raises(PathTemplateError, match=r"\.\."):
        validate_capture_folder_template("%TARGET%/../escaped")


def test_an_absolute_template_is_refused() -> None:
    with pytest.raises(PathTemplateError, match="relative"):
        validate_capture_folder_template("/etc/%TARGET%")


def test_a_valid_template_passes() -> None:
    validate_capture_folder_template(DEFAULT_CAPTURE_FOLDER_TEMPLATE)
    validate_capture_folder_template("%IMAGETYPE%/%TARGET%/%SEQUENCE%")


def test_a_blank_template_is_refused() -> None:
    with pytest.raises(PathTemplateError):
        validate_capture_folder_template("   ")


@pytest.mark.parametrize("template", ["../frame", "sub/frame", r"sub\\frame"])
def test_filename_template_cannot_create_a_path(template: str) -> None:
    with pytest.raises(PathTemplateError):
        validate_capture_filename_template(template)


@pytest.mark.parametrize("template", ["", "   ", ".", "..", "frame.fits", "%BOGUS%"])
def test_invalid_filename_template_is_refused(template: str) -> None:
    with pytest.raises(PathTemplateError):
        validate_capture_filename_template(template)


# -- the vocabulary (dispatch verification bullet 4) --------------------------- #


def test_every_frame_metadata_field_is_covered_by_exactly_one_tag() -> None:
    """The one source rule: a header field added to ``FrameMetadata`` without
    a matching tag must fail this test, not silently leave Settings stale.

    A field may opt out only by being named in ``_FIELDS_WITHOUT_A_TAG``,
    which carries the reason — so "no tag" stays a recorded decision rather
    than something that happens by forgetting.
    """
    covered = [tag.field for tag in PATH_TAGS if tag.field is not None]
    assert len(covered) == len(set(covered)), "a FrameMetadata field must map to exactly one tag"
    assert set(covered) | _FIELDS_WITHOUT_A_TAG == {f.name for f in fields(FrameMetadata)}
    assert not (set(covered) & _FIELDS_WITHOUT_A_TAG), "a field cannot both have a tag and opt out"


def test_path_tags_endpoint_data_has_an_example_per_tag() -> None:
    rendered = path_tags()
    assert len(rendered) == len(PATH_TAGS)
    names = {tag.name for tag, _ in rendered}
    assert "TARGET" in names
    assert "DATEOFCAPTURE" in names
    assert next(example for tag, example in rendered if tag.name == "DATE") == "20261228"
    assert "DATETIME" in names
    assert "IMAGETYPE" in names
    assert "SEQUENCE" in names
    for tag, example in rendered:
        assert example, f"{tag.name} produced an empty example"
        assert example != "None"
