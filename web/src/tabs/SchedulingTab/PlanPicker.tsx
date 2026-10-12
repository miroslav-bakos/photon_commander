// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Which plan is open: pick, create, rename, delete. The list itself is the
 * snapshot's `plans` (kept current by `plans.changed`), so two phones and a
 * laptop all show the same plans without any of them polling.
 *
 * Delete is two taps — the second button is the confirmation — rather than
 * a modal: a modal on a phone in the dark is one more thing to find.
 */
import { useState } from "react";
import { DeletePlanIcon, NewPlanIcon, RenamePlanIcon } from "../../components/ActionIcons";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import type { PlanSummary } from "../../api/plans";

const SELECT_CLASSES = "min-h-[44px] w-full min-w-0 rounded-md border border-border-strong bg-bg px-3 text-base text-fg lg:min-h-11";

export function PlanPicker({
  plans,
  selectedId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  busy,
  error,
}: {
  plans: readonly PlanSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: (name: string) => void;
  onRename: (name: string) => void;
  onDelete: () => void;
  busy: boolean;
  error: string | null;
}) {
  const [mode, setMode] = useState<"idle" | "create" | "rename" | "confirm-delete">("idle");
  const [name, setName] = useState("");
  const selected = plans.find((p) => p.id === selectedId) ?? null;

  const submitName = () => {
    const trimmed = name.trim();
    if (trimmed === "") return;
    if (mode === "create") onCreate(trimmed);
    if (mode === "rename") onRename(trimmed);
    setMode("idle");
    setName("");
  };

  return (
    <Card className="space-y-3">
      <CardTitle>Plan</CardTitle>

      {plans.length === 0 ? (
        <p className="text-sm text-fg-muted">No plans yet. Create one to start scheduling a night.</p>
      ) : (
        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-medium text-fg-muted" htmlFor="plan-select">
            Open plan
          </label>
          <select
            id="plan-select"
            className={SELECT_CLASSES}
            value={selectedId ?? ""}
            onChange={(e) => {
              onSelect(e.target.value);
            }}
          >
            {selectedId === null && (
              <option value="" disabled>
                Choose a plan…
              </option>
            )}
            {plans.map((p) => (
              <option key={p.id} value={p.id} translate="no">
                {p.name}
              </option>
            ))}
          </select>
        </div>
      )}

      {(mode === "create" || mode === "rename") && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            submitName();
          }}
        >
          <Field
            label={mode === "create" ? "New plan name" : "Rename plan"}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
            }}
          />
          <div className="flex gap-2">
            <Button type="submit" variant="primary" className="flex-1" disabled={busy || name.trim() === ""}>
              {mode === "create" ? <NewPlanIcon /> : <RenamePlanIcon />}
              {mode === "create" ? "Create" : "Save name"}
            </Button>
            <Button
              className="flex-1"
              onClick={() => {
                setMode("idle");
                setName("");
              }}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}

      {mode === "confirm-delete" && selected !== null && (
        <div className="flex flex-col gap-2 rounded-md border border-danger bg-surface-raised p-3">
          <p className="text-sm text-fg">
            Delete <span className="font-medium" translate="no">{selected.name}</span> and all its items? This cannot be undone.
          </p>
          <div className="flex gap-2">
            <Button
              variant="danger"
              className="flex-1"
              disabled={busy}
              onClick={() => {
                onDelete();
                setMode("idle");
              }}
            >
              <DeletePlanIcon />
              Delete plan
            </Button>
            <Button
              className="flex-1"
              onClick={() => {
                setMode("idle");
              }}
            >
              Keep
            </Button>
          </div>
        </div>
      )}

      {mode === "idle" && (
        <div className="flex flex-wrap gap-2">
          <Button
            variant={plans.length === 0 ? "primary" : "secondary"}
            className="flex-1"
            disabled={busy}
            onClick={() => {
              setName("");
              setMode("create");
            }}
          >
            <NewPlanIcon />
            New plan
          </Button>
          {selected !== null && (
            <>
              <Button
                className="flex-1"
                disabled={busy}
                onClick={() => {
                  setName(selected.name);
                  setMode("rename");
                }}
              >
                <RenamePlanIcon />
                Rename
              </Button>
              <Button
                variant="danger"
                className="flex-1"
                disabled={busy}
                onClick={() => {
                  setMode("confirm-delete");
                }}
              >
                <DeletePlanIcon />
                Delete
              </Button>
            </>
          )}
        </div>
      )}

      {error !== null && (
        <p className="rounded-md border border-danger bg-surface-raised px-2 py-1.5 text-xs text-danger">{error}</p>
      )}
    </Card>
  );
}
