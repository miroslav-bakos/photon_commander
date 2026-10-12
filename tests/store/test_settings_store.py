"""Config round-trips, location provenance, and the remembered address."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ssc.capture.pathtemplate import (
    DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    INTERIM_DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    PREVIOUS_DEFAULT_CAPTURE_FILENAME_TEMPLATE,
)
from ssc.settings.capture_folder import CaptureFolderError
from ssc.settings.location import resolve
from ssc.settings.models import HorizonPointFix, LocationFix, LocationSettings
from ssc.settings.store import SettingsError, SettingsStore
from ssc.state.models import DeviceDescriptor, LocationSource
from ssc.store.database import Database

MOMENT = datetime(2026, 8, 28, 21, 0, tzinfo=UTC)


def fix(latitude: float, **extra: object) -> LocationFix:
    return LocationFix.model_validate(
        {"latitude_deg": latitude, "longitude_deg": 17.1, "acquired_at": MOMENT, **extra}
    )


def device(host: str, *, reachable: bool = True) -> DeviceDescriptor:
    return DeviceDescriptor(
        host=host,
        control_port=4701,
        reachable=reachable,
        firmware="8.46" if reachable else None,
        note=None if reachable else "TimeoutError",
    )


async def test_sections_survive_a_restart(settings: SettingsStore, migrated: Database) -> None:
    await settings.record_fix(LocationSource.MANUAL, fix(49.0))
    reloaded = SettingsStore(migrated)
    await reloaded.load()
    active = reloaded.active_location()
    assert active is not None
    assert active.latitude_deg == 49.0
    assert active.source is LocationSource.MANUAL


async def test_healthy_route_survives_discovery_and_restart(settings: SettingsStore, migrated: Database) -> None:
    await settings.remember_address(device("10.0.0.7"))
    await settings.remember_healthy_endpoint("10.0.0.7", 4701)
    await settings.remember_address(device("10.0.0.8"))
    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert reloaded.device.last_healthy_host == "10.0.0.7"
    assert reloaded.device.last_healthy_control_port == 4701


async def test_an_unparsable_section_stops_the_process_rather_than_resetting_it(
    migrated: Database,
) -> None:
    await migrated.execute(
        "INSERT INTO config (key, value, updated_at) VALUES ('location', ?, ?)",
        ('{"fixes": "not a mapping"}', MOMENT.isoformat()),
    )
    store = SettingsStore(migrated)
    with pytest.raises(SettingsError, match="Refusing to start"):
        await store.load()


async def test_an_appearance_row_from_before_units_and_language_were_removed_still_loads(
    migrated: Database,
) -> None:
    """A row written by a previous build — the owner's own live database has
    exactly this shape — must still load rather than crashing startup."""
    await migrated.execute(
        "INSERT INTO config (key, value, updated_at) VALUES ('appearance', ?, ?)",
        (
            '{"units":"metric","theme":"dark","font_scale":"normal","language":"en"}',
            MOMENT.isoformat(),
        ),
    )
    store = SettingsStore(migrated)
    await store.load()
    assert store.appearance.theme == "dark"
    assert store.appearance.font_scale == "normal"
    assert store.appearance.accent == "blue"
    assert not hasattr(store.appearance, "units")
    assert not hasattr(store.appearance, "language")


def test_precedence_is_manual_then_browser_then_device_then_ip() -> None:
    settings = LocationSettings(
        fixes={
            LocationSource.IP: fix(40.0),
            LocationSource.DEVICE: fix(47.0),
            LocationSource.BROWSER: fix(48.0),
            LocationSource.MANUAL: fix(49.0),
        }
    )
    assert resolve(settings) is not None
    for expected, source in (
        (49.0, LocationSource.MANUAL),
        (48.0, LocationSource.BROWSER),
        (47.0, LocationSource.DEVICE),
        (40.0, LocationSource.IP),
    ):
        active = resolve(settings)
        assert active is not None
        assert (active.latitude_deg, active.source) == (expected, source)
        settings = settings.model_copy(
            update={"fixes": {k: v for k, v in settings.fixes.items() if k is not source}}
        )
    assert resolve(settings) is None


def test_no_fix_resolves_to_none_not_to_zero_zero() -> None:
    assert resolve(LocationSettings()) is None


async def test_only_a_reachable_address_becomes_the_remembered_one(
    settings: SettingsStore,
) -> None:
    await settings.remember_address(device("10.0.0.9", reachable=False))
    assert settings.device.host is None

    await settings.remember_address(device("10.0.0.7"))
    assert settings.device.host == "10.0.0.7"
    assert settings.device.last_good_at is not None

    known = await settings.known_addresses()
    assert {entry.host for entry in known} == {"10.0.0.9", "10.0.0.7"}
    assert known[0].host == "10.0.0.7", "the last good address sorts first"
    unreachable = next(entry for entry in known if entry.host == "10.0.0.9")
    assert unreachable.last_good_at is None
    assert unreachable.last_seen_at is not None


async def test_forgetting_the_remembered_address_clears_it(settings: SettingsStore) -> None:
    await settings.remember_address(device("10.0.0.7"))
    await settings.forget_address("10.0.0.7", 4701)
    assert settings.device.host is None
    assert await settings.known_addresses() == []


async def test_auto_connect_defaults_on_and_is_settable(
    settings: SettingsStore, migrated: Database
) -> None:
    assert settings.device.auto_connect is True

    await settings.set_auto_connect(False)
    assert settings.device.auto_connect is False

    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert reloaded.device.auto_connect is False


async def test_auto_connect_survives_a_new_address_being_remembered(
    settings: SettingsStore,
) -> None:
    """The master switch is independent of *which* address is remembered — a
    fresh probe finding a new host must not silently re-enable it."""
    await settings.set_auto_connect(False)
    await settings.remember_address(device("10.0.0.7"))
    assert settings.device.auto_connect is False

    await settings.forget_address("10.0.0.7", 4701)
    assert settings.device.auto_connect is False


async def test_hardware_profile_survives_restart_and_is_bound_to_address(
    settings: SettingsStore, migrated: Database
) -> None:
    await settings.remember_address(device("10.0.0.7"))
    await settings.set_hardware_profile("zwo-s50")

    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert reloaded.device.hardware_profile_id == "zwo-s50"
    assert reloaded.device.hardware_profile_source == "manual"
    assert (await reloaded.known_addresses())[0].hardware_profile_id == "zwo-s50"

async def test_the_summary_carries_names_but_never_values(settings: SettingsStore) -> None:
    await settings.add_secret_name("owm")
    summary = settings.summary()
    assert summary.configured_secret_names == ["owm"]
    assert "value" not in summary.model_dump()


async def test_preferring_a_source_without_a_fix_is_refused(settings: SettingsStore) -> None:
    with pytest.raises(ValueError, match="no browser location fix"):
        await settings.set_preferred_source(LocationSource.BROWSER)


# -- horizon profile (DESIGN.md §5) ----------------------------------------- #


async def test_no_horizon_profile_resolves_to_none_not_an_empty_mask(
    settings: SettingsStore,
) -> None:
    assert settings.horizon.points is None
    assert settings.active_horizon_profile() is None
    assert settings.summary().horizon_profile_set is False


async def test_a_horizon_profile_survives_a_restart(
    settings: SettingsStore, migrated: Database
) -> None:
    points = [
        HorizonPointFix(azimuth_deg=0.0, altitude_deg=5.0),
        HorizonPointFix(azimuth_deg=90.0, altitude_deg=20.0),
        HorizonPointFix(azimuth_deg=180.0, altitude_deg=5.0),
    ]
    await settings.set_horizon(points)
    assert settings.summary().horizon_profile_set is True
    profile = settings.active_horizon_profile()
    assert profile is not None
    assert profile.altitude_at(0.0) == 5.0

    reloaded = SettingsStore(migrated)
    await reloaded.load()
    reloaded_profile = reloaded.active_horizon_profile()
    assert reloaded_profile is not None
    assert reloaded_profile.altitude_at(90.0) == 20.0


async def test_clearing_a_horizon_profile_returns_it_to_none(settings: SettingsStore) -> None:
    await settings.set_horizon(
        [
            HorizonPointFix(azimuth_deg=0.0, altitude_deg=5.0),
            HorizonPointFix(azimuth_deg=180.0, altitude_deg=5.0),
        ]
    )
    await settings.set_horizon(None)
    assert settings.horizon.points is None
    assert settings.active_horizon_profile() is None


async def test_capture_folder_unset_by_default(settings: SettingsStore) -> None:
    assert settings.capture.folder is None


async def test_a_valid_capture_folder_is_persisted_and_survives_a_restart(
    settings: SettingsStore, migrated: Database, tmp_path
) -> None:
    await settings.set_capture_folder(tmp_path)
    assert settings.capture.folder == tmp_path

    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert reloaded.capture.folder == tmp_path


async def test_an_invalid_capture_folder_is_refused_and_not_persisted(
    settings: SettingsStore, tmp_path
) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(CaptureFolderError):
        await settings.set_capture_folder(missing)
    assert settings.capture.folder is None


async def test_clearing_the_capture_folder_needs_no_validation(
    settings: SettingsStore, tmp_path
) -> None:
    await settings.set_capture_folder(tmp_path)
    await settings.set_capture_folder(None)
    assert settings.capture.folder is None


async def test_capture_folder_template_defaults_to_target_date_imagetype(
    settings: SettingsStore,
) -> None:
    assert settings.capture.folder_template == "%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%"


@pytest.mark.parametrize(
    "legacy_default",
    [
        PREVIOUS_DEFAULT_CAPTURE_FILENAME_TEMPLATE,
        INTERIM_DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    ],
)
async def test_previous_default_filename_templates_migrate_on_restart(
    settings: SettingsStore, migrated: Database, legacy_default: str
) -> None:
    await settings.set_capture_filename_template(legacy_default)

    reloaded = SettingsStore(migrated)
    await reloaded.load()

    assert reloaded.capture.filename_template == DEFAULT_CAPTURE_FILENAME_TEMPLATE


async def test_custom_filename_template_is_preserved_on_restart(
    settings: SettingsStore, migrated: Database
) -> None:
    custom = "%IMAGETYPE%-%SEQUENCE%"
    await settings.set_capture_filename_template(custom)

    reloaded = SettingsStore(migrated)
    await reloaded.load()

    assert reloaded.capture.filename_template == custom


async def test_calibration_path_templates_default_and_persist(
    settings: SettingsStore, migrated: Database
) -> None:
    assert settings.capture.calibration_bias_folder_template == "Calibration/bias/singles"
    assert settings.capture.calibration_dark_folder_template == "Calibration/darks/singles"
    assert settings.capture.calibration_flat_folder_template == "Calibration/flats/%FILTER%"
    assert settings.capture.calibration_filename_template is None

    await settings.set_calibration_path_templates(
        bias_folder_template="Cal/%IMAGETYPE%/bias",
        dark_folder_template="Cal/%IMAGETYPE%/%EXPOSURE%",
        flat_folder_template="Cal/%FILTER%/flats",
        filename_template="%IMAGETYPE%-%GAIN%-%SEQUENCE%",
    )
    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert reloaded.capture.calibration_dark_folder_template == "Cal/%IMAGETYPE%/%EXPOSURE%"
    assert reloaded.capture.calibration_filename_template == "%IMAGETYPE%-%GAIN%-%SEQUENCE%"


async def test_a_valid_capture_folder_template_is_persisted_and_survives_a_restart(
    settings: SettingsStore, migrated: Database
) -> None:
    await settings.set_capture_folder_template("%IMAGETYPE%/%TARGET%")
    assert settings.capture.folder_template == "%IMAGETYPE%/%TARGET%"

    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert reloaded.capture.folder_template == "%IMAGETYPE%/%TARGET%"


async def test_an_invalid_capture_folder_template_is_refused_and_not_persisted(
    settings: SettingsStore,
) -> None:
    from ssc.capture.pathtemplate import PathTemplateError

    with pytest.raises(PathTemplateError):
        await settings.set_capture_folder_template("%NOT_A_REAL_TAG%")
    assert settings.capture.folder_template == "%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%"


async def test_setting_the_folder_and_the_template_do_not_clobber_each_other(
    settings: SettingsStore, tmp_path
) -> None:
    """Regression: both setters must update only their own field — a naive
    ``CaptureSettings(folder=...)`` construction would silently reset the
    other back to its default."""
    await settings.set_capture_folder_template("%IMAGETYPE%/%TARGET%")
    await settings.set_capture_folder(tmp_path)
    assert settings.capture.folder == tmp_path
    assert settings.capture.folder_template == "%IMAGETYPE%/%TARGET%"

    await settings.set_capture_folder_template("%TARGET%/%IMAGETYPE%")
    assert settings.capture.folder == tmp_path
    assert settings.capture.folder_template == "%TARGET%/%IMAGETYPE%"


async def test_concurrent_device_updates_survive_reload(settings, migrated, monkeypatch):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    original = settings._write

    async def delayed(section, value):
        if section == 'device' and not value.auto_connect:
            entered.set()
            await release.wait()
        await original(section, value)

    monkeypatch.setattr(settings, '_write', delayed)
    first = asyncio.create_task(settings.set_auto_connect(False))
    await entered.wait()
    second = asyncio.create_task(settings.remember_healthy_endpoint('10.0.0.7', 4701))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert not reloaded.device.auto_connect
    assert reloaded.device.last_healthy_host == '10.0.0.7'


async def test_broadcast_switch_preserves_concurrent_stream_configuration(settings, migrated, monkeypatch):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    original = settings._write

    async def delayed(section, value):
        if section == 'streaming' and value.wide_camera_picture_in_picture:
            entered.set()
            await release.wait()
        await original(section, value)

    monkeypatch.setattr(settings, '_write', delayed)
    first = asyncio.create_task(settings.set_streaming(
        broadcast_when_unparked=False, wide_camera_picture_in_picture=True))
    await entered.wait()
    second = asyncio.create_task(settings.set_broadcast_when_unparked(True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    reloaded = SettingsStore(migrated)
    await reloaded.load()
    assert reloaded.streaming.broadcast_when_unparked
    assert reloaded.streaming.wide_camera_picture_in_picture
