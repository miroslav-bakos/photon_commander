// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";
import { rectWorldCorners, screenToWorld, worldToScreen, type Viewport } from "../../projection";
import type { FovShape } from "../../types";
import {
  applyDrag,
  draggableShapeScreenPolygons,
  draggedShapeCenter,
  distanceToPolygonEdges,
  hitTestShapeScreen,
  isFrameFollowerShape,
  pointInPolygon,
  polygonCentroidRaDec,
  primaryShapeCenter,
  primaryShapeWorldPolygon,
  recenterFrameFollowers,
  translateFovShape,
  translateFovShapes,
  worldDelta,
} from "./fovDrag";

const SQUARE: [number, number][] = [
  [-10, -10],
  [10, -10],
  [10, 10],
  [-10, 10],
];

describe("pointInPolygon", () => {
  it("is true for a point inside the rectangle", () => {
    expect(pointInPolygon(0, 0, SQUARE)).toBe(true);
  });

  it("is false for a point outside the rectangle", () => {
    expect(pointInPolygon(50, 50, SQUARE)).toBe(false);
  });

  it("is false just outside an edge, true just inside it", () => {
    expect(pointInPolygon(9.9, 0, SQUARE)).toBe(true);
    expect(pointInPolygon(10.1, 0, SQUARE)).toBe(false);
  });
});

describe("hitTestShapeScreen", () => {
  it("grabs a point inside the shape with no padding needed", () => {
    expect(hitTestShapeScreen(SQUARE, 0, 0, 0)).toBe(true);
  });

  it("misses a point well outside the shape and its pad", () => {
    expect(hitTestShapeScreen(SQUARE, 40, 40, 5)).toBe(false);
  });

  it("grabs a point just outside the stroke when it falls within the touch pad", () => {
    // 2px outside the right edge (x = 10): outside the polygon itself, but
    // well within a 24px touch margin — this is exactly what the pad is for
    // on a touchscreen, where the 1-2px stroke alone is not a reliable target.
    expect(pointInPolygon(12, 0, SQUARE)).toBe(false);
    expect(hitTestShapeScreen(SQUARE, 12, 0, 24)).toBe(true);
    expect(distanceToPolygonEdges(12, 0, SQUARE)).toBeCloseTo(2, 9);
  });

  it("misses a point outside the shape and beyond the pad", () => {
    expect(hitTestShapeScreen(SQUARE, 40, 0, 24)).toBe(false);
  });
});

