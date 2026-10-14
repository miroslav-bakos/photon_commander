// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * schema_version 2 (`ssc.plan.models`, 2026-09-10, DESIGN.md §3.6): a plan
 * item's target is a discriminated `TargetSpec`, never the flat
 * `target_name`/`ra_j2000_h`/`dec_j2000_deg` this editor used to write.
 * These tests exist because the flat shape used to typecheck against a
 * stale generated `schema.d.ts` even though the server had already moved
 * on — the exact trap this dispatch's brief calls out — so they assert the
 * *wire JSON shape* an authored item produces, not just that some object
 * compiles.
 */
import { describe, expect, it } from "vitest";
import type { PlanItem } from "../../api/plans";
import { ADVANCED_ITEM_KINDS, ITEM_KINDS, ITEM_KIND_LABELS, NORMAL_ITEM_KINDS, fieldsFor, itemHasTarget, itemSummary, newItem, newM31FirstLightPlan, type FieldDef, type SelectFieldDef } from "./itemKinds";

it("names the telescope-controlled GoTo and stack action plainly", () => {
  expect(ITEM_KIND_LABELS.goto_and_stack).toBe("Telescope GoTo + live stack");
});

it("partitions every scheduler item into normal or Advanced without overlap", () => {
  expect(new Set([...NORMAL_ITEM_KINDS, ...ADVANCED_ITEM_KINDS])).toEqual(new Set(ITEM_KINDS));
  expect(NORMAL_ITEM_KINDS.filter((kind) => (ADVANCED_ITEM_KINDS as readonly string[]).includes(kind))).toEqual([]);
});

function selectField(item: PlanItem, key: string): SelectFieldDef {
  const field = fieldsFor(item).find((f) => f.key === key);
  if (field === undefined) throw new Error(`no field ${key} on ${item.kind}`);
  if (field.type !== "select") throw new Error(`field ${key} is not a select`);
  return field;
}

function typedField<T extends FieldDef["type"]>(item: PlanItem, key: string, type: T): Extract<FieldDef, { type: T }> {
  const field = fieldsFor(item).find((f) => f.key === key);
  if (field === undefined) throw new Error(`no field ${key} on ${item.kind}`);
  if (field.type !== type) throw new Error(`field ${key} is a ${field.type}, not ${type}`);
  return field as Extract<FieldDef, { type: T }>;
}

