// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { LocalizedText } from "../../i18n/jsx-runtime";
import { Camera, Layers3 } from "lucide-react";
import { NavLink } from "react-router-dom";

const modes = [
  { to: "/live", label: "Capture", icon: Camera },
  { to: "/live-stacking", label: "Live stack", icon: Layers3 },
] as const;

export function ObservingModeSwitch() {
  return (
    <nav aria-label="Observing image view" className="grid grid-cols-2 rounded-md border border-border bg-bg p-1">
      {modes.map((mode) => (
        <NavLink
          key={mode.to}
          to={mode.to}
          className={({ isActive }) => `flex min-h-10 items-center justify-center gap-2 rounded px-3 text-sm font-medium ${isActive ? "bg-accent-muted text-accent" : "text-fg-muted hover:text-fg"}`}
        >
          <mode.icon className="size-4" />
          <LocalizedText value={mode.label} />
        </NavLink>
      ))}
    </nav>
  );
}
