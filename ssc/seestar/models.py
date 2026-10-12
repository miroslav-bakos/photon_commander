# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Typed models for the payload shapes actually observed on firmware 8.46.

Every field here corresponds to a key quoted verbatim in ``docs/M0-FINDINGS.md``.
Where a payload was only described in prose (``get_controls``, ``get_albums``,
``get_app_state``, ``pi_get_time``), no model is defined — the caller gets the raw
mapping and the gap is an explicit open item, not a fabricated shape.

Parsing rules that hold everywhere in this module:

* ``None`` means **unknown**. It never means zero, empty or false.
* A missing or wrongly-typed key yields ``None`` rather than an exception: the
  device is the authority on its own payloads and a firmware bump that adds or
  renames a key must degrade, not crash (``CLAUDE.md`` invariant 7).
* Credential-bearing keys (``ap.passwd``, ``setting.password``) are **never**
  modelled here, so they cannot reach a caller through a typed field. Use
  ``transport.redact`` before logging any raw blob.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# --------------------------------------------------------------------------- #
# Coercion helpers
# --------------------------------------------------------------------------- #


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else None


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items()}
    return {}


def _as_str_list(value: object) -> tuple[str, ...] | None:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return None
    return tuple(item for item in value if isinstance(item, str))


def _as_int_list(value: object) -> tuple[int, ...] | None:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return None
    return tuple(item for item in value if isinstance(item, int) and not isinstance(item, bool))


def _as_float_list(value: object) -> tuple[float, ...] | None:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return None
    return tuple(
        float(item)
        for item in value
        if isinstance(item, int | float) and not isinstance(item, bool)
    )


# --------------------------------------------------------------------------- #
# Sensor geometry — the transposition hazard
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChipSize:
    """Sensor dimensions in pixels.

    The device reports ``chip_size`` as ``[width, height]`` — observed
    ``[2160, 3840]`` on the S30 Pro's portrait sensor, matching both native
    Alpaca ``CameraXSize``/``CameraYSize`` and the Seestar app's own FITS
    ``NAXIS1``/``NAXIS2``.
    """

    height_px: int
    width_px: int

    @classmethod
    def from_chip_size(cls, value: object) -> ChipSize | None:
        pair = _as_int_list(value)
        if pair is None or len(pair) != 2:
            return None
        width, height = pair
        return cls(height_px=height, width_px=width)


# --------------------------------------------------------------------------- #
# get_device_state keys
# --------------------------------------------------------------------------- #

#: Keys accepted by ``get_device_state``. An *invalid* key returns ``{}`` with
#: ``code`` 0 rather than an error, so an empty result proves nothing about the
#: key's validity — never build validation on that.
DEVICE_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "device",
        "camera",
        "focuser",
        "mount",
        "ap",
        "station",
        "pi_status",
        "storage",
        "setting",
        "balance_sensor",
    }
)

#: ``get_device_state`` keys that return cleartext credentials. The session never
#: requests them; if a caller does, the result must be redacted before it is
#: logged, cached, displayed or written to a fixture.
CREDENTIAL_BEARING_KEYS: Final[frozenset[str]] = frozenset({"ap", "setting"})


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """``get_device_state`` key ``device`` — identity and capability flags."""

    name: str | None
    firmware_version: str | None
    firmware_version_int: int | None
    product_model: str | None
    user_product_model: str | None
    serial_number: str | None
    ios_version: str | None
    is_verified: bool | None
    focal_length_mm: float | None
    f_number: float | None
    firmware_platform: int | None
    #: The device's own capability-negotiation surface: every ``can_*`` flag it
    #: reported, verbatim. Use these rather than probing for behaviour.
    capabilities: Mapping[str, bool]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DeviceIdentity:
        return cls(
            name=_as_str(payload.get("name")),
            firmware_version=_as_str(payload.get("firmware_ver_string")),
            firmware_version_int=_as_int(payload.get("firmware_ver_int")),
            product_model=_as_str(payload.get("product_model")),
            user_product_model=_as_str(payload.get("user_product_model")),
            serial_number=_as_str(payload.get("sn")),
            ios_version=_as_str(payload.get("ios_ver")),
            is_verified=_as_bool(payload.get("is_verified")),
            focal_length_mm=_as_float(payload.get("focal_len")),
            f_number=_as_float(payload.get("fnumber")),
            firmware_platform=_as_int(payload.get("firmware_platform")),
            capabilities={
                str(key): value
                for key, value in payload.items()
                if str(key).startswith("can_") and isinstance(value, bool)
            },
        )


@dataclass(frozen=True, slots=True)
class MountState:
    """``get_device_state`` key ``mount``.

    Observed idle: ``{"move_type":"none","close":true,"tracking":false,"equ_mode":true}``.

    ``close`` is the arm stow state — ``True`` means the arm is folded and
    **nothing will move** until it is opened. ``equ_mode`` selects EQ vs alt-az,
    which decides whether field rotation is constant.
    """

    move_type: str | None
    close: bool | None
    tracking: bool | None
    equ_mode: bool | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MountState:
        return cls(
            move_type=_as_str(payload.get("move_type")),
            close=_as_bool(payload.get("close")),
            tracking=_as_bool(payload.get("tracking")),
            equ_mode=_as_bool(payload.get("equ_mode")),
        )


