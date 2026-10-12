// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { SetupOffer } from "./SetupOffer";
import { AppDiagnostics } from "./AppDiagnostics";
import { PaneLayout } from "../components/PaneLayout";
import { Navigate, Route, HashRouter, Routes } from "react-router-dom";
import { PairingGate } from "./PairingGate";
import { Providers } from "./Providers";
import { Shell } from "./Shell";
import { useThemeSync } from "../theme/useThemeSync";
import { useFontScaleSync } from "../theme/useFontScaleSync";
import { useAccentSync } from "../theme/useAccentSync";
import { useWsConnection } from "../ws/useWsConnection";
import { useQueryRecovery } from "../ws/useQueryRecovery";
import { CaptureTab } from "../tabs/Capture/CaptureTab";
import { MountTab } from "../tabs/MountTab/MountTab";
import { PersistentPlanningTab } from "./PersistentPlanningTab";
import { PolarAlignTab } from "../tabs/PolarAlignTab/PolarAlignTab";
import { SchedulingTab } from "../tabs/SchedulingTab/SchedulingTab";
import { SettingsTab } from "../tabs/SettingsTab/SettingsTab";
import { LiveStackingTab } from "../tabs/LiveStacking/LiveStackingTab";
import { StreamingTab } from "../tabs/Streaming/StreamingTab";
import { SetupTab } from "../tabs/Setup/SetupTab";
import { HelpTab } from "../tabs/Help/HelpTab";
import { ArchiveTab } from "../tabs/Archive/ArchiveTab";
import { SystemTab } from "../tabs/System/SystemTab";
import { DashboardTab } from "../tabs/Dashboard/DashboardTab";
import { CalendarTab } from "../tabs/Calendar/CalendarTab";
import { LayoutRouteProvider } from "../layout/LayoutContext";
import { ClientConfigurationSync } from "./ClientConfigurationSync";
import { StreamPictureInPicture } from "./StreamPictureInPicture";
import { RouteTiming } from "../performance/RouteTiming";
import { RouteScrollReset } from "./RouteScrollReset";

/** Everything that needs a session: the WS connection, theme sync, and the
 * four tabs. Nothing here runs before `PairingGate` has one. */
function AuthenticatedApp() {
  useWsConnection(true);
  useQueryRecovery();
  useThemeSync();
  useFontScaleSync();
  useAccentSync();

  return (
    <LayoutRouteProvider>
      <RouteTiming />
      <AppDiagnostics />
      <RouteScrollReset />
      <ClientConfigurationSync />
      <Shell>
        <SetupOffer />
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<PaneLayout secondary={<DashboardTab />} />} />
          <Route path="/planning" element={null} />
          <Route path="/scheduling" element={<SchedulingTab />} />
          <Route path="/calendar" element={<PaneLayout secondary={<CalendarTab />} />} />
          <Route path="/mount" element={<PaneLayout compactSecondary secondary={<MountTab />} />} />
          <Route path="/polar-align" element={<PolarAlignTab />} />
          <Route path="/live" element={<CaptureTab />} />
          <Route path="/live-stacking" element={<LiveStackingTab />} />
          <Route path="/streaming" element={<StreamingTab />} />
          <Route path="/archive" element={<PaneLayout secondary={<ArchiveTab />} />} />
          <Route path="/system" element={<PaneLayout secondary={<SystemTab />} />} />
          <Route path="/settings" element={<PaneLayout secondary={<SettingsTab />} />} />
          <Route path="/setup" element={<PaneLayout secondary={<SetupTab />} />} />
          <Route path="/help" element={<PaneLayout secondary={<HelpTab />} />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
        <PersistentPlanningTab />
      </Shell>
      <StreamPictureInPicture />
    </LayoutRouteProvider>
  );
}

export function App() {
  return (
    <Providers>
      {/* Hash routing: the phone reaches this over a bare LAN IP with no
          server-side route fallback to configure, and the pairing link
          (`/?t=...`) stays a plain query string on the app's one real path. */}
      <HashRouter>
        <PairingGate>
          <AuthenticatedApp />
        </PairingGate>
      </HashRouter>
    </Providers>
  );
}
