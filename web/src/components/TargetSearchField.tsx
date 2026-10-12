// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useEffect, useId, useMemo, useState } from "react";
import {
  useAstroBodiesQuery,
  useAstroTargetQuery,
  useAstroTargetSuggestionsQuery,
  type AstroBodyModel,
  type TargetResolutionResponse,
} from "../api/astro";
import { Button } from "./Button";
import { Field } from "./Field";

export type TargetSearchSelection =
  | { kind: "catalog"; result: TargetResolutionResponse }
  | { kind: "body"; body: AstroBodyModel };

function score(name: string, query: string): number {
  const candidate = name.toLocaleLowerCase();
  if (query.length < 2) return 0;
  if (candidate === query) return 3;
  if (candidate.startsWith(query)) return 2;
  return candidate.includes(query) ? 1 : 0;
}

/** Planning-style typeahead shared by Scheduling and Mount. */
export function TargetSearchField({
  label = "Search targets",
  initialText = "",
  instant,
  onSelect,
}: {
  label?: string;
  initialText?: string;
  instant: Date | null;
  onSelect: (selection: TargetSearchSelection) => void;
}) {
  const [text, setText] = useState(initialText);
  const [settled, setSettled] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const listId = useId();
  const suggestions = useAstroTargetSuggestionsQuery(settled);
  const bodies = useAstroBodiesQuery(instant?.toISOString() ?? null);
  const resolution = useAstroTargetQuery(submitted);

  const items = useMemo<TargetSearchSelection[]>(() => {
    const query = settled.trim().toLocaleLowerCase();
    const bodyItems = (bodies.data?.bodies ?? [])
      .filter((body) => body.id !== "sun")
      .map((body) => ({ body, score: score(body.name, query) }))
      .filter((entry) => entry.score > 0)
      .sort((left, right) => right.score - left.score)
      .map(({ body }) => ({ kind: "body" as const, body }));
    const catalogItems = (suggestions.data?.suggestions ?? []).map((result) => ({
      kind: "catalog" as const,
      result,
    }));
    return [...bodyItems, ...catalogItems].slice(0, 8);
  }, [bodies.data, settled, suggestions.data]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const trimmed = text.trim();
      setSettled(trimmed.length >= 2 ? trimmed : "");
    }, 180);
    return () => { window.clearTimeout(timer); };
  }, [text]);

  useEffect(() => {
    if (resolution.data === undefined) return;
    onSelect({ kind: "catalog", result: resolution.data });
    // A fresh successful lookup is the event; callers intentionally need not
    // memoise their onSelect closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolution.dataUpdatedAt]);

  const choose = (item: TargetSearchSelection) => {
    const name = item.kind === "body" ? item.body.name : item.result.name;
    setText(name);
    setOpen(false);
    setActive(-1);
    onSelect(item);
  };
  const resolve = () => {
    const query = text.trim();
    if (query === "") return;
    if (query === submitted) void resolution.refetch();
    else setSubmitted(query);
  };

  return (
    <div className="space-y-2 sm:col-span-2">
      <form className="relative flex items-end gap-2" onSubmit={(event) => { event.preventDefault(); resolve(); }}>
        <div className="relative min-w-0 flex-1">
          <Field
            label={label}
            placeholder="M31, Veil Nebula, Jupiter…"
            value={text}
            role="combobox"
            aria-autocomplete="list"
            aria-controls={listId}
            aria-expanded={open && settled.length >= 2}
            aria-activedescendant={active >= 0 ? `${listId}-${String(active)}` : undefined}
            autoComplete="off"
            wrapperClassName="min-w-0"
            className="w-full"
            onFocus={() => { setOpen(true); }}
            onChange={(event) => { setText(event.target.value); setOpen(true); setActive(-1); }}
            onKeyDown={(event) => {
              if (event.key === "Escape") { setOpen(false); setActive(-1); return; }
              if ((event.key === "ArrowDown" || event.key === "ArrowUp") && items.length > 0) {
                event.preventDefault();
                setOpen(true);
                setActive((current) => event.key === "ArrowDown"
                  ? (current + 1) % items.length
                  : current <= 0 ? items.length - 1 : current - 1);
              }
              if (event.key === "Enter" && open && active >= 0) {
                const item = items[active];
                if (item !== undefined) { event.preventDefault(); choose(item); }
              }
            }}
          />
        </div>
        <Button type="submit" variant="primary" className="shrink-0" disabled={text.trim() === "" || resolution.isFetching}>
          {resolution.isFetching ? "Resolving…" : "Resolve"}
        </Button>
        {open && settled.length >= 2 && (
          <div id={listId} role="listbox" aria-label="Target suggestions" className="absolute inset-x-0 top-full z-30 mt-1 max-h-72 overflow-y-auto rounded-md border border-border-strong bg-surface-raised p-1 shadow-lg">
            {(suggestions.isFetching || bodies.isFetching) && items.length === 0 && <p className="px-3 py-2 text-sm text-fg-muted">Finding matches…</p>}
            {!suggestions.isFetching && !bodies.isFetching && items.length === 0 && <p className="px-3 py-2 text-sm text-fg-muted">No target matches</p>}
            {items.map((item, index) => {
              const name = item.kind === "body" ? item.body.name : (item.result.common_name ?? item.result.name);
              const detail = item.kind === "body" ? "Solar-system body" : item.result.designations.join(" · ");
              return (
                <button key={item.kind === "body" ? `body-${item.body.id}` : `catalog-${item.result.name}`} id={`${listId}-${String(index)}`} type="button" role="option" aria-selected={active === index} className={`flex min-h-[44px] w-full items-start rounded px-3 py-2 text-left text-sm hover:bg-surface ${active === index ? "bg-surface" : ""}`} onClick={() => { choose(item); }}>
                  <span className="min-w-0 flex-1"><span className="block break-words font-medium text-fg">{name}</span><span className="block break-words text-xs text-fg-muted">{detail}</span></span>
                </button>
              );
            })}
          </div>
        )}
      </form>
      {resolution.isError && <p role="alert" className="text-sm text-danger">{resolution.error instanceof Error ? resolution.error.message : "Target not found."}</p>}
    </div>
  );
}
