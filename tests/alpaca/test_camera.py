"""The camera mapping: sensor geometry, ranges read from the device, honest gaps."""

from __future__ import annotations

import httpx
import pytest

from ssc.alpaca.devices.camera import CAMERA_IDLE, SENSOR_RGGB, US_PER_S
from ssc.alpaca.errors import ErrorNumber
from ssc.seestar.emulator import SeestarEmulator
from tests.alpaca.conftest import error_number, override_method, put_error_number, value


async def test_the_chip_size_preserves_the_portrait_sensor_axes(linked: httpx.AsyncClient) -> None:
    """``chip_size`` is ``[width, height]``. Getting this backwards is expensive."""
    assert await value(linked, "camera", "cameraxsize") == 2160
    assert await value(linked, "camera", "cameraysize") == 3840
    assert await value(linked, "camera", "numx") == 2160
    assert await value(linked, "camera", "numy") == 3840
    assert await value(linked, "camera", "startx") == 0
    assert await value(linked, "camera", "starty") == 0


async def test_pixel_size_and_binning_come_from_the_device(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "camera", "pixelsizex") == pytest.approx(2.9)
    assert await value(linked, "camera", "pixelsizey") == pytest.approx(2.9)
    assert await value(linked, "camera", "maxbinx") == 2  # bins [1, 2]
    assert await value(linked, "camera", "maxbiny") == 2
    assert await value(linked, "camera", "binx") == 1
    assert await value(linked, "camera", "canasymmetricbin") is False


async def test_gain_limits_are_read_from_get_controls(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "camera", "gainmin") == 0
    assert await value(linked, "camera", "gainmax") == 600


async def test_the_current_gain_is_unknown_rather_than_invented(
    linked: httpx.AsyncClient,
) -> None:
    """``get_controls`` describes ranges and carries no ``value`` field."""
    assert await error_number(linked, "camera", "gain") == ErrorNumber.VALUE_NOT_SET


async def test_gains_and_offsets_are_notimplemented_not_empty_lists(
    linked: httpx.AsyncClient,
) -> None:
    for member in ("gains", "offset", "offsetmin", "offsetmax", "offsets"):
        assert await error_number(linked, "camera", member) == ErrorNumber.NOT_IMPLEMENTED, member


async def test_exposure_limits_are_converted_from_microseconds(
    linked: httpx.AsyncClient,
) -> None:
    assert await value(linked, "camera", "exposuremin") == pytest.approx(30 / US_PER_S)
    assert await value(linked, "camera", "exposuremax") == pytest.approx(2_000_000_000 / US_PER_S)
    assert await value(linked, "camera", "exposureresolution") == pytest.approx(1 / US_PER_S)


async def test_a_missing_control_is_valuenotset(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(emulator, "get_controls", [{"name": "Exposure", "min": 30, "max": 100}])
    assert await error_number(linked, "camera", "gainmax") == ErrorNumber.VALUE_NOT_SET


async def test_the_colour_sensor_maps_to_rggb_with_the_right_bayer_origin(
    linked: httpx.AsyncClient,
) -> None:
    """debayer_pattern "GR" is GRBG: red sits one column across, same row."""
    assert await value(linked, "camera", "sensortype") == SENSOR_RGGB
    assert await value(linked, "camera", "bayeroffsetx") == 1
    assert await value(linked, "camera", "bayeroffsety") == 0


async def test_an_unknown_bayer_pattern_is_not_guessed(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(
        emulator,
        "get_camera_info",
        {"chip_size": [2160, 3840], "is_color": True, "debayer_pattern": "ZZ", "bins": [1]},
    )
    assert await error_number(linked, "camera", "bayeroffsetx") == ErrorNumber.VALUE_NOT_SET


async def test_camera_state_maps_only_states_that_were_observed(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    assert await value(linked, "camera", "camerastate") == CAMERA_IDLE
    assert await value(linked, "camera", "sensorname") == "imx585"

    override_method(emulator, "get_camera_state", {"state": "somethingnew", "name": "imx585"})
    assert await error_number(linked, "camera", "camerastate") == ErrorNumber.VALUE_NOT_SET


async def test_there_is_no_cooler_and_the_driver_says_so(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "camera", "cansetccdtemperature") is False
    assert await value(linked, "camera", "cangetcoolerpower") is False
    for member in ("ccdtemperature", "cooleron", "coolerpower", "setccdtemperature"):
        assert await error_number(linked, "camera", member) == ErrorNumber.NOT_IMPLEMENTED, member


async def test_there_is_no_exposure_path_and_the_flags_agree(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "camera", "imageready") is False
    assert await value(linked, "camera", "canabortexposure") is False
    assert await value(linked, "camera", "canstopexposure") is False
    for member in ("startexposure", "abortexposure", "stopexposure"):
        assert (
            await put_error_number(linked, "camera", member, Duration=1.0, Light=True)
            == ErrorNumber.NOT_IMPLEMENTED
        ), member
    for member in ("imagearray", "imagearrayvariant", "percentcompleted", "maxadu"):
        assert await error_number(linked, "camera", member) in {
            ErrorNumber.NOT_IMPLEMENTED,
            ErrorNumber.VALUE_NOT_SET,
        }, member


async def test_writing_a_control_needs_a_verified_verb(linked: httpx.AsyncClient) -> None:
    for member, param in (("gain", "Gain"), ("binx", "BinX"), ("biny", "BinY")):
        assert (
            await put_error_number(linked, "camera", member, **{param: 1})
            == ErrorNumber.NOT_IMPLEMENTED
        ), member


async def test_the_subframe_is_the_full_frame_and_says_so(linked: httpx.AsyncClient) -> None:
    """Accepting a value it already holds is truthful; accepting one it ignores is not."""
    assert (await put_error_number(linked, "camera", "numx", NumX=1920)) == (
        ErrorNumber.INVALID_VALUE
    )
    assert (await put_error_number(linked, "camera", "startx", StartX=10)) == (
        ErrorNumber.INVALID_VALUE
    )

    ok = await linked.put("/api/v1/camera/0/numx", data={"NumX": "2160"})
    assert ok.json()["ErrorNumber"] == 0
    ok = await linked.put("/api/v1/camera/0/starty", data={"StartY": "0"})
    assert ok.json()["ErrorNumber"] == 0


async def test_the_heat_sink_temperature_is_not_the_mainboard_temperature(
    linked: httpx.AsyncClient,
) -> None:
    assert (
        await error_number(linked, "camera", "heatsinktemperature") == ErrorNumber.NOT_IMPLEMENTED
    )
