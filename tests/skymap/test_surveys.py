"""The registry: what is enabled, what is not and why, and that no entry
points at a host the compliance reading forbids."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ssc.skymap import REGISTRY, Survey, SurveyKind, TileScheme, find_survey
from ssc.skymap import surveys as registry
from ssc.skymap.surveys import client_tile_template, local_tile_path, upstream_tile_url


def test_pan_starrs_is_no_longer_in_the_registry() -> None:
    """Removed 2026-09-08 — product owner: "completely remove obsolete ugly
    panstarrs", "not usable for our purpose" (see `THIRD_PARTY_NOTICES.md`
    "Sky-survey layers" for the closed-out STScI redistribution question)."""
    assert find_survey("panstarrs-dr1-color") is None
    assert not hasattr(registry, "PANSTARRS_DR1")


def test_cutout_route_surveys_are_listed_disabled_with_the_reason() -> None:
    for survey_id in ("wise", "sdss"):
        survey = find_survey(survey_id)
        assert survey is not None, survey_id
        assert survey.enabled is False
        assert survey.pending_clearance is not None
        assert "target-field" in survey.pending_clearance
        assert survey.bundle_allowed is False
    assert find_survey("wise").kind is SurveyKind.INFRARED  # type: ignore[union-attr]
    assert find_survey("sdss").credit_url == "https://www.sdss.org"  # type: ignore[union-attr]


def test_wham_is_disabled_and_no_survey_pixels_are_bundled() -> None:
    wham = find_survey("wham-halpha")
    assert wham is not None
    assert not wham.enabled and not wham.production_cleared
    assert not wham.bundled and not wham.bundle_allowed
    assert wham.local_dir is None
    assert wham.rights_notice and "permission" in wham.rights_notice
    assert not any(s.bundled for s in REGISTRY)


def test_no_survey_points_at_uncleared_or_forbidden_servers() -> None:
    for survey in REGISTRY:
        if survey.id in {"dss2-color-hips-poc"}:
            assert survey.enabled and not survey.production_cleared
            assert survey.rights_notice and "permission" in survey.rights_notice
        else:
            assert survey.production_cleared or not survey.enabled, survey.id
        host = survey.upstream_host
        if survey.id not in {"dss2-color-hips-poc"}:
            assert "alasky" not in host and "unistra" not in host, survey.id
        assert "worldwidetelescope" not in host and "wwtassets" not in host, survey.id


def test_dss_colour_is_remote_only_and_explicitly_uncleared() -> None:
    dss = find_survey("dss2-color-hips-poc")
    assert dss is registry.DSS_COLOR_REMOTE_POC
    assert dss.enabled and not dss.production_cleared
    assert not dss.bundle_allowed and not dss.bundled
    assert dss.local_dir is None
    assert dss.rights_notice and "No DSS pixels are bundled" in dss.rights_notice



def test_unwise_background_is_restored_for_the_all_sky_poc() -> None:
    assert find_survey("unwise") is registry.UNWISE


@pytest.mark.parametrize(
    "url",
    [
        "https://alasky.cds.unistra.fr/Pan-STARRS/DR1/color",
        "http://www.worldwidetelescope.org/wwtweb/tiles",
        "https://web.wwtassets.org/engine/tiles",
    ],
)
def test_the_model_refuses_forbidden_upstreams(url: str) -> None:
    with pytest.raises(ValidationError, match="never an upstream"):
        Survey(
            id="bad",
            label="bad",
            kind=SurveyKind.OPTICAL,
            scheme=TileScheme.HIPS,
            upstream_base_url=url,
            tile_format="png",
            max_order=3,
            credit_label="x",
            credit_url="https://example.org",
        )


def test_uncleared_poc_source_requires_notice_and_cannot_be_bundled() -> None:
    kwargs = {
        "id": "poc",
        "label": "POC",
        "kind": SurveyKind.OPTICAL,
        "scheme": TileScheme.HIPS,
        "upstream_base_url": "https://alasky.cds.unistra.fr/poc",
        "tile_format": "jpg",
        "max_order": 1,
        "credit_label": "credit",
        "credit_url": "https://example.org",
        "production_cleared": False,
    }
    with pytest.raises(ValidationError, match="rights_notice"):
        Survey(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="cannot be bundled"):
        Survey(**kwargs, rights_notice="POC ONLY", bundle_allowed=True)  # type: ignore[arg-type]


def test_a_bundled_survey_must_be_allowed_to_be_bundled(tmp_path: object) -> None:
    with pytest.raises(ValidationError, match="bundle_allowed"):
        Survey(
            id="bad",
            label="bad",
            kind=SurveyKind.HALPHA,
            scheme=TileScheme.HIPS,
            upstream_base_url="",
            local_dir=tmp_path,  # type: ignore[arg-type]
            tile_format="png",
            max_order=3,
            credit_label="x",
            credit_url="https://example.org",
            bundle_allowed=False,
        )


def test_every_entry_has_a_credit_with_a_url() -> None:
    for survey in REGISTRY:
        assert survey.credit_label and survey.credit_url.startswith("https://"), survey.id


def _nsns_survey_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "id": "test-nsns",
        "label": "test",
        "kind": SurveyKind.HALPHA,
        "scheme": TileScheme.HIPS,
        "upstream_base_url": "http://example.org/nsns",
        "tile_format": "png",
        "fits_stretch": True,
        "fits_stretch_black": 0.0,
        "fits_stretch_white": 1000.0,
        "max_order": 6,
        "credit_label": "x",
        "credit_url": "https://example.org",
    }
    kwargs.update(overrides)
    return kwargs


def test_fits_stretch_requires_a_fixed_black_and_white_point() -> None:
    with pytest.raises(ValidationError, match="fits_stretch_black/white"):
        Survey(**_nsns_survey_kwargs(fits_stretch_black=None))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="fits_stretch_black/white"):
        Survey(**_nsns_survey_kwargs(fits_stretch_white=None))  # type: ignore[arg-type]


def test_fits_stretch_white_point_must_exceed_black_point() -> None:
    with pytest.raises(ValidationError, match="must exceed"):
        Survey(**_nsns_survey_kwargs(fits_stretch_black=500.0, fits_stretch_white=500.0))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="must exceed"):
        Survey(**_nsns_survey_kwargs(fits_stretch_black=500.0, fits_stretch_white=100.0))  # type: ignore[arg-type]


def test_black_and_white_points_are_meaningless_without_fits_stretch() -> None:
    with pytest.raises(ValidationError, match="meaningless without fits_stretch"):
        Survey(**_nsns_survey_kwargs(fits_stretch=False))  # type: ignore[arg-type]


def test_every_fits_stretch_survey_in_the_registry_has_a_valid_reference() -> None:
    for survey in REGISTRY:
        if not survey.fits_stretch:
            assert survey.fits_stretch_black is None and survey.fits_stretch_white is None, (
                survey.id
            )
            continue
        assert survey.fits_stretch_black is not None, survey.id
        assert survey.fits_stretch_white is not None, survey.id
        assert survey.fits_stretch_white > survey.fits_stretch_black, survey.id


def test_nsns_ohs8_composite_has_no_fits_stretch() -> None:
    """The composited RGB tile is already a browser-decodable PNG from the
    archive itself — nothing for this registry to convert or normalise."""
    ohs8 = find_survey("nsns-ohs8")
    assert ohs8 is not None
    assert ohs8.fits_stretch is False
    assert ohs8.fits_stretch_black is None and ohs8.fits_stretch_white is None


def test_nsns_is_disabled_until_commercial_permission_is_recorded() -> None:
    nsns = [survey for survey in REGISTRY if survey.id.startswith("nsns-")]
    assert len(nsns) == 4
    assert all(not survey.enabled for survey in nsns)
    assert all("written commercial-use permission" in (survey.pending_clearance or "") for survey in nsns)


#: No registry entry uses TOAST any more (Pan-STARRS DR1, the one that did,
#: was removed 2026-09-08) — the scheme itself stays (`TileScheme.TOAST`'s
#: own docstring), so its addressing is exercised here against a synthetic
#: survey the same way `test_the_model_refuses_forbidden_upstreams` builds
#: one, rather than dropped with the entry that used to carry the coverage.
_SYNTHETIC_TOAST_SURVEY: Survey = Survey(
    id="synthetic-toast",
    label="Synthetic TOAST survey (test only)",
    kind=SurveyKind.OPTICAL,
    scheme=TileScheme.TOAST,
    upstream_base_url="https://example.org/toast/demo",
    tile_format="png",
    max_order=3,
    credit_label="Test fixture",
    credit_url="https://example.org",
)


def test_toast_addresses_mirror_the_upstream_layout() -> None:
    toast_survey = _SYNTHETIC_TOAST_SURVEY
    npix = registry.toast.pack(3, 5, 2)  # x=5, y=2
    assert (
        upstream_tile_url(toast_survey, 3, npix, "png")
        == "https://example.org/toast/demo/3/2/2_5.png"
    )
    assert local_tile_path(toast_survey, 3, npix, "png") == "toast/3/2/2_5.png"
    assert (
        client_tile_template(toast_survey)
        == "/api/skymap/tiles/synthetic-toast/toast/{level}/{x}/{y}.png"
    )


def test_legacy_surveys_is_the_full_sky_background_survey_distinct_from_the_cutout() -> None:
    ls = find_survey("legacy-surveys-dr11")
    assert ls is not None
    assert ls.enabled and ls.pending_clearance is None
    assert ls.upstream_host == "www.legacysurvey.org"
    assert ls.credit_label == "Legacy Surveys / D. Lang (Perimeter Institute)"
    assert ls.credit_url == "https://www.legacysurvey.org/"
    # CC BY 4.0 permits redistribution outright, so the licence *allows* a
    # bundled pack (commercial-licensing audit, 2026-09-08) even though none
    # ships: `bundled` stays False, and would additionally need `local_dir`,
    # which the model restricts to HiPS pyramids.
    assert ls.bundle_allowed is True and ls.redistributable_pack_url is None
    assert ls.bundled is False and ls.local_dir is None
    assert ls.scheme is TileScheme.MERCATOR and ls.max_order == registry.mercator.MAX_ZOOM
    # A background tile survey, not the per-target cutout source of the same name.
    assert find_survey("legacy-surveys") is None


def test_mercator_addresses_mirror_the_upstream_xyz_layout() -> None:
    ls = registry.LEGACY_SURVEYS
    npix = registry.mercator.pack(2, 1, 3)  # x=1, y=3
    assert (
        upstream_tile_url(ls, 2, npix, "jpg")
        == "https://www.legacysurvey.org/viewer/ls-dr11/1/2/1/3.jpg"
    )
    assert local_tile_path(ls, 2, npix, "jpg") == "mercator/2/1/3.jpg"
    assert (
        client_tile_template(ls)
        == "/api/skymap/tiles/legacy-surveys-dr11/mercator/{level}/{x}/{y}.jpg"
    )


def test_disabled_wham_retains_template_without_bundled_pixels() -> None:
    wham = registry.WHAM_HALPHA
    assert (
        client_tile_template(wham)
        == "/api/skymap/tiles/wham-halpha/Norder{order}/Dir{dir}/Npix{npix}.png"
    )
    assert local_tile_path(wham, 3, 123, "png") == "Norder3/Dir0/Npix123.png"
    assert not wham.enabled and wham.local_dir is None
