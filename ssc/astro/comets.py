# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Offline comet discovery and two-body position previews.

The catalogue is a maintainer-refreshed snapshot of NASA/JPL SBDB, never a
runtime network dependency. Comet elements are osculating and can age badly
(especially through a close planetary encounter), so every answer retains the
element epoch, solution date, snapshot age and provenance. Callers must show
that age and refuse to present an old solution as current precision.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from skyfield.constants import GM_SUN_DE440_km3_s2
from skyfield.data.spice import inertial_frames
from skyfield.keplerlib import _KeplerOrbit

from ssc.astro.events import Ephemeris, Site, _require_utc
from ssc.astro.units import Deg, Hours, wrap_hours

COMET_SNAPSHOT_MAX_AGE_DAYS: Final = 45
COMET_MAX_CONDITION_CODE: Final = 5
COMET_MAX_ELEMENT_AGE_DAYS: Final = 365.0
COMET_CLOSE_EARTH_MOID_AU: Final = 0.05
COMET_CLOSE_JUPITER_MOID_AU: Final = 0.25
COMET_CLOSE_APPROACH_MAX_ELEMENT_AGE_DAYS: Final = 30.0


@dataclass(frozen=True, slots=True)
class CometOrbit:
    spk_id: int
    designation: str
    name: str | None
    full_name: str
    epoch_jd_tdb: float
    eccentricity: float
    perihelion_distance_au: float
    perihelion_time_jd_tdb: float
    inclination_deg: float
    ascending_node_deg: float
    argument_of_perihelion_deg: float
    solution_date: str | None
    orbit_id: str | None
    condition_code: int | None
    data_arc_days: float | None
    observations_used: int | None
    two_body_model: bool
    nongrav_a1: float | None
    nongrav_a2: float | None
    nongrav_a3: float | None
    earth_moid_au: float | None
    jupiter_moid_au: float | None

    @property
    def has_nongrav_model(self) -> bool:
        return any(value is not None for value in (self.nongrav_a1, self.nongrav_a2, self.nongrav_a3))


@dataclass(frozen=True, slots=True)
class CometPosition:
    comet: CometOrbit
    when: datetime
    ra_j2000_h: Hours
    dec_j2000_deg: Deg
    element_age_days: float


@dataclass(frozen=True, slots=True)
class CometCatalog:
    objects: tuple[CometOrbit, ...]
    source: str
    source_url: str
    fetched_at: datetime

    def age_days(self, at: datetime) -> float:
        return (_require_utc("at", at) - self.fetched_at).total_seconds() / 86_400.0

    def is_stale(self, at: datetime) -> bool:
        return self.age_days(at) > COMET_SNAPSHOT_MAX_AGE_DAYS

    def suggest(self, query: str, limit: int = 8) -> tuple[CometOrbit, ...]:
        if not 1 <= limit <= 20:
            raise ValueError(f"limit must be in [1, 20], got {limit}")
        needle = " ".join(query.casefold().split())
        if len(needle) < 2:
            return ()
        ranked: list[tuple[float, str, CometOrbit]] = []
        for comet in self.objects:
            terms = {comet.designation.casefold(), comet.full_name.casefold()}
            if comet.name is not None:
                terms.add(comet.name.casefold())
            best = max((_match_score(needle, term) for term in terms), default=0.0)
            if best > 0.0:
                ranked.append((best, comet.full_name, comet))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[:limit])

    def find(self, *, spk_id: int, designation: str) -> CometOrbit | None:
        """Resolve both durable identifiers; never accept a recycled/mismatched id."""
        normalized = designation.strip().casefold()
        return next(
            (
                comet
                for comet in self.objects
                if comet.spk_id == spk_id and comet.designation.strip().casefold() == normalized
            ),
            None,
        )


