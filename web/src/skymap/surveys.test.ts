// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { DEFAULT_BACKGROUND_SURVEY_ID, isRestrictedImagesetName } from "./surveys";

describe("DEFAULT_BACKGROUND_SURVEY_ID", () => {
  it("selects DSS with its independent restricted-use classification intact", () => {
    expect(DEFAULT_BACKGROUND_SURVEY_ID).toBe("dss2-color-hips-poc");
    expect(isRestrictedImagesetName(DEFAULT_BACKGROUND_SURVEY_ID)).toBe(true);
  });
});

describe("isRestrictedImagesetName", () => {
  /** The registry is checked server-side, where provenance is known. This is
   * the independent second check on whatever name is about to be shown —
   * every engine tried so far ships DSS in its own defaults. */
  it.each([
    "Digitized Sky Survey (Color)",
    "DSS",
    "DSS2 Red",
    "Guide Star Catalog",
    "GSC 2.3",
  ])("rejects %s", (name) => {
    expect(isRestrictedImagesetName(name)).toBe(true);
  });

  it.each(["PanSTARRS1 3pi", "Pan-STARRS DR1 (color)", "H-alpha (WHAM)", "AllWISE (infrared)"])(
    "allows %s",
    (name) => {
      expect(isRestrictedImagesetName(name)).toBe(false);
    },
  );

  it("does not reject a name that merely contains the letters", () => {
    // "Godsson" and friends: the pattern is word-bounded, not a substring match.
    expect(isRestrictedImagesetName("Cassiopeia Deep Sky Survey Mosaic")).toBe(false);
  });
});
