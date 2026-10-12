// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Scheduling tab's data layer over `/api/plans` (DESIGN.md §4). Every
 * type is a straight alias into the generated schema — never hand-mirrored.
 *
 * Ownership, restated because it is the whole point: the *list* of plans
 * lives in the snapshot (`StateSnapshot.plans`, kept current by
 * `plans.changed` over the WS), so every open client agrees on it without a
 * fetch. The *document* is fetched here when a plan is actually opened. The
 * tab never keeps its own copy of a plan beyond the in-progress edit of one
 * item; a Save is a `PUT` carrying `expected_revision`, and a `409` means
 * someone else saved first — surfaced and refetched, never overwritten.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError, api, unwrap, unwrapVoid } from "./client";
import type { components } from "./schema";

export type Plan = components["schemas"]["Plan"];
export type PlanItem = Plan["items"][number];
export type PlanItemKind = PlanItem["kind"];
/** A plan item's target: fixed J2000 coordinates, a named body, or a comet
 * retained by JPL identity/provenance and resolved only at execution. */
export type CatalogTargetSpec = components["schemas"]["CatalogTargetSpec"];
export type BodyTargetSpec = components["schemas"]["BodyTargetSpec"];
export type CometTargetSpec = components["schemas"]["CometTargetSpec"];
export type TargetSpec = CatalogTargetSpec | BodyTargetSpec | CometTargetSpec;
export type BodyId = BodyTargetSpec["body"];
export type PlanSummary = components["schemas"]["PlanSummary"];
export type PlanRecord = components["schemas"]["PlanRecordResponse"];
export type TimeSpec = PlanItem["start"];
export type TimeSpecKind = TimeSpec["kind"];
export type AbsoluteTimeSpec = components["schemas"]["AbsoluteTimeSpec"];
export type EventTimeSpec = components["schemas"]["EventTimeSpec"];
export type TargetEventTimeSpec = components["schemas"]["TargetEventTimeSpec"];
export type RelativeTimeSpec = components["schemas"]["RelativeTimeSpec"];
export type AsapTimeSpec = components["schemas"]["AsapTimeSpec"];
export type SolarEvent = components["schemas"]["SolarEvent"];
export type LunarEvent = components["schemas"]["LunarEvent"];
export type TargetEvent = components["schemas"]["TargetEvent"];
/** `PlanItem["end"]`, non-nullable: the picked kind, without the `| null`
 * every item's `end` also allows (unset — instantaneous). */
export type EndSpec = NonNullable<PlanItem["end"]>;
export type EndSpecKind = EndSpec["kind"];
export type DurationEndSpec = components["schemas"]["DurationEndSpec"];
export type EventEndSpec = components["schemas"]["EventEndSpec"];
export type TargetEventEndSpec = components["schemas"]["TargetEventEndSpec"];
export type FramingSpec = components["schemas"]["FramingSpec"];
export type MosaicGridSpec = components["schemas"]["MosaicGridSpec"];
export type ConstraintsSpec = components["schemas"]["ConstraintsSpec"];
/** The exposure block (DESIGN.md §4.3.0, schema_version 3) — derived from
 * `PlanItem` rather than named off the raw schema, the same pattern as
 * `EndSpec` above: a field the item's own union drops is a type error here
 * too, not just server-side. */
