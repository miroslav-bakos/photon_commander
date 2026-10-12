"""Target-field cutouts through the real app: a fetch job, its manifest, its
pieces, deletion, the offline point-lookup, and the reconnect test the
CLAUDE.md checklist asks for every ``ssc/state`` change to carry.

Upstream is ``respx``-mocked (the same pattern as ``test_skymap.py``); CI runs
with sockets disabled, so nothing here can reach IRSA/SDSS/Legacy Surveys by
accident.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx

from ssc.settings.config import AppConfig
from ssc.state.events import STATE_EVENT_ADAPTER
from ssc.state.models import StateSnapshot
from ssc.state.reducer import replay
from tests.api.conftest import CLIENT_ADDRESS, FakeGateway, TestClient, pairing_token
from tests.fixtures.skymap.build import build_fits_bytes

from ssc.main import create_app  # isort: skip

_WCS = {
    "CRPIX1": 4.0,
    "CRPIX2": 4.0,
    "CRVAL1": 10.6847,
    "CRVAL2": 41.269,
    "CD1_1": -0.0002777,
    "CD1_2": 0.0,
    "CD2_1": 0.0,
    "CD2_2": 0.0002777,
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
}


def _fits(value: float = 50.0) -> bytes:
    return build_fits_bytes(np.full((8, 8), value, dtype=np.float32), _WCS)


def _votable(access_urls: list[str]) -> bytes:
    """A SIA v2 discovery response with exactly the science/image-fits rows
    ``CutoutService._wise_access_urls`` keeps — the shape it filters for
    among the weight/uncertainty FITS and auxiliary tables IRSA's real
    service also lists for the same Atlas tile (THIRD_PARTY_NOTICES.md "AllWISE
    cutouts" fix, verified live 2026-09-07)."""
    rows = "".join(
        f"<TR><TD>{url}</TD><TD>science</TD><TD>image/fits</TD></TR>" for url in access_urls
    )
    return (
        '<VOTABLE version="1.3" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">'
        "<RESOURCE><TABLE>"
        '<FIELD name="access_url" datatype="char"/>'
        '<FIELD name="dataproduct_subtype" datatype="char"/>'
        '<FIELD name="access_format" datatype="char"/>'
        f"<DATA><TABLEDATA>{rows}</TABLEDATA></DATA>"
        "</TABLE></RESOURCE></VOTABLE>"
    ).encode()


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


def _wait_for_cutout(
    client: TestClient, cutout_id: str, *, timeout_s: float = 5.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cutouts = _state(client)["snapshot"]["skymap"]["cutouts"]
        cutout = next((c for c in cutouts if c["id"] == cutout_id), None)
        if cutout is not None and cutout["status"] not in {"queued", "running"}:
            return cutout
        time.sleep(0.02)
    raise AssertionError(f"cutout {cutout_id} did not finish within {timeout_s}s")


def _request_wise(paired: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "survey": "wise",
        "ra_deg": 10.6847,
        "dec_deg": 41.269,
        "size_deg": 0.5,
        "bands": ["W1"],
    }
    body.update(overrides)
    response = paired.post("/api/skymap/cutouts", json=body)
    assert response.status_code == 202, response.text
    result: dict[str, Any] = response.json()
    return result


def test_request_fetch_and_read_a_wise_cutout(paired: TestClient) -> None:
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(
                200, content=_votable(["https://irsa.ipac.caltech.edu/tile1"])
            )
        )
        router.get("https://irsa.ipac.caltech.edu/tile1").mock(
            return_value=httpx.Response(200, content=_fits())
        )
        started = _request_wise(paired)
        cutout_id = started["cutout"]["id"]
        cutout = _wait_for_cutout(paired, cutout_id)

    assert cutout["status"] == "complete"
    assert cutout["bytes"] > 0

    manifest = paired.get(f"/api/skymap/cutouts/{cutout_id}")
    assert manifest.status_code == 200, manifest.text
    body = manifest.json()
    assert body["credit_label"].startswith("AllWISE")
    assert len(body["pieces"]) == 1
    piece = body["pieces"][0]
    assert piece["band"] == "W1"
    assert piece["wcs"]["ctype1"] == "RA---TAN"
    assert piece["display_png_url"] is not None

    fits_response = paired.get(f"/api/skymap/cutouts/{cutout_id}/pieces/0.fits")
    assert fits_response.status_code == 200
    assert fits_response.content[:6] == b"SIMPLE"

    png_response = paired.get(f"/api/skymap/cutouts/{cutout_id}/pieces/0.png")
    assert png_response.status_code == 200
    assert png_response.headers["content-type"] == "image/png"
    assert png_response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_repeat_request_for_the_same_key_is_a_cache_hit(paired: TestClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        first = _request_wise(paired, ra_deg=5.0, dec_deg=5.0)
        _wait_for_cutout(paired, first["cutout"]["id"])
        second = _request_wise(paired, ra_deg=5.0, dec_deg=5.0)
    assert first["cutout"]["id"] == second["cutout"]["id"]
    assert route.call_count == 1


def test_off_footprint_is_unavailable_with_a_reason(paired: TestClient) -> None:
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        started = _request_wise(paired, ra_deg=99.0, dec_deg=-1.0)
        cutout = _wait_for_cutout(paired, started["cutout"]["id"])
    assert cutout["status"] == "unavailable"
    assert cutout["error"]


def test_a_piece_that_is_not_on_disk_is_a_404_with_a_reason(paired: TestClient) -> None:
    response = paired.get("/api/skymap/cutouts/does-not-exist/pieces/0.fits")
    assert response.status_code == 404
    assert response.json()["reason"] in {"offline", "not_found"}


def test_manifest_of_an_unknown_cutout_is_404(paired: TestClient) -> None:
    response = paired.get("/api/skymap/cutouts/does-not-exist")
    assert response.status_code == 404


def test_list_covering_a_point_only_returns_completed_cutouts_nearby(paired: TestClient) -> None:
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(
                200, content=_votable(["https://irsa.ipac.caltech.edu/tile1"])
            )
        )
        router.get("https://irsa.ipac.caltech.edu/tile1").mock(
            return_value=httpx.Response(200, content=_fits())
        )
        started = _request_wise(paired, ra_deg=10.6847, dec_deg=41.269)
        _wait_for_cutout(paired, started["cutout"]["id"])

    near = paired.get(
        "/api/skymap/cutouts", params={"ra_deg": 10.7, "dec_deg": 41.3, "radius_deg": 1.0}
    )
    assert near.status_code == 200
    assert any(c["id"] == started["cutout"]["id"] for c in near.json()["cutouts"])

    far = paired.get(
        "/api/skymap/cutouts", params={"ra_deg": 200.0, "dec_deg": -50.0, "radius_deg": 1.0}
    )
    assert far.json()["cutouts"] == []


def test_list_by_source_returns_all_cached_fields_not_only_current_target(
    paired: TestClient,
) -> None:
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(
                200, content=_votable(["https://irsa.ipac.caltech.edu/tile-all-fields"])
            )
        )
        router.get("https://irsa.ipac.caltech.edu/tile-all-fields").mock(
            return_value=httpx.Response(200, content=_fits())
        )
        first = _request_wise(paired, ra_deg=10.0, dec_deg=20.0)
        second = _request_wise(paired, ra_deg=200.0, dec_deg=-30.0)
        _wait_for_cutout(paired, first["cutout"]["id"])
        _wait_for_cutout(paired, second["cutout"]["id"])

    response = paired.get("/api/skymap/cutouts", params={"source": "wise"})
    assert response.status_code == 200, response.text
    ids = {cutout["id"] for cutout in response.json()["cutouts"]}
    assert {first["cutout"]["id"], second["cutout"]["id"]} <= ids


