// SPDX-License-Identifier: GPL-3.0-only
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import { isTranslatable, translate } from "./locale";
import messages from "./messages.json";

const attributes = new Set(["aria-label", "aria-description", "title", "placeholder", "alt", "label", "layoutLabel", "emptyText", "description"]);
const preserved = new Set(["Photon Commander", "CPU", "RAM", "HFR", "FWHM", "PID", "HFR (px)", "MiB/s", "s", "h", "min", "RA", "Dec", "J2000", "J2000 RA", "J2000 Dec", "arcsec", "″/px", "°C /", "°C).", "s /", ".fits", "mm)", "Alt-az / EQ", "Alt-az only", "b0c4155", "Tailscale Serve", "YouTube Live", "1080p · 10 Mbps", "4K · 30 Mbps", "1 fps", "2 fps", "Met.no", "OpenWeatherMap", "WeatherAPI.com", "Visual Crossing", "h, Dec", "h · Dec", "m · Dec", "° · Az", "flats/<filter>", "darks/singles", "bias/singles", "| FWHM", "OK", "AlignmentMode", "EquatorialSystem", "h /", "· FWHM", "h ·", "°/h", "RA / Dec", "° (N→E)", "epoch JD", "NASA/JPL SBDB", "h)", "/ az", "· az", "· max", ") — a", "alt /", "Copyright © 2026 Miroslav Bakoš."]);
const criticalFiles = [
  "app/PairingGate.tsx", "tabs/Help/BugReportPanel.tsx",
  "tabs/System/SystemTab.tsx", "tabs/Capture/AutofocusChart.tsx", "tabs/Capture/FocuserPanel.tsx",
  "tabs/Calendar/NightReportPanel.tsx", "tabs/Calendar/HistoricalNightPanel.tsx", "tabs/Setup/SetupTab.tsx",
  "tabs/SettingsTab/AccessPanel.tsx", "tabs/SettingsTab/CaptureFolderPanel.tsx",
  "tabs/SettingsTab/DeviceConnectionPanel.tsx", "tabs/SettingsTab/ExportImportPanel.tsx",
  "tabs/SettingsTab/ExternalToolsPanel.tsx", "tabs/SettingsTab/InteropKeyPanel.tsx",
  "tabs/SettingsTab/LocationPanel.tsx", "tabs/SettingsTab/PairingPanel.tsx",
  "tabs/SettingsTab/RemoteAccessPanel.tsx", "tabs/SettingsTab/StreamingSettingsPanel.tsx",
  "tabs/SettingsTab/ToolPathsPanel.tsx", "tabs/SettingsTab/WeatherPanel.tsx",
];

function visibleStrings(source: string, name: string): Set<string> {
  const file = ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const strings = new Set<string>();
  const add = (raw: string) => {
    const decoder = document.createElement("textarea");
    decoder.innerHTML = raw;
    const value = decoder.value.replace(/\s+/g, " ").trim();
    if (value && /[a-z]/i.test(value)) strings.add(value);
  };
  const expressions = (node: ts.Expression): void => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) add(node.text);
    else if (ts.isConditionalExpression(node)) { expressions(node.whenTrue); expressions(node.whenFalse); }
    else if (ts.isParenthesizedExpression(node)) expressions(node.expression);
    else if (ts.isBinaryExpression(node) && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.QuestionQuestionToken].includes(node.operatorToken.kind)) expressions(node.right);
  };
  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) add(node.text);
    if (ts.isJsxAttribute(node) && attributes.has(node.name.getText(file)) && node.initializer) {
      if (ts.isStringLiteral(node.initializer)) add(node.initializer.text);
      else if (ts.isJsxExpression(node.initializer) && node.initializer.expression) expressions(node.initializer.expression);
    }
    if (ts.isJsxExpression(node) && node.expression && !ts.isJsxAttribute(node.parent)) expressions(node.expression);
    if (name.endsWith("SystemTab.tsx") && node.parent?.parent && ts.isFunctionDeclaration(node.parent.parent) && node.parent.parent.name?.text === "volumeTitle" && ts.isReturnStatement(node) && node.expression) expressions(node.expression);
    ts.forEachChild(node, visit);
  };
  visit(file);
  return strings;
}

describe("actual interface translation coverage", () => {
  it("covers static diagnostics, setup, pairing, privacy and settings labels", () => {
    const missing: string[] = [];
    const files = new Set([...criticalFiles, ...readdirSync(resolve(process.cwd(), "src"), { recursive: true }).filter((file): file is string => typeof file === "string" && file.endsWith(".tsx") && !file.includes(".test.") && !file.startsWith("i18n/"))]);
    for (const file of files) {
      const source = readFileSync(resolve(process.cwd(), "src", file), "utf8");
      for (const text of visibleStrings(source, file)) {
        if (!preserved.has(text) && !isTranslatable(text)) missing.push(`${file}: ${text}`);
      }
    }
    expect(missing).toEqual([]);
  });
  it("keeps both locale entries and every interpolation parameter intact", () => {
    const parameters = (text: string) => [...text.matchAll(/\{([A-Za-z]+)\}/g)].map((match) => match[1]).sort();
    for (const [english, locales] of Object.entries(messages)) {
      expect(locales, english).toHaveLength(2);
      for (const translation of locales) {
        expect(translation.trim(), english).not.toBe("");
        expect(parameters(translation), english).toEqual(parameters(english));
      }
    }
  });
  it("decodes JSX entities and follows conditional accessible labels", () => {
    expect([...visibleStrings('<button aria-label={busy ? "Saving…" : "Save &amp; continue"}>Download &amp; install</button>', "fixture.tsx")]).toEqual(["Saving…", "Save & continue", "Download & install"]);
  });
  it("localizes diagnostics and interpolated statuses without altering supplied values", () => {
    expect(translate("RAM used", "sk")).toBe("Využitá RAM");
    expect(translate("Swap used", "cs")).toBe("Využitý odkládací prostor");
    expect(translate("Pair this device", "sk")).toBe("Spárovať toto zariadenie");
    expect(translate("Applied the validated curve minimum at 1204.", "cs")).toBe("Použito ověřené minimum křivky v poloze 1204.");
    expect(translate("Saved in C:\\My Frames\\IC 1805.", "sk")).toBe("Uložené v C:\\My Frames\\IC 1805.");
    for (const value of ["Photon Commander", "IC 1805", "ssc.autofocus", "unknown firmware 9.123", "private-user-token"]) expect(translate(value, "cs")).toBe(value);
  });
});
