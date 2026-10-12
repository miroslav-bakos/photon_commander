from __future__ import annotations

from pathlib import Path

import pytest

from ssc.api.external_tools import ConfiguredAstap, list_host_paths, solver_environment
from ssc.platesolve.astap import resolve_astap_cli
from ssc.settings.store import SettingsStore
from tests.api.conftest import TestClient, services_of


def test_tool_paths_survive_store_reload_and_export(paired: TestClient, tmp_path: Path) -> None:
    paths = {"astap_cli": str(tmp_path / "astap.exe"), "astap_data_dir": str(tmp_path / "stars")}
    response = paired.put("/api/settings/external-tools/paths", json=paths)
    assert response.status_code == 200, response.text
    store = SettingsStore(services_of(paired).database)
    assert paired.portal is not None
    paired.portal.call(store.load)
    assert store.external_tools.model_dump(mode="json") == paths
    assert (
        paired.get("/api/settings/configuration/export").json()["settings"]["external_tools"]
        == paths
    )
    assert solver_environment(store)["SSC_ASTAP_CLI"] == paths["astap_cli"]
    assert (
        paired.put(
            "/api/settings/external-tools/paths", json={"astap_cli": "relative.exe"}
        ).status_code
        == 422
    )


def test_host_browser_lists_server_paths_and_rejects_missing_folder(
    paired: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "browse"
    root.mkdir()
    (root / "stars").mkdir()
    (root / "astap.exe").write_bytes(b"not executed by listing")
    (root / ".hidden").write_bytes(b"not listed")
    response = paired.get("/api/settings/host-paths", params={"path": str(root)})
    assert response.status_code == 200, response.text
    listing = response.json()
    assert [(entry["name"], entry["directory"]) for entry in listing["entries"]] == [
        ("stars", True),
        ("astap.exe", False),
    ]
    assert listing["parent"] == str(tmp_path)
    assert not listing["truncated"]
    assert (
        paired.get(
            "/api/settings/host-paths", params={"path": str(tmp_path / "missing")}
        ).status_code
        == 422
    )


def test_host_browser_bounds_large_directories(tmp_path: Path) -> None:
    for index in range(502):
        (tmp_path / str(index)).touch()
    listing = list_host_paths(tmp_path)
    assert len(listing.entries) == 500
    assert listing.truncated


def test_windows_install_detection_uses_injected_program_files(tmp_path: Path) -> None:
    binary = tmp_path / "astap" / "astap.exe"
    binary.parent.mkdir()
    binary.touch(mode=0o755)
    assert resolve_astap_cli({"PATH": "", "ProgramFiles": str(tmp_path)}) == binary


@pytest.mark.asyncio
async def test_configured_solver_passes_saved_paths_to_probe_and_solve(
    paired: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paired.put("/api/settings/external-tools/paths", json={"astap_cli": str(tmp_path / "astap")})
    seen = []

    async def probe(**kwargs):
        seen.append(kwargs)
        return None

    async def solve(path, **kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr("ssc.api.external_tools.astap_availability", probe)
    monkeypatch.setattr("ssc.api.external_tools.astap_solve", solve)
    solver = ConfiguredAstap(services_of(paired).settings)
    await solver.availability()
    await solver.solve(tmp_path / "frame.fits", position_hint=(1.0, 2.0), field_height_deg=2.24)
    assert all(call["env"]["SSC_ASTAP_CLI"] == str(tmp_path / "astap") for call in seen)
    assert seen[1]["position_hint"] == (1.0, 2.0)
