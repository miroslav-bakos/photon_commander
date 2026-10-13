# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Liveness. Deliberately unauthenticated and deliberately uninformative.

Unauthenticated means exactly that: any device on the LAN can hit this to ask
"is the process up", the way a liveness check should work. It must not also
mean "and here is the server's filesystem layout and how busy it has been" —
those go out only once a caller has already proven it holds a paired session
(``ssc.api.security.SESSION_COOKIE``), same as every other detail this service
would otherwise keep to itself.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Request

from ssc.api.deps import ServicesDep
from ssc.api.schemas import HealthResponse
from ssc.api.security import SESSION_COOKIE
from ssc.state.models import SNAPSHOT_SCHEMA_VERSION

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request, services: ServicesDep) -> HealthResponse:
    session = await services.auth.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return HealthResponse(status="ok", schema_version=SNAPSHOT_SCHEMA_VERSION, desktop_instance=services.config.desktop_instance)
    version = None
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        with suppress(OSError):
            version = (Path(sys.executable).parent / "VERSION").read_text().strip() or None
    return HealthResponse(
        version=version,
        status="ok",
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        desktop_instance=services.config.desktop_instance,
        seq=services.state.seq,
        database=services.database.path,
        device_gateway=type(services.device).__name__,
    )