describe("primaryShapeWorldPolygon / draggableShapeScreenPolygons", () => {
  const rectShape: FovShape = {
    kind: "rect",
    id: "primary",
    centerRa: 83.7,
    centerDec: 22.1,
    widthDeg: 3.99,
    heightDeg: 2.24,
    positionAngleDeg: 15,
    style: { role: "primary" },
  };
  const otherShape: FovShape = {
    kind: "polygon",
    id: "ghost",
    verticesRaDec: [[10, 10], [11, 10], [11, 11], [10, 11]],
    style: { role: "ghost" },
  };

  it("picks out only the shape with id 'primary', never a ghost/union/keep shape alongside it", () => {
    const world = primaryShapeWorldPolygon([otherShape, rectShape]);
    expect(world).toEqual(
      rectWorldCorners(rectShape.centerRa, rectShape.centerDec, rectShape.widthDeg, rectShape.heightDeg, rectShape.positionAngleDeg),
    );
  });

  it("returns null when there is no primary shape", () => {
    expect(primaryShapeWorldPolygon([otherShape])).toBeNull();
  });

  it("projects the world polygon through the given viewport the same way worldToScreen does directly", () => {
    const viewport: Viewport = { view: { ra: 83.7, dec: 22.1, fovDeg: 3 }, widthPx: 800, heightPx: 600 };
    const screen = draggableShapeScreenPolygons([rectShape], viewport);
    const expected = rectWorldCorners(
      rectShape.centerRa,
      rectShape.centerDec,
      rectShape.widthDeg,
      rectShape.heightDeg,
      rectShape.positionAngleDeg,
    ).map(([ra, dec]) => worldToScreen(viewport, ra, dec));
    expect(screen).toEqual([expected]);
  });

  it("also accepts EQ's polygon-shaped primary", () => {
    const eqShape: FovShape = {
      kind: "polygon",
      id: "primary",
      verticesRaDec: [[1, 2], [3, 2], [3, 4], [1, 4]],
      style: { role: "primary" },
    };
    expect(primaryShapeWorldPolygon([eqShape])).toEqual(eqShape.verticesRaDec);
  });

  // Regression: PlanningTab.tsx filters the "primary" shape out of the
  // overlay whenever a genuine multi-panel mosaic is active
  // (`isMultiPanelMosaic`, "Planning map: hide the single-frame overlay for
  // a multi-panel mosaic"), landed a few hours after FOV dragging itself —
  // with nothing left carrying `id === "primary"`, the drag hit test found
  // nothing to grab and silently did nothing (reported live: "impossible to
  // drag ... mosaic to narrow down the framing"). `draggableShapeScreenPolygons`
  // must fall back to the mosaic panels' own outlines.
  describe("falls back to the mosaic panel outlines when there is no primary shape", () => {
    const panelA: FovShape = {
      kind: "polygon",
      id: "mosaic-0-0",
      verticesRaDec: [[10, 20], [11, 20], [11, 21], [10, 21]],
      style: { role: "grid" },
    };
    const panelB: FovShape = {
      kind: "polygon",
      id: "mosaic-0-1",
      verticesRaDec: [[11, 20], [12, 20], [12, 21], [11, 21]],
      style: { role: "grid" },
    };
    // A panel's own `-keep` shading shares the mosaic id prefix but must not
    // widen the hit-test box beyond the panels' real outlines.
    const panelAKeep: FovShape = {
      kind: "polygon",
      id: "mosaic-0-0-keep",
      verticesRaDec: [[50, 50], [51, 50], [51, 51], [50, 51]],
      style: { role: "grid" },
    };

    it("world polygon is null but each mosaic panel remains a separate screen hit area", () => {
      expect(primaryShapeWorldPolygon([panelA, panelB, panelAKeep])).toBeNull();
      const viewport: Viewport = { view: { ra: 11, dec: 20.5, fovDeg: 5 }, widthPx: 800, heightPx: 600 };
      const screen = draggableShapeScreenPolygons([panelA, panelB, panelAKeep], viewport);
      expect(screen).toEqual([
        panelA.verticesRaDec.map(([ra, dec]) => worldToScreen(viewport, ra, dec)),
        panelB.verticesRaDec.map(([ra, dec]) => worldToScreen(viewport, ra, dec)),
      ]);
    });

    it("does not grab empty sky merely because it lies inside the mosaic's overall bounding box", () => {
      const diagonalPanel: FovShape = {
        kind: "polygon",
        id: "mosaic-1-1",
        verticesRaDec: [[12, 22], [13, 22], [13, 23], [12, 23]],
        style: { role: "grid" },
      };
      const viewport: Viewport = { view: { ra: 11.5, dec: 21.5, fovDeg: 6 }, widthPx: 800, heightPx: 600 };
      const polygons = draggableShapeScreenPolygons([panelA, diagonalPanel], viewport);
      const [emptyX, emptyY] = worldToScreen(viewport, 12.5, 20.5);
      expect(polygons.some((polygon) => hitTestShapeScreen(polygon, emptyX, emptyY, 0))).toBe(false);
    });

    it("returns no hit areas when the overlay has neither a primary rectangle nor any mosaic panel", () => {
      const catalogOutline: FovShape = {
        kind: "polygon",
        id: "catalog:M31:outline",
        verticesRaDec: [[0, 0], [1, 0], [1, 1], [0, 1]],
        style: { role: "grid" },
      };
      expect(draggableShapeScreenPolygons([catalogOutline], { view: { ra: 0, dec: 0, fovDeg: 1 }, widthPx: 100, heightPx: 100 })).toEqual([]);
    });
  });
});

