import { StrictMode } from "react";
import { renderToString } from "react-dom/server";

import { App, STATIC_ROUTES, canonicalUrlForPath } from "./App";

export { STATIC_ROUTES, canonicalUrlForPath };

export function render(pathname: string): string {
  return renderToString(
    <StrictMode>
      <App pathname={pathname} />
    </StrictMode>,
  );
}
