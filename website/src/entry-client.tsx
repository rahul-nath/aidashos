import { StrictMode } from "react";
import { createRoot, hydrateRoot } from "react-dom/client";

import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root")!;

// Production HTML is prerendered by scripts/prerender.mjs, so the client
// hydrates. The dev server serves the empty shell, where hydrate would warn.
if (root.hasChildNodes()) {
  hydrateRoot(
    root,
    <StrictMode>
      <App />
    </StrictMode>,
  );
} else {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
