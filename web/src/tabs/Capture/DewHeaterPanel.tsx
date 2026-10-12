// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import {
  useHeaterAutomationQuery,
  useHeaterQuery,
  useSetHeaterAutomationMutation,
  useSetHeaterMutation,
} from "../../api/heater";
import { SegmentedControl } from "../../components/SegmentedControl";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { nearestForecastPoint, useForecastQuery } from "../../api/weather";
import { useConnectionStore } from "../../store/connectionStore";
import { deviceNotesSayDisconnected } from "../../api/deviceStatus";
import { WeatherAttribution } from "../../widgets/weather/WeatherAttribution";
import { useAlpacaDeviceConnected } from "../../api/deviceAvailability";

export const DEW_HEATER_RECOMMEND_MARGIN_C = 5;

export function DewHeaterPanel() {
  const connected = useAlpacaDeviceConnected("switch");
  const heater = useHeaterQuery();
  const setHeater = useSetHeaterMutation();
  const automation = useHeaterAutomationQuery();
  const setAutomation = useSetHeaterAutomationMutation();
  const [onMarginTouched, setOnMarginTouched] = useState(false);
  const [offMarginTouched, setOffMarginTouched] = useState(false);
  const [onMarginText, setOnMarginText] = useState("5");
  const [offMarginText, setOffMarginText] = useState("7");
  const enabled = heater.data?.enabled;
  const disconnected = !connected || deviceNotesSayDisconnected(heater.data?.note);
  const hasLocation = useConnectionStore((state) => state.snapshot?.location != null);
  const forecast = useForecastQuery(hasLocation);
  const current = forecast.data === undefined ? null : nearestForecastPoint(forecast.data.points);
  const dewMargin = current?.temp_c == null || current.dew_point_c == null
    ? null
    : current.temp_c - current.dew_point_c;
  const weatherIsFresh = forecast.data?.stale_since == null;
  const heatRecommended = weatherIsFresh && dewMargin !== null && dewMargin <= DEW_HEATER_RECOMMEND_MARGIN_C;
  const displayedOnMargin = onMarginTouched
    ? onMarginText
    : String(automation.data?.on_margin_c ?? 5);
  const displayedOffMargin = offMarginTouched
    ? offMarginText
    : String(automation.data?.off_margin_c ?? 7);
  const onMargin = Number(displayedOnMargin);
  const offMargin = Number(displayedOffMargin);
  const policyValid =
    displayedOnMargin.trim() !== "" &&
    displayedOffMargin.trim() !== "" &&
    Number.isFinite(onMargin) &&
    Number.isFinite(offMargin) &&
    onMargin >= 0 &&
    onMargin <= 15 &&
    offMargin > onMargin &&
    offMargin <= 20;

  function saveAutomation(nextEnabled: boolean): void {
    if (!policyValid) return;
    setAutomation.mutate({
      enabled: nextEnabled,
      on_margin_c: onMargin,
      off_margin_c: offMargin,
    });
  }

  return (
    <Card>
      <CardTitle>Dew heater</CardTitle>
      {disconnected ? (
        <p className="text-sm text-fg-muted">Disconnected</p>
      ) : (
        <p className="text-sm text-fg-muted">
          Status: <span className="text-fg">{enabled === true ? "on" : enabled === false ? "off" : "unknown"}</span>
        </p>
      )}
      {!disconnected && heater.data?.note != null && (
        <p className="mt-1 text-xs text-fg-muted">{heater.data.note}</p>
      )}
      {heater.isError && <p className="mt-1 text-xs text-danger">Could not read the dew heater.</p>}
      {heatRecommended && enabled !== true && (
        <p role="alert" className="mt-2 rounded-md border border-warning bg-surface-raised p-2 text-sm text-warning">
          Dew heater recommended: outside air is only {dewMargin.toFixed(1)}°C above the dew point
          ({current?.temp_c?.toFixed(1)}°C / {current?.dew_point_c?.toFixed(1)}°C).
        </p>
      )}
      {heatRecommended && enabled === true && (
        <p className="mt-2 text-xs text-success">
          Dew risk: {dewMargin.toFixed(1)}°C margin · heater is on.
        </p>
      )}
      {forecast.isSuccess && dewMargin === null && (
        <p className="mt-2 text-xs text-fg-muted">Dew risk unknown — current temperature or dew point is unavailable.</p>
      )}
      {forecast.data?.stale_since != null && (
        <p className="mt-1 text-xs text-warning">Weather data is stale; verify conditions outside.</p>
      )}
      <WeatherAttribution source={current?.source} />
      <SegmentedControl className="mt-3" label="Dew heater" semantics="pressed"
        value={enabled === true ? "on" : enabled === false ? "off" : "unknown"}
        options={[{ value: "on", label: "On" }, { value: "off", label: "Off" }]}
        disabled={setHeater.isPending}
        onChange={(state) => { setHeater.mutate({ enabled: state === "on" }); }} />
      {setHeater.isError && (
        <p role="alert" className="mt-2 text-xs text-danger">
          {setHeater.error instanceof Error ? setHeater.error.message : "Could not change the dew heater."}
        </p>
      )}

      <div className="mt-4 border-t border-border pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-sm font-medium text-fg">Automatic weather control</p>
            <p className="text-xs text-fg-muted">
              Uses the nearest fresh forecast. Unknown or stale weather never changes the heater.
            </p>
          </div>
          <Button
            variant={automation.data?.enabled === true ? "primary" : "secondary"}
            aria-pressed={automation.data?.enabled === true}
            disabled={!policyValid || automation.isPending || setAutomation.isPending}
            onClick={() => {
              saveAutomation(automation.data?.enabled !== true);
            }}
          >
            {automation.data?.enabled === true ? "Automatic on" : "Automatic off"}
          </Button>
        </div>

        <div className="mt-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs tracking-wide text-fg-muted uppercase">Turn on at ≤ (°C)</span>
            <input
              aria-label="Dew heater turn-on margin"
              type="number"
              inputMode="decimal"
              step={0.5}
              min={0}
              max={15}
              value={displayedOnMargin}
              onChange={(event) => {
                setOnMarginTouched(true);
                setOnMarginText(event.target.value);
              }}
              className="min-h-11 w-28 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs tracking-wide text-fg-muted uppercase">Turn off at ≥ (°C)</span>
            <input
              aria-label="Dew heater turn-off margin"
              type="number"
              inputMode="decimal"
              step={0.5}
              min={0}
              max={20}
              value={displayedOffMargin}
              onChange={(event) => {
                setOffMarginTouched(true);
                setOffMarginText(event.target.value);
              }}
              className="min-h-11 w-28 rounded-md border border-border-strong bg-surface-raised px-2 text-fg"
            />
          </label>
          <Button
            disabled={
              (!onMarginTouched && !offMarginTouched) || !policyValid || setAutomation.isPending
            }
            onClick={() => {
              saveAutomation(automation.data?.enabled === true);
            }}
          >
            Save thresholds
          </Button>
        </div>
        {!policyValid && (
          <p className="mt-2 text-xs text-warning">
            The turn-off margin must be higher than the turn-on margin.
          </p>
        )}
        {automation.data?.dew_margin_c != null && (
          <p className="mt-2 text-xs text-fg-muted">
            Last evaluation: {automation.data.dew_margin_c.toFixed(1)}°C dew-point margin
            {automation.data.action === "turned_on" ? " · heater turned on" : ""}
            {automation.data.action === "turned_off" ? " · heater turned off" : ""}.
          </p>
        )}
        {automation.data?.note != null && (
          <p className="mt-1 text-xs text-fg-muted">{automation.data.note}</p>
        )}
        {(automation.isError || setAutomation.isError) && (
          <p role="alert" className="mt-2 text-xs text-danger">
            Could not read or save automatic dew-heater settings.
          </p>
        )}
      </div>
    </Card>
  );
}
