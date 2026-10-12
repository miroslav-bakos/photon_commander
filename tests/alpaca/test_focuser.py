"""The focuser mapping: readable position, no verified motion verb."""

from __future__ import annotations

import httpx

from ssc.alpaca.errors import ErrorNumber
from ssc.seestar.emulator import SeestarEmulator
from tests.alpaca.conftest import error_number, override_method, put_error_number, value


async def test_the_focuser_reports_its_range_and_position(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "focuser", "absolute") is True
    assert await value(linked, "focuser", "maxstep") == 2600
    assert await value(linked, "focuser", "maxincrement") == 2600
    assert await value(linked, "focuser", "position") == 1209
    assert await value(linked, "focuser", "ismoving") is False


async def test_ismoving_follows_the_device_state(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(
        emulator,
        "get_focuser_state",
        {"state": "moving", "name": "ZWO GPIO EAF", "id": 0, "max_step_range": 2600},
    )
    assert await value(linked, "focuser", "ismoving") is True


async def test_a_missing_range_is_valuenotset(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(emulator, "get_focuser_state", {"state": "idle", "name": "ZWO GPIO EAF"})
    assert await error_number(linked, "focuser", "maxstep") == ErrorNumber.VALUE_NOT_SET


async def test_a_non_integer_position_is_not_coerced(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_method(emulator, "get_focuser_position", "1209")
    assert await error_number(linked, "focuser", "position") == ErrorNumber.VALUE_NOT_SET


async def test_moving_the_focuser_needs_a_verified_verb(linked: httpx.AsyncClient) -> None:
    assert (
        await put_error_number(linked, "focuser", "move", Position=1300)
        == ErrorNumber.NOT_IMPLEMENTED
    )
    assert await put_error_number(linked, "focuser", "halt") == ErrorNumber.NOT_IMPLEMENTED


async def test_temperature_compensation_is_absent_not_faked(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "focuser", "tempcomp") is False
    assert await value(linked, "focuser", "tempcompavailable") is False
    assert await error_number(linked, "focuser", "temperature") == ErrorNumber.NOT_IMPLEMENTED
    assert await error_number(linked, "focuser", "stepsize") == ErrorNumber.NOT_IMPLEMENTED
    assert (
        await put_error_number(linked, "focuser", "tempcomp", TempComp=True)
        == ErrorNumber.NOT_IMPLEMENTED
    )
