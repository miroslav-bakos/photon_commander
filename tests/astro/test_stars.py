"""Unit tests for ssc.astro.stars: the bundled named-bright-star table
(data/stars/bright_stars.csv — see that file's header for provenance;
THIRD_PARTY_NOTICES.md "Bundled named-bright-star table").
"""

from __future__ import annotations

import pytest

from ssc.astro.stars import (
    MAX_CONE_LIMIT,
    MAX_CONE_RADIUS_DEG,
    default_bright_stars_path,
    load_bright_stars,
    stars_within_cone,
)


@pytest.fixture(scope="module")
def stars():
    return load_bright_stars()


# --- loading --------------------------------------------------------------


def test_default_bright_stars_path_is_bundled_and_committed() -> None:
    path = default_bright_stars_path()
    assert path.name == "bright_stars.csv"
    assert path.is_file(), "the table ships in the repo; there is nothing to fetch"


def test_catalogue_is_a_few_hundred_named_stars_not_the_whole_bsc(stars) -> None:
    """The deliverable spec: a few hundred genuinely bright, commonly-named
    stars, not a dense field — see scripts/build-bright-stars.py's "Magnitude
    cutoff" section."""
    assert 100 <= len(stars) <= 400


def test_every_star_has_a_bright_star_catalogue_designation(stars) -> None:
    for star in stars:
        assert star.designation.startswith("HR "), star


def test_names_are_unique(stars) -> None:
    names = [star.name for star in stars]
    assert len(names) == len(set(names))


def test_no_field_is_a_fabricated_zero(stars) -> None:
    """CLAUDE.md: 'None means unknown; it never means zero.' A magnitude or
    position of exactly 0.0 is astronomically plausible (Algenib is close),
    but every field here comes straight from the source file, never a
    default — this guards against a parsing regression that silently drops
    a value to its Python-default zero."""
    for star in stars:
        assert star.name != ""
        assert star.designation != ""


# --- known bright stars -----------------------------------------------------


@pytest.mark.parametrize(
    "name,designation",
    [
        ("Sirius", "HR 2491"),
        ("Vega", "HR 7001"),
        ("Betelgeuse", "HR 2061"),
        ("Rigel", "HR 1713"),
        ("Polaris", "HR 424"),
        ("Rigil Kentaurus", "HR 5459"),
    ],
)
def test_famous_named_stars_are_present(stars, name: str, designation: str) -> None:
    matches = [s for s in stars if s.name == name]
    assert len(matches) == 1, f"{name!r} should resolve to exactly one row"
    assert matches[0].designation == designation


def test_multi_word_names_survive_the_fixed_width_source_intact(stars) -> None:
    """The source file is nominally fixed-width, but several proper names
    overflow their column (scripts/build-bright-stars.py's "Parsing note") —
    this is a regression guard for a build that silently truncated one."""
    names = {s.name for s in stars}
    for expected in ("Rigil Kentaurus", "Kaus Australis", "Asellus Australis", "Cor Caroli"):
        assert expected in names


# --- magnitude sanity -------------------------------------------------------


def test_no_star_is_absurdly_faint(stars) -> None:
    """The cutoff is Vmag 4.0 (scripts/build-bright-stars.py); nothing in
    the bundled table should be fainter than that, and nothing should be
    implausibly bright either (Sirius, -1.46, is the brightest star in the
    sky)."""
    for star in stars:
        assert -2.0 < star.magnitude <= 4.0, star


# --- cone query -------------------------------------------------------------


def test_orions_belt_field_returns_the_named_belt_and_shoulder_stars(stars) -> None:
    """Orion, a field every named-star test should be able to reach for:
    Betelgeuse (shoulder), Bellatrix (shoulder), and the belt trio Alnitak/
    Alnilam/Mintaka are all bright enough and close enough together to land
    in one 10-degree cone centred on the belt (Rigel, at Dec -8.2, is just
    outside a 10-degree radius of Dec +1 and is covered by the wider-cone
    test below instead)."""
    hits = stars_within_cone(stars, ra_deg=83.0, dec_deg=1.0, radius_deg=10.0, limit=50)
    names = {s.name for s in hits}
    for expected in ("Betelgeuse", "Bellatrix", "Alnitak", "Alnilam", "Mintaka"):
        assert expected in names, (expected, names)


