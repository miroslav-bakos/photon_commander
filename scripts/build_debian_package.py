#!/usr/bin/env python3
"""Build Photon Commander's redistributable ARM64 Debian package.

The release entry point intentionally accepts an exact commit rather than an
ambient working tree description. It builds both immutable payloads, assembles
them below ``/opt``, and leaves all mutable application data on the external
SSD. Signing is detached so an unsigned byte-identical artifact can be rebuilt
without access to the release private key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.release_manual import write_installation_manual
except ModuleNotFoundError:  # Direct execution from scripts/.
    from release_manual import write_installation_manual

ROOT = Path(__file__).resolve().parents[1]
DEBIAN_INPUT = ROOT / "packaging" / "debian"
PACKAGE = "photon-commander"
INSTALL_ROOT = Path("opt/photon-commander")
DOC_ROOT = Path("usr/share/doc/photon-commander")
FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
DEBIAN_VERSION = re.compile(r"[0-9][0-9A-Za-z.+:~-]*")
FORBIDDEN_SUFFIXES = {".py", ".pyi", ".pyc", ".map", ".ts", ".tsx", ".jsx"}
FORBIDDEN_PARTS = {".git", ".beads", "tests", "__pycache__"}
FORBIDDEN_SURVEY_PAYLOAD_PARTS = {
    "dss",
    "dss2",
    "dsscolor",
    "dss2-color",
}


def run(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if result.returncode:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(args)}")
    return result.stdout.strip()


def validate_release_identity(commit: str) -> tuple[str, int]:
    if FULL_COMMIT.fullmatch(commit) is None:
        raise SystemExit("build-deb: commit must be a full 40-character lowercase SHA-1")
    head = run("git", "rev-parse", "HEAD")
    if head != commit:
        raise SystemExit(f"build-deb: requested {commit}, but the checkout is {head}")
    dirty = run("git", "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise SystemExit("build-deb: checkout contains modified or untracked files")
    described = run("git", "describe", "--tags", "--always", commit)
    raw_version = described.removeprefix("v").replace("-", "+", 1).replace("-g", ".")
    version = re.sub(r"[^0-9A-Za-z.+:~-]", ".", raw_version)
    if DEBIAN_VERSION.fullmatch(version) is None:
        version = f"0.0.0+git.{commit[:12]}"
    epoch = int(run("git", "show", "-s", "--format=%ct", commit))
    return version, epoch


def require_arm64() -> None:
    machine = platform.machine().lower()
    if machine not in {"aarch64", "arm64"}:
        raise SystemExit(
            f"build-deb: Nuitka requires a native ARM64 build host, not {machine or 'unknown'}"
        )


def validate_arm64_binary(binary: Path) -> None:
    header = run("readelf", "-h", str(binary))
    if "Machine:" not in header or "AArch64" not in header:
        raise SystemExit(f"build-deb: {binary} is not an AArch64 ELF executable")


def copy_tree(source: Path, target: Path, *, exclude_product: bool = False) -> None:
    if not source.is_dir():
        raise SystemExit(f"build-deb: required directory is missing: {source}")
    shutil.copytree(source, target, symlinks=True,
                    ignore=shutil.ignore_patterns("product") if exclude_product else None)


def assert_payload_boundary(package_root: Path) -> None:
    failures: list[str] = []
    for path in package_root.rglob("*"):
        relative = path.relative_to(package_root)
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            failures.append(str(relative))
        parts = [part.lower() for part in relative.parts]
        if "hips" in parts:
            index = parts.index("hips") + 1
            if index < len(parts) and parts[index] != "wham":
                failures.append(str(relative))
        if any(part.lower() in FORBIDDEN_SURVEY_PAYLOAD_PARTS for part in relative.parts):
            failures.append(str(relative))
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(str(relative))
    if failures:
        joined = "\n  ".join(sorted(set(failures)))
        raise SystemExit(f"build-deb: compiled payload contains forbidden files:\n  {joined}")


def write_text(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


def assemble_root(
    package_root: Path,
    *,
    app_dir: Path,
    web_dir: Path,
    version: str,
    commit: str,
    epoch: int,
) -> None:
    copy_tree(app_dir, package_root / INSTALL_ROOT / "app")
    copy_tree(web_dir, package_root / INSTALL_ROOT / "web", exclude_product=True)

    service_target = package_root / "lib/systemd/system/photon-commander.service"
    service_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DEBIAN_INPUT / "photon-commander.service", service_target)

    environment_target = package_root / "etc/photon-commander/environment"
    environment_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DEBIAN_INPUT / "environment", environment_target)

    doc_target = package_root / DOC_ROOT
    doc_target.mkdir(parents=True, exist_ok=True)
    write_installation_manual(doc_target, "pi", commit, epoch)
    for source in (ROOT / "LICENSE", ROOT / "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(source, doc_target / source.name)
    copy_tree(ROOT / "packaging/third-party-licenses", doc_target / "third-party-licenses")
    copy_tree(ROOT / "packaging/third-party-sources", doc_target / "third-party-sources")
    manifest = {
        "architecture": "arm64",
        "commit": commit,
        "package": PACKAGE,
        "source_date_epoch": epoch,
        "version": version,
    }
    write_text(
        doc_target / "build-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    debian = package_root / "DEBIAN"
    debian.mkdir(parents=True, exist_ok=True)
    installed_size = sum(path.stat().st_size for path in package_root.rglob("*") if path.is_file())
    control = f"""Package: {PACKAGE}
