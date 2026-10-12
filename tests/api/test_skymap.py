"""Sky-survey tiles through the real app: cache miss -> upstream -> hit, every
failure a 404 with a reason, batch routes absent, and a bundled survey served
with no network at all.

Upstream is ``respx``-mocked (the same pattern as the weather tests); CI runs
with sockets disabled, so nothing here can reach STScI by accident.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ssc.settings.config import AppConfig
from ssc.skymap import hips
from ssc.skymap import service as skymap_service
from ssc.state.events import STATE_EVENT_ADAPTER
from ssc.state.models import StateSnapshot
from ssc.state.reducer import replay
from tests.api.conftest import CLIENT_ADDRESS, FakeGateway, TestClient, pairing_token

from ssc.main import create_app  # isort: skip

LEGACY_SURVEYS = "legacy-surveys-dr11"
LEGACY_SURVEYS_UPSTREAM = re.compile(r"https://www\.legacysurvey\.org/viewer/ls-dr11/1/.*\.jpg")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def config(tmp_path: Path, request: pytest.FixtureRequest) -> AppConfig:
    return AppConfig(
        host="127.0.0.1",
        data_dir=tmp_path / "data",
        bundled_hips_dir=tmp_path / "bundled",
        mdns_enabled=False,
        auth_required=getattr(request, "param", None),
        event_buffer_size=64,
        reconnect_initial_delay_s=0.02,
        reconnect_max_delay_s=0.05,
    )


@pytest.fixture
def client(config: AppConfig, gateway: FakeGateway) -> Iterator[TestClient]:
    app = create_app(config, gateway=gateway)
    with TestClient(app, client=CLIENT_ADDRESS) as test_client:
        yield test_client


@pytest.fixture
def paired(client: TestClient) -> TestClient:
    response = client.post("/api/auth/pair", json={"token": pairing_token(client)})
    assert response.status_code == 200, response.text
    return client


def _state(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _wait_for_job(client: TestClient, job_id: str, *, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        jobs = _state(client)["snapshot"]["skymap"]["jobs"]
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is not None and job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


# -- the registry over HTTP ---------------------------------------------------- #


def test_surveys_lists_every_entry_with_credit_and_availability(paired: TestClient) -> None:
    body = paired.get("/api/skymap/surveys").json()
    by_id = {s["id"]: s for s in body["surveys"]}
    assert set(by_id) == {
        "dss2-color-hips-poc",
        LEGACY_SURVEYS,
        "unwise",
        "wham-halpha",
        "wise",
        "sdss",
        "nsns-halpha",
        "nsns-oiii",
        "nsns-sii",
        "nsns-ohs8",
    }
    # Pan-STARRS DR1 was removed 2026-09-08 (product owner: "completely
    # remove obsolete ugly panstarrs") — no registry entry answers to it any
    # more, and Legacy Surveys DR11 is the new default background.
    assert "panstarrs-dr1-color" not in by_id

    # The full-sky background layer (Mercator, Legacy Surveys' own scheme),
    # distinct from the per-target `legacy-surveys` cutout source below.
    ls = by_id[LEGACY_SURVEYS]
    assert ls["enabled"] is True and ls["reason"] is None
    assert ls["scheme"] == "mercator"
    assert ls["credit"] == {
        "label": "Legacy Surveys / D. Lang (Perimeter Institute)",
        "url": "https://www.legacysurvey.org/",
    }
    assert (
        ls["tile_url_template"]
        == f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/{{level}}/{{x}}/{{y}}.jpg"
    )
    # CC BY 4.0 permits redistribution, so the registry reports the licence as
    # allowing a bundle (audit 2026-09-08); no pack actually ships.
    assert ls["bundle_allowed"] is True and ls["properties_url"] is None

    # Restored deliberately for the full-sky POC; this is the existing
    # cleared all-sky infrared context, not a new source.
    assert by_id["unwise"]["enabled"] is True
    assert by_id["unwise"]["production_cleared"] is True

    dss = by_id["dss2-color-hips-poc"]
    assert dss["enabled"] is True
    assert dss["scheme"] == "hips" and dss["frame"] == "equatorial"
    assert dss["max_order"] == 9
    assert dss["production_cleared"] is False and dss["bundle_allowed"] is False
    assert "permission" in dss["rights_notice"] and "No DSS pixels are bundled" in dss["rights_notice"]
    assert (
        dss["tile_url_template"]
        == "/api/skymap/tiles/dss2-color-hips-poc/Norder{order}/Dir{dir}/Npix{npix}.jpg"
    )

    assert by_id["wise"]["enabled"] is False and "target-field" in by_id["wise"]["reason"]
    assert by_id["sdss"]["enabled"] is False and "target-field" in by_id["sdss"]["reason"]
    for survey_id in ("nsns-halpha", "nsns-oiii", "nsns-sii", "nsns-ohs8"):
        assert by_id[survey_id]["enabled"] is False
        assert "written commercial-use permission" in by_id[survey_id]["reason"]
    # The test fixture has no local pack: it remains unavailable and no
    # commercial package copies that data directory.
    wham = by_id["wham-halpha"]
    assert wham["enabled"] is False and wham["reason"].startswith("bundled H-alpha pack missing")
    assert wham["bundled"] is True and wham["blend_only"] is True
    assert body["cache_max_bytes"] > 0

    # The cutout-source registry rides alongside the tile registry: the
    # per-target `legacy-surveys` cutout (bands, optional-layer semantics) is
    # a separate registry entry from the `legacy-surveys-dr11` background
    # tile survey above, even though both come from the same archive.
    by_cutout_id = {s["id"]: s for s in body["cutout_sources"]}
    assert set(by_cutout_id) == {"unwise", "wise", "sdss", "legacy-surveys"}
    # unWISE is the one genuinely all-sky cutout source, so it leads the
    # list — the default (THIRD_PARTY_NOTICES.md "unWISE resolved, 2026-09-07").
    assert body["cutout_sources"][0]["id"] == "unwise"
    assert by_cutout_id["unwise"]["credit"] == {
        "label": "unWISE / NASA/JPL-Caltech / D. Lang (Perimeter Institute)",
        "url": "https://www.legacysurvey.org/acknowledgment/",
    }
    assert by_cutout_id["unwise"]["optional"] is False
    assert by_cutout_id["legacy-surveys"]["credit"] == {
        "label": "Legacy Surveys / D. Lang (Perimeter Institute)",
        "url": "https://www.legacysurvey.org/",
    }
    assert by_cutout_id["legacy-surveys"]["optional"] is True
    assert by_cutout_id["wise"]["bands"] == ["W1", "W2", "W3", "W4"]
    assert by_cutout_id["wise"]["default_bands"] == ["W3", "W1"]
def test_retired_dss_background_is_not_a_runtime_target(paired: TestClient) -> None:
    body = paired.get("/api/skymap/surveys").json()
    assert "dss2-color" not in {s["id"] for s in body["surveys"]}


def test_dss_remote_poc_tile_fetches_through_without_bundling(
    paired: TestClient, config: AppConfig
) -> None:
    url = "/api/skymap/tiles/dss2-color-hips-poc/Norder3/Dir0/Npix123.jpg"
    upstream = "https://alasky.cds.unistra.fr/DSS/DSSColor/Norder3/Dir0/Npix123.jpg"
    with respx.mock(assert_all_called=True) as router:
        route = router.get(upstream).mock(return_value=httpx.Response(200, content=JPG))
        response = paired.get(url)
        assert route.call_count == 1
    assert response.status_code == 200 and response.content == JPG
    assert response.headers["x-ssc-tile-source"] == "upstream"
    assert (
        config.tiles_dir
        / "dss2-color-hips-poc"
        / "Norder3"
        / "Dir0"
        / "Npix123.jpg"
    ).read_bytes() == JPG


def removed_dss_remote_poc_refuses_whole_sky_but_allows_bounded_cone_prefetch(
    paired: TestClient,
) -> None:
    whole_sky = paired.post(
        "/api/skymap/prefetch",
        json={"survey": "dss2-color-hips-poc", "mode": "sky", "max_order": 1},
    )
    assert whole_sky.status_code == 409
    assert "evaluation-only; whole-sky download is disabled" in whole_sky.json()["detail"]

    with respx.mock() as router:
        router.get(re.compile(r"https://alasky\.cds\.unistra\.fr/DSS/DSSColor/.*\.jpg")).mock(
            return_value=httpx.Response(200, content=JPG)
        )
        started = paired.post(
            "/api/skymap/prefetch",
            json={
                "survey": "dss2-color-hips-poc",
                "mode": "cone",
                "max_order": 3,
                "ra_deg": 10.0,
                "dec_deg": 20.0,
                "radius_deg": 1.0,
            },
        )
        assert started.status_code == 202, started.text
        job = _wait_for_job(paired, started.json()["job"]["id"])
    assert job["status"] == "complete", job
    assert job["done"] == job["total"] > 0

@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_surveys_requires_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    response = client.get("/api/skymap/surveys")
    assert (response.status_code == 401) is bool(config.auth_required)


# -- tiles ------------------------------------------------------------------------ #


def test_upstream_error_is_a_404_naming_the_status(paired: TestClient) -> None:
    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(return_value=httpx.Response(503))
        response = paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/1/0/0.jpg")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["reason"] == "upstream"
    assert response.json()["upstream_status"] == 503
    assert response.headers["cache-control"] == "no-store"


def test_offline_is_a_404_saying_offline(paired: TestClient) -> None:
    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(side_effect=httpx.ConnectError("no route to host"))
        response = paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/1/0/0.jpg")
    assert response.status_code == 404
    assert response.json()["reason"] == "offline"
    assert _state(paired)["snapshot"]["skymap"]["upstream_reachable"] is False


def test_an_upstream_404_is_remembered_and_never_refetched(paired: TestClient) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.get(LEGACY_SURVEYS_UPSTREAM).mock(return_value=httpx.Response(404))
        first = paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/1/0/0.jpg")
        assert first.status_code == 404 and first.json()["reason"] == "missing"
        second = paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/1/0/0.jpg")
        assert second.status_code == 404 and second.json()["reason"] == "missing"
        assert route.call_count == 1
    survey = next(
        s for s in paired.get("/api/skymap/surveys").json()["surveys"] if s["id"] == LEGACY_SURVEYS
    )
    assert survey["missing_tiles"] == 1


def test_a_disabled_survey_is_refused_with_its_reason_and_no_upstream_call(
    paired: TestClient,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(re.compile(r".*")).mock(return_value=httpx.Response(200, content=PNG))
        response = paired.get("/api/skymap/tiles/wise/3/123.png")
        assert route.call_count == 0
    assert response.status_code == 404
    assert response.json()["reason"] == "survey_unavailable"
    assert "target-field" in response.json()["detail"]


def test_unknown_survey_is_404_json(paired: TestClient) -> None:
    # `format`/`out_of_range`/`not_hips` for a real (Mercator) survey are
    # `test_mercator_out_of_range_and_bad_extension_are_404_json` below; this
    # is the one case that needs no registry entry at all.
    assert paired.get("/api/skymap/tiles/nope/1/0.png").json()["reason"] == "unknown_survey"


def test_a_bundled_survey_is_served_from_disk_with_no_network(
    paired: TestClient, config: AppConfig
) -> None:
    # The service checked the directory at startup; this app has none, so a
    # fresh app over a populated directory is what the bundled path looks like.
    wham = config.bundled_hips_dir / "wham"
    (wham / "Norder0" / "Dir0").mkdir(parents=True)
    (wham / "properties").write_text("hips_order = 0\nhips_tile_format = png\n")
    (wham / "Norder0" / "Dir0" / "Npix5.png").write_bytes(PNG)
    app = create_app(config, gateway=FakeGateway())
    with TestClient(app, client=CLIENT_ADDRESS) as fresh:
        fresh.post("/api/auth/pair", json={"token": pairing_token(fresh)})
        surveys = {s["id"]: s for s in fresh.get("/api/skymap/surveys").json()["surveys"]}
        assert surveys["wham-halpha"]["enabled"] is True
        assert (
            surveys["wham-halpha"]["properties_url"] == "/api/skymap/tiles/wham-halpha/properties"
        )
        with respx.mock(assert_all_called=False) as router:
            router.get(re.compile(r".*")).mock(side_effect=httpx.ConnectError("offline"))
            tile = fresh.get("/api/skymap/tiles/wham-halpha/0/5.png")
            assert tile.status_code == 200 and tile.content == PNG
            assert tile.headers["x-ssc-tile-source"] == "bundled"
            props = fresh.get("/api/skymap/tiles/wham-halpha/properties")
            assert props.status_code == 200 and "hips_order = 0" in props.text
            hole = fresh.get("/api/skymap/tiles/wham-halpha/0/6.png")
            assert hole.status_code == 404 and hole.json()["reason"] == "missing"


# -- Mercator tiles (Legacy Surveys) -------------------------------------------------- #


def test_mercator_cache_miss_fetches_upstream_then_serves_from_cache(
    paired: TestClient, config: AppConfig
) -> None:
    url = f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/2/1/3.jpg"
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://www.legacysurvey.org/viewer/ls-dr11/1/2/1/3.jpg").mock(
            return_value=httpx.Response(200, content=JPG)
        )
        first = paired.get(url)
        assert first.status_code == 200, first.text
        assert first.content == JPG
        assert first.headers["content-type"] == "image/jpeg"
        assert first.headers["x-ssc-tile-source"] == "upstream"
        assert "immutable" in first.headers["cache-control"]
        sent = route.calls.last.request.headers["user-agent"]
        assert sent.startswith("SSC/"), sent

    # The mock is gone: a second request must not touch the network -- this
    # is the live-panning path (architecture policy invariant 10's default survey),
    # exercised with no prefetch job ever started: a plain cache miss then
    # hit, exactly like Pan-STARRS used to work.
    second = paired.get(url)
    assert second.status_code == 200
    assert second.content == JPG
    assert second.headers["x-ssc-tile-source"] == "cache"
    assert (
        config.tiles_dir / LEGACY_SURVEYS / "mercator" / "2" / "1" / "3.jpg"
    ).read_bytes() == JPG

    survey = next(
        s for s in paired.get("/api/skymap/surveys").json()["surveys"] if s["id"] == LEGACY_SURVEYS
    )
    assert survey["cached_tiles"] == 1 and survey["cached_bytes"] == len(JPG)


def test_mercator_out_of_range_and_bad_extension_are_404_json(paired: TestClient) -> None:
    assert (
        paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/1/5/0.jpg").json()["reason"]
        == "out_of_range"
    )
    assert (
        paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/17/0/0.jpg").json()["reason"]
        == "out_of_range"
    )
    assert (
        paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/1/0/0.png").json()["reason"]
        == "format"
    )
    assert (
        paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/properties").json()["reason"] == "not_hips"
    )


# -- retired batch-download API ----------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/skymap/prefetch/estimate"),
        ("post", "/api/skymap/prefetch"),
        ("delete", "/api/skymap/prefetch/old-job"),
        ("post", "/api/skymap/cutouts/bulk"),
        ("delete", "/api/skymap/cutouts/bulk/old-job"),
    ],
)
def test_batch_download_routes_are_removed(paired: TestClient, method: str, path: str) -> None:
    response = paired.post(path, json={}) if method == "post" else paired.delete(path)
    assert response.status_code in {404, 405}


def removed_estimate_is_none_until_a_tile_size_has_been_measured(paired: TestClient) -> None:
    body = {"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 2}

    # Nothing cached and the archive unreachable: `estimate()` samples one
    # tile to answer honestly (2026-09-08: "surface cost before the download
    # starts"), and a sample that fails leaves every figure `None` -- never a
    # guess (invariant 7).
    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(side_effect=httpx.ConnectError("offline"))
        estimate = paired.post("/api/skymap/prefetch/estimate", json=body).json()
    assert estimate == {
        "survey": LEGACY_SURVEYS,
        "tile_count": 1 + 4 + 16,
        "already_cached": 0,
        "mean_tile_bytes": None,
        "estimated_bytes": None,
        "estimated_seconds": None,
    }

    # Reachable this time: the very first `estimate()` call for this survey
    # samples one real tile itself (never asked for out loud, no separate
    # browse), and that sample is what makes the rest of the figures real
    # measurements rather than nothing.
    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(return_value=httpx.Response(200, content=JPG))
        estimate = paired.post("/api/skymap/prefetch/estimate", json=body).json()
    assert estimate["mean_tile_bytes"] == len(JPG)
    assert estimate["already_cached"] == 1
    # 21 total, 1 already sampled/cached -- the *remaining* download's size,
    # not the whole pack's (a re-run over a partly-cached pack must not
    # overstate what is left to fetch).
    assert estimate["estimated_bytes"] == 20 * len(JPG)
    assert estimate["estimated_seconds"] is not None and estimate["estimated_seconds"] >= 0


def removed_prefetch_runs_to_completion_and_the_snapshot_reflects_it(
    paired: TestClient, config: AppConfig
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        # Zoom-2 tile (0,0) is outside the footprint, like Legacy Surveys'
        # own real DR9/DR10 hemisphere gaps used to be.
        if request.url.path.endswith("/2/0/0.jpg"):
            return httpx.Response(404)
        return httpx.Response(200, content=JPG)

    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(side_effect=upstream)
        started = paired.post(
            "/api/skymap/prefetch", json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 2}
        )
        assert started.status_code == 202, started.text
        job_id = started.json()["job"]["id"]
        assert started.json()["job"]["total"] == 21
        job = _wait_for_job(paired, job_id)

    assert job["status"] == "complete", job
    assert job["done"] == 21 and job["missing"] == 1 and job["bytes"] == 20 * len(JPG)
    assert job["error"] is None and job["finished_at"] is not None

    skymap = _state(paired)["snapshot"]["skymap"]
    survey = next(s for s in skymap["surveys"] if s["id"] == LEGACY_SURVEYS)
    assert survey["cached_tiles"] == 20 and survey["missing_tiles"] == 1
    assert survey["pack_complete_order"] == 2, "a known hole still counts as covered"
    assert skymap["cache_bytes"] == 20 * len(JPG)

    # The pack survives a restart, and the finished job is still on record.
    app = create_app(config, gateway=FakeGateway())
    with TestClient(app, client=CLIENT_ADDRESS) as fresh:
        fresh.post("/api/auth/pair", json={"token": pairing_token(fresh)})
        after = _state(fresh)["snapshot"]["skymap"]
        assert (
            next(s for s in after["surveys"] if s["id"] == LEGACY_SURVEYS)["pack_complete_order"]
            == 2
        )
        assert [j["status"] for j in after["jobs"] if j["id"] == job_id] == ["complete"]


def removed_sky_mode_prefetch_also_works_for_a_mercator_survey(paired: TestClient) -> None:
    # `mode: "sky"` was already scheme-agnostic in service.py before this
    # test existed (it iterates `tile_count(survey, order)`, never branches
    # on `survey.scheme`) -- this pins that down for the Mercator scheme
    # specifically. Written against unWISE, which was the Mercator survey
    # "cache the whole sky" was asked for; repointed at Legacy Surveys DR11
    # on 2026-09-09 when unWISE was removed from the registry, since DR11 is
    # now the only Mercator entry. The behaviour under test is the scheme,
    # not the survey.
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1/1/1.jpg"):  # one corner missing, a real coverage gap
            return httpx.Response(404)
        return httpx.Response(200, content=JPG)

    with respx.mock() as router:
        router.get(re.compile(r"https://www\.legacysurvey\.org/viewer/ls-dr11/1/.*\.jpg")).mock(
            side_effect=upstream
        )
        started = paired.post(
            "/api/skymap/prefetch", json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 1}
        )
        assert started.status_code == 202, started.text
        job_id = started.json()["job"]["id"]
        assert started.json()["job"]["total"] == 5  # zoom 0 (1 tile) + zoom 1 (4 tiles)
        job = _wait_for_job(paired, job_id)

    assert job["status"] == "complete", job
    assert job["done"] == 5 and job["missing"] == 1 and job["bytes"] == 4 * len(JPG)

    mercator = next(
        s for s in paired.get("/api/skymap/surveys").json()["surveys"] if s["id"] == LEGACY_SURVEYS
    )
    assert mercator["cached_tiles"] == 4 and mercator["missing_tiles"] == 1
    assert mercator["pack_complete_order"] == 1, "a known hole still counts as covered"


def removed_prefetch_fails_loudly_when_upstream_goes_away(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A connect error is retried (a research archive drops connections
    # sometimes; one blip must not fail a whole pack — see
    # `SkymapService._fetch`), but every attempt fails the same way here, so
    # the job still ends up `failed`. The base delay is trimmed so the test
    # does not spend several real seconds walking the backoff.
    monkeypatch.setattr(skymap_service, "PREFETCH_RETRY_BASE_DELAY_S", 0.01)
    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(side_effect=httpx.ConnectError("unplugged"))
        started = paired.post(
            "/api/skymap/prefetch", json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 1}
        )
        assert started.status_code == 202, started.text
        job = _wait_for_job(paired, started.json()["job"]["id"])
    assert job["status"] == "failed"
    assert job["error"].startswith("offline:")


def removed_prefetch_survives_one_transient_upstream_error(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live bug, 2026-09-08: "legacy surveys still downloads only one tile".

    A single timeout or 5xx anywhere in a batch used to raise out of
    ``asyncio.gather`` and fail the *entire* job outright, even though every
    other tile in the batch (and every prior batch) had already succeeded —
    on a several-thousand-tile pack against a real archive, a lone blip is
    routine, not exceptional, so this killed almost every real attempt.
    ``SkymapService._fetch`` now retries a transient error a bounded number
    of times before giving up on that one tile; this pins down that a job
    with exactly one flaky tile still reaches ``complete``.
    """
    monkeypatch.setattr(skymap_service, "PREFETCH_RETRY_BASE_DELAY_S", 0.01)
    flaky_path = "/2/1/2.jpg"  # one arbitrary zoom-2 tile (z/x/y.ext)
    attempts_at_flaky_path = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal attempts_at_flaky_path
        if request.url.path.endswith(flaky_path):
            attempts_at_flaky_path += 1
            if attempts_at_flaky_path == 1:
                return httpx.Response(503)
        return httpx.Response(200, content=JPG)

    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(side_effect=upstream)
        started = paired.post(
            "/api/skymap/prefetch", json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 2}
        )
        assert started.status_code == 202, started.text
        job = _wait_for_job(paired, started.json()["job"]["id"])

    assert job["status"] == "complete", job
    assert job["done"] == job["total"] == 21
    assert job["missing"] == 0
    assert attempts_at_flaky_path == 2, "the flaky tile was retried, not skipped or fatal"


