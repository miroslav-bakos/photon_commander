// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Name search over the bundled offline catalogue (`/api/astro/target`,
 * `ssc.astro.catalog` — no network, no live lookup) plus a manual RA/Dec
 * fallback for anything the catalogue does not carry. Either path ends the
 * same way: one `PlanningTarget` handed to `PlanningTab`.
 */
import { useEffect, useId, useMemo, useState } from "react";
import { Badge } from "../../components/Badge";
import { ClearIcon, SearchGoIcon } from "../../components/ActionIcons";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import {
  useAstroBodiesQuery,
  useAstroTargetQuery,
  useAstroTargetSuggestionsQuery,
  type AstroBodyModel,
  type TargetResolutionResponse,
} from "../../api/astro";
import { objectTypeLabel } from "./objectTypeLabel";
import { targetFromCoordinates, type PlanningTarget } from "./types";

type SearchSuggestion =
  | { kind: "catalog"; result: TargetResolutionResponse }
  | { kind: "body"; body: AstroBodyModel };

function editDistance(left: string, right: string): number {
  const previous = Array.from({ length: right.length + 1 }, (_, index) => index);
  for (let leftIndex = 1; leftIndex <= left.length; leftIndex += 1) {
    const current = [leftIndex];
    for (let rightIndex = 1; rightIndex <= right.length; rightIndex += 1) {
      current[rightIndex] = Math.min(
        (current[rightIndex - 1] ?? 0) + 1,
        (previous[rightIndex] ?? 0) + 1,
        (previous[rightIndex - 1] ?? 0) + (left[leftIndex - 1] === right[rightIndex - 1] ? 0 : 1),
      );
    }
    previous.splice(0, previous.length, ...current);
  }
  return previous[right.length] ?? right.length;
}

function bodyMatchScore(name: string, query: string): number {
  if (query.length < 2) return 0;
  const candidate = name.toLocaleLowerCase();
  if (candidate === query) return 100;
  if (candidate.startsWith(query)) return 90;
  if (candidate.includes(query)) return 80;
  return editDistance(candidate, query) <= 2 ? 60 : 0;
}

