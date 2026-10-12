// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The Sun, Moon and planets: `GET /api/astro/bodies` (`astro-math`'s own
 * `ssc.astro`), through the same overlay path as catalogue outlines,
 * constellation boundaries and named bright stars (`SkyMap.tsx` merges all
 * four into one `setOverlay` call).
 *
 * These are the only shapes on the map that actually move as the scrubber's
 * instant changes — everything else here (catalogue outlines, constellation
 * boundaries, bright stars) is fixed against the sky, and this module owns
 * none of that motion itself: `ra_j2000_h`/`dec_j2000_deg` come straight from
 * the endpoint for whatever instant it was asked about, computed by
 * `astro/`, never re-derived here (architecture policy: "that number belongs to
 * astro-math").
 *
 * The Sun and Moon draw at their true apparent size (`apparent_diameter_arcmin`,
 * about half a degree for both — a real disc once zoomed in, not a dot).
 * Planets draw as a fixed-size marker instead: every planet's real angular
 * size is at most a couple of arcseconds, which would be an invisible speck
 * at any FOV this map allows, so `apparent_diameter_arcmin` is deliberately
 * ignored for `kind: "planet"` — drawing "to scale" would draw nothing.
 */
import type { AstroBodyModel } from "../api/astro";
import type { FovShape } from "./types";

/** A fixed marker radius for every planet, regardless of which one or how
 * bright — there is no true-to-life size worth drawing (see the module doc
 * comment), so this exists purely to be found on the map. Comparable to a
 * bright star's own dot size (`brightStars.ts`'s `MAX_DOT_RADIUS_DEG`, 0.09)
 * rather than smaller: a planet is worth noticing at a glance, not blending
 * into the star field it is drawn over. */
export const PLANET_MARKER_RADIUS_DEG = 0.12;

export function solarSystemShapeId(body: Pick<AstroBodyModel, "id">): string {
  return `solar-system:${body.id}`;
}

/** Half of `apparent_diameter_arcmin`, converted from arcminutes to degrees:
 * `diameter_arcmin / 60` is the diameter in degrees, so the radius is that
 * halved again. */
export function apparentRadiusDeg(apparentDiameterArcmin: number): number {
  return apparentDiameterArcmin / 60 / 2;
}

export interface SolarSystemVisibility {
  sun: boolean;
  moon: boolean;
  planets: boolean;
}

/** One shape per visible body — no FOV gating and no label budget the way
 * `catalogOutlines.ts`/`brightStars.ts` need one: there are at most a
 * handful of these (Sun, Moon, eight planets) in any response, so every
 * visible one gets a label, always. */
export function buildSolarSystemShapes(
  bodies: readonly AstroBodyModel[],
  visibility: SolarSystemVisibility,
): FovShape[] {
  const shapes: FovShape[] = [];
  for (const body of bodies) {
    if (body.kind === "sun" && !visibility.sun) continue;
    if (body.kind === "moon" && !visibility.moon) continue;
    if (body.kind === "planet" && !visibility.planets) continue;
    const radiusDeg = body.kind === "planet" ? PLANET_MARKER_RADIUS_DEG : apparentRadiusDeg(body.apparent_diameter_arcmin);
    shapes.push({
      kind: "circle",
      id: solarSystemShapeId(body),
      centerRa: body.ra_j2000_h * 15,
      centerDec: body.dec_j2000_deg,
      radiusDeg,
      style: { role: body.kind, fillOpacity: 1 },
      label: body.name,
    });
  }
  return shapes;
}
