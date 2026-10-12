// SPDX-License-Identifier: GPL-3.0-only
import { afterEach, describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { act, useRef, useState } from "react";
import { MemoryRouter, NavLink } from "react-router-dom";
import { LocalizedText } from "./jsx-runtime";
import { HelpTab } from "../tabs/Help/HelpTab";
import { LanguagePicker } from "./LanguagePicker";
import { setLanguage, translate } from "./locale";

afterEach(() => { act(() => { setLanguage("en"); }); });
function DraftForm() {
  const [draft, setDraft] = useState("IC 1805");
  const input = useRef<HTMLInputElement>(null);
  return <><LanguagePicker /><MemoryRouter><NavLink to="/live"><LocalizedText value="Capture" /></NavLink></MemoryRouter>
    <label>Target<input ref={input} value={draft} onChange={(e) => { setDraft(e.target.value); }} placeholder="Select target" /></label>
    <select aria-label="Frame type" defaultValue="light"><option value="light">Capture</option><option value="dark">Dark</option></select>
    <button title="Save">Save</button><p><>Every 5 frames</></p><span translate="no">Dark</span><code>Capture</code></>;
}
describe("presentation localization", () => {
  it("switches labels without remounting a draft or changing command values", () => {
    render(<DraftForm />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "IC 1805 panel 2" } });
    fireEvent.change(screen.getByLabelText("Language"), { target: { value: "sk" } });
    expect(screen.getByRole("link", { name: "Snímanie" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Uložiť" })).toHaveAttribute("title", "Uložiť");
    expect(screen.getByRole("textbox")).toBe(input);
    expect(input).toHaveValue("IC 1805 panel 2");
    expect(screen.getByLabelText("Typ snímky")).toHaveValue("light");
    expect(screen.getByRole("option", { name: "Tmavá" })).toHaveValue("dark");
    expect(document.documentElement.lang).toBe("sk");
    expect(screen.getByText("Každých 5 snímok")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Jazyk"), { target: { value: "cs" } });
    expect(screen.getByRole("link", { name: "Snímání" })).toBeInTheDocument();
    expect(input).toHaveValue("IC 1805 panel 2");
    expect(document.documentElement.lang).toBe("cs");
  });
  it("finds translated knowledge-base content with a Czech query", () => {
    act(() => { setLanguage("cs"); });
    render(<MemoryRouter initialEntries={["/help?section=knowledge"]}><HelpTab /></MemoryRouter>);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "soukromí" } });
    expect(screen.getByRole("heading", { name: "Soukromí YouTube a streamovací klíče" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Když selže astrometrické řešení" })).not.toBeInTheDocument();
  });
  it("interpolates only declared messages and preserves parameters", () => {
    expect(translate("Every 5 frames", "sk")).toBe("Každých 5 snímok");
    expect(translate("Outside 20.5 °C", "cs")).toBe("Venku 20.5 °C");
    expect(translate("Saved in /data/$Capture/Dark.", "cs")).toBe("Uloženo v /data/$Capture/Dark.");
    expect(translate("Saved in /data/My  Frames/Dark.", "cs")).toBe("Uloženo v /data/My  Frames/Dark.");
    expect(translate("Backend failure: unexpected 9.1", "sk")).toBe("Backend failure: unexpected 9.1");
  });
  it("preserves technical names, paths, opt-outs and code", () => {
    act(() => { setLanguage("sk"); });
    render(<DraftForm />);
    expect(screen.getByText("Capture", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByText("Dark", { selector: "span" })).toBeInTheDocument();
    for (const text of ["IC 1805", "/data/Capture/Dark.fits", "Unknown firmware 9.123", "ssc.set_heater"]) {
      expect(translate(text, "cs")).toBe(text);
    }
    expect(translate("  Save  ", "sk")).toBe("  Uložiť  ");
  });
});
