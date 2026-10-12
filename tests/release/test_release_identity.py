import subprocess
from pathlib import Path

import pytest

from scripts.build_release_identity import release_version


def test_only_exact_stable_tag_names_release(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("commit", "--allow-empty", "-qm", "fixture")
    first = git("rev-parse", "HEAD")
    assert release_version(tmp_path, first) == f"0.0.0+g{first[:12]}"
    git("tag", "v0.0.1")
    assert release_version(tmp_path, first) == "0.0.1"
    git("commit", "--allow-empty", "-qm", "next")
    second = git("rev-parse", "HEAD")
    assert release_version(tmp_path, second) == f"0.0.0+g{second[:12]}"
    git("tag", "not-a-version")
    assert release_version(tmp_path, second) == f"0.0.0+g{second[:12]}"


def test_invalid_identity_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="immutable"):
        release_version(tmp_path, "dirty")
