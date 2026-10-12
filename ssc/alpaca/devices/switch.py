# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""ASCOM ``ISwitchV2`` over the Seestar's dew heater.

``switch/0`` is the device's **only** switch — one entry, ``'Dew heater'``,
range ``0``-``1``, writable (``docs/M0-FINDINGS.md`` 9.31, confirmed by the
owner: "switch device is solely dew heater"). It has **no JSON-RPC route at
all**: firmware 8.46 never exposed a dew-heater verb, so every member here is
``NotImplemented`` until the native Alpaca surface (9.16+) is active, at which
point ``prefer_native_getters``/``prefer_native_setters``
(``ssc/alpaca/devices/base.py``) pass every call straight through.

**Do not surface ``OCHTestPowerReport`` as telemetry.** It is the one vendor
action this switch advertises, and it returns fixed **ASCOM sample-driver
boilerplate** — "All observatory power systems are functioning properly." —
regardless of the device's actual state (measured with the battery flat).
There is no battery reading anywhere on the Alpaca surface on 9.31; see
``docs/M0-FINDINGS.md``'s "No battery telemetry on 9.31" section. This module
does not expose that action at all: a vendor action exists only where it
carries real information, and this one carries none.

**Reading a capability before connecting lies.** The device's own server
answers ``MaxSwitch`` ``0`` while disconnected and ``1`` once connected — the
same "plausible, wrong zero" trap as camera ``BayerOffsetX``/``Y`` and focuser
``MaxStep``. ``AlpacaDevice._guard`` already refuses every member here before
this driver's own ``Connected = True``, so that lying zero is never reached
in the first place; nothing in this module needs to special-case it.
"""

from __future__ import annotations

from typing import ClassVar, Final

from ssc.alpaca.devices.base import AlpacaDevice, Getter, Setter, unimplemented, unimplemented_put

#: Why every member here is ``NotImplemented`` off the native Alpaca surface.
NO_JSON_RPC_ROUTE_TO_SWITCH: Final = (
    "no dew-heater verb is verified on the Seestar JSON-RPC surface (firmware "
    "8.46); the switch is served only through the device's own native Alpaca "
    "surface (9.16+), and this session is not on it"
)


class SwitchDevice(AlpacaDevice):
    """The dew heater, the device's one and only ``Switch``."""

    device_type: ClassVar[str] = "switch"
    interface_version: ClassVar[int] = 2
    default_name: ClassVar[str] = "Photon Commander Seestar Switch"
    default_description: ClassVar[str] = "ZWO Seestar dew heater switch via Photon Commander"

    def build_getters(self) -> dict[str, Getter]:
        return self.prefer_native_getters(
            {
                "maxswitch": unimplemented("Switch.MaxSwitch", NO_JSON_RPC_ROUTE_TO_SWITCH),
                "canwrite": unimplemented("Switch.CanWrite", NO_JSON_RPC_ROUTE_TO_SWITCH),
                "getswitch": unimplemented("Switch.GetSwitch", NO_JSON_RPC_ROUTE_TO_SWITCH),
                "getswitchdescription": unimplemented(
                    "Switch.GetSwitchDescription", NO_JSON_RPC_ROUTE_TO_SWITCH
                ),
                "getswitchname": unimplemented(
                    "Switch.GetSwitchName", NO_JSON_RPC_ROUTE_TO_SWITCH
                ),
                "getswitchvalue": unimplemented(
                    "Switch.GetSwitchValue", NO_JSON_RPC_ROUTE_TO_SWITCH
                ),
                "maxswitchvalue": unimplemented(
                    "Switch.MaxSwitchValue", NO_JSON_RPC_ROUTE_TO_SWITCH
                ),
                "minswitchvalue": unimplemented(
                    "Switch.MinSwitchValue", NO_JSON_RPC_ROUTE_TO_SWITCH
                ),
                "switchstep": unimplemented("Switch.SwitchStep", NO_JSON_RPC_ROUTE_TO_SWITCH),
            }
        )

    def build_setters(self) -> dict[str, Setter]:
        return self.prefer_native_setters(
            {
                "setswitch": unimplemented_put("Switch.SetSwitch", NO_JSON_RPC_ROUTE_TO_SWITCH),
                "setswitchname": unimplemented_put(
                    "Switch.SetSwitchName", NO_JSON_RPC_ROUTE_TO_SWITCH
                ),
                "setswitchvalue": unimplemented_put(
                    "Switch.SetSwitchValue", NO_JSON_RPC_ROUTE_TO_SWITCH
                ),
            }
        )


__all__ = ["NO_JSON_RPC_ROUTE_TO_SWITCH", "SwitchDevice"]