describe("polygonCentroidRaDec / primaryShapeCenter", () => {
  it("recovers the true centre of a symmetric rectangle built by rectWorldCorners", () => {
    const cases: [ra: number, dec: number][] = [
      [83.7, 22.1],
      [45, 60], // moderately high declination, still well short of the pole
      [359.9, -10], // across the RA wrap seam
    ];
    for (const [centerRa, centerDec] of cases) {
      const corners = rectWorldCorners(centerRa, centerDec, 4, 2.24, 30);
      const [ra, dec] = polygonCentroidRaDec(corners);
      // A loose tolerance, not exact: the small-angle mean over a 4deg-wide
      // rectangle drifts more as declination rises, which is this function's
      // own documented approximation, not a bug to chase here.
      expect(Math.abs(((ra - centerRa + 540) % 360) - 180)).toBeLessThan(0.1);
      expect(Math.abs(dec - centerDec)).toBeLessThan(0.1);
    }
  });

  it("reads centerRa/centerDec directly off a 'rect' primary, no averaging needed", () => {
    const shape: FovShape = {
      kind: "rect",
      id: "primary",
      centerRa: 200,
      centerDec: -30,
      widthDeg: 1,
      heightDeg: 1,
      positionAngleDeg: 0,
      style: { role: "primary" },
    };
    expect(primaryShapeCenter([shape])).toEqual([200, -30]);
  });

  // Regression, same cause as `draggableShapeScreenPolygons`' mosaic fallback
  // above: `endDrag` reports whatever `primaryShapeCenter` returns as the
  // session's new target RA/Dec, so a mosaic drag needs a real centre to
  // report even though no "primary" shape exists to read one off.
  it("falls back to the mosaic panels' own centroid when there is no primary shape", () => {
    const panelA: FovShape = {
      kind: "polygon",
      id: "mosaic-0-0",
      verticesRaDec: [[10, 20], [11, 20], [11, 21], [10, 21]],
      style: { role: "grid" },
    };
    const panelB: FovShape = {
      kind: "polygon",
      id: "mosaic-0-1",
      verticesRaDec: [[11, 20], [12, 20], [12, 21], [11, 21]],
      style: { role: "grid" },
    };
    const panelAKeep: FovShape = {
      kind: "polygon",
      id: "mosaic-0-0-keep",
      verticesRaDec: [[50, 50], [51, 50], [51, 51], [50, 51]],
      style: { role: "grid" },
    };
    expect(primaryShapeCenter([panelA, panelB, panelAKeep])).toEqual(
      polygonCentroidRaDec([...panelA.verticesRaDec, ...panelB.verticesRaDec]),
    );
  });

  it("returns null when there is nothing to drag at all", () => {
    const catalogOutline: FovShape = {
      kind: "polygon",
      id: "catalog:M31:outline",
      verticesRaDec: [[0, 0], [1, 0], [1, 1], [0, 1]],
      style: { role: "grid" },
    };
    expect(primaryShapeCenter([catalogOutline])).toBeNull();
    expect(primaryShapeCenter([])).toBeNull();
  });
});

