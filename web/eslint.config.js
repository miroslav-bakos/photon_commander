import js from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";
import noLiteralColor from "./eslint-rules/no-literal-color.js";

export default tseslint.config(
  {
    ignores: ["dist", "scratch", "src/api/schema.d.ts", "openapi.json", "coverage"],
  },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      ...tseslint.configs.strictTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
      jsxA11y.flatConfigs.recommended,
    ],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
      local: { rules: { "no-literal-color": noLiteralColor } },
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      "local/no-literal-color": "error",
      // Crashes on openapi-fetch's generic overloaded call signatures
      // (typescript-eslint 8.68 + TS 5.7): https://github.com/typescript-eslint/typescript-eslint — not
      // a rule this codebase relies on; disabled rather than worked around.
      "@typescript-eslint/unified-signatures": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // Every REST/WS boundary type comes from the generated schema; `any`
      // there hides exactly the drift generation exists to prevent.
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    // Config and test-setup files run under Node, not the browser, and are not
    // part of the component tree the colour rule protects.
    files: ["*.config.{ts,js}", "scripts/**/*.js"],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
    rules: { "local/no-literal-color": "off" },
  },
  {
    files: ["src/test/**/*.{ts,tsx}", "**/*.test.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
  {
    // The rule's own fixtures are colour literals by design, and
    // `eslint.RuleTester`'s untyped rule-object API is what it is.
    files: ["eslint-rules/**/*.test.ts"],
    rules: {
      "local/no-literal-color": "off",
      "@typescript-eslint/no-unsafe-argument": "off",
    },
  },
  {
    // `withAlpha` is a hex-string -> rgba-string transform (`roleStyle.ts`'s
    // own doc comment); its test literally is colour-string arithmetic —
    // input/output fixtures, never a rendered UI colour — which is what this
    // rule otherwise protects against.
    files: ["src/skymap/frames/shared/roleStyle.test.ts"],
    rules: { "local/no-literal-color": "off" },
  },
);
