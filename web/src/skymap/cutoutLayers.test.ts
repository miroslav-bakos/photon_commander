// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { CutoutSourceModel, CutoutState } from "../api/skymap";
import {
  findFieldCutoutIdBySource,
  mergeLiveCutouts,
  resolveCutoutRows,
  summarizeCutoutFetchStatus,
  type CutoutSourceRow,
} from "./cutoutLayers";

/** The one row a single-source `resolveCutoutRows` call returns — asserts
 * the length so a regression that drops a row fails loudly, then hands back
 * a non-optional type without a `!` (forbidden by lint here). */
function only(rows: CutoutSourceRow[]): CutoutSourceRow {
  expect(rows).toHaveLength(1);
  const [row] = rows;
  if (row === undefined) throw new Error("unreachable: toHaveLength(1) just passed");
  return row;
}

function source(overrides: Partial<CutoutSourceModel> = {}): CutoutSourceModel {
  return {
    id: "wise",
    label: "AllWISE (infrared)",
    credit: { label: "AllWISE: NASA/JPL-Caltech/UCLA · NASA/IPAC IRSA", url: "https://irsa.ipac.caltech.edu/" },
    bands: ["W1", "W2", "W3", "W4"],
    default_bands: ["W3", "W1"],
    optional: false,
    max_pixels: 3000,
    ...overrides,
  };
}

function job(overrides: Partial<CutoutState> = {}): CutoutState {
  return {
    id: "job-1",
    source: "wise",
    ra_deg: 283.396,
    dec_deg: -1.294,
    size_deg: 1.5,
    status: "running",
    bytes: 0,
    error: null,
    error_detail: null,
    created_at: "2026-09-07T20:00:00Z",
    updated_at: "2026-09-07T20:00:00Z",
    ...overrides,
  };
}

const target = { raDeg: 283.396, decDeg: -1.294 };