describe("recenterFrameFollowers", () => {
  it("updates the host's framing mirror while leaving fixed catalogue shapes untouched", () => {
    const primary: FovShape = {
      kind: "rect",
      id: "primary",
      centerRa: 56.8,
      centerDec: 24.1,
      widthDeg: 4,
      heightDeg: 2,
      positionAngleDeg: 35,
      style: { role: "primary" },
    };
    const catalog: FovShape = {
      kind: "circle",
      id: "catalog:M45:outline",
      centerRa: 56.75,
      centerDec: 24.12,
      radiusDeg: 0.5,
      style: { role: "catalog" },
    };

    const moved = recenterFrameFollowers([primary, catalog], 150, -12.3);

    const movedPrimary = moved.find((shape) => shape.id === "primary");
    if (movedPrimary?.kind !== "rect") throw new Error("expected moved primary rectangle");
    expect(movedPrimary.centerRa).toBeCloseTo(150, 9);
    expect(movedPrimary.centerDec).toBeCloseTo(-12.3, 9);
    expect(moved).toContain(catalog);
  });

  it("also mirrors a multi-panel mosaic whose primary rectangle is absent", () => {
    const panels: FovShape[] = [
      {
        kind: "polygon",
        id: "mosaic-0-0",
        verticesRaDec: [[10, 20], [11, 20], [11, 21], [10, 21]],
        style: { role: "grid" },
      },
      {
        kind: "polygon",
        id: "mosaic-0-1",
        verticesRaDec: [[11, 20], [12, 20], [12, 21], [11, 21]],
        style: { role: "grid" },
      },
    ];

    const moved = recenterFrameFollowers(panels, 50, -5);
    const center = primaryShapeCenter(moved);
    expect(center?.[0]).toBeCloseTo(50, 9);
    expect(center?.[1]).toBeCloseTo(-5, 9);
  });
});

describe("worldDelta", () => {
  it("is the plain difference away from the RA wrap seam", () => {
    expect(worldDelta(10, 20, 12, 21)).toEqual({ dRaDeg: 2, dDecDeg: 1 });
  });

  it("takes the short way round the 0h/24h RA seam", () => {
    const { dRaDeg } = worldDelta(359, 0, 1, 0);
    expect(dRaDeg).toBeCloseTo(2, 9);
  });
});

describe("draggedShapeCenter", () => {
  it("moves the frame centre by exactly the pointer's screen delta even when grabbed off-centre", () => {
    const viewport: Viewport = { view: { ra: 40, dec: 70, fovDeg: 12 }, widthPx: 1000, heightPx: 700 };
    const baseCenter: [number, number] = [42, 72];
    const pointerDown: [number, number] = [710, 180];
    const pointerNow: [number, number] = [755, 225];
    const [movedRa, movedDec] = draggedShapeCenter(
      viewport,
      baseCenter[0],
      baseCenter[1],
      pointerDown[0],
      pointerDown[1],
      pointerNow[0],
      pointerNow[1],
    );
    const [baseX, baseY] = worldToScreen(viewport, baseCenter[0], baseCenter[1]);
    const [movedX, movedY] = worldToScreen(viewport, movedRa, movedDec);
    expect(movedX).toBeCloseTo(baseX + 45, 7);
    expect(movedY).toBeCloseTo(baseY + 45, 7);
  });

  it("does not move the centre at pointer-down", () => {
    const viewport: Viewport = { view: { ra: 180, dec: 0, fovDeg: 8 }, widthPx: 800, heightPx: 600 };
    const center = draggedShapeCenter(viewport, 181, 1, 100, 500, 100, 500);
    expect(center[0]).toBeCloseTo(181, 9);
    expect(center[1]).toBeCloseTo(1, 9);
  });

  it("reproduces the last rendered mobile drag position without needing release coordinates", () => {
    const viewport: Viewport = { view: { ra: 83.7, dec: 22.1, fovDeg: 2 }, widthPx: 390, heightPx: 640 };
    const lastRendered = draggedShapeCenter(viewport, 83.8, 22.2, 195, 320, 236, 283);
    const committed = draggedShapeCenter(viewport, 83.8, 22.2, 195, 320, 236, 283);
    const rogueMobileRelease = draggedShapeCenter(viewport, 83.8, 22.2, 195, 320, 0, 0);

    expect(committed[0]).toBeCloseTo(lastRendered[0], 12);
    expect(committed[1]).toBeCloseTo(lastRendered[1], 12);
    expect(Math.hypot(rogueMobileRelease[0] - committed[0], rogueMobileRelease[1] - committed[1])).toBeGreaterThan(0.5);
  });
});