export type TakeExposuresItem = Extract<PlanItem, { kind: "take_exposures" }>;
export type ExposureCount = TakeExposuresItem["count"];
export type ExposureCountKind = ExposureCount["kind"];
export type FixedCountSpec = Extract<ExposureCount, { kind: "fixed" }>;
export type UntilBlockEndSpec = Extract<ExposureCount, { kind: "until_block_end" }>;
export type DitherSpec = NonNullable<TakeExposuresItem["dither"]>;
export type ExposureFrameType = TakeExposuresItem["frame_type"];
export type Diagnostic = components["schemas"]["Diagnostic"];
export type Severity = components["schemas"]["Severity"];
export type CompiledSchedule = components["schemas"]["CompiledSchedule"];
export type CompiledItem = components["schemas"]["CompiledItem"];
export type NightWindow = CompiledSchedule["night"];
export type ResolvedEvent = NightWindow["solar_events"][number];
export type PlanCompileResponse = components["schemas"]["PlanCompileResponse"];
export type TargetIntent = components["schemas"]["TargetIntent"];
export type FromIntentRequest = components["schemas"]["FromIntentRequest"];
export type FromIntentResponse = components["schemas"]["FromIntentResponse"];
export type ScheduleIntentRequest = components["schemas"]["ScheduleIntentRequest"];

const plansKey = ["plans"] as const;
const planKey = (id: string) => ["plans", id] as const;
const compileKey = (id: string, nightDate: string | null) => ["plans", id, "compile", nightDate] as const;

/** How long the editor waits after the last keystroke before asking
 * `/api/plans/validate` again. Structural lint is cheap server-side, but a
 * request per keystroke on a phone over Wi-Fi is not. */
export const VALIDATE_DEBOUNCE_MS = 400;

/** A `PUT` was refused because the stored revision moved on — the caller's
 * document was based on something that no longer exists. */
export function isRevisionConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

export function usePlansQuery() {
  return useQuery({
    queryKey: plansKey,
    queryFn: async () => unwrap(api.GET("/api/plans")),
  });
}

export async function readPlan(id: string): Promise<PlanRecord> {
  return unwrap(api.GET("/api/plans/{plan_id}", { params: { path: { plan_id: id } } }));
}

export function usePlanQuery(id: string | null) {
  return useQuery({
    queryKey: planKey(id ?? ""),
    queryFn: async () => readPlan(id ?? ""),
    enabled: id !== null,
  });
}

export function useCreatePlan() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (document: Plan) => unwrap(api.POST("/api/plans", { body: { document } })),
    onSuccess: (record) => {
      queryClient.setQueryData(planKey(record.document.id), record);
      void queryClient.invalidateQueries({ queryKey: plansKey });
    },
  });
}

/**
 * Save a document against the revision it was edited from. A `409` means the
 * plan changed elsewhere: the stored copy is refetched so the editor shows
 * what is actually there, and the caller decides what to do with its
 * in-progress edit — the one thing this hook never does is retry with the
 * new revision, which would be exactly the silent overwrite the revision
 * exists to prevent.
 */
export function useUpdatePlan() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ document, revision }: { document: Plan; revision: number }) =>
      unwrap(
        api.PUT("/api/plans/{plan_id}", {
          params: { path: { plan_id: document.id } },
          body: { document, expected_revision: revision },
        }),
      ),
    onSuccess: (record) => {
      queryClient.setQueryData(planKey(record.document.id), record);
      // The PUT response is authoritative. Recompilation runs under its new
      // revision key; awaiting a prefix invalidation here also waits for that
      // expensive query and leaves all timeline handles disabled after saving.
    },
    onError: (error, { document }) => {
      if (isRevisionConflict(error)) {
        // Release editing and show the conflict immediately. Only refetch
        // the document; its new revision starts the matching compilation.
        void queryClient.invalidateQueries({ queryKey: planKey(document.id), exact: true });
      }
    },
  });
}

export function useDeletePlan() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) =>
      unwrapVoid(api.DELETE("/api/plans/{plan_id}", { params: { path: { plan_id: id } } })),
    onSuccess: (_result, id) => {
      queryClient.removeQueries({ queryKey: planKey(id) });
      void queryClient.invalidateQueries({ queryKey: plansKey });
    },
  });
}

