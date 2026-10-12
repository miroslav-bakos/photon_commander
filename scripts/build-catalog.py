#!/usr/bin/env python3
"""Derive the bundled deep-sky catalogue files from their upstream sources.

This is a **developer-time** tool: it runs once, by hand, and its outputs are
committed. The app never fetches — ``ssc.astro.catalog`` reads the committed
files only (CLAUDE.md invariant 3; DESIGN.md's offline-first rule). Fetch the
inputs first, then point this script at them::

    SHA=<OpenNGC commit>
    curl -L -o /tmp/NGC.csv      https://raw.githubusercontent.com/mattiaverga/OpenNGC/$SHA/database_files/NGC.csv
    curl -L -o /tmp/addendum.csv https://raw.githubusercontent.com/mattiaverga/OpenNGC/$SHA/database_files/addendum.csv
    curl -L -o /tmp/heasarc_hiiregion.tdat.gz \\
        https://heasarc.gsfc.nasa.gov/FTP/heasarc/dbase/tdat_files/heasarc_hiiregion.tdat.gz
    gunzip /tmp/heasarc_hiiregion.tdat.gz
    python3 scripts/build-catalog.py --openngc-commit $SHA \\
        /tmp/NGC.csv /tmp/addendum.csv /tmp/heasarc_hiiregion.tdat

Licensing is the reason there are two output files (THIRD_PARTY_NOTICES.md,
"Bundled star catalogue"):

* ``data/catalog/openngc.csv`` is a derivative of OpenNGC (CC BY-SA 4.0). The
  share-alike obligation attaches to that file, so it stays a separate data
  file under the same licence, never merged into ``objects.toml`` or into code.
* ``data/catalog/sharpless.csv`` is derived from the NASA HEASARC ``hiiregion``
  table (Sharpless 1959), a US-government-hosted table with no copyright
  notice — public domain.

Both outputs share one schema so ``ssc.astro.catalog`` has a single CSV reader;
the schema is documented in each file's header. Fields the source does not give
are left empty — never guessed (CLAUDE.md: "None means unknown; it never means
zero.").
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "catalog"

COLUMNS = [
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
]

SCHEMA_DOC = """\
# Schema (one CSV row per object; a header row names the columns; '#' lines
# are comments and are skipped by the reader):
#   designations        ';'-separated, first is the display name ("NGC 7000")
#   common_names        ';'-separated, may be empty; all are resolvable
#   object_type         an ssc.astro.catalog.ObjectType value, may be empty
#   ra_j2000_deg        degrees [0, 360)
#   dec_j2000_deg       degrees [-90, 90]
#   constellation       IAU 3-letter abbreviation, may be empty
#   size_major_arcmin   arcmin, may be empty
#   size_minor_arcmin   arcmin, may be empty (never assumed equal to major)
#   position_angle_deg  degrees, major axis, north through east, may be empty
#   magnitude           apparent total magnitude, may be empty
#   magnitude_band      'V' or 'B' — which band `magnitude` is; empty iff it is
"""

# --- OpenNGC ----------------------------------------------------------------

# OpenNGC "Type" -> ObjectType value. Every upstream code is mapped explicitly;
# an unknown one aborts the build rather than being lumped anywhere.
OPENNGC_TYPES: dict[str, str | None] = {
    "*": "star",
    "**": "double_star",
    "*Ass": "stellar_association",
    "OCl": "open_cluster",
    "GCl": "globular_cluster",
    "Cl+N": "cluster_with_nebula",
    "G": "galaxy",  # refined from the Hubble column below
    "GPair": "galaxy_pair",
    "GTrpl": "galaxy_triplet",
    "GGroup": "galaxy_group",
    "PN": "planetary_nebula",
    "HII": "hii_region",
    "DrkN": "dark_nebula",
    "EmN": "emission_nebula",
    "Neb": "diffuse_nebula",
    "RfN": "reflection_nebula",
    "SNR": "supernova_remnant",
    "Nova": "nova",
    "NonEx": "nonexistent",
    "Other": "other",
    # "Dup" rows are folded into their master object, see _openngc_rows.
}

# OpenNGC name prefixes -> the spaced designation prefix we emit. The main file
# uses NGC/IC only; the addendum adds a handful of other catalogues.
OPENNGC_PREFIXES: dict[str, str] = {
    "NGC": "NGC",
    "IC": "IC",
    "M": "M",
    "B": "B",  # Barnard
    "C": "C",  # Caldwell
    "Cl": "Cr",  # OpenNGC's "Cl399" is Collinder 399 (Brocchi's Cluster)
    "ESO": "ESO",
    "H": "H",  # Harvard
    "HCG": "HCG",
    "Mel": "Mel",
    "MWSC": "MWSC",
    "PGC": "PGC",
    "UGC": "UGC",
}

_OPENNGC_NAME_RE = re.compile(r"^([A-Za-z]+)0*(\d+)(?:-0*(\d+))?([A-Z]{0,2})$")


def _galaxy_subtype(hubble: str) -> str:
    """Refine OpenNGC's ``G`` using its Hubble column (de Vaucouleurs classes):
    E* elliptical; S0, S0-a and E-S0 lenticular; other S* spiral; I* irregular;
    empty or anything else stays the generic ``galaxy``."""
    if not hubble:
        return "galaxy"
    if hubble in ("S0", "S0-a", "E-S0"):
        return "galaxy_lenticular"
    if hubble.startswith("E"):
        return "galaxy_elliptical"
    if hubble.startswith("S"):
        return "galaxy_spiral"
    if hubble.startswith("I"):
        return "galaxy_irregular"
    return "galaxy"


def _openngc_designation(name: str) -> str:
    match = _OPENNGC_NAME_RE.match(name)
    if match is None:
        raise SystemExit(f"OpenNGC name {name!r} has an unexpected shape")
    prefix, number, second, suffix = match.groups()
    if prefix not in OPENNGC_PREFIXES:
        raise SystemExit(f"OpenNGC name {name!r}: unmapped catalogue prefix {prefix!r}")
    out = f"{OPENNGC_PREFIXES[prefix]} {int(number)}"
    if second is not None:
        out += f"-{int(second)}"
    return out + suffix


def _sexagesimal_ra_deg(text: str) -> float:
    h, m, s = text.split(":")
    return (int(h) + int(m) / 60.0 + float(s) / 3600.0) * 15.0


def _sexagesimal_dec_deg(text: str) -> float:
    sign = -1.0 if text.startswith("-") else 1.0
    d, m, s = text.lstrip("+-").split(":")
    return sign * (int(d) + int(m) / 60.0 + float(s) / 3600.0)


def _fmt(value: float | None, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _openngc_rows(paths: list[Path]) -> tuple[list[dict[str, str]], dict[str, int]]:
    raw: list[dict[str, str]] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            raw.extend(csv.DictReader(handle, delimiter=";"))
    by_name = {r["Name"]: r for r in raw}
    stats = {
        "rows": len(raw),
        "ned_components": 0,
        "dup_folded": 0,
        "dup_m_only": 0,
        "no_position": 0,
    }

    # Pass 1: fold "Dup" rows into the master they duplicate, as extra
    # designations. A Dup whose only master is a Messier number is dropped:
    # Messier identity is objects.toml's call (it is the higher-precedence file
    # and the M102/M101 question is contested).
    aliases: dict[str, list[str]] = {}
    for r in raw:
        if r["Type"] != "Dup" or " NED" in r["Name"]:
            continue
        master = None
        for col, prefix in (("NGC", "NGC"), ("IC", "IC")):
            if r[col]:
                # "3058 NED02": a NED sub-component; fold into the parent row.
                master = prefix + r[col].split(",")[0].split()[0]
                break
        if master is None:
            stats["dup_m_only"] += 1
            continue
        if master not in by_name or by_name[master]["Type"] == "Dup":
            raise SystemExit(f"OpenNGC {r['Name']}: Dup master {master!r} missing or itself a Dup")
        aliases.setdefault(master, []).append(_openngc_designation(r["Name"]))
        stats["dup_folded"] += 1

    out: list[dict[str, str]] = []
    for r in raw:
        name = r["Name"]
        if r["Type"] == "Dup":
            continue
        if " NED" in name:
            # NED sub-components of a pair/group; the base row (always present)
            # is the imaging target.
            stats["ned_components"] += 1
            continue
        if not r["RA"] or not r["Dec"]:
            stats["no_position"] += 1
            continue
        if r["Type"] not in OPENNGC_TYPES:
            raise SystemExit(f"OpenNGC {name}: unmapped Type {r['Type']!r}")
        object_type = OPENNGC_TYPES[r["Type"]]
        if object_type == "galaxy":
            object_type = _galaxy_subtype(r["Hubble"])

        designations = [_openngc_designation(name)]
        if r["M"]:
            designations.append(f"M {int(r['M'])}")
        designations.extend(aliases.get(name, ()))
        if name == "Cl399":
            designations.append("Cl 399")
        designations = list(dict.fromkeys(designations))  # addendum "M040" also has M=040

        magnitude: str
        band: str
        if r["V-Mag"]:
            magnitude, band = r["V-Mag"], "V"
        elif r["B-Mag"]:
            magnitude, band = r["B-Mag"], "B"
        else:
            magnitude, band = "", ""

        const = r["Const"]
        if const in ("Se1", "Se2"):
            const = "Ser"

        out.append(
            {
                "designations": ";".join(designations),
                "common_names": ";".join(
                    n.strip() for n in r["Common names"].split(",") if n.strip()
                ),
                "object_type": object_type,
                "ra_j2000_deg": _fmt(_sexagesimal_ra_deg(r["RA"]), 5),
                "dec_j2000_deg": _fmt(_sexagesimal_dec_deg(r["Dec"]), 5),
                "constellation": const,
                "size_major_arcmin": _fmt(float(r["MajAx"]) if r["MajAx"] else None, 2),
                "size_minor_arcmin": _fmt(float(r["MinAx"]) if r["MinAx"] else None, 2),
                "position_angle_deg": _fmt(float(r["PosAng"]) if r["PosAng"] else None, 1),
                "magnitude": _fmt(float(magnitude) if magnitude else None, 2),
                "magnitude_band": band,
            }
        )
    return out, stats


def _openngc_header(commit: str, today: str, stats: dict[str, int]) -> str:
    return f"""\
