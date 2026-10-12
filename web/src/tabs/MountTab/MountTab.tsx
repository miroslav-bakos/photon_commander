// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useRef, useState, type ReactNode } from "react";

import { Card, CardTitle } from "../../components/Card";
import { useMountState } from "../../api/mount";
import { useCaptureState } from "../../api/capture";
import { DewHeaterPanel } from "../Capture/DewHeaterPanel";
import { FilterWheelPanel } from "../Capture/FilterWheelPanel";
import { FocuserPanel } from "../Capture/FocuserPanel";
import { JogPad, StopButton } from "./JogPad";
import { DitheringPanel } from "./DitheringPanel";
import { ParkHomeCard } from "./ParkHomeCard";
import { PointingReadout } from "./PointingReadout";
import { TrackingCard } from "./TrackingCard";
import { WedgeSimulatorCard } from "./WedgeSimulatorCard";
import { useConnectionStore } from "../../store/connectionStore";
import { usePolarDebugStore } from "../../store/polarDebugStore";
import type { ActiveMountMotion } from "./MountMotionStopButton";

type ControlPanelId = "pointing" | "jog" | "tracking" | "park-home" | "focuser" | "filter-wheel" | "dew-heater" | "dithering";

function requestedControlPanel(): ControlPanelId | null {
  const query = window.location.hash.split("?", 2)[1];
  const panel = new URLSearchParams(query ?? "").get("panel");
  return panel === "pointing" || panel === "jog" || panel === "tracking" || panel === "park-home"
    || panel === "focuser" || panel === "filter-wheel" || panel === "dew-heater" || panel === "dithering"
    ? panel
    : null;
}

function EquipmentCell({ children, panel, highlighted }: { children: ReactNode; panel: ControlPanelId; highlighted: boolean }) {
  return (
    <div
      className={`min-w-0 scroll-mt-24 rounded-lg empty:hidden ${highlighted ? "ring-2 ring-accent ring-offset-2 ring-offset-bg" : ""}`}
      data-control-panel={panel}
      tabIndex={-1}
    >
      {children}
    </div>
  );
}

/**
 * DESIGN.md §4.4b: "you cannot polar align a scope you have not physically
 * placed and pointed" — this tab is that manual pointing surface, placed
 * before the (future) polar-alignment tab for exactly that reason.
 *
 * Every mount control here is gated on `mount.capabilities`, not offered and left
 * to fail: this device cannot slew alt/az, cannot set a park position and
 * has no custom tracking rate, so none of those controls exist (the tab's
 * brief — "a control the device will refuse is worse than an absent one").
 */