@dataclass(frozen=True, slots=True)
class FocuserSummary:
    """``get_device_state`` key ``focuser``: ``{"state","max_step","step"}``."""

    state: str | None
    max_step: int | None
    step: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> FocuserSummary:
        return cls(
            state=_as_str(payload.get("state")),
            max_step=_as_int(payload.get("max_step")),
            step=_as_int(payload.get("step")),
        )


@dataclass(frozen=True, slots=True)
class CameraSummary:
    """``get_device_state`` key ``camera``."""

    chip_size: ChipSize | None
    pixel_size_um: float | None
    debayer_pattern: str | None
    hot_pixel_count: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CameraSummary:
        return cls(
            chip_size=ChipSize.from_chip_size(payload.get("chip_size")),
            pixel_size_um=_as_float(payload.get("pixel_size_um")),
            debayer_pattern=_as_str(payload.get("debayer_pattern")),
            hot_pixel_count=_as_int(payload.get("hpc_num")),
        )


@dataclass(frozen=True, slots=True)
class PiStatus:
    """``get_device_state`` key ``pi_status`` — thermals and battery."""

    temp_c: float | None
    charger_status: str | None
    battery_capacity: int | None
    charge_online: bool | None
    is_overtemp: bool | None
    battery_temp_c: float | None
    battery_temp_type: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> PiStatus:
        return cls(
            temp_c=_as_float(payload.get("temp")),
            charger_status=_as_str(payload.get("charger_status")),
            battery_capacity=_as_int(payload.get("battery_capacity")),
            charge_online=_as_bool(payload.get("charge_online")),
            is_overtemp=_as_bool(payload.get("is_overtemp")),
            battery_temp_c=_as_float(payload.get("battery_temp")),
            battery_temp_type=_as_str(payload.get("battery_temp_type")),
        )


@dataclass(frozen=True, slots=True)
class BalanceSensor:
    """``get_device_state`` key ``balance_sensor`` — the levelling readout.

    Wraps its own ``code``/``data`` envelope; ``angle`` is in degrees.
    """

    x: float | None
    y: float | None
    z: float | None
    angle_deg: float | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> BalanceSensor:
        data = _as_mapping(payload.get("data"))
        return cls(
            x=_as_float(data.get("x")),
            y=_as_float(data.get("y")),
            z=_as_float(data.get("z")),
            angle_deg=_as_float(data.get("angle")),
        )


@dataclass(frozen=True, slots=True)
class StorageState:
    """``get_device_state`` key ``storage``.

    ``storage_volume`` was elided in the capture, so it is passed through raw
    rather than modelled from a guess.
    """

    connected_storage: tuple[str, ...] | None
    current_storage: str | None
    volumes: tuple[Mapping[str, object], ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> StorageState:
        raw_volumes = payload.get("storage_volume")
        volumes: tuple[Mapping[str, object], ...] = ()
        if isinstance(raw_volumes, Sequence) and not isinstance(raw_volumes, str):
            volumes = tuple(_as_mapping(item) for item in raw_volumes if isinstance(item, Mapping))
        return cls(
            connected_storage=_as_str_list(payload.get("connected_storage")),
            current_storage=_as_str(payload.get("cur_storage")),
            volumes=volumes,
        )


@dataclass(frozen=True, slots=True)
class DeviceState:
    """The subset of ``get_device_state`` this driver requests.

    Only keys that carry no credentials are modelled. ``ap`` and ``setting`` are
    deliberately absent — see :data:`CREDENTIAL_BEARING_KEYS`.
    """

    device: DeviceIdentity | None = None
    mount: MountState | None = None
    focuser: FocuserSummary | None = None
    camera: CameraSummary | None = None
    pi_status: PiStatus | None = None
    storage: StorageState | None = None
    balance_sensor: BalanceSensor | None = None
    #: Keys that came back present but ``{}``.
    empty_keys: tuple[str, ...] = ()
    #: Keys that were requested and are **not in the reply at all**. Recorded on
    #: 8.46: an unrecognised key is dropped silently with ``code`` 0. So this
    #: means "no data OR not a real key" and is reported rather than being
    #: silently read as "this device does not have that feature".
    missing_keys: tuple[str, ...] = ()

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object], requested: Iterable[str] = ()
    ) -> DeviceState:
        def section(key: str) -> Mapping[str, object] | None:
            value = payload.get(key)
            if not isinstance(value, Mapping):
                return None
            mapped = _as_mapping(value)
            return mapped or None

        empty = tuple(
            str(key) for key, value in payload.items() if isinstance(value, Mapping) and not value
        )
        missing = tuple(key for key in requested if key not in payload)
        device = section("device")
        mount = section("mount")
        focuser = section("focuser")
        camera = section("camera")
        pi_status = section("pi_status")
        storage = section("storage")
        balance = section("balance_sensor")
        return cls(
            device=DeviceIdentity.from_payload(device) if device else None,
            mount=MountState.from_payload(mount) if mount else None,
            focuser=FocuserSummary.from_payload(focuser) if focuser else None,
            camera=CameraSummary.from_payload(camera) if camera else None,
            pi_status=PiStatus.from_payload(pi_status) if pi_status else None,
            storage=StorageState.from_payload(storage) if storage else None,
            balance_sensor=BalanceSensor.from_payload(balance) if balance else None,
            empty_keys=empty,
            missing_keys=missing,
        )