def test_delete_removes_it_from_the_snapshot(paired: TestClient) -> None:
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        started = _request_wise(paired, ra_deg=7.0, dec_deg=7.0)
        cutout_id = started["cutout"]["id"]
        _wait_for_cutout(paired, cutout_id)

    deleted = paired.delete(f"/api/skymap/cutouts/{cutout_id}")
    assert deleted.status_code == 204
    assert paired.get(f"/api/skymap/cutouts/{cutout_id}").status_code == 404

    again = paired.delete(f"/api/skymap/cutouts/{cutout_id}")
    assert again.status_code == 404


def test_delete_all_removes_every_cutout_including_off_footprint_ones(paired: TestClient) -> None:
    """Settings' "Delete all" (2026-09-07: the per-cutout list grew
    unwieldy) -- unconditional, unlike LRU eviction: an UNAVAILABLE row from
    a genuinely off-footprint field goes too, not just COMPLETE ones."""
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        first = _request_wise(paired, ra_deg=7.0, dec_deg=7.0)
        _wait_for_cutout(paired, first["cutout"]["id"])
    with respx.mock() as router:
        router.get("https://irsa.ipac.caltech.edu/SIA").mock(
            return_value=httpx.Response(200, content=_votable([]))
        )
        second = _request_wise(paired, ra_deg=100.0, dec_deg=-30.0)
        _wait_for_cutout(paired, second["cutout"]["id"])

    response = paired.delete("/api/skymap/cutouts")
    assert response.status_code == 200
    assert response.json()["deleted"] == 2
    assert paired.get(f"/api/skymap/cutouts/{first['cutout']['id']}").status_code == 404
    assert paired.get(f"/api/skymap/cutouts/{second['cutout']['id']}").status_code == 404

    empty = paired.delete("/api/skymap/cutouts")
    assert empty.status_code == 200
    assert empty.json()["deleted"] == 0


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_delete_all_requires_control_scope_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    response = client.delete("/api/skymap/cutouts")
    assert (response.status_code == 401) is bool(config.auth_required)


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_request_requires_control_scope_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    response = client.post(
        "/api/skymap/cutouts",
        json={"survey": "wise", "ra_deg": 1.0, "dec_deg": 1.0, "size_deg": 0.5},
    )
    assert (response.status_code == 401) is bool(config.auth_required)


