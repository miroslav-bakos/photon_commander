// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The reference lines and grids the frame draws itself, as `FovShape`s
 * through the same overlay path as everything else: the celestial equator,
 * the local meridian and zenith marker, the equatorial and alt-az
 * coordinate grids, the ecliptic, and the horizon. Pure — no engine import —
 * so the geometry is unit-tested without a map.
 *
 * Why the frame and not the host: these follow the scrubber instant, and the
 * host already re-sends `setDisplay` per scrub step; one polyline rebuild
 * per change is the whole cost (skymap-viz.md's 16 ms budget), and the
 * sidereal time it needs is `sidereal.ts`'s one deliberate local formula.
 * The vector frame has no engine underneath with its own grid settings to
 * fight (WWT's own `Settings.set_showGrid`/`set_showAltAzGrid` did, gated
 * behind its solar-system layer, before it was removed — `THIRD_PARTY_NOTICES.md`
 * §9), so every grid this map can show is built here regardless.
 *
 * Every line below is its own small `FovShape` with a stable id rather than
 * one shape holding the whole grid: the equatorial grid and ecliptic never
 * change while only the instant advances (they are pure RA/Dec), so on a
 * scrub tick they are cheap to rebuild identically; only the alt-az grid and
 * horizon, which genuinely depend on the instant, actually change.
 *
 * Point density is bucketed by field of view (`gridStepDeg`) rather than
 * fixed: at 60° FoV the difference between a line sampled every 1° and
 * every 10° is invisible, and the coarser sampling keeps every scrub tick's
 * `JSON.stringify` diff (and the label placement below) cheap. Bucketing
 * (rather than a continuous function of FoV) also means panning within a
 * bucket costs nothing extra — the previous tick's shapes come back with an
 * identical signature.
 */
import type { FovShape } from "../../types";
import { type SiteInstant, altAzToRaDec, circularDistanceDeg, localMeanSiderealTimeHours, raDecToAltAz } from "./sidereal";

export const CELESTIAL_EQUATOR_ID = "skyline:celestial-equator";
export const MERIDIAN_ID = "skyline:meridian";
export const ZENITH_ID = "skyline:zenith";

/** Dec = 0 all the way round, one vertex per degree of RA. */
export function celestialEquatorShape(): FovShape {
  const vertices: [number, number][] = [];
  for (let ra = 0; ra <= 360; ra += 1) vertices.push([ra % 360, 0]);
  return {
    kind: "polyline",
    id: CELESTIAL_EQUATOR_ID,
    verticesRaDec: vertices,
    style: { role: "skyline", dashed: true },
    label: "Celestial equator",
    labelAt: [0, 0],
  };
}

export interface MeridianInputs {
  latitudeDeg: number;
  longitudeDeg: number;
  instant: Date;
}

/** The local meridian — RA = local sidereal time, pole to pole through the
 * zenith — and the zenith itself at (LST, latitude). The half through the
 * nadir is not drawn: an EQ user watches for a *transit*, which happens on
 * this half. */
export function meridianShapes(inputs: MeridianInputs): FovShape[] {
  const lstDeg = localMeanSiderealTimeHours(inputs.instant, inputs.longitudeDeg) * 15;
  const vertices: [number, number][] = [];
  for (let dec = -90; dec <= 90; dec += 2) vertices.push([lstDeg, dec]);
  return [
    {
      kind: "polyline",
      id: MERIDIAN_ID,
      verticesRaDec: vertices,
      style: { role: "skyline" },
      label: "Meridian",
      labelAt: [lstDeg, Math.max(-88, Math.min(88, inputs.latitudeDeg - 20))],
    },
    {
      kind: "circle",
      id: ZENITH_ID,
      centerRa: lstDeg,
      centerDec: inputs.latitudeDeg,
      radiusDeg: 0.4,
      style: { role: "skyline" },
      label: "Zenith",
      labelAt: [lstDeg, inputs.latitudeDeg + 0.4],
    },
  ];
}

/** Vertex spacing in degrees, coarser as the field of view widens — see the
 * module doc comment. `null`/non-finite (view not yet known) gets the
 * coarsest bucket, never the finest: an unknown FoV is not evidence the user
 * is zoomed in. */
function gridStepDeg(fovDeg: number | null): number {
  if (fovDeg === null || !Number.isFinite(fovDeg) || fovDeg > 40) return 10;
  if (fovDeg > 15) return 5;
  if (fovDeg > 4) return 2;
  return 1;
}

/** RA/Dec grid label text: "2h" for a whole-hour RA line, "+30°"/"0°"/"-30°"
 * for a Dec line — the exact forms `skymap-viz.md` specifies. */
function raLabel(raH: number): string {
  return `${String(raH)}h`;
}
function decLabel(dec: number): string {
  return dec === 0 ? "0°" : `${dec > 0 ? "+" : ""}${String(dec)}°`;
}

/** Labels only render when the grid is dense enough to read them, and only
 * near the current view, capped at `MAX_GRID_LABELS` total — a
 * 24-RA/17-Dec-line grid unconditionally labelled would be unreadable
 * clutter, not a coordinate readout.
 *
 * 60°, raised from 30° on 2026-09-08. Both frames open at
 * `INITIAL_FOV_DEG = 60`, so a 30° ceiling meant **no labels at all on a
 * fresh load** — every coordinate label, star name and constellation name
 * was invisible until the user happened to zoom past halfway in. The owner
 * reported that three separate times before the shared cause was found
 * ("alt/az and eq grid stopped showing degrees in both axes", star names
 * and constellation names "still missing"); the gate was doing what it was
 * written to do, and what it was written to do was wrong for this app's own
 * default view. The threshold now matches the widest view the map ever
 * opens at, so labels are present when someone is orienting themselves —
 * which is exactly when a coordinate readout is most useful. Per-layer
 * density is controlled by each layer's own count cap and size rule
 * (`MAX_GRID_LABELS` here, `MAX_STAR_LABELS`, `MAX_CONSTELLATION_LABELS` /
 * `CONSTELLATION_LABEL_MIN_FOV_FRACTION`), which is the right place for it:
 * a single shared on/off ceiling cannot know that twelve grid ticks and
 * twelve constellation names are very different amounts of ink. */
/** Raised 60 -> 120 with the map's own `MAX_FOV_DEG`. It has been the wrong
 * number twice now: at 30 no grid label ever showed, because both frames
 * open at 60. A gate tied to the zoom ceiling has to move when the ceiling
 * does. */
export const GRID_LABEL_MAX_FOV_DEG = 120;
const MAX_GRID_LABELS = 12;

interface EquatorialGridOptions {
  /** Current field of view in degrees, or `null` before the frame's first
   * view report — governs both point density and whether labels show. */
  fovDeg: number | null;
  viewRaDeg: number;
  viewDecDeg: number;
  /** True when the dedicated "Celestial equator" toggle is *off* — the
   * Dec = 0 grid line is then the only representation of the equator, so it
   * is drawn with the equator's own stronger style instead of the plain
   * grid style. When the toggle is on, the dedicated line already carries
   * that emphasis and the grid's own Dec = 0 line stays plain, so the two
   * do not double up. */
  strengthenEquator: boolean;
}

const EQ_GRID_RA_PREFIX = "skyline:eq-grid:ra:";
const EQ_GRID_DEC_PREFIX = "skyline:eq-grid:dec:";
/** How far along an RA (hour-circle) meridian the grid is drawn. 90 means
 * pole to pole, so all 24 meridians converge at the celestial poles the way
 * meridians therefore converge at both celestial poles.
 *
 * It was 80, which stopped every meridian ten degrees short and left the
 * pole as an empty gap with two dozen lines pointing at nothing. Labels
 * still clamp to 85 (`clampLabelDec`) so a label anchor is never asked to
 * project the singular point itself. */
const EQ_GRID_RA_DEC_LIMIT = 90;
const EQ_GRID_DEC_STEP = 10;
const EQ_GRID_DEC_LIMIT = 80;

/** RA lines every 1h (Dec −80…+80) and Dec lines every 10° (full circle) —
 * pure celestial coordinates, independent of site and instant. */
export function equatorialGridShapes(options: EquatorialGridOptions): FovShape[] {
  const step = gridStepDeg(options.fovDeg);
  const labelsOn = options.fovDeg !== null && Number.isFinite(options.fovDeg) && options.fovDeg <= GRID_LABEL_MAX_FOV_DEG;
  const raHours = Array.from({ length: 24 }, (_, i) => i);
  const decs: number[] = [];
  for (let dec = -EQ_GRID_DEC_LIMIT; dec <= EQ_GRID_DEC_LIMIT; dec += EQ_GRID_DEC_STEP) decs.push(dec);
  const keep = labelsOn
    ? nearestLabelsPerAxis(
        raHours.map((raH) => ({
          id: `${EQ_GRID_RA_PREFIX}${String(raH)}`,
          dist: circularDistanceDeg(raH * 15, options.viewRaDeg),
        })),
        decs.map((dec) => ({
          id: `${EQ_GRID_DEC_PREFIX}${String(dec)}`,
          dist: Math.abs(dec - options.viewDecDeg),
        })),
        MAX_GRID_LABELS,
      )
    : null;

  const shapes: FovShape[] = [];
  for (const raH of raHours) {
    const raDeg = raH * 15;
    const vertices: [number, number][] = [];
    for (let dec = -EQ_GRID_RA_DEC_LIMIT; dec < EQ_GRID_RA_DEC_LIMIT; dec += step) vertices.push([raDeg, dec]);
    vertices.push([raDeg, EQ_GRID_RA_DEC_LIMIT]);
    const id = `${EQ_GRID_RA_PREFIX}${String(raH)}`;
    shapes.push({
      kind: "polyline",
      id,
      verticesRaDec: vertices,
      style: { role: "coordGrid" },
      labelAt: [raDeg, clampLabelDec(options.viewDecDeg)],
      label: keep?.has(id) === true ? raLabel(raH) : undefined,
    });
  }

  for (const dec of decs) {
    const vertices: [number, number][] = [];
    for (let ra = 0; ra < 360; ra += step) vertices.push([ra, dec]);
    vertices.push([0, dec]);
    const id = `${EQ_GRID_DEC_PREFIX}${String(dec)}`;
    const strong = dec === 0 && options.strengthenEquator;
    shapes.push({
      kind: "polyline",
      id,
      verticesRaDec: vertices,
      style: strong ? { role: "skyline" } : { role: "coordGrid" },
      labelAt: [options.viewRaDeg, dec],
      label: keep?.has(id) === true ? decLabel(dec) : undefined,
    });
  }

  return shapes;
}

/** Dec stays inside (-90, 90) even for a view centred near a pole, so an RA
 * line's label anchor is never asked to project a point that does not
 * exist. */
function clampLabelDec(viewDecDeg: number): number {
  return Math.max(-85, Math.min(85, viewDecDeg));
}

/** The `max` label candidates closest to the current view, by id — shared
 * by both grids (`equatorialGridShapes`/`altAzGridShapes`): a
 * 24-RA/17-Dec or 24-azimuth/9-altitude grid unconditionally labelled would
 * be unreadable clutter, not a coordinate readout. Each grid builds its own
 * `candidates` because the distance metric differs by axis — RA and azimuth
 * wrap at 0/360 (`circularDistanceDeg`), Dec and altitude do not — not
 * because the picking rule itself differs. */
export function nearestLabels(candidates: readonly { id: string; dist: number }[], max: number): Set<string> {
  const sorted = [...candidates].sort((a, b) => a.dist - b.dist);
  return new Set(sorted.slice(0, max).map((c) => c.id));
}

/**
 * `nearestLabels` with a **per-axis budget**, which is what both grids
 * actually want.
 *
 * Reported live: "altitude labels are present but not the longitude". The
 * cause was ranking both axes in one shared pool: the two distances are not
 * comparable. An azimuth candidate's distance runs 0…180° while an altitude
 * candidate's runs only 0…80° (and RA 0…180° against Dec 0…90°), so the
 * shorter-ranged axis is systematically "nearer" and takes nearly every
 * slot — nine altitude circles could crowd out all but a couple of the
 * twenty-four azimuth spokes, leaving a grid labelled in one direction
 * only. Sorting was working perfectly; it was being asked the wrong
 * question.
 *
 * Each axis gets half the budget, and whatever one axis does not use goes
 * to the other (a view near the pole legitimately has few RA lines worth
 * labelling), so the total cap still holds and a sparse axis costs nothing.
 */
export function nearestLabelsPerAxis(
  first: readonly { id: string; dist: number }[],
  second: readonly { id: string; dist: number }[],
  max: number,
): Set<string> {
  const half = Math.floor(max / 2);
  const firstKept = nearestLabels(first, Math.min(half, first.length));
  const secondKept = nearestLabels(second, max - firstKept.size);
  // Whatever the second axis left unused flows back to the first.
  const spare = max - firstKept.size - secondKept.size;
  const firstTopped = spare > 0 ? nearestLabels(first, firstKept.size + spare) : firstKept;
  return new Set([...firstTopped, ...secondKept]);
}

interface AltAzGridOptions extends SiteInstant {
  fovDeg: number | null;
  /** Current view centre, RA/Dec — the alt-az grid's own lines are built in
   * alt/az, but `ViewState` (`types.ts`) always reports RA/Dec regardless of
   * backend, so picking which lines are "near the view" needs the view's
   * alt/az too (`raDecToAltAz`), the same role `EquatorialGridOptions`'s
   * `viewRaDeg`/`viewDecDeg` play directly for that grid's own coordinates. */
  viewRaDeg: number;
  viewDecDeg: number;
  /** True when the dedicated "Horizon" toggle is *off* — mirrors
   * `EquatorialGridOptions.strengthenEquator` for the alt = 0 line. */
  strengthenHorizon: boolean;
}

const ALTAZ_GRID_AZ_PREFIX = "skyline:altaz-grid:az:";
const ALTAZ_GRID_ALT_PREFIX = "skyline:altaz-grid:alt:";
const ALTAZ_GRID_AZ_STEP = 15;
const ALTAZ_GRID_ALT_STEP = 10;
/** The altitude *rings* stop at 80: a ring at 90 is a point, and one at 85
 * is a tiny circle round the zenith that reads as a smudge. */
const ALTAZ_GRID_ALT_MAX = 80;
/** The azimuth *spokes*, though, run all the way to the zenith so they
 * converge there — the alt-az counterpart of the meridians meeting at the
 * pole. Separate from the ring limit above because the two want different
 * answers: spokes gain from meeting, rings do not. */
const ALTAZ_GRID_AZ_ALT_MAX = 90;

/** Azimuth/altitude label text: "90°"/"30°" — magnitude only, unlike
 * `decLabel`'s +/- sign, because neither azimuth (already [0, 360)) nor this
 * grid's altitude range (always >= 0) is ever negative. */
function azOrAltLabel(deg: number): string {
  return `${String(deg)}°`;
}

/** Altitude stays inside the grid's own drawn range even for a view whose
 * real altitude is above `ALTAZ_GRID_ALT_MAX` or below the horizon, so an
 * azimuth spoke's label anchor is never asked to project a point off the
 * line it labels — the alt-az analogue of `clampLabelDec`. */
function clampLabelAlt(viewAltDeg: number): number {
  return Math.max(0, Math.min(ALTAZ_GRID_ALT_MAX, viewAltDeg));
}

/** Azimuth lines every 15° and altitude lines every 10° (0…80°, staying
 * short of the zenith singularity), for the planned site and instant — the
 * alt-az analogue of `equatorialGridShapes`, genuinely dependent on the
 * instant (unlike the equatorial grid) so it is rebuilt every scrub tick,
 * cheaply (a few hundred `altAzToRaDec` calls at most, well under the
 * 16 ms budget). Labelled the same way and under the same rules as
 * `equatorialGridShapes` (near-view only, capped, hidden past
 * `GRID_LABEL_MAX_FOV_DEG`, `coordGrid`'s muted token) — `nearestLabels` is
 * the one picking rule both grids share. */
export function altAzGridShapes(options: AltAzGridOptions): FovShape[] {
  const step = gridStepDeg(options.fovDeg);
  const labelsOn = options.fovDeg !== null && Number.isFinite(options.fovDeg) && options.fovDeg <= GRID_LABEL_MAX_FOV_DEG;
  const azimuths: number[] = [];
  for (let az = 0; az < 360; az += ALTAZ_GRID_AZ_STEP) azimuths.push(az);
  const alts: number[] = [];
  for (let alt = 0; alt <= ALTAZ_GRID_ALT_MAX; alt += ALTAZ_GRID_ALT_STEP) alts.push(alt);

  // The view's own position in this grid's coordinates — RA/Dec everywhere
  // else in this file, but alt/az is what "near the view" means here.
  const [viewAltDeg, viewAzDeg] = raDecToAltAz(options.viewRaDeg, options.viewDecDeg, options);
  const keep = labelsOn
    ? nearestLabelsPerAxis(
        azimuths.map((az) => ({ id: `${ALTAZ_GRID_AZ_PREFIX}${String(az)}`, dist: circularDistanceDeg(az, viewAzDeg) })),
        alts.map((alt) => ({ id: `${ALTAZ_GRID_ALT_PREFIX}${String(alt)}`, dist: Math.abs(alt - viewAltDeg) })),
        MAX_GRID_LABELS,
      )
    : null;

  const shapes: FovShape[] = [];

  for (const az of azimuths) {
    const vertices: [number, number][] = [];
    for (let alt = 0; alt < ALTAZ_GRID_AZ_ALT_MAX; alt += step) vertices.push(altAzToRaDec(alt, az, options));
    vertices.push(altAzToRaDec(ALTAZ_GRID_AZ_ALT_MAX, az, options));
    const id = `${ALTAZ_GRID_AZ_PREFIX}${String(az)}`;
    shapes.push({
      kind: "polyline",
      id,
      verticesRaDec: vertices,
      style: { role: "coordGrid" },
      labelAt: altAzToRaDec(clampLabelAlt(viewAltDeg), az, options),
      label: keep?.has(id) === true ? azOrAltLabel(az) : undefined,
    });
  }

  for (const alt of alts) {
    const vertices: [number, number][] = [];
    for (let az = 0; az < 360; az += step) vertices.push(altAzToRaDec(alt, az, options));
    vertices.push(altAzToRaDec(alt, 0, options));
    const id = `${ALTAZ_GRID_ALT_PREFIX}${String(alt)}`;
    const strong = alt === 0 && options.strengthenHorizon;
    shapes.push({
      kind: "polyline",
      id,
      verticesRaDec: vertices,
      style: strong ? { role: "skyline" } : { role: "coordGrid" },
      labelAt: altAzToRaDec(alt, viewAzDeg, options),
      label: keep?.has(id) === true ? azOrAltLabel(alt) : undefined,
    });
  }

  return shapes;
}

export const ECLIPTIC_ID = "skyline:ecliptic";
/** Mean obliquity of the ecliptic — a constant to the precision this map
 * needs (drifts ~0.013°/century; invisible at any zoom here), same value
 * `dash.ts`'s neighbours use for other fixed-geometry constants. */
const ECLIPTIC_OBLIQUITY_DEG = 23.44;

/** The frame's own ecliptic: λ = 0…360° at β = 0, converted to RA/Dec at
 * the mean obliquity. Passes through RA 0h/Dec 0 (the vernal equinox, λ=0)
 * and RA 6h/Dec +23.44° (the summer solstice, λ=90°) by construction. Pure
 * celestial coordinates — independent of site and instant, like the
 * equatorial grid. */
export function eclipticShape(fovDeg: number | null): FovShape {
  const step = gridStepDeg(fovDeg);
  const obliquity = ECLIPTIC_OBLIQUITY_DEG * (Math.PI / 180);
  const cosE = Math.cos(obliquity);
  const sinE = Math.sin(obliquity);
  const first = eclipticPointRaDec(0, cosE, sinE);
  const vertices: [number, number][] = [first];
  for (let lambda = step; lambda < 360; lambda += step) vertices.push(eclipticPointRaDec(lambda, cosE, sinE));
  vertices.push(eclipticPointRaDec(360, cosE, sinE));
  return {
    kind: "polyline",
    id: ECLIPTIC_ID,
    verticesRaDec: vertices,
    style: { role: "skyline", dashed: true },
    label: "Ecliptic",
    labelAt: first,
  };
}

function eclipticPointRaDec(lambdaDeg: number, cosE: number, sinE: number): [number, number] {
  const lambda = lambdaDeg * (Math.PI / 180);
  const sinL = Math.sin(lambda);
  const ra = (((Math.atan2(sinL * cosE, Math.cos(lambda)) * (180 / Math.PI)) % 360) + 360) % 360;
  const dec = Math.asin(sinL * sinE) * (180 / Math.PI);
  return [ra, dec];
}

export const HORIZON_ID = "skyline:horizon";

/** The alt = 0 line by itself, dashed — the alt-az analogue of
 * `celestialEquatorShape`, and the shape `applyDisplay` uses for the
 * dedicated "Horizon" toggle (independent of the alt-az grid, needing the
 * same site + instant as the meridian). */
/** The four cardinal points, as azimuth and the letter that names it.
 * North is azimuth 0 and azimuth increases through East — the convention
 * `sidereal.ts`'s own transform uses, so these are simply where the horizon
 * ring crosses each quarter. */
const CARDINALS: readonly (readonly [az: number, name: string])[] = [
  [0, "N"],
  [45, "NE"],
  [90, "E"],
  [135, "SE"],
  [180, "S"],
  [225, "SW"],
  [270, "W"],
  [315, "NW"],
];

export function cardinalShapeId(name: string): string {
  return `skyline:cardinal:${name}`;
}

/**
 * The alt = 0 line, plus a marker at each cardinal point (owner's request,
 * 2026-09-08: "i would like to add also N S W E markers on the 0 deg
 * line"). The alt-az analogue of `celestialEquatorShape`, and what the
 * dedicated "Horizon" toggle draws.
 *
 * Returns several shapes rather than one because a `FovShape` carries a
 * single label, and N/E/S/W are four separate anchored labels, not one
 * caption on the ring. They ride with the horizon rather than the alt-az
 * grid on purpose: the horizon is the line people orient against, and the
 * grid's own azimuth spokes are already labelled in degrees.
 *
 * The letters carry no degree value deliberately — "N" reads instantly in
 * the dark where "0°" needs a moment, and the alt-az grid still gives the
 * numeric azimuth for anyone who wants it.
 */
export function horizonShapes(options: SiteInstant & { fovDeg: number | null }): FovShape[] {
  const step = gridStepDeg(options.fovDeg);
  const first = altAzToRaDec(0, 0, options);
  const vertices: [number, number][] = [first];
  for (let az = step; az < 360; az += step) vertices.push(altAzToRaDec(0, az, options));
  vertices.push(altAzToRaDec(0, 360, options));
  const ring: FovShape = {
    kind: "polyline",
    id: HORIZON_ID,
    verticesRaDec: vertices,
    style: { role: "skyline", dashed: true },
    label: "Horizon",
    // Anchored between two cardinals, not at `first` (azimuth 0). `first` is
    // due north, which is exactly where the "N" marker below now sits, and
    // the two labels drew on top of each other — reported live, verbatim:
    // "horizon is called norizon on the map :)". Azimuth 45 keeps the word
    // on the ring while leaving every cardinal letter its own space.
    labelAt: altAzToRaDec(0, 45, options),
  };
  const cardinals = CARDINALS.map(([az, name]): FovShape => {
    const [raDeg, decDeg] = altAzToRaDec(0, az, options);
    return {
      kind: "circle",
      id: cardinalShapeId(name),
      centerRa: raDeg,
      centerDec: decDeg,
      // Nothing is drawn for this shape (`roleStyle.ts` gives the cardinal
      // role a zero line width): it exists purely to anchor the letter. The
      // radius is therefore arbitrary and tiny — a point, not a ring.
      radiusDeg: 0.01,
      style: { role: "cardinal" },
      label: name,
      labelAt: [raDeg, decDeg],
    };
  });
  return [ring, ...cardinals];
}

export const UNOBSERVABLE_CAP_ID = "skyline:unobservable-cap";

/**
 * The declination bound of the sky that **never rises** from `latitudeDeg`,
 * or `null` when every declination is reachable.
 *
 * Owner's request, 2026-09-08: "make background of the south hemisphere much
 * darker if observer location is in north one, and vice versa". Taken
 * literally that would be Dec = 0, but that is not where the sky stops being
 * observable: from 48°N a target culminates at altitude `90 - 48 + dec`, so
 * everything down to Dec = −42° still clears the horizon at some point in
 * the year — all of Sagittarius, most of Scorpius. Greying those out would
 * hide sky this app exists to help plan.
 *
 * The real boundary is the circle of perpetual invisibility, `|φ| − 90`:
 * below it for a northern observer, above it for a southern one. At the
 * equator it correctly vanishes — from 0° latitude the whole sky is
 * reachable over a year, and nothing should be dimmed.
 *
 * Returned as an object rather than a bare number so the caller does not
 * have to re-derive which side is dark, and so a literal-hemisphere variant
 * (`{ side: "below", decDeg: 0 }`) stays a one-line change here rather than
 * a rewrite of the shape builder.
 */
export function unobservableCapBound(
  latitudeDeg: number,
): { side: "below" | "above"; decDeg: number } | null {
  const bound = Math.abs(latitudeDeg) - 90;
  if (bound <= -90) return null;
  return latitudeDeg >= 0 ? { side: "below", decDeg: bound } : { side: "above", decDeg: -bound };
}

/**
 * A filled band over the never-rising cap — a wash toward the page
 * background (`"unobservable"` role), not a coloured overlay, so it reads as
 * "not your sky" in both themes without competing with anything drawn on
 * top. `null` when the observer's latitude leaves the whole sky reachable.
 *
 * Built as a Dec band across all RA rather than a polar polygon: the cap is
 * bounded by a parallel of declination, and a rectangle in (RA, Dec) is
 * exactly that region. Sampled every few degrees of RA so the frames'
 * projections curve it correctly instead of drawing four straight chords.
 */
export function unobservableCapShape(latitudeDeg: number, fovDeg: number | null): FovShape | null {
  const bound = unobservableCapBound(latitudeDeg);
  if (bound === null) return null;
  const step = gridStepDeg(fovDeg);
  const outerDec = bound.side === "below" ? -90 : 90;
  const vertices: [number, number][] = [];
  for (let ra = 0; ra <= 360; ra += step) vertices.push([ra % 360, bound.decDeg]);
  for (let ra = 360; ra >= 0; ra -= step) vertices.push([ra % 360, outerDec]);
  return {
    kind: "polygon",
    id: UNOBSERVABLE_CAP_ID,
    verticesRaDec: vertices,
    style: { role: "unobservable", fillOpacity: 0.72 },
  };
}

export const ALTITUDE_LIMIT_ID = "skyline:altitude-limit";

/**
 * A full constant-altitude circle across the whole sky at the user's
 * configured altitude limit (`planningUiStore`'s `altitudeLimitDeg`) — the
 * fixed `"danger"` (red) token always, regardless of anything else drawn.
 * Same `altAzToRaDec` rotation `horizonShape`/`altAzGridShapes` use, at an
 * arbitrary altitude rather than a fixed 0deg or the grid's 10deg steps.
 *
 * Deliberately distinct from `overlay.ts`'s `buildAltitudeLimitRingShape`:
 * that one is a short local marker at the *target's* own bearing, coloured
 * by how much margin the target currently has (`roleForMarginDeg` — keep,
 * warning or danger). This is the limit itself, a fixed reference line
 * independent of any target, the same way `horizonShape` is independent of
 * one — "here is the line", not "here is how close something is to it". */
export function altitudeLimitCircleShape(
  options: SiteInstant & { fovDeg: number | null; altitudeLimitDeg: number },
): FovShape {
  const step = gridStepDeg(options.fovDeg);
  const first = altAzToRaDec(options.altitudeLimitDeg, 0, options);
  const vertices: [number, number][] = [first];
  for (let az = step; az < 360; az += step) vertices.push(altAzToRaDec(options.altitudeLimitDeg, az, options));
  vertices.push(altAzToRaDec(options.altitudeLimitDeg, 360, options));
  return {
    kind: "polyline",
    id: ALTITUDE_LIMIT_ID,
    verticesRaDec: vertices,
    style: { role: "danger", dashed: true },
    label: `${options.altitudeLimitDeg.toFixed(0)}° altitude limit`,
    labelAt: first,
  };
}
