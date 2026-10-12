// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useRef, useState } from "react";
import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { DraftNumberField } from "../../components/DraftNumberField";
import { useClearHorizonMutation, useSetHorizonMutation, useSettingsQuery } from "../../api/settings";
import type { HorizonPointRequest } from "../../api/types";
import {
  ALTITUDE_MAX_DEG,
  ALTITUDE_MIN_DEG,
  azimuthCollides,
  clampAltitudeDeg,
  horizonStats,
  roundToStep,
  sortByAzimuth,
  widestGapMidpointDeg,
  wrapAzimuthDeg,
} from "./horizonMath";

/** How close (in degrees) a pointer-driven add/drag is allowed to land next
 * to an existing point before it is treated as a collision and refused
 * client-side — the same "no duplicate azimuths" rule the server enforces
 * (`HorizonRequest` in `ssc/api/schemas.py`), applied early so an imprecise
 * tap fails softly instead of round-tripping a 422. Numeric entry in the
 * table is not gated this way: it is a deliberate, precise action, and a
 * genuine duplicate there surfaces the server's own message. */
const POINTER_COLLISION_TOLERANCE_DEG = 1;
const DEGREE_STEP = 0.5;

function formatDeg(value: number): string {
  return `${value.toFixed(1)}°`;
}

/**
 * DESIGN.md §5: "under-appreciated and very high value for a backyard scope"
 * — the per-azimuth altitude mask for trees, roofs and the neighbour's
 * floodlight. §3.2 wants it drawn; the map itself is `skymap-viz`'s (not
 * owned here), this is the *editor*.
 *
 * The saved profile lives entirely in the settings query cache — `draft` is
 * the only local state, and it holds nothing when there is no in-progress
 * edit (architecture policy: "clients render state, never own it").
 */
