#!/usr/bin/env python3
"""Build a native standalone Photon Commander backend with Nuitka."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from scripts.build_release_identity import release_version
except ModuleNotFoundError:
    from build_release_identity import release_version

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "packaging" / "entrypoint.py"
BUILD_ROOT = ROOT / "build" / "nuitka"
DEFAULT_OUTPUT = ROOT / "dist" / "ssc-core-nuitka"

REQUIRED_FILES = (
    Path("data/ephemeris/de440s.bsp"),
)
DATA_DIRECTORIES = (
    Path("ssc/store/migrations"),
    Path("data/catalog"),
    Path("data/stars"),
    Path("data/constellations"),
    Path("data/comets"),
    Path("data/profiles"),
)
FORBIDDEN_SUFFIXES = {".py", ".pyi", ".pyc", ".map", ".ts", ".tsx", ".jsx"}


def version() -> str:
    commit = os.environ.get("SSC_BUILD_COMMIT")
    if commit:
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise SystemExit("build-nuitka: invalid immutable build commit")
        return release_version(ROOT, commit)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not re.fullmatch(r"[a-f0-9]{40}", result.stdout.strip()):
        raise SystemExit("build-nuitka: immutable build identity unavailable")
    return release_version(ROOT, result.stdout.strip())


def validate_inputs() -> None:
    missing = [path for path in REQUIRED_FILES if not (ROOT / path).is_file()]
    missing.extend(path for path in DATA_DIRECTORIES if not (ROOT / path).is_dir())
    if not any((ROOT / "ssc/store/migrations").glob("*.sql")):
        missing.append(Path("ssc/store/migrations/*.sql"))
    if missing:
        formatted = "\n  ".join(str(path) for path in missing)
        raise SystemExit(f"build-nuitka: required runtime assets are missing:\n  {formatted}")


def build_jobs() -> int:
    explicit = os.environ.get("SSC_BUILD_JOBS")
    if explicit is not None:
        if not re.fullmatch(r"[1-9][0-9]*", explicit):
            raise SystemExit("build-nuitka: SSC_BUILD_JOBS must be a positive integer")
        return int(explicit)
    if os.name != "posix" or platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        return 2
    try:
        available_kib = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                             if line.startswith("MemAvailable:"))
    except (OSError, ValueError, StopIteration):
        return 2
    # Observed largest GCC workers used ~800 MiB. Budget 1 GiB per worker
    # and retain 2 GiB for the OS, backend and compiler coordination.
    return max(1, min(os.cpu_count() or 1, 4, (available_kib // 1024 - 2048) // 1024))


def compiler_environment() -> dict[str, str]:
    env = os.environ.copy()
    appliance = Path(env.get("SSC_APPLIANCE_ROOT", "/mnt/photon/Photon"))
    binary = appliance / "tools/build-cache/bin/ccache"
    cache = env.get("SSC_BUILD_CACHE_DIR")
    if cache is None and binary.is_file():
        cache = str(appliance / "shared/cache/nuitka")
    if cache:
        env.setdefault("NUITKA_CACHE_DIR", cache)
        env.setdefault("NUITKA_CACHE_DIR_CCACHE", env.get("CCACHE_DIR", str(Path(env["NUITKA_CACHE_DIR"]) / "ccache")))
        env.setdefault("CCACHE_DIR", env["NUITKA_CACHE_DIR_CCACHE"])
        env.setdefault("CCACHE_BASEDIR", str(ROOT))
        env.setdefault("CCACHE_COMPILERCHECK", "content")
        if binary.is_file():
            env.setdefault("NUITKA_CCACHE_BINARY", str(binary))
    return env


def nuitka_command() -> list[str]:
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=standalone",
        f"--output-dir={BUILD_ROOT}",
        f"--output-filename={executable_name()}",
        "--remove-output",
        "--update-check=never",
        "--assume-yes-for-downloads",
        f"--jobs={build_jobs()}",
        f"--report={BUILD_ROOT.parent / 'nuitka-report.xml'}",
        "--include-package=ssc",
        "--include-package=keyring.backends",
        "--include-package-data=skyfield",
        "--noinclude-data-files=skyfield/tests/*",
        "--nofollow-import-to=*.tests",
    ]
    diagnostic = os.environ.get("SSC_BUILD_DIAGNOSTIC", "0")
    if diagnostic not in {"0", "1"}:
        raise SystemExit("build-nuitka: diagnostic mode must be 0 or 1")
    if diagnostic == "1":
        command.extend(["--debug", "--no-debug-c-warnings"])
    command.extend(
        f"--include-data-dir={ROOT / source}={source.as_posix()}" for source in DATA_DIRECTORIES
    )
    if os.name == "nt":
        command.extend(["--include-package-data=tzdata", "--mingw64"])
    command.append(
        f"--include-data-files={ROOT / 'data/ephemeris/de440s.bsp'}=data/ephemeris/de440s.bsp"
    )
    command.append(str(ENTRYPOINT))
    return command


def assert_compiled_boundary(output: Path) -> None:
    forbidden = [
        path.relative_to(output)
        for path in output.rglob("*")
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES
    ]
    if forbidden:
        formatted = "\n  ".join(str(path) for path in sorted(forbidden))
        raise SystemExit(f"build-nuitka: compiled artifact contains source files:\n  {formatted}")


def copy_legal_material(output: Path, build_version: str) -> None:
    (output / "VERSION").write_text(f"{build_version}\n", encoding="utf-8")
    commit = os.environ.get("SSC_BUILD_COMMIT") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
    ).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise SystemExit("build-nuitka: invalid full release identity")
    (output / "COMMIT").write_text(commit + "\n", encoding="utf-8")
    for filename in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / filename, output / filename)
    for directory in ("third-party-licenses", "third-party-sources"):
        shutil.copytree(ROOT / "packaging" / directory, output / directory)


def build(output: Path, *, private_appliance: bool, skip_license_check: bool) -> Path:
    validate_inputs()
    if not skip_license_check:
        license_command = [sys.executable, "scripts/check-licenses.py"]
        if not private_appliance:
            license_command.append("--commercial")
        subprocess.run(license_command, cwd=ROOT, check=True)

    build_version = version()
    shutil.rmtree(BUILD_ROOT, ignore_errors=True)
    subprocess.run(nuitka_command(), cwd=ROOT, env=compiler_environment(), check=True)

    generated = BUILD_ROOT / "entrypoint.dist"
    if not generated.is_dir() or not (generated / executable_name()).is_file():
        raise SystemExit(
            f"build-nuitka: Nuitka did not produce the expected artifact at {generated}"
        )
    shutil.rmtree(output, ignore_errors=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(generated), output)
    copy_legal_material(output, build_version)
    assert_compiled_boundary(output)
    return output


def executable_name() -> str:
    return "ssc-core.exe" if os.name == "nt" else "ssc-core"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--private-appliance",
        action="store_true",
        help="allow owner-only survey sources; the result is not redistributable",
    )
    parser.add_argument(
        "--skip-license-check",
        action="store_true",
        help="development-only: skip the dependency and redistribution policy gate",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the compiler command")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.dry_run:
        print(subprocess.list2cmdline(nuitka_command()))
        return
    artifact = build(
        args.output.resolve(),
        private_appliance=args.private_appliance,
        skip_license_check=args.skip_license_check,
    )
    print(f"build-nuitka: artifact at {artifact / executable_name()}")


if __name__ == "__main__":
    main()
