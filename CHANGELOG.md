# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