def removed_prefetch_refusals(paired: TestClient) -> None:
    disabled = paired.post(
        "/api/skymap/prefetch", json={"survey": "wise", "mode": "sky", "max_order": 1}
    )
    assert disabled.status_code == 409 and "target-field" in disabled.json()["detail"]
    # The TOAST-scheme cone refusal (`{id} is a TOAST pyramid: cone prefetch
    # is not defined for TOAST surveys`) has no live HTTP survey to exercise
    # it against any more -- Pan-STARRS DR1 was the one TOAST entry and was
    # removed 2026-09-08 -- so it is pinned at the unit level instead
    # (`tests/skymap/test_service.py::test_cone_prefetch_refuses_toast`).
    # Mercator's own cone prefetch (Legacy Surveys, unWISE) is no longer
    # refused at all: `test_cone_prefetch_works_for_a_mercator_survey` below.
    too_deep = paired.post(
        "/api/skymap/prefetch", json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 17}
    )
    assert too_deep.status_code == 409 and "does not exist upstream" in too_deep.json()["detail"]
    too_big = paired.post(
        "/api/skymap/prefetch", json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 12}
    )
    too_big_detail = too_big.json()["detail"]
    assert too_big.status_code == 409
    assert "limit is 349,525" in too_big_detail
    # The actionable part: names the deepest order that *does* fit, its tile
    # count, and a resulting resolution -- not just a bare "lower it" (live
    # complaint, 2026-09-08: the old message named no actual value).
    assert "Order 9 fits" in too_big_detail
    assert "349,525 tiles" in too_big_detail
    assert '"/px' in too_big_detail
    assert "Lower max_order to 9" in too_big_detail
    # Body validation (extra="forbid", cone needs a centre) is the same schema
    # on the un-throttled estimate endpoint.
    cone_without_centre = paired.post(
        "/api/skymap/prefetch/estimate",
        json={"survey": LEGACY_SURVEYS, "mode": "cone", "max_order": 3},
    )
    assert cone_without_centre.status_code == 422
    unknown_field = paired.post(
        "/api/skymap/prefetch/estimate",
        json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 1, "x": 1},
    )
    assert unknown_field.status_code == 422


