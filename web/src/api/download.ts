// SPDX-License-Identifier: GPL-3.0-only

/** Bound a stalled download, not its total runtime: large ZIPs may take minutes. */
export async function readDownloadBlob(response: Response): Promise<Blob> {
  if (response.body === null) return new Blob([], { type: response.headers.get("content-type") ?? "" });
  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  try {
    for (;;) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const chunk = await Promise.race([
          reader.read(),
          new Promise<never>((_, reject) => {
            timer = setTimeout(() => { reject(new Error("Download interrupted: no data received for 30 seconds. Reconnect and retry the download.")); }, 30_000);
          }),
        ]);
        if (chunk.done) break;
        chunks.push(new Uint8Array(chunk.value));
      } finally { if (timer !== undefined) clearTimeout(timer); }
    }
    return new Blob(chunks, { type: response.headers.get("content-type") ?? "" });
  } catch (error) {
    // Cancellation must not hold the error UI hostage if the network hangs.
    void reader.cancel().catch(() => undefined);
    throw error;
  } finally { reader.releaseLock(); }
}