describe("resolveCutoutRows", () => {
  it("reports no-target when nothing is resolved, for every source", () => {
    const rows = resolveCutoutRows({
      target: null,
      sources: [source(), source({ id: "sdss", label: "SDSS (color)" })],
      currentCutoutIdBySource: new Map(),
      liveCutouts: [],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.state.kind === "no-target")).toBe(true);
  });

  it("is not-fetched, and says offline, when upstream is unreachable and nothing is cached", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map(),
      liveCutouts: [],
      upstreamReachable: false,
      defaultSizeDeg: () => 2.1,
    });
    expect(only(rows).state).toEqual({ kind: "not-fetched", offline: true, sizeDeg: 2.1 });
  });

  it("is not-fetched, not offline, when upstream is reachable", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map(),
      liveCutouts: [],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).state).toEqual({ kind: "not-fetched", offline: false, sizeDeg: 1.5 });
  });

  it("asks defaultSizeDeg per source, not once for every row", () => {
    // unWISE deliberately requests more than the frame for framing context
    // (reported live) — resolveCutoutRows has to consult defaultSizeDeg
    // per source, not compute one flat number for the whole call, or the
    // "not-fetched" row's own promised size would be wrong for whichever
    // source is not the first one.
    const rows = resolveCutoutRows({
      target,
      sources: [source({ id: "unwise", label: "unWISE (infrared)" }), source({ id: "sdss", label: "SDSS (color)" })],
      currentCutoutIdBySource: new Map(),
      liveCutouts: [],
      upstreamReachable: true,
      defaultSizeDeg: (sourceId) => (sourceId === "unwise" ? 4.5 : 1.5),
    });
    expect(rows).toHaveLength(2);
    expect(rows[0]?.state).toEqual({ kind: "not-fetched", offline: false, sizeDeg: 4.5 });
    expect(rows[1]?.state).toEqual({ kind: "not-fetched", offline: false, sizeDeg: 1.5 });
  });

  it("treats an unknown upstream reachability as not offline (unknown is not zero)", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map(),
      liveCutouts: [],
      upstreamReachable: null,
      defaultSizeDeg: () => 1.5,
    });
    expect((only(rows).state as { offline: boolean }).offline).toBe(false);
  });

  it("is fetching while the tracked job is queued or running, with its bytes so far", () => {
    for (const status of ["queued", "running"] as const) {
      const rows = resolveCutoutRows({
        target,
        sources: [source()],
        currentCutoutIdBySource: new Map([["wise", "job-1"]]),
        liveCutouts: [job({ status, bytes: 4096 })],
        upstreamReachable: true,
        defaultSizeDeg: () => 1.5,
      });
      expect(only(rows).state).toEqual({ kind: "fetching", status, bytes: 4096 });
    }
  });

  it("is ready with the cutout id once the tracked job completes", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map([["wise", "job-1"]]),
      liveCutouts: [job({ status: "complete" })],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).state).toEqual({ kind: "ready", cutoutId: "job-1" });
  });

  it("is unavailable, not retryable, with the manifest's own reason when the archive confirms nothing is here", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source({ id: "legacy-surveys", label: "Legacy Surveys", optional: true })],
      currentCutoutIdBySource: new Map([["legacy-surveys", "job-2"]]),
      liveCutouts: [
        job({
          id: "job-2",
          source: "legacy-surveys",
          status: "unavailable",
          error: "off footprint",
          size_deg: 2.0,
        }),
      ],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).state).toEqual({
      kind: "unavailable",
      reason: "off footprint",
      detail: null,
      retryable: false,
      sizeDeg: 2.0,
    });
  });

  it("is unavailable with a fallback reason when the archive gives none", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map([["wise", "job-1"]]),
      liveCutouts: [job({ status: "unavailable", error: null })],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).state).toEqual({
      kind: "unavailable",
      reason: "AllWISE (infrared) does not cover this field",
      detail: null,
      retryable: false,
      sizeDeg: 1.5,
    });
  });

  it("is unavailable and retryable, verbatim reason plus its raw detail, for a failed/cancelled/interrupted job", () => {
    for (const status of ["failed", "cancelled", "interrupted"] as const) {
      const rows = resolveCutoutRows({
        target,
        sources: [source()],
        currentCutoutIdBySource: new Map([["wise", "job-1"]]),
        liveCutouts: [
          job({
            status,
            error: "AllWISE (infrared) took too long to respond — try again later.",
            error_detail: "irsa.ipac.caltech.edu did not answer in time: ...",
          }),
        ],
        upstreamReachable: true,
        defaultSizeDeg: () => 1.5,
      });
      expect(only(rows).state).toEqual({
        kind: "unavailable",
        reason: "AllWISE (infrared) took too long to respond — try again later.",
        detail: "irsa.ipac.caltech.edu did not answer in time: ...",
        retryable: true,
        sizeDeg: 1.5,
      });
    }
  });

  it("falls back to not-fetched if the tracked id is stale (no longer in the live job list)", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map([["wise", "job-deleted"]]),
      liveCutouts: [],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).state).toEqual({ kind: "not-fetched", offline: false, sizeDeg: 1.5 });
  });

  it("resolves to ready when the completed job is only visible through the point lookup, not the (capped) snapshot list — the SkyMap.tsx regression: SDSS stuck on 'Fetch' after a real, successful fetch", () => {
    // The snapshot's `skymap.cutouts` (capped at 50 server-side) has aged
    // this job out entirely: it is not in `liveCutouts` at all, only in
    // what `mergeLiveCutouts` folds in from the uncapped point lookup.
    const agedOutJob = job({ id: "job-aged-out", status: "complete" });
    const merged = mergeLiveCutouts([], [agedOutJob]);
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map([["wise", "job-aged-out"]]),
      liveCutouts: merged,
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).state).toEqual({ kind: "ready", cutoutId: "job-aged-out" });
  });

  it("carries the credit and optional flag through untouched, per source", () => {
    const rows = resolveCutoutRows({
      target: null,
      sources: [source({ id: "legacy-surveys", label: "Legacy Surveys", optional: true, credit: { label: "Legacy Surveys / D. Lang (Perimeter Institute)", url: "https://www.legacysurvey.org/" } })],
      currentCutoutIdBySource: new Map(),
      liveCutouts: [],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).credit).toEqual({
      label: "Legacy Surveys / D. Lang (Perimeter Institute)",
      url: "https://www.legacysurvey.org/",
    });
    expect(only(rows).optional).toBe(true);
  });
});

describe("mergeLiveCutouts", () => {
  it("includes a job present only in the point-lookup result, absent from the (capped) snapshot list", () => {
    const pointLookupOnly = job({ id: "job-aged-out", status: "complete" });
    const merged = mergeLiveCutouts([], [pointLookupOnly]);
    expect(merged).toEqual([pointLookupOnly]);
  });

  it("includes a job present only in the snapshot (e.g. still running, not yet in any point lookup)", () => {
    const snapshotOnly = job({ id: "job-running", status: "running", bytes: 4096 });
    const merged = mergeLiveCutouts([snapshotOnly], []);
    expect(merged).toEqual([snapshotOnly]);
  });

  it("prefers the snapshot's own entry on an id collision — it has the live byte count and is never staler", () => {
    const stalePointLookup = job({ id: "job-1", status: "running", bytes: 1024 });
    const freshSnapshot = job({ id: "job-1", status: "running", bytes: 8192 });
    const merged = mergeLiveCutouts([freshSnapshot], [stalePointLookup]);
    expect(merged).toEqual([freshSnapshot]);
  });

  it("de-duplicates by id rather than concatenating both lists", () => {
    const shared = job({ id: "job-1" });
    const merged = mergeLiveCutouts([shared], [shared]);
    expect(merged).toHaveLength(1);
  });
});

