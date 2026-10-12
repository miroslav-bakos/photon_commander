// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureTab } from "./CaptureTab";

vi.mock("../Observe/ObservingModeSwitch", () => ({ ObservingModeSwitch: () => <div>Capture / Live stack</div> }));
vi.mock("../Observe/ObservingSessionPanel", () => ({ ObservingSessionPanel: () => <div>Observing now</div> }));

vi.mock("../CapturePanel", () => ({ CapturePanel: () => <p>Capture form</p> }));
vi.mock("./CapturePreviewPanel", () => ({ CapturePreviewPanel: () => <p>Capture preview</p> }));
vi.mock("./CaptureStatusPanel", () => ({ CaptureStatusPanel: () => <p>Capture status</p> }));
vi.mock("./AutofocusChart", () => ({ AutofocusChart: () => <p>Autofocus chart</p> }));

afterEach(cleanup);

describe("CaptureTab", () => {
  it("contains capture work only after equipment controls move out", () => {
    render(<CaptureTab />);

    expect(screen.getByText("Capture status")).toBeInTheDocument();
    expect(screen.getByText("Capture form")).toBeInTheDocument();
    expect(screen.getByText("Capture preview")).toBeInTheDocument();
    expect(screen.getByText("Autofocus chart")).toBeInTheDocument();
    expect(screen.queryByText(/Focuser|Filter wheel|Dew heater|Dithering|Center after drift/)).not.toBeInTheDocument();
  });
});