export function HorizonPanel() {
  const settings = useSettingsQuery();
  const setHorizon = useSetHorizonMutation();
  const clearHorizon = useClearHorizonMutation();

  const [draft, setDraft] = useState<HorizonPointRequest[] | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (settings.isPending) return <Card>Loading horizon profile…</Card>;
  if (settings.isError && settings.data === undefined) {
    return (
      <Card className="text-danger">Could not load the horizon profile: {String(settings.error)}</Card>
    );
  }

  const horizon = settings.data.horizon;
  // `active` is the real signal (architecture policy invariant 7): a profile that was
  // never drawn is `null` here, never an empty array standing in for it.
  const savedPoints: HorizonPointRequest[] | null = horizon.active ? (horizon.points ?? []) : null;
  const points = draft ?? savedPoints ?? [];
  const dirty = draft !== null;
  const stats = horizonStats(points);

  function startDrawing() {
    // ssc/astro/horizon.py's own `flat_horizon()`: two points is the minimum
    // that pins a constant altitude round the whole circle, so editing
    // starts from "clear horizon" rather than an arbitrary shape.
    setDraft([
      { azimuth_deg: 0, altitude_deg: 0 },
      { azimuth_deg: 180, altitude_deg: 0 },
    ]);
    setNotice(null);
  }

  function addPoint(azimuthDeg: number, altitudeDeg: number) {
    const azimuth_deg = roundToStep(wrapAzimuthDeg(azimuthDeg), DEGREE_STEP);
    const altitude_deg = roundToStep(clampAltitudeDeg(altitudeDeg), DEGREE_STEP);
    if (azimuthCollides(points, azimuth_deg, POINTER_COLLISION_TOLERANCE_DEG)) {
      setNotice("There's already a point near that bearing — drag it instead of adding another.");
      return;
    }
    const next = [...points, { azimuth_deg, altitude_deg }];
    setDraft(next);
    setSelectedIndex(next.length - 1);
    setNotice(null);
  }

  function movePoint(index: number, azimuthDeg: number, altitudeDeg: number) {
    const azimuth_deg = roundToStep(wrapAzimuthDeg(azimuthDeg), DEGREE_STEP);
    const altitude_deg = roundToStep(clampAltitudeDeg(altitudeDeg), DEGREE_STEP);
    // Mid-drag, a collision just means "don't move past the neighbour" —
    // the point holds its last valid position rather than jumping and
    // producing a duplicate, and there is no need to narrate every frame.
    if (azimuthCollides(points, azimuth_deg, DEGREE_STEP, index)) return;
    setDraft(points.map((point, i) => (i === index ? { azimuth_deg, altitude_deg } : point)));
  }

  function editPointField(index: number, field: "azimuth_deg" | "altitude_deg", raw: string) {
    if (raw.trim() === "") return; // mid-edit (backspaced to empty); wait for a real value
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return;
    const value = field === "azimuth_deg" ? wrapAzimuthDeg(parsed) : clampAltitudeDeg(parsed);
    setDraft(points.map((point, i) => (i === index ? { ...point, [field]: value } : point)));
    setNotice(null);
  }

  function deletePoint(index: number) {
    setDraft(points.filter((_, i) => i !== index));
    setSelectedIndex((current) => {
      if (current === null) return null;
      if (current === index) return null;
      return current > index ? current - 1 : current;
    });
    setNotice(null);
  }

  function handleSave() {
    if (points.length < 2) return;
    setHorizon.mutate(
      { points: points.map(({ azimuth_deg, altitude_deg }) => ({ azimuth_deg, altitude_deg })) },
      {
        onSuccess: () => {
          setDraft((current) => current === draft ? null : current);
          setNotice(null);
        },
      },
    );
  }

  function handleDiscard() {
    setDraft(null);
    setSelectedIndex(null);
    setNotice(null);
    setHorizon.reset();
  }

  function handleClear() {
    clearHorizon.mutate(undefined, {
      onSuccess: () => {
        setDraft((current) => current === draft ? null : current);
        setNotice(null);
      },
    });
  }

  if (savedPoints === null && draft === null) {
    return (
      <Card>
      {(settings.isError) && <p role="alert" className="mb-3 text-sm text-danger">Could not refresh settings. Showing the last saved values; your edits are retained.</p>}
        <CardTitle>No profile set</CardTitle>
        <p className="text-sm text-fg-muted">
          No skyline has been drawn. Planning assumes a clear horizon — 0° minimum altitude at every
          bearing — until you add one. Trees, roofs and anything else that actually blocks the sky are
          not accounted for.
        </p>
        <div className="mt-3">
          <Button variant="primary" onClick={startDrawing}>
            Draw a horizon
          </Button>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      {(settings.isError) && <p role="alert" className="mb-3 text-sm text-danger">Could not refresh settings. Showing the last saved values; your edits are retained.</p>}
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <CardTitle>Skyline mask</CardTitle>
        {dirty && <Badge tone="warning">Unsaved changes</Badge>}
      </div>

      {stats !== null && (
        <dl className="mb-3 grid grid-cols-3 gap-2 text-sm">
          <div>
            <dt className="text-fg-muted">Min altitude</dt>
            <dd className="text-fg">{formatDeg(stats.minAltitudeDeg)}</dd>
          </div>
          <div>
            <dt className="text-fg-muted">Max altitude</dt>
            <dd className="text-fg">{formatDeg(stats.maxAltitudeDeg)}</dd>
          </div>
          <div>
            <dt className="text-fg-muted">Mean altitude</dt>
            <dd className="text-fg">{formatDeg(stats.meanAltitudeDeg)}</dd>
          </div>
        </dl>
      )}

      <HorizonEditor
        points={points}
        selectedIndex={selectedIndex}
        onSelect={setSelectedIndex}
        onAdd={addPoint}
        onMove={movePoint}
        onDelete={deletePoint}
      />

      {notice !== null && (
        <p role="status" className="mt-2 text-sm text-fg-muted">
          {notice}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          variant="secondary"
          onClick={() => {
            addPoint(widestGapMidpointDeg(points), 0);
          }}
        >
          Add point
        </Button>
        <Button
          variant="danger"
          onClick={() => {
            if (selectedIndex !== null) deletePoint(selectedIndex);
          }}
          disabled={selectedIndex === null}
        >
          Delete selected point
        </Button>
      </div>

      <div className="mt-3">
        <HorizonPointsTable
          points={points}
          selectedIndex={selectedIndex}
          onSelect={setSelectedIndex}
          onChangeField={editPointField}
          onDelete={deletePoint}
        />
      </div>

      {points.length < 2 && (
        <p className="mt-2 text-xs text-fg-muted">A horizon profile needs at least two points to save.</p>
      )}

      {setHorizon.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {String(setHorizon.error)}
        </p>
      )}
      {clearHorizon.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {String(clearHorizon.error)}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          onClick={handleSave}
          disabled={!dirty || points.length < 2 || setHorizon.isPending}
        >
          {setHorizon.isPending ? "Saving…" : "Save horizon"}
        </Button>
        {dirty && (
          <Button variant="secondary" onClick={handleDiscard}>
            Discard changes
          </Button>
        )}
        {savedPoints !== null && (
          <Button variant="danger" onClick={handleClear} disabled={clearHorizon.isPending}>
            {clearHorizon.isPending ? "Clearing…" : "Clear profile"}
          </Button>
        )}
      </div>
    </Card>
  );
}

/**
 * The direct-manipulation surface: azimuth along x (0–360°, N/E/S/W
 * labelled), altitude along y. A tap on empty space adds a point; a point is
 * a real `<button>` — draggable via pointer events, movable with arrow keys,
 * deletable with Delete/Backspace — sized to a 44px hit target regardless of
 * how far the plot itself is scaled down (frontend-react.md: touch and
 * gloves), positioned by percentage so it tracks the plot at any width.
 */
function HorizonEditor({
  points,
  selectedIndex,
  onSelect,
  onAdd,
  onMove,
  onDelete,
}: {
  points: HorizonPointRequest[];
  selectedIndex: number | null;
  onSelect: (index: number | null) => void;
  onAdd: (azimuthDeg: number, altitudeDeg: number) => void;
  onMove: (index: number, azimuthDeg: number, altitudeDeg: number) => void;
  onDelete: (index: number) => void;
}) {
  const plotRef = useRef<HTMLDivElement>(null);

  function coordsFromPoint(clientX: number, clientY: number): { azimuthDeg: number; altitudeDeg: number } | null {
    const rect = plotRef.current?.getBoundingClientRect();
    if (rect === undefined || rect.width === 0 || rect.height === 0) return null;
    const fx = (clientX - rect.left) / rect.width;
    const fy = (clientY - rect.top) / rect.height;
    return {
      azimuthDeg: wrapAzimuthDeg(fx * 360),
      altitudeDeg: clampAltitudeDeg(ALTITUDE_MAX_DEG - fy * (ALTITUDE_MAX_DEG - ALTITUDE_MIN_DEG)),
    };
  }

  function handleBackgroundClick(event: ReactMouseEvent<HTMLDivElement>) {
    const coords = coordsFromPoint(event.clientX, event.clientY);
    if (coords === null) return;
    onAdd(coords.azimuthDeg, coords.altitudeDeg);
  }

  function handlePointerDown(index: number, event: ReactPointerEvent<HTMLButtonElement>) {
    event.stopPropagation();
    onSelect(index);
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(index: number, event: ReactPointerEvent<HTMLButtonElement>) {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
    const coords = coordsFromPoint(event.clientX, event.clientY);
    if (coords === null) return;
    onMove(index, coords.azimuthDeg, coords.altitudeDeg);
  }

  function handleKeyDown(index: number, event: ReactKeyboardEvent<HTMLButtonElement>) {
    const point = points[index];
    if (point === undefined) return;
    const step = event.shiftKey ? 5 : 1;
    switch (event.key) {
      case "ArrowLeft":
        event.preventDefault();
        onMove(index, point.azimuth_deg - step, point.altitude_deg);
        return;
      case "ArrowRight":
        event.preventDefault();
        onMove(index, point.azimuth_deg + step, point.altitude_deg);
        return;
      case "ArrowUp":
        event.preventDefault();
        onMove(index, point.azimuth_deg, point.altitude_deg + step);
        return;
      case "ArrowDown":
        event.preventDefault();
        onMove(index, point.azimuth_deg, point.altitude_deg - step);
        return;
      case "Delete":
      case "Backspace":
        event.preventDefault();
        onDelete(index);
        return;
      default:
        return;
    }
  }

  return (
    // The background tap-to-add is a pointer/touch shortcut, not the only way
    // to add a point: the "Add point" button and the numeric table below are
    // both fully keyboard- and screen-reader-operable (and more precise), so
    // this div is deliberately a passive drawing surface rather than a fake
    // button.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/click-events-have-key-events
    <div
      ref={plotRef}
      role="group"
      aria-label="Horizon profile plot. Tap empty space to add a point; drag a point to move it."
      className="relative h-64 w-full touch-none rounded-md border border-border-strong bg-surface-raised select-none sm:h-80"
      onClick={handleBackgroundClick}
    >
      <HorizonGridlines />
      <HorizonMaskShape points={points} />
      {points.map((point, index) => {
        const leftPct = (wrapAzimuthDeg(point.azimuth_deg) / 360) * 100;
        const topPct =
          ((ALTITUDE_MAX_DEG - clampAltitudeDeg(point.altitude_deg)) / (ALTITUDE_MAX_DEG - ALTITUDE_MIN_DEG)) *
          100;
        const isSelected = index === selectedIndex;
        return (
          <button
            key={index}
            type="button"
            aria-label={`Horizon point, azimuth ${formatDeg(point.azimuth_deg)}, altitude ${formatDeg(
              point.altitude_deg,
            )}${isSelected ? ", selected" : ""}. Arrow keys move it; Delete removes it.`}
            className={`absolute flex h-11 w-11 -translate-x-1/2 -translate-y-1/2 touch-none items-center justify-center rounded-full ${
              isSelected ? "z-10" : ""
            }`}
            style={{ left: `${String(leftPct)}%`, top: `${String(topPct)}%` }}
            onPointerDown={(e) => {
              handlePointerDown(index, e);
            }}
            onPointerMove={(e) => {
              handlePointerMove(index, e);
            }}
            onClick={(e) => {
              e.stopPropagation();
            }}
            onKeyDown={(e) => {
              handleKeyDown(index, e);
            }}
          >
            <span
              aria-hidden="true"
              className="block h-3.5 w-3.5 rounded-full"
              style={{
                backgroundColor: isSelected ? "var(--ssc-accent)" : "var(--ssc-fg)",
                boxShadow: isSelected ? "0 0 0 5px var(--ssc-accent-muted)" : "none",
              }}
            />
          </button>
        );
      })}
    </div>
  );
}

const ALTITUDE_TICKS_DEG = [-10, 0, 30, 60, 90];
const AZIMUTH_TICKS: readonly [number, string][] = [
  [0, "N"],
  [90, "E"],
  [180, "S"],
  [270, "W"],
  [360, "N"],
];

function HorizonGridlines() {
  return (
    <div className="pointer-events-none absolute inset-0" aria-hidden="true">
      {ALTITUDE_TICKS_DEG.map((alt) => {
        const top = ((ALTITUDE_MAX_DEG - alt) / (ALTITUDE_MAX_DEG - ALTITUDE_MIN_DEG)) * 100;
        return (
          <div
            key={alt}
            className={`absolute right-0 left-0 border-t ${alt === 0 ? "border-border-strong" : "border-border"}`}
            style={{ top: `${String(top)}%` }}
          />
        );
      })}
      {AZIMUTH_TICKS.map(([az]) => (
        <div
          key={az}
          className="absolute top-0 bottom-0 border-l border-border"
          style={{ left: `${String((az / 360) * 100)}%` }}
        />
      ))}
      {ALTITUDE_TICKS_DEG.map((alt) => (
        <span
          key={alt}
          className="absolute left-1 text-xs text-fg-muted"
          style={{
            top: `${String(((ALTITUDE_MAX_DEG - alt) / (ALTITUDE_MAX_DEG - ALTITUDE_MIN_DEG)) * 100)}%`,
            transform: "translateY(-50%)",
          }}
        >
          {alt}°
        </span>
      ))}
      {AZIMUTH_TICKS.map(([az, label]) => (
        <span
          key={az}
          className="absolute bottom-1 text-xs text-fg-muted"
          style={{
            left: `${String((az / 360) * 100)}%`,
            transform: az === 0 ? "translateX(0%)" : az === 360 ? "translateX(-100%)" : "translateX(-50%)",
          }}
        >
          {label}
        </span>
      ))}
    </div>
  );
}

/**
 * The mask line itself, plus the segment closing the circle from the last
 * point back to the first — drawn faint and dashed at both edges of the
 * plot, because that segment runs *through north* (ssc/astro/horizon.py:
 * "the profile is cyclic... the short way across the seam, not backwards
 * through south") and is otherwise the one connection a linear azimuth axis
 * cannot show directly.
 */
function HorizonMaskShape({ points }: { points: HorizonPointRequest[] }) {
  if (points.length < 2) return null;
  const sorted = sortByAzimuth(points);
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  // Unreachable given the length guard above, but `noUncheckedIndexedAccess`
  // cannot see that — an explicit check reads honestly rather than asserting
  // it away.
  if (first === undefined || last === undefined) return null;
  const toY = (alt: number) =>
    (((ALTITUDE_MAX_DEG - clampAltitudeDeg(alt)) / (ALTITUDE_MAX_DEG - ALTITUDE_MIN_DEG)) * 100).toFixed(2);
  const toPoint = (azimuthDeg: number, altitudeDeg: number) => `${String(azimuthDeg)},${toY(altitudeDeg)}`;
  const main = sorted.map((p) => toPoint(wrapAzimuthDeg(p.azimuth_deg), p.altitude_deg)).join(" ");
  const leftGhost = `${toPoint(wrapAzimuthDeg(last.azimuth_deg) - 360, last.altitude_deg)} ${toPoint(
    wrapAzimuthDeg(first.azimuth_deg),
    first.altitude_deg,
  )}`;
  const rightGhost = `${toPoint(wrapAzimuthDeg(last.azimuth_deg), last.altitude_deg)} ${toPoint(
    wrapAzimuthDeg(first.azimuth_deg) + 360,
    first.altitude_deg,
  )}`;
  return (
    <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 360 100" preserveAspectRatio="none" aria-hidden="true">
      <polyline
        points={leftGhost}
        style={{ stroke: "var(--ssc-fg-muted)", fill: "none", opacity: 0.6 }}
        strokeDasharray="4 3"
        vectorEffect="non-scaling-stroke"
      />
      <polyline
        points={rightGhost}
        style={{ stroke: "var(--ssc-fg-muted)", fill: "none", opacity: 0.6 }}
        strokeDasharray="4 3"
        vectorEffect="non-scaling-stroke"
      />
      <polyline points={main} style={{ stroke: "var(--ssc-accent)", fill: "none" }} strokeWidth={2} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** The precision path (frontend-react.md: "numeric entry available for
 * anyone who wants precision") and the reachable way to delete a point —
 * exactly as prominent as adding one, a dedicated 44px button per row rather
 * than a gesture someone has to discover. */
function HorizonPointsTable({
  points,
  selectedIndex,
  onSelect,
  onChangeField,
  onDelete,
}: {
  points: HorizonPointRequest[];
  selectedIndex: number | null;
  onSelect: (index: number) => void;
  onChangeField: (index: number, field: "azimuth_deg" | "altitude_deg", raw: string) => void;
  onDelete: (index: number) => void;
}) {
  const ordered = points
    .map((point, index) => ({ point, index }))
    .sort((a, b) => a.point.azimuth_deg - b.point.azimuth_deg);

  if (ordered.length === 0) {
    return <p className="text-sm text-fg-muted">No points yet — tap the plot above to add one.</p>;
  }

  return (
    <div className="max-h-64 overflow-y-auto rounded-md border border-border">
      <div className="grid grid-cols-[1fr_1fr_auto] gap-2 border-b border-border bg-surface px-2 py-1.5 text-xs font-medium text-fg-muted">
        <span>Azimuth</span>
        <span>Altitude</span>
        <span className="sr-only">Delete</span>
      </div>
      <ul>
        {ordered.map(({ point, index }) => (
          <li
            key={index}
            className={`grid grid-cols-[1fr_1fr_auto] items-center gap-2 border-b border-border px-2 py-1.5 last:border-b-0 ${
              index === selectedIndex ? "bg-accent-muted" : ""
            }`}
          >
            <div className="flex items-center gap-1 text-sm">
              <DraftNumberField
                label={`Azimuth, degrees, point ${String(index + 1)}`}
                labelClassName="sr-only"
                inputMode="decimal"
                step={DEGREE_STEP}
                min={0}
                max={359.9}
                value={point.azimuth_deg}
                onFocus={() => { onSelect(index); }}
                onValueChange={(value) => { if (value !== null) onChangeField(index, "azimuth_deg", String(value)); }}
                className="w-full px-2"
              />
              <span aria-hidden="true" className="text-fg-muted">
                °
              </span>
            </div>
            <div className="flex items-center gap-1 text-sm">
              <DraftNumberField
                label={`Altitude, degrees, point ${String(index + 1)}`}
                labelClassName="sr-only"
                inputMode="decimal"
                step={DEGREE_STEP}
                min={ALTITUDE_MIN_DEG}
                max={ALTITUDE_MAX_DEG}
                value={point.altitude_deg}
                onFocus={() => { onSelect(index); }}
                onValueChange={(value) => { if (value !== null) onChangeField(index, "altitude_deg", String(value)); }}
                className="w-full px-2"
              />
              <span aria-hidden="true" className="text-fg-muted">
                °
              </span>
            </div>
            <Button
              variant="danger"
              onClick={() => {
                onDelete(index);
              }}
              aria-label={`Delete point at azimuth ${formatDeg(point.azimuth_deg)}`}
            >
              Delete
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
