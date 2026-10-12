from pathlib import Path

import pytest

from scripts.release_manual import write_installation_manual


@pytest.mark.parametrize("platform", ["windows", "pi"])
def test_package_has_localized_offline_illustrated_guides(tmp_path: Path, platform: str) -> None:
    commit = "a" * 40
    manual = write_installation_manual(tmp_path, platform, commit, 0)
    assert f"Exact build commit: {commit}" in manual.read_text(encoding="utf-8")
    assert len(list((tmp_path / "docs/images").glob("*/*.jpg"))) == 21
    for language in ("en", "sk", "cs"):
        assert (tmp_path / f"INSTALLATION.{language}.txt").is_file()
        page = (tmp_path / f"docs/getting-started.{language}.html").read_text(encoding="utf-8")
        assert f'lang="{language}"' in page
        assert f'images/{language}/planning.jpg' in page
        assert "<h1>" in page
    assert (tmp_path / "docs/index.html").is_file()


def test_invalid_identity_does_not_create_manual(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full lowercase commit"):
        write_installation_manual(tmp_path, "windows", "dirty", 0)
    assert not list(tmp_path.iterdir())
