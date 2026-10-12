"""ssc.objectinfo.client against respx-mocked Wikipedia/Wikidata/Commons —
the exact endpoints and parsing THIRD_PARTY_NOTICES.md's gate reading checked
live, with the host allowlist that keeps a response from redirecting this
module anywhere else."""

from __future__ import annotations

import httpx
import pytest
import respx

from ssc.objectinfo.client import (
    ObjectInfoUpstreamError,
    fetch_commons_image_info,
    fetch_image_bytes,
    fetch_wikidata_facts,
    fetch_wikipedia_summary,
)

UA = "SSC/test (+ops@example.org)"


def _ua() -> str:
    return UA


async def test_wikipedia_summary_parses_extract_and_wikibase_item() -> None:
    with respx.mock() as router:
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/Ring_Nebula").mock(
            return_value=httpx.Response(
                200,
                json={
                    "title": "Ring Nebula",
                    "extract": "The Ring Nebula is a planetary nebula.",
                    "wikibase_item": "Q356359",
                    "content_urls": {
                        "desktop": {"page": "https://en.wikipedia.org/wiki/Ring_Nebula"}
                    },
                },
            )
        )
        async with httpx.AsyncClient() as http:
            summary = await fetch_wikipedia_summary(http, _ua, "Ring Nebula")
    assert summary is not None
    assert summary.title == "Ring Nebula"
    assert summary.wikidata_qid == "Q356359"
    assert summary.article_url == "https://en.wikipedia.org/wiki/Ring_Nebula"


async def test_wikipedia_404_is_none_not_an_error() -> None:
    with respx.mock() as router:
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/Nonexistent_Thing").mock(
            return_value=httpx.Response(404, json={"type": "https://.../not_found"})
        )
        async with httpx.AsyncClient() as http:
            summary = await fetch_wikipedia_summary(http, _ua, "Nonexistent Thing")
    assert summary is None


async def test_wikipedia_disambiguation_page_is_treated_as_no_article() -> None:
    with respx.mock() as router:
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/Mercury").mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "disambiguation",
                    "title": "Mercury",
                    "extract": "Mercury may refer to:",
                },
            )
        )
        async with httpx.AsyncClient() as http:
            summary = await fetch_wikipedia_summary(http, _ua, "Mercury")
    assert summary is None


async def test_wikipedia_server_error_raises_rather_than_silently_returning_none() -> None:
    with respx.mock() as router:
        router.get("https://en.wikipedia.org/api/rest_v1/page/summary/M31").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(ObjectInfoUpstreamError):
                await fetch_wikipedia_summary(http, _ua, "M31")


async def test_wikidata_facts_resolve_p18_and_p31_labels_in_two_calls() -> None:
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
                                    {"mainsnak": {"datavalue": {"value": "Andromeda Galaxy.jpg"}}}
                                ],
                                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q318"}}}}],
                                "P575": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {
                                                "type": "time",
                                                "value": {
                                                    "time": "+0964-01-01T00:00:00Z",
                                                    "precision": 9,
                                                },
                                            }
                                        }
                                    }
                                ],
                            }
                        }
                    }
                },
            )
        assert ids == "Q318"
        return httpx.Response(
            200, json={"entities": {"Q318": {"labels": {"en": {"value": "galaxy"}}}}}
        )

    with respx.mock() as router:
        router.get("https://www.wikidata.org/w/api.php").mock(side_effect=entities)
        async with httpx.AsyncClient() as http:
            facts = await fetch_wikidata_facts(http, _ua, "Q4592")
    assert facts.image_commons_title == "File:Andromeda Galaxy.jpg"
    assert facts.instance_of == ("galaxy",)
    assert facts.discovery_date == "0964"


async def test_commons_imageinfo_carries_the_files_own_licence() -> None:
    with respx.mock() as router:
        router.get("https://commons.wikimedia.org/w/api.php").mock(
            return_value=httpx.Response(
                200,
                json={
                    "query": {
                        "pages": {
                            "123": {
                                "imageinfo": [
                                    {
                                        "thumburl": "https://upload.wikimedia.org/thumb.jpg",
                                        "url": "https://upload.wikimedia.org/full.jpg",
                                        "descriptionurl": "https://commons.wikimedia.org/wiki/File:X.jpg",
                                        "thumbwidth": 800,
                                        "thumbheight": 600,
                                        "extmetadata": {
                                            "LicenseShortName": {"value": "CC BY-SA 2.5"},
                                            "Artist": {"value": '<a href="...">Lviatour</a>'},
                                            "Credit": {"value": "Own work"},
                                        },
                                    }
                                ]
                            }
                        }
                    }
                },
            )
        )
        async with httpx.AsyncClient() as http:
            info = await fetch_commons_image_info(http, _ua, "File:X.jpg")
    assert info is not None
    assert info.license_short_name == "CC BY-SA 2.5"
    assert info.artist == "Lviatour"  # HTML stripped
    assert info.thumb_url == "https://upload.wikimedia.org/thumb.jpg"
    assert info.width == 800 and info.height == 600


async def test_commons_missing_file_is_none() -> None:
    with respx.mock() as router:
        router.get("https://commons.wikimedia.org/w/api.php").mock(
            return_value=httpx.Response(200, json={"query": {"pages": {"-1": {"missing": True}}}})
        )
        async with httpx.AsyncClient() as http:
            info = await fetch_commons_image_info(http, _ua, "File:Deleted.jpg")
    assert info is None


async def test_image_bytes_refuses_a_host_other_than_upload_wikimedia_org() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ObjectInfoUpstreamError, match=r"upload\.wikimedia\.org"):
            await fetch_image_bytes(http, _ua, "https://evil.example.com/thumb.jpg")


async def test_image_bytes_fetched_with_the_identifying_user_agent() -> None:
    with respx.mock() as router:
        route = router.get("https://upload.wikimedia.org/thumb.jpg").mock(
            return_value=httpx.Response(
                200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"}
            )
        )
        async with httpx.AsyncClient() as http:
            data, media_type = await fetch_image_bytes(
                http, _ua, "https://upload.wikimedia.org/thumb.jpg"
            )
    assert data == b"\xff\xd8\xff"
    assert media_type == "image/jpeg"
    assert route.calls.last.request.headers["user-agent"] == UA
