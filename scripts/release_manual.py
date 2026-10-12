"""Include localized user guides and immutable identity in release packages."""
from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from html import escape
from pathlib import Path


def write_installation_manual(output: Path, platform: str, commit: str, epoch: int) -> Path:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("manual requires a full lowercase commit SHA")
    if platform not in {"windows", "linux", "pi", "pi-appliance", "appimage"}:
        raise ValueError(f"unsupported manual platform: {platform}")
    root = Path(__file__).resolve().parents[1]
    guide_platform = "pi" if platform == "pi-appliance" else platform
    guide_root = root / "docs" / "installation"
    output.mkdir(parents=True, exist_ok=True)
    metadata = (
        "PHOTON COMMANDER — INSTALLATION AND FIRST RUN\n"
        f"Exact build commit: {commit}\n"
        f"Build timestamp: {datetime.fromtimestamp(epoch, UTC).isoformat()}\n"
        f"Package variant: {platform}\n\n"
    )
    for language in ("en", "sk", "cs"):
        source = guide_root / f"{guide_platform}.{language}.txt"
        if not source.is_file():
            raise FileNotFoundError(f"Missing release guide: {source}")
        (output / f"INSTALLATION.{language}.txt").write_text(
            metadata + source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path = output / "INSTALLATION.txt"
    shutil.copy2(output / "INSTALLATION.en.txt", path)
    destination = output / "docs"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(root / "docs", destination)
    from markdown_it import MarkdownIt

    style = """body{font:17px/1.65 system-ui,sans-serif;max-width:1000px;margin:auto;padding:24px;color:#192c3a;background:#fafcfe}a{color:#006d91}img{width:100%;height:auto;border-radius:12px}h1,h2,h3{line-height:1.25}h2{margin-top:2em;border-bottom:1px solid #ccdce4;padding-bottom:.4em}code{overflow-wrap:anywhere}li{margin:.5em 0}nav{display:flex;gap:24px;flex-wrap:wrap;padding:16px;background:#e9f2f7;border-radius:12px}pre{white-space:pre-wrap} :focus-visible{outline:3px solid #d57000;outline-offset:3px}"""
    languages = (("en", "English"), ("sk", "Slovenčina"), ("cs", "Čeština"))
    navigation = "<nav>" + " ".join(
        f'<a href="getting-started.{language}.html">{name}</a>'
        for language, name in languages
    ) + "</nav>"
    renderer = MarkdownIt("commonmark", {"html": False})
    for language, _ in languages:
        source = destination / f"getting-started.{language}.md"
        page = (
            f'<!doctype html><html lang="{language}"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Photon Commander — {escape(language)}</title><style>{style}</style>"
            f"<body>{navigation}<main>{renderer.render(source.read_text(encoding='utf-8'))}</main></body></html>"
        )
        source.with_suffix(".html").write_text(page, encoding="utf-8")
    (destination / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Photon Commander guides</title><style>{style}</style>"
        f"<body><h1>Photon Commander</h1>{navigation}<p>Installation · Setup · Observing · Troubleshooting</p></body></html>",
        encoding="utf-8",
    )
    return path
