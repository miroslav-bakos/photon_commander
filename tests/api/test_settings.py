"""Location provenance and secret handling (DESIGN.md §5, CLAUDE.md invariant 8)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from ssc.remote_access import RemoteAccessStatus
from ssc.settings.models import StreamingSettings
from ssc.settings.secrets import SERVICE_NAME
from ssc.state.events import CaptureChanged, MountChanged
from ssc.state.models import CaptureState, MountState
from tests.api.conftest import MemoryKeyring, TestClient, services_of


def _terminal_operation(client: TestClient, response: Any) -> dict[str, Any]:
    operation_id = response.json()["id"]
    for _ in range(100):
        operation = client.get(f"/api/operations/{operation_id}").json()
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.01)
    return operation


def test_client_configuration_is_backend_owned_and_exported(paired: TestClient) -> None:
    entries = {
        "ssc.layouts.v1": '{"state":{"saved":{}}}',
        "ssc.skymap.layers.v1": '{"backgroundOpacity":0.4}',
    }
    saved = paired.put("/api/settings/client-configuration", json={"entries": entries})
    assert saved.status_code == 200, saved.text
    assert paired.get("/api/settings/client-configuration").json() == {"entries": entries}

    exported = paired.get("/api/settings/configuration/export").json()
    assert exported["settings"]["client"] == {"entries": entries}


def test_external_tools_catalog_is_explicit_about_separate_licences(paired: TestClient) -> None:
    response = paired.get("/api/settings/external-tools")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["platform"]
    assert body["architecture"]
    tools = {tool["id"]: tool for tool in body["tools"]}
    assert set(tools) == {"ffmpeg", "astap", "dwarf-alpaca"}
    assert tools["dwarf-alpaca"]["distribution"] == "separate_process"
    assert tools["dwarf-alpaca"]["license"] == "GPL-3.0-only (separate process)"
    assert all(tool["source_url"].startswith("https://") for tool in tools.values())


def test_remote_access_reports_private_serve_state_without_credentials(
    paired: TestClient,
    monkeypatch,
) -> None:
    status = RemoteAccessStatus(
        state="serving",
        detail="Private HTTPS remote access is active inside your tailnet.",
        detected_path="/usr/bin/tailscale",
        dns_name="observatory.example.ts.net",
        url="https://observatory.example.ts.net",
        funnel_enabled=False,
        setup_command="sudo tailscale serve --bg 127.0.0.1:7777",
    )
    monkeypatch.setattr(
        "ssc.api.routes.settings.inspect_tailscale", lambda _port, **_kwargs: status
    )

    response = paired.get("/api/settings/remote-access")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "serving"
    assert body["url"] == "https://observatory.example.ts.net"
    assert "token" not in body
    assert "key" not in body


def _fix(client: TestClient, source: str, latitude: float, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "source": source,
        "latitude_deg": latitude,
        "longitude_deg": 17.1,
    }
    body.update(extra)
    response = client.put("/api/settings/location/fix", json=body)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


def test_no_location_is_none_rather_than_a_guess(paired: TestClient) -> None:
    body = paired.get("/api/settings").json()
    assert body["location"]["active"] is None
    assert body["location"]["sources"] == {}


def test_capture_dither_settings_have_s30_defaults_and_persist(paired: TestClient) -> None:
    defaults = paired.get("/api/settings/capture-dither")
    assert defaults.status_code == 200
    assert defaults.json() == {
        "enabled": True,
        "every_n_frames": 6,
        "min_pixels": 30.0,
        "max_pixels": 200.0,
    }
    saved = paired.put(
        "/api/settings/capture-dither",
        json={"enabled": False, "every_n_frames": 4, "min_pixels": 40, "max_pixels": 180},
    )
    assert saved.status_code == 200, saved.text
    assert paired.get("/api/settings/capture-dither").json() == saved.json()


def test_rejected_frame_retention_defaults_to_keep_and_persists(paired: TestClient) -> None:
    defaults = paired.get("/api/settings/capture-rejected-frames")
    assert defaults.status_code == 200
    assert defaults.json() == {"keep_rejected_frames": True}

    saved = paired.put(
        "/api/settings/capture-rejected-frames",
        json={"keep_rejected_frames": False},
    )
    assert saved.status_code == 200, saved.text
    assert paired.get("/api/settings/capture-rejected-frames").json() == {
        "keep_rejected_frames": False
    }


def test_capture_center_after_drift_defaults_disabled_and_persists_opt_in(
    paired: TestClient,
) -> None:
    defaults = paired.get("/api/settings/capture-center-after-drift")
    assert defaults.status_code == 200
    assert defaults.json() == {"enabled": False, "threshold_pixels": 400.0}

    saved = paired.put(
        "/api/settings/capture-center-after-drift",
        json={"enabled": True, "threshold_pixels": 250},
    )
    assert saved.status_code == 200, saved.text
    assert paired.get("/api/settings/capture-center-after-drift").json() == saved.json()


def test_auto_lp_filter_defaults_disabled_and_persists_opt_in(paired: TestClient) -> None:
    assert paired.get("/api/settings/auto-lp-filter").json() == {"enabled": False}
    saved = paired.put("/api/settings/auto-lp-filter", json={"enabled": True})
    assert saved.status_code == 200, saved.text
    assert paired.get("/api/settings/auto-lp-filter").json() == {"enabled": True}


def test_unparked_broadcast_setting_has_url_and_persists(paired: TestClient) -> None:
    defaults = paired.get("/api/settings/streaming")
    assert defaults.status_code == 200
    assert defaults.json() == {
        "broadcast_when_unparked": False,
        "wide_camera_picture_in_picture": False,
        "rtsp_host": "0.0.0.0",
        "rtsp_port": 8554,
        "rtsp_path": "live",
        "rtsp_width": 1280,
        "rtsp_height": 720,
        "rtsp_fps": 1.0,
        "rtsp_bitrate_kbps": 2500,
        "rtsp_update_interval_s": 5.0,
        "rtsp_auto_crop": True,
        "youtube_resolution": "720p",
        "youtube_stream_key_set": False,
        "active": False,
        "url": "rtsp://HOST:8554/live",
        "source": None,
        "last_error": None,
    }

    saved = paired.put(
        "/api/settings/streaming",
        json={
            "broadcast_when_unparked": True,
            "wide_camera_picture_in_picture": True,
            "rtsp_host": "127.0.0.1",
            "rtsp_port": 9554,
            "rtsp_path": "night",
            "rtsp_width": 1920,
            "rtsp_height": 1080,
            "rtsp_fps": 2,
            "rtsp_bitrate_kbps": 6000,
            "rtsp_update_interval_s": 10,
            "youtube_resolution": "1080p",
            "rtsp_auto_crop": False,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["broadcast_when_unparked"] is True
    assert paired.get("/api/settings/streaming").json()["broadcast_when_unparked"] is True
    assert saved.json()["wide_camera_picture_in_picture"] is True
    assert paired.get("/api/settings/streaming").json()["wide_camera_picture_in_picture"] is True
    assert services_of(paired).settings.streaming.broadcast_when_unparked is True
    assert services_of(paired).settings.streaming.wide_camera_picture_in_picture is True
    assert services_of(paired).settings.streaming.rtsp_port == 9554
    assert saved.json()["url"] == "rtsp://HOST:9554/night"


def test_supervised_application_restart_requires_confirmation_and_is_deferred(
    paired: TestClient, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("INVOCATION_ID", "test-supervisor")
    monkeypatch.setattr(
        "ssc.api.routes.settings._terminate_for_supervisor_restart",
        lambda: calls.append("restart"),
    )

    rejected = paired.post("/api/settings/restart", json={"confirm": False})
    accepted = paired.post("/api/settings/restart", json={"confirm": True})

    assert rejected.status_code == 422
    assert accepted.status_code == 202
    assert accepted.json() == {"accepted": True}
    assert calls == ["restart"]


def test_application_restart_explains_when_no_supervisor_can_relaunch(
    paired: TestClient, monkeypatch
) -> None:
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    response = paired.post("/api/settings/restart", json={"confirm": True})

    assert response.status_code == 409
    assert "supervised service" in response.json()["detail"]


def test_host_reboot_refuses_unprovisioned_control(paired: TestClient, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("ssc.api.routes.settings.host_reboot.reboot", lambda: calls.append("reboot"))
    monkeypatch.setattr("ssc.api.routes.settings.host_reboot.availability", lambda **kwargs: (False, "Permission not configured"))
    status = paired.get("/api/settings/host-reboot")
    response = paired.post("/api/settings/host-reboot", json={"confirm": True})
    assert status.status_code == 200
    assert status.json()["available"] is False
    assert "Permission" in status.json()["reason"]
    assert response.status_code == 409
    assert calls == []


@pytest.mark.parametrize("config", [True], indirect=True)
def test_host_reboot_requires_idle_parked_mount_and_confirmation(
    paired: TestClient, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "ssc.api.routes.settings.host_reboot.availability",
        lambda *, auth_required: (auth_required, "Ready"),
    )
    monkeypatch.setattr("ssc.api.routes.settings.host_reboot.reboot", lambda: calls.append("reboot"))
    state = services_of(paired).state
    state.apply(MountChanged(mount=MountState(
        polled_at=datetime.now(tz=UTC), stale=False, at_park=False, slewing=True,
    )))
    assert "Park the mount" in paired.get("/api/settings/host-reboot").json()["reason"]
    assert paired.post("/api/settings/host-reboot", json={"confirm": True}).status_code == 409
    state.apply(MountChanged(mount=MountState(
        polled_at=datetime.now(tz=UTC), stale=False, at_park=True, slewing=False,
    )))
    state.apply(CaptureChanged(capture=CaptureState(in_progress=True)))
    assert paired.post("/api/settings/host-reboot", json={"confirm": True}).status_code == 409
    state.apply(CaptureChanged(capture=CaptureState()))
    assert paired.post("/api/settings/host-reboot", json={"confirm": False}).status_code == 422
    response = paired.post("/api/settings/host-reboot", json={"confirm": True})
    assert response.status_code == 202
    assert response.json()["previous_epoch"] == state.epoch
    assert calls == ["reboot"]


@pytest.mark.parametrize("config", [True], indirect=True)
def test_host_shutdown_requires_idle_parked_mount_and_confirmation(
    paired: TestClient, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "ssc.api.routes.settings.host_reboot.availability",
        lambda *, auth_required, action: (auth_required, "Ready"),
    )
    monkeypatch.setattr("ssc.api.routes.settings.host_reboot.shutdown", lambda: calls.append("shutdown"))
    state = services_of(paired).state
    state.apply(MountChanged(mount=MountState(
        polled_at=datetime.now(tz=UTC), stale=False, at_park=False, slewing=True,
    )))
    assert "Park the mount" in paired.get("/api/settings/host-shutdown").json()["reason"]
    assert paired.post("/api/settings/host-shutdown", json={"confirm": True}).status_code == 409
    state.apply(MountChanged(mount=MountState(
        polled_at=datetime.now(tz=UTC), stale=False, at_park=True, slewing=False,
    )))
    state.apply(CaptureChanged(capture=CaptureState(in_progress=True)))
    assert paired.post("/api/settings/host-shutdown", json={"confirm": True}).status_code == 409
    state.apply(CaptureChanged(capture=CaptureState()))
    assert paired.post("/api/settings/host-shutdown", json={"confirm": False}).status_code == 422
    response = paired.post("/api/settings/host-shutdown", json={"confirm": True})
    assert response.status_code == 202
    assert response.json()["previous_epoch"] == state.epoch
    assert calls == ["shutdown"]


def test_legacy_streaming_source_toggles_migrate_to_one_unparked_policy() -> None:
    settings = StreamingSettings.model_validate(
        {
            "wide_camera_when_idle": False,
            "auto_start_when_stacking": True,
            "wide_camera_until_stacked": True,
            "wide_camera_picture_in_picture": True,
        }
    )

    assert settings.broadcast_when_unparked is True
    assert settings.wide_camera_picture_in_picture is True
    assert "wide_camera_when_idle" not in settings.model_dump()
    assert "auto_start_when_stacking" not in settings.model_dump()


def test_capture_filename_template_has_a_default_and_persists(paired: TestClient) -> None:
    defaults = paired.get("/api/settings/capture-filename-template")
    assert defaults.status_code == 200
    assert defaults.json() == {
        "template": "%IMAGETYPE%_%TARGET%_%EXPOSURE%_%CCDTEMP%_%FILTER%_%DATETIME%_%SEQUENCE%"
    }

    saved = paired.put(
        "/api/settings/capture-filename-template",
        json={"template": "%IMAGETYPE%-%SEQUENCE%"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"template": "%IMAGETYPE%-%SEQUENCE%"}
    assert paired.get("/api/settings/capture-filename-template").json() == saved.json()
    assert services_of(paired).settings.capture.filename_template == "%IMAGETYPE%-%SEQUENCE%"


def test_date_tag_is_offered_and_accepted_for_capture_and_calibration_paths(
    paired: TestClient,
) -> None:
    tags = paired.get("/api/settings/capture/path-tags")
    assert tags.status_code == 200
    date_tag = next(tag for tag in tags.json()["tags"] if tag["name"] == "DATE")
    assert date_tag["example"] == "20261228"
    assert "site's timezone" in date_tag["description"]
    utc_tag = next(tag for tag in tags.json()["tags"] if tag["name"] == "DATEUTC")
    assert "UTC" in utc_tag["description"]

    folder = paired.put(
        "/api/settings/capture-folder-template",
        json={"template": "%DATE%/%IMAGETYPE%"},
    )
    filename = paired.put(
        "/api/settings/capture-filename-template",
        json={"template": "%DATE%_%SEQUENCE%"},
    )
    calibration = paired.put(
        "/api/settings/calibration-path-templates",
        json={
            "bias_folder_template": "Calibration/%DATE%/bias",
            "dark_folder_template": "Calibration/%DATE%/dark",
            "flat_folder_template": "Calibration/%DATE%/flat",
            "filename_template": "%DATE%_%IMAGETYPE%_%SEQUENCE%",
        },
    )

    assert folder.status_code == 200, folder.text
    assert filename.status_code == 200, filename.text
    assert calibration.status_code == 200, calibration.text


def test_capture_filename_template_rejects_paths_extensions_and_unknown_tags(
    paired: TestClient,
) -> None:
    for template in ("../frame", "frame.fits", "%BOGUS%"):
        response = paired.put(
            "/api/settings/capture-filename-template", json={"template": template}
        )
        assert response.status_code == 422, response.text


def test_calibration_path_templates_have_compatible_defaults_and_persist(
    paired: TestClient,
) -> None:
    defaults = paired.get("/api/settings/calibration-path-templates")
    assert defaults.status_code == 200
    assert defaults.json() == {
        "bias_folder_template": "Calibration/bias/singles",
        "dark_folder_template": "Calibration/darks/singles",
        "flat_folder_template": "Calibration/flats/%FILTER%",
        "filename_template": (
            "%IMAGETYPE%_%TARGET%_%EXPOSURE%_%CCDTEMP%_%FILTER%_%DATETIME%_%SEQUENCE%"
        ),
    }

    body = {
        "bias_folder_template": "Cal/bias",
        "dark_folder_template": "Cal/%IMAGETYPE%/%EXPOSURE%",
        "flat_folder_template": "Cal/flats/%FILTER%",
        "filename_template": "%IMAGETYPE%-%GAIN%-%SEQUENCE%",
    }
    saved = paired.put("/api/settings/calibration-path-templates", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json() == body
    assert paired.get("/api/settings/calibration-path-templates").json() == body


def test_calibration_path_templates_reject_invalid_values(paired: TestClient) -> None:
    body = {
        "bias_folder_template": "../bias",
        "dark_folder_template": "Cal/darks",
        "flat_folder_template": "Cal/flats",
        "filename_template": "frame.fits",
    }
    response = paired.put("/api/settings/calibration-path-templates", json=body)
    assert response.status_code == 422, response.text


def test_manual_beats_every_automatic_source(paired: TestClient) -> None:
    _fix(paired, "ip", 40.0)
    _fix(paired, "device", 47.0, timezone="Europe/Bratislava")
    body = _fix(paired, "browser", 48.0, accuracy_m=12.0)
    assert body["active"]["source"] == "browser"

    body = _fix(paired, "manual", 49.0)
    assert body["active"]["source"] == "manual"
    assert body["active"]["latitude_deg"] == 49.0
    assert set(body["sources"]) == {"ip", "device", "browser", "manual"}


def test_the_device_fix_carries_the_timezone_the_scope_reports(paired: TestClient) -> None:
    body = _fix(paired, "device", 48.15, timezone="Europe/Bratislava", elevation_m=150.0)
    assert body["sources"]["device"]["timezone"] == "Europe/Bratislava"
    assert body["active"]["source"] == "device"


def test_a_preferred_source_overrides_the_precedence(paired: TestClient) -> None:
    _fix(paired, "manual", 49.0)
    _fix(paired, "device", 47.0)
    response = paired.put("/api/settings/location/preferred-source", json={"source": "device"})
    assert response.status_code == 200
    assert response.json()["active"]["source"] == "device"


def test_preferring_a_source_that_has_no_fix_is_refused(paired: TestClient) -> None:
    response = paired.put("/api/settings/location/preferred-source", json={"source": "browser"})
    assert response.status_code == 422


def test_clearing_a_fix_falls_back_to_the_next_source(paired: TestClient) -> None:
    _fix(paired, "manual", 49.0)
    _fix(paired, "device", 47.0)
    response = paired.delete("/api/settings/location/fix/manual")
    assert response.status_code == 200
    assert response.json()["active"]["source"] == "device"


def test_location_changes_reach_the_snapshot(paired: TestClient) -> None:
    _fix(paired, "manual", 49.0)
    location = paired.get("/api/state").json()["snapshot"]["location"]
    assert location["source"] == "manual"
    assert location["latitude_deg"] == 49.0


def test_a_secret_value_never_appears_in_any_response(
    paired: TestClient, memory_keyring: MemoryKeyring
) -> None:
    secret = "super-secret-owm-key"
    response = paired.put("/api/settings/secrets", json={"name": "owm", "value": secret})
    assert response.status_code == 200
    assert response.json() == {"names": ["owm"]}
    assert secret not in response.text

    for path in ("/api/settings", "/api/settings/secrets", "/api/state"):
        assert secret not in paired.get(path).text

    # It really is in the keyring, and really is readable server-side.
    assert memory_keyring.get_password(SERVICE_NAME, "owm") == secret
    assert paired.get("/api/settings").json()["secret_names"] == ["owm"]


def test_configuration_export_round_trips_without_secrets_or_security(
    paired: TestClient, memory_keyring: MemoryKeyring
) -> None:
    secret = "never-export-this-value"
    paired.put("/api/settings/secrets", json={"name": "owm", "value": secret})
    paired.put(
        "/api/settings/appearance",
        json={
            "theme": "red",
            "font_scale": "large",
            "accent": "amber",
            "background_enabled": True,
            "background_opacity_percent": 22,
        },
    )
    exported = paired.get("/api/settings/configuration/export")
    assert exported.status_code == 200, exported.text
    bundle = exported.json()
    assert bundle["schema_version"] == 1
    assert bundle["settings"]["appearance"] == {
        "theme": "red",
        "font_scale": "large",
        "accent": "amber",
        "background_enabled": True,
        "background_opacity_percent": 22,
    }
    assert "security" not in bundle["settings"]
    assert "secrets" not in bundle["settings"]
    assert "secret_names" not in exported.text
    assert secret not in exported.text

    paired.put("/api/settings/appearance", json={"theme": "dark"})
    restored = paired.put("/api/settings/configuration/import", json=bundle)
    assert restored.status_code == 202, restored.text
    result = _terminal_operation(paired, restored)["result"]
    assert "appearance" in result["applied_sections"]
    assert paired.get("/api/settings").json()["appearance"] == bundle["settings"]["appearance"]
    assert memory_keyring.get_password(SERVICE_NAME, "owm") == secret


def test_configuration_import_validates_everything_before_writing(paired: TestClient, monkeypatch, tmp_path) -> None:
    bundle = paired.get("/api/settings/configuration/export").json()
    bundle["settings"]["appearance"]["theme"] = "red"
    bundle["settings"]["capture"]["folder"] = "/definitely/not/a/real/capture/folder"
    monkeypatch.setattr("ssc.api.routes.settings.user_pictures_folder", lambda: None)
    response = paired.put("/api/settings/configuration/import", json=bundle)
    assert response.status_code == 202
    assert _terminal_operation(paired, response)["status"] == "failed"
    assert paired.get("/api/settings").json()["appearance"]["theme"] == "dark"


def test_configuration_import_refuses_a_split_bundle_without_client_ui(
    paired: TestClient,
) -> None:
    bundle = paired.get("/api/settings/configuration/export").json()
    del bundle["settings"]["client"]

    response = paired.put("/api/settings/configuration/import", json=bundle)

    assert response.status_code == 422
    assert "complete client UI configuration" in response.json()["detail"]


def test_deleting_a_secret_removes_the_name_and_the_value(
    paired: TestClient, memory_keyring: MemoryKeyring
) -> None:
    paired.put("/api/settings/secrets", json={"name": "owm", "value": "x"})
    assert paired.delete("/api/settings/secrets/owm").json() == {"names": []}
    assert memory_keyring.get_password(SERVICE_NAME, "owm") is None


def test_the_control_pin_is_not_writable_through_the_secrets_endpoint(paired: TestClient) -> None:
    response = paired.put("/api/settings/secrets", json={"name": "control_pin", "value": "1234"})
    assert response.status_code == 409


def test_request_bodies_forbid_unknown_fields(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/location/fix",
        json={"source": "manual", "latitude_deg": 1.0, "longitude_deg": 2.0, "lat": 3.0},
    )
    assert response.status_code == 422
    assert "lat" in response.text


def test_appearance_round_trips_and_publishes(paired: TestClient) -> None:
    response = paired.put("/api/settings/appearance", json={"theme": "red"})
    assert response.status_code == 200
    assert services_of(paired).state.snapshot.settings.theme == "red"
    assert paired.get("/api/settings").json()["appearance"]["theme"] == "red"


def test_theme_accepts_auto_as_a_stored_choice(paired: TestClient) -> None:
    """`auto` is stored verbatim — the client, not this store, resolves it to
    `dark`/`red` from the site's own sunset/sunrise."""
    response = paired.put("/api/settings/appearance", json={"theme": "auto"})
    assert response.status_code == 200
    assert services_of(paired).state.snapshot.settings.theme == "auto"
    assert paired.get("/api/settings").json()["appearance"]["theme"] == "auto"