def _match_score(needle: str, term: str) -> float:
    if needle == term:
        return 100.0
    if term.startswith(needle):
        return 90.0 + len(needle) / max(len(term), 1) * 5.0
    if needle in term:
        return 78.0
    similarity = SequenceMatcher(None, needle, term).ratio()
    return 50.0 + similarity * 20.0 if len(needle) >= 4 and similarity >= 0.64 else 0.0


def default_comet_catalog_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "comets" / "jpl-sbdb.json"


@lru_cache(maxsize=1)
def load_comet_catalog(path: Path | None = None) -> CometCatalog:
    resolved = default_comet_catalog_path() if path is None else path
    raw: dict[str, Any] = json.loads(resolved.read_text(encoding="utf-8"))
    fields = raw["fields"]
    expected = [
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
    ]
    if fields != expected:
        raise ValueError(f"{resolved}: unsupported SBDB field layout")
    index = {name: position for position, name in enumerate(fields)}

    def value(row: list[Any], field: str) -> Any:
        return row[index[field]]

    objects = tuple(
        CometOrbit(
            spk_id=int(value(row, "spkid")),
            designation=str(value(row, "pdes")),
            name=None if value(row, "name") is None else str(value(row, "name")),
            full_name=str(value(row, "full_name")).strip(),
            epoch_jd_tdb=float(value(row, "epoch")),
            eccentricity=float(value(row, "e")),
            perihelion_distance_au=float(value(row, "q")),
            perihelion_time_jd_tdb=float(value(row, "tp")),
            inclination_deg=float(value(row, "i")),
            ascending_node_deg=float(value(row, "om")),
            argument_of_perihelion_deg=float(value(row, "w")),
            solution_date=(
                None if value(row, "soln_date") is None else str(value(row, "soln_date"))
            ),
            orbit_id=None if value(row, "orbit_id") is None else str(value(row, "orbit_id")),
            condition_code=(
                None if value(row, "condition_code") is None else int(value(row, "condition_code"))
            ),
            data_arc_days=None if value(row, "data_arc") is None else float(value(row, "data_arc")),
            observations_used=(
                None if value(row, "n_obs_used") is None else int(value(row, "n_obs_used"))
            ),
            two_body_model=str(value(row, "two_body") or "").upper()
            in {"1", "T", "TRUE", "Y", "YES"},
            nongrav_a1=None if value(row, "A1") is None else float(value(row, "A1")),
            nongrav_a2=None if value(row, "A2") is None else float(value(row, "A2")),
            nongrav_a3=None if value(row, "A3") is None else float(value(row, "A3")),
            earth_moid_au=None if value(row, "moid") is None else float(value(row, "moid")),
            jupiter_moid_au=(
                None if value(row, "moid_jup") is None else float(value(row, "moid_jup"))
            ),
        )
        for row in raw["data"]
    )
    fetched_at = datetime.fromisoformat(raw["fetched_at"]).astimezone(UTC)
    return CometCatalog(
        objects=objects,
        source=str(raw["source"]),
        source_url=str(raw["source_url"]),
        fetched_at=fetched_at,
    )


