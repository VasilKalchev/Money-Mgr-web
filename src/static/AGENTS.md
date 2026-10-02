# Static files (PWA)

Icons (`icon.svg` is the source; the PNGs are rendered from it),
`manifest.webmanifest`, `sw.js` and `offline.html`. The manifest and worker
are served from the root (`/manifest.webmanifest`, `/sw.js`, both public).

The worker only caches the offline page: never make it cache pages or
`/api/` responses, they hold financial data and are sent `no-store`.

Tests: `tests/test_pwa.py`.
