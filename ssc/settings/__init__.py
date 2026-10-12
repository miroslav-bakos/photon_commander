# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Configuration store and keyring-backed secrets."""

from ssc.settings.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MDNS_SERVICE_TYPE,
    AppConfig,
    default_data_dir,
)
from ssc.settings.location import PRECEDENCE, resolve
from ssc.settings.models import (
    AppearanceSettings,
    DeviceSettings,
    LocationFix,
    LocationSettings,
    SecretRegistry,
    SecuritySettings,
)
from ssc.settings.secrets import (
    CONTROL_PIN_NAME,
    SERVICE_NAME,
    SecretError,
    SecretStore,
    validate_name,
)
from ssc.settings.store import RememberedAddress, SettingsError, SettingsStore

__all__ = [
    "CONTROL_PIN_NAME",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MDNS_SERVICE_TYPE",
    "PRECEDENCE",
    "SERVICE_NAME",
    "AppConfig",
    "AppearanceSettings",
    "DeviceSettings",
    "LocationFix",
    "LocationSettings",
    "RememberedAddress",
    "SecretError",
    "SecretRegistry",
    "SecretStore",
    "SecuritySettings",
    "SettingsError",
    "SettingsStore",
    "default_data_dir",
    "resolve",
    "validate_name",
]
