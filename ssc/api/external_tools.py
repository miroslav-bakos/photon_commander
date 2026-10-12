# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
"""Host tool configuration and bounded directory listings; never file contents."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ssc.platesolve import PlateSolveResult, SolverAvailability, astap_availability, astap_solve
from ssc.settings.store import SettingsStore


def solver_environment(settings: SettingsStore) -> dict[str, str]:
    env = dict(os.environ)
    tools = settings.external_tools
    if tools.astap_cli is not None:
        env["SSC_ASTAP_CLI"] = str(tools.astap_cli)
    if tools.astap_data_dir is not None:
        env["SSC_ASTAP_DATA_DIR"] = str(tools.astap_data_dir)
    return env


class ConfiguredAstap:
    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    async def availability(self) -> SolverAvailability:
        return await astap_availability(env=solver_environment(self._settings))

    async def solve(
        self,
        path: Path,
        *,
        position_hint: tuple[float, float] | None = None,
        field_height_deg: float | None = None,
    ) -> PlateSolveResult:
        return await astap_solve(
            path,
            position_hint=position_hint,
            field_height_deg=field_height_deg,
            env=solver_environment(self._settings),
        )


class HostPathEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    path: str
    directory: bool


class HostPathListing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    parent: str | None
    roots: list[str]
    entries: list[HostPathEntry]
    truncated: bool


def list_host_paths(path: Path | None, fallback: Path | None = None) -> HostPathListing:
    if path is None:
        home = Path.home()
        path = home if home.is_dir() else (fallback or home)
    if not path.is_absolute():
        raise ValueError("enter an absolute path on the server computer")
    path = path.resolve(strict=True)
    entries: list[HostPathEntry] = []
    truncated = False
    with os.scandir(path) as children:
        for scanned, child in enumerate(children):
            if len(entries) >= 500 or scanned >= 1000:
                truncated = True
                break
            if child.name.startswith("."):
                continue
            try:
                directory = child.is_dir()
                if not directory and not child.is_file():
                    continue
            except OSError:
                continue
            entries.append(
                HostPathEntry(name=child.name, path=str(path / child.name), directory=directory)
            )
    entries.sort(key=lambda item: (not item.directory, item.name.casefold()))
    roots = (
        [
            str(Path(f"{letter}:\\"))
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{letter}:\\").is_dir()
        ]
        if os.name == "nt"
        else [path.anchor]
    )
    return HostPathListing(
        path=str(path),
        parent=str(path.parent) if path.parent != path else None,
        roots=roots,
        entries=entries,
        truncated=truncated,
    )
