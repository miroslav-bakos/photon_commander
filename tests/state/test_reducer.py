"""The reducer is the shared definition of what an event means."""

from __future__ import annotations

from datetime import UTC, datetime

from ssc.state.events import (
    AutoConnectChanged,
    DeviceChanged,
    DeviceEventReceived,
    DiscoveryChanged,
    LinkChanged,
    LocationChanged,
    SettingsChanged,
)
from ssc.state.models import (
    AutoConnectState,
    DeviceDescriptor,
    DeviceEventRecord,
    DiscoveryState,
    LinkState,
    LinkStatus,
    LocationSource,
    LocationState,
    SettingsSummary,
    StateSnapshot,
)
from ssc.state.reducer import reduce

MOMENT = datetime(2026, 8, 28, 21, 0, tzinfo=UTC)
BASE = StateSnapshot(server_started_at=MOMENT)


def test_every_section_is_replaced_wholesale() -> None:
    snapshot = reduce(BASE, LinkChanged(link=LinkState(status=LinkStatus.READY)))
    snapshot = reduce(
        snapshot,
        DeviceChanged(device=DeviceDescriptor(host="10.0.0.7", control_port=4701, reachable=True)),
    )
    snapshot = reduce(snapshot, DiscoveryChanged(discovery=DiscoveryState(running=True, total=254)))
    snapshot = reduce(
        snapshot,
        LocationChanged(
            location=LocationState(
                latitude_deg=48.15,
                longitude_deg=17.11,
                source=LocationSource.DEVICE,
                acquired_at=MOMENT,
            )
        ),
    )
    snapshot = reduce(snapshot, SettingsChanged(settings=SettingsSummary(theme="red")))

    assert snapshot.link.status is LinkStatus.READY
    assert snapshot.device is not None and snapshot.device.host == "10.0.0.7"
    assert snapshot.discovery.total == 254
    assert snapshot.location is not None and snapshot.location.source is LocationSource.DEVICE
    assert snapshot.settings.theme == "red"
    assert snapshot.server_started_at == MOMENT, "the reducer must not invent a clock"


def test_auto_connect_state_is_replaced_wholesale() -> None:
    snapshot = reduce(
        BASE,
        AutoConnectChanged(
            auto_connect=AutoConnectState(enabled=True, attempt=2, reason="retrying")
        ),
    )
    assert snapshot.auto_connect.attempt == 2
    assert snapshot.auto_connect.reason == "retrying"
    assert BASE.auto_connect.attempt == 0, "the reducer must not mutate its input"


def test_the_base_snapshot_is_never_mutated() -> None:
    reduce(BASE, LinkChanged(link=LinkState(status=LinkStatus.DEGRADED)))
    assert BASE.link.status is LinkStatus.DISCONNECTED


def test_device_events_are_counted_and_the_last_one_kept() -> None:
    snapshot = BASE
    for index in range(3):
        snapshot = reduce(
            snapshot,
            DeviceEventReceived(
                event=DeviceEventRecord(name=f"PiStatus{index}", received_at=MOMENT)
            ),
        )
    assert snapshot.device_events_seen == 3
    assert snapshot.last_device_event is not None
    assert snapshot.last_device_event.name == "PiStatus2"


def test_skymap_section_is_replaced_wholesale() -> None:
    from ssc.state.events import SkymapChanged
    from ssc.state.models import SkymapState, SurveyCacheState

    section = SkymapState(
        surveys=[SurveyCacheState(id="legacy-surveys-dr11", enabled=True, cached_tiles=7)],
        cache_bytes=700,
        cache_max_bytes=1000,
        upstream_reachable=True,
    )
    snapshot = reduce(BASE, SkymapChanged(skymap=section))
    assert snapshot.skymap == section
    assert BASE.skymap.surveys == [], "the base snapshot was not mutated"
    assert snapshot.server_started_at == MOMENT


