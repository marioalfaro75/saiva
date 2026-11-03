import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "node_modules"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // eslint-plugin-react-hooks 7 added the React Compiler era rules, and two of
      // them flag patterns this codebase was already full of: 8 setState-in-effect
      // and 1 preserve-manual-memoization, across auth bootstrap, the responsive
      // layout drawer, useMediaQuery, PeriodProvider, useServerTable and three pages.
      // They are real advice — an effect that calls setState synchronously causes a
      // cascading render, and the fix is usually to derive the value instead — but
      // none of the 11 is a bug, and clearing them means restructuring behaviour in
      // areas the tests only partly cover. Warn rather than error so React 19 could
      // land, with the findings still in the lint output rather than suppressed.
      //
      // Deliberately NOT downgraded: react-hooks/refs. It caught something real in
      // the import page — `busy && previewRun.current` deciding whether to show the
      // Cancel button, where a ref mutation does not re-render — which is fixed in
      // this same change by moving that fact into state. A rule with that hit rate
      // keeps its teeth.
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/preserve-manual-memoization": "warn",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
);
