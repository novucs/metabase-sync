# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `export` now works like `apply` in reverse. It makes the files match Metabase, lists the files it will change, and asks first. It marks any file where it would overwrite an edit you haven't applied. Use `--auto-approve` in CI. Files that aren't in Metabase are left as they are.
- `--force` on `plan` and `apply` now only overrides the remaining conflicts. Each conflict says what `--force` does.

### Removed

- `export --force`. Answering yes to the prompt does the same.

### Fixed

- `apply` now makes Metabase match the files, like Terraform. It puts back items edited, archived, or deleted in Metabase, and the files win when both sides changed. `plan` marks each of these. To keep a change made in Metabase, run `export` first. Restoring an older copy of the files, with its state file, now restores the items. After upgrading, run `export` before your first `apply` to keep any changes made in Metabase.

## [0.4.2] - 2026-11-06

### Changed

- Questions and dashboards using tables or fields that no longer exist in the database get their own warning, separate from references to other things Metabase doesn't list.
- While working, the spinner says what metabase-sync is waiting on, with a count such as `Reading dashboards 23/60` or `Applying 3/12`.

### Fixed

- A question whose query uses tables or fields that no longer exist no longer takes a request per missing table or field.

## [0.4.1] - 2026-11-06

### Added

- `init --collection NAME` looks up collections by name or entity id and writes them under `collections:`.
- `unmanaged: archive` or `unmanaged: delete` in `metabase-sync.yaml` removes items that exist in Metabase but were never in the files.

### Changed

- With `collections` set, snippets are no longer managed; set `snippets: true` in `metabase-sync.yaml` to keep them.
- Supports Python 3.14 and older versions of its dependencies.

### Fixed

- A removal that would break something that stays, such as a collection holding a document or a question another file uses, now stops `apply` until it's resolved, even with `--force`, instead of being skipped.

## [0.4.0] - 2026-11-06

A rewrite. Files from 0.3 are not compatible; export into a new directory once.

### Added

- `init`, `status`, `validate`, and `fmt` commands.
- `metabase-sync.state` records each item at the last sync, so `export` keeps your unapplied edits, `apply` leaves changes made in Metabase alone, and edits on both sides stop as a conflict until `--force`.
- `plan` shows a diff per item. `plan --out` with `apply PLANFILE`, `plan --json`, and `export --check`.
- New files need no entity id, and a file for an item already in Metabase is imported instead of duplicated.
- Removing a file archives the item, or deletes it with `removal: delete`.
- `collections` in `metabase-sync.yaml` limits the files to chosen collections.
- An interrupted `apply` finishes on the next run without creating duplicates.
- `apply` fails, naming the fields, when Metabase doesn't keep a change.
- `METABASE_TIMEOUT` and `METABASE_CONCURRENCY`.

### Changed

- Files use the metabase/representations format: one YAML file per item, references by entity id, and databases, tables, and fields by name.
- Commands run in the current directory or in `--chdir DIR`, instead of `--state-dir`.
- Supports Metabase 0.58 and later, tested on 0.58, 0.64, and 0.65. Use 0.3 for 0.55 to 0.57.
- Requires Python 3.15.
- `apply` asks before changing anything, unless given `--auto-approve` or a saved plan.
- `plan` exits 1 on conflicts; `--detailed-exitcode` exits 2 only for changes.

### Removed

- `index`, `diagnose`, `doctor` (use `status`), `apply --only`, `apply --backup-dir`, and the `STATE_DIR`, `HTTP_TIMEOUT_S`, `HTTP_MAX_RETRIES`, and `HTTP_RETRY_BACKOFF_S` variables.

## [0.3.1] - 2026-10-27

### Fixed

- **Native cards export and apply on Metabase v0.64.3+.** Those versions
  return a native stage's `template-tags` as a list instead of a map keyed by
  tag name, which crashed apply. Export now converts the list back to the map,
  so state files keep the same shape across versions.

### Testing

- Unit coverage for list-form template tags (`test_legacy_query.py`); the
  integration suite verified against Metabase v0.64.3.5 and the pinned v0.62.2.

## [0.3.0] - 2026-10-27

### Added

- **`METABASE_HEADERS` sends extra headers on every request**, as a JSON
  object. Use it to reach a Metabase behind an auth proxy such as Google
  Cloud IAP (`{"Authorization": "Bearer <token>"}`).

## [0.2.2] — 2026-07-16

### Fixed

- **Export no longer persists `lib/uuid` (and sibling server bookkeeping)
  inside queries.** Metabase regenerates these MBQL5 values server-side, so
  storing them made re-exports rewrite otherwise-unchanged cards forever. The
  diff already ignored them; writes (export and apply write-backs) now strip
  them from `dataset_query` and `template_tags` too. The first export after
  upgrading removes them from existing state once.

### Testing

- Unit coverage that written card files carry no `lib/uuid` in either form
  (`test_legacy_query.py`); both integration suites (pinned v0.62.2 and
  v0.55.16) verified.

## [0.2.1] — 2026-07-16

### Fixed

