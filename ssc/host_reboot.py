# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Narrow, opt-in Raspberry Pi host reboot capability.

The application keeps NoNewPrivileges. An administrator may grant its service
user only login1 reboot/poweroff actions through polkit.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)
_SYSTEMCTL = "/usr/bin/systemctl"
_PKCHECK = "/usr/bin/pkcheck"
_MODEL = Path("/proc/device-tree/model")


def availability(*, auth_required: bool, action: str = "reboot") -> tuple[bool, str]:
    """Return a user-facing reason without attempting a reboot."""
    del auth_required  # Use the same ControlDep authentication policy as other controls.
    if action not in {"reboot", "poweroff"}:
        return False, "Unsupported power action."
    try:
        model = _MODEL.read_bytes().decode("utf-8", errors="replace").strip("\x00")
    except OSError:
        return False, "Host reboot is available only on a Raspberry Pi."
    if not model.startswith("Raspberry Pi"):
        return False, "Host reboot is available only on a Raspberry Pi."
    try:
        result = subprocess.run(
            [_PKCHECK, "--action-id", "org.freedesktop.login1." + ("power-off" if action == "poweroff" else "reboot"), "--process", str(os.getpid())],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "Raspberry Pi power permission is not configured."
    if result.returncode != 0:
        return False, "Raspberry Pi power permission is not configured."
    return True, "Ready to reboot the Raspberry Pi."


def reboot() -> None:
    """Invoke only the administrator-approved, exact reboot command."""
    try:
        subprocess.run([_SYSTEMCTL, "--no-ask-password", "reboot"], timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        _LOG.exception("Raspberry Pi reboot command failed")


def shutdown() -> None:
    """Request an orderly host shutdown through the exact login1 permission."""
    try:
        subprocess.run([_SYSTEMCTL, "--no-ask-password", "poweroff"], timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        _LOG.exception("Raspberry Pi shutdown command failed")
