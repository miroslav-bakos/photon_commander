// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Catalogue outlines: the deep-sky objects in the visible field, drawn from
 * their catalogued size and position angle as `FovShape`s through the same
 * overlay path as the frame rectangles (`SkyMap.tsx` merges them).
 *
 * Nothing here is astronomy the server should own: size, PA and type come
 * straight from `GET /api/astro/objects` (`ssc.astro.catalog`); this file
 * only turns arcminutes and a position angle into vertices. The rule for
 * missing data is the catalogue's own — `None` means unknown, never zero —
 * so an object with no minor axis or no PA is drawn as a circle of its
 * major axis in the "size only" dotted style, never as an ellipse at a
 * guessed orientation.
 *
 * Position angle is measured from north through east (the catalogue's
 * convention and `ssc.astro.framing`'s): PA 0 is elongated along Dec, PA 90
 * along RA, and `catalogOutlines.test.ts` proves both against
 * `projection.ts`.
 */
import type { FieldObjectModel } from "../api/skymap";
import { rotateTangentOffset, tangentOffsetToWorld } from "./projection";
import type { FovShape, ShapeStyle } from "./types";

/** Vertices round an outline. 36 keeps a 2-degree galaxy smooth at any zoom
 * this map allows while a 200-object field stays a few thousand vertices. */
const ELLIPSE_SEGMENTS = 36;
/** Objects smaller than this fraction of the field of view get no label —
 * a field of fifty NGC galaxies would otherwise be fifty overlapping words. */
export const LABEL_MIN_FOV_FRACTION = 0.04;
/** Outlines are drawn at every zoom this map allows (`MAX_FOV_DEG`, raised
 * from 60 to 120 on 2026-09-09 — this constant follows it, or outlines would
 * silently stop at the old ceiling), on the owner's request: "make boundaries of objects visible
 * also on 60 degrees view zoom". A wide view is where you decide *what* to
 * image, so it is exactly where knowing what is out there matters.
 *
 * The old value was 20, guarding against "a texture of specks" — a real
 * concern, but the wrong instrument. Size, not zoom, is what makes an outline
 * useless: `OUTLINE_MIN_FOV_FRACTION` below drops anything too small to read
 * at the current field, so a 60-degree view shows the handful of large
 * objects that dominate it (M31, M42, the Veil) and none of the specks. */
export const OUTLINES_MAX_FOV_DEG = 120;

/** An outline smaller than this fraction of the field is not drawn: below it
 * an ellipse is a few pixels across and reads as dirt on the screen, not as
 * an object. At 60 degrees that keeps objects above ~0.6 degrees; at 1 degree
 * it keeps everything down to ~36 arcsec, so close-in framing is unchanged.
 *
 * Deliberately four times smaller than `LABEL_MIN_FOV_FRACTION`: an outline
 * worth drawing is much smaller than an outline worth *naming*, which is why
 * a wide field shows many shapes and few words. */
export const OUTLINE_MIN_FOV_FRACTION = 0.01;

export function catalogShapeId(designation: string, part: string): string {
  return `catalog:${designation}:${part}`;
}

/** Glyph per `ObjectType` (skymap-viz brief): what the line *style* says
 * about what the thing is, before any label is read. */
type Glyph =
  | { form: "ellipse"; style: ShapeStyle }
  | { form: "circle-cross"; style: ShapeStyle }
  | { form: "ring"; style: ShapeStyle };

function glyphFor(objectType: FieldObjectModel["object_type"]): Glyph {
  switch (objectType) {
    case "galaxy":
    case "galaxy_spiral":
    case "galaxy_elliptical":
    case "galaxy_lenticular":
    case "galaxy_irregular":
    case "galaxy_pair":
    case "galaxy_triplet":
    case "galaxy_group":
      return { form: "ellipse", style: { role: "catalog" } };
    case "open_cluster":
    case "stellar_association":
    case "asterism":
    case "star_cloud":
    case "cluster_with_nebula":
      return { form: "ellipse", style: { role: "catalog", dotted: true } };
    case "globular_cluster":
      return { form: "circle-cross", style: { role: "catalog" } };
    case "planetary_nebula":
      return { form: "ring", style: { role: "catalog" } };
    case "hii_region":
    case "emission_nebula":
    case "reflection_nebula":
    case "diffuse_nebula":
    case "supernova_remnant":
      return { form: "ellipse", style: { role: "catalog", dashed: true } };
    case "dark_nebula":
      return { form: "ellipse", style: { role: "catalog", dashed: true, fillOpacity: 0 } };
    default:
      return { form: "ellipse", style: { role: "catalog", dotted: true } };
  }
}

/**
 * The ellipse of semi-axes `aDeg` (major) and `bDeg` (minor) at position
 * angle `paDeg` (north through east) about (`raDeg`, `decDeg`), as world
 * vertices. `theta` = 0 is the major-axis end at the position angle, so the
 * first vertex is where a label belongs.
 */
export function ellipseWorldVertices(
  raDeg: number,
  decDeg: number,
  aDeg: number,
  bDeg: number,
  paDeg: number,
  segments = ELLIPSE_SEGMENTS,
): [ra: number, dec: number][] {
  const vertices: [number, number][] = [];
  for (let i = 0; i < segments; i += 1) {
    const theta = (i / segments) * 2 * Math.PI;
    // Unrotated: major axis along +y (north), minor along +x (east).
    const x = bDeg * Math.sin(theta);
    const y = aDeg * Math.cos(theta);
    const [rx, ry] = rotateTangentOffset(x, y, paDeg);
    vertices.push(tangentOffsetToWorld(raDeg, decDeg, rx, ry));
  }
  return vertices;
}

export interface CatalogOutlineOptions {
  /** Current field of view (viewport height), degrees — decides labels. */
  fovDeg: number;
}

/** One object's outline(s) plus, when it is big enough in the field, a label. */
export function buildCatalogOutlineShapes(
  objects: readonly FieldObjectModel[],
  options: CatalogOutlineOptions,
): FovShape[] {
  const shapes: FovShape[] = [];
  for (const object of objects) {
    if (object.size_major_arcmin === null) continue;
    const raDeg = object.ra_j2000_h * 15;
    const decDeg = object.dec_j2000_deg;
    const majorDeg = object.size_major_arcmin / 60;
    // Too small to read at this field — see `OUTLINE_MIN_FOV_FRACTION`. This
    // is what lets the FOV gate go all the way to 60 degrees without the map
    // filling with specks.
    if (majorDeg < options.fovDeg * OUTLINE_MIN_FOV_FRACTION) continue;
    const aDeg = majorDeg / 2;
    const glyph = glyphFor(object.object_type);
    const labelled = majorDeg >= options.fovDeg * LABEL_MIN_FOV_FRACTION;
    const label = labelled ? object.designation : undefined;

    // An orientation the catalogue did not give is not guessed: no minor
    // axis or no PA means a circle of the major axis, styled "size only".
    const oriented = object.size_minor_arcmin !== null && object.position_angle_deg !== null;
    const bDeg = oriented && object.size_minor_arcmin !== null ? object.size_minor_arcmin / 120 : aDeg;
    const paDeg = oriented && object.position_angle_deg !== null ? object.position_angle_deg : 0;
    const style: ShapeStyle = oriented ? glyph.style : { ...glyph.style, dashed: false, dotted: true };

    const outline = ellipseWorldVertices(raDeg, decDeg, aDeg, bDeg, paDeg);
    shapes.push({
      kind: "polygon",
      id: catalogShapeId(object.designation, "outline"),
      verticesRaDec: outline,
      style,
      label,
      labelAt: outline[0],
    });

    if (glyph.form === "circle-cross") {
      const arm = aDeg * 0.7;
      const bar = (id: string, dx: number, dy: number): FovShape => ({
        kind: "polyline",
        id: catalogShapeId(object.designation, id),
        verticesRaDec: [tangentOffsetToWorld(raDeg, decDeg, -dx, -dy), tangentOffsetToWorld(raDeg, decDeg, dx, dy)],
        style: { role: "catalog" },
      });
      shapes.push(bar("cross-ew", arm, 0), bar("cross-ns", 0, arm));
    } else if (glyph.form === "ring") {
      shapes.push({
        kind: "polygon",
        id: catalogShapeId(object.designation, "ring"),
        verticesRaDec: ellipseWorldVertices(raDeg, decDeg, aDeg * 0.6, bDeg * 0.6, paDeg),
        style,
      });
    }
  }
  return shapes;
}