def comet_execution_rejection(
    catalog: CometCatalog, comet: CometOrbit, when: datetime
) -> str | None:
    """Return why SSC's local two-body solver must not point at this orbit.

    The catalogue preview remains useful for planning, but execution is
    deliberately conservative: the local propagator does not implement JPL's
    comet outgassing parameters or planetary encounters.
    """
    instant = _require_utc("when", when)
    if catalog.is_stale(instant):
        return (
            f"the bundled JPL comet snapshot is {catalog.age_days(instant):.1f} days old; "
            f"refresh it before execution (limit {COMET_SNAPSHOT_MAX_AGE_DAYS} days)"
        )
    if comet.condition_code is None or comet.condition_code > COMET_MAX_CONDITION_CODE:
        return (
            "the JPL orbit uncertainty condition code is unavailable or worse than "
            f"{COMET_MAX_CONDITION_CODE}"
        )
    if comet.two_body_model:
        return "JPL marks this as a two-body-only orbit solution"
    if comet.has_nongrav_model:
        return (
            "the JPL solution contains comet non-gravitational parameters that SSC's "
            "local two-body propagator cannot apply"
        )
    element_age_days = abs(_julian_date(instant) - comet.epoch_jd_tdb)
    if element_age_days > COMET_MAX_ELEMENT_AGE_DAYS:
        return (
            f"the orbit elements are {element_age_days:.1f} days from the execution "
            f"instant (limit {COMET_MAX_ELEMENT_AGE_DAYS:.0f} days)"
        )
    near_planetary_orbit = (
        comet.earth_moid_au is not None and comet.earth_moid_au < COMET_CLOSE_EARTH_MOID_AU
    ) or (
        comet.jupiter_moid_au is not None
        and comet.jupiter_moid_au < COMET_CLOSE_JUPITER_MOID_AU
    )
    if near_planetary_orbit and element_age_days > COMET_CLOSE_APPROACH_MAX_ELEMENT_AGE_DAYS:
        return (
            "the orbit passes close to Earth or Jupiter and its osculating elements are "
            f"{element_age_days:.1f} days from the execution instant; refresh or use a "
            "full perturbation ephemeris"
        )
    return None


def _julian_date(when: datetime) -> float:
    """UTC instant as Julian date; the sub-minute UTC/TDB offset is immaterial to guards."""
    return when.timestamp() / 86_400.0 + 2_440_587.5


def comet_position(
    ephemeris: Ephemeris, site: Site, comet: CometOrbit, when: datetime
) -> CometPosition:
    """Two-body apparent topocentric preview from the stored osculating elements."""
    instant = _require_utc("when", when)
    eccentricity = comet.eccentricity
    if eccentricity == 1.0:
        semilatus_rectum = comet.perihelion_distance_au * 2.0
    else:
        semimajor_axis = comet.perihelion_distance_au / (1.0 - eccentricity)
        semilatus_rectum = semimajor_axis * (1.0 - eccentricity * eccentricity)
    perihelion = ephemeris.timescale.tt_jd(comet.perihelion_time_jd_tdb)
    orbit = _KeplerOrbit._from_periapsis(
        semilatus_rectum,
        eccentricity,
        comet.inclination_deg,
        comet.ascending_node_deg,
        comet.argument_of_perihelion_deg,
        perihelion,
        GM_SUN_DE440_km3_s2,
        10,
        comet.full_name,
    )
    # SBDB elements are heliocentric ecliptic J2000; Skyfield's observer and
    # planetary kernel are ICRF. This is the same conversion its MPC helper
    # applies, without importing that helper's optional pandas dependency.
    orbit._rotation = inertial_frames["ECLIPJ2000"].T
    target = ephemeris.sun + orbit
    time = ephemeris.timescale.from_datetime(instant)
    # Skyfield 1.54 assigns ndarray.shape in its universal-variable solver;
    # NumPy 2.5 deprecates that spelling even though the result is unchanged.
    # Keep the compatibility warning out of strict test/runtime logs while
    # leaving every warning from our own code visible.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Setting the shape on a NumPy array has been deprecated",
            category=DeprecationWarning,
            module=r"skyfield\.keplerlib",
        )
        apparent = ephemeris.observer(site).at(time).observe(target).apparent()
    ra, dec, _distance = apparent.radec()
    return CometPosition(
        comet=comet,
        when=instant,
        ra_j2000_h=wrap_hours(float(ra.hours)),
        dec_j2000_deg=Deg(float(dec.degrees)),
        element_age_days=abs(float(time.tdb) - comet.epoch_jd_tdb),
    )


__all__ = [
    "COMET_MAX_CONDITION_CODE",
    "COMET_MAX_ELEMENT_AGE_DAYS",
    "COMET_SNAPSHOT_MAX_AGE_DAYS",
    "CometCatalog",
    "CometOrbit",
    "CometPosition",
    "comet_execution_rejection",
    "comet_position",
    "default_comet_catalog_path",
    "load_comet_catalog",
]
