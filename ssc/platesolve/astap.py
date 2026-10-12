# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Detect a system-installed ASTAP CLI, invoke it on a FITS, parse its result.

``THIRD_PARTY_NOTICES.md`` "Plate solver — gate reading of 2026-09-10": ASTAP's
code is MPL-2.0 (PASS), but permission to redistribute its Gaia DR3 star
database is undocumented — so SSC **never ships astap_cli and never ships a
database**. This module only ever drives a copy the machine's owner installed
themselves (the same relationship N.I.N.A. has with it), and is honest when
that copy, or its database, is not there.

**Every fact this module relies on about astap_cli's command line, exit codes
and output format was read from Han Kleijn's own source, fetched live on
2026-09-10** (LGPL3+ per its own `--help` banner; the gate reading found the
shipped `LICENSE` file and source headers to be MPL-2.0 Exhibit-A-only — that
finding is not re-litigated here):
`github.com/ortegaalfredo/astap-command-line` — `astap_command_line.lpr`
(argument parsing, help banner, exit codes) and
`unit_command_line_general.pas` (`write_ini`, `update_*` — the `.ini` field
names and format) at commit reachable from `main` that day. No detail below
was invented from memory or from a forum post describing behaviour secondhand.

**Exit codes** (`astap_command_line.lpr`, verbatim comment block):
``0`` solved, ``1`` no solution, ``2`` not enough stars, ``16`` could not read
the image, ``32`` no star database found, ``33`` error reading the star
database. All other values (documented elsewhere as up to ``255`` for a
command-line error) are not given a specific reading here — refused rather
than guessed at.

**The `.ini` file** (`write_ini`) is always written, solved or not, next to
whatever `-o` names (`ChangeFileExt(filename2, '.ini')`). On success it has
`PLTSOLVD=T` plus `CRVAL1`/`CRVAL2` (the reference pixel's RA/Dec **in
degrees, J2000** — the source comment says so explicitly), `CDELT1`/`CDELT2`
(deg/pixel), `CROTA1`/`CROTA2` (twist, deg) and a CD matrix; on failure just
`PLTSOLVD=F`. Both carry `CMDLINE=` and, keyed by the exit code, an
`ERROR=...` line (`Not enough stars.` / `Error reading image file.` / `No
star database found.` / `Error reading star database.`); a `WARNING=...` line
can appear on either. This module treats the `.ini` as the single source of
truth for the solution — never the `-wcs`/`-update` outputs, which this
module does not request.

**Database files.** `unit_command_line_star_database.pas` names its per-tile
files literally `NNNN.1476` (5.14x5.14 deg cells) and an older `NNNN.290`
series — the presence check below looks for exactly those, not a guessed
extension. Default search order, mirrored from `astap_command_line.lpr`'s
`Tastap.DoRun`: the directory the binary itself lives in, and — only when
that directory's path starts with `/usr` — `/usr/share/astap/data/` instead
(the Debian/Arch package layout). `-d` overrides both.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ssc.astro.frames import j2000_coord
from ssc.astro.units import Deg
from ssc.platesolve.base import (
    InvalidPlateSolveInputError,
    MalformedSolverOutputError,
    PlateSolveError,
    PlateSolveResult,
    SolveFailedError,
    SolveIssue,
    SolverAvailability,
    SolverUnavailableError,
    SolveTimedOutError,
)

__all__ = [
    "ASTAP_DEFAULT_SOLVE_TIMEOUT_S",
    "ASTAP_DEFAULT_VERSION_PROBE_TIMEOUT_S",
    "astap_availability",
    "astap_solve",
]

_log = logging.getLogger(__name__)

#: Names astap_cli's own docs and packaging use. The GUI binary works
#: identically "if renamed" (astap.htm) — SSC never renames anything, it only
#: looks for names a package or a manual install would actually produce.
_CANDIDATE_NAMES: Final = ("astap_cli", "astap")

