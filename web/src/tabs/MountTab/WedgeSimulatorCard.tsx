// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { usePolarDebugStore, type WedgeAxis, type WedgeStepArcsec } from "../../store/polarDebugStore";

const STEPS: readonly { value: WedgeStepArcsec; label: string }[] = [
  { value: 3600, label: "1°" },
  { value: 1800, label: "0.5°" },
  { value: 60, label: "60″" },
  { value: 30, label: "30″" },
  { value: 10, label: "10″" },
];

export function WedgeSimulatorCard() {
  const stepArcsec = usePolarDebugStore((state) => state.stepArcsec);
  const altitudeArcsec = usePolarDebugStore((state) => state.altitudeAdjustmentArcsec);
  const azimuthArcsec = usePolarDebugStore((state) => state.azimuthAdjustmentArcsec);
  const setStep = usePolarDebugStore((state) => state.setStep);
  const move = usePolarDebugStore((state) => state.move);
  const reset = usePolarDebugStore((state) => state.resetWedge);
  function control(axis: WedgeAxis, direction: -1 | 1, label: string) {
    return <Button onClick={() => { move(axis, direction); }}>{label}</Button>;
  }
  return (
    <Card>
      <CardTitle>EQ wedge simulator</CardTitle>
      <p className="text-xs text-warning">Debug simulation only. These controls never move the real mount.</p>
      <label className="mt-3 block text-sm" htmlFor="wedge-step">Step size</label>
      <select
        id="wedge-step"
        className="mt-1 min-h-11 rounded-md border border-border bg-surface px-3"
        value={stepArcsec}
        onChange={(event) => { setStep(Number(event.target.value) as WedgeStepArcsec); }}
      >
        {STEPS.map((step) => <option key={step.value} value={step.value}>{step.label}</option>)}
      </select>
      <p className="mt-3 text-xs text-fg-muted">Simulated adjustments: altitude {altitudeArcsec >= 0 ? "+" : ""}{altitudeArcsec}″ · azimuth {azimuthArcsec >= 0 ? "+" : ""}{azimuthArcsec}″</p>
      <div className="mt-3 grid grid-cols-2 gap-2">
        {control("altitude", 1, "Altitude ↑")}
        {control("altitude", -1, "Altitude ↓")}
        {control("azimuth", -1, "Azimuth ← West")}
        {control("azimuth", 1, "Azimuth → East")}
      </div>
      <Button className="mt-3" onClick={reset}>Reset simulated wedge</Button>
    </Card>
  );
}
