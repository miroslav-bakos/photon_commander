// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SurveyModel } from "../api/skymap";
import type { CutoutRowState, CutoutSourceRow } from "./cutoutLayers";
import { CUTOUT_DEFAULT_OPACITY, DEFAULT_LAYER_PREFS, type LayerPrefs } from "./layerPrefs";
import { LayersPanel } from "./LayersPanel";

afterEach(cleanup);

const noop = vi.fn();

function survey(overrides: Partial<SurveyModel>): SurveyModel {
  return {
    id: "legacy-surveys-dr11",
    label: "Legacy Surveys DR11 (grz)",
    kind: "optical",
    scheme: "mercator",
    frame: "equatorial",
    tile_format: "jpg",
    max_order: 16,
    credit: { label: "Legacy Surveys / D. Lang (Perimeter Institute)", url: "https://www.legacysurvey.org/" },
    upstream_host: "www.legacysurvey.org",
    bundle_allowed: true,
    redistributable_pack_url: null,
    blend_only: false,
    bundled: false,
    enabled: true,
    reason: null,
    production_cleared: true,
    rights_notice: null,
    tile_url_template: "/api/skymap/tiles/legacy-surveys-dr11/mercator/{level}/{x}/{y}.jpg",
    properties_url: null,
    cached_tiles: 0,
    cached_bytes: 0,
    missing_tiles: 0,
    pack_complete_order: null,
    ...overrides,
  };
}

const surveys: SurveyModel[] = [
  survey({}),
  survey({
    id: "wise",
    label: "AllWISE (infrared)",
    kind: "infrared",
    scheme: "hips",
    enabled: false,
    reason: "Available as a target-field image below, not a full-sky background.",
  }),
  survey({
    id: "wham-halpha",
    label: "H-alpha (WHAM)",
    kind: "halpha",
    scheme: "hips",
    blend_only: true,
    bundled: true,
    enabled: false,
    reason: "bundled H-alpha pack missing",
  }),
];

function open() {
  fireEvent.click(screen.getByRole("button", { name: "Layers" }));
}

const CUTOUT_CREDIT = {
  label: "unWISE / NASA/JPL-Caltech / D. Lang (Perimeter Institute)",
  url: "https://www.legacysurvey.org/acknowledgment/",
};

function cutoutRow(state: CutoutRowState, overrides: Partial<CutoutSourceRow> = {}): CutoutSourceRow {
  return {
    sourceId: "unwise",
    label: "unWISE",
    credit: CUTOUT_CREDIT,
    optional: false,
    state,
    ...overrides,
  };
}

