import { StrictMode } from "react";
import { renderToString } from "react-dom/server";

import { App } from "./App";

// Consumed by scripts/prerender.mjs after `vite build --ssr`. The page is a
// single static route, so prerendering is one renderToString, not a framework.
export function render(): string {
  return renderToString(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
