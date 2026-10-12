from __future__ import annotations

from pathlib import Path

from ssc.api.astro_service import _load_sync

ROOT = Path(__file__).resolve().parents[2]


def test_production_profile_set_never_contains_the_dev_simulator() -> None:
    _catalog, profiles, ephemeris, _error = _load_sync()
    try:
        assert "dev-s30pro-simulator" not in profiles
    finally:
        if ephemeris is not None:
            ephemeris.close()


def test_dev_profile_requires_an_explicit_loader_flag() -> None:
    _catalog, profiles, ephemeris, _error = _load_sync(include_dev_profiles=True)
    try:
        simulator = profiles["dev-s30pro-simulator"]
        assert simulator.name.startswith("DEV —")
        assert simulator.aliases == ("SSC S30 Pro Simulator",)
    finally:
        if ephemeris is not None:
            ephemeris.close()


def test_packaging_spec_does_not_bundle_dev_profiles() -> None:
    spec = (ROOT / "packaging" / "ssc-core.spec").read_text(encoding="utf-8")
    assert "data/dev-profiles" not in spec
    assert '"data" / "profiles"' in spec