describe("authoring a target — JSON shape", () => {
  it("keeps explicitly saved five-point PA geometry when editing other fields", () => {
    const freshPolar = newItem("polar_alignment", "pa", { kind: "asap" });
    const freshStartup = newItem("startup", "startup", { kind: "asap" });
    if (freshPolar.kind !== "polar_alignment" || freshStartup.kind !== "startup") throw new Error("wrong item kind");
    let polar: PlanItem = { ...freshPolar, point_count: 5, step_deg: 15 };
    let startup: PlanItem = { ...freshStartup, polar_point_count: 5, polar_step_deg: 15 };
    expect(typedField(polar, "point_count", "number").read(polar)).toBe(5);
    expect(typedField(startup, "polar_point_count", "number").read(startup)).toBe(5);
    polar = typedField(polar, "exposure_s", "number").write(polar, 4);
    startup = typedField(startup, "polar_exposure_s", "number").write(startup, 4);
    expect(polar).toMatchObject({ point_count: 5, step_deg: 15 });
    expect(startup).toMatchObject({ polar_point_count: 5, polar_step_deg: 15 });
  });

  it.each(["take_exposures", "goto_and_stack", "mosaic" ] as const)("uses six-frame dithering when authoring or re-enabling %s", (kind) => {
    let item = newItem(kind, "science", { kind: "asap" });
    expect(item).toMatchObject({ dither: { every_n_frames: 6 } });
    const enabled = typedField(item, "dither.enabled", "boolean");
    item = enabled.write(item, false);
    expect(item).toMatchObject({ dither: null });
    item = enabled.write(item, true);
    expect(item).toMatchObject({ dither: { every_n_frames: 6 } });
  });

  it("uses the same polar-alignment defaults in direct and startup plan items", () => {
    let polar = newItem("polar_alignment", "pa", { kind: "asap" });
    expect(polar).toMatchObject({
      point_count: 3,
      step_deg: 30,
      exposure_s: 2,
    });
    const polarStart = selectField(polar, "start_point");
    expect(polarStart.options).toContainEqual({ value: "seestar", label: "Seestar field" });
    polar = polarStart.write(polar, "seestar");
    expect(polar).toMatchObject({ start_point: "seestar" });

    let startup = newItem("startup", "startup", { kind: "asap" });
    expect(startup).toMatchObject({
      polar_point_count: 3,
      polar_step_deg: 30,
      polar_exposure_s: 2,
    });
    const startupStart = selectField(startup, "polar_start_point");
    expect(startupStart.options).toContainEqual({ value: "seestar", label: "Seestar field" });
    startup = startupStart.write(startup, "seestar");
    expect(startup).toMatchObject({ polar_start_point: "seestar" });
  });

  it("a fresh catalogue target serialises as a discriminated CatalogTargetSpec", () => {
    const item = newItem("goto_and_stack", "a", { kind: "asap" });
    expect(item).toMatchObject({
      kind: "goto_and_stack",
    live_stack: true,
      target: { kind: "catalog", name: null, ra_j2000_h: 0, dec_j2000_deg: 0 },
    });
    // The old flat fields must not be on the wire at all — they no longer
    // exist on the server's `GotoAndStackItem` (schema_version 2).
    const json = JSON.parse(JSON.stringify(item)) as Record<string, unknown>;
    expect(json.target_name).toBeUndefined();
    expect(json.ra_j2000_h).toBeUndefined();
    expect(json.dec_j2000_deg).toBeUndefined();
  });

  it("switching a goto_and_stack item's target type to body serialises as a discriminated BodyTargetSpec", () => {
    let item = newItem("goto_and_stack", "a", { kind: "asap" });
    const kindField = selectField(item, "target.kind");
    item = kindField.write(item, "body");
    expect(item).toMatchObject({ kind: "goto_and_stack", target: { kind: "body", body: "moon" } });

    const bodyField = selectField(item, "target.body");
    item = bodyField.write(item, "jupiter");
    const json = JSON.parse(JSON.stringify(item)) as Record<string, unknown>;
    expect(json.target).toEqual({ kind: "body", body: "jupiter" });
    expect(json.target_name).toBeUndefined();
    expect(json.ra_j2000_h).toBeUndefined();
    expect(json.dec_j2000_deg).toBeUndefined();
  });

  it.each(["goto_and_stack", "mosaic"] as const)(
    "%s owns editable exposure, gain and filter settings",
    (kind) => {
      let item = newItem(kind, "image", { kind: "asap" });
      item = typedField(item, "exposure_sec", "number").write(item, 23);
      item = typedField(item, "gain", "number").write(item, 117);
      item = selectField(item, "filter_name").write(item, "LP");

      expect(item).toMatchObject({ exposure_sec: 23, gain: 117, filter_name: "LP" });
    },
  );

  it.each(["goto_and_stack", "mosaic", "take_exposures", "set_exposure"] as const)(
    "new %s items default to 60-second exposures",
    (kind) => {
      const fresh = newItem(kind, "new", { kind: "asap" });
      expect("exposure_sec" in fresh && fresh.exposure_sec).toBe(60);
    },
  );

  it("switching back to catalogue from body produces a fresh, editable CatalogTargetSpec", () => {
    let item = newItem("slew", "a", { kind: "asap" });
    item = selectField(item, "target.kind").write(item, "body");
    item = selectField(item, "target.body").write(item, "saturn");
    item = selectField(item, "target.kind").write(item, "catalog");
    expect(item).toMatchObject({ target: { kind: "catalog", name: null, ra_j2000_h: 0, dec_j2000_deg: 0 } });
  });

  it("offers the polar-alignment best field as a Slew destination", () => {
    let item = newItem("slew", "a", { kind: "asap" });
    item = selectField(item, "destination").write(item, "polar_best");

    expect(itemSummary(item)).toBe("Polar-alignment best field");
    expect(itemHasTarget(item)).toBe(false);
    expect(fieldsFor(item).map((field) => field.key)).toEqual([
      "destination",
      "unpark_before",
    ]);
  });

  it("a sync item defaults to no target, and can be set to catalog, body, or back to none", () => {
    let item = newItem("sync", "a", { kind: "asap" });
    expect(item).toMatchObject({ kind: "sync", target: null });
    expect(itemHasTarget(item)).toBe(false);

    item = selectField(item, "target.kind").write(item, "body");
    expect(item).toMatchObject({ target: { kind: "body", body: "moon" } });
    expect(itemHasTarget(item)).toBe(true);

    item = selectField(item, "target.kind").write(item, "none");
    expect(item).toMatchObject({ target: null });
    expect(itemHasTarget(item)).toBe(false);
  });

  it("the Sun is never offered as a selectable body", () => {
    let item = newItem("goto_and_stack", "a", { kind: "asap" });
    item = selectField(item, "target.kind").write(item, "body");
    const bodyOptions = selectField(item, "target.body").options ?? [];
    expect(bodyOptions.map((o) => o.value)).not.toContain("sun");
    expect(bodyOptions.map((o) => o.value)).toEqual([
      "moon",
      "mercury",
      "venus",
      "mars",
      "jupiter",
      "saturn",
      "uranus",
      "neptune",
    ]);
  });
});

