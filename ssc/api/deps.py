# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The service container and the FastAPI dependencies that reach into it.

Everything the app owns is built once in the lifespan and hung off
``app.state.services``. Handlers take :class:`Services` as a dependency rather
than reaching for a module-level global, so a test builds its own container with
a fake gateway and an in-memory database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import Depends, HTTPException, Request, WebSocket, status

from ssc.api.astro_service import AstroService
from ssc.api.capture_orchestrator import CaptureService
from ssc.api.heater_automation import HeaterAutomationService
from ssc.api.operation_service import OperationService
from ssc.api.polar_alignment_service import PolarAlignmentService
from ssc.api.run_service import RunService
from ssc.api.security import SESSION_COOKIE, AuthService, RateLimiter, Scope, Session
from ssc.api.services import DeviceService
from ssc.api.target_centering_service import TargetCenteringService
from ssc.api.tasks import TaskRegistry
from ssc.api.weather_service import WeatherService
from ssc.objectinfo import ObjectInfoService
from ssc.settings.config import AppConfig
from ssc.settings.secrets import SecretStore
from ssc.settings.store import SettingsStore
from ssc.skymap import BulkCutoutService, CutoutService, ExportImportService, SkymapService
from ssc.state.models import LocationState
from ssc.state.store import StateStore
from ssc.store.database import Database
from ssc.store.plans import PlanRepository


@dataclass(slots=True)
class Services:
    """Everything with a lifetime longer than one request."""

    config: AppConfig
    database: Database
    settings: SettingsStore
    secrets: SecretStore
    state: StateStore
    auth: AuthService
    device: DeviceService
    astro: AstroService
    weather: WeatherService
    plans: PlanRepository
    skymap: SkymapService
    cutouts: CutoutService
    bulk_cutouts: BulkCutoutService
    export_import: ExportImportService
    runs: RunService
    capture: CaptureService
    polar_alignment: PolarAlignmentService
    target_centering: TargetCenteringService
    heater_automation: HeaterAutomationService
    tasks: TaskRegistry
    object_info: ObjectInfoService
    operations: OperationService
    reference_image_slot: asyncio.Lock = field(default_factory=asyncio.Lock)
    limiters: dict[str, RateLimiter] = field(default_factory=dict)
    #: Shuts down ssc-alpaca hosting (server + client). ``None`` when a test
    #: substituted its own gateway and no driver was ever hosted (architecture policy
    #: invariant 2 is a runtime seam, not a per-test requirement). A plain
    #: callable rather than the runtime object itself, so this module does not
    #: need to import ssc.alpaca/ssc.client just to type a shutdown hook.
    alpaca_close: Callable[[], Awaitable[None]] | None = None


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, Services):  # pragma: no cover - a wiring bug, not a path
        raise RuntimeError("app.state.services is not set; the lifespan did not run")
    return services


def get_services_ws(websocket: WebSocket) -> Services:
    services = getattr(websocket.app.state, "services", None)
    if not isinstance(services, Services):  # pragma: no cover - a wiring bug, not a path
        raise RuntimeError("app.state.services is not set; the lifespan did not run")
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


#: Handed back by :func:`require_session` when ``auth_required`` is off, so
#: every dependent (``require_control`` included, since its scope is
#: ``CONTROL``) passes with no cookie at all — the loginless-by-default
#: posture (architecture policy invariant 7: made visible via ``auth_required`` on the
#: state snapshot, never a silent bypass). Never round-trips through the
#: database and is never compared against a real cookie, so its timestamps
#: are placeholders rather than meaningful session bounds.
_OPEN_SESSION: Final = Session(
    scope=Scope.CONTROL,
    label=None,
    created_at=datetime.fromtimestamp(0, tz=UTC),
    expires_at=datetime.max.replace(tzinfo=UTC),
)


