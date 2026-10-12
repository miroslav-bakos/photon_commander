# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Alpaca transport envelope: parameters in, JSON responses out.

Everything here is the part of the protocol that has nothing to do with
telescopes and everything to do with passing ConformU:

* **Parameter names are case-insensitive.** ``ClientTransactionID``,
  ``clienttransactionid`` and ``CLIENTTRANSACTIONID`` are the same parameter.
  Parameter *values* stay exactly as sent.
* ``PUT`` bodies are ``application/x-www-form-urlencoded``, never JSON.
* Every response echoes ``ClientTransactionID`` and carries a **monotonically
  increasing** ``ServerTransactionID``.
* A missing or malformed ``ClientTransactionID`` is echoed as ``0`` rather than
  rejected — the spec makes it optional, and refusing the request would fail
  conformance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ssc.alpaca.errors import AlpacaError, BadRequestError

#: The only Alpaca API version this driver speaks.
ALPACA_API_VERSION = 1

#: Transaction ids are unsigned 32-bit on the wire.
MAX_TRANSACTION_ID = 4_294_967_295

#: Accepted spellings of an Alpaca boolean. ASCOM clients send ``True``/``False``;
#: the numeric forms are accepted because several real clients send them.
_TRUE_WORDS = frozenset({"true", "1", "yes"})
_FALSE_WORDS = frozenset({"false", "0", "no"})


class TransactionCounter:
    """Hands out ``ServerTransactionID`` values, strictly increasing.

    Single-threaded by construction: the whole driver runs on one event loop, so
    the increment needs no lock. It wraps rather than overflowing the wire type,
    and never yields ``0``, which the spec reserves for "not supplied".
    """

    def __init__(self, start: int = 0) -> None:
        self._value = start

    def allocate(self) -> int:
        self._value += 1
        if self._value > MAX_TRANSACTION_ID:
            self._value = 1
        return self._value


class RequestParams:
    """Case-insensitive view over one request's parameters.

    Query string for ``GET``, form body for ``PUT``. If the same name arrives
    twice in different cases, the first occurrence wins; the alternative — an
    error — would reject requests the spec permits.
    """

    def __init__(self, pairs: Iterable[tuple[str, str]]) -> None:
        values: dict[str, str] = {}
        for key, value in pairs:
            values.setdefault(key.strip().lower(), value)
        self._values = values

    @classmethod
    def empty(cls) -> RequestParams:
        return cls(())

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._values

    def as_mapping(self) -> Mapping[str, str]:
        """The lower-cased parameters, for logging and diagnostics."""
        return dict(self._values)

    # -- raw access ---------------------------------------------------------- #

    def raw(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name.lower(), default)

    def require(self, name: str) -> str:
        value = self._values.get(name.lower())
        if value is None:
            raise BadRequestError(f"required parameter '{name}' is missing")
        return value

    # -- typed access -------------------------------------------------------- #

    def as_bool(self, name: str) -> bool:
        text = self.require(name).strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
        raise BadRequestError(f"parameter '{name}' must be True or False, got {text!r}")

    def as_int(self, name: str) -> int:
        text = self.require(name).strip()
        try:
            return int(text)
        except ValueError as exc:
            raise BadRequestError(f"parameter '{name}' must be an integer, got {text!r}") from exc

    def as_float(self, name: str) -> float:
        text = self.require(name).strip()
        try:
            return float(text)
        except ValueError as exc:
            raise BadRequestError(f"parameter '{name}' must be a number, got {text!r}") from exc

    # -- the two ids every request may carry --------------------------------- #

    @property
    def client_transaction_id(self) -> int:
        """The client's id, echoed verbatim; ``0`` when absent or malformed.

        The spec makes this optional and reserves ``0`` for "not supplied", so a
        junk value is reported as not-supplied rather than failing the request.
        """
        return _optional_uint32(self._values.get("clienttransactionid"))

    @property
    def client_id(self) -> int:
        return _optional_uint32(self._values.get("clientid"))


def _optional_uint32(text: str | None) -> int:
    if text is None:
        return 0
    try:
        value = int(text.strip())
    except (AttributeError, ValueError):
        return 0
    if 0 <= value <= MAX_TRANSACTION_ID:
        return value
    return 0


# --------------------------------------------------------------------------- #
# Response envelopes
# --------------------------------------------------------------------------- #


def value_response(
    value: object, *, client_transaction_id: int, server_transaction_id: int
) -> dict[str, object]:
    """A successful ``GET``, or a ``PUT`` that returns a value (``Action``)."""
    return {
        "Value": value,
        "ClientTransactionID": client_transaction_id,
        "ServerTransactionID": server_transaction_id,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }


def method_response(*, client_transaction_id: int, server_transaction_id: int) -> dict[str, object]:
    """A successful ``PUT`` with no return value. Carries no ``Value`` key."""
    return {
        "ClientTransactionID": client_transaction_id,
        "ServerTransactionID": server_transaction_id,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }


def error_response(
    error: AlpacaError, *, client_transaction_id: int, server_transaction_id: int
) -> dict[str, object]:
    """A device error. HTTP 200; the failure lives in the body.

    ``Value`` is omitted deliberately — there is no value, and inventing a
    default is the exact failure mode this driver exists to avoid.
    """
    return {
        "ClientTransactionID": client_transaction_id,
        "ServerTransactionID": server_transaction_id,
        "ErrorNumber": int(error.number),
        "ErrorMessage": error.message,
    }


__all__ = [
    "ALPACA_API_VERSION",
    "MAX_TRANSACTION_ID",
    "RequestParams",
    "TransactionCounter",
    "error_response",
    "method_response",
    "value_response",
]
