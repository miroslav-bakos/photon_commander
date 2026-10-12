// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { CircleAlert, RotateCcw } from "lucide-react";
import { Button } from "./Button";

export function ActionableError({ message, impact, action, onRetry }: { message: string; impact: string; action: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-md border border-danger bg-danger/5 p-3 text-sm">
      <div className="flex items-start gap-2"><CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" /><div><p className="font-medium text-danger">{message}</p><p className="mt-1 text-xs text-fg-muted">Impact: {impact}</p><p className="text-xs text-fg">Next: {action}</p></div></div>
      {onRetry !== undefined && <div className="mt-2"><Button onClick={onRetry}><RotateCcw className="size-4" />Retry</Button></div>}
    </div>
  );
}
