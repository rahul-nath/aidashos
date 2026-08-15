import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The prompt sequence is imported from ../docs/onboarding/prompts.json so the
// page can never drift from what the repo actually ships. The dev server's
// filesystem sandbox must therefore reach one level above this package.
export default defineConfig({
  plugins: [react()],
  server: {
    fs: {
      allow: [".."],
    },
  },
  build: {
    target: "es2022",
  },
});
