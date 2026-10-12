#!/usr/bin/env python3
"""Derive the bundled IAU constellation boundary table from Delporte (1930).

This is a **developer-time** tool: it runs once, by hand, and its output is
committed. The app never fetches — ``ssc.astro.constellations`` reads the
committed file only (CLAUDE.md invariant 3), the same way
``ssc.astro.catalog`` reads its bundled files. Fetch the one input first::

    curl -L -A "Mozilla/5.0" -o /tmp/bound_ed.dat \\
        https://watcheroftheskies.net/constellations/bound_ed.dat
    python3 scripts/build-constellation-boundaries.py /tmp/bound_ed.dat

Provenance and public-domain reasoning (THIRD_PARTY_NOTICES.md, "Constellations
— gate reading of 2026-09-07"):

Eugène Delporte drew the 88 official constellation boundaries on behalf of
the IAU and published them, with tables and 28 charts, as *Délimitation
Scientifique des Constellations* (Cambridge University Press, 1930).
Delporte died 1955-10-19; under EU rule-of-the-shorter-term / life+70, that
1930 work entered the public domain on 2026-01-01.

The two ready machine-readable transcriptions of it — VizieR VI/49
(Davenhall & Leggett) and VI/42 (Roman 1987, what astropy bundles) — carry
no licence statement of their own from CDS, so the compliance gate ruled
them not clearable and **this script does not read either one, even to
cross-check**. Instead it reads a third, independent transcription: Pierre
Barbier's "Constellation Boundaries" page[1] states plainly that its
``bound_ed.dat`` was "compiled ... directly from Delporte's book" — in the
book's own B1875.0 sexagesimal precision, in the book's own vertex order,
*without* VI/49's decimal rounding or the extra plotting vertices VI/49's
own ReadMe admits adding. Barbier licenses his site's original content,
data files included, CC BY-SA 4.0[2] (Last-Modified on ``bound_ed.dat``
itself: 2016-01-01, unchanged for a decade as of this fetch). This file is
kept separate from ``objects.toml``/other public-domain-only data, exactly
as ``data/catalog/openngc.csv`` is kept separate from the Messier core, so
CC BY-SA's share-alike obligation attaches to this file alone — the same
conservative call, even though the underlying vertex coordinates are
historical measurements a court would likely find below the threshold of
copyrightable expression on their own (Feist v. Rural has no EU equivalent
holding cited here, but this project treats compiled astronomical tables
conservatively either way; see openngc.csv's own header for the identical
reasoning).

[1] https://watcheroftheskies.net/constellations/boundaries.html
[2] https://watcheroftheskies.net/terms.html ("All the original content on
    this site is licensed under a Creative Commons Attribution-ShareAlike
    4.0 International License." — Pierre Barbier, (c) 2012)

The 88 full constellation names paired with their IAU 3-letter abbreviation
are the nomenclature the IAU fixed in 1922 (H.N. Russell's report) and that
Delporte's own tables use; this exact name/abbreviation pairing is
reproduced identically across every astronomical source there is, which is
precisely what keeps a plain factual list like this below the threshold of
copyrightable expression — it is hand-maintained below, not read from
VI/49's ``constell.txt``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "constellations"

COLUMNS: Final = ("abbreviation", "name", "ring", "ra_b1875_deg", "dec_b1875_deg")

#: Serpens is split into two disjoint boundary pieces in Delporte's own
#: tables (Caput and Cauda either side of Ophiuchus); every other source
#: file line uses its IAU abbreviation directly.
_MERGE_ABBREVIATIONS: Final[dict[str, str]] = {"SER1": "SER", "SER2": "SER"}

#: The 88 IAU constellation names, keyed by IAU 3-letter abbreviation —
#: standard nomenclature (H.N. Russell's 1922 IAU report; Delporte's own
#: 1930 tables use the same pairing), not read from any VizieR file.
IAU_NAMES: Final[dict[str, str]] = {
    "AND": "Andromeda",
    "ANT": "Antlia",
    "APS": "Apus",
    "AQL": "Aquila",
    "AQR": "Aquarius",
    "ARA": "Ara",
    "ARI": "Aries",
    "AUR": "Auriga",
    "BOO": "Boötes",
    "CAE": "Caelum",
    "CAM": "Camelopardalis",
    "CAP": "Capricornus",
    "CAR": "Carina",
    "CAS": "Cassiopeia",
    "CEN": "Centaurus",
    "CEP": "Cepheus",
    "CET": "Cetus",
    "CHA": "Chamaeleon",
    "CIR": "Circinus",
    "CMA": "Canis Major",
    "CMI": "Canis Minor",
    "CNC": "Cancer",
    "COL": "Columba",
    "COM": "Coma Berenices",
    "CRA": "Corona Australis",
    "CRB": "Corona Borealis",
    "CRT": "Crater",
    "CRU": "Crux",
    "CRV": "Corvus",
    "CVN": "Canes Venatici",
    "CYG": "Cygnus",
    "DEL": "Delphinus",
    "DOR": "Dorado",
    "DRA": "Draco",
    "EQU": "Equuleus",
    "ERI": "Eridanus",
    "FOR": "Fornax",
    "GEM": "Gemini",
    "GRU": "Grus",
    "HER": "Hercules",
    "HOR": "Horologium",
    "HYA": "Hydra",
    "HYI": "Hydrus",
    "IND": "Indus",
    "LAC": "Lacerta",
    "LEO": "Leo",
    "LEP": "Lepus",
    "LIB": "Libra",
    "LMI": "Leo Minor",
    "LUP": "Lupus",
    "LYN": "Lynx",
    "LYR": "Lyra",
    "MEN": "Mensa",
    "MIC": "Microscopium",
    "MON": "Monoceros",
    "MUS": "Musca",
    "NOR": "Norma",
    "OCT": "Octans",
    "OPH": "Ophiuchus",
    "ORI": "Orion",
    "PAV": "Pavo",
    "PEG": "Pegasus",
    "PER": "Perseus",
    "PHE": "Phoenix",
    "PIC": "Pictor",
    "PSA": "Piscis Austrinus",
    "PSC": "Pisces",
    "PUP": "Puppis",
    "PYX": "Pyxis",
    "RET": "Reticulum",
    "SCL": "Sculptor",
    "SCO": "Scorpius",
    "SCT": "Scutum",
    "SER": "Serpens",
    "SEX": "Sextans",
    "SGE": "Sagitta",
    "SGR": "Sagittarius",
    "TAU": "Taurus",
    "TEL": "Telescopium",
    "TRA": "Triangulum Australe",
    "TRI": "Triangulum",
    "TUC": "Tucana",
    "UMA": "Ursa Major",
    "UMI": "Ursa Minor",
    "VEL": "Vela",
    "VIR": "Virgo",
    "VOL": "Volans",
    "VUL": "Vulpecula",
}


def _parse_ra_h(text: str) -> float:
    """``"HH:MM:SS"`` right ascension, B1875, to decimal hours."""
    h, m, s = text.split(":")
    return int(h) + int(m) / 60.0 + int(s) / 3600.0


def _parse_dec_deg(text: str) -> float:
    """``"+DD:MM:SS"`` / ``"-DD:MM:SS"`` declination, B1875, to decimal degrees."""
    sign = 1.0 if text[0] == "+" else -1.0
    d, m, s = text[1:].split(":")
    return sign * (int(d) + int(m) / 60.0 + int(s) / 3600.0)


def _short_delta_deg(ra1: float, ra2: float) -> float:
    """Signed shortest angular step from ``ra1`` to ``ra2`` in degrees,
    ``(-180, 180]`` — the direction Delporte's own short boundary edges
    always take; used only to validate axis-alignment below, never to
    reshape the data actually written."""
    delta = (ra2 - ra1) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return delta


class _SourceFormatError(ValueError):
    """The upstream file's shape does not match what this script expects.
    Raised instead of silently skipping or guessing a row (CLAUDE.md
    invariant 7)."""


def _read_vertices(path: Path) -> list[tuple[str, float, float]]:
    """``bound_ed.dat``'s one-vertex-per-line records, in file order:
    ``(abbreviation, ra_b1875_deg, dec_b1875_deg)``."""
    rows: list[tuple[str, float, float]] = []
    with path.open(encoding="ascii") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) != 3:
                raise _SourceFormatError(
                    f"{path}:{line_number}: expected 'RA DEC CON', got {line!r}"
                )
            ra_text, dec_text, abbreviation = parts
            ra_deg = _parse_ra_h(ra_text) * 15.0
            dec_deg = _parse_dec_deg(dec_text)
            rows.append((abbreviation, ra_deg, dec_deg))
    if not rows:
        raise _SourceFormatError(f"{path}: no vertex rows found")
    return rows


def _group_into_rings(
    rows: list[tuple[str, float, float]],
) -> dict[str, list[list[tuple[float, float]]]]:
    """Group the flat vertex list into one ring per contiguous same-
    abbreviation run (Delporte's own tables list one constellation's
    boundary at a time, so a run's start is exactly where the previous
    constellation's ring closes), merging Serpens' two runs under "SER"
    as two separate rings rather than one.

    Fails loudly if a source abbreviation's vertices are not contiguous —
    that would mean this script's "one run = one ring" assumption about the
    source file's layout no longer holds, and silently concatenating two
    unrelated runs would draw a wrong polygon.
    """
    rings: dict[str, list[list[tuple[float, float]]]] = {}
    seen_abbrevs: set[str] = set()
    current_raw: str | None = None
    current_ring: list[tuple[float, float]] = []
    for raw_abbrev, ra_deg, dec_deg in rows:
        if raw_abbrev != current_raw:
            if current_raw is not None:
                _finish_ring(rings, current_raw, current_ring)
            if raw_abbrev in seen_abbrevs:
                raise _SourceFormatError(
                    f"{raw_abbrev}'s vertices are not contiguous in the source file"
                )
            seen_abbrevs.add(raw_abbrev)
            current_raw = raw_abbrev
            current_ring = []
        current_ring.append((ra_deg, dec_deg))
    if current_raw is not None:
        _finish_ring(rings, current_raw, current_ring)
    return rings


def _finish_ring(
    rings: dict[str, list[list[tuple[float, float]]]],
    raw_abbrev: str,
    ring: list[tuple[float, float]],
) -> None:
    abbreviation = _MERGE_ABBREVIATIONS.get(raw_abbrev, raw_abbrev)
    rings.setdefault(abbreviation, []).append(ring)


def _validate_ring(abbreviation: str, ring_index: int, ring: list[tuple[float, float]]) -> None:
    """Every edge — including the implied closing edge — must be a segment
    of a meridian (constant RA) or a parallel (constant Dec), exactly as
    Delporte's tables define a boundary. A ring failing this means either a
    parsing bug or a source row out of place; either way this must not
    silently ship a wrong-shaped polygon."""
    n = len(ring)
    if n < 3:
        raise _SourceFormatError(f"{abbreviation} ring {ring_index}: only {n} vertices")
    for i in range(n):
        ra1, dec1 = ring[i]
        ra2, dec2 = ring[(i + 1) % n]
        same_ra = ra1 == ra2
        same_dec = dec1 == dec2
        if not (same_ra or same_dec):
            raise _SourceFormatError(
                f"{abbreviation} ring {ring_index}: edge {ring[i]} -> {ring[(i + 1) % n]} "
                "is neither constant-RA nor constant-Dec"
            )
        if same_ra and not same_dec:
            continue
        if same_dec and not same_ra:
            span = abs(_short_delta_deg(ra1, ra2))
            if span > 180.0 or span == 0.0:
                raise _SourceFormatError(
                    f"{abbreviation} ring {ring_index}: parallel edge {ring[i]} -> "
                    f"{ring[(i + 1) % n]} has an ambiguous or zero RA span ({span} deg)"
                )


def build_rows(source_path: Path) -> list[dict[str, str]]:
    vertex_rows = _read_vertices(source_path)
    rings_by_abbrev = _group_into_rings(vertex_rows)

    missing = set(IAU_NAMES) - set(rings_by_abbrev)
    extra = set(rings_by_abbrev) - set(IAU_NAMES)
    if missing:
        raise _SourceFormatError(f"source file is missing constellations: {sorted(missing)}")
    if extra:
        raise _SourceFormatError(f"source file has unrecognised abbreviations: {sorted(extra)}")

    out_rows: list[dict[str, str]] = []
    for abbreviation in sorted(rings_by_abbrev):
        name = IAU_NAMES[abbreviation]
        for ring_index, ring in enumerate(rings_by_abbrev[abbreviation]):
            _validate_ring(abbreviation, ring_index, ring)
            for ra_deg, dec_deg in ring:
                out_rows.append(
                    {
                        "abbreviation": abbreviation,
                        "name": name,
                        "ring": str(ring_index),
                        "ra_b1875_deg": repr(ra_deg),
                        "dec_b1875_deg": repr(dec_deg),
                    }
                )
    return out_rows


_HEADER: Final = """\
# SSC bundled constellation boundary data: the 88 IAU constellation regions
# defined by Eugène Delporte, "Délimitation Scientifique des Constellations"
# (Cambridge University Press, on behalf of the IAU, 1930).
#
# Public-domain reasoning (THIRD_PARTY_NOTICES.md, "Constellations — gate
# reading of 2026-09-07"): Delporte died 1955-10-19; under EU rule of the
# shorter term / life+70, his original 1930 published work entered the
# public domain on 2026-01-01. The two ready machine-readable
# transcriptions of it, VizieR VI/49 (Davenhall & Leggett) and VI/42
# (Roman 1987, bundled by astropy), carry no licence statement of their own
# from CDS and are explicitly NOT used here, even to cross-check — see the
# gate reading. This file is instead derived from a third, independent
# transcription: Pierre Barbier's "Constellation Boundaries" page states it
# was compiled directly from Delporte's own printed tables (sexagesimal,
# B1875.0, book vertex order, no rounding or added plotting vertices — the
# opposite of what VI/49's own ReadMe says it did), and is licensed by its
# author under CC BY-SA 4.0. Kept in its own data file, like openngc.csv,
# so the share-alike obligation attaches to this file alone (see that
# file's header for the identical reasoning), even though the underlying
# vertex coordinates are historical measurements a court would likely find
# below the threshold of copyrightable expression on their own — this is
# the conservative reading, matching the NGC/IC precedent's rigor.
#
# source: Pierre Barbier, "Constellation Boundaries" (bound_ed.dat), CC BY-SA 4.0,
#   compiled directly from E. Delporte, Délimitation Scientifique des
#   Constellations, Cambridge University Press, 1930 (public domain: EU
#   life+70, Delporte d. 1955-10-19)
# license: CC-BY-SA-4.0
# license_url: https://creativecommons.org/licenses/by-sa/4.0/
# attribution: Pierre Barbier, https://watcheroftheskies.net/constellations/boundaries.html (CC BY-SA 4.0), transcribing E. Delporte (1930)
# upstream: https://watcheroftheskies.net/constellations/bound_ed.dat
# upstream_last_modified: 2016-01-01 (HTTP Last-Modified; unchanged for a decade as of fetch)
# fetched: {fetched}
# built_by: scripts/build-constellation-boundaries.py
#
# NOT sourced from, and not cross-checked against: VizieR VI/49 or VI/42
# (THIRD_PARTY_NOTICES.md 2026-09-07 gate: no licence statement of their own).
#
# Schema (one CSV row per boundary vertex; '#' lines are comments, skipped
# by the reader):
#   abbreviation      IAU 3-letter constellation abbreviation (e.g. "UMI")
#   name              full constellation name (e.g. "Ursa Minor")
#   ring              0-indexed polygon number within this abbreviation; 0
#                     for every constellation except Serpens ("SER"), whose
#                     boundary is two disjoint pieces (Caput, ring 0; Cauda,
#                     ring 1) sharing one abbreviation, per Delporte.
#   ra_b1875_deg      right ascension, degrees [0, 360), EQUINOX B1875.0 —
#                     the epoch Delporte defined these boundaries in. Not
#                     J2000: ssc.astro.constellations precesses the *query*
#                     point to B1875 instead of precessing ~1500 boundary
#                     vertices, so every edge here stays the exact
#                     axis-aligned constant-RA/constant-Dec line Delporte
#                     drew, with no interpolation or curve-approximation
#                     error at all.
#   dec_b1875_deg     declination, degrees [-90, 90], EQUINOX B1875.0
#
# Vertices are listed in ring order; the closing edge (last vertex -> first)
# is implied, never duplicated. Consecutive vertices differ in only one of
# RA or Dec — every edge is a segment of a meridian or a parallel, exactly
# as Delporte's tables define the boundaries, never a straight-line chord
# between two arbitrary points.
#
# Full names are the standard IAU constellation nomenclature fixed in 1922
# (H.N. Russell, on behalf of the IAU) and Delporte's own 1930 tables;
# reproduced identically across every authoritative source and, as a
# closed, universally-agreed list of 88 names and abbreviations, below the
# threshold of copyrightable expression — not sourced from VI/49's
# constell.txt.
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "bound_ed_dat", type=Path, help="watcheroftheskies.net/constellations/bound_ed.dat"
    )
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    rows = build_rows(args.bound_ed_dat)
    abbreviations = {row["abbreviation"] for row in rows}
    assert abbreviations == set(IAU_NAMES), "build_rows already checked this; re-asserting"

    today = dt.datetime.now(tz=dt.UTC).date().isoformat()
    out_path = args.out_dir / "boundaries.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(_HEADER.format(fetched=today))
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    ring_count = len({(r["abbreviation"], r["ring"]) for r in rows})
    print(
        f"boundaries.csv: {len(abbreviations)} constellations, {ring_count} rings, {len(rows)} vertices"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