def test_run_section_is_replaced_wholesale_and_can_be_cleared() -> None:
    from ssc.state.events import RunChanged
    from ssc.state.models import RunAction, RunItemState, RunItemStatus, RunLifecycle, RunState

    section = RunState(
        run_id="r1",
        plan_id="p1",
        plan_name="tonight",
        plan_revision=2,
        night_date=MOMENT.date(),
        status=RunLifecycle.RUNNING,
        started_at=MOMENT,
        current_item_id="goto",
        items=[
            RunItemState(
                item_id="goto",
                kind="goto_and_stack",
                enabled=True,
                status=RunItemStatus.RUNNING,
                attempt=1,
                reason="issuing slew_to (attempt 1)",
            )
        ],
        last_decision="issuing slew_to (attempt 1)",
        last_decision_at=MOMENT,
        journal_length=2,
        available_actions=[RunAction.PAUSE, RunAction.SKIP, RunAction.STOP, RunAction.RETRY],
    )
    snapshot = reduce(BASE, RunChanged(run=section))
    assert snapshot.run == section
    assert BASE.run is None, "the base snapshot was not mutated"
    assert reduce(snapshot, RunChanged(run=None)).run is None


def test_mount_section_is_replaced_wholesale_and_carries_a_freshness_stamp() -> None:
    """DESIGN.md §4.4b: the snapshot has to carry *where the mount is* with a
    stamp of when that was true, and ``None`` before anything has ever been
    read — never a zero and never a fabricated position."""
    from ssc.state.events import MountChanged
    from ssc.state.models import MountState

    assert BASE.mount is None, "nothing has polled yet"

    reading = MountState(
        polled_at=MOMENT,
        stale=False,
        ra_hours=12.5,
        dec_deg=45.0,
        alt_deg=30.0,
        az_deg=180.0,
        tracking=True,
        at_park=False,
        slewing=False,
    )
    snapshot = reduce(BASE, MountChanged(mount=reading))
    assert snapshot.mount == reading
    assert snapshot.mount is not None and snapshot.mount.polled_at == MOMENT
    assert BASE.mount is None, "the base snapshot was not mutated"
    assert reduce(snapshot, MountChanged(mount=None)).mount is None


def test_mount_stale_is_unknown_never_polled_and_true_past_the_threshold() -> None:
    """A property that could not be read is unknown (CLAUDE.md invariant 7);
    a reading old enough is exactly the same "do not trust this" signal, both
    represented the same honest way."""
    from datetime import timedelta

    from ssc.state.models import MOUNT_STALE_AFTER_S, mount_stale

    assert mount_stale(None, now=MOMENT) is True, "never polled is stale by definition"
    assert mount_stale(MOMENT, now=MOMENT) is False, "a reading taken this instant is fresh"
    just_inside = MOMENT + timedelta(seconds=MOUNT_STALE_AFTER_S - 0.5)
    assert mount_stale(MOMENT, now=just_inside) is False
    just_outside = MOMENT + timedelta(seconds=MOUNT_STALE_AFTER_S + 0.5)
    assert mount_stale(MOMENT, now=just_outside) is True


def test_refresh_mount_staleness_settles_it_without_touching_anything_else() -> None:
    """The function every full-snapshot read (``GET /api/state``, every WS
    ``snapshot`` frame) calls — see ``MountState``'s own docstring for why
    this is not a ``@computed_field`` instead."""
    from datetime import timedelta

    from ssc.state.events import MountChanged
    from ssc.state.models import MOUNT_STALE_AFTER_S, MountState, refresh_mount_staleness

    reading = MountState(polled_at=MOMENT, stale=False, ra_hours=1.0)
    snapshot = reduce(BASE, MountChanged(mount=reading))

    fresh_now = MOMENT + timedelta(seconds=1)
    refreshed = refresh_mount_staleness(snapshot, now=fresh_now)
    assert refreshed.mount is not None and refreshed.mount.stale is False
    assert refreshed.mount.ra_hours == 1.0, "only staleness changes, never the reading itself"

    old_now = MOMENT + timedelta(seconds=MOUNT_STALE_AFTER_S + 1)
    gone_stale = refresh_mount_staleness(snapshot, now=old_now)
    assert gone_stale.mount is not None and gone_stale.mount.stale is True
    assert snapshot.mount is not None and snapshot.mount.stale is False, (
        "refresh_mount_staleness must not mutate its input"
    )

    assert refresh_mount_staleness(BASE, now=old_now).mount is None, (
        "no mount reading yet: nothing to refresh"
    )
