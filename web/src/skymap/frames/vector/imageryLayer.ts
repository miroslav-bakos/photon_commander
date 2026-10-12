// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * The vector frame's own background/overlay imagery layer — the shipped
 * successor to the `imagerySpike`/`imagerySpikeRenderer` spike (`1eb2807`,
 * `757788c`, `f1bda71`), reached now through the ordinary `setSurveys`/
 * `setLayers` messages every backend gets (`SkyMap.tsx` calls both
 * regardless of which iframe is mounted), not a URL flag. Draws whatever
 * **Mercator- or HiPS-scheme** survey the registry offers, reprojected into
 * this frame's own alt-az view through the *same* `worldToScreen` every
 * other shape uses, so a tile lands exactly where a `FovShape` at the same
 * RA/Dec would.
 *
 * HiPS support (skymap-viz brief, "NSNS OIII/H-alpha/SII (composite) is
 * dead on skymap (vector)") is the reason this matters beyond one survey:
 * every entry in the registry except `unwise` and `legacy-surveys-dr11` is
 * HiPS — `nsns-ohs8` (the composite background the brief asked for),
 * `nsns-halpha`, `nsns-oiii`, `nsns-sii`, `wham-halpha`, plus the disabled
 * `wise`/`sdss`. Before this, the vector frame could draw no overlay at
 * all. Tile *selection* and *mesh generation* differ per scheme
 * (`mercatorTiles.ts` vs `hipsTiles.ts`); everything downstream of "here is
 * a `TileMeshGrid` and a resident atlas slot" — batching
 * (`imageryBatch.ts`), the atlas (`tileAtlas.ts`), triangulation and
 * far-hemisphere culling (`tileMesh.ts`) — is shared, unchanged, between
 * the two.
 *
 * Batched, unlike the spike: the spike issued one WebGL draw call per
 * visible tile (measured ~85/frame) and never evicted a GPU texture. This
 * module packs every visible, loaded tile for a layer into one shared
 * texture atlas (`tileAtlas.ts`) and one shared vertex/index buffer
 * (`imageryBatch.ts`), so drawing a whole layer costs one `drawElements`
 * call (occasionally a small, bounded few — `imageryBatch.ts`'s
 * `maxTilesPerBatch`), and bounds GPU memory with a stated, explicit budget
 * (`tileAtlas.ts`'s own doc comment: 256 resident tiles per layer, ~64MiB,
 * LRU-evicted by frame number while panning).
 *
 * What is *not* new, and must not regress (skymap-viz brief, 2026-09-09):
 * the far-hemisphere per-triangle culling and the corrected (mirrored)
 * Mercator `x` addressing, both hard-won against live defects the spike
 * hit (`mercatorTiles.ts`'s own doc comments on `FAR_HEMISPHERE_COSC_MARGIN`
 * and `tileFracFromRaDec`) — this module calls those same, unchanged
 * functions rather than re-deriving either.
 *
 * A survey this frame cannot draw — TOAST-scheme (no registry entry uses it
 * today, but a survey could), disabled, restricted-named, or an overlay id
 * used as a background — is refused with a stated reason
 * (`refusalForImagery`, the same pattern WWT's own `refusalFor` used before
 * it was removed, `THIRD_PARTY_NOTICES.md` §9) rather than silently showing
 * nothing (architecture policy invariant 7).
 *
 * Also drawn here, since 2026-09-09: target-field cutout pieces
 * (`setCutoutLayers`) — the regression fix for the vector frame becoming
 * default (`afeb4fc`) without implementing this method at all, which left
 * every cutout fetched, cached and never drawn. Reuses this module's own
 * GL program/canvas and this frame's shared mesh-culling maths
 * (`cutoutMesh.ts`, `tileMesh.ts`) but *not* the atlas/batch machinery
 * above — see `cutoutMesh.ts`'s own doc comment for why a handful of
 * arbitrarily-sized, host-selected pieces is the wrong shape for a
 * 256px-tile atlas. Drawn after every survey layer in the same `draw()`
 * pass (above the background/overlay imagery, still behind this frame's
 * own 2D overlay canvas which paints all `FovShape`s and grids on top of
 * this whole WebGL canvas by DOM order) — exactly the draw order
 * `skymap-viz.md` asks for.
 */
import { isRestrictedImagesetName } from "../../surveys";
import { screenToWorld, type Viewport } from "../../projection";
import type { CutoutLayerPiece, LayerSelection, SkySurvey, SurveyCredit } from "../../types";
import { buildCutoutMesh } from "./cutoutMesh";
import { imageryImageRequests } from "./imageRequests";
import { buildImageryBatches, type ResidentTile } from "./imageryBatch";
import { buildHipsTileMeshGrid, hipsTileUrl, pickHipsOrder, visibleHipsTiles, type HipsTileKey } from "./hipsTiles";
import { buildTileMeshFracGrid, mercatorTileUrl, pickZoom, visibleTiles, type TileKey } from "./mercatorTiles";
import { ATLAS_TILE_PX, DEFAULT_ATLAS_SLOTS_PER_ROW, TileAtlasAllocator, type AtlasSlot } from "./tileAtlas";

/** Vertices per tile edge (`buildTileMeshFracGrid`'s `subdivisions`). 8,
 * unchanged from the spike: `mercatorTiles.test.ts`'s own measurements put
 * worst-case curvature error at this subdivision around 3px at a 60deg FOV
 * and well under 0.1px at 5deg, in both the equatorial and near-zenith
 * horizontal cases. */
const SUBDIVISIONS = 8;

/** The host consent-gates this remote-only personal-use source. */
const CONSENT_GATED_REMOTE_IDS = new Set(["dss2-color-hips-poc"]);

/** Why `survey` may not be drawn as imagery here, or `null` when it may.
 * Every refusal is a sentence the credit line can show (architecture policy invariant
 * 7) — the same pattern WWT's own `refusalFor` used before it was removed,
 * plus one addition:
 * this frame's tile math addresses a Mercator XYZ pyramid
 * (`mercatorTiles.ts`) or a HiPS/HEALPix pyramid (`hipsTiles.ts`); a
 * TOAST-scheme survey (none in today's registry, but the type allows one)
 * is refused here with an honest "not supported by this frame" rather than
 * a silent no-op the way the pre-imagery vector frame handled every
 * survey. */
export function refusalForImagery(survey: SkySurvey | undefined, id: string, asBackground: boolean): string | null {
  if (survey === undefined) return `${id}: not in the survey registry`;
  if (!survey.enabled) return `${survey.label}: ${survey.reason ?? "unavailable"}`;
  if (asBackground && survey.blendOnly) return `${survey.label}: an overlay, not a background`;
  const consentGatedRemote = CONSENT_GATED_REMOTE_IDS.has(survey.id) && survey.rightsNotice !== null;
  if (!survey.productionCleared && !consentGatedRemote) {
    return `refusing uncleared imagery "${survey.label}"`;
  }
  if (!consentGatedRemote && (isRestrictedImagesetName(survey.label) || isRestrictedImagesetName(survey.id))) {
    return `refusing restricted imagery "${survey.label}"`;
  }
  if (survey.scheme !== "mercator" && survey.scheme !== "hips") {
    return `${survey.label}: ${survey.scheme}-scheme tiling is not supported by this frame`;
  }
  return null;
}

/**
 * The credit-line contribution of a cutout layer's visible pieces — pure
 * and GL-free, like `refusalForImagery` above, so it is testable without a
 * WebGL context (this module's own tests only cover the two functions that
 * do not need one).
 *
 * `pieces` is already exactly what a viewer sees: `SkyMap.tsx` only ever
 * sends pieces for a *visible, ready* row (`visibleReadyRows`), so there is
 * no separate visibility check here, only dedup — a multi-piece mosaic from
 * one source (`manifest.pieces`) carries the same `creditLabel`/
 * `creditUrl` on every piece and must not list a source once per tile.
 * Order is preserved (first piece per distinct
 * credit wins the position), which is `cutoutPieces`' own insertion order —
 * draw order, the same rule every other credit here follows. */
export function cutoutLayerCredits(pieces: readonly CutoutLayerPiece[]): SurveyCredit[] {
  const seen = new Set<string>();
  const credits: SurveyCredit[] = [];
  for (const piece of pieces) {
    const key = `${piece.creditLabel} ${piece.creditUrl}`;
    if (seen.has(key)) continue;
    seen.add(key);
    credits.push({ label: piece.creditLabel, url: piece.creditUrl });
  }
  return credits;
}

function mercatorTileKeyString(tile: TileKey): string {
  return `mercator/${String(tile.z)}/${String(tile.x)}/${String(tile.y)}`;
}

function hipsTileKeyString(tile: HipsTileKey): string {
  return `hips/${String(tile.order)}/${String(tile.npix)}`;
}

/**
 * One survey's own atlas texture, fetch queue and LRU allocator — scheme-
 * agnostic (it only ever sees a string key and a same-origin URL, both
 * already resolved by the caller's own scheme-specific tile math), so the
 * same class serves a Mercator or a HiPS survey. Allocates the full atlas
 * texture storage once, uninitialised (`texImage2D(..., null)`), so every
 * tile upload afterward is a bounded `texSubImage2D` into its own slot
 * rather than a GPU (re)allocation — the memory this source ever holds is
 * fixed at construction, not "however many tiles have ever loaded".
 */
export class TileSource {
  readonly texture: WebGLTexture;
  readonly slotsPerRow: number;
  private readonly gl: WebGLRenderingContext;
  private readonly atlas: TileAtlasAllocator;
  private readonly pending = new Map<string, HTMLImageElement>();
  private destroyed = false;
  private readonly errored = new Set<string>();
  private frame = 0;
  // A HiPS tile is not necessarily `ATLAS_TILE_PX` (256) square the way
  // every Mercator tile this frame requests is (`ssc/skymap/mercator.py`'s
  // own fixed 256px tiling): NSNS and WHAM both publish `hips_tile_width =
  // 512` (`ssc/skymap/surveys.py`'s own doc comments). Uploading a fetched
  // image straight into an atlas slot sized for 256px would write past the
  // slot (and, live, past the whole atlas texture — verified against a
  // running DEV: `texSubImage2D` reported "Offset overflows texture
  // dimensions" the first time an NSNS tile loaded) — so every image is
  // first drawn, resampled, onto this reusable `ATLAS_TILE_PX`-square
  // canvas, and *that* canvas — never the original `Image` — is what
  // actually reaches the GPU. One canvas per source, reused across every
  // tile upload, so this costs one small 2D draw per tile, not a
  // per-upload allocation.
  private readonly resampleCanvas: HTMLCanvasElement;
  private readonly resampleCtx: CanvasRenderingContext2D;

  constructor(gl: WebGLRenderingContext, slotsPerRow: number = DEFAULT_ATLAS_SLOTS_PER_ROW) {
    this.gl = gl;
    this.slotsPerRow = slotsPerRow;
    this.atlas = new TileAtlasAllocator(slotsPerRow);
    this.resampleCanvas = document.createElement("canvas");
    this.resampleCanvas.width = ATLAS_TILE_PX;
    this.resampleCanvas.height = ATLAS_TILE_PX;
    const ctx = this.resampleCanvas.getContext("2d");
    if (ctx === null) throw new Error("[skymap] imageryLayer: 2D context for tile resampling unavailable");
    this.resampleCtx = ctx;
    // WebGL may return null after context loss even though this TypeScript DOM
    // version declares a non-null return.
    const texture = gl.createTexture() as WebGLTexture | null;
    if (texture === null) throw new Error("[skymap] imageryLayer: createTexture failed");
    this.texture = texture;
    const atlasPx = slotsPerRow * ATLAS_TILE_PX;
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, atlasPx, atlasPx, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  }

  setFrame(frame: number, wantedKeys?: ReadonlySet<string>): void {
    this.frame = frame;
    if (wantedKeys === undefined) return;
    const obsolete: HTMLImageElement[] = [];
    for (const [key, image] of this.pending) {
      if (wantedKeys.has(key)) continue;
      this.pending.delete(key);
      obsolete.push(image);
    }
    imageryImageRequests.cancelMany(obsolete);
  }

  /** The slot `tileKey` currently occupies, marking it used this frame — or
   * `null` if it is not resident (still loading, errored, or never
   * requested). Never fetches; `ensureRequested` does that. */
  residentSlot(tileKey: string): AtlasSlot | null {
    if (!this.atlas.has(tileKey)) return null;
    return this.atlas.acquire(tileKey, this.frame).slot;
  }

  /** Requests `tileKey` from `url` if it is not already resident, loading,
   * or previously failed — same-origin only by construction (`url` always
   * comes from `mercatorTileUrl` or `hipsTileUrl`, never a caller-supplied
   * host). No retry/backoff on failure, same as the spike: a tile that
   * 404s or times out is simply never drawn, an honest gap rather than a
   * wrong pixel. */
  ensureRequested(tileKey: string, url: string, onReady: () => void): void {
    if (this.destroyed || this.atlas.has(tileKey) || this.pending.has(tileKey) || this.errored.has(tileKey)) return;
    const image = new Image();
    this.pending.set(tileKey, image);
    image.onload = () => {
      if (this.destroyed || this.pending.get(tileKey) !== image) return;
      this.pending.delete(tileKey);
      const { slot } = this.atlas.acquire(tileKey, this.frame);
      // Always resample to exactly ATLAS_TILE_PX square first (see this
      // class's own doc comment) — a no-op resize for a Mercator tile that
      // already arrives at that size, a real downsample for a 512px HiPS
      // tile, either way the only thing ever uploaded to a slot.
      this.resampleCtx.clearRect(0, 0, ATLAS_TILE_PX, ATLAS_TILE_PX);
      this.resampleCtx.drawImage(image, 0, 0, ATLAS_TILE_PX, ATLAS_TILE_PX);
      const gl = this.gl;
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.texSubImage2D(
        gl.TEXTURE_2D,
        0,
        slot.col * ATLAS_TILE_PX,
        slot.row * ATLAS_TILE_PX,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        this.resampleCanvas,
      );
      onReady();
    };
    image.onerror = () => {
      if (this.destroyed || this.pending.get(tileKey) !== image) return;
      this.pending.delete(tileKey);
      this.errored.add(tileKey);
    };
    imageryImageRequests.request(image, url);
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    imageryImageRequests.cancelMany([...this.pending.values()]);
    this.gl.deleteTexture(this.texture);
    this.atlas.reset();
    this.pending.clear();
    this.errored.clear();
  }
}

/**
 * One `CutoutLayerPiece`'s own GL texture — deliberately not a `TileSource`:
 * a cutout is a single image with no fetch retries and no atlas slot
 * (`cutoutMesh.ts`'s own doc comment), so this only ever needs a texture
 * handle and whether it has finished loading. Uploaded at the image's own
 * decoded size (`texImage2D`'s `HTMLImageElement` overload) rather than
 * resampled onto a fixed-size canvas the way `TileSource` must for an
 * atlas slot — a cutout keeps its native resolution, which is the whole
 * point of drawing it (the owner frames against it).
 */
class CutoutTexture {
  readonly texture: WebGLTexture;
  private readonly gl: WebGLRenderingContext;
  private readonly image: HTMLImageElement;
  ready = false;
  /** Set by `destroy`, checked by the pending `onload` above. */
  private destroyed = false;

  constructor(gl: WebGLRenderingContext, url: string, onReady: () => void) {
    this.gl = gl;
    const texture = gl.createTexture() as WebGLTexture | null;
    if (texture === null) throw new Error("[skymap] imageryLayer: createTexture failed (cutout)");
    this.texture = texture;
    const image = new Image();
    this.image = image;
    image.onload = () => {
      // The piece list can change while this image is still in flight — a
      // different target resolved, the layer switched off — and
      // `setCutoutLayers` destroys any texture no longer wanted. Uploading
      // into a deleted texture is an INVALID_OPERATION that WebGL swallows
      // silently, so it costs a wasted decode and leaves no trace. Cheap to
      // rule out, and easy to hit by clicking around.
      if (this.destroyed) return;
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      this.ready = true;
      onReady();
    };
    // No `onerror` handler and no retry, same honest-gap policy as
    // `TileSource.ensureRequested`: `ready` simply stays `false` forever if
    // the image never loads, so a failed piece is never drawn — never
    // silently replaced by a guess. `imageryLoadTracking.ts`'s own listener
    // (attached via `addEventListener`, not this property) still sees the
    // `error` event regardless.
    imageryImageRequests.request(image, url);
  }

  destroy(): void {
    this.destroyed = true;
    imageryImageRequests.cancelMany([this.image]);
    this.gl.deleteTexture(this.texture);
  }
}

const VERTEX_SHADER = `
  attribute vec2 aPosition;
  attribute vec2 aUV;
  uniform vec2 uResolutionPx;
  varying vec2 vUV;
  void main() {
    vec2 clip = vec2(
      (aPosition.x / uResolutionPx.x) * 2.0 - 1.0,
      1.0 - (aPosition.y / uResolutionPx.y) * 2.0
    );
    gl_Position = vec4(clip, 0.0, 1.0);
    vUV = aUV;
  }
`;

/**
 * `vUV` addresses a shared, e.g. 4096px-square atlas texture (`tileAtlas.ts`)
 * rather than one dedicated texture per tile the way this shader's own
 * predecessor did — so a UV value now needs enough precision to tell two
 * *adjacent atlas slots* apart, not just two edges of one 256px tile.
 * `mediump` is only required to guarantee about 10 bits of relative
 * precision in GLSL ES 1.00 (`highp` is *optional* in the fragment stage —
 * `GL_FRAGMENT_PRECISION_HIGH` — which is why this cannot be assumed and
 * must be probed for); 10 bits over a 4096px atlas is only ~4px of
 * addressable precision, comfortably capable of rounding a UV meant for one
 * atlas row/slot into a neighbouring one on hardware that actually
 * implements `mediump` at its minimum guaranteed precision (real embedded
 * GPUs, unlike desktop drivers and SwiftShader — which is why this was not
 * caught testing on DEV: software/desktop GL backends commonly implement
 * `mediump` as full `float32` under the hood, so this class of error is
 * invisible there and only shows up on hardware that takes the precision
 * qualifier at its word). Opting into `highp` when the implementation
 * supports it (`GL_FRAGMENT_PRECISION_HIGH`, the standard portable
 * feature-test — never assume, always probe) removes that ceiling
 * entirely; falling back to `mediump` otherwise keeps every implementation
 * still compiling. Recorded here, not claimed as a confirmed root cause: it
 * is the most plausible mechanism found for a live report of tiles
 * "shifted" by roughly one atlas row/column that this session's own
 * extensive DEV/SwiftShader testing (static views and active panning, one
 * and multiple tile rows, heavy LRU eviction) could not reproduce — see the
 * PR discussion for the full investigation, including a wrong atlas
 * row-mirroring theory that was tried, found wrong by direct experiment,
 * and reverted.
 */
const FRAGMENT_SHADER = `
  #ifdef GL_FRAGMENT_PRECISION_HIGH
    precision highp float;
  #else
    precision mediump float;
  #endif
  varying vec2 vUV;
  uniform sampler2D uSampler;
  uniform float uOpacity;
  void main() {
    vec4 texel = texture2D(uSampler, vUV);
    gl_FragColor = vec4(texel.rgb, texel.a * uOpacity);
  }
`;

function compileShader(gl: WebGLRenderingContext, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (shader === null) throw new Error("[skymap] imageryLayer: createShader failed");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!(gl.getShaderParameter(shader, gl.COMPILE_STATUS) as boolean)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`[skymap] imageryLayer: shader compile failed: ${info ?? "unknown"}`);
  }
  return shader;
}

