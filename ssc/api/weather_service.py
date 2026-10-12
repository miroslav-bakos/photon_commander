# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Weather orchestration for the header widget and ``GET /api/weather/forecast``
(DESIGN.md §6): turns the active location into one merged
:class:`~ssc.weather.base.ForecastSeries`, in the shape ``ssc/weather/``
already defines. Nothing about HTTP or a route lives here — that is
``ssc/api/routes/weather.py``.

Met.no requires an identifying User-Agent contact
(``ssc.weather.providers.metno.build_user_agent``) that must be supplied by
the deploying operator, never baked into the app. It comes from the stored
setting first and ``AppConfig.weather_contact`` (``SSC_WEATHER_CONTACT``)
second, and :meth:`WeatherService.set_metno_contact` re-applies it at runtime
so a change made in Settings takes effect without a restart. Its absence is
not a crash (CLAUDE.md
invariant 7): :meth:`WeatherService.forecast` raises
:class:`WeatherUnavailableError` naming the fix, exactly the pattern
``ssc.api.astro_service.AstroService`` uses for a missing ephemeris kernel.

OpenWeatherMap is always constructed — its own ``availability()`` already
reports "no key configured" or "secrets backend unavailable" without this
module re-probing either (``ssc.weather.providers.openweathermap``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

from ssc.settings.config import AppConfig
from ssc.settings.models import WeatherProviderName
from ssc.settings.secrets import BackendStatus, SecretStore
from ssc.weather.base import ForecastSeries, Provider, ProviderUnavailableError
from ssc.weather.cache import ForecastCache
from ssc.weather.merge import merge_series
from ssc.weather.providers.metno import MetNoConfig, MetNoConfigError, MetNoProvider
from ssc.weather.providers.openweathermap import OpenWeatherMapConfig, OpenWeatherMapProvider
from ssc.weather.providers.visualcrossing import VisualCrossingConfig, VisualCrossingProvider
from ssc.weather.providers.weatherapi import WeatherApiConfig, WeatherApiProvider

_log = logging.getLogger(__name__)

#: Relative to ``AppConfig.data_dir`` — survives a restart, same rationale as
#: ``ssc.weather.cache.ForecastCache``'s own docstring: a dark site with no
#: signal is the normal case, not a corner case worth losing on restart.
CACHE_FILENAME: Final = "weather_cache.json"

#: Met.no is DESIGN.md §6's one authoritative default; any user-keyed provider
#: (OpenWeatherMap today) is a ranked fallback behind it. Order here is the
#: rank order ``merge_series`` uses, and the order a status view must present
#: them in whether or not each one happens to be enabled right now.
PROVIDER_RANK: Final = ("metno", "openweathermap", "weatherapi", "visualcrossing")

_METNO_MISSING_CONTACT_REASON: Final = (
    "no Met.no contact configured: set one in Settings > Weather (or the "
    "SSC_WEATHER_CONTACT environment variable) to a real identifying contact "
    "— an email address or a URL — before the weather forecast becomes "
    "available"
)


@dataclass(frozen=True, slots=True)
class MetNoContact:
    """Which contact Met.no will actually send, and where it came from.

    The source is reported, not just the value, because "I set it in Settings
    but the deployment's environment variable is what's really in force" is
    precisely the kind of thing a user cannot diagnose from a value alone.
    """

    value: str | None
    #: ``"settings"``, ``"environment"`` or ``"unset"``.
    source: str


def resolve_metno_contact(*, stored: str | None, environment: str | None) -> MetNoContact:
    """Stored setting wins over the environment.

    That order is deliberate and is the opposite of how an operator override
    usually works: the environment variable exists to bootstrap a headless
    deployment, but a value typed into Settings must visibly take effect, or
    the field is a lie. ``--check-config`` and ``GET /api/settings/weather``
    both report which one won.
    """
    cleaned_stored = (stored or "").strip()
    if cleaned_stored:
        return MetNoContact(value=cleaned_stored, source="settings")
    cleaned_env = (environment or "").strip()
    if cleaned_env:
        return MetNoContact(value=cleaned_env, source="environment")
    return MetNoContact(value=None, source="unset")


class WeatherUnavailableError(RuntimeError):
    """No provider could serve a forecast for this location, fresh or cached.
    Every caller must surface this, never fabricate a series (CLAUDE.md
    invariant 7)."""


def build_weather_service(
    config: AppConfig,
    secrets: SecretStore,
    backend_status: BackendStatus,
    *,
    stored_contact: str | None = None,
    selected_provider: WeatherProviderName = "metno",
) -> tuple[WeatherService, str | None]:
    """Build the service and, if Met.no could not be enabled, the reason —
    a startup notice, not a raised exception. ``ssc.main.build_services``
    surfaces the reason exactly like a missing ephemeris kernel: the rest of
    the app comes up fine without it, and every weather-backed endpoint names
    the fix when actually asked for a forecast.
    """
    cache = ForecastCache(path=config.data_dir / CACHE_FILENAME)
    providers: list[Provider] = []
    startup_reasons: list[str] = []
    metno_notice: str | None = None

    contact = resolve_metno_contact(stored=stored_contact, environment=config.weather_contact)
    if contact.value is not None:
        try:
            providers.append(MetNoProvider(MetNoConfig(contact=contact.value), cache=cache))
        except MetNoConfigError as exc:
            metno_notice = str(exc)
            startup_reasons.append(f"metno: {exc}")
    else:
        metno_notice = _METNO_MISSING_CONTACT_REASON
        startup_reasons.append(f"metno: {_METNO_MISSING_CONTACT_REASON}")

    # Always constructed: its own availability() reports "no key" or "secrets
    # backend unavailable" without this module re-probing either.
    providers.append(
        OpenWeatherMapProvider(
            OpenWeatherMapConfig(),
            cache=cache,
            secrets=secrets,
            backend_status=backend_status,
        )
    )
    providers.append(
        WeatherApiProvider(
            WeatherApiConfig(), cache=cache, secrets=secrets, backend_status=backend_status
        )
    )
    providers.append(
        VisualCrossingProvider(
            VisualCrossingConfig(), cache=cache, secrets=secrets, backend_status=backend_status
        )
    )

    service = WeatherService(
        providers=providers,
        startup_reasons=startup_reasons,
        cache=cache,
        selected_provider=selected_provider,
    )
    return service, metno_notice if selected_provider == "metno" else None


class WeatherService:
    """The configured provider is the sole source for each forecast request.

    All shipped providers remain constructed so Settings can report their
    real availability and switching takes effect immediately. Per-row
    ``source`` provenance is still preserved.
    """

    def __init__(
        self,
        *,
        providers: list[Provider],
        startup_reasons: list[str] | None = None,
        cache: ForecastCache | None = None,
        selected_provider: WeatherProviderName = "metno",
    ) -> None:
        self._providers = providers
        self._startup_reasons = list(startup_reasons or [])
        #: Needed only to rebuild Met.no on a contact change; a service built
        #: without one simply cannot be reconfigured, and says so.
        self._cache = cache
        self._selected_provider = selected_provider

    def set_provider(self, provider: WeatherProviderName) -> None:
        """Apply a persisted provider choice without requiring a restart."""
        self._selected_provider = provider

    async def set_metno_contact(self, contact: str | None) -> str | None:
        """Apply a new Met.no contact, returning the reason it is still
        unusable or ``None`` if Met.no is now enabled.

        The provider validates its contact at construction and that is worth
        keeping — a bad contact fails here, loudly, at the moment the user
        saves it, rather than silently at the next forecast. So this rebuilds
        rather than mutating, and an invalid value leaves Met.no disabled with
        the validation error as its reason.
        """
        if self._cache is None:
            raise RuntimeError(
                "this WeatherService was built without a cache and cannot be "
                "reconfigured; use build_weather_service()"
            )

        previous = [p for p in self._providers if p.name == MetNoProvider.name]
        for provider in previous:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
        rest = [p for p in self._providers if p.name != MetNoProvider.name]

        cleaned = (contact or "").strip()
        if not cleaned:
            self._providers = rest
            self._startup_reasons = [f"metno: {_METNO_MISSING_CONTACT_REASON}"]
            return _METNO_MISSING_CONTACT_REASON

        try:
            metno = MetNoProvider(MetNoConfig(contact=cleaned), cache=self._cache)
        except MetNoConfigError as exc:
            self._providers = rest
            self._startup_reasons = [f"metno: {exc}"]
            return str(exc)

        # Met.no stays ranked first (DESIGN.md 6): it is the authoritative
        # default, and any keyed provider is a fallback behind it.
        self._providers = [metno, *rest]
        self._startup_reasons = []
        return None

    async def availability(self) -> list[tuple[str, bool, str | None]]:
        """``(name, available, reason)`` for every provider, plus any provider
        that is disabled entirely and therefore absent from ``_providers``.

        Deliberately built from the SAME ``provider.availability()`` call the
        forecast path uses, so a status screen cannot drift from what actually
        happens when a forecast is requested — a status view that reimplements
        its own idea of "configured" is how a UI ends up confidently wrong.
        """
        rows: dict[str, tuple[str, bool, str | None]] = {}
        # Keyring backends can take seconds to answer on a headless host. The
        # providers are independent, so do not multiply that latency by the
        # number of configured accounts on every Settings refresh.
        results = await asyncio.gather(*(provider.availability() for provider in self._providers))
        for provider, result in zip(self._providers, results, strict=True):
            rows[provider.name] = (provider.name, result.available, result.reason)
        for reason in self._startup_reasons:
            name, _, detail = reason.partition(": ")
            if name not in rows:
                rows[name] = (name, False, detail or reason)

        # Rank order, not construction order. A disabled Met.no is absent from
        # `_providers` entirely and would otherwise be appended *after* the
        # fallback it outranks — which reads, wrongly, as OpenWeatherMap being
        # the primary provider.
        order = {name: i for i, name in enumerate(PROVIDER_RANK)}
        return sorted(rows.values(), key=lambda row: (order.get(row[0], len(order)), row[0]))

    async def aclose(self) -> None:
        for provider in self._providers:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()

    async def forecast(self, latitude_deg: float, longitude_deg: float) -> ForecastSeries:
        """The selected provider's series for this location, fresh or
        cache-stale.

        Raises :class:`WeatherUnavailableError` only when *no* provider can
        answer at all, fresh or cached — never a fabricated series.
        """
        serieses: list[ForecastSeries] = []
        errors = [
            reason
            for reason in self._startup_reasons
            if reason.partition(": ")[0] == self._selected_provider
        ]
        selected = [p for p in self._providers if p.name == self._selected_provider]
        for provider in selected:
            availability = await provider.availability()
            if not availability.available:
                errors.append(f"{provider.name}: {availability.reason}")
                continue
            try:
                serieses.append(await provider.fetch(latitude_deg, longitude_deg))
            except ProviderUnavailableError as exc:
                errors.append(str(exc))

        if not serieses:
            if not selected and not errors:
                errors.append(f"{self._selected_provider}: provider is not configured")
            reason = "; ".join(errors) if errors else "no weather provider is configured"
            raise WeatherUnavailableError(f"no weather forecast is available: {reason}")
        return merge_series(*serieses)


__all__ = [
    "CACHE_FILENAME",
    "PROVIDER_RANK",
    "MetNoContact",
    "WeatherService",
    "WeatherUnavailableError",
    "build_weather_service",
    "resolve_metno_contact",
]