describe("translateFovShape / translateFovShapes", () => {
  it("shifts a rect's centre and nothing else", () => {
    const shape: FovShape = {
      kind: "rect",
      id: "primary",
      centerRa: 10,
      centerDec: 20,
      widthDeg: 4,
      heightDeg: 2,
      positionAngleDeg: 15,
      style: { role: "primary" },
    };
    const moved = translateFovShape(shape, 1, -2);
    expect(moved).toEqual({ ...shape, centerRa: 11, centerDec: 18 });
  });

  it("shifts every vertex of a polygon by the same delta", () => {
    const shape: FovShape = {
      kind: "polygon",
      id: "keep",
      verticesRaDec: [[10, 10], [12, 10], [12, 12], [10, 12]],
      style: { role: "keep" },
    };
    const moved = translateFovShape(shape, 0.5, 0.5);
    if (moved.kind !== "polygon") throw new Error("expected polygon");
    expect(moved.verticesRaDec).toEqual([[10.5, 10.5], [12.5, 10.5], [12.5, 12.5], [10.5, 12.5]]);
  });

  it("shifts every frame stack sample's own centre", () => {
    const shape: FovShape = {
      kind: "frameStack",
      id: "stack",
      style: { role: "stack" },
      frames: [
        { atSeconds: 0, centerRa: 10, centerDec: 20, widthDeg: 4, heightDeg: 2, positionAngleDeg: 0 },
        { atSeconds: 3600, centerRa: 10, centerDec: 20, widthDeg: 4, heightDeg: 2, positionAngleDeg: 5 },
      ],
    };
    const moved = translateFovShape(shape, 1, 1);
    if (moved.kind !== "frameStack") throw new Error("expected frameStack");
    expect(moved.frames.map((f) => [f.centerRa, f.centerDec])).toEqual([[11, 21], [11, 21]]);
    // Everything else about each sample (including position angle — not
    // recomputed here, this is a display-only preview) is untouched.
    expect(moved.frames.map((f) => f.positionAngleDeg)).toEqual([0, 5]);
  });

  it("passes a position-free 'grid' shape through unchanged", () => {
    const shape: FovShape = { kind: "grid", id: "coverage", spacingDeg: 1, style: { role: "grid" } };
    expect(translateFovShape(shape, 5, 5)).toEqual(shape);
  });

  it("wraps RA at the 0h/24h seam and clamps Dec at the pole", () => {
    const shape: FovShape = {
      kind: "rect",
      id: "primary",
      centerRa: 359,
      centerDec: 89,
      widthDeg: 1,
      heightDeg: 1,
      positionAngleDeg: 0,
      style: { role: "primary" },
    };
    const moved = translateFovShape(shape, 3, 5);
    if (moved.kind !== "rect") throw new Error("expected rect");
    expect(moved.centerRa).toBeCloseTo(2, 9);
    expect(moved.centerDec).toBe(90);
  });

  it("translateFovShapes moves the whole overlay set together, e.g. primary and its ghost stay the same distance apart", () => {
    const primary: FovShape = {
      kind: "rect",
      id: "primary",
      centerRa: 10,
      centerDec: 20,
      widthDeg: 4,
      heightDeg: 2,
      positionAngleDeg: 0,
      style: { role: "primary" },
    };
    const ghost: FovShape = {
      kind: "rect",
      id: "ghost",
      centerRa: 10.5,
      centerDec: 20.2,
      widthDeg: 4,
      heightDeg: 2,
      positionAngleDeg: 10,
      style: { role: "ghost" },
    };
    const [movedPrimary, movedGhost] = translateFovShapes([primary, ghost], 2, -1) as [FovShape, FovShape];
    if (movedPrimary.kind !== "rect" || movedGhost.kind !== "rect") throw new Error("expected rects");
    expect(movedGhost.centerRa - movedPrimary.centerRa).toBeCloseTo(ghost.centerRa - primary.centerRa, 9);
    expect(movedGhost.centerDec - movedPrimary.centerDec).toBeCloseTo(ghost.centerDec - primary.centerDec, 9);
  });
});

