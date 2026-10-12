# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""SSC's own ASCOM Alpaca driver for the ZWO Seestar.

The sole importer of ``ssc.seestar`` (``CLAUDE.md`` invariant 1). Everything
above this package — SSC's own UI included — talks to the device through the
standard ASCOM interfaces served here, plus the ``ssc.*`` vendor actions.

Layout:

``server.py``
    The HTTP surface, transaction ids, error envelopes and the bind policy.
``discovery.py``
    The UDP :32227 responder.
``backend.py``
    The one place that holds a Seestar session and translates its errors.
``capabilities.py``
    What each command verb is actually known to do, as data. Raising a verb's
    confidence is an edit to that table and nothing else.
``devices/``
    Telescope, Camera, Focuser and FilterWheel.
``actions.py``
    The ``ssc.*`` vocabulary, derived from the device's own operation list.
"""

from ssc.alpaca.actions import REGISTRY, SUPPORTED_ACTIONS, ActionSpec, Param
from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.capabilities import (
    COMMAND_VOCABULARY_COMPLETE,
    OPERATIONS,
    VERBS,
    Verb,
    VerbStatus,
    verb_is_callable,
)
from ssc.alpaca.devices import (
    AlpacaDevice,
    CameraDevice,
    FilterWheelDevice,
    FocuserDevice,
    TelescopeDevice,
)
from ssc.alpaca.discovery import (
    DISCOVERY_MESSAGE,
    DISCOVERY_PORT,
    AlpacaDiscoveryProtocol,
    start_discovery_responder,
)
from ssc.alpaca.errors import (
    ActionNotImplementedError,
    AlpacaError,
    AlpacaNotImplementedError,
    BadRequestError,
    ErrorNumber,
    InvalidOperationError,
    InvalidValueError,
    NotConnectedError,
    UnspecifiedError,
    ValueNotSetError,
)
from ssc.alpaca.server import (
    DEFAULT_ALPACA_PORT,
    BindRefusedError,
    DeviceRegistry,
    ServerConfig,
    build_devices,
    create_app,
    resolve_bind_host,
)

__all__ = [
    "COMMAND_VOCABULARY_COMPLETE",
    "DEFAULT_ALPACA_PORT",
    "DISCOVERY_MESSAGE",
    "DISCOVERY_PORT",
    "OPERATIONS",
    "REGISTRY",
    "SUPPORTED_ACTIONS",
    "VERBS",
    "ActionNotImplementedError",
    "ActionSpec",
    "AlpacaDevice",
    "AlpacaDiscoveryProtocol",
    "AlpacaError",
    "AlpacaNotImplementedError",
    "BadRequestError",
    "BindRefusedError",
    "CameraDevice",
    "DeviceRegistry",
    "ErrorNumber",
    "FilterWheelDevice",
    "FocuserDevice",
    "InvalidOperationError",
    "InvalidValueError",
    "NotConnectedError",
    "Param",
    "SeestarBackend",
    "ServerConfig",
    "TelescopeDevice",
    "UnspecifiedError",
    "ValueNotSetError",
    "Verb",
    "VerbStatus",
    "build_devices",
    "create_app",
    "resolve_bind_host",
    "start_discovery_responder",
    "verb_is_callable",
]
