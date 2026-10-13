// SPDX-License-Identifier: GPL-3.0-only
import { afterEach, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { useLiveStackQuery } from "./capture";
import { renderWithQueryClient } from "../test/queryClient";
function Reading() {
  const query = useLiveStackQuery(true);
  return <p>{query.isError ? "failed" : query.data === null ? "idle" : "waiting"}</p>;
}
afterEach(() => vi.unstubAllGlobals());
it.each([
  [404, "no live stack has been started", "idle"],
  [404, "route not found", "failed"],
  [503, "no live stack has been started", "failed"],
])("classifies HTTP %s / %s as %s", async (status, detail, expected) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } })));
  renderWithQueryClient(<Reading />);
  await waitFor(() => expect(screen.getByText(expected)).toBeInTheDocument());
});
