// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Scheduling tab: plan picker + the plan's structural diagnostics
 * (primary), the night timeline that *is* the item list — rows with an
 * enable switch, a reorder handle and the editor inline beneath the
 * selected row (the wide centre — `TimelinePane`, `skymap-viz`'s) — and the
 * night-date picker (detail). On phone the three stack in that order.
 *
 * What is state where (frontend-react.md):
 * - the plan *list* is the snapshot's (`plans.changed` over the WS);
 * - the plan *document* is TanStack's (`usePlanQuery`), refetched whenever
 *   the snapshot says its revision moved;
 * - the compiled schedule and validation diagnostics are TanStack's;
 * - the only local document state is `draft`: the in-progress edit of one
 *   item, saved with an explicit `PUT` and discarded with Cancel. Every
 *   structural change (move, enable, delete, add, rename) is an immediate
 *   `PUT`. An uncertain response keeps a separate pending intent until an
 *   authoritative read resolves it; that intent never replaces server state.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Lock, LockOpen } from "lucide-react";
import { ApiError } from "../../api/client";
import { samePlanContent } from "./saveRecovery";
import { useNow } from "../MountTab/useNow";
import { observingDate } from "../../widgets/weather/nightConditions";
import { useAstroProfilesQuery } from "../../api/astro";
import {
  isRevisionConflict,
  readPlan,
  useCompilePlan,
  useApplyPlanNight,
  useCreatePlan,
  useDeletePlan,
  usePlanQuery,
  usePlansQuery,
  useUpdatePlan,
  useValidatePlan,
  type CompiledItem,
  type Plan,
  type PlanItem,
  type PlanItemKind,
} from "../../api/plans";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import { PaneLayout } from "../../components/PaneLayout";
import { useConnectionStore } from "../../store/connectionStore";
import { DiagnosticsList } from "./DiagnosticsList";
import { ItemEditor } from "./ItemEditor";
import { estimateIntegration } from "./integrationEstimate";
import { ITEM_KIND_LABELS, defaultStartFor, newId, newItem, newPlanDocument } from "./itemKinds";
import { PlanPicker } from "./PlanPicker";
import type { ResolvedTimeState } from "./ResolvedTimeLabel";
import { RunControls } from "./RunControls";
import { isPlanEditLocked, planEditRunId, useSchedulingUiStore } from "./schedulingUiStore";
import { TimelinePane, type ItemPatch } from "./TimelinePane";
import { orderAfterTimelineMove } from "./timelineOrder";
import { snapTargets, utcIsoToSiteWallClock } from "./timelineGeometry";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

interface Draft {
  planId: string;
  itemId: string;
  item: PlanItem;
}

