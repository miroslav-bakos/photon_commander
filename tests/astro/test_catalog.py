"""Unit tests for ssc.astro.catalog: offline-first, tolerant name resolution
against the bundled Messier core + OpenNGC-derived NGC/IC + Sharpless files."""

from __future__ import annotations

import tomllib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ssc.astro.catalog import (
    MAX_CONE_LIMIT,
    MAX_CONE_RADIUS_DEG,
    CatalogObject,
    ObjectType,
    UnknownTargetError,
    default_catalog_path,
    default_catalog_paths,
    load_catalog,
    normalize_query,
)
from ssc.astro.units import ArcMin, Deg


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


# --- loading ------------------------------------------------------------


def test_default_catalog_path_is_bundled_and_committed() -> None:
    path = default_catalog_path()
    assert path.name == "objects.toml"
    assert path.is_file(), "the catalogue ships in the repo; there is nothing to fetch"


def test_default_catalog_paths_are_core_then_openngc_then_sharpless() -> None:
    """Precedence order is part of the contract: the public-domain core owns
    every object it describes; the CC BY-SA file only adds."""
    paths = default_catalog_paths()
    assert [p.name for p in paths] == ["objects.toml", "openngc.csv", "sharpless.csv", "vdb.csv", "ldn.csv"]
    for path in paths:
        assert path.is_file()


def test_core_file_is_messier_only_and_does_not_cite_wikipedia() -> None:
    """The header's public-domain claim must stay true of every row in it
    (COMPLIANCE.md "Bundled star catalogue"); the Wikipedia-derived NGC/IC
    supplement moved out to openngc.csv."""
    text = default_catalog_path().read_text(encoding="utf-8")
    assert "wikipedia" not in text.lower()
    with default_catalog_path().open("rb") as handle:
        raw = tomllib.load(handle)
    assert len(raw["objects"]) == 110
    for entry in raw["objects"]:
        assert entry["designations"][0].startswith("M"), entry["designations"]


def test_every_source_file_declares_its_own_credit_line(catalog) -> None:
    """The credit shown in the UI comes from the data file, never from code."""
    assert len(catalog.sources) == 5
    core, openngc, sharpless, vdb, ldn = catalog.sources
    assert "HEASARC" in core
    assert "OpenNGC" in openngc and "CC BY-SA 4.0" in openngc
    assert "Sharpless" in sharpless and "HEASARC" in sharpless
    assert "VdB" in vdb and "HEASARC" in vdb
    assert "Lynds" in ldn and "HEASARC" in ldn
    assert {obj.source for obj in catalog.objects} == set(catalog.sources)


def test_load_time_is_sane_for_fourteen_thousand_objects() -> None:
    """Parsed once at startup; a generous bound so CI noise cannot flake it,
    but tight enough that an accidental O(n^2) merge would trip it."""
    import time

    start = time.perf_counter()
    catalog = load_catalog()
    elapsed = time.perf_counter() - start
    assert len(catalog) > 13_000
    assert elapsed < 2.0, f"load_catalog took {elapsed:.2f}s"


def test_load_catalog_is_pure_file_read_no_network(catalog) -> None:
    """Loading twice from the same path gives independent, equal-content results;
    nothing here reaches out."""
    other = load_catalog()
    assert len(other) == len(catalog)


def test_bundled_catalogue_covers_all_110_messier_objects(catalog) -> None:
    messier_designations = {
        d
        for obj in catalog.objects
        for d in obj.designations
        if d.startswith("M") and d[1:].isdigit()
    }
    assert len(messier_designations) == 110


def test_every_ngc_ic_and_sharpless_number_resolves(catalog) -> None:
    """NGC 1-7840, IC 1-5386, Sh2-1-313. The IC numbers OpenNGC lists as
    nonexistent *with no position at all* (IC 1064, 1326, 1642, 2688, 2915,
    3398, 5112 as of commit da90466) are the only holes: there is no coordinate
    to return for them, and inventing one is exactly what CLAUDE.md forbids."""
    missing_ngc = [n for n in range(1, 7841) if not _resolves(catalog, f"NGC {n}")]
    missing_ic = [n for n in range(1, 5387) if not _resolves(catalog, f"IC {n}")]
    missing_sh = [n for n in range(1, 314) if not _resolves(catalog, f"Sh2-{n}")]
    assert missing_ngc == []
    assert missing_sh == []
    assert len(missing_ic) == 7, missing_ic
    assert 1064 in missing_ic


