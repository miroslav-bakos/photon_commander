"""``ssc.settings.capture_folder``: each failure mode gets its own message
(DESIGN.md §9.5.2 — exists, is a directory, is writable, has room)."""

from __future__ import annotations

import stat
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

import pytest

from ssc.settings.capture_folder import (
    MIN_FREE_BYTES,
    CaptureFolderError,
    default_capture_root,
    validate_capture_folder,
)


def test_a_missing_path_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(CaptureFolderError, match="does not exist"):
        validate_capture_folder(missing)


def test_a_file_is_not_a_directory(tmp_path: Path) -> None:
    a_file = tmp_path / "a_file"
    a_file.write_text("not a folder")
    with pytest.raises(CaptureFolderError, match="not a directory"):
        validate_capture_folder(a_file)


def test_an_unwritable_directory_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "readonly"
    folder.mkdir()
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(CaptureFolderError, match="not writable"):
            validate_capture_folder(folder)
    finally:
        # restore so tmp_path cleanup can remove it
        folder.chmod(stat.S_IRWXU)


_Usage = namedtuple("_Usage", "total used free")


def test_a_nearly_full_disk_is_refused(tmp_path: Path) -> None:
    with patch(
        "ssc.settings.capture_folder.shutil.disk_usage",
        return_value=_Usage(total=100 * 1024**3, used=99 * 1024**3, free=1024**2),
    ), pytest.raises(CaptureFolderError, match="GiB free"):
        validate_capture_folder(tmp_path)


def test_a_valid_folder_with_room_returns_its_free_bytes(tmp_path: Path) -> None:
    with patch(
        "ssc.settings.capture_folder.shutil.disk_usage",
        return_value=_Usage(total=100 * 1024**3, used=10 * 1024**3, free=MIN_FREE_BYTES + 1),
    ):
        free = validate_capture_folder(tmp_path)
    assert free == MIN_FREE_BYTES + 1


# -- default_capture_root: a suggestion, never a hardcoded default ----------- #


def test_default_root_prefers_xdg_user_dir_and_appends_ssc() -> None:
    with (
        patch("ssc.settings.capture_folder.shutil.which", return_value="/usr/bin/xdg-user-dir"),
        patch("ssc.settings.capture_folder.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        run.return_value.stdout = "/home/test/Obrázky\n"
        root = default_capture_root()
    assert root == Path("/home/test/Obrázky/SSC")
    run.assert_called_once_with(
        ["/usr/bin/xdg-user-dir", "PICTURES"],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )


def test_default_root_falls_back_to_home_pictures_without_xdg_user_dir() -> None:
    with (
        patch("ssc.settings.capture_folder.shutil.which", return_value=None),
        patch.dict("os.environ", {"HOME": "/home/test"}, clear=False),
    ):
        root = default_capture_root()
    assert root == Path("/home/test/Pictures/SSC")


def test_default_root_is_none_without_home_or_xdg_user_dir() -> None:
    with (
        patch("ssc.settings.capture_folder.shutil.which", return_value=None),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert default_capture_root() is None