def test_an_unknown_theme_is_rejected(paired: TestClient) -> None:
    response = paired.put("/api/settings/appearance", json={"theme": "light"})
    assert response.status_code == 422


def test_font_scale_round_trips_and_publishes(paired: TestClient) -> None:
    """`font_scale` gets the identical treatment `theme` gets — server state,
    not a client preference, so every open client agrees (CLAUDE.md)."""
    response = paired.put(
        "/api/settings/appearance",
        json={"theme": "dark", "font_scale": "large"},
    )
    assert response.status_code == 200
    assert response.json()["font_scale"] == "large"
    assert services_of(paired).state.snapshot.settings.font_scale == "large"
    assert paired.get("/api/settings").json()["appearance"]["font_scale"] == "large"


def test_font_scale_defaults_to_normal(paired: TestClient) -> None:
    assert paired.get("/api/settings").json()["appearance"]["font_scale"] == "normal"
    assert services_of(paired).state.snapshot.settings.font_scale == "normal"


def test_an_unknown_font_scale_is_rejected(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/appearance",
        json={"theme": "dark", "font_scale": "huge"},
    )
    assert response.status_code == 422


def test_accent_round_trips_and_publishes(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/appearance",
        json={"theme": "dark", "font_scale": "normal", "accent": "violet"},
    )
    assert response.status_code == 200
    assert response.json()["accent"] == "violet"
    assert services_of(paired).state.snapshot.settings.accent == "violet"
    assert paired.get("/api/settings").json()["appearance"]["accent"] == "violet"