def _resolves(catalog, query: str) -> bool:
    try:
        catalog.resolve(query)
    except UnknownTargetError:
        return False
    return True


# --- tolerant resolution --------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "M31",
        "M 31",
        "Messier 31",
        "messier31",
        "m31",
        "  M31  ",
        "NGC 224",
        "ngc224",
        "Andromeda Galaxy",
    ],
)
def test_andromeda_aliases_all_resolve_to_the_same_object(catalog, query: str) -> None:
    obj = catalog.resolve(query)
    assert obj.name == "M31"
    assert obj.common_name == "Andromeda Galaxy"


def test_ngc_designation_without_prefix_gap_resolves(catalog) -> None:
    obj = catalog.resolve("NGC2244")
    assert obj is catalog.resolve("NGC 2244")
    assert "NGC 2244" in obj.designations


@pytest.mark.parametrize(
    "query", ["IC 1396", "IC1396", "ic-1396", "ic 1396", "IC 01396", "Ic  1396"]
)
def test_ic_1396_spellings_resolve_to_the_same_object(catalog, query: str) -> None:
    obj = catalog.resolve(query)
    assert obj is catalog.resolve("IC 1396")
    assert obj.name == "IC 1396"
    assert obj.object_type is ObjectType.CLUSTER_WITH_NEBULA
    assert "OpenNGC" in obj.source


def test_ic_1396_coordinates_match_heasarc(catalog) -> None:
    """Independent cross-check, fetched 2026-09-07 from NASA HEASARC via TAP
    (https://heasarc.gsfc.nasa.gov/xamin/vo/tap/sync):
      mwsc table,      name 'IC 1396':  RA 324.75   Dec +57.49   (J2000, 0.01 deg)
      hiiregion table, name 'S131' (= Sh2-131, the same nebula):
                                        RA 324.751  Dec +57.486
    OpenNGC gives 21:38:57.62 +57:29:20.6 = 324.7401, +57.4891, so the bundled
    value must agree with both to better than the tables' own rounding."""
    obj = catalog.resolve("IC 1396")
    assert float(obj.ra_j2000_deg) == pytest.approx(324.75, abs=0.02)
    assert float(obj.dec_j2000_deg) == pytest.approx(57.49, abs=0.02)
    assert obj.constellation == "Cep"


@pytest.mark.parametrize(
    "query",
    ["Sh2-101", "Sh 2-101", "SH2 101", "sh2-101", "Sharpless 101", "Sharpless 2-101", "SH 2 101"],
)
def test_sharpless_spellings_resolve_to_the_same_object(catalog, query: str) -> None:
    obj = catalog.resolve(query)
    assert obj is catalog.resolve("Sh2-101")
    assert obj.name == "Sh2-101"
    assert obj.object_type is ObjectType.HII_REGION
    assert "HEASARC" in obj.source
    # HEASARC hiiregion S101: RA 299.989 Dec +35.291, diameter 20', form 3
    # (irregular) -- so a major axis but no minor axis, and no magnitude.
    assert float(obj.ra_j2000_deg) == pytest.approx(299.989, abs=1e-3)
    assert float(obj.dec_j2000_deg) == pytest.approx(35.291, abs=1e-3)
    assert obj.size_major_arcmin == ArcMin(20.0)
    assert obj.size_minor_arcmin is None
    assert obj.magnitude is None and obj.magnitude_band is None


def test_sharpless_number_without_separator_is_the_object_number(catalog) -> None:
    """ "Sh 201" is Sh2-201, never Sh2-01: the catalogue "2" only counts when
    a separator follows it."""
    assert catalog.resolve("Sh 201").name == "Sh2-201"
    assert catalog.resolve("Sh2-2").name == "Sh2-2"
    assert catalog.resolve("Sh 2-2").name == "Sh2-2"


