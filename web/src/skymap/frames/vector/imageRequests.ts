// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
/** Reserve browser HTTP capacity for normal API traffic. Shared by all imagery
 * sources in this iframe; queued requests carry no active image URL yet. */
interface RequestJob {
  image: HTMLImageElement;
  url: string;
  active: boolean;
  load: HTMLImageElement["onload"];
  error: HTMLImageElement["onerror"];
}

export class ImageRequestDispatcher {
  private readonly jobs = new Map<HTMLImageElement, RequestJob>();
  private queued: RequestJob[] = [];
  private active = 0;
  private batching = 0;

  constructor(private readonly limit = 4) {
    if (!Number.isInteger(limit) || limit < 1) throw new Error("image request limit must be positive");
  }

  batch<T>(work: () => T): T {
    this.batching += 1;
    try { return work(); } finally { this.batching -= 1; this.pump(); }
  }

  request(image: HTMLImageElement, url: string): void {
    if (this.jobs.has(image)) return;
    const job: RequestJob = { image, url, active: false, load: image.onload, error: image.onerror };
    this.jobs.set(image, job);
    image.onload = (event) => this.complete(job, () => job.load?.call(image, event));
    image.onerror = (event) => this.complete(job, () => job.error?.call(image, event));
    this.queued.push(job);
    this.pump();
  }

  cancelMany(images: readonly HTMLImageElement[]): void {
    this.batch(() => {
      for (const image of images) {
        const job = this.jobs.get(image);
        if (job === undefined) continue;
        this.jobs.delete(image);
        if (job.active) this.active -= 1;
        image.onload = null;
        image.onerror = null;
        if (job.active) image.src = "";
      }
      this.queued = this.queued.filter((job) => this.jobs.has(job.image));
    });
  }

  private complete(job: RequestJob, notify: () => unknown): void {
    // Cancellation may race an already queued load/error callback.
    if (this.jobs.get(job.image) !== job || !job.active) return;
    this.jobs.delete(job.image);
    this.active -= 1;
    try { notify(); } finally { this.pump(); }
  }

  private pump(): void {
    if (this.batching > 0) return;
    while (this.active < this.limit && this.queued.length > 0) {
      // Latest viewport requests precede older still-visible queued tiles.
      const job = this.queued.pop();
      if (job === undefined || this.jobs.get(job.image) !== job) continue;
      job.active = true;
      this.active += 1;
      job.image.src = job.url;
    }
  }
}

export const imageryImageRequests = new ImageRequestDispatcher(4);