#: Install locations seen in ASTAP's own docs/packaging beyond plain PATH:
#: the Debian/Arch package path, and the opt-style path some distro packages
#: and the macOS build use. Checked only after PATH comes up empty.
_FALLBACK_DIRS: Final = (Path("/opt/astap"), Path("/usr/share/astap"), Path("/usr/local/bin"))

#: Debian/Arch's astap-cli package layout (astap_command_line.lpr: "if
#: copy(database_path,1,4)='/usr' then database_path:='/usr/share/astap/data/'").
_USR_SHARE_DATABASE_DIR: Final = Path("/usr/share/astap/data")

#: The official D20 Debian package used on Raspberry Pi installs all tiles
#: directly in ``/opt/astap`` while putting ``astap_cli`` in ``/usr/bin``.
#: ASTAP does not discover that split layout by itself, so SSC must both
#: detect the directory and pass it with ``-d`` when solving.
_OPT_DATABASE_DIR: Final = Path("/opt/astap")

#: Database tile extensions, read verbatim from `filenames1476` in
#: unit_command_line_star_database.pas (".1476") and the module comment
#: naming the older wide-cell series (".290"). Not a guess.
#:
#: ".001" was added 2026-09-10 after installing W08, the wide-field database
#: the S30 Pro's 32x54 deg wide camera needs: its single tile ships as
#: `w08_0101.001`, so an install carrying only W08 would have been reported
#: as "no star database" while astap_cli solved with it perfectly well.
_DATABASE_TILE_GLOBS: Final = ("*.1476", "*.290", "*.001")

#: astap_cli's own `--help`/no-args banner. Upstream source builds it as
#: `'ASTAP astrometric solver, '+version` (astap_command_line.lpr), but the
#: shipped CLI-2026.09.01 binary prints `ASTAP astrometric solver version
#: CLI-2026.09.01` — no comma, an extra `version` word. Verified by running
#: the real binary on 2026-09-10; both spellings are accepted because a
#: version probe that rejects a working ASTAP is worse than one that is loose.
_VERSION_RE: Final = re.compile(
    r"ASTAP astrometric solver(?:,)?\s*(?:version\s+)?(\S+)"
)

ASTAP_DEFAULT_VERSION_PROBE_TIMEOUT_S: Final = 5.0
ASTAP_DEFAULT_SOLVE_TIMEOUT_S: Final = 30.0

#: astap_command_line.lpr's own exit-code comment block, verbatim reading.
_ERROR_DETAIL_BY_EXIT_CODE: Final[Mapping[int, str]] = {
    1: "astap_cli found no solution for this field",
    2: "astap_cli detected too few stars to attempt a solve",
    16: "astap_cli could not read the image file",
    32: "astap_cli found no star database",
    33: "astap_cli could not read its star database",
}

_REQUIRED_SOLUTION_KEYS: Final = ("CRVAL1", "CRVAL2", "CDELT1", "CDELT2", "CROTA2")

_CARD_SIZE: Final = 80
_BLOCK_SIZE: Final = 2880
#: 20 blocks = 57,600 bytes. Every FITS header SSC itself writes
#: (`ssc.capture.fits`) is a fraction of this; a header that runs longer is
#: refused rather than read further, per this module's "refuse, don't guess"
#: rule for untrusted-adjacent input.
_MAX_HEADER_BLOCKS: Final = 20