- **Pulses no longer plan a perpetual `disable_links: null → False` update on
  Metabase <= v0.55.** Those versions return `disable_links` as null, which
  the 0.2.0 pulse diff compared against the default. A null remote value is
  never diffed against the default, and a null on disk (state exported from
  such a version) is sent as false.

### Testing

- Unit coverage for the null-remote case (`test_pulse_diff.py`); the full
  integration suite verified against Metabase v0.55.16 and the pinned
  v0.62.2.

## [0.2.0] — 2026-07-16

Minor (not patch) because output formatting changes: the first export after
upgrading canonicalizes existing state files once, and previously-undetected
disk edits will surface as plan updates.

### Fixed

- **Every field the apply payload sends is now diffed; disk edits can no
  longer plan as silent skips.** Previously each resource compared only a
  subset of what it PUT, so edits to the uncompared fields never applied:
  - Cards: `visualization_settings` (chart colours/metrics/dimensions),
    `type`, `parameters`, `database_id`, `enable_embedding`,
    `embedding_params`, `cache_ttl`.
  - Dashboards: `parameters`, `width`, `position`, `cache_ttl`,
    `enable_embedding`, `embedding_params`; dashcard tab moves,
    `parameter_mappings`, `visualization_settings`, `series` (by card id) and
    `action_id`. Dashcard `inline_parameters` was exported but silently
    dropped from the PUT payload; it is now sent (only when authored) and
    diffed.
  - Pulses: `parameters`, `cards` (display/CSV/XLS/format/pivot/mappings) and
    `channels` (schedules, enabled, recipients); `disable_links` and
    `archived` were exported but never sent, and are now both sent and
    diffed.
  - Collections: `archived` was exported but never sent; now sent and diffed.
  Container fields treat empty and missing as equal, and a missing remote card
  `type` is never diffed against the default, so unchanged resources still
  plan as skips (no perpetual re-PUTs).
- **Apply write-back no longer corrupts GUI card files.** Stamping an
  entity_id onto a newly created GUI card rewrote its full `dataset_query`
  under the legacy `query:` key, which the reader then double-wrapped on the
  next apply.
- **Deterministic YAML key order on every write.** Exports and apply
  write-backs now share per-resource canonical key templates (unknown keys
  keep their order after the template) and nested mappings are sorted, so
  hand-authored files, write-backs (entity_id no longer lands at the end of
  the frontmatter) and re-exports produce stable, minimal diffs. The first
  export after upgrading canonicalizes existing state once; after that,
  diffs stay clean.

### Testing

- Unit coverage for every new diff (`test_card_diff.py`,
  `test_dashboard_diff.py`, `test_pulse_diff.py`) and for canonical ordering
  (`test_canon.py`); an integration regression proving a
  settings-only disk edit plans as one update, applies, and settles
  (`test_apply_semantics.py::test_settings_only_edit_plans_update_and_settles`).

## [0.1.3] — 2026-07-16

### Fixed

- **Export no longer writes stale SQL over native cards, silently undoing an
  apply.** Metabase's `legacy_query` projection can lag behind the live
  `dataset_query` after an API write, still returning the pre-write SQL. Export
  read `legacy_query` first, so a card that had been updated correctly was
  re-serialised to disk with its *old* SQL: the applied change looked like it had
  never landed, and applying that export would have pushed the old SQL back to
  the server for real. Export now prefers the live `dataset_query` whenever it
  carries SQL, matching the precedence the diff path adopted in 0.1.2, and falls
  back to `legacy_query` only for versions that leave `dataset_query` empty.

### Testing

- Unit coverage that export prefers the live query over a stale `legacy_query`
  (`test_legacy_query.py`), plus an integration regression that applies a SQL
  edit and exports it back to disk (`test_cards.py`). The existing
  server-side assertion could not catch this class of bug: only a round trip
  back to disk can.

## [0.1.2] — 2026-06-28

### Fixed

- **Field-filter (`dimension`) native cards no longer lose their query on
  apply.** Metabase ≥ v0.61 returns a `dimension` template tag's field reference
  in MBQL5 form (`["field", {…}, <id>]`). Re-sending that inside a classic
  native query fails server-side normalisation: v0.62 rejects the write, while
  v0.61 accepts it (HTTP 200) and stores an empty query. Apply now converts
  dimension field references to classic form (`["field", <id>, <opts|null>]`)
  before sending.
- **Apply aborts instead of silently succeeding when a write empties a card.**
  Some Metabase versions answer a query-normalisation failure with HTTP 2xx and
  an empty `dataset_query`. Apply now verifies the stored query is non-empty and
  raises rather than reporting a successful but destructive write.
- **Native cards no longer re-PUT on every apply.** The diff preferred the
  server's `legacy_query`, which can lag behind the live `dataset_query` on some
  versions, so native cards whose live query already matched disk produced a
  perpetual false-positive diff. The diff now prefers the live `dataset_query`
  for native cards.

### Testing

- Unit coverage for dimension-tag normalisation, the live-vs-stale query diff,
  and the empty-query guard (`test_template_tags.py`, `test_card_diff.py`), plus
  an integration regression that a field-filter native card keeps its query
  through a disk-driven SQL update. Verified against Metabase v0.61.3.