# --------------------------------------------------------------------------- #
# Dedicated getters
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CameraInfo:
    """``get_camera_info``."""

    chip_size: ChipSize | None
    pixel_size_um: float | None
    bins: tuple[int, ...] | None
    is_color: bool | None
    debayer_pattern: str | None
    has_cooler: bool | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CameraInfo:
        return cls(
            chip_size=ChipSize.from_chip_size(payload.get("chip_size")),
            pixel_size_um=_as_float(payload.get("pixel_size_um")),
            bins=_as_int_list(payload.get("bins")),
            is_color=_as_bool(payload.get("is_color")),
            debayer_pattern=_as_str(payload.get("debayer_pattern")),
            has_cooler=_as_bool(payload.get("has_cooler")),
        )


@dataclass(frozen=True, slots=True)
class CameraState:
    """``get_camera_state``: ``{"state":"idle","name":"imx585","path":"..."}``."""

    state: str | None
    name: str | None
    path: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CameraState:
        return cls(
            state=_as_str(payload.get("state")),
            name=_as_str(payload.get("name")),
            path=_as_str(payload.get("path")),
        )


@dataclass(frozen=True, slots=True)
class ExposureAndBin:
    """``get_camera_exp_and_bin``. **Exposure is in microseconds.**"""

    exposure_us: int | None
    bin: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ExposureAndBin:
        return cls(
            exposure_us=_as_int(payload.get("exposure")),
            bin=_as_int(payload.get("bin")),
        )


@dataclass(frozen=True, slots=True)
class FocuserState:
    """``get_focuser_state``."""

    state: str | None
    name: str | None
    focuser_id: int | None
    max_step_range: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> FocuserState:
        return cls(
            state=_as_str(payload.get("state")),
            name=_as_str(payload.get("name")),
            focuser_id=_as_int(payload.get("id")),
            max_step_range=_as_int(payload.get("max_step_range")),
        )


@dataclass(frozen=True, slots=True)
class WheelState:
    """``get_wheel_state``: the LP filter wheel."""

    wheel_id: int | None
    state: str | None
    unidirection: bool | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> WheelState:
        return cls(
            wheel_id=_as_int(payload.get("id")),
            state=_as_str(payload.get("state")),
            unidirection=_as_bool(payload.get("unidirection")),
        )


@dataclass(frozen=True, slots=True)
class WheelSetting:
    """``get_wheel_setting``.

    ``names`` maps straight onto ASCOM ``FilterWheel.Names``. Wheel **position is
    1-based on the wire** (``get_wheel_position`` returned ``1``); ASCOM is
    0-based, and that conversion belongs to ``ssc/alpaca/``, not here.
    """

    names: tuple[str, ...] | None
    exposure_seconds: tuple[float, ...] | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> WheelSetting:
        return cls(
            names=_as_str_list(payload.get("names")),
            exposure_seconds=_as_float_list(payload.get("exp_sec")),
        )


@dataclass(frozen=True, slots=True)
class StackInfo:
    """``get_stack_info``: ``{"width":0,"height":0}`` when nothing is stacked."""

    width: int | None
    height: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> StackInfo:
        return cls(width=_as_int(payload.get("width")), height=_as_int(payload.get("height")))


@dataclass(frozen=True, slots=True)
class DiskVolume:
    """``get_disk_volume``: ``{"totalMB":…,"freeMB":…}``."""

    total_mb: int | None
    free_mb: int | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DiskVolume:
        return cls(total_mb=_as_int(payload.get("totalMB")), free_mb=_as_int(payload.get("freeMB")))


@dataclass(frozen=True, slots=True)
class StationState:
    """``pi_station_state`` — the device's Wi-Fi client state.

    ``key_mgmt`` names the security mode (not a key), so it is kept. No password
    field appears in this payload and none is modelled.
    """

    ip: str | None
    gateway: str | None
    netmask: str | None
    ssid: str | None
    freq: int | None
    signal_level: int | None
    key_mgmt: str | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> StationState:
        return cls(
            ip=_as_str(payload.get("ip")),
            gateway=_as_str(payload.get("gateway")),
            netmask=_as_str(payload.get("netmask")),
            ssid=_as_str(payload.get("ssid")),
            freq=_as_int(payload.get("freq")),
            signal_level=_as_int(payload.get("sig_lev")),
            key_mgmt=_as_str(payload.get("key_mgmt")),
        )
