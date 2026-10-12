// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import type { SurveyModel } from "../api/skymap";
import { DEFAULT_LAYER_PREFS, loadLayerPrefs, resolveLayerSelection, saveLayerPrefs, STORAGE_KEY } from "./layerPrefs";

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
    data,
  };
}

describe("layerPrefs", () => {
  it("defaults to acknowledged DSS, outlines on, no lines, no cutouts", () => {
    expect(loadLayerPrefs(memoryStorage())).toEqual(DEFAULT_LAYER_PREFS);
    expect(DEFAULT_LAYER_PREFS.background).toBe("dss2-color-hips-poc");
    expect(DEFAULT_LAYER_PREFS.overlays).toEqual({});
    expect(DEFAULT_LAYER_PREFS.catalogOutlines).toBe(true);
    expect(DEFAULT_LAYER_PREFS.constellationBoundaries).toBe(false);
    expect(DEFAULT_LAYER_PREFS.altitudeLimitRing).toBe(false);
    expect(DEFAULT_LAYER_PREFS.brightStars).toBe(true);
    expect(DEFAULT_LAYER_PREFS.cutouts).toEqual({});
    expect(DEFAULT_LAYER_PREFS.restrictedImageryConsents).toEqual({});
    expect(DEFAULT_LAYER_PREFS.solarSystem).toEqual({ sun: true, moon: true, planets: true });
    expect(DEFAULT_LAYER_PREFS.solarSystemOpacity).toBe(0.5);
  });

  it("ignores a stored mapKind from before the Imagery/Vector toggle was removed, rather than throwing", () => {
    const withWwt = loadLayerPrefs(memoryStorage({ [STORAGE_KEY]: JSON.stringify({ mapKind: "wwt" }) }));
    expect(withWwt).toEqual(DEFAULT_LAYER_PREFS);
    expect((withWwt as unknown as Record<string, unknown>).mapKind).toBeUndefined();

    const withVector = loadLayerPrefs(memoryStorage({ [STORAGE_KEY]: JSON.stringify({ mapKind: "vector" }) }));
    expect(withVector).toEqual(DEFAULT_LAYER_PREFS);
  });

  it("round-trips constellationBoundaries and falls back to its own default when missing", () => {
    const storage = memoryStorage();
    saveLayerPrefs({ ...DEFAULT_LAYER_PREFS, constellationBoundaries: true }, storage);
    expect(loadLayerPrefs(storage).constellationBoundaries).toBe(true);

    const partial = loadLayerPrefs(memoryStorage({ [STORAGE_KEY]: JSON.stringify({ constellationBoundaries: "yes" }) }));
    expect(partial.constellationBoundaries).toBe(DEFAULT_LAYER_PREFS.constellationBoundaries);
  });

  it("defaults altitudeLimitRing to false for a stored prefs object that predates the key, and round-trips true", () => {
    // The whole point of the toggle (owner: "don't show ... alt limit line
    // if ... alt limit not set"): the ring must not silently appear for a
    // viewer whose saved prefs were written before this key existed.
    const predatesTheKey = loadLayerPrefs(
      memoryStorage({ [STORAGE_KEY]: JSON.stringify({ background: "legacy-surveys-dr11", catalogOutlines: true }) }),
    );
    expect(predatesTheKey.altitudeLimitRing).toBe(false);

    const storage = memoryStorage();
    saveLayerPrefs({ ...DEFAULT_LAYER_PREFS, altitudeLimitRing: true }, storage);
    expect(loadLayerPrefs(storage).altitudeLimitRing).toBe(true);

    const invalid = loadLayerPrefs(memoryStorage({ [STORAGE_KEY]: JSON.stringify({ altitudeLimitRing: "yes" }) }));
    expect(invalid.altitudeLimitRing).toBe(false);
  });

  it("round-trips brightStars and falls back to its own default when missing", () => {
    const storage = memoryStorage();
    saveLayerPrefs({ ...DEFAULT_LAYER_PREFS, brightStars: false }, storage);
    expect(loadLayerPrefs(storage).brightStars).toBe(false);

    const partial = loadLayerPrefs(memoryStorage({ [STORAGE_KEY]: JSON.stringify({ brightStars: "yes" }) }));
    expect(partial.brightStars).toBe(DEFAULT_LAYER_PREFS.brightStars);
  });

  it("round-trips solarSystem and falls back to its own default when missing or malformed", () => {
    const storage = memoryStorage();
    saveLayerPrefs({ ...DEFAULT_LAYER_PREFS, solarSystem: { sun: false, moon: true, planets: false } }, storage);
    expect(loadLayerPrefs(storage).solarSystem).toEqual({ sun: false, moon: true, planets: false });

    const predatesTheKey = loadLayerPrefs(
      memoryStorage({ [STORAGE_KEY]: JSON.stringify({ background: "legacy-surveys-dr11" }) }),
    );
    expect(predatesTheKey.solarSystem).toEqual({ sun: true, moon: true, planets: true });

    const malformed = loadLayerPrefs(memoryStorage({ [STORAGE_KEY]: JSON.stringify({ solarSystem: { sun: "yes" } }) }));
    expect(malformed.solarSystem).toEqual({ sun: true, moon: true, planets: true });
  });

  it("round-trips through storage", () => {
    const storage = memoryStorage();
    const prefs = {
      ...DEFAULT_LAYER_PREFS,
      overlays: { "wham-halpha": { visible: true, opacity: 0.4 } },
      skyLines: { ...DEFAULT_LAYER_PREFS.skyLines, meridian: true },
      cutouts: { wise: { visible: true, opacity: 0.7 } },
    };
    saveLayerPrefs(prefs, storage);
    expect(loadLayerPrefs(storage)).toEqual(prefs);
  });

  it("falls back field by field on a corrupt or stale entry, never throws", () => {
    expect(loadLayerPrefs(memoryStorage({ [STORAGE_KEY]: "not json" }))).toEqual(DEFAULT_LAYER_PREFS);
    const partial = loadLayerPrefs(
      memoryStorage({
        [STORAGE_KEY]: JSON.stringify({
          background: null,
          overlays: { x: { opacity: 7 } },
          skyLines: 3,
          cutouts: { wise: { opacity: 7 }, sdss: { visible: true, opacity: 0.5 } },
        }),
      }),
    );
    expect(partial.background).toBeNull();
    expect(partial.overlays).toEqual({ x: { visible: false, opacity: 0.5 } });
    expect(partial.skyLines).toEqual(DEFAULT_LAYER_PREFS.skyLines);
    // Falls back to the cutout-specific default opacity, not the overlay one.
    expect(partial.cutouts).toEqual({
      wise: { visible: false, opacity: 0.5 },
      sdss: { visible: true, opacity: 0.5 },
    });
  });

  it("survives a storage that throws (locked-down browser)", () => {
    const hostile = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    };
    expect(loadLayerPrefs(hostile)).toEqual(DEFAULT_LAYER_PREFS);
    expect(() => { saveLayerPrefs(DEFAULT_LAYER_PREFS, hostile); }).not.toThrow();
  });

  it("does not restore a restricted background from stale preferences without consent", () => {
    const restricted = {
      id: "dss2-color-hips-poc",
      enabled: true,
      blend_only: false,
      production_cleared: false,
    } as SurveyModel;
    const cleared = {
      id: "legacy-surveys-dr11",
      enabled: true,
      blend_only: false,
      production_cleared: true,
    } as SurveyModel;
    expect(
      resolveLayerSelection({ ...DEFAULT_LAYER_PREFS, background: restricted.id }, [restricted, cleared]).background,
    ).toBe(null);
    expect(
      resolveLayerSelection({
        ...DEFAULT_LAYER_PREFS,
        background: restricted.id,
        restrictedImageryConsents: { [restricted.id]: true },
      }, [restricted, cleared]).background,
    ).toBe(restricted.id);
  });
});

it("selects acknowledged DSS independently with half-opacity defaults", () => {
  const dss = { id: "dss2-color-hips-poc", enabled: true, blend_only: false, production_cleared: false } as SurveyModel;
  const prefs = { ...DEFAULT_LAYER_PREFS, background: dss.id, restrictedImageryConsents: { [dss.id]: true } };
  expect(resolveLayerSelection(prefs, [dss]).background).toBe(dss.id);
  for (const key of ["backgroundOpacity", "skyLinesOpacity", "catalogueOpacity", "solarSystemOpacity", "mosaicOpacity"] as const) {
    expect(DEFAULT_LAYER_PREFS[key]).toBe(0.5);
  }
});