describe("screen-delta -> sky-delta round trip, a couple of FOV sizes and declinations", () => {
  for (const [label, view] of [
    ["wide field near the equator", { ra: 180, dec: 0, fovDeg: 8 }],
    ["S30 Pro tele-ish field at moderate declination", { ra: 83.7, dec: 22.1, fovDeg: 2 }],
    ["narrow field at high declination", { ra: 40, dec: 70, fovDeg: 1 }],
  ] as const) {
    it(`recovers a known screen-pixel drag as the equivalent worldDelta (${label})`, () => {
      const viewport: Viewport = { view, widthPx: 800, heightPx: 600 };
      const [fromRa, fromDec] = screenToWorld(viewport, 400, 300);
      const toX = 420;
      const toY = 280;
      const [toRa, toDec] = screenToWorld(viewport, toX, toY);
      const { dRaDeg, dDecDeg } = worldDelta(fromRa, fromDec, toRa, toDec);
      // Applying the recovered delta to the anchor's own screen point lands
      // back near the drag's actual destination — "near", not exact, is the
      // small-angle approximation this module's own doc comment states.
      const shape: FovShape = {
        kind: "rect",
        id: "primary",
        centerRa: fromRa,
        centerDec: fromDec,
        widthDeg: 0.01,
        heightDeg: 0.01,
        positionAngleDeg: 0,
        style: { role: "primary" },
      };
      const moved = translateFovShape(shape, dRaDeg, dDecDeg);
      if (moved.kind !== "rect") throw new Error("expected rect");
      const [backX, backY] = worldToScreen(viewport, moved.centerRa, moved.centerDec);
      expect(backX).toBeCloseTo(toX, 1);
      expect(backY).toBeCloseTo(toY, 1);
    });
  }
});

