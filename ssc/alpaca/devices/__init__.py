# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The seven ASCOM device interfaces SSC serves."""

from ssc.alpaca.devices.base import (
    DRIVER_VERSION,
    MANUFACTURER,
    AlpacaDevice,
    Getter,
    Setter,
)
from ssc.alpaca.devices.camera import CameraDevice
from ssc.alpaca.devices.filterwheel import FilterWheelDevice
from ssc.alpaca.devices.focuser import FocuserDevice
from ssc.alpaca.devices.switch import SwitchDevice
from ssc.alpaca.devices.telescope import TelescopeDevice

__all__ = [
    "DRIVER_VERSION",
    "MANUFACTURER",
    "AlpacaDevice",
    "CameraDevice",
    "FilterWheelDevice",
    "FocuserDevice",
    "Getter",
    "Setter",
    "SwitchDevice",
    "TelescopeDevice",
]
