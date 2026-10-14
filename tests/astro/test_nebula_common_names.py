"""Familiar-name curation must preserve scientific catalogue facts."""

import importlib.util
from pathlib import Path


def test_common_names_preserve_identifiers_coordinates_and_existing_aliases():
    path = Path(__file__).resolve().parents[2] / "scripts/build-nebula-catalogs.py"
    spec = importlib.util.spec_from_file_location("nebula_builder_names", path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    row = {
        "designations": "LDN 1235",
        "ra_j2000_deg": "333.7283",
        "dec_j2000_deg": "73.4161",
        "common_names": "Existing name",
    }
    result = builder.with_common_names(row)
    assert result["designations"] == row["designations"]
    assert result["ra_j2000_deg"] == row["ra_j2000_deg"]
    assert result["dec_j2000_deg"] == row["dec_j2000_deg"]
    assert result["common_names"].split(";") == [
        "Existing name",
        "Dark Shark Nebula",
        "Shark Nebula",
        "Dark Shark",
        "Shark",
    ]
    assert row["common_names"] == "Existing name"
    assert builder.with_common_names(result) == result
    assert (
        builder.with_common_names({"designations": "VdB 1", "common_names": "VDB 1a-d"})[
            "common_names"
        ]
        == "VDB 1a-d"
    )


def test_regenerated_catalogue_keeps_familiar_names(tmp_path):
    import csv
    import runpy

    path = Path(__file__).resolve().parents[2] / "scripts/build-nebula-catalogs.py"
    builder = runpy.run_path(str(path))
    upstream = tmp_path / "input.tdat"
    upstream.write_text("pinned test catalogue input")
    output = tmp_path / "ldn.csv"
    builder["write_catalog"](
        output,
        [{"designations": "LDN 1235", "ra_j2000_deg": "333.7283", "dec_j2000_deg": "73.4161"}],
        "fixture",
        upstream,
        "ldn",
        "fixture",
    )
    rows = list(
        csv.DictReader(line for line in output.read_text().splitlines() if not line.startswith("#"))
    )
    assert len(rows) == 1
    assert "Dark Shark Nebula" in rows[0]["common_names"].split(";")
    assert rows[0]["ra_j2000_deg"] == "333.7283"
