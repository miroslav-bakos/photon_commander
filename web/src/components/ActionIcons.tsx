// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ComponentType, SVGProps } from "react";
import {
  CalendarPlus,
  Camera,
  Cast,
  CirclePlay,
  CirclePlus,
  CircleSlash2,
  CircleStop,
  CircleX,
  Compass,
  Focus,
  Layers3,
  Link,
  MapPin,
  Play,
  Radar,
  Repeat,
  Repeat2,
  SearchCheck,
  Sparkles,
  Telescope,
  TextCursorInput,
  ToggleRight,
  Trash2,
} from "lucide-react";

type SvgIcon = ComponentType<SVGProps<SVGSVGElement>>;

function ApprovedIcon({ glyph: Glyph, choice }: { glyph: SvgIcon; choice: string }) {
  return (
    <Glyph
      aria-hidden="true"
      className="size-5 shrink-0"
      data-action-icon={choice}
      focusable="false"
      strokeWidth={1.8}
    />
  );
}

export const SlewScopeIcon = () => <ApprovedIcon glyph={Telescope} choice="1d" />;
export const PlateSolveIcon = () => <ApprovedIcon glyph={Focus} choice="2c" />;
export const SyncIcon = () => <ApprovedIcon glyph={Repeat2} choice="3b" />;
export const ClearIcon = () => <ApprovedIcon glyph={CircleX} choice="4b" />;
export const SearchGoIcon = () => <ApprovedIcon glyph={SearchCheck} choice="5e" />;
export const NewPlanIcon = () => <ApprovedIcon glyph={CalendarPlus} choice="6c" />;
export const RenamePlanIcon = () => <ApprovedIcon glyph={TextCursorInput} choice="7c" />;
export const DeletePlanIcon = () => <ApprovedIcon glyph={Trash2} choice="8a" />;
export const StartPlanIcon = () => <ApprovedIcon glyph={Play} choice="9a" />;
export const AddIcon = () => <ApprovedIcon glyph={CirclePlus} choice="10b" />;
export const TrackingOnIcon = () => <ApprovedIcon glyph={Radar} choice="11d" />;
export const TrackingOffIcon = () => <ApprovedIcon glyph={CircleSlash2} choice="12d" />;
export const SlewHereIcon = () => <ApprovedIcon glyph={MapPin} choice="13b" />;
export const SyncHereIcon = () => <ApprovedIcon glyph={Link} choice="14e" />;
export const PolarAlignmentIcon = () => <ApprovedIcon glyph={Compass} choice="15a" />;
export const CaptureIcon = () => <ApprovedIcon glyph={Camera} choice="16a" />;
export const LoopIcon = () => <ApprovedIcon glyph={Repeat2} choice="17b" />;
export const CaptureSetIcon = () => <ApprovedIcon glyph={Layers3} choice="18d" />;
export const EnabledIcon = () => <ApprovedIcon glyph={ToggleRight} choice="19a" />;
export const AutofocusIcon = () => <ApprovedIcon glyph={Sparkles} choice="20e" />;
export const StopIcon = () => <ApprovedIcon glyph={CircleStop} choice="22c" />;
export const StartStreamingIcon = () => <ApprovedIcon glyph={Cast} choice="24c" />;
export const StopStreamingIcon = () => <ApprovedIcon glyph={CircleStop} choice="25a" />;
export const RestartStreamingIcon = () => <ApprovedIcon glyph={Repeat} choice="26d" />;
export const StartYoutubeIcon = () => <ApprovedIcon glyph={CirclePlay} choice="27b" />;
export const StopYoutubeIcon = () => <ApprovedIcon glyph={CircleStop} choice="28b" />;
