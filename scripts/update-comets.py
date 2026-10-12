#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only

"""Refresh the bundled comet-orbit snapshot from NASA/JPL SBDB.

This is a maintainer action, never an application startup side effect. Runtime
search therefore remains useful at an offline observing site, while the file's
``fetched_at`` and each row's solution/element epoch make age visible.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

URL = "https://ssd-api.jpl.nasa.gov/sbdb_query.api"
FIELDS = (
    "spkid",
    "full_name",
    "pdes",
    "name",
    "prefix",
    "epoch",
    "e",
    "q",
    "tp",
    "i",
    "om",
    "w",
    "soln_date",
    "orbit_id",
    "condition_code",
    "data_arc",
    "n_obs_used",
    "two_body",
    "A1",
    "A2",
    "A3",
    "moid",
    "moid_jup",
)
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "comets" / "jpl-sbdb.json"


def main() -> None:
    query = urllib.parse.urlencode(
        {"fields": ",".join(FIELDS), "sb-kind": "c", "limit": "5000", "full-prec": "1"}
    )
    request = urllib.request.Request(
        f"{URL}?{query}",
        headers={"User-Agent": "Smart-Telescope-Commander comet snapshot updater"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)

    signature = payload.get("signature", {})
    if signature.get("version") != "1.0" or "NASA/JPL" not in signature.get("source", ""):
        raise RuntimeError(f"unexpected SBDB signature: {signature!r}")
    if payload.get("fields") != list(FIELDS):
        raise RuntimeError("SBDB returned a different field layout; update the parser first")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) < 3_000:
        raise RuntimeError(f"SBDB returned an implausible comet count: {len(rows or [])}")

    output = {
        "source_url": f"{URL}?{query}",
        "source": signature["source"],
        "api_version": signature["version"],
        "fetched_at": datetime.now(tz=UTC).isoformat(),
        "fields": list(FIELDS),
        "data": rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(rows)} comet orbit solutions)")


if __name__ == "__main__":
    main()