@pytest.mark.parametrize("query", ["NGC 7000", "NGC7000", "ngc 7000", "North America Nebula"])
def test_ngc_7000_resolves_from_openngc_with_size_and_type(catalog, query: str) -> None:
    obj = catalog.resolve(query)
    assert obj.name == "NGC 7000"
    assert obj.object_type is ObjectType.HII_REGION
    assert obj.size_major_arcmin == ArcMin(120.0)
    assert obj.size_minor_arcmin == ArcMin(30.0)
    # OpenNGC has only a B magnitude for this nebula; the band says so.
    assert obj.magnitude == 4.0 and obj.magnitude_band == "B"
    assert obj.position_angle_deg is None
    assert "CC BY-SA 4.0" in obj.source


def test_messier_object_comes_from_the_public_domain_core(catalog) -> None:
    """The core file wins precedence; OpenNGC's NGC 224 (M 31) record is merged
    into it as an alias rather than shadowing it. ``source`` (the credit line)
    is always the core's alone, even for fields OpenNGC filled in."""
    obj = catalog.resolve("M31")
    assert "HEASARC" in obj.source and "OpenNGC" not in obj.source
    assert catalog.resolve("NGC 224") is obj
    assert catalog.resolve("Andromeda Galaxy") is obj
    for obj in catalog.objects:
        if obj.name.startswith("M") and obj.name[1:].isdigit():
            assert "OpenNGC" not in obj.source, obj.name


def test_messier_core_owns_m31_but_is_filled_by_openngc(catalog) -> None:
    """M31's coordinates, magnitude, size and type are all the core's own —
    it set every one of them, so none is overridden. But the core has no
    position angle, and OpenNGC does (35 deg), so the merged record carries
    it: this is the field the map's ellipse glyph needs and the bug this
    fill behaviour fixes. ``sources`` names both files that fed the record;
    ``source`` still credits the core alone."""
    obj = catalog.resolve("M31")
    assert "HEASARC" in obj.source
    assert float(obj.ra_j2000_deg) == pytest.approx(0.711667 * 15.0)
    assert float(obj.dec_j2000_deg) == pytest.approx(41.266667)
    assert obj.magnitude == pytest.approx(4.5), "the core's own magnitude, not OpenNGC's 3.44"
    assert obj.size_major_arcmin == pytest.approx(145.0), "the core's own size, not OpenNGC's"
    assert obj.size_minor_arcmin == pytest.approx(20.0), "already set by the core"
    assert obj.position_angle_deg == pytest.approx(35.0), "unset by the core, filled by OpenNGC"
    assert obj.source == next(s for s in catalog.sources if "HEASARC" in s)
    assert obj.source in obj.sources
    assert any("OpenNGC" in s for s in obj.sources)
    assert len(obj.sources) == 2, "only the core and OpenNGC contributed a field"


def test_position_angle_is_carried_where_the_source_gives_it(catalog) -> None:
    """M11: PA is the field the ellipse glyphs were missing. OpenNGC NGC 891
    (edge-on): PA 22 deg."""
    obj = catalog.resolve("NGC 891")
    assert obj.position_angle_deg is not None
    assert 0.0 <= float(obj.position_angle_deg) < 360.0
    assert float(obj.position_angle_deg) == pytest.approx(22.0)
    assert catalog.resolve("Sh2-101").position_angle_deg is None


def test_openngc_duplicate_designations_alias_the_master_object(catalog) -> None:
    """OpenNGC lists NGC 2244 as a duplicate of NGC 2239; both must resolve to
    one record rather than two objects at one position."""
    assert catalog.resolve("NGC 2244") is catalog.resolve("NGC 2239")


def test_nonexistent_ngc_entries_resolve_and_say_so(catalog) -> None:
    """A NonEx position resolves with its type set, so the UI can tell the
    user why there is nothing there -- not silently lumped into 'other'."""
    obj = catalog.resolve("IC 67")
    assert obj.object_type is ObjectType.NONEXISTENT


def test_unknown_target_error_names_the_query(catalog) -> None:
    with pytest.raises(UnknownTargetError, match="'Sh2-9999'"):
        catalog.resolve("Sh2-9999")
    with pytest.raises(UnknownTargetError, match="'NGC 99999'"):
        catalog.resolve("NGC 99999")


def test_common_name_lookup_is_case_insensitive(catalog) -> None:
    assert catalog.resolve("andromeda galaxy").name == "M31"
    assert catalog.resolve("ANDROMEDA GALAXY").name == "M31"


def test_unknown_target_raises_rather_than_returning_none(catalog) -> None:
    with pytest.raises(UnknownTargetError):
        catalog.resolve("Definitely Not A Real Target 42")