def _resolve_cli(env: Mapping[str, str]) -> Path | None:
    """The astap_cli binary to run, or ``None``. Search order: an explicit
    ``SSC_ASTAP_CLI`` override, then ``$PATH``, then a short list of known
    install locations. Never a hardcoded default path used unconditionally
    (CLAUDE.md invariant 11's spirit: nothing here is assumed present)."""
    override = env.get("SSC_ASTAP_CLI")
    if override:
        path = Path(override)
        return path if path.is_file() and os.access(path, os.X_OK) else None
    for name in _CANDIDATE_NAMES:
        # Search the caller's PATH, not the process's. `shutil.which(name)`
        # silently reads os.environ, which made an injected env's PATH a lie
        # and let a solver installed on the dev host leak into a test that
        # had explicitly asked for an empty PATH.
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return Path(found)
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        # dict(os.environ) uppercases Windows keys and loses the original
        # mapping's case-insensitive lookup. Keep PATH/explicit overrides as
        # supplied; accept both forms only for these Windows install roots.
        if root := env.get(key) or env.get(key.upper()):
            for name in ("astap_cli.exe", "astap.exe"):
                candidate = Path(root) / "astap" / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate
    for directory in _FALLBACK_DIRS:
        for name in _CANDIDATE_NAMES:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _default_database_dirs(cli_path: Path) -> list[Path]:
    bindir = cli_path.resolve().parent
    dirs = [bindir]
    if str(bindir).startswith("/usr"):
        dirs.append(_USR_SHARE_DATABASE_DIR)
    if _OPT_DATABASE_DIR not in dirs:
        dirs.append(_OPT_DATABASE_DIR)
    return dirs


def resolve_astap_cli(env: Mapping[str, str]) -> Path | None:
    """Expose the same host detection used by availability and actual solves."""
    return _resolve_cli(env)


def _has_star_database(directory: Path) -> bool:
    """A database tile exists directly in ``directory`` or one level below
    it (ASTAP's GUI installs each series into its own subfolder, e.g.
    ``d50/``). Bounded to two levels deliberately — this only ever runs
    against a handful of candidate directories, never an arbitrary tree."""
    if not directory.is_dir():
        return False
    for pattern in _DATABASE_TILE_GLOBS:
        if next(directory.glob(pattern), None) is not None:
            return True
        if next(directory.glob(f"*/{pattern}"), None) is not None:
            return True
    return False


def resolve_database_dir(cli_path: Path, *, env: Mapping[str, str]) -> Path | None:
    """The first candidate directory that actually has a star database in
    it, or ``None`` if none does. ``SSC_ASTAP_DATA_DIR`` overrides the search
    entirely, mirroring astap_cli's own ``-d``."""
    override = env.get("SSC_ASTAP_DATA_DIR")
    candidates = [Path(override)] if override else _default_database_dirs(cli_path)
    for candidate in candidates:
        if _has_star_database(candidate):
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class _VersionProbe:
    version: str | None
    timed_out: bool