export interface ImageryLayer {
  setSurveys(surveys: SkySurvey[]): void;
  setLayers(layers: LayerSelection): void;
  /** Reconciles active layers against the last `setSurveys`/`setLayers`
   * call — tears down any GPU source no longer selected — and returns the
   * credit line: one entry per visible layer, in draw order (background
   * first), with a refusal in place of any layer that could not be shown.
   * Call whenever surveys or layers change. */
  applyLayers(): SurveyCredit[];
  /** The full set of target-field cutout pieces to draw, every call — the
   * frame reconciles by `id` (`SkyMapBackend.setCutoutLayers`'s own
   * contract), tearing down the GL texture for any piece no longer
   * present. An empty array draws nothing, the same as never having
   * called this. */
  setCutoutLayers(pieces: CutoutLayerPiece[]): void;
  /** Draws every active layer, then every cutout piece with a loaded
   * texture, behind whatever `existingCanvas` draws next — call once per
   * host `render()`, before the overlay shapes. Returns the number of
   * `gl.drawElements` calls issued, for a manual measurement harness;
   * production code has no reason to read it. */
  draw(viewport: Viewport): number;
  resize(widthPx: number, heightPx: number, dpr: number): void;
  destroy(): void;
}

/**
 * Creates the layer and its own `<canvas>`, inserted as `root`'s first
 * child so it paints behind `existingCanvas` (the frame's 2D overlay
 * canvas) — DOM order, not a compositing trick, the same reasoning the
 * spike's renderer used: WebGL and 2D contexts cannot coexist on one
 * `<canvas>`, and drawing imagery behind the existing transparent-clear
 * overlay canvas means the FOV rectangles/grid/labels this frame already
 * draws need no change at all to appear on top. `onTileReady` fires once
 * per tile that finishes loading, so the caller can schedule a redraw — a
 * tile that arrives between frames must not wait for the next unrelated
 * redraw to appear.
 */
