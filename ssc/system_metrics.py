# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Privacy-safe host telemetry for the local System dashboard.

The collector is deliberately read-only.  It reports enough to answer "will
this Pi finish the night?" without leaking process arguments, environment
variables, Wi-Fi names, usernames, or filesystem contents.
"""

from __future__ import annotations

import platform
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psutil


def default_live_stack_binning() -> Literal[1, 2]:
    """Use installed RAM, not fluctuating available memory or swap capacity.

    Reserved memory makes a nominal 4GB Pi report less than 4GiB. The 3GiB
    boundary separates 2GB-class hosts from 4GB-and-larger hosts.
    """
    return 2 if psutil.virtual_memory().total < 3 * 1024**3 else 1


@dataclass(frozen=True, slots=True)
class VolumeMetric:
    kind: str
    label: str
    path: str
    device: str | None
    filesystem: str | None
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float
    same_device_as_os: bool


_PROCESS = psutil.Process()
_PROCESS_STARTED_AT = _PROCESS.create_time()
# Prime psutil's non-blocking counters.  Subsequent requests report the period
# since the previous sample instead of sleeping inside an API request.
psutil.cpu_percent(interval=None)
_PROCESS.cpu_percent(interval=None)


def _machine_model() -> str | None:
    model_path = Path("/proc/device-tree/model")
    try:
        value = model_path.read_bytes().rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


def _cpu_temperature_c() -> float | None:
    thermal_root = Path("/sys/class/thermal")
    candidates: list[tuple[int, Path]] = []
    try:
        zones = list(thermal_root.glob("thermal_zone*"))
    except OSError:
        zones = []
    for zone in zones:
        try:
            zone_type = (zone / "type").read_text(encoding="utf-8").strip().lower()
        except OSError:
            zone_type = ""
        priority = 0 if zone_type in {"cpu-thermal", "cpu_thermal", "x86_pkg_temp"} else 1
        candidates.append((priority, zone / "temp"))
    for _, path in sorted(candidates, key=lambda item: item[0]):
        try:
            raw = float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        value = raw / 1000.0 if abs(raw) > 500 else raw
        if -40.0 <= value <= 150.0:
            return round(value, 1)
    return None


_THROTTLE_FLAGS = {
    0: "under_voltage_now",
    1: "frequency_capped_now",
    2: "throttled_now",
    3: "soft_temperature_limit_now",
    16: "under_voltage_occurred",
    17: "frequency_capped_occurred",
    18: "throttling_occurred",
    19: "soft_temperature_limit_occurred",
}


def _pi_throttling() -> tuple[str | None, list[str] | None]:
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=0.75,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0:
        return None, None
    text = result.stdout.strip()
    try:
        raw = text.split("=", maxsplit=1)[1]
        bits = int(raw, 16)
    except (IndexError, ValueError):
        return None, None
    flags = [name for bit, name in _THROTTLE_FLAGS.items() if bits & (1 << bit)]
    return f"0x{bits:x}", flags


def _existing_path(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _partition_for(path: Path) -> Any | None:
    resolved = str(_existing_path(path))
    matches = [
        partition
        for partition in psutil.disk_partitions(all=True)
        if resolved == partition.mountpoint
        or resolved.startswith(partition.mountpoint.rstrip("/") + "/")
    ]
    return max(matches, key=lambda partition: len(partition.mountpoint), default=None)


def _volume(kind: str, label: str, path: Path, *, os_device: str | None) -> VolumeMetric:
    existing = _existing_path(path)
    usage = psutil.disk_usage(str(existing))
    partition = _partition_for(existing)
    device = partition.device if partition is not None else None
    return VolumeMetric(
        kind=kind,
        label=label,
        path=str(path),
        device=device,
        filesystem=partition.fstype or None if partition is not None else None,
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        percent_used=round(usage.percent, 1),
        same_device_as_os=device is not None and device == os_device,
    )


def collect_system_metrics(data_dir: Path) -> dict[str, object]:
    """Collect one instantaneous sample; callers may run this in a worker thread."""

    now = time.time()
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    # psutil emulates load averages on Windows, while os.getloadavg() is not
    # available there and made the packaged telemetry endpoint return HTTP 500.
    load_1, load_5, load_15 = psutil.getloadavg()
    root_partition = _partition_for(Path("/"))
    os_device = root_partition.device if root_partition is not None else None
    temperature = _cpu_temperature_c()
    throttled_raw, throttle_flags = _pi_throttling()

    interfaces: list[dict[str, str]] = []
    for name, addresses in sorted(psutil.net_if_addrs().items()):
        for address in addresses:
            if address.family == socket.AF_INET and not address.address.startswith("127."):
                interfaces.append({"name": name, "ipv4_address": address.address})

    network = psutil.net_io_counters()
    process_memory = _PROCESS.memory_info()
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", platform.platform())
    except OSError:
        os_name = platform.platform()
    return {
        "collected_at": datetime.now(tz=UTC),
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "model": _machine_model(),
        "os_name": os_name,
        "kernel": platform.release(),
        "system_uptime_s": max(0.0, now - psutil.boot_time()),
        "process": {
            "pid": _PROCESS.pid,
            "uptime_s": max(0.0, now - _PROCESS_STARTED_AT),
            "cpu_percent": round(_PROCESS.cpu_percent(interval=None), 1),
            "memory_bytes": process_memory.rss,
            "threads": _PROCESS.num_threads(),
        },
        "cpu": {
            "percent": round(psutil.cpu_percent(interval=None), 1),
            "logical_cores": psutil.cpu_count(logical=True) or 0,
            "load_1m": round(load_1, 2),
            "load_5m": round(load_5, 2),
            "load_15m": round(load_15, 2),
            "temperature_c": temperature,
            "throttled_raw": throttled_raw,
            "throttle_flags": throttle_flags,
        },
        "memory": {
            "total_bytes": virtual.total,
            "used_bytes": virtual.used,
            "available_bytes": virtual.available,
            "percent_used": round(virtual.percent, 1),
        },
        "swap": {
            "total_bytes": swap.total,
            "used_bytes": swap.used,
            "free_bytes": swap.free,
            "percent_used": round(swap.percent, 1),
        },
        "volumes": [
            asdict(_volume("os", "OS / root", Path("/"), os_device=os_device)),
            asdict(_volume("data", "Photon data", data_dir, os_device=os_device)),
        ],
        "network": {
            "interfaces": interfaces,
            "bytes_sent": network.bytes_sent,
            "bytes_received": network.bytes_recv,
        },
    }
