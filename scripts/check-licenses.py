#!/usr/bin/env python3
"""Dependency inventory and source-release policy gate.

Runs ``pip-licenses`` for the Python side, diffs the result against
``packaging/licenses-allowlist.toml``, and — if ``web/package.json`` exists —
does the JS-side equivalent with ``license-checker-rseidelsohn``. Any of the
following fails the build:

* a package present in the resolved environment but absent from the allowlist
  (a new dependency, direct or transitive, that nobody has looked at yet)
* a package whose reported license text no longer matches the allowlist
  (the same dependency changed license on an update)

``--commercial`` additionally verifies the project licence, required notices,
survey exclusions and source package boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = ROOT / "packaging" / "licenses-allowlist.toml"


def _load_allowlist() -> dict[str, dict[str, str]]:
    with ALLOWLIST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    packages: dict[str, dict[str, str]] = data.get("packages", {})
    return packages


def _load_js_policy() -> tuple[set[str], dict[str, dict[str, str]]]:
    """Allowed SPDX identifiers, plus per-package exceptions.

    The JS tree is allow-by-license rather than allow-by-package: a per-package
    list would be hundreds of entries of pure noise, and noise is how a gate
    gets disabled. Anything outside the permissive set still needs an explicit,
    noted exception.
    """
    with ALLOWLIST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    js = data.get("javascript", {})
    allowed = {str(x) for x in js.get("allowed_licenses", [])}
    exceptions: dict[str, dict[str, str]] = js.get("packages", {})
    return allowed, exceptions


def _run_pip_licenses() -> list[dict[str, str]]:
    uv = shutil.which("uv")
    if uv is None:
        print("check-licenses: uv not found on PATH", file=sys.stderr)
        raise SystemExit(1)
    result = subprocess.run(
        [uv, "run", "--with", "pip-licenses", "pip-licenses", "--format=json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("check-licenses: pip-licenses failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(1)
    entries: list[dict[str, str]] = json.loads(result.stdout)
    return entries


def _check_python(allowlist: dict[str, dict[str, str]]) -> list[str]:
    failures: list[str] = []
    for entry in _run_pip_licenses():
        name = entry["Name"]
        license_text = entry["License"]
        allowed = allowlist.get(name)
        if allowed is None:
            failures.append(
                f"{name}: not in the allowlist (reports license {license_text!r}). "
                f"Add it to {ALLOWLIST_PATH.relative_to(ROOT)} with status "
                f'"ok" (permissive) or "reviewed" (copyleft/no-license, with a note).'
            )
            continue
        if allowed["license"] != license_text:
            failures.append(
                f"{name}: license changed from {allowed['license']!r} (allowlisted) "
                f"to {license_text!r} (resolved). Review and update the allowlist entry."
            )
    return failures


def _check_javascript() -> list[str]:
    package_json = ROOT / "web" / "package.json"
    if not package_json.is_file():
        print("check-licenses: web/package.json does not exist yet — skipping JS inventory.")
        return []
    npx = shutil.which("npx")
    if npx is None:
        return [
            "web/package.json exists but npx is unavailable — the JS license "
            "inventory cannot run. This must not silently pass once web/ ships."
        ]
    # license-checker-rseidelsohn is the actively maintained fork of the
    # now-archived license-checker; it needs no config to emit JSON.
    result = subprocess.run(
        [npx, "--yes", "license-checker-rseidelsohn", "--json", "--production"],
        cwd=ROOT / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [f"license-checker-rseidelsohn failed:\n{result.stderr}"]

    allowed, exceptions = _load_js_policy()
    if not allowed:
        return [
            "no [javascript].allowed_licenses in the allowlist — the JS gate "
            "would pass everything. Configure it rather than leaving it open."
        ]

    try:
        inventory: dict[str, dict[str, object]] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [f"could not parse the JS license inventory: {exc}"]

    root_name = _js_root_name()
    failures: list[str] = []
    for spec, meta in sorted(inventory.items()):
        name = spec.rsplit("@", 1)[0]
        if name == root_name:
            continue  # our own package; it is the thing being licensed
        raw = meta.get("licenses", "")
        text = raw if isinstance(raw, str) else "/".join(str(x) for x in raw)
        exception = exceptions.get(name)
        if exception is not None:
            if exception.get("license") != text:
                failures.append(
                    f"{name}: license changed from {exception.get('license')!r} "
                    f"(excepted) to {text!r} (resolved). Re-review the exception."
                )
            continue
        if text not in allowed:
            failures.append(
                f"{name}: license {text!r} is not in [javascript].allowed_licenses. "
                f"Add the identifier if it is permissive, or add a "
                f'[javascript.packages."{name}"] exception with a note.'
            )
    if failures:
        return failures
    print(
        f"check-licenses: JS inventory checked "
        f"({len(inventory)} packages, all licenses allowlisted)."
    )
    return []


def _js_root_name() -> str:
    with (ROOT / "web" / "package.json").open() as fh:
        return str(json.load(fh).get("name", ""))


def _unresolved_exceptions() -> list[tuple[str, str]]:
    """Allowlist entries whose review did not actually settle the question.

    An exception carries a `status`, and `reviewed` is meant to mean a human
    looked and concluded. `unresolved` means the opposite: the package is
    allowed through so work can continue, but the licence question is open and
    must be closed before release. Without this, `status` is a comment nobody
    reads and an open question disappears into a green gate.
    """
    with ALLOWLIST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    unresolved: list[tuple[str, str]] = []
    for section in ("packages", "javascript"):
        block = data.get(section, {})
        packages = block.get("packages", block) if section == "javascript" else block
        for name, entry in packages.items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status", "")).lower() == "unresolved":
                first_line = str(entry.get("note", "")).strip().splitlines()
                unresolved.append((name, first_line[0] if first_line else "no note"))
    return unresolved


def _check_commercial_product() -> list[str]:
    """Mechanical guardrails for the source release boundary."""
    failures: list[str] = []
    with (ROOT / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    if project.get("license") != "GPL-3.0-only":
        failures.append("pyproject.toml must declare GPL-3.0-only")
    package = json.loads((ROOT / "web" / "package.json").read_text())
    if package.get("license") != "GPL-3.0-only" or package.get("private") is not True:
        failures.append("web/package.json must remain private and GPL-3.0-only")

    for required in (
        ROOT / "LICENSE",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "packaging" / "third-party-licenses" / "LGPL-2.1-zeroconf.txt",
        ROOT / "packaging" / "third-party-licenses" / "MPL-2.0-certifi.txt",
        ROOT / "packaging" / "third-party-sources" / "zeroconf-0.150.0.tar.gz",
        ROOT / "packaging" / "third-party-sources" / "certifi-2026.7.22.tar.gz",
    ):
        if not required.is_file():
            failures.append(f"required commercial notice is missing: {required.relative_to(ROOT)}")

    source_hashes = {
        "zeroconf-0.150.0.tar.gz": "a5fe7feab1de6ef5e541e0a3d07e534fd91629b813fc27281593584100f63164",
        "certifi-2026.7.22.tar.gz": "741e2c3b351ddf169a738da9f2c048608ff7f2c5cc02f1ebc6b118bb090d5d55",
    }
    for package_name in ("zeroconf", "certifi"):
        expected_prefix = f"{package_name}-{version(package_name)}.tar.gz"
        if expected_prefix not in source_hashes:
            failures.append(
                f"source archive is not pinned for resolved {package_name}=={version(package_name)}"
            )
    for filename, expected in source_hashes.items():
        path = ROOT / "packaging" / "third-party-sources" / filename
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            failures.append(f"third-party source archive hash changed: {path.relative_to(ROOT)}")

    product_files = [
        *sorted((ROOT / "ssc").rglob("*.py")),
        *sorted((ROOT / "web" / "src").rglob("*.ts")),
        *sorted((ROOT / "web" / "src").rglob("*.tsx")),
        *sorted((ROOT / "web" / "src").rglob("*.css")),
        ROOT / "packaging" / "ssc-core.spec",
    ]
    forbidden = ("CDS/P/DSS2", "DSS2_COLOR", "alasky.cds.unistra.fr/hips-image-services")
    for path in product_files:
        text = path.read_text(errors="replace")
        for needle in forbidden:
            if needle in text:
                failures.append(
                    f"{path.relative_to(ROOT)} contains forbidden survey path {needle!r}"
                )
        if "SPDX-License-Identifier: GPL-3.0-or-later" in text:
            failures.append(f"{path.relative_to(ROOT)} still declares the old project licence")

    # The exact retired id is retained only for its one-way purge. The
    # consent-gated personal-use imagery deliberately uses a distinct, explicit id.
    allowed_retired_id_files = {
        ROOT / "ssc" / "skymap" / "cutouts.py",
        ROOT / "ssc" / "store" / "migrations" / "0015_remove_restricted_survey_cache.sql",
    }
    for path in [*product_files, *sorted((ROOT / "ssc" / "store" / "migrations").glob("*.sql"))]:
        text = path.read_text(errors="replace")
        if re.search(r"dss2-color(?!-hips-poc)", text) and path not in allowed_retired_id_files:
            failures.append(f"{path.relative_to(ROOT)} still contains the retired survey id")

    from ssc.skymap.surveys import REGISTRY

    remote_poc_ids = {"dss2-color-hips-poc"}
    for survey in REGISTRY:
        if survey.enabled and not survey.production_cleared and survey.id not in remote_poc_ids:
            failures.append(f"{survey.id} is enabled without commercial clearance")
        if survey.id in remote_poc_ids and (
            survey.production_cleared or survey.bundle_allowed or not survey.rights_notice
        ):
            failures.append(
                f"{survey.id} personal-use imagery must remain uncleared, unbundled, and carry a rights notice"
            )
        if survey.id.startswith("nsns-") and survey.enabled:
            failures.append(f"{survey.id} is enabled without recorded commercial permission")
    frontend_surveys = (ROOT / "web" / "src" / "skymap" / "surveys.ts").read_text()
    if 'DEFAULT_BACKGROUND_SURVEY_ID = "dss2-color-hips-poc"' not in frontend_surveys:
        failures.append("default DSS background must remain consent-gated and unbundled")

    spec = (ROOT / "packaging" / "ssc-core.spec").read_text()
    build_script = (ROOT / "scripts" / "build-package.sh").read_text()
    if 'data" / "hips"' in spec:
        failures.append("survey data packs must not be bundled without recorded redistribution rights")
    for notice in ("THIRD_PARTY_NOTICES.md", "third-party-licenses", "third-party-sources"):
        if notice not in build_script:
            failures.append(f"packaging build does not ship {notice} beside the executable")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commercial", action="store_true")
    args = parser.parse_args()
    allowlist = _load_allowlist()
    failures = _check_python(allowlist)
    failures += _check_javascript()
    if args.commercial:
        failures += _check_commercial_product()

    if failures:
        print("check-licenses: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    unresolved = _unresolved_exceptions()
    if unresolved:
        # Deliberately NOT a failure: these are known, recorded, and blocking
        # release rather than development. Deliberately loud, and on stderr, so
        # it cannot be read as a clean gate. Flip this to `return 1` for the
        # release build.
        print("check-licenses: PASS, BUT WITH UNRESOLVED LICENCES", file=sys.stderr)
        for name, note in unresolved:
            print(f"  - {name}: {note}", file=sys.stderr)
        print("  These must be settled before the product is sold.", file=sys.stderr)
        return 0

    if args.commercial:
        print(
            "check-licenses: WARNING — DSS personal-use source is available "
            "only after user consent; commercial-use permission remains unresolved and no "
            "DSS pixels are bundled.",
            file=sys.stderr,
        )
        print(
            "check-licenses: commercial distribution boundary verified with explicit "
            "DSS consent-gated remote exception."
        )
    print("check-licenses: PASS — all resolved licenses are allowlisted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