async def _probe_version(cli_path: Path, *, timeout_s: float) -> _VersionProbe:
    """Run ``astap_cli -h``, which prints the version banner and exits
    without touching any image or database (astap_command_line.lpr: the
    ``hasOption('h','help')`` branch prints usage and terminates). Timed out
    on purpose — a renamed GUI build with no console can hang waiting for a
    display it will never get, and this module must never hang detecting
    that (CLAUDE.md: a stuck client retry loop / hung subprocess must not be
    allowed to sit forever)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(cli_path),
            "-h",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return _VersionProbe(version=None, timed_out=False)
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return _VersionProbe(version=None, timed_out=True)
    match = _VERSION_RE.search(stdout.decode("utf-8", errors="replace"))
    return _VersionProbe(version=match.group(1) if match else None, timed_out=False)


async def astap_availability(
    *,
    env: Mapping[str, str] | None = None,
    version_probe_timeout_s: float = ASTAP_DEFAULT_VERSION_PROBE_TIMEOUT_S,
) -> SolverAvailability:
    """Whether ASTAP can be used right now, and precisely why not.

    Three distinguishable negative outcomes (module docstring / CLAUDE.md
    invariant 7): no binary found, a binary found that does not identify as
    astap_cli, and a recognised astap_cli with no usable star database.
    Never raises — this is itself the "tell the user what to do" surface.
    """
    resolved_env = env if env is not None else os.environ
    cli_path = await asyncio.to_thread(_resolve_cli, resolved_env)
    if cli_path is None:
        override = resolved_env.get("SSC_ASTAP_CLI")
        if override:
            reason = (
                f"SSC_ASTAP_CLI is set to {override!r}, but that is not an executable "
                "file. Fix the path, or unset SSC_ASTAP_CLI to search $PATH instead."
            )
        else:
            names = " or ".join(_CANDIDATE_NAMES)
            reason = (
                f"ASTAP's command-line solver ({names}) was not found on $PATH or in its "
                "usual install locations. Install ASTAP from www.hnsky.org (or your distro's "
                "astap-cli package) and make sure it is on $PATH, or set SSC_ASTAP_CLI to its "
                "full path."
            )
        return SolverAvailability(available=False, issue=SolveIssue.SOLVER_NOT_FOUND, reason=reason)

    probe = await _probe_version(cli_path, timeout_s=version_probe_timeout_s)
    if probe.timed_out:
        return SolverAvailability(
            available=False,
            issue=SolveIssue.UNSUPPORTED_VERSION,
            reason=(
                f"{cli_path} did not respond to '-h' within {version_probe_timeout_s:.0f}s. "
                "It may be ASTAP's GUI build without console support rather than astap_cli; "
                "point SSC_ASTAP_CLI at the command-line build."
            ),
        )
    if probe.version is None:
        return SolverAvailability(
            available=False,
            issue=SolveIssue.UNSUPPORTED_VERSION,
            reason=(
                f"{cli_path} did not print an 'ASTAP astrometric solver, CLI-<version>' "
                "banner for '-h'; it does not look like ASTAP's command-line solver."
            ),
        )

    db_dir = await asyncio.to_thread(resolve_database_dir, cli_path, env=resolved_env)
    if db_dir is None:
        override_note = (
            f" (SSC_ASTAP_DATA_DIR={resolved_env['SSC_ASTAP_DATA_DIR']!r})"
            if resolved_env.get("SSC_ASTAP_DATA_DIR")
            else ""
        )
        reason = (
            f"ASTAP {probe.version} was found at {cli_path}, but no star database tile "
            f"(*.1476/*.290/*.001) was found near it{override_note}. Install D20 (~0.4 GB, covers "
            "0.3-10 deg, so the S30 Pro's 2.24x3.99 deg main field) and W08 for the 32x54 deg "
            "wide camera, next to astap_cli or in /opt/astap, or set SSC_ASTAP_DATA_DIR."
        )
        return SolverAvailability(
            available=False, issue=SolveIssue.DATABASE_UNAVAILABLE, reason=reason, version=probe.version
        )

    return SolverAvailability(available=True, version=probe.version)


def _card_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("'"):
        end = text.find("'", 1)
        return text[1:end] if end != -1 else text[1:]
    return text.split("/", 1)[0].strip()


def _read_fits_header_cards(fits_path: Path) -> dict[str, str]:
    """Just enough of a FITS header to read one card (``DATE-OBS``) back —
    deliberately not a general reader; ``ssc.capture.fits`` is the writer
    this is reading, and its card format (keyword padded to 8, ``= ``, value
    padded to 80) is the only shape trusted here."""
    cards: dict[str, str] = {}
    with fits_path.open("rb") as handle:
        for _ in range(_MAX_HEADER_BLOCKS):
            block = handle.read(_BLOCK_SIZE)
            if len(block) < _BLOCK_SIZE:
                raise InvalidPlateSolveInputError(
                    SolveIssue.INVALID_INPUT, f"{fits_path} is truncated or not a FITS file"
                )
            for offset in range(0, _BLOCK_SIZE, _CARD_SIZE):
                card = block[offset : offset + _CARD_SIZE].decode("ascii", errors="replace")
                key = card[:8].strip()
                if key == "END":
                    return cards
                if key and card[8:10] == "= ":
                    cards[key] = _card_text(card[10:])
        raise InvalidPlateSolveInputError(
            SolveIssue.INVALID_INPUT,
            f"{fits_path}'s header has no END card in the first "
            f"{_MAX_HEADER_BLOCKS * _BLOCK_SIZE} bytes",
        )
    raise InvalidPlateSolveInputError(SolveIssue.INVALID_INPUT, f"{fits_path} could not be read")


def _read_date_obs(fits_path: Path) -> datetime:
    """``DATE-OBS`` from the frame that will be solved — the timestamp every
    :class:`~ssc.platesolve.base.PlateSolveResult` carries (module docstring
    / CLAUDE.md invariant 5). A *missing* card is refused, not defaulted to
    "now".

    A card with no timezone is read as **UTC**, because that is what the FITS
    standard says it is: FITS 4.0 section 4.4.2.1 defines DATE-OBS in UTC for
    modern data, and writers omit the zone rather than write "+00:00" — the
    zone is absent because it is implied, not because it is unknown. Refusing
    it was over-applying invariant 5, which governs SSC's own storage and
    interfaces, not its reading of a format whose default is already UTC.

    This was refusing every real frame until 2026-09-10, when it rejected all
    five of the owner's own Seestar files. Their filenames carry local time
    and are uniformly +2h against DATE-OBS at a UTC+2 site, which confirms
    the card is UTC by measurement and not only by specification."""
    if not fits_path.is_file():
        raise InvalidPlateSolveInputError(SolveIssue.INVALID_INPUT, f"{fits_path} does not exist")
    cards = _read_fits_header_cards(fits_path)
    raw = cards.get("DATE-OBS")
    if raw is None:
        raise InvalidPlateSolveInputError(
            SolveIssue.INVALID_INPUT,
            f"{fits_path.name} has no DATE-OBS card; a plate solve result must carry the "
            "exposure's own timestamp, and this seam will not substitute the solve's "
            "completion time for it",
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidPlateSolveInputError(
            SolveIssue.INVALID_INPUT, f"{fits_path.name}'s DATE-OBS {raw!r} is not a valid ISO datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_ini(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="ascii", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def _result_from_ini(
    ini: Mapping[str, str], *, date_obs: datetime, solver_version: str | None
) -> PlateSolveResult:
    if ini.get("PLTSOLVD") != "T":
        raise MalformedSolverOutputError(
            SolveIssue.MALFORMED_OUTPUT,
            "astap_cli exited 0 (solved) but its .ini file does not say PLTSOLVD=T",
        )
    missing = [key for key in _REQUIRED_SOLUTION_KEYS if key not in ini]
    if missing:
        raise MalformedSolverOutputError(
            SolveIssue.MALFORMED_OUTPUT, f"astap_cli's .ini is missing required keys: {missing}"
        )
    try:
        ra_deg = float(ini["CRVAL1"])
        dec_deg = float(ini["CRVAL2"])
        cdelt2 = float(ini["CDELT2"])
        crota2 = float(ini["CROTA2"])
    except ValueError as exc:
        raise MalformedSolverOutputError(
            SolveIssue.MALFORMED_OUTPUT, f"astap_cli's .ini has a non-numeric solution value: {exc}"
        ) from exc

    try:
        # CRVAL1/CRVAL2 are "RA/DEC of reference pixel [deg]" per the source
        # comment quoted in the module docstring — astap_cli's own catalogue
        # match, in the catalogue's frame, i.e. J2000. Tagged explicitly so a
        # caller can never mistake it for the mount's topocentric frame.
        coord = j2000_coord(Deg(ra_deg), Deg(dec_deg))
    except ValueError as exc:
        raise MalformedSolverOutputError(
            SolveIssue.MALFORMED_OUTPUT, f"astap_cli returned an invalid coordinate: {exc}"
        ) from exc

    return PlateSolveResult(
        coord=coord,
        date_obs=date_obs,
        pixel_scale_arcsec=abs(cdelt2) * 3600.0,
        rotation_deg=crota2,
        solver="astap",
        solver_version=solver_version,
    )


async def astap_solve(
    fits_path: Path,
    *,
    cli_path: Path | None = None,
    solver_version: str | None = None,
    timeout_s: float = ASTAP_DEFAULT_SOLVE_TIMEOUT_S,
    position_hint: tuple[float, float] | None = None,
    field_height_deg: float | None = None,
    env: Mapping[str, str] | None = None,
) -> PlateSolveResult:
    """Solve ``fits_path`` with ASTAP and return the position, or raise.

    ``cli_path``/``solver_version`` are normally the ones a prior
    :func:`astap_availability` call already found — passed in rather than
    re-probed, so a solve never spends a second subprocess re-confirming
    what is already known (CLAUDE.md invariant 7: read once). If omitted,
    this call resolves the binary itself (and the result simply has no
    ``solver_version``).

    Never blind-parses: a non-zero exit is mapped to the specific ASTAP
    reading in :data:`_ERROR_DETAIL_BY_EXIT_CODE` where one exists and
    refused outright otherwise; an exit-0 ``.ini`` missing its solution keys
    raises :class:`~ssc.platesolve.base.MalformedSolverOutputError` rather
    than being read optimistically. A hang is killed and reported, never
    left running.
    """
    resolved_env = env if env is not None else os.environ
    resolved_cli = cli_path or await asyncio.to_thread(_resolve_cli, resolved_env)
    if resolved_cli is None:
        raise SolverUnavailableError(
            SolveIssue.SOLVER_NOT_FOUND,
            "ASTAP's command-line solver was not found; call astap_availability() first",
        )

    date_obs = await asyncio.to_thread(_read_date_obs, fits_path)

    with tempfile.TemporaryDirectory(prefix="ssc-astap-") as tmp_dir:
        out_base = Path(tmp_dir) / "solve"
        args = [str(resolved_cli), "-f", str(fits_path), "-o", str(out_base)]
        if position_hint is not None:
            ra_h, dec_deg = position_hint
            if not (math.isfinite(ra_h) and math.isfinite(dec_deg) and 0 <= ra_h < 24 and -90 <= dec_deg <= 90):
                raise ValueError("invalid J2000 solver position hint")
            args += ["-ra", str(ra_h), "-spd", str(dec_deg + 90)]
        if field_height_deg is not None:
            if not math.isfinite(field_height_deg) or not 0 < field_height_deg <= 180:
                raise ValueError("invalid solver field height")
            args += ["-fov", str(field_height_deg)]
        database_dir = await asyncio.to_thread(
            resolve_database_dir, resolved_cli, env=resolved_env
        )
        if database_dir is not None:
            args += ["-d", str(database_dir)]

        _log.info("astap_cli invoking: %s", " ".join(args))
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SolverUnavailableError(
                SolveIssue.SOLVER_NOT_FOUND, f"could not start {resolved_cli}: {exc}"
            ) from exc

        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.CancelledError:
            # A caller may impose a shorter operation-specific deadline
            # (live PA refinement does).  Never leave ASTAP searching after
            # that caller has moved on to a fresh frame.
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise SolveTimedOutError(
                SolveIssue.TIMED_OUT,
                f"astap_cli did not finish within {timeout_s:.0f}s solving {fits_path.name}",
            ) from None

        # communicate() has already returned, so the process has exited and
        # returncode is set; asyncio's stub still types it Optional.
        assert proc.returncode is not None
        exit_code: int = proc.returncode
        ini = await asyncio.to_thread(_read_ini, out_base.with_suffix(".ini"))

        if exit_code == 0:
            return _result_from_ini(ini, date_obs=date_obs, solver_version=solver_version)

        detail = ini.get("ERROR") or _ERROR_DETAIL_BY_EXIT_CODE.get(
            exit_code, f"astap_cli exited {exit_code}"
        )
        _log.info("astap_cli exit %s: %s", exit_code, detail)
        if exit_code in (32, 33):
            raise SolverUnavailableError(SolveIssue.DATABASE_UNAVAILABLE, detail)
        if exit_code in (1, 2):
            raise SolveFailedError(SolveIssue.NOT_SOLVED, detail)
        if exit_code == 16:
            raise InvalidPlateSolveInputError(SolveIssue.INVALID_INPUT, detail)
        raise PlateSolveError(
            SolveIssue.INVOCATION_FAILED,
            f"astap_cli exited {exit_code}: {stderr.decode('utf-8', errors='replace')[:500] or detail}",
        )
