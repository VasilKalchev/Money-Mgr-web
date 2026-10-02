# Templates and frontend conventions

Jinja2 templates, one per page. `base.html` is the shared layout with nav
and the column-toggle/sort JS helpers. `setup.html`, `gdrive_setup.html` and
`settings.html` handle database provisioning, `sync.html` reviews a merge;
`login.html` (login and first-run admin creation) and `users.html` handle
accounts.

## Security-related rules

- Every new POST form needs `{{ csrf_token() }}` as a `csrf_token` field.
  `base.html` adds `X-CSRF-Token` to every same-origin `fetch`.
- The CSP only allows same-origin resources: keep JS/CSS inline or served by
  the app (see `src/AGENTS.md` for `form-action`).

## Conventions

- No JS framework/build step: plain `<script>` blocks per template, plus
  shared helpers in `base.html` (e.g. `makeSortableRows`).
- Column visibility per table is persisted to `localStorage` per page
  (`mmw_cols_<page>` keys) via a self-registering IIFE reading
  `data-col`/`data-default-hidden` attributes off `<th>` elements. See
  `accounts.html` for the reference implementation.
- Less common controls get `class="adv"` and stay hidden until the shared
  "Advanced" toggle (`toggleAdvanced()` in `base.html`, `mmw_show_advanced`
  in `localStorage`) is on. Add `adv-active` server-side when the control
  holds a non-default value so an applied filter is never invisible.
- Inline edits (`patchField`, `patchAmount`, etc.) PATCH `/api/...` endpoints
  and only ever touch fields in the corresponding `*_FIELDS` allowlist in
  `app.py` (`TRANSACTION_FIELDS`, `ACCOUNT_FIELDS`, `CATEGORY_FIELDS`,
  `BULK_TRANSACTION_FIELDS`).
