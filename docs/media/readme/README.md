# README media

## Cockpit preview

`cockpit-demo.gif` records the actual public frontend at commit `55bde9d7ad4490c88e606502b54266557f1aa06c` with synthetic data adapted from `web/e2e/work-unit-cockpit.spec.ts`.
It was captured on September 28, 2026.
Every frame labels it **Example data · UI preview**.
The five views show the blocker, lifecycle, approval request, evidence and milestone explanation.
This is a UI demonstration, not evidence of a completed live execution.
`cockpit-preview.png` is the static opening frame.

The frontend ran by itself, with browser requests intercepted using the public test fixtures.
External requests, unknown API requests and non-GET requests were rejected.
The capture recorded 56 GET requests, no unexpected requests, no page errors and no mutation requests.
It did not start a backend, connect to a ledger, invoke providers or approve work.

The GIF is 1060 by 680 pixels, loops after 19.2 seconds and occupies 206,245 bytes.
Its SHA-256 is `f42b8ba9308ac3ec6c51ec7c92c8b7003c3bde299af15bff94aa1b55bc64d397`.

## Badges

The SVG badges are local assets with accessible names and no remote image dependency.
The green badges summarize the September 12, 2026 snapshot validation published in [PR #8](https://github.com/rahul-nath/aidashos/pull/8).
They do not query GitHub Actions or assert that subsequent commits passed those checks.
When recording newer validation, update the date, commit, linked evidence and badges together.
