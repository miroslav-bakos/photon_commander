import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useRecentFrameQualities } from "./useRecentFrameQualities";

function Queue({ ids }: { ids: number[] }) {
  const results = useRecentFrameQualities(ids);
  return <output>{results.filter((result) => result.data !== undefined).length}</output>;
}
afterEach(() => { vi.unstubAllGlobals(); });

it("serializes ten cold quality requests, advances after errors, and aborts on unmount", async () => {
  const pending: { request: Request; resolve: (value: Response) => void }[] = [];
  vi.stubGlobal("fetch", vi.fn((request: Request) => new Promise<Response>((resolve) => pending.push({ request, resolve }))));
  const client = new QueryClient();
  const requestAt = (index: number) => { const request = pending[index]; if (request === undefined) throw new Error("Missing request"); return request; };
  const view = render(<Queue ids={[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]} />, { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
  await waitFor(() => { expect(pending).toHaveLength(1); });
  expect(requestAt(0).request.url).toContain("/1/quality");
  await act(async () => { requestAt(0).resolve(new Response(JSON.stringify({ hfr_px: 2, fwhm_px: 3 }))); await Promise.resolve(); });
  await waitFor(() => { expect(pending).toHaveLength(2); });
  expect(screen.getByRole("status")).toHaveTextContent("1");
  await act(async () => { requestAt(1).resolve(new Response("failed", { status: 500 })); await Promise.resolve(); });
  await waitFor(() => { expect(pending).toHaveLength(3); });
  expect(requestAt(2).request.url).toContain("/3/quality");
  // Inserting a new frame must not run alongside the existing request.
  view.rerender(<Queue ids={[11, 3, 4, 5, 6, 7, 8, 9, 10]} />);
  expect(pending).toHaveLength(3);
  view.unmount();
  expect(requestAt(2).request.signal.aborted).toBe(true);
  await act(async () => { requestAt(2).resolve(new Response("{}")); await Promise.resolve(); });
  expect(pending).toHaveLength(3);
  client.clear();
});

it("reuses a completed latest-frame query without fetching that frame again", async () => {
  const client = new QueryClient();
  client.setQueryData(["capture", "frame-quality", 5], { hfr_px: 2, fwhm_px: 3 });
  const fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ hfr_px: null, fwhm_px: null }))));
  vi.stubGlobal("fetch", fetch);
  const view = render(<Queue ids={[5, 6, 5]} />, { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
  await waitFor(() => { expect(screen.getByRole("status")).toHaveTextContent("2"); });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect((fetch.mock.calls[0] as unknown as [Request])[0].url).toContain("/6/quality");
  view.unmount();
  client.clear();
});

it("retries a cached failure once per visit and continues after another failure", async () => {
  const client = new QueryClient();
  await client.query({ queryKey: ["capture", "frame-quality", 1], queryFn: () => Promise.reject(new Error("old 503")), retry: false }).catch(() => undefined);
  const fetch = vi.fn((request: Request) => Promise.resolve(request.url.includes("/1/quality")
    ? new Response("busy", { status: 503 })
    : new Response(JSON.stringify({ hfr_px: 2, fwhm_px: 3 }))));
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const first = render(<Queue ids={[1, 2]} />, { wrapper });
  await waitFor(() => { expect(screen.getByRole("status")).toHaveTextContent("1"); });
  expect(fetch).toHaveBeenCalledTimes(2);
  first.unmount();
  const second = render(<Queue ids={[1, 2]} />, { wrapper });
  await waitFor(() => { expect(fetch).toHaveBeenCalledTimes(3); });
  await waitFor(() => { expect(client.getQueryState(["capture", "frame-quality", 1])?.fetchStatus).toBe("idle"); });
  expect(fetch).toHaveBeenCalledTimes(3);
  second.unmount();
  client.clear();
});