export function SchedulingTab() {
  const queryClient = useQueryClient();
  const snapshotPlans = useConnectionStore((s) => s.snapshot?.plans);
  const siteTimeZone = useConnectionStore((s) => s.snapshot?.location?.timezone ?? undefined);
  const run = useConnectionStore((s) => s.snapshot?.run ?? null);
  const selectedNightDate = useSchedulingUiStore((s) => s.selectedNightDate);
  const selectedNightAt = useSchedulingUiStore((s) => s.selectedNightAt);
  const now = useNow(30_000);
  // The REST list only matters before the first snapshot (or on a server
  // whose snapshot predates `plans`); once the WS has spoken, it wins.
  const plansQuery = usePlansQuery();
  const plans = useMemo(() => {
    const list = snapshotPlans ?? plansQuery.data?.plans ?? [];
    return [...list].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  }, [snapshotPlans, plansQuery.data]);

  const { selectedPlanId, selectedItemId, selectPlan, selectItem, selectNightDate } = useSchedulingUiStore();
  // The selection is honoured when it names a plan that still exists;
  // otherwise the most recently updated plan is open. Computed, not synced.
  const planId = plans.some((p) => p.id === selectedPlanId) ? selectedPlanId : (plans[0]?.id ?? null);
  const summary = plans.find((p) => p.id === planId) ?? null;

  const planQuery = usePlanQuery(planId);
  const record = planQuery.data?.document.id === planId ? planQuery.data : undefined;
  const document = record?.document;

  // Someone else saved: the snapshot's revision moved past the fetched one.
  useEffect(() => {
    if (summary === null || record === undefined) return;
    if (summary.revision > record.revision) {
      void queryClient.invalidateQueries({ queryKey: ["plans", summary.id], exact: true });
    }
  }, [summary, record, queryClient]);

  // A saved plan date is history, not the default for a new observing session.
  // Active runs retain their journalled night even after midnight or reload.
  const runningNight = run?.plan_id === planId && (run.status === "running" || run.status === "paused")
    ? run.night_date : null;
  const today = observingDate(new Date(now), siteTimeZone ?? "UTC");
  const explicitNight = selectedNightDate !== null && (selectedNightDate >= today
    || (selectedNightAt !== null && observingDate(new Date(selectedNightAt), siteTimeZone ?? "UTC") === today))
    ? selectedNightDate : null;
  const nightDate = runningNight ?? explicitNight ?? today;
  const compile = useCompilePlan(planId, nightDate, record?.revision ?? null);
  const compiledById = useMemo(() => {
    const map = new Map<string, CompiledItem>();
    for (const item of compile.data?.schedule.items ?? []) map.set(item.item_id, item);
    return map;
  }, [compile.data]);

  const profiles = useAstroProfilesQuery();
  const lensOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const profile of profiles.data?.profiles ?? []) {
      for (const optic of profile.optics) if (!seen.has(optic.id)) seen.set(optic.id, optic.label);
    }
    return [...seen].map(([value, label]) => ({ value, label }));
  }, [profiles.data]);

  // --- the one piece of local document state ------------------------------ //
  const [draft, setDraft] = useState<Draft | null>(null);
  const [pendingSave, setPendingSave] = useState<{ document: Plan; revision: number; onSaved?: () => void } | null>(null);
  const [checkingSave, setCheckingSave] = useState(false);
  const [checkedRevision, setCheckedRevision] = useState<number | null>(null);
  const [recoveryMessage, setRecoveryMessage] = useState<string | null>(null);
  const pendingItem = draft !== null && draft.planId === planId ? draft.item : null;
  const storedPendingItem = document?.items.find((i) => i.id === pendingItem?.id) ?? null;
  const hasUnsavedDraft = pendingItem !== null && storedPendingItem !== null && JSON.stringify(pendingItem) !== JSON.stringify(storedPendingItem);
  const storedItem = document?.items.find((i) => i.id === selectedItemId) ?? null;
  const editing = draft !== null && draft.planId === planId && draft.itemId === selectedItemId ? draft.item : null;
  const shownItem = editing ?? storedItem;
  const isDirty = editing !== null && storedItem !== null && JSON.stringify(editing) !== JSON.stringify(storedItem);

  const documentWithDraft: Plan | null = useMemo(() => {
    if (document === undefined) return null;
    if (pendingItem === null || !hasUnsavedDraft) return document;
    return { ...document, items: document.items.map((i) => (i.id === pendingItem.id ? pendingItem : i)) };
  }, [document, pendingItem, hasUnsavedDraft]);
  const validation = useValidatePlan(documentWithDraft);

  // --- mutations ----------------------------------------------------------- //
  const create = useCreatePlan();
  const update = useUpdatePlan();
  const remove = useDeletePlan();
  const applyNight = useApplyPlanNight();
  const busy = create.isPending || update.isPending || remove.isPending || applyNight.isPending || pendingSave !== null;
  const activePlanRun = run?.plan_id === planId && (run.status === "running" || run.status === "paused");
  const editLocks = useSchedulingUiStore((s) => s.editLocks);
  const planLocked = isPlanEditLocked(planId, run, editLocks);
  // Re-read at mutation time too: a run can start or Lock can be pressed
  // between pointer-down and release, or while a save-recovery request awaits.
  const editsLockedNow = (id: string | null = planId) => isPlanEditLocked(
    id, useConnectionStore.getState().snapshot?.run ?? null, useSchedulingUiStore.getState().editLocks,
  );
  const cannotChangeNight = planLocked || busy || record === undefined || hasUnsavedDraft;
  const applySelectedNight = (next: string) => {
    if (record === undefined || cannotChangeNight || editsLockedNow()) return;
    applyNight.mutate(
      { planId: record.document.id, nightDate: next, revision: record.revision },
      { onSuccess: () => { selectNightDate(next); } },
    );
  };
  const conflict = isRevisionConflict(update.error);

  const put = (next: Plan, onSaved?: () => void) => {
    if (record === undefined || busy || editsLockedNow(next.id)) return;
    // A fresh browser can display the first plan as a fallback before its
    // selection is persisted. Establish that identity without clearing its
    // item selection, so completion guards refer to the plan actually shown.
    if (useSchedulingUiStore.getState().selectedPlanId !== record.document.id) {
      useSchedulingUiStore.setState({ selectedPlanId: record.document.id });
    }
    const intent = { document: { ...next, updated_at: new Date().toISOString() }, revision: record.revision, onSaved };
    update.mutate(
      intent,
      { onSuccess: () => {
        if (useSchedulingUiStore.getState().selectedPlanId === intent.document.id) onSaved?.();
      }, onError: (error) => {
        if (!(error instanceof ApiError) || error.status >= 500) {
          setPendingSave(intent);
          setCheckedRevision(null);
          setRecoveryMessage("The save was not confirmed. Your pending change is kept here; check the server before retrying.");
        }
      } },
    );
  };
  const checkPendingSave = async () => {
    if (pendingSave === null || checkingSave) return;
    setCheckingSave(true);
    try {
      const saved = await queryClient.query({ queryKey: ["plans", pendingSave.document.id],
        queryFn: () => readPlan(pendingSave.document.id), staleTime: 0, retry: false });
      if (saved.document.id !== pendingSave.document.id) throw new Error("A different plan was returned.");
      if (saved.revision > pendingSave.revision && samePlanContent(saved.document, pendingSave.document)) {
        if (useSchedulingUiStore.getState().selectedPlanId === pendingSave.document.id) pendingSave.onSaved?.();
        setPendingSave(null);
        update.reset();
      } else {
        setCheckedRevision(saved.revision);
        setRecoveryMessage(saved.revision === pendingSave.revision
          ? "The pending change is not in the saved plan. You may retry it against the original revision."
          : "The saved plan has changed. Your pending change is kept, but it cannot overwrite the newer revision. Review the saved plan before reapplying it.");
      }
    } catch {
      setCheckedRevision(null);
      setRecoveryMessage("Cannot check the saved plan yet. Your pending change is still kept; reconnect and check again.");
    } finally { setCheckingSave(false); }
  };
  const retryPendingSave = () => {
    if (pendingSave === null) return;
    if (checkedRevision !== pendingSave.revision || update.isPending || editsLockedNow(pendingSave.document.id)) return;
    // Exactly the original revision; never silently overwrite another client.
    update.mutate(pendingSave, { onSuccess: () => {
      if (useSchedulingUiStore.getState().selectedPlanId === pendingSave.document.id) pendingSave.onSaved?.();
      setPendingSave(null);
    }, onError: () => { setCheckedRevision(null); } });
  };
  const putItems = (items: PlanItem[], onSaved?: () => void) => {
    if (document === undefined) return;
    put({ ...document, items }, onSaved);
  };

  const onReorderItem = (id: string, newIndex: number) => {
    if (document === undefined) return;
    const items = [...document.items];
    const from = items.findIndex((i) => i.id === id);
    if (from === -1 || newIndex < 0 || newIndex >= items.length || newIndex === from) return;
    const [moved] = items.splice(from, 1);
    if (moved === undefined) return;
    items.splice(newIndex, 0, moved);
    putItems(items);
  };
  const onToggleItem = (id: string, enabled: boolean) => {
    if (document === undefined) return;
    putItems(document.items.map((i) => (i.id === id ? { ...i, enabled } : i)));
  };
  // A timeline gesture edits the *stored* item — an unsaved draft of the
  // same item is left as it is and stays marked unsaved against the new copy.
  const onEditItem = (id: string, patch: ItemPatch) => {
    if (document === undefined) return;
    const editedIndex = document.items.findIndex((item) => item.id === id);
    const nextItems = document.items.map((i) => (i.id === id ? { ...i, ...patch } : i));
    // A chained next item normally starts exactly at this item's completion.
    // Once the user resizes this item, preserve the next item's current
    // absolute start so the extra/removed time becomes visible empty space
    // rather than silently resizing the neighbour as well.
    if ("end" in patch && editedIndex >= 0 && compile.data !== undefined) {
      const nextIndex = document.items.findIndex((item, index) => index > editedIndex && item.enabled);
      const next = nextItems[nextIndex];
      const compiledNext = nextIndex >= 0 ? compiledById.get(next?.id ?? "") : undefined;
      const wall = compiledNext?.start_utc != null ? utcIsoToSiteWallClock(compiledNext.start_utc, compile.data.schedule.night.tz) : null;
      if (next !== undefined && (next.start.kind === "relative" || next.start.kind === "asap") && wall !== null) {
        nextItems[nextIndex] = { ...next, start: { kind: "absolute", local_datetime: wall.local_datetime, fold: wall.fold } };
      }
    }
    const startPatch = "start" in patch ? patch.start : null;
    const movedEvent = startPatch?.kind === "event" && compile.data !== undefined
      ? snapTargets(compile.data.schedule.night).find((event) => event.event === startPatch.event)
      : undefined;
    const movedStartUtc = movedEvent !== undefined && startPatch?.kind === "event"
      ? new Date(movedEvent.atMs + startPatch.offset_min * 60_000).toISOString()
      : undefined;
    putItems((startPatch?.kind === "absolute" || movedStartUtc !== undefined) && compile.data !== undefined
      ? orderAfterTimelineMove(nextItems, id, compiledById, compile.data.schedule.night.tz, movedStartUtc)
      : nextItems);
  };
  const onDeleteItem = (id: string) => {
    if (document === undefined) return;
    putItems(
      document.items.filter((i) => i.id !== id),
      () => {
        if (selectedItemId === id) selectItem(null);
      },
    );
  };
  const onAdd = (kind: PlanItemKind) => {
    if (document === undefined) return;
    const id = newId();
    const item = newItem(kind, id, defaultStartFor(kind, document.items.length === 0));
    putItems([...document.items, item], () => {
      selectItem(id);
    });
  };
  const onSave = () => {
    if (documentWithDraft === null || !isDirty) return;
    const submitted = draft;
    let next = documentWithDraft;
    if (pendingItem !== null && storedPendingItem !== null && compile.data !== undefined
        && JSON.stringify(pendingItem.start) !== JSON.stringify(storedPendingItem.start)) {
      const start = pendingItem.start;
      const event = start.kind === "event"
        ? snapTargets(compile.data.schedule.night).find((entry) => entry.event === start.event)
        : undefined;
      const movedStartUtc = event !== undefined && start.kind === "event"
        ? new Date(event.atMs + start.offset_min * 60_000).toISOString()
        : undefined;
      if (start.kind === "absolute" || movedStartUtc !== undefined) {
        next = { ...next, items: orderAfterTimelineMove(
          next.items, pendingItem.id, compiledById, compile.data.schedule.night.tz, movedStartUtc,
        ) };
      }
    }
    put(next, () => {
      setDraft((current) => current === submitted ? null : current);
    });
  };

  const itemLabel = (itemId: string): string => {
    const index = document?.items.findIndex((i) => i.id === itemId) ?? -1;
    const item = document?.items[index];
    return item === undefined ? itemId : `${String(index + 1)}. ${ITEM_KIND_LABELS[item.kind]}`;
  };

  const resolved: ResolvedTimeState = (() => {
    if (compile.isError) return { status: "error", message: errorMessage(compile.error, "compile failed") };
    if (shownItem === null) return { status: "pending" };
    const compiled = compiledById.get(shownItem.id);
    if (compiled === undefined) return compile.data === undefined ? { status: "pending" } : { status: "unsaved" };
    return { status: "compiled", item: compiled };
  })();
  const selectedEstimate = shownItem === null
    ? null
    : estimateIntegration(
        shownItem,
        compiledById.get(shownItem.id),
        document?.items.findIndex((candidate) => candidate.enabled && candidate.kind === "goto_and_stack") ===
          document?.items.findIndex((candidate) => candidate.id === shownItem.id),
      );

  const planError =
    create.isError
      ? errorMessage(create.error, "could not create the plan")
      : remove.isError
        ? errorMessage(remove.error, "could not delete the plan")
        : planQuery.isError
          ? errorMessage(planQuery.error, "could not load the plan")
          : null;
  const saveError = update.isError && !conflict ? errorMessage(update.error, "could not save the plan") : null;
  const startBlockedReason = (() => {
    if (pendingSave !== null) return "Check the pending plan save before starting.";
    if (update.isPending || applyNight.isPending) return "Wait for the plan save to finish before starting.";
    if (hasUnsavedDraft) return "Save or cancel the current item edit before starting the plan.";
    if (validation.data?.diagnostics.some((diagnostic) => diagnostic.severity === "error") === true) {
      return "Fix the plan errors before starting.";
    }
    if (compile.data?.diagnostics.some((diagnostic) => diagnostic.severity === "error") === true) {
      return "Fix the compiled schedule errors before starting.";
    }
    if (compile.isPending || compile.isPlaceholderData || compile.data === undefined) return "Waiting for the schedule to compile.";
    return null;
  })();

  return (
    <PaneLayout
      primary={
        <div className="flex flex-col gap-4">
          {activePlanRun && (
            <p role="status" className="text-sm text-fg-muted">
              This plan is running from its saved execution snapshot. Edits and night changes apply to the next start.
            </p>
          )}
          <PlanPicker
            readOnly={planLocked}
            editingControl={planId !== null && (
              <Button aria-label={planLocked ? "Unlock plan" : "Lock plan"} aria-pressed={planLocked}
                disabled={update.isPending || applyNight.isPending || remove.isPending}
                onClick={() => {
                  const currentRun = useConnectionStore.getState().snapshot?.run ?? null;
                  useSchedulingUiStore.getState().setPlanEditLock(
                    planId, planEditRunId(planId, currentRun), !editsLockedNow(),
                  );
                }}>
                {planLocked ? <Lock size={18} aria-hidden="true" /> : <LockOpen size={18} aria-hidden="true" />}
                {planLocked ? "Unlock" : "Lock"}
              </Button>
            )}
            plans={plans}
            selectedId={planId}
            onSelect={selectPlan}
            onCreate={(name) => {
              const doc = newPlanDocument(name);
              create.mutate(doc, {
                onSuccess: () => {
                  selectPlan(doc.id);
                },
              });
            }}
            onRename={(name) => {
              if (document !== undefined) put({ ...document, name });
            }}
            onDelete={() => {
              if (planId !== null && !editsLockedNow()) {
                remove.mutate(planId, {
                  onSuccess: () => {
                    selectPlan(null);
                  },
                });
              }
            }}
            busy={busy}
            error={planError}
          />
          {planLocked && <p role="status" className="text-sm text-fg-muted">
            Plan locked. Unlock to edit values or move timeline items.
          </p>}
          <Card className="space-y-2">
            <CardTitle>Night</CardTitle>
            {compile.data?.schedule.night.solar_events.some(
              (event) => event.event === "astronomical_midnight" && event.outcome === "always_above",
            ) && (
              <p role="alert" className="rounded-md border border-danger bg-surface-raised px-3 py-2 text-sm text-danger">
                No astronomical night on this date at the observing site. Astronomical midnight cannot be
                calculated; only shallower twilight (nautical or civil) may occur.
              </p>
            )}
            <Field
              label="Night of"
              type="date"
              disabled={cannotChangeNight}
              value={nightDate}
              hint="Changing the night reuses this plan, preserving local clock times."
              onChange={(e) => {
                const next = e.target.value;
                if (next) applySelectedNight(next);
              }}
            />
            {(
              <Button
                disabled={cannotChangeNight}
                onClick={() => { applySelectedNight(observingDate(new Date(), siteTimeZone ?? compile.data?.schedule.night.tz ?? "UTC")); }}
              >
                Use tonight
              </Button>
            )}
            <Button
              variant="primary"
              disabled={cannotChangeNight}
              onClick={() => { applySelectedNight(nightDate); }}
            >
              {applyNight.isPending ? "Applying plan…" : "Use plan for selected night"}
            </Button>
            <p className="text-xs text-fg-muted">Moves fixed start times to this night, keeping their local clock times.</p>
            {applyNight.isError && <p role="alert" className="text-xs text-danger">{errorMessage(applyNight.error, "Could not apply the plan to this night.")}</p>}
          </Card>
          <RunControls
            planId={planId}
            nightDate={compile.data?.schedule.night.date ?? null}
            timeZone={compile.data?.schedule.night.tz ?? siteTimeZone}
            startBlockedReason={startBlockedReason}
            itemLabel={itemLabel}
            onExecutionStart={(id) => {
              useSchedulingUiStore.getState().setPlanEditLock(
                id, planEditRunId(id, useConnectionStore.getState().snapshot?.run ?? null), true,
              );
            }}
          />
          {planId !== null && document === undefined && planQuery.isPending && (
            <Card className="text-sm text-fg-muted">Loading plan…</Card>
          )}
          {pendingSave !== null && (
            <Card className="space-y-2">
              <p role="alert">{recoveryMessage}</p>
              <div className="flex flex-wrap gap-2">
                <Button disabled={checkingSave || update.isPending} onClick={() => { void checkPendingSave(); }}>
                  {checkingSave ? "Checking saved plan…" : "Check saved plan"}
                </Button>
                <Button disabled={isPlanEditLocked(pendingSave.document.id, run, editLocks) || checkingSave || update.isPending || checkedRevision !== pendingSave.revision} onClick={retryPendingSave}>Retry pending change</Button>
                <Button disabled={checkingSave || update.isPending || checkedRevision === null} onClick={() => { setPendingSave(null); update.reset(); }}>Discard pending change</Button>
              </div>
            </Card>
          )}
          {document !== undefined && (
            <Card className="space-y-2">
              <CardTitle>Plan check</CardTitle>
              {validation.isError && (
                <p className="text-sm text-danger">{errorMessage(validation.error, "validation failed")}</p>
              )}
              {!validation.isError && validation.data === undefined && (
                <p className="text-sm text-fg-muted">Checking…</p>
              )}
              {validation.data?.diagnostics.length === 0 && (
                <p className="text-sm text-fg-muted">No structural problems.</p>
              )}
              {validation.data !== undefined && (
                <DiagnosticsList diagnostics={validation.data.diagnostics} itemLabel={itemLabel} onSelectItem={selectItem} />
              )}
              {saveError !== null && shownItem === null && (
                <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-sm text-danger" role="alert">
                  {saveError}
                </p>
              )}
              {conflict && shownItem === null && (
                <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-sm text-danger" role="alert">
                  This plan changed elsewhere — it has been reloaded. Repeat the change if it is still wanted.
                </p>
              )}
            </Card>
          )}
          {document === undefined && plans.length === 0 && (
            <Card className="text-sm text-fg-muted">
              A plan is a list of items — start-up, targets, calibration, shutdown — each with a start that is
              resolved against the night when it is compiled. Create one above.
            </Card>
          )}
        </div>
      }
      secondary={
        <TimelinePane
          schedule={compile.data?.schedule}
          run={run}
          isPending={compile.isPending || compile.isFetching}
          isError={compile.isError}
          errorMessage={compile.isError ? errorMessage(compile.error, "compile failed") : undefined}
          selectedItemId={selectedItemId}
          onSelectItem={selectItem}
          items={document?.items}
          dirtyItemId={hasUnsavedDraft ? pendingItem.id : null}
          busy={busy}
          editor={
            shownItem !== null && planId !== null ? (
              <ItemEditor
                key={`${planId}/${shownItem.id}`}
                item={shownItem}
                readOnly={planLocked}
                isDirty={isDirty}
                saving={update.isPending}
                saveBlocked={pendingSave !== null}
                conflict={conflict}
                saveError={saveError}
                diagnostics={validation.data?.diagnostics.filter((d) => d.item_id === shownItem.id) ?? []}
                resolved={resolved}
                lensOptions={lensOptions}
                siteTimeZone={siteTimeZone}
                night={compile.data?.schedule.night}
                integrationEstimate={selectedEstimate}
                onChange={(item) => {
                  if (editsLockedNow()) return;
                  setDraft({ planId, itemId: item.id, item });
                }}
                onSave={onSave}
                onCancel={() => {
                  setDraft(null);
                  update.reset();
                }}
                onDelete={() => {
                  onDeleteItem(shownItem.id);
                }}
              />
            ) : undefined
          }
          onEditItem={document === undefined || planLocked ? undefined : onEditItem}
          onReorderItem={document === undefined || planLocked ? undefined : onReorderItem}
          onToggleItem={document === undefined || planLocked ? undefined : onToggleItem}
          onAddItem={document === undefined || planLocked ? undefined : onAdd}
        />
      }
    />
  );
}
