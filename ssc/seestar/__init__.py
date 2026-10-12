# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Native Seestar protocol client.

The only package in SSC permitted to know the device's wire format
(``CLAUDE.md`` invariant 1). ``ssc.alpaca`` is its sole importer.

Everything here is grounded in ``docs/M0-FINDINGS.md`` — observed against a ZWO
Seestar S30 Pro on **firmware 8.46**. Where the findings leave something unknown,
this package exposes a seam and says so rather than guessing:

* The **command vocabulary is partly mapped**. ``iscope_start_view`` then
  ``start_auto_goto`` is verified end to end on 8.46 — the mount really slewed —
  and the ``stop_*`` family is enumerated in ``session.VERIFIED_COMMAND_METHODS``.
  There is still no park/unpark verb on this firmware under any name, and no
  ``start_stack`` / ``start_solve`` / ``start_auto_focus`` has been called.
* A verb can exist and still refuse: code ``210`` means *not in this state*, not
  *not implemented*. It arrives as ``rpc.RpcNotSupportedError`` and reaches
  callers as ``session.PreconditionNotMetError``. Never map it to ASCOM
  ``NotImplemented``.
* Only one **event** name (``PiStatus``) has been seen, and never during an idle
  period: :4700 pushed nothing across 110 s of held-open listening. Everything
  else parses to ``events.UnknownEvent`` and is surfaced, not dropped.
* The **binary framing on :4800/:4801** (imaging streams) is not implemented at
  all — the header layout has not been decoded.
"""

from ssc.seestar.discovery import (
    DiscoveredDevice,
    DiscoveryError,
    discover,
    probe,
)
from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator
from ssc.seestar.events import (
    EventEnvelope,
    EventParser,
    EventStreamClosed,
    EventStreamOpened,
    MalformedFrameEvent,
    PiStatusEvent,
    SeestarEvent,
    UnknownEvent,
)
from ssc.seestar.models import (
    CameraInfo,
    CameraState,
    ChipSize,
    DeviceIdentity,
    DeviceState,
    DiskVolume,
    ExposureAndBin,
    MountState,
    PiStatus,
    StationState,
    WheelSetting,
    WheelState,
)
from ssc.seestar.native import (
    DEFAULT_ALPACA_PORT,
    FIRMWARE_UNKNOWN_NOTE,
    NO_EVENT_CHANNEL_NOTE,
    NativeAlpacaSession,
)
from ssc.seestar.routing import (
    ALPACA_DISCOVERY_PORT,
    CONTROL_SURFACE_ENV,
    ControlSurface,
    RoutedSession,
    SurfaceChoice,
    probe_control_surface,
    routed_session,
)
from ssc.seestar.rpc import (
    DeviceError,
    JsonRpcClient,
    RpcConnectionLostError,
    RpcError,
    RpcNotSupportedError,
    RpcProtocolError,
    RpcRemoteError,
    RpcResponse,
    RpcTimeoutError,
)
from ssc.seestar.session import (
    AuthenticationRequiredError,
    BackoffPolicy,
    ConnectionState,
    DeviceInfo,
    HazardousCallError,
    JsonRpcSeestarSession,
    MethodNotAvailableError,
    MethodPrecondition,
    NotConnectedError,
    PreconditionNotMetError,
    SeestarSession,
    SessionError,
    SessionStatus,
    UnverifiedMethodError,
    identify,
)
from ssc.seestar.transport import (
    CONTROL_PORT,
    EVENT_PORT,
    ConnectError,
    ConnectionLostError,
    EmulatorTransport,
    PortRefusedError,
    SeestarTransport,
    TcpTransport,
    TcpTransportFactory,
    TransportError,
    TransportFactory,
    redact,
)

__all__ = [
    "ALPACA_DISCOVERY_PORT",
    "CONTROL_PORT",
    "CONTROL_SURFACE_ENV",
    "DEFAULT_ALPACA_PORT",
    "EVENT_PORT",
    "FIRMWARE_UNKNOWN_NOTE",
    "NO_EVENT_CHANNEL_NOTE",
    "AuthenticationRequiredError",
    "BackoffPolicy",
    "CameraInfo",
    "CameraState",
    "ChipSize",
    "ConnectError",
    "ConnectionLostError",
    "ConnectionState",
    "ControlSurface",
    "DeviceError",
    "DeviceIdentity",
    "DeviceInfo",
    "DeviceState",
    "DiscoveredDevice",
    "DiscoveryError",
    "DiskVolume",
    "EmulatorConfig",
    "EmulatorTransport",
    "EventEnvelope",
    "EventParser",
    "EventStreamClosed",
    "EventStreamOpened",
    "ExposureAndBin",
    "HazardousCallError",
    "JsonRpcClient",
    "JsonRpcSeestarSession",
    "MalformedFrameEvent",
    "MethodNotAvailableError",
    "MethodPrecondition",
    "MountState",
    "NativeAlpacaSession",
    "NotConnectedError",
    "PiStatus",
    "PiStatusEvent",
    "PortRefusedError",
    "PreconditionNotMetError",
    "RoutedSession",
    "RpcConnectionLostError",
    "RpcError",
    "RpcNotSupportedError",
    "RpcProtocolError",
    "RpcRemoteError",
    "RpcResponse",
    "RpcTimeoutError",
    "SeestarEmulator",
    "SeestarEvent",
    "SeestarSession",
    "SeestarTransport",
    "SessionError",
    "SessionStatus",
    "StationState",
    "SurfaceChoice",
    "TcpTransport",
    "TcpTransportFactory",
    "TransportError",
    "TransportFactory",
    "UnknownEvent",
    "UnverifiedMethodError",
    "WheelSetting",
    "WheelState",
    "discover",
    "identify",
    "probe",
    "probe_control_surface",
    "redact",
    "routed_session",
]
