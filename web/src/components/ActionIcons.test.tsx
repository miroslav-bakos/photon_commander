// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  AddIcon,
  AutofocusIcon,
  CaptureIcon,
  CaptureSetIcon,
  ClearIcon,
  DeletePlanIcon,
  EnabledIcon,
  LoopIcon,
  NewPlanIcon,
  PlateSolveIcon,
  PolarAlignmentIcon,
  RenamePlanIcon,
  RestartStreamingIcon,
  SearchGoIcon,
  SlewHereIcon,
  SlewScopeIcon,
  StartPlanIcon,
  StartStreamingIcon,
  StartYoutubeIcon,
  StopIcon,
  StopStreamingIcon,
  StopYoutubeIcon,
  SyncHereIcon,
  SyncIcon,
  TrackingOffIcon,
  TrackingOnIcon,
} from "./ActionIcons";

const APPROVED = [
  [SlewScopeIcon, "1d"], [PlateSolveIcon, "2c"], [SyncIcon, "3b"],
  [ClearIcon, "4b"], [SearchGoIcon, "5e"], [NewPlanIcon, "6c"],
  [RenamePlanIcon, "7c"], [DeletePlanIcon, "8a"], [StartPlanIcon, "9a"],
  [AddIcon, "10b"], [TrackingOnIcon, "11d"], [TrackingOffIcon, "12d"],
  [SlewHereIcon, "13b"], [SyncHereIcon, "14e"], [PolarAlignmentIcon, "15a"],
  [CaptureIcon, "16a"], [LoopIcon, "17b"], [CaptureSetIcon, "18d"],
  [EnabledIcon, "19a"], [AutofocusIcon, "20e"], [StopIcon, "22c"],
  [StartStreamingIcon, "24c"], [StopStreamingIcon, "25a"],
  [RestartStreamingIcon, "26d"], [StartYoutubeIcon, "27b"], [StopYoutubeIcon, "28b"],
] as const;

describe("approved action icons", () => {
  it.each(APPROVED)("renders shortlist choice %s", (Icon, choice) => {
    render(<div data-testid="icon-host"><Icon /></div>);
    expect(screen.getByTestId("icon-host").querySelector(`[data-action-icon="${choice}"]`)).toBeInTheDocument();
  });
});