export function useApplyPlanNight() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ planId, nightDate, revision }: { planId: string; nightDate: string; revision: number }) =>
      unwrap(api.POST("/api/plans/{plan_id}/apply-night", {
        params: { path: { plan_id: planId } },
        body: { night_date: nightDate, expected_revision: revision },
      })),
    onSuccess: (record) => {
      queryClient.setQueryData(planKey(record.document.id), record);
      void queryClient.invalidateQueries({ queryKey: plansKey });
    },
    onError: () => { void queryClient.invalidateQueries({ queryKey: plansKey }); },
  });
}

export function useSchedulePlanNight() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ planId, nightDate }: { planId: string; nightDate: string | null }) =>
      unwrap(api.PUT("/api/plans/{plan_id}/night", {
        params: { path: { plan_id: planId } },
        body: { night_date: nightDate },
      })),
    onSuccess: (record) => {
      queryClient.setQueryData(planKey(record.document.id), record);
      void queryClient.invalidateQueries({ queryKey: plansKey });
    },
  });
}

/**
 * Structural diagnostics for a document *as it is being edited* — the
 * stored plan with the in-progress item applied — debounced so a keystroke
 * burst turns into one request. `null` means nothing to validate.
 *
 * Modelled as a query keyed on the serialised body (the same pattern as
 * `useFramingQuery`) rather than a mutation: the response is a pure
 * function of the document, so identical documents de-duplicate and the
 * previous answer stays on screen while the next one is in flight.
 */
export function useValidatePlan(document: Plan | null) {
  const serialised = document === null ? null : JSON.stringify(document);
  const [debounced, setDebounced] = useState(serialised);
  useEffect(() => {
    if (serialised === debounced) return;
    const timer = setTimeout(() => {
      setDebounced(serialised);
    }, VALIDATE_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [serialised, debounced]);
  return useQuery({
    queryKey: ["plans", "validate", debounced] as const,
    queryFn: async () =>
      unwrap(api.POST("/api/plans/validate", { body: { document: JSON.parse(debounced ?? "null") as Plan } })),
    enabled: debounced !== null,
    placeholderData: (previous) => previous,
  });
}

/**
 * Compile the *stored* plan for one night. `nightDate` is `null` for "the
 * server's own idea of tonight" — the request then carries no `night_date`
 * at all, so the server's default is never second-guessed client-side.
 * `revision` is part of the key so a save (or someone else's) recompiles.
 */
export function useCompilePlan(id: string | null, nightDate: string | null, revision: number | null) {
  return useQuery({
    queryKey: [...compileKey(id ?? "", nightDate), revision] as const,
    queryFn: async () =>
      unwrap(
        api.POST("/api/plans/{plan_id}/compile", {
          params: { path: { plan_id: id ?? "" } },
          body: nightDate === null ? {} : { night_date: nightDate },
        }),
      ),
    enabled: id !== null && revision !== null,
    placeholderData: (previous) => previous,
  });
}

/** DESIGN.md §3.4's "Send to Scheduling": append one item built from a
 * `TargetIntent`. The server bumps the revision; the plan query is refreshed
 * with the returned document so the Scheduling tab opens on it directly. */
export function useAddItemFromIntent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ planId, body }: { planId: string; body: FromIntentRequest }) =>
      unwrap(api.POST("/api/plans/{plan_id}/items/from-intent", { params: { path: { plan_id: planId } }, body })),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: planKey(result.document.id) });
    },
  });
}

export function useScheduleIntent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: ScheduleIntentRequest) =>
      unwrap(api.POST("/api/plans/actions/schedule-intent", { body })),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: plansKey });
      void queryClient.invalidateQueries({ queryKey: planKey(result.document.id) });
    },
  });
}


/** Save a generated document as a new plan with an explicit observing date. */
export function useCreatePlanForNight() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["PlanCreateRequest"]) => unwrap(api.POST("/api/plans", { body })),
    onSuccess: (record) => {
      queryClient.setQueryData(planKey(record.document.id), record);
      void queryClient.invalidateQueries({ queryKey: plansKey });
    },
  });
}