describe("round-trip: an item loaded from a v2 plan renders its target correctly", () => {
  it("a catalogue target round-trips as its name/coordinates, not a body", () => {
    const item: PlanItem = {
      id: "a",
      kind: "goto_and_stack",
    live_stack: true,
      start: { kind: "asap" },
      end: null,
      enabled: true,
      target: { kind: "catalog", name: "M31", ra_j2000_h: 0.712, dec_j2000_deg: 41.27 },
      framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
      constraints: {},
      autofocus_before: false,
      exposure_sec: 10,
      gain: 200,
      filter_name: null,
    };
    expect(itemSummary(item)).toBe("M31");
    expect(selectField(item, "target.kind").read(item)).toBe("catalog");
    const raField = fieldsFor(item).find((f) => f.key === "target.ra_j2000_h");
    expect(raField?.read(item)).toBe(0.712);
    const nameField = fieldsFor(item).find((f) => f.key === "target.name");
    expect(nameField?.read(item)).toBe("M31");
    expect(itemSummary(nameField?.write(item, "M31 panel A") ?? item)).toBe("M31 panel A");
    const autofocusField = fieldsFor(item).find((f) => f.key === "autofocus_before");
    expect(autofocusField?.read(item)).toBe(false);
    expect(autofocusField?.read(autofocusField.write(item, true))).toBe(true);
  });

  it("a body target round-trips as a body — never as coordinates it does not have", () => {
    const item: PlanItem = {
      id: "b",
      kind: "goto_and_stack",
    live_stack: true,
      start: { kind: "asap" },
      end: null,
      enabled: true,
      target: { kind: "body", body: "jupiter" },
      framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
      constraints: {},
      autofocus_before: false,
      exposure_sec: 10,
      gain: 200,
      filter_name: null,
    };
    // Visibly a body, per DESIGN.md §3.6 — not "0.000 h, 0.00°" or any
    // other stand-in for a position this item does not carry.
    expect(itemSummary(item)).toBe("Jupiter (body)");
    expect(selectField(item, "target.kind").read(item)).toBe("body");
    // No RA/Dec/name fields are offered for a body target at all.
    const fields = fieldsFor(item).map((f) => f.key);
    expect(fields).not.toContain("target.ra_j2000_h");
    expect(fields).not.toContain("target.dec_j2000_deg");
    expect(fields).not.toContain("target.name");
    expect(fields).toContain("target.body");
  });

  it("a v1-shaped mosaic item migrated server-side (flat fields folded into target) round-trips once loaded", () => {
    // What `parse_plan`'s `_flat_target_alias` produces server-side for a v1
    // document — the client only ever sees the v2 shape once a plan is
    // fetched, but this pins that a migrated catalogue target reads the
    // same as one authored fresh.
    const item: PlanItem = {
      id: "c",
      kind: "mosaic",
    live_stack: true,
      start: { kind: "asap" },
      end: null,
      enabled: true,
      target: { kind: "catalog", name: "Sh2-155", ra_j2000_h: 22.15, dec_j2000_deg: 62.6 },
      framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
      mosaic: { ra_num: 3, dec_num: 2, overlap_pct: 10 },
      constraints: {},
      exposure_sec: 10,
      gain: 200,
      filter_name: null,
    };
    expect(itemSummary(item)).toBe("Sh2-155");
  });
});

