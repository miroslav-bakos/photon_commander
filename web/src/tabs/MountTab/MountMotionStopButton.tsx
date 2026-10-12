// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useMountStopMutation } from "../../api/mount";
import { StopIcon } from "../../components/ActionIcons";
import { Button } from "../../components/Button";

export type ActiveMountMotion = "park" | "find-home" | "slew";

/** A local companion to the action that started a real mount movement. */
export function MountMotionStopButton({
  disabled = false,
  onStopped,
}: {
  disabled?: boolean;
  onStopped: () => void;
}) {
  const stop = useMountStopMutation();

  return (
    <>
      <Button
        variant="danger"
        className="!border-danger !bg-danger !text-bg hover:brightness-110"
        aria-label="Stop mount motion"
        disabled={disabled || stop.isPending}
        onClick={() => {
          stop.mutate(undefined, { onSuccess: onStopped });
        }}
      >
        <StopIcon />
        {stop.isPending ? "Stopping…" : "Stop"}
      </Button>
      {stop.isError && (
        <p role="alert" className="basis-full text-sm text-danger">
          {stop.error instanceof Error ? stop.error.message : "Stop failed."}
        </p>
      )}
    </>
  );
}