def test_an_unknown_source_is_a_422(paired: TestClient) -> None:
    response = paired.post(
        "/api/skymap/cutouts",
        json={"survey": "not-a-source", "ra_deg": 1.0, "dec_deg": 1.0, "size_deg": 0.5},
    )
    assert response.status_code == 422, response.text


def test_size_deg_over_six_is_refused_at_the_boundary(paired: TestClient) -> None:
    # 6, not 3: CutoutRequest.size_deg's shared ceiling was raised so
    # unWISE can request more than the lens-fitted frame for framing
    # context (UNWISE_MAX_CUTOUT_SIZE_DEG); every other source still
    # self-limits to 3 via its own request logic, not this field.
    response = paired.post(
        "/api/skymap/cutouts",
        json={"survey": "wise", "ra_deg": 1.0, "dec_deg": 1.0, "size_deg": 6.01},
    )
    assert response.status_code == 422


def test_cutout_events_replay_to_the_fresh_snapshot(paired: TestClient) -> None:
    """The reconnect check (CLAUDE.md invariant 4): every event a client
    applies from ``seq`` must land it on exactly the server's live snapshot,
    byte-identical to a fresh ``GET /api/state``."""
    with paired.websocket_connect("/ws") as websocket:
        hello = json.loads(websocket.receive_text())
        reconstructed = StateSnapshot.model_validate(hello["snapshot"])
        epoch, seq = hello["epoch"], hello["seq"]
        with respx.mock() as router:
            router.get("https://irsa.ipac.caltech.edu/SIA").mock(
                return_value=httpx.Response(
                    200, content=_votable(["https://irsa.ipac.caltech.edu/tile1"])
                )
            )
            router.get("https://irsa.ipac.caltech.edu/tile1").mock(
                return_value=httpx.Response(200, content=_fits())
            )
            started = _request_wise(paired)
            _wait_for_cutout(paired, started["cutout"]["id"])
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
