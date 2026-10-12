// SPDX-License-Identifier: GPL-3.0-only
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useAnnualAltitudeQuery, useNightQuery, type NightRequest } from "./astro";

const fixture = vi.hoisted(() => ({
  post: vi.fn(),
  location: { latitude_deg: 48, longitude_deg: 17, elevation_m: 100, acquired_at: "old" },
  horizon: { active: false, points: [] as { azimuth_deg: number; altitude_deg: number }[] },
}));
vi.mock("./client", () => ({ api: { POST: fixture.post }, unwrap: async (value: Promise<{data:unknown}>) => (await value).data }));
vi.mock("../store/connectionStore", () => ({
  useConnectionStore: (selector: (state: unknown) => unknown) => selector({ snapshot: { location: fixture.location } }),
}));
vi.mock("./settings", () => ({ useSettingsQuery: () => ({data:{horizon:fixture.horizon}}) }));
const clients: QueryClient[] = [];
afterEach(() => { for (const client of clients) client.clear(); clients.length=0; fixture.post.mockReset(); });
function setup() {
  const client = new QueryClient({defaultOptions:{queries:{retry:false}}});clients.push(client);
  fixture.post.mockResolvedValue({data:{nights:[],solar_events:[]}});
  return ({children}:{children:ReactNode}) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
it("reuses an annual calculation when the chart is reopened, but recalculates at a new site", async () => {
  const body={ra_j2000_h:1,dec_j2000_deg:10,start_time:"2026-10-05T10:00:00Z",nights:365,min_altitude_deg:20,step_minutes:15};
  const wrapper=setup();
  const first=renderHook(()=>useAnnualAltitudeQuery(body),{wrapper});
  await waitFor(()=> { expect(first.result.current.isSuccess).toBe(true); });first.unmount();
  const second=renderHook(()=>useAnnualAltitudeQuery(body),{wrapper});
  await waitFor(()=> { expect(second.result.current.isSuccess).toBe(true); });
  expect(fixture.post).toHaveBeenCalledTimes(1);
  fixture.location={...fixture.location,acquired_at:"new"};second.rerender();
  expect(fixture.post).toHaveBeenCalledTimes(1);
  fixture.location={...fixture.location,latitude_deg:49};second.rerender();
  await waitFor(()=> { expect(fixture.post).toHaveBeenCalledTimes(2); });second.unmount();
});
it("recalculates a cached night when the saved skyline changes", async () => {
  const body={start_time:"2026-10-05T10:00:00Z",end_time:"2026-10-06T10:00:00Z",min_altitude_deg:20,altitude_samples:145} as NightRequest;
  const hook=renderHook(()=>useNightQuery(body),{wrapper:setup()});
  await waitFor(()=> { expect(hook.result.current.isSuccess).toBe(true); });
  fixture.horizon={active:true,points:[{azimuth_deg:0,altitude_deg:30},{azimuth_deg:180,altitude_deg:30}]};hook.rerender();
  await waitFor(()=> { expect(fixture.post).toHaveBeenCalledTimes(2); });hook.unmount();
});
