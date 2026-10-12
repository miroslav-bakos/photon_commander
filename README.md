# Photon Commander

Free, complete observatory control and imaging software by **Miroslav Bakoš**.
The application source is licensed under **GPL-3.0-only**. There are no paid
feature tiers or application expiry gates. Third-party code and data retain
their own licences and attribution; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Support development

[Support Photon Commander on PayPal](https://www.paypal.com/donate/?business=7TG8WFWH5K79Y&no_recurring=0&item_name=for+Photon+Commander+development+and+support+for+new+devices&currency_code=EUR)

Donations help fund continued development and equipment for adding support for
new devices. Support is voluntary; Photon Commander remains free.

## Features

- Smart telescope control through ASCOM Alpaca, discovery and device status.
- Observation planning, scheduling, historical night reports and persisted recovery.
- Image capture, calibration masters, quality measurements and live stacking.
- Autofocus, polar alignment, target centring and equipment profiles.
- Sky maps, framing, catalogues, weather and observing conditions.
- Optional FFmpeg video streaming, plate solving and external integrations.

Capability and telemetry availability depend on the connected equipment. Unknown
or unavailable measurements are reported rather than invented.

## Build and develop

Use Python **3.12**, `uv`, and Node.js **22 or later** with npm.

```sh
uv sync --dev --frozen
npm --prefix web ci
npm --prefix web run typecheck
npm --prefix web run build
```

Obtain the JPL DE440s ephemeris kernel separately from NASA/JPL:
<https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp>.
Store it at `data/ephemeris/de440s.bsp`. It is required for astronomy calculations
and the packaging scripts. No kernel download is performed by the application
build. Keep its upstream provenance and assess applicable distribution terms.

**Survey imagery is not included.** Current packaging checks require a local
WHAM H-alpha HiPS pack at `data/hips/wham/`. Acquire the input and assess applicable
rights before using `scripts/build-wham-hips.py --source <local-source.fits>` to
generate it. The source recipe, upstream information and acknowledgements are
retained. Source licensing and credits do not establish WHAM redistribution rights.
The H-alpha layer remains unavailable without that separately provided asset;
packaging refuses an incomplete asset set rather than silently omitting it.

DSS2 is fetched only subject to its user consent and applicable personal-use
restrictions. DSS pixels, user caches and observing data are not distributed here.
GPL licensing does not grant survey image rights.

After providing the required local assets, build a backend bundle with
`scripts/build-package.sh`; native platform package scripts are under `scripts/`.
Use their `--help` output for the platform and immutable Git revision requirements.
`npm --prefix web run api:codegen` regenerates the OpenAPI TypeScript contract
without connecting to equipment.

For local use, build the frontend and run `scripts/run-dev.sh`; open the pairing
URL printed by the application. Weather providers require the user's own contact
or server-side keyring credentials. ASTAP and its database are installed separately;
optional video streaming requires FFmpeg. No credentials are supplied.

## Checks

```sh
uv run pytest
uv run ruff check ssc tests scripts
uv run mypy ssc
npm --prefix web run test
npm --prefix web run typecheck
```

Historical owner observations, recorded hardware traces, private deployment tests
and their dependent tests are intentionally excluded. The retained tests use
synthetic fixtures and public astronomy reference data. Ephemeris and locally
provided WHAM checks need their respective assets. No physical telescope is
required for ordinary offline tests.

## Licences and credits

Copyright (C) 2026 Miroslav Bakoš. See [LICENSE](LICENSE) for GPLv3 terms.
Required library licence texts and matching weak-copyleft source archives are
preserved under `packaging/third-party-licenses/` and
`packaging/third-party-sources/`. Astronomy datasets retain their own notices.
This directory is a private preparation candidate, not a published release.

## Optional background

The decorative M31 background is disabled by default. To use it in a local build, place an image you own or have permission to redistribute at `web/public/backgrounds/m31.webp`, then enable the background in Appearance. No third-party or personal photograph is included.
