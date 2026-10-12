# Third-party notices

Photon Commander is GPL-3.0-only software, but its binary distribution
contains third-party software and separately licensed data. The distribution
ships the applicable upstream licence notices and source archives required for
its weak-copyleft components.

## Runtime software

The Python runtime includes packages under MIT, BSD-2-Clause, BSD-3-Clause,
Apache-2.0, PSF-2.0, MIT-0, 0BSD, Zlib and CC0-1.0 licences. The locked inventory
and exact reported licence expressions are enforced by
`packaging/licenses-allowlist.toml`.

The browser bundle includes React, React DOM, React Router, TanStack Query,
Zustand, openapi-fetch, qrcode.react and their production dependencies. They are
MIT or ISC licensed; the locked production inventory is checked during release.

Notable weak-copyleft components:

- `certifi` is MPL-2.0 and is used unmodified. Its licence notice is shipped as
  `third-party-licenses/MPL-2.0-certifi.txt`; matching source is shipped as
  `third-party-sources/certifi-2026.7.22.tar.gz`.
- `zeroconf` is LGPL-2.1-or-later and is used unmodified for local mDNS service
  advertisement. Its licence is shipped as
  `third-party-licenses/LGPL-2.1-zeroconf.txt`. The matching source release is
  shipped as `third-party-sources/zeroconf-0.150.0.tar.gz` and is also
  available from <https://pypi.org/project/zeroconf/>. Replacement builds may
  substitute a compatible `zeroconf` package; reverse engineering for debugging
  such a replacement is expressly permitted by the applicable LGPL licence.

PyInstaller is a build tool. Its official bootloader exception permits generated
commercial bundles under the application's own licence. PyInstaller itself is
not installed as an importable package in the shipped runtime.

`macholib` 1.16.4 is a Darwin build dependency of PyInstaller under the MIT
licence, copyright Bob Ippolito (2006–2010) and Ronald Oussoren (2010–2020).
Its exact upstream notice is retained as `third-party-licenses/MIT-macholib.txt`.
Source: <https://github.com/ronaldoussoren/macholib> and the checksum-pinned
PyPI source distribution in `uv.lock`. Photon does not import it directly;
the notice remains included whether or not a native artifact contains it.

## Bundled astronomy data

- `data/catalog/openngc.csv` is adapted from OpenNGC by Mattia Verga and remains
  CC BY-SA 4.0. The file records attribution, upstream revision, and changes.
- `data/constellations/boundaries.csv` is derived from Pierre Barbier's
  re-derivation of Delporte's tables and remains CC BY-SA 4.0. Its file header
  records attribution and changes.
- `data/stars/bright_stars.csv` uses the IAU Catalog of Star Names under
  CC BY 4.0 and carries its attribution in the file header.
- `data/ephemeris/de440s.bsp` and the JPL Small-Body Database comet-element
  snapshot are NASA/JPL scientific data. Provenance and refresh details are
  retained beside the data.
- The separately obtained local `data/hips/wham` pack is derived from WHAM Sky Survey DR1 integrated H-alpha
  intensity data. Credit: WHAM Sky Survey DR1 — Wisconsin H-Alpha Mapper,
  funded by the U.S. National Science Foundation. Source and acknowledgement:
  <https://www.astro.wisc.edu/research/research-areas/galactic-astronomy/wham/>.
- The Messier and Sharpless tables are factual/public-domain sources described
  in their own file headers.

Licence links: [MPL 2.0](https://www.mozilla.org/MPL/2.0/),
[LGPL 2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), and
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).

## External tools

FFmpeg, ASTAP and dwarfAlp are detected or launched as separately installed
programs. They are not part of the Smart Telescope Commander binary and retain
their own licences.

## AppImage desktop container

Linux AppImage packages embed our source-built AppImage type2-runtime,
revision `8f39b89e2ac31e1640b3d3f7e9a5108e6ce805fa`, under the MIT License.
Copyright (c) 2004-23 probonopd. Its static components include libfuse 3.15.0
(LGPL 2.1), squashfuse 0.5.2 (BSD), musl 1.2.5 (MIT and included notices),
zstd 1.5.6 (BSD/GPL alternative), zlib 1.3.2 (zlib), mimalloc 2.1.7 (MIT),
and GCC 14.2 support under the GCC Runtime Library Exception.
Full licenses and notices are in `third-party-licenses/appimage-runtime/`.
Corresponding sources, patches, build scripts and relink instructions for the
separate runtime are in `third-party-sources/appimage-runtime/`; they allow
rebuilding with modified libfuse with the corresponding Photon source. The link map
and build log record the captured inputs used for our shipped static runtime.
Upstream: https://github.com/AppImage/type2-runtime/tree/8f39b89e2ac31e1640b3d3f7e9a5108e6ce805fa
The appimagetool packager is a build tool and is not shipped in the application.

Survey pixels and user caches are not included in this source distribution.
DSS/WHAM credits do not establish redistribution permission. Obtain and assess
applicable rights before acquiring, generating, publishing or redistributing imagery.