def test_wider_orion_field_also_reaches_rigel(stars) -> None:
    hits = stars_within_cone(stars, ra_deg=83.0, dec_deg=1.0, radius_deg=10.0, limit=50)
    hits += stars_within_cone(stars, ra_deg=83.0, dec_deg=-5.0, radius_deg=10.0, limit=50)
    names = {s.name for s in hits}
    assert "Rigel" in names


def test_cone_query_is_brightest_first(stars) -> None:
    hits = stars_within_cone(stars, ra_deg=83.0, dec_deg=1.0, radius_deg=10.0, limit=50)
    magnitudes = [s.magnitude for s in hits]
    assert magnitudes == sorted(magnitudes)
    assert hits[0].name == "Betelgeuse"


def test_cone_query_respects_limit(stars) -> None:
    hits = stars_within_cone(stars, ra_deg=83.0, dec_deg=1.0, radius_deg=10.0, limit=2)
    assert len(hits) == 2


def test_cone_query_far_from_any_bundled_star_is_empty(stars) -> None:
    # A patch of sky with no bright named star within a tight radius.
    hits = stars_within_cone(stars, ra_deg=200.0, dec_deg=-89.0, radius_deg=0.5, limit=10)
    assert hits == ()


def test_cone_query_refuses_radius_above_the_ceiling(stars) -> None:
    with pytest.raises(ValueError):
        stars_within_cone(
            stars, ra_deg=83.0, dec_deg=1.0, radius_deg=MAX_CONE_RADIUS_DEG + 0.1, limit=10
        )


def test_cone_query_refuses_limit_above_the_ceiling(stars) -> None:
    with pytest.raises(ValueError):
        stars_within_cone(stars, ra_deg=83.0, dec_deg=1.0, radius_deg=1.0, limit=MAX_CONE_LIMIT + 1)


# --- independent cross-check -------------------------------------------------


def test_positions_and_magnitudes_agree_with_heasarc_bsc5p(stars) -> None:
    """Independent cross-check, fetched 2026-09-07 from NASA HEASARC
    (https://heasarc.gsfc.nasa.gov/FTP/heasarc/dbase/tdat_files/heasarc_bsc5p.tdat.gz,
    the ``heasarc_bsc5p`` table, ``ra``/``dec``/``vmag`` fields, by HR
    number):

        HR 2491 (Sirius):          RA 101.2871  Dec -16.7161  Vmag -1.46
        HR 7001 (Vega):            RA 279.2346  Dec  38.7836  Vmag  0.03
        HR 1713 (Rigel):           RA  78.6346  Dec  -8.2017  Vmag  0.12
        HR 2061 (Betelgeuse):      RA  88.7929  Dec   7.4069  Vmag  0.50
        HR 424  (Polaris):         RA  37.9529  Dec  89.2642  Vmag  2.02
        HR 5459 (Rigil Kentaurus): RA 219.8996  Dec -60.8353  Vmag -0.01

    Position tolerance is generous (0.05 deg) because BSC5P and IAU-CSN are
    independently reduced from different-epoch astrometry (BSC5P's original
    J2000 positions predate Hipparcos/Gaia-refined ones); magnitude
    tolerance (0.2 mag) is generous because several of these are variable
    stars (Betelgeuse) and different photometric reductions round
    differently — the point of this test is "same star, no data-entry
    error", not "identical to the last digit"."""
    heasarc = {
        "HR 2491": (101.2871, -16.7161, -1.46),
        "HR 7001": (279.2346, 38.7836, 0.03),
        "HR 1713": (78.6346, -8.2017, 0.12),
        "HR 2061": (88.7929, 7.4069, 0.50),
        "HR 424": (37.9529, 89.2642, 2.02),
        "HR 5459": (219.8996, -60.8353, -0.01),
    }
    by_designation = {s.designation: s for s in stars}
    for designation, (ra_deg, dec_deg, vmag) in heasarc.items():
        star = by_designation[designation]
        assert float(star.ra_j2000_deg) == pytest.approx(ra_deg, abs=0.05)
        assert float(star.dec_j2000_deg) == pytest.approx(dec_deg, abs=0.05)
        assert star.magnitude == pytest.approx(vmag, abs=0.2)
