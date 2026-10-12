"""M3's acceptance gate: ssc.astro.events against an independent almanac.

Unit tests only prove the solver is self-consistent. This file proves it agrees
with the U.S. Naval Observatory and with IMCCE across 4 sites (one at 69.6 N,
inside the Arctic Circle) x 5 dates (both solstices, both EU DST transitions,
and the September equinox) — 20 rows, 240 phenomena.

The reference values live in ``almanac_reference.json`` with the exact query
URLs, resolutions and horizon conventions of the two services that produced
them. Nothing in that file was computed by SSC.

Run ``pytest -s tests/astro/test_almanac_validation.py`` to see the worst-case
deviation table; it is also embedded in the failure message of
``test_worst_case_deviations_are_within_tolerance``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from ssc.astro.events import Ephemeris, EventTime, Outcome, lunar_events, solar_events
from tests.astro.conftest import REFERENCE_PATH, reference_time, site_of, utc_day

SOLAR_PHENOMENA = (
    "astronomical_dawn",
    "nautical_dawn",
    "civil_dawn",
    "sunrise",
    "solar_transit",
    "sunset",
    "civil_dusk",
    "nautical_dusk",
    "astronomical_dusk",
)
LUNAR_PHENOMENA = ("moonrise", "moon_transit", "moonset")

TOLERANCE_KIND = {
    "astronomical_dawn": "twilight",
    "nautical_dawn": "twilight",
    "civil_dawn": "twilight",
    "civil_dusk": "twilight",
    "nautical_dusk": "twilight",
    "astronomical_dusk": "twilight",
    "solar_transit": "transit",
    "sunrise": "rise_set",
    "sunset": "rise_set",
    "moonrise": "moon",
    "moonset": "moon",
    "moon_transit": "moon",
}

with REFERENCE_PATH.open("rb") as _handle:
    _REFERENCE: dict[str, Any] = json.load(_handle)

#: Filled by the parametrised tests, reported by the last test in the file.
DEVIATIONS: list[tuple[str, float, str, str, str, str]] = []


def _cases(phenomena: tuple[str, ...]) -> list[tuple[dict[str, Any], str]]:
    return [(row, phenomenon) for row in _REFERENCE["rows"] for phenomenon in phenomena]


def _ident(case: tuple[dict[str, Any], str]) -> str:
    row, phenomenon = case
    return f"{row['site']}-{row['date_utc']}-{phenomenon}"


_SOLVED: dict[tuple[str, str, bool], dict[str, list[EventTime]]] = {}


def _computed(
    ephemeris: Ephemeris, almanac: dict[str, Any], row: dict[str, Any], solar: bool
) -> dict[str, list[EventTime]]:
    """One solve per (site, date, body), shared by that row's phenomena."""
    key = (row["site"], row["date_utc"], solar)
    cached = _SOLVED.get(key)
    if cached is not None:
        return cached
    site = site_of(almanac, row["site"])
    start, end = utc_day(row["date_utc"])
    solver = solar_events if solar else lunar_events
    by_name: dict[str, list[EventTime]] = {}
    for event in solver(ephemeris, site, start, end):
        by_name.setdefault(str(event.event), []).append(event)
    _SOLVED[key] = by_name
    return by_name


