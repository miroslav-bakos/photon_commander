"""Shared fixtures for the astro core tests."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ssc.astro.events import Ephemeris, EphemerisMissingError, Site, load_ephemeris
from ssc.astro.framing import Profile, load_profile
from ssc.astro.units import Deg, Metres

REFERENCE_PATH = Path(__file__).parent / "almanac_reference.json"
PROFILE_PATH = Path(__file__).resolve().parents[2] / "data" / "profiles" / "zwo-s30pro.toml"


@pytest.fixture(scope="session")
def ephemeris() -> Iterator[Ephemeris]:
    """The pinned de440s kernel.

    Absent means skipped, loudly and with the fetch command in the reason —
    never quietly downloaded. Set ``SSC_REQUIRE_EPHEMERIS=1`` (as CI should,
    once it caches the kernel) to turn the skip into a failure, so that the M3
    acceptance gate cannot go green by not running.
    """
    try:
        loaded = load_ephemeris()
    except EphemerisMissingError as exc:
        if os.environ.get("SSC_REQUIRE_EPHEMERIS") == "1":
            pytest.fail(str(exc))
        pytest.skip(str(exc))
    with loaded as handle:
        yield handle


@pytest.fixture(scope="session")
def profile() -> Profile:
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="session")
def almanac() -> dict[str, Any]:
    """The checked-in USNO/IMCCE reference table, with its provenance."""
    with REFERENCE_PATH.open("rb") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


def site_of(almanac_data: dict[str, Any], site_id: str) -> Site:
    """Build a :class:`Site` from a fixture row's site id.

    Elevation is 0 m for every fixture site: the USNO API takes no elevation, so
    comparing at sea level is the only way to compare like with like. The
    horizon dip is exercised separately in ``test_events.py``.
    """
    for entry in almanac_data["sites"]:
        if entry["id"] == site_id:
            return Site(
                latitude_deg=Deg(entry["latitude_deg"]),
                longitude_deg=Deg(entry["longitude_deg"]),
                elevation_m=Metres(entry["elevation_m"]),
            )
    raise KeyError(site_id)


def utc_day(date_utc: str) -> tuple[datetime, datetime]:
    """The [00:00, 24:00) UTC window the almanac tabulates for a date."""
    start = datetime.fromisoformat(date_utc).replace(tzinfo=UTC)
    return start, start + timedelta(days=1)


def reference_time(date_utc: str, hhmmss: str) -> datetime:
    """Parse an ``HH:MM`` or ``HH:MM:SS`` almanac reading on a given UTC date."""
    parts = [int(piece) for piece in hhmmss.split(":")]
    while len(parts) < 3:
        parts.append(0)
    start, _ = utc_day(date_utc)
    return start + timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2])
