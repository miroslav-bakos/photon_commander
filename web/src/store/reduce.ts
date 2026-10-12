// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `(snapshot, event) -> snapshot`, ported field-for-field from
 * `ssc/state/reducer.py`. That module is the single definition of what an
 * event means; this is the same function so a replaying client reconstructs
 * exactly the snapshot the server would have. If the two ever disagree, the
 * `reduce.test.ts` fixture below and the backend's own
 * `tests/api/test_ws_replay.py` are where that would be caught — keep this in
 * sync with the Python by hand, since it is logic, not shape, and therefore
 * cannot be generated.
 *
 * No clock, no I/O: every timestamp comes out of the event.
 */
import type { StateEvent, StateSnapshot } from "../api/types";
import type { MountState } from "../api/mount";

const MAX_NOTICES = 50;

/**
 * Not yet in the generated `StateEvent` union: `ssc/state/events.py`'s
 * `MountChanged` (`type: "mount.changed"`, mirroring `DeviceChanged`) is
 * being built alongside the Mount tab (2026-09-10) but is not merged, so
 * `schema.d.ts` does not know it yet. Ported here by hand from that
 * in-flight `ssc/state/reducer.py` the same way every other case in this
 * file is — `case MountChanged(): return snapshot.model_copy(update={"mount":
 * event.mount})` — so the tab gets live updates each poll tick rather than
 * only on a full resnapshot. Delete this local type and fold the case back
 * into the plain `StateEvent` switch once `npm run api:codegen` picks up
 * the real one; re-verify the shape against the merged backend first.
 */
interface MountChangedEvent {
  type: "mount.changed";
  mount: MountState | null;
}

/** Same provisional treatment as `MountChangedEvent`: the backend emits
 * `devices.changed` (`ssc/state/events.py`) and codegen has not carried it
 * here yet. Fold into `StateEvent` once it does. */
interface DevicesChangedEvent {
  type: "devices.changed";
  devices: unknown;
}

export function reduce(
  snapshot: StateSnapshot,
  event: StateEvent | MountChangedEvent | DevicesChangedEvent,
): StateSnapshot {
  switch (event.type) {
    case "mount.changed":
      return { ...snapshot, mount: event.mount } as StateSnapshot;
    case "camera_fov.changed":
      return { ...snapshot, camera_fov: event.camera_fov ?? null };
    case "link.changed":
      return { ...snapshot, link: event.link };
    case "device.changed":
      return { ...snapshot, device: event.device ?? null };
    case "discovery.changed":
      return { ...snapshot, discovery: event.discovery };
    case "location.changed":
      return { ...snapshot, location: event.location ?? null };
    case "settings.changed":
      return { ...snapshot, settings: event.settings };
    case "auto_connect.changed":
      return { ...snapshot, auto_connect: event.auto_connect };
    case "notice.raised": {
      const kept = (snapshot.notices ?? []).filter((n) => n.id !== event.notice.id);
      kept.push(event.notice);
      return { ...snapshot, notices: kept.slice(-MAX_NOTICES) };
    }
    case "notice.cleared": {
      const kept = (snapshot.notices ?? []).filter((n) => n.id !== event.notice_id);
      return { ...snapshot, notices: kept };
    }
    case "device.event":
      return {
        ...snapshot,
        last_device_event: event.event,
        device_events_seen: snapshot.device_events_seen + 1,
      };
    case "plans.changed":
      return { ...snapshot, plans: event.plans };
    case "skymap.changed":
      return { ...snapshot, skymap: event.skymap };
    case "run.changed":
      return { ...snapshot, run: event.run ?? null };
    case "capture.changed":
      // Capture progress and completion are pushed exactly like run/PA
      // state. Without this case the server saved the FITS successfully but
      // the browser kept the initial `last_frame_id: null`, so the Capture
      // screen never displayed the new image until a full resnapshot.
      return { ...snapshot, capture: event.capture };
    case "devices.changed":
      return { ...snapshot, devices: event.devices ?? null } as StateSnapshot;
    case "polar_alignment.changed":
      // The whole `polar_alignment` section, replaced — mirrors `run.changed`
      // above. Without this case a phone at the tripod would only see the
      // per-point progress advance on the next full resnapshot rather than
      // after every slew/settle/capture/solve step, defeating the point of
      // the WS event the backend documents for exactly this tab.
      return { ...snapshot, polar_alignment: event.polar_alignment };
    case "operations.changed":
      return { ...snapshot, operations: event.operations };
    case "autofocus.changed":
      return { ...snapshot, autofocus: event.autofocus };
    default:
      // An event this build does not know must leave the snapshot alone.
      //
      // This `switch` had no default, so an unrecognised type fell straight
      // through and the function returned `undefined` — which replaced the
      // whole snapshot with nothing. The UI rendered correctly from the
      // initial snapshot and then blanked on the first unknown event, which
      // reads as "reverts to no location set after a second" and is
      // indistinguishable from the backend having lost the data. Observed
      // 2026-09-10 the moment `devices.changed` shipped ahead of this file.
      //
      // A newer server is expected — SSC's own WS contract is versioned and a
      // client may lag a deploy — so ignoring an unknown event is correct.
      // Destroying state over one never is.
      return snapshot;
  }
}

export function replay(snapshot: StateSnapshot, events: StateEvent[]): StateSnapshot {
  return events.reduce(reduce, snapshot);
}