Version: {version}
Architecture: arm64
Maintainer: Photon Commander <maintainer@example.org>
Installed-Size: {(installed_size + 1023) // 1024}
Depends: adduser, systemd, mount
Section: misc
Priority: optional
Homepage: https://photoncommander.com
X-Photon-Commit: {commit}
Description: Observatory control and imaging automation for smart telescopes
 Photon Commander provides planning, capture, calibration, live stacking,
 streaming, scheduling, and safety automation from a browser-based interface.
"""
    write_text(debian / "control", control)
    write_text(debian / "conffiles", "/etc/photon-commander/environment\n")
    for name in ("postinst", "prerm", "postrm"):
        shutil.copy2(DEBIAN_INPUT / name, debian / name)
        (debian / name).chmod(0o755)

    md5_lines: list[str] = []
    for path in sorted(package_root.rglob("*")):
        if path.is_file() and "DEBIAN" not in path.relative_to(package_root).parts:
            digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
            md5_lines.append(f"{digest}  {path.relative_to(package_root)}")
    write_text(debian / "md5sums", "\n".join(md5_lines) + "\n")

    assert_payload_boundary(package_root)
    for path in package_root.rglob("*"):
        if not path.is_symlink():
            os.utime(path, (epoch, epoch), follow_symlinks=False)


def build_payloads() -> None:
    run("uv", "run", "python", "scripts/check-licenses.py", "--commercial")
    run("npm", "ci", cwd=ROOT / "web")
    run("npm", "run", "build", cwd=ROOT / "web")
    run("uv", "run", "python", "scripts/build_nuitka.py", "--output", "dist/ssc-core")
    validate_arm64_binary(ROOT / "dist/ssc-core/ssc-core")


def build_deb(commit: str, *, output_dir: Path, sign_key: str | None) -> Path:
    require_arm64()
    version, epoch = validate_release_identity(commit)
    build_payloads()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{PACKAGE}_{version}_arm64.deb"
    with tempfile.TemporaryDirectory(prefix="photon-deb-") as temporary:
        package_root = Path(temporary) / "root"
        package_root.mkdir()
        assemble_root(
            package_root,
            app_dir=ROOT / "dist/ssc-core",
            web_dir=ROOT / "web/dist",
            version=version,
            commit=commit,
            epoch=epoch,
        )
        environment = os.environ.copy()
        environment["SOURCE_DATE_EPOCH"] = str(epoch)
        run(
            "dpkg-deb",
            "--root-owner-group",
            "--uniform-compression",
            "--build",
            str(package_root),
            str(output),
            env=environment,
        )
        for manual in (package_root / DOC_ROOT).glob("INSTALLATION*.txt"):
            shutil.copy2(manual, output.with_suffix("." + manual.name))

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    write_text(output.with_suffix(output.suffix + ".sha256"), f"{digest}  {output.name}\n")
    if sign_key:
        signature = output.with_suffix(output.suffix + ".asc")
        run(
            "gpg",
            "--batch",
            "--yes",
            "--local-user",
            sign_key,
            "--armor",
            "--detach-sign",
            "--output",
            str(signature),
            str(output),
        )
        run("gpg", "--verify", str(signature), str(output))
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="exact full release commit")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/debian")
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument(
        "--sign-key",
        help="GPG key fingerprint for detached signing; the private key is never copied",
    )
    signing.add_argument(
        "--unsigned",
        action="store_true",
        help="build an explicitly unsigned CI/reproducibility candidate",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.sign_key is None and not args.unsigned:
        raise SystemExit("build-deb: pass --sign-key FINGERPRINT or explicitly pass --unsigned")
    output = build_deb(args.commit, output_dir=args.output_dir, sign_key=args.sign_key)
    print(f"build-deb: artifact {output}")


if __name__ == "__main__":
    main()
