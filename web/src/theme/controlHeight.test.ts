// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { describe, expect, it } from "vitest";

import appCss from "../index.css?raw";
import tokensCss from "./tokens.css?raw";

describe("editable control height", () => {
  it("keeps field-style controls at the standard responsive button heights", () => {
    expect(appCss).toMatch(/input:not\([\s\S]*select:not\(\[multiple\]\)\s*{[\s\S]*height: 36px;[\s\S]*padding-block: 0;/);
    expect(appCss).toMatch(/data-font-scale="small"[\s\S]*height: 33px;[\s\S]*min-height: 33px;/);
    expect(appCss).toMatch(/data-font-scale="large"[\s\S]*height: 40px;[\s\S]*min-height: 40px;/);
  });

  it("keeps multiline and multiple-selection controls flexible", () => {
    expect(appCss).toMatch(/select\[multiple\],[\s\S]*textarea\s*{\s*min-height: 36px;/);
    expect(appCss).toMatch(/data-font-scale="small"[\s\S]*select\[multiple\],[\s\S]*textarea\s*{\s*min-height: 33px;/);
  });

  it("reduces Small Text spacing and fixed 44px button targets by 25 percent", () => {
    expect(tokensCss).toMatch(/data-font-scale="small"[\s\S]*--spacing: 0\.1875rem;/);
    expect(tokensCss).toMatch(/\.min-h-\\\[44px\\\][\s\S]*min-height: 2\.0625rem;/);
    expect(tokensCss).toMatch(/\.h-\\\[44px\\\][\s\S]*height: 2\.0625rem;/);
    expect(tokensCss).toMatch(/\.w-\\\[44px\\\][\s\S]*width: 2\.0625rem;/);
  });

  it.each(["checkbox", "radio", "range", "hidden", "button", "submit", "reset", "image"])(
    "does not resize %s inputs as editable fields",
    (type) => {
      expect(appCss).toContain(`:not([type="${type}"])`);
    },
  );
});
