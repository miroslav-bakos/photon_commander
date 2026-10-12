// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { SetupLink } from "../Setup/SetupTab";
import { LanguagePicker } from "../../i18n/LanguagePicker";
import type { ReactNode } from "react";

import { Card } from "../../components/Card";
import { AboutPanel } from "./AboutPanel";
import { AppearancePanel } from "./AppearancePanel";
import { CaptureFolderPanel } from "./CaptureFolderPanel";
import { DeviceConnectionPanel } from "./DeviceConnectionPanel";
import { ExternalToolsPanel } from "./ExternalToolsPanel";
import { HorizonPanel } from "./HorizonPanel";
import { LocationPanel } from "./LocationPanel";
import { ObjectInfoSettingsPanel } from "./ObjectInfoSettingsPanel";
import { OfflineMapsPanel } from "./OfflineMapsPanel";
import { PlanningEquipmentPanel } from "./PlanningEquipmentPanel";
import { PolarDebugSettingsPanel } from "./PolarDebugSettingsPanel";
import { RemoteAccessPanel } from "./RemoteAccessPanel";
import { SkymapAutofetchPanel } from "./SkymapAutofetchPanel";
import { StreamingSettingsPanel } from "./StreamingSettingsPanel";
import { WeatherPanel } from "./WeatherPanel";
import { ExportImportPanel } from "./ExportImportPanel";

function SettingsBlock({
  title,
  description,
  collapseKey,
  defaultCollapsed = true,
  children,
}: {
  title: string;
  description: string;
  collapseKey: string;
  defaultCollapsed?: boolean;
  children: ReactNode;
}) {
  return (
    <Card
      title={title}
      collapseKey={collapseKey}
      defaultCollapsed={defaultCollapsed}
      className="mb-4 inline-block w-full break-inside-avoid"
    >
      <p className="mb-3 text-sm text-fg-muted">{description}</p>
      <div className="space-y-4">{children}</div>
    </Card>
  );
}

/** A scan-first Settings index. Every category starts folded on a new
 * browser, so the page presents a short vocabulary of user intentions
 * instead of mounting every settings form and live query at once. Expanded
 * choices are remembered by Card/useCollapsed for that browser. */
export function SettingsTab() {
  return (
    <div
      data-testid="settings-columns"
      className="mx-auto min-w-0 max-w-6xl columns-1 gap-4 lg:columns-2"
    >
      <Card className="mb-4"><LanguagePicker /><SetupLink /></Card>
      <SettingsBlock
        title="Equipment"
        description="Choose the telescope and lens, configure Planning defaults, and manage Seestar connections."
        collapseKey="settings.telescopeConnection"
      >
        <DeviceConnectionPanel />
        <PlanningEquipmentPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Observing site"
        description="Location, timezone, and the local horizon that constrain what Photon Commander can observe."
        collapseKey="settings.observingSite"
      >
        <LocationPanel />
        <HorizonPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Capture files"
        description="Where captured FITS files are stored and how their folders and filenames are formatted."
        collapseKey="settings.captureFiles"
      >
        <CaptureFolderPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Broadcasting"
        description="Configure RTSP, wide-camera handoff, picture-in-picture, and YouTube Live."
        collapseKey="settings.broadcasting"
      >
        <StreamingSettingsPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Weather"
        description="Forecast provider and credentials used for observing conditions and dew-risk decisions."
        collapseKey="settings.weather"
      >
        <WeatherPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Sky map & cache"
        description="Viewed-image cache, automatic imagery, and object-information sources."
        collapseKey="settings.skyMapOffline"
      >
        <OfflineMapsPanel />
        <SkymapAutofetchPanel />
        <ObjectInfoSettingsPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Appearance"
        description="Theme and text size for this Photon Commander installation."
        collapseKey="settings.appearanceAccess"
      >
        <AppearancePanel />
      </SettingsBlock>

      <SettingsBlock
        title="Remote access"
        description="Reach Photon Commander securely away from home through a private Tailscale HTTPS address."
        collapseKey="settings.remoteAccess"
      >
        <RemoteAccessPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Integrations & tools"
        description="Detect optional external components and open reviewed installation and setup guidance."
        collapseKey="settings.externalTools"
      >
        <ExternalToolsPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Debug tools"
        description="Rehearse polar alignment with an artificial sky and a simulated EQ wedge. No device commands are sent."
        collapseKey="settings.debugTools"
      >
        <PolarDebugSettingsPanel />
      </SettingsBlock>

      <SettingsBlock
        title="Export & import"
        description="Back up or restore application settings and this browser's saved interface layout."
        collapseKey="settings.exportImport"
      >
        <ExportImportPanel />
      </SettingsBlock>

      <SettingsBlock
        title="About Photon Commander"
        description="Version, build, licence, and project information."
        collapseKey="settings.about"
        defaultCollapsed={false}
      >
        <AboutPanel />
      </SettingsBlock>
    </div>
  );
}
