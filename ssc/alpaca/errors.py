# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""ASCOM Alpaca error numbers, and the exceptions that carry them.

Two error channels exist and they are not interchangeable:

* **Device errors ride on HTTP 200.** The transaction succeeded; the *device*
  could not do what was asked. The body carries ``ErrorNumber`` and a non-empty
  ``ErrorMessage``. An empty ``ErrorMessage`` alongside a non-zero
  ``ErrorNumber`` is a ConformU failure, so the constructor refuses one.
* **HTTP 400** is only for a malformed request — an unknown member, a missing
  required parameter, a value that is not of the declared type. **HTTP 500** is
  only for an unhandled fault in this driver.

The reserved number range is fixed by ASCOM; ``0x500``-``0xFFF`` is the
driver-specific range and SSC does not use it: every failure this driver can
describe already has a standard number.
"""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar


class ErrorNumber(IntEnum):
    """The ASCOM reserved error numbers this driver emits."""

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


class AlpacaError(Exception):
    """A device error, reported on HTTP 200 with a number and a message."""

    number: ClassVar[ErrorNumber] = ErrorNumber.UNSPECIFIED

    def __init__(self, message: str) -> None:
        text = message.strip()
        if not text:
            raise ValueError(
                "an Alpaca error must carry a non-empty ErrorMessage; a blank one "
                "is a conformance failure and hides the cause from the client"
            )
        super().__init__(text)
        self.message = text


class AlpacaNotImplementedError(AlpacaError):
    """0x400 — the member exists in the interface but this driver cannot do it.

    Used for every capability whose Seestar verb is not verified on the device's
    firmware. A conformant ``NotImplemented`` is a correct answer; a fabricated
    value is a mis-pointed telescope.
    """

    number: ClassVar[ErrorNumber] = ErrorNumber.NOT_IMPLEMENTED


class InvalidValueError(AlpacaError):
    """0x401 — the value was well-formed but out of the member's legal range."""

    number: ClassVar[ErrorNumber] = ErrorNumber.INVALID_VALUE


class ValueNotSetError(AlpacaError):
    """0x402 — the value is genuinely unknown. Never substitute a zero for it."""

    number: ClassVar[ErrorNumber] = ErrorNumber.VALUE_NOT_SET


class NotConnectedError(AlpacaError):
    """0x407 — the operation needs a connected device and there is not one."""

    number: ClassVar[ErrorNumber] = ErrorNumber.NOT_CONNECTED


class InvalidOperationError(AlpacaError):
    """0x40B — legal member, legal value, wrong device state right now."""

    number: ClassVar[ErrorNumber] = ErrorNumber.INVALID_OPERATION


class ActionNotImplementedError(AlpacaError):
    """0x40C — the named ``Action`` is not in ``SupportedActions``."""

    number: ClassVar[ErrorNumber] = ErrorNumber.ACTION_NOT_IMPLEMENTED


class UnspecifiedError(AlpacaError):
    """0x4FF — the device failed in a way with no better ASCOM number."""

    number: ClassVar[ErrorNumber] = ErrorNumber.UNSPECIFIED


class BadRequestError(Exception):
    """A malformed HTTP request. Answered with **HTTP 400** and a text body.

    Deliberately *not* an :class:`AlpacaError`: this never reaches the JSON
    envelope, because there is no valid transaction to put it in.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


__all__ = [
    "ActionNotImplementedError",
    "AlpacaError",
    "AlpacaNotImplementedError",
    "BadRequestError",
    "ErrorNumber",
    "InvalidOperationError",
    "InvalidValueError",
    "NotConnectedError",
    "UnspecifiedError",
    "ValueNotSetError",
]
