// Inject the server-rendered app into the built index.html.
//
// SEO is the reason the page is prerendered at all: the full content is in the
// HTML for every crawler and for first paint, and React hydrates on top. Run
// after both client and SSR builds; `npm run build` sequences the three.

import { readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const indexPath = path.join(root, "dist", "index.html");
const marker = "<!--app-html-->";

const { render } = await import(path.join(root, "dist-ssr", "entry-server.js"));

const html = await readFile(indexPath, "utf8");
if (!html.includes(marker)) {
  throw new Error(`dist/index.html is missing the ${marker} marker`);
}
await writeFile(indexPath, html.replace(marker, render()), "utf8");
await rm(path.join(root, "dist-ssr"), { recursive: true, force: true });
console.log("prerendered dist/index.html");
