import { RuleTester } from "eslint";
import { describe, it } from "vitest";
import rule from "./no-literal-color.js";

describe("local/no-literal-color", () => {
  it("runs the ESLint RuleTester suite", () => {
    // RuleTester itself uses Mocha-style `describe`/`it` globals when it can
    // find them; vitest's globals are compatible, so this nested run reports
    // through vitest rather than needing a second test runner.
    const tester = new RuleTester({
      languageOptions: { ecmaVersion: 2023, sourceType: "module" },
    });

    tester.run("no-literal-color", rule, {
      valid: [
        "const x = 'var(--ssc-fg)'",
        "const cls = 'bg-surface text-fg border-border-strong'",
        "const cls = 'bg-accent-muted text-accent'",
        "const theme = 'red'",
        "const theme = 'dark'",
        "const id = '#main'",
        "const label = 'to the store'",
      ],
      invalid: [
        { code: "const x = '#ffffff'", errors: 1 },
        { code: "const x = '#fff'", errors: 1 },
        { code: "const x = 'rgb(255, 255, 255)'", errors: 1 },
        { code: "const x = 'hsl(0, 0%, 100%)'", errors: 1 },
        { code: "const cls = 'bg-white'", errors: 1 },
        { code: "const cls = 'text-red-500'", errors: 1 },
        { code: "const cls = 'border-gray-800'", errors: 1 },
        { code: "const cls = 'text-[white]'", errors: 1 },
      ],
    });
  });
});
