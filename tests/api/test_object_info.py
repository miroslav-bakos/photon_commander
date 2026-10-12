"""Click-through info through the real app (M11): disabled by default and
provably network-free, an honest "no catalogue source configured" response
shape, the settings toggle, and a found lookup with a per-image licence
threaded all the way to the API response and the image-serving route.

Upstream is respx-mocked, matching ``tests/api/test_skymap.py``'s pattern;
``--disable-socket`` (pyproject) means a call respx does not intercept fails
loudly rather than silently reaching the real internet.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from ssc.settings.config import AppConfig
from tests.api.conftest import TestClient

M31_WIKIPEDIA = "https://en.wikipedia.org/api/rest_v1/page/summary/Andromeda_Galaxy"
WIKIDATA = "https://www.wikidata.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"
UPLOAD = "https://upload.wikimedia.org/thumb.jpg"


def _enable_object_info(client: TestClient) -> None:
    response = client.put("/api/settings/object-info", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True}


def _mock_m31(router: respx.MockRouter) -> None:
    router.get(M31_WIKIPEDIA).mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "Andromeda Galaxy",
                "extract": "The Andromeda Galaxy is a barred spiral galaxy.",
                "wikibase_item": "Q4592",
                "content_urls": {
                    "desktop": {"page": "https://en.wikipedia.org/wiki/Andromeda_Galaxy"}
                },
            },
        )
    )
    router.get(WIKIDATA).mock(
        return_value=httpx.Response(
            200,
            json={
                "entities": {
                    "Q4592": {
                        "claims": {
                            "P18": [
                                {
                                    "mainsnak": {
                                        "datavalue": {
                                            "value": "M31-Andromede-16-09-2023-Hamois.jpg"
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            },
        )
    )
    router.get(COMMONS).mock(
        return_value=httpx.Response(
            200,
            json={
                "query": {
                    "pages": {
                        "1": {
                            "imageinfo": [
                                {
                                    "thumburl": UPLOAD,
                                    "descriptionurl": (
                                        "https://commons.wikimedia.org/wiki/"
                                        "File:M31-Andromede-16-09-2023-Hamois.jpg"
                                    ),
                                    "thumbwidth": 800,
                                    "thumbheight": 533,
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY-SA 2.5"},
                                        "Artist": {"value": "Lviatour"},
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        )
    )
    router.get(UPLOAD).mock(
        return_value=httpx.Response(
            200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"}
        )
    )


def test_object_info_settings_default_off(paired: TestClient) -> None:
    response = paired.get("/api/settings/object-info")
    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_disabled_answers_no_source_configured_with_zero_network_calls(paired: TestClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(re.compile(r".*")).mock(return_value=httpx.Response(200, json={}))
        response = paired.get("/api/astro/objects/M31/info")
        assert route.call_count == 0

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is False
    assert body["found"] is False
    assert body["message"] == "no catalogue source configured"
    assert body["extract"] is None
    assert body["image"] is None
    assert body["attributions"] == []


def test_unknown_designation_is_a_404_regardless_of_the_toggle(paired: TestClient) -> None:
    response = paired.get("/api/astro/objects/NotACatalogueObject/info")
    assert response.status_code == 404


def test_enabled_found_lookup_threads_the_images_own_licence_to_the_response(
    paired: TestClient,
) -> None:
    _enable_object_info(paired)
    with respx.mock() as router:
        _mock_m31(router)
        response = paired.get("/api/astro/objects/M31/info")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["found"] is True
    assert body["common_name"] == "Andromeda Galaxy"
    assert body["title"] == "Andromeda Galaxy"
    assert body["wikidata_qid"] == "Q4592"
    assert body["message"] is None
    image = body["image"]
    assert image is not None
    assert (
        image["license_short_name"] == "CC BY-SA 2.5"
    )  # this file's own licence, not a blanket one
    assert image["artist"] == "Lviatour"
    assert image["url"] == "/api/astro/objects/M31/info/image"
    attribution_texts = [a["text"] for a in body["attributions"]]
    assert "Wikipedia contributors" in attribution_texts
    assert any("Wikidata" in text for text in attribution_texts)
    assert any("CC BY-SA 2.5" in text for text in attribution_texts)

    # A repeat lookup is served from cache with no further network calls.
    with respx.mock(assert_all_called=False) as router:
        route = router.get(re.compile(r".*")).mock(return_value=httpx.Response(200, json={}))
        second = paired.get("/api/astro/objects/M31/info")
        assert route.call_count == 0
    assert second.json()["cached"] is True
    assert second.json()["found"] is True

    image_response = paired.get("/api/astro/objects/M31/info/image")
    assert image_response.status_code == 200
    assert image_response.content == b"\xff\xd8\xff"
    assert image_response.headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_object_info_route_requires_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    response = client.get("/api/astro/objects/M31/info")
    assert (response.status_code == 401) is bool(config.auth_required)


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_object_info_settings_toggle_requires_control_iff_auth_required(
    paired: TestClient, config: AppConfig
) -> None:
    # Setting a control PIN drops this same session back to view-only
    # (DESIGN.md §9) — the same technique tests/api/test_cutouts_bulk.py
    # uses to prove a mutation is actually control-gated, not just
    # session-gated.
    assert paired.put("/api/auth/control-pin", json={"pin": "8421"}).status_code == 200
    assert paired.get("/api/auth/session").json()["scope"] == "view"
    response = paired.put("/api/settings/object-info", json={"enabled": True})
    assert response.status_code == (403 if config.auth_required else 200)


def test_object_info_settings_toggle_rejects_unknown_fields(paired: TestClient) -> None:
    response = paired.put("/api/settings/object-info", json={"enabled": True, "extra": "nope"})
    assert response.status_code == 422
