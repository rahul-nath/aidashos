# aidashos.com

This package builds the public aidashos website.
The homepage carries the product promise and agent setup prompts.
`/quickstart/` and `/docs/` provide setup and reference links; `/about/` describes Rahul Nath's reason for building aidashos.

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

The build emits four prerendered static routes:

```text
dist/index.html
dist/quickstart/index.html
dist/docs/index.html
dist/about/index.html
```

`src/App.tsx` owns the route table, visible content, and metadata.
`scripts/prerender.mjs` renders every route and refuses to leave unresolved metadata tokens.

## Deploying to aidashos.com

The static output is configured for Netlify by `netlify.toml`.
For a repository-connected deployment, the base directory is `landing_page_website`, the build command is `npm run build`, and the publish directory is `dist` relative to the base.
A manual deployment uploads the contents of `dist/`.
`public/_headers` is copied into that output, so the same security and cache headers apply to both deployment paths.

Keep the primary custom domain and canonical URLs under `https://www.aidashos.com/`.
Configure `aidashos.com` as an alias of the primary domain and enable Netlify's managed TLS certificate and HTTPS redirect.
Verify the exact candidate on its Netlify URL before replacing the old DNS records.
Verify both custom hostnames over HTTPS before disabling the previous GitHub Pages deployment.

Every public route is a prerendered directory index, so no single-page-app catch-all rewrite is needed.
Hashed assets have long immutable cache lifetimes; HTML must revalidate to receive new releases.

## Telemetry

Page telemetry is off by default.
Set `VITE_TELEMETRY_ENDPOINT` at build time only when first-party click measurement is intentionally configured.
The local control plane has no AiDashOS hosted backend, while configured frontier CLIs still connect to their providers.
