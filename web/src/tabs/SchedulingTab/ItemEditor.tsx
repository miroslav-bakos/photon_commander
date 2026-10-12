// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The editor for one selected item: the common fields every kind shares
 * (`start` via `TimeSpecPicker`, `end` via `EndSpecPicker`, `enabled`), then
 * the kind's own fields from `fieldsFor` — a field map, not a form per kind.
 *
 * `item` is the in-progress draft when there is one and the stored item
 * otherwise; the parent owns that distinction (frontend-react.md: the tab
 * never keeps its own copy of a plan beyond the in-progress edit of one
 * item). Save is an explicit `PUT`; Cancel discards; unsaved is marked.
 */
import { SegmentedControl } from "../../components/SegmentedControl";
import { useState } from "react";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { DraftNumberField } from "../../components/DraftNumberField";
import { Field } from "../../components/Field";
import { TargetSearchField, type TargetSearchSelection } from "../../components/TargetSearchField";
import { isFilterWheelDisconnected, useFilterWheelQuery } from "../../api/filterwheel";
import type { Diagnostic, EndSpec, NightWindow, PlanItem, TimeSpec } from "../../api/plans";
import { DiagnosticsList } from "./DiagnosticsList";
import { EndSpecPicker } from "./EndSpecPicker";
import { IntegrationEstimateSummary } from "./IntegrationEstimateSummary";
import type { IntegrationEstimate } from "./integrationEstimate";
import { ITEM_KIND_LABELS, fieldsFor, getTarget, itemHasTarget, setTarget, type FieldDef, type FieldValue } from "./itemKinds";
import { ResolvedTimeLabel, type ResolvedTimeState } from "./ResolvedTimeLabel";
import { TimeSpecPicker } from "./TimeSpecPicker";

const SELECT_CLASSES = "min-h-[44px] rounded-md border border-border-strong bg-bg px-3 text-base text-fg lg:min-h-11";
// Planning must work without a connected wheel. These are the Seestar slot
// names seen across supported models; a connected wheel's own names win.
const OFFLINE_FILTER_NAMES = ["Dark", "IR", "LP", "UVIRCut"] as const;