@pytest.mark.parametrize(
    "accent",
    ["teal", "indigo", "fuchsia", "orange", "lime", "blue-bright", "indigo-deep", "h-alpha", "sulfur", "oiii"],
)
def test_new_accent_colors_round_trip(paired: TestClient, accent: str) -> None:
    response = paired.put(
        "/api/settings/appearance",
        json={"theme": "dark", "font_scale": "normal", "accent": accent},
    )
    assert response.status_code == 200
    assert response.json()["accent"] == accent
    assert paired.get("/api/settings").json()["appearance"]["accent"] == accent


def test_accent_defaults_to_blue(paired: TestClient) -> None:
    assert paired.get("/api/settings").json()["appearance"]["accent"] == "blue"
    assert services_of(paired).state.snapshot.settings.accent == "blue"


def test_an_unknown_accent_is_rejected(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/appearance",
        json={"theme": "dark", "font_scale": "normal", "accent": "invisible"},
    )
    assert response.status_code == 422


def test_background_appearance_round_trips_and_is_bounded(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/appearance",
        json={"background_enabled": True, "background_opacity_percent": 28},
    )
    assert response.status_code == 200
    assert response.json()["background_enabled"] is True
    assert response.json()["background_opacity_percent"] == 28
    assert services_of(paired).state.snapshot.settings.background_enabled is True

    rejected = paired.put(
        "/api/settings/appearance",
        json={"background_enabled": True, "background_opacity_percent": 75},
    )
    assert rejected.status_code == 422


