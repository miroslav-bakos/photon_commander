# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Offline-first target resolution: a typed name to J2000 RA/Dec plus metadata.

A dark site often has no signal (DESIGN.md 6 makes the same point about
weather), so this is catalogue lookup against **bundled** tables, not a
network call. :func:`load_catalog` reads the files under ``data/catalog/``
once; those file reads are the only I/O in this module, exactly as
:func:`ssc.astro.framing.load_profile` is the only I/O in that one. There is
no online fallback here — if one is ever added elsewhere, DESIGN.md's
offline-first framing requires it to be an explicit, clearly-marked opt-in,
never a silent dependency of :func:`resolve`.

The catalogue is several files, merged in a fixed precedence order, because
they carry different licences (THIRD_PARTY_NOTICES.md, "Bundled star catalogue"):

1. ``objects.toml`` — the Messier core; public-domain facts, HEASARC-checked.
2. ``openngc.csv`` — NGC 1-7840 and IC 1-5386 derived from OpenNGC, which is
   CC BY-SA 4.0 and therefore lives in its own file.
3. ``sharpless.csv`` — Sh2-1-313 from NASA HEASARC's ``hiiregion`` table.

Every object records the ``source`` credit line of the file it came from, read
from that file's own header — the UI's credit line is driven by the data, never
by a string in code. When two files describe the same object (OpenNGC's
``NGC 224`` is the core's ``M31``), the earlier file's record wins: its own
values are never overridden, and its coordinates always stay its own. But a
later file may still *fill* a field the earlier file left ``None``
(``position_angle_deg``, ``size_major_arcmin``, ``size_minor_arcmin``,
``magnitude``/``magnitude_band`` together, and ``object_type`` when the
owner's is unknown or the generic ``other``) — this is how M31 ends up with
OpenNGC's position angle even though the Messier core owns the record. The
later file's extra designations become aliases of the owner, and
``CatalogObject.sources`` lists every file that actually contributed a field,
owner first, so the UI credit line can name all of them; ``source`` itself
never changes from the owner's.

Right ascension is degrees inside this module, exactly as in the rest of
``ssc.astro`` (see ``units.py``); ``objects.toml`` stores it in hours, the
JSON boundary form, and :func:`load_catalog` converts once at load time; the
CSV files store degrees.
"""

from __future__ import annotations

import csv
import math
import re
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ssc.astro.units import ArcMin, Deg, Hours, hours_to_deg, wrap_deg_360

__all__ = [
    "MAX_CONE_LIMIT",
    "MAX_CONE_RADIUS_DEG",
    "CatalogObject",
    "ObjectType",
    "TargetCatalog",
    "UnknownTargetError",
    "cos_angular_separation",
    "default_catalog_path",
    "default_catalog_paths",
    "load_catalog",
    "normalize_query",
]


class ObjectType(StrEnum):
    """The categories present in the bundled catalogues. Every upstream class
    OpenNGC or Sharpless uses maps to its own member — nothing is lumped into
    ``OTHER`` that the source did not itself call "other"."""

    STAR = "star"
    DOUBLE_STAR = "double_star"
    STELLAR_ASSOCIATION = "stellar_association"
    ASTERISM = "asterism"
    OPEN_CLUSTER = "open_cluster"
    GLOBULAR_CLUSTER = "globular_cluster"
    CLUSTER_WITH_NEBULA = "cluster_with_nebula"
    STAR_CLOUD = "star_cloud"
    GALAXY = "galaxy"  # OpenNGC "G" with no Hubble type to refine it
    GALAXY_SPIRAL = "galaxy_spiral"
    GALAXY_ELLIPTICAL = "galaxy_elliptical"
    GALAXY_LENTICULAR = "galaxy_lenticular"
    GALAXY_IRREGULAR = "galaxy_irregular"
    GALAXY_PAIR = "galaxy_pair"
    GALAXY_TRIPLET = "galaxy_triplet"
    GALAXY_GROUP = "galaxy_group"
    PLANETARY_NEBULA = "planetary_nebula"
    HII_REGION = "hii_region"
    EMISSION_NEBULA = "emission_nebula"
    REFLECTION_NEBULA = "reflection_nebula"
    DARK_NEBULA = "dark_nebula"
    DIFFUSE_NEBULA = "diffuse_nebula"
    SUPERNOVA_REMNANT = "supernova_remnant"
    NOVA = "nova"
    #: An NGC/IC number whose catalogued position holds nothing (OpenNGC
    #: "NonEx"); it still resolves so the user learns *why* there is no target.
    NONEXISTENT = "nonexistent"
    #: The source's own "other" class — see OpenNGC's per-object notes.
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class CatalogObject:
    """One bundled target. Any field the source data did not give is ``None`` —
    never a guessed magnitude or size (CLAUDE.md: "None means unknown; it
    never means zero.").
    """

    designations: tuple[str, ...]
    common_name: str | None
    object_type: ObjectType | None
    ra_j2000_deg: Deg
    dec_j2000_deg: Deg
    magnitude: float | None
    #: Which photometric band ``magnitude`` is (``"V"`` or ``"B"``); ``None``
    #: exactly when ``magnitude`` is. OpenNGC often has only B for nebulae.
    magnitude_band: str | None
    size_major_arcmin: ArcMin | None
    size_minor_arcmin: ArcMin | None
    #: Major-axis position angle, degrees north through east, [0, 180).
    position_angle_deg: Deg | None
    constellation: str | None
    #: The credit line of the data file this record came from, verbatim from
    #: that file's header. Drives the UI's attribution; never a static string.
    #: This is the *owning* file only — see :attr:`sources` for every file
    #: that contributed a field.
    source: str
    #: Every ``source`` credit line that actually contributed a field to this
    #: record, owner first, in load order. A file that merely re-claims one
    #: of the owner's designations without filling anything new (every field
    #: it had was already set) is not listed. Defaults to ``()`` for objects
    #: built outside :func:`load_catalog` (e.g. test fixtures); real records
    #: always have at least the owner's own credit line.
    sources: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The designation a caller should display: the first one listed."""
        return self.designations[0]

    def fits_inscribed_circle(self, inscribed_circle_deg: Deg) -> bool | None:
        """Whether the object's larger dimension fits the guaranteed-keep circle.

        ``None`` when the size is unknown rather than a possibly-wrong guess —
        the caller (the planner) decides what to do about an unknown size, this
        module never invents one to make the question answerable.
        """
        if self.size_major_arcmin is None:
            return None
        return float(self.size_major_arcmin) <= float(inscribed_circle_deg) * 60.0


class UnknownTargetError(LookupError):
    """Raised by :meth:`TargetCatalog.resolve` for a query no entry matches."""


# --- name normalisation ------------------------------------------------------
#
# Tolerant matching: "M31", "M 31", "Messier 31", "NGC 224" and a common name
# like "Andromeda Galaxy" must all resolve to the same object. Catalogue
# designations are normalised to a single canonical key; common names are
# matched on a separately normalised (casefolded, whitespace-collapsed) form.

#: Spelled-out catalogue names -> the canonical prefix.
_PREFIX_ALIASES: Final[dict[str, str]] = {
    "MESSIER": "M",
    "CALDWELL": "C",
    "COLLINDER": "CR",
    "MELOTTE": "MEL",
    "BARNARD": "B",
}

#: Sharpless is the odd one out: the catalogue number is "2" and the object
#: number follows it, so "Sh2-101", "Sh 2-101", "SH2 101", "Sharpless 101"
#: and "Sharpless 2-101" are all Sh2-101. The "2" is only recognised as the
#: catalogue part when a separator follows it, so "Sh 201" is Sh2-201, never
#: Sh2-01.
_SHARPLESS_RE: Final = re.compile(r"^(?:SH|SHARPLESS)\s*-?\s*(?:2\s*-\s*|2\s+|)0*([0-9]+)$")

#: Everything else: PREFIX [sep] NUMBER [-NUMBER] [SUFFIX], e.g. "NGC 7000",
#: "ic-1396", "IC 715NW", "ESO 56-115", "M31".
_DESIGNATION_RE: Final = re.compile(
    r"^([A-Z]+)\s*-?\s*0*([0-9]+)(?:\s*-\s*0*([0-9]+))?\s*([A-Z]{0,2})$"
)


def normalize_query(raw: str) -> str:
    """Collapse whitespace and case for both designation and common-name matching.

    Exposed because a caller (e.g. an autocomplete box) may want the same
    normalisation the resolver uses, without going through a lookup. It is
    idempotent: ``normalize_query(normalize_query(s)) == normalize_query(s)``.
    """
    return re.sub(r"\s+", " ", raw.strip()).upper()


def _canonical_designation(raw: str) -> str | None:
    """``"M 31"``, ``"Messier 31"``, ``"m31"`` -> ``"M31"``; ``"Sh 2-101"`` ->
    ``"SH2-101"``. ``None`` if not a recognised catalogue designation shape at
    all (e.g. a common name)."""
    text = normalize_query(raw)
    sharpless = _SHARPLESS_RE.match(text)
    if sharpless is not None:
        return f"SH2-{int(sharpless.group(1))}"
    match = _DESIGNATION_RE.match(text)
    if match is None:
        return None
    prefix, number, second, suffix = match.groups()
    prefix = _PREFIX_ALIASES.get(prefix, prefix)
    key = f"{prefix}{int(number)}"
    if second is not None:
        key += f"-{int(second)}"
    return key + suffix


def _canonical_common_name(raw: str) -> str:
    return normalize_query(raw).casefold()


# --- catalogue files ---------------------------------------------------------


def default_catalog_path() -> Path:
    """``<repo>/data/catalog/objects.toml`` — the public-domain Messier core.
    Bundled and committed, unlike the gitignored ephemeris kernel: there is
    nothing to fetch."""
    return _catalog_dir() / "objects.toml"


def default_catalog_paths() -> tuple[Path, ...]:
    """All bundled catalogue files, in precedence order (module docstring)."""
    directory = _catalog_dir()
    return (
        directory / "objects.toml",
        directory / "openngc.csv",
        directory / "sharpless.csv",
    )


def _catalog_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "catalog"


#: One parsed row: the object plus every common name the file lists for it
#: (``CatalogObject.common_name`` keeps the first; all of them resolve).
_Record = tuple[CatalogObject, tuple[str, ...]]


def _optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return None if value is None else str(value)


def _optional_float(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    return None if value is None else float(value)


def _check_ranges(name: str, ra_deg: float, dec_deg: float, pa_deg: float | None) -> None:
    if not 0.0 <= ra_deg < 360.0:
        raise ValueError(f"{name}: RA out of [0, 360) deg: {ra_deg}")
    if not -90.0 <= dec_deg <= 90.0:
        raise ValueError(f"{name}: Dec out of [-90, 90] deg: {dec_deg}")
    if pa_deg is not None and not 0.0 <= pa_deg < 360.0:
        raise ValueError(f"{name}: position angle out of [0, 360) deg: {pa_deg}")


def _parse_toml_object(raw: dict[str, Any], source: str) -> _Record:
    designations = tuple(str(d) for d in raw["designations"])
    if not designations:
        raise ValueError("catalogue entry has an empty designations list")
    object_type_raw = raw.get("object_type")
    ra_h = Hours(float(raw["ra_j2000_h"]))
    if not 0.0 <= float(ra_h) < 24.0:
        raise ValueError(f"{designations[0]}: ra_j2000_h out of [0, 24): {ra_h}")
    ra_deg = float(hours_to_deg(ra_h))
    dec_deg = float(raw["dec_j2000_deg"])
    pa_deg = _optional_float(raw, "position_angle_deg")
    _check_ranges(designations[0], ra_deg, dec_deg, pa_deg)
    magnitude = _optional_float(raw, "magnitude")
    size_major = _optional_float(raw, "size_major_arcmin")
    size_minor = _optional_float(raw, "size_minor_arcmin")
    common_name = _optional_str(raw, "common_name")
    obj = CatalogObject(
        designations=designations,
        common_name=common_name,
        object_type=None if object_type_raw is None else ObjectType(object_type_raw),
        ra_j2000_deg=wrap_deg_360(ra_deg),
        dec_j2000_deg=Deg(dec_deg),
        magnitude=magnitude,
        # objects.toml's schema defines `magnitude` as apparent visual magnitude.
        magnitude_band=None if magnitude is None else "V",
        size_major_arcmin=None if size_major is None else ArcMin(size_major),
        size_minor_arcmin=None if size_minor is None else ArcMin(size_minor),
        position_angle_deg=None if pa_deg is None else Deg(pa_deg),
        constellation=_optional_str(raw, "constellation"),
        source=source,
        sources=(source,),
    )
    return obj, () if common_name is None else (common_name,)


def _read_toml(path: Path) -> tuple[str, list[_Record]]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"catalogue at {path} declares no top-level `source` credit line")
    records = [_parse_toml_object(entry, source) for entry in raw.get("objects", ())]
    if not records:
        raise ValueError(f"catalogue at {path} declares no [[objects]]")
    return source, records


_CSV_COLUMNS: Final = (
    "designations",
    "common_names",
    "object_type",
    "ra_j2000_deg",
    "dec_j2000_deg",
    "constellation",
    "size_major_arcmin",
    "size_minor_arcmin",
    "position_angle_deg",
    "magnitude",
    "magnitude_band",
)
_CSV_LIST_SEP: Final = ";"


def _csv_float(text: str) -> float | None:
    return None if text == "" else float(text)


def _parse_csv_row(row: Sequence[str], source: str, path: Path, line: int) -> _Record:
    if len(row) != len(_CSV_COLUMNS):
        raise ValueError(f"{path}:{line}: expected {len(_CSV_COLUMNS)} columns, got {len(row)}")
    (
        designations_text,
        common_names_text,
        object_type_text,
        ra_text,
        dec_text,
        constellation,
        major_text,
        minor_text,
        pa_text,
        magnitude_text,
        magnitude_band,
    ) = row
    designations = tuple(d for d in designations_text.split(_CSV_LIST_SEP) if d)
    if not designations:
        raise ValueError(f"{path}:{line}: empty designations")
    common_names = tuple(n for n in common_names_text.split(_CSV_LIST_SEP) if n)
    ra_deg = float(ra_text)
    dec_deg = float(dec_text)
    pa_deg = _csv_float(pa_text)
    _check_ranges(designations[0], ra_deg, dec_deg, pa_deg)
    magnitude = _csv_float(magnitude_text)
    if (magnitude is None) != (magnitude_band == ""):
        raise ValueError(f"{path}:{line}: magnitude and magnitude_band must be given together")
    size_major = _csv_float(major_text)
    size_minor = _csv_float(minor_text)
    obj = CatalogObject(
        designations=designations,
        common_name=common_names[0] if common_names else None,
        object_type=None if object_type_text == "" else ObjectType(object_type_text),
        ra_j2000_deg=wrap_deg_360(ra_deg),
        dec_j2000_deg=Deg(dec_deg),
        magnitude=magnitude,
        magnitude_band=None if magnitude is None else magnitude_band,
        size_major_arcmin=None if size_major is None else ArcMin(size_major),
        size_minor_arcmin=None if size_minor is None else ArcMin(size_minor),
        position_angle_deg=None if pa_deg is None else Deg(pa_deg),
        constellation=None if constellation == "" else constellation,
        source=source,
        sources=(source,),
    )
    return obj, common_names


def _csv_lines(handle: Iterator[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` skipping ``#`` comment lines, so the
    licence header travels inside the data file it licenses."""
    for number, line in enumerate(handle, start=1):
        if not line.startswith("#"):
            yield number, line


def _read_csv(path: Path) -> tuple[str, list[_Record]]:
    source: str | None = None
    records: list[_Record] = []
    with path.open(encoding="utf-8", newline="") as handle:
        # The credit line is a `# source: ...` header comment; read it before
        # handing the rest to the csv reader.
        header_lines: list[str] = []
        for line in handle:
            if not line.startswith("#"):
                header_lines.append(line)
                break
            if line.startswith("# source:"):
                source = line.removeprefix("# source:").strip()
        if source is None or not source:
            raise ValueError(f"catalogue at {path} declares no `# source:` credit line")
        if not header_lines:
            raise ValueError(f"catalogue at {path} has no column header row")
        column_row = next(csv.reader(header_lines))
        if tuple(column_row) != _CSV_COLUMNS:
            raise ValueError(f"catalogue at {path} has unexpected columns {column_row!r}")
        numbered = list(_csv_lines(handle))
        rows = csv.reader(line for _, line in numbered)
        for (line_number, _), row in zip(numbered, rows, strict=True):
            if not row:
                continue
            records.append(_parse_csv_row(row, source, path, line_number))
    if not records:
        raise ValueError(f"catalogue at {path} has no data rows")
    return source, records


def _read_file(path: Path) -> tuple[str, list[_Record]]:
    if path.suffix == ".toml":
        return _read_toml(path)
    if path.suffix == ".csv":
        return _read_csv(path)
    raise ValueError(f"catalogue file {path} has an unrecognised extension (want .toml or .csv)")


# --- catalogue ---------------------------------------------------------------


#: Ceilings for :meth:`TargetCatalog.within_cone`. A sky-map field query is
#: for what is *in the field*; more than 500 objects is a catalogue dump,
#: which is not what this method is for.
#:
#: The radius was 10 degrees until 2026-09-09, on the same "a field, not a
#: dump" reasoning. But the sky map opens at 60 degrees and zooms out to 60,
#: whose half-diagonal is over 50 degrees — so a 10-degree ceiling did not
#: bound a *dump*, it silently cropped the field to a disc in the middle of
#: the view and left the corners empty. The owner asked for object outlines
#: at 60 degrees; this is what makes that cover the visible sky rather than
#: a patch of it.
#:
#: Raised again to 90 on the same day, when the map's `MAX_FOV_DEG` went to
#: 120 and a 60-degree cone once more covered only part of the view. 90 is
#: the sensible ceiling for a cone: past it the "cone" is most of the sky
#: and `MAX_CONE_LIMIT` is what bounds the answer anyway. `within_cone` is a
#: linear scan
#: whose cost does not depend on the radius. `MAX_CONE_LIMIT` is unchanged
#: and is what actually bounds the answer: results come back largest-first,
#: so when the cap bites it keeps the objects that dominate a wide field and
#: drops the ones too small to draw there anyway.
MAX_CONE_RADIUS_DEG: Final = 90.0
MAX_CONE_LIMIT: Final = 500


def cos_angular_separation(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float
) -> float:
    """The cosine of the great-circle separation between two J2000 positions.

    Comparing cosines against ``cos(radius)`` avoids an ``acos`` call per
    candidate and is exact for the "inside this cone or not" question that
    every cone query in ``ssc.astro`` asks — shared here rather than
    reimplemented per catalogue (:meth:`TargetCatalog.within_cone` and
    :func:`ssc.astro.stars.stars_within_cone` both call this one function).
    """
    dec1 = math.radians(dec1_deg)
    dec2 = math.radians(dec2_deg)
    d_ra = math.radians(ra2_deg - ra1_deg)
    return math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(d_ra)


@dataclass(frozen=True, slots=True)
class TargetCatalog:
    """The bundled objects plus the indexes :meth:`resolve` searches.

    Built once by :func:`load_catalog`; pure data from then on, so a caller can
    hold one for the process lifetime.
    """

    objects: tuple[CatalogObject, ...]
    #: The credit line of every file that was loaded, in precedence order.
    sources: tuple[str, ...]
    _by_designation: dict[str, CatalogObject]
    _by_common_name: dict[str, CatalogObject]

    def resolve(self, query: str) -> CatalogObject:
        """Resolve a name tolerantly: ``"M31"``, ``"M 31"``, ``"Messier 31"``,
        ``"NGC 224"`` and ``"Andromeda Galaxy"`` all return the same object;
        so do ``"IC1396"``, ``"IC 1396"`` and ``"ic-1396"``, and ``"Sh2-101"``,
        ``"Sh 2-101"``, ``"SH2 101"`` and ``"Sharpless 101"``.

        Raises :class:`UnknownTargetError` rather than returning ``None`` — an
        unresolved target is a caller error to handle explicitly, not a value
        to silently propagate (CLAUDE.md invariant 7).
        """
        designation_key = _canonical_designation(query)
        if designation_key is not None:
            found = self._by_designation.get(designation_key)
            if found is not None:
                return found
        common_key = _canonical_common_name(query)
        found = self._by_common_name.get(common_key)
        if found is not None:
            return found
        raise UnknownTargetError(
            f"no bundled catalogue entry matches {query!r}. The offline catalogue covers the "
            "Messier objects, NGC 1-7840, IC 1-5386 and Sharpless Sh2-1-313 plus their "
            "common names (data/catalog/) — it is not a full-sky resolver."
        )

    def suggest(self, query: str, limit: int = 8) -> tuple[CatalogObject, ...]:
        """Rank catalogue matches for an autocomplete box.

        Exact and prefix matches always beat substring matches, which in turn
        beat typo-tolerant matches.  Every designation and every bundled
        common-name alias participates, but an object is returned only once.
        This remains an offline, deterministic lookup over the in-memory
        indexes built by :func:`load_catalog`.
        """
        if not 1 <= limit <= 20:
            raise ValueError(f"limit must be in [1, 20], got {limit}")
        text = normalize_query(query)
        if not text:
            return ()

        designation_query = _canonical_designation(query)
        common_query = _canonical_common_name(query)
        ranked: dict[int, tuple[float, CatalogObject]] = {}

        def consider(
            term: str, needle: str, obj: CatalogObject, *, allow_fuzzy: bool = False
        ) -> None:
            if term == needle:
                score = 100.0
            elif term.startswith(needle):
                score = 90.0 + min(5.0, len(needle) / max(len(term), 1) * 5.0)
            elif any(part.startswith(needle) for part in term.split()):
                score = 82.0
            elif needle in term:
                score = 76.0
            elif allow_fuzzy and len(needle) >= 3:
                similarity = SequenceMatcher(None, needle, term).ratio()
                threshold = 0.72 if len(needle) <= 4 else 0.58
                if similarity < threshold:
                    return
                score = 50.0 + similarity * 20.0
            else:
                return
            identity = id(obj)
            previous = ranked.get(identity)
            if previous is None or score > previous[0]:
                ranked[identity] = (score, obj)

        if designation_query is not None:
            for term, obj in self._by_designation.items():
                consider(term, designation_query, obj)
        for term, obj in self._by_common_name.items():
            consider(term, common_query, obj)

        # Typo matching is the more expensive fallback. Prefix/substring
        # matches are both more useful and much cheaper (important while the
        # user is typing a catalogue number), so only run edit similarity if
        # the lexical pass found nothing at all.
        if not ranked:
            if designation_query is not None:
                for term, obj in self._by_designation.items():
                    consider(term, designation_query, obj, allow_fuzzy=True)
            for term, obj in self._by_common_name.items():
                consider(term, common_query, obj, allow_fuzzy=True)

        return tuple(
            obj
            for _, obj in sorted(
                ranked.values(),
                key=lambda item: (-item[0], item[1].name),
            )[:limit]
        )

    def within_cone(
        self, ra_deg: float, dec_deg: float, radius_deg: float, limit: int
    ) -> tuple[CatalogObject, ...]:
        """Every object within ``radius_deg`` of ``(ra_deg, dec_deg)``, largest
        first, at most ``limit`` of them. Pure: an angular-separation filter
        over the bundled objects, nothing more.

        Largest first (unknown size last) so that when the cap bites, the
        things that dominate a field are the ones a map still draws.
        ``radius_deg`` and ``limit`` are refused above :data:`MAX_CONE_RADIUS_DEG`
        / :data:`MAX_CONE_LIMIT` rather than clamped — a caller asking for more
        than a field's worth of outlines should learn so.
        """
        if not 0.0 < radius_deg <= MAX_CONE_RADIUS_DEG:
            raise ValueError(f"radius_deg must be in (0, {MAX_CONE_RADIUS_DEG}], got {radius_deg}")
        if not 1 <= limit <= MAX_CONE_LIMIT:
            raise ValueError(f"limit must be in [1, {MAX_CONE_LIMIT}], got {limit}")
        cos_max = math.cos(math.radians(radius_deg))
        hits: list[CatalogObject] = []
        for obj in self.objects:
            cos_sep = cos_angular_separation(
                ra_deg, dec_deg, float(obj.ra_j2000_deg), float(obj.dec_j2000_deg)
            )
            if cos_sep >= cos_max:
                hits.append(obj)
        hits.sort(
            key=lambda o: -1.0 if o.size_major_arcmin is None else float(o.size_major_arcmin),
            reverse=True,
        )
        return tuple(hits[:limit])

    def __len__(self) -> int:
        return len(self.objects)


#: ``object_type`` values that count as "no real information yet", so a
#: later file's more specific type is still allowed to fill the field.
#: ``None`` (truly unset) always counts too, without being listed here.
_GENERIC_OBJECT_TYPES: Final = frozenset({ObjectType.OTHER})


def _fill_missing_fields(owner: CatalogObject, contributor: CatalogObject) -> CatalogObject | None:
    """The owner record with any field it left ``None`` filled in from
    ``contributor``, or ``None`` if ``contributor`` had nothing to add.

    Only the descriptive fields the merge contract covers are eligible —
    ``position_angle_deg``, ``size_major_arcmin``, ``size_minor_arcmin``,
    ``magnitude`` (with ``magnitude_band``, always together) and
    ``object_type`` when the owner's is unset or the generic ``OTHER``.
    Identity and geometry the owner already gave — designations, coordinates,
    ``common_name``, ``constellation``, ``source`` — are never touched, and a
    field the owner *did* set is never overridden, whatever the contributor says.
    """
    updates: dict[str, Any] = {}
    if owner.position_angle_deg is None and contributor.position_angle_deg is not None:
        updates["position_angle_deg"] = contributor.position_angle_deg
    if owner.size_major_arcmin is None and contributor.size_major_arcmin is not None:
        updates["size_major_arcmin"] = contributor.size_major_arcmin
    if owner.size_minor_arcmin is None and contributor.size_minor_arcmin is not None:
        updates["size_minor_arcmin"] = contributor.size_minor_arcmin
    if owner.magnitude is None and contributor.magnitude is not None:
        updates["magnitude"] = contributor.magnitude
        updates["magnitude_band"] = contributor.magnitude_band
    if contributor.object_type is not None and (
        owner.object_type is None or owner.object_type in _GENERIC_OBJECT_TYPES
    ):
        updates["object_type"] = contributor.object_type
    if not updates:
        return None
    return replace(owner, **updates)


def load_catalog(paths: Sequence[Path] | None = None) -> TargetCatalog:
    """Read the bundled catalogue files and merge them. The only I/O in this module.

    ``paths`` are merged in the order given (default: :func:`default_catalog_paths`):
    the first file to claim a designation owns the object — its coordinates,
    and every field it set, are final. A later record that shares any
    designation with an earlier one is the same object: it is not added
    again, its designations and common names that are still free become
    aliases of the owner, and any field the owner left ``None`` is filled
    from it (:func:`_fill_missing_fields`) — this is how ``M31`` ends up with
    OpenNGC's position angle despite the Messier core owning the record.
    ``CatalogObject.sources`` on the merged record lists every file that
    actually filled something, owner's own file first; a file that only
    re-claims a designation without adding data is not listed. A later
    record whose designations point at *two different* earlier objects is a
    data conflict and raises.
    """
    resolved_paths = default_catalog_paths() if paths is None else tuple(paths)
    if not resolved_paths:
        raise ValueError("load_catalog needs at least one catalogue file")

    objects: list[CatalogObject] = []
    contributed_sources: list[list[str]] = []
    file_sources: list[str] = []
    index_by_designation: dict[str, int] = {}
    index_by_common_name: dict[str, int] = {}

    for path in resolved_paths:
        source, records = _read_file(path)
        file_sources.append(source)
        for obj, common_names in records:
            keys: list[str] = []
            for designation in obj.designations:
                key = _canonical_designation(designation)
                if key is None:
                    raise ValueError(
                        f"{path}: {obj.name}: designation {designation!r} is not a "
                        "recognised catalogue designation shape"
                    )
                keys.append(key)
            owner_indices = {index_by_designation[k] for k in keys if k in index_by_designation}
            if len(owner_indices) > 1:
                names = sorted(objects[i].name for i in owner_indices)
                raise ValueError(
                    f"{path}: {obj.name} claims designations already owned by {names} — "
                    "two different earlier objects; the data files disagree about identity"
                )
            if owner_indices:
                idx = next(iter(owner_indices))
                filled = _fill_missing_fields(objects[idx], obj)
                if filled is not None:
                    objects[idx] = filled
                    if source not in contributed_sources[idx]:
                        contributed_sources[idx].append(source)
            else:
                idx = len(objects)
                objects.append(obj)
                contributed_sources.append([source])
            for key in keys:
                index_by_designation.setdefault(key, idx)
            for name in common_names:
                index_by_common_name.setdefault(_canonical_common_name(name), idx)

    final_objects = tuple(
        replace(obj, sources=tuple(contributed))
        for obj, contributed in zip(objects, contributed_sources, strict=True)
    )
    by_designation = {key: final_objects[idx] for key, idx in index_by_designation.items()}
    by_common_name = {key: final_objects[idx] for key, idx in index_by_common_name.items()}

    return TargetCatalog(
        objects=final_objects,
        sources=tuple(file_sources),
        _by_designation=by_designation,
        _by_common_name=by_common_name,
    )