describe("isFrameFollowerShape / applyDrag", () => {
  const primary: FovShape = {
    kind: "rect",
    id: "primary",
    centerRa: 10,
    centerDec: 20,
    widthDeg: 4,
    heightDeg: 2,
    positionAngleDeg: 0,
    style: { role: "primary" },
  };
  const ghost: FovShape = { ...primary, kind: "rect", id: "ghost", positionAngleDeg: 10 };
  const union: FovShape = { kind: "polygon", id: "union", verticesRaDec: [[9, 19], [11, 19], [11, 21], [9, 21]], style: { role: "union" } };
  const keep: FovShape = { kind: "polygon", id: "keep", verticesRaDec: [[9.5, 19.5], [10.5, 19.5], [10.5, 20.5], [9.5, 20.5]], style: { role: "keep" } };
  const mosaicOutline: FovShape = { kind: "polygon", id: "mosaic-0-0", verticesRaDec: [[10, 20], [11, 20], [11, 21], [10, 21]], style: { role: "grid" } };
  const mosaicKeep: FovShape = { kind: "polygon", id: "mosaic-0-0-keep", verticesRaDec: [[10, 20], [10.5, 20], [10.5, 20.5], [10, 20.5]], style: { role: "grid" } };
  const stack: FovShape = {
    kind: "frameStack",
    id: "stack",
    style: { role: "stack" },
    frames: [{ atSeconds: 0, centerRa: 10, centerDec: 20, widthDeg: 4, heightDeg: 2, positionAngleDeg: 5 }],
  };
  const horizonMarker: FovShape = { kind: "polyline", id: "altitude-limit-ring", verticesRaDec: [[10, 20], [10.1, 20.1]], style: { role: "keep" } };
  const catalogOutline: FovShape = { kind: "polygon", id: "catalog:m32:outline", verticesRaDec: [[15, 25], [16, 25], [16, 26], [15, 26]], style: { role: "catalog" } };

  it("accepts exactly the target's own framing shapes — primary, frame stack, ghost, swept envelope, keep-region, mosaic panels", () => {
    for (const shape of [primary, stack, ghost, union, keep, mosaicOutline, mosaicKeep]) {
      expect(isFrameFollowerShape(shape)).toBe(true);
    }
  });

  it("rejects horizon markers and catalogue outlines of other objects", () => {
    for (const shape of [horizonMarker, catalogOutline]) {
      expect(isFrameFollowerShape(shape)).toBe(false);
    }
  });

  it("applyDrag moves only the follower shapes and leaves everything else byte-for-byte unchanged", () => {
    const all = [primary, ghost, union, keep, mosaicOutline, mosaicKeep, stack, horizonMarker, catalogOutline];
    const result = applyDrag(all, 1, 1);

    const movedPrimary = result.find((s) => s.id === "primary");
    if (movedPrimary?.kind !== "rect") throw new Error("expected rect");
    expect(movedPrimary.centerRa).toBe(11);
    expect(movedPrimary.centerDec).toBe(21);

    const movedStack = result.find((s) => s.id === "stack");
    if (movedStack?.kind !== "frameStack") throw new Error("expected moved frame stack");
    expect(movedStack.frames[0]).toEqual(expect.objectContaining({ centerRa: 11, centerDec: 21 }));

    // Non-follower shapes come back as the exact same object — not just
    // equal, unchanged — which is what lets `DiffedOverlay` treat them as a
    // no-op every pointer-move frame instead of a pointless remove+re-add.
    expect(result.find((s) => s.id === "altitude-limit-ring")).toBe(horizonMarker);
    expect(result.find((s) => s.id === "catalog:m32:outline")).toBe(catalogOutline);
  });

  it("applyDrag with a zero delta still returns every shape (nothing silently dropped)", () => {
    const all = [primary, ghost, stack, catalogOutline];
    expect(applyDrag(all, 0, 0).map((s) => s.id).sort()).toEqual(all.map((s) => s.id).sort());
  });
});

function angularEdge(a: readonly number[], b: readonly number[]): number {
  const vector = (point: readonly number[]) => {
    const ra = (point[0] ?? 0) * Math.PI / 180;
    const dec = (point[1] ?? 0) * Math.PI / 180;
    return [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)] as const;
  };
  const u = vector(a); const v = vector(b);
  const cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
  return Math.atan2(Math.hypot(...cross), u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) * 180 / Math.PI;
}

it("recovers the true footprint center near Polaris", () => {
  const vertices = rectWorldCorners(37.95, 89.26, 1.29, 0.73, 32);
  const center = polygonCentroidRaDec(vertices);
  expect(center[0]).toBeCloseTo(37.95, 9);
  expect(center[1]).toBeCloseTo(89.26, 9);
});

it.each([[359.8, 88.5], [190, 89.9], [0.2, -89.9]])(
  "preserves angular footprint edges and diagonal while dragging near poles to %s %s",
  (ra, dec) => {
    const vertices = rectWorldCorners(37.95, 89.26, 1.29, 0.73, 32);
    const shape: FovShape = { kind: "polygon", id: "primary", verticesRaDec: vertices, style: { role: "primary" } };
    const result = recenterFrameFollowers([shape], ra, dec)[0];
    if (result?.kind !== "polygon") throw new Error("Framing polygon missing");
    for (const [a, b] of [[0, 1], [1, 2], [2, 3], [3, 0], [0, 2]] as const) {
      expect(angularEdge(result.verticesRaDec[a] ?? [], result.verticesRaDec[b] ?? [])).toBeCloseTo(angularEdge(vertices[a] ?? [], vertices[b] ?? []), 9);
    }
  },
);

