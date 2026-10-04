# Sync, Drive and backups

Read this before touching `src/dbsync.py`, `src/merge.py`, `src/gdrive.py`
or the backup/install code in `src/dbstore.py`.

## Modules

- `merge.py`: the three-way merge of two `.mmbak` files on a common base:
  row diffing, conflicts, duplicate detection, reference checks, writing the
  result. Pure, no Flask or `Store`.
- `dbsync.py`: sync orchestration on top of `merge.py`, `gdrive.py` and
  `Store`: Drive sync and push, merging an uploaded export, the pending
  review, downloads, and the review page's view model.
- `gdrive.py`: Google OAuth (user's own Desktop-app client; full `drive`
  scope, or `drive.readonly` if the user turns writing off) and Drive I/O:
  list, download, replace a file's content. Plain `requests`. Every function
  takes the user's `Store`.

## Installing a database

There is exactly one working database per user. No picker, no per-tab
`?db=` override. Provenance (`db_source` = `manual`|`gdrive`, `db_info`
with original name, install time, md5, Drive file id) is stored in the
user's `config.json`.

No database yet: `_require_db()` redirects pages to `/setup` (409 JSON for
`/api/`). The user either uploads a `.mmbak` (`/db/upload`) or walks
through Google setup (`/setup/gdrive`). Both paths end in
`Store.install_db()`, which validates, backs up whatever it replaces,
swaps the file in atomically and makes it the sync base.

## Sync

Sync is a three-way merge (`merge.py`) of the app's snapshot into the
working db, on top of `sync/base.mmbak`, the last snapshot taken from the
app. Rows match by `uid` (`ZCATEGORY` by `uid` + `TYPE`), never by the
numeric ids. A merge that brings changes from the app waits in
`sync/incoming.mmbak` + `pending.json` for review on `/sync`, which lists
what each side changed (`dbsync._side_changes()`) and any conflicts,
likely duplicates or broken references to settle. Applying it goes through
`Store.install_merged()`, which backs up the working db with the incoming
file alongside. A snapshot that changes nothing is applied right away.

- Drive (`/db/sync` -> `dbsync.sync_gdrive()`): the newest `MM*.mmbak`
  (by `modifiedTime`) is merged, then the result replaces that file's
  content on Drive, keeping its name and id, since the app only offers its
  own backups for restoring (`dbsync.push()`). Nothing new on Drive but
  local changes: push them. `replace=1` installs the snapshot instead.
- Manual (`/db/sync/upload` -> `dbsync.sync_file()`); the result comes
  back to the app via `/db/download`.
- Every pushed or downloaded state is kept in `sync/pushed/` as another
  base candidate: if the user restored it in the app, the app's next
  backup descends from it. `merge.choose_base()` picks the closest
  candidate, and the base on a tie, since a wrong pushed base would read
  the app's backup as reverting local changes.
- Installs from before sync have no base: `_ensure_base()` uses the
  unedited working db or re-downloads the installed Drive file; failing
  both, the merge is two-way (every differing row is a conflict).
- `Store.unsynced()`: the working db has changes the app (Drive) lacks.
- Columns that hold one value between them (`merge.COLUMN_GROUPS`:
  `WDATE` + `ZDATE`) merge as a unit: both come from the same side, and a
  conflict covers the whole group.
- MMW's change times: a synced row keeps the app's `UTIME`, which can be
  from before the sync, so `changes.sqlite` (beside the user's `db/`)
  records when MMW saw each transaction change. `Store._swap_in()` stamps
  every row that differs between the working db and the one replacing it
  (sync, replace, upload), and `app.write_db()` stamps the rows an edit
  writes. The API's `updated_ms`/`updated_since` read it, falling back to
  `UTIME`. It lives outside the MM file, so it never reaches the app.

## OAuth

The redirect is the app's own `/setup/gdrive/callback` when reached
via localhost, otherwise the user pastes the failed-redirect URL into
`/setup/gdrive/paste`. The granted scope is saved; `gdrive.can_write()`
checks it (older sign-ins were read-only and need a reconnect).

## Backups

Backups live in the user's `backups/<stamp>/` (copy + `diff.txt`), taken
by `Store.backup_once()` before the first write after startup/install and
by `install_db()`/`install_merged()` before replacing a file (a merge also
keeps the app's file there as `incoming.mmbak`). `Store.backup_settings()`
merges the `backups` key of `config.json` (`keep`, `on_first_write`,
`diff`; set in Settings) over `BACKUP_DEFAULTS` (`keep` defaults to
`MMW_BACKUP_KEEP`, 30). The replace-time backup can't be turned off.

Tests: `tests/test_merge.py`, `tests/test_sync.py`, `tests/test_gdrive.py`,
`tests/test_dbstore.py`.