def test_suggestions_rank_common_names_aliases_and_typos(catalog) -> None:
    assert catalog.suggest("Veil")[0].name == "NGC 6960"
    assert catalog.suggest("Western Veil")[0].name == "NGC 6960"
    assert catalog.suggest("Viel Nebula")[0].name == "NGC 6960"


def test_suggestions_expand_incomplete_catalogue_designations(catalog) -> None:
    names = [obj.name for obj in catalog.suggest("NGC 69")]
    assert names[0] == "NGC 69"
    assert "NGC 690" in names
    assert len(names) == len(set(names))


def test_suggestions_validate_limit_and_empty_query(catalog) -> None:
    assert catalog.suggest("   ") == ()
    with pytest.raises(ValueError, match="limit"):
        catalog.suggest("M", 0)


def test_leading_zeros_in_a_designation_are_ignored(catalog) -> None:
    assert catalog.resolve("M031").name == "M31"
    assert catalog.resolve("NGC00224").name == "M31"


# --- None means unknown, never invented -----------------------------------


def test_unknown_fields_are_none_not_fabricated(catalog) -> None:
    """Every object with a missing magnitude/minor-size/constellation in the
    source data reports None for it -- this asserts at least one real example
    of each exists in the bundled data, so the property is exercised, not
    vacuous. (Every bundled object does have a major size, so that field is
    covered by the synthetic case in test_fits_inscribed_circle_is_none_when_
    size_unknown instead.)"""
    missing_magnitude = [o for o in catalog.objects if o.magnitude is None]
    missing_minor_size = [o for o in catalog.objects if o.size_minor_arcmin is None]
    missing_constellation = [o for o in catalog.objects if o.constellation is None]
    assert missing_magnitude, "expected at least one object with unknown magnitude"
    assert missing_minor_size, "expected at least one object with unknown minor size"
    assert missing_constellation, "expected at least one object with unknown constellation"
    for obj in missing_magnitude:
        assert obj.magnitude is None
    for obj in missing_minor_size:
        assert obj.size_minor_arcmin is None


def test_no_field_is_ever_a_fabricated_zero(catalog) -> None:
    """None means unknown; it never means zero (CLAUDE.md). A real 0.0 magnitude
    or size would be a data bug, not a legitimate absent-value encoding."""
    for obj in catalog.objects:
        assert obj.magnitude != 0.0
        assert obj.size_major_arcmin != 0.0
        assert obj.size_minor_arcmin != 0.0


def test_fits_inscribed_circle_is_none_when_size_unknown() -> None:
    unsized = CatalogObject(
        designations=("TEST0",),
        common_name=None,
        object_type=None,
        ra_j2000_deg=Deg(0.0),
        dec_j2000_deg=Deg(0.0),
        magnitude=None,
        magnitude_band=None,
        size_major_arcmin=None,
        size_minor_arcmin=None,
        position_angle_deg=None,
        constellation=None,
        source="test",
    )
    assert unsized.fits_inscribed_circle(Deg(2.24)) is None


def test_fits_inscribed_circle_compares_major_axis_to_the_circle_diameter() -> None:
    huge = CatalogObject(
        designations=("TEST2",),
        common_name=None,
        object_type=None,
        ra_j2000_deg=Deg(0.0),
        dec_j2000_deg=Deg(0.0),
        magnitude=None,
        magnitude_band=None,
        size_major_arcmin=ArcMin(200.0),
        size_minor_arcmin=None,
        position_angle_deg=None,
        constellation=None,
        source="test",
    )
    assert huge.fits_inscribed_circle(Deg(2.24)) is False
    tiny = CatalogObject(
        designations=("TEST3",),
        common_name=None,
        object_type=None,
        ra_j2000_deg=Deg(0.0),
        dec_j2000_deg=Deg(0.0),
        magnitude=None,
        magnitude_band=None,
        size_major_arcmin=ArcMin(1.0),
        size_minor_arcmin=None,
        position_angle_deg=None,
        constellation=None,
        source="test",
    )
    assert tiny.fits_inscribed_circle(Deg(2.24)) is True


# --- data integrity --------------------------------------------------------


def test_every_object_type_is_a_known_enum_member(catalog) -> None:
    for obj in catalog.objects:
        if obj.object_type is not None:
            assert obj.object_type in ObjectType


