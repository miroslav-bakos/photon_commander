// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState, type SyntheticEvent } from "react";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import {
  useClearLocationFixMutation,
  useRecordLocationFixMutation,
  useSetPreferredSourceMutation,
  useSettingsQuery,
} from "../../api/settings";
import type { components } from "../../api/schema";

type LocationSource = components["schemas"]["LocationSource"];

const SOURCE_LABEL: Record<LocationSource, string> = {
  manual: "Manual",
  browser: "Browser",
  device: "Device",
  ip: "IP geolocation",
};

const SOURCE_ORDER: LocationSource[] = ["manual", "browser", "device", "ip"];

/**
 * DESIGN.md §5: manual > browser > device > IP, manual always wins unless a
 * different preferred source is pinned. The active source is always shown —
 * "a silently wrong location produces silently wrong twilight times".
 */
export function LocationPanel() {
  const settings = useSettingsQuery();
  const recordFix = useRecordLocationFixMutation();
  const clearFix = useClearLocationFixMutation();
  const setPreferred = useSetPreferredSourceMutation();
  const [geoError, setGeoError] = useState<string | null>(null);
  const [geoBusy, setGeoBusy] = useState(false);
  const browserLocationAvailable = window.isSecureContext;

  function useBrowserLocation() {
    setGeoError(null);
    if (!("geolocation" in navigator)) {
      setGeoError("This browser does not support geolocation.");
      return;
    }
    // The Geolocation API refuses to run at all outside a secure context
    // (HTTPS, or the browser's own machine via localhost/127.0.0.1) — a
    // browser-enforced rule this app cannot work around, not a bug in the
    // request below. Reported live: "getting gps ... does not work - maybe
    // browser location access?" — exactly this, from a phone reached over
    // the plain-HTTP LAN address the pairing QR code points at. Checked and
    // reported honestly *before* calling getCurrentPosition, whose own
    // error for this case is a browser-specific, easy-to-miss string
    // ("Only secure origins are allowed", Chrome; something else
    // elsewhere) rather than an explained cause with a real next step
    // (architecture policy invariant 7 — never a silently confusing failure).
    if (!window.isSecureContext) {
      setGeoError(
        "Browser location needs a secure connection, which this app does not have when reached over " +
          "the plain http:// LAN address (the one the pairing QR code uses). Use Manual entry below, or " +
          "open this app as http://localhost on the machine running ssc-core itself, where it does work.",
      );
      return;
    }
    setGeoBusy(true);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setGeoBusy(false);
        recordFix.mutate({
          source: "browser",
          latitude_deg: position.coords.latitude,
          longitude_deg: position.coords.longitude,
          elevation_m: position.coords.altitude ?? undefined,
          accuracy_m: position.coords.accuracy,
        });
      },
      (error) => {
        setGeoBusy(false);
        setGeoError(error.message);
      },
      { enableHighAccuracy: true, timeout: 15000 },
    );
  }

  if (settings.isPending) return <Card>Loading location…</Card>;
  if (settings.isError) {
    return (
      <Card className="text-danger">Could not load location settings: {String(settings.error)}</Card>
    );
  }

  const { location } = settings.data;

  return (
    <div className="space-y-4">
      <Card>
        <CardTitle>Active location</CardTitle>
        {location.active === null ? (
          <p className="text-sm text-fg-muted">
            No location set yet. Add one manually
            {browserLocationAvailable ? " or use the browser below" : ""}.
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge tone="accent">{SOURCE_LABEL[location.active.source]}</Badge>
            <span>
              {location.active.latitude_deg.toFixed(4)}°, {location.active.longitude_deg.toFixed(4)}°
              {location.active.elevation_m !== null && ` · ${location.active.elevation_m.toFixed(0)} m`}
            </span>
            {location.active.timezone !== null && (
              <span className="text-fg-muted">{location.active.timezone}</span>
            )}
          </div>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label htmlFor="preferred-source" className="text-sm text-fg-muted">
            Preferred source
          </label>
          <select
            id="preferred-source"
            /* A <select> sizes itself to its WIDEST OPTION and will not shrink
               below it. "Automatic (manual > browser > device > IP)" has a
               min-content width of 359 px, which at 390 px pushed the whole
               Settings page to 409 px and made it scroll sideways — verified in
               a real browser, and present before this card was ever regrouped.
               Full width on a phone, intrinsic width from `sm` up. */
            className="min-h-11 w-full min-w-0 max-w-full rounded-md border border-border-strong bg-bg px-3 text-fg sm:w-auto"
            value={location.preferred_source ?? ""}
            onChange={(e) => {
              const value = e.target.value;
              setPreferred.mutate(value === "" ? null : (value as LocationSource));
            }}
          >
            <option value="">Automatic (manual &gt; browser &gt; device &gt; IP)</option>
            {SOURCE_ORDER.map((source) => (
              <option key={source} value={source}>
                {SOURCE_LABEL[source]}
              </option>
            ))}
          </select>
        </div>
      </Card>

      {browserLocationAvailable && (
        <Card>
          <CardTitle>Browser location</CardTitle>
          <Button variant="primary" onClick={useBrowserLocation} disabled={geoBusy}>
            {geoBusy ? "Requesting…" : "Use this browser's location"}
          </Button>
          {geoError !== null && (
            <p role="alert" className="mt-2 text-sm text-danger">
              {geoError}
            </p>
          )}
        </Card>
      )}

      <Card>
        <CardTitle>Manual location</CardTitle>
        <ManualLocationForm onSubmit={(fix) => { recordFix.mutate(fix); }} pending={recordFix.isPending} />
      </Card>

      <Card>
        <CardTitle>All sources</CardTitle>
        <ul className="space-y-2 text-sm">
          {SOURCE_ORDER.map((source) => {
            const fix = location.sources[source];
            return (
              <li
                key={source}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-2"
              >
                <div>
                  <span className="font-medium text-fg">{SOURCE_LABEL[source]}</span>{" "}
                  {fix === undefined ? (
                    <span className="text-fg-muted">no fix recorded</span>
                  ) : (
                    <span className="text-fg-muted">
                      {fix.latitude_deg.toFixed(4)}°, {fix.longitude_deg.toFixed(4)}° · acquired{" "}
                      {fix.acquired_at}
                    </span>
                  )}
                </div>
                {fix !== undefined && (
                  <Button
                    variant="secondary"
                    onClick={() => {
                      clearFix.mutate(source);
                    }}
                  >
                    Clear
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      </Card>
    </div>
  );
}

function ManualLocationForm({
  onSubmit,
  pending,
}: {
  onSubmit: (fix: components["schemas"]["LocationFixRequest"]) => void;
  pending: boolean;
}) {
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [elevation, setElevation] = useState("");
  const [timezone, setTimezone] = useState("");

  function decimal(value: string): number {
    const trimmed = value.trim();
    if (!/^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)$/.test(trimmed)) return NaN;
    return Number(trimmed.replace(",", "."));
  }
  const latitude = decimal(lat);
  const longitude = decimal(lon);
  const height = elevation.trim() === "" ? undefined : decimal(elevation);
  const valid = Number.isFinite(latitude) && Math.abs(latitude) <= 90
    && Number.isFinite(longitude) && Math.abs(longitude) <= 180
    && (height === undefined || Number.isFinite(height));

  function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid) return;
    onSubmit({
      source: "manual",
      latitude_deg: latitude,
      longitude_deg: longitude,
      elevation_m: height,
      timezone: timezone.trim() === "" ? undefined : timezone.trim(),
    });
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
      <div className="w-full space-y-2 text-sm text-fg-muted">
        <p>Find the exact place where you will observe in Google Maps. On a computer, right-click the place and copy its coordinates. On a phone, press and hold the place to drop a pin and view its coordinates.</p>
        <p>Enter latitude first, then longitude, in decimal degrees (for example 49.5555 and 18.5555). A decimal point or comma is accepted. Keep any minus sign. Latitude must be between −90 and 90; longitude between −180 and 180.</p>
        <p className="flex flex-wrap gap-4">
          <a href="https://www.google.com/maps" target="_blank" rel="noopener noreferrer" className="underline">Open Google Maps</a>
          <a href="https://support.google.com/maps/answer/18539" target="_blank" rel="noopener noreferrer" className="underline">How to find coordinates</a>
        </p>
      </div>
      <Field label="Latitude" value={lat} onChange={(e) => { setLat(e.target.value); }} inputMode="decimal" className="w-32" />
      <Field label="Longitude" value={lon} onChange={(e) => { setLon(e.target.value); }} inputMode="decimal" className="w-32" />
      <Field label="Elevation (m)" value={elevation} onChange={(e) => { setElevation(e.target.value); }} inputMode="decimal" className="w-32" />
      <Field label="Timezone (IANA)" value={timezone} onChange={(e) => { setTimezone(e.target.value); }} placeholder="e.g. Europe/London" className="w-48" />
      <Button type="submit" variant="primary" disabled={pending || !valid}>
        {pending ? "Saving…" : "Save"}
      </Button>
    </form>
  );
}