export function ItemEditor({
  item,
  isDirty,
  saving,
  saveBlocked = false,
  conflict,
  saveError,
  diagnostics,
  resolved,
  lensOptions,
  siteTimeZone,
  night,
  integrationEstimate,
  onChange,
  onSave,
  onCancel,
  onDelete,
}: {
  item: PlanItem;
  isDirty: boolean;
  saving: boolean;
  saveBlocked?: boolean;
  /** The last save was refused because the plan changed elsewhere; the
   * stored copy has been reloaded and the draft is still here. */
  conflict: boolean;
  saveError: string | null;
  /** This item's own findings from the live `/api/plans/validate` check
   * (`SchedulingTab`'s `validation`, run against the draft, not just the
   * stored document) — filtered to `item.id` and shown right where the
   * choice that caused them is being made, rather than only in the plan-wide
   * list above the timeline. An "until block end" with no block end is the
   * case this exists for: the owner sees the server's ERROR before Save is
   * even enabled to click, not after a 422. Save itself is not blocked on
   * severity — no item kind in this editor blocks on one — the diagnostic is
   * what surfaces it. */
  diagnostics: readonly Diagnostic[];
  resolved: ResolvedTimeState;
  /** Optic ids from the loaded scope profiles, for `framing.lens`. */
  lensOptions: readonly { value: string; label: string }[];
  siteTimeZone: string | undefined;
  /** The compiled night, for the start/end event dropdowns' own resolved
   * time. `undefined` before anything has compiled. */
  night?: NightWindow;
  integrationEstimate?: IntegrationEstimate | null;
  onChange: (item: PlanItem) => void;
  onSave: () => void;
  onCancel: () => void;
  /** Remove the item from the plan (an immediate `PUT` by the parent). */
  onDelete?: () => void;
}) {
  const fields = fieldsFor(item);
  const target = getTarget(item);
  const [searchInstant] = useState(() => new Date());
  const filterWheel = useFilterWheelQuery();
  const liveFilterNames = filterWheel.data !== undefined && !isFilterWheelDisconnected(filterWheel.data)
    ? filterWheel.data.names ?? []
    : [];
  const filterOptions = (liveFilterNames.length > 0 ? liveFilterNames : OFFLINE_FILTER_NAMES).map((name, index) => ({
    value: name,
    label: name.trim() === "" ? `Position ${String(index + 1)}` : name,
  }));
  const filterStatus = liveFilterNames.length === 0
    ? "Offline filter choices; the selected name is checked against the wheel when the plan runs."
    : null;
  const estimatesDeviceCompletion = !["wait", "goto_and_stack", "mosaic", "take_exposures"].includes(item.kind);
  return (
    <Card className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2" data-testid="item-details-header">
        <CardTitle>{ITEM_KIND_LABELS[item.kind]}</CardTitle>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          {isDirty && <Badge tone="warning">Unsaved changes</Badge>}
          <Button variant="primary" className="shrink-0" disabled={!isDirty || saving || saveBlocked} onClick={onSave}>
            {saving ? "Saving…" : "Save"}
          </Button>
          {onDelete !== undefined && (
            <Button variant="danger" className="shrink-0" disabled={saving || saveBlocked} onClick={onDelete} aria-label="Delete item">
              Delete
            </Button>
          )}
        </div>
      </div>

      {conflict && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-sm text-danger" role="alert">
          This plan changed elsewhere — it has been reloaded.{" "}
          {isDirty
            ? "Your edit was not saved: review it against the reloaded plan and save again, or cancel to discard it."
            : "Repeat the change if it is still wanted."}
        </p>
      )}
      {saveError !== null && !conflict && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-sm text-danger" role="alert">
          {saveError}
        </p>
      )}
      {diagnostics.length > 0 && (
        <div className="rounded-md border border-border-strong bg-surface-raised px-2 py-1.5">
          <DiagnosticsList diagnostics={diagnostics} />
        </div>
      )}

      <div className="flex flex-col gap-2">
        <TimeSpecPicker
          value={item.start}
          hasTarget={itemHasTarget(item)}
          siteTimeZone={siteTimeZone}
          night={night}
          onChange={(start: TimeSpec) => {
            onChange({ ...item, start });
          }}
        />
        <ResolvedTimeLabel state={resolved} field="start" />
      </div>

      <div className="flex flex-col gap-2 border-t border-border pt-3">
        <EndSpecPicker
          value={item.end ?? null}
          hasTarget={itemHasTarget(item)}
          night={night}
          durationLabel={item.kind === "mosaic" ? "Duration per panel (minutes)" : "Duration (minutes)"}
          onChange={(end: EndSpec | null) => {
            onChange({ ...item, end });
          }}
        />
        <ResolvedTimeLabel state={resolved} field="end" />
      </div>

      <div className="border-t border-border pt-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-fg-muted">Enabled</span>
          <ToggleButtons
            value={item.enabled}
            onLabel="Enabled"
            offLabel="Disabled"
            label="Enabled"
            onChange={(enabled) => {
              onChange({ ...item, enabled });
            }}
          />
        </div>
      </div>

      {integrationEstimate !== null && integrationEstimate !== undefined && (
        <IntegrationEstimateSummary estimate={integrationEstimate} />
      )}

      {estimatesDeviceCompletion && (
        <div className="grid grid-cols-1 gap-3 border-t border-border pt-3 sm:grid-cols-2">
          <DraftNumberField
            label="Planning estimate (minutes)"
            hint="Timeline and ASAP chaining only. The action still finishes from device state."
            min={0.1}
            step={0.1}
            value={item.planning_estimate_min ?? null}
            nullable
            placeholder="Automatic conservative estimate"
            onValueChange={(value) => { onChange({ ...item, planning_estimate_min: value }); }}
          />
          <DraftNumberField
            label="Hard timeout (minutes)"
            hint="Failure guard for asynchronous device completion, never a success timer."
            min={0.1}
            step={0.1}
            value={item.timeout_min ?? null}
            nullable
            placeholder="Device-safe default"
            onValueChange={(value) => { onChange({ ...item, timeout_min: value }); }}
          />
        </div>
      )}

      {target !== undefined && (
        <div className="border-t border-border pt-3">
          <TargetSearchField
            key={`${item.id}-${target?.kind ?? "none"}`}
            label="Search target"
            initialText={target?.kind === "catalog" ? (target.name ?? "") : target?.kind === "body" ? target.body : ""}
            instant={resolved.status === "compiled" && resolved.item.start_utc !== null ? new Date(resolved.item.start_utc) : searchInstant}
            onSelect={(selection: TargetSearchSelection) => {
              onChange(setTarget(item, selection.kind === "body"
                ? { kind: "body", body: selection.body.id }
                : {
                    kind: "catalog",
                    name: selection.result.name,
                    ra_j2000_h: selection.result.ra_j2000_h,
                    dec_j2000_deg: selection.result.dec_j2000_deg,
                  }));
            }}
          />
        </div>
      )}

      {fields.length > 0 && (
        <div className="grid grid-cols-1 gap-3 border-t border-border pt-3 sm:grid-cols-2">
          {fields.map((field) => (
            <FieldControl
              key={field.key}
              field={field}
              value={field.read(item)}
              lensOptions={lensOptions}
              filterOptions={filterOptions}
              filterStatus={filterStatus}
              onChange={(value) => {
                onChange(field.write(item, value));
              }}
            />
          ))}
        </div>
      )}

      <div className="flex gap-2 border-t border-border pt-3">
        <Button className="flex-1" disabled={!isDirty || saving} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </Card>
  );
}

