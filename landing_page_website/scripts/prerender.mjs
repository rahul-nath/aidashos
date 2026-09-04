// Render each public route into a static directory index.
// One route table owns the React page, metadata, canonical URL, and output path.

import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const distRoot = path.join(root, "dist");
const templatePath = path.join(distRoot, "index.html");
const marker = "<!--app-html-->";
const titleToken = "__AIDASHOS_PAGE_TITLE__";
const descriptionToken = "__AIDASHOS_PAGE_DESCRIPTION__";
const canonicalToken = "__AIDASHOS_PAGE_CANONICAL__";

const { render, STATIC_ROUTES, canonicalUrlForPath } = await import(
  path.join(root, "dist-ssr", "entry-server.js")
);

const template = await readFile(templatePath, "utf8");
for (const required of [marker, titleToken, descriptionToken, canonicalToken]) {
  if (!template.includes(required)) {
    throw new Error(`dist/index.html is missing required token ${required}`);
  }
}

for (const route of STATIC_ROUTES) {
  const canonical = canonicalUrlForPath(route.path);
  const page = template
    .replace(marker, render(route.path))
    .replaceAll(titleToken, route.title)
    .replaceAll(descriptionToken, route.description)
    .replaceAll(canonicalToken, canonical);
  if (page.includes("__AIDASHOS_PAGE_") || page.includes(marker)) {
    throw new Error(`unresolved prerender token for ${route.path}`);
  }

  const outputPath =
    route.path === "/"
      ? templatePath
      : path.join(distRoot, route.path.slice(1), "index.html");
  await mkdir(path.dirname(outputPath), { recursive: true });
  await writeFile(outputPath, page, "utf8");
  console.log(`prerendered ${path.relative(root, outputPath)}`);
}

const sitemapUrls = STATIC_ROUTES.map(
  (route) => `  <url>\n    <loc>${canonicalUrlForPath(route.path)}</loc>\n  </url>`,
).join("\n");
await writeFile(
  path.join(distRoot, "sitemap.xml"),
  `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${sitemapUrls}\n</urlset>\n`,
  "utf8",
);

await rm(path.join(root, "dist-ssr"), { recursive: true, force: true });