export function MountTab() {
  const mount = useMountState();
  const capture = useCaptureState();
  const connected = useConnectionStore((state) => state.snapshot?.link?.control_connected === true);
  const debugEnabled = usePolarDebugStore((state) => state.enabled);
  const dithering = capture?.in_progress === true && capture.stage === "dithering";
  const [activeMotion, setActiveMotion] = useState<ActiveMountMotion | null>(null);
  const [highlightedPanel, setHighlightedPanel] = useState<ControlPanelId | null>(() => requestedControlPanel());
  const observedMoving = useRef(false);
  const trackedMotion = useRef<ActiveMountMotion | null>(null);
  const motionStartedAfterPoll = useRef<string | null>(null);

  useEffect(() => {
    const requested = requestedControlPanel();
    if (requested === null) return;
    const element = document.querySelector<HTMLElement>(`[data-control-panel="${requested}"]`);
    if (element === null) return;
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    element.focus({ preventScroll: true });
    const timer = window.setTimeout(() => { setHighlightedPanel(null); }, 2500);
    return () => { window.clearTimeout(timer); };
  }, []);

  useEffect(() => {
    if (activeMotion === null) {
      observedMoving.current = false;
      trackedMotion.current = null;
      motionStartedAfterPoll.current = null;
      return;
    }
    if (trackedMotion.current !== activeMotion) {
      trackedMotion.current = activeMotion;
      motionStartedAfterPoll.current = mount?.polled_at ?? null;
      observedMoving.current = false;
      return;
    }
    if (mount?.slewing === true) {
      observedMoving.current = true;
      return;
    }
    const reachedDestination =
      (activeMotion === "park" && mount?.at_park === true)
      || (activeMotion === "find-home" && mount?.at_home === true);
    const settledOnFreshPoll =
      mount?.slewing === false
      && mount.polled_at !== null
      && mount.polled_at !== motionStartedAfterPoll.current;
    // Park and Find home have authoritative destination flags. A transient
    // `slewing=false` poll before those flags arrive must not make their
    // progress/Stop controls disappear. A coordinate slew has no equivalent
    // destination flag, so it alone retains the settled-poll fallback.
    const slewSettled = activeMotion === "slew"
      && (settledOnFreshPoll || (observedMoving.current && mount?.slewing === false));
    if (reachedDestination || slewSettled) {
      observedMoving.current = false;
      setActiveMotion(null);
    }
  }, [activeMotion, mount?.at_home, mount?.at_park, mount?.polled_at, mount?.slewing]);

  return (
    <div data-mount-control-source className="space-y-3">
      {dithering && (
        <div
          role="status"
          className="rounded-md border border-warning bg-surface-raised p-3 text-sm text-warning"
        >
          <p className="font-semibold">Dithering mount…</p>
          <p className="mt-1 text-xs">
            The next loop exposure waits until the mount stops and settles on the new point.
          </p>
        </div>
      )}
      {debugEnabled && <WedgeSimulatorCard />}
      <div
        aria-label="Equipment controls"
        className="grid grid-cols-1 items-start gap-3 md:grid-cols-2 xl:grid-cols-4"
      >
        {mount === undefined && (
          <EquipmentCell panel="pointing" highlighted={highlightedPanel === "pointing"}>
            <Card>
              <CardTitle>Pointing</CardTitle>
              <p className="text-sm text-fg-muted">Waiting for state…</p>
            </Card>
          </EquipmentCell>
        )}
        {mount === null && (
          <EquipmentCell panel="pointing" highlighted={highlightedPanel === "pointing"}>
            <Card>
              <CardTitle>Pointing</CardTitle>
              <p className="text-sm text-fg-muted">
                No mount reading yet. Connect the scope from Settings, or wait for the first poll.
              </p>
              {/* A missing reading is not proof that motion stopped. */}
              {connected && (
                <div className="mt-3 max-w-48">
                  <StopButton />
                </div>
              )}
            </Card>
          </EquipmentCell>
        )}
        {mount !== undefined && mount !== null && (
          <>
            <EquipmentCell panel="pointing" highlighted={highlightedPanel === "pointing"}>
              <Card>
                <CardTitle>Pointing</CardTitle>
                <PointingReadout mount={mount} />
              </Card>
            </EquipmentCell>
            <EquipmentCell panel="jog" highlighted={highlightedPanel === "jog"}>
              <Card>
                <CardTitle>Jog</CardTitle>
                <JogPad
                  canMovePrimary={mount.capabilities?.can_move_primary_axis ?? false}
                  canMoveSecondary={mount.capabilities?.can_move_secondary_axis ?? false}
                  axisRateMaxDegS={mount.capabilities?.axis_rate_max_deg_s ?? null}
                  parked={mount.at_park === true}
                  showStop={connected}
                />
              </Card>
            </EquipmentCell>
            <EquipmentCell panel="tracking" highlighted={highlightedPanel === "tracking"}>
              <Card title="Tracking" collapseKey="mount.tracking">
                <TrackingCard mount={mount} />
              </Card>
            </EquipmentCell>
            <EquipmentCell panel="park-home" highlighted={highlightedPanel === "park-home"}>
              <Card title="Park / home" collapseKey="mount.parkHome">
                <ParkHomeCard
                  mount={mount}
                  activeMotion={activeMotion}
                  setActiveMotion={setActiveMotion}
                />
              </Card>
            </EquipmentCell>
          </>
        )}
        <EquipmentCell panel="focuser" highlighted={highlightedPanel === "focuser"}><FocuserPanel /></EquipmentCell>
        <EquipmentCell panel="filter-wheel" highlighted={highlightedPanel === "filter-wheel"}><FilterWheelPanel /></EquipmentCell>
        <EquipmentCell panel="dew-heater" highlighted={highlightedPanel === "dew-heater"}><DewHeaterPanel /></EquipmentCell>
        <EquipmentCell panel="dithering" highlighted={highlightedPanel === "dithering"}><DitheringPanel /></EquipmentCell>
      </div>
    </div>
  );
}
