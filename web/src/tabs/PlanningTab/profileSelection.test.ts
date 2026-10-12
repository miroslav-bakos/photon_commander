// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { selectPlanningProfile } from "./profileSelection";

const profiles = [
  { id: "zwo-s30", name: "Seestar S30" },
  { id: "zwo-s30pro", name: "Seestar S30 Pro" },
];

describe("selectPlanningProfile", () => {
  it("automatically follows an exact detected profile when no manual choice exists", () => {
    expect(selectPlanningProfile(profiles, null, "zwo-s30pro")?.id).toBe("zwo-s30pro");
  });

  it("preserves an explicit Planning-only choice", () => {
    expect(selectPlanningProfile(profiles, "zwo-s30", "zwo-s30pro")?.id).toBe("zwo-s30");
  });

  it("falls back safely when no detected profile is available", () => {
    expect(selectPlanningProfile(profiles, null, null)?.id).toBe("zwo-s30");
    expect(selectPlanningProfile(profiles, null, "unknown")?.id).toBe("zwo-s30");
  });
});