export function TargetPicker({
  target,
  onTargetChange,
  instant = null,
}: {
  target: PlanningTarget | null;
  onTargetChange: (target: PlanningTarget | null) => void;
  /** The selected observing instant. Body coordinates are computed for this
   * time and the active site by `/api/astro/bodies`. */
  instant?: Date | null;
}) {
  const [searchText, setSearchText] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [settledSearchText, setSettledSearchText] = useState("");
  const [suggestionsOpen, setSuggestionsOpen] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const suggestionsId = useId();
  const resolution = useAstroTargetQuery(submittedQuery);
  const suggestions = useAstroTargetSuggestionsQuery(settledSearchText);
  const bodies = useAstroBodiesQuery(instant?.toISOString() ?? null);
  const suggestionItems = useMemo<SearchSuggestion[]>(() => {
    const query = settledSearchText.trim().toLocaleLowerCase();
    const bodyMatches = (bodies.data?.bodies ?? [])
      .filter((body) => body.id !== "sun")
      .map((body) => ({ kind: "body" as const, body, score: bodyMatchScore(body.name, query) }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score)
      .map(({ kind, body }) => ({ kind, body }));
    const catalogueMatches = (suggestions.data?.suggestions ?? []).map((result) => ({
      kind: "catalog" as const,
      result,
    }));
    return [...bodyMatches, ...catalogueMatches].slice(0, 8);
  }, [bodies.data, settledSearchText, suggestions.data]);

  useEffect(() => {
    const trimmed = searchText.trim();
    const timer = window.setTimeout(() => {
      setSettledSearchText(trimmed.length >= 2 ? trimmed : "");
    }, 180);
    return () => {
      window.clearTimeout(timer);
    };
  }, [searchText]);

  useEffect(() => {
    if (resolution.data === undefined) return;
    const r = resolution.data;
    onTargetChange({
      name: r.name,
      raH: r.ra_j2000_h,
      decDeg: r.dec_j2000_deg,
      magnitude: r.magnitude,
      sizeMajorArcmin: r.size_major_arcmin,
      positionAngleDeg: r.position_angle_deg,
      objectType: r.object_type,
    });
    // Only re-run when a *new* resolution arrives — `onTargetChange` is a
    // fresh closure every render in the parent, and including it here would
    // re-fire this effect (and re-push the same target) on every keystroke
    // elsewhere in the tab.
    //
    // `dataUpdatedAt` and not just `data`: TanStack Query's structural
    // sharing hands back the *same object reference* when a refetch produces
    // deeply-equal data, so re-resolving the same target (see
    // `handleResolve`) left this effect's `[data]` dependency untouched and
    // the re-push never happened — the fetch went out and its result was
    // silently dropped. `dataUpdatedAt` advances on every successful fetch
    // whether or not the payload changed, which is exactly the "a resolution
    // arrived" signal this wants. Verified in `TargetPicker.test.tsx`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolution.data, resolution.dataUpdatedAt]);

  function handleResolve(): void {
    const trimmed = searchText.trim();
    if (trimmed.length === 0) return;
    // Reported live: "resolve button does not work properly for the same
    // object already resolved - it should re-resolve". Clicking Resolve
    // again with the exact same text is a no-op React state update
    // (`setSubmittedQuery` sees an unchanged string), so the query key
    // never changes and the effect above never re-fires -- if `target` had
    // since been cleared (e.g. "Clear target" below), it stayed cleared.
    // `refetch()` runs the lookup again unconditionally; the effect's
    // `dataUpdatedAt` dependency is what makes the identical answer count as
    // a new resolution and re-push the target.
    if (trimmed === submittedQuery) {
      void resolution.refetch();
    } else {
      setSubmittedQuery(trimmed);
    }
  }

  function chooseSuggestion(item: SearchSuggestion): void {
    const name = item.kind === "body" ? item.body.name : item.result.name;
    setSearchText(name);
    setSuggestionsOpen(false);
    setActiveSuggestion(-1);
    if (item.kind === "body") {
      setSubmittedQuery("");
      onTargetChange({
        name: item.body.name,
        raH: item.body.ra_j2000_h,
        decDeg: item.body.dec_j2000_deg,
        magnitude: null,
        sizeMajorArcmin: item.body.apparent_diameter_arcmin,
        positionAngleDeg: null,
        objectType: item.body.kind,
        bodyId: item.body.id,
      });
      return;
    }
    if (name === submittedQuery) {
      void resolution.refetch();
    } else {
      setSubmittedQuery(name);
    }
  }

  return (
    <Card title="Target" collapseKey="planning.target" className="space-y-4">
      <form
        className="relative flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          handleResolve();
        }}
      >
        <div className="relative min-w-0 flex-1">
          <Field
            label="Search targets"
            placeholder="M31, Veil Nebula, Jupiter…"
            value={searchText}
            role="combobox"
            aria-autocomplete="list"
            aria-controls={suggestionsId}
            aria-expanded={suggestionsOpen && settledSearchText.length >= 2}
            aria-activedescendant={
              activeSuggestion >= 0 ? `${suggestionsId}-${String(activeSuggestion)}` : undefined
            }
            autoComplete="off"
            onFocus={() => {
              setSuggestionsOpen(true);
            }}
            onChange={(e) => {
              setSearchText(e.target.value);
              setSuggestionsOpen(true);
              setActiveSuggestion(-1);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setSuggestionsOpen(false);
                setActiveSuggestion(-1);
                return;
              }
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                if (suggestionItems.length === 0) return;
                e.preventDefault();
                setSuggestionsOpen(true);
                setActiveSuggestion((current) => {
                  if (e.key === "ArrowDown") return (current + 1) % suggestionItems.length;
                  return current <= 0 ? suggestionItems.length - 1 : current - 1;
                });
                return;
              }
              if (e.key === "Enter" && suggestionsOpen && activeSuggestion >= 0) {
                const item = suggestionItems[activeSuggestion];
                if (item !== undefined) {
                  e.preventDefault();
                  chooseSuggestion(item);
                }
              }
            }}
            className="w-full"
            wrapperClassName="min-w-0"
          />
        </div>
        <Button type="submit" variant="primary" className="size-11 shrink-0 p-0" aria-label="Go" title="Resolve and go to target">
          <SearchGoIcon />
        </Button>
        {target !== null && (
          <Button
            type="button"
            variant="danger"
            className="size-11 shrink-0 p-0"
            aria-label={`Clear ${target.name}`}
            title="Clear target"
            onClick={() => {
              onTargetChange(null);
            }}
          >
            <ClearIcon />
          </Button>
        )}
        {suggestionsOpen && settledSearchText.length >= 2 && (
          <div
            id={suggestionsId}
            role="listbox"
            aria-label="Target suggestions"
            className="absolute inset-x-0 top-full z-30 mt-1 max-h-72 overflow-y-auto rounded-md border border-border-strong bg-surface-raised p-1 shadow-lg"
          >
              {(suggestions.isFetching || bodies.isFetching) && suggestionItems.length === 0 && (
                <p className="px-3 py-2 text-sm text-fg-muted">Finding matches…</p>
              )}
              {!suggestions.isFetching && !bodies.isFetching && suggestionItems.length === 0 && !suggestions.isError && (
                <p className="px-3 py-2 text-sm text-fg-muted">No target matches</p>
              )}
              {suggestions.isError && (
                <p className="px-3 py-2 text-sm text-danger">Suggestions unavailable</p>
              )}
              {suggestionItems.map((item, index) => (
                <button
                  key={
                    item.kind === "body"
                      ? `body-${item.body.id}`
                      : `catalog-${item.result.name}`
                  }
                  id={`${suggestionsId}-${String(index)}`}
                  type="button"
                  role="option"
                  aria-selected={index === activeSuggestion}
                  className={`flex min-h-[44px] w-full items-start justify-between gap-3 rounded px-3 py-2 text-left text-sm hover:bg-surface ${
                    index === activeSuggestion ? "bg-surface" : ""
                  }`}
                  onClick={() => {
                    chooseSuggestion(item);
                  }}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block break-words font-medium text-fg">
                      {item.kind === "body"
                        ? item.body.name
                        : (item.result.common_name ?? item.result.name)}
                    </span>
                    <span className="block break-words text-xs text-fg-muted">
                      {item.kind === "body"
                        ? "Moving target · position at selected session time"
                        : item.result.designations.join(" · ")}
                    </span>
                  </span>
                  {item.kind === "body" ? (
                    <span className="max-w-[35%] shrink-0 text-right text-xs text-fg-muted">solar system</span>
                  ) : item.result.object_type !== null && (
                    <span className="max-w-[35%] shrink-0 text-right text-xs text-fg-muted">
                      {objectTypeLabel(item.result.object_type)}
                    </span>
                  )}
                </button>
              ))}
          </div>
        )}
      </form>

      {instant === null && (
        <p className="text-xs text-fg-muted">
          Solar-system targets become available when the selected session start resolves.
        </p>
      )}
      {instant !== null && bodies.isError && (
        <p className="text-xs text-danger">
          Solar-system targets are unavailable: {bodies.error instanceof Error ? bodies.error.message : "ephemeris lookup failed"}
        </p>
      )}

      {target?.bodyId !== undefined && (
        <p className="rounded-md border border-accent/40 bg-accent-muted p-2 text-xs text-fg-muted">
          Moving target: these coordinates are for the selected session time. The saved plan keeps the
          {" "}{target.name} identity and resolves it again immediately before execution.
        </p>
      )}

      {target?.comet !== undefined && (
        <p className="rounded-md border border-warning bg-surface-raised p-2 text-xs text-warning">
          Comet preview at {new Date(target.comet.resolvedAt).toLocaleString()}. The map shows this selected-time
          position only. A scheduled comet keeps its JPL identity and is resolved again immediately before each
          slew; unsafe or stale orbit data is refused. {target.comet.accuracyNote}
        </p>
      )}

      {resolution.isFetching && <p className="text-sm text-fg-muted">Looking up "{submittedQuery}"…</p>}
      {resolution.isError && (
        <p className="text-sm text-danger">{resolution.error instanceof Error ? resolution.error.message : "not found"}</p>
      )}

      {target !== null && (
        <div className="space-y-1.5 rounded-md border border-border bg-surface-raised p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="min-w-0 truncate font-medium text-fg">{target.name}</span>
            {target.objectType !== null && <Badge tone="accent">{objectTypeLabel(target.objectType)}</Badge>}
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-fg-muted">
            <dt>RA / Dec</dt>
            <dd className="text-fg tabular-nums">
              {target.raH.toFixed(4)}h / {target.decDeg.toFixed(3)}°
            </dd>
            {target.magnitude !== null && (
              <>
                <dt>Magnitude</dt>
                <dd className="text-fg tabular-nums">{target.magnitude.toFixed(1)}</dd>
              </>
            )}
            {target.sizeMajorArcmin !== null && (
              <>
                <dt>Size</dt>
                <dd className="text-fg tabular-nums">{target.sizeMajorArcmin.toFixed(1)}′</dd>
              </>
            )}
            {target.positionAngleDeg !== null && (
              <>
                <dt>Position angle</dt>
                <dd className="text-fg tabular-nums">{target.positionAngleDeg.toFixed(0)}° (N→E)</dd>
              </>
            )}
            {target.comet !== undefined && (
              <>
                <dt>Orbit elements</dt>
                <dd className="text-fg tabular-nums">
                  epoch JD {target.comet.elementEpochJdTdb.toFixed(1)} · {Math.round(target.comet.elementAgeDays).toLocaleString()} d from preview
                </dd>
                <dt>Orbit solution</dt>
                <dd className="text-fg">{target.comet.solutionDate ?? "date unavailable"}</dd>
                <dt>Snapshot</dt>
                <dd className={target.comet.stale ? "text-warning" : "text-fg"}>
                  {target.comet.snapshotAgeDays.toFixed(1)} d old{target.comet.stale ? " · stale" : ""} ·{" "}
                  <a className="underline" href={target.comet.sourceUrl} target="_blank" rel="noreferrer">
                    NASA/JPL SBDB
                  </a>
                </dd>
              </>
            )}
          </dl>
        </div>
      )}

      <ManualCoordinates onTargetChange={onTargetChange} />
    </Card>
  );
}

