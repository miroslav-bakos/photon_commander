"""Shared fixtures for the scheduler compiler tests.

Mirrors ``tests/astro/conftest.py``'s ``ephemeris``/``profile`` fixtures: the
compiler needs a real loaded ephemeris to resolve ``event``/``target_event``
TimeSpecs and a real scope profile to expand mosaic panels, and both are
worth sharing the same "skip if absent, fail if SSC_REQUIRE_EPHEMERIS=1"
discipline as the astro-core tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from ssc.astro.events import Ephemeris, EphemerisMissingError, load_ephemeris
from ssc.astro.framing import Profile, load_profile

PROFILE_PATH = Path(__file__).resolve().parents[2] / "data" / "profiles" / "zwo-s30pro.toml"


@pytest.fixture(scope="session")
def ephemeris() -> Iterator[Ephemeris]:
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