describe("findFieldCutoutIdBySource", () => {
  const ic1805 = { raDeg: 38.173, decDeg: 61.457 };

  it("finds a job the server autofetched, live in the snapshot, with no explicit request from this session", () => {
    const autofetched = job({
      id: "job-autofetch-1",
      source: "unwise",
      ra_deg: ic1805.raDeg,
      dec_deg: ic1805.decDeg,
      size_deg: 1.5,
      status: "complete",
      bytes: 1_368_663,
    });

    // The bug this function replaced (`SkyMap.tsx`, before 2026-09-10): the
    // old lookup only ever learned of a job through (a) this session's own
    // explicit fetch, or (b) whatever the one-shot REST point-lookup query
    // happened to return the moment it fired. A job the server autofetched
    // afterwards, seen only through the live snapshot, satisfied neither —
    // reproduced directly: given only a session map, with no way at all to
    // consult the live job list, the old shape of this lookup finds
    // nothing for a real, complete job.
    const oldLookupHadNoWayToSeeLiveJobs = (sessionMap: ReadonlyMap<string, string>) => sessionMap.get("unwise");
    expect(oldLookupHadNoWayToSeeLiveJobs(new Map())).toBeUndefined();

    const result = findFieldCutoutIdBySource({
      target: ic1805,
      sourceIds: ["unwise"],
      liveCutouts: [autofetched],
      sessionCutoutIdBySource: new Map(),
      defaultSizeDeg: () => 1.5,
      matchRadiusDeg: 0.05,
    });
    expect(result.get("unwise")).toBe("job-autofetch-1");

    // Feeding that id straight into `resolveCutoutRows`, exactly as
    // `SkyMap.tsx` does, is what actually fixes the panel: "ready", not
    // "not-fetched".
    const rows = resolveCutoutRows({
      target: ic1805,
      sources: [source({ id: "unwise", label: "unWISE" })],
      currentCutoutIdBySource: result,
      liveCutouts: [autofetched],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(only(rows).state).toEqual({ kind: "ready", cutoutId: "job-autofetch-1" });
  });

  it("trusts an explicit session request over a geo-matched candidate, once the session's own job is itself live", () => {
    const sessionJob = job({ id: "job-session", source: "wise", status: "running", ra_deg: 283.396, dec_deg: -1.294 });
    const olderComplete = job({ id: "job-older", source: "wise", status: "complete", ra_deg: 283.396, dec_deg: -1.294 });
    const result = findFieldCutoutIdBySource({
      target: { raDeg: 283.396, decDeg: -1.294 },
      sourceIds: ["wise"],
      liveCutouts: [sessionJob, olderComplete],
      sessionCutoutIdBySource: new Map([["wise", "job-session"]]),
      defaultSizeDeg: () => 1.5,
      matchRadiusDeg: 0.05,
    });
    expect(result.get("wise")).toBe("job-session");
  });

  it("never matches a job for a different field, even at the same source and an identical size", () => {
    const elsewhere = job({ id: "job-elsewhere", source: "wise", status: "complete", ra_deg: 10.0, dec_deg: 41.267 });
    const result = findFieldCutoutIdBySource({
      target: ic1805,
      sourceIds: ["wise"],
      liveCutouts: [elsewhere],
      sessionCutoutIdBySource: new Map(),
      defaultSizeDeg: () => 1.5,
      matchRadiusDeg: 0.05,
    });
    expect(result.has("wise")).toBe(false);
  });

  it("matches across the RA 0h/24h wrap rather than reading a job at RA 359.99 as far from a target at RA 0.01", () => {
    const acrossTheWrap = job({ id: "job-wrap", source: "wise", status: "complete", ra_deg: 359.99, dec_deg: 0 });
    const result = findFieldCutoutIdBySource({
      target: { raDeg: 0.01, decDeg: 0 },
      sourceIds: ["wise"],
      liveCutouts: [acrossTheWrap],
      sessionCutoutIdBySource: new Map(),
      defaultSizeDeg: () => 1.5,
      matchRadiusDeg: 0.05,
    });
    expect(result.get("wise")).toBe("job-wrap");
  });

  it("excludes a matching-position job that is smaller than the current default size, same tolerance as the point lookup used to apply", () => {
    const tooSmall = job({ id: "job-small", source: "wise", status: "complete", size_deg: 1.0 });
    const result = findFieldCutoutIdBySource({
      target: { raDeg: 283.396, decDeg: -1.294 },
      sourceIds: ["wise"],
      liveCutouts: [tooSmall],
      sessionCutoutIdBySource: new Map(),
      defaultSizeDeg: () => 1.5,
      matchRadiusDeg: 0.05,
    });
    expect(result.has("wise")).toBe(false);
  });

  it("prefers the smallest matching complete job over a larger one, so a fresh tight fetch is not shadowed by an old wide one", () => {
    const wide = job({ id: "job-wide", source: "wise", status: "complete", size_deg: 6.0 });
    const tight = job({ id: "job-tight", source: "wise", status: "complete", size_deg: 1.5 });
    const result = findFieldCutoutIdBySource({
      target: { raDeg: 283.396, decDeg: -1.294 },
      sourceIds: ["wise"],
      liveCutouts: [wide, tight],
      sessionCutoutIdBySource: new Map(),
      defaultSizeDeg: () => 1.5,
      matchRadiusDeg: 0.05,
    });
    expect(result.get("wise")).toBe("job-tight");
  });

  it("surfaces a running job so 'fetching' can render, when nothing complete is there yet", () => {
    const running = job({ id: "job-running", source: "wise", status: "running", size_deg: 1.5 });
    const result = findFieldCutoutIdBySource({
      target: { raDeg: 283.396, decDeg: -1.294 },
      sourceIds: ["wise"],
      liveCutouts: [running],
      sessionCutoutIdBySource: new Map(),
      defaultSizeDeg: () => 1.5,
      matchRadiusDeg: 0.05,
    });
    expect(result.get("wise")).toBe("job-running");
  });
});

describe("summarizeCutoutFetchStatus", () => {
  it("returns null when there is nothing to show (no target)", () => {
    const rows = resolveCutoutRows({
      target: null,
      sources: [source()],
      currentCutoutIdBySource: new Map(),
      liveCutouts: [],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(summarizeCutoutFetchStatus(rows)).toBeNull();
  });

  it("returns null for idle/ready rows — no active fetch, no failure", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source(), source({ id: "sdss", label: "SDSS (color)" })],
      currentCutoutIdBySource: new Map([["sdss", "job-1"]]),
      liveCutouts: [job({ id: "job-1", source: "sdss", status: "complete" })],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(summarizeCutoutFetchStatus(rows)).toBeNull();
  });

  it("reports a single fetching source by label and status", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map([["wise", "job-1"]]),
      liveCutouts: [job({ status: "running" })],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(summarizeCutoutFetchStatus(rows)).toEqual({
      kind: "fetching",
      label: "AllWISE (infrared)",
      status: "running",
      extraCount: 0,
    });
  });

  it("counts extra fetching sources rather than dropping them silently", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source(), source({ id: "sdss", label: "SDSS (color)" })],
      currentCutoutIdBySource: new Map([
        ["wise", "job-wise"],
        ["sdss", "job-sdss"],
      ]),
      liveCutouts: [
        job({ id: "job-wise", source: "wise", status: "queued" }),
        job({ id: "job-sdss", source: "sdss", status: "running" }),
      ],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(summarizeCutoutFetchStatus(rows)).toEqual({
      kind: "fetching",
      label: "AllWISE (infrared)",
      status: "queued",
      extraCount: 1,
    });
  });

  it("reports a real failure (retryable) with its human reason and raw detail", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map([["wise", "job-1"]]),
      liveCutouts: [
        job({
          status: "failed",
          error: "AllWISE fetch failed",
          error_detail: "upstream 500",
        }),
      ],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(summarizeCutoutFetchStatus(rows)).toEqual({
      kind: "failed",
      label: "AllWISE (infrared)",
      reason: "AllWISE fetch failed",
      detail: "upstream 500",
      extraCount: 0,
    });
  });

  it("never surfaces a footprint miss (retryable: false) as a failure", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source()],
      currentCutoutIdBySource: new Map([["wise", "job-1"]]),
      liveCutouts: [job({ status: "unavailable", error: null })],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(summarizeCutoutFetchStatus(rows)).toBeNull();
  });

  it("prefers an active fetch over a stale failure from another source", () => {
    const rows = resolveCutoutRows({
      target,
      sources: [source(), source({ id: "sdss", label: "SDSS (color)" })],
      currentCutoutIdBySource: new Map([
        ["wise", "job-wise"],
        ["sdss", "job-sdss"],
      ]),
      liveCutouts: [
        job({ id: "job-wise", source: "wise", status: "failed", error: "AllWISE fetch failed" }),
        job({ id: "job-sdss", source: "sdss", status: "running" }),
      ],
      upstreamReachable: true,
      defaultSizeDeg: () => 1.5,
    });
    expect(summarizeCutoutFetchStatus(rows)).toEqual({
      kind: "fetching",
      label: "SDSS (color)",
      status: "running",
      extraCount: 0,
    });
  });
});
