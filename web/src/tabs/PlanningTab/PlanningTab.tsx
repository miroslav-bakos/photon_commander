// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Planning tab: target -> scope profile/mount mode -> the five-shape
 * alt-az overlay (or EQ's one rectangle) -> the time scrubber -> the mosaic
 * grid. `DESIGN.md` §3.2 is the spec; this component is the wiring between
 * `ssc.astro`'s already-computed geometry (`api/astro.ts`) and the drawing
 * layer (`overlay.ts`, `SkyMap.tsx`).
 *
 * State owned here is deliberately just the *inputs* (target, mount, profile,
 * session bounds, scrub instant, mosaic spec) — every number on screen is
 * either server state (`FramingResponse`/`MosaicResponse`/`NightResponse`,
 * via TanStack Query) or derived from it, never duplicated into a second
 * `useState` (frontend-react.md).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { OwnedByNotice } from "../../components/OwnedByNotice";
import { Card } from "../../components/Card";
import { PaneLayout } from "../../components/PaneLayout";
import {
  useAstroProfilesQuery,
  useAstroBodiesQuery,
  useCometSuggestionsQuery,
  useAstroTargetQuery,
  useBestTargetsQuery,
  useFramingQuery,
  useMoonAltitudeQuery,
  useMosaicQuery,
  useNightQuery,
  resolveCometForExecution,
} from "../../api/astro";
import type { AnnualAltitudeRequest, BestTargetsRequest, FramingRequest, MoonAltitudeRequest, MosaicRequest, NightRequest } from "../../api/astro";
import type { TargetIntent } from "../../api/plans";
import { useConnectionStore } from "../../store/connectionStore";
import { SkyMap, type SkyMapTarget } from "../../skymap/SkyMap";
import { MountSlewSyncControls } from "../../skymap/MountSlewSyncControls";
import type { FovShape, SkyMapBackend, ViewState } from "../../skymap/types";
import {
  buildAltitudeLimitRingShape,
  buildFovShapes,
  buildHorizonProfileMarkerShape,
  buildMosaicOverlayShapes,
} from "../../skymap/overlay";
import { fromDateLocalValue, fromDatetimeLocalValue, toDateLocalValue, toDatetimeLocalValue } from "./dateLocal";
import { FramingStatsPanel } from "./FramingStatsPanel";
import { useNow } from "../MountTab/useNow";
import { observingDate } from "../../widgets/weather/nightConditions";
import { siteNightDate } from "./siteNightWindow";
import { AutoNightPanel } from "./AutoNightPanel";
import { objectTypesForCategories } from "./targetCategories";
import { BestTargetsPanel } from "./BestTargetsPanel";
import { MosaicPanel } from "./MosaicPanel";
import { NightAltitudeChart } from "./NightAltitudeChart";
import { NightSummaryPanel } from "./NightSummaryPanel";
import { usePlanningUiStore } from "./planningUiStore";
import { profileMountFov } from "./profileMountFov";
import { selectPlanningProfile } from "./profileSelection";
import { SendToSchedulingButton } from "./SendToSchedulingButton";
import { SessionControls } from "./SessionControls";
import { SessionStartField } from "./SessionStartField";
import { TimeScrubber } from "./TimeScrubber";
import { TargetPicker } from "./TargetPicker";
import { targetFromCoordinates } from "./types";
import { ReferenceImageFraming } from "./ReferenceImageFraming";
import { nightWindowForAnchor, nightWindowForInstant, resolveEventStart } from "./twilight";
import { VisibilityPanel } from "./VisibilityPanel";

const DEFAULT_MAX_PA_STEP_DEG = 0.5;
const DEFAULT_BOUNDARY_SAMPLES = 360;
/** Fallback only, for the moment between resolving a target and the framing
 * response arriving. Once geometry is known the view is sized from it — see
 * `fitFovDeg`. */
const DEFAULT_GOTO_FOV_DEG = 6;

/** How much sky to show around the widest thing being drawn. Framing the frame
 * exactly would put its edges on the viewport border, where they read as the
 * edge of the map rather than the edge of the sensor. */
const FIT_MARGIN = 1.5;

/** Bounds outside which a frame is not usefully visible in the current view:
 * larger than the viewport, or a speck in the middle of it. Between them the
 * view is left alone, so switching lens visibly changes the size of the frame
 * instead of the map silently rescaling to match and looking like nothing
 * happened but the zoom. */
const REFIT_TOO_BIG = 1.1;
const REFIT_TOO_SMALL = 0.12;

function annualAltitudeAnchor(twilightStart: Date): Date {
  const localMidnight = fromDateLocalValue(toDateLocalValue(twilightStart));
  if (localMidnight === null) return twilightStart;
  localMidnight.setHours(12, 0, 0, 0);
  return localMidnight;
}

/**
 * A view wide enough to contain everything the overlay draws.
 *
 * The previous fixed 3 deg was narrower than the S30 Pro's own 3.99 deg tele
 * frame, so the rectangle's own edges fell outside the viewport and the
 * feature rendered correctly but invisibly. In alt-az the swept envelope is
 * wider still — a 38.6 deg rotation sweeps the frame diagonal — so the fit has
 * to follow the diagonal, not the width.
 */
function fitFovDeg(widthDeg: number, heightDeg: number, rotates: boolean): number {
  const diagonal = Math.hypot(widthDeg, heightDeg);
  // A rotating frame sweeps a disc of its own diagonal; a fixed one only needs
  // to contain the rectangle itself.
  return (rotates ? diagonal : Math.max(widthDeg, heightDeg)) * FIT_MARGIN;
}

function targetCoordinateKey(raH: number, decDeg: number): string {
  return `${String(raH)}/${String(decDeg)}`;
}

export function PlanningTab({ active = true }: { active?: boolean }) {
  // Keep a stable map instant until a new observing-site day requires
  // refreshing a past night. Deliberate edits must not creep forward on render.
  const [openedAt, setOpenedAt] = useState(() => new Date());
  const lastActiveDayRef = useRef<string | null>(null);
  const location = useConnectionStore((s) => s.snapshot?.location ?? null);
  const activeHardwareProfileId = useConnectionStore(
    (s) => s.snapshot?.settings?.hardware_profile_id ?? null,
  );
  const nowMs = useNow(30_000);
  const activeSiteDay = observingDate(new Date(nowMs), location?.timezone ?? "UTC");
  const profilesQuery = useAstroProfilesQuery();
  const profiles = useMemo(() => profilesQuery.data?.profiles ?? [], [profilesQuery.data]);

  // Session setup lives in `planningUiStore`, not `useState`: `<Routes>`
  // unmounts this tab on navigation, and a `useState` would lose the
  // resolved target and the rest of the session setup every time
  // (planningUiStore.ts's own doc comment).
  const target = usePlanningUiStore((s) => s.target);
  const setTarget = usePlanningUiStore((s) => s.setTarget);
  const mount = usePlanningUiStore((s) => s.mount);
  const setMount = usePlanningUiStore((s) => s.setMount);
  const chosenProfileId = usePlanningUiStore((s) => s.chosenProfileId);
  const setChosenProfileId = usePlanningUiStore((s) => s.setChosenProfileId);
  const setChosenOpticId = usePlanningUiStore((s) => s.setChosenOpticId);
  const chosenOpticId = usePlanningUiStore((s) => s.chosenOpticId);
  const startMode = usePlanningUiStore((s) => s.startMode);
  const setStartMode = usePlanningUiStore((s) => s.setStartMode);
  const startAbsolute = usePlanningUiStore((s) => s.startAbsolute);
  const setStartAbsolute = usePlanningUiStore((s) => s.setStartAbsolute);
  const startAnchorDate = usePlanningUiStore((s) => s.startAnchorDate);
  const setStartAnchorDate = usePlanningUiStore((s) => s.setStartAnchorDate);
  const startEventName = usePlanningUiStore((s) => s.startEventName);
  const setStartEventName = usePlanningUiStore((s) => s.setStartEventName);
  const startEventOffsetMin = usePlanningUiStore((s) => s.startEventOffsetMin);
  const setStartEventOffsetMin = usePlanningUiStore((s) => s.setStartEventOffsetMin);
  const sessionLengthHours = usePlanningUiStore((s) => s.sessionLengthHours);
  const setSessionLengthHours = usePlanningUiStore((s) => s.setSessionLengthHours);
  const instantSeconds = usePlanningUiStore((s) => s.instantSeconds);
  const setInstantSeconds = usePlanningUiStore((s) => s.setInstantSeconds);
  const resetObservationTime = usePlanningUiStore((s) => s.resetObservationTime);
  const mosaicSpec = usePlanningUiStore((s) => s.mosaicSpec);
  const setMosaicSpec = usePlanningUiStore((s) => s.setMosaicSpec);
  const altitudeLimitDeg = usePlanningUiStore((s) => s.altitudeLimitDeg);
  const bestTargetCategories = usePlanningUiStore((s) => s.bestTargetCategories);
  const centeringToleranceText = usePlanningUiStore((s) => s.centeringToleranceText);
  const setCenteringToleranceText = usePlanningUiStore((s) => s.setCenteringToleranceText);
  const handleTargetChange = useCallback((nextTarget: typeof target) => {
    if (nextTarget === null) {
      // Clearing ends the current framing decision. Preserve the user's
      // overlap preference for the next mosaic, but make the next resolved
      // object start as one ordinary frame instead of inheriting an old grid.
      setMosaicSpec({ raNum: 1, decNum: 1, overlapPct: mosaicSpec.overlapPct });
    }
    setTarget(nextTarget);
  }, [mosaicSpec.overlapPct, setMosaicSpec, setTarget]);

  useEffect(() => {
    if (!active) return;
    const now = new Date();
    const timeZone = location?.timezone ?? "UTC";
    const today = observingDate(now, timeZone);
    const previousDay = lastActiveDayRef.current;
    lastActiveDayRef.current = today;
    const state = usePlanningUiStore.getState();
    const selectedDay = state.startMode === "event"
      ? toDateLocalValue(state.startAnchorDate)
      : observingDate(state.startAbsolute, timeZone);
    // Retaining the map must not retain a past default night indefinitely.
    // Same-day edits and future choices survive navigation unchanged.
    if (previousDay !== null && (previousDay === today || selectedDay >= today)) return;
    resetObservationTime(now);
    const anchor = fromDateLocalValue(today);
    if (anchor !== null) {
      anchor.setHours(12, 0, 0, 0);
      setStartAnchorDate(anchor);
    }
    setOpenedAt(now);
  }, [active, activeSiteDay, location?.timezone, resetObservationTime, setStartAnchorDate]);
  const observationTimeReset = startAbsolute.getTime() === openedAt.getTime();

  const selectedProfile = useMemo(
    () => selectPlanningProfile(profiles, chosenProfileId, activeHardwareProfileId),
    [profiles, chosenProfileId, activeHardwareProfileId],
  );
  const profileId = selectedProfile?.id ?? null;
  const opticId = useMemo(() => {
    if (selectedProfile === null) return null;
    if (selectedProfile.optics.some((o) => o.id === chosenOpticId)) return chosenOpticId;
    return selectedProfile.optics[0]?.id ?? null;
  }, [selectedProfile, chosenOpticId]);
  const configuredMountFov = useMemo(
    () => profileMountFov(selectedProfile, opticId),
    [selectedProfile, opticId],
  );

  // Dragging "session length" shorter than the current scrub instant should
  // not desync `instantSeconds` from the slider it drives — clamped here,
  // at every read site, rather than written back with a setState-in-effect.


  // The night whose dusk/dawn boundaries the event-relative start picker and
  // the night summary offer — independent of `target` (DESIGN.md §4.1's
  // event picker and night summary are useful before a target is even
  // chosen) and of `sessionLengthHours` (a session shorter than "dusk to
  // dawn" must not hide the boundaries outside it). Local noon-to-noon so
  // the window always contains a whole evening's dusk and the following
  // morning's dawn regardless of DST (`twilight.ts`'s own doc comment).
  const twilightWindow = startMode === "absolute"
    ? nightWindowForInstant(startAbsolute)
    : nightWindowForAnchor(startAnchorDate);
  // Solar events are deliberately independent of the selected target. They
  // resolve event-relative session start, so putting target coordinates in
  // this query key made every FOV drop temporarily erase `startTime` while a
  // replacement request loaded. SkyMap then received `instant=null`, fell
  // from horizontal into equatorial projection, and rotated back a moment
  // later, causing an abrupt projection change.
  const twilightNightRequest: NightRequest | null =
    location === null
      ? null
      : {
          start_time: twilightWindow.start.toISOString(),
          end_time: twilightWindow.end.toISOString(),
          min_altitude_deg: 0,
          altitude_samples: 2,
        };
  const twilightNight = useNightQuery(twilightNightRequest);

  // The whole-night target curve is a separate query because it legitimately
  // changes when the frame is dragged. Its pending state may briefly remove
  // only the curve; it can never invalidate the solar events or map instant.
  const twilightTargetRequest: NightRequest | null =
    target === null || location === null
      ? null
      : {
          start_time: twilightWindow.start.toISOString(),
          end_time: twilightWindow.end.toISOString(),
          min_altitude_deg: altitudeLimitDeg,
          altitude_samples: 145,
          ra_j2000_h: target.raH,
          dec_j2000_deg: target.decDeg,
        };
  const twilightTargetNight = useNightQuery(twilightTargetRequest);

  const startResolution =
    startMode === "absolute"
      ? ({ status: "resolved", eventWhen: startAbsolute, when: startAbsolute } as const)
      : resolveEventStart(twilightNight.data?.solar_events, startEventName, startEventOffsetMin);
  const startTime: Date | null = startResolution.status === "resolved" ? startResolution.when : null;
  const bodyPositions = useAstroBodiesQuery(startTime?.toISOString() ?? null);
  const cometPositions = useCometSuggestionsQuery(
    target?.comet?.designation ?? "",
    startTime?.toISOString() ?? null,
  );
  useEffect(() => {
    if (target?.bodyId === undefined || bodyPositions.data === undefined) return;
    const position = bodyPositions.data.bodies.find((body) => body.id === target.bodyId);
    if (position === undefined) return;
    if (
      target.raH === position.ra_j2000_h &&
      target.decDeg === position.dec_j2000_deg &&
      target.sizeMajorArcmin === position.apparent_diameter_arcmin
    ) return;
    // The stored identity stays unchanged; only the Planning preview follows
    // a changed session start. The scheduled item persists `bodyId` and the
    // executor resolves it again at the actual execution instant.
    setTarget({
      ...target,
      raH: position.ra_j2000_h,
      decDeg: position.dec_j2000_deg,
      sizeMajorArcmin: position.apparent_diameter_arcmin,
    });
    // Fetched ephemeris data is the trigger. `target` is deliberately read as
    // the current store snapshot without turning this into an update loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bodyPositions.data, bodyPositions.dataUpdatedAt, target?.bodyId]);

  useEffect(() => {
    if (target?.comet === undefined || cometPositions.data === undefined) return;
    const position = cometPositions.data.comets.find((comet) => comet.spk_id === target.comet?.spkId);
    if (position === undefined) return;
    if (
      target.raH === position.ra_j2000_h &&
      target.decDeg === position.dec_j2000_deg &&
      target.comet.resolvedAt === position.resolved_at
    ) return;
    setTarget({
      ...target,
      raH: position.ra_j2000_h,
      decDeg: position.dec_j2000_deg,
      comet: {
        ...target.comet,
        resolvedAt: position.resolved_at,
        elementEpochJdTdb: position.element_epoch_jd_tdb,
        elementAgeDays: position.element_age_days,
        solutionDate: position.solution_date,
        source: cometPositions.data.source,
        sourceUrl: cometPositions.data.source_url,
        snapshotFetchedAt: cometPositions.data.snapshot_fetched_at,
        snapshotAgeDays: cometPositions.data.snapshot_age_days,
        stale: cometPositions.data.stale,
        accuracyNote: cometPositions.data.accuracy_note,
      },
    });
    // Same server-data trigger and store-snapshot reasoning as the body effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cometPositions.data, cometPositions.dataUpdatedAt, target?.comet?.spkId]);

  // The scrubbed instant used to be clamped to the session (start + length),
  // which is what the TimeScrubber slider spans. The night timeline below the
  // map scrubs the *whole night* (owner: "whole night timeline with dragable
  // current time line"), so the instant has to be free to sit outside the
  // session — before it, or after it — and is clamped to the night window
  // instead. It can therefore be negative, meaning "earlier than the session
  // start", which every consumer already handles: they add it to `startTime`.
  const nightBoundsSeconds = useMemo(() => {
    if (startTime === null) return null;
    return {
      min: (twilightWindow.start.getTime() - startTime.getTime()) / 1000,
      max: (twilightWindow.end.getTime() - startTime.getTime()) / 1000,
    };
  }, [startTime, twilightWindow.start, twilightWindow.end]);
  const clampedInstantSeconds =
    nightBoundsSeconds === null
      ? Math.min(instantSeconds, sessionLengthHours * 3600)
      : Math.min(Math.max(instantSeconds, nightBoundsSeconds.min), nightBoundsSeconds.max);

  // Only while nothing is resolved: this is the "what should I point at
  // tonight?" answer, and once the user has picked something the detail
  // column has far more specific things to say about it. Keyed on the night
  // window and the optics, never on the scrub instant — it is a
  // whole-catalogue solve, not a per-frame one.
  const bestTargetsRequest: BestTargetsRequest | null =
    target !== null || location === null || profileId === null || opticId === null
      ? null
      : {
          start_time: twilightWindow.start.toISOString(),
          end_time: twilightWindow.end.toISOString(),
          min_altitude_deg: altitudeLimitDeg,
          top_n: 10,
          profile_id: profileId,
          optic_id: opticId,
          max_candidate_magnitude: 13,
          allowed_object_types: objectTypesForCategories(bestTargetCategories),
        };
  const bestTargets = useBestTargetsQuery(bestTargetsRequest);

  const framingRequest: FramingRequest | null =
    target === null || location === null || profileId === null || opticId === null || startTime === null
      ? null
      : {
          ra_j2000_h: target.raH,
          dec_j2000_deg: target.decDeg,
          profile_id: profileId,
          optic_id: opticId,
          mount,
          pa_offset_deg: 0,
          start_time: startTime.toISOString(),
          session_length_hours: sessionLengthHours,
          max_pa_step_deg: DEFAULT_MAX_PA_STEP_DEG,
          boundary_samples: DEFAULT_BOUNDARY_SAMPLES,
        };
  const framing = useFramingQuery(framingRequest);

  const isMosaic = mosaicSpec.raNum > 1 || mosaicSpec.decNum > 1;
  const mosaicRequest: MosaicRequest | null =
    !isMosaic || framingRequest === null
      ? null
      : {
          ...framingRequest,
          mosaic: { ra_num: mosaicSpec.raNum, dec_num: mosaicSpec.decNum, overlap_pct: mosaicSpec.overlapPct },
        };
  const mosaic = useMosaicQuery(mosaicRequest);

  const nightRequest: NightRequest | null =
    target === null || location === null || startTime === null
      ? null
      : {
          start_time: startTime.toISOString(),
          end_time: new Date(startTime.getTime() + sessionLengthHours * 3600_000).toISOString(),
          ra_j2000_h: target.raH,
          dec_j2000_deg: target.decDeg,
          min_altitude_deg: altitudeLimitDeg,
          // `moon`/`horizon_profile_active`/`visibility_intervals` feed
          // `VisibilityPanel`; `altitude_curve` feeds `NightAltitudeChart`'s
          // target curve — 145 matches the density Scheduling's own
          // per-item altitude curve requests use (`TimelinePane.tsx`) for a
          // curve that actually looks smooth, not the flat 2-point line a
          // minimal request would give.
          altitude_samples: 145,
        };
  const night = useNightQuery(nightRequest);

  // The Moon's own altitude across the same session window, for
  // `NightAltitudeChart`'s grey moon curve — the same query and the same
  // `{ when: s.at, altitude_deg: s.alt_deg }` adaptation the Scheduling
  // tab's strip already uses (`TimelinePane.tsx`).
  const moonAltitudeRequest: MoonAltitudeRequest | null =
    nightRequest === null ? null : { start: nightRequest.start_time, end: nightRequest.end_time };
  const moonAltitudeQuery = useMoonAltitudeQuery(moonAltitudeRequest);
  const moonAltitude = useMemo(
    () => moonAltitudeQuery.data?.samples.map((s) => ({ when: s.at, altitude_deg: s.alt_deg })),
    [moonAltitudeQuery.data],
  );

  const annualAltitudeRequest: AnnualAltitudeRequest | null =
    target === null || location === null
      ? null
      : {
          ra_j2000_h: target.raH,
          dec_j2000_deg: target.decDeg,
          start_time: annualAltitudeAnchor(twilightWindow.start).toISOString(),
          nights: 365,
          min_altitude_deg: altitudeLimitDeg,
          step_minutes: 15,
        };
  // DESIGN.md §3.4's "Send to Scheduling" payload, straight from the inputs
  // above — the same target, mount, lens, grid and altitude floor the
  // framing/mosaic/visibility queries were computed from, so what gets
  // scheduled is what was planned. `pa_offset_deg` is the 0 `framingRequest`
  // sends; the moon-separation floor is not a Planning input, so it stays
  // unset rather than guessed.
  const intent: TargetIntent | null =
    target === null || opticId === null || (
      target.comet !== undefined && (mosaicSpec.raNum > 1 || mosaicSpec.decNum > 1)
    )
      ? null
      : {
          ...(target.comet !== undefined
            ? {
                name: target.name,
                comet: {
                  kind: "comet" as const,
                  spk_id: target.comet.spkId,
                  designation: target.comet.designation,
                  full_name: target.name,
                  source_url: target.comet.sourceUrl,
                  snapshot_fetched_at: target.comet.snapshotFetchedAt,
                  solution_date: target.comet.solutionDate,
                  element_epoch_jd_tdb: target.comet.elementEpochJdTdb,
                },
              }
            : target.bodyId === undefined
            ? {
                name: target.name,
                object_type: target.objectType as TargetIntent["object_type"],
                ra_j2000_h: target.raH,
                dec_j2000_deg: target.decDeg,
              }
            : { body: target.bodyId }),
          framing: { mode: mount, pa_offset_deg: 0, lens: opticId },
          mosaic: { ra_num: mosaicSpec.raNum, dec_num: mosaicSpec.decNum, overlap_pct: mosaicSpec.overlapPct },
          constraints: { min_alt_deg: altitudeLimitDeg, min_moon_sep_deg: null },
          panels: [],
        };

  // Resolving a designation picked from the sky map's object-info popup.
  // Goes through the same catalogue lookup `TargetPicker` uses rather than
  // a second path: the popup's own endpoint carries an article and an image
  // but no coordinates, so there is nothing to shortcut.
  const [pickedDesignation, setPickedDesignation] = useState<string | null>(null);
  // Which resolution has already been applied. A ref rather than clearing
  // `pickedDesignation`, because clearing it here would be a `setState`
  // inside the effect that reacts to the query — cascading renders, and the
  // lint rule that says so is right. Reset on each pick so choosing the same
  // object twice (after clearing the target, say) applies again.
  const pickedAppliedAtRef = useRef<number | null>(null);
  const pickedResolution = useAstroTargetQuery(pickedDesignation ?? "");
  const pickDesignation = useCallback((designation: string) => {
    pickedAppliedAtRef.current = null;
    setPickedDesignation(designation);
  }, []);
  useEffect(() => {
    if (pickedDesignation === null || pickedResolution.data === undefined) return;
    if (pickedAppliedAtRef.current === pickedResolution.dataUpdatedAt) return;
    pickedAppliedAtRef.current = pickedResolution.dataUpdatedAt;
    const r = pickedResolution.data;
    setTarget({
      name: r.name,
      raH: r.ra_j2000_h,
      decDeg: r.dec_j2000_deg,
      magnitude: r.magnitude,
      sizeMajorArcmin: r.size_major_arcmin,
      positionAngleDeg: r.position_angle_deg,
      objectType: r.object_type,
    });
    // `dataUpdatedAt` for the same reason `TargetPicker` needs it: TanStack
    // Query's structural sharing hands back the identical object reference
    // when a refetch is deeply equal, so picking the same object twice would
    // otherwise never re-fire this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pickedDesignation, pickedResolution.data, pickedResolution.dataUpdatedAt]);

  const backendRef = useRef<SkyMapBackend | null>(null);
  const skipGotoForDraggedTargetRef = useRef<string | null>(null);

  // The scrubbed moment as a Date, for the map's meridian and alt-az grid.
  // Memoised on the two numbers so the map's `setDisplay` effect runs once
  // per scrub step, not once per render.
  const startTimeMs = startTime === null ? null : startTime.getTime();
  const initialCurrentInstantAppliedRef = useRef<number | null>(null);
  useEffect(() => {
    if (!observationTimeReset || startTimeMs === null || initialCurrentInstantAppliedRef.current === openedAt.getTime()) return;
    initialCurrentInstantAppliedRef.current = openedAt.getTime();
    setInstantSeconds((openedAt.getTime() - startTimeMs) / 1000);
  }, [observationTimeReset, openedAt, setInstantSeconds, startTimeMs]);
  const scrubbedInstant = useMemo(
    () => (startTimeMs === null ? null : new Date(startTimeMs + clampedInstantSeconds * 1000)),
    [startTimeMs, clampedInstantSeconds],
  );

  // goto() only on an actual target change, not on every scrub/session-bound
  // edit — otherwise dragging the instant slider would also re-centre the
  // map on every frame.
  useEffect(() => {
    if (target === null) return;
    const targetKey = targetCoordinateKey(target.raH, target.decDeg);
    if (skipGotoForDraggedTargetRef.current === targetKey) {
      skipGotoForDraggedTargetRef.current = null;
      return;
    }
    backendRef.current?.goto(target.raH * 15, target.decDeg, DEFAULT_GOTO_FOV_DEG);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.raH, target?.decDeg]);

  // The live field of view, so a re-fit can be conditional rather than
  // unconditional. Kept in a ref: it changes on every pan and zoom and must
  // not re-render the tab or restart the effect below.
  const viewFovRef = useRef<number | null>(null);
  const fittedForTargetRef = useRef<string | null>(null);
  const skipNextFitForWarmMapRef = useRef<string | null>(null);

  // Fit the view to the geometry, but only when it actually needs fitting.
  //
  // A new target is always framed. A lens or mount-mode change on the SAME
  // target is not: re-fitting there scales the viewport in step with the
  // frame, so the rectangle keeps the same size on screen and switching lens
  // looks like it only zoomed the map. The frame is left to change size
  // against a stable view, which is the point of choosing a lens — unless the
  // new frame is bigger than the viewport or a speck inside it, where leaving
  // the view alone would show nothing useful.
  const fitGeometry = framing.data?.geometry;
  useEffect(() => {
    if (target === null || fitGeometry === undefined) return;
    // A mosaic is wider than one frame: n panels at (1 - overlap) spacing plus
    // one frame's worth of edge. Fitting the single-frame geometry left the
    // outer panels off-screen, which looks identical to them not being drawn.
    const spanFactor = (count: number) => 1 + (count - 1) * (1 - mosaicSpec.overlapPct / 100);
    const wanted = fitFovDeg(
      fitGeometry.width_deg * spanFactor(mosaicSpec.raNum),
      fitGeometry.height_deg * spanFactor(mosaicSpec.decNum),
      mount === "altaz",
    );
    const targetKey = targetCoordinateKey(target.raH, target.decDeg);
    if (skipNextFitForWarmMapRef.current === targetKey) {
      skipNextFitForWarmMapRef.current = null;
      fittedForTargetRef.current = targetKey;
      return;
    }
    const isNewTarget = fittedForTargetRef.current !== targetKey;
    const current = viewFovRef.current;
    const unusable =
      current === null || wanted > current * REFIT_TOO_BIG || wanted < current * REFIT_TOO_SMALL;

    if (isNewTarget || unusable) {
      backendRef.current?.goto(target.raH * 15, target.decDeg, wanted);
      fittedForTargetRef.current = targetKey;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    target?.raH,
    target?.decDeg,
    fitGeometry?.width_deg,
    fitGeometry?.height_deg,
    mount,
    mosaicSpec.raNum,
    mosaicSpec.decNum,
    mosaicSpec.overlapPct,
  ]);

  const response = framing.data;
  // A coordinate-mode change invalidates every existing framing shape. The
  // replacement query may take a moment, but retaining EQ's fixed polygon
  // during that wait makes it look like Alt/Az contains both indicators.
  // Clear immediately; the response effect below repopulates the overlay
  // once geometry for the selected mode is available. Dragging still keeps
  // its local preview because a drag does not change `mount`.
  useEffect(() => {
    backendRef.current?.setOverlay([]);
  }, [mount]);

  useEffect(() => {
    const backend = backendRef.current;
    if (backend === null) return;
    if (target === null) {
      backend.setOverlay([]);
      return;
    }
    // A drop changes the query keys immediately, while the frame has already
    // drawn the exact dragged preview locally. Keep that preview until the
    // replacement geometry is complete; clearing here made both a single
    // frame and a mosaic blink away before snapping back. A mosaic also waits
    // for its panel query so it cannot briefly degrade to a lone primary
    // rectangle between the two responses.
    if (response === undefined || (isMosaic && mosaic.data === undefined)) return;
    // A genuine multi-panel mosaic (more than one actual panel — a 1x1
    // "mosaic" is just a single frame under a different name) replaces the
    // single "primary" frame with its own panel grid below; drawing both
    // is not "more information", it is a stray rectangle at the target
    // centre that answers a question ("where does one frame land?") the
    // mosaic already made moot (reported live). Every other shape here
    // (keep/union/stack/ghost) still describes real per-session geometry
    // and stays.
    const isMultiPanelMosaic = mosaic.data !== undefined && mosaic.data.panels.length > 1;
    const shapes = buildFovShapes({
      mount: response.mount,
      geometry: response.geometry,
      envelope: response.envelope,
      targetRaDeg: target.raH * 15,
      targetDecDeg: target.decDeg,
      instantSeconds: clampedInstantSeconds,
    }).filter((shape) => !isMultiPanelMosaic || shape.style.role !== "primary");
    const mosaicShapes = mosaic.data !== undefined
      ? buildMosaicOverlayShapes(mosaic.data.panels, mosaic.data.mount, clampedInstantSeconds)
      : [];
    // Horizon markers (skymap-viz.md items 1/2): alt-az only —
    // `HorizonMarkerInputs`'s own doc comment on why EQ's samples are not
    // real ephemeris to interpolate.
    const horizonShapes: FovShape[] = [];
    if (response.mount === "altaz") {
      const markerInputs = {
        envelope: response.envelope,
        geometry: response.geometry,
        targetRaDeg: target.raH * 15,
        targetDecDeg: target.decDeg,
        instantSeconds: clampedInstantSeconds,
      };
      horizonShapes.push(buildAltitudeLimitRingShape(markerInputs, altitudeLimitDeg));
      if (response.visibility.horizon_profile_active) {
        horizonShapes.push(
          buildHorizonProfileMarkerShape(markerInputs, response.visibility.start_horizon_altitude_deg),
        );
      }
    }
    backend.setOverlay([...shapes, ...mosaicShapes, ...horizonShapes]);
  }, [response, target, clampedInstantSeconds, isMosaic, mosaic.data, altitudeLimitDeg]);

  // What the map's "Target field" cutout group fetches imagery for — the
  // resolved target, plus the lens-fitted field size once framing geometry
  // is known (`response.geometry.diagonal_deg`; `SkyMap`'s own default
  // applies while it is not).
  const skyMapTarget: SkyMapTarget | null = useMemo(
    () =>
      target === null
        ? null
        : {
            label: target.name,
            raDeg: target.raH * 15,
            decDeg: target.decDeg,
            fovFittedSizeDeg: response?.geometry.diagonal_deg ?? null,
          },
    [target, response],
  );

  const handleWarmSkyMapViewRestore = useCallback((restoredView: ViewState) => {
    viewFovRef.current = restoredView.fovDeg;
    const currentTarget = usePlanningUiStore.getState().target;
    if (currentTarget === null) return;
    const targetKey = targetCoordinateKey(currentTarget.raH, currentTarget.decDeg);
    skipGotoForDraggedTargetRef.current = targetKey;
    skipNextFitForWarmMapRef.current = targetKey;
    fittedForTargetRef.current = targetKey;
  }, []);

  if (location === null) {
    return (
      <Card className="text-sm text-fg-muted">
        No observing location is set. Add one in Settings → Location before planning a session — a planning
        answer computed against the wrong location is the worst failure mode this app has.
      </Card>
    );
  }

  return (
    <PaneLayout
      fill
      primary={
        <div className="contents">
          <TargetPicker
            target={target}
            onTargetChange={handleTargetChange}
            instant={startTime}
          />
          <ReferenceImageFraming
            framingKey={target === null ? "none" : targetCoordinateKey(target.raH, target.decDeg)}
            onSolved={(raH, decDeg) => { handleTargetChange(targetFromCoordinates(raH, decDeg)); }}
          />
          <NightSummaryPanel
            night={twilightNight.data}
            isPending={twilightNight.isPending}
            isError={twilightNight.isError}
          />
        </div>
      }
      secondary={
        <>
          {twilightNight.data !== undefined && (
            // Keep the night-wide scrubber above the map at the full width
            // of the center pane, rather than squeezing it into the rail.
            <NightAltitudeChart
              night={twilightTargetNight.data ?? twilightNight.data}
              moonAltitude={moonAltitude}
              annualAltitudeRequest={annualAltitudeRequest}
              instantMs={scrubbedInstant === null ? null : scrubbedInstant.getTime()}
              onInstantChange={(atMs) => {
                if (startTime !== null) setInstantSeconds((atMs - startTime.getTime()) / 1000);
              }}
            />
          )}
          <div className="relative order-last min-h-0 flex-1" data-testid="planning-skymap">
            <SkyMap
              instant={scrubbedInstant}
              target={skyMapTarget}
              altitudeLimitDeg={altitudeLimitDeg}
              mountFovDeg={configuredMountFov}
              onResolveDesignation={pickDesignation}
              onWarmViewRestore={handleWarmSkyMapViewRestore}
              onBackendReady={(backend) => {
                backendRef.current = backend;
                // Track the live field of view so the fit effect can tell
                // whether the frame is still usefully visible.
                backend?.onViewChange((v) => {
                  viewFovRef.current = v.fovDeg;
                });
                // The user dragged the FOV rectangle to a new position and
                // let go (skymap-viz.md): one message, already debounced to
                // the drag's end by the frame itself (never per pointer-move
                // frame — the frame previews the drag locally and only
                // reports the final centre), so this is exactly one
                // `setTarget` per drag, not a redraw-on-every-frame re-query.
                // Dropping catalogue identity here, not keeping it stale
                // next to coordinates that are no longer that object's own
                // position, matches what `TargetPicker`'s manual RA/Dec entry
                // already does today.
                backend?.onFovMoved((ra, dec) => {
                  const currentTarget = usePlanningUiStore.getState().target;
                  if (currentTarget === null) return;
                  // This is a framing adjustment, not a new object
                  // resolution. Preserve the selected object's identity and
                  // descriptive data so the surrounding cards do not tear
                  // down and rebuild as a bare-coordinate target on drop.
                  const movedTarget = { ...currentTarget, raH: ra / 15, decDeg: dec };
                  const movedTargetKey = targetCoordinateKey(movedTarget.raH, movedTarget.decDeg);
                  // The map already recentered this position through its
                  // continuous pan camera before reporting the exact dropped
                  // centre. Mark the ordinary target-change and geometry-fit
                  // paths satisfied so neither sends a discontinuous `goto`
                  // that re-seeds ALT/AZ or replaces the deliberate drop with
                  // a fresh six-degree view.
                  skipGotoForDraggedTargetRef.current = movedTargetKey;
                  fittedForTargetRef.current = movedTargetKey;
                  setTarget(movedTarget);
                });
              }}
            />
            <div className="pointer-events-none absolute bottom-10 inset-x-2 z-20 flex flex-wrap items-end justify-end gap-2">
              {target !== null && target.bodyId === undefined && (
                <div className="pointer-events-auto">
                  <MountSlewSyncControls
                    target={{ label: target.name, raHoursJ2000: target.raH, decDegJ2000: target.decDeg }}
                    allowSync={target.comet === undefined}
                    resolveTarget={target.comet === undefined ? undefined : async () => {
                      const comet = target.comet;
                      if (comet === undefined) throw new Error("The selected comet identity is unavailable.");
                      const resolved = await resolveCometForExecution({
                        spkId: comet.spkId,
                        designation: comet.designation,
                      });
                      return {
                        label: resolved.full_name,
                        raHoursJ2000: resolved.ra_j2000_h,
                        decDegJ2000: resolved.dec_j2000_deg,
                      };
                    }}
                    toleranceText={centeringToleranceText}
                    onToleranceTextChange={setCenteringToleranceText}
                    showToleranceControl={false}
                    toolbar
                  />
                </div>
              )}
              <div className="pointer-events-auto min-w-0 max-w-full">
                <SessionControls
                  profiles={profiles}
                  profileId={profileId}
                  opticId={opticId}
                  onOpticChange={setChosenOpticId}
                  mount={mount}
                  mosaicPanel={(
                    <MosaicPanel
                      raNum={mosaicSpec.raNum}
                      decNum={mosaicSpec.decNum}
                      overlapPct={mosaicSpec.overlapPct}
                      mount={mount}
                      response={mosaic.data}
                      isFetching={mosaic.isFetching}
                      onChange={setMosaicSpec}
                      embedded
                    />
                  )}
                  onProfileChange={setChosenProfileId}
                  onMountChange={setMount}
                />
              </div>
            </div>
          </div>
        </>
      }
      detail={
        <>
          <Card title="Time" collapseKey="planning.time" className="space-y-5">
            <SessionStartField
              mode={startMode}
              onModeChange={(mode) => {
                if (mode === "event") setStartAnchorDate(startAbsolute);
                setStartMode(mode);
              }}
              absoluteLocalValue={toDatetimeLocalValue(startAbsolute)}
              onAbsoluteLocalChange={(value) => {
                const parsed = fromDatetimeLocalValue(value);
                if (parsed !== null) setStartAbsolute(parsed);
              }}
              anchorDateLocalValue={toDateLocalValue(startAnchorDate)}
              onAnchorDateLocalChange={(value) => {
                const parsed = fromDateLocalValue(value);
                if (parsed !== null) setStartAnchorDate(parsed);
              }}
              eventName={startEventName}
              onEventChange={setStartEventName}
              offsetMin={startEventOffsetMin}
              onOffsetChange={setStartEventOffsetMin}
              solarEvents={twilightNight.data?.solar_events}
              isError={twilightNight.isError}
              resolution={startResolution}
            />
            <div className="border-t border-border pt-4">
              {startTime !== null ? (
                <TimeScrubber
                  embedded
                  mount={mount}
                  startTime={startTime}
                  sessionLengthHours={sessionLengthHours}
                  instantSeconds={clampedInstantSeconds}
                  onSessionLengthChange={setSessionLengthHours}
                  onInstantChange={setInstantSeconds}
                />
              ) : (
                <p className="text-sm text-fg-muted">
                  {startResolution.status === "pending"
                    ? "Working out tonight's twilight times…"
                    : "Session start does not resolve to a time."}
                </p>
              )}
            </div>
          </Card>
          <Card title="Scheduling" data-testid="planning-scheduling" className="space-y-3">
            <SendToSchedulingButton
              intent={intent}
              mosaicPanels={mosaic.data?.panels}
              solarEvents={twilightNight.data?.solar_events}
              nightLabel={toDateLocalValue(twilightWindow.start)}
              unavailableReason={
                target?.comet === undefined || intent !== null
                  ? undefined
                  : "Comets can currently be scheduled as a single field, not a mosaic."
              }
            />
          </Card>
          <AutoNightPanel
            startTime={twilightWindow.start.toISOString()}
            endTime={twilightWindow.end.toISOString()}
            nightDate={startMode === "absolute" ? siteNightDate(startAbsolute, location.timezone ?? "UTC") : toDateLocalValue(startAnchorDate)}
            timeZone={location.timezone ?? "UTC"}
            profileId={profileId}
            opticId={opticId}
            mountMode={mount}
            minAltitudeDeg={altitudeLimitDeg}
            categories={bestTargetCategories}
            siteKey={location.timezone ? JSON.stringify([location.latitude_deg, location.longitude_deg, location.elevation_m, location.timezone]) : null}
            lpAvailable={opticId !== "wide" && (selectedProfile?.capabilities.filter_wheel ?? false)}
          />
          {target === null && <OwnedByNotice owner="you" feature="A resolved target (pick one on the left)" />}
          {target === null && bestTargetsRequest !== null && (
            <BestTargetsPanel
              data={bestTargets.data}
              isPending={bestTargets.isPending}
              isError={bestTargets.isError}
              error={bestTargets.error}
              onPick={setTarget}
            />
          )}
          {target !== null && framing.isError && (
            <Card className="text-sm text-danger">
              {framing.error instanceof Error ? framing.error.message : "framing request failed"}
            </Card>
          )}
          {target !== null && response !== undefined && (
            <FramingStatsPanel
              mount={response.mount}
              geometry={response.geometry}
              envelope={response.envelope}
              calibrationMeasured={profiles.find((p) => p.id === profileId)?.calibration.measured ?? false}
              deviceMountCheck={response.device_mount_check}
              moon={
                night.data === undefined
                  ? null
                  : { illuminatedFraction: night.data.moon.illuminated_fraction, separationDeg: night.data.moon.separation_deg }
              }
            />
          )}
          {target !== null && startTime !== null && night.data !== undefined && response !== undefined && (
            <VisibilityPanel
              night={night.data}
              sessionStartTime={startTime}
              sessionEndTime={new Date(startTime.getTime() + sessionLengthHours * 3600_000)}
              startAboveHorizon={response.visibility.start_above_horizon}
              mount={response.mount}
            />
          )}
        </>
      }
    />
  );
}