it.each([[359.8, 89.9], [0.2, -89.9]])("retains primary angular size and PA while moving companion footprints to %s %s", (ra, dec) => {
  const primary: FovShape = { kind: "rect", id: "primary", centerRa: 37.95, centerDec: 89.26,
    widthDeg: 1.29, heightDeg: 0.73, positionAngleDeg: 32, style: { role: "primary" } };
  const ghost: FovShape = { kind: "polygon", id: "ghost", verticesRaDec: rectWorldCorners(37.95, 89.26, 1.29, 0.73, 32), style: { role: "ghost" } };
  const fixed: FovShape = { kind: "circle", id: "catalog:fixed", centerRa: 38, centerDec: 88,
    radiusDeg: 0.1, style: { role: "catalog" } };
  const moved = recenterFrameFollowers([primary, ghost, fixed], ra, dec);
  expect(moved[0]).toEqual({ ...primary, centerRa: ra, centerDec: dec });
  const movedGhost = moved[1];
  if (movedGhost?.kind !== "polygon") throw new Error("Ghost missing");
  const expected = rectWorldCorners(ra, dec, 1.29, 0.73, 32);
  for (let i = 0; i < 4; i++) expect(angularEdge(movedGhost.verticesRaDec[i] ?? [], expected[i] ?? [])).toBeLessThan(1e-9);
  expect(moved[2]).toBe(fixed);
});

it.each([[359.8, 89.9], [0.2, -89.9]])("transports offset rectangles and frame-stack PA using independent gnomonic projections at %s %s", (ra, dec) => {
  const from: Viewport = { view: { ra: 37.95, dec: 89.26, fovDeg: 6 }, widthPx: 1200, heightPx: 1200 };
  const to: Viewport = { ...from, view: { ra, dec, fovDeg: 6 } };
  const a = screenToWorld(from, 530, 670); const b = screenToWorld(from, 640, 550);
  const primary: FovShape = { kind: "rect", id: "primary", centerRa: from.view.ra, centerDec: from.view.dec,
    widthDeg: 1.29, heightDeg: 0.73, positionAngleDeg: 32, style: { role: "primary" } };
  const ghost: FovShape = { ...primary, id: "ghost", centerRa: a[0], centerDec: a[1], positionAngleDeg: 61 };
  const stack: FovShape = { kind: "frameStack", id: "stack", style: { role: "stack" }, frames: [
    { atSeconds: 0, centerRa: a[0], centerDec: a[1], widthDeg: 0.8, heightDeg: 0.4, positionAngleDeg: 61 },
    { atSeconds: 60, centerRa: b[0], centerDec: b[1], widthDeg: 0.8, heightDeg: 0.4, positionAngleDeg: -17 },
  ] };
  const moved = recenterFrameFollowers([primary, ghost, stack], ra, dec);
  const movedGhost = moved[1]; const movedStack = moved[2];
  if (movedGhost?.kind !== "rect" || movedStack?.kind !== "frameStack") throw new Error("Frames missing");
  const originalFrames = [ghost, ...stack.frames]; const movedFrames = [movedGhost, ...movedStack.frames];
  for (let i = 0; i < originalFrames.length; i++) {
    const old = originalFrames[i]; const actual = movedFrames[i];
    if (old === undefined || actual === undefined) throw new Error("Frame missing");
    const expectedCenter = screenToWorld(to, ...worldToScreen(from, old.centerRa, old.centerDec));
    expect(angularEdge([actual.centerRa, actual.centerDec], expectedCenter)).toBeLessThan(1e-9);
    const oldCorners = rectWorldCorners(old.centerRa, old.centerDec, old.widthDeg, old.heightDeg, old.positionAngleDeg);
    const actualCorners = rectWorldCorners(actual.centerRa, actual.centerDec, actual.widthDeg, actual.heightDeg, actual.positionAngleDeg);
    for (let j = 0; j < 4; j++) {
      const corner = oldCorners[j]; if (corner === undefined) throw new Error("Corner missing");
      const expected = screenToWorld(to, ...worldToScreen(from, ...corner));
      expect(angularEdge(actualCorners[j] ?? [], expected)).toBeLessThan(1e-9);
    }
  }
});
