// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * `refusalForImagery` and `cutoutLayerCredits` are the two GL-free pieces
 * of this module's credit-line logic (architecture policy invariant 7 for the first,
 * invariant 10 for the second) — everything else here needs a live WebGL
 * context, which this file does not set up.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { TileSource, cutoutLayerCredits, refusalForImagery } from "./imageryLayer";
import type { CutoutLayerPiece, SkySurvey } from "../../types";

function survey(overrides: Partial<SkySurvey> = {}): SkySurvey {
  return {
    id: "unwise",
    label: "unWISE (infrared)",
    kind: "infrared",
    scheme: "mercator",
    frame: "equatorial",
    tileUrlTemplate: "/api/skymap/tiles/unwise/mercator/{level}/{x}/{y}.jpg",
    tileFormat: "jpg",
    maxOrder: 16,
    credit: { label: "unWISE / NASA/JPL-Caltech / D. Lang (Perimeter Institute)" },
    blendOnly: false,
    enabled: true,
    reason: null,
    productionCleared: true,
    rightsNotice: null,
    ...overrides,
  };
}

describe("refusalForImagery", () => {
  it("allows an enabled, Mercator-scheme background survey", () => {
    expect(refusalForImagery(survey(), "unwise", true)).toBeNull();
  });

  it("refuses a survey not in the registry", () => {
    expect(refusalForImagery(undefined, "missing-id", true)).toBe("missing-id: not in the survey registry");
  });

  it("refuses a disabled survey, naming its own reason", () => {
    const result = refusalForImagery(survey({ enabled: false, reason: "no local pack" }), "unwise", true);
    expect(result).toBe("unWISE (infrared): no local pack");
  });

  it("refuses a disabled survey with no stated reason with a generic fallback", () => {
    const result = refusalForImagery(survey({ enabled: false, reason: null }), "unwise", true);
    expect(result).toBe("unWISE (infrared): unavailable");
  });

  it("refuses a blend-only survey used as a background", () => {
    const result = refusalForImagery(survey({ blendOnly: true }), "unwise", true);
    expect(result).toBe("unWISE (infrared): an overlay, not a background");
  });

  it("allows a blend-only survey used as an overlay", () => {
    expect(refusalForImagery(survey({ blendOnly: true }), "unwise", false)).toBeNull();
  });

  it("refuses a restricted-named survey by label even if enabled and Mercator", () => {
    const result = refusalForImagery(survey({ label: "Digitized Sky Survey (Color)" }), "dss", true);
    expect(result).toBe('refusing restricted imagery "Digitized Sky Survey (Color)"');
  });

  it("refuses a restricted-named survey by id even if the label looks innocuous", () => {
    const result = refusalForImagery(survey({ id: "dss2", label: "Some Survey" }), "dss2", true);
    expect(result).toBe('refusing restricted imagery "Some Survey"');
  });

  it("refuses uncleared imagery even if it carries an explicit POC notice", () => {
    const result = refusalForImagery(
      survey({
        id: "evaluation-only",
        label: "Evaluation-only imagery",
        productionCleared: false,
        rightsNotice: "POC ONLY — commercial use requires written permission.",
      }),
      "evaluation-only",
      true,
    );
    expect(result).toBe('refusing uncleared imagery "Evaluation-only imagery"');
  });

  it("allows consent-gated DSS personal-use imagery while retaining its notice", () => {
    const result = refusalForImagery(
      survey({
        id: "dss2-color-hips-poc",
        label: "DSS2 colour (personal use)",
        scheme: "hips",
        productionCleared: false,
        rightsNotice: "POC ONLY — commercial-use permission remains unresolved.",
      }),
      "dss2-color-hips-poc",
      true,
    );
    expect(result).toBeNull();
  });


  it("allows an enabled HiPS survey — the vector frame draws Mercator and HiPS imagery", () => {
    const result = refusalForImagery(survey({ scheme: "hips", label: "NSNS OIII/H-alpha/SII (composite)" }), "nsns-ohs8", true);
    expect(result).toBeNull();
  });

  it("allows an enabled HiPS overlay", () => {
    const result = refusalForImagery(
      survey({ scheme: "hips", blendOnly: true, label: "NSNS H-alpha (narrowband)" }),
      "nsns-halpha",
      false,
    );
    expect(result).toBeNull();
  });

  it("refuses a TOAST-scheme survey — this frame has no TOAST tile math", () => {
    const result = refusalForImagery(survey({ scheme: "toast", label: "Pan-STARRS DR1" }), "panstarrs-dr1", true);
    expect(result).toBe("Pan-STARRS DR1: toast-scheme tiling is not supported by this frame");
  });

  it("checks enablement and restriction before the scheme check", () => {
    // A disabled TOAST survey should report "disabled", not "wrong scheme" —
    // the more specific, more actionable reason wins.
    const result = refusalForImagery(
      survey({ scheme: "toast", label: "Pan-STARRS DR1", enabled: false, reason: "cutouts only" }),
      "panstarrs-dr1",
      false,
    );
    expect(result).toBe("Pan-STARRS DR1: cutouts only");
  });
});