/** Not every target of interest is in the bundled catalogue — a manual
 * RA/Dec entry that produces the exact same `PlanningTarget` shape, so
 * everything downstream (framing, mosaic, the overlay) does not need to
 * know which path a target came from. */
function ManualCoordinates({
  onTargetChange,
}: {
  onTargetChange: (target: PlanningTarget | null) => void;
}) {
  const [raH, setRaH] = useState("");
  const [decDeg, setDecDeg] = useState("");

  return (
    <details className="text-sm">
      <summary className="cursor-pointer text-fg-muted">Or enter RA/Dec directly</summary>
      <form
        className="mt-2 flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const ra = Number.parseFloat(raH);
          const dec = Number.parseFloat(decDeg);
          if (!Number.isFinite(ra) || !Number.isFinite(dec)) return;
          onTargetChange(targetFromCoordinates(ra, dec));
        }}
      >
        <Field
          label="RA (hours)"
          type="number"
          step="any"
          min={0}
          max={24}
          value={raH}
          onChange={(e) => {
            setRaH(e.target.value);
          }}
          className="w-28"
        />
        <Field
          label="Dec (deg)"
          type="number"
          step="any"
          min={-90}
          max={90}
          value={decDeg}
          onChange={(e) => {
            setDecDeg(e.target.value);
          }}
          className="w-28"
        />
        <Button type="submit" variant="secondary">
          Use coordinates
        </Button>
      </form>
      {/* The "Clear target" button used to live here, inside this collapsed
          <details>. It has moved onto the resolved-target card above, where
          it is visible without opening a section about entering coordinates
          by hand — two ways to do the same thing, one of them hidden, was
          worse than one obvious way. */}
    </details>
  );
}