def removed_cone_prefetch_works_for_a_mercator_survey(paired: TestClient) -> None:
    """Deliverable 4: "download more detailed 6-degree version for the
    currently selected/resolved object" -- `_plan`'s cone branch used to
    refuse every non-HiPS survey outright (and misreport it as "a TOAST
    pyramid" even for a Mercator one); Legacy Surveys DR11 and unWISE, both
    Mercator, are exactly the surveys this is for."""
    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(return_value=httpx.Response(200, content=JPG))
        started = paired.post(
            "/api/skymap/prefetch",
            json={
                "survey": LEGACY_SURVEYS,
                "mode": "cone",
                "max_order": 6,
                "ra_deg": 280.0,
                "dec_deg": 10.0,
                "radius_deg": 3.0,
            },
        )
        assert started.status_code == 202, started.text
        job = _wait_for_job(paired, started.json()["job"]["id"])
    assert job["status"] == "complete", job
    assert job["done"] == job["total"] > 0
    assert job["missing"] == 0


@pytest.mark.parametrize("config", [False, True], indirect=True)
def removed_prefetch_requires_control_scope_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    response = client.post(
        "/api/skymap/prefetch", json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 1}
    )
    assert (response.status_code == 401) is bool(config.auth_required)


def removed_an_interrupted_job_is_marked_not_resumed(paired: TestClient, config: AppConfig) -> None:
    """A job row left 'running' by a dead process must come back 'interrupted'
    with a reason, and nothing must start fetching on its own."""
    from ssc.api.deps import Services

    services = paired.app.state.services  # type: ignore[union-attr]
    assert isinstance(services, Services)

    async def plant() -> None:
        await services.database.execute(
            "INSERT INTO skymap_prefetch_jobs (id, survey, mode, max_order, status, done, total, "
            "missing, bytes, created_at, updated_at) VALUES ('dead01', ?, 'sky', 1, 'running', "
            "2, 5, 0, 100, '2026-09-07T00:00:00+00:00', '2026-09-07T00:00:00+00:00')",
            (LEGACY_SURVEYS,),
        )

    paired.portal.call(plant)  # type: ignore[attr-defined]

    app = create_app(config, gateway=FakeGateway())
    with respx.mock(assert_all_called=False) as router:
        route = router.get(LEGACY_SURVEYS_UPSTREAM).mock(
            return_value=httpx.Response(200, content=JPG)
        )
        with TestClient(app, client=CLIENT_ADDRESS) as fresh:
            fresh.post("/api/auth/pair", json={"token": pairing_token(fresh)})
            jobs = {j["id"]: j for j in _state(fresh)["snapshot"]["skymap"]["jobs"]}
        assert route.call_count == 0
    assert jobs["dead01"]["status"] == "interrupted"
    assert "start it again" in jobs["dead01"]["error"]
    assert jobs["dead01"]["done"] == 2, "progress made before the crash is kept, not zeroed"


