// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { usePolarDebugStore } from "../../store/polarDebugStore";

export function PolarDebugSettingsPanel() {
  const enabled = usePolarDebugStore((state) => state.enabled);
  const setEnabled = usePolarDebugStore((state) => state.setEnabled);
  return (
    <div>
      <label className="flex min-h-11 items-center gap-3 text-sm">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => { setEnabled(event.target.checked); }}
          className="h-5 w-5 accent-accent"
        />
        <span>Enable PA debug simulator</span>
      </label>
      <p className="text-xs text-fg-muted">Saved on this browser. Simulated wedge settings do not affect the real telescope.</p>
    </div>
  );
}