describe("LayersPanel", () => {
  it("uses a clean background name without persistent early-build rights text", () => {
    render(
      <LayersPanel
        surveys={[survey({
          id: "dss2-color-hips-poc",
          label: "DSS2 colour (POC only)",
          frame: "equatorial",
          production_cleared: false,
          rights_notice: "POC ONLY — not cleared for commercial distribution.",
        })]}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    expect(screen.getByRole("radio", { name: "DSS2 colour" })).toBeInTheDocument();
    expect(screen.queryByText("POC ONLY — not cleared for commercial distribution.")).not.toBeInTheDocument();
  });

  it("requires acknowledgement before selecting restricted remote imagery", () => {
    const onChange = vi.fn();
    const restricted = survey({
      id: "dss2-color-hips-poc",
      label: "DSS2 colour (personal use)",
      frame: "equatorial",
      production_cleared: false,
      rights_notice: "Personal, non-commercial use only.",
    });
    render(
      <LayersPanel
        surveys={[restricted]}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={onChange}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    fireEvent.click(screen.getByRole("radio", { name: /DSS2 colour/ }));
    expect(onChange).not.toHaveBeenCalled();
    const panel = screen.getByRole("group", { name: "Map layers" });
    const dialog = screen.getByRole("alertdialog", { name: "Personal-use imagery" });
    expect(panel).not.toContainElement(dialog);
    expect(document.body).toContainElement(dialog);
    expect(dialog).toHaveTextContent(
      "Personal, non-commercial use only",
    );
    fireEvent.click(screen.getByRole("button", { name: "Accept and use" }));
    expect(onChange).toHaveBeenCalledWith({
      ...DEFAULT_LAYER_PREFS,
      background: "dss2-color-hips-poc",
      restrictedImageryConsents: { "dss2-color-hips-poc": true },
    });
  });


  it("lists an enabled background survey, and omits a disabled one from the Background group (2026-09-08 menu cleanup)", () => {
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    expect(screen.getByRole("radio", { name: /Legacy Surveys DR11/ })).toBeInTheDocument();
    // AllWISE cannot be a full-sky background (registry `enabled: false`) --
    // the disabled row used to be shown greyed out; it is omitted now, the
    // same product decision that removed the "Target field" cutout group.
    expect(screen.queryByRole("radio", { name: /AllWISE/ })).not.toBeInTheDocument();
    expect(
      screen.queryByText("Available as a target-field image below, not a full-sky background."),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /H-alpha \(WHAM\)/ })).not.toBeInTheDocument();
    expect(screen.queryByText("bundled H-alpha pack missing")).not.toBeInTheDocument();
  });

  it("selects a background and toggles an overlay through onChange", () => {
    const onChange = vi.fn();
    const enabledWham = surveys.map((s) => (s.id === "wham-halpha" ? { ...s, enabled: true, reason: null } : s));
    render(
      <LayersPanel
        surveys={enabledWham}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={onChange}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    fireEvent.click(screen.getByRole("radio", { name: /None \(black sky\)/ }));
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_LAYER_PREFS, background: null });
    fireEvent.click(screen.getByRole("checkbox", { name: /H-alpha \(WHAM\)/ }));
    expect(onChange).toHaveBeenLastCalledWith({
      ...DEFAULT_LAYER_PREFS,
      overlays: { "wham-halpha": { visible: true, opacity: 0.5 } },
    });
    fireEvent.change(screen.getByRole("slider", { name: "H-alpha (WHAM) opacity" }), { target: { value: "0.25" } });
    expect(onChange).toHaveBeenLastCalledWith({
      ...DEFAULT_LAYER_PREFS,
      overlays: { "wham-halpha": { visible: false, opacity: 0.25 } },
    });
  });

  it("toggles the altitude-limit ring, off by default, alongside constellation boundaries, through onChange", () => {
    const onChange = vi.fn();
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={onChange}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    const ring = screen.getByRole("checkbox", { name: "Altitude limit ring" });
    expect(ring).not.toBeChecked();
    fireEvent.click(ring);
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_LAYER_PREFS, altitudeLimitRing: true });
  });

  it("toggles named bright stars, on by default, through onChange", () => {
    const onChange = vi.fn();
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={onChange}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    const stars = screen.getByRole("checkbox", { name: "Named bright stars" });
    expect(stars).toBeChecked();
    fireEvent.click(stars);
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_LAYER_PREFS, brightStars: false });
  });

  it("toggles the Sun/Moon/planets group, all on by default, and its opacity slider, through onChange", () => {
    const onChange = vi.fn();
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={onChange}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    const sun = screen.getByRole("checkbox", { name: "Sun" });
    const moon = screen.getByRole("checkbox", { name: "Moon" });
    const planets = screen.getByRole("checkbox", { name: "Planets" });
    expect(sun).toBeChecked();
    expect(moon).toBeChecked();
    expect(planets).toBeChecked();
    fireEvent.click(moon);
    expect(onChange).toHaveBeenLastCalledWith({
      ...DEFAULT_LAYER_PREFS,
      solarSystem: { ...DEFAULT_LAYER_PREFS.solarSystem, moon: false },
    });
    fireEvent.change(screen.getByRole("slider", { name: "Solar system opacity" }), { target: { value: "0.4" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT_LAYER_PREFS, solarSystemOpacity: 0.4 });
  });

  it("disables the site-dependent sky lines with the reason when there is no location", () => {
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason="set a location in Settings"
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    expect(screen.getByRole("checkbox", { name: /Alt-az grid/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Meridian and zenith/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /^Horizon/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Equatorial grid/ })).toBeEnabled();
  });

  it("disables the Sun/Moon/planets toggles with the same reason: positions are topocentric and need a location", () => {
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason="set a location in Settings"
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    expect(screen.getByRole("checkbox", { name: /^Sun/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /^Moon/ })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /^Planets/ })).toBeDisabled();
    // Alt-az grid, meridian, horizon, Sun, Moon, planets: six rows share the
    // one reason.
    expect(screen.getAllByText("set a location in Settings")).toHaveLength(6);
  });

  it("shows the registry fetch error instead of an empty list", () => {
    render(
      <LayersPanel
        surveys={undefined}
        surveysError="request failed with status 503"
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
      />,
    );
    open();
    expect(screen.getByText("request failed with status 503")).toBeInTheDocument();
  });

  it("shows the meridian's off-screen hint and gotos it on click", () => {
    const onGotoMeridian = vi.fn();
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={{ ...DEFAULT_LAYER_PREFS, skyLines: { ...DEFAULT_LAYER_PREFS.skyLines, meridian: true } }}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={{ text: "at RA 21h14m — off-screen" }}
        onGotoMeridian={onGotoMeridian}
      />,
    );
    open();
    expect(screen.getByText("at RA 21h14m — off-screen")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Go to" }));
    expect(onGotoMeridian).toHaveBeenCalledOnce();
  });

  it("renders a Target field row for a source with a cutout, and none for one nobody has fetched", () => {
    // The scoping rule: "fetching"/"ready"/"unavailable" all have a row;
    // "no-target" and "not-fetched" (nothing ever asked for) do not, or the
    // group would fill with every registered cutout source the way the
    // pre-2026-09-08 group did.
    const rows = [
      cutoutRow(
        { kind: "ready", cutoutId: "job-1" },
        { sourceId: "wise", label: "AllWISE (infrared)" },
      ),
      cutoutRow(
        { kind: "not-fetched", offline: false, sizeDeg: 1.5 },
        { sourceId: "sdss", label: "SDSS (colour)" },
      ),
    ];
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
        cutoutRows={rows}
      />,
    );
    open();
    expect(screen.getByText("Target field")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "AllWISE (infrared)" })).toBeInTheDocument();
    expect(screen.queryByText("SDSS (colour)")).not.toBeInTheDocument();
  });

  it("renders no Target field group at all when no source has a cutout for the target", () => {
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
        cutoutRows={[cutoutRow({ kind: "no-target" })]}
      />,
    );
    open();
    expect(screen.queryByText("Target field")).not.toBeInTheDocument();
  });

  // The owner's own case (2026-09-10, "BUT THE TOGGLE DISSAPPEARED TOO"):
  // AllWISE was visible from an earlier target; the viewer moved on, the
  // *current* target has nothing fetched for AllWISE at all, but
  // `layerPrefs.cutouts` still says `visible: true` for it — a stale flag
  // left over from the earlier target, exactly what a leftover on-screen
  // image would look like from this component's own inputs. Against the
  // pre-2026-09-10 filter (`kind === "fetching" || "ready" || "unavailable"`
  // and nothing else) this row does not exist at all, so there is no
  // control left to turn the leftover image off — asserted failing against
  // that code path directly below, before asserting the fix.
  it("keeps a row (and its checkbox) for a source whose visibility is still on, even though the current target's own state is not-fetched — the only way to hide a previous target's leftover imagery", () => {
    const preFix = (rows: readonly CutoutSourceRow[]) =>
      rows.filter((r) => r.state.kind === "fetching" || r.state.kind === "ready" || r.state.kind === "unavailable");
    const staleVisibleRow = cutoutRow(
      { kind: "not-fetched", offline: false, sizeDeg: 1.5 },
      { sourceId: "wise", label: "AllWISE (infrared)" },
    );
    // Confirms the bug: the pre-2026-09-10 scoping rule drops this row
    // outright, leaving no control at all for a source `prefs` says is on.
    expect(preFix([staleVisibleRow])).toHaveLength(0);

    const onChange = vi.fn();
    const staleVisiblePrefs: LayerPrefs = {
      ...DEFAULT_LAYER_PREFS,
      cutouts: { wise: { visible: true, opacity: CUTOUT_DEFAULT_OPACITY } },
    };
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={staleVisiblePrefs}
        onChange={onChange}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
        cutoutRows={[staleVisibleRow]}
      />,
    );
    open();
    const checkbox = screen.getByRole("checkbox", { name: "AllWISE (infrared)" });
    expect(checkbox).toBeChecked();
    // The case that makes "no visible layer without a control" true rather
    // than merely stated: the row's own checkbox actually turns it off.
    fireEvent.click(checkbox);
    expect(onChange).toHaveBeenLastCalledWith({
      ...staleVisiblePrefs,
      cutouts: { wise: { visible: false, opacity: CUTOUT_DEFAULT_OPACITY } },
    });
  });

  it("always renders a not-fetched row, distinguishable from ready and unavailable, for a source with no background-survey form of its own", () => {
    // DSS2 colour is cutout-only (`CUTOUT_ONLY_SOURCE_IDS`): this panel is
    // its only control surface, so "nothing fetched yet" must say so rather
    // than looking like the source does not exist.
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
        cutoutRows={[cutoutRow({ kind: "not-fetched", offline: false, sizeDeg: 1.5 })]}
      />,
    );
    open();
    expect(screen.getByText("unWISE")).toBeInTheDocument();
    expect(screen.getByText("Not fetched yet")).toBeInTheDocument();
    // Distinguishable from "ready" (a checkbox, checked or not) and from
    // "unavailable" (a failure reason) — plain idle text, no checkbox, since
    // there is nothing yet to hide.
    expect(screen.queryByRole("checkbox", { name: "DSS2 colour" })).not.toBeInTheDocument();
    expect(screen.queryByText(/does not cover|failed|cancelled|interrupted/i)).not.toBeInTheDocument();
  });

  it("says offline instead of not-fetched when upstream is unreachable, for a cutout-only source's idle row", () => {
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
        cutoutRows={[cutoutRow({ kind: "not-fetched", offline: true, sizeDeg: 1.5 })]}
      />,
    );
    open();
    expect(screen.getByText("Offline — not fetched")).toBeInTheDocument();
  });

  it("shows a fetching row (no checkbox yet) and an unavailable row with its reason, neither with a Fetch/Retry button", () => {
    const rows = [
      cutoutRow({ kind: "fetching", status: "running", bytes: 2048 }),
      cutoutRow(
        { kind: "unavailable", reason: "SDSS does not cover this field", detail: null, retryable: false, sizeDeg: 1.5 },
        { sourceId: "sdss", label: "SDSS (color)" },
      ),
    ];
    render(
      <LayersPanel
        surveys={surveys}
        surveysError={null}
        prefs={DEFAULT_LAYER_PREFS}
        onChange={noop}
        siteUnavailableReason={null}
        meridianHint={null}
        onGotoMeridian={noop}
        cutoutRows={rows}
      />,
    );
    open();
    expect(screen.getByText("Fetching…")).toBeInTheDocument();
    expect(screen.getByText("SDSS does not cover this field")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Fetch|Retry/ })).not.toBeInTheDocument();
  });
});
