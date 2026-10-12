# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""ASCOM ``ICameraV3`` over the Seestar's IMX585.

``get_controls`` is the source of truth for gain and exposure. It reports
``Gain`` 0-600 and ``Exposure`` 30-2 000 000 000 microseconds on firmware 8.46,
and this driver *reads* it rather than hardcoding either range — a firmware that
changes the limits changes SSC's limits with it.

Two shapes that are easy to get wrong and are handled explicitly:

* ``chip_size`` is ``[width, height]``. The S30 Pro's sensor/output is
  portrait: 2160 x 3840, matching native Alpaca and Seestar-authored FITS.
* Exposure is **microseconds on the wire** and **seconds in ASCOM**.

Imaging itself is absent on firmware 8.46 by design as well as by necessity: no
``start_`` exposure verb is verified, and ``DESIGN.md`` §2.1 routes the live
preview through its own stream rather than Alpaca ``ImageArray``.

**On 9.16+, that reasoning no longer holds.** The device's own native Alpaca
server (``ssc.seestar.native``) serves ``StartExposure``, ``ImageReady`` and
``ImageArray`` as ordinary ``ICameraV3`` members, and this driver passes them
straight through (``prefer_native_getters``/``prefer_native_setters``,
``ssc/alpaca/devices/base.py``) exactly as it already does for Telescope,
FilterWheel and Focuser. Measured 9.31 (``docs/M0-FINDINGS.md``):

* ``ImageArray`` is served **only** through the ImageBytes binary transfer
  (``Accept: application/imagebytes``, ``ssc/alpaca/imagebytes.py`` encodes it
  and ``ssc/client/imagebytes.py`` is what decoded the device's own copy to
  build it) — never the plain JSON array, which is 2.5x the size for the same
  frame. A client that does not ask for ImageBytes gets ``NotImplemented``,
  and so does ``ImageArrayVariant``, which has no ImageBytes equivalent.
* One frame is 16 588 844 bytes: rank 2, **2160 x 3840**, uint16, ``DataStart``
  44. ``NumX``/``NumY``/``StartX``/``StartY`` are accepted and answered as
  success but **not honoured** — the device always sends the full sensor — so
  this driver keeps refusing anything but the full frame on every firmware
  rather than relaying that quiet lie.
* Both cameras (``camera/0`` telephoto, ``camera/1`` wide) expose
  independently and need their own ``Connected = True``; this driver serves
  both, but ``camera/1`` has no JSON-RPC route at all (see
  ``NO_JSON_RPC_ROUTE_TO_SECOND_CAMERA``) and works only once the native
  Alpaca surface is active.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Final

from ssc.alpaca.devices.base import (
    AlpacaDevice,
    Getter,
    Setter,
    const,
    unimplemented,
    unimplemented_put,
)
from ssc.alpaca.errors import (
    AlpacaNotImplementedError,
    InvalidValueError,
    NotConnectedError,
    ValueNotSetError,
)
from ssc.alpaca.wire import RequestParams
from ssc.client.imagebytes import ImageBytesFrame
from ssc.seestar.models import CameraInfo, ChipSize

#: Microseconds per second — the only unit conversion in this file, written once.
US_PER_S: Final = 1_000_000.0

#: ASCOM ``CameraStates``.
CAMERA_IDLE: Final = 0
CAMERA_WAITING: Final = 1
CAMERA_EXPOSING: Final = 2
CAMERA_READING: Final = 3
CAMERA_DOWNLOAD: Final = 4
CAMERA_ERROR: Final = 5

#: Device ``get_camera_state.state`` strings seen on 8.46. Anything else is
#: reported as unknown rather than folded into "idle".
CAMERA_STATE_MAP: Final[Mapping[str, int]] = {
    "idle": CAMERA_IDLE,
    "exposing": CAMERA_EXPOSING,
    "reading": CAMERA_READING,
    "error": CAMERA_ERROR,
}

#: ASCOM ``SensorType``.
SENSOR_MONOCHROME: Final = 0
SENSOR_RGGB: Final = 2

#: ZWO names a colour sensor's CFA by its top-left 2x2 pair. ASCOM expresses the
#: same thing as ``SensorType.RGGB`` plus the offset of the pattern's origin.
BAYER_OFFSETS: Final[Mapping[str, tuple[int, int]]] = {
    "RG": (0, 0),
    "GR": (1, 0),
    "GB": (0, 1),
    "BG": (1, 1),
}

