# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Frame-type-aware calibration-master integration.

Biases and darks retain their absolute signal and are robustly averaged.
Flats first have a compatible bias pedestal removed, are independently
median-normalised, robustly averaged, and are finally normalised to unit
median.  The result is a float FITS so no precision is discarded.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ssc.capture.calibration import analyse_flat_frame
from ssc.capture.fits import write_fits_file
from ssc.capture.fits_rows import FitsRowSource
from ssc.capture.frame import FrameMetadata, FrameType
from ssc.skymap.fits import read_fits_image
from ssc.stacking.integration import StackResult, sigma_clip_stack_rows

# A five-sigma, single-pass rejection preserves the efficiency of the mean for
# normal sensor noise while removing only credible transient excursions. The
# combiner's pooled temporal-noise floor prevents Seestar ADC quantization from
# turning zero per-pixel MAD into runaway rejection.
CALIBRATION_REJECTION_SIGMA = 5.0
CALIBRATION_REJECTION_ITERATIONS = 1


class CalibrationMasterError(ValueError):
    """The supplied calibration set cannot safely make a master."""


@dataclass(frozen=True, slots=True)
class CalibrationMasterResult:
    path: Path
    provenance_path: Path
    frame_count: int
    rejected_pixels: int


def _sources(paths: tuple[Path, ...]) -> tuple[tuple[FitsRowSource, ...], list[dict[str, object]]]:
    if not paths:
        raise CalibrationMasterError("a master requires at least one source frame")
    sources = tuple(FitsRowSource.open(path) for path in paths)
    if any(source.shape != sources[0].shape for source in sources):
        raise CalibrationMasterError("all calibration frames must have the same 2-D shape")
    return sources, [source.header for source in sources]


def _integrate(
    sources: tuple[FitsRowSource, ...],
    *,
    bias: np.ndarray | None = None,
    medians: np.ndarray | None = None,
) -> StackResult:
    def read_rows(start: int, stop: int) -> np.ndarray:
        values = np.empty((len(sources), stop - start, sources[0].shape[1]), dtype=np.float32)
        for index, source in enumerate(sources):
            values[index] = source.read_rows(start, stop)
        if bias is not None:
            values -= bias[start:stop]
        if medians is not None:
            values /= medians[:, None, None]
        return values

    return sigma_clip_stack_rows(
        (len(sources), *sources[0].shape),
        read_rows,
        sigma=CALIBRATION_REJECTION_SIGMA,
        iterations=CALIBRATION_REJECTION_ITERATIONS,
    )


def _integration_report(result: StackResult) -> dict[str, object]:
    sample_count = result.frame_count * int(np.prod(result.pixels.shape))
    return {
        "combination": "arithmetic mean of accepted samples",
        "rejection": "quantization-aware pooled-noise sigma clipping",
        "sigma": result.sigma,
        "iterations": result.iterations,
        "pooled_temporal_sigma": result.pooled_sigma,
        "rejected_samples": result.rejected_pixels,
        "rejected_fraction": result.rejected_pixels / sample_count,
        "rejected_low": result.rejected_low,
        "rejected_high": result.rejected_high,
        "rejected_nonfinite": result.rejected_nonfinite,
        "accepted_samples_per_pixel": {
            "minimum": result.accepted_count_min,
            "median": result.accepted_count_median,
            "maximum": result.accepted_count_max,
        },
    }


def _header_number(header: dict[str, object], key: str) -> float | None:
    value = header.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _consistent_header_value(headers: list[dict[str, object]], key: str) -> object | None:
    values = {header.get(key) for header in headers}
    return values.pop() if len(values) == 1 else None


