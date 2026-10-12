#!/usr/bin/env python3
"""Build matching native DEB and RPM packages from one exact Linux commit."""

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
PACKAGE = "photon-commander"
INPUT = ROOT / "packaging" / "linux"
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


def write_text(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


def validate_release_identity(commit: str) -> tuple[str, str, str, int]:
    if FULL_COMMIT.fullmatch(commit) is None:
        raise SystemExit("build-linux: commit must be a full 40-character lowercase SHA-1")
    head = run("git", "rev-parse", "HEAD")
    if head != commit:
        raise SystemExit(f"build-linux: requested {commit}, but the checkout is {head}")
    dirty = run("git", "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise SystemExit("build-linux: checkout contains modified or untracked files")
    described = run("git", "describe", "--tags", "--always", commit)
    raw_version = described.removeprefix("v").replace("-", "+", 1).replace("-g", ".")
    deb_version = re.sub(r"[^0-9A-Za-z.+:~-]", ".", raw_version)
    if DEBIAN_VERSION.fullmatch(deb_version) is None:
        deb_version = f"0.0.0+git.{commit[:12]}"
    exact_tag = (
        run("git", "describe", "--tags", "--exact-match", commit)
        if _has_exact_tag(commit)
        else None
    )
    if exact_tag:
        rpm_version = re.sub(r"[^0-9A-Za-z._+~]", ".", exact_tag.removeprefix("v"))
        rpm_release = "1"
    else:
        rpm_version = "0.0.0"
        rpm_release = f"1.git.{commit[:12]}"
    epoch = int(run("git", "show", "-s", "--format=%ct", commit))
    return deb_version, rpm_version, rpm_release, epoch


def _has_exact_tag(commit: str) -> bool:
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", commit],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def native_architecture() -> tuple[str, str, str]:
    machine = platform.machine().lower()
    mapping = {
        "x86_64": ("amd64", "x86_64", "Advanced Micro Devices X86-64"),
        "amd64": ("amd64", "x86_64", "Advanced Micro Devices X86-64"),
        "aarch64": ("arm64", "aarch64", "AArch64"),
        "arm64": ("arm64", "aarch64", "AArch64"),
    }
    if machine not in mapping:
        raise SystemExit(f"build-linux: unsupported native architecture {machine or 'unknown'}")
    return mapping[machine]


def validate_binary(binary: Path, expected_machine: str) -> None:
    header = run("readelf", "-h", str(binary))
    if f"Machine:                           {expected_machine}" not in header:
        raise SystemExit(f"build-linux: {binary} is not a native {expected_machine} ELF executable")


def copy_tree(source: Path, target: Path, *, exclude_product: bool = False) -> None:
    if not source.is_dir():
        raise SystemExit(f"build-linux: required directory is missing: {source}")
    shutil.copytree(source, target, symlinks=True,
                    ignore=shutil.ignore_patterns("product") if exclude_product else None)


def assert_payload_boundary(package_root: Path) -> None:
    failures: list[str] = []
    for path in package_root.rglob("*"):
        relative = path.relative_to(package_root)
        if path.is_symlink() and not path.resolve().is_relative_to(package_root.resolve()):
            failures.append(str(relative))
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            failures.append(str(relative))
        parts = [part.lower() for part in relative.parts]
        if "hips" in parts:
            failures.append(str(relative))
        if any(part.lower() in FORBIDDEN_SURVEY_PAYLOAD_PARTS for part in relative.parts):
            failures.append(str(relative))
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(str(relative))
    if failures:
        joined = "\n  ".join(sorted(set(failures)))
        raise SystemExit(f"build-linux: compiled payload contains forbidden files:\n  {joined}")


def assemble_payload(
    package_root: Path,
    *,
    app_dir: Path,
    web_dir: Path,
    architecture: str,
    version: str,
    commit: str,
    epoch: int,
) -> None:
    copy_tree(app_dir, package_root / INSTALL_ROOT / "app")
    copy_tree(web_dir, package_root / INSTALL_ROOT / "web", exclude_product=True)
    for source, destination in (
        (
            INPUT / "photon-commander.service",
            package_root / "usr/lib/systemd/system/photon-commander.service",
        ),
        (INPUT / "environment", package_root / "etc/photon-commander/environment"),
        (
            INPUT / "photon-commander.desktop",
            package_root / "usr/share/applications/photon-commander.desktop",
        ),
        (
            ROOT / "web/public/icons/icon-192.png",
            package_root / "usr/share/icons/hicolor/192x192/apps/photon-commander.png",
        ),
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    doc_target = package_root / DOC_ROOT
    doc_target.mkdir(parents=True, exist_ok=True)
    manual_platform = "linux" if architecture in {"x86_64", "amd64"} else "pi"
    write_installation_manual(doc_target, manual_platform, commit, epoch)
    for source in (ROOT / "LICENSE", ROOT / "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(source, doc_target / source.name)
    copy_tree(ROOT / "packaging/third-party-licenses", doc_target / "third-party-licenses")
    copy_tree(ROOT / "packaging/third-party-sources", doc_target / "third-party-sources")
    manifest = {
        "architecture": architecture,
        "commit": commit,
        "package": PACKAGE,
        "source_date_epoch": epoch,
        "version": version,
    }
    write_text(
        doc_target / "build-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    assert_payload_boundary(package_root)
    for path in package_root.rglob("*"):
        if not path.is_symlink():
            os.utime(path, (epoch, epoch), follow_symlinks=False)


def add_debian_metadata(root: Path, *, version: str, architecture: str) -> None:
    debian = root / "DEBIAN"
    debian.mkdir()
    installed_size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    control = f"""Package: {PACKAGE}
Version: {version}
Architecture: {architecture}
Maintainer: Photon Commander <maintainer@example.org>
Installed-Size: {(installed_size + 1023) // 1024}
Depends: adduser, systemd, xdg-utils
Section: science
Priority: optional
Homepage: https://photoncommander.com
Description: Observatory control and imaging automation for smart telescopes
 Photon Commander provides planning, capture, calibration, live stacking,
 streaming, scheduling, and safety automation from a browser-based interface.
"""
    write_text(debian / "control", control)
    write_text(debian / "conffiles", "/etc/photon-commander/environment\n")
    for name in ("postinst", "prerm", "postrm"):
        shutil.copy2(INPUT / "debian" / name, debian / name)
        (debian / name).chmod(0o755)


def build_payloads(expected_machine: str) -> None:
    run("uv", "run", "python", "scripts/check-licenses.py", "--commercial")
    run("npm", "ci", cwd=ROOT / "web")
    run("npm", "run", "build", cwd=ROOT / "web")
    run("uv", "run", "python", "scripts/build_nuitka.py", "--output", "dist/ssc-core")
    validate_binary(ROOT / "dist/ssc-core/ssc-core", expected_machine)


def checksum(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    write_text(path.with_suffix(path.suffix + ".sha256"), f"{digest}  {path.name}\n")


def build_packages(
    commit: str, output_dir: Path, *, formats: str = "both"
) -> tuple[Path, Path | None]:
    if formats not in {"both", "deb"}:
        raise SystemExit(f"build-linux: unsupported package formats: {formats}")
    deb_arch, rpm_arch, expected_machine = native_architecture()
    deb_version, rpm_version, rpm_release, epoch = validate_release_identity(commit)
    for command in (("dpkg-deb", "rpmbuild") if formats == "both" else ("dpkg-deb",)):
        if shutil.which(command) is None:
            raise SystemExit(f"build-linux: required packaging tool is missing: {command}")
    build_payloads(expected_machine)
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    with tempfile.TemporaryDirectory(prefix="photon-linux-") as temporary:
        temp = Path(temporary)
        common = temp / "common"
        common.mkdir()
        assemble_payload(
            common,
            app_dir=ROOT / "dist/ssc-core",
            web_dir=ROOT / "web/dist",
            architecture=rpm_arch,
            version=deb_version,
            commit=commit,
            epoch=epoch,
        )
        deb_root = temp / "deb-root"
        shutil.copytree(common, deb_root, symlinks=True)
        add_debian_metadata(deb_root, version=deb_version, architecture=deb_arch)
        deb = output_dir / f"{PACKAGE}_{deb_version}_{deb_arch}.deb"
        run("dpkg-deb", "--root-owner-group", "--build", str(deb_root), str(deb), env=environment)
        for manual in (common / DOC_ROOT).glob("INSTALLATION*.txt"):
            shutil.copy2(manual, deb.with_suffix("." + manual.name))

        rpm = None
        if formats == "both":
            topdir = temp / "rpmbuild"
            for child in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
                (topdir / child).mkdir(parents=True)
            run(
                "rpmbuild",
                "-bb",
                "--define",
                f"_topdir {topdir}",
                "--define",
                f"photon_stage {common}",
                "--define",
                f"photon_version {rpm_version}",
                "--define",
                f"photon_release {rpm_release}",
                "--define",
                f"photon_arch {rpm_arch}",
                str(INPUT / "photon-commander.spec"),
                env=environment,
            )
            built_rpms = list((topdir / "RPMS" / rpm_arch).glob("*.rpm"))
            if len(built_rpms) != 1:
                raise SystemExit(f"build-linux: expected one RPM, found {len(built_rpms)}")
            rpm = output_dir / built_rpms[0].name
            shutil.copy2(built_rpms[0], rpm)
    checksum(deb)
    if rpm is not None:
        checksum(rpm)
    return deb, rpm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="exact full release commit")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/linux")
    parser.add_argument("--formats", choices=("both", "deb"), default="both",
                        help="build both DEB and RPM (default), or DEB only")
    parser.add_argument("--unsigned", action="store_true", help="acknowledge unsigned artifacts")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.unsigned:
        raise SystemExit("build-linux: explicitly pass --unsigned for unsigned artifacts")
    deb, rpm = build_packages(args.commit, args.output_dir, formats=args.formats)
    print(f"build-linux: DEB artifact {deb}")
    if rpm is not None:
        print(f"build-linux: RPM artifact {rpm}")


if __name__ == "__main__":
    main()
