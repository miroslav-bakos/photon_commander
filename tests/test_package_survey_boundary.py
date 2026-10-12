"""A stale survey pack must never re-enter a distributable payload."""
from pathlib import Path

import pytest

from scripts.build_debian_package import assert_payload_boundary as debian_boundary
from scripts.build_linux_packages import assert_payload_boundary as linux_boundary


@pytest.mark.parametrize("boundary", [debian_boundary, linux_boundary])
def test_payload_rejects_stale_wham_pack(tmp_path: Path, boundary) -> None:
    tile = tmp_path / "app/data/hips/wham/Norder0/Dir0/Npix0.png"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"stale-survey-pixels")
    with pytest.raises(SystemExit):
        boundary(tmp_path)
