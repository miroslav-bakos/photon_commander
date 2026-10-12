"""The filter wheel: names straight from the device, and the 1-based/0-based seam."""

from __future__ import annotations

import httpx
import pytest

from ssc.alpaca.devices.filterwheel import POSITION_MOVING, ascom_to_wire, wire_to_ascom
from ssc.alpaca.errors import ErrorNumber
from ssc.seestar.emulator import SeestarEmulator
from tests.alpaca.conftest import error_number, override_method, put_error_number, value


@pytest.mark.parametrize("wire", [1, 2, 3])
def test_the_position_origin_converts_both_ways(wire: int) -> None:
    assert ascom_to_wire(wire_to_ascom(wire)) == wire
    assert wire_to_ascom(wire) == wire - 1


async def test_names_come_straight_from_get_wheel_setting(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "filterwheel", "names") == ["dark", "IRCUT", "LP"]


async def test_focus_offsets_are_zero_per_slot_not_the_exposure_hints(
    linked: httpx.AsyncClient,
) -> None:
    """``exp_sec`` sits next to ``names`` and is seconds, not focuser steps."""
    assert await value(linked, "filterwheel", "focusoffsets") == [0, 0, 0]


@pytest.mark.parametrize(("wire", "ascom"), [(1, 0), (2, 1), (3, 2)])
async def test_the_wire_is_one_based_and_ascom_is_zero_based(
    linked: httpx.AsyncClient, emulator: SeestarEmulator, wire: int, ascom: int
) -> None:
    override_method(emulator, "get_wheel_position", wire)
    assert await value(linked, "filterwheel", "position") == ascom


async def test_a_moving_wheel_reports_minus_one(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(emulator, "get_wheel_state", {"id": 0, "state": "moving"})
    assert await value(linked, "filterwheel", "position") == POSITION_MOVING


async def test_a_slot_outside_the_name_list_is_not_reported_as_a_position(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(emulator, "get_wheel_position", 9)
    assert await error_number(linked, "filterwheel", "position") == ErrorNumber.VALUE_NOT_SET


async def test_missing_names_are_valuenotset(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(emulator, "get_wheel_setting", {"exp_sec": [2.0]})
    assert await error_number(linked, "filterwheel", "names") == ErrorNumber.VALUE_NOT_SET


async def test_moving_the_wheel_needs_a_verified_verb(linked: httpx.AsyncClient) -> None:
    assert (
        await put_error_number(linked, "filterwheel", "position", Position=1)
        == ErrorNumber.NOT_IMPLEMENTED
    )


async def test_the_filterwheel_interface_version_is_v2(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "filterwheel", "interfaceversion") == 2
