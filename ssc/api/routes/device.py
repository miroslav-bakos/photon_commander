# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Finding, testing and connecting to the scope (CLAUDE.md invariant 11).

There is no default address anywhere in this file. The three ways to get one are
a scan, a typed host, and the address the store remembers from last time — which
is exactly the set the Settings tab offers.

Every endpoint here is rate-limited: a stuck retry loop must not sweep the
neighbour's network or hammer the mount's control port, which firmware 8.46 is
known to take badly (``docs/M0-FINDINGS.md``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status

from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.operation_service import OperationBusyError, OperationContext
from ssc.api.schemas import (
    AddressBookResponse,
    AutoConnectRequest,
    ConnectRequest,
    DiscoverRequest,
    DiscoverResponse,
    EthernetAddressRequest,
    HardwareProfileRequest,
    ProbeRequest,
    ProbeResponse,
    WifiAddressRequest,
)
from ssc.api.services import DeviceBusyError
from ssc.settings.models import DeviceSettings
from ssc.state.events import SettingsChanged
from ssc.state.models import OperationState
from ssc.state.ports import DeviceGatewayError

router = APIRouter(prefix="/api/device", tags=["device"])


@router.post(
    "/discover",
    response_model=DiscoverResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("device"))],
)
async def discover(
    body: DiscoverRequest, services: ServicesDep, session: ControlDep
) -> DiscoverResponse:
    """Start a bounded scan of this host's own RFC 1918 subnets.

    Returns immediately; progress and results arrive as ``discovery.changed``
    events on the WS. Cancel with ``DELETE /api/device/discover``.
    """
    del session
    try:
        scan_id = services.device.start_discovery(timeout_s=body.timeout_s)
    except DeviceBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DeviceGatewayError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return DiscoverResponse(scan_id=scan_id)


@router.delete("/discover", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_discover(services: ServicesDep, session: ControlDep) -> None:
    """Cancel a running scan. Idempotent: no scan running is not an error."""
    del session
    await services.device.cancel_discovery()


@router.post(
    "/probe-job",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=[Depends(rate_limit("device"))],
)
async def probe_job(
    body: ProbeRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Test one address and report what is actually there.

    An unreachable host is a 200 with ``reachable: false`` and the real reason in
    ``note`` — not a 4xx, and never a green tick.
    """
    del session
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message=f"Probing {body.host}")
        port = body.control_port or services.device.default_control_port
        device = await services.device.probe(
            body.host, control_port=port, timeout_s=body.timeout_s
        )
        return ProbeResponse(
            device=device,
            tested_at=datetime.now(tz=UTC),
            remembered=device.reachable,
        ).model_dump(mode="json")

    try:
        return services.operations.start(
            kind="device_probe",
            work=work,
            message=f"Starting probe of {body.host}",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/probe",
    response_model=ProbeResponse,
    dependencies=[Depends(rate_limit("device"))],
)
async def probe(body: ProbeRequest, services: ServicesDep, session: ControlDep) -> ProbeResponse:
    """Compatibility endpoint; the UI uses reconnectable ``probe-job``."""
    del session
    port = body.control_port or services.device.default_control_port
    device = await services.device.probe(body.host, control_port=port, timeout_s=body.timeout_s)
    return ProbeResponse(
        device=device,
        tested_at=datetime.now(tz=UTC),
        remembered=device.reachable,
    )


@router.post(
    "/connect-job",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=[Depends(rate_limit("device"))],
)
async def connect_job(
    body: ConnectRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Connect to the given address, or to the one the store remembers."""
    del session
    async def work(context: OperationContext) -> None:
        context.progress(message="Connecting to telescope")
        if body.host is None:
            await services.device.connect_preferred(control_port=body.control_port)
            return
        port = body.control_port or services.device.default_control_port
        await services.device.connect(body.host, control_port=port)

    try:
        return services.operations.start(
            kind="device_connect",
            work=work,
            message="Starting connection",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/connect",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("device"))],
)
async def connect(body: ConnectRequest, services: ServicesDep, session: ControlDep) -> None:
    """Compatibility endpoint; the UI uses reconnectable ``connect-job``."""
    del session
    if body.host is None:
        try:
            await services.device.connect_preferred(control_port=body.control_port)
        except DeviceGatewayError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return
    port = body.control_port or services.device.default_control_port
    await services.device.connect(body.host, control_port=port)


@router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(services: ServicesDep, session: ControlDep) -> None:
    """Disconnect, and stay disconnected: the auto-connect supervisor treats
    this as an explicit intent and will not reconnect on its own until asked
    (``POST /connect`` or turning auto-connect back on)."""
    del session
    await services.device.disconnect()


@router.put(
    "/auto-connect",
    response_model=DeviceSettings,
    dependencies=[Depends(rate_limit("device"))],
)
async def set_auto_connect(
    body: AutoConnectRequest, services: ServicesDep, session: ControlDep
) -> DeviceSettings:
    """The master auto-connect switch (default on). Turning it on also clears
    a standing explicit-disconnect intent."""
    del session
    return await services.device.set_auto_connect(body.enabled)


@router.put(
    "/ethernet-address",
    response_model=DeviceSettings,
    dependencies=[Depends(rate_limit("device"))],
)
async def set_ethernet_address(
    body: EthernetAddressRequest, services: ServicesDep, session: ControlDep
) -> DeviceSettings:
    """Set the preferred wired endpoint, leaving remembered Wi-Fi as fallback."""
    del session
    return await services.device.set_ethernet_address(
        None if body.host is None else str(body.host), control_port=body.control_port
    )


@router.put(
    "/wifi-address",
    response_model=DeviceSettings,
    dependencies=[Depends(rate_limit("device"))],
)
async def set_wifi_address(
    body: WifiAddressRequest, services: ServicesDep, session: ControlDep
) -> DeviceSettings:
    """Set the fallback wireless endpoint without changing preferred Ethernet."""
    del session
    return await services.device.set_wifi_address(
        None if body.host is None else str(body.host), control_port=body.control_port
    )


@router.put(
    "/hardware-profile",
    response_model=DeviceSettings,
    dependencies=[Depends(rate_limit("device"))],
)
async def set_hardware_profile(
    body: HardwareProfileRequest, services: ServicesDep, session: ControlDep
) -> DeviceSettings:
    """Confirm the hardware profile when firmware detection is unavailable."""
    del session
    try:
        return await services.device.set_hardware_profile(body.profile_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/addresses", response_model=AddressBookResponse)
async def addresses(services: ServicesDep, session: SessionDep) -> AddressBookResponse:
    del session
    return AddressBookResponse(
        remembered=services.settings.device,
        known=await services.settings.known_addresses(),
    )


@router.delete("/addresses/{host}/{control_port}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_address(
    host: str, control_port: int, services: ServicesDep, session: ControlDep
) -> None:
    del session
    await services.settings.forget_address(host, control_port)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
