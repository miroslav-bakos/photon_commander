# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Process configuration: where to bind, where the data lives, what to advertise.

Read from the environment with the ``SSC_`` prefix, or passed in directly by
tests. Deliberately **not** here: any device address. The scope is discovered or
typed and then remembered in the store; a build-time default would be CLAUDE.md
invariant 11 broken in the least visible possible place. A live test address
belongs in the developer's own environment, not in this model.

The default bind is loopback. Serving the phone means opting into
``0.0.0.0`` or a specific LAN address, and :func:`ssc.main.check_bind_address`
refuses anything that is not loopback or RFC 1918 (DESIGN.md §9).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ssc.net import local_ipv4_interfaces

DEFAULT_PORT: Final = 7777
DEFAULT_HOST: Final = "127.0.0.1"

#: mDNS service type advertised so the phone finds the desktop (DESIGN.md §1).
MDNS_SERVICE_TYPE: Final = "_ssc._tcp.local."


def default_data_dir() -> Path:
    """``$XDG_DATA_HOME/ssc`` or the platform equivalent."""
    override = os.environ.get("SSC_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "ssc"
    return Path.home() / ".local" / "share" / "ssc"


def default_bundled_hips_dir() -> Path:
    """``data/hips/`` beside the package — the convention the catalogue,
    ephemeris and profile data already follow (``ssc.astro.catalog``)."""
    return Path(__file__).resolve().parents[2] / "data" / "hips"


class AppConfig(BaseSettings):
    """Everything the process needs before it has a database."""

    model_config = SettingsConfigDict(env_prefix="SSC_", extra="forbid")

    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    data_dir: Path = Field(default_factory=default_data_dir)
    #: Built web assets. ``None`` means "serve the API only" — normal in dev.
    static_dir: Path | None = None
    mdns_enabled: bool = True
    instance_name: str = "Photon Commander"
    #: How many events stay replayable for a reconnecting client.
    event_buffer_size: int = Field(default=512, ge=16, le=65536)
    discovery_timeout_s: float = Field(default=8.0, gt=0.0, le=120.0)
    probe_timeout_s: float = Field(default=3.0, gt=0.0, le=60.0)
    #: The auto-connect supervisor's own backoff (``ssc.api.services.DeviceService``)
    #: — layered *above* the transport-level reconnect ``ssc.seestar.session``
    #: already does. Retry quickly at first, then back off to one minute
    #: during a prolonged outage. Explicit intent wakes the wait immediately.
    #: Attempts remain sequential and bounded, with jitter to avoid a hot loop.
    reconnect_initial_delay_s: float = Field(default=2.0, gt=0.0, le=60.0)
    reconnect_max_delay_s: float = Field(default=60.0, gt=0.0, le=3600.0)
    session_ttl_days: int = Field(default=30, ge=1, le=365)
    #: Whether a paired session is required to reach a mutating endpoint at
    #: all. Pairing is on by default; None leaves the stored Settings decision
    #: in charge. An explicit environment override remains locked in the UI.
    auth_required: bool | None = None
    desktop_instance: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    # Explicit reverse-proxy origins; no wildcard domains or arbitrary Host trust.
    trusted_browser_origins: list[str] = Field(default_factory=list)
    #: Load profiles from ``data/dev-profiles``. Off by default and those
    #: files are not included in packaged releases, so a simulator identity
    #: cannot silently appear in a production installation.
    enable_dev_profiles: bool = False
    log_level: str = "INFO"
    beta_diagnostics: bool = True
    #: The deploying operator's own identifying contact (an email address or a
    #: URL), sent as part of Met.no's mandatory User-Agent
    #: (``ssc.weather.providers.metno.build_user_agent``) — Met.no's terms
    #: require a real contact, not a generic string, so this has no default
    #: and no fallback. ``None`` means the weather forecast is unavailable
    #: until it is set (architecture policy invariant 7: surfaced as a startup notice,
    #: not a crash — see ``ssc.api.weather_service.build_weather_service``).
    weather_contact: str | None = None
    #: Maximum retained sky-survey tiles. Browsed tiles are never evicted:
    #: when this cap or the disk-space reserve is reached, new browsing tiles
    #: are served without caching. Existing tiles remain available offline.
    tiles_max_bytes: int = Field(default=512 * 1024**3, ge=64 * 1024**2)
    #: Where surveys whose licence permits redistribution ship as HiPS
    #: directories (``ssc.skymap.surveys``: WHAM H-alpha under ``wham/``).
    bundled_hips_dir: Path = Field(default_factory=default_bundled_hips_dir)
    #: Disk budget for target-field cutouts (``ssc/skymap/cutouts.py``) —
    #: separate from ``tiles_max_bytes`` because a cutout is per-target, not
    #: per-survey, and is evicted LRU except when a saved plan item's target
    #: still points at it.
    cutouts_max_bytes: int = Field(default=1024**3, ge=64 * 1024**2)
    #: Off by default (``ssc.settings.tls`` module docstring has the reasoning):
    #: turning this on for an already-deployed headless Pi whose systemd unit
    #: does not pass it would otherwise fail a previously-working upgrade
    #: outright (architecture policy invariant 7 — no cert material means no silent
    #: fallback to plain HTTP, it refuses to start), so it stays opt-in rather
    #: than becoming a surprise on the next release. A customer who wants the
    #: PWA install prompt (which requires a secure context) sets
    #: ``SSC_TLS_ENABLED=1`` once.
    tls_enabled: bool = False
    #: Optional plain-HTTP bootstrap listener used only when TLS is enabled.
    #: It serves the public local-CA certificate and redirects every other
    #: request to the HTTPS listener. ``None`` keeps the second listener off.
    tls_http_redirect_port: int | None = Field(default=None, ge=1, le=65535)
    #: Both set together to serve a certificate the operator supplies (their
    #: own CA, or a real domain behind a reverse proxy) instead of the
    #: generated local CA. Both unset (the default) means "generate one".
    tls_cert_path: Path | None = None
    tls_key_path: Path | None = None
    #: Port for the bundled ``ssc-alpaca`` driver. ``None`` means "the
    #: driver's own default" (5555) — deliberately not the literal here, so
    #: this module needs no import edge into ``ssc.alpaca`` just to name a
    #: constant, and so "unset" stays distinguishable from "deliberately
    #: 5555" (architecture policy: ``None`` means unknown, never a disguised value).
    #:
    #: Exists because 5555 was hardcoded, which meant a second ``ssc-core``
    #: could not run at all while one was up: it blocked the DEV instance the
    #: DEV/SIT split calls for, and blocked ``qa:skymap-compliance`` — the
    #: live-browser gate that would have caught a default survey which never
    #: drew a tile. ``0`` asks the OS for an ephemeral port, which is what a
    #: throwaway instance usually wants.
    alpaca_port: int | None = Field(default=None, ge=0, le=65535)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "ssc.sqlite3"

    @property
    def tiles_dir(self) -> Path:
        return self.data_dir / "tiles"

    @property
    def cutouts_dir(self) -> Path:
        return self.data_dir / "cutouts"

    @property
    def tls_dir(self) -> Path:
        """Where a generated local CA and leaf certificate live — irrelevant,
        and untouched, when ``tls_cert_path``/``tls_key_path`` name an
        operator-supplied certificate instead."""
        return self.data_dir / "tls"

    @property
    def scheme(self) -> str:
        """The scheme every URL this process hands out should use — the
        pairing QR code, the mDNS advertisement, ``origin`` below. A hardcoded
        ``http://`` here was the original bug report: once TLS is on, a
        pairing URL or a CORS check built from a hardcoded scheme sends the
        phone, or a legitimate browser request, to the wrong origin."""
        return "https" if self.tls_enabled else "http"

    @property
    def advertised_host(self) -> str:
        """The host a client should be told to use — the pairing URL's own
        host, so this is what a *phone's camera* has to resolve, not just
        this machine's own browser.

        ``0.0.0.0``/``::`` is not an answer, and neither, here, is
        ``localhost``: a wildcard bind means "reachable from the LAN"
        (``ssc.main._allowed_origins``'s own words: "0.0.0.0 is legitimate —
        it is how the phone reaches the service"), but a phone resolving
        "localhost" off a scanned QR code reaches *itself*, not this host
        (reported live — the pairing QR code pointed a phone at its own
        loopback). So a wildcard bind prefers this host's first real private
        IPv4 address (:func:`ssc.net.local_ipv4_interfaces`, the same
        enumeration ``_allowed_origins`` already trusts for the identical
        reasoning), and only falls back to ``localhost`` when this host
        genuinely has none — at which point a phone was never going to reach
        it regardless of what the URL said.

        An explicit loopback bind (``127.0.0.1``) is a different, intentional
        choice — "local only, no phone" — and still answers ``localhost``
        here, unchanged; only the wildcard case was ever wrong.
        """
        if self.host not in {"0.0.0.0", "::"}:
            return "localhost" if self.host == "127.0.0.1" else self.host
        for interface in local_ipv4_interfaces():
            if interface.is_private:
                return interface.address
        return "localhost"

    @property
    def origin(self) -> str:
        default_port = 443 if self.tls_enabled else 80
        port = "" if self.port == default_port else f":{self.port}"
        return f"{self.scheme}://{self.advertised_host}{port}"


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MDNS_SERVICE_TYPE",
    "AppConfig",
    "default_bundled_hips_dir",
    "default_data_dir",
]
