"""ObjectInfoService against a real SQLite database: disabled means zero
network calls, a found lookup threads per-image Commons licence through to
the cached record, a genuine "no article" is cached too, and a cache hit
never touches the network again."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from ssc.objectinfo.service import ObjectInfoService
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate

UA = "SSC/test (+ops@example.org)"


def _ua() -> str:
    return UA


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await open_database(tmp_path / "ssc.sqlite3")
    await migrate(database)
    try:
        yield database
    finally:
        await database.aclose()


def _no_network_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _mock_full_resolution(router: respx.MockRouter) -> None:
    """Wikipedia -> Wikidata (claims + P31 labels) -> Commons -> the image
    bytes, for "Andromeda Galaxy" / "M31", mirroring what
    THIRD_PARTY_NOTICES.md's gate reading observed live for M31's own P18 file."""
    router.get("https://en.wikipedia.org/api/rest_v1/page/summary/Andromeda_Galaxy").mock(
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

    def entities(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"]
        if ids == "Q4592":
            return httpx.Response(
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
                                ],
                                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q318"}}}}],
                            }
                        }
                    }
                },
            )
        return httpx.Response(
            200, json={"entities": {"Q318": {"labels": {"en": {"value": "galaxy"}}}}}
        )

    router.get("https://www.wikidata.org/w/api.php").mock(side_effect=entities)
    router.get("https://commons.wikimedia.org/w/api.php").mock(
        return_value=httpx.Response(
            200,
            json={
                "query": {
                    "pages": {
                        "1": {
                            "imageinfo": [
                                {
                                    "thumburl": "https://upload.wikimedia.org/thumb.jpg",
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
    router.get("https://upload.wikimedia.org/thumb.jpg").mock(
        return_value=httpx.Response(
            200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"}
        )
    )


async def test_disabled_makes_zero_network_calls_and_returns_none(db: Database) -> None:
    service = ObjectInfoService(
        db, enabled=lambda: False, user_agent=_ua, http=_no_network_client()
    )
    try:
        result = await service.get_info("M31", "Andromeda Galaxy")
    finally:
        await service.aclose()
    assert result is None


async def test_found_threads_the_files_own_licence_through_to_the_result(db: Database) -> None:
    with respx.mock() as router:
        _mock_full_resolution(router)
        async with httpx.AsyncClient() as http:
            service = ObjectInfoService(db, enabled=lambda: True, user_agent=_ua, http=http)
            result = await service.get_info("M31", "Andromeda Galaxy")

    assert result is not None
    assert result.found is True
    assert result.cached is False
    assert result.title == "Andromeda Galaxy"
    assert result.wikidata_qid == "Q4592"
    assert result.instance_of == ("galaxy",)
    assert result.image is not None
    assert result.image.license_short_name == "CC BY-SA 2.5"  # not a blanket "CC BY-SA"
    assert result.image.artist == "Lviatour"
    assert result.image.data == b"\xff\xd8\xff"

    # Second look: cache hit, zero network calls, same facts.
    cached_client = _no_network_client()
    service2 = ObjectInfoService(db, enabled=lambda: True, user_agent=_ua, http=cached_client)
    try:
        cached = await service2.get_info("M31", "Andromeda Galaxy")
    finally:
        await service2.aclose()
    assert cached is not None
    assert cached.cached is True
    assert cached.found is True
    assert cached.image is not None
    assert cached.image.license_short_name == "CC BY-SA 2.5"

    image_bytes = await service2.get_image_bytes("M31")
    assert image_bytes == (b"\xff\xd8\xff", "image/jpeg")


async def test_no_wikipedia_article_is_a_real_cacheable_negative_result(db: Database) -> None:
    with respx.mock() as router:
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/Some_Obscure_Name").mock(
            return_value=httpx.Response(404)
        )
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/OBS1").mock(
            return_value=httpx.Response(404)
        )
        async with httpx.AsyncClient() as http:
            service = ObjectInfoService(db, enabled=lambda: True, user_agent=_ua, http=http)
            first = await service.get_info("OBS1", "Some Obscure Name")

    assert first is not None
    assert first.found is False
    assert first.cached is False

    # A repeat lookup must not hit the network again to reach the same answer.
    cached_client = _no_network_client()
    service2 = ObjectInfoService(db, enabled=lambda: True, user_agent=_ua, http=cached_client)
    try:
        second = await service2.get_info("OBS1", "Some Obscure Name")
    finally:
        await service2.aclose()
    assert second is not None
    assert second.found is False
    assert second.cached is True


async def test_no_p18_claim_means_no_image_but_text_still_returned(db: Database) -> None:
    with respx.mock() as router:
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/Widefield_Target").mock(
            return_value=httpx.Response(
                200,
                json={
                    "title": "Widefield Target",
                    "extract": "A thing with no Wikidata image.",
                    "wikibase_item": "Q999",
                    "content_urls": {
                        "desktop": {"page": "https://en.wikipedia.org/wiki/Widefield_Target"}
                    },
                },
            )
        )
        router.get("https://www.wikidata.org/w/api.php").mock(
            return_value=httpx.Response(200, json={"entities": {"Q999": {"claims": {}}}})
        )
        async with httpx.AsyncClient() as http:
            service = ObjectInfoService(db, enabled=lambda: True, user_agent=_ua, http=http)
            result = await service.get_info("WF1", "Widefield Target")

    assert result is not None
    assert result.found is True
    assert result.extract == "A thing with no Wikidata image."
    assert result.image is None


async def test_a_commons_upstream_failure_loses_only_the_image_not_the_text(db: Database) -> None:
    with respx.mock() as router:
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/Flaky_Image_Target").mock(
            return_value=httpx.Response(
                200,
                json={
                    "title": "Flaky Image Target",
                    "extract": "Text should survive even if the image fetch fails.",
                    "wikibase_item": "Q1",
                    "content_urls": {
                        "desktop": {"page": "https://en.wikipedia.org/wiki/Flaky_Image_Target"}
                    },
                },
            )
        )
        router.get("https://www.wikidata.org/w/api.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "entities": {
                        "Q1": {"claims": {"P18": [{"mainsnak": {"datavalue": {"value": "X.jpg"}}}]}}
                    }
                },
            )
        )
        router.get("https://commons.wikimedia.org/w/api.php").mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as http:
            service = ObjectInfoService(db, enabled=lambda: True, user_agent=_ua, http=http)
            result = await service.get_info("FLAKY1", "Flaky Image Target")

    assert result is not None
    assert result.found is True
    assert result.extract == "Text should survive even if the image fetch fails."
    assert result.image is None
