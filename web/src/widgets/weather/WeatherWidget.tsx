// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Badge } from "../../components/Badge";
import { nearestForecastPoint, useForecastQuery, type ForecastPoint } from "../../api/weather";
import { useConnectionStore } from "../../store/connectionStore";
import { formatClock } from "../tonight/moonEventMessage";
import { Disclosure, TonightDetails } from "../tonight/TonightWidget";
import { MoonGlyph } from "../tonight/MoonGlyph";
import { isTonightRouteMissing, useTonightQuery } from "../tonight/api";
import { describeNightQuality } from "./nightConditions";
import { WeatherAttribution } from "./WeatherAttribution";
import { NightForecastMiniCalendar } from "./NightForecastMiniCalendar";

function WeatherGlyph({ point }: { point: ForecastPoint | null }) {
  const cloud = point?.cloud_total;
  const label = point?.temp_c !== null && point?.temp_c !== undefined ? `Weather ${String(Math.round(point.temp_c))}°C` : "Weather";
  return (
    <span className="inline-flex h-7 w-7 items-center justify-center" role="img" aria-label={label}>
      <svg viewBox="0 0 24 24" className="h-6 w-6 text-accent" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
        <circle cx="16.5" cy="7.5" r="3.2" fill="currentColor" opacity={cloud !== null && cloud !== undefined && cloud < 50 ? 0.2 : 0.08} stroke="none" />
        <path d="M7.5 18h9.2a3.3 3.3 0 0 0 .3-6.6 5.2 5.2 0 0 0-10-1.2A3.9 3.9 0 0 0 7.5 18Z" fill="currentColor" opacity=".16" />
        <path d="M7.5 18h9.2a3.3 3.3 0 0 0 .3-6.6 5.2 5.2 0 0 0-10-1.2A3.9 3.9 0 0 0 7.5 18Z" />
      </svg>
    </span>
  );
}

function weatherValue(value: number | null, suffix = "") {
  return value === null ? "—" : `${String(value)}${suffix}`;
}

function windDirection(degrees: number | null | undefined): string {
  if (degrees === null || degrees === undefined) return "—";
  const directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const direction = directions[Math.round((((degrees % 360) + 360) % 360) / 45) % 8] ?? "—";
  return `${direction} (${String(Math.round(degrees))}°)`;
}

export function WeatherWidget() {
  const location = useConnectionStore((s) => s.snapshot?.location ?? null);
  const forecast = useForecastQuery(location !== null);
  const tonight = useTonightQuery(location !== null);

  if (location === null) {
    return (
      <Disclosure trigger={<Badge tone="neutral">Weather</Badge>} label="Weather and forecast calendar">
        No observing location is set. Add one in Settings → Location to see weather, nightly conditions, and moon data.
      </Disclosure>
    );
  }
  const data = forecast.data;
  const point = data === undefined ? null : nearestForecastPoint(data.points);
  const timezone = location.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  const quality = describeNightQuality(tonight.data, tonight.isPending, tonight.isError);
  const hemisphere = location.latitude_deg < 0 ? "south" : "north";
  const trigger = (
    <>
      <WeatherGlyph point={point} />
      <span className="text-xs font-medium text-fg">
        {forecast.isPending ? "…" : point?.temp_c === null || point === null ? "—" : `${String(Math.round(point.temp_c))}°C`}
      </span>
      {tonight.data !== undefined ? (
        <>
          <MoonGlyph
            illuminatedFraction={tonight.data.moon.illuminated_fraction}
            waxing={tonight.data.moon.waxing}
            hemisphere={hemisphere}
            size={20}
          />
          <Badge tone={quality.tone} className="max-w-20 truncate text-[10px]">{quality.label}</Badge>
        </>
      ) : tonight.isError && !isTonightRouteMissing(tonight.error) ? (
        <Badge tone="danger" className="text-[10px]">Tonight</Badge>
      ) : null}
    </>
  );

  return (
    <Disclosure trigger={trigger} label="Weather and forecast calendar" lazy>
      <div className="max-h-[min(80dvh,36rem)] space-y-3 overflow-y-auto overscroll-contain">
        <NightForecastMiniCalendar points={data?.points ?? []} timezone={timezone} />
        {point !== null ? (
          <div className="space-y-2 border-t border-border pt-2">
            <p className="font-medium text-fg">Current weather</p>
            <p className="text-xs text-fg-muted">As of {formatClock(point.t)} · {point.source}</p>
            <WeatherAttribution source={point.source} />
            <dl className="grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
              <dt className="text-fg-muted">Temperature</dt><dd className="text-right text-fg">{weatherValue(point.temp_c, "°C")}</dd>
              <dt className="text-fg-muted">Cloud total</dt><dd className="text-right text-fg">{weatherValue(point.cloud_total, "%")}</dd>
              <dt className="text-fg-muted">Cloud layers</dt><dd className="text-right text-fg">{weatherValue(point.cloud_low, "%")} / {weatherValue(point.cloud_mid, "%")} / {weatherValue(point.cloud_high, "%")}</dd>
              <dt className="text-fg-muted">Humidity</dt><dd className="text-right text-fg">{weatherValue(point.humidity, "%")}</dd>
              <dt className="text-fg-muted">Dew point</dt><dd className="text-right text-fg">{weatherValue(point.dew_point_c, "°C")}</dd>
              <dt className="text-fg-muted">Wind</dt><dd className="text-right text-fg">{point.wind_ms === null ? "—" : `${(point.wind_ms * 3.6).toFixed(0)} km/h`}</dd>
              <dt className="text-fg-muted">Wind direction</dt><dd className="text-right text-fg">{windDirection(point.wind_direction_deg)}</dd>
              <dt className="text-fg-muted">Precipitation</dt><dd className="text-right text-fg">{weatherValue(point.precip_prob, "%")}</dd>
              <dt className="text-fg-muted">Visibility</dt><dd className="text-right text-fg">{point.visibility_m === null ? "—" : `${(point.visibility_m / 1000).toFixed(1)} km`}</dd>
            </dl>
            {data?.stale_since !== null && data?.stale_since !== undefined && <p className="border-t border-border pt-2 text-xs text-warning">Forecast is stale — fetched {formatClock(data.fetched_at)}.</p>}
          </div>
        ) : (
          <p className="border-t border-border pt-2 text-xs text-warning">
            {forecast.isPending ? "Loading current weather…" : forecast.isError ? "Could not load current weather." : "No current forecast is available."}
          </p>
        )}
        <section className="space-y-2 border-t border-border pt-2">
          <h3 className="text-xs font-semibold text-fg">Tonight and moon</h3>
          {tonight.data !== undefined ? (
            <TonightDetails tonight={tonight.data} showWeatherSummary={false} />
          ) : tonight.isPending ? (
            <p className="text-xs text-fg-muted">Loading tonight’s conditions…</p>
          ) : isTonightRouteMissing(tonight.error) ? (
            <p className="text-xs text-fg-muted">Tonight’s forecast is not available on this server.</p>
          ) : (
            <p className="text-xs text-warning">Could not load tonight’s conditions.</p>
          )}
        </section>
      </div>
    </Disclosure>
  );
}
