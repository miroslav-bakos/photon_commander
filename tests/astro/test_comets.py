# SPDX-License-Identifier: GPL-3.0-only

"""Offline comet catalogue search and a pinned Horizons position check."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ssc.astro.comets import COMET_SNAPSHOT_MAX_AGE_DAYS, comet_position, load_comet_catalog
from ssc.astro.events import Ephemeris, Site
from ssc.astro.units import Deg, Metres


def test_catalog_is_current_searchable_and_typo_tolerant() -> None:
    catalog = load_comet_catalog()
    assert len(catalog.objects) >= 3_000
    assert "NASA/JPL" in catalog.source
    assert catalog.source_url.startswith("https://ssd-api.jpl.nasa.gov/sbdb_query.api?")
    assert catalog.suggest("Halley", 1)[0].designation == "1P"
    assert catalog.suggest("1P", 1)[0].designation == "1P"
    assert catalog.suggest("Haly", 1)[0].designation == "1P"
    assert catalog.suggest("Tsuchinshan-ATLAS", 1)[0].designation == "2023 A3"
    assert next(obj for obj in catalog.objects if obj.designation == "-146 P1").two_body_model


def test_snapshot_staleness_is_explicit_at_the_documented_boundary() -> None:
    catalog = load_comet_catalog()
    assert not catalog.is_stale(catalog.fetched_at + timedelta(days=COMET_SNAPSHOT_MAX_AGE_DAYS))
    assert catalog.is_stale(
        catalog.fetched_at + timedelta(days=COMET_SNAPSHOT_MAX_AGE_DAYS, seconds=1)
    )


def test_c2023_a3_position_matches_pinned_horizons_reference(ephemeris: Ephemeris) -> None:
    """Two-body SBDB preview stays within 1.5 arcmin of JPL Horizons here.

    Horizons includes perturbations; this deliberately looser-than-planet
    tolerance guards unit/frame/site mistakes while not claiming that an
    osculating two-body preview is a precision pointing ephemeris.
    """
    fixture = json.loads(
        (Path(__file__).with_name("comet_reference.json")).read_text(encoding="utf-8")
    )
    site_data = fixture["site"]
    site = Site(
        latitude_deg=Deg(site_data["latitude_deg"]),
        longitude_deg=Deg(site_data["longitude_deg"]),
        elevation_m=Metres(site_data["elevation_m"]),
    )
    comet = load_comet_catalog().suggest("C/2023 A3", 1)[0]
    result = comet_position(
        ephemeris, site, comet, datetime.fromisoformat(fixture["instant_utc"]).astimezone(UTC)
    )
    expected_ra_h = 17 + 43 / 60 + 56.92 / 3600
    expected_dec_deg = 16 + 52 / 60 + 24.5 / 3600
    assert float(result.ra_j2000_h) == pytest.approx(expected_ra_h, abs=0.1 / 60)
    assert float(result.dec_j2000_deg) == pytest.approx(expected_dec_deg, abs=1.5 / 60)
    assert result.element_age_days > 800
