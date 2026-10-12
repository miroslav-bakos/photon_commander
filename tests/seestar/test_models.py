"""Typed payloads, and the traps the findings call out by name."""

from __future__ import annotations

from ssc.seestar.models import (
    CameraInfo,
    ChipSize,
    DeviceIdentity,
    DeviceState,
    DiskVolume,
    ExposureAndBin,
    MountState,
    WheelSetting,
)

# The identity blob exactly as captured from firmware 8.46.
DEVICE_SECTION: dict[str, object] = {
    "name": "ASI AIR imager",
    "firmware_ver_int": 2846,
    "firmware_ver_string": "8.46",
    "is_verified": True,
    "ios_ver": "Seestar_v1.05_rv1126bp 20260110",
    "sn": "SYNTHETIC000001",
    "product_model": "Seestar S30 Pro",
    "user_product_model": "Seestar S30 Pro",
    "focal_len": 160.0,
    "fnumber": 5.0,
    "can_star_mode_sel_cam": True,
    "can_wide_cam_roi": True,
    "can_wide_cam_af": True,
    "can_roi_scale": True,
    "firmware_platform": 1,
}


def test_chip_size_is_width_then_height_for_the_portrait_sensor() -> None:
    # Confirmed by native Alpaca and the Seestar app's own FITS.
    size = ChipSize.from_chip_size([2160, 3840])

    assert size == ChipSize(height_px=3840, width_px=2160)
    assert size is not None
    assert size.height_px > size.width_px


def test_a_malformed_chip_size_is_unknown_rather_than_a_wrong_guess() -> None:
    assert ChipSize.from_chip_size([2160]) is None
    assert ChipSize.from_chip_size("2160x3840") is None
    assert ChipSize.from_chip_size(None) is None


def test_device_identity_reads_firmware_and_the_devices_own_capability_flags() -> None:
    identity = DeviceIdentity.from_payload(DEVICE_SECTION)

    assert identity.firmware_version == "8.46"
    assert identity.firmware_version_int == 2846
    assert identity.serial_number == "SYNTHETIC000001"
    assert identity.product_model == "Seestar S30 Pro"
    assert identity.is_verified is True
    assert identity.capabilities == {
        "can_star_mode_sel_cam": True,
        "can_wide_cam_roi": True,
        "can_wide_cam_af": True,
        "can_roi_scale": True,
    }
    # The device says f/5.0 where the profile says f/5.3 — recorded, not corrected.
    assert identity.f_number == 5.0


def test_mount_state_models_the_stow_and_eq_flags() -> None:
    mount = MountState.from_payload(
        {"move_type": "none", "close": True, "tracking": False, "equ_mode": True}
    )

    assert mount.close is True  # arm stowed: nothing will move
    assert mount.equ_mode is True
    assert mount.tracking is False


def test_an_absent_key_is_unknown_never_false_or_zero() -> None:
    mount = MountState.from_payload({})
    assert mount.close is None
    assert mount.tracking is None

    volume = DiskVolume.from_payload({})
    assert volume.free_mb is None


def test_empty_sections_are_reported_because_an_invalid_key_looks_the_same() -> None:
    state = DeviceState.from_payload({"device": DEVICE_SECTION, "battery": {}, "wheel": {}})

    assert state.device is not None
    assert sorted(state.empty_keys) == ["battery", "wheel"]


def test_camera_info_and_exposure_units() -> None:
    info = CameraInfo.from_payload(
        {
            "chip_size": [2160, 3840],
            "pixel_size_um": 2.9,
            "bins": [1, 2],
            "is_color": True,
            "debayer_pattern": "GR",
            "has_cooler": False,
        }
    )
    assert info.chip_size == ChipSize(height_px=3840, width_px=2160)
    assert info.bins == (1, 2)
    assert info.has_cooler is False

    exposure = ExposureAndBin.from_payload({"exposure": 30000000, "bin": 1})
    assert exposure.exposure_us == 30000000  # microseconds, i.e. 30 s


def test_wheel_setting_names_map_onto_the_ascom_filter_names() -> None:
    wheel = WheelSetting.from_payload(
        {"names": ["dark", "IRCUT", "LP"], "exp_sec": [2.0, 2.0, 2.0]}
    )
    assert wheel.names == ("dark", "IRCUT", "LP")
    assert wheel.exposure_seconds == (2.0, 2.0, 2.0)
