# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The cutout-autofetch-on-resolve setting (``ssc.settings.models.SkymapSettings``)
and its one call site (``GET /api/astro/target``).

Off by default — commit ``b0c4155`` removed automatic cutout fetching on
resolve because it was surprising and expensive; this setting brings it back
as something a user opts into. Both directions are asserted here, not just
"on", because a test that only proves the "on" path leaves the default
untested (dispatch brief).
"""

from __future__ import annotations

import pytest

from tests.api.conftest import TestClient, services_of


def test_skymap_autofetch_is_off_by_default(paired: TestClient) -> None:
    response = paired.get("/api/settings/skymap-autofetch")
    assert response.status_code == 200, response.text
    assert response.json() == {"cutout_autofetch_enabled": False}


def test_skymap_autofetch_toggle_round_trips(paired: TestClient) -> None:
    response = paired.put("/api/settings/skymap-autofetch", json={"cutout_autofetch_enabled": True})
    assert response.status_code == 200, response.text
    assert response.json() == {"cutout_autofetch_enabled": True}
    assert paired.get("/api/settings/skymap-autofetch").json() == {"cutout_autofetch_enabled": True}


def test_skymap_autofetch_toggle_rejects_unknown_fields(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/skymap-autofetch",
        json={"cutout_autofetch_enabled": True, "extra": "nope"},
    )
    assert response.status_code == 422, response.text


def _spy_schedule_autofetch(client: TestClient) -> list[tuple[float, float]]:
    calls: list[tuple[float, float]] = []
    services_of(client).cutouts.schedule_autofetch = (  # type: ignore[method-assign]
        lambda ra_deg, dec_deg: calls.append((ra_deg, dec_deg))
    )
    return calls


def test_resolving_a_target_never_autofetches_while_the_setting_is_off(
    paired: TestClient,
) -> None:
    calls = _spy_schedule_autofetch(paired)
    response = paired.get("/api/astro/target", params={"query": "M31"})
    assert response.status_code == 200, response.text
    assert calls == []


def test_resolving_a_target_autofetches_exactly_once_while_the_setting_is_on(
    paired: TestClient,
) -> None:
    enabled = paired.put("/api/settings/skymap-autofetch", json={"cutout_autofetch_enabled": True})
    assert enabled.status_code == 200, enabled.text
    calls = _spy_schedule_autofetch(paired)

    response = paired.get("/api/astro/target", params={"query": "M31"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(calls) == 1
    ra_deg, dec_deg = calls[0]
    assert ra_deg == pytest.approx(body["ra_j2000_h"] * 15.0)
    assert dec_deg == pytest.approx(body["dec_j2000_deg"])