def _consistent_int(headers: list[dict[str, object]], key: str) -> int | None:
    value = _consistent_header_value(headers, key)
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _consistent_float(headers: list[dict[str, object]], key: str) -> float | None:
    value = _consistent_header_value(headers, key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _consistent_str(headers: list[dict[str, object]], key: str) -> str | None:
    value = _consistent_header_value(headers, key)
    return value if isinstance(value, str) else None


def _unsupported_hardlinks(error: OSError, path: Path) -> bool:
    if error.errno in {errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV} or getattr(
        error, "winerror", None
    ) in {1, 50}:
        return True
    if error.errno != errno.EPERM:
        return False
    # Linux FAT drivers lack the link operation and report EPERM. Distinguish
    # those filesystems from ordinary ownership/security permission failures.
    try:
        resolved = path.parent.resolve()
        device = resolved.stat().st_dev
        device_key = f"{os.major(device)}:{os.minor(device)}"
        mounts = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields = left.split()
            mount = Path(fields[4].replace("\\040", " ").replace("\\134", "\\"))
            if fields[2] == device_key and resolved.is_relative_to(mount):
                mounts.append((len(mount.parts), right.split()[0]))
        return bool(mounts) and max(mounts)[1] in {"vfat", "exfat", "msdos", "fat"}
    except (OSError, ValueError, IndexError, AttributeError):
        return False


def _backup_master_artifact(path: Path, backup: Path) -> None:
    try:
        os.link(path, backup)
        return
    except OSError as error:
        if not _unsupported_hardlinks(error, path):
            raise
    # Reserve an owned private file, then copy through shutil's bounded buffers.
    # copy2 retains the old file's mode/timestamps; sync copied bytes before use.
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        shutil.copy2(path, backup)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        backup.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)


def _publish_master_pair(
    pending_path: Path,
    output_path: Path,
    temporary: Path,
    provenance_path: Path,
) -> None:
    """Roll back publication exceptions without copying image buffers.

    Same-directory hard links (or bounded copies) retain old files. This is exception recovery,
    not an atomic transaction across two files or a crash recovery protocol.
    """
    destinations = (provenance_path, output_path)
    existed = {path: path.exists() for path in destinations}
    backups: dict[Path, Path] = {}
    publication_started = False
    try:
        for path in destinations:
            if existed[path]:
                backup = temporary.with_name(f"{temporary.name}.{path.name}.rollback")
                _backup_master_artifact(path, backup)
                backups[path] = backup
        publication_started = True
        os.replace(temporary, provenance_path)
        # Discover the verified FITS only after its complete QA report is staged.
        os.replace(pending_path, output_path)
    except BaseException as publication_error:
        recovery_errors: list[BaseException] = []
        failed_restore: set[Path] = set()
        if publication_started:
            for path in reversed(destinations):
                try:
                    if existed[path]:
                        os.replace(backups[path], path)
                    else:
                        path.unlink(missing_ok=True)
                except BaseException as error:
                    failed_restore.add(path)
                    recovery_errors.append(error)
        for staging in (temporary, pending_path):
            try:
                staging.unlink(missing_ok=True)
            except BaseException as error:
                recovery_errors.append(error)
        for path, backup in backups.items():
            # Never remove the only surviving prior artifact after failed restore.
            if path in failed_restore:
                continue
            if backup.exists() and not path.exists():
                recovery_errors.append(
                    CalibrationMasterError("prior artifact retained in recovery backup")
                )
                continue
            try:
                backup.unlink(missing_ok=True)
            except BaseException as error:
                recovery_errors.append(error)
        if recovery_errors:
            raise BaseExceptionGroup(
                "master publication failed; recovery incomplete, remaining backups retained",
                [publication_error, *recovery_errors],
            ) from None
        raise
    # Publication succeeded; a cleanup failure surfaces while the valid pair stays.
    for backup in backups.values():
        backup.unlink(missing_ok=True)


def calibration_optic_id(header: dict[str, object]) -> str | None:
    """Resolve explicit lens provenance; missing legacy provenance stays unknown."""
    value = header.get("OPTICID")
    optic = value.strip().casefold() if isinstance(value, str) and value.strip() else None
    if optic is not None:
        optic = {"main": "tele", "primary": "tele", "secondary": "wide"}.get(optic, optic)
    wide = header.get("WIDECAM")
    wide_optic = "wide" if wide == 1 else "tele" if wide == 0 else None
    if optic is not None and wide_optic is not None and optic != wide_optic:
        raise CalibrationMasterError("conflicting optic provenance")
    return optic if optic is not None else wide_optic


def calibration_optics_match(header: dict[str, object], optic_id: str | None) -> bool:
    """Legacy unknown masters remain Tele-only, never explicit Wide."""
    candidate = calibration_optic_id(header)
    return (candidate or "tele") == (optic_id or "tele")


