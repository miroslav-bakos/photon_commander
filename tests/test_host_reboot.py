"""The host reboot escape hatch is opt-in and executes one fixed command."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ssc import host_reboot


def test_host_reboot_requires_pi_and_exact_polkit_permission(tmp_path: Path, monkeypatch) -> None:
    model = tmp_path / "model"
    model.write_bytes(b"Generic Linux\x00")
    monkeypatch.setattr(host_reboot, "_MODEL", model)
    assert host_reboot.availability(auth_required=True)[0] is False

    model.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\x00")
    commands: list[list[str]] = []

    def denied(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(host_reboot.subprocess, "run", denied)
    assert host_reboot.availability(auth_required=True)[0] is False
    assert commands == [["/usr/bin/pkcheck", "--action-id", "org.freedesktop.login1.reboot", "--process", str(host_reboot.os.getpid())]]

    monkeypatch.setattr(
        host_reboot.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )
    assert host_reboot.availability(auth_required=True)[0] is True


def test_host_reboot_invokes_only_exact_command(monkeypatch) -> None:
    commands: list[list[str]] = []

    def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(host_reboot.subprocess, "run", record)
    host_reboot.reboot()
    assert commands == [["/usr/bin/systemctl", "--no-ask-password", "reboot"]]


def test_shutdown_uses_only_fixed_poweroff_command(monkeypatch) -> None:
    commands = []
    monkeypatch.setattr(host_reboot.subprocess, "run", lambda command, **kwargs: commands.append(command))
    host_reboot.shutdown()
    assert commands == [["/usr/bin/systemctl", "--no-ask-password", "poweroff"]]


def test_shutdown_probes_power_off_permission(tmp_path, monkeypatch) -> None:
    model = tmp_path / "model"
    model.write_bytes(b"Raspberry Pi 5")
    monkeypatch.setattr(host_reboot, "_MODEL", model)
    commands = []
    def probe(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(host_reboot.subprocess, "run", probe)
    assert host_reboot.availability(auth_required=False, action="poweroff")[0]
    assert commands[0][2] == "org.freedesktop.login1.power-off"
