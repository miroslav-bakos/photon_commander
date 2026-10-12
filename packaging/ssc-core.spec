# PyInstaller spec for ``ssc-core``, one-dir build.
#
# Run via ``scripts/build-package.sh``, never invoked with a bare
# ``pyinstaller`` — the wrapper stamps the version and validates the
# ephemeris kernel and migrations are present *before* the build starts, so a
# missing 32 MB kernel fails loudly at build time, not on a user's machine.
#
# Two packaging risks this spec exists to close (see docs/IMPLEMENTATION.md
# M0.8):
#
# 1. Data files. PyInstaller's import analysis only follows *code*. The SQL
#    migrations under ``ssc/store/migrations/`` and the vendored
#    ``de440s.bsp`` ephemeris kernel are not code and must be added as
#    ``datas`` explicitly, or the packaged binary starts, looks fine, and
#    fails the first time it touches the database or the almanac.
# 2. Plugin discovery. ``keyring`` and (indirectly) ``skyfield`` resolve
#    backends through import machinery PyInstaller's static analysis cannot
#    see. PyInstaller ships ``hook-keyring.py`` and
#    ``pyinstaller-hooks-contrib`` ships ``hook-skyfield.py``; both fire
#    automatically because they are on the default hook path. ``zeroconf``
#    has no such hook, so it is collected explicitly below.
from __future__ import annotations

from pathlib import Path

# ``SPECPATH`` is injected into the spec's exec namespace by PyInstaller.
ROOT = Path(SPECPATH).parent  # noqa: F821 - PyInstaller global

block_cipher = None

from PyInstaller.utils.hooks import collect_all  # noqa: E402

datas = [
    (str(ROOT / "ssc" / "store" / "migrations"), "ssc/store/migrations"),
    (str(ROOT / "data" / "ephemeris" / "de440s.bsp"), "data/ephemeris"),
    # Every module that reads bundled data resolves it as
    # Path(__file__).parents[2] / "data" / ..., which in a one-dir build is
    # _internal/data/... -- so these must land at the same relative path.
    # Without the catalogue no target resolves; without the profiles no FOV;
    # without the star and constellation tables the skymap API fails at runtime.
    (str(ROOT / "data" / "catalog"), "data/catalog"),
    (str(ROOT / "data" / "stars"), "data/stars"),
    (str(ROOT / "data" / "constellations"), "data/constellations"),
    # Maintainer-refreshed JPL SBDB comet elements. Runtime search remains
    # useful at an offline observing site and never calls JPL from a browser.
    (str(ROOT / "data" / "comets"), "data/comets"),
    (str(ROOT / "data" / "profiles"), "data/profiles"),
    # The redistributable bundled sky survey. Runtime resolves this beneath
    # ``_internal/data`` in the one-dir artifact.
]

binaries: list[tuple[str, str]] = []

hiddenimports: list[str] = []

# Explicit belt-and-braces collection: zeroconf's compiled (cython)
# extensions have no PyInstaller hook of their own, and skyfield's runtime
# data matters even though its hook (from pyinstaller-hooks-contrib) normally
# covers it — the app has no display and no second chance if either is wrong
# on a user's machine.
for _pkg in ("zeroconf", "skyfield"):
    _datas, _binaries, _hiddenimports = collect_all(_pkg)
    # ``collect_all`` classifies upstream Python sources (and skyfield's own
    # test suite) as data. Runtime code is already carried in the PYZ archive;
    # shipping those plain-text duplicates would bloat the appliance and
    # violate the proprietary package boundary's no-source/no-tests promise.
    datas += [
        (source, destination)
        for source, destination in _datas
        if Path(source).suffix not in {".py", ".pyi"}
        and "tests" not in Path(source).parts
    ]
    binaries += _binaries
    hiddenimports += [name for name in _hiddenimports if ".tests" not in name]

a = Analysis(  # noqa: F821 - PyInstaller global
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["skyfield.tests"],
    noarchive=False,
    optimize=0,
)

# The upstream skyfield hook adds its large orbital regression corpus after
# our own ``collect_all`` filtering above. It is useful to skyfield's test
# suite but is neither runtime data nor acceptable product-package content.
a.datas = [entry for entry in a.datas if not entry[0].startswith("skyfield/tests/")]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821 - PyInstaller global

exe = EXE(  # noqa: F821 - PyInstaller global
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ssc-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(  # noqa: F821 - PyInstaller global
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="ssc-core",
)