def build_calibration_master(
    *,
    frame_type: FrameType,
    source_paths: tuple[Path, ...],
    output_path: Path,
    bias_paths: tuple[Path, ...] = (),
    ssc_version: str,
    max_adu: float | None = None,
) -> CalibrationMasterResult:
    """Integrate and durably write one master plus a provenance sidecar."""
    if frame_type is FrameType.LIGHT:
        raise CalibrationMasterError("light frames cannot make a calibration master")
    if len(source_paths) < 10:
        raise CalibrationMasterError("a verified master requires at least 10 source frames")
    sources, headers = _sources(source_paths)
    expected_type = frame_type.value
    if any(header.get("IMAGETYP") != expected_type for header in headers):
        raise CalibrationMasterError(f"every source must be tagged as {expected_type}")
    gains = {header.get("GAIN") for header in headers}
    exposures = {header.get("EXPTIME") for header in headers}
    filters = {header.get("FILTER") for header in headers}
    optics = {calibration_optic_id(header) for header in headers}
    if len({optic or "tele" for optic in optics}) != 1:
        raise CalibrationMasterError("source optic ids do not match")
    master_optic = next((optic for optic in optics if optic is not None), None)
    if len(gains) != 1:
        raise CalibrationMasterError("source gains do not match")
    if len(exposures) != 1:
        raise CalibrationMasterError("source exposure times do not match")
    if frame_type is FrameType.FLAT and len(filters) != 1:
        raise CalibrationMasterError("flat filters or optic ids do not match")
    pedestal_sources: tuple[Path, ...] = ()
    pedestal_integration: StackResult | None = None
    bias: np.ndarray | None = None
    medians: np.ndarray | None = None
    if frame_type is FrameType.FLAT:
        if max_adu is None:
            raise CalibrationMasterError("flat verification requires Camera.MaxADU")
        for index, source in enumerate(sources):
            plane = source.read_rows(0, source.shape[0])
            metrics = analyse_flat_frame(plane, max_adu=max_adu)
            if not metrics.target_brightness or metrics.clipped or metrics.banded:
                raise CalibrationMasterError(
                    f"flat source {index + 1} failed illumination verification: "
                    f"median {metrics.median_fraction:.1%}, clipped "
                    f"{metrics.clipped_pixel_fraction:.3%}, banding {metrics.banding_fraction:.2%}"
                )
        del plane
        if not bias_paths:
            raise CalibrationMasterError(
                "flat master generation needs compatible bias frames at the same gain"
            )
        biases, bias_headers = _sources(bias_paths)
        if any(not calibration_optics_match(header, master_optic) for header in bias_headers):
            raise CalibrationMasterError("bias optic ids do not match flat optic")
        if biases[0].shape != sources[0].shape:
            raise CalibrationMasterError("bias and flat dimensions do not match")
        pedestal_integration = _integrate(biases)
        bias = pedestal_integration.pixels
        medians = np.empty(len(sources), dtype=np.float32)
        for index, source in enumerate(sources):
            plane = source.read_rows(0, source.shape[0])
            plane -= bias
            medians[index] = np.median(plane)
        del plane
        if np.any(~np.isfinite(medians)) or np.any(medians <= 0):
            raise CalibrationMasterError("a pedestal-subtracted flat has no positive median")
        pedestal_sources = bias_paths

    integrated = _integrate(sources, bias=bias, medians=medians)
    pixels = integrated.pixels
    if frame_type is FrameType.FLAT:
        final_median = float(np.median(pixels))
        if not np.isfinite(final_median) or final_median <= 0:
            raise CalibrationMasterError("integrated flat has no positive median")
        pixels = pixels / final_median

    first = headers[0]
    exposure = _header_number(first, "EXPTIME")
    gain_value = first.get("GAIN")
    gain = (
        int(gain_value)
        if isinstance(gain_value, int) and not isinstance(gain_value, bool)
        else None
    )
    temperatures = [
        temperature
        for header in headers
        if (temperature := _header_number(header, "CCD-TEMP")) is not None
    ]
    focuser_temperatures = [
        temperature
        for header in headers
        if (temperature := _header_number(header, "FOCTEMP")) is not None
    ]
    pattern_offsets = {
        "RGGB": (0, 0),
        "GRBG": (1, 0),
        "GBRG": (0, 1),
        "BGGR": (1, 1),
    }.get(_consistent_str(headers, "BAYERPAT") or "")
    metadata = FrameMetadata(
        date_obs=datetime.now(tz=UTC),
        exposure_s=exposure,
        gain=gain,
        ccd_temp_c=float(np.median(temperatures)) if temperatures else None,
        bayer_offset_x=None if pattern_offsets is None else pattern_offsets[0],
        bayer_offset_y=None if pattern_offsets is None else pattern_offsets[1],
        pixel_size_um=_header_number(first, "XPIXSZ"),
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name=str(first["FILTER"]) if "FILTER" in first else None,
        object_name=None,
        frame_type=frame_type,
        focal_length_mm=_header_number(first, "FOCALLEN"),
        telescope_name="Photon Commander calibration master",
        ssc_version=ssc_version,
        device_firmware=None,
        optic_id=master_optic,
        flat_source=str(first["FLATSRC"]) if "FLATSRC" in first else None,
        bin_x=_consistent_int(headers, "XBINNING"),
        bin_y=_consistent_int(headers, "YBINNING"),
        subframe_x=_consistent_int(headers, "XORGSUBF"),
        subframe_y=_consistent_int(headers, "YORGSUBF"),
        pixel_size_y_um=_consistent_float(headers, "YPIXSZ"),
        sensor_name=_consistent_str(headers, "INSTRUME"),
        camera_offset=_consistent_int(headers, "OFFSET"),
        max_adu=_consistent_float(headers, "MAXADU"),
        readout_mode=_consistent_int(headers, "READOUTM"),
        focuser_position=_consistent_int(headers, "FOCUSPOS"),
        focuser_temperature_c=(
            float(np.median(focuser_temperatures)) if focuser_temperatures else None
        ),
        alignment_mode=_consistent_int(headers, "EQMODE"),
        aperture_diameter_mm=_consistent_float(headers, "APTDIA"),
        focal_ratio=_consistent_float(headers, "APERTURE"),
        wide_camera=(
            bool(wide_value)
            if (wide_value := _consistent_int(headers, "WIDECAM")) is not None
            else None
        ),
        stack_count=len(source_paths),
        total_exposure_s=None if exposure is None else exposure * len(source_paths),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path = output_path.with_name(f".{output_path.name}.pending")
    temporary: str | None = None
    try:
        write_fits_file(pending_path, pixels.astype(np.float32), metadata)
        verified = read_fits_image(pending_path.read_bytes())
        if verified.data.shape != pixels.shape or not np.all(np.isfinite(verified.data)):
            raise CalibrationMasterError("written master failed FITS verification")
        if verified.header.get("IMAGETYP") != frame_type.value:
            raise CalibrationMasterError("written master has the wrong frame type")
        if frame_type is FrameType.FLAT:
            median = float(np.median(verified.data))
            if abs(median - 1.0) > 1e-4 or np.any(verified.data <= 0):
                raise CalibrationMasterError(
                    "written flat master is not positive and unit-normalised"
                )
            master_banding = analyse_flat_frame(
                verified.data, max_adu=max(float(np.max(verified.data)) * 2.0, 1.0)
            )
            if master_banding.banded:
                raise CalibrationMasterError(
                    f"written flat master retains likely {master_banding.banding_axis} "
                    f"banding ({master_banding.banding_fraction:.2%})"
                )
        provenance_path = output_path.with_suffix(".json")
        verified_values = verified.data.astype(np.float64)
        percentiles = np.percentile(verified_values, (0.1, 1, 25, 50, 75, 99, 99.9))
        histogram_counts, histogram_edges = np.histogram(verified_values, bins=64)
        median_value = float(percentiles[3])
        robust_mad = float(np.median(np.abs(verified_values - median_value)))
        dark_pattern: dict[str, object] | None = None
        if frame_type is FrameType.DARK:
            dark_median = float(np.median(verified_values))
            dark_mad = float(np.median(np.abs(verified_values - dark_median)))
            hot_threshold = dark_median + 8.0 * max(1.4826 * dark_mad, 1e-12)
            hot_pixels = int(np.count_nonzero(verified_values > hot_threshold))
            height, width = verified_values.shape
            border_y, border_x = max(1, height // 10), max(1, width // 10)
            edge_mask = np.zeros((height, width), dtype=bool)
            edge_mask[:border_y] = True
            edge_mask[-border_y:] = True
            edge_mask[:, :border_x] = True
            edge_mask[:, -border_x:] = True
            centre = verified_values[border_y : height - border_y, border_x : width - border_x]
            centre_median = float(np.median(centre)) if centre.size else dark_median
            edge_median = max(
                float(np.median(verified_values[:border_y])),
                float(np.median(verified_values[-border_y:])),
                float(np.median(verified_values[:, :border_x])),
                float(np.median(verified_values[:, -border_x:])),
            )
            master_centred = verified_values - dark_median
            correlations: list[float] = []
            residual_mads: list[float] = []
            for source_file in sources:
                source_pixels = source_file.read_rows(0, source_file.shape[0])
                source_centred = source_pixels - np.median(source_pixels)
                if np.std(master_centred) > 0 and np.std(source_centred) > 0:
                    correlations.append(
                        float(np.corrcoef(master_centred.ravel(), source_centred.ravel())[0, 1])
                    )
                residual = source_pixels - verified_values
                residual_mads.append(float(np.median(np.abs(residual - np.median(residual)))))
            dark_pattern = {
                "stable_hot_pixel_candidates": hot_pixels,
                "stable_hot_pixel_fraction": hot_pixels / verified_values.size,
                "hot_pixel_threshold": hot_threshold,
                "edge_median": edge_median,
                "centre_median": centre_median,
                "edge_glow_excess": edge_median - centre_median,
                "median_pattern_correlation": (
                    float(np.median(correlations)) if correlations else None
                ),
                "median_residual_mad_after_master_subtraction": float(np.median(residual_mads)),
                "interpretation": (
                    "Repeatable hot pixels and glow remain in the master so subtraction removes "
                    "them from light frames; transient outliers are rejected."
                ),
            }

        payload = {
            "schema": 1,
            "frame_type": frame_type.value,
            "method": (
                "bias-subtracted, per-frame median-normalised, quantization-aware clipped mean, "
                "final median 1"
                if frame_type is FrameType.FLAT
                else "unnormalised quantization-aware clipped mean"
            ),
            "sigma": integrated.sigma,
            "iterations": integrated.iterations,
            "rejected_pixels": integrated.rejected_pixels,
            "integration": _integration_report(integrated),
            "pedestal_integration": (
                None if pedestal_integration is None else _integration_report(pedestal_integration)
            ),
            "quality": {
                "minimum": float(np.min(verified_values)),
                "maximum": float(np.max(verified_values)),
                "mean": float(np.mean(verified_values)),
                "median": median_value,
                "standard_deviation": float(np.std(verified_values)),
                "robust_mad": robust_mad,
                "percentiles": {
                    key: float(value)
                    for key, value in zip(
                        ("p0.1", "p1", "p25", "p50", "p75", "p99", "p99.9"),
                        percentiles,
                        strict=True,
                    )
                },
                "histogram": {
                    "counts": histogram_counts.tolist(),
                    "edges": histogram_edges.tolist(),
                },
                "dark_pattern": dark_pattern,
            },
            "verification": {
                "homogeneous_type": True,
                "homogeneous_shape": True,
                "homogeneous_gain": True,
                "homogeneous_exposure": True,
                "finite_output": True,
                "fits_reopened": True,
                "flat_positive_unit_median": frame_type is FrameType.FLAT,
                "dark_fixed_pattern_retained": frame_type is FrameType.DARK,
            },
            "sources": [str(path) for path in source_paths],
            "pedestal_sources": [str(path) for path in pedestal_sources],
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
        fd, temporary = tempfile.mkstemp(
            dir=provenance_path.parent, prefix=f".{provenance_path.name}.", suffix=".part"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        pending_path.unlink(missing_ok=True)
        raise

    _publish_master_pair(pending_path, output_path, Path(temporary), provenance_path)
    return CalibrationMasterResult(
        output_path,
        provenance_path,
        len(source_paths),
        integrated.rejected_pixels,
    )


__all__ = ["CalibrationMasterError", "CalibrationMasterResult", "build_calibration_master"]
