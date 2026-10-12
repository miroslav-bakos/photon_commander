# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ssc.api.deps import ControlDep, ServicesDep, rate_limit
from ssc.api.diagnostics import BugReport, build_identity, read_events

router = APIRouter(prefix="/api/bug-reports", tags=["bug-reports"])
_prepared: OrderedDict[str, tuple[float, BugReport]] = OrderedDict()
REPORT_ENDPOINT = "https://miroslavbakos.com/photon-report.php"


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["connection", "capture", "planning", "alignment", "interface", "other"]


@router.post("/preview", response_model=BugReport, dependencies=[Depends(rate_limit("bug-reports"))])
async def preview(body: ReportRequest, services: ServicesDep, session: ControlDep) -> BugReport:
    del session
    events = await asyncio.to_thread(read_events, services.config.data_dir / "diagnostics")
    if not events:
        raise HTTPException(status_code=409, detail="No diagnostic events are available. Enable diagnostics, reproduce the issue and prepare a new report.")
    report = BugReport(build=build_identity(), report_id=uuid.uuid4().hex, category=body.category, events=events,
                       logging_enabled=services.config.beta_diagnostics)
    # A traceback-heavy history can exceed the receiver's byte limit even below
    # its event limit. Bound the immutable preview, keeping the newest evidence.
    while len(report.model_dump_json().encode()) > 512_000:
        report.events = report.events[100:]
    _prepared[report.report_id] = (time.monotonic(), report)
    while len(_prepared) > 8:
        _prepared.popitem(last=False)
    return report


class SendReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    details: str = Field(default="", max_length=4000)


@router.post("/send", dependencies=[Depends(rate_limit("bug-reports"))])
async def send(request: SendReportRequest, session: ControlDep) -> dict[str, str]:
    del session
    prepared = _prepared.get(request.report_id)
    if prepared is None or time.monotonic() - prepared[0] > 1800:
        raise HTTPException(status_code=409, detail="Preview expired. Prepare a new report before sending.")
    body = prepared[1]
    # Never upload client-provided events; send exactly the server-generated preview.
    # Serialize ONLY this strict schema. No hostname, cookies, keyring or location.
    payload = body.model_dump_json().encode()
    if len(payload) > 512_000 or any(
        len(part) != 16 or any(c not in "0123456789abcdef" for c in part)
        for event in body.events for part in event.trace
    ):
        raise HTTPException(status_code=422, detail="Invalid diagnostic report")
    try:
        # Voluntary reporter text is separate from the immutable diagnostic archive.
        if request.details:
            payload = json.dumps({**body.model_dump(), "details": request.details}, ensure_ascii=False, separators=(",", ":")).encode()
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            response = await client.post(REPORT_ENDPOINT, content=payload,
                                         headers={"Content-Type": "application/json", "User-Agent": "Photon-Bug-Report/1"})
        receipt = response.json() if response.status_code == 202 else None
        if not isinstance(receipt, dict) or receipt.get("report_id") != body.report_id:
            raise ValueError("Report not accepted")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Report delivery was not confirmed. Keep the preview and try again.") from None
    return {"report_id": body.report_id, "status": "accepted"}


class BrowserEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["navigation", "action", "error", "unhandledrejection"]
    screen: Literal["dashboard", "planning", "scheduling", "calendar", "mount", "polar-align", "live", "live-stacking", "streaming", "archive", "system", "settings", "help", "other"]
    control: Literal["button", "link", "none"] = "none"


@router.post("/event", status_code=204, dependencies=[Depends(rate_limit("diagnostics"))])
async def browser_event(body: BrowserEvent, services: ServicesDep, session: ControlDep) -> None:
    import logging

    from ssc.api.diagnostics import fingerprint

    del session
    if services.config.beta_diagnostics:
        logging.getLogger("ssc.diagnostics.browser").debug("browser event", extra={
            "diagnostic_event": fingerprint(f"ui:{body.screen}:{body.kind}:{body.control}", 0),
        })