const DSS2_WCS = {
  crpix1: 500,
  crpix2: 500,
  crval1: 83.633,
  crval2: 22.014,
  cd1_1: -0.0008,
  cd1_2: 0,
  cd2_1: 0,
  cd2_2: 0.0008,
  ctype1: "RA---TAN",
  ctype2: "DEC--TAN",
};

const LEGACY_CREDIT_LABEL = "Legacy Surveys / D. Lang (Perimeter Institute)";
const LEGACY_CREDIT_URL = "https://www.legacysurvey.org/";

function cutoutPiece(overrides: Partial<CutoutLayerPiece> = {}): CutoutLayerPiece {
  return {
    id: "legacy-surveys:0",
    displayPngUrl: "/api/skymap/cutouts/job-1/pieces/0.png",
    wcs: DSS2_WCS,
    widthPx: 1000,
    heightPx: 1000,
    opacity: 0.85,
    creditLabel: LEGACY_CREDIT_LABEL,
    creditUrl: LEGACY_CREDIT_URL,
    ...overrides,
  };
}

describe("cutoutLayerCredits", () => {
  it("is empty with no visible cutout pieces", () => {
    expect(cutoutLayerCredits([])).toEqual([]);
  });

  it("credits a visible cutout", () => {
    const credits = cutoutLayerCredits([cutoutPiece()]);
    expect(credits).toEqual([{ label: LEGACY_CREDIT_LABEL, url: LEGACY_CREDIT_URL }]);
  });

  it("lists a source once even when its manifest has several pieces (a mosaic)", () => {
    const credits = cutoutLayerCredits([
      cutoutPiece({ id: "legacy-surveys:0" }),
      cutoutPiece({ id: "legacy-surveys:1" }),
      cutoutPiece({ id: "legacy-surveys:2" }),
    ]);
    expect(credits).toHaveLength(1);
  });

  it("credits two different cutout sources separately, in piece order", () => {
    const wise = cutoutPiece({
      id: "wise:0",
      creditLabel: "AllWISE: NASA/JPL-Caltech/UCLA · NASA/IPAC IRSA",
      creditUrl: "https://irsa.ipac.caltech.edu/",
    });
    const credits = cutoutLayerCredits([wise, cutoutPiece({ id: "legacy-surveys:0" })]);
    expect(credits).toEqual([
      { label: "AllWISE: NASA/JPL-Caltech/UCLA · NASA/IPAC IRSA", url: "https://irsa.ipac.caltech.edu/" },
      { label: LEGACY_CREDIT_LABEL, url: LEGACY_CREDIT_URL },
    ]);
  });
});


