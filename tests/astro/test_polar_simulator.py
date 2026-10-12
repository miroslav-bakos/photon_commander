# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
"""The interactive artificial sky goes through the production PA fitter."""

from datetime import UTC, datetime

import pytest

from ssc.astro.events import Ephemeris, Site
from ssc.astro.polar_simulator import simulate_polar_alignment
from ssc.astro.units import Deg

SITE = Site(latitude_deg=Deg(48.15), longitude_deg=Deg(17.11))
WHEN = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize("count", [3, 5])
def test_simulated_wedge_changes_are_recalculated_from_new_solved_points(
    ephemeris: Ephemeris, count: int
) -> None:
    before_points, before = simulate_polar_alignment(
        ephemeris, SITE, WHEN, altitude_adjustment_deg=0, azimuth_adjustment_deg=0, point_count=count
    )
    after_points, after = simulate_polar_alignment(
        ephemeris, SITE, WHEN, altitude_adjustment_deg=-1, azimuth_adjustment_deg=1, point_count=count
    )
    assert len(before_points) == len(after_points) == count
    assert before_points != after_points
    assert float(before.total_error_deg) > 1.0
    assert float(after.total_error_deg) * 60 < 0.2
    if count == 3:
        assert after.rms_residual_arcsec is None
    else:
        assert after.rms_residual_arcsec is not None


def test_simulated_wedge_direction_and_step_size(ephemeris: Ephemeris) -> None:
    _, baseline = simulate_polar_alignment(
        ephemeris, SITE, WHEN, altitude_adjustment_deg=0, azimuth_adjustment_deg=0, point_count=3
    )
    _, moved = simulate_polar_alignment(
        ephemeris, SITE, WHEN, altitude_adjustment_deg=-10 / 3600, azimuth_adjustment_deg=60 / 3600, point_count=3
    )
    assert float(moved.altitude_error_deg) < float(baseline.altitude_error_deg)
    assert float(moved.azimuth_error_deg) > float(baseline.azimuth_error_deg)
