# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Typed results for the three pre-connect vendor actions.

Restated here for the same reason ``ssc.client.errors`` restates the ASCOM
error numbers: ``ssc.client`` must never import ``ssc.alpaca`` or
``ssc.seestar`` (``CLAUDE.md`` invariants 1 and 2), so these are the client's
own view of the JSON ``ssc.discover``, ``ssc.probe`` and
``ssc.set_device_address`` already return — not an alias for a driver-side
type.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """One device ``ssc.discover`` found on the LAN."""

    host: str
    port: int
    firmware: str | None
    model: str | None
    serial: str | None
    #: What the scan observed about it. ``None`` when the driver did not say.
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What ``ssc.probe`` found at one address — a real result, not a bare boolean.

    ``reachable`` and ``handshake`` are deliberately separate: a host can
    accept the TCP connection without ever answering the Seestar protocol
    handshake. ``reason`` carries the driver's own explanation either way.
    """

    reachable: bool
    handshake: bool
    firmware: str | None
    model: str | None
    serial: str | None
    verified: bool
    reason: str | None
    #: The port that answered — not necessarily the one asked about, since a
    #: device on the native Alpaca surface is identified on its Alpaca port.
    #: ``None`` when the driver did not say, and then the requested port stands.
    port: int | None = None


@dataclass(frozen=True, slots=True)
class DeviceAddress:
    """The target ``ssc.set_device_address`` recorded for the next connect."""

    host: str
    port: int


__all__ = ["DeviceAddress", "DiscoveredDevice", "ProbeResult"]
