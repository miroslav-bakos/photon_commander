# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Validating a chosen capture folder: exists, is a directory, is writable,
has room. DESIGN.md §9.5.2: this is the one place SSC writes gigabytes a
night, so a folder that fails silently would either fill a system partition
or quietly drop the night (CLAUDE.md invariant 7) — a capture that cannot
write must say so and refuse, before the first frame, not the four-hundredth.

Mirrors :mod:`ssc.settings.tls`'s discipline: I/O validation lives in a plain
function that raises a domain error with a message naming the fix, not inside
the (frozen, I/O-free) Pydantic model.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

#: A measured ImageBytes frame is ~16.6 MB (DESIGN.md §9.5.2). This is not a
#: night's budget — it is a tripwire: ~300 frames' worth of headroom, roughly
#: 20-30 minutes at the measured ~3.5-4 s cadence, is enough that a folder
#: which is already nearly full is refused at Settings time rather than
#: discovered mid-session when the disk finally fills.
MIN_FREE_BYTES: Final = 5 * 1024**3


class CaptureFolderError(ValueError):
    """The chosen capture folder cannot be written to."""


def validate_capture_folder(folder: Path) -> int:
    """Raise :class:`CaptureFolderError` naming the problem, or return the
    folder's free byte count on success.

    Each failure mode gets its own message, deliberately, per the gate this
    dispatch is built against: a user pointing this at a typo'd path, a file,
    a read-only mount or a full disk should never see the same generic error.
    """
    if not folder.exists():
        raise CaptureFolderError(f"capture folder {folder} does not exist")
    if not folder.is_dir():
        raise CaptureFolderError(f"capture folder {folder} is not a directory")
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".ssc-write-test-") as probe:
            probe.write(b"ssc")
            probe.flush()
    except OSError as exc:
        raise CaptureFolderError(f"capture folder {folder} is not writable: {exc}") from exc
    usage = shutil.disk_usage(folder)
    if usage.free < MIN_FREE_BYTES:
        raise CaptureFolderError(
            f"capture folder {folder} has only {usage.free / 1024**3:.2f} GiB free; "
            f"at least {MIN_FREE_BYTES / 1024**3:.0f} GiB is required before capture may start"
        )
    return usage.free


def user_pictures_folder() -> Path | None:
    """Resolve the host user's Pictures directory, including Windows redirection."""
    if os.name == "nt":
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x27, None, 0, buffer) == 0:
            return Path(buffer.value)
        home = os.environ.get("USERPROFILE")
        return Path(home) / "Pictures" if home else None
    return _xdg_pictures_folder()


def default_capture_root() -> Path | None:
    pictures = user_pictures_folder()
    return None if pictures is None else pictures / "SSC"


def _xdg_pictures_folder() -> Path | None:
    """A *suggestion* for a fresh install — never what ``CaptureSettings.folder``
    silently defaults to, which stays ``None`` until the user actually
    chooses (CLAUDE.md invariant 11's sibling rule for storage).

    ``xdg-user-dir PICTURES`` is the desktop-environment-aware answer: it
    resolves the user's own *localised* Pictures folder
    (``~/.config/user-dirs.dirs`` — ``Obrázky``, ``Bilder``, ...), so a
    hardcoded ``~/Pictures`` would create a stray second, wrong folder next
    to the real one rather than suggesting it. Falls back to
    ``$HOME/Pictures`` only when that tool is unavailable or answers nothing
    usable (a headless or non-XDG host). Returns ``None`` if even ``$HOME``
    is unknown — there is nothing left to suggest.

    Does its own subprocess/filesystem I/O; callers on the event loop must
    wrap this in ``asyncio.to_thread`` (CLAUDE.md's async discipline).
    """
    pictures: Path | None = None
    xdg_user_dir = shutil.which("xdg-user-dir")
    if xdg_user_dir is not None:
        try:
            result = subprocess.run(
                [xdg_user_dir, "PICTURES"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            pictures = Path(result.stdout.strip())
    if pictures is None:
        home = os.environ.get("HOME")
        if home:
            pictures = Path(home) / "Pictures"
    return pictures


__all__ = [
    "MIN_FREE_BYTES",
    "CaptureFolderError",
    "default_capture_root",
    "validate_capture_folder",
]