export function createImageryLayer(
  root: HTMLElement,
  existingCanvas: HTMLCanvasElement,
  onTileReady: () => void,
): ImageryLayer {
  const canvas = document.createElement("canvas");
  canvas.id = "skymap-imagery-canvas";
  canvas.style.position = "absolute";
  canvas.style.inset = "0";
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.pointerEvents = "none";
  root.insertBefore(canvas, existingCanvas);

  const glOrNull = canvas.getContext("webgl");
  if (glOrNull === null) throw new Error("[skymap] imageryLayer: WebGL unavailable");
  const gl: WebGLRenderingContext = glOrNull;

  const program = gl.createProgram() as WebGLProgram | null;
  if (program === null) throw new Error("[skymap] imageryLayer: createProgram failed");
  gl.attachShader(program, compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER));
  gl.attachShader(program, compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
  gl.linkProgram(program);
  if (!(gl.getProgramParameter(program, gl.LINK_STATUS) as boolean)) {
    throw new Error(`[skymap] imageryLayer: program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
  }
  gl.useProgram(program);
  const aPosition = gl.getAttribLocation(program, "aPosition");
  const aUV = gl.getAttribLocation(program, "aUV");
  const uResolutionPx = gl.getUniformLocation(program, "uResolutionPx");
  const uSampler = gl.getUniformLocation(program, "uSampler");
  const uOpacity = gl.getUniformLocation(program, "uOpacity");

  const positionBuffer = gl.createBuffer();
  const uvBuffer = gl.createBuffer();
  const indexBuffer = gl.createBuffer();

  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

  let widthPx = 800;
  let heightPx = 600;
  let surveys: SkySurvey[] = [];
  let layers: LayerSelection = { background: null, backgroundOpacity: 0.5, overlays: [] };
  const sources = new Map<string, TileSource>();
  let cutoutPieces: CutoutLayerPiece[] = [];
  const cutoutTextures = new Map<string, CutoutTexture>();

  function setCutoutLayers(pieces: CutoutLayerPiece[]): void {
    cutoutPieces = pieces;
    const wanted = new Set(pieces.map((p) => p.id));
    imageryImageRequests.batch(() => {
    for (const [id, texture] of cutoutTextures) {
      if (wanted.has(id)) continue;
      texture.destroy();
      cutoutTextures.delete(id);
    }
    for (const piece of pieces) {
      if (cutoutTextures.has(piece.id)) continue;
      cutoutTextures.set(piece.id, new CutoutTexture(gl, piece.displayPngUrl, onTileReady));
    }    });
  }
  let frameCounter = 0;

  interface ActiveLayer {
    survey: SkySurvey;
    opacity: number;
    }
  let activeLayers: ActiveLayer[] = [];

  function sourceFor(surveyId: string): TileSource {
    let source = sources.get(surveyId);
    if (source === undefined) {
      source = new TileSource(gl, DEFAULT_ATLAS_SLOTS_PER_ROW);
      sources.set(surveyId, source);
    }
    return source;
  }

  function destroySource(surveyId: string): void {
    const source = sources.get(surveyId);
    if (source === undefined) return;
    source.destroy();
    sources.delete(surveyId);
  }

  function applyLayers(): SurveyCredit[] {
    const credits: SurveyCredit[] = [];
    const wanted = new Set<string>();
    const next: ActiveLayer[] = [];
    const backgroundOpacity = Math.min(1, Math.max(0, layers.backgroundOpacity));

    if (layers.background !== null) {
      const survey = surveys.find((s) => s.id === layers.background);
      const refusal = refusalForImagery(survey, layers.background, true);
      // No pixels are drawn for an unavailable layer, so it must not take
      // space in the visible-layer attribution footer.
      if (refusal === null && survey !== undefined) {
        wanted.add(survey.id);
        next.push({ survey, opacity: backgroundOpacity });
        credits.push(survey.credit);
      }
    }
    for (const selection of layers.overlays) {
      if (!selection.visible) continue;
      const survey = surveys.find((s) => s.id === selection.surveyId);
      const refusal = refusalForImagery(survey, selection.surveyId, false);
      if (refusal !== null || survey === undefined) {
        continue;
      }
      wanted.add(survey.id);
      next.push({ survey, opacity: Math.min(1, Math.max(0, selection.opacity)) });
      credits.push(survey.credit);
    }
    // Target-field cutout pieces (`setCutoutLayers`), drawn after every
    // survey layer (this module's own doc comment) so their credit is
    // listed last too.
    credits.push(...cutoutLayerCredits(cutoutPieces));
    imageryImageRequests.batch(() => {
      for (const id of [...sources.keys()]) {
        if (!wanted.has(id)) destroySource(id);
      }
    });
    activeLayers = next;
    return credits;
  }

  /** Uploads one layer's already-built batches and issues one
   * `gl.drawElements` per batch — the GL half of a layer's draw, shared
   * between the Mercator and HiPS branches of `draw` below since neither
   * scheme changes anything past "here are the batches". Returns the
   * number of draw calls issued. */
  function drawBatches(batches: ReturnType<typeof buildImageryBatches>, source: TileSource, opacity: number): number {
    let drawCalls = 0;
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, source.texture);
    gl.uniform1i(uSampler, 0);
    gl.uniform1f(uOpacity, opacity);
    for (const batch of batches) {
      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, batch.positions, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(aPosition);
      gl.vertexAttribPointer(aPosition, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, uvBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, batch.uvs, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(aUV);
      gl.vertexAttribPointer(aUV, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, batch.indices, gl.DYNAMIC_DRAW);
      gl.drawElements(gl.TRIANGLES, batch.indices.length, gl.UNSIGNED_SHORT, 0);
      drawCalls += 1;
    }
    return drawCalls;
  }

  /**
   * Draws every active layer and returns the number of `gl.drawElements`
   * calls it issued — the batching win this module exists for, stated as a
   * number rather than left implicit. Not read by production code; a manual
   * frame-time/draw-call measurement harness reads it via the frame's own
   * `render()` (`main.ts`), the same opt-in-cost-nothing pattern as
   * `window.__skymapFrameTimesMs`.
   */
  function draw(viewport: Viewport): number {
    frameCounter += 1;
    let drawCalls = 0;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (activeLayers.length === 0) return drawCalls + drawCutouts(viewport);
    gl.useProgram(program);
    gl.uniform2f(uResolutionPx, widthPx, heightPx);

    // Base zoom/order picked once per frame, independent of any one
    // layer's own `maxOrder` — each layer clamps to its own registry entry
    // below, the same split `mercatorTiles.ts`'s own `pickZoom` documents.
    const mercatorZoom = pickZoom(viewport.view.fovDeg, widthPx, heightPx);
    const hipsOrder = pickHipsOrder(viewport.view.fovDeg, widthPx, heightPx);
    const visibleMercatorAtZoom = new Map<number, TileKey[]>();
    const visibleMercatorFor = (z: number): TileKey[] => {
      let tiles = visibleMercatorAtZoom.get(z);
      if (tiles === undefined) {
        tiles = visibleTiles(viewport, z, screenToWorld);
        visibleMercatorAtZoom.set(z, tiles);
      }
      return tiles;
    };
    const visibleHipsAtOrder = new Map<string, HipsTileKey[]>();
    const visibleHipsFor = (order: number, frame: SkySurvey["frame"]): HipsTileKey[] => {
      const cacheKey = `${frame}:${String(order)}`;
      let tiles = visibleHipsAtOrder.get(cacheKey);
      if (tiles === undefined) {
        tiles = visibleHipsTiles(viewport, order, screenToWorld, 9, frame);
        visibleHipsAtOrder.set(cacheKey, tiles);
      }
      return tiles;
    };

    imageryImageRequests.batch(() => {
    for (const layer of activeLayers) {
      const layerOpacity = layer.opacity;
      if (layerOpacity <= 0) {
        sources.get(layer.survey.id)?.setFrame(frameCounter, new Set());
        continue;
      }
      const source = sourceFor(layer.survey.id);
      // A layer never asks for a deeper tile than its own registry entry
      // has (`maxOrder`) — the upstream simply does not have one, so asking
      // would just 404 forever, not "more detail".
      if (layer.survey.scheme === "mercator") {
        const layerZoom = Math.min(mercatorZoom, layer.survey.maxOrder);
        const layerTiles = visibleMercatorFor(layerZoom);
        source.setFrame(frameCounter, new Set(layerTiles.map(mercatorTileKeyString)));
        const resident: ResidentTile<TileKey>[] = [];
        for (const tile of layerTiles) {
          const key = mercatorTileKeyString(tile);
          const slot = source.residentSlot(key);
          if (slot !== null) {
            resident.push({ key, tile, slot });
          } else {
            source.ensureRequested(key, mercatorTileUrl(layer.survey.tileUrlTemplate, tile), onTileReady);
          }
        }
        if (resident.length === 0) continue;
        const batches = buildImageryBatches(viewport, resident, SUBDIVISIONS, source.slotsPerRow, buildTileMeshFracGrid);
        drawCalls += drawBatches(batches, source, layerOpacity);
      } else {
        // refusalForImagery only ever admits "mercator" or "hips" into
        // activeLayers, so this is the HiPS branch, not a fallback.
        const layerOrder = Math.min(hipsOrder, layer.survey.maxOrder);
        const layerTiles = visibleHipsFor(layerOrder, layer.survey.frame);
        source.setFrame(frameCounter, new Set(layerTiles.map(hipsTileKeyString)));
        const resident: ResidentTile<HipsTileKey>[] = [];
        for (const tile of layerTiles) {
          const key = hipsTileKeyString(tile);
          const slot = source.residentSlot(key);
          if (slot !== null) {
            resident.push({ key, tile, slot });
          } else {
            source.ensureRequested(key, hipsTileUrl(layer.survey.tileUrlTemplate, tile), onTileReady);
          }
        }
        if (resident.length === 0) continue;
        const mesh = (vp: Viewport, tile: HipsTileKey, subdivisions: number) =>
          buildHipsTileMeshGrid(vp, tile, subdivisions, layer.survey.frame);
        const batches = buildImageryBatches(viewport, resident, SUBDIVISIONS, source.slotsPerRow, mesh);
        drawCalls += drawBatches(batches, source, layerOpacity);
      }
    }
    });
    return drawCalls + drawCutouts(viewport);
  }

  /** One `gl.drawElements` per piece with a loaded texture — no batching
   * (`cutoutMesh.ts`'s own doc comment on why a handful of arbitrarily
   * sized pieces is the wrong shape for it). A piece still loading, or
   * that failed to load, is simply skipped: an honest gap, not a missing
   * draw call worth erroring over (architecture policy invariant 7 — "unknown" is
   * not "broken"; the piece may finish loading and appear on the very next
   * frame via `onTileReady`). */
  function drawCutouts(viewport: Viewport): number {
    if (cutoutPieces.length === 0) return 0;
    let drawCalls = 0;
    gl.useProgram(program);
    gl.uniform2f(uResolutionPx, widthPx, heightPx);
    for (const piece of cutoutPieces) {
      const texture = cutoutTextures.get(piece.id);
      if (texture?.ready !== true) continue;
      const mesh = buildCutoutMesh(viewport, piece, SUBDIVISIONS);
      if (mesh.indices.length === 0) continue; // entirely on the far hemisphere this frame.

      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture.texture);
      gl.uniform1i(uSampler, 0);
      gl.uniform1f(uOpacity, Math.min(1, Math.max(0, piece.opacity)));

      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(aPosition);
      gl.vertexAttribPointer(aPosition, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, uvBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, mesh.uvs, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(aUV);
      gl.vertexAttribPointer(aUV, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.DYNAMIC_DRAW);
      gl.drawElements(gl.TRIANGLES, mesh.indices.length, gl.UNSIGNED_SHORT, 0);
      drawCalls += 1;
    }
    return drawCalls;
  }

  return {
    setSurveys(next: SkySurvey[]): void {
      surveys = next;
    },
    setLayers(next: LayerSelection): void {
      layers = next;
    },
    applyLayers,
    setCutoutLayers,
    draw,
    resize(newWidthPx: number, newHeightPx: number, dpr: number): void {
      widthPx = newWidthPx;
      heightPx = newHeightPx;
      canvas.width = Math.round(newWidthPx * dpr);
      canvas.height = Math.round(newHeightPx * dpr);
      gl.viewport(0, 0, canvas.width, canvas.height);
    },
    destroy(): void {
      imageryImageRequests.batch(() => {
        for (const id of [...sources.keys()]) destroySource(id);
        for (const texture of cutoutTextures.values()) texture.destroy();
      });
      cutoutTextures.clear();
      canvas.remove();
    },
  };
}
