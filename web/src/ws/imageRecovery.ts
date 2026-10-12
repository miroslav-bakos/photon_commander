// SPDX-License-Identifier: GPL-3.0-only
/** Keep the resource identity but retry browser image loads after resync. */
export function recoveredImageSrc(src: string, generation: number, retry = 0): string {
  if ((generation === 0 && retry === 0) || !src.startsWith("/api/")) return src;
  const url = new URL(src, window.location.origin);
  url.searchParams.set("_reconnect", `${String(generation)}.${String(retry)}`);
  return `${url.pathname}${url.search}${url.hash}`;
}