#: The controls this driver reads out of ``get_controls``.
CONTROL_GAIN: Final = "Gain"
CONTROL_EXPOSURE: Final = "Exposure"

#: Why ``Camera.ImageArray`` answers ``NotImplemented`` on firmware 8.46 — no
#: start-exposure verb exists there at all, so there is nothing to transfer.
#: Kept at module level so :meth:`CameraDevice._get_image_array_json` can use
#: it for both firmwares, not just the ``build_getters`` dict literal.
NO_IMAGE_PATH_8_46: Final = (
    "no start-exposure verb is verified on firmware 8.46, so this driver never "
    "starts an ASCOM exposure to pull a frame from"
)

#: Why ``Camera.ImageArrayVariant`` is never offered, on either firmware.
NO_IMAGE_ARRAY_VARIANT: Final = (
    "this driver offers ImageArray only through the Alpaca ImageBytes binary "
    "transfer; ImageArrayVariant's JSON encoding of the same frame is roughly "
    "2.5x the size for no benefit (docs/M0-FINDINGS.md) and is not implemented"
)

#: Why every member of ``camera/{n != 0}`` answers ``NotImplemented`` on the
#: Seestar JSON-RPC surface (firmware 8.46). ``get_camera_info`` and its
#: siblings address one sensor with no camera-selection parameter at all —
#: there is no wire vocabulary for a second camera — so nothing here may be
#: read off it and reported as if it were the wide camera's own answer. Only
#: the device's own native Alpaca server (9.16+) actually exposes ``camera/1``
#: (``prefer_native_getters``/``prefer_native_setters`` below try it first).
NO_JSON_RPC_ROUTE_TO_SECOND_CAMERA: Final = (
    "this driver's Seestar JSON-RPC mapping (firmware 8.46) has no method that "
    "addresses a second camera; camera/{n} exists only on the device's own "
    "native Alpaca surface (9.16+), and this session is not on it"
)


