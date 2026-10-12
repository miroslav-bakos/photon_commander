# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The HTTP and WebSocket surface. Routers are thin; the work is below them."""

from fastapi import APIRouter

from ssc.api import ws
from ssc.api.routes import (
    astro,
    auth,
    bug_reports,
    capture,
    device,
    filterwheel,
    focuser,
    health,
    heater,
    mount,
    operations,
    plans,
    polar_alignment,
    runs,
    settings,
    skymap,
    state,
    system,
    tonight,
    weather,
)

#: Every router, mounted by :func:`ssc.main.create_app`. Order is cosmetic — it
#: is the order the OpenAPI document (and therefore the generated TS client)
#: lists the tags in.
api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(bug_reports.router)
api_router.include_router(state.router)
api_router.include_router(operations.router)
api_router.include_router(system.router)
api_router.include_router(device.router)
api_router.include_router(mount.router)
api_router.include_router(focuser.router)
api_router.include_router(filterwheel.router)
api_router.include_router(heater.router)
api_router.include_router(settings.router)
api_router.include_router(astro.router)
api_router.include_router(weather.router)
api_router.include_router(tonight.router)
api_router.include_router(plans.router)
api_router.include_router(polar_alignment.router)
api_router.include_router(skymap.router)
api_router.include_router(runs.router)
api_router.include_router(capture.router)
api_router.include_router(ws.router)

__all__ = ["api_router"]
