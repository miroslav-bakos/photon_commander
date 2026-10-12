// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { CutoutSourceRow } from "./cutoutLayers";
import { CutoutFetchIndicator } from "./CutoutFetchIndicator";

afterEach(cleanup);

function row(overrides: Partial<CutoutSourceRow> = {}): CutoutSourceRow {
  return {
    sourceId: "wise",
    label: "AllWISE (infrared)",
    credit: { label: "AllWISE", url: "https://irsa.ipac.caltech.edu/" },
    optional: false,
    state: { kind: "fetching", status: "running", bytes: 0 },
    ...overrides,
  };
}

describe("CutoutFetchIndicator", () => {
  it("renders nothing when there are no rows at all (map not yet ready)", () => {
    const { container } = render(<CutoutFetchIndicator rows={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when nothing is fetching or failed", () => {
    const { container } = render(
      <CutoutFetchIndicator rows={[row({ state: { kind: "ready", cutoutId: "job-1" } })]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("names the source while a fetch is in flight", () => {
    render(<CutoutFetchIndicator rows={[row()]} />);
    expect(screen.getByText(/Fetching/)).toHaveTextContent("Fetching AllWISE (infrared)…");
  });

  it("says Queued rather than Fetching for a queued job", () => {
    render(<CutoutFetchIndicator rows={[row({ state: { kind: "fetching", status: "queued", bytes: 0 } })]} />);
    expect(screen.getByText(/Queued/)).toHaveTextContent("Queued: AllWISE (infrared)…");
  });

  it("resolves once the fetch completes — nothing left on screen", () => {
    const { rerender, container } = render(<CutoutFetchIndicator rows={[row()]} />);
    expect(screen.getByText(/Fetching/)).toBeInTheDocument();
    rerender(<CutoutFetchIndicator rows={[row({ state: { kind: "ready", cutoutId: "job-1" } })]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("leaves a readable failure on screen, with the reason as text and detail as a tooltip", () => {
    render(
      <CutoutFetchIndicator
        rows={[
          row({
            state: {
              kind: "unavailable",
              reason: "AllWISE fetch failed",
              detail: "upstream 500",
              retryable: true,
              sizeDeg: 1.5,
            },
          }),
        ]}
      />,
    );
    expect(screen.getByText("AllWISE (infrared) fetch failed")).toBeInTheDocument();
    const reason = screen.getByText("AllWISE fetch failed");
    expect(reason).toHaveAttribute("title", "upstream 500");
  });

  it("never shows a footprint miss (retryable: false) as a failure", () => {
    const { container } = render(
      <CutoutFetchIndicator
        rows={[
          row({
            state: {
              kind: "unavailable",
              reason: "Legacy Surveys does not cover this field",
              detail: null,
              retryable: false,
              sizeDeg: 1.5,
            },
          }),
        ]}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
