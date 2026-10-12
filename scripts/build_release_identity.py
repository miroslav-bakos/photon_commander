"""Resolve release versions from an exact Git tag without losing commit identity."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def release_version(root: Path, commit: str) -> str:
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("Full immutable commit required")
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", commit], cwd=root,
        capture_output=True, text=True, check=False,
    )
    tag = result.stdout.strip().removeprefix("v")
    if result.returncode == 0 and re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag):
        return tag
    return f"0.0.0+g{commit[:12]}"
