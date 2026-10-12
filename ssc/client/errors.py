# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Error numbers and exceptions for the Alpaca client.

These numbers are deliberately **restated** here rather than imported from
``ssc.alpaca``. ``ssc.client`` is what ``ssc-core`` uses, and ``CLAUDE.md``
invariant 1 forbids anything above the seam from reaching ``ssc.seestar`` — even
transitively. Importing the driver's error module would put ``ssc.alpaca`` on
``ssc.client``'s import graph and drag the protocol layer along with it.

The duplication is the point: a client of a standard protocol should depend on
the protocol, not on one particular server that speaks it.
"""

from __future__ import annotations

from enum import IntEnum


class AlpacaErrorNumber(IntEnum):
    """The ASCOM reserved error numbers, as seen from the client side."""

    OK = 0x000
    NOT_IMPLEMENTED = 0x400
    INVALID_VALUE = 0x401
    VALUE_NOT_SET = 0x402
    NOT_CONNECTED = 0x407
    INVALID_WHILE_PARKED = 0x408
    INVALID_WHILE_SLAVED = 0x409
    SETTINGS_PROVIDER_ERROR = 0x40A
    INVALID_OPERATION = 0x40B
    ACTION_NOT_IMPLEMENTED = 0x40C
    NOT_IN_CACHE = 0x40D
    UNSPECIFIED = 0x4FF


class AlpacaClientError(Exception):
    """Base class for every failure this client reports."""


class AlpacaTransportError(AlpacaClientError):
    """The HTTP request failed, or the response was not a valid Alpaca envelope."""


class AlpacaProtocolError(AlpacaClientError):
    """The server broke the protocol — most often a mis-echoed transaction id.

    Not pedantry: the echoed ``ClientTransactionID`` is the only thing tying a
    response to its request, and a driver that gets it wrong may be answering a
    different question than the one that was asked.
    """


class AlpacaImageBytesError(AlpacaTransportError):
    """The ``ImageBytes`` binary payload is not one this client will decode.

    Covers an unsupported ``MetadataVersion``, a rank other than 2, an
    element type with no representation here, a payload too short to hold
    what its own header declares, or a data length that disagrees with
    ``Dimension1 x Dimension2``. Never raised for a device-reported failure —
    that arrives as :class:`AlpacaRemoteError`, signalled inside the same
    header per the Alpaca API Reference §8.9, exactly as an ``ErrorNumber``
    signals it in the JSON envelope.
    """


class AlpacaRemoteError(AlpacaClientError):
    """The device reported an error on HTTP 200."""

    def __init__(self, number: int, message: str, *, member: str) -> None:
        super().__init__(f"{member}: [{number:#05x}] {message}")
        self.number = number
        self.message = message
        self.member = member

    @property
    def is_not_implemented(self) -> bool:
        return self.number == AlpacaErrorNumber.NOT_IMPLEMENTED

    @property
    def is_not_connected(self) -> bool:
        return self.number == AlpacaErrorNumber.NOT_CONNECTED

    @property
    def is_value_not_set(self) -> bool:
        return self.number == AlpacaErrorNumber.VALUE_NOT_SET


__all__ = [
    "AlpacaClientError",
    "AlpacaErrorNumber",
    "AlpacaImageBytesError",
    "AlpacaProtocolError",
    "AlpacaRemoteError",
    "AlpacaTransportError",
]