describe("tile source lifetime", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
  it("ignores a late tile load after its survey texture is destroyed", () => {
    const images: HTMLImageElement[] = [];
    vi.stubGlobal("Image", class {
      onload: (() => void) | null = null; onerror: (() => void) | null = null; src = "";
      constructor() { images.push(this as unknown as HTMLImageElement); }
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ clearRect: vi.fn(), drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
    const gl = { createTexture: vi.fn(() => ({})), bindTexture: vi.fn(), texImage2D: vi.fn(), texParameteri: vi.fn(), deleteTexture: vi.fn(), texSubImage2D: vi.fn() } as unknown as WebGLRenderingContext;
    const ready = vi.fn();
    const source = new TileSource(gl);
    source.ensureRequested("old", "/api/skymap/tiles/test.jpg", ready);
    const image = images[0];
    if (image === undefined) throw new Error("Tile image was not requested");
    const lateLoad = image.onload;
    source.destroy();
    lateLoad?.call(image, new Event("load"));
    expect(gl.texSubImage2D).not.toHaveBeenCalled();
    expect(ready).not.toHaveBeenCalled();
    expect(image.src).toBe("");
  });
  it("cancels obsolete viewport tiles, keeps wanted loads, and ignores cancelled late callbacks", () => {
    const images: HTMLImageElement[] = [];
    vi.stubGlobal("Image", class {
      onload: (() => void) | null = null; onerror: (() => void) | null = null; src = "";
      constructor() { images.push(this as unknown as HTMLImageElement); }
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ clearRect: vi.fn(), drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
    const gl = { createTexture: vi.fn(() => ({})), bindTexture: vi.fn(), texImage2D: vi.fn(), texParameteri: vi.fn(), deleteTexture: vi.fn(), texSubImage2D: vi.fn() } as unknown as WebGLRenderingContext;
    const ready = vi.fn(); const source = new TileSource(gl);
    source.setFrame(1);
    source.ensureRequested("old", "/api/skymap/tiles/old.jpg", ready);
    source.ensureRequested("current", "/api/skymap/tiles/current.jpg", ready);
    const old = images[0]; const current = images[1];
    if (old === undefined || current === undefined) throw new Error("Tiles not requested");
    const lateLoad = old.onload;
    source.setFrame(2, new Set(["current"]));
    expect(old.src).toBe("");
    expect(current.src).toBe("/api/skymap/tiles/current.jpg");
    lateLoad?.call(old, new Event("load"));
    expect(gl.texSubImage2D).not.toHaveBeenCalled();
    current.onload?.call(current, new Event("load"));
    expect(ready).toHaveBeenCalledTimes(1);
    source.ensureRequested("old", "/api/skymap/tiles/old.jpg", ready);
    expect(images).toHaveLength(3); // Cancellation is not a permanent failure.
    source.destroy();
  });

  it("shares at most four active image requests across survey layers", () => {
    const images: HTMLImageElement[] = [];
    vi.stubGlobal("Image", class {
      onload: (() => void) | null = null; onerror: (() => void) | null = null; src = "";
      constructor() { images.push(this as unknown as HTMLImageElement); }
    });
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ clearRect: vi.fn(), drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
    const gl = { createTexture: vi.fn(() => ({})), bindTexture: vi.fn(), texImage2D: vi.fn(), texParameteri: vi.fn(), deleteTexture: vi.fn(), texSubImage2D: vi.fn() } as unknown as WebGLRenderingContext;
    const sources = [new TileSource(gl), new TileSource(gl)];
    try {
      for (const [sourceIndex, source] of sources.entries()) for (let i = 0; i < 4; i++) source.ensureRequested(`${sourceIndex}:${i}`, `/api/skymap/tiles/${sourceIndex}/${i}.jpg`, vi.fn());
      expect(images.filter((image) => image.src !== "")).toHaveLength(4);
    } finally { for (const source of sources) source.destroy(); }
  });

});