async def require_session(request: Request, services: ServicesDep) -> Session:
    """Any authenticated session. Applied to every mutating endpoint.

    Loginless by default (CLAUDE.md 2026-09-09): with ``auth_required`` off,
    there is nothing to pair against and no cookie to check, so every caller
    that can reach the port gets an implicit control-scoped session. Turning
    ``auth_required`` on (``SSC_AUTH_REQUIRED=1``, or the Settings toggle)
    restores exactly today's behaviour below.
    """
    if not services.settings.effective_auth_required:
        return _OPEN_SESSION
    session = await services.auth.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not paired: open the QR link from the desktop shell",
        )
    return session


SessionDep = Annotated[Session, Depends(require_session)]


async def require_control(request: Request, session: SessionDep) -> Session:
    """A session allowed to move the scope. Distinct from view-only (DESIGN §9)."""
    if session.scope is not Scope.CONTROL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this session is view-only; enter the control PIN to continue",
        )
    return session


ControlDep = Annotated[Session, Depends(require_control)]


def require_location(services: Services) -> LocationState:
    """The active observing location, or a loud 409.

    Shared by every endpoint that needs a real place to compute against
    (``ssc/api/routes/astro.py``, ``ssc/api/routes/weather.py``,
    ``ssc/api/routes/tonight.py``): a result computed against a wrong or
    missing location is the worst failure mode this app has (DESIGN.md 5), so
    it is refused rather than guessed at.

    Deliberately **not** a FastAPI ``Depends`` dependency: dependency errors
    can be raised while FastAPI is still solving the request (before request
    -body validation runs), which would turn a malformed body's 422 into a
    409 that hides it. Called explicitly, inside the handler, after FastAPI's
    own body validation has already run — exactly like ``services.settings``
    or any other plain attribute access already used there.
    """
    location = services.settings.active_location()
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "no observing location is set: add one in Settings > Location before "
                "requesting a result that depends on it. A result computed against a "
                "wrong location is the worst failure mode this app has (DESIGN.md 5)."
            ),
        )
    return location


#: Addresses that get to see the pairing token without already holding a session.
#: The desktop shell runs on the same machine as the service; the phone does not,
#: which is exactly why the phone has to be handed the token out of band (the QR).
_LOOPBACK = frozenset({"127.0.0.1", "::1"})
_PROXY_MARKERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "tailscale-user-login",
    "tailscale-user-name",
)


def _is_direct_loopback(request: Request) -> bool:
    """A reverse proxy on loopback is still a remote caller.

    Tailscale Serve deliberately forwards to ``127.0.0.1``. Treating only
    ``request.client`` as provenance would therefore hand every tailnet
    visitor the pairing token. Serve strips spoofed Tailscale identity
    headers before adding its own; conventional proxy headers cover tagged
    clients for which no user identity header is supplied.
    """
    client = request.client.host if request.client is not None else ""
    return client in _LOOPBACK and not any(request.headers.get(name) for name in _PROXY_MARKERS)


async def require_local_or_session(request: Request, services: ServicesDep) -> Session | None:
    """Loopback callers, or an existing session. Guards the pairing token."""
    if _is_direct_loopback(request):
        return await services.auth.resolve(request.cookies.get(SESSION_COOKIE))
    return await require_session(request, services)


LocalOrSessionDep = Annotated[Session | None, Depends(require_local_or_session)]


def rate_limit(name: str) -> Callable[[Request, Services], Awaitable[None]]:
    """Dependency factory: throttle a named bucket, per client address."""

    async def dependency(request: Request, services: ServicesDep) -> None:
        limiter = services.limiters.get(name)
        if limiter is None:
            return
        client = request.client.host if request.client is not None else "unknown"
        if not limiter.allow(f"{name}:{client}"):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"too many {name} requests",
                headers={
                    "Retry-After": str(max(1, int(limiter.retry_after(f"{name}:{client}") + 1)))
                },
            )

    return dependency


__all__ = [
    "ControlDep",
    "LocalOrSessionDep",
    "Services",
    "ServicesDep",
    "SessionDep",
    "get_services",
    "get_services_ws",
    "rate_limit",
    "require_control",
    "require_local_or_session",
    "require_location",
    "require_session",
]
