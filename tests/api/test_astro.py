"""The Planning tab's HTTP surface (DESIGN.md 3): profiles, target resolution,
framing, mosaic and night — all pure computation, no device required.

Framing/mosaic/night need the pinned ``de440s`` ephemeris kernel
(``ssc.astro.events.load_ephemeris``); tests that need it are skipped when it
is not present on disk, exactly like ``tests/astro/conftest.py``, and
``SSC_REQUIRE_EPHEMERIS=1`` turns that skip into a failure so the gate cannot
go green by not running.
"""

from __future__ import annotations

import os
from itertools import pairwise

import pytest

from ssc.astro.catalog import MAX_CONE_LIMIT, MAX_CONE_RADIUS_DEG
from ssc.astro.events import EphemerisMissingError, load_ephemeris
from ssc.settings.config import AppConfig
from tests.api.conftest import FakeGateway, TestClient


def _ephemeris_available() -> bool:
    try:
        with load_ephemeris():
            return True
    except EphemerisMissingError:
        return False


requires_ephemeris = pytest.mark.skipif(
    not _ephemeris_available() and os.environ.get("SSC_REQUIRE_EPHEMERIS") != "1",
    reason="ephemeris kernel not present; see ssc.astro.events.load_ephemeris",
)


def _set_location(client: TestClient, *, latitude: float = 45.0, longitude: float = 10.0) -> None:
    response = client.put(
        "/api/settings/location/fix",
        json={
            "source": "manual",
            "latitude_deg": latitude,
            "longitude_deg": longitude,
            "elevation_m": 100.0,
        },
    )
    assert response.status_code == 200, response.text


def _set_high_horizon(client: TestClient) -> None:
    """A mask so tall (89 deg everywhere) nothing but the zenith clears it —
    used to prove the horizon profile is actually reached, not just accepted."""
    response = client.put(
        "/api/settings/horizon",
        json={
            "points": [
                {"azimuth_deg": 0.0, "altitude_deg": 89.0},
                {"azimuth_deg": 180.0, "altitude_deg": 89.0},
            ]
        },
    )
    assert response.status_code == 200, response.text


def _framing_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "ra_j2000_h": 6.0,
        "dec_j2000_deg": 20.0,
        "profile_id": "zwo-s30pro",
        "optic_id": "tele",
        "mount": "altaz",
        "start_time": "2026-01-15T20:00:00+00:00",
        "session_length_hours": 6.0,
    }
    body.update(overrides)
    return body


def _polygon_area(points: list[dict[str, float]]) -> float:
    """The shoelace formula. Every shape here is a closed polygon with the
    closing edge implied (``ssc.astro.framing.Shape``'s own convention)."""
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]["x_deg"], points[i]["y_deg"]
        x2, y2 = points[(i + 1) % n]["x_deg"], points[(i + 1) % n]["y_deg"]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


# -- profiles and target resolution: no ephemeris, no location needed --------- #


def test_profiles_lists_all_four_bundled_scope_profiles(paired: TestClient) -> None:
    response = paired.get("/api/astro/profiles")
    assert response.status_code == 200, response.text
    profiles = {p["id"]: p for p in response.json()["profiles"]}
    assert set(profiles) == {"zwo-s30", "zwo-s30pro", "zwo-s50", "zwo-s50pro"}
    profile = profiles["zwo-s30pro"]
    optic_ids = {optic["id"] for optic in profile["optics"]}
    assert {"tele", "wide"} <= optic_ids
    assert profile["calibration"]["measured"] is False
    assert profile["capabilities"]["wide_camera"] is True
    assert {item["defaults"]["gain"] for item in profiles.values()} == {200}
    assert profile["source_urls"]