# -- horizon profile (DESIGN.md §5) ----------------------------------------- #


def _horizon_points(*pairs: tuple[float, float]) -> list[dict[str, float]]:
    return [{"azimuth_deg": az, "altitude_deg": alt} for az, alt in pairs]


def test_no_horizon_profile_is_none_rather_than_empty(paired: TestClient) -> None:
    response = paired.get("/api/settings/horizon")
    assert response.status_code == 200
    body = response.json()
    assert body == {"active": False, "points": None}
    assert paired.get("/api/settings").json()["horizon"] == {"active": False, "points": None}


def test_a_horizon_profile_round_trips(paired: TestClient) -> None:
    points = _horizon_points((0.0, 5.0), (90.0, 20.0), (180.0, 5.0), (270.0, 12.0))
    response = paired.put("/api/settings/horizon", json={"points": points})
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is True
    assert body["points"] == points

    fetched = paired.get("/api/settings/horizon").json()
    assert fetched == body
    assert paired.get("/api/settings").json()["horizon"] == body


def test_horizon_profile_needs_at_least_two_points(paired: TestClient) -> None:
    response = paired.put("/api/settings/horizon", json={"points": _horizon_points((0.0, 5.0))})
    assert response.status_code == 422


def test_horizon_profile_rejects_a_duplicate_azimuth(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/horizon",
        json={"points": _horizon_points((10.0, 5.0), (10.0, 8.0))},
    )
    assert response.status_code == 422
    assert "duplicate azimuth" in response.text


