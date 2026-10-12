// SPDX-License-Identifier: GPL-3.0-only
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { LanguagePicker } from "../../i18n/LanguagePicker";
import { AlpacaDevicesPanel } from "../Capture/AlpacaDevicesPanel";
import { DeviceConnectionPanel } from "../SettingsTab/DeviceConnectionPanel";
import { PlanningEquipmentPanel } from "../SettingsTab/PlanningEquipmentPanel";
import { LocationPanel } from "../SettingsTab/LocationPanel";
import { HorizonPanel } from "../SettingsTab/HorizonPanel";
import { CaptureFolderPanel } from "../SettingsTab/CaptureFolderPanel";
import { ExternalToolsPanel } from "../SettingsTab/ExternalToolsPanel";
import { SetupDownloads } from "./SetupDownloads";
import { SetupEphemeris } from "./SetupEphemeris";
import { SetupScreenshot } from "./SetupScreenshot";
import { setupScreenshots } from "./setupAssets";
import { useSetupCopy } from "./setupCopy";
export const SETUP_STORAGE_KEY = "ssc.setup.v1";
const key = SETUP_STORAGE_KEY;
interface Progress { step: number; reviewed: number[] }
function readProgress(): Progress {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) ?? "null");
    if (value && typeof value === "object" && "step" in value && "reviewed" in value && typeof value.step === "number" && Number.isInteger(value.step) && value.step >= 0 && value.step <= 8 && Array.isArray(value.reviewed)) {
      return { step: value.step, reviewed: [...new Set(value.reviewed.filter((item): item is number => typeof item === "number" && Number.isInteger(item) && item >= 0 && item < 8))] };
    }
  } catch { /* Locked or invalid storage leaves setup usable. */ }
  return { step: 0, reviewed: [] };
}
export function SetupLink() {
  const text = useSetupCopy();
  return <a href="#/setup" className="inline-flex min-h-11 items-center text-link underline">{text.open}</a>;
}

export function SetupTab() {
  const text = useSetupCopy();
  const [progress, setProgress] = useState(readProgress);
  const heading = useRef<HTMLHeadingElement>(null);
  const step = progress.step;
  const reviewed = progress.reviewed.includes(step);
  useEffect(() => { heading.current?.focus(); }, [step]);
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(progress)); } catch { /* Session remains usable. */ } }, [progress]);
  function move(next: number) { setProgress((current) => ({ ...current, step: next })); }
  return <div className="mx-auto w-full max-w-4xl space-y-4">
    <h1 className="text-2xl font-semibold">{text.title}</h1>
    <aside aria-labelledby="setup-firmware-warning" className="rounded-lg border-2 border-warning bg-surface-raised p-4 text-fg">
      <p id="setup-firmware-warning" className="mb-2 text-base font-semibold text-warning">{text.firmwareTitle}</p>
      <p className="text-sm leading-relaxed">{text.firmwareWarning}</p>
    </aside>
    <p className="text-sm text-fg-muted">{text.storage}</p>
    <nav aria-label={text.title} className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {text.steps.map((title, index) => <Button key={title} aria-current={step === index ? "step" : undefined} onClick={() => { move(index); }} className="justify-start text-left">{index + 1}. {title}{progress.reviewed.includes(index) ? " ✓" : ""}</Button>)}
    </nav>
    <Card>
      <h2 ref={heading} tabIndex={-1} className="text-xl font-semibold">{step < 8 ? `${String(step + 1)}/8 · ${text.steps[step] ?? ""}` : progress.reviewed.length === 8 ? text.done : text.pending}</h2>
      {step < 8 && <p className="my-3 text-sm text-fg-muted">{text.descriptions[step]}</p>}
      <div className="space-y-4">
        {setupScreenshots[step] && <SetupScreenshot asset={setupScreenshots[step]} />}
        {step === 0 && <><LanguagePicker /><SetupDownloads /></>}
        {step === 1 && <><p>{text.network}</p><a className="inline-flex min-h-11 items-center text-link underline" href="https://www.seestar.com/blogs/tutorial/use-station-mode-screen-mirroring-seestar" target="_blank" rel="noreferrer">{text.guide}</a></>}
        {step === 2 && <><p>{text.alpaca}</p><AlpacaDevicesPanel /></>}
        {step === 3 && <DeviceConnectionPanel />}
        {step === 4 && <PlanningEquipmentPanel />}
        {step === 5 && <><LocationPanel /><HorizonPanel /></>}
        {step === 6 && <CaptureFolderPanel />}
        {step === 7 && <><SetupDownloads /><SetupEphemeris /><ExternalToolsPanel /></>}
      </div>
      {step < 8 && <><p className="my-4 text-sm text-fg-muted">{text.save}</p><label className="flex min-h-11 items-center gap-3"><input type="checkbox" checked={reviewed} onChange={(event) => { const checked = event.target.checked; setProgress((current) => ({ ...current, reviewed: checked ? [...current.reviewed.filter((item) => item !== step), step] : current.reviewed.filter((item) => item !== step) })); }} />{text.ack}</label></>}
      {step === 8 && <ul className="my-4 space-y-2">{text.steps.map((title, index) => <li key={title}><button type="button" className="min-h-11 text-link underline" onClick={() => { move(index); }}>{progress.reviewed.includes(index) ? "✓ " : "○ "}{title}</button></li>)}</ul>}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button disabled={step === 0} onClick={() => { move(step - 1); }}>{text.back}</Button>
        {step < 8 && <><Button variant="primary" disabled={!reviewed} onClick={() => { move(step + 1); }}>{step === 7 ? text.finish : text.next}</Button><Button onClick={() => { move(step + 1); }}>{text.skip}</Button></>}
      </div>
    </Card>
    <Link to="/dashboard" className="inline-flex min-h-11 items-center text-link underline">{text.resume}</Link>
  </div>;
}
