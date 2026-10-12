"""System telemetry is useful without exposing host secrets."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ssc.api.schemas import SystemMetricsResponse
from ssc.system_metrics import collect_system_metrics
from tests.api.conftest import TestClient


def _sample(data_dir: Path) -> dict[str, object]:
    return {
        "collected_at": datetime(2026, 9, 14, 18, 0, tzinfo=UTC),
        "hostname": "photon",
        "platform": "Linux",
        "architecture": "aarch64",
        "model": "Raspberry Pi 5 Model B Rev 1.1",
        "os_name": "Debian GNU/Linux 13 (trixie)",
        "kernel": "6.18.34+rpt-rpi-2712",
        "system_uptime_s": 3600.0,
        "process": {
            "pid": 123,
            "uptime_s": 120.0,
            "cpu_percent": 8.5,
            "memory_bytes": 64_000_000,
            "threads": 9,
        },
        "cpu": {
            "percent": 12.5,
            "logical_cores": 4,
            "load_1m": 0.4,
            "load_5m": 0.3,
            "load_15m": 0.2,
            "temperature_c": 42.5,
            "throttled_raw": "0x0",
            "throttle_flags": [],
        },
        "memory": {
            "total_bytes": 8_000_000_000,
            "used_bytes": 2_000_000_000,
            "available_bytes": 6_000_000_000,
            "percent_used": 25.0,
        },
        "swap": {
            "total_bytes": 2_000_000_000,
            "used_bytes": 0,
            "free_bytes": 2_000_000_000,
            "percent_used": 0.0,
        },
        "volumes": [
            {
                "kind": "os",
                "label": "OS / root",
                "path": "/",
                "device": "/dev/mmcblk0p2",
                "filesystem": "ext4",
                "total_bytes": 32_000_000_000,
                "used_bytes": 4_000_000_000,
                "free_bytes": 28_000_000_000,
                "percent_used": 12.5,
                "same_device_as_os": True,
            },
            {
                "kind": "data",
                "label": "Photon data",
                "path": str(data_dir),
                "device": "/dev/sda1",
                "filesystem": "ext4",
                "total_bytes": 1_000_000_000_000,
                "used_bytes": 100_000_000_000,
                "free_bytes": 900_000_000_000,
                "percent_used": 10.0,
                "same_device_as_os": False,
            },
        ],
        "network": {
            "interfaces": [
                {"name": "eth0", "ipv4_address": "192.168.100.13"},
                {"name": "wlan0", "ipv4_address": "192.168.100.14"},
            ],
            "bytes_sent": 1234,
            "bytes_received": 5678,
        },
    }


def test_system_metrics_reports_pi_and_distinct_storage(paired: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("ssc.api.routes.system.collect_system_metrics", _sample)

    response = paired.get("/api/system/metrics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hostname"] == "photon"
    assert body["cpu"]["temperature_c"] == 42.5
    assert body["cpu"]["throttle_flags"] == []
    assert body["volumes"][0]["device"] == "/dev/mmcblk0p2"
    assert body["volumes"][1]["device"] == "/dev/sda1"
    assert body["volumes"][1]["same_device_as_os"] is False
    assert body["network"]["interfaces"] == [
        {"name": "eth0", "ipv4_address": "192.168.100.13"},
        {"name": "wlan0", "ipv4_address": "192.168.100.14"},
    ]
    serialized = response.text.lower()
    assert "environment" not in serialized
    assert "password" not in serialized
    assert "command" not in serialized


def test_system_metrics_allows_unavailable_pi_sensors(paired: TestClient, monkeypatch) -> None:
    sample = _sample(Path("/tmp/data"))
    cpu = dict(sample["cpu"])  # type: ignore[arg-type]
    cpu.update(temperature_c=None, throttled_raw=None, throttle_flags=None)
    sample["cpu"] = cpu
    monkeypatch.setattr("ssc.api.routes.system.collect_system_metrics", lambda _path: sample)

    response = paired.get("/api/system/metrics")

    assert response.status_code == 200, response.text
    assert response.json()["cpu"] == {
        "percent": 12.5,
        "logical_cores": 4,
        "load_1m": 0.4,
        "load_5m": 0.3,
        "load_15m": 0.2,
        "temperature_c": None,
        "throttled_raw": None,
        "throttle_flags": None,
    }


def test_real_collector_output_validates_against_response_schema(tmp_path: Path) -> None:
    sample = collect_system_metrics(tmp_path)
    validated = SystemMetricsResponse.model_validate(sample)

    assert len(validated.volumes) == 2
    assert validated.volumes[1].path == str(tmp_path)


def test_collector_uses_cross_platform_load_average(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ssc.system_metrics.psutil.getloadavg", lambda: (1.25, 2.5, 3.75))

    sample = collect_system_metrics(tmp_path)

    assert sample["cpu"]["load_1m"] == 1.25  # type: ignore[index]
    assert sample["cpu"]["load_5m"] == 2.5  # type: ignore[index]
    assert sample["cpu"]["load_15m"] == 3.75  # type: ignore[index]
