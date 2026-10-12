# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Capture and the session catalogue (DESIGN.md §9.5).

SSC drives every exposure over the Alpaca seam and writes every FITS itself
— there is no longer a route to a device-captured frame (§9.5.1). This
package owns that write path and the catalogue built from it: it knows FITS
and the configured capture folder, and nothing about the Seestar wire
protocol or the Alpaca transport (§9.5.5; enforced by ``.importlinter``,
same contract as everything else above the protocol seam).

A frame arrives here already captured and already decoded — a numpy array
plus the metadata read at exposure time (:mod:`ssc.capture.frame`) — from
whatever drove the exposure over Alpaca (``ssc.client``/``ssc.alpaca``, out
of scope for this package). Given that, this package:

* writes the FITS file, authoring every header card itself
  (:mod:`ssc.capture.fits`);
* catalogues the result durably, row-before-bytes
  (:mod:`ssc.capture.pipeline`, ``ssc.store.capture``);
* groups catalogued frames into sessions purely from ``object``/``date_obs``
  (:mod:`ssc.capture.catalogue`).

The capture-folder *setting* itself — where these files land, validated on
the way in — lives in :mod:`ssc.settings.capture_folder`, following the same
split every other setting in ``ssc/settings`` already uses.
"""

from ssc.capture.binning import bin_2x2_for_solving
from ssc.capture.catalogue import SESSION_GAP, Session, derive_sessions
from ssc.capture.fits import (
    ROW_ORDER,
    FitsWriteError,
    bayer_pattern,
    debayer,
    write_fits_bytes,
    write_fits_file,
)
from ssc.capture.frame import FrameMetadata, FrameType, RawFrame
from ssc.capture.masters import (
    CalibrationMasterError,
    CalibrationMasterResult,
    build_calibration_master,
)
from ssc.capture.pathtemplate import (
    DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    DEFAULT_CAPTURE_FOLDER_TEMPLATE,
    PATH_TAGS,
    PathTag,
    PathTemplateError,
    capture_filename_template_without_target,
    path_tags,
    render_capture_filename,
    render_capture_path,
    validate_capture_filename_template,
    validate_capture_folder_template,
)
from ssc.capture.pipeline import capture_frame, frame_filename, reconcile_incomplete
from ssc.capture.quality import FrameQuality, measure_frame_quality

__all__ = [
    "DEFAULT_CAPTURE_FILENAME_TEMPLATE",
    "DEFAULT_CAPTURE_FOLDER_TEMPLATE",
    "PATH_TAGS",
    "ROW_ORDER",
    "SESSION_GAP",
    "CalibrationMasterError",
    "CalibrationMasterResult",
    "FitsWriteError",
    "FrameMetadata",
    "FrameQuality",
    "FrameType",
    "PathTag",
    "PathTemplateError",
    "RawFrame",
    "Session",
    "bayer_pattern",
    "bin_2x2_for_solving",
    "build_calibration_master",
    "capture_filename_template_without_target",
    "capture_frame",
    "debayer",
    "derive_sessions",
    "frame_filename",
    "measure_frame_quality",
    "path_tags",
    "reconcile_incomplete",
    "render_capture_filename",
    "render_capture_path",
    "validate_capture_filename_template",
    "validate_capture_folder_template",
    "write_fits_bytes",
    "write_fits_file",
]
