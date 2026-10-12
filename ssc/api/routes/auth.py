# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Pairing, sessions and the control PIN (DESIGN.md §9).

The pairing token is readable by a loopback caller — the desktop shell, which is
what renders the QR code — or by an already-paired client. It is never readable
by an unauthenticated device on the LAN, because that would make the QR code
pointless.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ssc.api.deps import (
    ControlDep,
    LocalOrSessionDep,
    ServicesDep,
    SessionDep,
    rate_limit,
)
from ssc.api.schemas import (
    ControlPinRequest,
    PairingResponse,
    PairRequest,
    PinRequest,
    SessionResponse,
)
from ssc.api.security import SESSION_COOKIE, AuthError, Session
from ssc.remote_access import inspect_tailscale
from ssc.state.events import SettingsChanged

_log = logging.getLogger("ssc")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _session_response(session: Session | None) -> SessionResponse:
    if session is None:
        return SessionResponse(authenticated=False)
    return SessionResponse(
        authenticated=True,
        scope=session.scope,
        label=session.label,
        expires_at=session.expires_at,
    )


async def _pairing_response(services: ServicesDep) -> PairingResponse:
    remote = await asyncio.to_thread(
        inspect_tailscale, services.config.port, host=services.config.host
    )
    # Never advertise a Funnel URL in a pairing QR. A verified private Serve
    # route is the only remote origin SSC may suggest.
    origin = remote.url if remote.state == "serving" and remote.url else services.config.origin
    return PairingResponse(
        pairing_url=services.auth.pairing_url(origin),
        token=services.auth.pairing_token,
        origin=origin,
    )


@router.get("/session", response_model=SessionResponse)
async def read_session(
    request: Request, services: ServicesDep, response: Response
) -> SessionResponse:
    """Unauthenticated on purpose: the client asks *whether* it is paired."""
    response.headers["Cache-Control"] = "no-store"
    session = await services.auth.resolve(request.cookies.get(SESSION_COOKIE))
    return _session_response(session)


@router.post(
    "/pair",
    response_model=SessionResponse,
    dependencies=[Depends(rate_limit("auth"))],
)
async def pair(body: PairRequest, services: ServicesDep, response: Response) -> SessionResponse:
    try:
        issued = await services.auth.pair(body.token, label=body.label)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=int((issued.session.expires_at - issued.session.created_at).total_seconds()),
    )
    response.headers["Cache-Control"] = "no-store"
    return _session_response(issued.session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, session: SessionDep, services: ServicesDep, response: Response
) -> None:
    del session  # the dependency's job is to reject an unpaired caller
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await services.auth.revoke(token)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/pairing", response_model=PairingResponse)
async def pairing(services: ServicesDep, caller: LocalOrSessionDep) -> PairingResponse:
    """The QR payload. Loopback (the desktop shell) or an existing session only."""
    del caller
    return await _pairing_response(services)


@router.post("/pairing/rotate", response_model=PairingResponse)
async def rotate_pairing(services: ServicesDep, session: ControlDep) -> PairingResponse:
    del session
    services.auth.rotate_pairing_token()
    response = await _pairing_response(services)
    # The old QR is now dead; an operator at the console is the only other way
    # to reach a fresh one on a machine bound to a single LAN address (see
    # ssc.main's startup log line for why that matters).
    _log.info("pairing URL rotated: %s", response.pairing_url)
    return response


@router.post(
    "/pin",
    response_model=SessionResponse,
    dependencies=[Depends(rate_limit("auth"))],
)
async def enter_pin(
    body: PinRequest, request: Request, services: ServicesDep, session: SessionDep
) -> SessionResponse:
    """Upgrade a view-only session to control."""
    del session
    token = request.cookies.get(SESSION_COOKIE)
    if token is None:  # pragma: no cover - require_session already proved it exists
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no session cookie")
    try:
        upgraded = await services.auth.elevate(token, body.pin)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return _session_response(upgraded)


@router.put("/control-pin", response_model=SessionResponse)
async def set_control_pin(
    body: ControlPinRequest, request: Request, services: ServicesDep, session: ControlDep
) -> SessionResponse:
    """Set or clear the PIN that gates control actions.

    Setting one drops every existing session back to view-only, including this
    one: the tablet already paired in the shed must prove it knows the new PIN.
    """
    del session
    try:
        await services.auth.set_control_pin(body.pin)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return _session_response(await services.auth.resolve(request.cookies.get(SESSION_COOKIE)))