# SSC bundled deep-sky catalogue: NGC 1-7840 and IC 1-5386, plus OpenNGC's addendum.
#
# This file is a DERIVATIVE OF OpenNGC and is licensed CC BY-SA 4.0. It is kept
# as its own data file, separate from data/catalog/objects.toml (public-domain
# core) and from the application code, precisely so the share-alike obligation
# attaches to this file alone. See THIRD_PARTY_NOTICES.md, "Bundled star catalogue".
#
# source: OpenNGC by Mattia Verga, CC BY-SA 4.0
# license: CC-BY-SA-4.0
# license_url: https://creativecommons.org/licenses/by-sa/4.0/
# attribution: OpenNGC (https://github.com/mattiaverga/OpenNGC), Mattia Verga, licensed under CC BY-SA 4.0
# upstream: https://github.com/mattiaverga/OpenNGC
# upstream_commit: {commit}
# upstream_files: database_files/NGC.csv, database_files/addendum.csv
# fetched: {today}
# built_by: scripts/build-catalog.py
#
# Changes made to the upstream data (the "adaptation" CC BY-SA asks us to mark):
#   - designations are re-spaced ("NGC0224" -> "NGC 224"); "Cl399" is emitted as
#     "Cr 399" (Collinder) with "Cl 399" kept as an alias
#   - the {stats["dup_folded"]} "Dup" rows are folded into their master object as
#     extra designations; {stats["dup_m_only"]} Dup row(s) whose only master is a
#     Messier number are dropped (Messier identity is objects.toml's call)
#   - the {stats["ned_components"]} "NED01/02/.." sub-component rows are dropped; the
#     parent row is always present and is the imaging target
#   - {stats["no_position"]} row(s) with no position (nonexistent objects) are dropped
#   - "G" is refined into spiral/elliptical/lenticular/irregular from the
#     Hubble column (E*, S0|S0-a|E-S0, other S*, I*); no Hubble type -> "galaxy"
#   - `magnitude` is V-Mag when given, else B-Mag; `magnitude_band` says which
#   - RA/Dec are converted from sexagesimal to decimal degrees (J2000 unchanged);
#     Se1/Se2 (Serpens Caput/Cauda) are both written "Ser"
#   - all other upstream columns (proper motions, redshift, notes, ...) are omitted
#
{SCHEMA_DOC}"""


# --- Sharpless via HEASARC ----------------------------------------------------

_TDAT_FIELD_RE = re.compile(r"^field\[(\w+)\]")


def _sharpless_rows(path: Path) -> list[dict[str, str]]:
    fields: list[str] = []
    line_fields: list[str] | None = None
    rows: list[dict[str, str]] = []
    in_data = False
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not in_data:
                m = _TDAT_FIELD_RE.match(line)
                if m:
                    fields.append(m.group(1))
                elif line.startswith("line[1] = "):
                    line_fields = line.removeprefix("line[1] = ").split()
                elif line == "<DATA>":
                    in_data = True
                continue
            if line == "<END>":
                break
            values = line.split("|")
            if line_fields is None or len(values) < len(line_fields):
                raise SystemExit(f"hiiregion: malformed data line {line!r}")
            rows.append(dict(zip(line_fields, values, strict=False)))
    if len(rows) != 313:
        raise SystemExit(f"hiiregion: expected 313 rows, read {len(rows)}")

    out: list[dict[str, str]] = []
    for r in rows:
        m = re.fullmatch(r"S\s*0*(\d+)", r["name"])
        if m is None:
            raise SystemExit(f"hiiregion: unexpected name {r['name']!r}")
        number = int(m.group(1))
        diameter = float(r["diameter"]) if r["diameter"] else None
        # Form 1 = circular per the HEASARC field description, so the minor
        # axis equals the diameter by the source's own statement; forms 2 and 3
        # (elliptical, irregular) give no minor axis and none is invented.
        minor = diameter if (diameter is not None and r["form"] == "1") else None
        out.append(
            {
                "designations": f"Sh2-{number}",
                "common_names": "",
                "object_type": "hii_region",
                "ra_j2000_deg": _fmt(float(r["ra"]), 4),
                "dec_j2000_deg": _fmt(float(r["dec"]), 4),
                "constellation": "",
                "size_major_arcmin": _fmt(diameter, 1),
                "size_minor_arcmin": _fmt(minor, 1),
                "position_angle_deg": "",
                "magnitude": "",
                "magnitude_band": "",
            }
        )
    out.sort(key=lambda row: int(row["designations"][4:]))
    return out


def _sharpless_header(today: str, table_modified: str) -> str:
    return f"""\