def test_horizon_profile_rejects_an_azimuth_out_of_range(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/horizon",
        json={"points": _horizon_points((0.0, 5.0), (360.0, 5.0))},
    )
    assert response.status_code == 422


def test_horizon_profile_rejects_an_altitude_below_the_floor(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/horizon",
        json={"points": _horizon_points((0.0, -20.0), (180.0, 5.0))},
    )
    assert response.status_code == 422


def test_clearing_a_horizon_profile_returns_it_to_unknown(paired: TestClient) -> None:
    points = _horizon_points((0.0, 5.0), (180.0, 5.0))
    paired.put("/api/settings/horizon", json={"points": points})
    response = paired.delete("/api/settings/horizon")
    assert response.status_code == 200
    assert response.json() == {"active": False, "points": None}
    assert paired.get("/api/settings/horizon").json() == {"active": False, "points": None}


def test_horizon_profile_changes_publish_to_the_snapshot(paired: TestClient) -> None:
    seq_before = paired.get("/api/state").json()["seq"]
    points = _horizon_points((0.0, 5.0), (180.0, 5.0))
    paired.put("/api/settings/horizon", json={"points": points})
    state = paired.get("/api/state").json()
    assert state["seq"] > seq_before
    assert state["snapshot"]["settings"]["horizon_profile_set"] is True


def test_configuration_import_replaces_unavailable_capture_path(paired, monkeypatch, tmp_path):
    pictures = tmp_path / "Pictures"
    monkeypatch.setattr("ssc.api.routes.settings.user_pictures_folder", lambda: pictures)
    bundle = paired.get("/api/settings/configuration/export").json()
    bundle["settings"]["capture"]["folder"] = "/missing/linux/mount/captures"
    response = paired.put("/api/settings/configuration/import", json=bundle)
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "succeeded", operation
    assert operation["result"]["warnings"]
    assert str(pictures) in operation["result"]["warnings"][0]
    assert paired.get("/api/settings/configuration/export").json()["settings"]["capture"]["folder"] == str(pictures)
    assert pictures.is_dir()
