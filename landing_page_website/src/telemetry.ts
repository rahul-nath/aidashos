// Funnel telemetry, honest by construction.
//
// The OS this page describes reports nothing, so the page must not undercut
// that claim silently. Both sinks below are off until the site owner turns one
// on: the Plausible snippet in index.html is shipped commented out, and the
// beacon endpoint only exists when VITE_TELEMETRY_ENDPOINT is set at build
// time. Events carry an event name and nothing else about the visitor.

type PlausibleFn = (event: string, options?: { props?: Record<string, string> }) => void;

declare global {
  interface Window {
    plausible?: PlausibleFn;
  }
}

const endpoint: string | undefined = import.meta.env.VITE_TELEMETRY_ENDPOINT;

export function track(event: string, props?: Record<string, string>): void {
  try {
    window.plausible?.(event, props ? { props } : undefined);
    if (endpoint) {
      const payload = JSON.stringify({ event, props: props ?? {}, ts: Date.now() });
      navigator.sendBeacon?.(endpoint, new Blob([payload], { type: "application/json" }));
    }
  } catch {
    // Telemetry must never break the page.
  }
}
