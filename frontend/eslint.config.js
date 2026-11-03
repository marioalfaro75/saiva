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
      // The React Compiler era rules from eslint-plugin-react-hooks 7. Both were
      // warnings for one release so React 19 could land ahead of the nine findings
      // they turned up (8 set-state-in-effect, 1 preserve-manual-memoization). All
      // nine are now fixed where they stood rather than suppressed, so both are
      // errors again and the next instance fails the build.
      //
      // What the nine wanted, in case it comes up again. Five were state mirroring a
      // query, where deriving while rendering is the whole fix: PeriodProvider's
      // period placeholder, the financial-year select, and three forms seeded from
      // fetched data (household, AI, alert thresholds). For a seeded form the
      // derivation has to let an edit in progress win — `null` for untouched, server
      // value showing through — or it clobbers what is being typed, which is the trap
      // the effect was there to avoid. useMediaQuery was an external store copied
      // into state by hand, and is now read with useSyncExternalStore. Auth bootstrap
      // published three requests as three commits, the first of them synchronous; it
      // now publishes one. The layout drawer closes on the breakpoint crossing during
      // render rather than in an effect afterwards. The memoisation finding was
      // neither a leak nor a pointless memo: the compiler cannot preserve a useMemo
      // written below a callback that sets the search params, so moving it above them
      // was the fix.
      //
      // Nothing in this family was relaxed in the end, react-hooks/refs included —
      // that one caught a real finding on the import page during the upgrade.
      "react-hooks/set-state-in-effect": "error",
      "react-hooks/preserve-manual-memoization": "error",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
);
