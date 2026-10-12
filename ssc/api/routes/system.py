# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Read-only host telemetry used by the System dashboard."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from ssc.api.deps import ServicesDep, SessionDep
from ssc.api.schemas import SystemMetricsResponse
from ssc.system_metrics import collect_system_metrics

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/metrics", response_model=SystemMetricsResponse)
async def read_system_metrics(
    services: ServicesDep, _session: SessionDep
) -> SystemMetricsResponse:
    sample = await asyncio.to_thread(collect_system_metrics, services.config.data_dir)
    return SystemMetricsResponse.model_validate(sample)