## [0.1.1] — 2026-06-22

### Fixed

- **Card SQL edits now apply on Metabase ≥ v0.62.** Cards created/updated via the
  API on newer Metabase return `legacy_query: null`, with the query only in
  `dataset_query` (MBQL5). The diff previously skipped the query comparison when
  `legacy_query` was null, so SQL edits to such cards were silently never
  applied. The diff now falls back to `dataset_query` and compares native SQL
  across both the classic and MBQL5 forms.
- **Snippets in collection folders no longer abort apply.** Metabase keeps
  snippet collections in a separate `:snippets` namespace; sending a normal
  collection id to the snippet endpoint returns a 400 that aborted the whole
  run. Snippets authored under a collection folder now apply to the root
  snippets namespace with a warning instead of crashing. (Proper snippet-folder
  support remains a follow-up.)

### Testing

- Rebuilt the integration suite around shared builders (`_builders.py`) and a
  thin admin client (`_mb.py`), split by resource (export/cards/dashboards/
  snippets/pulses/apply-semantics/cli). 33 integration tests, green against both
  Metabase v0.55.16 and v0.62.2.
- New real-server coverage for the previously mock-only paths: apply
  idempotency + entity_id write-back, native SQL updates, pulses, GUI/MBQL
  cards, optimistic-concurrency drift, both preflights, the schema-version gate,
  dashboard parameters, nested collections, and the full CLI surface.

## [0.1.0] — 2026-06-20

### Commands

- `metabase-sync export` — pull the live Metabase instance into a YAML + SQL state tree under `state/`.
- `metabase-sync plan` — itemised per-resource diff between the on-disk state and the live instance. Writes a human-readable report to stdout and a machine-readable `state/.plan.json` for CI consumption.
- `metabase-sync apply` — re-derives the diff and executes the necessary POST/PUT calls. Idempotent (a second run is a no-op). Writes `state/.last-apply.json` as an audit log.
- `metabase-sync index` — debug command listing remote resource counts.
- `metabase-sync --version` prints the package version.

### Resources covered

- Collections, native and GUI cards (including dashboard-internal cards), dashboards (tabs + dashcards), snippets (with collection routing), pulses (dashboard subscriptions, recipients matched by email), and database manifests (name + engine only — credentials never serialised).
- Personal collections are intentionally excluded.
- Out-of-scope resources (alerts, segments, legacy metrics) produce a warning on export so they aren't silently missed.

### Code-first authoring

- Dashboards and pulses reference cards by relative file path (`card_path: ../../cards/foo.sql`) and dashboards by `dashboard_path` — brand-new items can be authored without knowing any Metabase ids ahead of time. Apply allocates ids and writes the new `entity_id` back into the source file.

### Round-trip stability

- Serialises Metabase's classic `legacy_query` form for native cards and accepts either classic or MBQL5 form for GUI cards. The diff path strips volatile keys (`lib/uuid`, `info`, `lib.convert/converted?`) so MBQL5 cards don't churn across exports.
- SQL bodies are stored byte-faithfully (trailing whitespace, template-tag UUIDs all survive).
- Atomic file writes (tempfile + `os.replace`) — Ctrl+C never leaves a corrupted YAML.

### Production safety

- **HTTP retries with exponential backoff** for connection errors, 408, 429, 502, 503, 504. Configurable via `HTTP_MAX_RETRIES` (default 3) and `HTTP_RETRY_BACKOFF_S` (default 1.0). `HTTP_TIMEOUT_S` default is 120s so `result_metadata` recomputation on large cards doesn't trip.
- **Single PUT for dashboard updates** — metadata + tabs + dashcards in one request, so a dashboard can never be left half-applied.
- **`result_metadata` preserved** for metadata-only card updates so Metabase doesn't re-run the underlying query on every cosmetic edit.
- **Reference preflight** — apply walks every dashboard and pulse and resolves every `card_path` / `dashboard_path` against the filesystem before any HTTP call.
- **Optimistic-concurrency check** — plan captures `updated_at` per item; apply refuses to overwrite anything that changed since plan unless `--force` is set.
- **Pre-apply confirmation prompt** — `apply` re-prints the plan and asks `[y/N]` unless `--yes`, `CI=true`, or stdout is not a TTY.
- **Loud error on missing pulse recipients** — apply errors with the list of bad emails unless `--allow-missing-recipients` is set.
- **`--delete` rejected at CLI parse time** with exit code 2 (planned for a future release).
- **Terraform exit codes**: `plan` exits 2 when changes are detected, 0 when nothing to do, 1 on error.

### Compatibility

- Built and tested against Metabase OSS v0.62.2 (pinned for CI). A non-blocking CI job runs the integration suite against `metabase:latest` to surface upcoming breakage.
- Supports Python 3.11, 3.12, 3.13.

### Known limitations

- `--delete` (opt-in archival of items absent from disk) is not yet implemented.
- Permissions, users, and groups are out of scope.
- Alerts, legacy metrics, and segments are not yet supported (export warns when they exist).
