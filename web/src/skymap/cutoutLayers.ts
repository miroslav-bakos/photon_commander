// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The "Target field" group's state machine: one row per cutout source
 * (`ssc.skymap.cutouts.CUTOUT_REGISTRY`, via `GET /api/skymap/surveys`'s
 * `cutout_sources`), resolved against the snapshot's live job list
 * (`skymap.cutouts`, the same one a prefetch job renders progress from) —
 * never a second copy of a job's status kept in component state.
 *
 * Pure and React-free so the four states `LayersPanel.tsx` renders
 * (not-fetched / fetching / ready / unavailable) are testable without
 * mounting anything (skymap-viz.md: "LayersPanel state machine per
 * source").
 *
 * `findFieldCutoutIdBySource` (2026-09-10) lives here too: it is the other
 * half of the same state machine — which live job counts as "the one for
 * this field" per source, the input `resolveCutoutRows` needs for its own
 * `currentCutoutIdBySource` parameter. It used to be computed in
 * `SkyMap.tsx` from the REST point-lookup query alone, which only ever saw
 * a job the *server* had already scoped to the right point at the moment
 * that one query fired, and had no way to notice a job the server finished
 * autofetching afterwards — see its own doc comment for the live bug this
 * caused (reported: IC 1805, third resolve in one session, complete with
 * real bytes on disk, showing nothing).
 */
import type { CutoutJobStatus, CutoutSourceModel, CutoutState } from "../api/skymap";

/** Float-rounding slack only — `size_deg` is stored/compared as a plain
 * float, not a reason to reject a cutout that is a hair under the exact
 * target it was requested at. */
export const CUTOUT_SIZE_MATCH_TOLERANCE_DEG = 0.01;

export interface CutoutTarget {
  raDeg: number;
  decDeg: number;
}

export type CutoutRowState =
  /** No resolved Planning target — nothing to fetch a field for yet. */
  | { kind: "no-target" }
  | { kind: "not-fetched"; offline: boolean; sizeDeg: number }
  | { kind: "fetching"; status: CutoutJobStatus; bytes: number }
  | { kind: "ready"; cutoutId: string }
  | {
      kind: "unavailable";
      /** Already a sentence — an off-footprint message verbatim, or (a
       * `FAILED` job) `ssc.skymap.cutouts._humanize_upstream_error`'s
       * output. Never the raw upstream detail; see `detail`. */
      reason: string;
      /** The raw upstream text behind `reason` (`CutoutState.error_detail`),
       * for a title/tooltip — `null` for an off-footprint result, which has
       * no separate raw detail. */
      detail: string | null;
      /** `true` for a `FAILED`/`CANCELLED`/`INTERRUPTED` job — an
       * unexpected failure worth a retry. `false` for `UNAVAILABLE` — the
       * archive itself confirmed there is nothing here, and retrying would
       * just ask the same question again. */
      retryable: boolean;
      /** What a retry (or, for `UNAVAILABLE`, a fresh look later) would
       * request — the job's own size, not the caller's current default. */
      sizeDeg: number;
    };

export interface CutoutSourceRow {
  sourceId: string;
  label: string;
  credit: { label: string; url: string };
  /** Legacy Surveys: on only when the field is confirmed in its footprint,
   * never an error when it is not — the row still renders, `unavailable`
   * reads as "not in this survey's footprint", not a failure. */
  optional: boolean;
  state: CutoutRowState;
}

export interface ResolveCutoutRowsParams {
  target: CutoutTarget | null;
  sources: readonly CutoutSourceModel[];
  /** Which cutout job (if any) is "the one for this field" per source —
   * owned by the caller (`SkyMap.tsx`): seeded from the point lookup on a
   * target change, updated to a fresh id once a fetch is requested, reset
   * when the target changes again. Not derived here, because "the field"
   * changing is a caller-visible event (a new `useEffect` run), not
   * something this pure function can detect from the job list alone. */
  currentCutoutIdBySource: ReadonlyMap<string, string>;
  /** `skymap.cutouts` from the snapshot — every job, live or historical. */
  liveCutouts: readonly CutoutState[];
  /** `skymap.upstream_reachable`; `null` before any attempt (never treated
   * as "offline" — architecture policy invariant 7: unknown is not zero). */
  upstreamReachable: boolean | null;
  /** The size a fresh fetch would request for a given source: the
   * FOV-fitted size when one is known, else the 1.5 deg default — resolved
   * by the caller since it is a framing concern, not a cutout-registry
   * one. Per-source, not one flat number, because unWISE deliberately
   * requests more than the FOV fit (framing context beyond the exact
   * frame — reported live), while every other source still requests
   * exactly the frame. */
  defaultSizeDeg: (sourceId: string) => number;
}

/**
 * `resolveCutoutRows`'s `liveCutouts` input is meant to be "every job, live
 * or historical", but the snapshot's own `skymap.cutouts` is capped at the
 * last 50 entries server-side (snapshot-size, `ssc/skymap/service.py`'s
 * publisher) — a genuinely complete job can age out of that list once 50
 * newer jobs exist, and `resolveCutoutRows` then sees no job at all and
 * reports `not-fetched` forever for a field that really was fetched
 * (reported live: SDSS for M31 stuck on "Fetch (3.00°)" after a real,
 * successful fetch). The point-lookup query (`useCachedCutoutsQuery`, `GET
 * /api/skymap/cutouts?ra_deg&dec_deg&radius_deg`) queries the DB directly
 * and is not capped, so it still has that job's real status; this merges
 * the two into the one list `resolveCutoutRows` should have been given.
 *
 * On an id collision the snapshot's own entry wins: it is the only source
 * for a still-running job's live byte count, and for a completed job it is
 * never staler than the point lookup's copy of the same row. */
export function mergeLiveCutouts(
  snapshotCutouts: readonly CutoutState[],
  pointLookupCutouts: readonly CutoutState[],
): CutoutState[] {
  const byId = new Map<string, CutoutState>();
  for (const cutout of pointLookupCutouts) byId.set(cutout.id, cutout);
  for (const cutout of snapshotCutouts) byId.set(cutout.id, cutout);
  return [...byId.values()];
}

/** Great-circle separation between two sky points, in degrees — the cosine
 * rule (haversine form), correct at any declination including near the
 * poles, and handling RA wrap at 0h/24h for free (it is a function of
 * `cos(ra1 - ra2)`, which is periodic). This is not a domain computation —
 * parallactic angle, keep-region geometry and twilight times stay
 * `astro/`'s, per this module's own ownership boundary — it is the plain
 * "how far apart are these two points" check `findFieldCutoutIdBySource`
 * needs to answer "is this job's own ra/dec the same field as the current
 * target", nothing more. */
function angularSeparationDeg(a: CutoutTarget, b: CutoutTarget): number {
  const toRad = (deg: number) => (deg * Math.PI) / 180;
  const ra1 = toRad(a.raDeg);
  const dec1 = toRad(a.decDeg);
  const ra2 = toRad(b.raDeg);
  const dec2 = toRad(b.decDeg);
  const cosSeparation = Math.sin(dec1) * Math.sin(dec2) + Math.cos(dec1) * Math.cos(dec2) * Math.cos(ra1 - ra2);
  return (Math.acos(Math.min(1, Math.max(-1, cosSeparation))) * 180) / Math.PI;
}

export interface FindFieldCutoutIdsParams {
  target: CutoutTarget;
  sourceIds: readonly string[];
  /** `skymap.cutouts` merged with the point lookup (`mergeLiveCutouts`) —
   * the live, continuously-pushed job list, not a one-shot REST snapshot. */
  liveCutouts: readonly CutoutState[];
  /** A job this session itself just requested for the current field, per
   * source — trusted ahead of any geo-matched candidate, since it is known
   * to be for this exact field the moment the request succeeds, before the
   * round trip even lands it in `liveCutouts`. `SkyMap.tsx` resets this
   * whenever the target itself changes. */
  sessionCutoutIdBySource: ReadonlyMap<string, string>;
  defaultSizeDeg: (sourceId: string) => number;
  /** How close a job's own `ra_deg`/`dec_deg` must be to `target` to count
   * as "the same field" — a small, fixed tolerance, not the field's own
   * footprint: this answers "is this the same point", not "does this
   * fetched field cover that point" (a completed cutout is typically much
   * larger than the tolerance and still the right one). */
  matchRadiusDeg: number;
}

/**
 * Which live cutout job (if any) counts as "the one for this field", per
 * source. Matches by the job's own `ra_deg`/`dec_deg` against `target`
 * directly, rather than trusting a caller-side query to already be scoped
 * to the right point — the bug this replaced (`SkyMap.tsx`, before
 * 2026-09-10): the REST point-lookup query fires once per resolved target
 * and is never invalidated, so a source the *server* autofetched (no
 * explicit fetch from this session, so no session override either) stayed
 * invisible to the panel forever once that one query had already run
 * before the server's own job finished — nothing re-ran it to notice.
 * Matching directly against the live job list, which is pushed
 * continuously through the snapshot and does not depend on any query
 * re-firing, removes that staleness window entirely: a job that goes
 * `complete` shows up the next time the snapshot carries it, full stop.
 */
export function findFieldCutoutIdBySource(params: FindFieldCutoutIdsParams): Map<string, string> {
  const { target, sourceIds, liveCutouts, sessionCutoutIdBySource, defaultSizeDeg, matchRadiusDeg } = params;
  const liveById = new Map(liveCutouts.map((job) => [job.id, job] as const));
  const result = new Map<string, string>();

  for (const sourceId of sourceIds) {
    const sessionId = sessionCutoutIdBySource.get(sourceId);
    if (sessionId !== undefined && liveById.has(sessionId)) {
      result.set(sourceId, sessionId);
      continue;
    }

    const wanted = defaultSizeDeg(sourceId);
    const candidates = liveCutouts.filter(
      (job) =>
        job.source === sourceId &&
        job.size_deg >= wanted - CUTOUT_SIZE_MATCH_TOLERANCE_DEG &&
        angularSeparationDeg(target, { raDeg: job.ra_deg, decDeg: job.dec_deg }) <= matchRadiusDeg,
    );
    if (candidates.length === 0) continue;

    // Prefer the smallest complete job that still meets the size floor —
    // the tightest fetch actually for this field, not an unrelated wider
    // one that happens to also cover it (`SkyMap.tsx`'s original reasoning
    // for this tradeoff, carried over unchanged). Failing that, the most
    // recently created candidate of any status, so an in-flight or failed
    // fetch still surfaces rather than losing to a stale complete job from
    // long before.
    const complete = candidates.filter((job) => job.status === "complete");
    const best =
      complete.length > 0
        ? complete.reduce((a, b) => (b.size_deg < a.size_deg ? b : a))
        : candidates.reduce((a, b) => (b.created_at > a.created_at ? b : a));
    result.set(sourceId, best.id);
  }

  return result;
}

export function resolveCutoutRows(params: ResolveCutoutRowsParams): CutoutSourceRow[] {
  const { target, sources, currentCutoutIdBySource, liveCutouts, upstreamReachable, defaultSizeDeg } = params;
  const jobsById = new Map(liveCutouts.map((job) => [job.id, job] as const));

  return sources.map((source): CutoutSourceRow => {
    const credit = { label: source.credit.label, url: source.credit.url };
    const row = { sourceId: source.id, label: source.label, credit, optional: source.optional };

    if (target === null) {
      return { ...row, state: { kind: "no-target" } };
    }

    const cutoutId = currentCutoutIdBySource.get(source.id);
    const job = cutoutId === undefined ? undefined : jobsById.get(cutoutId);
    if (job === undefined) {
      return {
        ...row,
        state: { kind: "not-fetched", offline: upstreamReachable === false, sizeDeg: defaultSizeDeg(source.id) },
      };
    }

    switch (job.status) {
      case "queued":
      case "running":
        return { ...row, state: { kind: "fetching", status: job.status, bytes: job.bytes } };
      case "complete":
        return { ...row, state: { kind: "ready", cutoutId: job.id } };
      case "unavailable":
        return {
          ...row,
          state: {
            kind: "unavailable",
            reason: job.error ?? `${source.label} does not cover this field`,
            detail: job.error_detail,
            retryable: false,
            sizeDeg: job.size_deg,
          },
        };
      case "failed":
      case "cancelled":
      case "interrupted":
        return {
          ...row,
          state: {
            kind: "unavailable",
            reason: job.error ?? `${source.label} fetch ${job.status}`,
            detail: job.error_detail,
            retryable: true,
            sizeDeg: job.size_deg,
          },
        };
    }
  });
}

/** What the bottom-right `CutoutFetchIndicator` (`CutoutFetchIndicator.tsx`)
 * shows: one row picked out of the whole "Target field" group to speak for
 * it, or `null` when there is nothing worth an ambient signal for. */
export type CutoutFetchIndicatorSummary =
  | { kind: "fetching"; label: string; status: CutoutJobStatus; extraCount: number }
  | { kind: "failed"; label: string; reason: string; detail: string | null; extraCount: number };

/**
 * Picks the one row `CutoutFetchIndicator` renders, out of every source's
 * resolved state. Two rules, in priority order:
 *
 *  1. Any source currently `fetching` wins outright, over a failure from
 *     another source — it is work happening *right now*, which is what the
 *     owner's request ("so user knows that it's fetching") is actually
 *     about. The first one in `rows` order (the cutout-source registry's own
 *     order — stable, not fetch-start order) stands for the group; the rest
 *     fold into `extraCount` rather than stacking one line per source.
 *  2. Failing that, any source whose fetch actually *failed*
 *     (`retryable: true` — `failed`/`cancelled`/`interrupted`, matching
 *     `CutoutState.status`) is shown so a failure never just vanishes
 *     (architecture policy invariant 7). `retryable: false` ("unavailable" because the
 *     field is outside the survey's footprint, e.g. Legacy Surveys off the
 *     imaged strip) is deliberately excluded: that is an expected outcome
 *     already explained inline in the "Target field" group, not the
 *     "something went wrong" this indicator exists to surface.
 *
 * `ready`/`not-fetched`/`no-target` rows never produce a line here — the
 * panel already shows every row; this is a one-line summary of activity and
 * failure, not a duplicate of the whole group.
 */
export function summarizeCutoutFetchStatus(rows: readonly CutoutSourceRow[]): CutoutFetchIndicatorSummary | null {
  const fetching = rows.filter((r): r is CutoutSourceRow & { state: Extract<CutoutRowState, { kind: "fetching" }> } =>
    r.state.kind === "fetching",
  );
  if (fetching.length > 0) {
    const [first, ...rest] = fetching;
    if (first === undefined) throw new Error("unreachable: fetching.length > 0 just checked");
    return { kind: "fetching", label: first.label, status: first.state.status, extraCount: rest.length };
  }

  const failed = rows.filter(
    (r): r is CutoutSourceRow & { state: Extract<CutoutRowState, { kind: "unavailable" }> } =>
      r.state.kind === "unavailable" && r.state.retryable,
  );
  if (failed.length > 0) {
    const [first, ...rest] = failed;
    if (first === undefined) throw new Error("unreachable: failed.length > 0 just checked");
    return {
      kind: "failed",
      label: first.label,
      reason: first.state.reason,
      detail: first.state.detail,
      extraCount: rest.length,
    };
  }

  return null;
}