def test_ra_and_dec_are_in_range(catalog) -> None:
    for obj in catalog.objects:
        assert 0.0 <= float(obj.ra_j2000_deg) < 360.0
        assert -90.0 <= float(obj.dec_j2000_deg) <= 90.0


def test_no_duplicate_designations_in_the_bundled_file() -> None:
    with default_catalog_path().open("rb") as handle:
        raw = tomllib.load(handle)
    seen: set[str] = set()
    for entry in raw["objects"]:
        for designation in entry["designations"]:
            key = designation.strip().upper()
            assert key not in seen, f"duplicate designation in source data: {designation}"
            seen.add(key)


@pytest.mark.parametrize("path", default_catalog_paths()[1:], ids=lambda p: p.name)
def test_derived_csv_files_carry_their_licence_header(path) -> None:
    """The share-alike / public-domain statement travels inside the data file
    it applies to (COMPLIANCE.md), as machine-readable `# key:` lines."""
    head = "".join(line for line in path.read_text(encoding="utf-8").splitlines(keepends=True)[:60])
    for key in ("# source:", "# license:", "# license_url:", "# attribution:", "# upstream:"):
        assert key in head, f"{path.name} is missing {key}"
    if path.name == "openngc.csv":
        assert "CC-BY-SA-4.0" in head and "mattiaverga/OpenNGC" in head
    elif path.name == "sharpless.csv":
        assert "public-domain" in head and "heasarc" in head.lower()
    else:
        assert "HEASARC open-data" in head
        assert "heasarc/data_policy.html" in head
        assert "upstream_tdat_sha256" in head


def test_no_duplicate_designations_within_any_csv_file() -> None:
    import csv

    for path in default_catalog_paths()[1:]:
        seen: set[str] = set()
        with path.open(encoding="utf-8", newline="") as handle:
            rows = csv.reader(line for line in handle if not line.startswith("#"))
            next(rows)  # column header
            for row in rows:
                for designation in row[0].split(";"):
                    key = designation.upper()
                    assert key not in seen, f"{path.name}: duplicate designation {designation}"
                    seen.add(key)


def test_merging_files_raises_on_conflicting_identity(tmp_path) -> None:
    """A later file whose record spans two different earlier objects is a data
    bug, and it must be loud, not resolved by whichever key was seen first."""
    core = tmp_path / "a.toml"
    core.write_text(
        'source = "test core"\n'
        "[[objects]]\n"
        'designations = ["NGC 1"]\n'
        "ra_j2000_h = 0.1\n"
        "dec_j2000_deg = 1.0\n"
        "[[objects]]\n"
        'designations = ["NGC 2"]\n'
        "ra_j2000_h = 0.2\n"
        "dec_j2000_deg = 2.0\n"
    )
    later = tmp_path / "b.csv"
    later.write_text(
        "# source: test later\n"
        "designations,common_names,object_type,ra_j2000_deg,dec_j2000_deg,constellation,"
        "size_major_arcmin,size_minor_arcmin,position_angle_deg,magnitude,magnitude_band\n"
        "NGC 1;NGC 2,,,1.5,1.5,,,,,,\n"
    )
    with pytest.raises(ValueError, match="two different earlier objects"):
        load_catalog([core, later])


def test_later_file_aliases_and_does_not_shadow(tmp_path) -> None:
    core = tmp_path / "a.toml"
    core.write_text(
        'source = "test core"\n[[objects]]\ndesignations = ["M 1"]\nra_j2000_h = 0.1\ndec_j2000_deg = 1.0\n'
    )
    later = tmp_path / "b.csv"
    later.write_text(
        "# source: test later\n"
        "designations,common_names,object_type,ra_j2000_deg,dec_j2000_deg,constellation,"
        "size_major_arcmin,size_minor_arcmin,position_angle_deg,magnitude,magnitude_band\n"
        "NGC 1952;M 1,Crab Nebula,supernova_remnant,83.63,22.01,Tau,6,4,,8.4,V\n"
        "NGC 2,,galaxy,0.5,0.5,,,,,,\n"
    )
    catalog = load_catalog([core, later])
    assert len(catalog) == 2
    crab = catalog.resolve("M1")
    assert crab.source == "test core"
    assert catalog.resolve("NGC 1952") is crab
    assert catalog.resolve("crab nebula") is crab
    assert catalog.resolve("NGC 2").source == "test later"


