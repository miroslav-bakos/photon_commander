// SPDX-License-Identifier: GPL-3.0-only
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BugReportPanel } from "./BugReportPanel";
const post = vi.hoisted(() => vi.fn());
vi.mock("../../api/client", () => ({ api: { POST: post }, unwrap: (value: unknown) => Promise.resolve(value) }));

describe("Bug report consent", () => {
  beforeEach(() => { post.mockReset(); });
  it("announces preparation immediately and recovers after failure", async () => {
    let rejectPreview!: (reason: Error) => void;
    post.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPreview = reject; }));
    render(<BugReportPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Prepare bug report" }));
    expect(screen.getByRole("button", { name: "Preparing report…" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("nothing is being sent");
    rejectPreview(new Error("offline"));
    await screen.findByText(/Could not prepare the report/);
    expect(screen.getByRole("button", { name: "Prepare bug report" })).toBeEnabled();
  });

  it("sends exactly the preview only after deliberate consent", async () => {
    const report = { schema_version: 1, build: "development", report_id: "a".repeat(32), category: "other", events: [], logging_enabled: true };
    post.mockResolvedValueOnce(report).mockResolvedValueOnce({ status: "accepted" });
    render(<BugReportPanel />);
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox", { name: "What happened? (optional)" }), { target: { value: "Camera stopped after the second frame." } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare bug report" }));
    const send = await screen.findByRole("button", { name: "Send bug report" });
    expect(send).toBeDisabled();
    expect(post).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(send);
    await waitFor(() => { expect(post).toHaveBeenCalledWith("/api/bug-reports/send", { body: { report_id: report.report_id, details: "Camera stopped after the second frame." } }); });
    await screen.findByText(/Accepted for email delivery/);
    expect(send).toBeDisabled();
  });
  it("requires a fresh preview after editing details following uncertain delivery", async () => {
    const report = { schema_version: 1, build: "development", report_id: "a".repeat(32), category: "other", events: [], logging_enabled: true };
    post.mockResolvedValueOnce(report).mockRejectedValueOnce(new Error("response lost"));
    render(<BugReportPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Prepare bug report" }));
    const send = await screen.findByRole("button", { name: "Send bug report" });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(send);
    await screen.findByText(/Delivery was not confirmed/);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Updated details" } });
    expect(screen.queryByRole("button", { name: "Send bug report" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prepare bug report" })).toBeEnabled();
  });

});
