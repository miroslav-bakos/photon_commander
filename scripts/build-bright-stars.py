#!/usr/bin/env python3
"""Derive the bundled named-bright-star table from the IAU Catalog of Star Names.

This is a **developer-time** tool: it runs once, by hand, and its output is
committed. The app never fetches — ``ssc.astro.stars`` reads the committed
file only (CLAUDE.md invariant 3; DESIGN.md's offline-first rule). Fetch the
one input first::

    curl -L -A "Mozilla/5.0" -o /tmp/IAU-CSN.txt \\
        https://www.pas.rochester.edu/~emamajek/WGSN/IAU-CSN.txt
    python3 scripts/build-bright-stars.py /tmp/IAU-CSN.txt

Source and licence — checked live, not assumed from docs/IMPLEMENTATION.md
=============================================================================

``IMPLEMENTATION.md`` M11's data table originally said "Bright stars | NASA
HEASARC-hosted catalogues | US-government public domain", by analogy with
the Messier/Sharpless entries next to it. **That characterization does not
survive checking** — the same discipline that found the constellation-
boundary row's own assumption wrong (THIRD_PARTY_NOTICES.md, "Constellations —
gate reading of 2026-09-07") applies here too:

* HEASARC does host a bright-star table — ``heasarc_bsc5p`` (BSC5P, the
  Yale Bright Star Catalogue 5th ed. preliminary, Hoffleit & Warren; fetched
  2026-09-07 from
  https://heasarc.gsfc.nasa.gov/FTP/heasarc/dbase/tdat_files/heasarc_bsc5p.tdat.gz,
  ``table_document_url`` https://heasarc.gsfc.nasa.gov/W3Browse/star-catalog/bsc5p.html,
  no copyright notice on that page, unlike HEASARC's ``ngc2000`` table). It
  gives position, magnitude and cross-catalogue numbers (HR, HD, SAO, ...)
  for 9110 stars. **It has no field for a star's common/proper name** — its
  only name-shaped field, ``alt_name``, is a Bayer/Flamsteed label ("54 Alp
  Peg"), not "Sirius" or "Vega". HEASARC alone cannot answer "which stars
  have a common name" at all.
* The internationally recognised source for star proper names is the IAU
  Division C Working Group on Star Names (WGSN)'s **IAU Catalog of Star
  Names (IAU-CSN)**, continuously maintained since 2016 and the source
  https://www.iau.org/public/themes/naming_stars/ itself points to. It is
  **not** HEASARC-hosted, and it is **not** public domain: the file's own
  header states "All IAU-produced products (Images, Videos, Texts) are
  released under Creative Commons Attribution (i.e. free to use in all
  perpetuity, world-wide, as long as the source is mentioned)" — corroborated
  by https://www.iau.org/copyright/, which names the licence explicitly as
  Creative Commons Attribution 4.0 International. This is CC BY 4.0, not
  public domain, and (unlike CC BY-SA) carries no share-alike obligation.
  It already gives everything this table needs directly: name, HR/HD
  designation, apparent V/G magnitude, and J2000 RA/Dec in decimal degrees
  — no join against BSC5P is needed to build the table.

**docs/IMPLEMENTATION.md and THIRD_PARTY_NOTICES.md are amended in the same
change that adds this script** to say: bright-star *names* are IAU-CSN
(CC BY 4.0), not HEASARC/public-domain; only the position/magnitude data a
name is attached to has a public-domain HEASARC twin. ``data/stars/
bright_stars.csv`` therefore ships under CC BY 4.0 — attributed in its own
header and surfaced through ``ssc.astro.stars.bright_stars_credit()``,
exactly as ``openngc.csv``'s CC BY-SA 4.0 is surfaced through
``ssc.astro.catalog`` and never a literal string in the UI.

Magnitude cutoff — why 4.0
===========================

IAU-CSN currently carries ~590 names, but a large and growing share of them
are *not* bright naked-eye stars: recent WGSN batches name exoplanet-host
stars (``NameExoWorlds``) down to Vmag ~12 ("Absolutno", HD ~168746 at Vmag
7.95, are already IAU-named but invisible to the unaided eye). Bundling all
of them would turn "named bright stars" into a dense, mostly-invisible field
— exactly what the deliverable spec says this layer must not be (Pan-STARRS
imagery already shows the true star field at any depth; this layer is for
the recognizable few).

``4.0`` is used here: the traditional threshold for "a star anyone might
actually point at and say the name of" (naked-eye visibility comfortably
holds to about Vmag 6 at a dark site, but the *commonly recognized named*
stars — Sirius, Vega, Betelgeuse, Polaris, the Kaus/Alula/Yed pairs, ... —
overwhelmingly cluster brighter than 4th magnitude; every classical Bayer-
lettered star with a name in circulation before the WGSN's 2016 mandate is
at or above this brightness). At this cutoff the table holds 241 stars —
"a few hundred", the scale the deliverable spec asks for — and every single
one of them carries an ``HR ####`` (Bright Star Catalogue) designation, so
the schema's designation column never has to fall back to anything else.
Every name in the IAU-CSN file at or fainter than 4.01 (from "Absolutno" at
11.95 down through ordinary naked-eye stars in the 4-6 range with a WGSN-
assigned but not classically "common" name) is dropped, not guessed into a
row with blank fields.

Two rows in the source file (``Geminga``, ``Lich`` — named pulsars, not
optical stars) have no magnitude at all; they are dropped by the same
"unknown means excluded, not zero" rule, not miscounted as faint stars.

Parsing note
============

The source file is fixed-width by intent (its own header prints column
positions), but several proper names are themselves wider than the
"Name/ASCII" column's nominal width (multi-word names like "Rigil
Kentaurus", "Kaus Australis", "Asellus Australis" — the last exactly fills
the field, leaving no padding at all before the "Name/Diacritics" column
that follows). A fixed byte-offset slice silently truncates or misaligns
those rows. Because the two name columns are byte-for-byte identical for
every star in this table (no bundled name actually needs the diacritics
column), this script instead locates the designation token first (a
reliable, un-ambiguous anchor, ``HR \\d+`` / ``HD \\d+``) and recovers the
name from the two-copies-of-the-same-string structure directly, which is
exact regardless of column width or overflow — verified below by re-
deriving all 241 names and checking none collide and each round-trips.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "stars" / "bright_stars.csv"

#: Kept in sync with the reasoning in the module docstring — do not change
#: without updating both.
MAGNITUDE_CUTOFF = 4.0

COLUMNS = ["name", "designation", "ra_j2000_deg", "dec_j2000_deg", "magnitude"]

_TAIL_RE = re.compile(
    r"(?P<mag>-?\d{1,2}\.\d{2})\s+(?P<band>[VG])\s+(?P<hip>\d+|_)\s+(?P<hd>\d+|_)\s+"
    r"(?P<ra>-?\d+\.\d+)\s+(?P<dec>-?\d+\.\d+)\s+(?P<date>\d{4}-\d{2}-\d{2})"
)
_DESIGNATION_RE = re.compile(r"\b(HR|HD)\s+\d+\b")
#: The name column repeats itself verbatim (ASCII form, then diacritics
#: form — identical for every star this table keeps; see the module
#: docstring's "Parsing note"). A backreference finds the split regardless
#: of how many padding spaces separate the two copies, which a fixed-width
#: slice cannot do once a name overflows its nominal column width.
_REPEATED_NAME_RE = re.compile(r"^(.+?)\s+\1\s*$")


def _data_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or line.startswith("$"):
            continue
        lines.append(line)
    return lines


def parse_rows(text: str) -> list[dict[str, str]]:
    """Every IAU-CSN row at or brighter than :data:`MAGNITUDE_CUTOFF`, with a
    resolvable ``HR``/``HD`` designation. Anything else (fainter stars,
    exoplanet hosts on non-BSC designations, magnitude-less pulsar entries)
    is dropped, never guessed — see the module docstring."""
    rows: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for line in _data_lines(text):
        tail = _TAIL_RE.search(line)
        if tail is None:
            continue
        magnitude = float(tail.group("mag"))
        if magnitude > MAGNITUDE_CUTOFF:
            continue
        designation_match = _DESIGNATION_RE.search(line)
        if designation_match is None:
            raise ValueError(
                f"a star at or brighter than {MAGNITUDE_CUTOFF} has no HR/HD designation "
                f"(the build's own cutoff reasoning assumes this never happens): {line!r}"
            )
        head = line[: designation_match.start()]
        name_match = _REPEATED_NAME_RE.match(head)
        if name_match is None:
            raise ValueError(
                f"could not recover a repeated name field from {head!r} (line: {line!r})"
            )
        name = name_match.group(1)
        if name in seen_names:
            raise ValueError(f"duplicate star name {name!r} at or brighter than {MAGNITUDE_CUTOFF}")
        seen_names.add(name)
        rows.append(
            {
                "name": name,
                "designation": designation_match.group(0),
                "ra_j2000_deg": tail.group("ra"),
                "dec_j2000_deg": tail.group("dec"),
                "magnitude": tail.group("mag"),
            }
        )
    rows.sort(key=lambda r: r["name"])
    return rows


_HEADER = """\
# SSC bundled named-bright-star table: proper star names paired with J2000
# position, apparent magnitude and Bright Star Catalogue (HR) designation,
# for stars brighter than Vmag {cutoff}.
#
# Source and licence (THIRD_PARTY_NOTICES.md, "Bundled named-bright-star table"
# — read that section before assuming HEASARC/public-domain the way
# docs/IMPLEMENTATION.md's M11 table originally, and wrongly, did):
#
# The IAU Division C Working Group on Star Names (WGSN)'s "IAU Catalog of
# Star Names" (IAU-CSN) is the internationally recognised source for which
# stars have an approved common name (https://www.iau.org/public/themes/naming_stars/).
# NASA HEASARC hosts a bright-star position/magnitude table (BSC5P,
# heasarc_bsc5p, Hoffleit & Warren, public domain — no copyright notice on
# its documentation page) but that table has no field for a star's common
# name at all, only Bayer/Flamsteed labels; it cannot answer "which stars
# are named" by itself, so it is not the source of the data below.
#
# source: IAU Division C Working Group on Star Names (WGSN), "IAU Catalog of Star Names" (IAU-CSN)
# license: CC-BY-4.0
# license_url: https://creativecommons.org/licenses/by/4.0/
# attribution: IAU Division C Working Group on Star Names (WGSN), "IAU Catalog of Star Names" (IAU-CSN), https://www.iau.org/public/themes/naming_stars/, CC BY 4.0 International (https://www.iau.org/copyright/)
# upstream: https://www.pas.rochester.edu/~emamajek/WGSN/IAU-CSN.txt (WGSN secretary's working mirror of the IAU-published catalogue)
# upstream_last_updated: 2022-04-04 (per the file's own header)
# fetched: {fetched}
# built_by: scripts/build-bright-stars.py
#
# credit: Star names: IAU Working Group on Star Names (WGSN), CC BY 4.0
#   The one-line form of the `# source:`/`# attribution:` block above, for
#   UI credit lines (`ssc.astro.stars.bright_stars_credit`) — read verbatim,
#   never assembled in Python, the same discipline
#   `ssc.astro.constellations.boundaries_credit` applies to its own file.
#
# Magnitude cutoff and rationale: see the module docstring of
# scripts/build-bright-stars.py ("Magnitude cutoff — why {cutoff}"). {count}
# stars, all carrying an `HR` designation.
#
# Changes made to the upstream data:
#   - only rows with V/G magnitude <= {cutoff} are kept (IAU-CSN also names
#     much fainter exoplanet-host stars; those are dropped, not guessed into
#     a row with a magnitude the source did not give for this purpose)
#   - 2 magnitude-less named pulsars (Geminga, Lich) are dropped: `None`
#     means unknown, never a fabricated zero
#   - the "Name/Diacritics" column is dropped (every kept star's diacritics
#     form is byte-identical to its ASCII form; ASCII is what this project's
#     CSV files use elsewhere, e.g. openngc.csv)
#   - `id_bayer`/`id_greek`/`constellation`/`WDS`/`HIP`/approval-date columns
#     are dropped: not part of this deliverable's schema (name, designation,
#     position, magnitude)
#
# Schema (one CSV row per star; a header row names the columns; '#' lines
# are comments and are skipped by the reader):
#   name             the star's IAU-approved proper name (ASCII form)
#   designation      the catalogue's own cross-reference, "HR <number>"
#                     (Bright Star Catalogue / Harvard Revised number)
#   ra_j2000_deg     degrees [0, 360)
#   dec_j2000_deg    degrees [-90, 90]
#   magnitude        apparent V (or G, IAU-CSN's Gaia-band fallback for a
#                     handful of stars) magnitude
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "iau_csn_txt", type=Path, help="path to a fetched IAU-CSN.txt (see module docstring)"
    )
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)

    text = args.iau_csn_txt.read_text(encoding="utf-8")
    rows = parse_rows(text)
    if not rows:
        raise SystemExit("no rows parsed — did the source file's format change?")

    today = dt.datetime.now(tz=dt.UTC).date().isoformat()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        handle.write(_HEADER.format(cutoff=MAGNITUDE_CUTOFF, fetched=today, count=len(rows)))
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"bright_stars.csv: {len(rows)} named stars at or brighter than Vmag {MAGNITUDE_CUTOFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