describe("take_exposures — the exposure block (DESIGN.md §4.3.0, schema_version 3)", () => {
  it("a fresh block is a self-contained target imaging block", () => {
    const item = newItem("take_exposures", "e1", { kind: "asap" });
    expect(item).toMatchObject({
      kind: "take_exposures",
      count: { kind: "fixed", frames: 60 },
      exposure_sec: 60,
      gain: 200,
      filter_name: null,
      frame_type: "light",
      dither: { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 6 },
      target: { kind: "catalog", name: null, ra_j2000_h: 0, dec_j2000_deg: 0 },
      framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
      constraints: { min_alt_deg: null, min_moon_sep_deg: null },
      live_stack: true,
    });
    const json = JSON.parse(JSON.stringify(item)) as Record<string, unknown>;
    expect(Object.keys(json).sort()).toEqual(
      ["id", "start", "end", "enabled", "kind", "target", "count", "exposure_sec", "gain", "filter_name", "frame_type", "dither", "center_after_drift", "framing", "constraints", "live_stack"].sort(),
    );
  });

  it("switching to 'until the block ends' and back to a fixed count is a visible either/or, not a checkbox on frames", () => {
    let item = newItem("take_exposures", "e1", { kind: "asap" });
    const countKind = selectField(item, "count.kind");
    expect(countKind.read(item)).toBe("fixed");
    expect(fieldsFor(item).map((f) => f.key)).toContain("count.frames");

    item = countKind.write(item, "until_block_end");
    expect(item).toMatchObject({ count: { kind: "until_block_end" } });
    // The frame-count field disappears entirely once the block is
    // open-ended — there is nothing to type a count into.
    expect(fieldsFor(item).map((f) => f.key)).not.toContain("count.frames");
    const json = JSON.parse(JSON.stringify(item)) as { count: Record<string, unknown> };
    expect(json.count).toEqual({ kind: "until_block_end" });
    expect(json.count.frames).toBeUndefined();

    item = countKind.write(item, "fixed");
    expect(item).toMatchObject({ count: { kind: "fixed", frames: 60 } });
  });

  it("editing the frame count only applies while the count is fixed", () => {
    let item = newItem("take_exposures", "e1", { kind: "asap" });
    const frames = typedField(item, "count.frames", "number");
    item = frames.write(item, 120);
    expect(item).toMatchObject({ count: { kind: "fixed", frames: 120 } });
  });

  it("exposure time, gain, filter and frame type round-trip through their fields", () => {
    let item = newItem("take_exposures", "e1", { kind: "asap" });
    item = typedField(item, "exposure_sec", "number").write(item, 30);
    item = typedField(item, "gain", "number").write(item, 200);
    item = typedField(item, "filter_name", "select").write(item, "LP");
    item = typedField(item, "frame_type", "select").write(item, "dark");
    expect(item).toMatchObject({ exposure_sec: 30, gain: 200, filter_name: "LP", frame_type: "dark" });
    expect(typedField(item, "exposure_sec", "number").read(item)).toBe(30);
    expect(typedField(item, "gain", "number").read(item)).toBe(200);
    expect(typedField(item, "filter_name", "select").read(item)).toBe("LP");
    expect(typedField(item, "filter_name", "select").dynamic).toBe("filter");
    expect(typedField(item, "frame_type", "select").read(item)).toBe("dark");
    expect(itemSummary(item)).toBe("0.000 h, 0.00° · 60 × 30 s dark through LP, gain 200 · live stack");
  });

  it("clearing the filter name stores null, not an empty string — the server's own 'sets no filter' warning depends on this", () => {
    let item = newItem("take_exposures", "e1", { kind: "asap" });
    item = typedField(item, "filter_name", "select").write(item, "LP");
    item = typedField(item, "filter_name", "select").write(item, "");
    const json = JSON.parse(JSON.stringify(item)) as Record<string, unknown>;
    expect(json.filter_name).toBeNull();
  });

  it("dithering is on by default and is a whole DitherSpec object that can be disabled", () => {
    let item = newItem("take_exposures", "e1", { kind: "asap" });
    const enabled = typedField(item, "dither.enabled", "boolean");
    expect(enabled.read(item)).toBe(true);
    expect(item).toMatchObject({ dither: { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 6 } });
    expect(fieldsFor(item).map((f) => f.key)).toEqual(expect.arrayContaining(["dither.min_pixels", "dither.max_pixels", "dither.every_n_frames"]));

    item = enabled.write(item, false);
    expect(item).toMatchObject({ dither: null });
    expect(fieldsFor(item).map((f) => f.key)).not.toContain("dither.pixels");

    item = enabled.write(item, true);
    expect(item).toMatchObject({ dither: { pixels: null, min_pixels: 15, max_pixels: 200, every_n_frames: 6 } });

    item = typedField(item, "dither.min_pixels", "number").write(item, 8);
    item = typedField(item, "dither.max_pixels", "number").write(item, 18);
    item = typedField(item, "dither.every_n_frames", "number").write(item, 3);
    expect(item).toMatchObject({ dither: { pixels: null, min_pixels: 8, max_pixels: 18, every_n_frames: 3 } });

    item = enabled.write(item, false);
    expect(item).toMatchObject({ dither: null });
  });

  it("round-trips a stored block with every field set, unchanged", () => {
    const stored: PlanItem = {
      id: "e2",
      kind: "take_exposures",
      start: { kind: "asap" },
      end: { kind: "duration", minutes: 90 },
      enabled: true,
      count: { kind: "until_block_end" },
      exposure_sec: 20,
      gain: 150,
      filter_name: "IR",
      frame_type: "flat",
      dither: { pixels: 5, every_n_frames: 4 },
      target: null,
      framing: { mode: "altaz", pa_offset_deg: 0, lens: "tele" },
      constraints: { min_alt_deg: null, min_moon_sep_deg: null },
      live_stack: false,
    };
    for (const field of fieldsFor(stored)) {
      // Every field the editor renders for this item reads back exactly
      // what is already on it — the round-trip the editor promises on open.
      if (field.key === "count.kind") expect(field.read(stored)).toBe("until_block_end");
    }
    expect(typedField(stored, "exposure_sec", "number").read(stored)).toBe(20);
    expect(typedField(stored, "gain", "number").read(stored)).toBe(150);
    expect(typedField(stored, "filter_name", "select").read(stored)).toBe("IR");
    expect(typedField(stored, "frame_type", "select").read(stored)).toBe("flat");
    expect(typedField(stored, "dither.pixels", "number").read(stored)).toBe(5);
    expect(typedField(stored, "dither.every_n_frames", "number").read(stored)).toBe(4);
    const json = JSON.parse(JSON.stringify(stored)) as PlanItem;
    expect(json).toEqual(stored);
  });

  it("itemHasTarget is true — the reusable image block owns its target", () => {
    const item = newItem("take_exposures", "e1", { kind: "asap" });
    expect(itemHasTarget(item)).toBe(true);
  });
});

