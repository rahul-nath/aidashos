# aidashos.com

This package builds the public aidashos website.
The homepage carries the short product promise, while `/quickstart/` and `/docs/` own setup and reference links.

## Single-source onboarding

The clone command and agent prompt sequence come from [../docs/onboarding/prompts.json](../docs/onboarding/prompts.json).
`tests/test_onboarding_prompts.py` pins that document to the scripts it names.
The hosted quickstart therefore cannot describe a setup lane the repository does not ship.

## Develop and build

```bash
cd landing_page_website
npm run dev
npm run build
npm run preview
```

The build emits three prerendered static routes:

```text
dist/index.html
dist/quickstart/index.html
dist/docs/index.html
```

`src/App.tsx` owns the route table, visible content, and metadata.
`scripts/prerender.mjs` renders every route and refuses to leave unresolved metadata tokens.

## Deploying to aidashos.com

The production site is served by GitHub Pages from the derived `gh-pages` branch.

- Build from this directory.
- Publish the contents of `dist/` at the branch root without editing generated files.
- Keep the Pages custom domain and every canonical URL under `https://www.aidashos.com/`.
- Treat hosting, DNS, and deployment changes as separate operator decisions.

## Telemetry

Page telemetry is off by default.
Set `VITE_TELEMETRY_ENDPOINT` at build time only when first-party click measurement is intentionally configured.
The local control plane has no AiDashOS hosted backend, while configured frontier CLIs still connect to their providers.