def test_resolve_a_known_target_by_messier_number(paired: TestClient) -> None:
    response = paired.get("/api/astro/target", params={"query": "M31"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "M31" or "M31" in body["designations"]
    assert 0.0 <= body["ra_j2000_h"] < 24.0
    assert -90.0 <= body["dec_j2000_deg"] <= 90.0
    assert "HEASARC" in body["source"]


def test_resolve_ic_and_sharpless_designations_credit_their_source(paired: TestClient) -> None:
    """The merged catalogue reaches the API with no wiring beyond the loader,
    and `source` tells the UI whose data it is showing (CC BY-SA attribution)."""
    ic = paired.get("/api/astro/target", params={"query": "Sh 2-101"}).json()
    assert ic["name"] == "Sh2-101" and "HEASARC" in ic["source"]
    ngc = paired.get("/api/astro/target", params={"query": "ic1396"}).json()
    assert ngc["name"] == "IC 1396" and "OpenNGC" in ngc["source"]


def test_resolve_an_unknown_target_is_a_404(paired: TestClient) -> None:
    response = paired.get("/api/astro/target", params={"query": "not a real target"})
    assert response.status_code == 404


def test_target_suggestions_are_ranked_alias_aware_and_offline(paired: TestClient) -> None:
    response = paired.get("/api/astro/target-suggestions", params={"query": "Viel Nebula"})
    assert response.status_code == 200, response.text
    suggestions = response.json()["suggestions"]
    assert suggestions[0]["name"] == "NGC 6960"
    assert suggestions[0]["common_name"] == "Veil Nebula"
    assert "NGC 6960" in suggestions[0]["designations"]


def test_target_suggestions_return_clear_empty_list(paired: TestClient) -> None:
    response = paired.get(
        "/api/astro/target-suggestions", params={"query": "DefinitelyNotARealTargetXYZ"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"suggestions": []}


@requires_ephemeris
def test_comet_suggestions_resolve_name_and_retain_orbit_provenance(paired: TestClient) -> None:
    _set_location(paired, latitude=48.1486, longitude=17.1077)
    response = paired.get(
        "/api/astro/comet-suggestions",
        params={"query": "Haly", "instant": "2026-09-13T00:00:00Z"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unavailable"] == []
    assert "NASA/JPL" in body["source"]
    assert body["source_url"].startswith("https://ssd-api.jpl.nasa.gov/sbdb_query.api?")
    assert body["snapshot_age_days"] >= 0
    assert isinstance(body["stale"], bool)
    halley = body["comets"][0]
    assert halley["designation"] == "1P"
    assert halley["full_name"] == "1P/Halley"
    assert halley["resolved_at"] == "2026-09-13T00:00:00Z"
    assert halley["element_epoch_jd_tdb"] > 2_400_000
    assert halley["element_age_days"] > 20_000
    assert halley["solution_date"] is not None
    assert halley["execution_safe"] is False
    assert halley["execution_rejection"]


def test_comet_suggestions_require_location(paired: TestClient) -> None:
    response = paired.get(
        "/api/astro/comet-suggestions",
        params={"query": "Halley", "instant": "2026-09-13T00:00:00Z"},
    )
    assert response.status_code == 409


@requires_ephemeris
def test_comet_suggestions_explain_ephemeris_range(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.get(
        "/api/astro/comet-suggestions",
        params={"query": "Halley", "instant": "2200-09-13T00:00:00Z"},
    )
    assert response.status_code == 422
    assert "1849-2150" in response.json()["detail"]


def test_constellation_at_polaris_is_ursa_minor(paired: TestClient) -> None:
    """HIP 11767 (Polaris), J2000 RA 02h31m49.09s Dec +89d15m50.8s —
    Hipparcos Catalogue (ESA 1997) via SIMBAD, the same reference point
    ``tests/astro/test_constellations.py`` checks directly."""
    response = paired.get(
        "/api/astro/constellation",
        params={
            "ra_j2000_h": 2 + 31 / 60 + 49.09 / 3600,
            "dec_j2000_deg": 89 + 15 / 60 + 50.8 / 3600,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"abbreviation": "UMI", "name": "Ursa Minor"}


def test_constellation_at_galactic_centre_is_sagittarius(paired: TestClient) -> None:
    """Sgr A*, J2000 RA 17h45m40.04s Dec -29d00m28.1s (Reid & Brunthaler
    2004, ApJ 616, 872)."""
    response = paired.get(
        "/api/astro/constellation",
        params={"ra_j2000_h": 17 + 45 / 60 + 40.04 / 3600, "dec_j2000_deg": -(29 + 28.1 / 3600)},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"abbreviation": "SGR", "name": "Sagittarius"}


def test_constellation_at_rejects_ra_out_of_range(paired: TestClient) -> None:
    response = paired.get(
        "/api/astro/constellation", params={"ra_j2000_h": 24.0, "dec_j2000_deg": 0.0}
    )
    assert response.status_code == 422


def test_constellation_at_requires_no_location(paired: TestClient) -> None:
    """Pure coordinate lookup: unlike framing/mosaic/night, no site is
    involved, so it must not 409 even with no location configured."""
    response = paired.get(
        "/api/astro/constellation", params={"ra_j2000_h": 6.75, "dec_j2000_deg": -16.0}
    )
    assert response.status_code == 200, response.text


def test_constellation_boundaries_returns_all_88_regions_and_the_data_credit(
    paired: TestClient,
) -> None:
    """`ssc.astro.constellations.boundaries_j2000`'s J2000 payload, exposed
    verbatim — the frontend must never precess this itself
    (skymap-viz.md)."""
    from ssc.astro.constellations import boundaries_credit

    response = paired.get("/api/astro/constellation-boundaries")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["regions"]) == 88
    assert body["credit"] == boundaries_credit()
    total_vertices = sum(len(ring) for region in body["regions"] for ring in region["rings"])
    assert total_vertices == 1562
    ori = next(r for r in body["regions"] if r["abbreviation"] == "ORI")
    assert ori["name"] == "Orion"
    for ring in ori["rings"]:
        for vertex in ring:
            assert 0.0 <= vertex["ra_j2000_deg"] < 360.0
            assert -90.0 <= vertex["dec_j2000_deg"] <= 90.0


def test_constellation_boundaries_requires_no_location(paired: TestClient) -> None:
    """Same "pure lookup, no site needed" rule as `/constellation`."""
    response = paired.get("/api/astro/constellation-boundaries")
    assert response.status_code == 200, response.text


def test_field_objects_in_the_m31_field_are_the_andromeda_trio_largest_first(
    paired: TestClient,
) -> None:
    """A 1.5 deg cone on M31 holds M31, M32 and M110, largest first.

    M31's own record is owned by the public-domain Messier core, whose
    HEASARC cross-check table carries no position angle — but the loader
    fills that field from OpenNGC (CC BY-SA) rather than leaving it ``null``,
    since the core never set it (see
    ``test_messier_core_owns_m31_but_is_filled_by_openngc``). ``source``
    still credits the core alone; ``sources`` names both files.
    """
    m31 = paired.get("/api/astro/target", params={"query": "M31"}).json()
    assert "HEASARC" in m31["source"]
    assert m31["source"] in m31["sources"]
    assert any("OpenNGC" in s for s in m31["sources"])
    response = paired.get(
        "/api/astro/objects",
        params={
            "ra_deg": m31["ra_j2000_h"] * 15.0,
            "dec_deg": m31["dec_j2000_deg"],
            "radius_deg": 1.5,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    by_name = {obj["designation"]: obj for obj in body["objects"]}
    assert {"M31", "M32", "M110"} <= set(by_name)
    assert body["objects"][0]["designation"] == "M31", "largest first"
    assert by_name["M31"]["position_angle_deg"] == pytest.approx(35.0)
    assert "HEASARC" in by_name["M31"]["source"]
    assert any("OpenNGC" in s for s in by_name["M31"]["sources"])
    assert by_name["M31"]["size_major_arcmin"] > by_name["M32"]["size_major_arcmin"]
    assert by_name["M31"]["source"] in body["sources"]
    assert len(body["sources"]) == len(set(body["sources"]))


def test_field_objects_carry_the_position_angle_where_the_source_gives_it(
    paired: TestClient,
) -> None:
    """NGC 891 (edge-on, OpenNGC PA 22 deg): the value the ellipse is oriented by."""
    ngc891 = paired.get("/api/astro/target", params={"query": "NGC 891"}).json()
    # M11's "add position_angle_deg" task, closed: a resolved target's own
    # detail view carries the same PA its map outline is already oriented
    # by, not just the field-objects listing below.
    assert ngc891["position_angle_deg"] == pytest.approx(22.0)
    body = paired.get(
        "/api/astro/objects",
        params={
            "ra_deg": ngc891["ra_j2000_h"] * 15.0,
            "dec_deg": ngc891["dec_j2000_deg"],
            "radius_deg": 0.5,
        },
    ).json()
    by_name = {obj["designation"]: obj for obj in body["objects"]}
    assert by_name["NGC 891"]["position_angle_deg"] == pytest.approx(22.0)
    assert "OpenNGC" in by_name["NGC 891"]["source"]


def test_field_objects_refuses_a_cone_wider_than_a_field(paired: TestClient) -> None:
    # Derived from the constants, never literals: this asserted a hardcoded
    # 10.5 and started passing 200 the day `MAX_CONE_RADIUS_DEG` rose to 60
    # to cover a 60-degree view. The route already takes its bound from the
    # constant, so only the test could drift.
    too_wide = paired.get(
        "/api/astro/objects",
        params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": MAX_CONE_RADIUS_DEG + 0.5},
    )
    assert too_wide.status_code == 422
    too_many = paired.get(
        "/api/astro/objects",
        params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": 1.0, "limit": MAX_CONE_LIMIT + 1},
    )
    assert too_many.status_code == 422
    at_the_ceiling = paired.get(
        "/api/astro/objects",
        params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": MAX_CONE_RADIUS_DEG},
    )
    assert at_the_ceiling.status_code == 200, at_the_ceiling.text


def test_bright_stars_in_the_orion_belt_field_are_named_and_brightest_first(
    paired: TestClient,
) -> None:
    """A 10-degree cone on the belt (RA 83, Dec 1) holds the belt trio and
    both shoulders, brightest star first — same field
    ``tests/astro/test_stars.py`` proves against the module directly."""
    response = paired.get(
        "/api/astro/stars",
        params={"ra_deg": 83.0, "dec_deg": 1.0, "radius_deg": 10.0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    names = {star["name"] for star in body["stars"]}
    for expected in ("Betelgeuse", "Bellatrix", "Alnitak", "Alnilam", "Mintaka"):
        assert expected in names
    magnitudes = [star["magnitude"] for star in body["stars"]]
    assert magnitudes == sorted(magnitudes), "brightest first"
    assert body["stars"][0]["name"] == "Betelgeuse"
    betelgeuse = body["stars"][0]
    assert betelgeuse["designation"] == "HR 2061"
    assert "IAU" in body["credit"]


def test_bright_stars_refuses_a_cone_wider_than_a_field(paired: TestClient) -> None:
    # Same drift as the objects test above — derived, not literal.
    too_wide = paired.get(
        "/api/astro/stars",
        params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": MAX_CONE_RADIUS_DEG + 0.5},
    )
    assert too_wide.status_code == 422
    too_many = paired.get(
        "/api/astro/stars",
        params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": 1.0, "limit": MAX_CONE_LIMIT + 1},
    )
    assert too_many.status_code == 422
    at_the_ceiling = paired.get(
        "/api/astro/stars",
        params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": MAX_CONE_RADIUS_DEG},
    )
    assert at_the_ceiling.status_code == 200, at_the_ceiling.text


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_bright_stars_requires_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    response = client.get(
        "/api/astro/stars", params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": 1.0}
    )
    assert response.status_code == (401 if config.auth_required else 200)


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_astro_endpoints_require_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    """With auth off, none of these is gated any more (CLAUDE.md 2026-09-09) —
    checked as "not a 401" rather than pinning each one's exact success code,
    since several of them have their own, unrelated preconditions (a location,
    an ephemeris kernel) this test is not the place to re-litigate."""
    gated = bool(config.auth_required)
    assert (client.get("/api/astro/profiles").status_code == 401) is gated
    assert (
        client.get(
            "/api/astro/objects", params={"ra_deg": 10.0, "dec_deg": 41.0, "radius_deg": 1.0}
        ).status_code
        == 401
    ) is gated
    assert (client.get("/api/astro/target", params={"query": "M31"}).status_code == 401) is gated
    assert (
        client.get("/api/astro/target-suggestions", params={"query": "M3"}).status_code == 401
    ) is gated
    assert (
        client.get(
            "/api/astro/constellation", params={"ra_j2000_h": 6.75, "dec_j2000_deg": -16.0}
        ).status_code
        == 401
    ) is gated
    assert (client.post("/api/astro/framing", json=_framing_body()).status_code == 401) is gated


# -- location is required for every computed endpoint -------------------------- #


def test_framing_without_a_location_is_a_clear_409(paired: TestClient) -> None:
    response = paired.post("/api/astro/framing", json=_framing_body())
    assert response.status_code == 409
    assert "Settings" in response.text


def test_framing_rejects_an_unknown_profile(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.post("/api/astro/framing", json=_framing_body(profile_id="not-a-profile"))
    assert response.status_code == 422


# -- framing: the invariant that makes the feature meaningful ------------------ #


@requires_ephemeris
def test_framing_keep_region_is_strictly_smaller_than_the_swept_envelope(
    paired: TestClient,
) -> None:
    _set_location(paired)
    response = paired.post("/api/astro/framing", json=_framing_body())
    assert response.status_code == 200, response.text
    envelope = response.json()["envelope"]

    # A real alt-az session over 6 hours rotates the field — this is the
    # premise the area comparison below depends on, so assert it explicitly
    # rather than let a degenerate (non-rotating) session pass silently.
    assert abs(envelope["total_rotation_deg"]) > 1.0

    keep_area = _polygon_area(envelope["keep_region"]["points"])
    swept_area = _polygon_area(envelope["swept_envelope"]["points"])
    assert keep_area < swept_area

    # The same invariant, restated per-angle (SessionEnvelope's own docstring):
    # keep_radii_deg[i] <= swept_radii_deg[i] everywhere, strictly somewhere.
    keep_radii = envelope["keep_radii_deg"]
    swept_radii = envelope["swept_radii_deg"]
    assert all(k <= s for k, s in zip(keep_radii, swept_radii, strict=True))
    assert any(k < s for k, s in zip(keep_radii, swept_radii, strict=True))


@requires_ephemeris
def test_framing_pa_samples_carry_a_timestamp_and_pa_for_the_frame_stack(
    paired: TestClient,
) -> None:
    """DESIGN.md 3.2: the frame stack and time scrubber both draw from this one
    fetched-once sample set — each sample needs a timestamp and a PA."""
    _set_location(paired)
    response = paired.post("/api/astro/framing", json=_framing_body())
    samples = response.json()["envelope"]["samples"]
    assert len(samples) >= 2
    for sample in samples:
        assert sample.get("when")
        assert "position_angle_deg" in sample
    # Adaptive, not uniform: consecutive elapsed_hours steps are not all equal.
    steps = {round(b["elapsed_hours"] - a["elapsed_hours"], 9) for a, b in pairwise(samples)}
    assert len(steps) > 1


@requires_ephemeris
def test_framing_eq_mode_has_no_rotation(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.post("/api/astro/framing", json=_framing_body(mount="eq"))
    assert response.status_code == 200, response.text
    envelope = response.json()["envelope"]
    assert envelope["total_rotation_deg"] == 0.0
    assert envelope["rotation_severity"] == "none"
    assert envelope["frame_start"] == envelope["frame_end"]


@requires_ephemeris
def test_framing_reports_no_device_check_when_nothing_is_connected(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.post("/api/astro/framing", json=_framing_body())
    check = response.json()["device_mount_check"]
    assert check == {
        "checked": False,
        "device_equ_mode": None,
        "conflict": False,
        "message": None,
    }


@requires_ephemeris
def test_framing_flags_a_conflicting_connected_device_mode(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """Mount mode is a request field, never inferred from the device (DESIGN.md
    3.2 amendment): a connected EQ mount must not block planning an alt-az
    session, but the mismatch is surfaced so the UI can say so."""
    _set_location(paired)
    gateway.mount_equ_mode = True
    response = paired.post("/api/astro/framing", json=_framing_body(mount="altaz"))
    assert response.status_code == 200, response.text
    check = response.json()["device_mount_check"]
    assert check["checked"] is True
    assert check["device_equ_mode"] is True
    assert check["conflict"] is True
    assert "EQ" in check["message"]
    # The plan itself is unaffected — still alt-az, still rotates.
    assert response.json()["mount"] == "altaz"


@requires_ephemeris
def test_framing_reports_no_conflict_when_modes_agree(
    paired: TestClient, gateway: FakeGateway
) -> None:
    _set_location(paired)
    gateway.mount_equ_mode = False  # alt-az, matches the request below
    response = paired.post("/api/astro/framing", json=_framing_body(mount="altaz"))
    check = response.json()["device_mount_check"]
    assert check["checked"] is True
    assert check["conflict"] is False
    assert check["message"] is None


# -- framing: horizon-aware visibility (DESIGN.md §5) --------------------------- #


@requires_ephemeris
def test_framing_visibility_falls_back_to_the_true_horizon_when_no_profile_is_set(
    paired: TestClient,
) -> None:
    _set_location(paired)
    response = paired.post("/api/astro/framing", json=_framing_body())
    assert response.status_code == 200, response.text
    visibility = response.json()["visibility"]
    assert visibility["horizon_profile_active"] is False
    assert isinstance(visibility["clear_intervals"], list)


@requires_ephemeris
def test_framing_visibility_honours_a_recorded_horizon_profile(paired: TestClient) -> None:
    _set_location(paired)
    _set_high_horizon(paired)
    response = paired.post("/api/astro/framing", json=_framing_body())
    assert response.status_code == 200, response.text
    visibility = response.json()["visibility"]
    assert visibility["horizon_profile_active"] is True
    # A mask at 89 deg blocks everything but the zenith — the target framed
    # here is not there, so the session never clears it.
    assert visibility["start_above_horizon"] is False
    assert visibility["start_horizon_altitude_deg"] == pytest.approx(89.0)
    assert visibility["clear_intervals"] == []


# -- mosaic --------------------------------------------------------------------- #


@requires_ephemeris
def test_mosaic_returns_a_panel_grid_with_a_per_panel_envelope(paired: TestClient) -> None:
    _set_location(paired)
    body = _framing_body(mosaic={"ra_num": 2, "dec_num": 2, "overlap_pct": 20.0})
    response = paired.post("/api/astro/mosaic", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["panel_count"] == 4
    assert len(payload["panels"]) == 4
    rows = {panel["center"]["row"] for panel in payload["panels"]}
    cols = {panel["center"]["col"] for panel in payload["panels"]}
    assert rows == {0, 1}
    assert cols == {0, 1}
    for panel in payload["panels"]:
        assert panel["envelope"]["samples"]


def test_mosaic_without_a_location_is_a_clear_409(paired: TestClient) -> None:
    body = _framing_body(mosaic={"ra_num": 1, "dec_num": 1, "overlap_pct": 0.0})
    response = paired.post("/api/astro/mosaic", json=body)
    assert response.status_code == 409


# -- night ------------------------------------------------------------------------ #


@requires_ephemeris
def test_night_outside_ephemeris_range_is_an_explained_422(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.post(
        "/api/astro/night",
        json={
            "start_time": "2200-01-15T12:00:00+00:00",
            "end_time": "2200-01-16T12:00:00+00:00",
        },
    )
    assert response.status_code == 422, response.text
    assert "DE440s" in response.text and "1849-2150" in response.text


@requires_ephemeris
def test_night_without_a_target_has_twilight_and_moon_but_no_altitude_curve(
    paired: TestClient,
) -> None:
    _set_location(paired)
    response = paired.post(
        "/api/astro/night",
        json={
            "start_time": "2026-01-15T12:00:00+00:00",
            "end_time": "2026-01-16T12:00:00+00:00",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["solar_events"]) > 0
    assert len(body["lunar_events"]) > 0
    assert 0.0 <= body["moon"]["illuminated_fraction"] <= 1.0
    assert body["moon"]["separation_deg"] is None
    assert body["target_events"] is None
    assert body["altitude_curve"] is None
    assert body["moon_separation_curve"] is None
    assert body["horizon_profile_active"] is False
    assert body["visibility_intervals"] is None


@requires_ephemeris
def test_night_with_a_target_returns_events_and_an_altitude_curve(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.post(
        "/api/astro/night",
        json={
            "start_time": "2026-01-15T12:00:00+00:00",
            "end_time": "2026-01-16T12:00:00+00:00",
            "ra_j2000_h": 6.0,
            "dec_j2000_deg": 20.0,
            "altitude_samples": 25,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_events"] is not None
    assert body["altitude_curve"] is not None
    assert len(body["altitude_curve"]) == 25
    assert len(body["moon_separation_curve"]) == 25
    assert all(0 <= sample["separation_deg"] <= 180 for sample in body["moon_separation_curve"])
    assert body["moon"]["separation_deg"] is not None
    # No profile recorded: visibility falls back to the flat true horizon, the
    # same code path a recorded profile would use (item 4 of the horizon spec).
    assert body["horizon_profile_active"] is False
    assert body["visibility_intervals"] is not None


@requires_ephemeris
def test_night_visibility_honours_a_recorded_horizon_profile(paired: TestClient) -> None:
    _set_location(paired)
    _set_high_horizon(paired)
    response = paired.post(
        "/api/astro/night",
        json={
            "start_time": "2026-01-15T12:00:00+00:00",
            "end_time": "2026-01-16T12:00:00+00:00",
            "ra_j2000_h": 6.0,
            "dec_j2000_deg": 20.0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["horizon_profile_active"] is True
    # An 89 deg mask blocks the target for the whole window — a real interval
    # list (possibly empty), never flattened to a single rise/set pair, and
    # never silently reporting the flat-limit's "visible" answer instead.
    assert body["visibility_intervals"] == []
    # target_events is untouched: it is still the flat rise/transit/set summary.
    assert body["target_events"] is not None


def test_night_rejects_ra_without_dec(paired: TestClient) -> None:
    response = paired.post(
        "/api/astro/night",
        json={
            "start_time": "2026-01-15T12:00:00+00:00",
            "end_time": "2026-01-16T12:00:00+00:00",
            "ra_j2000_h": 6.0,
        },
    )
    assert response.status_code == 422


def test_night_rejects_an_end_before_the_start(paired: TestClient) -> None:
    response = paired.post(
        "/api/astro/night",
        json={
            "start_time": "2026-01-16T12:00:00+00:00",
            "end_time": "2026-01-15T12:00:00+00:00",
        },
    )
    assert response.status_code == 422


def test_a_naive_datetime_is_rejected(paired: TestClient) -> None:
    response = paired.post(
        "/api/astro/framing",
        json=_framing_body(start_time="2026-01-15T20:00:00"),
    )
    assert response.status_code == 422


@requires_ephemeris
def test_moon_altitude_spans_the_window_and_honours_the_step(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.get(
        "/api/astro/moon-altitude",
        params={
            "start": "2026-01-15T18:00:00+00:00",
            "end": "2026-01-16T06:00:00+00:00",
            "step_min": 60.0,
        },
    )
    assert response.status_code == 200, response.text
    samples = response.json()["samples"]
    # 12 hours at a 60-minute step: 13 samples, first and last at the window edges.
    assert len(samples) == 13
    assert samples[0]["at"] == "2026-01-15T18:00:00Z"
    assert samples[-1]["at"] == "2026-01-16T06:00:00Z"
    for sample in samples:
        assert -90.0 <= sample["alt_deg"] <= 90.0


@requires_ephemeris
def test_moon_altitude_defaults_to_a_ten_minute_step(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.get(
        "/api/astro/moon-altitude",
        params={
            "start": "2026-01-15T18:00:00+00:00",
            "end": "2026-01-15T19:00:00+00:00",
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["samples"]) == 7


def test_moon_altitude_without_a_location_is_a_clear_409(paired: TestClient) -> None:
    response = paired.get(
        "/api/astro/moon-altitude",
        params={
            "start": "2026-01-15T18:00:00+00:00",
            "end": "2026-01-16T06:00:00+00:00",
        },
    )
    assert response.status_code == 409


def test_moon_altitude_rejects_an_end_before_the_start(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.get(
        "/api/astro/moon-altitude",
        params={
            "start": "2026-01-16T06:00:00+00:00",
            "end": "2026-01-15T18:00:00+00:00",
        },
    )
    assert response.status_code == 422


# -- best targets tonight (DESIGN.md 3.5) ---------------------------------------- #


def _best_targets_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "start_time": "2026-01-15T12:00:00+00:00",
        "end_time": "2026-01-16T12:00:00+00:00",
        "min_altitude_deg": 25.0,
        "top_n": 10,
        "profile_id": "zwo-s30pro",
        "optic_id": "tele",
    }
    body.update(overrides)
    return body


@requires_ephemeris
def test_best_targets_returns_a_ranked_list_with_full_components(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.post("/api/astro/best-targets", json=_best_targets_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["darkness_hours"] > 0.0
    assert 0.0 <= body["moon_illuminated_fraction"] <= 1.0
    assert body["candidates_considered"] > 0
    assert len(body["targets"]) == 10
    scores = [t["score"] for t in body["targets"]]
    assert scores == sorted(scores, reverse=True)
    for target in body["targets"]:
        assert target["hours_above_limit"] > 0.0
        assert target["peak_altitude_deg"] >= 25.0
        assert target["moon_sensitivity"] in {
            "narrowband_friendly",
            "mixed",
            "cluster",
            "broadband",
            "dark_sky_required",
            "neutral",
        }
        assert target["moon_impact"] in {
            "moon_down",
            "narrowband_tolerant",
            "far_enough",
            "interfering",
        }
        assert target.get("designation")


@requires_ephemeris
def test_best_targets_excludes_galaxies_on_a_near_full_moon_night(paired: TestClient) -> None:
    """The user's own acceptance criterion: "we don't want to offer galaxies
    during full moon and so on"."""
    _set_location(paired)
    response = paired.post(
        "/api/astro/best-targets",
        json=_best_targets_body(
            start_time="2026-01-03T12:00:00+00:00", end_time="2026-01-04T12:00:00+00:00"
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["moon_illuminated_fraction"] > 0.9
    galaxy_types = {
        "galaxy",
        "galaxy_spiral",
        "galaxy_elliptical",
        "galaxy_lenticular",
        "galaxy_irregular",
        "galaxy_pair",
        "galaxy_triplet",
        "galaxy_group",
    }
    assert all(t["object_type"] not in galaxy_types for t in body["targets"])


def test_best_targets_without_a_location_is_a_clear_409(paired: TestClient) -> None:
    response = paired.post("/api/astro/best-targets", json=_best_targets_body())
    assert response.status_code == 409


def test_best_targets_rejects_an_unknown_profile(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.post(
        "/api/astro/best-targets", json=_best_targets_body(profile_id="not-a-profile")
    )
    assert response.status_code == 422


def test_best_targets_rejects_an_end_before_the_start(paired: TestClient) -> None:
    response = paired.post(
        "/api/astro/best-targets",
        json=_best_targets_body(
            start_time="2026-01-16T12:00:00+00:00", end_time="2026-01-15T12:00:00+00:00"
        ),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_best_targets_requires_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    response = client.post("/api/astro/best-targets", json=_best_targets_body())
    assert (response.status_code == 401) is bool(config.auth_required)


# -- solar-system bodies -------------------------------------------------------- #


def test_bodies_without_a_location_is_a_clear_409(paired: TestClient) -> None:
    """Topocentric, so it needs a real place: the Moon's parallax reaches
    ~1 deg and a guessed location would put it a diameter out."""
    response = paired.get("/api/astro/bodies")
    assert response.status_code == 409
    assert "Settings" in response.text


@requires_ephemeris
def test_bodies_at_a_pinned_instant_matches_the_astro_core(paired: TestClient) -> None:
    """The endpoint is a wrapper; the numbers are already validated against
    JPL Horizons in ``tests/astro/test_bodies.py``. What is checked here is
    that the wrapper does not lose or reshape anything: nine bodies, the
    contract's field names, the Sun's phase fields null rather than zero, and
    the Moon a half-degree disk."""
    _set_location(paired, latitude=48.15, longitude=17.11)
    response = paired.get("/api/astro/bodies", params={"instant": "2026-09-09T21:00:00Z"})
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["instant"].startswith("2026-09-09T21:00:00")
    assert "DE440s" in payload["credit"]
    assert [body["id"] for body in payload["bodies"]] == [
        "sun",
        "moon",
        "mercury",
        "venus",
        "mars",
        "jupiter",
        "saturn",
        "uranus",
        "neptune",
    ]

    by_id = {body["id"]: body for body in payload["bodies"]}
    assert by_id["sun"]["kind"] == "sun"
    assert by_id["moon"]["kind"] == "moon"
    assert by_id["jupiter"]["kind"] == "planet"
    assert by_id["jupiter"]["name"] == "Jupiter"

    # None, not 0.0: the Sun is its own light source, so illumination does not
    # apply — and 0.0 would read as "unlit", the opposite of the truth.
    assert by_id["sun"]["illuminated_fraction"] is None
    assert by_id["sun"]["phase_angle_deg"] is None

    # The Moon is a ~2% waning crescent at this instant (Horizons: 2.00039%).
    assert by_id["moon"]["illuminated_fraction"] == pytest.approx(0.0200, abs=1e-3)
    assert by_id["moon"]["apparent_diameter_arcmin"] == pytest.approx(31.8, abs=0.6)
    assert by_id["sun"]["apparent_diameter_arcmin"] == pytest.approx(31.7, abs=0.6)
    # A planet's disk is a marker, not a drawable circle.
    assert by_id["jupiter"]["apparent_diameter_arcmin"] < 1.0


@requires_ephemeris
def test_bodies_defaults_to_now_and_echoes_the_instant_used(paired: TestClient) -> None:
    """``instant`` is optional. The clock is read at the boundary, never inside
    ``ssc.astro``, and the instant used comes back so the caller knows it."""
    _set_location(paired)
    response = paired.get("/api/astro/bodies")
    assert response.status_code == 200, response.text
    instant = response.json()["instant"]
    assert instant.endswith("Z") or "+00:00" in instant


def test_bodies_rejects_an_instant_without_an_offset(paired: TestClient) -> None:
    """Invariant 5: a bare local-looking timestamp is refused rather than
    silently assumed to be UTC."""
    _set_location(paired)
    response = paired.get("/api/astro/bodies", params={"instant": "2026-09-09T21:00:00"})
    assert response.status_code == 422
    assert "UTC offset" in response.text


@requires_ephemeris
def test_bodies_outside_ephemeris_range_is_an_explained_422(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.get("/api/astro/bodies", params={"instant": "2200-01-15T21:00:00Z"})
    assert response.status_code == 422, response.text
    assert "DE440s" in response.text and "1849-2150" in response.text


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_bodies_requires_a_session_iff_auth_required(client: TestClient, config: AppConfig) -> None:
    response = client.get("/api/astro/bodies")
    assert (response.status_code == 401) is bool(config.auth_required)
