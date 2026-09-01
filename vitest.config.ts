import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
export default defineConfig({
  test: {
    // vitest 4 removed vitest.workspace.ts — use test.projects.
    // Do NOT set passWithNoTests here: it would let T-054's filtered verify pass with zero tests.
    projects: [
      { test: { name: "contracts", root: "./packages/contracts", environment: "node",
                include: ["tests/**/*.test.ts","src/**/*.test.ts"] } },
      { test: { name: "pixel", root: "./pixel", environment: "node",
                include: ["tests/**/*.test.ts","src/**/*.test.ts"] } },
      { plugins: [react()], test: { name: "merchant", root: "./apps/merchant", environment: "jsdom",
                setupFiles: ["./app/test-setup.ts"], include: ["app/**/*.test.{ts,tsx}"],
                exclude: ["**/node_modules/**","**/build/**","**/.react-router/**"] } },
      { plugins: [react()], test: { name: "buyer", root: "./apps/buyer", environment: "jsdom",
                setupFiles: ["./app/test-setup.ts"], include: ["app/**/*.test.{ts,tsx}"],
                exclude: ["**/node_modules/**","**/.next/**"] } },
    ],
  },
});
