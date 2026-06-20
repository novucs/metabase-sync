# metabase-sync

[![PyPI](https://img.shields.io/pypi/v/metabase-sync.svg)](https://pypi.org/project/metabase-sync/)
[![Python](https://img.shields.io/pypi/pyversions/metabase-sync.svg)](https://pypi.org/project/metabase-sync/)
[![CI](https://github.com/novucs/metabase-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/novucs/metabase-sync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Metabase as code. `export`, `plan`, and `apply` your dashboards, cards, collections, snippets and pulses via the regular Metabase REST API. Works with Metabase **OSS** (no Enterprise license required).

```
$ metabase-sync plan

plan ← state/

collections: 0 create, 0 update, 12 skip
cards:       1 create, 1 update, 48 skip
  CREATE  collections/finance/cards/q4-revenue.sql                  Q4 Revenue — SELECT SUM(revenue) FROM finance.q4
  UPDATE  collections/finance/cards/profit-margin.sql               SQL: 23 → 25 lines, +2 −0
dashboards:  1 create, 0 update, 8 skip
  CREATE  collections/finance/dashboards/q4-review/dashboard.yaml   Q4 Review (1 dashcards)

2 create, 1 update, 68 skip
run `metabase-sync apply` to apply this plan.
```

## Why this exists

Metabase's [official serialization](https://www.metabase.com/docs/latest/installation-and-operation/serialization) and Remote Sync are gated behind Enterprise / Pro. If you self-host the open-source edition, the `/api/ee/serialization/*` endpoints return 404 and you have no first-party way to put your dashboards under version control.

This tool wraps the plain REST API and gives you the same workflow without the licence wall:

- One YAML/SQL tree per Metabase instance, suitable for git.
- A `plan` / `apply` loop that reads exactly like Terraform.
- Round-trip determinism: `export` → `apply` → `export` produces zero diff.
- Code-first authoring: drop a new `.sql` and a new `dashboard.yaml`, run `apply`, done. No need to know any Metabase ids ahead of time.

## Install

```bash
uv tool install metabase-sync         # recommended: isolated install
# or
pipx install metabase-sync
# or
pip install metabase-sync
```

## Quickstart

```bash
mkdir my-metabase && cd my-metabase
cat > .env <<EOF
METABASE_URL=https://your-instance.example.com
METABASE_API_KEY=mb_...
EOF
metabase-sync export        # creates ./state/
git init && git add state && git commit -m "import metabase state"
```

Then your day-to-day:

```bash
# Edit a .sql or dashboard.yaml in your editor.
metabase-sync plan          # see exactly what will change
metabase-sync apply         # write changes
```

## Commands

| Command | Purpose |
| --- | --- |
| `metabase-sync export` | Pull the live instance into the on-disk state tree. |
| `metabase-sync plan` | Read-only diff. Prints a per-item report and writes `state/.plan.json`. |
| `metabase-sync apply` | Re-derives the diff and executes it. Idempotent. |
| `metabase-sync apply --only cards` | Restrict to one resource (`collections,snippets,cards,dashboards,pulses`). |
| `metabase-sync index` | Debug: print remote resource counts. |

Both `plan` and `apply` re-fetch the live instance, so you can't apply a stale plan against a moved instance.

## Authoring new cards and dashboards in code

You don't need to know any Metabase ids ahead of time. Create the files; apply allocates the ids and writes them back.

### A new card

`state/collections/finance/cards/q4-revenue.sql`:

```sql
---
entity_id: null
name: Q4 Revenue
description: null
type: question
display: scalar
database: bigquery
parameters: []
visualization_settings: {}
enable_embedding: false
embedding_params: null
cache_ttl: null
archived: false
template_tags: {}
---
---body---
SELECT SUM(revenue) FROM finance.q4
```

Run `plan` to see the CREATE line, then `apply`. The `entity_id: null` becomes the server-assigned nanoid in your local file after apply.

### A new dashboard referencing a new card

`state/collections/finance/dashboards/q4-review/dashboard.yaml`:

```yaml
entity_id: null
name: Q4 Review
description: null
archived: false
auto_apply_filters: true
cache_ttl: null
enable_embedding: false
embedding_params: null
position: null
width: fixed
parameters: []
tabs: []
dashcards:
  - entity_id: null
    card_path: ../../cards/q4-revenue.sql       # path relative to this dashboard dir
    tab_position: null
    row: 0
    col: 0
    size_x: 12
    size_y: 6
    parameter_mappings: []
    visualization_settings: {}
    series: []
```

`card_path` is the file path from this dashboard's directory to the card's `.sql` file. Apply resolves it after creating the card, then writes the new entity_id back to your file.

## On-disk layout

```
state/
  databases/<name>.yaml         # manifest only — name + engine, NO credentials
  snippets/<slug>.sql           # YAML frontmatter + raw SQL body
  collections/
    <slug>/_collection.yaml
           /<nested-slug>/_collection.yaml
                         /cards/<slug>.sql or .yaml
                         /dashboards/<slug>/dashboard.yaml
                                           /cards/<slug>.sql   # dashboard-internal cards
    root/cards/...              # cards directly under the root collection
  pulses/<slug>.yaml            # dashboard subscriptions
```

- Native SQL cards: `.sql` file with YAML frontmatter + raw query body.
- GUI / MBQL cards: `.yaml` file with the full `dataset_query` (classic or MBQL5) inlined.
- Dashcards: reference cards by `card_path` (relative).
- Pulses: reference cards by `card_path`, the target dashboard by `dashboard_path`. Recipients are stored by email.
- Snippets without a collection live in `state/snippets/`; snippets inside a collection live in `state/collections/<...>/snippets/`.
- Personal collections are filtered out.

## Configuration

| Env var | Required | Default | Notes |
| --- | --- | --- | --- |
| `METABASE_URL` | yes | — | e.g. `https://metabase.example.com` |
| `METABASE_API_KEY` | yes | — | Admin API key (see below) |
| `STATE_DIR` | no | `state/` | Relative to CWD |
| `HTTP_TIMEOUT_S` | no | `120` | Per HTTP request (large cards' `result_metadata` recompute can take time) |
| `HTTP_MAX_RETRIES` | no | `3` | Retries on 408 / 429 / 502 / 503 / 504 / connection errors |
| `HTTP_RETRY_BACKOFF_S` | no | `1.0` | Base delay; doubles per retry (1s, 2s, 4s) |

A `.env` file in the current working directory is read automatically.

API keys are minted from the Metabase admin UI (Settings → Admin settings → Authentication → API keys). The key needs admin permissions to fetch + write everything `export` and `apply` touch.

## Exit codes

`plan` and `apply` follow the terraform convention so CI pipelines can fan out:

| Code | Meaning |
| --- | --- |
| 0 | Success and no changes (or apply finished cleanly) |
| 1 | Error (HTTP failure, preflight failure, missing recipient, concurrency drift) |
| 2 | `plan` detected pending changes (informational; not an error) |

## CI/CD recipes

See [`examples/`](examples/) for two GitHub Actions templates:

- [`github-actions-plan-on-pr.yml`](examples/github-actions-plan-on-pr.yml) — comment the plan output on every PR that touches `state/`.
- [`github-actions-apply-on-merge.yml`](examples/github-actions-apply-on-merge.yml) — apply automatically on merge to `main`, gated by an environment approval.

## Round-trip guarantees

- `export` is deterministic: byte-identical output across runs.
- `plan` against a freshly-exported tree reports `nothing to do.`.
- SQL bodies are byte-faithful — trailing whitespace and template-tag UUIDs survive the round-trip.

## Caveats

Before running this on a production Metabase, you should know:

- **Apply overwrites concurrent UI edits unless you re-plan first.** `apply` runs a fresh `plan` and checks the captured `updated_at` for every item it touched. If a UI user has edited an item since you ran `plan`, apply aborts with the list and tells you to re-plan. Pass `--force` to overwrite anyway.
- **Dashboard contents are a full replacement.** Tabs and dashcards are PUT in one go with client-assigned negative temp ids; the server replaces existing rows and allocates new `dashcard.entity_id`s. Two simultaneous applies race; wrap CI in a concurrency group to serialise them.
- **Out-of-scope resources are silently NOT synced.** Alerts, segments, legacy metrics, permissions, users and groups are not part of the state tree. `export` prints a warning if it finds any so you don't discover this in production.
- **Personal collections are excluded.** Cards/dashboards inside a user's personal collection don't appear in the export. Dashboards in shared collections that reference a personal-collection card will fail the reference preflight at plan time, not at apply time.
- **`--delete` is not yet implemented.** Items removed from `state/` are not auto-archived on apply. Archive them through the UI or directly via the API. Passing `--delete` is rejected with exit code 2.

### Pre-apply backup

`apply --backup-dir <path>` re-exports the live instance to `<path>` before mutating. If apply messes something up, `metabase-sync apply --state-dir <path>` rolls forward against the backup.

```bash
metabase-sync apply --backup-dir /tmp/pre-apply-$(date +%Y%m%d-%H%M%S)
```

## Troubleshooting / FAQ

**`401 Unauthorized` on the first call.** Your API key was revoked or you're pointed at the wrong instance. Run `metabase-sync diagnose` — it prints the URL and key length.

**`plan` reports updates I didn't make.** Most often a Metabase version mismatch — server-generated `lib/uuid` values on GUI cards regenerate on every UI save. The tool strips them at diff time; if you still see noise, `metabase-sync export` once to refresh and try again.

**How do I rename a card?** Edit the `name:` in the frontmatter. The `entity_id` stays the same so the rename round-trips cleanly.

**How do I move a card to another collection?** Move the file. The collection that contains the card is determined by its on-disk path; on apply the collection_id is rebound and Metabase moves it.

**How do I delete a card?** Archive it via the Metabase UI for now. The `--delete` flag is not implemented in this release. After archival, re-export and commit.

**`metabase-sync diagnose` to file a bug.** It captures everything we'd ask for in a triage thread. Paste the output into the GitHub issue template.

**My Metabase is on a really old version.** Check the [compatibility band](#compatibility). Versions older than v0.45 are explicitly refused; v0.45–v0.55 will warn but try; v0.55–v0.62 are in our tested band; newer versions warn but proceed.

## Compatibility

| Metabase | Status |
| --- | --- |
| <v0.45 | **Refused** — collection API shape too different |
| v0.45–v0.55 | Warns; proceed at your own risk |
| v0.55–v0.62 | **Tested** in CI integration matrix |
| >v0.62 | Warns; we want to hear about issues |
| `latest` | Non-blocking CI job runs against it to surface upcoming breakage |

We pin `v0.62.2` for the blocking CI job; the matrix in `.github/workflows/ci.yml` runs the same integration suite against `v0.55`, `v0.58`, `v0.60`, and `latest` non-blockingly.

Python: 3.11, 3.12, 3.13.

If something goes wrong, you can `cp -r` the backup over `state/` and re-apply to roll back.

## Security

- The API key is read from the environment (or an `.env` file). Anything that captures the environment — CI logs, shell history (`printenv`), error stack traces from third-party deps that `repr()` settings — can leak it. Tools like [sops](https://github.com/getsops/sops) + `sops exec-env encrypted.env 'metabase-sync apply'` work well.
- HTTPS verification follows httpx's defaults (CA bundle from `certifi`). The tool does not disable cert verification.
- `state/.plan.json` and `state/.last-apply.json` include full SQL bodies — they're written under `state/` and should be `.gitignore`d. The default `.gitignore` snippet:
  ```
  state/.plan.json
  state/.last-apply.json
  ```
- The tool never serialises database connection credentials (`details` is stripped from `databases/<name>.yaml`).

## Limitations

- `--delete` (opt-in archival of items absent from disk) is not yet implemented.
- Permissions, users, and groups are out of scope.
- Alerts, legacy metrics, and segments are not yet supported.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, running tests, and the release process.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This is an unofficial, community-built tool. Not affiliated with or endorsed by Metabase, Inc. "Metabase" is a trademark of Metabase, Inc.