it("creates the complete M31 first-light plan without hand-edited JSON", () => {
  const plan = newM31FirstLightPlan();
  expect(plan.schema_version).toBe(8);
  expect(plan.items.map((item) => item.kind)).toEqual([
    "polar_alignment", "wait", "take_exposures", "capture_calibration", "shutdown",
  ]);
  expect(plan.items[1]).toMatchObject({ requires_confirmation: true });
  expect(plan.items[2]).toMatchObject({
    target: { kind: "catalog", name: "M31" },
    framing: { mode: "eq", lens: "tele" },
    exposure_sec: 60,
    gain: 200,
    filter_name: "LP",
    count: { kind: "until_block_end" },
    end: { kind: "event", event: "astronomical_dawn" },
    live_stack: true,
  });
  expect(plan.items[3]).toMatchObject({
    frame_type: "dark", count: 50, exposure_sec: 60, filter_name: "Dark",
    generate_master: true, safe_state: true,
    planning_estimate_min: 60, timeout_min: 75,
  });
});

it.each(["goto_and_stack", "mosaic", "take_exposures"] as const)("lets %s opt out of stacking", (kind) => {
  const item = newItem(kind, "panel", { kind: "asap" });
  const field = fieldsFor(item).find((f) => f.key === "live_stack");
  expect(field).toBeDefined();
  if (field === undefined) throw new Error("Missing live stacking field");
  expect(field.read(field.write(item, false))).toBe(false);
  expect(field.read(field.write(item, true))).toBe(true);
});

it("persists an optional autofocus filter and summarizes it with the temperature trigger", () => {
  let item = newItem("autofocus", "focus-lp", { kind: "asap" });
  expect(item).toMatchObject({ filter_name: null });
  const filter = selectField(item, "filter_name");
  expect(filter.dynamic).toBe("filter");
  expect(filter.allowUnset).toBe(true);
  expect(filter.hint).toContain("autofocus");
  expect(filter.read(item)).toBeNull();
  expect(itemSummary(item)).toBe("");
  item = filter.write(item, "LP");
  expect(filter.read(item)).toBe("LP");
  expect(JSON.parse(JSON.stringify(item))).toMatchObject({ filter_name: "LP" });
  expect(itemSummary(item)).toBe("LP");
  item = typedField(item, "temperature_trigger_delta_c", "number").write(item, 2);
  expect(itemSummary(item)).toBe("LP · Δ2 °C");
  item = filter.write(item, "");
  expect(filter.read(item)).toBeNull();
  expect(itemSummary(item)).toBe("Δ2 °C");
  const legacy = { ...item };
  delete (legacy as unknown as { filter_name?: string | null }).filter_name;
  expect(filter.read(legacy)).toBeNull();
  expect(itemSummary(legacy)).toBe("Δ2 °C");
});
