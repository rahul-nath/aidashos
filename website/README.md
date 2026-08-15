# aidashos.com

The landing page: a single-route React app whose one job is the top of the funnel.
It shows the clone command, the boot command, and the three agent prompts, and it counts nothing except its own button clicks.

## Single-source content

The clone command and the prompt sequence are imported at build time from [../docs/onboarding/prompts.json](../docs/onboarding/prompts.json).
`tests/test_onboarding_prompts.py` (repo root) pins that file to the scripts it names, so the page cannot describe a funnel the repo does not ship.

## Develop and build

```bash
cd website
npm install
npm run dev        # local dev server
npm run build      # client build + SSR build + prerender into dist/
npm run preview    # serve the built dist/
```

The build prerenders the full page into `dist/index.html` (see `scripts/prerender.mjs`), so crawlers and first paint get complete HTML and React hydrates on top.
`dist/` is a fully static site: deploy it to any static host.

## Deploying to aidashos.com

Any static host works; Cloudflare Pages and Netlify are the obvious ones.

- Build command: `npm run build`, output directory: `dist`, root directory: `website`.
- Point `aidashos.com` and `www.aidashos.com` at the host and set the canonical to `https://www.aidashos.com/` (already in `index.html`).

## Telemetry

Off by default, in both directions.

- Page analytics: `index.html` ships a commented-out Plausible snippet.
  Create the site in Plausible (or a self-hosted instance), uncomment, done.
  The custom events arrive automatically: `copy_clone`, `copy_boot`, `copy_prompt_boot`, `copy_prompt_first-run`, `copy_prompt_attach-tool`, and `click_github` with a `where` prop.
- First-party beacons: set `VITE_TELEMETRY_ENDPOINT=https://your-endpoint` at build time and the same events also POST as JSON beacons with no visitor identifiers.
- GitHub's own clone counts: `gh api repos/rahul-nath/aidashos/traffic/clones` shows the trailing 14 days, which complements the click counts here.

The OS itself reports nothing; keep this page holding that line (the footer says so out loud).