def _rounded_minute(moment: datetime) -> str:
    """The reading a minute-resolution almanac would print for this instant."""
    seconds = moment.second + moment.microsecond / 1e6
    minutes = moment.hour * 60 + moment.minute + (1 if seconds >= 30.0 else 0)
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _check(
    ephemeris: Ephemeris,
    almanac: dict[str, Any],
    row: dict[str, Any],
    phenomenon: str,
    *,
    solar: bool,
) -> None:
    expected = row["expected"][phenomenon]
    found = _computed(ephemeris, almanac, row, solar).get(phenomenon, [])
    label = f"{row['site']} {row['date_utc']} {phenomenon}"

    if expected["utc"] is None:
        # An absent phenomenon must be *explained*, and explained the way the
        # almanac explains it — never returned as a bare None.
        assert found, f"{label}: no result at all; expected outcome {expected['outcome']}"
        assert found[0].when is None, (
            f"{label}: the almanac says this does not occur ({expected['outcome']}), "
            f"SSC produced {found[0].when}"
        )
        assert found[0].outcome.value == expected["outcome"], (
            f"{label}: expected outcome {expected['outcome']}, got {found[0].outcome.value}"
        )
        return

    assert found, f"{label}: expected {expected['utc']} ({expected['source']}), got nothing"
    occurring = [event for event in found if event.outcome is Outcome.OCCURS]
    assert occurring, (
        f"{label}: expected {expected['utc']} ({expected['source']}), "
        f"got outcome {found[0].outcome.value}"
    )

    target = reference_time(row["date_utc"], expected["utc"])
    nearest = min((abs((e.when - target).total_seconds()), i) for i, e in enumerate(occurring))
    deviation, index = nearest
    best = occurring[index]
    assert best.when is not None
    tolerance = _REFERENCE["tolerances_seconds"][TOLERANCE_KIND[phenomenon]]
    DEVIATIONS.append(
        (
            phenomenon,
            deviation,
            expected["source"],
            row["site"],
            row["date_utc"],
            f"ref {expected['utc']} vs {best.when.time().isoformat(timespec='milliseconds')}",
        )
    )

    if len(expected["utc"]) == len("HH:MM"):
        # A minute-resolution reference: the real statement is that SSC's answer
        # prints as the same minute the almanac printed.
        assert _rounded_minute(best.when) == expected["utc"], (
            f"{label}: SSC {best.when.isoformat()} rounds to {_rounded_minute(best.when)}, "
            f"the almanac printed {expected['utc']}"
        )
    assert deviation <= tolerance, (
        f"{label}: {deviation:.1f} s from the {expected['source']} value {expected['utc']} "
        f"(tolerance {tolerance} s)"
    )


@pytest.mark.parametrize("case", _cases(SOLAR_PHENOMENA), ids=_ident)
def test_solar_event_matches_almanac(
    ephemeris: Ephemeris, almanac: dict[str, Any], case: tuple[dict[str, Any], str]
) -> None:
    row, phenomenon = case
    _check(ephemeris, almanac, row, phenomenon, solar=True)


@pytest.mark.parametrize("case", _cases(LUNAR_PHENOMENA), ids=_ident)
def test_lunar_event_matches_almanac(
    ephemeris: Ephemeris, almanac: dict[str, Any], case: tuple[dict[str, Any], str]
) -> None:
    row, phenomenon = case
    _check(ephemeris, almanac, row, phenomenon, solar=False)


def test_fixture_covers_the_m3_gate(almanac: dict[str, Any]) -> None:
    """The gate is 3+ sites (one above 60 N), 4+ dates including a solstice and a DST
    transition. Assert the table still satisfies it, so nobody can shrink it quietly."""
    sites = almanac["sites"]
    dates = {row["date_utc"] for row in almanac["rows"]}
    assert len(sites) >= 3
    assert any(site["latitude_deg"] > 60.0 for site in sites)
    assert len(dates) >= 4
    assert {"2026-06-21", "2026-12-21"} <= dates, "both solstices must be covered"
    assert {"2026-03-29", "2026-10-25"} <= dates, "both EU DST transitions must be covered"
    assert len(almanac["rows"]) == len(sites) * len(dates)


def test_worst_case_deviations_are_within_tolerance() -> None:
    """Report the actual worst-case deviation per phenomenon, not 'passed'."""
    if not DEVIATIONS:
        pytest.skip("no deviations collected — run the whole module")

    worst: dict[tuple[str, str], tuple[float, str, str, str]] = {}
    for phenomenon, deviation, source, site, date, detail in DEVIATIONS:
        key = (phenomenon, source)
        if deviation > worst.get(key, (-1.0, "", "", ""))[0]:
            worst[key] = (deviation, site, date, detail)

    lines = ["", "worst-case deviation from the independent almanac:"]
    failures = []
    for (phenomenon, source), (deviation, site, date, detail) in sorted(
        worst.items(), key=lambda item: -item[1][0]
    ):
        tolerance = _REFERENCE["tolerances_seconds"][TOLERANCE_KIND[phenomenon]]
        lines.append(
            f"  {phenomenon:<18} vs {source:<6} {deviation:7.2f} s "
            f"(tol {tolerance:>2} s)  {site} {date}  {detail}"
        )
        if deviation > tolerance:
            failures.append(f"{phenomenon} vs {source}: {deviation:.2f} s > {tolerance} s")
    report = "\n".join(lines)
    print(report)  # visible with -s or on failure
    assert not failures, report + "\n\nover tolerance: " + "; ".join(failures)