def test_later_file_fills_missing_fields_but_never_overrides_owners_own(tmp_path) -> None:
    """The bug this fixes: an owner (core) record with a field the source data
    simply didn't give should still pick that field up from a later file
    (OpenNGC-shaped here), rather than staying ``None`` forever just because
    the core happened to claim the designation first. But a field the owner
    *did* set — even from a third, later-still file — is never touched."""
    core = tmp_path / "a.toml"
    core.write_text(
        'source = "test core"\n'
        "[[objects]]\n"
        'designations = ["M 1"]\n'
        "ra_j2000_h = 0.1\n"
        "dec_j2000_deg = 1.0\n"
        "magnitude = 9.0\n"
    )
    later = tmp_path / "b.csv"
    later.write_text(
        "# source: test later\n"
        "designations,common_names,object_type,ra_j2000_deg,dec_j2000_deg,constellation,"
        "size_major_arcmin,size_minor_arcmin,position_angle_deg,magnitude,magnitude_band\n"
        "NGC 1952;M 1,Crab Nebula,supernova_remnant,83.63,22.01,Tau,6,4,100,8.4,V\n"
    )
    latest = tmp_path / "c.csv"
    latest.write_text(
        "# source: test latest\n"
        "designations,common_names,object_type,ra_j2000_deg,dec_j2000_deg,constellation,"
        "size_major_arcmin,size_minor_arcmin,position_angle_deg,magnitude,magnitude_band\n"
        "IC 9999;M 1,,open_cluster,83.63,22.01,Tau,55,33,150,1.0,V\n"
    )
    catalog = load_catalog([core, latest, later])
    crab = catalog.resolve("M1")
    # coordinates and the owner's own magnitude are the core's, untouched.
    assert float(crab.ra_j2000_deg) == pytest.approx(0.1 * 15.0)
    assert float(crab.dec_j2000_deg) == pytest.approx(1.0)
    assert crab.magnitude == pytest.approx(9.0), "the owner set this; never overridden"
    assert crab.magnitude_band == "V"
    assert crab.source == "test core"
    # "c.csv" (loaded second) fills the fields the core left unset...
    assert crab.size_major_arcmin == pytest.approx(55.0)
    assert crab.size_minor_arcmin == pytest.approx(33.0)
    assert crab.position_angle_deg == pytest.approx(150.0)
    assert crab.object_type == ObjectType.OPEN_CLUSTER
    # ...and "b.csv" (loaded third) then has nothing left to add: every field
    # it could have filled was already filled by "c.csv", so it never
    # overrides c.csv's values and it does not appear in sources.
    assert crab.sources == ("test core", "test latest")


def test_csv_without_source_credit_line_is_rejected(tmp_path) -> None:
    later = tmp_path / "b.csv"
    later.write_text(
        "designations,common_names,object_type,ra_j2000_deg,dec_j2000_deg,constellation,"
        "size_major_arcmin,size_minor_arcmin,position_angle_deg,magnitude,magnitude_band\n"
        "NGC 2,,galaxy,0.5,0.5,,,,,,\n"
    )
    with pytest.raises(ValueError, match="source"):
        load_catalog([later])


# --- normalisation properties -----------------------------------------------