class CameraDevice(AlpacaDevice):
    """The imaging camera, limited to what firmware 8.46 actually reports.

    ``device_number`` 0 is the telephoto camera the 8.46 JSON-RPC mapping
    below was written for. ``device_number`` 1 is the wide camera
    (``docs/M0-FINDINGS.md`` 9.31: same 2160 x 3840 geometry, 6 mm f/1.75 at
    1.6 µm against the telephoto's 160 mm f/5.3 at 2.9 µm) — it has no JSON-RPC
    route at all, so every member of it answers ``NotImplemented`` unless the
    native Alpaca surface is active, in which case it is served directly.
    """

    device_type: ClassVar[str] = "camera"
    interface_version: ClassVar[int] = 3
    default_name: ClassVar[str] = "Photon Commander Seestar Camera"
    default_description: ClassVar[str] = "ZWO Seestar imaging camera via Photon Commander"

    # -- device reads -------------------------------------------------------- #

    async def _camera_info(self) -> CameraInfo:
        payload = await self.backend.call("get_camera_info", cache=True)
        if not isinstance(payload, Mapping):
            raise ValueNotSetError("get_camera_info did not return an object")
        return CameraInfo.from_payload(payload)

    async def _chip(self) -> ChipSize:
        info = await self._camera_info()
        if info.chip_size is None:
            raise ValueNotSetError("the device did not report chip_size")
        return info.chip_size

    async def _control(self, name: str) -> Mapping[str, object]:
        """One entry from ``get_controls``, the authority on its own range."""
        payload = await self.backend.call("get_controls", cache=True)
        if isinstance(payload, str) or not isinstance(payload, Sequence):
            raise ValueNotSetError("get_controls did not return a list of controls")
        wanted = name.casefold()
        for entry in payload:
            if isinstance(entry, Mapping) and str(entry.get("name", "")).casefold() == wanted:
                return entry
        raise ValueNotSetError(
            f"the device's get_controls list has no {name!r} control, so its range is unknown"
        )

    async def _control_number(self, control: str, field: str) -> float:
        entry = await self._control(control)
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueNotSetError(f"get_controls {control!r} has no numeric {field!r}")
        return float(value)

    async def _exposure_and_bin(self) -> Mapping[str, object]:
        payload = await self.backend.call("get_camera_exp_and_bin", cache=True)
        if not isinstance(payload, Mapping):
            raise ValueNotSetError("get_camera_exp_and_bin did not return an object")
        return payload

    # -- getters ------------------------------------------------------------- #

    def build_getters(self) -> dict[str, Getter]:
        no_cooler = "get_camera_info reports has_cooler false; this camera has no cooler"
        no_exposure_path = (
            "no start-exposure verb is verified on firmware 8.46, so this driver "
            "never starts an ASCOM exposure"
        )
        getters: dict[str, Getter] = {
            "bayeroffsetx": self._get_bayer_offset_x,
            "bayeroffsety": self._get_bayer_offset_y,
            "binx": self._get_bin,
            "biny": self._get_bin,
            "camerastate": self._get_camera_state,
            "cameraxsize": self._get_camera_x_size,
            "cameraysize": self._get_camera_y_size,
            # stop_exposure is a verified verb, but ASCOM's Abort/StopExposure
            # abort an exposure *this driver started*, and it starts none. A True
            # here would advertise a capability whose method must then refuse.
            # The verb is still reachable, as the ssc.stop_capture action.
            "canabortexposure": const(False),
            "canasymmetricbin": const(False),
            "canfastreadout": const(False),
            "cangetcoolerpower": const(False),
            "canpulseguide": const(False),
            "cansetccdtemperature": const(False),
            "canstopexposure": const(False),
            "ccdtemperature": unimplemented("Camera.CCDTemperature", no_cooler),
            "cooleron": unimplemented("Camera.CoolerOn", no_cooler),
            "coolerpower": unimplemented("Camera.CoolerPower", no_cooler),
            "electronsperadu": unimplemented(
                "Camera.ElectronsPerADU", "the device does not report sensor gain in e-/ADU"
            ),
            "exposuremax": self._get_exposure_max,
            "exposuremin": self._get_exposure_min,
            "exposureresolution": const(1.0 / US_PER_S),
            "fastreadout": unimplemented(
                "Camera.FastReadout", "the device exposes no fast-readout mode"
            ),
            "fullwellcapacity": unimplemented(
                "Camera.FullWellCapacity", "the device does not report full well capacity"
            ),
            "gain": self._get_gain,
            "gainmax": self._get_gain_max,
            "gainmin": self._get_gain_min,
            "gains": unimplemented(
                "Camera.Gains",
                "gain is a continuous range on this camera, so GainMin/GainMax are the "
                "implemented pair (ASCOM allows exactly one of the two forms)",
            ),
            "hasshutter": const(False),
            "heatsinktemperature": unimplemented(
                "Camera.HeatSinkTemperature",
                "the device reports a mainboard temperature, not a sensor heat-sink "
                "temperature; the two are not interchangeable (read it with ssc.device_state)",
            ),
            "imagearray": self._get_image_array_json,
            "imagearrayvariant": unimplemented("Camera.ImageArrayVariant", NO_IMAGE_ARRAY_VARIANT),
            "imageready": const(False),
            "ispulseguiding": unimplemented(
                "Camera.IsPulseGuiding", "the camera exposes no pulse-guide interface"
            ),
            "lastexposureduration": unimplemented("Camera.LastExposureDuration", no_exposure_path),
            "lastexposurestarttime": unimplemented(
                "Camera.LastExposureStartTime", no_exposure_path
            ),
            "maxadu": unimplemented(
                "Camera.MaxADU",
                "the device does not report the sensor's full-scale ADU value, and "
                "guessing a bit depth would corrupt every downstream calculation",
            ),
            "maxbinx": self._get_max_bin,
            "maxbiny": self._get_max_bin,
            "numx": self._get_camera_x_size,
            "numy": self._get_camera_y_size,
            "offset": unimplemented(
                "Camera.Offset", "the device's get_controls list has no offset control"
            ),
            "offsetmax": unimplemented("Camera.OffsetMax", "no offset control is reported"),
            "offsetmin": unimplemented("Camera.OffsetMin", "no offset control is reported"),
            "offsets": unimplemented("Camera.Offsets", "no offset control is reported"),
            "percentcompleted": unimplemented("Camera.PercentCompleted", no_exposure_path),
            "pixelsizex": self._get_pixel_size,
            "pixelsizey": self._get_pixel_size,
            "readoutmode": const(0),
            "readoutmodes": const(["Default"]),
            "sensorname": self._get_sensor_name,
            "sensortype": self._get_sensor_type,
            "setccdtemperature": unimplemented("Camera.SetCCDTemperature", no_cooler),
            "startx": const(0),
            "starty": const(0),
            "subexposureduration": unimplemented(
                "Camera.SubExposureDuration", "the device exposes no sub-exposure control"
            ),
        }
        if self.device_number != 0:
            # No JSON-RPC method addresses a second camera at all — see
            # NO_JSON_RPC_ROUTE_TO_SECOND_CAMERA. Every legacy answer above
            # would in fact be the telephoto's own reading, mislabelled; refuse
            # instead of relaying it. prefer_native_getters below still tries
            # the device's own camera/{n} first, so this only bites on 8.46.
            reason = NO_JSON_RPC_ROUTE_TO_SECOND_CAMERA.format(n=self.device_number)
            getters = {member: unimplemented(f"Camera.{member}", reason) for member in getters}
        wrapped = self.prefer_native_getters(getters)
        # ImageArray/ImageArrayVariant never go through the generic native
        # pass-through: a plain GET there would ask the device's own server for
        # its nested-JSON array (2.5x the size for the same frame), which this
        # driver does not relay — see _get_image_array_json and
        # NO_IMAGE_ARRAY_VARIANT. NumX/NumY/StartX/StartY keep this driver's own
        # honest refusal on every firmware too: the native surface accepts any
        # value here and answers success while always sending the full sensor
        # regardless (docs/M0-FINDINGS.md — "subframing is advertised and NOT
        # honoured"), which this driver will not relay as a truthful read.
        for member in ("imagearray", "imagearrayvariant", "numx", "numy", "startx", "starty"):
            wrapped[member] = getters[member]
        return wrapped

    async def _get_camera_x_size(self, _: RequestParams) -> object:
        return (await self._chip()).width_px

    async def _get_camera_y_size(self, _: RequestParams) -> object:
        return (await self._chip()).height_px

    async def _get_pixel_size(self, _: RequestParams) -> object:
        info = await self._camera_info()
        if info.pixel_size_um is None:
            raise ValueNotSetError("the device did not report pixel_size_um")
        return info.pixel_size_um

    async def _get_max_bin(self, _: RequestParams) -> object:
        info = await self._camera_info()
        if not info.bins:
            raise ValueNotSetError("the device did not report its supported bin factors")
        return max(info.bins)

    async def _get_bin(self, _: RequestParams) -> object:
        payload = await self._exposure_and_bin()
        value = payload.get("bin")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueNotSetError("get_camera_exp_and_bin did not report an integer bin")
        return value

    async def _get_sensor_name(self, _: RequestParams) -> object:
        payload = await self.backend.call("get_camera_state", cache=True)
        if not isinstance(payload, Mapping):
            raise ValueNotSetError("get_camera_state did not return an object")
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueNotSetError("get_camera_state did not report a sensor name")
        return name

    async def _get_camera_state(self, _: RequestParams) -> object:
        payload = await self.backend.call("get_camera_state", cache=True)
        if not isinstance(payload, Mapping):
            raise ValueNotSetError("get_camera_state did not return an object")
        state = payload.get("state")
        if not isinstance(state, str):
            raise ValueNotSetError("get_camera_state did not report a state")
        mapped = CAMERA_STATE_MAP.get(state.casefold())
        if mapped is None:
            raise ValueNotSetError(
                f"the device reported camera state {state!r}, which is not in SSC's "
                "observed state table; it is not being guessed at"
            )
        return mapped

    async def _get_sensor_type(self, _: RequestParams) -> object:
        info = await self._camera_info()
        if info.is_color is None:
            raise ValueNotSetError("the device did not report is_color")
        if not info.is_color:
            return SENSOR_MONOCHROME
        return SENSOR_RGGB

    async def _bayer_offset(self) -> tuple[int, int]:
        info = await self._camera_info()
        if info.is_color is False:
            return (0, 0)
        pattern = info.debayer_pattern
        if pattern is None:
            raise ValueNotSetError("the device did not report debayer_pattern")
        offsets = BAYER_OFFSETS.get(pattern.upper())
        if offsets is None:
            raise ValueNotSetError(
                f"the device reported debayer_pattern {pattern!r}, which is not one of "
                f"{sorted(BAYER_OFFSETS)}; the CFA origin is therefore unknown"
            )
        return offsets

    async def _get_bayer_offset_x(self, _: RequestParams) -> object:
        return (await self._bayer_offset())[0]

    async def _get_bayer_offset_y(self, _: RequestParams) -> object:
        return (await self._bayer_offset())[1]

    async def _get_gain(self, _: RequestParams) -> object:
        """The *current* gain, which firmware 8.46 does not report.

        ``get_controls`` describes each control's **range** and carries no
        ``value`` field; no other method reports the camera's gain either. The
        range is therefore readable through ``GainMin``/``GainMax`` while the
        setting itself is genuinely unknown — and an unknown gain reported as 0,
        or as the range minimum, would silently mis-calibrate every frame.
        """
        await self._control(CONTROL_GAIN)  # proves the control exists before saying why
        raise ValueNotSetError(
            "the device reports gain limits but not the current gain: get_controls "
            "carries min/max and no value, and no other method on firmware 8.46 "
            "exposes it. GainMin and GainMax are readable"
        )

    async def _get_gain_min(self, _: RequestParams) -> object:
        return int(await self._control_number(CONTROL_GAIN, "min"))

    async def _get_gain_max(self, _: RequestParams) -> object:
        return int(await self._control_number(CONTROL_GAIN, "max"))

    async def _get_exposure_min(self, _: RequestParams) -> object:
        return await self._control_number(CONTROL_EXPOSURE, "min") / US_PER_S

    async def _get_exposure_max(self, _: RequestParams) -> object:
        return await self._control_number(CONTROL_EXPOSURE, "max") / US_PER_S

    async def _get_image_array_json(self, _: RequestParams) -> object:
        """``Camera.ImageArray`` over the ordinary JSON ``GET`` dispatch.

        Never reached by a client that asks for ``Accept:
        application/imagebytes`` — ``ssc/alpaca/server.py`` routes that
        straight to :meth:`image_bytes` instead, bypassing this dict entirely.
        This is what answers a plain JSON request, and every request on
        firmware 8.46, which offers no exposure path at all.
        """
        if self.backend.native_surface_active:
            raise AlpacaNotImplementedError(
                "Camera.ImageArray is not available: this driver serves it only "
                "through the ImageBytes binary transfer (request it with "
                "Accept: application/imagebytes) — the plain JSON array is roughly "
                "2.5x the size for the same frame (docs/M0-FINDINGS.md) and is not "
                "offered"
            )
        raise AlpacaNotImplementedError(f"Camera.ImageArray is not available: {NO_IMAGE_PATH_8_46}")

    async def image_bytes(self, params: RequestParams) -> ImageBytesFrame:
        """``Camera.ImageArray``, pulled off the device's own ImageBytes transfer.

        Reached only from ``ssc/alpaca/server.py``'s dedicated binary path — an
        ``Accept: application/imagebytes`` request — never through the ordinary
        ``get()`` dispatch above, so it repeats ``get()``'s own ``Connected``
        guard (``AlpacaDevice._guard``) rather than inheriting it.
        """
        del params  # nothing this driver forwards: NumX/NumY/StartX/StartY are
        # never honoured by the device (module docstring), so no ROI parameter
        # is meaningful to pass on, and the transfer is always the full sensor.
        if not self.is_connected:
            raise NotConnectedError(
                f"{self.device_type}.ImageArray requires Connected = True "
                f"(this device is {'linked but the session is down' if self._connected else 'not connected'})"
            )
        if not self.backend.native_surface_active:
            raise AlpacaNotImplementedError(f"Camera.ImageArray is not available: {NO_IMAGE_PATH_8_46}")
        return await self.backend.native_image_bytes(self.device_type, self.device_number)

    # -- setters ------------------------------------------------------------- #

    def build_setters(self) -> dict[str, Setter]:
        no_control_verb = (
            "no verb that writes a device control is verified on firmware 8.46; "
            "SSC will not send an unverified verb to hardware"
        )
        no_cooler = "get_camera_info reports has_cooler false; this camera has no cooler"
        setters: dict[str, Setter] = {
            "abortexposure": unimplemented_put(
                "Camera.AbortExposure",
                "this driver starts no ASCOM exposures, so there is none to abort; "
                "the device's own stop_exposure verb is reachable as the "
                "ssc.stop_capture action",
            ),
            "binx": unimplemented_put("Camera.BinX", no_control_verb),
            "biny": unimplemented_put("Camera.BinY", no_control_verb),
            "cooleron": unimplemented_put("Camera.CoolerOn", no_cooler),
            "fastreadout": unimplemented_put(
                "Camera.FastReadout", "the device exposes no fast-readout mode"
            ),
            "gain": unimplemented_put("Camera.Gain", no_control_verb),
            "numx": self._put_num_x,
            "numy": self._put_num_y,
            "offset": unimplemented_put(
                "Camera.Offset", "the device's get_controls list has no offset control"
            ),
            "pulseguide": unimplemented_put(
                "Camera.PulseGuide", "the camera exposes no pulse-guide interface"
            ),
            "readoutmode": unimplemented_put(
                "Camera.ReadoutMode", "the device exposes a single readout mode"
            ),
            "setccdtemperature": unimplemented_put("Camera.SetCCDTemperature", no_cooler),
            "startexposure": unimplemented_put(
                "Camera.StartExposure",
                "no start-exposure verb is verified on firmware 8.46; use the "
                "ssc.start_capture / ssc.start_stack actions once their verbs are",
            ),
            "startx": self._put_start_x,
            "starty": self._put_start_y,
            "stopexposure": unimplemented_put(
                "Camera.StopExposure",
                "this driver starts no ASCOM exposures, so there is none to stop; "
                "the device's own stop_exposure verb is reachable as the "
                "ssc.stop_capture action",
            ),
            "subexposureduration": unimplemented_put(
                "Camera.SubExposureDuration", "the device exposes no sub-exposure control"
            ),
        }
        if self.device_number != 0:
            # Same reasoning as build_getters: no JSON-RPC verb writes anything
            # on a second camera, and _put_num_x/_put_num_y would otherwise
            # silently validate against the telephoto's own chip size.
            reason = NO_JSON_RPC_ROUTE_TO_SECOND_CAMERA.format(n=self.device_number)
            setters = {
                member: unimplemented_put(f"Camera.{member}", reason) for member in setters
            }
        wrapped = self.prefer_native_setters(setters)
        # NumX/NumY/StartX/StartY keep this driver's own honest refusal on every
        # firmware — see the matching note in build_getters.
        for member in ("numx", "numy", "startx", "starty"):
            wrapped[member] = setters[member]
        return wrapped

    async def _put_num_x(self, params: RequestParams) -> object:
        await self._require_full_frame("NumX", params.as_int("NumX"), (await self._chip()).width_px)
        return None

    async def _put_num_y(self, params: RequestParams) -> object:
        await self._require_full_frame(
            "NumY", params.as_int("NumY"), (await self._chip()).height_px
        )
        return None

    async def _put_start_x(self, params: RequestParams) -> object:
        await self._require_full_frame("StartX", params.as_int("StartX"), 0)
        return None

    async def _put_start_y(self, params: RequestParams) -> object:
        await self._require_full_frame("StartY", params.as_int("StartY"), 0)
        return None

    @staticmethod
    async def _require_full_frame(member: str, value: int, only_allowed: int) -> None:
        """Accept the value the camera is already at; refuse to pretend otherwise.

        The device exposes no verified ROI verb, so the subframe is always the
        full frame. Writing the value it already holds is a truthful no-op;
        writing anything else would be accepted and then ignored, which is the
        kind of quiet lie that shows up as a mis-framed target.
        """
        if value != only_allowed:
            raise InvalidValueError(
                f"{member} must be {only_allowed}: this driver has no verified "
                f"region-of-interest verb, so only the full frame is available"
            )


__all__ = [
    "BAYER_OFFSETS",
    "CAMERA_STATE_MAP",
    "CONTROL_EXPOSURE",
    "CONTROL_GAIN",
    "NO_IMAGE_ARRAY_VARIANT",
    "NO_IMAGE_PATH_8_46",
    "NO_JSON_RPC_ROUTE_TO_SECOND_CAMERA",
    "SENSOR_MONOCHROME",
    "SENSOR_RGGB",
    "US_PER_S",
    "CameraDevice",
]