def test_clear_cache_removes_files_and_index(paired: TestClient, config: AppConfig) -> None:
    with respx.mock() as router:
        router.get(LEGACY_SURVEYS_UPSTREAM).mock(return_value=httpx.Response(200, content=JPG))
        assert (
            paired.get(f"/api/skymap/tiles/{LEGACY_SURVEYS}/mercator/0/0/0.jpg").status_code == 200
        )
    assert (config.tiles_dir / LEGACY_SURVEYS).is_dir()
    cleared = paired.delete(f"/api/skymap/cache/{LEGACY_SURVEYS}")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"survey": LEGACY_SURVEYS, "freed_bytes": len(JPG)}
    assert not (config.tiles_dir / LEGACY_SURVEYS).exists()
    survey = next(
        s for s in _state(paired)["snapshot"]["skymap"]["surveys"] if s["id"] == LEGACY_SURVEYS
    )
    assert survey["cached_tiles"] == 0 and survey["cached_bytes"] == 0
    assert paired.delete("/api/skymap/cache/nope").status_code == 404


# -- the snapshot contract ----------------------------------------------------------- #


def removed_skymap_events_replay_to_the_fresh_snapshot(paired: TestClient) -> None:
    """The reconnect check (CLAUDE.md invariant 4) for this section: every
    ``skymap.changed`` a client applies must land it on exactly the server's
    snapshot."""
    with paired.websocket_connect("/ws") as websocket:
        hello = json.loads(websocket.receive_text())
        reconstructed = StateSnapshot.model_validate(hello["snapshot"])
        epoch, seq = hello["epoch"], hello["seq"]
        with respx.mock() as router:
            router.get(LEGACY_SURVEYS_UPSTREAM).mock(return_value=httpx.Response(200, content=JPG))
            started = paired.post(
                "/api/skymap/prefetch",
                json={"survey": LEGACY_SURVEYS, "mode": "sky", "max_order": 1},
            )
            assert started.status_code == 202
            _wait_for_job(paired, started.json()["job"]["id"])
    with paired.websocket_connect(f"/ws?seq={seq}&epoch={epoch}") as websocket:
        fresh = _state(paired)
        frames: list[dict[str, Any]] = []
        while not frames or frames[-1]["seq"] < fresh["seq"]:
            frames.append(json.loads(websocket.receive_text()))
    assert {f["type"] for f in frames} == {"event"}
    assert {f["event"]["type"] for f in frames} >= {"skymap.changed"}
    reconstructed = replay(
        reconstructed, [STATE_EVENT_ADAPTER.validate_python(f["event"]) for f in frames]
    )
    assert reconstructed.model_dump(mode="json") == fresh["snapshot"]


def test_hips_cone_tiles_are_addressable_over_http_for_a_hips_survey() -> None:
    """Sanity for the HiPS route shape the WWT frame will use once a HiPS
    survey is enabled: order/npix -> Norder/Dir/Npix upstream."""
    assert hips.tile_path(3, 123, "png") == "Norder3/Dir0/Npix123.png"


def test_hips_standard_layout_route_refuses_a_wrong_dir_bucket(paired: TestClient) -> None:
    """``/tiles/<survey>/`` is a HiPS root: the standard ``Norder/Dir/Npix``
    path is served, and a ``Dir`` that is not ``npix``'s bucket is refused
    rather than quietly corrected."""
    wrong = paired.get("/api/skymap/tiles/wham-halpha/Norder3/Dir10000/Npix123.png")
    assert wrong.status_code == 404
    assert wrong.json()["reason"] == "out_of_range"