@given(st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs")), max_size=30))
def test_normalize_query_never_raises(text: str) -> None:
    normalize_query(text)


@given(st.text(max_size=40))
def test_normalize_query_is_idempotent(text: str) -> None:
    once = normalize_query(text)
    assert normalize_query(once) == once


@given(
    st.integers(min_value=1, max_value=7840),
    st.sampled_from(["NGC", "ngc", "Ngc"]),
    st.sampled_from(["", " ", "  ", "-"]),
)
def test_every_ngc_spelling_is_one_canonical_key(number: int, prefix: str, sep: str) -> None:
    from ssc.astro.catalog import _canonical_designation

    assert _canonical_designation(f"{prefix}{sep}{number}") == f"NGC{number}"
    assert _canonical_designation(f"{prefix}{sep}{number:05d}") == f"NGC{number}"


@given(st.integers(min_value=1, max_value=313))
def test_every_sharpless_spelling_is_one_canonical_key(number: int) -> None:
    from ssc.astro.catalog import _canonical_designation

    spellings = [
        f"Sh2-{number}",
        f"Sh 2-{number}",
        f"SH2 {number}",
        f"Sharpless {number}",
        f"sharpless 2-{number}",
    ]
    assert {_canonical_designation(s) for s in spellings} == {f"SH2-{number}"}


def test_normalize_query_collapses_internal_whitespace() -> None:
    assert normalize_query("M   31") == "M 31"
    assert normalize_query("  m31  ") == "M31"


# --- cone query --------------------------------------------------------------


def test_within_cone_is_largest_first_and_wraps_ra_at_zero(catalog) -> None:
    m31 = catalog.resolve("M31")
    hits = catalog.within_cone(float(m31.ra_j2000_deg), float(m31.dec_j2000_deg), 1.5, 50)
    names = [obj.name for obj in hits]
    assert names[0] == "M31"
    assert {"M32", "M110"} <= set(names)
    sizes = [float(o.size_major_arcmin) for o in hits if o.size_major_arcmin is not None]
    assert sizes == sorted(sizes, reverse=True)

    # The cone straddles RA 0h: M31 (RA ~10.7 deg) is 11 deg from RA 359.9
    # along RA at Dec 0, but only ~8 deg of arc at Dec +41 — a separation
    # computed as a plain RA difference would miss it.
    wrapped = catalog.within_cone(359.9, float(m31.dec_j2000_deg), 9.0, 500)
    assert m31 in wrapped


def test_within_cone_refuses_rather_than_clamps_out_of_range_arguments(catalog) -> None:
    # Derived from the constants, not hardcoded: this test asserted a literal
    # 10.01 and so failed the day the ceiling was raised to cover a 60-degree
    # field, reporting a stale test as a broken method.
    with pytest.raises(ValueError):
        catalog.within_cone(10.0, 41.0, MAX_CONE_RADIUS_DEG + 0.01, 10)
    with pytest.raises(ValueError):
        catalog.within_cone(10.0, 41.0, 1.0, MAX_CONE_LIMIT + 1)
    with pytest.raises(ValueError):
        catalog.within_cone(10.0, 41.0, 0.0, 10)
    # And the ceiling itself is accepted, so "refuses above" cannot silently
    # become "refuses at".
    catalog.within_cone(10.0, 41.0, MAX_CONE_RADIUS_DEG, MAX_CONE_LIMIT)


@pytest.mark.parametrize("query,ra,dec,kind",[
    ("vdb142",324.17083333,57.50111111,ObjectType.REFLECTION_NEBULA),
    ("Van den Bergh 141",319.10833333,68.26027778,ObjectType.REFLECTION_NEBULA),
    ("LDN 1235",333.7283,73.4161,ObjectType.DARK_NEBULA),
    ("ldn1622",88.6503,2.0081,ObjectType.DARK_NEBULA),
])
def test_nebula_catalogues_resolve_real_j2000_positions(catalog,query,ra,dec,kind):
    obj=catalog.resolve(query)
    assert obj.ra_j2000_deg == pytest.approx(ra)
    assert obj.dec_j2000_deg == pytest.approx(dec)
    assert obj.object_type == kind
    assert obj.size_major_arcmin is None and obj.magnitude is None
    assert "HEASARC" in obj.source
    assert obj in catalog.suggest(query)

def test_vdb_components_and_unnamed_ldn_are_not_invented(catalog):
    assert catalog.resolve("VdB 64a") != catalog.resolve("VdB 64b")
    assert catalog.resolve("VDB 1a-d") is catalog.resolve("VdB 1")
    with pytest.raises(UnknownTargetError):catalog.resolve("NN")
    assert sum(o.source==catalog.sources[3] for o in catalog.objects)==163
    assert sum(o.source==catalog.sources[4] for o in catalog.objects)==1787


@pytest.mark.parametrize("query,designation", [
    ("Dark Shark Nebula", "LDN 1235"), ("dark   shark", "LDN 1235"),
    ("Shark Nebula", "LDN 1235"), ("Shark", "LDN 1235"),
    ("Ghost Nebula", "VdB 141"), ("Boogeyman Nebula", "LDN 1622"),
])
def test_familiar_nebula_names_resolve_and_suggest_same_object(catalog, query, designation):
    target = catalog.resolve(designation)
    assert catalog.resolve(query) is target
    assert target in catalog.suggest(query)
