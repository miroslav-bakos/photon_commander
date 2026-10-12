// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { PaneLayout } from "../../components/PaneLayout";
import { CapturePanel } from "../CapturePanel";
import { CapturePreviewPanel } from "./CapturePreviewPanel";
import { CaptureStatusPanel } from "./CaptureStatusPanel";
import { ObservingModeSwitch } from "../Observe/ObservingModeSwitch";
import { ObservingSessionPanel } from "../Observe/ObservingSessionPanel";
import { AutofocusChart } from "./AutofocusChart";

/**
 * The all-around capture control centre (owner brief, 2026-09-11): "usable
 * without having any object resolved", capture controls on the left and
 * capture options on the right, with the preview in the middle. This screen owns no
 * target — `PlanningTab` and `SchedulingTab` are where a target gets
 * resolved — so nothing here is gated on one, unlike `MountTab`'s own
 * capability-gated controls, which are reused as-is.
 *
 * Capture status and the capture form stay together on the left. Equipment
 * controls now live together on the Control screen, leaving preview and
 * capture preview as Capture's second working surface. Autofocus controls
 * stay in Control's Focuser card; measured diagnostics also remain visible here.
 *
 * Mount state is `undefined` (no snapshot yet) / `null` (no reading) /
 * present, the same three-way handling `MountTab` uses — this screen is not
 * the mount's only control surface, so it must stay honest about the same
 * states rather than assuming a reading exists just because this is a
 * different tab.
 */
export function CaptureTab() {
  return (
    <PaneLayout
      primary={
        <div className="space-y-3">
          <ObservingModeSwitch />
          <ObservingSessionPanel />
          <AutofocusChart />
          <details className="rounded-lg border border-border bg-surface p-3">
            <summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">Capture settings and calibration</summary>
            <CaptureStatusPanel />
            <CapturePanel />
          </details>
        </div>
      }
      secondary={
        <CapturePreviewPanel />
      }
    />
  );
}