function ToggleButtons({
  value,
  onLabel,
  offLabel,
  label,
  onChange,
}: {
  value: boolean;
  onLabel: string;
  offLabel: string;
  label: string;
  onChange: (value: boolean) => void;
}) {
  return (
    <SegmentedControl
      label={label}
      value={value ? "on" : "off"}
      options={[{ value: "on", label: onLabel }, { value: "off", label: offLabel }]}
      onChange={(candidate) => { onChange(candidate === "on"); }}
    />
  );
}

function FieldControl({
  field,
  value,
  lensOptions,
  filterOptions,
  filterStatus,
  onChange,
}: {
  field: FieldDef;
  value: FieldValue;
  lensOptions: readonly { value: string; label: string }[];
  filterOptions: readonly { value: string; label: string }[];
  filterStatus: string | null;
  onChange: (value: FieldValue) => void;
}) {
  switch (field.type) {
    case "text":
      return (
        <Field
          label={field.label}
          hint={field.hint}
          type="text"
          value={typeof value === "string" ? value : ""}
          onChange={(e) => {
            onChange(e.target.value);
          }}
        />
      );
    case "number":
      return (
        <DraftNumberField
          label={field.unit === undefined ? field.label : `${field.label} (${field.unit})`}
          hint={field.hint}
          step={field.step}
          min={field.min}
          max={field.max}
          value={typeof value === "number" ? value : null}
          nullable={field.nullable === true}
          onValueChange={onChange}
        />
      );
    case "boolean":
      return (
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-medium text-fg-muted">{field.label}</span>
          <ToggleButtons
            value={value === true}
            onLabel={field.onLabel}
            offLabel={field.offLabel}
            label={field.label}
            onChange={onChange}
          />
        </div>
      );
    case "select": {
      const current = typeof value === "string" ? value : "";
      const options = field.dynamic === "lens" ? lensOptions : field.dynamic === "filter" ? filterOptions : (field.options ?? []);
      // The stored value is always offered, even if no loaded profile knows
      // it — a select that silently snapped to its first option would rewrite
      // the document on the next keystroke.
      const withUnset = field.allowUnset === true && !options.some((o) => o.value === "")
        ? [{ value: "", label: "(leave unchanged)" }, ...options]
        : options;
      const withCurrent = withUnset.some((o) => o.value === current) ? withUnset : [{ value: current, label: current === "" ? "(unset)" : current }, ...withUnset];
      const id = `field-${field.key.replace(/\./g, "-")}`;
      return (
        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-medium text-fg-muted" htmlFor={id}>
            {field.label}
          </label>
          <select
            id={id}
            className={SELECT_CLASSES}
            value={current}
            onChange={(e) => {
              onChange(e.target.value);
            }}
          >
            {withCurrent.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          {field.hint !== undefined && <p className="text-xs text-fg-muted">{field.hint}</p>}
          {field.dynamic === "filter" && filterStatus !== null && <p className="text-xs text-fg-muted">{filterStatus}</p>}
        </div>
      );
    }
  }
}
