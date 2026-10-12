// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Reports whether any imagery tile or cutout piece is currently being
 * fetched, for the "loading sky survey imagery" indicator
 * (`protocol.ts`'s `imageryLoadingChange`, reported live: an earlier
 * version keyed the indicator on `creditChange`, which fires as soon as a
 * layer is *selected* — near-instantly, well before any pixel exists — so
 * the indicator never actually showed during a real 10-20s unWISE fetch).
 *
 * This observes the browser's own `<img>` loading rather than a private hook
 * into a vendored engine's internal state — WWT's `@wwtelescope/engine`
 * tracked a per-tile `downloading` flag internally (`Tile.requestImage`),
 * untyped and undocumented, before it was removed (`THIRD_PARTY_NOTICES.md`
 * §9). Every tile and cutout piece this frame ever shows reaches the screen
 * through a plain `document.createElement('img')` with its `src` set once
 * (`frames/vector/imageryLayer.ts`'s own tiles included) — and this
 * document has no other reason to load an `<img>` — so watching every
 * `HTMLImageElement.prototype.src` assignment in this document is a
 * complete, engine-version-proof signal.
 */

/** How long a fully-settled (zero in-flight) state must hold before
 * `onChange(false)` fires. Panning loads many small tiles back-to-back;
 * without this the indicator would flicker on/off every few ms instead of
 * reading as one continuous "still loading" stretch. */
const SETTLE_MS = 200;

/** Installs the tracker; returns a cleanup that restores the original
 * `src` accessor (for tests — the frame itself never tears this down, it
 * lives as long as the document). `onChange` fires only on an actual
 * true/false transition, never redundantly. */
export function trackImageryLoading(onChange: (loading: boolean) => void): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
  // Every real browser (and jsdom) defines `src` as an accessor property on
  // the prototype. If that ever stops being true, staying silent is safer
  // than guessing — architecture policy invariant 7 cuts both ways: "unknown" must
  // never be reported as a false "yes, loading" either.
  if (descriptor?.get === undefined || descriptor.set === undefined) {
    return () => undefined;
  }
  // These browser accessors require an image receiver; the wrapper below
  // deliberately preserves it via Reflect.apply.
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const originalGet = descriptor.get;
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const originalSet = descriptor.set;

  let pending = 0;
  const active = new Map<HTMLImageElement, () => void>();
  let reported = false;
  let settleTimer: ReturnType<typeof setTimeout> | null = null;

  function report(loading: boolean): void {
    if (loading === reported) return;
    reported = loading;
    onChange(loading);
  }

  function oneFinished(): void {
    pending = Math.max(0, pending - 1);
    if (pending > 0) return;
    if (settleTimer !== null) clearTimeout(settleTimer);
    settleTimer = setTimeout(() => { report(false); }, SETTLE_MS);
  }

  Object.defineProperty(HTMLImageElement.prototype, "src", {
    configurable: true,
    enumerable: descriptor.enumerable,
    get(this: HTMLImageElement) {
      return Reflect.apply(originalGet, this, []) as string;
    },
    set(this: HTMLImageElement, value: string) {
      active.get(this)?.();
      if (value === "") {
        Reflect.apply(originalSet, this, [value]);
        return;
      }
      pending += 1;
      if (settleTimer !== null) {
        clearTimeout(settleTimer);
        settleTimer = null;
      }
      report(true);
      const image = this;
      const finish = (): void => {
        if (active.get(image) !== finish) return;
        image.removeEventListener("load", finish);
        image.removeEventListener("error", finish);
        active.delete(image);
        oneFinished();
      };
      active.set(image, finish);
      image.addEventListener("load", finish);
      image.addEventListener("error", finish);
      try {
        Reflect.apply(originalSet, image, [value]);
      } catch (error) {
        finish();
        throw error;
      }
    },
  });

  return () => {
    Object.defineProperty(HTMLImageElement.prototype, "src", descriptor);
    for (const [image, finish] of active) {
      image.removeEventListener("load", finish);
      image.removeEventListener("error", finish);
    }
    active.clear();
    if (settleTimer !== null) clearTimeout(settleTimer);
  };
}
