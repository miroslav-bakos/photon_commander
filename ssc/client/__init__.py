# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Alpaca client ``ssc-core`` uses to reach the scope.

Above the seam by construction: this package speaks HTTP and ASCOM, and knows
nothing about the Seestar wire protocol (``CLAUDE.md`` invariants 1 and 2).
"""

from ssc.client.alpaca import (
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_REQUEST_TIMEOUT_S,
    AlpacaClient,
    AlpacaDeviceClient,
)
from ssc.client.errors import (
    AlpacaClientError,
    AlpacaErrorNumber,
    AlpacaImageBytesError,
    AlpacaProtocolError,
    AlpacaRemoteError,
    AlpacaTransportError,
)
from ssc.client.imagebytes import ImageArrayElementType, ImageBytesFrame, decode_image_bytes
from ssc.client.models import DeviceAddress, DiscoveredDevice, ProbeResult

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "AlpacaClient",
    "AlpacaClientError",
    "AlpacaDeviceClient",
    "AlpacaErrorNumber",
    "AlpacaImageBytesError",
    "AlpacaProtocolError",
    "AlpacaRemoteError",
    "AlpacaTransportError",
    "DeviceAddress",
    "DiscoveredDevice",
    "ImageArrayElementType",
    "ImageBytesFrame",
    "ProbeResult",
    "decode_image_bytes",
]
