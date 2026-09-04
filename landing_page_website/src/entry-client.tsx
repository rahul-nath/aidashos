import { StrictMode } from "react";
import { createRoot, hydrateRoot } from "react-dom/client";

import { App, metadataForPath } from "./App";
import "./styles.css";

const root = document.getElementById("root")!;
const pathname = window.location.pathname;
document.title = metadataForPath(pathname).title;

const app = (
  <StrictMode>
    <App pathname={pathname} />
  </StrictMode>
);

if (root.hasChildNodes()) {
  hydrateRoot(root, app);
} else {
  createRoot(root).render(app);
}
