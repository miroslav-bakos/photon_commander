// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { ArchiveEntry } from "../../api/capture";
import { buildArchiveFolders, calendarCells, deriveArchiveSessions, shiftMonth } from "./archiveModel";

function entry(key: string, dateObs: string, objectName = "M31"): ArchiveEntry {
  const slash = key.lastIndexOf("/");
  return {
    key,
    name: key.slice(slash + 1),
    directory: slash < 0 ? "." : key.slice(0, slash),
    kind: "fits",
    size_bytes: 100,
    modified_at: dateObs,
    date_obs: dateObs,
    date_source: "fits_header",
    catalogue_id: null,
    object_name: objectName,
    filter_name: "LP",
    frame_type: "Light",
    exposure_s: 30,
    gain: 200,
    ccd_temp_c: 10,
    width: 3840,
    height: 2160,
    bayer_pattern: "GRBG",
    preview_available: true,
  };
}

describe("archiveModel", () => {
  it("builds ancestors and retains empty directories", () => {
    const folders = buildArchiveFolders(["empty"], [entry("M31/lights/one.fit", "2026-09-12T20:00:00Z")]);
    expect(folders.map((folder) => folder.key)).toEqual([".", "empty", "M31", "M31/lights"]);
    expect(folders.find((folder) => folder.key === "M31")).toMatchObject({ directFiles: 0, totalFiles: 1, totalBytes: 100 });
    expect(folders.find((folder) => folder.key === "empty")).toMatchObject({ directFiles: 0, totalFiles: 0 });
  });

  it("groups interleaved targets independently and uses a thirty-minute gap", () => {
    const sessions = deriveArchiveSessions([
      entry("M31/a.fit", "2026-09-12T20:00:00Z"),
      entry("M42/a.fit", "2026-09-12T20:05:00Z", "M42"),
      entry("M31/b.fit", "2026-09-12T20:20:00Z"),
      entry("M31/c.fit", "2026-09-12T20:51:00Z"),
    ]);
    expect(sessions).toHaveLength(3);
    expect(sessions.filter((session) => session.target === "M31").map((session) => session.entries.length).sort()).toEqual([1, 2]);
  });

  it("makes a Monday-first six-week calendar with counts", () => {
    const entries = [entry("M31/a.fit", "2026-09-12T20:00:00Z")];
    const sessions = deriveArchiveSessions(entries);
    const cells = calendarCells("2026-09", entries, sessions);
    expect(cells).toHaveLength(42);
    expect(cells[0]?.day).toBe("2026-08-31");
    expect(cells.find((cell) => cell.day === "2026-09-12")).toMatchObject({ fileCount: 1, sessionCount: 1, inMonth: true });
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
  });
});
