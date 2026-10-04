# Templates and frontend conventions

Jinja2 templates, one per page. `app.html` is the app at `/`: a standalone
shell (not `base.html`) whose screens `static/app/app.js` renders from
`/api/app/`; see "The app" below. `base.html` is the editor's shared layout
with nav and the column-toggle/sort JS helpers. `setup.html`, `gdrive_setup.html` and
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
  naming an MM column, which must be in the matching `*_FIELDS` map in
  `app.py` (`TRANSACTION_FIELDS`, `ACCOUNT_FIELDS`, `CATEGORY_FIELDS`). The
  route maps it to a field in `src/edits.py`, which checks the value. Bulk
  edits stage one PATCH per row.

## The app (`app.html`, `static/app/`)

- One page, plain JS, no build step. Routes are in the URL hash (see the
  comment at the top of `app.js`); the form and the pages over the main
  screen are history entries, so Android's Back closes them.
- Render with the `h` tagged template, which escapes every value; wrap
  only trusted markup (icons, other `h` results) in `raw()`.
- Its layout follows Money Manager's (sizes were measured on a phone running
  it; px read as Android dp), but the look is MMW's own: the icons are drawn
  for MMW and the palette (CSS variables at the top of `app.css`, the pie
  colours in `app.js`) is MMW's, not sampled from the app. Keep it that way.
- It writes through `/api/app/` with the `X-CSRF-Token` header from its
  `app-config` JSON, and only sends the fields that changed on an edit.
