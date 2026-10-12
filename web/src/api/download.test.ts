// SPDX-License-Identifier: GPL-3.0-only
import { afterEach, expect, it, vi } from "vitest";
import { readDownloadBlob } from "./download";
afterEach(() => { vi.useRealTimers(); });
it("releases a stalled ZIP download and cancels its reader", async () => {
  vi.useFakeTimers();
  const cancel = vi.fn();
  const response = new Response(new ReadableStream({ cancel }), { headers: { "Content-Type": "application/zip" } });
  const assertion = expect(readDownloadBlob(response)).rejects.toThrow("Download interrupted");
  await vi.advanceTimersByTimeAsync(30_001);
  await assertion;
  expect(cancel).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});
it("allows a progressing download to take longer than 30 seconds", async () => {
  vi.useFakeTimers();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(new ReadableStream<Uint8Array>({ start(value) { controller = value; } }), { headers: { "Content-Type": "application/zip" } });
  const result = readDownloadBlob(response);
  for (let i = 0; i < 8; i++) {
    await vi.advanceTimersByTimeAsync(10_000);
    controller.enqueue(new Uint8Array([i]));
    await Promise.resolve();
  }
  controller.close();
  const blob = await result;
  expect(blob.size).toBe(8);
  expect(blob.type).toBe("application/zip");
  expect(vi.getTimerCount()).toBe(0);
});
