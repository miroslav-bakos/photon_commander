// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { timedApiFetch } from "../performance/diagnostics";
import { createCommandKey, waitForOperation, type OperationState } from "./operations";

export interface ReferenceImageSolution {
  ra_j2000_h: number;
  dec_j2000_deg: number;
  rotation_deg: number;
  pixel_scale_arcsec: number;
  solver: string;
  solver_version: string | null;
}

export async function solveReferenceImage(file: File): Promise<ReferenceImageSolution> {
  const response = await timedApiFetch(new Request(new URL("/api/skymap/solve-reference", window.location.origin), {
    method: "POST", credentials: "same-origin", body: file,
    headers: { "Content-Type": file.type || "application/octet-stream", "Idempotency-Key": createCommandKey() },
  }));
  if (!response.ok) {
    const body = await response.json() as { detail?: string };
    throw new Error(body.detail ?? "Could not solve the uploaded image.");
  }
  let result: ReferenceImageSolution;
  try {
    result = await waitForOperation<ReferenceImageSolution>(await response.json() as OperationState);
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("ValueError: ")) {
      throw new Error(error.message.slice("ValueError: ".length));
    }
    throw error;
  }
  if (!Number.isFinite(result.ra_j2000_h) || result.ra_j2000_h < 0 || result.ra_j2000_h >= 24 ||
      !Number.isFinite(result.dec_j2000_deg) || Math.abs(result.dec_j2000_deg) > 90) {
    throw new Error("The plate solver returned an invalid image solution.");
  }
  return result;
}

