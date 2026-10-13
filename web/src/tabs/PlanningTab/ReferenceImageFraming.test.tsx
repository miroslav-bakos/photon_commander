import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { ReferenceImageFraming } from "./ReferenceImageFraming";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), wait: vi.fn() }));
vi.mock("../../performance/diagnostics", () => ({ timedApiFetch: mocks.fetch }));
vi.mock("../../api/operations", () => ({ createCommandKey: () => "reference", waitForOperation: mocks.wait }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const solution = { ra_j2000_h: 23.123456, dec_j2000_deg: -45.654321, rotation_deg: 15, pixel_scale_arcsec: 2, solver: "astap", solver_version: null };
function setup(key = "original") {
  const onSolved = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const view = (framingKey: string) => <QueryClientProvider client={client}><ReferenceImageFraming framingKey={framingKey} onSolved={onSolved} /></QueryClientProvider>;
  const rendered = render(view(key));
  const upload = (file = new File(["pixels"], "reference.fits")) => fireEvent.change(screen.getByLabelText(/Frame from picture/), { target: { files: [file] } });
  mocks.fetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ id: "operation" }) });
  return { ...rendered, onSolved, upload, view };
}
it("shows loading then applies exact solved coordinates", async () => {
  let finish!: (value: typeof solution) => void;
  mocks.wait.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const test = setup(); test.upload();
  fireEvent.click(screen.getByRole("button", { name: "Solve and place blue FOV" }));
  expect(await screen.findByText("Plate solving reference image")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Solving picture…" })).toBeDisabled();
  await act(async () => { finish(solution); await Promise.resolve(); });
  await waitFor(() => { expect(test.onSolved).toHaveBeenCalledWith(23.123456, -45.654321); });
  expect(screen.getByText("Solved position applied.")).toBeInTheDocument();
});
it("keeps manual framing until the owner explicitly applies a pending result", async () => {
  let finish!: (value: typeof solution) => void;
  mocks.wait.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const test = setup(); test.upload(); fireEvent.click(screen.getByRole("button", { name: "Solve and place blue FOV" }));
  await screen.findByText("Plate solving reference image");
  test.rerender(test.view("manual change"));
  await act(async () => { finish(solution); await Promise.resolve(); });
  const apply = await screen.findByRole("button", { name: "Apply solved position" });
  expect(test.onSolved).not.toHaveBeenCalled(); fireEvent.click(apply);
  expect(test.onSolved).toHaveBeenCalledWith(23.123456, -45.654321);
});
it("shows solver failure and allows retry", async () => {
  mocks.wait.mockRejectedValue(new Error("No stars found"));
  const test = setup(); test.upload(); fireEvent.click(screen.getByRole("button", { name: "Solve and place blue FOV" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("No stars found");
  expect(test.onSolved).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Solve and place blue FOV" })).toBeEnabled();
});
it("ignores a solution after the panel unmounts", async () => {
  let finish!: (value: typeof solution) => void;
  mocks.wait.mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const test = setup(); test.upload(); fireEvent.click(screen.getByRole("button", { name: "Solve and place blue FOV" }));
  await screen.findByText("Plate solving reference image"); test.unmount();
  await act(async () => { finish(solution); await Promise.resolve(); }); expect(test.onSolved).not.toHaveBeenCalled();
});
it("rejects an oversized upload before sending any pixels", async () => {
  const test = setup(); const file = new File(["pixels"], "huge.fits");
  Object.defineProperty(file, "size", { value: 64 * 1024 * 1024 + 1 }); test.upload(file);
  const button = screen.getByRole("button", { name: "Solve and place blue FOV" });
  if (!button.hasAttribute("disabled")) fireEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent(/64 MiB/);
  expect(mocks.fetch).not.toHaveBeenCalled(); expect(test.onSolved).not.toHaveBeenCalled();
});
