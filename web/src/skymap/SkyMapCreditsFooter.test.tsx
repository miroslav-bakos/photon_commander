// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { SkyMapCreditsFooter } from "./SkyMapCreditsFooter";
import { useSkyMapCreditsStore } from "./skyMapCreditsStore";

afterEach(() => {
  useSkyMapCreditsStore.setState({ mounted: false, credits: null });
});

function renderAt(pathname: string) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <SkyMapCreditsFooter />
    </MemoryRouter>,
  );
}

describe("SkyMapCreditsFooter", () => {
  it("shows skymap credits on the visible skymap screen", () => {
    useSkyMapCreditsStore.setState({
      mounted: true,
      credits: [{ label: "Legacy Surveys / D. Lang", url: "https://www.legacysurvey.org/" }],
    });

    renderAt("/planning");

    expect(screen.getByRole("link", { name: "Legacy Surveys / D. Lang" })).toHaveAttribute(
      "href",
      "https://www.legacysurvey.org/",
    );
  });

  it("does not leak credits onto other screens when Planning keeps the skymap mounted offscreen", () => {
    useSkyMapCreditsStore.setState({
      mounted: true,
      credits: [{ label: "Legacy Surveys / D. Lang", url: "https://www.legacysurvey.org/" }],
    });

    renderAt("/live");

    expect(screen.queryByText("Legacy Surveys / D. Lang")).not.toBeInTheDocument();
    expect(screen.queryByText("Loading survey attribution…")).not.toBeInTheDocument();
  });
});
