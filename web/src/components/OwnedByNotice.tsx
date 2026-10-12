// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Card } from "./Card";

/**
 * A placeholder for content this agent does not own (frontend-react.md:
 * "Does not own: SkyMapBackend and its adapters, FOV overlay rendering, the
 * timeline component"). Real, not a stub that pretends to be a feature: it
 * says plainly what is missing and who owns it, in both themes, at every
 * width — the same honesty invariant 7 asks of the backend.
 */
export function OwnedByNotice({ owner, feature }: { owner: string; feature: string }) {
  return (
    <Card className="text-fg-muted">
      <p className="text-sm">
        <span className="font-medium text-fg">{feature}</span> is not built yet. It belongs to{" "}
        <code className="rounded bg-surface-raised px-1.5 py-0.5 text-xs text-fg">{owner}</code>,
        not the app shell.
      </p>
    </Card>
  );
}