# SSC bundled deep-sky catalogue: Sharpless (1959) H II regions, Sh2-1 to Sh2-313.
#
# Derived from the NASA HEASARC "hiiregion" table (Sharpless H II Regions), a
# US-government-hosted database table carrying no copyright notice, unlike
# HEASARC's ngc2000 table which is Sky Publishing copyright and was therefore
# NOT used. The underlying catalogue is Sharpless, S. 1959, ApJS 4, 257
# (bibcode 1959ApJS....4..257S), a table of measured positions and sizes.
#
# source: Sharpless (1959) H II regions, via NASA HEASARC hiiregion table (public domain)
# license: public-domain
# license_url: https://heasarc.gsfc.nasa.gov/docs/heasarc/policies.html
# attribution: Sharpless, S. 1959, ApJS 4, 257; table hosted by NASA HEASARC (https://heasarc.gsfc.nasa.gov/W3Browse/nebula-catalog/hiiregion.html)
# upstream: https://heasarc.gsfc.nasa.gov/FTP/heasarc/dbase/tdat_files/heasarc_hiiregion.tdat.gz
# upstream_table_modified: {table_modified}
# fetched: {today}
# built_by: scripts/build-catalog.py
#
# Changes made to the upstream data:
#   - names are written "Sh2-N" (HEASARC's "S N"; the page documents both forms)
#   - `size_major_arcmin` is the table's maximum angular diameter; the source
#     warns diameters may be uncertain by ~20% for diffuse objects
#   - `size_minor_arcmin` is set equal to the diameter only where the source's
#     form code is 1 (circular); forms 2/3 leave it empty
#   - only the J2000 `ra`/`dec` columns are kept; the 1900/1950/galactic
#     positions and the form/structure/brightness/stars codes are omitted
#   - the catalogue has no magnitudes, position angles or constellations; those
#     columns are empty, not guessed
#
{SCHEMA_DOC}"""


# --- main ---------------------------------------------------------------------


def _write(path: Path, header: str, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        handle.write(header)
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("openngc_ngc", type=Path, help="OpenNGC database_files/NGC.csv")
    parser.add_argument("openngc_addendum", type=Path, help="OpenNGC database_files/addendum.csv")
    parser.add_argument(
        "hiiregion_tdat", type=Path, help="HEASARC heasarc_hiiregion.tdat (gunzipped)"
    )
    parser.add_argument(
        "--openngc-commit", required=True, help="OpenNGC git commit the CSVs were fetched at"
    )
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    today = dt.datetime.now(tz=dt.UTC).date().isoformat()

    openngc, stats = _openngc_rows([args.openngc_ngc, args.openngc_addendum])
    _write(
        args.out_dir / "openngc.csv", _openngc_header(args.openngc_commit, today, stats), openngc
    )
    print(f"openngc.csv: {len(openngc)} objects  ({stats})")

    modified = "unknown"
    with args.hiiregion_tdat.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("# LAST MODIFIED:"):
                modified = line.split(":", 1)[1].strip()
                break
    sharpless = _sharpless_rows(args.hiiregion_tdat)
    _write(args.out_dir / "sharpless.csv", _sharpless_header(today, modified), sharpless)
    print(f"sharpless.csv: {len(sharpless)} objects")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
